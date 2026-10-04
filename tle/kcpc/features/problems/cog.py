"""Problems: /randproblem, the weekly problem, and commands about them.

- Members: ``/randproblem <topic> <difficulty> [platform]`` picks a random
  Codeforces or AtCoder problem, leaving out the ones the member solved on
  the account they linked, as far as can be told within 10 seconds.
  ``/weekly current`` (also plain ``;weekly``) shows this week's problem, and
  ``/weekly history`` the earlier ones, with their solutions once posted.
- Admins: ``/kcpc weekly`` to queue problems, set a solution's link, choose
  the rotation, preview the next post and post this week's problem now,
  attached under /kcpc (see ``tle.kcpc.bot.admin``).
- The job problems.refresh keeps both platforms' problem lists in memory
  (see ``catalog``): it runs as the bot starts and then every 30 minutes,
  fetching a list again once it is older than its max age. The job
  weekly.post runs each Friday at noon, club time, posting last week's
  solution, then the new problem, in every server with the weekly problem on
  that the bot is in (see ``weekly``). A fresh install posts from the next
  Friday on, and a Friday the bot missed is posted if it is back within 6
  hours.
"""

import asyncio
import logging
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot import codeforces_links
from tle.kcpc.bot.admin import (
    attach_admin_group,
    detach_admin_group,
    withhold_admin_group,
)
from tle.kcpc.bot.checks import kcpc_admin_only
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import alert_embed, info_embed, success_embed, to_embed
from tle.kcpc.bot.pages import send_pages
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.ledger import DeliveryStatus
from tle.kcpc.core.messages import (
    FIELD_VALUE_LIMIT,
    EmbedField,
    OutgoingMessage,
    shorten,
)
from tle.kcpc.core.publishing import PublishOutcome
from tle.kcpc.core.schedule import Every, Weekly
from tle.kcpc.core.scheduler import ScheduledJob
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.core.timeutil import discord_timestamp
from tle.kcpc.features.problems import markdown
from tle.kcpc.features.problems.catalog import (
    ATCODER,
    CODEFORCES,
    Problem,
    ProblemCatalog,
    ProblemRef,
    describe_difficulty,
    parse_problem_ref,
    platform_name,
    platform_possessive,
    problem_title,
)
from tle.kcpc.features.problems.editorials import (
    CONTEST_MATERIALS,
    EditorialFinder,
    SolutionLink,
    solution_links,
)
from tle.kcpc.features.problems.randomizer import (
    DifficultyChoice,
    Pick,
    difficulty_choices,
    parse_difficulty,
    pick,
    window_of,
)
from tle.kcpc.features.problems.repo import QueuedProblem, WeeklyProblem, WeeklyRepo
from tle.kcpc.features.problems.rotation import (
    RotationEntry,
    describe_entry,
    parse_rotation,
)
from tle.kcpc.features.problems.settings import WEEKLY, weekly_settings
from tle.kcpc.features.problems.solved import SolvedProblems, SolvedSet
from tle.kcpc.features.problems.topics import (
    ANY,
    KNOWN_TAGS,
    resolve_topic,
    topic_choices,
)
from tle.kcpc.features.problems.weekly import (
    FOOTER,
    FRIDAY,
    POST_TIME,
    SOLUTION_LOOKBACK,
    WEEKLY_JOB,
    NoWeeklyProblem,
    PostResult,
    WeeklyPlan,
    WeeklyReport,
    WeeklyService,
    problem_key,
)
from tle.kcpc.platforms.atcoder.editorials import AtCoderEditorialsClient
from tle.kcpc.platforms.atcoder.problems import AtCoderProblemsClient

logger = logging.getLogger(__name__)

REFRESH_JOB = 'problems.refresh'
# How often the problem lists are checked: each is fetched again only once it
# is older than its max age (see ProblemCatalog).
_REFRESH_INTERVAL = timedelta(minutes=30)
# How late the weekly job may still post a Friday it missed, while the bot was
# down, say: the same afternoon is worth it, the next day not.
_CATCH_UP_GRACE = timedelta(hours=6)

# Seconds /randproblem waits for the problems a member solved before it picks
# without them, or with those read by then: a long AtCoder history takes many
# requests, each a second or more apart.
_SOLVED_WAIT = 10.0
# How far from the rating asked for a pick may be before the reply says that
# none was nearer: the randomizer's first window on each platform. AtCoder's
# difficulties convert to any rating, so its first window is 50 either side.
_FIRST_WINDOW = {CODEFORCES: 0, ATCODER: 50}

_MAX_TOPIC_LENGTH = 64
_MAX_DIFFICULTY_LENGTH = 16
_MAX_ROTATION_LENGTH = 1000
# How long after its problem a solution can still be posted, in weeks.
_LOOKBACK_WEEKS = SOLUTION_LOOKBACK.days // 7
_PER_PAGE = 10  # weeks on each page of /weekly history
_LISTED = 10  # the most queued problems /kcpc weekly preview lists
_LISTED_TITLE_LIMIT = 60  # how much of a queued problem's title preview shows
_MAX_CHOICES = 25  # the most autocomplete suggestions Discord shows
_CHOICE_NAME_LIMIT = 100  # Discord's limit on a suggestion's name
_WEEK_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
# What resets the rotation, given as the rotation's entries.
_DEFAULT = 'default'
# DiscordPublisher's reason while Discord hasn't sent a guild's channels, as
# just after the bot reconnects: the post goes out if tried again then.
_GUILD_UNAVAILABLE = 'guild-unavailable'
# The outcomes of a post that went out, or was already out.
_WENT_OUT = frozenset({PublishOutcome.SENT, PublishOutcome.ALREADY_HANDLED})

