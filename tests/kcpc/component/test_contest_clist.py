"""Tests for the contests feature's clist.by sources: CodeChef, LeetCode,
TopCoder and the ICPC World Finals.

``ClistSource`` is checked first, on its own and with ``ContestSync``. The rest
run the contests cog on real KCPC services (database, settings, ledger,
scheduler and reminder engine, which posts through FakePublisher), on a real
bot with the admin cog, as test_contests_cog does. Only the sites are faked,
by FakeSites. The clist.by credentials are made up.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tests.kcpc.fakes import FakePublisher
from tle.config import Settings
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.scheduler import Scheduler
from tle.kcpc.core.settings import (
    FeatureRegistry,
    GuildSettingsRepo,
    default_registry,
)
from tle.kcpc.features.admin.cog import setup as add_admin_cog
from tle.kcpc.features.contests import cog as contests_cog
from tle.kcpc.features.contests.cog import setup, sync_job_name
from tle.kcpc.features.contests.repo import (
    ContestInfo,
    ContestRepo,
    ContestStatus,
    SourceState,
)
from tle.kcpc.features.contests.settings import CONTESTS, SPEC
from tle.kcpc.features.contests.sources import ClistSource, clist_sources
from tle.kcpc.features.contests.sync import ContestSync, SourceSnapshot, SyncReport
from tle.kcpc.platforms.clist import ClistClient, ClistContest
from tle.kcpc.platforms.icpc import IcpcContest
from tle.kcpc.services import KcpcServices
from tle.util import codeforces_api as cf

# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
CHANNEL = 1_200_000_000_000_000_001

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
UKIEPC_NAME = 'The 2026 ICPC UK & Ireland Programming Contest'
# Real seconds a healthy teardown needs, many times over.
TEARDOWN_TIMEOUT = 10
SYNCED = '0 updated, 0 moved, 0 cancelled, 0 reinstated.'
SOURCES_LOGGER = 'tle.kcpc.features.contests.sources'

# Codeforces lists every past contest, so TLE's cache always has some.
FINISHED = cf.Contest(
    1, 'Codeforces Beta Round 1', 1266580800, 7200, 'CF', 'FINISHED', None
)


class KcpcBot(commands.Bot):
    """A bot that carries KCPC services and TLE's Codeforces cache, as TLEBot does."""

    kcpc: KcpcServices | None = None
    cf_cache: SimpleNamespace | None = None


def starters(start: datetime) -> ClistContest:
    return ClistContest(
        clist_id=70900001,
        resource='codechef.com',
        name='Starters 210 (Rated)',
        start=start,
        end=start + 2 * HOUR,
        url='https://www.codechef.com/START210',
    )


def placement_prep(start: datetime) -> ClistContest:
    """CodeChef's weekend of practice for job interviews: not a contest,
    although clist.by lists it as one."""
    return ClistContest(
        clist_id=70831805,
        resource='codechef.com',
        name='Placement Prep Weekends - 10',
        start=start,
        end=start + 50 * HOUR,
        url='https://www.codechef.com/PLACEPREP10',
    )


