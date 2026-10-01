"""Tests for the workshops cog (tle.kcpc.features.workshops.cog).

The cog runs on real KCPC services (database, settings, ledger, scheduler and
reminder engine, which posts through FakePublisher), on a real bot that also
has the admin cog. Only Luma is faked: the cog's calendar client is replaced
by a feed the test controls. Most tests call a command's callback with a
mocked context; the rest go through discord.py, for its checks, its error
handling and how it adds and removes the cog.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock
from zoneinfo import ZoneInfo

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
from tle.kcpc.features.workshops import cog as workshops_cog
from tle.kcpc.features.workshops.cog import (
    NEVER_SYNCED,
    NO_CALENDAR,
    SYNC_JOB,
    KcpcWorkshops,
    setup,
)
from tle.kcpc.features.workshops.reminders import WorkshopOccurrence, workshop_details
from tle.kcpc.features.workshops.repo import EventRepo, EventStatus
from tle.kcpc.features.workshops.settings import SPEC, WORKSHOPS, WorkshopSettings
from tle.kcpc.features.workshops.sync import EventSync
from tle.kcpc.platforms.luma import CalendarNotFound, LumaEvent
from tle.kcpc.services import KcpcServices

CALENDAR = 'cal-ClubWorkshops01'
OTHER_CALENDAR = 'cal-OtherClub000002'
ICAL_LINK = f'https://api.lu.ma/ics/get?entity=calendar&id={CALENDAR}'
# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
THIRD_GUILD = 1_100_000_000_000_000_003
CHANNEL = 1_200_000_000_000_000_001

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # where the clock fixture starts
UNREACHABLE = 'Luma is not responding right now. Please try again later.'
NOT_FOUND = f'Luma has no calendar with the ID {CALENDAR}.'
COG_LOGGER = 'tle.kcpc.features.workshops.cog'
ADMIN_LOGGER = 'tle.kcpc.bot.admin'
# Real seconds a healthy teardown needs, many times over. One that hangs then
# fails its test instead of stalling the whole run.
TEARDOWN_TIMEOUT = 10


class KcpcBot(commands.Bot):
    """A bot that carries KCPC services, as TLEBot does."""

    kcpc: KcpcServices | None = None


class FakeFeed:
    """The Luma feed that the cog's calendar client fetches from.

    It serves whatever events each calendar is given; a calendar never served
    is not found. After ``fail``, fetching the calendar raises the error, once
    ``after`` more fetches have succeeded. ``fetched`` lists every fetch.
    """

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self._events: dict[str, list[LumaEvent]] = {}
        self._failures: dict[str, tuple[int, Exception]] = {}

    def serve(self, calendar_id: str, events: Sequence[LumaEvent]) -> None:
        self._events[calendar_id] = list(events)
        self._failures.pop(calendar_id, None)

    def fail(self, calendar_id: str, error: Exception, *, after: int = 0) -> None:
        self._failures[calendar_id] = (after, error)

    async def fetch(self, calendar_id: str) -> list[LumaEvent]:
        self.fetched.append(calendar_id)
        failure = self._failures.get(calendar_id)
        if failure is not None:
            successes_left, error = failure
            if successes_left == 0:
                raise error
            self._failures[calendar_id] = (successes_left - 1, error)
        if calendar_id not in self._events:
            raise CalendarNotFound(calendar_id)
        return list(self._events[calendar_id])


def luma_event(
    luma_id: str, start: datetime, *, end: datetime | None | bool = True
) -> LumaEvent:
    """A Luma event; ``end`` is two hours after the start unless given."""
    return LumaEvent(
        luma_id=luma_id,
        name=f'Workshop {luma_id}',
        start=start,
        end=start + 2 * HOUR if end is True else end or None,
        url=f'https://luma.com/{luma_id}',
        location='Bush House 3.01',
    )


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the workshops settings typed."""
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
        settings=Settings(),
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
def feed(monkeypatch: pytest.MonkeyPatch) -> FakeFeed:
    feed = FakeFeed()

    def client(http: HttpClient, *, tz: ZoneInfo) -> FakeFeed:
        return feed

    monkeypatch.setattr(workshops_cog, 'LumaCalendarClient', client)
    return feed


@pytest.fixture
async def admin_bot(services: KcpcServices, feed: FakeFeed) -> AsyncIterator[KcpcBot]:
    """A real bot with the KCPC services and the admin cog, as at startup."""
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    await add_admin_cog(bot)
    yield bot
    await bot.close()


