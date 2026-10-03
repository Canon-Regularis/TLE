"""The running KCPC services: one typed container, attached to the bot as ``bot.kcpc``.

``tle.kcpc.bootstrap`` builds it at startup; cogs reach it through
``KcpcCog.services``, which uses ``get_services``.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tle.config import Settings
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcDisabledError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.scheduler import Scheduler
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo

logger = logging.getLogger(__name__)


@dataclass
class KcpcServices:
    """What KCPC's features run on, built once at startup.

    Features post only through ``publisher``, never through a
    ``DiscordPublisher`` of their own: the reconcile job uses this one, and a
    publisher knows which posts are still on their way, and so must not be sent
    again, only among its own sends. Call ``publisher.publish`` outside any
    ``db.transaction()``, so that its claim is committed before the post is
    sent (see ``tle.kcpc.core.ledger``).

    A feature that reminds members of upcoming occurrences registers a source
    with ``reminders``, which posts through ``publisher`` every minute.
    """

    settings: Settings
    clock: Clock
    db: Database
    http: HttpClient
    features: FeatureRegistry
    guild_settings: GuildSettingsRepo
    ledger: DeliveryLedger
    publisher: DiscordPublisher
    reminders: ReminderEngine
    scheduler: Scheduler

    async def shutdown(self) -> None:
        """Stop the jobs, then close the HTTP client, then the database.

        Jobs go first because they use the other two. A step that fails is
        logged and the later steps still run. Idempotent.
        """
        steps: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
            ('stop the scheduler', self.scheduler.stop),
            ('close the HTTP client', self.http.close),
            ('close the database', self.db.close),
        )
        for description, step in steps:
            try:
                await step()
            except Exception:
                logger.exception('KCPC shutdown: could not %s', description)


def get_services(bot: object) -> KcpcServices:
    """The bot's KCPC services; ``KcpcDisabledError`` if they are not running.

    They are not running when the bot was started with ``--nodb``, when every
    KCPC extension is disabled, or when they failed to start.
    """
    services = getattr(bot, 'kcpc', None)
    if not isinstance(services, KcpcServices):
        raise KcpcDisabledError()
    return services
