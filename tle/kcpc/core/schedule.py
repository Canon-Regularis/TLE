"""Schedules: when a recurring job is due, as pure functions of time.

A slot is one instant at which a job is due. Every method takes an aware
datetime (a naive one raises ``ValueError``) and returns aware UTC datetimes.

``Weekly`` and ``Monthly`` fire at a local wall-clock time, so their UTC slots
move with daylight saving. A wall time that a clock change repeats or skips is
resolved as ``timeutil.resolve_local`` does by default (the first occurrence,
or as far past the gap as it was into it), which keeps every schedule total and
deterministic. ``Every`` is a fixed interval of elapsed time.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.timeutil import describe_duration, ensure_utc, resolve_local

_WEEKDAY_NAMES = (
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday',
    'Sunday',
)

_ONE_SECOND = timedelta(seconds=1)

# Candidate slots start at the last scheduled date at least this many days
# before t's local date. A wall time more than two days before t's is earlier
# in UTC too, and one more than two days after is later, because no zone's UTC
# offset has ever varied by more than 25.5 hours (Pacific/Apia). So the first
# candidate is at or before t and the third (at least 5 days after t's local
# date) is after it, even across clock changes that skip or repeat midnight.
_SEARCH_MARGIN = timedelta(days=3)


class Schedule(Protocol):
    """When a job is due: an endless series of instants called slots.

    For every aware ``t``, ``n = next_after(t)`` satisfies ``n > t`` and
    ``prev_at_or_before(n) == n``, and ``p = prev_at_or_before(t)`` satisfies
    ``p <= t`` and ``next_after(p) > t``, so no slot lies between ``p`` and ``t``.
    """

    def next_after(self, t: datetime) -> datetime:
        """The first slot strictly after ``t``."""
        ...

    def prev_at_or_before(self, t: datetime) -> datetime | None:
        """The latest slot at or before ``t``, or ``None`` if there is none."""
        ...

    def describe(self) -> str:
        """For people, e.g. ``'every Friday at 12:00 (Europe/London)'``."""
        ...


class _WallClockSchedule(ABC):
    """Fires at wall-clock time ``at`` in ``tz`` on recurring local dates.

    Subclasses say which dates; this class turns them into UTC slots.
    """

    # Fields of the dataclass subclasses.
    at: time
    tz: ZoneInfo

    def next_after(self, t: datetime) -> datetime:
        return self._neighbours(t)[1]

    def prev_at_or_before(self, t: datetime) -> datetime:
        return self._neighbours(t)[0]

    def _neighbours(self, t: datetime) -> tuple[datetime, datetime]:
        """The latest slot at or before ``t``, and the first slot after it."""
        t = ensure_utc(t)
        first = self._date_on_or_before(t.astimezone(self.tz).date() - _SEARCH_MARGIN)
        second = self._date_after(first)
        third = self._date_after(second)
        slots = [self._slot_on(day) for day in (first, second, third)]
        # _SEARCH_MARGIN guarantees that both lists are non-empty.
        at_or_before = [slot for slot in slots if slot <= t]
        after = [slot for slot in slots if slot > t]
        return at_or_before[-1], after[0]

    def _slot_on(self, local_date: date) -> datetime:
        return resolve_local(datetime.combine(local_date, self.at), self.tz)

    @abstractmethod
    def _date_on_or_before(self, local_date: date) -> date:
        """The latest scheduled date that is not after ``local_date``."""

    @abstractmethod
    def _date_after(self, scheduled: date) -> date:
        """The scheduled date that follows the scheduled date ``scheduled``."""


@dataclass(frozen=True)
class Weekly(_WallClockSchedule):
    """Every week on ``weekday`` (0 = Monday to 6 = Sunday) at ``at`` in ``tz``."""

    weekday: int
    at: time
    tz: ZoneInfo

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError(
                f'weekday must be 0 (Monday) to 6 (Sunday), got {self.weekday}'
            )
        _check_wall_time(self.at)

    def describe(self) -> str:
        weekday = _WEEKDAY_NAMES[self.weekday]
        return f'every {weekday} at {_clock_text(self.at)} ({self.tz})'

    def _date_on_or_before(self, local_date: date) -> date:
        return local_date - timedelta(days=(local_date.weekday() - self.weekday) % 7)

    def _date_after(self, scheduled: date) -> date:
        return scheduled + timedelta(weeks=1)


@dataclass(frozen=True)
class Monthly(_WallClockSchedule):
    """Every month on ``day`` at ``at`` in ``tz``.

    ``day`` is limited to 1 to 28 so that every month has it.
    """

    day: int
    at: time
    tz: ZoneInfo

    def __post_init__(self) -> None:
        if not 1 <= self.day <= 28:
            raise ValueError(
                f'day must be 1 to 28 (so every month has it), got {self.day}'
            )
        _check_wall_time(self.at)

    def describe(self) -> str:
        return f'on day {self.day} of every month at {_clock_text(self.at)} ({self.tz})'

    def _date_on_or_before(self, local_date: date) -> date:
        this_month = local_date.replace(day=self.day)
        return this_month if this_month <= local_date else _add_months(this_month, -1)

    def _date_after(self, scheduled: date) -> date:
        return _add_months(scheduled, 1)


@dataclass(frozen=True)
class Every:
    """Every ``interval``: slots at ``anchor + k * interval`` for every integer ``k``.

    The anchor only sets the phase; with the default (midnight UTC) an hourly
    schedule fires on the hour. The interval is elapsed time, so daylight
    saving doesn't affect it.
    """

    interval: timedelta
    anchor: datetime = datetime(2026, 1, 1, tzinfo=UTC)

    def __post_init__(self) -> None:
        if self.interval < _ONE_SECOND or self.interval % _ONE_SECOND != timedelta(0):
            raise ValueError(
                f'interval must be a whole number of seconds, at least 1, '
                f'got {self.interval!r}'
            )
        anchor = ensure_utc(self.anchor)
        if anchor.microsecond:
            raise ValueError(
                f'anchor must not have fractional seconds, got {self.anchor!r}'
            )
        # Keep the anchor in UTC: adding to a datetime in a zone with daylight
        # saving steps in wall-clock time, not elapsed time.
        object.__setattr__(self, 'anchor', anchor)

    def next_after(self, t: datetime) -> datetime:
        return self._slot(self._index_at_or_before(t) + 1)

    def prev_at_or_before(self, t: datetime) -> datetime:
        return self._slot(self._index_at_or_before(t))

    def describe(self) -> str:
        return f'every {describe_duration(self.interval, precise=True)}'

    def _index_at_or_before(self, t: datetime) -> int:
        """The ``k`` of the latest slot at or before ``t``."""
        return (ensure_utc(t) - self.anchor) // self.interval

    def _slot(self, index: int) -> datetime:
        return self.anchor + index * self.interval


def _check_wall_time(at: time) -> None:
    if at.tzinfo is not None:
        raise ValueError(f'at must be a naive wall-clock time, got {at!r}')
    if at.microsecond:
        # Slots are stored as whole epoch seconds. A fractional slot would not
        # survive that round trip, so the scheduler would run it twice.
        raise ValueError(f'at must not have fractional seconds, got {at!r}')


def _clock_text(at: time) -> str:
    """``'12:00'``, or ``'12:00:30'`` when there are seconds."""
    return at.isoformat(timespec='seconds' if at.second else 'minutes')


def _add_months(day: date, months: int) -> date:
    """``day`` moved by whole months; its day of the month must be at most 28."""
    years, month_index = divmod(day.month - 1 + months, 12)
    return day.replace(year=day.year + years, month=month_index + 1)