async def load_workshops(bot: commands.Bot) -> KcpcWorkshops:
    """Add the workshops cog as its extension does."""
    await setup(bot)
    cog = bot.get_cog('KcpcWorkshops')
    assert isinstance(cog, KcpcWorkshops)
    return cog


@pytest.fixture
async def cog(admin_bot: KcpcBot) -> KcpcWorkshops:
    return await load_workshops(admin_bot)


@pytest.fixture
def bot(admin_bot: KcpcBot, cog: KcpcWorkshops) -> KcpcBot:
    """The bot with both cogs."""
    return admin_bot


def make_member(*, manage_guild: bool) -> MagicMock:
    member = MagicMock(spec=discord.Member)
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
    bot: commands.Bot, guild: MagicMock, author: MagicMock, *, slash: bool = False
) -> commands.Context[commands.Bot]:
    """A real context in ``guild``; with ``slash``, of a slash command.

    Replies are recorded, and so is deferring a slash command.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
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
    bot: commands.Bot, name: str, ctx: MagicMock | commands.Context[Any], *args: object
) -> None:
    """Call the callback of the command ``name`` with parsed arguments ``args``."""
    command = command_named(bot, name)
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args)


async def invoke(bot: commands.Bot, name: str, ctx: commands.Context[Any]) -> None:
    """Run the command as ``Bot.invoke`` does: checks, then the callback, and
    any error to the command's error handlers.
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


async def follow(
    guild_settings: GuildSettingsRepo,
    guild_id: int,
    calendar_id: str | None,
    *,
    enabled: bool = True,
) -> None:
    """Set the guild up for workshops, with a channel and the calendar."""
    await guild_settings.update(
        guild_id,
        WORKSHOPS,
        enabled=enabled,
        channel_id=CHANNEL,
        calendar_id=calendar_id,
    )


async def synced(
    db: Database, clock: FakeClock, feed: FakeFeed, calendar_id: str, *events: LumaEvent
) -> None:
    """Sync ``events`` as the calendar's feed into the database, now."""
    feed.serve(calendar_id, events)
    report = await EventSync(db, EventRepo(db), feed, clock).sync(calendar_id)
    assert report.ok


def stamp(moment: datetime, style: str) -> str:
    return f'<t:{to_epoch(moment)}:{style}>'


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 5 s of real time) until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def test_loading_adds_the_commands_the_reminders_and_the_sync_job(
    bot: KcpcBot, services: KcpcServices
) -> None:
    assert services.reminders.features == [WORKSHOPS]
    (job,) = [job for job in services.scheduler.status() if job.name == SYNC_JOB]
    assert job.description == 'every 10m'
    assert not job.persistent

    # /kcpc workshops, for admins, on both paths and nowhere else.
    assert command_named(bot, 'kcpc workshops calendar').cog is bot.get_cog(
        'KcpcWorkshops'
    )
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    workshops = kcpc.get_command('workshops')
    assert isinstance(workshops, app_commands.Group)
    assert sorted(command.name for command in workshops.commands) == [
        'calendar',
        'sync',
    ]
    assert set(bot.all_commands) == {'help', 'kcpc', 'event'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc', 'event'}

    # /event, for members, kept out of DMs.
    event = bot.tree.get_command('event')
    assert isinstance(event, app_commands.Group)
    assert event.guild_only
    assert sorted(command.name for command in event.commands) == ['next', 'this-week']
    assert command_named(bot, 'event this-week').qualified_name == 'event this-week'


async def test_the_sync_job_runs_at_start_then_every_10_minutes(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [])

    services.scheduler.start()
    # Asleep until its next slot, which it takes only once its first run is over.
    await eventually(
        lambda: clock.next_deadline == NOW + 10 * MINUTE, 'the job waits for 12:10'
    )
    assert feed.fetched == [CALENDAR]

    await clock.advance(10 * MINUTE)

    assert feed.fetched == [CALENDAR, CALENDAR]


async def test_removing_the_cog_undoes_everything_loading_did(
    bot: KcpcBot, services: KcpcServices, cog: KcpcWorkshops
) -> None:
    await bot.remove_cog('KcpcWorkshops')

    assert services.reminders.features == []
    assert [job.name for job in services.scheduler.status()] == []
    assert bot.get_command('kcpc workshops') is None
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('workshops') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}

    # So the extension can be loaded again.
    again = await load_workshops(bot)
    assert command_named(bot, 'kcpc workshops sync').cog is again
    assert services.reminders.features == [WORKSHOPS]


