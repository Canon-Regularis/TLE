"""Tests for the contests cog (tle.kcpc.features.contests.cog) and its sources.

The cog runs on real KCPC services (database, settings, ledger, scheduler and
reminder engine), which post through FakePublisher, on a real bot that also
has the admin cog. Only the sites are faked: the cog's AtCoder and ICPC
clients are replaced by FakeSites, and TLE's Codeforces cache, user database
and event system by ones on the bot. Most tests call a command's callback
with a mocked context; the rest go through discord.py, for its checks, its
parsing, its error handling and how it adds and removes the cog. Then come
each source on its own, and contest results.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypeVar, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tests.kcpc.fakes import FakePublisher
from tle import constants
from tle.config import Settings
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.cog import UNEXPECTED_ERROR_MESSAGE
from tle.kcpc.bot.embeds import ALERT_COLOR, KCPC_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.reminders import Notice, ReminderEngine, ReminderPolicy
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import (
    FeatureRegistry,
    FeatureSettings,
    GuildSettingsRepo,
    default_registry,
)
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.admin.cog import setup as add_admin_cog
from tle.kcpc.features.contests import cog as contests_cog
from tle.kcpc.features.contests.cog import KcpcContests, setup, sync_job_name
from tle.kcpc.features.contests.reminders import ContestOccurrence
from tle.kcpc.features.contests.repo import ContestInfo, ContestRepo, StoredContest
from tle.kcpc.features.contests.results import RESULTS_JOB
from tle.kcpc.features.contests.results_repo import (
    CODEFORCES,
    ResultContest,
    ResultOutcome,
    ResultRepo,
    ResultStatus,
)
from tle.kcpc.features.contests.settings import (
    CONTESTS,
    SPEC,
    ContestSettings,
)
from tle.kcpc.features.contests.sources import (
    AtCoderSource,
    CodeforcesSource,
    IcpcSource,
)
from tle.kcpc.features.contests.sync import SourceSnapshot
from tle.kcpc.platforms.atcoder.contests import AtCoderContest, AtCoderContestsClient
from tle.kcpc.platforms.atcoder.profile import AtCoderProfile
from tle.kcpc.platforms.icpc import IcpcClient, IcpcContest
from tle.kcpc.services import KcpcServices
from tle.util import codeforces_api as cf, events

T = TypeVar('T')

# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
ADMIN = 1_300_000_000_000_000_001  # the user ID of the admin in ``ctx``
MEMBER = 1_300_000_000_000_000_002
LEFT = 1_300_000_000_000_000_003  # once a member of GUILD

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
ROUND = 2 * HOUR + 15 * MINUTE  # how long a Codeforces round here runs
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # 13:00 in London
COG_LOGGER = 'tle.kcpc.features.contests.cog'
SOURCES_LOGGER = 'tle.kcpc.features.contests.sources'
ADMIN_LOGGER = 'tle.kcpc.bot.admin'
UNREACHABLE = 'AtCoder is not responding right now. Please try again later.'
UKIEPC_NAME = 'The 2026 ICPC UK & Ireland Programming Contest'
ADMIN_COMMANDS = [
    'add',
    'platforms',
    'remove',
    'results',
    'settime',
    'start-posts',
    'sync',
]
DURATION_HINT = (
    'Give the duration in hours and minutes, such as 2h, 90m or 1h30m, '
    'from 1 minute to 7 days.'
)
BAD_LINK = 'The link must be a web address starting with https:// or http://.'
PICK_A_CONTEST = (
    'Pick the contest from the suggestions that appear as you type, or give its ID.'
)
DATE_AND_TIME = (
    'Give the start as a date and a time, YYYY-MM-DD HH:MM. In a `;kcpc` '
    'command, put it in quotes, as in '
    '`;kcpc contests add Weekly "2026-10-17 10:00" 2h`, or join the two with a '
    'T: 2026-10-17T10:00.'
)
# Real seconds a healthy teardown needs, many times over. One that hangs then
# fails its test instead of stalling the whole run.
TEARDOWN_TIMEOUT = 10


class FakeUserDb:
    """TLE's user database, as far as KCPC reads it: members' Codeforces handles.

    ``handles`` are each guild's active ones: TLE marks those of members who
    leave inactive.
    """

    def __init__(self) -> None:
        self.handles: dict[int, list[tuple[int, str]]] = {}

    async def get_handles_for_guild(self, guild_id: int) -> list[tuple[int, str]]:
        return list(self.handles.get(guild_id, []))


class KcpcBot(commands.Bot):
    """A bot that carries KCPC services and TLE's Codeforces cache, as TLEBot does.

    It is in the servers in ``servers``. TLE's user database is there once a
    test sets it, and so is TLE's event system (``event_sys``).
    """

    kcpc: KcpcServices | None = None
    cf_cache: SimpleNamespace | None = None
    user_db: FakeUserDb | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.servers: dict[int, MagicMock] = {}

    def get_guild(self, id: int, /) -> discord.Guild | None:
        return cast(discord.Guild | None, self.servers.get(id))


def tle_cache(
    *contests: cf.Contest, changes: dict[int, list[cf.RatingChange]] | None = None
) -> SimpleNamespace:
    """TLE's cache system, as far as the cog reads it.

    With ``changes``, it has the rating changes it saved, by contest ID.
    """
    cache = SimpleNamespace(contest_cache=SimpleNamespace(contests=list(contests)))
    if changes is not None:

        async def saved(contest_id: int) -> list[cf.RatingChange]:
            return list(changes.get(contest_id, []))

        cache.rating_changes_cache = SimpleNamespace(
            get_rating_changes_for_contest=saved
        )
    return cache


# Codeforces lists every past contest, so TLE's cache always has some.
FINISHED = cf.Contest(
    1, 'Codeforces Beta Round 1', 1266580800, 7200, 'CF', 'FINISHED', None
)


def cf_round(
    contest_id: int, start: datetime, *, phase: str = 'BEFORE', division: int = 1
) -> cf.Contest:
    return cf.Contest(
        contest_id,
        f'Codeforces Round 1050 (Div. {division})',
        to_epoch(start),
        int(ROUND.total_seconds()),
        'CF',
        phase,
        None,
    )


def atcoder_contest(contest_id: str, start: datetime) -> AtCoderContest:
    return AtCoderContest(
        contest_id=contest_id,
        name=f'AtCoder Beginner Contest {contest_id[3:]}',
        start=start,
        end=start + 100 * MINUTE,
        url=f'https://atcoder.jp/contests/{contest_id}',
        kind='Algorithm',
        rated_range='- 1999',
    )


def ukiepc() -> IcpcContest:
    return IcpcContest(
        code='UKIEPC',
        contest_id='9584',
        name=UKIEPC_NAME,
        start_date=date(2026, 10, 17),
        end_date=date(2026, 10, 17),
        url='https://ukiepc.info/',
    )


class FakeSites:
    """The sites behind the cog's AtCoder and ICPC clients, as the test sets them.

    ``atcoder`` is AtCoder's upcoming table, and ``icpc`` icpc.global's
    contests by code: a code it lacks is unknown, as for a 404. ``errors``
    makes fetching 'atcoder', or an ICPC code, raise. ``fetched`` lists every
    fetch: 'atcoder', or the ICPC code. ``profiles`` are AtCoder's profiles.
    """

    def __init__(self) -> None:
        self.atcoder: list[AtCoderContest] = []
        self.icpc: dict[str, IcpcContest] = {}
        self.errors: dict[str, Exception] = {}
        self.fetched: list[str] = []
        self.profiles = FakeProfiles()

    async def fetch_upcoming(self) -> list[AtCoderContest]:
        return self._answer('atcoder', list(self.atcoder))

    async def fetch(self, code: str) -> IcpcContest | None:
        return self._answer(code, self.icpc.get(code))

    def _answer(self, what: str, answer: T) -> T:
        self.fetched.append(what)
        error = self.errors.get(what)
        if error is not None:
            raise error
        return answer


class FakeProfiles:
    """AtCoder's profiles, by handle in any case; ``read`` lists each read."""

    def __init__(self) -> None:
        self.profiles: dict[str, AtCoderProfile] = {}
        self.read: list[str] = []

    def set(self, handle: str, rating: int, matches: int) -> None:
        self.profiles[handle.lower()] = AtCoderProfile(
            handle=handle,
            rating=rating,
            highest_rating=rating,
            rated_matches=matches,
            affiliation=None,
            color='green',
            url=f'https://atcoder.jp/users/{handle}',
        )

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        self.read.append(handle)
        return self.profiles.get(handle.lower())


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the contest settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
async def services(
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
) -> AsyncIterator[KcpcServices]:
    services = KcpcServices(
        settings=Settings(icpc_contest_codes=('UKIEPC',)),
        clock=clock,
        db=db,
        http=HttpClient(user_agent='kcpc-test', clock=clock),
        features=feature_registry,
        guild_settings=guild_settings,
        ledger=ledger,
        # Contest results post through the publisher itself, and reminders
        # through the engine's: both go to the fake.
        publisher=cast(DiscordPublisher, publisher),
        reminders=ReminderEngine(guild_settings, ledger, publisher, clock),
        scheduler=Scheduler(db, clock),
    )
    yield services
    # The db fixture closes the database.
    await asyncio.wait_for(services.scheduler.stop(), TEARDOWN_TIMEOUT)
    await asyncio.wait_for(services.http.close(), TEARDOWN_TIMEOUT)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> FakeSites:
    sites = FakeSites()

    def client(http: HttpClient) -> FakeSites:
        return sites

    def profiles(http: HttpClient) -> FakeProfiles:
        return sites.profiles

    monkeypatch.setattr(contests_cog, 'AtCoderContestsClient', client)
    monkeypatch.setattr(contests_cog, 'IcpcClient', client)
    monkeypatch.setattr(contests_cog, 'AtCoderProfileClient', profiles)
    return sites