_WEEKLY_TITLE = 'Weekly problem'
_HISTORY_TITLE = 'Weekly problems'
_NOTHING_POSTED = 'No weekly problem has been posted here yet.'
_ATCODER_TOPICS = 'AtCoder problems have no topics: use any with platform atcoder.'
_NOTHING_FOUND = "There's no {platform} problem{topic} at {difficulty}{solved}."
_SOLVED_AS = "Leaving out problems you've solved as {handle}"
_NOT_ALL_CHECKED = "Couldn't check all the problems you've solved"
# Not the nearest: on AtCoder, any problem within the window is as likely.
_WIDENED = (
    'Nothing left was rated {rating}, so this is one of those within {window} of it.'
)
_BAD_LINK = 'The link must be a web address starting with https:// or http://.'
_LONG_LINK = (
    f'The link is too long: it can be at most {markdown.LINK_LIMIT} characters, '
    'so that posts and replies can show it.'
)
_BAD_WEEK = "Give the week as its Friday's date, YYYY-MM-DD, such as 2026-10-09."
_UNKNOWN_PROBLEM = '{platform} has no problem {problem}.'
# Codeforces' problemset lists a problem that a Div. 1 and a Div. 2 round
# shared once, under the Div. 1 round.
_UNKNOWN_CODEFORCES = (
    "Codeforces' problemset has no problem {problem}. A problem that a Div. 2 "
    'round shares with a Div. 1 round held alongside it is listed under the Div. 1 '
    "round's number: give it that way, or link it from the Div. 1 contest."
)
_NOT_QUEUED = "{problem} isn't in this server's queue."
_NO_SOLUTION_TO_SET = "No weekly problem here has a solution that's still to come."
_NO_PROBLEM_THAT_WEEK = 'This server had no weekly problem on {week}.'
_SOLUTION_POSTED = (
    "The solution of {title} is posted already, so its link can't change."
)
_NEVER_POSTED = (
    "The problem of {week}, {title}, wasn't posted, so its solution won't be either."
)
_TOO_OLD = (
    "The solution of {title} won't be posted: by the next post, its problem will "
    f'be more than {_LOOKBACK_WEEKS} weeks old.'
)
_NOT_SET_UP = (
    "The weekly problem isn't set up here. Turn it on with `/kcpc enable weekly` "
    'and set its channel with `/kcpc channel weekly #channel`, then try again.'
)
_PROBLEM_WAITS = (
    "This week's problem goes out after that solution, so it wasn't posted either."
)
_PICK_FAILED = (
    '{reason} Please try again in a few minutes, or queue a problem with '
    '`/kcpc weekly queue`.'
)
_REFUSED_EARLIER = (
    "Discord refused to post {post}, earlier: {reason}. It can't be posted again "
    'this week.'
)
_TRY_AGAIN = (
    "Discord hasn't sent this server's channels yet. Please try again in a minute"
)
_CHECK_CHANNEL = (
    '{reason}. Check the channel and my permissions there, e.g. with '
    '`/kcpc channel weekly #channel`'
)
# What /kcpc weekly queue says the solution post of the problem will link.
_ADMINS_LINK = 'Its solution post will link {link}.'
_CONTEST_PAGE = (
    'Its solution post will link the contest page, where Codeforces lists the '
    'editorial under Contest materials, unless you set a link with '
    '`/kcpc weekly solution` once it is posted.'
)
_EDITORIAL_FOUND = (
    "Its solution post will link AtCoder's {link}, and the task's other editorials."
)
_NO_EDITORIAL_YET = (
    'AtCoder lists no official editorial for it yet. Its solution post will link '
    "the task's editorials, and the official one if there is one by then."
)
_EDITORIALS_UNCHECKED = (
    "I couldn't check AtCoder's editorials just now. Its solution post will link "
    "the task's editorials, and the official one if there is one by then."
)
_ROTATION_HINT = (
    'Set it with `/kcpc weekly rotation cf easy, ac medium, cf medium graphs, ac '
    'hard`, or go back to the default with `/kcpc weekly rotation default`.'
)
_EMPTY_QUEUE = 'Empty. Add a problem with `/kcpc weekly queue`.'
# The lines of a list that preview cut short, before and after what it shows.
_EARLIER = '…{count} earlier'
_MORE = '…and {count} more'
_ROTATION_TITLE = 'Weekly rotation'
_PREVIEW_TITLE = 'Weekly problem preview'
_STORED_ROTATION = "This server's rotation:"
_DEFAULT_ROTATION = 'This server has the default rotation:'
_ROTATION_SET = 'The rotation is now:'
_ROTATION_RESET = 'The rotation is the default again:'
_NEXT_FROM_ROTATION = 'From the rotation: {entry}, picked when it posts'
_NOTHING_YET = 'Nothing has been posted yet.'
_NOT_SOLVED = " that you haven't solved"
# Where this week's solution post points, as /kcpc weekly preview says it.
_SOLUTION_POSTED_ALREADY = 'posted'
_ADMINS_SOLUTION = '{link}, set by <@{admin}>'
_FOUND_EDITORIAL = "AtCoder's {link}"
_TASK_EDITORIALS_UNTIL = (
    "the task's editorials until it has an official one, or you set a link with "
    '`/kcpc weekly solution`'
)
_CONTEST_PAGE_UNTIL = 'the contest page until you set one with `/kcpc weekly solution`'
_SOLUTION_TOO_OLD = (
    "won't be posted: by the next post, the problem will be more than "
    f'{_LOOKBACK_WEEKS} weeks old'
)
# How /kcpc weekly rotation and preview mark the entry the next post picks by,
# which on a Friday morning is the entry of that day's post.
_NEXT_POST_MARK = '(next post)'

# How each outcome of a post-now's post is told. {post} names the post, as in
# "this week's problem, **X**", and {subject} names it to begin a sentence:
# "This week's problem, **X**,".
_OUTCOMES = {
    PublishOutcome.SENT: 'Posted {post}.',
    PublishOutcome.ALREADY_HANDLED: '{subject} is already posted.',
    PublishOutcome.PENDING: (
        "Posted {post}, but Discord didn't confirm it; I'll check within a few minutes."
    ),
    PublishOutcome.SKIPPED: 'Discord refused to post {post}: {reason}.',
    PublishOutcome.UNDELIVERABLE: "Couldn't post {post}: {reason}.",
}


# A subcommand of the cog, as discord.py types it.
_Subcommand = commands.HybridCommand[Any, ..., Any]


@dataclass(frozen=True)
class _Exclusion:
    """The problems /randproblem leaves out for a member, and what it says of them."""

    solved: SolvedSet | None  # None: nothing is left out
    footer: str | None  # whose solved problems they are, or that some may be missing