class StubSource:
    """Another reminder source for the workshops feature."""

    feature = WORKSHOPS

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        return ReminderPolicy(offsets=())

    async def occurrences(
        self, guild_id: int, settings: FeatureSettings, start: datetime, end: datetime
    ) -> list[WorkshopOccurrence]:
        return []

    def render(self, notice: Notice) -> OutgoingMessage:
        return OutgoingMessage()


async def other_workshops(ctx: commands.Context[Any]) -> None:
    """Another /kcpc workshops."""


def assert_nothing_left_by_a_failed_load(bot: commands.Bot) -> None:
    assert bot.get_cog('KcpcWorkshops') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}


async def test_a_load_that_cannot_register_the_reminders_changes_nothing(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    stub = StubSource()
    services.reminders.register(stub)

    with pytest.raises(ValueError, match='already registered'):
        await load_workshops(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc workshops') is None
    assert services.scheduler.status() == []
    assert services.reminders.features == [WORKSHOPS]  # still the other one


async def test_a_load_that_cannot_attach_the_admin_commands_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    kcpc = command_named(admin_bot, 'kcpc')
    assert isinstance(kcpc, commands.HybridGroup)
    clashing: commands.HybridGroup[Any, ..., Any] = commands.hybrid_group(
        name='workshops'
    )(other_workshops)
    kcpc.add_command(clashing)

    with pytest.raises(commands.CommandRegistrationError):
        await load_workshops(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc workshops') is clashing
    assert services.reminders.features == []
    assert services.scheduler.status() == []


async def test_a_load_that_cannot_add_the_sync_job_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    services.scheduler.add(ScheduledJob(SYNC_JOB, Every(HOUR), other, persistent=False))

    with pytest.raises(ValueError, match='already scheduled'):
        await load_workshops(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc workshops') is None
    kcpc = admin_bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('workshops') is None
    assert services.reminders.features == []


async def test_without_the_admin_cog_the_feature_runs_without_its_admin_commands(
    services: KcpcServices, feed: FakeFeed, caplog: pytest.LogCaptureFixture
) -> None:
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    bot.kcpc = services
    try:
        with caplog.at_level(logging.INFO, logger=ADMIN_LOGGER):
            await load_workshops(bot)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == ADMIN_LOGGER
        ] == ['Not adding /kcpc workshops: the kcpc.admin extension is not loaded']
        # Not a top-level command that every member would be shown.
        assert set(bot.all_commands) == {'help', 'event'}
        assert {command.name for command in bot.tree.get_commands()} == {'event'}
        assert services.reminders.features == [WORKSHOPS]
        assert [job.name for job in services.scheduler.status()] == [SYNC_JOB]

        await bot.remove_cog('KcpcWorkshops')

        assert set(bot.all_commands) == {'help'}
        assert services.reminders.features == []
    finally:
        await bot.close()


async def test_event_next_shows_the_next_workshop(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    start = NOW + 2 * DAY
    await synced(
        db,
        clock,
        feed,
        CALENDAR,
        luma_event('evt-later', start + DAY),
        luma_event('evt-started', NOW - HOUR),
        luma_event('evt-next', start),
    )
    await clock.advance(5 * MINUTE)

    await run(bot, 'event', ctx)

    # Everyone sees it.
    embed = reply(ctx)
    assert embed.title == 'Next workshop: Workshop evt-next'
    assert embed.url == 'https://luma.com/evt-next'
    assert embed.description == (
        f'**When:** {stamp(start, "F")} ({stamp(start, "R")})\n'
        f'**Ends:** {stamp(start + 2 * HOUR, "f")}\n'
        '**Where:** Bush House 3.01'
    )
    assert embed.colour == discord.Colour(KCPC_COLOR)
    # A footer can't show a Discord timestamp; the embed's own shows after it.
    assert embed.footer.text == 'Last synced'
    assert embed.timestamp == NOW


async def test_event_next_skips_cancelled_workshops(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(
        db,
        clock,
        feed,
        CALENDAR,
        luma_event('evt-cancelled', NOW + DAY),
        luma_event('evt-next', NOW + 2 * DAY),
    )
    repo = EventRepo(db)
    (cancelled,) = [
        e for e in await repo.events(CALENDAR) if e.luma_id == 'evt-cancelled'
    ]
    await repo.save([replace(cancelled, status=EventStatus.CANCELLED, revision=1)])

    await run(bot, 'event', ctx)

    assert reply(ctx).title == 'Next workshop: Workshop evt-next'


async def test_event_next_without_upcoming_workshops_says_so(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-over', NOW - DAY))

    await run(bot, 'event', ctx)

    embed = reply(ctx)
    assert embed.title == 'Next workshop'
    assert embed.description == 'There are no workshops coming up. Check back soon!'
    assert embed.footer.text == 'Last synced'
    assert embed.timestamp == NOW


@pytest.mark.parametrize('name', ['event', 'event this-week'])
async def test_member_commands_without_a_calendar_say_how_to_set_one(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo, name: str
) -> None:
    await follow(guild_settings, GUILD, None)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, name, ctx)

    assert (
        str(raised.value)
        == NO_CALENDAR
        == (
            'No Luma calendar is set up yet. An admin can set one with '
            '/kcpc workshops calendar.'
        )
    )
    cast(AsyncMock, ctx.send).assert_not_awaited()


@pytest.mark.parametrize('name', ['event', 'event this-week'])
async def test_member_commands_before_any_sync_say_so(
    bot: KcpcBot,
    ctx: MagicMock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    services: KcpcServices,
    name: str,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    assert NEVER_SYNCED == "Workshops haven't been synced yet."
    with pytest.raises(KcpcUserError, match=f'^{NEVER_SYNCED}$'):
        await run(bot, name, ctx)

    # Attempted, but never with success.
    feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))
    sync = EventSync(services.db, EventRepo(services.db), feed, services.clock)
    assert not (await sync.sync(CALENDAR)).ok
    with pytest.raises(KcpcUserError, match=f'^{NEVER_SYNCED}$'):
        await run(bot, name, ctx)


async def test_member_commands_use_the_bots_default_calendar(
    admin_bot: KcpcBot,
    services: KcpcServices,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    services.settings = Settings(luma_calendar_id=CALENDAR)
    await load_workshops(admin_bot)
    await follow(guild_settings, GUILD, None)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-next', NOW + DAY))

    await run(admin_bot, 'event', ctx)

    assert reply(ctx).title == 'Next workshop: Workshop evt-next'


async def test_event_this_week_lists_this_weeks_workshops_in_the_club_time_zone(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Wednesday 7 October 2026, 13:00 in London (BST, UTC+1): the week runs
    # from Monday 00:00 BST, Sunday 23:00 UTC, to the next Monday 00:00 BST.
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    week_start = datetime(2026, 10, 4, 23, 0, tzinfo=UTC)
    week_end = datetime(2026, 10, 11, 23, 0, tzinfo=UTC)
    in_progress = luma_event('evt-in-progress', now - HOUR)
    await clock.advance_to(now)
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(
        db,
        clock,
        feed,
        CALENDAR,
        luma_event('evt-last-week', week_start - MINUTE),
        luma_event('evt-monday', week_start),
        luma_event('evt-tuesday', now - DAY),
        in_progress,
        luma_event('evt-no-end', now - 30 * MINUTE, end=None),
        luma_event('evt-cancelled', now + DAY),
        luma_event('evt-sunday', week_end - timedelta(seconds=1)),
        luma_event('evt-next-week', week_end),
    )
    repo = EventRepo(db)
    (cancelled,) = [
        e for e in await repo.events(CALENDAR) if e.luma_id == 'evt-cancelled'
    ]
    await repo.save([replace(cancelled, status=EventStatus.CANCELLED, revision=1)])

    await run(bot, 'event this-week', ctx)

    embed = reply(ctx)
    assert embed.title == 'Workshops this week'
    assert embed.description is None
    # Over once ended, or started if the end isn't known.
    assert [field.name for field in embed.fields] == [
        'Workshop evt-monday (finished)',
        'Workshop evt-tuesday (finished)',
        'Workshop evt-in-progress',
        'Workshop evt-no-end (finished)',
        'Workshop evt-sunday',
    ]
    (stored,) = [
        e for e in await repo.events(CALENDAR) if e.luma_id == in_progress.luma_id
    ]
    assert embed.fields[2].value == workshop_details(
        WorkshopOccurrence.from_event(stored), link=True
    )
    assert embed.fields[2].value is not None
    assert embed.fields[2].value.endswith(
        '[Event page](https://luma.com/evt-in-progress)'
    )
    assert embed.footer.text == 'Last synced'
    assert embed.timestamp == now


async def test_event_this_week_without_workshops_says_so(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-later', NOW + 30 * DAY))

    await run(bot, 'event this-week', ctx)

    embed = reply(ctx)
    assert embed.title == 'Workshops this week'
    assert embed.description == (
        'There are no workshops this week. `/event next` shows the next one.'
    )
    assert embed.fields == []


async def test_event_on_its_own_shows_the_next_workshop(
    bot: KcpcBot,
    guild: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    # ;event, as /event next: the group's fallback.
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-next', NOW + DAY))
    ctx = make_context(bot, guild, make_member(manage_guild=False))

    await invoke(bot, 'event', ctx)

    assert reply(ctx).title == 'Next workshop: Workshop evt-next'


async def test_member_commands_need_a_server(bot: KcpcBot, ctx: MagicMock) -> None:
    ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await run(bot, 'event this-week', ctx)


async def test_setting_the_calendar_saves_syncs_and_reminds(
    bot: KcpcBot,
    ctx: MagicMock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    services: KcpcServices,
) -> None:
    await follow(guild_settings, GUILD, None)
    soon = NOW + 50 * MINUTE  # its 1h reminder is due
    later = NOW + 2 * DAY  # no reminder is due yet
    feed.serve(CALENDAR, [luma_event('evt-later', later), luma_event('evt-soon', soon)])

    await run(bot, 'kcpc workshops calendar', ctx, CALENDAR)

    settings = await guild_settings.get_typed(GUILD, WORKSHOPS, WorkshopSettings)
    assert settings.calendar_id == CALENDAR
    state = await EventRepo(services.db).calendar_state(CALENDAR)
    assert state is not None and state.last_ok == NOW
    # Checked with Luma, then synced.
    assert feed.fetched == [CALENDAR, CALENDAR]
    # The reminders that were due went out at once.
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        f'This server now follows Luma calendar `{CALENDAR}`.\n'
        f'It has 2 workshops coming up. The next is **Workshop evt-soon**, '
        f'{stamp(soon, "F")}.'
    )


async def test_the_calendar_can_be_given_as_its_ical_link(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    feed.serve(CALENDAR, [luma_event('evt-1', NOW + DAY)])

    await run(bot, 'kcpc workshops calendar', ctx, f' <{ICAL_LINK}> ')

    settings = await guild_settings.get_typed(GUILD, WORKSHOPS, WorkshopSettings)
    assert settings.calendar_id == CALENDAR
    description = reply(ctx, ephemeral=True).description
    assert description is not None
    assert description.endswith(
        'It has 1 workshop coming up. The next is **Workshop '
        f'evt-1**, {stamp(NOW + DAY, "F")}.'
    )


async def test_setting_a_calendar_without_upcoming_workshops_says_so(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed
) -> None:
    feed.serve(CALENDAR, [luma_event('evt-over', NOW - DAY)])

    await run(bot, 'kcpc workshops calendar', ctx, CALENDAR)

    assert reply(ctx, ephemeral=True).description == (
        f'This server now follows Luma calendar `{CALENDAR}`.\n'
        'It has no upcoming workshops.'
    )


async def test_a_calendar_that_fails_to_sync_once_checked_is_still_saved(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    feed.serve(CALENDAR, [luma_event('evt-1', NOW + DAY)])
    feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE), after=1)

    await run(bot, 'kcpc workshops calendar', ctx, CALENDAR)

    settings = await guild_settings.get_typed(GUILD, WORKSHOPS, WorkshopSettings)
    assert settings.calendar_id == CALENDAR
    assert reply(ctx, ephemeral=True).description == (
        f'This server now follows Luma calendar `{CALENDAR}`.\n'
        f'Syncing it failed: {UNREACHABLE}'
    )


@pytest.mark.parametrize(
    ('given', 'serve', 'error'),
    [
        (
            'calendar please',
            True,
            "That doesn't look like a Luma calendar. Give its calendar ID, which "
            'starts with "cal-", or its iCal link from "Add to calendar", which '
            'contains "id=cal-".',
        ),
        (CALENDAR, False, NOT_FOUND),
    ],
    ids=['not a calendar', 'no such calendar'],
)
async def test_a_calendar_that_is_not_one_is_refused_and_nothing_saved(
    bot: KcpcBot,
    ctx: MagicMock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    given: str,
    serve: bool,
    error: str,
) -> None:
    await follow(guild_settings, GUILD, OTHER_CALENDAR)
    if serve:
        feed.serve(CALENDAR, [])

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc workshops calendar', ctx, given)

    assert str(raised.value) == error
    settings = await guild_settings.get_typed(GUILD, WORKSHOPS, WorkshopSettings)
    assert settings.calendar_id == OTHER_CALENDAR
    cast(AsyncMock, ctx.defer).assert_awaited_once_with(ephemeral=True)
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_a_calendar_luma_cannot_check_now_is_not_saved(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))

    with pytest.raises(ExternalServiceError, match=UNREACHABLE):
        await run(bot, 'kcpc workshops calendar', ctx, CALENDAR)

    settings = await guild_settings.get_typed(GUILD, WORKSHOPS, WorkshopSettings)
    assert settings.calendar_id is None
    assert feed.fetched == [CALENDAR]


async def test_syncing_reports_what_changed_and_reminds(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(
        db,
        clock,
        feed,
        CALENDAR,
        luma_event('evt-same', NOW + 2 * DAY),
        luma_event('evt-moving', NOW + 3 * DAY),
    )
    feed.serve(
        CALENDAR,
        [
            luma_event('evt-same', NOW + 2 * DAY),
            luma_event('evt-moving', NOW + 4 * DAY),
            luma_event('evt-soon', NOW + 30 * MINUTE),
        ],
    )

    await run(bot, 'kcpc workshops sync', ctx)

    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        f'Synced Luma calendar `{CALENDAR}`: 1 added, 0 updated, 1 moved, '
        '0 cancelled, 0 reinstated. Upcoming workshops: 3.'
    )


async def test_syncing_without_changes_does_not_tick(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    services: KcpcServices,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-1', NOW + DAY))
    tick = AsyncMock(wraps=services.reminders.tick)
    monkeypatch.setattr(services.reminders, 'tick', tick)

    await run(bot, 'kcpc workshops sync', ctx)

    tick.assert_not_awaited()
    assert reply(ctx, ephemeral=True).description == (
        f'Synced Luma calendar `{CALENDAR}`: 0 added, 0 updated, 0 moved, '
        '0 cancelled, 0 reinstated. Upcoming workshops: 1.'
    )


async def test_syncing_that_fails_says_why(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc workshops sync', ctx)

    assert str(raised.value) == (
        f"Couldn't sync Luma calendar `{CALENDAR}`: {UNREACHABLE}"
    )
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_syncing_a_feed_that_lost_most_workshops_applies_it_and_warns(
    bot: KcpcBot,
    ctx: MagicMock,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    weekly = [luma_event(f'evt-W{n}', NOW + n * 7 * DAY) for n in range(1, 7)]
    await synced(db, clock, feed, CALENDAR, *weekly)
    # Five of the six vanish, as a glitch at Luma would look, while a workshop
    # starting soon is added.
    feed.serve(CALENDAR, [weekly[0], luma_event('evt-soon', NOW + 30 * MINUTE)])

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc workshops sync', ctx)

    assert str(raised.value) == (
        f'Luma calendar `{CALENDAR}` lists far fewer upcoming workshops than '
        'before (feed shrank from 6 to 2 upcoming events). Applied: 1 added, '
        '0 updated, 0 moved, 0 cancelled, 0 reinstated; workshops missing from '
        'it count as cancelled after about an hour.'
    )
    # What the feed did list was applied, and reminded of at once.
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_quick_syncs_by_hand_do_not_cancel_a_missing_workshop(
    bot: KcpcBot,
    ctx: MagicMock,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [luma_event('evt-1', NOW + 50 * MINUTE)])
    await run(bot, 'kcpc workshops sync', ctx)  # its 1h reminder goes out

    # Luma drops it, and an admin syncs three times in a minute.
    feed.serve(CALENDAR, [])
    for _ in range(3):
        await clock.advance(timedelta(seconds=20))
        await run(bot, 'kcpc workshops sync', ctx)

    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-1'
    ]
    last_reply = cast(AsyncMock, ctx.send).await_args_list[-1].kwargs['embed']
    assert last_reply.description == (
        f'Synced Luma calendar `{CALENDAR}`: 0 added, 0 updated, 0 moved, '
        '0 cancelled, 0 reinstated. Upcoming workshops: 0.'
    )


async def test_syncing_a_calendar_luma_does_not_have_says_so(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)

    with pytest.raises(CalendarNotFound, match=f'^{NOT_FOUND}$'):
        await run(bot, 'kcpc workshops sync', ctx)


async def test_syncing_without_a_calendar_says_how_to_set_one(
    bot: KcpcBot, ctx: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, None)

    with pytest.raises(KcpcUserError, match=f'^{NO_CALENDAR}$'):
        await run(bot, 'kcpc workshops sync', ctx)

    assert feed.fetched == []


@pytest.mark.parametrize(
    ('name', 'args'),
    [('kcpc workshops calendar', (CALENDAR,)), ('kcpc workshops sync', ())],
)
async def test_admin_commands_defer_before_anything_else(
    bot: KcpcBot,
    ctx: MagicMock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    name: str,
    args: tuple[object, ...],
) -> None:
    # A sync waits for Luma, longer than Discord waits for a slash command's
    # first answer.
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [])
    fetched_when_deferred: list[list[str]] = []
    cast(AsyncMock, ctx.defer).side_effect = lambda **kwargs: (
        fetched_when_deferred.append(list(feed.fetched))
    )

    await run(bot, name, ctx, *args)

    cast(AsyncMock, ctx.defer).assert_awaited_once_with(ephemeral=True)
    assert fetched_when_deferred == [[]]
    assert feed.fetched  # and then it did


async def test_a_slash_admin_command_defers_its_interaction(
    bot: KcpcBot, guild: MagicMock, feed: FakeFeed, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [])
    ctx = make_context(bot, guild, make_member(manage_guild=True), slash=True)

    await run(bot, 'kcpc workshops sync', ctx)

    assert ctx.interaction is not None
    defer = cast(AsyncMock, ctx.interaction.response.defer)
    defer.assert_awaited_once_with(ephemeral=True)
    reply(ctx, ephemeral=True)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ['kcpc workshops calendar', 'kcpc workshops sync'])
async def test_admin_commands_are_for_admins_only(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    guild: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # Their cog is this one, so the admin cog's check doesn't cover them.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    command = command_named(bot, name)
    admin = make_context(bot, guild, make_member(manage_guild=True), slash=slash)
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command.can_run(admin)
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(member)


async def test_the_admin_group_on_its_own_is_for_admins_only(
    monkeypatch: pytest.MonkeyPatch, bot: KcpcBot, guild: MagicMock
) -> None:
    # ;kcpc workshops; Discord can't run a slash group on its own.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    group = command_named(bot, 'kcpc workshops')
    member = make_context(bot, guild, make_member(manage_guild=False))

    with pytest.raises(NotKcpcAdmin):
        await group.can_run(member)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ['event', 'event this-week'])
async def test_member_commands_are_for_everyone(
    bot: KcpcBot, guild: MagicMock, name: str, slash: bool
) -> None:
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command_named(bot, name).can_run(member)


async def test_a_user_error_gets_a_private_reply(
    bot: KcpcBot, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, None)
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc workshops sync', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NO_CALENDAR


async def test_a_bug_gets_an_apology_and_is_logged(
    bot: KcpcBot,
    guild: MagicMock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.fail(CALENDAR, RuntimeError('boom'))
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc workshops sync', ctx)

    assert reply(ctx, ephemeral=True).description == UNEXPECTED_ERROR_MESSAGE
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'


async def test_the_sync_job_syncs_each_followed_calendar_once(
    bot: KcpcBot,
    services: KcpcServices,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await follow(guild_settings, OTHER_GUILD, CALENDAR)
    await follow(guild_settings, THIRD_GUILD, 'cal-TurnedOff00001', enabled=False)
    feed.serve(CALENDAR, [])

    await services.scheduler.run_slot(SYNC_JOB)

    assert feed.fetched == [CALENDAR]


async def test_the_sync_job_follows_the_bots_default_calendar_too(
    admin_bot: KcpcBot,
    services: KcpcServices,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    services.settings = Settings(luma_calendar_id=OTHER_CALENDAR)
    await load_workshops(admin_bot)
    await follow(guild_settings, GUILD, CALENDAR)
    await follow(guild_settings, OTHER_GUILD, None)
    feed.serve(CALENDAR, [])
    feed.serve(OTHER_CALENDAR, [])

    await services.scheduler.run_slot(SYNC_JOB)

    assert feed.fetched == [CALENDAR, OTHER_CALENDAR]


async def test_the_sync_job_reminds_at_once_when_events_changed(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    services: KcpcServices,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [luma_event('evt-soon', NOW + 30 * MINUTE)])
    tick = AsyncMock(wraps=services.reminders.tick)
    monkeypatch.setattr(services.reminders, 'tick', tick)

    await services.scheduler.run_slot(SYNC_JOB)

    tick.assert_awaited_once_with()
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]

    await services.scheduler.run_slot(SYNC_JOB)  # nothing changed

    tick.assert_awaited_once_with()


async def test_reminders_wait_for_the_first_sync_since_the_bot_started(
    bot: KcpcBot,
    services: KcpcServices,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    # Synced before the bot started, so the workshop may have moved since.
    await synced(db, clock, feed, CALENDAR, luma_event('evt-soon', NOW + 30 * MINUTE))

    await services.reminders.tick()  # the reminders job, which starts first

    assert publisher.posts == []

    # Nothing changed, but the reminder that waited for the sync goes out at
    # once rather than at the next tick.
    await services.scheduler.run_slot(SYNC_JOB)

    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]


@pytest.mark.parametrize(
    'error',
    [
        ExternalServiceError('Luma', UNREACHABLE),
        CalendarNotFound(CALENDAR),
        RuntimeError('boom'),
    ],
    ids=['unreachable', 'not found', 'bug'],
)
async def test_a_first_sync_that_fails_does_not_hold_reminders_back(
    bot: KcpcBot,
    services: KcpcServices,
    db: Database,
    clock: FakeClock,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    error: Exception,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await synced(db, clock, feed, CALENDAR, luma_event('evt-soon', NOW + 30 * MINUTE))
    feed.fail(CALENDAR, error)

    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError):  # only a bug fails the job
            await services.scheduler.run_slot(SYNC_JOB)
    else:
        await services.scheduler.run_slot(SYNC_JOB)

    # While Luma can't be read, the stored workshops are the best there is.
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]


async def test_a_failed_sync_does_not_stop_the_job(
    bot: KcpcBot,
    services: KcpcServices,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Luma has no such calendar, or can't be reached: EventSync records and
    # logs that for the calendar, and the job carries on.
    missing, unreachable = 'cal-Missing0000001', 'cal-Unreachable001'
    await follow(guild_settings, GUILD, missing)
    await follow(guild_settings, OTHER_GUILD, unreachable)
    await follow(guild_settings, THIRD_GUILD, CALENDAR)
    feed.fail(unreachable, ExternalServiceError('Luma', UNREACHABLE))
    feed.serve(CALENDAR, [])

    await services.scheduler.run_slot(SYNC_JOB)

    assert feed.fetched == [CALENDAR, missing, unreachable]
    repo = EventRepo(services.db)
    for calendar_id in (missing, unreachable):
        state = await repo.calendar_state(calendar_id)
        assert state is not None and state.consecutive_failures == 1
    assert [job.failures for job in services.scheduler.status()] == [0]


async def test_a_bug_in_one_sync_fails_the_job_after_the_others(
    bot: KcpcBot,
    services: KcpcServices,
    feed: FakeFeed,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = 'cal-Broken00000001'
    await follow(guild_settings, GUILD, broken)
    await follow(guild_settings, OTHER_GUILD, CALENDAR)
    feed.fail(broken, RuntimeError('boom'))
    feed.serve(CALENDAR, [luma_event('evt-soon', NOW + 30 * MINUTE)])

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        with pytest.raises(RuntimeError) as raised:
            await services.scheduler.run_slot(SYNC_JOB)

    # The other calendar was still synced, and its reminder sent, before the
    # job failed.
    assert feed.fetched == [broken, CALENDAR]
    assert [post.message.title for post in publisher.posts] == [
        'Starting soon: Workshop evt-soon'
    ]
    assert str(raised.value) == f'Could not sync Luma calendars: {broken}'
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == 'boom'
    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.levelno == logging.INFO
    assert record.getMessage() == f'Syncing Luma calendar {broken} failed'
    assert record.exc_info is not None and record.exc_info[1] is raised.value.__cause__
    (job,) = services.scheduler.status()
    assert job.failures == 1
    assert job.last_error == f'RuntimeError: Could not sync Luma calendars: {broken}'
