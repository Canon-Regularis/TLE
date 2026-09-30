"""Tests for tle.kcpc.core.schedule."""

import random
from bisect import bisect_right
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.schedule import Every, Monthly, Schedule, Weekly

LONDON = ZoneInfo('Europe/London')
NEW_YORK = ZoneInfo('America/New_York')
SYDNEY = ZoneInfo('Australia/Sydney')
LORD_HOWE = ZoneInfo('Australia/Lord_Howe')
ST_JOHNS = ZoneInfo('America/St_Johns')
MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = range(7)
MICROSECOND = timedelta(microseconds=1)
TWO_MINUTES = timedelta(minutes=2)
SEED = 20261025


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


FRIDAY_NOON = Weekly(FRIDAY, time(12, 0), LONDON)
SUNDAY_0130 = Weekly(SUNDAY, time(1, 30), LONDON)
FIRST_OF_MONTH_NOON = Monthly(1, time(12, 0), LONDON)
EVERY_TWO_MINUTES = Every(TWO_MINUTES)
DEFAULT_ANCHOR = utc(2026, 1, 1)
PROPERTY_START = utc(2026, 1, 1)
PROPERTY_END = utc(2029, 1, 1)


def slots_after(schedule: Schedule, t: datetime, count: int) -> list[datetime]:
    """The ``count`` slots after ``t``, by repeated ``next_after``."""
    slots: list[datetime] = []
    for _ in range(count):
        t = schedule.next_after(t)
        slots.append(t)
    return slots


def slots_before(schedule: Schedule, t: datetime, count: int) -> list[datetime]:
    """The ``count`` slots before ``t``, latest first, by ``prev_at_or_before``."""
    slots: list[datetime] = []
    for _ in range(count):
        previous = schedule.prev_at_or_before(t - MICROSECOND)
        assert previous is not None
        slots.append(previous)
        t = previous
    return slots


def check_invariants(schedule: Schedule, t: datetime) -> tuple[datetime, datetime]:
    """Assert what every schedule promises about ``t``; return (prev, next)."""
    following = schedule.next_after(t)
    previous = schedule.prev_at_or_before(t)
    assert previous is not None
    assert previous <= t < following
    assert previous.tzinfo is UTC and following.tzinfo is UTC
    assert schedule.prev_at_or_before(following) == following
    # Nothing lies between them: the slot after the previous one is the next.
    assert schedule.next_after(previous) == following
    return previous, following


def enumerate_slots(
    schedule: Weekly | Monthly, start: datetime, end: datetime
) -> list[datetime]:
    """Every slot from two months before ``start`` to two months after ``end``.

    Deliberately simple and independent of the implementation: it checks every
    local date and applies the spec's rule for wall times (the first fold).
    """
    first_day = start.date() - timedelta(days=62)
    days = (first_day + timedelta(days=n) for n in range((end - start).days + 124))
    return [
        datetime.combine(day, schedule.at, tzinfo=schedule.tz).astimezone(UTC)
        for day in days
        if fires_on(schedule, day)
    ]


def fires_on(schedule: Weekly | Monthly, day: date) -> bool:
    if isinstance(schedule, Weekly):
        return day.weekday() == schedule.weekday
    return day.day == schedule.day


def random_instants(count: int, start: datetime, end: datetime) -> list[datetime]:
    """``count`` seeded random instants in [start, end), to the microsecond."""
    rng = random.Random(SEED)
    span = (end - start) // MICROSECOND
    return [start + rng.randrange(span) * MICROSECOND for _ in range(count)]


def probe_times(
    slots: list[datetime], start: datetime, end: datetime, count: int = 500
) -> list[datetime]:
    """Instants to check in [start, end).

    ``count`` random ones, plus every slot in the range and a microsecond
    either side of it.
    """
    edges = [
        slot + nudge
        for slot in slots
        if start <= slot < end
        for nudge in (-MICROSECOND, timedelta(0), MICROSECOND)
    ]
    return random_instants(count, start, end) + edges