@pytest.fixture
async def admin_bot(services: KcpcServices, sites: FakeSites) -> AsyncIterator[KcpcBot]:
    """A real bot with the KCPC services and the admin cog, as at startup."""
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    bot.cf_cache = tle_cache(FINISHED)
    await add_admin_cog(bot)
    yield bot
    await bot.close()


async def load_contests(bot: commands.Bot) -> KcpcContests:
    """Add the contests cog as its extension does."""
    await setup(bot)
    cog = bot.get_cog('KcpcContests')
    assert isinstance(cog, KcpcContests)
    return cog


@pytest.fixture
async def cog(admin_bot: KcpcBot) -> KcpcContests:
    return await load_contests(admin_bot)


@pytest.fixture
def bot(admin_bot: KcpcBot, cog: KcpcContests) -> KcpcBot:
    """The bot with both cogs."""
    return admin_bot


@pytest.fixture
def repo(db: Database) -> ContestRepo:
    return ContestRepo(db)


def make_member(*, manage_guild: bool) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = ADMIN
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    return member


@pytest.fixture
def guild() -> MagicMock:
    return MagicMock(spec=discord.Guild, id=GUILD)


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    """The context of a command an admin runs; replies are recorded."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = make_member(manage_guild=True)
    ctx.send = AsyncMock()
    ctx.defer = AsyncMock()
    return ctx


def make_context(
    bot: commands.Bot,
    guild: MagicMock,
    author: MagicMock,
    *,
    slash: bool = False,
    args: str = '',
) -> commands.Context[commands.Bot]:
    """A real context in ``guild``; with ``slash``, of a slash command.

    A prefix command reads its arguments from ``args``. Replies are recorded,
    and so is deferring a slash command.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(args),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
        interaction.response.defer = AsyncMock()
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


async def run(
    bot: commands.Bot,
    name: str,
    ctx: MagicMock | commands.Context[Any],
    *args: object,
    **kwargs: object,
) -> None:
    """Call the callback of the command ``name`` with parsed arguments."""
    command = command_named(bot, name)
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args, **kwargs)


async def invoke(bot: commands.Bot, name: str, ctx: commands.Context[Any]) -> None:
    """Run the command as ``Bot.invoke`` does: checks, then its arguments and
    callback, and any error to the command's error handlers.
    """
    command = command_named(bot, name)
    ctx.command = command
    try:
        await command.invoke(ctx)
    except commands.CommandError as error:
        await command.dispatch_error(ctx, error)