class KcpcProblems(KcpcCog):
    """/randproblem, /weekly for members and /kcpc weekly for admins, and the jobs.

    Its subcommands are declared on their own and put in their groups by
    ``cog_load``. discord.py links a cog's subcommands to their groups by
    qualified name as it makes the cog, and /weekly and /kcpc weekly share one
    until the latter is attached under /kcpc: declared in their groups, the
    subcommands of one would end up in the other.
    """

    # Set by cog_load, which runs before any command or job of the cog can.
    _repo: WeeklyRepo
    _catalog: ProblemCatalog
    _editorials: EditorialFinder
    _solved: SolvedProblems
    _schedule: Weekly
    _rng: random.Random
    _weekly: WeeklyService
    # Each server's queue, which unqueue suggests from; read again after every
    # change, the weekly job's picks among them.
    _queues: dict[int, tuple[QueuedProblem, ...]]

    async def cog_load(self) -> None:
        """Make the services, add the admin commands, then the jobs.

        If a step fails, the steps before it are undone before the error
        propagates: discord.py doesn't call ``cog_unload`` when ``cog_load``
        raises.
        """
        services = self.services
        self._repo = WeeklyRepo(services.db)
        atcoder = AtCoderProblemsClient(services.http)
        self._catalog = ProblemCatalog(atcoder, services.clock)
        self._editorials = EditorialFinder(AtCoderEditorialsClient(services.http))
        self._solved = SolvedProblems(
            atcoder, services.clock, listed=self._codeforces_lists
        )
        self._schedule = Weekly(FRIDAY, POST_TIME, services.settings.tz)
        # One generator for /randproblem and the weekly picks.
        self._rng = random.Random()
        self._weekly = WeeklyService(
            self._repo,
            self._catalog,
            self._editorials,
            services.guild_settings,
            services.ledger,
            services.publisher,
            services.clock,
            self._schedule,
            rng=self._rng,
            in_guild=self._in_guild,
        )
        self._queues = {}
        await self._refresh_queues()
        self._nest_subcommands()
        added: list[str] = []
        try:
            if not attach_admin_group(self.bot, self.weekly_admin):
                withhold_admin_group(self, self.weekly_admin)
            for job in self._jobs():
                services.scheduler.add(job)
                added.append(job.name)
        except BaseException:
            for name in added:
                await services.scheduler.remove(name)
            # Detaching a group that isn't attached does nothing.
            detach_admin_group(self.bot, self.weekly_admin)
            raise

    async def cog_unload(self) -> None:
        """Stop the jobs and the lookups of solved problems still running,
        and take the admin commands away.
        """
        for name in (REFRESH_JOB, WEEKLY_JOB):
            await self.services.scheduler.remove(name)
        await self._solved.close()
        detach_admin_group(self.bot, self.weekly_admin)

    def _nest_subcommands(self) -> None:
        """Put each subcommand in its group (see the class docstring)."""
        member_commands: tuple[_Subcommand, ...] = (self.current, self.history)
        for command in member_commands:
            self.weekly.add_command(command)
        admin_commands: tuple[_Subcommand, ...] = (
            self.queue_problem,
            self.unqueue_problem,
            self.set_solution,
            self.set_rotation,
            self.preview,
            self.post_now,
        )
        for command in admin_commands:
            self.weekly_admin.add_command(command)

    async def topic_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the topics with what was typed: any, the groups, then tags.

        They come from memory: Discord asks again on every keystroke.
        """
        return _choices(topic_choices(current, self._known_tags()))

    async def difficulty_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the bands, then the ratings, with what was typed."""
        return _choices(difficulty_choices(current))

    async def queued_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the problems in the server's queue with what was typed.

        They come from a list in memory: Discord asks again on every keystroke.
        """
        if interaction.guild_id is None:
            return []
        needle = current.strip().lower()
        return [
            app_commands.Choice(name=_choice_name(item), value=_ref_text(item))
            for item in self._queues.get(interaction.guild_id, ())
            if needle in _queued_title(item).lower()
            or needle in item.problem_id.lower()
        ][:_MAX_CHOICES]

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_command(brief='A random problem by topic and difficulty')  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(
        topic='any, a group such as graphs, or a Codeforces tag such as dp',
        difficulty='easy, medium, hard or expert, or a rating from 800 to 3500',
        platform='Codeforces if left out',
    )
    @app_commands.autocomplete(
        topic=topic_autocomplete, difficulty=difficulty_autocomplete
    )
    async def randproblem(
        self,
        ctx: commands.Context[Any],
        topic: commands.Range[str, 1, _MAX_TOPIC_LENGTH],
        difficulty: commands.Range[str, 1, _MAX_DIFFICULTY_LENGTH],
        platform: Literal['codeforces', 'atcoder'] = 'codeforces',
    ) -> None:
        """Pick a random problem about a topic, as hard as you ask.

        Ratings are on Codeforces' scale on both platforms. Problems you solved
        on the account you linked with /link are left out, as far as can be
        told within 10 seconds. As a prefix command, quote a topic of several
        words: ;randproblem "binary search" 1600
        """
        guild = _guild(ctx)
        # Checked before deferring: after a public defer, Discord shows the
        # reply to an error to everyone.
        if platform == ATCODER and _normalized(topic) != ANY:
            raise KcpcUserError(_ATCODER_TOPICS)
        chosen = resolve_topic(topic, self._known_tags())
        wanted = parse_difficulty(difficulty)
        problems = self._catalog.pool(platform)
        # Reading what the member solved can take seconds, longer than Discord
        # waits for a slash command's first answer.
        await ctx.defer()
        exclusion = await self._exclusion(guild.id, ctx.author.id, platform)
        solved = exclusion.solved
        picked = pick(
            problems,
            topic=chosen,
            difficulty=wanted,
            exclude=_nothing_solved if solved is None else solved.contains,
            rng=self._rng,
        )
        if picked is None:
            raise KcpcUserError(
                _nothing_found(platform, chosen.key, wanted, solved is not None)
            )
        message = _picked_message(picked, wanted, exclusion.footer)
        await ctx.send(embed=to_embed(message))

    @commands.hybrid_group(fallback='current', brief="This week's problem")  # type: ignore[arg-type]
    @commands.guild_only()
    async def weekly(self, ctx: commands.Context[Any]) -> None:
        """Show this server's weekly problem, and its solution once it is out."""
        await self._show_current(ctx)

    # The slash command is the group's fallback, which prefix commands lack.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name='current', with_app_command=False, brief="This week's problem"
    )
    async def current(self, ctx: commands.Context[Any]) -> None:
        """Show this server's weekly problem, and its solution once it is out."""
        await self._show_current(ctx)

    @commands.hybrid_command(name='history', brief='Earlier weekly problems')  # type: ignore[arg-type]
    async def history(self, ctx: commands.Context[Any]) -> None:
        """List this server's weekly problems, newest first, with their solutions."""
        guild = _guild(ctx)
        rows = await self._weekly.history(guild.id)
        now = self.services.clock.now()
        await send_pages(ctx, self._history_pages(rows, now))

    @commands.hybrid_group(  # type: ignore[arg-type]
        name='weekly', brief='The weekly problem: queue, solutions, rotation'
    )
    @kcpc_admin_only()
    async def weekly_admin(self, ctx: commands.Context[Any]) -> None:
        """Queue problems, set solutions, choose the rotation, and post now."""
        # Only ;kcpc weekly gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @commands.hybrid_command(name='queue', brief='Queue a problem for a coming week')  # type: ignore[arg-type]
    @app_commands.describe(
        problem='A Codeforces problem such as 1520D, an AtCoder one such as '
        'abc300_d, or its link',
        solution='A link to its solution, if you have one (an http or https link)',
    )
    @kcpc_admin_only()
    async def queue_problem(
        self, ctx: commands.Context[Any], problem: str, solution: str | None = None
    ) -> None:
        """Queue a problem: the queue's oldest is posted before the rotation picks.

        A problem the server has had, or has queued, can't be queued again,
        unless its post never went out. Without a link to its solution, its
        solution post links AtCoder's official editorial, or the Codeforces
        contest's page, which lists the editorial.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        found = self._find(problem)
        solution_url = _web_link(solution)
        band = found.band
        # Only an admin's link is kept: a stored link counts as the admin's,
        # and the bot looks AtCoder's editorials up again when it posts.
        queued = await self._weekly.enqueue(
            QueuedProblem(
                guild_id=guild.id,
                source=found.platform,
                problem_id=found.problem_id,
                contest_id=found.contest_id,
                index=found.index,
                name=found.name,
                url=found.url,
                difficulty=found.rating,
                band=None if band is None else band.value,
                solution_url=solution_url,
                queued_by=ctx.author.id,
                queued_at=self.services.clock.now(),
            )
        )
        await self._refresh_queue(guild.id)
        logger.info(
            'Admin %d of guild %d queued %s %s as a weekly problem',
            ctx.author.id,
            guild.id,
            queued.source,
            queued.problem_id,
        )
        position = _position(self._queues.get(guild.id, ()), queued)
        note = await self._solution_note(queued)
        await _reply(
            ctx,
            success_embed(
                f'Queued {_bold(found.title)}, number {position} in the queue. {note}'
            ),
        )

    @commands.hybrid_command(name='unqueue', brief='Take a problem out of the queue')  # type: ignore[arg-type]
    @app_commands.describe(problem='The queued problem: pick one as you type')
    @app_commands.autocomplete(problem=queued_autocomplete)
    @kcpc_admin_only()
    async def unqueue_problem(self, ctx: commands.Context[Any], problem: str) -> None:
        """Take a problem out of this server's queue."""
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        ref = parse_problem_ref(problem)
        queue = await self._repo.queue(guild.id)
        queued = next((item for item in queue if _is_problem(item, ref)), None)
        removed = (
            None
            if queued is None
            else await self._weekly.unqueue(guild.id, queued.source, queued.problem_id)
        )
        if removed is None:
            raise KcpcUserError(_NOT_QUEUED.format(problem=_bold(ref.problem_id)))
        await self._refresh_queue(guild.id)
        logger.info(
            'Admin %d of guild %d took %s %s out of the weekly queue',
            ctx.author.id,
            guild.id,
            removed.source,
            removed.problem_id,
        )
        title = _bold(_queued_title(removed))
        await _reply(ctx, success_embed(f'Took {title} out of the queue.'))

    @commands.hybrid_command(  # type: ignore[arg-type]
        name='solution', brief="Set a weekly problem's solution link"
    )
    @app_commands.describe(
        url='The link to the solution (an http or https link)',
        week="The problem's Friday, YYYY-MM-DD; the latest whose solution is to "
        'come if left out',
    )
    @kcpc_admin_only()
    async def set_solution(
        self, ctx: commands.Context[Any], url: str, week: str | None = None
    ) -> None:
        """Set the link that a weekly problem's solution post gives.

        It replaces the Codeforces contest's page, or the AtCoder editorial
        the bot found. A solution that was posted keeps its link, and one that
        won't be posted takes none.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        link = _web_link(url)
        if link is None:
            raise KcpcUserError(_BAD_LINK)
        row = await self._solution_row(guild.id, week)
        title = _bold(_title(row))
        stored = await self._weekly.set_solution(
            guild.id, row.slot, link, set_by=ctx.author.id
        )
        if stored is None or stored.solution_posted:  # posted meanwhile
            raise KcpcUserError(_SOLUTION_POSTED.format(title=title))
        logger.info(
            'Admin %d of guild %d set the solution of the weekly problem of %s',
            ctx.author.id,
            guild.id,
            row.week,
        )
        shown = markdown.link('this link', link)
        text = f'The solution post of {title} will link {shown}.'
        due = self._schedule.next_after(row.slot)
        if due > self.services.clock.now():
            text += f' It goes out {_when(due)}.'
        await _reply(ctx, success_embed(text))

    @commands.hybrid_command(name='rotation', brief='Show or set the weekly rotation')  # type: ignore[arg-type]
    @app_commands.describe(
        entries='Such as: cf easy, ac medium, cf medium graphs, ac hard; or default'
    )
    @kcpc_admin_only()
    async def set_rotation(
        self,
        ctx: commands.Context[Any],
        *,
        entries: commands.Range[str, 1, _MAX_ROTATION_LENGTH] | None = None,
    ) -> None:
        """Show or set the rotation: the platform, band and topic of each week.

        Entries are 'platform band [topic]', separated by commas: cf or ac;
        easy, medium, hard or expert; a topic, any if left out (and always on
        AtCoder). The weeks take them in turn, by the calendar; default goes
        back to the default rotation. As a prefix command the entries take the
        rest of the message: ;kcpc weekly rotation cf easy, ac medium, ac hard
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        guild_settings = self.services.guild_settings
        if entries is None:
            settings = await guild_settings.get(guild.id, WEEKLY)
            plan = await self._weekly.plan_next(guild.id)
            text = (
                f'{_rotation_heading(settings)}\n'
                f'{_rotation_lines(plan.rotation, plan.entry)}\n\n{_ROTATION_HINT}'
            )
            await _reply(ctx, info_embed(_ROTATION_TITLE, text))
            return
        if entries.strip().lower() == _DEFAULT:
            encoded: tuple[str, ...] = ()
            heading = _ROTATION_RESET
        else:
            rotation = parse_rotation(entries, self._known_tags())
            encoded = tuple(entry.encode() for entry in rotation)
            heading = _ROTATION_SET
        await guild_settings.update(guild.id, WEEKLY, rotation=encoded)
        logger.info(
            'Admin %d of guild %d set the weekly rotation to %s',
            ctx.author.id,
            guild.id,
            ', '.join(encoded) or 'the default',
        )
        plan = await self._weekly.plan_next(guild.id)
        lines = _rotation_lines(plan.rotation, plan.entry)
        await _reply(ctx, success_embed(f'{heading}\n{lines}'))

    @commands.hybrid_command(name='preview', brief='What the next weekly post will be')  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def preview(self, ctx: commands.Context[Any]) -> None:
        """Show when and where the next weekly post goes, and what it holds.

        That is this week's problem and the link its solution post will give,
        the next problem, or how it will be picked, then the queue and the
        rotation.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        settings = await self.services.guild_settings.get(guild.id, WEEKLY)
        plan = await self._weekly.plan_next(guild.id)
        current = await self._weekly.current(guild.id)
        in_lookback = current is not None and self._weekly.in_lookback(current)
        stored = weekly_settings(settings).rotation
        rotation = 'Rotation' if stored else 'Rotation (the default)'
        fields = (
            EmbedField('Next post', _next_post(plan.slot, settings)),
            EmbedField('This week', _this_week(current, in_lookback)),
            EmbedField('Next problem', _next_problem(plan)),
            EmbedField(f'Queue ({len(plan.queue)})', _queue_lines(plan.queue)),
            EmbedField(rotation, _rotation_window(plan.rotation, plan.entry)),
        )
        message = OutgoingMessage(title=_PREVIEW_TITLE, fields=fields)
        await _reply(ctx, to_embed(message))

    @commands.hybrid_command(name='post-now', brief="Post this week's problem now")  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def post_now(self, ctx: commands.Context[Any]) -> None:
        """Post this week's problem now, after any solution that is due.

        For a server that has just turned the weekly problem on: the job posts
        at the next Friday's slot, and this posts the week's problem before
        then. Running it again posts nothing twice.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        slot = self._schedule.prev_at_or_before(self.services.clock.now())
        unpicked = None
        try:
            report = await self._weekly.run_guild(guild.id, slot)
        except NoWeeklyProblem as exc:
            if not exc.solutions:
                raise
            # Solutions went out before the pick failed: the admin hears of
            # them too.
            report = WeeklyReport(True, exc.solutions, None)
            unpicked = str(exc)
        finally:
            # Picking a queued problem takes it out of the queue.
            await self._refresh_queue(guild.id)
        logger.info(
            'Admin %d of guild %d ran the weekly problem of %s now',
            ctx.author.id,
            guild.id,
            slot,
        )
        refused = await self._refused_earlier(report)
        await _reply(ctx, _post_now_reply(report, refused=refused, unpicked=unpicked))

    def _jobs(self) -> tuple[ScheduledJob, ...]:
        return (
            # As the bot starts too: until a platform's list is loaded, neither
            # /randproblem nor the weekly problem can pick from it.
            ScheduledJob(
                REFRESH_JOB,
                Every(_REFRESH_INTERVAL),
                self._refresh_problems,
                persistent=False,
                run_on_start=True,
            ),
            # Persistent, so that a fresh install posts nothing at once, and a
            # Friday missed while the bot was down is posted within the grace.
            ScheduledJob(
                WEEKLY_JOB,
                self._schedule,
                self._post_weekly,
                catch_up_grace=_CATCH_UP_GRACE,
            ),
        )

    async def _refresh_problems(self, slot: datetime) -> None:
        """Refresh each platform's problem list that is due (see the catalog).

        A platform that fails keeps the list it had. Each failure is logged,
        then the job fails, so that the scheduler reports it.
        """
        # Each run refreshes the lists as they are now, whichever slot it is for.
        failures = await self._catalog.refresh()
        for platform, error in failures.items():
            owner = platform_possessive(platform)
            if isinstance(error, ExternalServiceError):
                logger.info('Could not refresh %s problem list: %s', owner, error)
            else:
                logger.info('Could not refresh %s problem list', owner, exc_info=error)
        if failures:
            failed = ', '.join(platform_name(platform) for platform in failures)
            message = f'Could not refresh the problem lists of: {failed}'
            raise RuntimeError(message) from next(iter(failures.values()))

    async def _post_weekly(self, slot: datetime) -> None:
        """Post every server's weekly problem for ``slot``; see ``WeeklyService``.

        A run takes the problems it picks out of the queues, so the queues that
        unqueue suggests from are read again afterwards, however it went.
        """
        try:
            await self._weekly.run_slot(slot)
        finally:
            await self._refresh_queues()

    def _in_guild(self, guild_id: int) -> bool:
        """Whether the bot is in the server, even if Discord hasn't sent it
        yet, as just after the bot reconnects.
        """
        return self.bot.get_guild(guild_id) is not None

    async def _refused_earlier(self, report: WeeklyReport) -> str | None:
        """Why Discord refused the post of this week's problem, if it did so
        before this post-now: its delivery is spent, so it can't go out again.
        """
        result = report.problem
        if result is None or result.outcome is not PublishOutcome.ALREADY_HANDLED:
            return None
        row = result.row
        record = await self.services.ledger.get(problem_key(row.guild_id, row.week))
        if record is None or record.status is not DeliveryStatus.SKIPPED:
            return None
        return record.reason or 'no reason given'

    async def _show_current(self, ctx: commands.Context[Any]) -> None:
        guild = _guild(ctx)
        row = await self._weekly.current(guild.id)
        if row is None:
            await ctx.send(embed=info_embed(_WEEKLY_TITLE, _NOTHING_POSTED))
            return
        now = self.services.clock.now()
        lines = [
            f'**Platform:** {platform_name(row.source)}',
            f'**Difficulty:** {self._weekly.difficulty(row)}',
        ]
        if row.topic is not None:
            lines.append(f'**Topic:** {row.topic}')
        lines.append(f'**Posted:** {_when(await self._posted_at(row))}')
        if self._solution_out(row, now):
            links = ' · '.join(_solution_text(link) for link in solution_links(row))
            lines.append(f'**Solution:** {links}')
        else:
            lines.append(f'**Solution:** {_when(self._solution_due(row))}')
        message = OutgoingMessage(
            title=f'Weekly problem: {_title(row)}',
            description='\n'.join(lines),
            url=row.url,
            footer=FOOTER,
        )
        await ctx.send(embed=to_embed(message))

    def _history_pages(
        self, rows: Sequence[WeeklyProblem], now: datetime
    ) -> list[discord.Embed]:
        """/weekly history: ten weeks a page, newest first."""
        if not rows:
            return [info_embed(_HISTORY_TITLE, _NOTHING_POSTED)]
        chunks = [
            rows[start : start + _PER_PAGE] for start in range(0, len(rows), _PER_PAGE)
        ]
        pages = []
        for number, chunk in enumerate(chunks, start=1):
            lines = (self._history_line(row, now) for row in chunk)
            page = info_embed(_HISTORY_TITLE, '\n'.join(lines))
            page.set_footer(text=f'Page {number} of {len(chunks)}')
            pages.append(page)
        return pages

    def _history_line(self, row: WeeklyProblem, now: datetime) -> str:
        """A week of /weekly history: its date, its problem and, once it is
        out, its solution.
        """
        title = _title(row)
        problem = markdown.link(title, row.url)
        if self._solution_out(row, now):
            first = solution_links(row)[0]
            solution = markdown.link(first.label, first.url)
        else:
            solution = f'solution {discord_timestamp(self._solution_due(row), "R")}'
        return f'`{row.week}` {problem} · {solution}'

    def _solution_due(self, row: WeeklyProblem) -> datetime:
        """When the solution of ``row``'s problem is posted: the next slot."""
        return self._schedule.next_after(row.slot)

    def _solution_out(self, row: WeeklyProblem, now: datetime) -> bool:
        """Whether members may see the solution of ``row``'s problem: it was
        posted, or its time has come, even if the server didn't get its post.
        """
        return row.solution_posted or now >= self._solution_due(row)

    async def _posted_at(self, row: WeeklyProblem) -> datetime:
        """When the post of ``row``'s problem went out, as the ledger has it.

        A post-now posts after the slot, and a retry or a catch-up later still.
        """
        record = await self.services.ledger.get(problem_key(row.guild_id, row.week))
        if record is None:  # never, for a problem that was posted
            return row.slot
        return record.sent_at or record.claimed_at

    async def _solution_row(self, guild_id: int, week: str | None) -> WeeklyProblem:
        """The weekly problem whose solution link an admin sets: the one of
        ``week``, else the latest whose solution is still to come.

        A solution is still to come once its problem's post went out, until
        it is posted, unless the runs no longer look back as far as it.
        """
        if week is None:
            rows = await self._repo.history(
                guild_id, at_or_before=self.services.clock.now()
            )
            for row in rows:  # newest first
                if not self._weekly.in_lookback(row):
                    break  # nor will any older one be posted
                if not row.solution_posted and await self._weekly.problem_posted(row):
                    return row
            raise KcpcUserError(_NO_SOLUTION_TO_SET)
        day = _week(week).isoformat()
        found = await self._repo.get_week(guild_id, day)
        if found is None:
            raise KcpcUserError(_NO_PROBLEM_THAT_WEEK.format(week=day))
        title = _bold(_title(found))
        if found.solution_posted:
            raise KcpcUserError(_SOLUTION_POSTED.format(title=title))
        if not await self._weekly.problem_posted(found):
            raise KcpcUserError(_NEVER_POSTED.format(week=day, title=title))
        if not self._weekly.in_lookback(found):
            raise KcpcUserError(_TOO_OLD.format(title=title))
        return found

    async def _solution_note(self, queued: QueuedProblem) -> str:
        """What the solution post of a problem just queued will link, as far as
        can be told now.
        """
        if queued.solution_url is not None:
            link = markdown.link('the link you gave', queued.solution_url)
            return _ADMINS_LINK.format(link=link)
        if queued.source == CODEFORCES:
            return _CONTEST_PAGE
        try:
            editorials = await self._editorials.atcoder(
                queued.contest_id, queued.problem_id
            )
        except KcpcUserError as exc:  # ExternalServiceError is one
            logger.info(
                'Could not look up the editorials of AtCoder %s: %s',
                queued.problem_id,
                exc,
            )
            return _EDITORIALS_UNCHECKED
        best = None if editorials is None else editorials.best()
        if best is None:
            return _NO_EDITORIAL_YET
        link = markdown.link('official editorial', best.url)
        return _EDITORIAL_FOUND.format(link=link)

    async def _exclusion(
        self, guild_id: int, user_id: int, platform: str
    ) -> _Exclusion:
        """The problems the member solved on ``platform``, for /randproblem to
        leave out, as far as can be told within ``_SOLVED_WAIT`` seconds.

        Nothing is left out for a member without an account linked there. A
        site that fails or is slow costs the exclusion, or on AtCoder only the
        part of it not read yet: what was read is kept, and a slow Codeforces
        lookup goes on for the next time (see ``solved``).
        """
        handle = await self._linked_handle(guild_id, user_id, platform)
        if handle is None:
            return _Exclusion(None, None)
        name = platform_name(platform)
        solved: SolvedSet | None
        try:
            solved = await asyncio.wait_for(
                self._fetch_solved(platform, handle), _SOLVED_WAIT
            )
        except asyncio.TimeoutError:
            logger.info(
                'Gave up reading the problems that %s user %s solved after %g seconds',
                name,
                handle,
                _SOLVED_WAIT,
            )
            solved = self._solved_so_far(platform, handle)
        except KcpcUserError as exc:  # ExternalServiceError is one
            logger.info(
                'Could not read the problems that %s user %s solved: %s',
                name,
                handle,
                exc,
            )
            solved = self._solved_so_far(platform, handle)
        if solved is not None and solved.complete:
            # Shown in a footer, which Discord doesn't format: no escaping.
            return _Exclusion(solved, _SOLVED_AS.format(handle=handle))
        return _Exclusion(solved, _NOT_ALL_CHECKED)

    async def _fetch_solved(self, platform: str, handle: str) -> SolvedSet | None:
        """What ``handle`` solved on ``platform``; None if Codeforces has no
        such user.
        """
        if platform == CODEFORCES:
            return await self._solved.codeforces(handle)
        return await self._solved.atcoder(handle)

    def _solved_so_far(self, platform: str, handle: str) -> SolvedSet | None:
        """What has been read of ``handle``'s solved problems, after a lookup
        that didn't finish: some of AtCoder's pages, maybe, but nothing of
        Codeforces', which comes in one request.
        """
        return self._solved.cached_atcoder(handle) if platform == ATCODER else None

    async def _linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        """The member's handle on ``platform``; None if they haven't linked one.

        Codeforces handles are TLE's. AtCoder's come from the feature that
        keeps them, while it is loaded (see ``tle.kcpc.core.handles``).
        """
        if platform == CODEFORCES:
            try:
                return await codeforces_links.linked_handle(self.bot, guild_id, user_id)
            except KcpcUserError:  # TLE runs without its user database
                return None
        return await self.services.handles.linked_handle(guild_id, user_id, platform)

    def _find(self, text: str) -> Problem:
        """The problem an admin named; ``KcpcUserError`` if there's none."""
        ref = parse_problem_ref(text)
        found = self._catalog.find(ref)
        if found is not None:
            return found
        problem = _bold(ref.problem_id)
        if ref.platform == CODEFORCES:
            raise KcpcUserError(_UNKNOWN_CODEFORCES.format(problem=problem))
        raise KcpcUserError(
            _UNKNOWN_PROBLEM.format(
                platform=platform_name(ref.platform), problem=problem
            )
        )

    def _codeforces_lists(self, problem_id: str) -> bool:
        """Whether Codeforces' problemset, as loaded, lists ``problem_id``."""
        return self._catalog.lists(CODEFORCES, problem_id)

    def _known_tags(self) -> frozenset[str]:
        """The tags a topic may be: those of 2026-10, and any Codeforces added."""
        return KNOWN_TAGS | self._catalog.tags()

    async def _refresh_queues(self) -> None:
        """Read every server's queue into memory again."""
        queues = await self._repo.all_queues()
        self._queues = {guild_id: tuple(queue) for guild_id, queue in queues.items()}

    async def _refresh_queue(self, guild_id: int) -> None:
        """Read the server's queue into memory again."""
        queue = tuple(await self._repo.queue(guild_id))
        if queue:
            self._queues[guild_id] = queue
        else:
            self._queues.pop(guild_id, None)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcProblems(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _choices(pairs: Iterable[tuple[str, str]]) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=label, value=value) for label, value in pairs]


