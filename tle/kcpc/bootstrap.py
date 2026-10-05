"""Builds and starts the KCPC services when the bot starts.

TLE's ``setup_hook`` calls ``build_services`` before it loads the KCPC
extensions, and leaves them out if it fails.
"""

from datetime import datetime, timedelta

from discord.ext import commands

from tle.config import Settings
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import Clock, SystemClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.migrations import open_database
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import GuildSettingsRepo, default_registry
from tle.kcpc.features.contests.settings import SPEC as CONTESTS_SPEC
from tle.kcpc.features.problems.settings import SPEC as WEEKLY_SPEC
from tle.kcpc.features.workshops.settings import SPEC as WORKSHOPS_SPEC
from tle.kcpc.services import KcpcServices

RECONCILE_JOB = 'kcpc.reconcile'
# How often unconfirmed posts are looked for; they count as stale after the
# publisher's stale_after, also 2 minutes.
_RECONCILE_INTERVAL = timedelta(minutes=2)

REMINDERS_JOB = 'kcpc.reminders'
# Reminder offsets are whole minutes, so a tick every minute posts each one on
# time.
_REMINDERS_INTERVAL = timedelta(minutes=1)


async def build_services(
    bot: commands.Bot, settings: Settings, *, clock: Clock | None = None
) -> KcpcServices:
    """Open kcpc.db, bringing its schema up to date, and start the services.

    The reconcile and reminders jobs start at once but wait for the bot to be
    ready. If a step fails, whatever was opened is closed again before the
    error propagates.
    """
    clock = SystemClock() if clock is None else clock
    db = await open_database(settings.kcpc_db_path)
    try:
        services = _assemble(bot, settings, clock, db)
    except BaseException:
        # Nothing else holds resources before it has started.
        await db.close()
        raise
    try:
        services.scheduler.start()
    except BaseException:
        await services.shutdown()
        raise
    return services


def _assemble(
    bot: commands.Bot, settings: Settings, clock: Clock, db: Database
) -> KcpcServices:
    """The services, wired together but not started."""
    features = default_registry()
    # Here rather than in the features' extensions, so that the settings decode
    # as their own type whichever extensions load: /kcpc shows them typed, and
    # an extension never sees the base settings.
    features.register(CONTESTS_SPEC, replace=True)
    features.register(WEEKLY_SPEC, replace=True)
    features.register(WORKSHOPS_SPEC, replace=True)
    guild_settings = GuildSettingsRepo(db, clock, features)
    ledger = DeliveryLedger(db, clock)
    publisher = DiscordPublisher(bot, guild_settings, ledger, clock)
    reminders = ReminderEngine(guild_settings, ledger, publisher, clock)
    scheduler = Scheduler(db, clock, ready=bot.wait_until_ready)
    # The publisher that features post through: it leaves alone the posts it
    # is still sending, which another instance would not know about.
    scheduler.add(_reconcile_job(publisher))
    scheduler.add(_reminders_job(reminders))
    return KcpcServices(
        settings=settings,
        clock=clock,
        db=db,
        http=HttpClient(user_agent=settings.http_user_agent, clock=clock),
        features=features,
        guild_settings=guild_settings,
        ledger=ledger,
        publisher=publisher,
        reminders=reminders,
        scheduler=scheduler,
    )


def _reconcile_job(publisher: DiscordPublisher) -> ScheduledJob:
    """Settles unconfirmed posts at startup, then every couple of minutes.

    Non-persistent: a missed run needs no catching up, as the next run
    reconciles everything that is stale by then.
    """

    async def reconcile(slot: datetime) -> None:
        # Each run settles whatever is stale by then, whichever slot it is for.
        await publisher.reconcile()

    return ScheduledJob(
        RECONCILE_JOB,
        Every(_RECONCILE_INTERVAL),
        reconcile,
        persistent=False,
        run_on_start=True,
    )


def _reminders_job(reminders: ReminderEngine) -> ScheduledJob:
    """Posts the reminders that are due, at startup and then every minute.

    Non-persistent: each tick works out from the time and the ledger what is
    due, including what came due while the bot was down.
    """

    async def remind(slot: datetime) -> None:
        # Each run plans from the current time, whichever slot it is for.
        await reminders.tick()

    return ScheduledJob(
        REMINDERS_JOB,
        Every(_REMINDERS_INTERVAL),
        remind,
        persistent=False,
        run_on_start=True,
    )