def world_finals(start: datetime) -> ClistContest:
    return ClistContest(
        clist_id=71000001,
        resource='icpc.global',
        name='The 2027 ICPC World Finals',
        start=start,
        end=start + 5 * HOUR,
        url='https://worldfinals.icpc.global/2027/',
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
    """The sites behind the cog's clients, as the test sets them.

    ``clist`` is clist.by's contests by resource, and ``icpc`` icpc.global's
    contests by code: a code it lacks is unknown. AtCoder lists none. ``asked``
    records each request to clist.by, its resource and event regex, and
    ``credentials`` those that each clist.by client was made with. While
    ``clist_error`` is set, each request to clist.by raises it instead.
    """

    def __init__(self) -> None:
        self.clist: dict[str, list[ClistContest]] = {}
        self.icpc: dict[str, IcpcContest] = {}
        self.asked: list[tuple[str, str | None]] = []
        self.credentials: list[tuple[str, str]] = []
        self.clist_error: ExternalServiceError | None = None

    async def upcoming(
        self, resource: str, *, event_regex: str | None = None
    ) -> list[ClistContest]:
        self.asked.append((resource, event_regex))
        if self.clist_error is not None:
            raise self.clist_error
        return list(self.clist.get(resource, ()))

    async def fetch_upcoming(self) -> list[object]:
        return []

    async def fetch(self, code: str) -> IcpcContest | None:
        return self.icpc.get(code)


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
def credentials() -> tuple[str | None, str | None]:
    """The clist.by username and API key in the bot's settings."""
    return 'test-user', 'test-key'


@pytest.fixture
async def services(
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
    credentials: tuple[str | None, str | None],
) -> AsyncIterator[KcpcServices]:
    username, api_key = credentials
    services = KcpcServices(
        settings=Settings(
            icpc_contest_codes=('UKIEPC',),
            clist_username=username,
            clist_api_key=api_key,
        ),
        clock=clock,
        db=db,
        http=HttpClient(user_agent='kcpc-test', clock=clock),
        features=feature_registry,
        guild_settings=guild_settings,
        ledger=ledger,
        publisher=DiscordPublisher(
            MagicMock(spec=commands.Bot), guild_settings, ledger, clock
        ),
        # Reminders post through the fake, as they would through publisher.
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

    def clist_client(http: HttpClient, *, username: str, api_key: str) -> FakeSites:
        sites.credentials.append((username, api_key))
        return sites

    monkeypatch.setattr(contests_cog, 'AtCoderContestsClient', client)
    monkeypatch.setattr(contests_cog, 'IcpcClient', client)
    monkeypatch.setattr(contests_cog, 'ClistClient', clist_client)
    return sites


@pytest.fixture
async def bot(services: KcpcServices, sites: FakeSites) -> AsyncIterator[KcpcBot]:
    """A real bot with the KCPC services, the admin cog and the contests cog."""
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    bot.cf_cache = SimpleNamespace(contest_cache=SimpleNamespace(contests=[FINISHED]))
    await add_admin_cog(bot)
    await setup(bot)
    yield bot
    await bot.close()


@pytest.fixture
def repo(db: Database) -> ContestRepo:
    return ContestRepo(db)


@pytest.fixture
def ctx() -> MagicMock:
    """The context of a command an admin runs in GUILD; replies are recorded."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = MagicMock(spec=discord.Guild, id=GUILD)
    ctx.send = AsyncMock()
    ctx.defer = AsyncMock()
    return ctx


async def run(bot: commands.Bot, name: str, ctx: MagicMock, *args: object) -> None:
    """Call the callback of the command ``name``."""
    command = bot.get_command(name)
    assert command is not None, name
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args)


def reply(ctx: MagicMock, **kwargs: object) -> discord.Embed:
    """The embed of the one reply, sent with ``kwargs`` besides."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, **kwargs)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


async def follow(guild_settings: GuildSettingsRepo) -> None:
    """Set GUILD up for contests on every platform: on, with a channel."""
    await guild_settings.update(GUILD, CONTESTS, enabled=True, channel_id=CHANNEL)


async def test_each_clist_source_reads_its_site(sites: FakeSites) -> None:
    sources = clist_sources(cast(ClistClient, sites))
    snapshots = [await source.fetch() for source in sources]

    assert [(source.name, source.platform) for source in sources] == [
        ('codechef', 'codechef'),
        ('leetcode', 'leetcode'),
        ('topcoder', 'topcoder'),
        ('icpc-world-finals', 'icpc'),
    ]
    assert sites.asked == [
        ('codechef.com', None),
        ('leetcode.com', None),
        ('topcoder.com', None),
        ('icpc.global', 'world finals'),
    ]
    # The ICPC source lists ICPC contests too, so a World Finals snapshot
    # can't have all of them.
    assert [snapshot.complete for snapshot in snapshots] == [True, True, True, False]


@pytest.mark.parametrize('complete', [True, False])
async def test_a_clist_source_lists_its_contests_by_their_clist_ids(
    sites: FakeSites, complete: bool
) -> None:
    contest = starters(NOW + DAY)
    sites.clist['codechef.com'] = [contest]
    source = ClistSource(
        cast(ClistClient, sites),
        name='chef',
        platform='codechef',
        resource='codechef.com',
        complete=complete,
    )

    snapshot = await source.fetch()

    assert (source.name, source.platform) == ('chef', 'codechef')
    assert snapshot == SourceSnapshot(
        [
            ContestInfo(
                'codechef',
                'clist-70900001',
                'Starters 210 (Rated)',
                contest.start,
                None,
                contest.end,
                'https://www.codechef.com/START210',
            )
        ],
        complete=complete,
    )
    assert sites.asked == [('codechef.com', None)]


async def test_codechefs_events_that_are_not_contests_are_left_out(
    sites: FakeSites, caplog: pytest.LogCaptureFixture
) -> None:
    sites.clist['codechef.com'] = [placement_prep(NOW - HOUR), starters(NOW + DAY)]
    codechef = clist_sources(cast(ClistClient, sites))[0]

    with caplog.at_level(logging.DEBUG, logger=SOURCES_LOGGER):
        snapshot = await codechef.fetch()

    assert [info.name for info in snapshot.contests] == ['Starters 210 (Rated)']
    assert snapshot.complete
    # At DEBUG: every sync skips it again, and a warning would reach the
    # Discord log channel every 30 minutes.
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == SOURCES_LOGGER
    ] == [
        (
            logging.DEBUG,
            "Skipping codechef.com event 'Placement Prep Weekends - 10' (clist.by ID "
            '70831805), which is not a contest',
        )
    ]


async def test_a_codechef_list_of_events_that_are_not_contests_syncs_none(
    db: Database, clock: FakeClock, repo: ContestRepo, sites: FakeSites
) -> None:
    # It is a list of no contests, not one that can't be read.
    sites.clist['codechef.com'] = [placement_prep(NOW + HOUR)]
    codechef = clist_sources(cast(ClistClient, sites))[0]

    report = await ContestSync(db, repo, clock).sync(codechef)

    assert report == SyncReport('codechef', ok=True)
    assert await repo.source_records('codechef') == []


@pytest.mark.parametrize(
    'error',
    [
        'clist.by has no site named codechef.com.',
        "clist.by's contest list could not be read.",
    ],
    ids=['unknown site', 'unreadable list'],
)
async def test_a_failed_clist_read_cancels_nothing_and_is_recorded(
    db: Database,
    clock: FakeClock,
    repo: ContestRepo,
    sites: FakeSites,
    caplog: pytest.LogCaptureFixture,
    error: str,
) -> None:
    caplog.set_level(logging.WARNING, logger='tle.kcpc.features.contests.sync')
    sites.clist['codechef.com'] = [starters(NOW + DAY)]
    codechef = clist_sources(cast(ClistClient, sites))[0]
    sync = ContestSync(db, repo, clock)
    await sync.sync(codechef)
    sites.clist_error = ExternalServiceError('clist.by', error)

    # Two hours of failed reads. Had they been empty lists, the third would
    # have cancelled the contest.
    reports: list[SyncReport] = []
    for _ in range(4):
        await clock.advance(30 * MINUTE)
        reports.append(await sync.sync(codechef))

    assert reports == 4 * [SyncReport('codechef', ok=False, error=error, applied=False)]
    (contest,) = await repo.upcoming(clock.now(), platforms=('codechef',), limit=5)
    assert (contest.name, contest.status, contest.miss_count) == (
        'Starters 210 (Rated)',
        ContestStatus.SCHEDULED,
        0,
    )
    assert await repo.source_state('codechef') == SourceState(
        source='codechef',
        last_attempt=NOW + 2 * HOUR,
        last_ok=NOW,
        last_future_count=1,
        consecutive_failures=4,
        last_error=error,
    )
    # Admins hear of it in the Discord log channel, once: at the third.
    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ] == [f'Could not sync contest source codechef (consecutive failures: 3): {error}']


