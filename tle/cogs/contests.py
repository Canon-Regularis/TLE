import asyncio
import contextlib
import datetime as dt
import functools
import json
import logging
import time
from collections import defaultdict, namedtuple
from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from matplotlib import pyplot as plt

from tle.access.cog import RATED_VC_STAFF_WARNING, RATED_VC_WARNING
from tle.util import (
    codeforces_api as cf,
    codeforces_common as cf_common,
    db,
    discord_common,
    events,
    graph_common as gc,
    paginator,
    ranklist as rl,
    table,
    tasks,
)
from tle.util.cache import CacheError, ContestNotFound, RanklistNotMonitored

_CONTESTS_PER_PAGE = 5
_CONTEST_PAGINATE_WAIT_TIME = 5 * 60
_STANDINGS_PER_PAGE = 15
_STANDINGS_PAGINATE_WAIT_TIME = 2 * 60
_FINISHED_CONTESTS_LIMIT = 5
_WATCHING_RATED_VC_WAIT_TIME = 5 * 60  # seconds
_RATED_VC_EXTRA_TIME = 10 * 60  # seconds
_MIN_RATED_CONTESTANTS_FOR_RATED_VC = 50

NO_REMINDERS_TEXT = "Contest reminders aren't set up in this server."
REMINDER_CHANNEL_GONE_TEXT = (
    'The channel for contest reminders no longer exists. Ask an admin to set '
    'reminders up again.'
)
REMINDER_ROLE_GONE_TEXT = (
    'The role that contest reminders ping no longer exists. Ask an admin to set '
    'reminders up again.'
)
# The reminder role is one that members give themselves, so the bot refuses
# roles that it must not hand out (see discord_common.self_assignable_problem),
# and takes it away only if that can't raise their rights
# (self_removable_problem).
UNSUITABLE_REMINDER_ROLE_TEXT = (
    "That role can't be the reminder role: {problem}. Members give it to "
    'themselves with `/remind on`, so choose a role just for pings.'
)
UNASSIGNABLE_REMINDER_ROLE_TEXT = (
    "I can't change who has the reminder role: {problem}. Ask an admin to "
    'choose a role just for pings.'
)
RATED_VC_CHANNEL_SET_TEXT = 'This is now the rated virtual contest channel.'


class ContestCogError(commands.CommandError):
    pass


def _contest_start_time_format(contest: Any, tz: dt.timezone) -> str:
    start = dt.datetime.fromtimestamp(contest.startTimeSeconds, tz)
    return f'{start.strftime("%d %b %y, %H:%M")} {tz}'


def _contest_duration_format(contest: Any) -> str:
    duration_days, duration_hrs, duration_mins, _ = cf_common.time_format(
        contest.durationSeconds
    )
    duration = f'{duration_hrs}h {duration_mins}m'
    if duration_days > 0:
        duration = f'{duration_days}d ' + duration
    return duration


def _get_formatted_contest_desc(
    id_str: str, start: str, duration: str, url: str, max_duration_len: int
) -> str:
    em = '\N{EN SPACE}'
    sq = '\N{WHITE SQUARE WITH UPPER RIGHT QUADRANT}'
    desc = f'`{em}{id_str}{em}|{em}{start}{em}|{em}{duration.rjust(max_duration_len, em)}{em}|{em}`[`link {sq}`]({url} "Link to contest page")'  # noqa: E501
    return desc


def _get_embed_fields_from_contests(contests: Sequence[Any]) -> list[tuple[str, str]]:
    infos = [
        (
            contest.name,
            str(contest.id),
            _contest_start_time_format(contest, dt.timezone.utc),
            _contest_duration_format(contest),
            contest.register_url,
        )
        for contest in contests
    ]

    max_duration_len = max(len(duration) for _, _, _, duration, _ in infos)

    fields = []
    for name, id_str, start, duration, url in infos:
        value = _get_formatted_contest_desc(
            id_str, start, duration, url, max_duration_len
        )
        fields.append((name, value))
    return fields


async def _send_reminder_at(
    channel: discord.TextChannel,
    role: discord.Role,
    contests: list[Any],
    before_secs: int,
    send_time: float,
) -> None:
    delay = send_time - time.time()
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    values = cf_common.time_format(before_secs)

    def make(value: int, label: str) -> str:
        tmp = f'{value} {label}'
        return tmp if value == 1 else tmp + 's'

    labels = 'day hr min sec'.split()
    before_str = ' '.join(
        make(value, label)
        for label, value in zip(labels, values, strict=False)
        if value > 0
    )
    desc = f'About to start in {before_str}'
    embed = discord_common.cf_color_embed(description=desc)
    for name, value in _get_embed_fields_from_contests(contests):
        embed.add_field(name=name, value=value)
    # The bot pings no role unless a message allows it: this one pings the
    # reminder role, and nobody else.
    mentions = discord.AllowedMentions(roles=[role], everyone=False, users=False)
    await channel.send(role.mention, embed=embed, allowed_mentions=mentions)


