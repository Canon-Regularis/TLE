"""Tests for tle.kcpc.bootstrap.build_services and the KcpcServices it returns."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from discord.ext import commands

from tle.config import Settings
from tle.kcpc import bootstrap
from tle.kcpc.bootstrap import RECONCILE_JOB, REMINDERS_JOB, build_services
from tle.kcpc.bot.publisher import DiscordPublisher, ReconcileReport
from tle.kcpc.core.clock import FakeClock, SystemClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcDisabledError
from tle.kcpc.core.ledger import Delivery
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.migrations import ALL_MIGRATIONS, schema_version
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.reminders import (
    Notice,
    Occurrence,
    ReminderEngine,
    ReminderPolicy,
    TickReport,
)
from tle.kcpc.core.scheduler import Scheduler
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.features.workshops.settings import (
    SPEC as WORKSHOPS_SPEC,
    WORKSHOPS,
    WorkshopSettings,
)
from tle.kcpc.services import KcpcServices, get_services

GUILD_ID = 1_100_000_000_000_000_001
CHANNEL_ID = 1_200_000_000_000_000_001
SERVICES_LOGGER = 'tle.kcpc.services'
LATEST_SCHEMA = ALL_MIGRATIONS[-1].version
# Real seconds a healthy shutdown needs, many times over. A shutdown that hangs
# then fails its test instead of stalling the whole run.
SHUTDOWN_TIMEOUT = 10


async def shut_down(services: KcpcServices) -> None:
    await asyncio.wait_for(services.shutdown(), SHUTDOWN_TIMEOUT)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(kcpc_db_path=tmp_path / 'data' / 'kcpc.db')


@pytest.fixture
async def ready() -> asyncio.Event:
    """Set it to make the bot ready, which lets the scheduler's jobs start."""
    return asyncio.Event()


@pytest.fixture
def bot(ready: asyncio.Event) -> MagicMock:
    """A bot that sees no guilds or channels."""
    bot = MagicMock(spec=commands.Bot)
    bot.wait_until_ready = AsyncMock(side_effect=ready.wait)
    bot.get_guild.return_value = None
    bot.get_channel.return_value = None
    return bot


@pytest.fixture
async def services(
    bot: MagicMock, settings: Settings, clock: FakeClock
) -> AsyncIterator[KcpcServices]:
    services = await build_services(bot, settings, clock=clock)
    yield services
    await shut_down(services)


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 5 s of real time) until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def is_closed(db: Database) -> bool:
    try:
        await db.fetchval('SELECT 1')
    except sqlite3.ProgrammingError:
        return True
    return False


async def http_is_closed(services: KcpcServices) -> bool:
    """Whether the services' HTTP client refuses requests, as a closed one does."""
    try:
        # A closed client refuses before connecting. An open one would try this
        # port, where nothing listens, then wait to retry on the fake clock,
        # which nothing advances; hence the time limit.
        await asyncio.wait_for(services.http.get('http://127.0.0.1:9/'), timeout=2)
    except RuntimeError as exc:
        return str(exc) == 'HttpClient is closed'
    except (asyncio.TimeoutError, ExternalServiceError):
        return False
    return False


async def test_build_services_opens_and_migrates_the_database(
    services: KcpcServices, settings: Settings
) -> None:
    assert settings.kcpc_db_path.is_file()  # its directory was created too
    assert services.db.path == str(settings.kcpc_db_path)
    assert await schema_version(services.db) == LATEST_SCHEMA