def _choice_name(item: QueuedProblem) -> str:
    """A queued problem as unqueue suggests it: its title, cut to fit."""
    title = _queued_title(item)
    return shorten(title, _CHOICE_NAME_LIMIT) or title


def _ref_text(item: QueuedProblem) -> str:
    """The queued problem as ``parse_problem_ref`` reads it back.

    A Codeforces index may start with a digit ('921/01'), which needs the '/'.
    """
    if item.source == ATCODER:
        return item.problem_id
    return f'{item.contest_id}/{item.index}'


def _is_problem(item: QueuedProblem, ref: ProblemRef) -> bool:
    """Whether ``item`` is the problem ``ref`` names; AtCoder's IDs in any case."""
    if item.source != ref.platform:
        return False
    if item.source == ATCODER:
        return item.problem_id.lower() == ref.problem_id.lower()
    return item.problem_id == ref.problem_id


def _position(queue: Sequence[QueuedProblem], item: QueuedProblem) -> int:
    """Where ``item`` is in ``queue``, counting from 1; the end if it isn't."""
    return next(
        (
            number
            for number, queued in enumerate(queue, start=1)
            if queued.queue_id == item.queue_id
        ),
        len(queue),
    )


def _normalized(text: str) -> str:
    """``text`` as topics are compared: lower case, one space between words."""
    return ' '.join(text.split()).lower()