def reply(ctx: MagicMock | commands.Context[Any], **kwargs: object) -> discord.Embed:
    """The embed of the one reply, sent with ``kwargs`` besides."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, **kwargs)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def titles(publisher: FakePublisher) -> list[str | None]:
    return [post.message.title for post in publisher.posts]


async def follow(
    guild_settings: GuildSettingsRepo, guild_id: int = GUILD, **settings: object
) -> None:
    """Set the guild up for contests: on, with a channel, and ``settings``."""
    await guild_settings.update(
        guild_id, CONTESTS, enabled=True, channel_id=CHANNEL, **settings
    )


async def sync_all(services: KcpcServices) -> None:
    """Run each sync job once, as at startup."""
    for source in ('codeforces', 'atcoder', 'icpc'):
        await services.scheduler.run_slot(sync_job_name(source))


async def only(repo: ContestRepo, platform: str) -> StoredContest:
    """The one upcoming contest of the platform."""
    (contest,) = await repo.upcoming(NOW, platforms=[platform], limit=2)
    return contest


def stamp(moment: datetime, style: str) -> str:
    return f'<t:{to_epoch(moment)}:{style}>'


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 5 s of real time) until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def test_loading_adds_the_commands_the_reminders_and_the_sync_jobs(
    bot: KcpcBot, cog: KcpcContests, services: KcpcServices
) -> None:
    assert services.reminders.features == [CONTESTS]
    jobs = [
        (job.name, job.description, job.persistent)
        for job in services.scheduler.status()
    ]
    assert jobs == [
        ('contests.results', 'every 5m', False),
        ('contests.sync.atcoder', 'every 30m', False),
        ('contests.sync.codeforces', 'every 5m', False),
        ('contests.sync.icpc', 'every 6h', False),
    ]

    # /contests for members, kept out of DMs; and ;contests upcoming, which
    # the slash command's fallback doesn't give prefix commands.
    members = bot.tree.get_command('contests')
    assert isinstance(members, app_commands.Group)
    assert members.guild_only
    assert sorted(command.name for command in members.commands) == [
        'live',
        'upcoming',
    ]
    group = command_named(bot, 'contests')
    assert isinstance(group, commands.HybridGroup)
    assert sorted(group.all_commands) == ['live', 'upcoming']
    upcoming = command_named(bot, 'contests upcoming')
    assert isinstance(upcoming, commands.HybridCommand)
    assert upcoming.app_command is None

    # /kcpc contests, for admins, on both paths and nowhere else.
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    admin = kcpc.get_command('contests')
    assert isinstance(admin, app_commands.Group)
    assert sorted(command.name for command in admin.commands) == ADMIN_COMMANDS
    for name in ADMIN_COMMANDS:
        assert command_named(bot, f'kcpc contests {name}').cog is cog
    assert set(bot.all_commands) == {'help', 'kcpc', 'contests'}
    assert {command.name for command in bot.tree.get_commands()} == {
        'kcpc',
        'contests',
    }


async def test_the_sync_jobs_run_at_start_then_each_on_its_interval(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    repo: ContestRepo,
) -> None:
    services.scheduler.start()
    # Each job sleeps until its next slot once its first run is over.
    await eventually(
        lambda: clock.pending_sleepers == 4 and len(sites.fetched) == 2,
        'the jobs wait for their next slots',
    )
    assert sorted(sites.fetched) == ['UKIEPC', 'atcoder']
    assert clock.next_deadline == NOW + 5 * MINUTE

    await clock.advance(30 * MINUTE)

    assert sorted(sites.fetched) == ['UKIEPC', 'atcoder', 'atcoder']
    codeforces = await repo.source_state('codeforces')
    assert codeforces is not None and codeforces.last_ok == NOW + 30 * MINUTE


async def test_removing_the_cog_undoes_everything_loading_did(
    bot: KcpcBot, services: KcpcServices
) -> None:
    await bot.remove_cog('KcpcContests')

    assert services.reminders.features == []
    assert services.scheduler.status() == []
    assert bot.get_command('kcpc contests') is None
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('contests') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}

    # So the extension can be loaded again.
    again = await load_contests(bot)
    assert command_named(bot, 'kcpc contests sync').cog is again
    assert command_named(bot, 'contests live').cog is again
    assert services.reminders.features == [CONTESTS]


class StubSource:
    """Another reminder source for the contests feature."""

    feature = CONTESTS

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        return ReminderPolicy(offsets=())

    async def occurrences(
        self, guild_id: int, settings: FeatureSettings, start: datetime, end: datetime
    ) -> list[ContestOccurrence]:
        return []

    def render(self, notice: Notice) -> OutgoingMessage:
        return OutgoingMessage()


async def other_contests(ctx: commands.Context[Any]) -> None:
    """Another /kcpc contests."""


def assert_nothing_left_by_a_failed_load(bot: commands.Bot) -> None:
    assert bot.get_cog('KcpcContests') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}


async def test_a_load_that_cannot_register_the_reminders_changes_nothing(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    services.reminders.register(StubSource())

    with pytest.raises(ValueError, match='already registered'):
        await load_contests(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc contests') is None
    assert services.scheduler.status() == []
    assert services.reminders.features == [CONTESTS]  # still the other one


async def test_a_load_that_cannot_attach_the_admin_commands_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    kcpc = command_named(admin_bot, 'kcpc')
    assert isinstance(kcpc, commands.HybridGroup)
    clashing: commands.HybridGroup[Any, ..., Any] = commands.hybrid_group(
        name='contests'
    )(other_contests)
    kcpc.add_command(clashing)

    with pytest.raises(commands.CommandRegistrationError):
        await load_contests(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc contests') is clashing
    assert services.reminders.features == []
    assert services.scheduler.status() == []


async def test_a_load_that_cannot_add_every_sync_job_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    taken = sync_job_name('atcoder')  # the Codeforces job is added before it
    services.scheduler.add(ScheduledJob(taken, Every(HOUR), other, persistent=False))

    with pytest.raises(ValueError, match='already scheduled'):
        await load_contests(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert [job.name for job in services.scheduler.status()] == [taken]
    assert admin_bot.get_command('kcpc contests') is None
    kcpc = admin_bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('contests') is None
    assert services.reminders.features == []


async def test_without_the_admin_cog_the_feature_runs_without_its_admin_commands(
    services: KcpcServices, sites: FakeSites, caplog: pytest.LogCaptureFixture
) -> None:
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    bot.kcpc = services
    try:
        with caplog.at_level(logging.INFO, logger=ADMIN_LOGGER):
            await load_contests(bot)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == ADMIN_LOGGER
        ] == ['Not adding /kcpc contests: the kcpc.admin extension is not loaded']
        # Not a top-level command that every member would be shown.
        assert set(bot.all_commands) == {'help', 'contests'}
        assert {command.name for command in bot.tree.get_commands()} == {'contests'}
        assert services.reminders.features == [CONTESTS]
        assert len(services.scheduler.status()) == 4

        await bot.remove_cog('KcpcContests')

        assert set(bot.all_commands) == {'help'}
        assert services.reminders.features == []
    finally:
        await bot.close()


async def test_contests_shows_the_next_contests_on_the_servers_platforms(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings)
    start = NOW + 2 * HOUR
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, start))
    sites.icpc['UKIEPC'] = ukiepc()
    await sync_all(services)
    await clock.advance(5 * MINUTE)

    await run(bot, 'contests', ctx)

    # Everyone sees it.
    embed = reply(ctx)
    assert embed.title == 'Upcoming contests'
    assert embed.description is None
    assert [(field.name, field.value) for field in embed.fields] == [
        (
            'Codeforces Round 1050 (Div. 1)',
            '\n'.join(
                [
                    '**Platform:** Codeforces',
                    f'**Starts:** {stamp(start, "F")} ({stamp(start, "R")})',
                    '**Duration:** 2h 15m',
                    '[Contest page](https://codeforces.com/contests/2051)',
                ]
            ),
        ),
        (
            UKIEPC_NAME,
            '\n'.join(
                [
                    '**Platform:** ICPC',
                    '**Date:** Sat 17 Oct 2026 · time TBA',
                    '[Contest page](https://ukiepc.info/)',
                ]
            ),
        ),
    ]
    assert embed.colour == discord.Colour(KCPC_COLOR)
    # A footer can't show a Discord timestamp, so the times are written out.
    assert embed.footer.text == (
        'Last synced: Codeforces 5m ago, AtCoder 5m ago, ICPC 5m ago'
    )


async def test_contests_shows_at_most_10_on_the_servers_platforms_only(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, platforms=('atcoder', 'manual'))
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + HOUR))
    sites.atcoder = [
        atcoder_contest(f'abc{n}', NOW + (n - 399) * DAY) for n in range(400, 412)
    ]
    await sync_all(services)

    await run(bot, 'contests', ctx)

    embed = reply(ctx)
    assert [field.name for field in embed.fields] == [
        f'AtCoder Beginner Contest {n}' for n in range(400, 410)
    ]
    # Club contests have no source to sync.
    assert embed.footer.text == 'Last synced: AtCoder just now'


async def test_contests_can_show_one_platform(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Even one the server doesn't follow.
    await follow(guild_settings, platforms=('codeforces',))
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + HOUR))
    sites.atcoder = [atcoder_contest('abc478', NOW + DAY)]
    await sync_all(services)

    await run(bot, 'contests', ctx, ' AtCoder ')

    embed = reply(ctx)
    assert embed.title == 'Upcoming AtCoder contests'
    assert [field.name for field in embed.fields] == ['AtCoder Beginner Contest 478']
    assert embed.footer.text == 'Last synced: AtCoder just now'


async def test_contests_on_an_unknown_platform_says_which_there_are(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'contests', ctx, 'hackerrank')

    assert str(raised.value) == (
        'There is no such platform. Choose from: codeforces, atcoder, codechef, '
        'leetcode, topcoder, icpc, manual.'
    )
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_contests_without_upcoming_contests_says_so(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)

    await run(bot, 'contests', ctx)

    embed = reply(ctx)
    assert embed.title == 'Upcoming contests'
    assert embed.description == 'No contests are coming up. Check back soon!'
    assert embed.fields == []
    assert embed.footer.text == (
        'Last synced: Codeforces not yet, AtCoder not yet, ICPC not yet'
    )


@pytest.mark.parametrize(
    ('args', 'title'),
    [
        ('', 'Upcoming contests'),
        ('atcoder', 'Upcoming AtCoder contests'),
        ('upcoming', 'Upcoming contests'),
        ('upcoming atcoder', 'Upcoming AtCoder contests'),
    ],
)
async def test_contests_works_as_a_prefix_command_too(
    bot: KcpcBot,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    args: str,
    title: str,
) -> None:
    await follow(guild_settings)
    ctx = make_context(bot, guild, make_member(manage_guild=False), args=args)

    await invoke(bot, 'contests', ctx)

    assert reply(ctx).title == title


@pytest.mark.parametrize('args', ['club', 'upcoming Club'])
async def test_contests_takes_a_platform_by_the_name_its_choices_show(
    bot: KcpcBot,
    guild: MagicMock,
    repo: ContestRepo,
    guild_settings: GuildSettingsRepo,
    args: str,
) -> None:
    # /contests upcoming offers the club's own contests as Club, though their
    # platform's key is manual: a member who saw it there types ;contests club.
    group = bot.tree.get_command('contests')
    assert isinstance(group, app_commands.Group)
    upcoming = group.get_command('upcoming')
    assert isinstance(upcoming, app_commands.Command)
    (platform,) = upcoming.parameters
    assert app_commands.Choice(name='Club', value='manual') in platform.choices
    await follow(guild_settings, platforms=('codeforces',))
    await repo.add_manual(
        'KCPC Autumn Contest', NOW + DAY, NOW + DAY + HOUR, None, now=NOW
    )
    ctx = make_context(bot, guild, make_member(manage_guild=False), args=args)

    await invoke(bot, 'contests', ctx)

    embed = reply(ctx)
    assert embed.title == 'Upcoming Club contests'
    assert [field.name for field in embed.fields] == ['KCPC Autumn Contest']


async def test_contests_live_shows_what_runs_now_and_when_it_ends(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings)
    start = NOW - 30 * MINUTE
    bot.cf_cache = tle_cache(
        FINISHED,
        cf_round(2051, start, phase='CODING'),
        cf_round(2052, NOW + DAY, division=2),
    )
    await sync_all(services)

    await run(bot, 'contests live', ctx)

    embed = reply(ctx)
    assert embed.title == 'Contests running now'
    assert [(field.name, field.value) for field in embed.fields] == [
        (
            'Codeforces Round 1050 (Div. 1)',
            '\n'.join(
                [
                    '**Platform:** Codeforces',
                    f'**Ends:** {stamp(start + ROUND, "R")}',
                    '[Contest page](https://codeforces.com/contests/2051)',
                ]
            ),
        )
    ]
    assert embed.footer.text == (
        'Last synced: Codeforces just now, AtCoder just now, ICPC just now'
    )


async def test_contests_live_without_running_contests_says_so(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, platforms=('manual',))

    await run(bot, 'contests live', ctx)

    embed = reply(ctx)
    # The slash command, which a group without its subcommand isn't.
    assert embed.description == (
        'No contests are running right now. `/contests upcoming` shows the next ones.'
    )
    assert embed.footer.text is None  # club contests have no source


@pytest.mark.parametrize('name', ['contests', 'contests live'])
async def test_member_commands_need_a_server(
    bot: KcpcBot, ctx: MagicMock, name: str
) -> None:
    ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await run(bot, name, ctx)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ['contests', 'contests live', 'contests upcoming'])
async def test_member_commands_are_for_everyone(
    bot: KcpcBot, guild: MagicMock, name: str, slash: bool
) -> None:
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command_named(bot, name).can_run(member)


async def test_adding_a_club_contest_stores_it_and_reminds_at_once(
    bot: KcpcBot,
    cog: KcpcContests,
    ctx: MagicMock,
    repo: ContestRepo,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)

    # 13:50 in London: 12:50 UTC, so its 1h reminder is due.
    await run(
        bot,
        'kcpc contests add',
        ctx,
        ' KCPC Autumn Contest ',
        '2026-10-01 13:50',
        '1h30m',
        '<https://kcpc.example.org/autumn>',
    )

    start = NOW + 50 * MINUTE
    stored = await only(repo, 'manual')
    assert (stored.name, stored.start, stored.end, stored.url) == (
        'KCPC Autumn Contest',
        start,
        start + 90 * MINUTE,
        'https://kcpc.example.org/autumn',
    )
    assert titles(publisher) == ['Starting soon: KCPC Autumn Contest']
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        f'Added **KCPC Autumn Contest** (ID {stored.contest_id}). It starts '
        f'{stamp(start, "F")} ({stamp(start, "R")}) and runs for 1h 30m.'
    )
    # Suggested at once by the commands that take a contest.
    interaction = MagicMock(spec=discord.Interaction)
    (choice,) = await cog.club_contest_autocomplete(interaction, 'autumn')
    assert choice.value == str(stored.contest_id)


@pytest.mark.parametrize(
    ('duration', 'length'),
    [
        ('2h', 2 * HOUR),
        ('90m', 90 * MINUTE),
        ('1H 30M', 90 * MINUTE),
        ('1m', MINUTE),
        ('7d', 7 * DAY),
        ('6d23h60m', 7 * DAY),
    ],
)
async def test_a_duration_is_hours_and_minutes(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: ContestRepo,
    duration: str,
    length: timedelta,
) -> None:
    await run(
        bot, 'kcpc contests add', ctx, 'Club contest', '2026-10-17 10:00', duration
    )

    stored = await only(repo, 'manual')
    assert stored.start is not None and stored.end == stored.start + length
    assert stored.url is None


@pytest.mark.parametrize(
    'duration', ['0m', '7d1m', '90', 'h', '2 hours', '30m1h', '', '999999h', '-5m']
)
async def test_a_duration_outside_1_minute_to_7_days_is_refused(
    bot: KcpcBot, ctx: MagicMock, repo: ContestRepo, duration: str
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(
            bot, 'kcpc contests add', ctx, 'Club contest', '2026-10-17 10:00', duration
        )

    assert str(raised.value) == DURATION_HINT
    assert await repo.upcoming(NOW, platforms=['manual'], limit=1) == []


@pytest.mark.parametrize(
    'url',
    [
        'kcpc.example.org/autumn',
        'ftp://kcpc.example.org/autumn',
        'javascript:alert(1)',
        'https://',
        'https://kcpc example.org',
        'https://[::1',
        'https://kcpc.example.org/' + 'a' * 2048,
    ],
)
async def test_a_link_that_is_no_web_address_is_refused(
    bot: KcpcBot, ctx: MagicMock, repo: ContestRepo, url: str
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(
            bot, 'kcpc contests add', ctx, 'Club contest', '2026-10-17 10:00', '2h', url
        )

    assert str(raised.value) == BAD_LINK
    assert await repo.upcoming(NOW, platforms=['manual'], limit=1) == []


@pytest.mark.parametrize(
    ('name', 'start', 'error'),
    [
        (
            'Club contest',
            '17/10/2026 10:00',
            'Use the format YYYY-MM-DD HH:MM, for example 2026-10-17 10:00.',
        ),
        (
            'Club contest',
            '2026-10-01 12:59',  # 11:59 UTC
            '2026-10-01 12:59 has passed. Give a time in the future, in club time '
            '(Europe/London).',
        ),
        ('Club contest', ' 2026-10-17 ', DATE_AND_TIME),
        ('   ', '2026-10-17 10:00', 'Give the contest a name.'),
    ],
)
async def test_a_contest_needs_a_name_and_a_start_to_come(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: ContestRepo,
    name: str,
    start: str,
    error: str,
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc contests add', ctx, name, start, '2h')

    assert str(raised.value) == error
    assert await repo.upcoming(NOW, platforms=['manual'], limit=1) == []


@pytest.mark.parametrize(
    'args', ['Weekly "2026-10-17 10:00" 2h', '  Weekly  2026-10-17T10:00  2h ']
)
async def test_add_as_a_prefix_command_takes_a_start_in_quotes_or_with_a_t(
    bot: KcpcBot, guild: MagicMock, repo: ContestRepo, args: str
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), args=args)

    await invoke(bot, 'kcpc contests add', ctx)

    assert reply(ctx, ephemeral=True).colour == discord.Colour(SUCCESS_COLOR)
    start = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    stored = await only(repo, 'manual')
    assert stored.name == 'Weekly'
    assert (stored.start, stored.end) == (start, start + 2 * HOUR)


async def test_add_as_a_prefix_command_says_to_quote_a_start_with_a_space(
    bot: KcpcBot, guild: MagicMock, repo: ContestRepo
) -> None:
    # Unquoted, the start splits at its space: the time is taken for the
    # duration, and the duration for the link.
    ctx = make_context(
        bot, guild, make_member(manage_guild=True), args='Weekly 2026-10-17 10:00 2h'
    )

    await invoke(bot, 'kcpc contests add', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == DATE_AND_TIME
    assert await repo.upcoming(NOW, platforms=['manual'], limit=1) == []


async def test_settime_gives_a_contest_known_by_its_date_a_time(
    bot: KcpcBot,
    cog: KcpcContests,
    ctx: MagicMock,
    db: Database,
    services: KcpcServices,
    repo: ContestRepo,
    sites: FakeSites,
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    await sync_all(services)
    interaction = MagicMock(spec=discord.Interaction)
    (before,) = await cog.contest_autocomplete(interaction, 'ireland')
    assert before.name == f'{UKIEPC_NAME} (2026-10-17)'

    await run(
        bot,
        'kcpc contests settime',
        ctx,
        before.value,
        start='2026-10-17 10:00',
        duration='5h',
    )

    start = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    contest = await only(repo, 'icpc')
    assert (contest.start, contest.end, contest.revision) == (
        start,
        start + 5 * HOUR,
        1,
    )
    assert await db.fetchval('SELECT set_by FROM contest_override') == str(ADMIN)
    assert reply(ctx, ephemeral=True).description == (
        f'**{UKIEPC_NAME}**: It starts {stamp(start, "F")} ({stamp(start, "R")}) '
        'and runs for 5h.'
    )
    (after,) = await cog.contest_autocomplete(interaction, 'ireland')
    assert after.name == f'{UKIEPC_NAME} (2026-10-17 10:00)'


async def test_settime_without_a_duration_keeps_the_contests_length(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    repo: ContestRepo,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + 30 * MINUTE))
    await sync_all(services)
    assert titles(publisher) == ['Starting soon: Codeforces Round 1050 (Div. 1)']
    contest = await only(repo, 'codeforces')

    # 14:00 in London: half an hour later.
    await run(
        bot,
        'kcpc contests settime',
        ctx,
        str(contest.contest_id),
        start='2026-10-01 14:00',
    )

    start = NOW + HOUR
    moved = await only(repo, 'codeforces')
    assert (moved.start, moved.end) == (start, start + ROUND)
    # Members who were reminded hear of it at once.
    assert titles(publisher)[1:] == ['Time changed: Codeforces Round 1050 (Div. 1)']
    assert reply(ctx, ephemeral=True).description == (
        f'**Codeforces Round 1050 (Div. 1)**: It starts {stamp(start, "F")} '
        f'({stamp(start, "R")}) and runs for 2h 15m.'
    )


@pytest.mark.parametrize(
    ('contest', 'error'),
    [
        ('999', 'There is no contest with ID 999.'),
        ('UKIEPC', PICK_A_CONTEST),
        ('-1', PICK_A_CONTEST),
        ('1' * 19, PICK_A_CONTEST),  # too big for SQLite
        ('١٢', PICK_A_CONTEST),  # Arabic-Indic digits
    ],
)
async def test_settime_needs_a_contest_that_exists(
    bot: KcpcBot, ctx: MagicMock, contest: str, error: str
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc contests settime', ctx, contest, start='2026-10-17 10:00')

    assert str(raised.value) == error


@pytest.mark.parametrize(
    ('args', 'length'),
    [
        ('{id} 2026-10-17 10:00 5h', 5 * HOUR),
        ('{id}   2026-10-17   10:00   1h30m ', 90 * MINUTE),
        ('{id} 2026-10-17T10:00 5h', 5 * HOUR),
        ('{id} 2026-10-17 10:00', None),
        ('{id} 2026-10-17   10:00 ', None),
    ],
)
async def test_settime_as_a_prefix_command_takes_a_time_with_a_space(
    bot: KcpcBot,
    guild: MagicMock,
    services: KcpcServices,
    repo: ContestRepo,
    sites: FakeSites,
    args: str,
    length: timedelta | None,
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    await sync_all(services)
    contest = await only(repo, 'icpc')
    ctx = make_context(
        bot,
        guild,
        make_member(manage_guild=True),
        args=args.format(id=contest.contest_id),
    )

    await invoke(bot, 'kcpc contests settime', ctx)

    reply(ctx, ephemeral=True)
    start = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    timed = await only(repo, 'icpc')
    assert timed.start == start
    assert timed.end == (None if length is None else start + length)


async def test_removing_a_club_contest_cancels_it_with_a_notice(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: ContestRepo,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    await run(
        bot, 'kcpc contests add', ctx, 'KCPC Autumn Contest', '2026-10-01 13:50', '2h'
    )
    club = await only(repo, 'manual')
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc contests remove', ctx, str(club.contest_id))

    assert await repo.upcoming(NOW, platforms=['manual'], limit=1) == []
    assert titles(publisher) == [
        'Starting soon: KCPC Autumn Contest',
        'Cancelled: KCPC Autumn Contest',
    ]
    assert reply(ctx, ephemeral=True).description == (
        'Removed **KCPC Autumn Contest**. Members who were reminded of it are told '
        'it is cancelled.'
    )


async def test_only_club_contests_can_be_removed(
    bot: KcpcBot, ctx: MagicMock, services: KcpcServices, repo: ContestRepo
) -> None:
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + DAY))
    await sync_all(services)
    contest = await only(repo, 'codeforces')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc contests remove', ctx, str(contest.contest_id))

    assert str(raised.value) == (
        'Only contests added with /kcpc contests add can be removed.'
    )
    assert not (await only(repo, 'codeforces')).cancelled


async def test_settime_and_remove_suggest_upcoming_contests(
    bot: KcpcBot,
    cog: KcpcContests,
    services: KcpcServices,
    repo: ContestRepo,
    sites: FakeSites,
) -> None:
    for name in ('settime', 'remove'):
        command = command_named(bot, f'kcpc contests {name}')
        assert isinstance(command, commands.HybridCommand)
        assert isinstance(command.app_command, app_commands.Command)
        parameter = command.app_command.get_parameter('contest')
        assert parameter is not None and parameter.autocomplete
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + 2 * HOUR))
    sites.icpc['UKIEPC'] = ukiepc()
    club = await repo.add_manual('N' * 100, NOW + DAY, NOW + DAY + HOUR, None, now=NOW)
    await sync_all(services)  # which refreshes the suggestions
    interaction = MagicMock(spec=discord.Interaction)

    suggested = await cog.contest_autocomplete(interaction, '')

    # By start, with the time in London, or the date if that's all there is.
    assert [(choice.name, choice.value) for choice in suggested] == [
        ('Codeforces Round 1050 (Div. 1) (2026-10-01 15:00)', '2'),
        ('N' * 80 + '… (2026-10-02 13:00)', str(club.contest_id)),
        (f'{UKIEPC_NAME} (2026-10-17)', '3'),
    ]
    assert [
        choice.value
        for choice in await cog.contest_autocomplete(interaction, ' uk & IRELAND ')
    ] == ['3']
    # Only club contests can be removed.
    removable = await cog.club_contest_autocomplete(interaction, '')
    assert [choice.value for choice in removable] == [str(club.contest_id)]


async def test_at_most_25_contests_are_suggested(
    cog: KcpcContests, services: KcpcServices, repo: ContestRepo
) -> None:
    for n in range(30):
        start = NOW + (n + 1) * HOUR
        await repo.add_manual(f'Club contest {n}', start, start + HOUR, None, now=NOW)
    await services.scheduler.run_slot(sync_job_name('icpc'))
    interaction = MagicMock(spec=discord.Interaction)

    assert len(await cog.contest_autocomplete(interaction, '')) == 25
    assert len(await cog.club_contest_autocomplete(interaction, 'club')) == 25
    # What was typed finds those further on.
    (found,) = await cog.contest_autocomplete(interaction, 'contest 29')
    assert found.name.startswith('Club contest 29 (')


@pytest.mark.parametrize(
    ('args', 'chosen', 'names'),
    [
        (
            'manual, ICPC codeforces,,icpc',
            ('codeforces', 'icpc', 'manual'),
            'Codeforces, ICPC, Club',
        ),
        ('atcoder', ('atcoder',), 'AtCoder'),
        (
            'topcoder LeetCode,codechef',
            ('codechef', 'leetcode', 'topcoder'),
            'CodeChef, LeetCode, TopCoder',
        ),
    ],
)
async def test_platforms_sets_the_platforms_the_server_follows(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    args: str,
    chosen: tuple[str, ...],
    names: str,
) -> None:
    await run(bot, 'kcpc contests platforms', ctx, platforms=args)

    settings = await guild_settings.get_typed(GUILD, CONTESTS, ContestSettings)
    assert settings.platforms == chosen
    assert reply(ctx, ephemeral=True).description == (
        f'This server now follows contests on: {names}.'
    )


@pytest.mark.parametrize(
    ('args', 'error'),
    [
        (
            'codeforces hackerrank `cf`',
            'Unknown platforms: `cf`, `hackerrank`. Choose from: codeforces, '
            'atcoder, codechef, leetcode, topcoder, icpc, manual.',
        ),
        (
            ' , ',
            'Name at least one platform: codeforces, atcoder, codechef, leetcode, '
            'topcoder, icpc, manual.',
        ),
    ],
)
async def test_platforms_must_all_be_known(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    args: str,
    error: str,
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc contests platforms', ctx, platforms=args)

    assert str(raised.value) == error
    settings = await guild_settings.get_typed(GUILD, CONTESTS, ContestSettings)
    assert settings == ContestSettings()


async def test_platforms_as_a_prefix_command_takes_them_all(
    bot: KcpcBot, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    ctx = make_context(
        bot, guild, make_member(manage_guild=True), args='atcoder manual'
    )

    await invoke(bot, 'kcpc contests platforms', ctx)

    settings = await guild_settings.get_typed(GUILD, CONTESTS, ContestSettings)
    assert settings.platforms == ('atcoder', 'manual')


@pytest.mark.parametrize(
    ('state', 'text'),
    [
        ('on', 'Each contest now gets a post as it starts, too.'),
        ('off', 'Contests no longer get a post as they start.'),
    ],
)
async def test_start_posts_can_be_turned_on_and_off(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    state: str,
    text: str,
) -> None:
    await follow(guild_settings, start_posts=state == 'off')

    await run(bot, 'kcpc contests start-posts', ctx, state)

    settings = await guild_settings.get_typed(GUILD, CONTESTS, ContestSettings)
    assert settings.start_posts == (state == 'on')
    assert reply(ctx, ephemeral=True).description == text


@pytest.mark.parametrize(
    ('state', 'text'),
    [
        (
            'on',
            "Members' rating changes are now posted after each Codeforces and "
            'AtCoder contest of the platforms this server follows, without a ping.',
        ),
        ('off', "Members' rating changes are no longer posted after contests."),
    ],
)
async def test_results_posts_can_be_turned_on_and_off(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    state: str,
    text: str,
) -> None:
    await follow(guild_settings, results_posts=state == 'off')

    await run(bot, 'kcpc contests results', ctx, state)

    settings = await guild_settings.get_typed(GUILD, CONTESTS, ContestSettings)
    assert settings.results_posts == (state == 'on')
    assert reply(ctx, ephemeral=True).description == text


async def test_sync_syncs_every_source_and_says_how_each_went(
    bot: KcpcBot,
    ctx: MagicMock,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + 30 * MINUTE))
    sites.icpc['UKIEPC'] = ukiepc()
    sites.errors['atcoder'] = ExternalServiceError('AtCoder', UNREACHABLE)

    await run(bot, 'kcpc contests sync', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == '\n'.join(
        [
            '**Codeforces**: 1 added, 0 updated, 0 moved, 0 cancelled, '
            '0 reinstated. Upcoming: 1.',
            f"**AtCoder**: couldn't sync: {UNREACHABLE}",
            '**ICPC**: 1 added, 0 updated, 0 moved, 0 cancelled, 0 reinstated. '
            'Upcoming: 1.',
        ]
    )
    # The round's reminder went out at once.
    assert titles(publisher) == ['Starting soon: Codeforces Round 1050 (Div. 1)']

    del sites.errors['atcoder']
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc contests sync', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description is not None
    assert embed.description.splitlines()[1] == (
        '**AtCoder**: 0 added, 0 updated, 0 moved, 0 cancelled, 0 reinstated. '
        'Upcoming: 0.'
    )


async def test_sync_reports_a_list_that_lost_most_of_its_contests(
    bot: KcpcBot, ctx: MagicMock, services: KcpcServices, sites: FakeSites
) -> None:
    sites.atcoder = [
        atcoder_contest(f'abc{n}', NOW + (n - 399) * DAY) for n in range(400, 404)
    ]
    await sync_all(services)
    sites.atcoder = sites.atcoder[:1]

    await run(bot, 'kcpc contests sync', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description is not None
    assert embed.description.splitlines()[1] == (
        '**AtCoder**: lists far fewer upcoming contests than before (contest list '
        'shrank from 4 to 1 upcoming contests). Applied: 0 added, 0 updated, '
        '0 moved, 0 cancelled, 0 reinstated; contests missing from it count as '
        'cancelled only once six syncs in a row have missed them, the last at '
        'least 50 minutes after they were last listed.'
    )


ADMIN_CALLS = [
    ('kcpc contests add', ('Club contest', '2026-10-17 10:00', '2h'), {}),
    ('kcpc contests settime', ('1',), {'start': '2026-10-17 10:00'}),
    ('kcpc contests remove', ('1',), {}),
    ('kcpc contests platforms', (), {'platforms': 'atcoder'}),
    ('kcpc contests start-posts', ('on',), {}),
    ('kcpc contests results', ('off',), {}),
    ('kcpc contests sync', (), {}),
]


@pytest.mark.parametrize(('name', 'args', 'kwargs'), ADMIN_CALLS)
async def test_admin_commands_defer_before_anything_else(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: ContestRepo,
    sites: FakeSites,
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    # A sync waits for the sites, longer than Discord waits for a slash
    # command's first answer; the others answer as fast, deferred alike.
    await repo.add_manual('Club contest', NOW + DAY, NOW + DAY + HOUR, None, now=NOW)
    when_deferred: list[tuple[list[str], int]] = []
    send = cast(AsyncMock, ctx.send)
    cast(AsyncMock, ctx.defer).side_effect = lambda **_: when_deferred.append(
        (list(sites.fetched), send.await_count)
    )

    await run(bot, name, ctx, *args, **kwargs)

    cast(AsyncMock, ctx.defer).assert_awaited_once_with(ephemeral=True)
    assert when_deferred == [([], 0)]
    reply(ctx, ephemeral=True)


async def test_a_slash_admin_command_defers_its_interaction(
    bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), slash=True)

    await run(bot, 'kcpc contests sync', ctx)

    assert ctx.interaction is not None
    defer = cast(AsyncMock, ctx.interaction.response.defer)
    defer.assert_awaited_once_with(ephemeral=True)
    reply(ctx, ephemeral=True)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ADMIN_COMMANDS)
async def test_admin_commands_are_for_admins_only(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    guild: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # Their cog is this one, so the admin cog's check doesn't cover them.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    command = command_named(bot, f'kcpc contests {name}')
    admin = make_context(bot, guild, make_member(manage_guild=True), slash=slash)
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command.can_run(admin)
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(member)


async def test_the_admin_group_on_its_own_is_for_admins_only(
    monkeypatch: pytest.MonkeyPatch, bot: KcpcBot, guild: MagicMock
) -> None:
    # ;kcpc contests; Discord can't run a slash group on its own.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    group = command_named(bot, 'kcpc contests')
    member = make_context(bot, guild, make_member(manage_guild=False))

    with pytest.raises(NotKcpcAdmin):
        await group.can_run(member)


async def test_a_user_error_gets_a_private_reply(
    bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), args='999')

    await invoke(bot, 'kcpc contests remove', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == 'There is no contest with ID 999.'


async def test_a_bug_gets_an_apology_and_is_logged(
    bot: KcpcBot,
    guild: MagicMock,
    sites: FakeSites,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sites.errors['atcoder'] = RuntimeError('boom')
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc contests sync', ctx)

    assert reply(ctx, ephemeral=True).description == UNEXPECTED_ERROR_MESSAGE
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'


async def test_a_sync_job_reminds_at_once_when_contests_changed(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    services: KcpcServices,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    sites.atcoder = [atcoder_contest('abc478', NOW + 30 * MINUTE)]
    tick = AsyncMock(wraps=services.reminders.tick)
    monkeypatch.setattr(services.reminders, 'tick', tick)
    job = sync_job_name('atcoder')

    await services.scheduler.run_slot(job)

    assert sites.fetched == ['atcoder']
    assert tick.await_count == 1
    assert titles(publisher) == ['Starting soon: AtCoder Beginner Contest 478']

    await services.scheduler.run_slot(job)  # nothing changed

    assert tick.await_count == 1

    sites.atcoder.append(atcoder_contest('abc479', NOW + 7 * DAY))
    await services.scheduler.run_slot(job)

    assert tick.await_count == 2


@pytest.mark.parametrize(
    'error',
    [ExternalServiceError('AtCoder', UNREACHABLE), RuntimeError('boom'), None],
    ids=['unreachable', 'bug', 'unchanged'],
)
async def test_reminders_wait_for_a_platforms_first_sync_since_the_bot_started(
    bot: KcpcBot,
    services: KcpcServices,
    db: Database,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    error: Exception | None,
) -> None:
    await follow(guild_settings)
    # Stored before the bot started, so it may have moved since.
    abc = atcoder_contest('abc478', NOW + 30 * MINUTE)
    await ContestRepo(db).add(
        [
            ContestInfo(
                'atcoder', abc.contest_id, abc.name, abc.start, None, abc.end, abc.url
            )
        ],
        now=NOW - DAY,
    )
    sites.atcoder = [abc]

    await services.reminders.tick()  # the reminders job, which starts first

    assert publisher.posts == []

    if error is None:
        await services.scheduler.run_slot(sync_job_name('atcoder'))
    else:
        sites.errors['atcoder'] = error
        if isinstance(error, RuntimeError):
            with pytest.raises(RuntimeError):  # only a bug fails the job
                await services.scheduler.run_slot(sync_job_name('atcoder'))
            await services.reminders.tick()  # the next tick
        else:
            await services.scheduler.run_slot(sync_job_name('atcoder'))

    # While AtCoder can't be read, the stored contests are the best there is.
    assert titles(publisher) == ['Starting soon: AtCoder Beginner Contest 478']


@pytest.mark.parametrize('cache', [None, tle_cache()], ids=['missing', 'empty'])
async def test_the_codeforces_job_waits_for_tles_cache(
    bot: KcpcBot,
    services: KcpcServices,
    repo: ContestRepo,
    cache: SimpleNamespace | None,
) -> None:
    bot.cf_cache = cache

    await services.scheduler.run_slot(sync_job_name('codeforces'))

    # Recorded as a failed sync, which cancels nothing, rather than as a
    # list without any contests.
    state = await repo.source_state('codeforces')
    assert state is not None and state.consecutive_failures == 1
    assert state.last_error == "TLE hasn't loaded Codeforces' contest list yet."
    assert [job.failures for job in services.scheduler.status()] == [0, 0, 0, 0]


async def test_the_codeforces_job_reads_tles_cache(
    bot: KcpcBot, services: KcpcServices, repo: ContestRepo
) -> None:
    bot.cf_cache = tle_cache(FINISHED, cf_round(2051, NOW + DAY))

    await services.scheduler.run_slot(sync_job_name('codeforces'))

    assert (await only(repo, 'codeforces')).external_id == '2051'


async def test_the_codeforces_source_lists_the_rounds_that_have_not_ended(
    clock: FakeClock,
) -> None:
    cached = [
        FINISHED,
        cf_round(2051, NOW + HOUR),
        cf_round(2040, NOW - HOUR, phase='CODING'),
        cf_round(2030, NOW - 3 * HOUR, phase='CODING'),  # over since it was cached
    ]
    source = CodeforcesSource(lambda: cached, clock)

    snapshot = await source.fetch()

    assert (source.name, source.platform) == ('codeforces', 'codeforces')
    assert snapshot == SourceSnapshot(
        [
            ContestInfo(
                'codeforces',
                str(contest_id),
                'Codeforces Round 1050 (Div. 1)',
                start,
                None,
                start + ROUND,
                f'https://codeforces.com/contests/{contest_id}',
            )
            for contest_id, start in ((2040, NOW - HOUR), (2051, NOW + HOUR))
        ],
        complete=True,
    )


async def test_the_codeforces_source_fails_while_tles_cache_is_empty(
    clock: FakeClock,
) -> None:
    with pytest.raises(ExternalServiceError) as raised:
        await CodeforcesSource(lambda: [], clock).fetch()

    assert raised.value.service == 'Codeforces'
    assert str(raised.value) == "TLE hasn't loaded Codeforces' contest list yet."


async def test_the_atcoder_source_lists_its_upcoming_contests(
    clock: FakeClock, sites: FakeSites
) -> None:
    abc = atcoder_contest('abc478', NOW + DAY)
    ended = atcoder_contest('abc477', NOW - 2 * HOUR)
    sites.atcoder = [abc, ended]
    source = AtCoderSource(cast(AtCoderContestsClient, sites), clock)

    snapshot = await source.fetch()

    assert (source.name, source.platform) == ('atcoder', 'atcoder')
    assert snapshot == SourceSnapshot(
        [
            ContestInfo(
                'atcoder',
                'abc478',
                'AtCoder Beginner Contest 478',
                abc.start,
                None,
                abc.end,
                'https://atcoder.jp/contests/abc478',
            )
        ],
        complete=True,
    )


async def test_the_icpc_source_lists_its_contests_by_date(
    sites: FakeSites, caplog: pytest.LogCaptureFixture
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    source = IcpcSource(cast(IcpcClient, sites), ['NWERC-2099', 'UKIEPC'])

    with caplog.at_level(logging.WARNING, logger=SOURCES_LOGGER):
        snapshot = await source.fetch()
        await source.fetch()

    assert (source.name, source.platform) == ('icpc', 'icpc')
    assert snapshot == SourceSnapshot(
        [
            ContestInfo(
                'icpc',
                '9584',
                UKIEPC_NAME,
                None,
                date(2026, 10, 17),
                None,
                'https://ukiepc.info/',
            )
        ],
        complete=False,
    )
    assert sites.fetched == ['NWERC-2099', 'UKIEPC'] * 2
    # A code icpc.global doesn't know is warned about once...
    unknown = (
        'icpc.global has no contest with the code NWERC-2099: check ICPC_CONTEST_CODES'
    )
    assert [record.getMessage() for record in caplog.records] == [unknown]

    # ...and again if it is found and then lost.
    sites.icpc['NWERC-2099'] = ukiepc()
    with caplog.at_level(logging.WARNING, logger=SOURCES_LOGGER):
        await source.fetch()
        del sites.icpc['NWERC-2099']
        await source.fetch()

    assert [record.getMessage() for record in caplog.records] == [unknown, unknown]


async def test_an_icpc_contest_that_cannot_be_fetched_fails_the_fetch(
    sites: FakeSites,
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    error = ExternalServiceError('ICPC', 'icpc.global is not responding right now.')
    sites.errors['NWERC-2099'] = error

    with pytest.raises(ExternalServiceError) as raised:
        await IcpcSource(cast(IcpcClient, sites), ['UKIEPC', 'NWERC-2099']).fetch()

    assert raised.value is error


def test_each_source_has_a_sync_job_of_its_own() -> None:
    assert [sync_job_name(name) for name in ('codeforces', 'atcoder', 'icpc')] == [
        'contests.sync.codeforces',
        'contests.sync.atcoder',
        'contests.sync.icpc',
    ]


# Contest results: the cog's part (see test_contest_results for the rest).

# A Codeforces round that ended an hour and three quarters ago.
ENDED = cf_round(2051, NOW - 4 * HOUR, phase='FINISHED', division=2)
RESULTS_LOGGER = 'tle.kcpc.features.contests.results'


def rating_change(handle: str, place: int, old: int, new: int) -> cf.RatingChange:
    """A rating change in ENDED, as TLE saves it."""
    return cf.RatingChange(
        contestId=ENDED.id,
        contestName=ENDED.name,
        handle=handle,
        rank=place,
        ratingUpdateTimeSeconds=to_epoch(NOW),
        oldRating=old,
        newRating=new,
    )


CHANGES = [
    rating_change('tourist', 1, 3700, 3750),
    rating_change('Amber_Owl', 30, 1500, 1623),
    rating_change('Left_Owl', 40, 1500, 1550),
]


def server(guild_id: int, *members: int, chunked: bool = True) -> MagicMock:
    """A guild with ``members``, all of them once ``chunked``."""
    guild = MagicMock(spec=discord.Guild, id=guild_id, chunked=chunked)
    guild.get_member.side_effect = lambda user_id: (
        MagicMock(spec=discord.Member, id=user_id) if user_id in members else None
    )
    return guild


@pytest.fixture
async def installed(db: Database) -> None:
    """KCPC has posted contest results before: it isn't a new install."""
    old = ResultContest(CODEFORCES, '2000', 'Old round', None, NOW - 30 * DAY)
    await ResultRepo(db).add_done([old], ResultOutcome.POSTED, now=NOW - 30 * DAY)