async def test_build_services_wires_the_services_together(
    services: KcpcServices, settings: Settings, clock: FakeClock
) -> None:
    assert services.settings is settings
    assert services.clock is clock
    assert services.features.keys() == ['algo', 'contests', 'weekly', 'workshops']
    assert services.guild_settings.registry is services.features
    assert services.reminders.features == []  # features register their sources

    # The settings repository and the ledger both use the services' database.
    await services.guild_settings.update(
        GUILD_ID, 'workshops', enabled=True, channel_id=CHANNEL_ID
    )
    await services.ledger.record_skip(Delivery('key', GUILD_ID, 'workshops'), 'test')
    assert await services.db.fetchval('SELECT COUNT(*) FROM guild_settings') == 1
    assert await services.db.fetchval('SELECT COUNT(*) FROM delivery_log') == 1

    # The publisher reads those settings: it finds the feature enabled, and
    # gets as far as looking for its channel.
    result = await services.publisher.publish(
        [Delivery('other-key', GUILD_ID, 'workshops')], OutgoingMessage(title='Hi')
    )
    assert result.outcome is PublishOutcome.UNDELIVERABLE
    assert result.reason == 'channel-missing'


async def test_workshop_settings_are_typed_whichever_extensions_load(
    services: KcpcServices,
) -> None:
    # No extension has loaded, yet the workshops settings are their own type:
    # /kcpc shows them so, and the extension never sees the base type.
    assert services.features.get(WORKSHOPS) is WORKSHOPS_SPEC

    await services.guild_settings.update(
        GUILD_ID, WORKSHOPS, enabled=True, calendar_id='cal-ClubWorkshops01'
    )

    assert await services.guild_settings.get(GUILD_ID, WORKSHOPS) == WorkshopSettings(
        enabled=True, calendar_id='cal-ClubWorkshops01'
    )


async def test_the_http_client_sends_the_configured_user_agent(
    bot: MagicMock, tmp_path: Path, clock: FakeClock
) -> None:
    agents: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        agents.append(request.headers['User-Agent'])
        return web.Response(text='ok')

    app = web.Application()
    app.router.add_get('/', handler)
    settings = Settings(
        kcpc_db_path=tmp_path / 'kcpc.db', http_user_agent='kcpc-test/1.0'
    )
    async with TestServer(app) as server:
        services = await build_services(bot, settings, clock=clock)
        try:
            assert await services.http.get_text(str(server.make_url('/'))) == 'ok'
        finally:
            await shut_down(services)

    assert agents == ['kcpc-test/1.0']


async def test_the_reconcile_and_reminders_jobs_are_registered_and_started(
    services: KcpcServices,
) -> None:
    reconcile, reminders = services.scheduler.status()

    assert reconcile.name == RECONCILE_JOB == 'kcpc.reconcile'
    assert reconcile.description == 'every 2m'
    assert reminders.name == REMINDERS_JOB == 'kcpc.reminders'
    assert reminders.description == 'every 1m'
    assert not reconcile.persistent and not reminders.persistent
    assert services.scheduler.running


async def test_reconcile_runs_once_ready_then_every_2_minutes(
    monkeypatch: pytest.MonkeyPatch,
    bot: MagicMock,
    settings: Settings,
    clock: FakeClock,
    ready: asyncio.Event,
) -> None:
    start = clock.now()  # on a 2-minute slot
    runs: list[datetime] = []

    async def reconcile(publisher: DiscordPublisher) -> ReconcileReport:
        runs.append(clock.now())
        return ReconcileReport()

    monkeypatch.setattr(DiscordPublisher, 'reconcile', reconcile)
    services = await build_services(bot, settings, clock=clock)
    try:
        await clock.settle()
        assert runs == []  # the bot isn't ready yet

        ready.set()
        await eventually(lambda: len(runs) == 1, 'reconcile runs at start')
        await clock.advance(timedelta(minutes=2))
        await eventually(lambda: len(runs) == 2, 'reconcile runs again')

        assert runs == [start, start + timedelta(minutes=2)]
    finally:
        await shut_down(services)


