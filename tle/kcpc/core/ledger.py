"""The delivery ledger: every automatic post is recorded before it is sent.

A post covers one or more deliveries, each with a unique key (e.g. one reminder
for one guild). The publisher claims the keys in a transaction of their own,
committed before the post is sent, so claims are refused inside a caller's
transaction. A key that is already in the ledger means the delivery was already
handled. After sending, the publisher confirms the batch as ``sent`` or marks
it ``skipped``. Claims left unresolved by a crash or a timeout are found with
``stale_batches`` and settled by the reconciler in ``tle.kcpc.bot.publisher``.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.timeutil import from_epoch, to_epoch

logger = logging.getLogger(__name__)


class DeliveryStatus(str, Enum):
    CLAIMED = 'claimed'
    SENT = 'sent'
    SKIPPED = 'skipped'


@dataclass(frozen=True)
class Delivery:
    """One thing to post at most once, such as one reminder for one guild.

    ``subject``, ``subject_id`` and ``kind`` identify what the delivery is about
    (e.g. 'event', a Luma id and the reminder offset), for ``latest_for``.
    Past ``expires_at``, (re)sending it is pointless.
    """

    key: str
    guild_id: int
    feature: str
    subject: str | None = None
    subject_id: str | None = None
    kind: str | None = None
    occurrence_start: datetime | None = None
    revision: int | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    """A row of the ledger."""

    key: str
    batch: str
    guild_id: int
    feature: str
    subject: str | None
    subject_id: str | None
    kind: str | None
    occurrence_start: datetime | None
    revision: int | None
    status: DeliveryStatus
    channel_id: int | None
    message_id: int | None
    reason: str | None
    payload: OutgoingMessage | None
    claimed_at: datetime
    sent_at: datetime | None
    expires_at: datetime | None


def marker_for(key: str) -> str:
    """The id of a batch whose first key is ``key``, also shown in its footer."""
    return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:8]


_INSERT = """
    INSERT INTO delivery_log (
        key, batch, guild_id, feature, subject, subject_id, kind,
        occurrence_start, revision, expires_at, status, channel_id, payload,
        reason, claimed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (key) DO NOTHING
"""


class DeliveryLedger:
    """Claims, confirmations and skips of automatic posts, in ``delivery_log``."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        # Keys whose unreadable payload was already reported, to log each once.
        self._reported_payloads: set[str] = set()

    async def claim(
        self,
        deliveries: Sequence[Delivery],
        *,
        channel_id: int,
        message: OutgoingMessage,
    ) -> tuple[list[Delivery], str | None]:
        """Claim the deliveries not yet in the ledger, for one post of ``message``.

        All deliveries must share a guild and feature. The keys are claimed all
        together or not at all, in a transaction that is committed before this
        returns; so inside a transaction it raises ``RuntimeError`` (see
        ``require_outside_transaction``). Returns the claimed deliveries in key
        order with their batch id, or ``([], None)`` if every key was already
        in the ledger.
        """
        self.require_outside_transaction()
        unique = _unique_by_key(deliveries)
        _require_single_scope(unique)
        claimed_at = self._now()
        payload = message.to_json()
        claimed: list[Delivery] = []
        batch: str | None = None
        async with self._db.transaction():
            for delivery in unique:
                # The batch is named after the first key that is actually new.
                candidate = batch or marker_for(delivery.key)
                inserted = await self._insert(
                    delivery,
                    batch=candidate,
                    status=DeliveryStatus.CLAIMED,
                    claimed_at=claimed_at,
                    channel_id=channel_id,
                    payload=payload,
                )
                if inserted:
                    claimed.append(delivery)
                    batch = candidate
        return claimed, batch

    def require_outside_transaction(self) -> None:
        """Raise ``RuntimeError`` if the calling task is inside a transaction.

        A post's claim must be committed before the post is sent. Inside a
        caller's transaction it would not be: a rollback, or a crash or
        cancellation during the send, would forget a post that went out, and
        it would go out again. The database lock would also be held while
        Discord answers.
        """
        if self._db.in_transaction():
            raise RuntimeError(
                'Deliveries must be claimed and posted outside any database '
                'transaction, so that each claim is committed before its post '
                'is sent'
            )

    async def confirm(self, batch: str, message_id: int) -> int:
        """Mark the batch's claimed rows as sent. Returns the rows changed."""
        result = await self._db.execute(
            'UPDATE delivery_log SET status = ?, message_id = ?, sent_at = ? '
            'WHERE batch = ? AND status = ?',
            (
                DeliveryStatus.SENT.value,
                str(message_id),
                self._now(),
                batch,
                DeliveryStatus.CLAIMED.value,
            ),
        )
        return result.rowcount

    async def mark_skipped(self, batch: str, reason: str) -> int:
        """Mark the batch's claimed rows as skipped. Returns the rows changed."""
        result = await self._db.execute(
            'UPDATE delivery_log SET status = ?, reason = ? '
            'WHERE batch = ? AND status = ?',
            (DeliveryStatus.SKIPPED.value, reason, batch, DeliveryStatus.CLAIMED.value),
        )
        return result.rowcount

    async def release(self, batch: str) -> int:
        """Forget the batch's claimed rows, so they can be claimed again."""
        result = await self._db.execute(
            'DELETE FROM delivery_log WHERE batch = ? AND status = ?',
            (batch, DeliveryStatus.CLAIMED.value),
        )
        return result.rowcount

    async def record_skip(self, delivery: Delivery, reason: str) -> bool:
        """Record a delivery decided without sending, such as a stale reminder.

        Returns False, changing nothing, if the key is already in the ledger.
        """
        return await self._insert(
            delivery,
            batch=marker_for(delivery.key),
            status=DeliveryStatus.SKIPPED,
            claimed_at=self._now(),
            reason=reason,
        )

    async def get(self, key: str) -> DeliveryRecord | None:
        row = await self._db.fetchone(
            'SELECT * FROM delivery_log WHERE key = ?', (key,)
        )
        return None if row is None else self._record(row)

    async def batch_records(self, batch: str) -> list[DeliveryRecord]:
        rows = await self._db.fetchall(
            'SELECT * FROM delivery_log WHERE batch = ? ORDER BY key', (batch,)
        )
        return [self._record(row) for row in rows]

    async def stale_batches(
        self, claimed_before: datetime
    ) -> list[list[DeliveryRecord]]:
        """Batches still claimed since before ``claimed_before``, oldest first."""
        rows = await self._db.fetchall(
            'SELECT * FROM delivery_log WHERE status = ? AND claimed_at < ? '
            'ORDER BY claimed_at, batch, key',
            (DeliveryStatus.CLAIMED.value, to_epoch(claimed_before)),
        )
        batches: dict[str, list[DeliveryRecord]] = {}
        for row in rows:
            record = self._record(row)
            batches.setdefault(record.batch, []).append(record)
        return list(batches.values())

    async def latest_for(
        self, guild_id: int, subject: str, subject_id: str, kind: str
    ) -> DeliveryRecord | None:
        """The most recently claimed delivery of ``kind`` about one subject."""
        row = await self._db.fetchone(
            'SELECT * FROM delivery_log '
            'WHERE guild_id = ? AND subject = ? AND subject_id = ? AND kind = ? '
            'ORDER BY claimed_at DESC, rowid DESC LIMIT 1',
            (str(guild_id), subject, subject_id, kind),
        )
        return None if row is None else self._record(row)

    async def status_counts(
        self, guild_id: int | None = None
    ) -> dict[DeliveryStatus, int]:
        """The number of rows in each status (0 if none), optionally for one guild."""
        if guild_id is None:
            rows = await self._db.fetchall(
                'SELECT status, COUNT(*) FROM delivery_log GROUP BY status'
            )
        else:
            rows = await self._db.fetchall(
                'SELECT status, COUNT(*) FROM delivery_log WHERE guild_id = ? '
                'GROUP BY status',
                (str(guild_id),),
            )
        counts = dict.fromkeys(DeliveryStatus, 0)
        counts.update((DeliveryStatus(status), count) for status, count in rows)
        return counts

    async def recent_skips(self, guild_id: int, limit: int = 5) -> list[DeliveryRecord]:
        """The guild's latest skipped deliveries, newest first."""
        rows = await self._db.fetchall(
            'SELECT * FROM delivery_log WHERE guild_id = ? AND status = ? '
            'ORDER BY claimed_at DESC, rowid DESC LIMIT ?',
            (str(guild_id), DeliveryStatus.SKIPPED.value, max(limit, 0)),
        )
        return [self._record(row) for row in rows]

    def _now(self) -> int:
        return to_epoch(self._clock.now())

    async def _insert(
        self,
        delivery: Delivery,
        *,
        batch: str,
        status: DeliveryStatus,
        claimed_at: int,
        channel_id: int | None = None,
        payload: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Insert ``delivery`` as a new row; False if its key is already present."""
        result = await self._db.execute(
            _INSERT,
            (
                delivery.key,
                batch,
                str(delivery.guild_id),
                delivery.feature,
                delivery.subject,
                delivery.subject_id,
                delivery.kind,
                _epoch_or_none(delivery.occurrence_start),
                delivery.revision,
                _epoch_or_none(delivery.expires_at),
                status.value,
                None if channel_id is None else str(channel_id),
                payload,
                reason,
                claimed_at,
            ),
        )
        return result.rowcount == 1

    def _record(self, row: Row) -> DeliveryRecord:
        return DeliveryRecord(
            key=row['key'],
            batch=row['batch'],
            guild_id=int(row['guild_id']),
            feature=row['feature'],
            subject=row['subject'],
            subject_id=row['subject_id'],
            kind=row['kind'],
            occurrence_start=_datetime_or_none(row['occurrence_start']),
            revision=row['revision'],
            status=DeliveryStatus(row['status']),
            channel_id=_int_or_none(row['channel_id']),
            message_id=_int_or_none(row['message_id']),
            reason=row['reason'],
            payload=self._payload(row['key'], row['payload']),
            claimed_at=from_epoch(row['claimed_at']),
            sent_at=_datetime_or_none(row['sent_at']),
            expires_at=_datetime_or_none(row['expires_at']),
        )

    def _payload(self, key: str, raw: str | None) -> OutgoingMessage | None:
        """The stored message, or None if there is none or it cannot be read."""
        if raw is None:
            return None
        try:
            return OutgoingMessage.from_json(raw)
        except ValueError as exc:
            if key not in self._reported_payloads:
                self._reported_payloads.add(key)
                logger.warning(
                    'Ignoring the unreadable payload of delivery %s: %s', key, exc
                )
            return None


def _unique_by_key(deliveries: Sequence[Delivery]) -> list[Delivery]:
    """The first delivery for each key, sorted by key."""
    first: dict[str, Delivery] = {}
    for delivery in deliveries:
        first.setdefault(delivery.key, delivery)
    return [first[key] for key in sorted(first)]


def _require_single_scope(deliveries: Sequence[Delivery]) -> None:
    scopes = {(delivery.guild_id, delivery.feature) for delivery in deliveries}
    if len(scopes) > 1:
        raise ValueError(
            f'One post must be for one guild and feature, not {sorted(scopes)}'
        )


def _epoch_or_none(dt: datetime | None) -> int | None:
    return None if dt is None else to_epoch(dt)


def _datetime_or_none(seconds: int | None) -> datetime | None:
    return None if seconds is None else from_epoch(seconds)


def _int_or_none(text: str | None) -> int | None:
    return None if text is None else int(text)