@pytest.mark.parametrize(
    'credentials',
    [(None, None), ('test-user', None), (None, 'test-key')],
    ids=['none', 'no key', 'no username'],
)
async def test_without_clist_credentials_there_are_no_clist_jobs(
    bot: KcpcBot, services: KcpcServices, sites: FakeSites
) -> None:
    assert [job.name for job in services.scheduler.status()] == [
        'contests.sync.atcoder',
        'contests.sync.codeforces',
        'contests.sync.icpc',
    ]
    assert sites.credentials == []


async def test_with_clist_credentials_each_clist_source_has_a_job(
    bot: KcpcBot, services: KcpcServices, sites: FakeSites
) -> None:
    jobs = [(job.name, job.description) for job in services.scheduler.status()]

    assert jobs == [
        ('contests.sync.atcoder', 'every 30m'),
        ('contests.sync.codechef', 'every 30m'),
        ('contests.sync.codeforces', 'every 5m'),
        ('contests.sync.icpc', 'every 6h'),
        ('contests.sync.icpc-world-finals', 'every 30m'),
        ('contests.sync.leetcode', 'every 30m'),
        ('contests.sync.topcoder', 'every 30m'),
    ]
    assert sites.credentials == [('test-user', 'test-key')]

    await bot.remove_cog('KcpcContests')

    assert services.scheduler.status() == []