def _nothing_solved(problem: Problem) -> bool:
    return False


def _nothing_found(
    platform: str, topic: str, wanted: DifficultyChoice, excluded: bool
) -> str:
    """Why /randproblem found nothing: no problem of that topic and difficulty."""
    if wanted.band is not None:
        difficulty = f'{wanted.band.value} difficulty'
    else:
        difficulty = f'a rating near {wanted.rating}'
    return _NOTHING_FOUND.format(
        platform=platform_name(platform),
        topic='' if topic == ANY else f' about {topic}',
        difficulty=difficulty,
        solved=_NOT_SOLVED if excluded else '',
    )


def _picked_message(
    picked: Pick, wanted: DifficultyChoice, footer: str | None
) -> OutgoingMessage:
    """/randproblem's reply: the problem, how hard it is, and its topics hidden."""
    problem = picked.problem
    difficulty = describe_difficulty(
        problem.platform, problem.rating, problem.difficulty
    )
    lines = [f'**Difficulty:** {difficulty}']
    if problem.tags:
        # Hidden: a problem's tags give its ideas away.
        lines.append(f'**Topics:** ||{", ".join(problem.tags)}||')
    if problem.solved_count is not None:
        people = 'person' if problem.solved_count == 1 else 'people'
        lines.append(f'**Solved by:** {problem.solved_count} {people}')
    if wanted.rating is not None and picked.distance > _FIRST_WINDOW[problem.platform]:
        window = window_of(problem.platform, picked.distance)
        lines.append(_WIDENED.format(rating=wanted.rating, window=window))
    return OutgoingMessage(
        title=problem.title,
        description='\n'.join(lines),
        url=problem.url,
        footer=footer,
    )