class TestWeekly:
    def test_summer_time_slot_before_the_clocks_go_back(self) -> None:
        assert FRIDAY_NOON.next_after(utc(2026, 10, 22)) == utc(2026, 10, 23, 11)

    def test_next_after_a_slot_is_a_week_later_in_winter_time(self) -> None:
        assert FRIDAY_NOON.next_after(utc(2026, 10, 23, 11)) == utc(2026, 10, 30, 12)

    def test_slots_either_side_of_the_spring_change(self) -> None:
        slots = slots_after(FRIDAY_NOON, utc(2027, 3, 20), 2)
        assert slots == [utc(2027, 3, 26, 12), utc(2027, 4, 2, 11)]

    @pytest.mark.parametrize(
        ('previous', 'slot'),
        [
            (utc(2026, 10, 16, 11), utc(2026, 10, 23, 11)),
            (utc(2026, 10, 23, 11), utc(2026, 10, 30, 12)),
            (utc(2027, 3, 19, 12), utc(2027, 3, 26, 12)),
            (utc(2027, 3, 26, 12), utc(2027, 4, 2, 11)),
        ],
    )
    def test_exact_slot_boundaries(self, previous: datetime, slot: datetime) -> None:
        assert FRIDAY_NOON.prev_at_or_before(slot) == slot
        assert FRIDAY_NOON.prev_at_or_before(slot - MICROSECOND) == previous
        assert FRIDAY_NOON.next_after(slot - MICROSECOND) == slot
        assert FRIDAY_NOON.next_after(previous) == slot

    def test_walking_backwards_retraces_walking_forwards(self) -> None:
        forwards = slots_after(FRIDAY_NOON, utc(2026, 10, 1), 30)
        backwards = slots_before(FRIDAY_NOON, forwards[-1] + MICROSECOND, 30)
        assert backwards[::-1] == forwards

    def test_repeated_wall_time_fires_once_at_the_first_occurrence(self) -> None:
        # On 2026-10-25, 01:30 happens at 00:30 UTC (BST) and 01:30 UTC (GMT).
        slots = slots_after(SUNDAY_0130, utc(2026, 10, 17), 3)
        assert slots == [
            utc(2026, 10, 18, 0, 30),
            utc(2026, 10, 25, 0, 30),
            utc(2026, 11, 1, 1, 30),
        ]
        second_occurrence = utc(2026, 10, 25, 1, 30)
        assert SUNDAY_0130.prev_at_or_before(second_occurrence) == slots[1]

    def test_skipped_wall_time_fires_as_far_after_the_gap(self) -> None:
        # 01:30 doesn't exist on 2027-03-28; the slot is 02:30 BST (01:30 UTC).
        slots = slots_after(SUNDAY_0130, utc(2027, 3, 20), 3)
        assert slots == [
            utc(2027, 3, 21, 1, 30),
            utc(2027, 3, 28, 1, 30),
            utc(2027, 4, 4, 0, 30),
        ]

    @pytest.mark.parametrize('weekday', range(7))
    def test_slots_fall_on_the_weekday_at_the_wall_time(self, weekday: int) -> None:
        schedule = Weekly(weekday, time(9, 15), LONDON)
        for slot in slots_after(schedule, utc(2026, 10, 20), 3):
            local = slot.astimezone(LONDON)
            assert (local.weekday(), local.time()) == (weekday, time(9, 15))

    def test_whole_skipped_day(self) -> None:
        # Samoa skipped Friday 30 December 2011, going from UTC-10 to UTC+14.
        # That Friday's noon slot moved a day on, past t, so the latest slot at
        # or before t is the Friday before.
        schedule = Weekly(FRIDAY, time(12, 0), ZoneInfo('Pacific/Apia'))
        t = utc(2011, 12, 30, 16)  # Saturday 31 December, 06:00 local
        assert schedule.prev_at_or_before(t) == utc(2011, 12, 23, 22)
        assert slots_after(schedule, t, 2) == [
            utc(2011, 12, 30, 22),  # Saturday 31 December, 12:00 local
            utc(2012, 1, 5, 22),
        ]

    def test_seconds_in_the_wall_time(self) -> None:
        schedule = Weekly(FRIDAY, time(12, 0, 30), LONDON)
        assert schedule.next_after(utc(2026, 10, 22)) == utc(2026, 10, 23, 11, 0, 30)