@pytest.fixture
def results_bot(admin_bot: KcpcBot, installed: None) -> KcpcBot:
    """The admin bot, ready, in GUILD, with TLE's user database and events.

    MEMBER has a Codeforces handle; LEFT had one, which TLE still has as
    active, having missed them leave while it was down.
    """
    admin_bot.event_sys = events.EventSystem()  # type: ignore[attr-defined]
    admin_bot.servers[GUILD] = server(GUILD, ADMIN, MEMBER)
    admin_bot.user_db = FakeUserDb()
    admin_bot.user_db.handles[GUILD] = [(MEMBER, 'amber_owl'), (LEFT, 'Left_Owl')]
    admin_bot.cf_cache = tle_cache(FINISHED, ENDED, changes={})
    admin_bot.is_ready = lambda: True  # type: ignore[method-assign]
    return admin_bot


def listeners(bot: KcpcBot) -> list[events.Listener]:
    event_sys: events.EventSystem = bot.event_sys  # type: ignore[attr-defined]
    return list(event_sys.listeners_by_event.get(events.RatingChangesUpdate, ()))


def dispatch(bot: KcpcBot, changes: list[cf.RatingChange]) -> None:
    """TLE saying it has saved ENDED's rating changes."""
    event_sys: events.EventSystem = bot.event_sys  # type: ignore[attr-defined]
    event_sys.dispatch(
        events.RatingChangesUpdate, contest=ENDED, rating_changes=changes
    )