def _post_now_reply(
    report: WeeklyReport, *, refused: str | None = None, unpicked: str | None = None
) -> discord.Embed:
    """What a post-now did: a line per post, green if every one is out.

    ``refused`` is why Discord refused this week's problem post before this
    post-now, if it did, and ``unpicked`` why no problem could be picked after
    the solutions went out.
    """
    results = [*report.solutions, *([report.problem] if report.problem else [])]
    if not results:
        return alert_embed(_NOT_SET_UP)
    lines = []
    for result in report.solutions:
        post = f'the solution of {_bold(_title(result.row))}'
        lines.append(_describe_post(result, post, f'T{post[1:]}'))
    if report.problem is not None:
        title = _bold(_title(report.problem.row))
        post = f"this week's problem, {title}"
        subject = f"This week's problem, {title},"
        if refused is not None:  # handled already, but never out
            lines.append(_REFUSED_EARLIER.format(post=post, reason=_code(refused)))
        else:
            lines.append(_describe_post(report.problem, post, subject))
    elif unpicked is not None:
        lines.append(_PICK_FAILED.format(reason=unpicked))
    elif report.configured:  # a solution couldn't be delivered yet
        lines.append(_PROBLEM_WAITS)
    text = '\n'.join(lines)
    went_out = all(result.outcome in _WENT_OUT for result in results)
    if went_out and refused is None and unpicked is None:
        return success_embed(text)
    return alert_embed(text)