async def test_the_reconcile_job_uses_the_publisher_that_features_publish_with(
    monkeypatch: pytest.MonkeyPatch, services: KcpcServices
) -> None:
    # A publisher leaves alone the batches it is still sending, which a slow
    # send can make stale, but it knows only its own sends. Reconciling with
    # any other instance could resend a post that is on its way: a duplicate.
    callers: list[DiscordPublisher] = []

    async def reconcile(publisher: DiscordPublisher) -> ReconcileReport:
        callers.append(publisher)
        return ReconcileReport()

    monkeypatch.setattr(DiscordPublisher, 'reconcile', reconcile)

    await services.scheduler.run_slot(RECONCILE_JOB)

    (caller,) = callers
    assert caller is services.publisher


async def test_reminders_run_once_ready_then_every_minute(
    monkeypatch: pytest.MonkeyPatch,
    bot: MagicMock,
    settings: Settings,
    clock: FakeClock,
    ready: asyncio.Event,
) -> None:
    start = clock.now()  # on a 1-minute slot
    ticks: list[datetime] = []

    async def tick(engine: ReminderEngine) -> TickReport:
        ticks.append(clock.now())
        return TickReport()

    monkeypatch.setattr(ReminderEngine, 'tick', tick)
    services = await build_services(bot, settings, clock=clock)
    try:
        await clock.settle()
        assert ticks == []  # the bot isn't ready yet

        ready.set()
        await eventually(lambda: len(ticks) == 1, 'reminders run at start')
        await clock.advance(timedelta(minutes=1))
        await eventually(lambda: len(ticks) == 2, 'reminders run again')

        assert ticks == [start, start + timedelta(minutes=1)]
    finally:
        await shut_down(services)


class OneWorkshop:
    """A reminder source with one workshop, half an hour after ``start``."""

    feature = 'workshops'

    def __init__(self, start: datetime) -> None:
        self._workshop = Occurrence(
            'event', 'evt-1', 'Graphs 101', start + timedelta(minutes=30), None, None, 0
        )

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        return ReminderPolicy(offsets=(timedelta(hours=1),))

    async def occurrences(
        self, guild_id: int, settings: FeatureSettings, start: datetime, end: datetime
    ) -> list[Occurrence]:
        return [self._workshop]

    def render(self, notice: Notice) -> OutgoingMessage:
        return OutgoingMessage(title=notice.occurrences[0].title)


async def test_reminders_go_out_through_the_publisher_that_reconciles(
    monkeypatch: pytest.MonkeyPatch, services: KcpcServices, clock: FakeClock
) -> None:
    # As for the reconcile job: the publisher leaves alone only the posts it
    # is sending itself, so any other instance could duplicate a reminder.
    callers: list[DiscordPublisher] = []

    async def publish(
        publisher: DiscordPublisher,
        deliveries: Sequence[Delivery],
        message: OutgoingMessage,
    ) -> PublishResult:
        callers.append(publisher)
        return PublishResult(PublishOutcome.SENT, message_id=1)

    monkeypatch.setattr(DiscordPublisher, 'publish', publish)
    await services.guild_settings.update(
        GUILD_ID, 'workshops', enabled=True, channel_id=CHANNEL_ID
    )
    services.reminders.register(OneWorkshop(clock.now()))

    await services.scheduler.run_slot(REMINDERS_JOB)

    (caller,) = callers
    assert caller is services.publisher


async def test_the_default_clock_is_the_system_clock(
    bot: MagicMock, settings: Settings
) -> None:
    services = await build_services(bot, settings)
    try:
        assert isinstance(services.clock, SystemClock)
    finally:
        await shut_down(services)


async def test_services_start_again_on_an_existing_database(
    bot: MagicMock, settings: Settings, clock: FakeClock
) -> None:
    first = await build_services(bot, settings, clock=clock)
    await first.guild_settings.update(GUILD_ID, 'weekly', enabled=True)
    await shut_down(first)

    second = await build_services(bot, settings, clock=clock)
    try:
        assert await schema_version(second.db) == LATEST_SCHEMA
        assert (await second.guild_settings.get(GUILD_ID, 'weekly')).enabled
    finally:
        await shut_down(second)


