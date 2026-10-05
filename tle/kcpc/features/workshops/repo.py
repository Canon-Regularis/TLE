"""Stored Luma events and how syncing each calendar has gone, in kcpc.db.

Events are stored per calendar, so a Luma event that two calendars list is
stored once for each, though its revisions are numbered across them all (see
the sync module). They are never deleted: an event that Luma drops is marked
cancelled. ``EventSync`` is the only writer. It calls the write methods
inside the transaction it opens to apply a feed, except that a fetch that
failed is recorded on its own.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch
from tle.kcpc.platforms.luma import LumaEvent

# The longest error text kept in calendar_state.last_error.
_MAX_ERROR_LENGTH = 500


class EventStatus(str, Enum):
    SCHEDULED = 'scheduled'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class StoredEvent:
    """A row of ``event``: one event of one Luma calendar, as last synced."""

    event_id: int
    calendar_id: str
    luma_id: str
    name: str
    start_time: datetime
    end_time: datetime | None
    url: str | None
    location: str | None
    status: EventStatus
    # Goes up when the start changes, and on cancelling and reinstating, so
    # that reminders sent for an earlier revision can be followed by a notice;
    # always above any revision the Luma ID had before, on any calendar.
    revision: int
    fingerprint: str  # LumaEvent.fingerprint() of the fields above
    miss_count: int  # syncs in a row whose feed did not list it
    first_seen: datetime
    last_synced: datetime  # the latest sync whose feed listed it

    @property
    def cancelled(self) -> bool:
        return self.status is EventStatus.CANCELLED


@dataclass(frozen=True)
class CalendarState:
    """A row of ``calendar_state``: how syncing one calendar has gone."""

    calendar_id: str
    last_attempt: datetime | None
    last_ok: datetime | None
    last_future_count: int | None  # upcoming events in the latest healthy feed
    consecutive_failures: int
    last_error: str | None  # why the latest attempt failed, if it did


_EVENT_COLUMNS = """
    event_id, calendar_id, luma_id, name, start_time, end_time, url, location,
    status, revision, fingerprint, miss_count, first_seen, last_synced