def _describe_post(result: PostResult, post: str, subject: str) -> str:
    """What became of one post of a post-now, which ``post`` and ``subject``
    name (see ``_OUTCOMES``).
    """
    template = _OUTCOMES.get(result.outcome)
    if template is None:  # NOT_CONFIGURED: turned off during the run
        return _NOT_SET_UP
    reason = result.reason or 'no reason given'
    if result.outcome is PublishOutcome.UNDELIVERABLE:
        if reason == _GUILD_UNAVAILABLE:
            reason = _TRY_AGAIN
        else:
            reason = _CHECK_CHANNEL.format(reason=_code(reason))
    elif result.outcome is PublishOutcome.SKIPPED:
        reason = _code(reason)
    return template.format(post=post, subject=subject, reason=reason)


def _next_post(slot: datetime, settings: FeatureSettings) -> str:
    """When the next weekly post goes, and where, or what stops it."""
    fixes = []
    if not settings.enabled:
        fixes.append('turn it on with `/kcpc enable weekly`')
    if settings.channel_id is None:
        fixes.append('set its channel with `/kcpc channel weekly #channel`')
    if not fixes:
        return f'{_when(slot)} in <#{settings.channel_id}>'
    return (
        f'{_when(slot)}, once you {" and ".join(fixes)}. Until then nothing is posted.'
    )