class TestMonthly:
    def test_slots_follow_summer_and_winter_time(self) -> None:
        assert slots_after(FIRST_OF_MONTH_NOON, utc(2026, 9, 15), 4) == [
            utc(2026, 10, 1, 11),
            utc(2026, 11, 1, 12),
            utc(2026, 12, 1, 12),
            utc(2027, 1, 1, 12),
        ]

    @pytest.mark.parametrize('slot', [utc(2026, 10, 1, 11), utc(2026, 11, 1, 12)])
    def test_exact_slot_boundaries(self, slot: datetime) -> None:
        assert FIRST_OF_MONTH_NOON.prev_at_or_before(slot) == slot
        assert FIRST_OF_MONTH_NOON.next_after(slot - MICROSECOND) == slot

    def test_prev_at_or_before_goes_back_to_the_previous_month(self) -> None:
        just_before = utc(2026, 10, 1, 10, 59, 59)
        assert FIRST_OF_MONTH_NOON.prev_at_or_before(just_before) == utc(2026, 9, 1, 11)
        new_year = utc(2027, 1, 1, 11, 59)
        assert FIRST_OF_MONTH_NOON.prev_at_or_before(new_year) == utc(2026, 12, 1, 12)

    def test_repeated_and_skipped_wall_times(self) -> None:
        repeated = Monthly(25, time(1, 30), LONDON)
        assert repeated.next_after(utc(2026, 10, 1)) == utc(2026, 10, 25, 0, 30)
        skipped = Monthly(28, time(1, 30), LONDON)
        assert skipped.next_after(utc(2027, 3, 1)) == utc(2027, 3, 28, 1, 30)

    def test_day_28_fires_every_february(self) -> None:
        schedule = Monthly(28, time(12, 0), LONDON)
        assert schedule.next_after(utc(2027, 2, 1)) == utc(2027, 2, 28, 12)
        assert slots_after(schedule, utc(2028, 2, 1), 2) == [
            utc(2028, 2, 28, 12),
            utc(2028, 3, 28, 11),  # BST started on 26 March 2028
        ]

    def test_clocks_going_back_across_midnight(self) -> None:
        # St John's went from 00:01 on 1 November 2009 back to 23:01 on
        # 31 October. The midnight slot is the first 00:00 (02:30 UTC), so it
        # comes before t, the second 23:30 on 31 October, despite its later date.
        schedule = Monthly(1, time(0, 0), ST_JOHNS)
        t = datetime(2009, 10, 31, 23, 30, fold=1, tzinfo=ST_JOHNS)
        # Compare in UTC: == across zones is always False for a repeated wall
        # time (PEP 495).
        assert t.astimezone(UTC) == utc(2009, 11, 1, 3)
        assert schedule.prev_at_or_before(t) == utc(2009, 11, 1, 2, 30)
        assert schedule.next_after(t) == utc(2009, 12, 1, 3, 30)