"""
_SELECT_EVENTS = f'SELECT {_EVENT_COLUMNS} FROM event WHERE calendar_id = ?'
_BY_START = 'ORDER BY start_time, luma_id'

_INSERT_EVENT = """
    INSERT INTO event (
        calendar_id, luma_id, name, start_time, end_time, url, location,
        revision, fingerprint, first_seen, last_synced
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_HIGHEST_REVISIONS = (
    'SELECT luma_id, MAX(revision) AS revision FROM event GROUP BY luma_id'
)
_UPDATE_EVENT = """
    UPDATE event SET
        name = ?, start_time = ?, end_time = ?, url = ?, location = ?,
        status = ?, revision = ?, fingerprint = ?, miss_count = ?, last_synced = ?
    WHERE event_id = ?
"""

_SELECT_STATE = """
    SELECT calendar_id, last_attempt, last_ok, last_future_count,
        consecutive_failures, last_error
    FROM calendar_state WHERE calendar_id = ?
"""
_RECORD_SUCCESS = """
    INSERT INTO calendar_state (
        calendar_id, last_attempt, last_ok, last_future_count,
        consecutive_failures, last_error
    ) VALUES (?, ?, ?, ?, 0, NULL)
    ON CONFLICT (calendar_id) DO UPDATE SET
        last_attempt = excluded.last_attempt,
        last_ok = excluded.last_ok,
        last_future_count = excluded.last_future_count,
        consecutive_failures = 0,
        last_error = NULL
"""
_RECORD_FAILURE = """
    INSERT INTO calendar_state (
        calendar_id, last_attempt, consecutive_failures, last_error
    ) VALUES (?, ?, 1, ?)
    ON CONFLICT (calendar_id) DO UPDATE SET
        last_attempt = excluded.last_attempt,
        consecutive_failures = consecutive_failures + 1,
        last_error = excluded.last_error
"""


class EventRepo:
    """Reads and writes the ``event`` and ``calendar_state`` tables.

    Times are compared in whole seconds, as they are stored, but exactly: an
    event at 12:00:00 does not start at or after 12:00:00.5.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def events(self, calendar_id: str) -> list[StoredEvent]:
        """Every stored event of the calendar, cancelled ones too, by start."""
        rows = await self._db.fetchall(f'{_SELECT_EVENTS} {_BY_START}', (calendar_id,))
        return [_stored_event(row) for row in rows]

    async def between(
        self,
        calendar_id: str,
        start: datetime,
        end: datetime,
        *,
        include_cancelled: bool = False,
    ) -> list[StoredEvent]:
        """The calendar's events that start in ``[start, end)``, by start.

        Cancelled events are left out unless ``include_cancelled``.
        """
        sql = f'{_SELECT_EVENTS} AND start_time >= ? AND start_time < ?'
        params: list[object] = [calendar_id, _ceil_epoch(start), _ceil_epoch(end)]
        if not include_cancelled:
            sql += ' AND status = ?'
            params.append(EventStatus.SCHEDULED.value)
        rows = await self._db.fetchall(f'{sql} {_BY_START}', params)
        return [_stored_event(row) for row in rows]

    async def next_from(self, calendar_id: str, when: datetime) -> StoredEvent | None:
        """The calendar's first scheduled event that starts at or after ``when``."""
        row = await self._db.fetchone(
            f'{_SELECT_EVENTS} AND status = ? AND start_time >= ? {_BY_START} LIMIT 1',
            (calendar_id, EventStatus.SCHEDULED.value, _ceil_epoch(when)),
        )
        return None if row is None else _stored_event(row)

    async def calendar_state(self, calendar_id: str) -> CalendarState | None:
        """How syncing the calendar has gone; None if it was never attempted."""
        row = await self._db.fetchone(_SELECT_STATE, (calendar_id,))
        if row is None:
            return None
        return CalendarState(
            calendar_id=row['calendar_id'],
            last_attempt=_datetime_or_none(row['last_attempt']),
            last_ok=_datetime_or_none(row['last_ok']),
            last_future_count=row['last_future_count'],
            consecutive_failures=row['consecutive_failures'],
            last_error=row['last_error'],
        )

    async def highest_revisions(self) -> dict[str, int]:
        """The highest revision that each Luma ID has, over every calendar."""
        rows = await self._db.fetchall(_HIGHEST_REVISIONS)
        return {row['luma_id']: row['revision'] for row in rows}

    async def add(
        self,
        calendar_id: str,
        events: Sequence[LumaEvent],
        *,
        now: datetime,
        revisions: Mapping[str, int] | None = None,
    ) -> None:
        """Store events new to the calendar: scheduled and seen ``now``.

        Each one's revision is what ``revisions`` gives for its Luma ID, else
        0. An event already stored for the calendar raises ``IntegrityError``.
        """
        seen = to_epoch(now)
        first_revisions = revisions or {}
        await self._db.executemany(
            _INSERT_EVENT,
            [
                (
                    calendar_id,
                    event.luma_id,
                    event.name,
                    to_epoch(event.start),
                    _epoch_or_none(event.end),
                    event.url,
                    event.location,
                    first_revisions.get(event.luma_id, 0),
                    event.fingerprint(),
                    seen,
                    seen,
                )
                for event in events
            ],
        )

    async def save(self, events: Sequence[StoredEvent]) -> None:
        """Write stored events back, by ``event_id``.

        Every column is written except the event's identity (``calendar_id``
        and ``luma_id``) and ``first_seen``, which never change.
        """
        await self._db.executemany(
            _UPDATE_EVENT,
            [
                (
                    event.name,
                    to_epoch(event.start_time),
                    _epoch_or_none(event.end_time),
                    event.url,
                    event.location,
                    event.status.value,
                    event.revision,
                    event.fingerprint,
                    event.miss_count,
                    to_epoch(event.last_synced),
                    event.event_id,
                )
                for event in events
            ],
        )

    async def record_success(
        self, calendar_id: str, *, now: datetime, future_count: int
    ) -> None:
        """Record a sync that applied a healthy feed, ending any failure streak."""
        at = to_epoch(now)
        await self._db.execute(_RECORD_SUCCESS, (calendar_id, at, at, future_count))

    async def record_failure(
        self, calendar_id: str, *, now: datetime, error: str
    ) -> int:
        """Record a failed sync attempt; returns how many have failed in a row.

        ``error`` is kept, cut to 500 characters.
        """
        async with self._db.transaction():
            await self._db.execute(
                _RECORD_FAILURE, (calendar_id, to_epoch(now), _clipped(error))
            )
            failures = await self._db.fetchval(
                'SELECT consecutive_failures FROM calendar_state WHERE calendar_id = ?',
                (calendar_id,),
            )
        return int(failures)


def _stored_event(row: Row) -> StoredEvent:
    return StoredEvent(
        event_id=row['event_id'],
        calendar_id=row['calendar_id'],
        luma_id=row['luma_id'],
        name=row['name'],
        start_time=from_epoch(row['start_time']),
        end_time=_datetime_or_none(row['end_time']),
        url=row['url'],
        location=row['location'],
        status=EventStatus(row['status']),
        revision=row['revision'],
        fingerprint=row['fingerprint'],
        miss_count=row['miss_count'],
        first_seen=from_epoch(row['first_seen']),
        last_synced=from_epoch(row['last_synced']),
    )


def _ceil_epoch(moment: datetime) -> int:
    """The first whole second at or after ``moment``, as stored.

    A start in whole seconds is at or after ``moment`` exactly when it is at or
    after this, and before ``moment`` exactly when it is before this.
    """
    floor = to_epoch(moment)
    return floor if ensure_utc(moment).microsecond == 0 else floor + 1


def _epoch_or_none(moment: datetime | None) -> int | None:
    return None if moment is None else to_epoch(moment)


def _datetime_or_none(seconds: int | None) -> datetime | None:
    return None if seconds is None else from_epoch(seconds)


def _clipped(text: str) -> str:
    """``text`` cut to fit ``calendar_state.last_error``."""
    if len(text) <= _MAX_ERROR_LENGTH:
        return text
    return text[: _MAX_ERROR_LENGTH - 1] + '…'