async def test_contests_offers_every_platform(bot: KcpcBot) -> None:
    members = bot.tree.get_command('contests')
    assert isinstance(members, app_commands.Group)
    upcoming = members.get_command('upcoming')
    assert isinstance(upcoming, app_commands.Command)

    (platform,) = upcoming.parameters

    assert [(choice.name, choice.value) for choice in platform.choices] == [
        ('Codeforces', 'codeforces'),
        ('AtCoder', 'atcoder'),
        ('CodeChef', 'codechef'),
        ('LeetCode', 'leetcode'),
        ('TopCoder', 'topcoder'),
        ('ICPC', 'icpc'),
        ('Club', 'manual'),
    ]


async def test_a_codechef_contest_gets_a_reminder(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    contest = starters(NOW + 90 * MINUTE)
    sites.clist['codechef.com'] = [contest]

    await services.scheduler.run_slot(sync_job_name('codechef'))

    assert publisher.posts == []  # it is due an hour before the start

    await clock.advance(30 * MINUTE)
    await services.reminders.tick()

    (post,) = publisher.posts
    assert post.message.title == 'Starting soon: Starters 210 (Rated)'
    assert post.message.url == 'https://www.codechef.com/START210'
    assert post.message.description is not None
    assert post.message.description.splitlines()[0] == '**Platform:** CodeChef'


async def test_a_codechef_event_that_is_not_a_contest_is_never_stored_or_announced(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: ContestRepo,
) -> None:
    await follow(guild_settings)
    await guild_settings.update(GUILD, CONTESTS, start_posts=True)
    sites.clist['codechef.com'] = [
        placement_prep(NOW + 70 * MINUTE),
        starters(NOW + 90 * MINUTE),
    ]

    await services.scheduler.run_slot(sync_job_name('codechef'))
    # Every 10 minutes, past both reminders and both starts.
    for _ in range(9):
        await clock.advance(10 * MINUTE)
        await services.reminders.tick()

    (record,) = await repo.source_records('codechef')
    assert record.reported.name == 'Starters 210 (Rated)'
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Starters 210 (Rated)',
        'Starting now: Starters 210 (Rated)',
    ]