class Contests(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot

        self.future_contests: list[Any] | None = None
        self.active_contests: list[Any] | None = None
        self.finished_contests: list[Any] | None = None
        self.start_time_map: defaultdict[int, list[Any]] = defaultdict(list)
        self.task_map: defaultdict[int, list[asyncio.Task[None]]] = defaultdict(list)

        self.member_converter: commands.MemberConverter = commands.MemberConverter()
        self.role_converter: commands.RoleConverter = commands.RoleConverter()

        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    async def _get_ongoing_vc_participants(self) -> set[str]:
        """Returns `member_id` of users who are registered in an ongoing vc."""
        ongoing_vc_ids = await self.bot.user_db.get_ongoing_rated_vc_ids()
        ongoing_vc_participants = set()
        for vc_id in ongoing_vc_ids:
            vc_participants = set(await self.bot.user_db.get_rated_vc_user_ids(vc_id))
            ongoing_vc_participants |= vc_participants
        return ongoing_vc_participants

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self) -> None:
        assert isinstance(self._update_task, tasks.Task)
        self._update_task.start()
        assert isinstance(self._watch_rated_vcs_task, tasks.Task)
        self._watch_rated_vcs_task.start()

    @tasks.task_spec(
        name='ContestCogUpdate',
        waiter=tasks.Waiter.for_event(events.ContestListRefresh),
    )
    async def _update_task(self, _: Any) -> None:
        contest_cache = self.bot.cf_cache.contest_cache
        self.future_contests = contest_cache.get_contests_in_phase('BEFORE')
        self.active_contests = (
            contest_cache.get_contests_in_phase('CODING')
            + contest_cache.get_contests_in_phase('PENDING_SYSTEM_TEST')
            + contest_cache.get_contests_in_phase('SYSTEM_TEST')
        )
        self.finished_contests = contest_cache.get_contests_in_phase('FINISHED')

        # Future contests already sorted by start time.
        self.active_contests.sort(key=lambda contest: contest.startTimeSeconds)
        self.finished_contests.sort(key=lambda contest: contest.end_time, reverse=True)
        # Keep most recent _FINISHED_LIMIT
        self.finished_contests = self.finished_contests[:_FINISHED_CONTESTS_LIMIT]

        self.logger.info('Refreshed cache')
        self.start_time_map.clear()
        for contest in self.future_contests:
            if not cf_common.is_nonstandard_contest(contest):
                # Exclude non-standard contests from reminders.
                self.start_time_map[contest.startTimeSeconds].append(contest)
        await self._reschedule_all_tasks()

    async def _reschedule_all_tasks(self) -> None:
        for guild in self.bot.guilds:
            await self._reschedule_tasks(guild.id)

    async def _reschedule_tasks(self, guild_id: int) -> None:
        for task in self.task_map[guild_id]:
            task.cancel()
        self.task_map[guild_id].clear()
        self.logger.info(f'Tasks for guild {guild_id} cleared')
        if not self.start_time_map:
            return
        try:
            settings = await self.bot.user_db.get_reminder_settings(guild_id)
        except db.DatabaseDisabledError:
            return
        if settings is None:
            return
        channel_id, role_id, before = settings
        channel_id, role_id, before = int(channel_id), int(role_id), json.loads(before)
        guild = self.bot.get_guild(guild_id)
        channel, role = guild.get_channel(channel_id), guild.get_role(role_id)
        for start_time, contests in self.start_time_map.items():
            for before_mins in before:
                before_secs = 60 * before_mins
                task = asyncio.create_task(
                    _send_reminder_at(
                        channel, role, contests, before_secs, start_time - before_secs
                    )
                )
                self.task_map[guild_id].append(task)
        self.logger.info(
            f'{len(self.task_map[guild_id])} tasks scheduled for guild {guild_id}'
        )

    @staticmethod
    def _make_contest_pages(
        contests: list[Any], title: str
    ) -> list[tuple[str, discord.Embed]]:
        pages = []
        chunks = paginator.chunkify(contests, _CONTESTS_PER_PAGE)
        for chunk in chunks:
            embed = discord_common.cf_color_embed()
            for name, value in _get_embed_fields_from_contests(chunk):
                embed.add_field(name=name, value=value, inline=False)
            pages.append((title, embed))
        return pages

    async def _send_contest_list(
        self,
        ctx: commands.Context,
        contests: list[Any] | None,
        *,
        title: str,
        empty_msg: str,
    ) -> None:
        if contests is None:
            raise ContestCogError(
                "The contest list isn't loaded yet. Try again in a minute."
            )
        if len(contests) == 0:
            await ctx.send(embed=discord_common.embed_neutral(empty_msg))
            return
        pages = self._make_contest_pages(contests, title)
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=_CONTEST_PAGINATE_WAIT_TIME,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @commands.hybrid_group(
        brief='Show the Codeforces contest list commands', fallback='show'
    )
    async def clist(self, ctx: commands.Context) -> None:
        """Show the commands that list Codeforces contests: those to come, those
        running now and those that finished recently.
        """
        await ctx.send_help(ctx.command)

    @clist.command(brief='List upcoming Codeforces contests')
    async def future(self, ctx: commands.Context) -> None:
        """List the Codeforces contests that haven't started yet, soonest first.

        Examples:
            /clist future
            ;clist future
        """
        await self._send_contest_list(
            ctx,
            self.future_contests,
            title='Future contests on Codeforces',
            empty_msg='No future contests scheduled',
        )

    @clist.command(brief='List the Codeforces contests running now')
    async def active(self, ctx: commands.Context) -> None:
        """List the Codeforces contests running now, including those still in
        system testing.

        Examples:
            /clist active
            ;clist active
        """
        await self._send_contest_list(
            ctx,
            self.active_contests,
            title='Active contests on Codeforces',
            empty_msg='No contests currently active',
        )

    @clist.command(brief='List recently finished Codeforces contests')
    async def finished(self, ctx: commands.Context) -> None:
        """List the 5 Codeforces contests that finished most recently, latest
        first.

        Examples:
            /clist finished
            ;clist finished
        """
        await self._send_contest_list(
            ctx,
            self.finished_contests,
            title='Recently finished contests on Codeforces',
            empty_msg='No finished contests found',
        )

    @commands.hybrid_group(
        brief='Show the Codeforces contest reminder commands', fallback='show'
    )
    async def remind(self, ctx: commands.Context) -> None:
        """Show the commands for contest reminders, which ping a role before each
        Codeforces contest. Take the role with /remind on to be pinged.
        """
        await ctx.send_help(ctx.command)

    @remind.command(
        brief='Post Codeforces contest reminders in this channel, pinging a role',
        usage='<role> <minutes...>',
        with_app_command=False,
    )
    async def here(
        self, ctx: commands.Context, role: discord.Role, *before: int
    ) -> None:
        """Post Codeforces contest reminders in this channel, pinging the role
        you give. Each number after the role is a reminder, that many minutes
        before the contest. Members take the role themselves with /remind on,
        so choose a role just for pings.

        Examples:
            ;remind here @Contests 60 10
        """
        if isinstance(ctx.channel, discord.Thread):
            raise ContestCogError(discord_common.NOT_IN_A_THREAD_MESSAGE)
        problem = discord_common.self_assignable_problem(role, ctx.guild.me)
        if problem is not None:
            raise ContestCogError(UNSUITABLE_REMINDER_ROLE_TEXT.format(problem=problem))
        if not role.mentionable:
            raise ContestCogError(
                "Reminders can't ping that role. Allow anyone to mention it "
                '(Server Settings → Roles), then try again.'
            )
        if not before or any(before_mins <= 0 for before_mins in before):
            raise ContestCogError(
                'Give one or more times to post reminders, each a number of '
                'minutes above 0.'
            )
        before_sorted = sorted(before, reverse=True)
        await self.bot.user_db.set_reminder_settings(
            ctx.guild.id, ctx.channel.id, role.id, json.dumps(before_sorted)
        )
        await ctx.send(
            embed=discord_common.embed_success(
                'Reminder settings saved. `/remind settings` shows them.'
            )
        )
        await self._reschedule_tasks(ctx.guild.id)

    @remind.command(brief='Stop posting contest reminders in this server')
    async def clear(self, ctx: commands.Context) -> None:
        """Stop posting contest reminders in this server, and forget their
        channel, role and times.

        Examples:
            ;remind clear
        """
        await self.bot.user_db.clear_reminder_settings(ctx.guild.id)
        await ctx.send(embed=discord_common.embed_success('Reminder settings cleared'))
        await self._reschedule_tasks(ctx.guild.id)

    @remind.command(brief='Show where and when contest reminders are posted')
    async def settings(self, ctx: commands.Context) -> None:
        """Show the channel that contest reminders are posted in, the role they
        ping and how many minutes before each contest they come.

        Examples:
            /remind settings
            ;remind settings
        """
        settings = await self.bot.user_db.get_reminder_settings(ctx.guild.id)
        if settings is None:
            await ctx.send(embed=discord_common.embed_neutral(NO_REMINDERS_TEXT))
            return
        channel_id, role_id, before = settings
        channel_id, role_id, before = int(channel_id), int(role_id), json.loads(before)
        channel, role = ctx.guild.get_channel(channel_id), ctx.guild.get_role(role_id)
        if channel is None:
            raise ContestCogError(REMINDER_CHANNEL_GONE_TEXT)
        if role is None:
            raise ContestCogError(REMINDER_ROLE_GONE_TEXT)
        before_str = ', '.join(str(before_mins) for before_mins in before)
        embed = discord_common.embed_success('Current reminder settings')
        embed.add_field(name='Channel', value=channel.mention)
        embed.add_field(name='Role', value=role.mention)
        embed.add_field(
            name='Before', value=f'{before_str} minutes before each contest'
        )
        await ctx.send(embed=embed)

    async def _get_remind_role(self, guild: discord.Guild) -> discord.Role:
        """The role that contest reminders ping, which members give themselves
        and take away again.

        ``ContestCogError`` if reminders aren't set up, or if the role is gone.
        Whether the bot may change who has it is checked by each command, every
        time: the role may have changed since an admin chose it, for example by
        gaining permissions.
        """
        settings = await self.bot.user_db.get_reminder_settings(guild.id)
        if settings is None:
            raise ContestCogError(NO_REMINDERS_TEXT)
        _, role_id, _ = settings
        role = guild.get_role(int(role_id))
        if role is None:
            raise ContestCogError(REMINDER_ROLE_GONE_TEXT)
        return role

    @remind.command(brief='Get pinged before Codeforces contests')
    async def on(self, ctx: commands.Context) -> None:
        """Give yourself the role that contest reminders ping, so that they ping
        you before each Codeforces contest.

        Examples:
            /remind on
            ;remind on
        """
        role = await self._get_remind_role(ctx.guild)
        problem = discord_common.self_assignable_problem(role, ctx.guild.me)
        if problem is not None:
            raise ContestCogError(
                UNASSIGNABLE_REMINDER_ROLE_TEXT.format(problem=problem)
            )
        if role in ctx.author.roles:
            embed = discord_common.embed_neutral(
                'You are already subscribed to contest reminders'
            )
        else:
            await ctx.author.add_roles(
                role, reason='User subscribed to contest reminders'
            )
            embed = discord_common.embed_success(
                'Successfully subscribed to contest reminders'
            )
        await ctx.send(embed=embed)

    @remind.command(brief='Stop getting pinged before Codeforces contests')
    async def off(self, ctx: commands.Context) -> None:
        """Take away your contest reminder role, so that reminders stop pinging
        you.

        Examples:
            /remind off
            ;remind off
        """
        role = await self._get_remind_role(ctx.guild)
        if role not in ctx.author.roles:
            embed = discord_common.embed_neutral(
                'You are not subscribed to contest reminders'
            )
        else:
            # Only what would raise the member's rights stops this, such as
            # one of TLE's roles: losing a ping role's permissions doesn't.
            problem = discord_common.self_removable_problem(role, ctx.guild.me)
            if problem is not None:
                raise ContestCogError(
                    UNASSIGNABLE_REMINDER_ROLE_TEXT.format(problem=problem)
                )
            await ctx.author.remove_roles(
                role, reason='User unsubscribed from contest reminders'
            )
            embed = discord_common.embed_success(
                'Successfully unsubscribed from contest reminders'
            )
        await ctx.send(embed=embed)

    @staticmethod
    def _get_cf_or_ioi_standings_table(
        problem_indices: Sequence[str],
        handle_standings: Sequence[tuple[str, Any]],
        deltas: Sequence[int | None] | None = None,
        *,
        mode: str,
    ) -> tuple[str, str, list[str], list[list[Any]]]:
        assert mode in ('cf', 'ioi')

        def maybe_int(value: Any) -> Any:
            return int(value) if mode == 'cf' else value

        header_style = '{:>} {:<}    {:^}  ' + '  '.join(
            ['{:^}'] * len(problem_indices)
        )
        body_style = '{:>} {:<}    {:>}  ' + '  '.join(['{:>}'] * len(problem_indices))
        header = ['#', 'Handle', '='] + list(problem_indices)
        if deltas:
            header_style += '  {:^}'
            body_style += '  {:>}'
            header += ['\N{INCREMENT}']

        body = []
        for handle, standing in handle_standings:
            virtual = '#' if standing.party.participantType == 'VIRTUAL' else ''
            tokens = [standing.rank, handle + ':' + virtual, maybe_int(standing.points)]
            for problem_result in standing.problemResults:
                score = ''
                if problem_result.points:
                    score = str(maybe_int(problem_result.points))
                tokens.append(score)
            body.append(tokens)

        if deltas:
            for tokens, delta in zip(body, deltas, strict=False):
                tokens.append('' if delta is None else f'{delta:+}')
        return header_style, body_style, header, body

    @staticmethod
    def _get_icpc_standings_table(
        problem_indices: Sequence[str],
        handle_standings: Sequence[tuple[str, Any]],
        deltas: Sequence[int | None] | None = None,
    ) -> tuple[str, str, list[str], list[list[Any]]]:
        header_style = '{:>} {:<}    {:^}  {:^}  ' + '  '.join(
            ['{:^}'] * len(problem_indices)
        )
        body_style = '{:>} {:<}    {:>}  {:>}  ' + '  '.join(
            ['{:<}'] * len(problem_indices)
        )
        header = ['#', 'Handle', '=', '-'] + list(problem_indices)
        if deltas:
            header_style += '  {:^}'
            body_style += '  {:>}'
            header += ['\N{INCREMENT}']

        body = []
        for handle, standing in handle_standings:
            virtual = '#' if standing.party.participantType == 'VIRTUAL' else ''
            tokens = [
                standing.rank,
                handle + ':' + virtual,
                int(standing.points),
                int(standing.penalty),
            ]
            for problem_result in standing.problemResults:
                score = '+' if problem_result.points else ''
                if problem_result.rejectedAttemptCount:
                    penalty = str(problem_result.rejectedAttemptCount)
                    if problem_result.points:
                        score += penalty
                    else:
                        score = '-' + penalty
                tokens.append(score)
            body.append(tokens)

        if deltas:
            for tokens, delta in zip(body, deltas, strict=False):
                tokens.append('' if delta is None else f'{delta:+}')
        return header_style, body_style, header, body

    def _make_standings_pages(
        self,
        contest: Any,
        problem_indices: list[str],
        handle_standings: list[tuple[str, Any]],
        deltas: list[int | None] | None = None,
    ) -> list[tuple[str, None]]:
        pages = []
        handle_standings_chunks = paginator.chunkify(
            handle_standings, _STANDINGS_PER_PAGE
        )
        num_chunks = len(handle_standings_chunks)
        delta_chunks: list[Sequence[Any] | None]
        if deltas:
            delta_chunks = list(paginator.chunkify(deltas, _STANDINGS_PER_PAGE))
        else:
            delta_chunks = [None] * num_chunks

        get_table: Callable[..., tuple[str, str, list[str], list[list[Any]]]]
        if contest.type == 'CF':
            get_table = functools.partial(
                self._get_cf_or_ioi_standings_table, mode='cf'
            )
        elif contest.type == 'ICPC':
            get_table = self._get_icpc_standings_table
        elif contest.type == 'IOI':
            get_table = functools.partial(
                self._get_cf_or_ioi_standings_table, mode='ioi'
            )
        else:
            raise AssertionError(f'Unexpected contest type {contest.type}')

        num_pages = 1
        for handle_standings_chunk, delta_chunk in zip(
            handle_standings_chunks, delta_chunks, strict=False
        ):
            header_style, body_style, header, body = get_table(
                problem_indices, handle_standings_chunk, delta_chunk
            )
            t = table.Table(table.Style(header=header_style, body=body_style))
            t += table.Header(*header)
            t += table.Line('\N{EM DASH}')
            for row in body:
                t += table.Data(*row)
            t += table.Line('\N{EM DASH}')
            page_num_footer = (
                f' # Page: {num_pages} / {num_chunks}' if num_chunks > 1 else ''
            )

            # We use yaml to get nice colors in the ranklist.
            content = f'```yaml\n{t}\n{page_num_footer}```'
            pages.append((content, None))
            num_pages += 1

        return pages

    @staticmethod
    def _make_contest_embed_for_ranklist(ranklist: Any) -> discord.Embed:
        contest = ranklist.contest
        assert contest.phase != 'BEFORE', f'Contest {contest.id} has not started.'
        embed = discord_common.cf_color_embed(title=contest.name, url=contest.url)
        phase = contest.phase.capitalize().replace('_', ' ')
        embed.add_field(name='Phase', value=phase)
        if ranklist.is_rated:
            embed.add_field(name='Deltas', value=ranklist.deltas_status)
        now = time.time()
        en = '\N{EN SPACE}'
        if contest.end_time > now:
            elapsed = cf_common.pretty_time_format(
                now - contest.startTimeSeconds, shorten=True
            )
            remaining = cf_common.pretty_time_format(
                contest.end_time - now, shorten=True
            )
            msg = f'{elapsed} elapsed{en}|{en}{remaining} remaining'
            embed.add_field(name='Tick tock', value=msg, inline=False)
        else:
            start = _contest_start_time_format(contest, dt.timezone.utc)
            duration = _contest_duration_format(contest)
            since = cf_common.pretty_time_format(
                now - contest.end_time, only_most_significant=True
            )
            msg = f'{start}{en}|{en}{duration}{en}|{en}Ended {since} ago'
            embed.add_field(name='When', value=msg, inline=False)
        return embed

    @staticmethod
    def _make_contest_embed_for_vc_ranklist(
        ranklist: Any,
        vc_start_time: float | None = None,
        vc_end_time: float | None = None,
    ) -> discord.Embed:
        contest = ranklist.contest
        embed = discord_common.cf_color_embed(title=contest.name, url=contest.url)
        embed.set_author(name='Virtual contest standings')
        now = time.time()
        if vc_start_time and vc_end_time:
            en = '\N{EN SPACE}'
            elapsed = cf_common.pretty_time_format(now - vc_start_time, shorten=True)
            remaining = cf_common.pretty_time_format(
                max(0, vc_end_time - now), shorten=True
            )
            msg = f'{elapsed} elapsed{en}|{en}{remaining} remaining'
            embed.add_field(name='Tick tock', value=msg, inline=False)
        return embed

    # The cooldowns of ranklist, ratedvc and vcrating count a use only once the
    # command's arguments are parsed, and a refusal that comes before any
    # request to Codeforces gives the use back: a mistake costs no wait.
    # Ranklist's cooldown is the whole server's, so it would hold everyone
    # back.
    @commands.command(
        brief="Show a Codeforces contest's standings for this server or given handles",
        usage='<contest_id> [handles...] [+server] [+official]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def ranklist(
        self, ctx: commands.Context, contest_id: int, *args: str
    ) -> None:
        """Show the standings of a Codeforces contest for this server's members,
        or for the handles you give. Name a member as !name, and add +server to
        include this server's members too. Add +official to show only official
        contestants, leaving out virtual and unofficial ones.

        Examples:
            ;ranklist 1950
            ;ranklist 1950 tourist !alice
            ;ranklist 1950 +official
        """
        (show_official,), handles = cf_common.filter_flags(args, ['+official'])
        handles = await cf_common.resolve_handles(
            ctx, self.member_converter, handles, maxcnt=None, default_to_all_server=True
        )
        contest = self._get_contest(ctx, contest_id)
        wait_msg = await ctx.send('Generating ranklist, please wait...')
        try:
            ranklist = await self._get_ranklist(ctx, contest, show_official)
        finally:
            # Gone whether the ranklist came or not.
            with contextlib.suppress(discord.HTTPException):
                await wait_msg.delete()
        await ctx.send(embed=self._make_contest_embed_for_ranklist(ranklist))
        await self._show_ranklist(
            channel=ctx.channel,
            contest_id=contest_id,
            handles=handles,
            ranklist=ranklist,
            ctx=ctx,
        )

    def _get_contest(self, ctx: commands.Context, contest_id: int) -> Any:
        """The contest ``contest_id`` in the bot's Codeforces contest list.

        If the list has no such contest, ``ContestNotFound``, and the use that
        the cooldown of ``ctx``'s command counted is given back.
        """
        try:
            return self.bot.cf_cache.contest_cache.get_contest(contest_id)
        except ContestNotFound:
            discord_common.undo_cooldown(ctx)
            raise

    async def _get_ranklist(
        self, ctx: commands.Context, contest: Any, show_official: bool
    ) -> Any:
        """The ranklist of ``contest``: the one the cache keeps up to date, or
        else a new one from Codeforces.
        """
        try:
            return self.bot.cf_cache.ranklist_cache.get_ranklist(contest, show_official)
        except RanklistNotMonitored:
            if contest.phase == 'BEFORE':
                # Codeforces wasn't asked anything.
                discord_common.undo_cooldown(ctx)
                raise ContestCogError(f"`{contest.name}` hasn't started yet.")
            return await self.bot.cf_cache.ranklist_cache.generate_ranklist(
                contest.id, fetch_changes=True, show_unofficial=not show_official
            )

    async def _show_ranklist(
        self,
        channel: discord.TextChannel,
        contest_id: int,
        handles: list[str],
        ranklist: Any,
        vc: bool = False,
        delete_after: float | None = None,
        ctx: commands.Context | None = None,
    ) -> None:
        contest = self.bot.cf_cache.contest_cache.get_contest(contest_id)
        if ranklist is None:
            raise ContestCogError('No ranklist to show')

        handle_standings = []
        for handle in handles:
            try:
                standing = ranklist.get_standing_row(handle)
            except rl.HandleNotPresentError:
                continue

            # Database has correct handle ignoring case, update to it
            handle = rl.Ranklist.get_ranklist_lookup_key(standing)
            if vc and standing.party.participantType != 'VIRTUAL':
                continue
            handle_standings.append((handle, standing))

        if not handle_standings:
            error = f'None of these handles are in the standings of `{contest.name}`.'
            if vc:
                await channel.send(
                    embed=discord_common.embed_alert(error), delete_after=delete_after
                )
                return
            raise ContestCogError(error)

        handle_standings.sort(key=lambda data: data[1].rank)
        deltas = None
        if ranklist.is_rated:
            deltas = [
                ranklist.get_delta(handle) for handle, standing in handle_standings
            ]

        problem_indices = [problem.index for problem in ranklist.problems]
        pages = self._make_standings_pages(
            contest, problem_indices, handle_standings, deltas
        )
        await paginator.paginate(
            channel,
            pages,
            wait_time=_STANDINGS_PAGINATE_WAIT_TIME,
            delete_after=delete_after,
            ctx=ctx,
        )

    @commands.command(
        brief='Start a rated virtual contest for the members you name',
        usage='<contest_id> <members...>',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def ratedvc(
        self, ctx: commands.Context, contest_id: int, *members: discord.Member
    ) -> None:
        """Replay a past Codeforces contest as a rated virtual contest for the
        members you name, yourself included if you take part. It must be a
        rated contest that none of you has submitted to. Use it in the rated
        virtual contest channel, where the standings follow every 5 minutes.

        Examples:
            ;ratedvc 1950 @alice @bob
        """
        ratedvc_channel_id = await self.bot.user_db.get_rated_vc_channel(ctx.guild.id)
        if not ratedvc_channel_id:
            discord_common.undo_cooldown(ctx)
            raise ContestCogError(
                'There is no rated virtual contest channel yet. Ask an admin to '
                'set one.'
            )
        if ctx.channel.id != ratedvc_channel_id:
            discord_common.undo_cooldown(ctx)
            raise ContestCogError(
                f'Use this command in <#{ratedvc_channel_id}>, the rated virtual '
                'contest channel.'
            )
        if not members:
            discord_common.undo_cooldown(ctx)
            raise ContestCogError(
                'Name the members who take part, yourself included if you do.'
            )
        contest = self._get_contest(ctx, contest_id)
        try:
            (await cf.contest.ratingChanges(contest_id=contest_id))[
                _MIN_RATED_CONTESTANTS_FOR_RATED_VC - 1
            ]
        except (cf.RatingChangesUnavailableError, IndexError):
            error = (
                f"`{contest.name}` can't be a rated virtual contest: fewer than "
                f'{_MIN_RATED_CONTESTANTS_FOR_RATED_VC} contestants were rated in '
                "it, or its rating changes aren't out yet."
            )
            raise ContestCogError(error)

        ongoing_vc_member_ids = await self._get_ongoing_vc_participants()
        this_vc_member_ids = {str(member.id) for member in members}
        intersection = this_vc_member_ids & ongoing_vc_member_ids
        if intersection:
            busy_members = ', '.join(
                [
                    ctx.guild.get_member(int(member_id)).mention
                    for member_id in intersection
                ]
            )
            error = f'Already in a rated virtual contest: {busy_members}.'
            raise ContestCogError(error)

        handles = await cf_common.members_to_handles(members, ctx.guild.id)
        visited_contests = await cf_common.get_visited_contests(handles)
        if contest_id in visited_contests:
            raise ContestCogError(
                f'Some of these handles have submitted to `{contest.name}` '
                f'already: {", ".join(handles)}.'
            )
        start_time = time.time()
        finish_time = start_time + contest.durationSeconds + _RATED_VC_EXTRA_TIME
        await self.bot.user_db.create_rated_vc(
            contest_id,
            start_time,
            finish_time,
            ctx.guild.id,
            [member.id for member in members],
        )
        title = f'Starting {contest.name} for:'
        msg = '\n'.join(
            f'[{discord.utils.escape_markdown(handle)}]({cf.PROFILE_BASE_URL}{handle})'
            for handle in handles
        )
        embed = discord_common.cf_color_embed(
            title=title, description=msg, url=contest.url
        )
        await ctx.send(embed=embed)
        embed = discord_common.embed_alert(
            f'You have {int(finish_time - start_time) // 60}'
            ' minutes to finish the contest!'
        )
        embed.set_footer(text='Good luck, and have fun!')
        await ctx.send(embed=embed)

    async def _make_vc_rating_changes_embed(
        self, guild: discord.Guild, contest_id: int, change_by_handle: dict[str, Any]
    ) -> discord.Embed:
        """Make an embed containing a list of rank changes and rating changes for ratedvc participants."""  # noqa: E501
        contest = self.bot.cf_cache.contest_cache.get_contest(contest_id)
        user_id_handle_pairs = await self.bot.user_db.get_handles_for_guild(guild.id)
        member_handle_pairs = [
            (guild.get_member(int(user_id)), handle)
            for user_id, handle in user_id_handle_pairs
        ]
        member_change_pairs = [
            (member, change_by_handle[handle])
            for member, handle in member_handle_pairs
            if member is not None and handle in change_by_handle
        ]

        member_change_pairs.sort(key=lambda pair: pair[1].newRating, reverse=True)
        rank_to_role = {role.name: role for role in guild.roles}

        def rating_to_displayable_rank(rating: int) -> str:
            rank = cf.rating2rank(rating).title
            role = rank_to_role.get(rank)
            return role.mention if role else rank

        rank_changes_str = []
        for member, change in member_change_pairs:
            if len(await self.bot.user_db.get_vc_rating_history(member.id)) == 1:
                # If this is the user's first rated contest.
                old_role = 'Unrated'
            else:
                old_role = rating_to_displayable_rank(change.oldRating)
            new_role = rating_to_displayable_rank(change.newRating)
            if new_role != old_role:
                rank_change_str = f'{member.mention} [{discord.utils.escape_markdown(change.handle)}]({cf.PROFILE_BASE_URL}{change.handle}): {old_role} \N{LONG RIGHTWARDS ARROW} {new_role}'  # noqa: E501
                rank_changes_str.append(rank_change_str)

        member_change_pairs.sort(
            key=lambda pair: pair[1].newRating - pair[1].oldRating, reverse=True
        )
        rating_changes_str = []
        for member, change in member_change_pairs:
            delta = change.newRating - change.oldRating
            rating_change_str = f'{member.mention} [{discord.utils.escape_markdown(change.handle)}]({cf.PROFILE_BASE_URL}{change.handle}): {change.oldRating} \N{HORIZONTAL BAR} **{delta:+}** \N{LONG RIGHTWARDS ARROW} {change.newRating}'  # noqa: E501
            rating_changes_str.append(rating_change_str)

        desc = '\n'.join(rank_changes_str) or 'No rank changes'
        embed = discord_common.cf_color_embed(
            title=contest.name, url=contest.url, description=desc
        )
        embed.set_author(name='Virtual contest results')
        embed.add_field(
            name='Rating changes',
            value='\n'.join(rating_changes_str) or 'No rating changes',
            inline=False,
        )
        return embed

    async def _watch_rated_vc(self, vc_id: int) -> None:
        vc = await self.bot.user_db.get_rated_vc(vc_id)
        channel_id = await self.bot.user_db.get_rated_vc_channel(vc.guild_id)
        if channel_id is None:
            raise ContestCogError('No Rated VC channel')
        channel = self.bot.get_channel(int(channel_id))
        member_ids = await self.bot.user_db.get_rated_vc_user_ids(vc_id)
        handles = [
            await self.bot.user_db.get_handle(member_id, channel.guild.id)
            for member_id in member_ids
        ]
        handle_to_member_id = {
            handle: member_id
            for handle, member_id in zip(handles, member_ids, strict=False)
        }
        now = time.time()
        ranklist = await self.bot.cf_cache.ranklist_cache.generate_vc_ranklist(
            vc.contest_id, handle_to_member_id
        )

        async def has_running_subs(handle: str) -> list[Any]:
            return [
                sub
                for sub in await cf.user.status(handle=handle)
                if sub.verdict == 'TESTING'
                and sub.problem.contestId == vc.contest_id
                and sub.relativeTimeSeconds <= vc.finish_time - vc.start_time
            ]

        running_subs_flag = any([await has_running_subs(handle) for handle in handles])
        if running_subs_flag:
            msg = 'Some submissions are still being judged'
            await channel.send(
                embed=discord_common.embed_alert(msg),
                delete_after=_WATCHING_RATED_VC_WAIT_TIME,
            )
        if now < vc.finish_time or running_subs_flag:
            # Display current standings
            await channel.send(
                embed=self._make_contest_embed_for_vc_ranklist(
                    ranklist, vc.start_time, vc.finish_time
                ),
                delete_after=_WATCHING_RATED_VC_WAIT_TIME,
            )
            await self._show_ranklist(
                channel,
                vc.contest_id,
                handles,
                ranklist=ranklist,
                vc=True,
                delete_after=_WATCHING_RATED_VC_WAIT_TIME,
            )
            return
        rating_change_by_handle = {}
        RatingChange = namedtuple('RatingChange', 'handle oldRating newRating')
        for handle, member_id in zip(handles, member_ids, strict=False):
            delta = ranklist.delta_by_handle.get(handle)
            if delta is None:  # The user did not participate.
                await self.bot.user_db.remove_last_ratedvc_participation(member_id)
                continue
            old_rating = await self.bot.user_db.get_vc_rating(member_id)
            new_rating = old_rating + delta
            rating_change_by_handle[handle] = RatingChange(
                handle=handle, oldRating=old_rating, newRating=new_rating
            )
            await self.bot.user_db.update_vc_rating(vc_id, member_id, new_rating)
        await self.bot.user_db.finish_rated_vc(vc_id)
        await channel.send(
            embed=await self._make_vc_rating_changes_embed(
                channel.guild, vc.contest_id, rating_change_by_handle
            )
        )
        await self._show_ranklist(
            channel, vc.contest_id, handles, ranklist=ranklist, vc=True
        )

    @tasks.task_spec(
        name='WatchRatedVCs',
        waiter=tasks.Waiter.fixed_delay(_WATCHING_RATED_VC_WAIT_TIME),
    )
    async def _watch_rated_vcs_task(self, _: Any) -> None:
        ongoing_rated_vcs = await self.bot.user_db.get_ongoing_rated_vc_ids()
        if ongoing_rated_vcs is None:
            return
        for rated_vc_id in ongoing_rated_vcs:
            await self._watch_rated_vc(rated_vc_id)

    @commands.hybrid_command(brief='Take a member out of their rated virtual contest')
    @app_commands.describe(
        user='The member to take out of the rated virtual contest they are in'
    )
    async def _unregistervc(self, ctx: commands.Context, user: discord.Member) -> None:
        """Take a member out of the rated virtual contest they are in, so that
        it doesn't change their rating.

        Examples:
            /_unregistervc user:@alice
            ;_unregistervc @alice
        """
        ongoing_vc_member_ids = await self._get_ongoing_vc_participants()
        if str(user.id) not in ongoing_vc_member_ids:
            raise ContestCogError(f"{user.mention} isn't in a rated virtual contest.")
        await self.bot.user_db.remove_last_ratedvc_participation(user.id)
        await ctx.send(
            embed=discord_common.embed_success(
                f'Took {user.mention} out of their rated virtual contest.'
            )
        )

    @commands.hybrid_command(
        brief='Make this channel the rated virtual contest channel'
    )
    async def set_ratedvc_channel(self, ctx: commands.Context) -> None:
        """Make this channel the one where ;ratedvc starts rated virtual
        contests and posts their standings. Choose a bot channel other than
        the staff channel: ;ratedvc works only in bot channels, and members
        can't read the staff channel.

        Examples:
            /set_ratedvc_channel
            ;set_ratedvc_channel
        """
        if isinstance(ctx.channel, discord.Thread):
            raise ContestCogError(discord_common.NOT_IN_A_THREAD_MESSAGE)
        await self.bot.user_db.set_rated_vc_channel(ctx.guild.id, ctx.channel.id)
        lines = [RATED_VC_CHANNEL_SET_TEXT]
        warning = self._ratedvc_warning(ctx.channel, ctx.guild.id)
        if warning is not None:
            lines.append(warning)
        await ctx.send(embed=discord_common.embed_success('\n\n'.join(lines)))

    def _ratedvc_warning(self, channel: Any, guild_id: int) -> str | None:
        """Why members can't use ;ratedvc in ``channel``, or None if they can.

        The access rules let it work in bot channels and in the staff channel,
        which members can't read. Without an access service, it works in
        every channel.
        """
        access = getattr(self.bot, 'access', None)
        if access is None:
            return None
        spot = access.spot(channel, guild_id, slash=False)
        place = spot.channel_id
        if place is not None and place == spot.staff_channel:
            return RATED_VC_STAFF_WARNING.format(channel=channel.mention)
        if place is not None and place in spot.bot_channels:
            return None
        return RATED_VC_WARNING.format(channel=channel.mention)

    @commands.hybrid_command(brief='Show the rated virtual contest channel')
    async def get_ratedvc_channel(self, ctx: commands.Context) -> None:
        """Show the channel where ;ratedvc starts rated virtual contests and
        posts their standings.

        Examples:
            /get_ratedvc_channel
            ;get_ratedvc_channel
        """
        channel_id = await self.bot.user_db.get_rated_vc_channel(ctx.guild.id)
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            raise ContestCogError('There is no rated virtual contest channel.')
        embed = discord_common.embed_success('Rated virtual contest channel')
        embed.add_field(name='Channel', value=channel.mention)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        brief="List this server's members by rated virtual contest rating"
    )
    async def vcratings(self, ctx: commands.Context) -> None:
        """List this server's members by their rated virtual contest rating,
        highest first. Members who haven't finished a rated virtual contest
        aren't listed.

        Examples:
            /vcratings
            ;vcratings
        """
        users = []
        # Finding every member can take longer than the 3 seconds a slash
        # command has to answer, so typing defers the answer first.
        async with ctx.typing():
            for member_id, handle in await self.bot.user_db.get_handles_for_guild(
                ctx.guild.id
            ):
                member = await self.member_converter.convert(ctx, str(member_id))
                rating = await self.bot.user_db.get_vc_rating(
                    member_id, default_if_not_exist=False
                )
                users.append((member, handle, rating))
        # Filter only rated users. (Those who entered at least one rated vc.)
        users = [
            (member, handle, rating)
            for member, handle, rating in users
            if rating is not None
        ]
        users.sort(key=lambda user: -user[2])

        _PER_PAGE = 10

        def make_page(chunk: Sequence[Any], page_num: int) -> tuple[str, discord.Embed]:
            style = table.Style('{:>}  {:<}  {:<}  {:<}')
            t = table.Table(style)
            t += table.Header('#', 'Name', 'Handle', 'Rating')
            t += table.Line()
            for index, (member, handle, rating) in enumerate(chunk):
                rating_str = f'{rating} ({cf.rating2rank(rating).title_abbr})'
                t += table.Data(
                    _PER_PAGE * page_num + index,
                    f'{member.display_name}',
                    handle,
                    rating_str,
                )

            table_str = f'```\n{t}\n```'
            embed = discord_common.cf_color_embed(description=table_str)
            return 'Virtual contest ratings', embed

        if not users:
            raise ContestCogError(
                'Nobody in this server has a rated virtual contest rating yet.'
            )

        pages = [
            make_page(chunk, k)
            for k, chunk in enumerate(paginator.chunkify(users, _PER_PAGE))
        ]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @commands.command(
        brief='Plot the rated virtual contest ratings of up to 5 members',
        usage='[members...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def vcrating(self, ctx: commands.Context, *members: discord.Member) -> None:
        """Plot the rated virtual contest ratings of up to 5 members over time,
        or yours if you name nobody.

        Examples:
            ;vcrating
            ;vcrating @alice @bob
        """
        assert isinstance(ctx.author, discord.Member)
        members = members or (ctx.author,)
        if len(members) > 5:
            raise ContestCogError('Name at most 5 members.')
        plot_data = defaultdict(list)

        min_rating = 1100
        max_rating = 1800

        for member in members:
            rating_history = await self.bot.user_db.get_vc_rating_history(member.id)
            if not rating_history:
                raise ContestCogError(
                    f"{member.mention} hasn't finished a rated virtual contest yet."
                )
            for vc_id, rating in rating_history:
                vc = await self.bot.user_db.get_rated_vc(vc_id)
                date = dt.datetime.fromtimestamp(vc.finish_time)
                plot_data[member.display_name].append((date, rating))
                min_rating = min(min_rating, rating)
                max_rating = max(max_rating, rating)

        plt.clf()
        # plot at least from mid gray to mid purple
        for rating_data in plot_data.values():
            x, y = zip(*rating_data, strict=False)
            plt.plot(
                x,
                y,
                linestyle='-',
                marker='o',
                markersize=4,
                markerfacecolor='white',
                markeredgewidth=0.5,
            )

        gc.plot_rating_bg(cf.RATED_RANKS)
        plt.gcf().autofmt_xdate()

        plt.ylim(min_rating - 100, max_rating + 200)
        labels = [
            gc.StrWrap('{} ({})'.format(member_display_name, rating_data[-1][1]))
            for member_display_name, rating_data in plot_data.items()
        ]
        plt.legend(labels, loc='upper left', prop=gc.fontprop)

        discord_file = gc.get_current_figure_as_file()
        embed = discord_common.cf_color_embed(title='Virtual contest ratings')
        discord_common.attach_image(embed, discord_file)
        discord_common.set_author_footer(embed, ctx.author)
        await ctx.send(embed=embed, file=discord_file)

    @discord_common.send_error_if(
        ContestCogError,
        rl.RanklistError,
        CacheError,
        cf_common.ResolveHandleError,
    )
    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Contests(bot))
