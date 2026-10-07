"""Contests: reminders of contests on programming sites and the club's, and commands.

- Members: ``/contests upcoming [platform]`` (also plain ``;contests``) and
  ``/contests live``. They read the database only, which syncing keeps up to
  date.
- Admins: ``/kcpc contests`` to choose the server's platforms and posts
  (start posts, results posts), attached under /kcpc (see
  ``tle.kcpc.bot.admin``). Adding, timing and removing club contests, and
  syncing now, concern every server, so the access rules leave them to the
  bot owner.
- A job per source syncs its platform's contests: Codeforces every 5 minutes
  (from TLE's own cache), AtCoder every 30 minutes, and the ICPC contests of
  ``ICPC_CONTEST_CODES`` every 6 hours. With clist.by's credentials set
  (``CLIST_USERNAME`` and ``CLIST_API_KEY``), CodeChef, LeetCode, TopCoder and
  the ICPC World Finals are read from clist.by, every 30 minutes each. The
  reminder engine's own job posts the reminders, which ``reminders``
  describes, for each platform once all its sources have been synced since the
  bot started.
- After Codeforces and AtCoder contests, members' rating changes are posted
  (see ``results``): the job contests.results runs as the bot starts and then
  every 5 minutes, and the cog passes on TLE's word that it has saved a
  Codeforces contest's rating changes, once the bot is ready.
"""

import contextlib
import logging
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
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
from tle.kcpc.bot.embeds import alert_embed, success_embed, to_embed
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.messages import URL_LIMIT, EmbedField, OutgoingMessage, shorten
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob
from tle.kcpc.core.timeutil import (
    describe_duration,
    discord_timestamp,
    parse_local_datetime,
)
from tle.kcpc.features.contests.reminders import (
    ContestOccurrence,
    ContestReminders,
    contest_details,
    contest_field,
    contest_link,
    platform_line,
    platform_name,
)
from tle.kcpc.features.contests.repo import ContestRepo, SourceState, StoredContest
from tle.kcpc.features.contests.results import (
    RESULTS_INTERVAL,
    RESULTS_JOB,
    ContestResults,
)
from tle.kcpc.features.contests.results_repo import CODEFORCES, ResultRepo
from tle.kcpc.features.contests.settings import (
    CONTESTS,
    MANUAL,
    PLATFORMS,
    contest_settings,
)
from tle.kcpc.features.contests.sources import (
    AtCoderSource,
    CachedContests,
    CodeforcesSource,
    IcpcSource,
    clist_sources,
)
from tle.kcpc.features.contests.sync import ContestSource, ContestSync, SyncReport
from tle.kcpc.platforms.atcoder.contests import AtCoderContestsClient
from tle.kcpc.platforms.atcoder.profile import AtCoderProfileClient
from tle.kcpc.platforms.clist import ClistClient
from tle.kcpc.platforms.icpc import IcpcClient
from tle.util import codeforces_api as cf, events

logger = logging.getLogger(__name__)

# How often each source is synced, by its name. Codeforces' is TLE's cache,
# so syncing it asks Codeforces nothing; ICPC dates rarely change. The last
# four read clist.by, which allows 10 requests a minute.
_SYNC_INTERVALS = {
    'codeforces': timedelta(minutes=5),
    'atcoder': timedelta(minutes=30),
    'icpc': timedelta(hours=6),
    'codechef': timedelta(minutes=30),
    'leetcode': timedelta(minutes=30),
    'topcoder': timedelta(minutes=30),
    'icpc-world-finals': timedelta(minutes=30),
}
# How replies name a source whose platform has another one; the others go by
# their platform's name.
_SOURCE_NAMES = {'icpc-world-finals': 'ICPC World Finals'}

_LISTED = 10  # the most contests /contests upcoming and live show
_MAX_CHOICES = 25  # the most autocomplete suggestions Discord shows
# The most upcoming contests that autocomplete picks from, as an admin types.
_CHOICE_POOL = 100
_CHOICE_NAME_LIMIT = 100  # Discord's limit on a suggestion's name
# The longest name an admin may give a contest: a post about several contests,
# and a suggestion, show no more of a name.
_MAX_NAME_LENGTH = 100
# The most digits a contest ID can have: SQLite's integers are 64-bit.
_MAX_ID_DIGITS = 18
_SHORTEST = timedelta(minutes=1)
_LONGEST = timedelta(days=7)