async def test_the_world_finals_and_icpc_global_never_cancel_each_others_contests(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    repo: ContestRepo,
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    sites.clist['icpc.global'] = [world_finals(NOW + 200 * DAY)]

    async def sync_alone(source: str) -> None:
        # 4 syncs, the last 40 minutes after the other source last listed its
        # contest: enough to cancel that contest if these snapshots claimed to
        # be complete (3 that miss it, the last at least 20 minutes after).
        for _ in range(4):
            await clock.advance(10 * MINUTE)
            await services.scheduler.run_slot(sync_job_name(source))

    async def assert_both_scheduled() -> None:
        contests = await repo.upcoming(clock.now(), platforms=('icpc',), limit=5)
        assert [
            (contest.name, contest.status, contest.miss_count) for contest in contests
        ] == [
            (UKIEPC_NAME, ContestStatus.SCHEDULED, 0),
            ('The 2027 ICPC World Finals', ContestStatus.SCHEDULED, 0),
        ]

    # The World Finals sync every 30 minutes, icpc.global every 6 hours.
    await services.scheduler.run_slot(sync_job_name('icpc'))
    await sync_alone('icpc-world-finals')
    await assert_both_scheduled()
    # And the other way round, as while clist.by is down.
    await sync_alone('icpc')
    await assert_both_scheduled()


async def test_icpc_reminders_wait_for_both_of_its_sources(
    bot: KcpcBot,
    services: KcpcServices,
    db: Database,
    sites: FakeSites,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings)
    # Stored before the bot started, so it may have moved since.
    finals = world_finals(NOW + 30 * MINUTE)
    await ContestRepo(db).add(
        [
            ContestInfo(
                'icpc',
                'clist-71000001',
                finals.name,
                finals.start,
                None,
                finals.end,
                finals.url,
            )
        ],
        now=NOW - DAY,
    )
    sites.icpc['UKIEPC'] = ukiepc()
    sites.clist['icpc.global'] = [finals]

    await services.scheduler.run_slot(sync_job_name('icpc'))
    await services.reminders.tick()

    assert publisher.posts == []

    await services.scheduler.run_slot(sync_job_name('icpc-world-finals'))

    (post,) = publisher.posts
    assert post.message.title == 'Starting soon: The 2027 ICPC World Finals'
    # The World Finals are ICPC contests.
    assert post.message.description is not None
    assert post.message.description.splitlines()[0] == '**Platform:** ICPC'


async def test_sync_and_the_lists_name_each_source(
    bot: KcpcBot, ctx: MagicMock, sites: FakeSites
) -> None:
    sites.icpc['UKIEPC'] = ukiepc()
    sites.clist['codechef.com'] = [starters(NOW + DAY)]
    sites.clist['icpc.global'] = [world_finals(NOW + 200 * DAY)]

    await run(bot, 'kcpc contests sync', ctx)

    assert reply(ctx, ephemeral=True).description == '\n'.join(
        [
            f'**Codeforces**: 0 added, {SYNCED} Upcoming: 0.',
            f'**AtCoder**: 0 added, {SYNCED} Upcoming: 0.',
            f'**ICPC**: 1 added, {SYNCED} Upcoming: 1.',
            f'**CodeChef**: 1 added, {SYNCED} Upcoming: 1.',
            f'**LeetCode**: 0 added, {SYNCED} Upcoming: 0.',
            f'**TopCoder**: 0 added, {SYNCED} Upcoming: 0.',
            f'**ICPC World Finals**: 1 added, {SYNCED} Upcoming: 1.',
        ]
    )

    cast(AsyncMock, ctx.send).reset_mock()
    await run(bot, 'contests', ctx, 'icpc')

    embed = reply(ctx)
    assert embed.title == 'Upcoming ICPC contests'
    assert [field.name for field in embed.fields] == [
        UKIEPC_NAME,
        'The 2027 ICPC World Finals',
    ]
    assert embed.footer.text == 'Last synced: ICPC just now, ICPC World Finals just now'