async def handled(listener: events.Listener) -> None:
    """Wait until the listener has dealt with every event dispatched so far.

    The task of each event takes the listener's lock as it starts, and the
    lock goes to the tasks that wait for it in turn.
    """
    await asyncio.sleep(0)  # the events' tasks start
    assert listener.lock is not None
    async with listener.lock:
        pass


async def test_the_cog_listens_to_tles_rating_changes_while_loaded(
    results_bot: KcpcBot, services: KcpcServices
) -> None:
    cog = await load_contests(results_bot)

    (listener,) = listeners(results_bot)
    assert listener.name == 'KcpcContestResults'
    assert listener.func == cog._on_rating_changes
    assert listener.lock is not None  # one event at a time

    await results_bot.remove_cog('KcpcContests')

    assert listeners(results_bot) == []
    assert RESULTS_JOB not in [job.name for job in services.scheduler.status()]

    again = await load_contests(results_bot)

    (listener,) = listeners(results_bot)
    assert listener.func == again._on_rating_changes


async def test_unloading_after_the_listener_went_is_fine(
    results_bot: KcpcBot,
    services: KcpcServices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await load_contests(results_bot)
    event_sys: events.EventSystem = results_bot.event_sys  # type: ignore[attr-defined]
    (listener,) = listeners(results_bot)
    event_sys.remove_listener(listener)

    await results_bot.remove_cog('KcpcContests')

    # discord.py logs an error that cog_unload raises, rather than raising it:
    # the unload went to its end only if all it undoes is undone.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert results_bot.get_cog('KcpcContests') is None
    assert services.reminders.features == []
    assert services.scheduler.status() == []
    assert results_bot.get_command('kcpc contests') is None
    kcpc = results_bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('contests') is None

    again = await load_contests(results_bot)

    (listener,) = listeners(results_bot)
    assert listener.func == again._on_rating_changes


async def test_without_tles_events_the_results_job_still_runs(
    bot: KcpcBot, services: KcpcServices, caplog: pytest.LogCaptureFixture
) -> None:
    # This bot has no event system, as without TLE's Codeforces features.
    assert not hasattr(bot, 'event_sys')
    assert RESULTS_JOB in [job.name for job in services.scheduler.status()]

    await services.scheduler.run_slot(RESULTS_JOB)
    await bot.remove_cog('KcpcContests')

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_load_that_cannot_add_the_results_job_is_undone(
    results_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    services.scheduler.add(
        ScheduledJob(RESULTS_JOB, Every(HOUR), other, persistent=False)
    )

    with pytest.raises(ValueError, match='already scheduled'):
        await load_contests(results_bot)

    assert_nothing_left_by_a_failed_load(results_bot)
    assert [job.name for job in services.scheduler.status()] == [RESULTS_JOB]
    assert listeners(results_bot) == []
    assert services.reminders.features == []


async def test_a_load_with_tles_events_that_cannot_attach_its_commands_is_undone(
    results_bot: KcpcBot, services: KcpcServices
) -> None:
    kcpc = command_named(results_bot, 'kcpc')
    assert isinstance(kcpc, commands.HybridGroup)
    clashing: commands.HybridGroup[Any, ..., Any] = commands.hybrid_group(
        name='contests'
    )(other_contests)
    kcpc.add_command(clashing)

    # It fails before it listens to TLE's events: the error is the clash's.
    with pytest.raises(commands.CommandRegistrationError):
        await load_contests(results_bot)

    assert_nothing_left_by_a_failed_load(results_bot)
    assert results_bot.get_command('kcpc contests') is clashing
    assert services.reminders.features == []
    assert services.scheduler.status() == []
    assert listeners(results_bot) == []


async def test_a_load_with_tles_events_that_cannot_add_a_sync_job_is_undone(
    results_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    taken = sync_job_name('atcoder')  # the Codeforces job is added before it
    services.scheduler.add(ScheduledJob(taken, Every(HOUR), other, persistent=False))

    with pytest.raises(ValueError, match='already scheduled'):
        await load_contests(results_bot)

    assert_nothing_left_by_a_failed_load(results_bot)
    assert [job.name for job in services.scheduler.status()] == [taken]
    assert results_bot.get_command('kcpc contests') is None
    assert services.reminders.features == []
    assert listeners(results_bot) == []


async def test_the_results_job_runs_as_the_bot_starts(
    results_bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    # TLE saved ENDED's rating changes while the bot was down.
    results_bot.cf_cache = tle_cache(FINISHED, ENDED, changes={ENDED.id: CHANGES})
    await load_contests(results_bot)

    services.scheduler.start()

    # At once: the clock doesn't reach the job's next slot, 5 minutes on.
    await eventually(lambda: len(publisher.posts) == 1, 'the results are posted')
    assert clock.now() == NOW


async def test_tles_rating_changes_are_posted_once_in_each_server(
    results_bot: KcpcBot,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD)
    # The bot isn't in the other server any more.
    await follow(guild_settings, OTHER_GUILD)
    results_bot.user_db.handles[OTHER_GUILD] = [(MEMBER, 'Amber_Owl')]  # type: ignore[union-attr]
    await load_contests(results_bot)
    (listener,) = listeners(results_bot)

    dispatch(results_bot, CHANGES)
    await eventually(lambda: len(publisher.posts) == 1, 'the results are posted')

    (post,) = publisher.posts
    assert post.keys == (f'results:{GUILD}:codeforces:2051',)
    assert not post.message.mention_role
    # In Codeforces' case; LEFT has left, whatever TLE says.
    description = post.message.description
    assert description is not None
    assert description.splitlines() == [
        '**Platform:** Codeforces',
        f'**30.** <@{MEMBER}> [Amber\\_Owl](https://codeforces.com/profile/'
        'Amber_Owl): 1500 → 1623 (**+123**), Specialist → Expert',
    ]

    # Neither the job nor the same news again posts it twice.
    results_bot.cf_cache = tle_cache(FINISHED, ENDED, changes={ENDED.id: CHANGES})
    await services.scheduler.run_slot(RESULTS_JOB)
    dispatch(results_bot, CHANGES)
    await handled(listener)

    assert len(publisher.posts) == 1


async def test_rating_changes_before_the_bot_is_ready_are_left_to_the_job(
    results_bot: KcpcBot,
    services: KcpcServices,
    db: Database,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    results_bot.is_ready = lambda: False  # type: ignore[method-assign]
    await load_contests(results_bot)
    (listener,) = listeners(results_bot)

    dispatch(results_bot, CHANGES)
    await handled(listener)

    assert publisher.posts == []
    assert await ResultRepo(db).get(CODEFORCES, '2051') is None

    # TLE saved them before it said so.
    results_bot.cf_cache = tle_cache(FINISHED, ENDED, changes={ENDED.id: CHANGES})
    await services.scheduler.run_slot(RESULTS_JOB)

    assert [post.keys for post in publisher.posts] == [
        (f'results:{GUILD}:codeforces:2051',)
    ]
    record = await ResultRepo(db).get(CODEFORCES, '2051')
    assert record is not None and record.status is ResultStatus.DONE


async def test_until_the_bot_has_every_member_everyone_counts(
    results_bot: KcpcBot,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    results_bot.servers[GUILD] = server(GUILD, MEMBER, chunked=False)
    await load_contests(results_bot)

    dispatch(results_bot, CHANGES)
    await eventually(lambda: len(publisher.posts) == 1, 'the results are posted')

    description = publisher.posts[0].message.description
    assert description is not None
    assert [line.split()[1] for line in description.splitlines()[1:]] == [
        f'<@{MEMBER}>',
        f'<@{LEFT}>',
    ]


async def test_a_listener_that_outlives_the_cog_logs_its_failure(
    results_bot: KcpcBot,
    db: Database,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    await follow(guild_settings)
    await load_contests(results_bot)

    # TLE's event starts a task, which gets going once the bot has shut down.
    (listener,) = listeners(results_bot)
    assert listener.lock is not None
    async with listener.lock:
        dispatch(results_bot, CHANGES)
        await results_bot.remove_cog('KcpcContests')
        await db.close()
    await handled(listener)

    assert publisher.posts == []
    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.levelno == logging.INFO
    assert record.exc_info is not None
    assert record.exc_info[0] is sqlite3.ProgrammingError  # the database is closed
    assert record.getMessage() == (
        'Could not post the results of Codeforces contest 2051; the results job '
        'tries again'
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_listener_that_finds_kcpc_shut_down_logs_its_failure(
    results_bot: KcpcBot,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    await follow(guild_settings)
    await load_contests(results_bot)

    # TLEBot.close() shuts KCPC down, closing kcpc.db, before discord.py
    # unloads the cog, and TLE's event starts a task that gets going between.
    (listener,) = listeners(results_bot)
    assert listener.lock is not None
    async with listener.lock:
        dispatch(results_bot, CHANGES)
        await services.shutdown()
    await eventually(
        lambda: any(r.name == COG_LOGGER for r in caplog.records), 'it is logged'
    )
    await results_bot.remove_cog('KcpcContests')

    assert publisher.posts == []
    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.levelno == logging.INFO
    assert record.exc_info is not None
    assert record.exc_info[0] is sqlite3.ProgrammingError  # the database is closed
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_listener_failure_after_the_cog_unloaded_is_info(
    monkeypatch: pytest.MonkeyPatch,
    results_bot: KcpcBot,
    guild_settings: GuildSettingsRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    await follow(guild_settings)
    await load_contests(results_bot)

    async def broken(guild_id: int) -> list[tuple[int, str]]:
        raise RuntimeError('boom')

    monkeypatch.setattr(results_bot.user_db, 'get_handles_for_guild', broken)

    # The extension is unloaded, as for a reload, while TLE's event waits; the
    # database stays open.
    (listener,) = listeners(results_bot)
    assert listener.lock is not None
    async with listener.lock:
        dispatch(results_bot, CHANGES)
        await results_bot.remove_cog('KcpcContests')
    await eventually(
        lambda: any(r.name == COG_LOGGER for r in caplog.records), 'it is logged'
    )

    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.levelno == logging.INFO
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'


async def test_a_listener_failure_while_loaded_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    results_bot: KcpcBot,
    guild_settings: GuildSettingsRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await follow(guild_settings)
    await load_contests(results_bot)

    async def broken(guild_id: int) -> list[tuple[int, str]]:
        raise RuntimeError('boom')

    monkeypatch.setattr(results_bot.user_db, 'get_handles_for_guild', broken)

    dispatch(results_bot, CHANGES)
    await eventually(
        lambda: any(r.name == COG_LOGGER for r in caplog.records), 'it is logged'
    )

    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'
    # TLE's own handler logs nothing: the cog did.
    assert [r for r in caplog.records if r.name == 'Listener'] == []


async def test_atcoder_handles_come_from_the_handle_registry(
    results_bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    repo: ContestRepo,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    class AtCoderLinks:
        async def linked_handle(
            self, guild_id: int, user_id: int, platform: str
        ) -> str | None:
            return None

        async def linked_handles(
            self, guild_id: int, platform: str
        ) -> list[tuple[int, str]]:
            # AtCoder links stay when members leave.
            return [(MEMBER, 'Amber_Owl'), (LEFT, 'Left_Owl')]

    await follow(guild_settings)
    services.handles.register('atcoder', AtCoderLinks())
    abc = atcoder_contest('abc478', NOW + 10 * MINUTE)
    info = ContestInfo(
        'atcoder', abc.contest_id, abc.name, abc.start, None, abc.end, abc.url
    )
    await repo.add([info], now=NOW)
    sites.profiles.set('Amber_Owl', 1200, 5)
    sites.profiles.set('Left_Owl', 1500, 9)
    await load_contests(results_bot)

    await clock.advance_to(abc.end - 30 * MINUTE)
    await services.scheduler.run_slot(RESULTS_JOB)

    assert sites.profiles.read == ['Amber_Owl']

    sites.profiles.set('Amber_Owl', 1290, 6)
    await clock.advance_to(abc.end + 15 * MINUTE)
    await services.scheduler.run_slot(RESULTS_JOB)

    (post,) = publisher.posts
    assert post.keys == (f'results:{GUILD}:atcoder:abc478',)
    assert post.message.title == 'Results: AtCoder Beginner Contest 478'
