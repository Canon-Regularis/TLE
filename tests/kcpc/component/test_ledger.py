"""Tests for tle.kcpc.core.ledger, and the publishing types built on it."""

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import (
    Delivery,
    DeliveryLedger,
    DeliveryRecord,
    DeliveryStatus,
    marker_for,
)
from tle.kcpc.core.messages import EmbedField, OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult, Publisher

CLAIMED = DeliveryStatus.CLAIMED
SENT = DeliveryStatus.SENT
SKIPPED = DeliveryStatus.SKIPPED

# Real snowflakes are 64-bit, beyond SQLite's REAL precision, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
MESSAGE_ID = 1_300_000_000_000_000_001
START = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)

MESSAGE = OutgoingMessage(
    title='Graphs 101 starts in 1 hour',
    fields=(EmbedField('Where', 'Bush House', inline=True),),
    footer='KCPC workshops',
)


def delivery(
    key: str, *, guild_id: int = GUILD, feature: str = 'workshops', **details: Any
) -> Delivery:
    return Delivery(key=key, guild_id=guild_id, feature=feature, **details)


async def claim_batch(ledger: DeliveryLedger, *keys: str) -> str:
    """Claim new ``keys`` as one post and return the batch."""
    claimed, batch = await ledger.claim(
        [delivery(key) for key in keys], channel_id=CHANNEL, message=MESSAGE
    )
    assert batch is not None
    assert [item.key for item in claimed] == sorted(keys)
    return batch


async def get(ledger: DeliveryLedger, key: str) -> DeliveryRecord:
    record = await ledger.get(key)
    assert record is not None
    return record


def keys_of(records: Sequence[DeliveryRecord]) -> list[str]:
    return [record.key for record in records]


def keys_of_deliveries(deliveries: Sequence[Delivery]) -> list[str]:
    return [item.key for item in deliveries]


def test_marker_is_the_start_of_the_keys_sha1() -> None:
    assert marker_for('abc') == 'a9993e36'  # the SHA-1 standard's test vector
    # Pinned: markers already posted in Discord footers must keep matching.
    assert marker_for('remind:1:event:x:60:r0') == '589263e7'


