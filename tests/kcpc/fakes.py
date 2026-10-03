"""Fakes shared by the KCPC tests."""

import itertools
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from tle.kcpc.core.ledger import Delivery, DeliveryLedger
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.settings import GuildSettingsRepo

# Real snowflakes are 64-bit, beyond SQLite's REAL precision, so use big ones.
FIRST_MESSAGE_ID = 1_300_000_000_000_000_001

# The outcomes that FakePublisher.fail_next can stage, besides an exception.
_FAILURE_OUTCOMES = (PublishOutcome.PENDING, PublishOutcome.SKIPPED)


@dataclass(frozen=True)
class Post:
    """A message that ``FakePublisher`` posted, for the deliveries it claimed."""

    deliveries: tuple[Delivery, ...]
    message: OutgoingMessage
    message_id: int

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(delivery.key for delivery in self.deliveries)


class FakePublisher:
    """A ``Publisher`` that posts by appending to ``posts``, on a real ledger.

    It does what ``DiscordPublisher`` does with a working channel. It raises
    ``RuntimeError`` inside a database transaction, and ``ValueError`` unless
    the deliveries share one guild and feature. It returns NOT_CONFIGURED,
    recording nothing, while the feature is disabled or has no channel. Then it
    claims the deliveries, returning ALREADY_HANDLED if none is new, records
    the post, confirms it in the ledger and returns SENT. ``fail_next`` makes
    posts fail instead.
    """

    def __init__(
        self, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
    ) -> None:
        self.posts: list[Post] = []
        self._settings = guild_settings
        self._ledger = ledger
        self._failures: deque[PublishOutcome | Exception] = deque()
        self._message_ids = itertools.count(FIRST_MESSAGE_ID)

    def fail_next(self, *failures: PublishOutcome | Exception) -> None:
        """Make the next posts that claim deliveries fail, one each, in order.

        - PENDING: the batch stays claimed, as when a send times out.
        - SKIPPED: the batch is marked skipped, as when Discord refuses it.
        - An exception: the claim is released and the exception raised, as when
          a post can't be built.

        A publish that claims nothing (the feature is not configured, or every
        key is handled) leaves them for the next.
        """
        for failure in failures:
            if isinstance(failure, PublishOutcome) and failure not in _FAILURE_OUTCOMES:
                raise ValueError(f'FakePublisher cannot fail with {failure}')
        self._failures.extend(failures)

    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        self._ledger.require_outside_transaction()
        guild_id, feature = _scope(deliveries)
        config = await self._settings.get(guild_id, feature)
        if not config.enabled:
            return PublishResult(PublishOutcome.NOT_CONFIGURED, reason='disabled')
        if config.channel_id is None:
            return PublishResult(PublishOutcome.NOT_CONFIGURED, reason='no-channel')
        claimed, batch = await self._ledger.claim(
            deliveries, channel_id=config.channel_id, message=message
        )
        if batch is None:
            return PublishResult(PublishOutcome.ALREADY_HANDLED)
        keys = tuple(delivery.key for delivery in claimed)
        failure = self._failures.popleft() if self._failures else None
        if isinstance(failure, Exception):
            await self._ledger.release(batch)
            raise failure
        if failure is PublishOutcome.PENDING:
            return PublishResult(PublishOutcome.PENDING, reason='timeout', keys=keys)
        if failure is PublishOutcome.SKIPPED:
            await self._ledger.mark_skipped(batch, 'discord-403')
            return PublishResult(
                PublishOutcome.SKIPPED, reason='discord-403', keys=keys
            )
        message_id = next(self._message_ids)
        self.posts.append(Post(tuple(claimed), message, message_id))
        await self._ledger.confirm(batch, message_id)
        return PublishResult(PublishOutcome.SENT, message_id=message_id, keys=keys)


def _scope(deliveries: Sequence[Delivery]) -> tuple[int, str]:
    """The one guild and feature of ``deliveries``, as DiscordPublisher insists."""
    scopes = {(delivery.guild_id, delivery.feature) for delivery in deliveries}
    if len(scopes) != 1:
        raise ValueError(
            'A post needs deliveries for exactly one guild and feature, '
            f'not {sorted(scopes)}'
        )
    (scope,) = scopes
    return scope