class TestEvery:
    @pytest.mark.parametrize(
        ('t', 'previous', 'following'),
        [
            (DEFAULT_ANCHOR, DEFAULT_ANCHOR, DEFAULT_ANCHOR + TWO_MINUTES),
            (
                DEFAULT_ANCHOR + MICROSECOND,
                DEFAULT_ANCHOR,
                DEFAULT_ANCHOR + TWO_MINUTES,
            ),
            (
                DEFAULT_ANCHOR + TWO_MINUTES - MICROSECOND,
                DEFAULT_ANCHOR,
                DEFAULT_ANCHOR + TWO_MINUTES,
            ),
            (
                DEFAULT_ANCHOR - MICROSECOND,
                DEFAULT_ANCHOR - TWO_MINUTES,
                DEFAULT_ANCHOR,
            ),
            (
                DEFAULT_ANCHOR - timedelta(minutes=3),
                DEFAULT_ANCHOR - timedelta(minutes=4),
                DEFAULT_ANCHOR - TWO_MINUTES,
            ),
            (utc(2025, 6, 1, 12, 1), utc(2025, 6, 1, 12, 0), utc(2025, 6, 1, 12, 2)),
            (
                utc(2027, 6, 1, 12, 3, 30),
                utc(2027, 6, 1, 12, 2),
                utc(2027, 6, 1, 12, 4),
            ),
        ],
    )
    def test_boundaries_around_the_anchor(
        self, t: datetime, previous: datetime, following: datetime
    ) -> None:
        assert EVERY_TWO_MINUTES.prev_at_or_before(t) == previous
        assert EVERY_TWO_MINUTES.next_after(t) == following

    def test_anchor_sets_the_phase(self) -> None:
        quarter_past = Every(timedelta(hours=1), anchor=utc(2026, 1, 1, 0, 15))
        t = utc(2026, 6, 1, 10, 20)
        assert quarter_past.prev_at_or_before(t) == utc(2026, 6, 1, 10, 15)
        assert quarter_past.next_after(t) == utc(2026, 6, 1, 11, 15)

    def test_interval_is_elapsed_time_even_with_a_local_anchor(self) -> None:
        # One day after noon GMT on 27 March 2027 is 13:00 BST, not noon BST:
        # the clocks go forward in between.
        daily = Every(
            timedelta(days=1), anchor=datetime(2027, 3, 27, 12, tzinfo=LONDON)
        )
        assert daily.anchor == utc(2027, 3, 27, 12)
        assert daily.anchor.tzinfo is UTC
        assert daily.next_after(utc(2027, 3, 27, 12)) == utc(2027, 3, 28, 12)

    def test_equivalent_anchors_make_equal_schedules(self) -> None:
        kolkata = ZoneInfo('Asia/Kolkata')
        in_utc = Every(timedelta(hours=1), anchor=utc(2026, 1, 1))
        in_kolkata = Every(
            timedelta(hours=1), anchor=datetime(2026, 1, 1, 5, 30, tzinfo=kolkata)
        )
        assert in_utc == in_kolkata
        assert hash(in_utc) == hash(in_kolkata)