def _this_week(row: WeeklyProblem | None, in_lookback: bool) -> str:
    """This week's problem, and the link its solution post will give.

    ``in_lookback`` says whether the next run looks back as far as the
    problem, so would post its solution.
    """
    if row is None:
        return _NOTHING_YET
    title = _title(row)
    problem = markdown.link(title, row.url)
    return f'{problem}\n**Solution:** {_solution_status(row, in_lookback)}'


def _solution_status(row: WeeklyProblem, in_lookback: bool) -> str:
    """The link the solution post of ``row``'s problem gives, and who set it;
    or that it won't be posted, the problem being too old.
    """
    if row.solution_posted:
        return _SOLUTION_POSTED_ALREADY
    if not in_lookback:
        return _SOLUTION_TOO_OLD
    if row.solution_url is not None:
        if row.solution_set_by is not None:
            link = markdown.link('this link', row.solution_url)
            return _ADMINS_SOLUTION.format(link=link, admin=row.solution_set_by)
        # Only AtCoder's editorials are found by the bot.
        link = markdown.link('official editorial', row.solution_url)
        return _FOUND_EDITORIAL.format(link=link)
    if row.source == ATCODER:
        return _TASK_EDITORIALS_UNTIL
    return _CONTEST_PAGE_UNTIL


def _next_problem(plan: WeeklyPlan) -> str:
    """What the next slot posts: its queued problem, or the rotation's entry."""
    queued = plan.queued
    if queued is None:
        return _NEXT_FROM_ROTATION.format(entry=describe_entry(plan.entry))
    title = _queued_title(queued)
    problem = markdown.link(title, queued.url)
    return f'{problem}, queued by <@{queued.queued_by}>'


def _queue_lines(queue: Sequence[QueuedProblem]) -> str:
    """The queue, oldest first, a problem a line: the first ``_LISTED``, as
    many as fit in an embed's field, then how many more there are.
    """
    if not queue:
        return _EMPTY_QUEUE
    lines: list[str] = []
    for number, item in enumerate(queue[:_LISTED], start=1):
        title = _queued_title(item)
        shown = markdown.escape(shorten(title, _LISTED_TITLE_LIMIT) or title)
        line = f'{number}. {shown} · queued by <@{item.queued_by}>'
        if len(_with_more([*lines, line], len(queue) - number)) > FIELD_VALUE_LIMIT:
            break
        lines.append(line)
    return _with_more(lines, len(queue) - len(lines))


def _rotation_heading(settings: FeatureSettings) -> str:
    if weekly_settings(settings).rotation:
        return _STORED_ROTATION
    return _DEFAULT_ROTATION


def _rotation_lines(
    rotation: Sequence[RotationEntry], next_entry: RotationEntry
) -> str:
    """The rotation, an entry a line, with the next post's marked.

    ``entry_for`` takes the next post's entry out of the rotation itself, so
    it is found by identity: equal entries may be in the rotation more than
    once.
    """
    lines = []
    for number, entry in enumerate(rotation, start=1):
        line = f'{number}. {describe_entry(entry)}'
        marked = f'**{line}** {_NEXT_POST_MARK}'
        lines.append(marked if entry is next_entry else line)
    return '\n'.join(lines)


def _rotation_window(
    rotation: Sequence[RotationEntry], next_entry: RotationEntry
) -> str:
    """The rotation as /kcpc weekly preview shows it: whole if it fits in an
    embed's field, else the entries around the next post's that fit, with how
    many more there are before and after them.
    """
    lines = _rotation_lines(rotation, next_entry).split('\n')
    if _fits_window(lines, 0, len(lines)):
        return '\n'.join(lines)
    start = next(n for n, entry in enumerate(rotation) if entry is next_entry)
    end = start + 1
    grown = True
    while grown:  # one more after the window, then one more before it
        grown = False
        if end < len(lines) and _fits_window(lines, start, end + 1):
            end += 1
            grown = True
        if start > 0 and _fits_window(lines, start - 1, end):
            start -= 1
            grown = True
    return _window(lines, start, end)


def _fits_window(lines: Sequence[str], start: int, end: int) -> bool:
    return len(_window(lines, start, end)) <= FIELD_VALUE_LIMIT


def _window(lines: Sequence[str], start: int, end: int) -> str:
    """``lines[start:end]``, with how many lines there are before and after."""
    shown = list(lines[start:end])
    if start:
        shown.insert(0, _EARLIER.format(count=start))
    return _with_more(shown, len(lines) - end)


def _with_more(lines: Sequence[str], more: int) -> str:
    """``lines``, then how many more there are, if any."""
    if more:
        lines = [*lines, _MORE.format(count=more)]
    return '\n'.join(lines)


def _solution_text(link: SolutionLink) -> str:
    """A link to a solution, as /weekly shows it."""
    if link.label == CONTEST_MATERIALS:
        page = markdown.link('the contest page', link.url)
        return f'Codeforces lists the editorial under **Contest materials** on {page}.'
    return markdown.link(link.label, link.url)


def _title(row: WeeklyProblem) -> str:
    return problem_title(row.source, row.contest_id, row.index, row.name)


def _queued_title(item: QueuedProblem) -> str:
    return problem_title(item.source, item.contest_id, item.index, item.name)


def _when(moment: datetime) -> str:
    return f'{discord_timestamp(moment, "F")} ({discord_timestamp(moment, "R")})'


def _bold(text: str) -> str:
    return f'**{markdown.escape(text)}**'


def _code(text: str) -> str:
    """``text`` as inline code; backticks would end it early, so they go."""
    return '`' + text.replace('`', "'") + '`'


def _week(text: str) -> date:
    """A week an admin gave, as its Friday's date: YYYY-MM-DD."""
    day = text.strip()
    if _WEEK_RE.fullmatch(day) is None:
        raise KcpcUserError(_BAD_WEEK)
    try:
        return date.fromisoformat(day)
    except ValueError:  # such as 2026-02-30
        raise KcpcUserError(_BAD_WEEK) from None


def _web_link(text: str | None) -> str | None:
    """A link an admin typed: an absolute http(s) URL, or None if there's none."""
    url = (text or '').strip()
    # Discord users put a link in <...> to keep it from showing a preview.
    if url.startswith('<') and url.endswith('>'):
        url = url[1:-1].strip()
    if not url:
        return None
    if any(char.isspace() or not char.isprintable() for char in url):
        raise KcpcUserError(_BAD_LINK)
    if not markdown.fits(url):
        raise KcpcUserError(_LONG_LINK)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. an unclosed IPv6 bracket
        raise KcpcUserError(_BAD_LINK) from None
    if parts.scheme.lower() not in ('http', 'https') or not host:
        raise KcpcUserError(_BAD_LINK)
    return url