_NOTHING_UPCOMING = 'No contests are coming up. Check back soon!'
_NOTHING_LIVE = (
    'No contests are running right now. `/contests upcoming` shows the next ones.'
)
_UNKNOWN_PLATFORM = f'There is no such platform. Choose from: {", ".join(PLATFORMS)}.'
_PICK_A_CONTEST = (
    'Pick the contest from the suggestions that appear as you type, or give its ID.'
)
_DURATION_HINT = (
    'Give the duration in hours and minutes, such as 2h, 90m or 1h30m, '
    'from 1 minute to 7 days.'
)
_BAD_LINK = 'The link must be a web address starting with https:// or http://.'
_RESULTS_ON = (
    "Members' rating changes are now posted after each Codeforces and AtCoder "
    'contest of the platforms this server follows, without a ping.'
)
_RESULTS_OFF = "Members' rating changes are no longer posted after contests."
_DATE_AND_TIME = (
    'Give the start as a date and a time, YYYY-MM-DD HH:MM. In a `;kcpc` '
    'command, put it in quotes, as in '
    '`;kcpc contests add Weekly "2026-10-17 10:00" 2h`, or join the two with a '
    'T: 2026-10-17T10:00.'
)

# [0-9] rather than \d, which also matches digits from other scripts; at most
# 5 digits each, so that no timedelta overflows before the range check.
_DURATION_RE = re.compile(
    r'(?:([0-9]{1,5})d)?\s*(?:([0-9]{1,5})h)?\s*(?:([0-9]{1,5})m)?', re.IGNORECASE
)
# A start without its time, as an unquoted start in ;kcpc contests add leaves
# it: the time after the space is taken for the duration.
_BARE_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
_LIST_SEPARATORS = re.compile(r'[\s,]+')

_PLATFORM_CHOICES = [
    app_commands.Choice(name=platform_name(platform), value=platform)
    for platform in PLATFORMS
]
# The platforms by the names that the choices show, such as club for manual,
# the club's own contests: ;contests takes either.
_PLATFORMS_BY_NAME = {
    platform_name(platform).lower(): platform for platform in PLATFORMS
}
# The brief of /contests upcoming, which the prefix twin ;contests upcoming
# shares.
_UPCOMING_BRIEF = (
    "Show upcoming contests on this server's platforms, or on one you choose"
)
# The description of /kcpc contests platforms' option. It names no list of every
# platform, which could outgrow Discord's 100 characters: the command's help
# lists them, and a mistake gets the list.
_PLATFORMS_OPTION = (
    'The platforms to follow, separated by spaces or commas, such as '
    'codeforces atcoder manual'
)


# A subcommand of the cog, as discord.py types it.
_Subcommand = commands.HybridCommand[Any, ..., Any]


def sync_job_name(source: str) -> str:
    """The name of the job that syncs the source: 'contests.sync.<source>'."""
    return f'contests.sync.{source}'


@dataclass(frozen=True)
class _ContestChoice:
    """An upcoming contest that admin commands suggest as an admin types."""

    contest_id: int
    platform: str
    label: str  # '<name> (<date or time>)', in the club's time zone