class TestValidation:
    @pytest.mark.parametrize('weekday', [-1, 7])
    def test_weekday_must_be_monday_to_sunday(self, weekday: int) -> None:
        with pytest.raises(ValueError, match='weekday'):
            Weekly(weekday, time(12, 0), LONDON)

    @pytest.mark.parametrize('day', [0, 29, 31])
    def test_monthly_day_must_exist_in_every_month(self, day: int) -> None:
        with pytest.raises(ValueError, match='day'):
            Monthly(day, time(12, 0), LONDON)

    @pytest.mark.parametrize('schedule_type', [Weekly, Monthly])
    def test_wall_time_must_be_naive(
        self, schedule_type: type[Weekly] | type[Monthly]
    ) -> None:
        with pytest.raises(ValueError, match='naive'):
            schedule_type(1, time(12, 0, tzinfo=UTC), LONDON)

    @pytest.mark.parametrize('schedule_type', [Weekly, Monthly])
    def test_wall_time_must_not_have_fractional_seconds(
        self, schedule_type: type[Weekly] | type[Monthly]
    ) -> None:
        with pytest.raises(ValueError, match='fractional seconds'):
            schedule_type(1, time(12, 0, 0, 500_000), LONDON)

    @pytest.mark.parametrize(
        'interval',
        [
            timedelta(0),
            timedelta(minutes=-2),
            timedelta(milliseconds=500),
            timedelta(seconds=90, milliseconds=500),
        ],
    )
    def test_interval_must_be_whole_positive_seconds(self, interval: timedelta) -> None:
        with pytest.raises(ValueError, match='interval'):
            Every(interval)

    def test_anchor_must_be_aware(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            Every(TWO_MINUTES, anchor=datetime(2026, 1, 1))

    def test_anchor_must_not_have_fractional_seconds(self) -> None:
        with pytest.raises(ValueError, match='fractional seconds'):
            Every(TWO_MINUTES, anchor=utc(2026, 1, 1, 0, 0, 0, 1))

    @pytest.mark.parametrize(
        'schedule', [FRIDAY_NOON, FIRST_OF_MONTH_NOON, EVERY_TWO_MINUTES]
    )
    def test_naive_times_are_rejected(self, schedule: Schedule) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            schedule.next_after(datetime(2026, 10, 22))
        with pytest.raises(ValueError, match='timezone-aware'):
            schedule.prev_at_or_before(datetime(2026, 10, 22))

    @pytest.mark.parametrize(
        'schedule', [FRIDAY_NOON, FIRST_OF_MONTH_NOON, EVERY_TWO_MINUTES]
    )
    def test_any_aware_time_is_accepted(self, schedule: Schedule) -> None:
        t = utc(2026, 10, 24, 23, 30)
        elsewhere = t.astimezone(ZoneInfo('Asia/Kolkata'))
        assert schedule.next_after(elsewhere) == schedule.next_after(t)
        assert schedule.prev_at_or_before(elsewhere) == schedule.prev_at_or_before(t)
        assert schedule.next_after(elsewhere).tzinfo is UTC


class TestDescribe:
    @pytest.mark.parametrize(
        ('schedule', 'expected'),
        [
            (FRIDAY_NOON, 'every Friday at 12:00 (Europe/London)'),
            (
                Weekly(MONDAY, time(9, 5, 30), NEW_YORK),
                'every Monday at 09:05:30 (America/New_York)',
            ),
            (FIRST_OF_MONTH_NOON, 'on day 1 of every month at 12:00 (Europe/London)'),
            (
                Monthly(28, time(0, 0), SYDNEY),
                'on day 28 of every month at 00:00 (Australia/Sydney)',
            ),
            (EVERY_TWO_MINUTES, 'every 2m'),
            (Every(timedelta(hours=1, minutes=30)), 'every 1h 30m'),
            (Every(timedelta(days=7)), 'every 7d'),
            # Exact, never rounded like a compact duration would be.
            (Every(timedelta(days=1, seconds=1)), 'every 1d 1s'),
        ],
    )
    def test_describe(self, schedule: Schedule, expected: str) -> None:
        assert schedule.describe() == expected

    def test_weekday_names(self) -> None:
        names = [Weekly(day, time(0), LONDON).describe().split()[1] for day in range(7)]
        assert names == [
            'Monday',
            'Tuesday',
            'Wednesday',
            'Thursday',
            'Friday',
            'Saturday',
            'Sunday',
        ]


WALL_CLOCK_SCHEDULES = [
    pytest.param(FRIDAY_NOON, id='weekly-problem'),
    pytest.param(SUNDAY_0130, id='london-sunday-0130'),
    pytest.param(Weekly(SUNDAY, time(1, 0), LONDON), id='london-sunday-0100'),
    pytest.param(Weekly(SUNDAY, time(1, 59, 59), LONDON), id='london-sunday-015959'),
    pytest.param(Weekly(MONDAY, time(0, 0), LONDON), id='london-monday-0000'),
    pytest.param(Weekly(SATURDAY, time(23, 59, 59), LONDON), id='london-saturday-late'),
    pytest.param(Weekly(SUNDAY, time(1, 30), NEW_YORK), id='new-york-sunday-0130'),
    pytest.param(Weekly(SUNDAY, time(2, 30), NEW_YORK), id='new-york-sunday-0230'),
    pytest.param(Weekly(SUNDAY, time(2, 30), SYDNEY), id='sydney-sunday-0230'),
    pytest.param(Weekly(SUNDAY, time(1, 45), LORD_HOWE), id='lord-howe-sunday-0145'),
    pytest.param(Weekly(SUNDAY, time(2, 15), LORD_HOWE), id='lord-howe-sunday-0215'),
    pytest.param(
        Weekly(SUNDAY, time(3, 0), ZoneInfo('Pacific/Chatham')), id='chatham-0300'
    ),
    pytest.param(
        Weekly(WEDNESDAY, time(18, 0), ZoneInfo('Asia/Kolkata')), id='kolkata'
    ),
    pytest.param(FIRST_OF_MONTH_NOON, id='algorithm-of-the-month'),
    pytest.param(Monthly(25, time(1, 30), LONDON), id='london-25th-0130'),
    pytest.param(Monthly(28, time(1, 30), LONDON), id='london-28th-0130'),
    pytest.param(Monthly(4, time(2, 30), SYDNEY), id='sydney-4th-0230'),
    pytest.param(Monthly(1, time(0, 0), NEW_YORK), id='new-york-1st-0000'),
]

# Clock changes that skip a whole day, or skip or repeat midnight.
HISTORIC_SCHEDULES = [
    pytest.param(
        Weekly(FRIDAY, time(12, 0), ZoneInfo('Pacific/Apia')),
        utc(2011, 9, 1),
        utc(2012, 3, 1),
        id='apia-skipped-friday',
    ),
    pytest.param(
        Weekly(SUNDAY, time(0, 0), ST_JOHNS),
        utc(2007, 1, 1),
        utc(2011, 1, 1),
        id='st-johns-sunday-0000',
    ),
    pytest.param(
        Weekly(SATURDAY, time(23, 30), ST_JOHNS),
        utc(2007, 1, 1),
        utc(2011, 1, 1),
        id='st-johns-saturday-2330',
    ),
    pytest.param(
        Monthly(1, time(0, 0), ST_JOHNS),
        utc(2007, 1, 1),
        utc(2011, 1, 1),
        id='st-johns-1st-0000',
    ),
    pytest.param(
        Weekly(THURSDAY, time(23, 30), ZoneInfo('Antarctica/Casey')),
        utc(2009, 6, 1),
        utc(2011, 6, 1),
        id='casey-three-hour-fold',
    ),
    pytest.param(
        Weekly(MONDAY, time(0, 0), ZoneInfo('Asia/Tehran')),
        utc(2020, 1, 1),
        utc(2023, 1, 1),
        id='tehran-monday-0000',
    ),
    pytest.param(
        Weekly(SATURDAY, time(23, 30), ZoneInfo('America/Sao_Paulo')),
        utc(2017, 6, 1),
        utc(2019, 6, 1),
        id='sao-paulo-saturday-2330',
    ),
    pytest.param(
        Weekly(SUNDAY, time(0, 30), ZoneInfo('America/Sao_Paulo')),
        utc(2017, 6, 1),
        utc(2019, 6, 1),
        id='sao-paulo-sunday-0030',
    ),
]

EVERY_SCHEDULES = [
    pytest.param(EVERY_TWO_MINUTES, id='reconcile'),
    pytest.param(
        Every(timedelta(seconds=7), anchor=utc(2026, 3, 29, 0, 59, 59)), id='7s'
    ),
    pytest.param(
        Every(
            timedelta(hours=1),
            anchor=datetime(2026, 1, 1, 0, 15, tzinfo=ZoneInfo('Asia/Kolkata')),
        ),
        id='hourly-kolkata-anchor',
    ),
    pytest.param(
        Every(timedelta(days=1), anchor=datetime(2027, 3, 27, 12, tzinfo=LONDON)),
        id='daily-london-anchor',
    ),
    pytest.param(
        Every(timedelta(minutes=90), anchor=utc(1999, 12, 31, 23, 59, 59)),
        id='90m-old-anchor',
    ),
    pytest.param(
        Every(timedelta(days=7, seconds=1), anchor=utc(2030, 1, 1)),
        id='weekly-plus-1s-future-anchor',
    ),
]


class TestProperties:
    @pytest.mark.parametrize('schedule', WALL_CLOCK_SCHEDULES)
    def test_wall_clock_schedules_match_a_day_by_day_enumeration(
        self, schedule: Weekly | Monthly
    ) -> None:
        slots = enumerate_slots(schedule, PROPERTY_START, PROPERTY_END)
        for t in probe_times(slots, PROPERTY_START, PROPERTY_END):
            index = bisect_right(slots, t)
            expected = (slots[index - 1], slots[index])
            assert check_invariants(schedule, t) == expected, t

    @pytest.mark.parametrize(('schedule', 'start', 'end'), HISTORIC_SCHEDULES)
    def test_historic_clock_changes_match_a_day_by_day_enumeration(
        self, schedule: Weekly | Monthly, start: datetime, end: datetime
    ) -> None:
        slots = enumerate_slots(schedule, start, end)
        for t in probe_times(slots, start, end, count=200):
            index = bisect_right(slots, t)
            expected = (slots[index - 1], slots[index])
            assert check_invariants(schedule, t) == expected, t

    @pytest.mark.parametrize('schedule', EVERY_SCHEDULES)
    def test_every_fires_at_whole_intervals_from_the_anchor(
        self, schedule: Every
    ) -> None:
        for t in random_instants(500, PROPERTY_START, PROPERTY_END):
            previous, following = check_invariants(schedule, t)
            assert (previous - schedule.anchor) % schedule.interval == timedelta(0)
            assert following - previous == schedule.interval
            for edge in (following - MICROSECOND, following, following + MICROSECOND):
                check_invariants(schedule, edge)
