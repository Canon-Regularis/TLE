"""Tests for tle.kcpc.core.reminders.plan, the pure rules of what to post when.

Each rule is checked at the exact instants where its answer changes: ``now``
moves in microseconds (``TICK``), while stored starts are whole seconds. Plans
are compared in brief, one line per action: 'send 60m evt-1', 'skip 1440m evt-1
late', 'send moved evt-1', and 'send 60m evt-1+evt-2' for a grouped reminder.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.ledger import Delivery, DeliveryRecord, DeliveryStatus
from tle.kcpc.core.reminders import (
    ActionKind,
    Notice,
    NoticeKind,
    Occurrence,
    PlannedAction,
    ReminderPolicy,
    plan,
    reminder_kind,
)

CLAIMED = DeliveryStatus.CLAIMED
SENT = DeliveryStatus.SENT
SKIPPED = DeliveryStatus.SKIPPED

GUILD = 1_100_000_000_000_000_001
FEATURE = 'workshops'
EVENT = 'event'

TICK = timedelta(microseconds=1)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

S = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)  # when evt-1 starts
CLAIMED_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

DEFAULT = ReminderPolicy(offsets=(DAY, HOUR))  # 24h and 1h, as workshops have
HOURLY = ReminderPolicy(offsets=(HOUR,))
NOTICES_ONLY = ReminderPolicy(offsets=())
# For the notice rules: between the 24h and 1h reminders, so a 24h record for a
# start less than a day away keeps the plan to notices.
BEFORE = S - 10 * HOUR


def occurrence(
    subject_id: str = 'evt-1',
    *,
    start: datetime = S,
    revision: int = 0,
    cancelled: bool = False,
    title: str | None = None,
    subject: str = EVENT,
) -> Occurrence:
    return Occurrence(
        subject=subject,
        subject_id=subject_id,
        title=f'Workshop {subject_id}' if title is None else title,
        start=start,
        end=start + 2 * HOUR,
        url=f'https://luma.com/{subject_id}',
        revision=revision,
        cancelled=cancelled,
    )


def record(
    kind: str,
    *,
    start: datetime | None = S,
    revision: int | None = 0,
    status: DeliveryStatus = SENT,
    subject_id: str = 'evt-1',
    subject: str = EVENT,
) -> DeliveryRecord:
    """A ledger row of ``kind`` about ``subject_id``, for a start at ``start``."""
    return DeliveryRecord(
        key=f'test:{subject}:{subject_id}:{kind}:r{revision}',
        batch='0123abcd',
        guild_id=GUILD,
        feature=FEATURE,
        subject=subject,
        subject_id=subject_id,
        kind=kind,
        occurrence_start=start,
        revision=revision,
        status=status,
        channel_id=None,
        message_id=None,
        reason=None,
        payload=None,
        claimed_at=CLAIMED_AT,
        sent_at=None,
        expires_at=None,
    )


def history(records: Sequence[DeliveryRecord]) -> dict[str, list[DeliveryRecord]]:
    """``records``, oldest first, by subject id, as history_for returns them."""
    by_id: dict[str, list[DeliveryRecord]] = {}
    for item in records:
        assert item.subject_id is not None
        by_id.setdefault(item.subject_id, []).append(item)
    return by_id


def plan_at(
    now: datetime,
    *occurrences: Occurrence,
    records: Sequence[DeliveryRecord] = (),
    policy: ReminderPolicy = DEFAULT,
) -> list[PlannedAction]:
    return plan(now, GUILD, FEATURE, policy, occurrences, history(records))


def brief(
    now: datetime,
    *occurrences: Occurrence,
    records: Sequence[DeliveryRecord] = (),
    policy: ReminderPolicy = DEFAULT,
) -> list[str]:
    actions = plan_at(now, *occurrences, records=records, policy=policy)
    return [describe(action) for action in actions]


def describe(action: PlannedAction) -> str:
    notice = action.notice
    what = notice.kind.value if notice.offset is None else reminder_kind(notice.offset)
    ids = '+'.join(item.subject_id for item in notice.occurrences)
    line = f'{action.kind.value} {what} {ids}'
    return line if action.reason is None else f'{line} {action.reason}'


def only_notice(
    now: datetime, item: Occurrence, *, records: Sequence[DeliveryRecord]
) -> Notice:
    """The notice of the one action planned for ``item``."""
    (action,) = plan_at(now, item, records=records)
    return action.notice


@pytest.mark.parametrize(
    ('offset', 'kind'),
    [
        (timedelta(0), 'start'),
        (MINUTE, '1m'),
        (HOUR, '60m'),
        (DAY, '1440m'),
        (timedelta(weeks=1), '10080m'),
    ],
)
def test_reminder_kind_counts_whole_minutes(offset: timedelta, kind: str) -> None:
    assert reminder_kind(offset) == kind


@pytest.mark.parametrize('offset', [-MINUTE, 30 * SECOND, MINUTE + TICK])
def test_reminder_kind_rejects_negative_and_partial_minutes(offset: timedelta) -> None:
    with pytest.raises(ValueError, match='zero or positive whole minutes'):
        reminder_kind(offset)


def test_policy_defaults() -> None:
    policy = ReminderPolicy(offsets=(HOUR,))

    assert policy.announce_start is False
    assert policy.start_window == 10 * MINUTE
    assert policy.late_window == 30 * MINUTE
    assert policy.min_tolerance == 10 * MINUTE
    assert policy.horizon == 400 * DAY


@pytest.mark.parametrize(
    'offsets', [(timedelta(0),), (-HOUR,), (HOUR, 90 * SECOND), (SECOND,)]
)
def test_policy_offsets_must_be_positive_whole_minutes(
    offsets: tuple[timedelta, ...],
) -> None:
    with pytest.raises(ValueError, match='positive whole minutes'):
        ReminderPolicy(offsets=offsets)


@pytest.mark.parametrize('offsets', [(HOUR, HOUR), (HOUR, DAY, 3600 * SECOND)])
def test_policy_offsets_must_be_distinct(offsets: tuple[timedelta, ...]) -> None:
    with pytest.raises(ValueError, match='distinct'):
        ReminderPolicy(offsets=offsets)


@pytest.mark.parametrize(
    ('field', 'value', 'message'),
    [
        ('late_window', -TICK, 'late_window must not be negative'),
        ('start_window', timedelta(0), 'start_window must be positive'),
        ('min_tolerance', timedelta(0), 'min_tolerance must be positive'),
        ('horizon', timedelta(0), 'horizon must be positive'),
    ],
)
def test_policy_windows_are_checked(field: str, value: timedelta, message: str) -> None:
    change: dict[str, Any] = {field: value}

    with pytest.raises(ValueError, match=message):
        replace(HOURLY, **change)


def test_a_policy_may_have_no_offsets_and_no_late_window() -> None:
    policy = ReminderPolicy(offsets=(), late_window=timedelta(0))

    assert policy.offsets == ()


@pytest.mark.parametrize('field', ['start', 'end'])
def test_occurrence_times_must_be_timezone_aware(field: str) -> None:
    change: dict[str, Any] = {field: datetime(2026, 10, 2, 18, 0)}

    with pytest.raises(ValueError, match=f'Occurrence.{field} must be timezone-aware'):
        replace(occurrence(), **change)


def test_an_occurrence_starts_on_a_whole_second() -> None:
    # The ledger keeps whole seconds: a fraction would make the start look moved.
    with pytest.raises(ValueError, match='Occurrence.start must be a whole second'):
        occurrence(start=S + TICK)

    assert occurrence(start=S).end == S + 2 * HOUR  # an end may be anything


def test_a_planned_skip_needs_a_reason_and_a_send_has_none() -> None:
    notice = Notice(NoticeKind.REMINDER, (occurrence(),), offset=HOUR)

    with pytest.raises(ValueError, match='A skip needs a reason'):
        PlannedAction(ActionKind.SKIP, notice, ())
    with pytest.raises(ValueError, match='only a skip has one'):
        PlannedAction(ActionKind.SEND, notice, (), reason='late')


def test_kind_values() -> None:
    assert [kind.value for kind in NoticeKind] == [
        'reminder',
        'moved',
        'cancelled',
        'reinstated',
    ]
    assert [kind.value for kind in ActionKind] == ['send', 'skip']


def test_each_occurrence_may_be_listed_once() -> None:
    with pytest.raises(ValueError, match=r"\('event', 'evt-1'\) is listed more"):
        plan_at(S - HOUR, occurrence(), occurrence(start=S + HOUR))

    # The same id under another subject is another occurrence.
    contest = occurrence(subject='contest')
    (action,) = plan_at(S - HOUR, occurrence(), contest, policy=HOURLY)
    assert [item.key for item in action.deliveries] == [
        f'remind:{GUILD}:contest:evt-1:60m:r0',
        f'remind:{GUILD}:event:evt-1:60m:r0',
    ]


@dataclass(frozen=True)
class Workshop(Occurrence):
    """An occurrence carrying more for rendering, as sources may define."""

    location: str | None = None


def test_notices_hold_the_occurrences_given_so_sources_can_add_fields() -> None:
    item = Workshop(EVENT, 'evt-1', 'Graphs', S, None, None, 0, location='Bush House')

    (action,) = plan_at(S - HOUR, item, policy=HOURLY)

    (planned,) = action.notice.occurrences
    assert planned is item


@pytest.mark.parametrize(
    ('now', 'expected'),
    [
        (S - HOUR - TICK, []),
        (S - HOUR, ['send 60m evt-1']),
        (S - TICK, ['send 60m evt-1']),
        (S, []),
    ],
)
def test_a_reminder_is_due_from_its_offset_before_the_start_until_the_start(
    now: datetime, expected: list[str]
) -> None:
    assert brief(now, occurrence(), policy=HOURLY) == expected


@pytest.mark.parametrize(
    ('now', 'expected'),
    [
        (S - TICK, []),
        (S, ['send start evt-1']),
        (S + 10 * MINUTE - TICK, ['send start evt-1']),
        (S + 10 * MINUTE, []),
    ],
)
def test_the_start_is_announced_from_the_start_until_start_window_after(
    now: datetime, expected: list[str]
) -> None:
    policy = ReminderPolicy(offsets=(), announce_start=True)

    assert brief(now, occurrence(), policy=policy) == expected


def test_the_start_window_can_be_shortened() -> None:
    policy = ReminderPolicy(offsets=(), announce_start=True, start_window=5 * MINUTE)

    assert brief(S + 5 * MINUTE - TICK, occurrence(), policy=policy) == [
        'send start evt-1'
    ]
    assert brief(S + 5 * MINUTE, occurrence(), policy=policy) == []


def test_the_start_is_not_announced_unless_asked() -> None:
    assert brief(S, occurrence(), policy=HOURLY) == []


def test_a_policy_without_offsets_posts_no_reminders() -> None:
    for now in (S - DAY, S - HOUR, S - TICK, S, S + MINUTE):
        assert brief(now, occurrence(), policy=NOTICES_ONLY) == []


@pytest.mark.parametrize('status', [CLAIMED, SENT, SKIPPED])
def test_a_reminder_in_the_ledger_in_any_status_is_handled(
    status: DeliveryStatus,
) -> None:
    records = [record('60m', status=status)]

    assert brief(S - HOUR, occurrence(), records=records, policy=HOURLY) == []


@pytest.mark.parametrize(
    ('moved_by', 'expected'),
    [
        (-HOUR, ['send 60m evt-1']),
        (-HOUR + SECOND, []),
        (HOUR - SECOND, []),
        (HOUR, ['send 60m evt-1']),
    ],
)
def test_a_reminder_is_handled_by_one_for_a_start_less_than_its_offset_away(
    moved_by: timedelta, expected: list[str]
) -> None:
    # The occurrence moved by moved_by since its reminder was recorded.
    records = [record('60m', start=S - moved_by)]

    assert brief(S - 30 * MINUTE, occurrence(), records=records, policy=HOURLY) == (
        expected
    )


@pytest.mark.parametrize(
    ('moved_by', 'expected'),
    [
        (-10 * MINUTE, ['send 5m evt-1']),
        (-10 * MINUTE + SECOND, []),
        (10 * MINUTE - SECOND, []),
        (10 * MINUTE, ['send 5m evt-1']),
    ],
)
def test_a_short_reminder_is_handled_by_one_less_than_min_tolerance_away(
    moved_by: timedelta, expected: list[str]
) -> None:
    policy = ReminderPolicy(offsets=(5 * MINUTE,))
    records = [record('5m', start=S - moved_by)]

    assert brief(S - 2 * MINUTE, occurrence(), records=records, policy=policy) == (
        expected
    )


@pytest.mark.parametrize(
    ('moved_by', 'expected'),
    [(5 * MINUTE - SECOND, []), (5 * MINUTE, ['send 5m evt-1'])],
)
def test_min_tolerance_can_be_lowered(moved_by: timedelta, expected: list[str]) -> None:
    policy = ReminderPolicy(offsets=(5 * MINUTE,), min_tolerance=SECOND)
    records = [record('5m', start=S - moved_by)]

    assert brief(S - 2 * MINUTE, occurrence(), records=records, policy=policy) == (
        expected
    )


@pytest.mark.parametrize(
    ('moved_by', 'expected'),
    [
        (timedelta(0), []),
        (10 * MINUTE - SECOND, []),
        (10 * MINUTE, ['send start evt-1']),
    ],
)
def test_the_start_announcement_is_handled_by_one_less_than_min_tolerance_away(
    moved_by: timedelta, expected: list[str]
) -> None:
    policy = ReminderPolicy(offsets=(), announce_start=True)
    records = [record('start', start=S - moved_by)]

    assert brief(S + MINUTE, occurrence(), records=records, policy=policy) == expected


@pytest.mark.parametrize('kind', ['1440m', 'start', 'moved', 'cancelled', '6m'])
def test_only_a_record_of_the_reminders_kind_handles_it(kind: str) -> None:
    records = [record(kind)]

    assert brief(S - HOUR, occurrence(), records=records, policy=HOURLY) == [
        'send 60m evt-1'
    ]


def test_a_reminder_recorded_under_another_revision_still_handles_it() -> None:
    # Say the occurrence was cancelled and reinstated at the same time.
    records = [record('60m', revision=0)]

    assert brief(S - HOUR, occurrence(revision=2), records=records, policy=HOURLY) == []


def test_records_about_another_subject_are_ignored() -> None:
    records = [record('60m', subject='contest')]

    assert brief(S - HOUR, occurrence(), records=records, policy=HOURLY) == [
        'send 60m evt-1'
    ]


def test_a_record_without_a_start_handles_no_reminder() -> None:
    records = [record('60m', start=None)]

    assert brief(S - HOUR, occurrence(), records=records, policy=HOURLY) == [
        'send 60m evt-1'
    ]


def test_a_reminder_gives_way_to_a_shorter_one_that_is_due() -> None:
    assert brief(S - HOUR, occurrence()) == [
        'skip 1440m evt-1 superseded',
        'send 60m evt-1',
    ]


@pytest.mark.parametrize(
    ('now', 'expected'),
    [
        (S - HOUR - TICK, ['skip 1440m evt-1 late']),
        (S - 90 * MINUTE, ['skip 1440m evt-1 late']),
        (S - 90 * MINUTE - TICK, ['send 1440m evt-1']),
        (S - DAY, ['send 1440m evt-1']),
        (S - DAY - TICK, []),
    ],
)
def test_a_reminder_is_late_if_a_shorter_one_is_due_within_late_window(
    now: datetime, expected: list[str]
) -> None:
    assert brief(now, occurrence()) == expected


def test_the_late_window_can_be_closed() -> None:
    policy = ReminderPolicy(offsets=(DAY, HOUR), late_window=timedelta(0))

    assert brief(S - HOUR - TICK, occurrence(), policy=policy) == ['send 1440m evt-1']
    assert brief(S - HOUR, occurrence(), policy=policy) == [
        'skip 1440m evt-1 superseded',
        'send 60m evt-1',
    ]


@pytest.mark.parametrize(
    ('now', 'expected'),
    [
        (
            S - HOUR,
            [
                'skip 1440m evt-1 superseded',
                'skip 120m evt-1 superseded',
                'send 60m evt-1',
            ],
        ),
        (S - 90 * MINUTE, ['skip 1440m evt-1 superseded', 'skip 120m evt-1 late']),
        (S - 90 * MINUTE - TICK, ['skip 1440m evt-1 superseded', 'send 120m evt-1']),
        (S - 2 * HOUR, ['skip 1440m evt-1 superseded', 'send 120m evt-1']),
        (S - 150 * MINUTE, ['skip 1440m evt-1 late']),
        (S - 150 * MINUTE - TICK, ['send 1440m evt-1']),
    ],
)
def test_each_reminder_gives_way_to_the_next_shorter_one(
    now: datetime, expected: list[str]
) -> None:
    policy = ReminderPolicy(offsets=(HOUR, DAY, 2 * HOUR))  # in any order

    assert brief(now, occurrence(), policy=policy) == expected


def test_a_reminder_gives_way_to_a_shorter_one_even_if_that_went_out() -> None:
    # As when the 24h reminder is added to the policy after the 1h one went out.
    records = [record('60m')]

    assert brief(S - 30 * MINUTE, occurrence(), records=records) == [
        'skip 1440m evt-1 superseded'
    ]


def test_the_start_announcement_never_makes_a_reminder_give_way() -> None:
    policy = ReminderPolicy(offsets=(HOUR,), announce_start=True)

    assert brief(S - TICK, occurrence(), policy=policy) == ['send 60m evt-1']
    assert brief(S, occurrence(), policy=policy) == ['send start evt-1']


def test_reminders_with_the_same_offset_and_start_are_one_post() -> None:
    occurrences = (
        occurrence('evt-2', title='Graphs'),
        occurrence('evt-1', title='Trees'),
        occurrence('evt-3', title='Graphs'),
    )

    (action,) = plan_at(S - HOUR, *occurrences, policy=HOURLY)

    assert action.kind is ActionKind.SEND
    assert action.notice == Notice(
        NoticeKind.REMINDER,
        (occurrences[0], occurrences[2], occurrences[1]),  # by title, then id
        offset=HOUR,
    )
    assert [item.key for item in action.deliveries] == [
        f'remind:{GUILD}:event:evt-1:60m:r0',
        f'remind:{GUILD}:event:evt-2:60m:r0',
        f'remind:{GUILD}:event:evt-3:60m:r0',
    ]


def test_reminders_with_another_start_or_offset_are_posts_of_their_own() -> None:
    occurrences = (
        occurrence('evt-3', start=S + 23 * HOUR),
        occurrence('evt-2', start=S + SECOND),
        occurrence('evt-1'),
    )

    assert brief(S - 30 * MINUTE, *occurrences) == [
        'skip 1440m evt-1 superseded',
        'send 60m evt-1',
        'skip 1440m evt-2 superseded',
        'send 60m evt-2',
        'send 1440m evt-3',
    ]


def test_skips_are_never_grouped() -> None:
    assert brief(S - HOUR, occurrence('evt-2'), occurrence('evt-1')) == [
        'skip 1440m evt-1 superseded',
        'skip 1440m evt-2 superseded',
        'send 60m evt-1+evt-2',
    ]


def test_only_the_occurrences_still_needing_a_reminder_share_its_post() -> None:
    records = [record('60m', subject_id='evt-1')]

    assert brief(
        S - HOUR,
        occurrence('evt-1'),
        occurrence('evt-2'),
        records=records,
        policy=HOURLY,
    ) == ['send 60m evt-2']


def test_a_reminder_post_is_about_at_most_ten_occurrences() -> None:
    # Discord shows at most 25 fields and 6000 characters of an embed, and an
    # occurrence left out of a post would still count as told. Titles run
    # against ids here, to show that posts are filled in title order.
    occurrences = [
        occurrence(f'evt-{n:02}', title=f'Workshop {23 - n:02}') for n in range(1, 24)
    ]
    by_title = occurrences[::-1]

    actions = plan_at(S - HOUR, *occurrences, policy=HOURLY)

    assert len(actions) == 3
    assert {action.notice.occurrences for action in actions} == {
        tuple(by_title[:10]),
        tuple(by_title[10:20]),
        tuple(by_title[20:]),
    }
    for action in actions:  # each post carries the deliveries of its own
        assert action.kind is ActionKind.SEND
        assert action.notice.offset == HOUR
        assert [item.subject_id for item in action.deliveries] == sorted(
            item.subject_id for item in action.notice.occurrences
        )
    assert plan_at(S - HOUR, *reversed(occurrences), policy=HOURLY) == actions


def test_the_post_an_occurrence_lands_in_does_not_depend_on_their_order() -> None:
    # Eleven reminders with one title, two of them an event and a contest with
    # one id: the subject decides which of those two gets the first post.
    occurrences = [occurrence(f'evt-{n}', title='Graphs') for n in range(10)]
    occurrences.append(occurrence('evt-9', title='Graphs', subject='contest'))

    for order in (occurrences, occurrences[::-1]):
        actions = plan_at(S - HOUR, *order, policy=HOURLY)

        assert [describe(action) for action in actions] == [
            'send 60m ' + '+'.join(f'evt-{n}' for n in range(10)),
            'send 60m evt-9',
        ]
        assert [item.key for item in actions[1].deliveries] == [
            f'remind:{GUILD}:event:evt-9:60m:r0'
        ]


def test_notices_are_never_grouped() -> None:
    records = [record('1440m', subject_id='evt-1'), record('1440m', subject_id='evt-2')]
    cancelled = [
        occurrence(subject_id, revision=1, cancelled=True)
        for subject_id in ('evt-2', 'evt-1')
    ]

    assert brief(BEFORE, *cancelled, records=records) == [
        'send cancelled evt-1',
        'send cancelled evt-2',
    ]


def mixed_occurrences() -> tuple[list[Occurrence], list[DeliveryRecord]]:
    """Occurrences needing every sort of action at S - 1h, with their records."""
    occurrences = [
        occurrence('evt-1'),
        occurrence('evt-2', start=S - 30 * MINUTE),
        occurrence('evt-3', start=S + 5 * HOUR, revision=1, cancelled=True),
        occurrence('evt-4', start=S + 2 * HOUR, revision=1),
    ]
    records = [
        record('1440m', subject_id='evt-3', start=S + 5 * HOUR),
        record('1440m', subject_id='evt-4', start=S + HOUR),
    ]
    return occurrences, records


def test_notices_come_first_then_reminders_by_start_and_longest_offset() -> None:
    occurrences, records = mixed_occurrences()

    assert brief(S - HOUR, *occurrences, records=records) == [
        'send moved evt-4',
        'send cancelled evt-3',
        'skip 1440m evt-2 superseded',
        'send 60m evt-2',
        'skip 1440m evt-1 superseded',
        'send 60m evt-1',
    ]


def test_the_plan_does_not_depend_on_the_order_of_occurrences() -> None:
    occurrences, records = mixed_occurrences()
    expected = plan_at(S - HOUR, *occurrences, records=records)

    for order in itertools.permutations(occurrences):
        assert plan_at(S - HOUR, *order, records=records) == expected


def test_a_reminders_delivery() -> None:
    (action,) = plan_at(S - HOUR, occurrence(revision=2), policy=HOURLY)

    assert action.deliveries == (
        Delivery(
            key=f'remind:{GUILD}:event:evt-1:60m:r2',
            guild_id=GUILD,
            feature=FEATURE,
            subject=EVENT,
            subject_id='evt-1',
            kind='60m',
            occurrence_start=S,
            revision=2,
            expires_at=S,
        ),
    )


def test_the_start_announcements_delivery_expires_at_the_end_of_its_window() -> None:
    policy = ReminderPolicy(offsets=(), announce_start=True, start_window=7 * MINUTE)

    (action,) = plan_at(S, occurrence(), policy=policy)

    (delivery,) = action.deliveries
    assert delivery.key == f'remind:{GUILD}:event:evt-1:start:r0'
    assert delivery.kind == 'start'
    assert delivery.expires_at == S + 7 * MINUTE
    assert action.notice == Notice(
        NoticeKind.REMINDER, (occurrence(),), offset=timedelta(0)
    )


def test_a_skip_carries_the_reminder_it_skips() -> None:
    (skip, _) = plan_at(S - HOUR, occurrence())

    assert skip.kind is ActionKind.SKIP
    assert skip.reason == 'superseded'
    assert skip.notice == Notice(NoticeKind.REMINDER, (occurrence(),), offset=DAY)
    (delivery,) = skip.deliveries
    assert delivery.key == f'remind:{GUILD}:event:evt-1:1440m:r0'
    assert delivery.expires_at == S


@pytest.mark.parametrize(
    ('records', 'item', 'kind'),
    [
        ([record('1440m')], occurrence(revision=3, cancelled=True), 'cancelled'),
        (
            [record('1440m'), record('cancelled', revision=1)],
            occurrence(revision=3),
            'reinstated',
        ),
        ([record('1440m', start=S - HOUR)], occurrence(revision=3), 'moved'),
    ],
)
def test_a_notices_delivery(
    records: list[DeliveryRecord], item: Occurrence, kind: str
) -> None:
    (action,) = plan_at(BEFORE, item, records=records)

    assert action.kind is ActionKind.SEND
    assert action.notice.kind.value == kind
    assert action.deliveries == (
        Delivery(
            key=f'notice:{GUILD}:event:evt-1:{kind}:r3',
            guild_id=GUILD,
            feature=FEATURE,
            subject=EVENT,
            subject_id='evt-1',
            kind=kind,
            occurrence_start=S,
            revision=3,
            expires_at=S,
        ),
    )


@pytest.mark.parametrize(
    'records',
    [
        [record('1440m')],
        [record('1440m', start=S - HOUR), record('moved', revision=1)],
        [
            record('1440m'),
            record('cancelled', revision=1),
            record('reinstated', revision=2),
        ],
    ],
    ids=['reminded', 'told of a move', 'told it was back'],
)
def test_a_cancellation_is_announced_if_members_were_last_told_it_was_on(
    records: list[DeliveryRecord],
) -> None:
    cancelled = occurrence(revision=3, cancelled=True)

    notice = only_notice(BEFORE, cancelled, records=records)

    assert notice == Notice(NoticeKind.CANCELLED, (cancelled,))


@pytest.mark.parametrize(
    'records',
    [
        [],
        [record('1440m', status=CLAIMED)],
        [record('1440m', status=SKIPPED)],
        [record('1440m', subject='contest')],
    ],
    ids=['nothing', 'claimed', 'skipped', 'another subject'],
)
def test_a_cancellation_is_not_announced_if_members_were_told_nothing(
    records: list[DeliveryRecord],
) -> None:
    cancelled = occurrence(revision=2, cancelled=True)

    assert brief(BEFORE, cancelled, records=records, policy=NOTICES_ONLY) == []


@pytest.mark.parametrize('status', [CLAIMED, SKIPPED])
def test_a_cancellation_is_not_announced_again_if_its_return_was_not(
    status: DeliveryStatus,
) -> None:
    # Cancelled under revision 1 and back under revision 2, but the notice of
    # its return is still on its way or was refused: members still think it
    # is cancelled when it is cancelled again.
    records = [
        record('1440m'),
        record('cancelled', revision=1),
        record('reinstated', revision=2, status=status),
    ]

    assert brief(BEFORE, occurrence(revision=3, cancelled=True), records=records) == []


def test_a_cancellation_is_not_announced_again_if_its_return_was_never_tried() -> None:
    records = [record('1440m'), record('cancelled', revision=1)]

    assert brief(BEFORE, occurrence(revision=3, cancelled=True), records=records) == []


@pytest.mark.parametrize('status', [CLAIMED, SENT, SKIPPED])
def test_a_cancellation_is_announced_once_per_revision(status: DeliveryStatus) -> None:
    # Claimed: it may be on its way. Skipped: Discord refused it.
    records = [record('1440m'), record('cancelled', revision=1, status=status)]

    assert brief(BEFORE, occurrence(revision=1, cancelled=True), records=records) == []


@pytest.mark.parametrize(
    ('now', 'expected'), [(S - TICK, ['send cancelled evt-1']), (S, [])]
)
def test_a_cancellation_is_announced_only_before_the_start(
    now: datetime, expected: list[str]
) -> None:
    cancelled = occurrence(revision=1, cancelled=True)

    assert brief(now, cancelled, records=[record('1440m')]) == expected


@pytest.mark.parametrize('now', [S - DAY, S - HOUR, S - TICK, S, S + MINUTE])
def test_a_cancelled_occurrence_gets_no_reminders(now: datetime) -> None:
    policy = ReminderPolicy(offsets=(DAY, HOUR), announce_start=True)

    assert brief(now, occurrence(revision=1, cancelled=True), policy=policy) == []


def test_a_reinstatement_is_announced_if_the_cancellation_was() -> None:
    reinstated = occurrence(revision=2)
    records = [record('1440m'), record('cancelled', revision=1)]

    notice = only_notice(BEFORE, reinstated, records=records)

    assert notice == Notice(NoticeKind.REINSTATED, (reinstated,))


@pytest.mark.parametrize('status', [CLAIMED, SKIPPED])
def test_a_reinstatement_is_not_announced_if_the_cancellation_was_not(
    status: DeliveryStatus,
) -> None:
    records = [record('1440m'), record('cancelled', revision=1, status=status)]

    assert brief(BEFORE, occurrence(revision=2), records=records) == []


def test_only_a_cancellation_under_an_earlier_revision_is_answered() -> None:
    records = [record('1440m'), record('cancelled', revision=2)]

    assert brief(BEFORE, occurrence(revision=2), records=records) == []


@pytest.mark.parametrize('status', [CLAIMED, SENT, SKIPPED])
def test_a_reinstatement_is_announced_once_per_revision(
    status: DeliveryStatus,
) -> None:
    records = [
        record('1440m'),
        record('cancelled', revision=1),
        record('reinstated', revision=2, status=status),
    ]

    assert brief(BEFORE, occurrence(revision=2), records=records) == []


@pytest.mark.parametrize(
    ('now', 'expected'), [(S - TICK, ['send reinstated evt-1']), (S, [])]
)
def test_a_reinstatement_is_announced_only_before_the_start(
    now: datetime, expected: list[str]
) -> None:
    records = [record('1440m'), record('cancelled', revision=1)]

    assert (
        brief(now, occurrence(revision=2), records=records, policy=NOTICES_ONLY)
        == expected
    )


def test_a_reinstatement_at_a_new_time_gets_no_moved_notice_as_well() -> None:
    records = [
        record('1440m', start=S - 2 * HOUR),
        record('cancelled', start=S - 2 * HOUR, revision=1),
    ]

    assert brief(BEFORE, occurrence(revision=2), records=records) == [
        'send reinstated evt-1'
    ]


@pytest.mark.parametrize('status', [CLAIMED, SENT, SKIPPED])
def test_a_reinstatement_at_a_new_time_gets_no_moved_notice_later(
    status: DeliveryStatus,
) -> None:
    # Sent, it gave the new start. Claimed or refused, members still think
    # the occurrence is cancelled, and a time change would make no sense.
    records = [
        record('1440m', start=S - 2 * HOUR),
        record('cancelled', start=S - 2 * HOUR, revision=1),
        record('reinstated', revision=2, status=status),
    ]

    assert brief(BEFORE, occurrence(revision=2), records=records) == []


def test_a_reinstated_occurrence_that_moves_later_gets_a_moved_notice() -> None:
    # Back at S - 1h under revision 2, then moved to S under revision 3:
    # members know it is back, so only the move is news.
    records = [
        record('1440m', start=S - 3 * HOUR),
        record('cancelled', start=S - 3 * HOUR, revision=1),
        record('reinstated', start=S - HOUR, revision=2),
    ]

    notice = only_notice(BEFORE, occurrence(revision=3), records=records)

    assert notice.kind is NoticeKind.MOVED
    assert notice.previous_start == S - HOUR


def test_a_return_still_on_its_way_is_announced_again_after_another_change() -> None:
    # The notice for revision 2 is unconfirmed, so members last heard that it
    # was cancelled; revision 3 moved it, and they need to hear it is back.
    records = [
        record('1440m', start=S - 3 * HOUR),
        record('cancelled', start=S - 3 * HOUR, revision=1),
        record('reinstated', start=S - HOUR, revision=2, status=CLAIMED),
    ]

    assert brief(BEFORE, occurrence(revision=3), records=records) == [
        'send reinstated evt-1'
    ]


def test_a_second_cancellation_and_return_are_announced_again() -> None:
    records = [
        record('1440m'),
        record('cancelled', revision=1),
        record('reinstated', revision=2),
        record('cancelled', revision=3),
    ]

    assert brief(BEFORE, occurrence(revision=4), records=records) == [
        'send reinstated evt-1'
    ]


def test_a_move_is_announced_citing_the_start_members_were_told() -> None:
    moved = occurrence(revision=1)

    notice = only_notice(BEFORE, moved, records=[record('1440m', start=S - HOUR)])

    assert notice == Notice(NoticeKind.MOVED, (moved,), previous_start=S - HOUR)


@pytest.mark.parametrize('status', [CLAIMED, SKIPPED])
def test_a_move_is_not_announced_if_members_were_told_nothing(
    status: DeliveryStatus,
) -> None:
    records = [record('1440m', start=S - HOUR, status=status)]

    assert brief(BEFORE, occurrence(revision=1), records=records) == []


def test_a_new_revision_with_the_start_members_were_told_is_not_a_move() -> None:
    assert brief(BEFORE, occurrence(revision=1), records=[record('1440m')]) == []


@pytest.mark.parametrize('revision', [1, 2])
def test_a_move_needs_a_newer_revision(revision: int) -> None:
    records = [record('1440m', start=S - HOUR, revision=revision)]

    assert brief(BEFORE, occurrence(revision=1), records=records) == []


def test_a_record_without_a_revision_is_older_than_any() -> None:
    records = [record('1440m', start=S - HOUR, revision=None)]

    assert brief(BEFORE, occurrence(revision=0), records=records) == [
        'send moved evt-1'
    ]


def test_a_post_without_a_start_announces_no_move() -> None:
    records = [record('1440m', start=None)]

    assert (
        brief(BEFORE, occurrence(revision=1), records=records, policy=NOTICES_ONLY)
        == []
    )


@pytest.mark.parametrize('status', [CLAIMED, SENT, SKIPPED])
def test_a_move_is_announced_once_per_revision(status: DeliveryStatus) -> None:
    records = [
        record('1440m', start=S - HOUR),
        record('moved', revision=1, status=status),
    ]

    assert brief(BEFORE, occurrence(revision=1), records=records) == []


@pytest.mark.parametrize(
    ('now', 'expected'), [(S - TICK, ['send moved evt-1']), (S, [])]
)
def test_a_move_is_announced_only_before_the_start(
    now: datetime, expected: list[str]
) -> None:
    records = [record('1440m', start=S - HOUR)]

    assert (
        brief(now, occurrence(revision=1), records=records, policy=NOTICES_ONLY)
        == expected
    )


def test_a_second_move_cites_the_start_the_first_moved_notice_gave() -> None:
    records = [
        record('1440m', start=S - 2 * HOUR),
        record('moved', start=S - HOUR, revision=1),
    ]

    notice = only_notice(BEFORE, occurrence(revision=2), records=records)

    assert notice.previous_start == S - HOUR


def test_a_move_back_to_the_first_start_is_announced() -> None:
    # Members were told S + 1h last, so S is news to them, though their
    # reminder gave S.
    records = [record('1440m'), record('moved', start=S + HOUR, revision=1)]

    notice = only_notice(BEFORE, occurrence(revision=2), records=records)

    assert notice.kind is NoticeKind.MOVED
    assert notice.previous_start == S + HOUR


def test_a_move_announced_by_a_later_reminder_is_not_announced_again() -> None:
    # The 1h reminder for the new start went out, and the moved notice did
    # not: the reminder told members the new start.
    records = [
        record('1440m', start=S - HOUR),
        record('60m', start=S, revision=1),
    ]

    assert brief(S - 30 * MINUTE, occurrence(revision=1), records=records) == []


def test_a_move_by_a_week_is_announced_and_its_reminders_re_armed() -> None:
    records = [record('1440m', start=S - 7 * DAY)]

    assert brief(S - 20 * HOUR, occurrence(revision=1), records=records) == [
        'send moved evt-1',
        'send 1440m evt-1',
    ]


def test_a_postponement_within_the_reminders_offset_is_only_announced() -> None:
    # Postponed by 10 minutes after the 1h reminder: no second 1h reminder.
    records = [
        record('1440m', start=S - 10 * MINUTE),
        record('60m', start=S - 10 * MINUTE),
    ]

    assert brief(S - 50 * MINUTE, occurrence(revision=1), records=records) == [
        'send moved evt-1'
    ]
    records.append(record('moved', revision=1))
    for now in (S - 50 * MINUTE, S - 10 * MINUTE, S - TICK):
        assert brief(now, occurrence(revision=1), records=records) == []


def test_a_start_postponed_after_it_was_announced_is_announced_again() -> None:
    # Announced at S - 30m, then postponed to S: members heard it had started.
    policy = ReminderPolicy(offsets=(), announce_start=True)
    records = [record('start', start=S - 30 * MINUTE)]
    postponed = occurrence(revision=1)

    assert brief(S - 25 * MINUTE, postponed, records=records, policy=policy) == [
        'send moved evt-1'
    ]
    assert brief(S, postponed, records=records, policy=policy) == ['send start evt-1']