class KcpcContests(KcpcCog):
    """Contest reminders and results, /contests for members and /kcpc contests
    for admins.

    Its subcommands are declared on their own and put in their groups by
    ``cog_load``. discord.py links a cog's subcommands to their groups by
    qualified name as it makes the cog, and /contests and /kcpc contests share
    one until the latter is attached under /kcpc: declared in their groups,
    the subcommands of one would end up in the other.
    """

    # Set by cog_load, which runs before any command or job of the cog can.
    _repo: ContestRepo
    _sync: ContestSync
    _sources: tuple[ContestSource, ...]
    # The sources this process has tried to sync (see _tried).
    _tried_sources: set[str]
    _reminders: ContestReminders
    # What settime and remove suggest; refreshed after every change.
    _choices: tuple[_ContestChoice, ...]
    _results: ContestResults
    # TLE's event system, if the bot has one, and the cog's listener there.
    _event_sys: events.EventSystem | None
    _listener: events.Listener
    _unloaded: bool
    _db: Database  # for the listener, which may run on after shutdown

    async def cog_load(self) -> None:
        """Start reminding, add the admin commands, then start syncing, and
        listen for Codeforces' rating changes and start the results job.

        If a step fails, the steps before it are undone before the error
        propagates: discord.py doesn't call ``cog_unload`` when ``cog_load``
        raises.
        """
        services = self.services
        self._db = services.db
        self._repo = ContestRepo(services.db, tz=services.settings.tz)
        # One ContestSync for the jobs and the commands, so that syncs of the
        # same source take turns.
        self._sync = ContestSync(services.db, self._repo, services.clock)
        self._sources = (
            CodeforcesSource(self._cached_codeforces_contests, services.clock),
            AtCoderSource(AtCoderContestsClient(services.http), services.clock),
            IcpcSource(IcpcClient(services.http), services.settings.icpc_contest_codes),
            *self._clist_sources(),
        )
        self._tried_sources = set()
        self._reminders = ContestReminders(self._repo)
        self._choices = ()
        self._results = ContestResults(
            self._repo,
            ResultRepo(services.db),
            services.guild_settings,
            services.ledger,
            services.publisher,
            AtCoderProfileClient(services.http),
            services.clock,
            linked_members=self._linked_members,
            in_guild=self._in_guild,
            codeforces_contests=self._cached_codeforces_contests,
            codeforces_changes=self._saved_rating_changes,
        )
        # TLE attaches its event system to the bot before KCPC loads; a bot
        # without TLE's Codeforces features has none.
        self._event_sys = getattr(self.bot, 'event_sys', None)
        self._listener = events.Listener(
            'KcpcContestResults',
            events.RatingChangesUpdate,
            self._on_rating_changes,
            with_lock=True,
        )
        self._unloaded = False
        self._nest_subcommands()
        services.reminders.register(self._reminders)
        added: list[str] = []
        try:
            if not attach_admin_group(self.bot, self.contests_admin):
                withhold_admin_group(self, self.contests_admin)
            for source in self._sources:
                job = self._sync_job(source)
                services.scheduler.add(job)
                added.append(job.name)
            if self._event_sys is not None:
                self._event_sys.add_listener(self._listener)
            services.scheduler.add(self._results_job())
        except BaseException:
            for name in added:
                await services.scheduler.remove(name)
            self._stop_listening()
            # Detaching a group that isn't attached does nothing.
            detach_admin_group(self.bot, self.contests_admin)
            services.reminders.unregister(CONTESTS)
            raise

    async def cog_unload(self) -> None:
        """Stop syncing, reminding and posting results, and take the admin
        commands away.

        A listener task that is running still finishes. As the bot shuts
        down, KCPC's services close before discord.py unloads the cog, so
        such a task may find the database closed even before this runs (see
        ``_on_rating_changes``).
        """
        services = self.services
        self._unloaded = True
        for source in self._sources:
            await services.scheduler.remove(sync_job_name(source.name))
        await services.scheduler.remove(RESULTS_JOB)
        self._stop_listening()
        services.reminders.unregister(CONTESTS)
        detach_admin_group(self.bot, self.contests_admin)

    def _stop_listening(self) -> None:
        """Stop listening to TLE's events, if the cog was."""
        if self._event_sys is None:
            return
        with contextlib.suppress(events.ListenerNotRegistered):
            self._event_sys.remove_listener(self._listener)

    def _nest_subcommands(self) -> None:
        """Put each subcommand in its group (see the class docstring)."""
        member_commands: tuple[_Subcommand, ...] = (self.upcoming, self.live)
        for command in member_commands:
            self.contests.add_command(command)
        admin_commands: tuple[_Subcommand, ...] = (
            self.add_contest,
            self.set_time,
            self.remove_contest,
            self.set_platforms,
            self.set_start_posts,
            self.set_results_posts,
            self.sync_now,
        )
        for command in admin_commands:
            self.contests_admin.add_command(command)

    async def contest_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the upcoming contests whose name or time has what was typed.

        They come from a list in memory: Discord asks again on every keystroke.
        """
        return _matching(self._choices, current)

    async def club_contest_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """As ``contest_autocomplete``, but only club contests, which admins add."""
        return _matching(
            (choice for choice in self._choices if choice.platform == MANUAL), current
        )

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(fallback='upcoming', brief=_UPCOMING_BRIEF)  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(
        platform="The platform whose contests to show; this server's platforms if "
        'left out'
    )
    @app_commands.choices(platform=_PLATFORM_CHOICES)
    async def contests(
        self, ctx: commands.Context[Any], platform: str | None = None
    ) -> None:
        """Show the next 10 contests on this server's platforms, or on the
        platform you choose, with when each starts and how long it runs.

        Examples:
            /contests upcoming
            /contests upcoming platform:atcoder
            ;contests
            ;contests atcoder
        """
        await self._show_upcoming(ctx, platform)

    # The slash command is the group's fallback, which prefix commands lack.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name='upcoming', with_app_command=False, brief=_UPCOMING_BRIEF
    )
    async def upcoming(
        self, ctx: commands.Context[Any], platform: str | None = None
    ) -> None:
        """Show the next 10 contests on this server's platforms, or on the
        platform you choose, with when each starts and how long it runs.

        Examples:
            /contests upcoming
            ;contests upcoming
            ;contests upcoming atcoder
        """
        await self._show_upcoming(ctx, platform)

    @commands.hybrid_command(name='live', brief='Show the contests running now')  # type: ignore[arg-type]
    async def live(self, ctx: commands.Context[Any]) -> None:
        """Show the contests running now on this server's platforms, and when
        each ends.

        Examples:
            /contests live
            ;contests live
        """
        guild = _guild(ctx)
        platforms = await self._platforms(guild.id)
        now = self.services.clock.now()
        contests = await self._repo.live(now, platforms=platforms)
        fields = tuple(_live_field(contest) for contest in contests[:_LISTED])
        message = OutgoingMessage(
            title='Contests running now',
            description=None if fields else _NOTHING_LIVE,
            fields=fields,
            footer=await self._sync_footer(platforms, now),
        )
        await ctx.send(embed=to_embed(message))

    @commands.hybrid_group(  # type: ignore[arg-type]
        name='contests',
        brief='Set up contest reminders, results posts and club contests',
    )
    @kcpc_admin_only()
    async def contests_admin(self, ctx: commands.Context[Any]) -> None:
        """Choose the platforms whose contests this server is reminded of, and
        what gets posted. The bot owner also adds, times and removes club
        contests here.
        """
        # Only ;kcpc contests gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @commands.hybrid_command(name='add', brief='Add a club contest')  # type: ignore[arg-type]
    @app_commands.describe(
        name="The contest's name",
        start='When it starts, in club time: YYYY-MM-DD HH:MM',
        duration='How long it runs, such as 2h, 90m or 1h30m',
        url='Its page, an http or https link; none if left out',
    )
    @kcpc_admin_only()
    async def add_contest(
        self,
        ctx: commands.Context[Any],
        name: commands.Range[str, 1, _MAX_NAME_LENGTH],
        start: str,
        duration: str,
        url: str | None = None,
    ) -> None:
        """Add a club contest, which members are reminded of.

        Every server that follows the `manual` platform, the club's own
        contests, gets its reminders. With the ; command, put quotes around a
        name or a start with a space in it.

        Examples:
            /kcpc contests add name:Weekly contest start:2026-10-17 10:00 duration:2h
            ;kcpc contests add "Weekly contest" "2026-10-17 10:00" 2h
        """
        await ctx.defer(ephemeral=True)
        title = name.strip()
        if not title:
            raise KcpcUserError('Give the contest a name.')
        if _BARE_DATE.fullmatch(start.strip()):
            raise KcpcUserError(_DATE_AND_TIME)
        begins = self._future_start(start)
        end = begins + _duration(duration)
        link = _web_link(url)
        now = self.services.clock.now()
        contest = await self._repo.add_manual(title, begins, end, link, now=now)
        await self._after_change()
        await _reply(
            ctx,
            success_embed(
                f'Added **{contest.name}** (ID {contest.contest_id}). '
                f'{_timing(begins, end)}'
            ),
        )

    @commands.hybrid_command(name='settime', brief="Set a contest's start time")  # type: ignore[arg-type]
    @app_commands.describe(
        contest='The contest: pick one as you type',
        start='When it starts, in club time: YYYY-MM-DD HH:MM',
        duration='How long it runs, such as 5h; unchanged if left out',
    )
    @app_commands.autocomplete(contest=contest_autocomplete)
    @kcpc_admin_only()
    async def set_time(
        self,
        ctx: commands.Context[Any],
        contest: str,
        *,
        start: str,
        duration: str | None = None,
    ) -> None:
        """Set when a contest starts, whatever its site says, such as an ICPC
        contest whose site gives only the date.

        Members are reminded of the new time, and those reminded of the old one
        are told it changed. With the ; command, give the contest's ID, then
        the start and the duration, without quotes.

        Examples:
            /kcpc contests settime contest:12 start:2026-10-17 10:00 duration:5h
            ;kcpc contests settime 12 2026-10-17 10:00 5h
        """
        await ctx.defer(ephemeral=True)
        start_text, duration_text = _start_and_duration(start, duration)
        begins = self._future_start(start_text)
        end = None if duration_text is None else begins + _duration(duration_text)
        updated = await self._repo.set_time(
            _contest_id(contest),
            begins,
            end,
            by=str(ctx.author.id),
            now=self.services.clock.now(),
        )
        await self._after_change()
        timing = _timing(begins, updated.end)
        await _reply(ctx, success_embed(f'**{updated.name}**: {timing}'))

    @commands.hybrid_command(name='remove', brief='Remove a club contest')  # type: ignore[arg-type]
    @app_commands.describe(contest='The club contest: pick one as you type')
    @app_commands.autocomplete(contest=club_contest_autocomplete)
    @kcpc_admin_only()
    async def remove_contest(self, ctx: commands.Context[Any], contest: str) -> None:
        """Remove a club contest added with /kcpc contests add.

        Members who were reminded of it are told it is cancelled.

        Examples:
            /kcpc contests remove 12
            ;kcpc contests remove 12
        """
        await ctx.defer(ephemeral=True)
        removed = await self._repo.cancel_manual(
            _contest_id(contest), now=self.services.clock.now()
        )
        await self._after_change()
        await _reply(
            ctx,
            success_embed(
                f'Removed **{removed.name}**. Members who were reminded of it '
                'are told it is cancelled.'
            ),
        )

    @commands.hybrid_command(  # type: ignore[arg-type]
        name='platforms',
        brief='Choose the platforms whose contests this server follows',
    )
    @app_commands.describe(platforms=_PLATFORMS_OPTION)
    @kcpc_admin_only()
    async def set_platforms(
        self, ctx: commands.Context[Any], *, platforms: str
    ) -> None:
        """Choose the platforms whose contests this server follows: codeforces,
        atcoder, codechef, leetcode, topcoder, icpc and manual, the club's own.

        The platforms you give replace those followed before.

        Examples:
            /kcpc contests platforms codeforces atcoder manual
            ;kcpc contests platforms codeforces, atcoder, icpc
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        chosen = _platform_keys(platforms)
        await self.services.guild_settings.update(guild.id, CONTESTS, platforms=chosen)
        names = ', '.join(platform_name(platform) for platform in chosen)
        await _reply(
            ctx, success_embed(f'This server now follows contests on: {names}.')
        )

    @commands.hybrid_command(  # type: ignore[arg-type]
        name='start-posts', brief='Choose whether each contest gets a post as it starts'
    )
    @app_commands.describe(state='on to post again as each contest starts, off to stop')
    @kcpc_admin_only()
    async def set_start_posts(
        self, ctx: commands.Context[Any], state: Literal['on', 'off']
    ) -> None:
        """Choose whether each contest gets a post as it starts, after its
        reminders.

        Examples:
            /kcpc contests start-posts on
            ;kcpc contests start-posts off
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        on = state == 'on'
        await self.services.guild_settings.update(guild.id, CONTESTS, start_posts=on)
        text = (
            'Each contest now gets a post as it starts, too.'
            if on
            else 'Contests no longer get a post as they start.'
        )
        await _reply(ctx, success_embed(text))

    @commands.hybrid_command(  # type: ignore[arg-type]
        name='results',
        brief="Choose whether members' rating changes are posted after contests",
    )
    @app_commands.describe(
        state="on to post members' rating changes after contests, off to stop"
    )
    @kcpc_admin_only()
    async def set_results_posts(
        self, ctx: commands.Context[Any], state: Literal['on', 'off']
    ) -> None:
        """Choose whether members' rating changes are posted after each
        Codeforces and AtCoder contest they take part in. The posts ping
        nobody.

        Examples:
            /kcpc contests results on
            ;kcpc contests results off
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        on = state == 'on'
        await self.services.guild_settings.update(guild.id, CONTESTS, results_posts=on)
        text = _RESULTS_ON if on else _RESULTS_OFF
        await _reply(ctx, success_embed(text))

    @commands.hybrid_command(name='sync', brief='Sync every contest source now')  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def sync_now(self, ctx: commands.Context[Any]) -> None:
        """Sync every contest source now, and say how each went.

        The sources are Codeforces, AtCoder and ICPC, and with clist.by's
        credentials set, CodeChef, LeetCode, TopCoder and the ICPC World Finals.

        Examples:
            /kcpc contests sync
            ;kcpc contests sync
        """
        await ctx.defer(ephemeral=True)
        reports = await self._sync_sources(*self._sources)
        text = '\n'.join(_describe_report(report) for report in reports)
        ok = all(report.ok for report in reports)
        await _reply(ctx, success_embed(text) if ok else alert_embed(text))

    def _sync_job(self, source: ContestSource) -> ScheduledJob:
        async def sync(slot: datetime) -> None:
            # Each run syncs the source as it is now, whichever slot it is for.
            await self._sync_sources(source)

        return ScheduledJob(
            sync_job_name(source.name),
            Every(_SYNC_INTERVALS[source.name]),
            sync,
            persistent=False,
            run_on_start=True,
        )

    def _results_job(self) -> ScheduledJob:
        """Does what is due about contest results (see ``results``).

        Non-persistent: each run works out from the time and the database
        what is due, including what came due while the bot was down.
        """

        async def post_results(slot: datetime) -> None:
            # Each run works from the time it runs, whichever slot it is for.
            await self._results.run(self.services.clock.now())

        return ScheduledJob(
            RESULTS_JOB,
            Every(RESULTS_INTERVAL),
            post_results,
            persistent=False,
            run_on_start=True,
        )

    async def _on_rating_changes(self, event: events.RatingChangesUpdate) -> None:
        """Post a Codeforces contest's results as TLE saves its rating changes.

        Until the bot is ready, no server's channel is known, so an event
        before then is left to the results job, which catches up once the bot
        is ready. TLE runs this as a task of its own, which may outlast the
        cog and KCPC's database, so errors are logged here.
        """
        if not self.bot.is_ready():
            return
        try:
            await self._results.report_codeforces(event.contest, event.rating_changes)
        except Exception as exc:
            # As the bot shuts down, KCPC's services close kcpc.db before
            # discord.py unloads the cog: either way, there's nothing to
            # report, and the job catches up after the restart.
            expected = (
                self._unloaded or self._db.closed or isinstance(exc, KcpcUserError)
            )
            logger.log(
                logging.INFO if expected else logging.WARNING,
                'Could not post the results of Codeforces contest %d; the '
                'results job tries again',
                event.contest.id,
                exc_info=True,
            )

    async def _linked_members(
        self, guild_id: int, platform: str
    ) -> list[tuple[int, str]]:
        """``(user_id, handle)`` of each member of the guild linked on ``platform``.

        Codeforces handles are TLE's, AtCoder handles whatever feature keeps
        them (see ``tle.kcpc.core.handles``). Either way, only those of the
        guild's members count, as far as the bot knows them: TLE marks the
        handles of members who leave inactive, but AtCoder links stay.
        """
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return []
        if platform == CODEFORCES:
            linked = await codeforces_links.guild_handles(self.bot, guild_id)
        else:
            linked = await self.services.handles.linked_handles(guild_id, platform)
        return [
            (user_id, handle)
            for user_id, handle in linked
            if _is_member(guild, user_id)
        ]

    def _in_guild(self, guild_id: int) -> bool:
        """Whether the bot is in the server, even if Discord hasn't sent it
        yet, as just after the bot reconnects.
        """
        return self.bot.get_guild(guild_id) is not None

    async def _sync_sources(self, *sources: ContestSource) -> list[SyncReport]:
        """Sync each source in turn, then remind at once if that changed anything.

        ``ContestSync`` records and logs a source it can't fetch or read;
        anything else it raises is a bug, which propagates. A platform's
        reminders wait for the first sync of each of its sources since the
        bot started, whether or not that works (see ``_tried``), so reminders
        tick after the last of those too, rather than at the next tick.
        """
        remind = False
        reports: list[SyncReport] = []
        for source in sources:
            try:
                report = await self._sync.sync(source)
            finally:
                released = self._tried(source)
            remind = remind or released or report.changed
            reports.append(report)
        await self._refresh_choices()
        if remind:
            await self.services.reminders.tick()
        return reports

    def _tried(self, source: ContestSource) -> bool:
        """Note that the source has been synced, or tried; True if that lets
        its platform's reminders go out (see ``ContestReminders.occurrences``).

        They wait until each source of the platform has been: ICPC's contests
        come from icpc.global and, through clist.by, the World Finals, and the
        stored contests of either may be from before the bot restarted.
        """
        self._tried_sources.add(source.name)
        platform = source.platform
        if self._reminders.is_synced(platform) or any(
            other.platform == platform and other.name not in self._tried_sources
            for other in self._sources
        ):
            return False
        self._reminders.mark_synced(platform)
        return True

    async def _after_change(self) -> None:
        """After an admin changed a contest, post what that made due at once."""
        await self._refresh_choices()
        await self.services.reminders.tick()

    async def _refresh_choices(self) -> None:
        """Reload the upcoming contests that settime and remove suggest."""
        services = self.services
        contests = await self._repo.upcoming(
            services.clock.now(), platforms=PLATFORMS, limit=_CHOICE_POOL
        )
        tz = services.settings.tz
        self._choices = tuple(
            _ContestChoice(contest.contest_id, contest.platform, _label(contest, tz))
            for contest in contests
        )

    async def _show_upcoming(
        self, ctx: commands.Context[Any], platform: str | None
    ) -> None:
        guild = _guild(ctx)
        if platform is None:
            platforms = await self._platforms(guild.id)
            title = 'Upcoming contests'
        else:
            key = _platform_key(platform)
            platforms = (key,)
            title = f'Upcoming {platform_name(key)} contests'
        now = self.services.clock.now()
        contests = await self._repo.upcoming(now, platforms=platforms, limit=_LISTED)
        fields = tuple(_upcoming_field(contest) for contest in contests)
        message = OutgoingMessage(
            title=title,
            description=None if fields else _NOTHING_UPCOMING,
            fields=fields,
            footer=await self._sync_footer(platforms, now),
        )
        await ctx.send(embed=to_embed(message))

    async def _platforms(self, guild_id: int) -> tuple[str, ...]:
        """The platforms whose contests the server follows."""
        settings = await self.services.guild_settings.get(guild_id, CONTESTS)
        return contest_settings(settings).platforms

    async def _sync_footer(
        self, platforms: Collection[str], now: datetime
    ) -> str | None:
        """When the sources of ``platforms`` last synced, if they have any.

        A footer can't show Discord's timestamp markup, so the times are
        written out, as of ``now``.
        """
        synced = [
            f'{_source_name(source.name)} '
            f'{_ago(await self._repo.source_state(source.name), now)}'
            for source in self._sources
            if source.platform in platforms
        ]
        return f'Last synced: {", ".join(synced)}' if synced else None

    def _future_start(self, text: str) -> datetime:
        """A start that an admin typed in club time; it must be in the future."""
        services = self.services
        start = parse_local_datetime(text, services.settings.tz)
        if start <= services.clock.now():
            raise KcpcUserError(
                f'{text.strip()} has passed. Give a time in the future, in club '
                f'time ({services.settings.kcpc_timezone}).'
            )
        return start

    def _clist_sources(self) -> tuple[ContestSource, ...]:
        """The sources read through clist.by; none without its credentials."""
        settings = self.services.settings
        username, api_key = settings.clist_username, settings.clist_api_key
        # Settings.clist_configured, spelled out so that mypy sees both set.
        if not (username and api_key):
            return ()
        client = ClistClient(self.services.http, username=username, api_key=api_key)
        return clist_sources(client)

    def _cached_codeforces_contests(self) -> CachedContests:
        """The contests in TLE's Codeforces cache; none while the bot has none.

        TLE makes its cache before KCPC starts, but a bot without TLE's
        Codeforces features has none.
        """
        cache = getattr(self.bot, 'cf_cache', None)
        if cache is None:
            return ()
        contests: CachedContests = cache.contest_cache.contests
        return contests

    async def _saved_rating_changes(self, contest_id: int) -> Sequence[cf.RatingChange]:
        """The contest's rating changes as TLE saved them in its cache; none
        while it has none, or the bot has no such cache.

        Reading them asks Codeforces nothing: TLE fetches them itself.
        """
        cache = getattr(self.bot, 'cf_cache', None)
        saved = getattr(cache, 'rating_changes_cache', None)
        if saved is None:
            return ()
        changes: list[cf.RatingChange] = await saved.get_rating_changes_for_contest(
            contest_id
        )
        return changes


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcContests(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


def _is_member(guild: discord.Guild, user_id: int) -> bool:
    """Whether a user is still a member of ``guild``, as far as the bot knows.

    After each new connection to Discord, the bot has only some of a guild's
    members, and Discord sends the rest while it runs. Until it has them all
    (``Guild.chunked``), everyone counts as a member.
    """
    return not guild.chunked or guild.get_member(user_id) is not None


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _upcoming_field(contest: StoredContest) -> EmbedField:
    """A contest coming up: its start and duration, else the day it's on."""
    if contest.start is not None:
        details = contest_details(ContestOccurrence.from_contest(contest))
        return contest_field(contest.name, details)
    lines = [platform_line(contest.platform)]
    if contest.start_date is not None:  # always, without a start time
        day = contest.start_date
        lines.append(f'**Date:** {day:%a} {day.day} {day:%b %Y} · time TBA')
    link = contest_link(contest.url)
    if link is not None:
        lines.append(link)
    return contest_field(contest.name, '\n'.join(lines))


def _live_field(contest: StoredContest) -> EmbedField:
    """A contest running now: when it ends."""
    lines = [platform_line(contest.platform)]
    if contest.end is not None:  # always, while it runs
        lines.append(f'**Ends:** {discord_timestamp(contest.end, "R")}')
    link = contest_link(contest.url)
    if link is not None:
        lines.append(link)
    return contest_field(contest.name, '\n'.join(lines))


def _source_name(source: str) -> str:
    """How replies name a source: by its own name, else by its platform's."""
    return _SOURCE_NAMES.get(source) or platform_name(source)


def _ago(state: SourceState | None, now: datetime) -> str:
    """How long ago a source last synced: '5m ago', say, or 'not yet'."""
    if state is None or state.last_ok is None:
        return 'not yet'
    age = now - state.last_ok
    return 'just now' if age < _SHORTEST else f'{describe_duration(age)} ago'


def _label(contest: StoredContest, tz: tzinfo) -> str:
    """'<name> (<date or time>)', in the club's time zone, as a suggestion."""
    if contest.start is not None:
        when = f'{contest.start.astimezone(tz):%Y-%m-%d %H:%M}'
    else:
        when = str(contest.start_date)
    suffix = f' ({when})'
    name = shorten(contest.name, _CHOICE_NAME_LIMIT - len(suffix)) or contest.name
    return f'{name}{suffix}'


def _matching(
    choices: Iterable[_ContestChoice], typed: str
) -> list[app_commands.Choice[str]]:
    """The first suggestions whose label has ``typed`` in it, in any case."""
    needle = typed.strip().lower()
    return [
        app_commands.Choice(name=choice.label, value=str(choice.contest_id))
        for choice in choices
        if needle in choice.label.lower()
    ][:_MAX_CHOICES]


def _timing(start: datetime, end: datetime | None) -> str:
    """When a contest starts, and for how long it runs if that's known."""
    when = f'{discord_timestamp(start, "F")} ({discord_timestamp(start, "R")})'
    if end is None:
        return f'It starts {when}.'
    duration = describe_duration(end - start, precise=True)
    return f'It starts {when} and runs for {duration}.'


def _contest_id(text: str) -> int:
    """The contest an admin chose: its ID, which a suggestion fills in."""
    digits = text.strip()
    if not (digits.isascii() and digits.isdigit()) or len(digits) > _MAX_ID_DIGITS:
        raise KcpcUserError(_PICK_A_CONTEST)
    return int(digits)


def _start_and_duration(start: str, duration: str | None) -> tuple[str, str | None]:
    """settime's start and duration, which a prefix command gets as one text.

    Its start takes the rest of the message, '2026-10-17 10:00 5h' say. A time
    ends in a word with a ':' in it, so a last word without one is the
    duration. Runs of spaces count as one.
    """
    words = start.split()
    if duration is None and len(words) > 1 and ':' not in words[-1]:
        return ' '.join(words[:-1]), words[-1]
    return ' '.join(words), duration


def _duration(text: str) -> timedelta:
    """A duration an admin typed, such as '2h', '90m' or '1h30m'."""
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None or not any(match.groups()):
        raise KcpcUserError(_DURATION_HINT)
    days, hours, minutes = (int(group or 0) for group in match.groups())
    duration = timedelta(days=days, hours=hours, minutes=minutes)
    if not _SHORTEST <= duration <= _LONGEST:
        raise KcpcUserError(_DURATION_HINT)
    return duration


def _web_link(text: str | None) -> str | None:
    """A link an admin typed: an absolute http(s) URL, or None if there's none."""
    url = (text or '').strip()
    # Discord users put a link in <...> to keep it from showing a preview.
    if url.startswith('<') and url.endswith('>'):
        url = url[1:-1].strip()
    if not url:
        return None
    if len(url) > URL_LIMIT or any(
        char.isspace() or not char.isprintable() for char in url
    ):
        raise KcpcUserError(_BAD_LINK)
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. an unclosed IPv6 bracket
        raise KcpcUserError(_BAD_LINK) from None
    if parts.scheme.lower() not in ('http', 'https') or not host:
        raise KcpcUserError(_BAD_LINK)
    return url


def _platform_key(text: str) -> str:
    """The key of a platform a member chose, by its key or by the name that
    /contests upcoming's choices show, such as Club for manual.
    """
    key = text.strip().lower()
    if key in PLATFORMS:
        return key
    named = _PLATFORMS_BY_NAME.get(key)
    if named is None:
        raise KcpcUserError(_UNKNOWN_PLATFORM)
    return named


def _platform_keys(text: str) -> tuple[str, ...]:
    """The platforms an admin listed, each once, in the order they are shown."""
    keys = {key for key in _LIST_SEPARATORS.split(text.lower()) if key}
    if not keys:
        raise KcpcUserError(f'Name at least one platform: {", ".join(PLATFORMS)}.')
    unknown = sorted(keys.difference(PLATFORMS))
    if unknown:
        listed = ', '.join(f'`{key.replace("`", "")}`' for key in unknown)
        raise KcpcUserError(
            f'Unknown platforms: {listed}. Choose from: {", ".join(PLATFORMS)}.'
        )
    return tuple(platform for platform in PLATFORMS if platform in keys)


def _describe_report(report: SyncReport) -> str:
    """How syncing one source went, in a line."""
    name = f'**{_source_name(report.source)}**'
    if not report.applied:
        return f"{name}: couldn't sync: {report.error}"
    changes = (
        f'{report.added} added, {report.updated} updated, {report.moved} moved, '
        f'{report.cancelled} cancelled, {report.reinstated} reinstated'
    )
    if not report.ok:
        # Failed the health check (see the sync module), but was applied. How
        # long the missing contests take to cancel depends on how often the
        # source syncs, so say it in syncs, as the sync module's
        # _PATIENCE_SHRUNK does.
        return (
            f'{name}: lists far fewer upcoming contests than before '
            f'({report.error}). Applied: {changes}; contests missing from it '
            'count as cancelled only once six syncs in a row have missed them, '
            'the last at least 50 minutes after they were last listed.'
        )
    return f'{name}: {changes}. Upcoming: {report.future_count}.'