async def test_claim_records_every_new_delivery(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    detailed = delivery(
        'remind:b',
        subject='event',
        subject_id='evt-1',
        kind='60',
        occurrence_start=START,
        revision=2,
        expires_at=START,
    )
    plain = delivery('remind:a')
    claimed, batch = await ledger.claim(
        [detailed, plain], channel_id=CHANNEL, message=MESSAGE
    )

    assert claimed == [plain, detailed]  # in key order
    assert batch == marker_for('remind:a')
    assert await get(ledger, 'remind:b') == DeliveryRecord(
        key='remind:b',
        batch=batch,
        guild_id=GUILD,
        feature='workshops',
        subject='event',
        subject_id='evt-1',
        kind='60',
        occurrence_start=START,
        revision=2,
        status=CLAIMED,
        channel_id=CHANNEL,
        message_id=None,
        reason=None,
        payload=MESSAGE,
        claimed_at=clock.now(),
        sent_at=None,
        expires_at=START,
    )
    assert (await get(ledger, 'remind:a')).batch == batch


async def test_claim_overlapping_known_keys_claims_only_the_new_ones(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    earlier = await claim_batch(ledger, 'k1')
    before = await get(ledger, 'k1')
    await clock.advance(timedelta(minutes=5))

    claimed, batch = await ledger.claim(
        [delivery('k1'), delivery('k2'), delivery('k3')],
        channel_id=CHANNEL,
        message=OutgoingMessage(title='later'),
    )
    assert keys_of_deliveries(claimed) == ['k2', 'k3']
    assert batch == marker_for('k2')  # named after the first key actually claimed
    assert keys_of(await ledger.batch_records(batch)) == ['k2', 'k3']
    assert await get(ledger, 'k1') == before
    assert before.batch == earlier


async def test_claim_of_known_keys_claims_nothing(ledger: DeliveryLedger) -> None:
    await claim_batch(ledger, 'k1', 'k2')
    assert await ledger.claim(
        [delivery('k2'), delivery('k1')], channel_id=CHANNEL, message=MESSAGE
    ) == ([], None)


async def test_claim_of_no_deliveries_claims_nothing(ledger: DeliveryLedger) -> None:
    assert await ledger.claim([], channel_id=CHANNEL, message=MESSAGE) == ([], None)


async def test_claim_keeps_the_first_of_duplicate_keys(ledger: DeliveryLedger) -> None:
    first = delivery('k1', kind='first')
    claimed, _ = await ledger.claim(
        [first, delivery('k1', kind='second'), delivery('k2')],
        channel_id=CHANNEL,
        message=MESSAGE,
    )
    assert claimed == [first, delivery('k2')]
    assert (await get(ledger, 'k1')).kind == 'first'


@pytest.mark.parametrize(
    'stranger',
    [delivery('k2', guild_id=OTHER_GUILD), delivery('k2', feature='contests')],
    ids=['other guild', 'other feature'],
)
async def test_claim_rejects_deliveries_for_several_guilds_or_features(
    ledger: DeliveryLedger, db: Database, stranger: Delivery
) -> None:
    with pytest.raises(ValueError, match='one guild and feature'):
        await ledger.claim(
            [delivery('k1'), stranger], channel_id=CHANNEL, message=MESSAGE
        )
    assert await db.fetchval('SELECT COUNT(*) FROM delivery_log') == 0


async def test_concurrent_claims_of_a_key_have_one_winner(
    ledger: DeliveryLedger,
) -> None:
    results = await asyncio.gather(
        *(
            ledger.claim([delivery('k1')], channel_id=CHANNEL, message=MESSAGE)
            for _ in range(5)
        )
    )
    assert [claimed for claimed, _ in results if claimed] == [[delivery('k1')]]


async def test_a_claim_that_fails_partway_claims_nothing(
    ledger: DeliveryLedger,
) -> None:
    # The keys of one post are claimed together or not at all. A half-made
    # claim would post twice: the reconciler would resend the claimed part,
    # and a retry would post again for the rest. Here k2's naive expiry is
    # rejected only as its row is written, after k1's.
    naive = datetime(2026, 10, 1, 18, 0)

    with pytest.raises(ValueError, match='timezone-aware'):
        await ledger.claim(
            [delivery('k1'), delivery('k2', expires_at=naive)],
            channel_id=CHANNEL,
            message=MESSAGE,
        )

    assert await ledger.get('k1') is None
    assert await ledger.status_counts() == {CLAIMED: 0, SENT: 0, SKIPPED: 0}
    assert await claim_batch(ledger, 'k1', 'k2') == marker_for('k1')


async def test_a_claim_the_database_fails_partway_claims_nothing(
    ledger: DeliveryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert = ledger._insert
    failures = [sqlite3.OperationalError('disk I/O error')]

    async def insert_failing_once_for_k2(item: Delivery, **fields: Any) -> bool:
        if item.key == 'k2' and failures:
            raise failures.pop()
        return await insert(item, **fields)

    monkeypatch.setattr(ledger, '_insert', insert_failing_once_for_k2)

    with pytest.raises(sqlite3.OperationalError, match='disk I/O'):
        await ledger.claim(
            [delivery('k1'), delivery('k2')], channel_id=CHANNEL, message=MESSAGE
        )

    assert await ledger.get('k1') is None
    assert await ledger.status_counts() == {CLAIMED: 0, SENT: 0, SKIPPED: 0}
    assert await claim_batch(ledger, 'k1', 'k2') == marker_for('k1')


async def test_a_claim_cancelled_partway_claims_nothing(
    ledger: DeliveryLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As when the bot shuts down while a job is claiming.
    insert = ledger._insert
    inserting_k2 = asyncio.Event()

    async def insert_hanging_for_k2(item: Delivery, **fields: Any) -> bool:
        if item.key == 'k2':
            inserting_k2.set()
            await asyncio.Event().wait()  # until cancelled
        return await insert(item, **fields)

    monkeypatch.setattr(ledger, '_insert', insert_hanging_for_k2)
    claiming = asyncio.create_task(
        ledger.claim(
            [delivery('k1'), delivery('k2')], channel_id=CHANNEL, message=MESSAGE
        )
    )
    await asyncio.wait_for(inserting_k2.wait(), timeout=5)
    claiming.cancel()
    with pytest.raises(asyncio.CancelledError):
        await claiming

    assert await ledger.get('k1') is None
    assert await ledger.status_counts() == {CLAIMED: 0, SENT: 0, SKIPPED: 0}


async def test_a_claim_inside_a_transaction_is_refused(
    ledger: DeliveryLedger, db: Database
) -> None:
    # Its post would go out before the claim is committed: see Publisher.publish.
    with pytest.raises(RuntimeError, match='outside any database transaction'):
        async with db.transaction():
            await ledger.claim([delivery('k1')], channel_id=CHANNEL, message=MESSAGE)

    assert await ledger.get('k1') is None
    with pytest.raises(RuntimeError, match='outside any database transaction'):
        async with db.transaction():
            ledger.require_outside_transaction()
    ledger.require_outside_transaction()  # fine outside one


async def test_confirm_marks_the_claimed_rows_sent(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    batch = await claim_batch(ledger, 'k1', 'k2')
    await clock.advance(timedelta(seconds=3))
    assert await ledger.confirm(batch, MESSAGE_ID) == 2

    records = await ledger.batch_records(batch)
    assert [(r.status, r.message_id, r.sent_at) for r in records] == [
        (SENT, MESSAGE_ID, clock.now()),
        (SENT, MESSAGE_ID, clock.now()),
    ]
    assert await ledger.confirm(batch, 1) == 0  # only claimed rows change
    assert (await get(ledger, 'k1')).message_id == MESSAGE_ID


async def test_mark_skipped_records_the_reason(ledger: DeliveryLedger) -> None:
    batch = await claim_batch(ledger, 'k1', 'k2')
    assert await ledger.mark_skipped(batch, 'discord-403') == 2
    records = await ledger.batch_records(batch)
    assert [(r.status, r.reason) for r in records] == [(SKIPPED, 'discord-403')] * 2
    assert await ledger.mark_skipped(batch, 'again') == 0


async def test_confirm_and_mark_skipped_only_touch_claimed_rows(
    ledger: DeliveryLedger,
) -> None:
    sent = await claim_batch(ledger, 'sent')
    await ledger.confirm(sent, MESSAGE_ID)
    assert await ledger.record_skip(delivery('skipped'), 'stale')
    skipped = marker_for('skipped')

    assert await ledger.mark_skipped(sent, 'late') == 0
    assert await ledger.confirm(skipped, 1) == 0
    assert await ledger.mark_skipped(skipped, 'other') == 0
    assert (await get(ledger, 'sent')).status is SENT
    record = await get(ledger, 'skipped')
    assert (record.status, record.reason, record.message_id) == (SKIPPED, 'stale', None)


async def test_release_forgets_a_claim_so_it_can_be_claimed_again(
    ledger: DeliveryLedger,
) -> None:
    batch = await claim_batch(ledger, 'k1', 'k2')
    assert await ledger.release(batch) == 2
    assert await ledger.get('k1') is None
    assert await claim_batch(ledger, 'k1', 'k2') == batch


async def test_release_keeps_rows_that_are_not_claimed(ledger: DeliveryLedger) -> None:
    batch = await claim_batch(ledger, 'k1')
    await ledger.confirm(batch, MESSAGE_ID)
    assert await ledger.release(batch) == 0
    assert (await get(ledger, 'k1')).status is SENT


async def test_record_skip_inserts_only_new_keys(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    stale = delivery(
        'remind:24h', subject='event', subject_id='e1', kind='1440', revision=0
    )
    assert await ledger.record_skip(stale, 'late-discovery') is True
    assert await get(ledger, 'remind:24h') == DeliveryRecord(
        key='remind:24h',
        batch=marker_for('remind:24h'),
        guild_id=GUILD,
        feature='workshops',
        subject='event',
        subject_id='e1',
        kind='1440',
        occurrence_start=None,
        revision=0,
        status=SKIPPED,
        channel_id=None,
        message_id=None,
        reason='late-discovery',
        payload=None,
        claimed_at=clock.now(),
        sent_at=None,
        expires_at=None,
    )
    assert await ledger.record_skip(stale, 'again') is False
    assert (await get(ledger, 'remind:24h')).reason == 'late-discovery'


async def test_record_skip_leaves_a_claimed_key_alone(ledger: DeliveryLedger) -> None:
    await claim_batch(ledger, 'k1')
    assert await ledger.record_skip(delivery('k1'), 'stale') is False
    assert (await get(ledger, 'k1')).status is CLAIMED


async def test_get_unknown_key(ledger: DeliveryLedger) -> None:
    assert await ledger.get('missing') is None


async def test_unreadable_payload_is_ignored_and_reported_once(
    ledger: DeliveryLedger, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await claim_batch(ledger, 'k1')
    await db.execute("UPDATE delivery_log SET payload = '{broken' WHERE key = 'k1'")
    for _ in range(2):
        assert (await get(ledger, 'k1')).payload is None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == 'tle.kcpc.core.ledger' and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert 'k1' in warnings[0]


async def test_batch_records_lists_one_batch_in_key_order(
    ledger: DeliveryLedger,
) -> None:
    batch = await claim_batch(ledger, 'k2', 'k1')
    other = await claim_batch(ledger, 'k3')
    assert keys_of(await ledger.batch_records(batch)) == ['k1', 'k2']
    assert keys_of(await ledger.batch_records(other)) == ['k3']
    assert await ledger.batch_records('00000000') == []


async def test_stale_batches_groups_old_claims_oldest_first(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    start = clock.now()
    oldest = await claim_batch(ledger, 'z1', 'z2')
    await clock.advance(timedelta(minutes=1))
    newer = await claim_batch(ledger, 'a1')
    await clock.advance(timedelta(minutes=1))
    await ledger.confirm(await claim_batch(ledger, 'sent'), MESSAGE_ID)
    await ledger.record_skip(delivery('skipped'), 'stale')
    await clock.advance(timedelta(minutes=10))
    await claim_batch(ledger, 'recent')

    stale = await ledger.stale_batches(start + timedelta(minutes=5))
    assert [keys_of(group) for group in stale] == [['z1', 'z2'], ['a1']]
    assert [group[0].batch for group in stale] == [oldest, newer]
    assert await ledger.stale_batches(start) == []  # strictly before the cutoff


async def test_latest_for_returns_the_most_recent_claim(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    async def claim(key: str, **details: Any) -> None:
        fields = {'subject': 'event', 'subject_id': 'e1', 'kind': '60'} | details
        await ledger.claim(
            [delivery(key, **fields)], channel_id=CHANNEL, message=MESSAGE
        )

    await claim('first')
    await clock.advance(timedelta(hours=1))
    # Claimed in the same second: the later insert wins, whatever the key.
    await claim('z-second')
    await claim('a-third')
    await claim('other-kind', kind='1440')
    await claim('other-subject', subject_id='e2')
    await ledger.claim(
        [delivery('elsewhere', guild_id=OTHER_GUILD, subject='event', subject_id='e1')],
        channel_id=CHANNEL,
        message=MESSAGE,
    )

    latest = await ledger.latest_for(GUILD, 'event', 'e1', '60')
    assert latest is not None
    assert latest.key == 'a-third'
    assert await ledger.latest_for(GUILD, 'event', 'e3', '60') is None


async def test_status_counts(ledger: DeliveryLedger) -> None:
    assert await ledger.status_counts() == {CLAIMED: 0, SENT: 0, SKIPPED: 0}

    await ledger.confirm(await claim_batch(ledger, 's1', 's2'), MESSAGE_ID)
    await claim_batch(ledger, 'c1')
    await ledger.record_skip(delivery('x1', guild_id=OTHER_GUILD), 'stale')

    assert await ledger.status_counts() == {CLAIMED: 1, SENT: 2, SKIPPED: 1}
    assert await ledger.status_counts(GUILD) == {CLAIMED: 1, SENT: 2, SKIPPED: 0}
    assert await ledger.status_counts(OTHER_GUILD) == {CLAIMED: 0, SENT: 0, SKIPPED: 1}


async def test_recent_skips_are_newest_first(
    ledger: DeliveryLedger, clock: FakeClock
) -> None:
    for i in range(6):
        await ledger.record_skip(delivery(f'skip-{i}'), f'reason {i}')
        await clock.advance(timedelta(minutes=1))
    await ledger.mark_skipped(await claim_batch(ledger, 'refused'), 'discord-404')
    await claim_batch(ledger, 'claimed')
    await ledger.record_skip(delivery('elsewhere', guild_id=OTHER_GUILD), 'stale')

    assert keys_of(await ledger.recent_skips(GUILD)) == [
        'refused',
        'skip-5',
        'skip-4',
        'skip-3',
        'skip-2',
    ]
    assert keys_of(await ledger.recent_skips(GUILD, limit=2)) == ['refused', 'skip-5']
    assert keys_of(await ledger.recent_skips(OTHER_GUILD)) == ['elsewhere']


class RecordingPublisher:
    """A fake Publisher, as feature tests will use."""

    def __init__(self) -> None:
        self.posts: list[tuple[list[Delivery], OutgoingMessage]] = []

    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        self.posts.append((list(deliveries), message))
        return PublishResult(
            PublishOutcome.SENT,
            message_id=MESSAGE_ID,
            keys=tuple(item.key for item in deliveries),
        )


async def test_publishing_types() -> None:
    publisher: Publisher = RecordingPublisher()
    result = await publisher.publish([delivery('k1')], MESSAGE)
    assert result == PublishResult(PublishOutcome.SENT, MESSAGE_ID, None, ('k1',))
    assert [outcome.value for outcome in PublishOutcome] == [
        'sent',
        'already_handled',
        'not_configured',
        'undeliverable',
        'skipped',
        'pending',
    ]