async def test_a_failure_while_wiring_closes_the_database(
    monkeypatch: pytest.MonkeyPatch,
    bot: MagicMock,
    settings: Settings,
    clock: FakeClock,
    opened_databases: list[Database],
) -> None:
    monkeypatch.setattr(
        bootstrap, 'DiscordPublisher', MagicMock(side_effect=RuntimeError('boom'))
    )

    with pytest.raises(RuntimeError, match='boom'):
        await build_services(bot, settings, clock=clock)

    (db,) = opened_databases
    assert db.path == str(settings.kcpc_db_path)
    assert await is_closed(db)


async def test_a_failure_to_start_the_jobs_shuts_everything_down(
    monkeypatch: pytest.MonkeyPatch,
    bot: MagicMock,
    settings: Settings,
    clock: FakeClock,
) -> None:
    shut_down: list[KcpcServices] = []
    real_shutdown = KcpcServices.shutdown

    async def shutdown_and_remember(services: KcpcServices) -> None:
        shut_down.append(services)
        await real_shutdown(services)

    monkeypatch.setattr(KcpcServices, 'shutdown', shutdown_and_remember)
    monkeypatch.setattr(Scheduler, 'start', MagicMock(side_effect=RuntimeError('boom')))

    with pytest.raises(RuntimeError, match='boom'):
        await build_services(bot, settings, clock=clock)

    (services,) = shut_down
    assert await is_closed(services.db)
    assert await http_is_closed(services)


async def test_shutdown_stops_the_jobs_and_closes_everything(
    services: KcpcServices,
) -> None:
    await shut_down(services)

    assert not services.scheduler.running
    assert await is_closed(services.db)
    assert await http_is_closed(services)


async def test_shutdown_is_idempotent(services: KcpcServices) -> None:
    await shut_down(services)
    await shut_down(services)

    assert await is_closed(services.db)


def recorded(
    steps: list[str], step: str, action: Callable[[], Awaitable[None]]
) -> Callable[[], Awaitable[None]]:
    """``action``, noting ``step`` in ``steps`` when it runs."""

    async def run() -> None:
        steps.append(step)
        await action()

    return run


async def test_shutdown_stops_the_jobs_before_closing_what_they_use(
    monkeypatch: pytest.MonkeyPatch, services: KcpcServices
) -> None:
    steps: list[str] = []
    monkeypatch.setattr(
        services.scheduler, 'stop', recorded(steps, 'jobs', services.scheduler.stop)
    )
    monkeypatch.setattr(
        services.http, 'close', recorded(steps, 'http', services.http.close)
    )
    monkeypatch.setattr(services.db, 'close', recorded(steps, 'db', services.db.close))

    await shut_down(services)

    assert steps == ['jobs', 'http', 'db']


async def test_a_failed_shutdown_step_is_logged_and_the_rest_still_run(
    monkeypatch: pytest.MonkeyPatch,
    services: KcpcServices,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = services.scheduler.stop

    async def stop_then_fail() -> None:
        await stop()
        raise RuntimeError('stuck')

    monkeypatch.setattr(services.scheduler, 'stop', stop_then_fail)

    with caplog.at_level(logging.ERROR, logger=SERVICES_LOGGER):
        await shut_down(services)

    assert await is_closed(services.db)
    assert await http_is_closed(services)
    (record,) = [r for r in caplog.records if r.name == SERVICES_LOGGER]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == 'KCPC shutdown: could not stop the scheduler'
    assert record.exc_info is not None


def test_get_services_returns_the_bots_services(services: KcpcServices) -> None:
    assert get_services(SimpleNamespace(kcpc=services)) is services


@pytest.mark.parametrize(
    'bot',
    [SimpleNamespace(), SimpleNamespace(kcpc=None), SimpleNamespace(kcpc=object())],
    ids=['no attribute', 'None', 'something else'],
)
def test_get_services_without_running_services_raises(bot: object) -> None:
    with pytest.raises(
        KcpcDisabledError, match='KCPC features are not available right now.'
    ):
        get_services(bot)
