"""The contract between features that post and the Discord layer that sends.

A feature describes a post as ``Delivery`` rows plus an ``OutgoingMessage`` and
hands them to a ``Publisher``. ``tle.kcpc.bot.publisher.DiscordPublisher`` is the
real one; tests use fakes. ``ledger`` explains how deliveries are recorded.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from tle.kcpc.core.ledger import Delivery
from tle.kcpc.core.messages import OutgoingMessage


class PublishOutcome(str, Enum):
    """What happened to a post.

    - SENT: posted now.
    - ALREADY_HANDLED: every key was already in the ledger; nothing was sent.
    - NOT_CONFIGURED: the feature is disabled or has no channel; nothing recorded.
    - UNDELIVERABLE: the channel is missing or the bot lacks permission there;
      nothing recorded, and admins are alerted. Also returned, without an
      alert, while Discord hasn't sent the guild's channels (reason
      'guild-unavailable', e.g. just after the bot reconnects): try again later.
    - SKIPPED: claimed, then Discord refused it (403, 404 or another 4xx);
      recorded as skipped.
    - PENDING: claimed, but the outcome is unknown (a timeout, a 5xx, or an
      unexpected error once the send began); the reconciler resolves it, as it
      does for a publish cancelled mid-send.
    """

    SENT = 'sent'
    ALREADY_HANDLED = 'already_handled'
    NOT_CONFIGURED = 'not_configured'
    UNDELIVERABLE = 'undeliverable'
    SKIPPED = 'skipped'
    PENDING = 'pending'


@dataclass(frozen=True)
class PublishResult:
    """The outcome of one ``publish`` call; ``keys`` are the keys it claimed."""

    outcome: PublishOutcome
    message_id: int | None = None
    reason: str | None = None
    keys: tuple[str, ...] = ()


class Publisher(Protocol):
    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        """Post ``message`` at most once for ``deliveries`` (one guild and feature).

        Call it outside any database transaction. The deliveries are claimed,
        and the claim committed, before the message is sent; inside a caller's
        transaction, a rollback, a cancellation or a crash after the send would
        undo the claim, and the post would go out again. So record a feature's
        own state in a transaction before or after publishing, never around it.
        ``DiscordPublisher`` raises ``RuntimeError`` inside a transaction; a
        fake can check ``Database.in_transaction()`` to do the same.
        """
        ...
