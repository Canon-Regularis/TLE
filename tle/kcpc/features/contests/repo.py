"""Stored contests, the times admins set for them, and each source's sync state.

Each contest is stored once in kcpc.db, by its platform and its ID there
(``external_id``), as its source last reported it. Contests are never deleted:
one that a complete snapshot of its source stops listing is marked cancelled
(see the sync module), and so is one that an admin removes. A contest known
only by its date (an ICPC regional, say) has a ``start_date`` but no start
time. Compared with a moment, it counts as starting at midnight at the start
of that date in the club's time zone (see ``ContestRepo.day_start``).

An admin can set a contest's times, whatever its source reports. They are kept
apart, as an override that syncing never touches, and every read applies them
except ``source_records``, which gives the sync what sources reported.

``ContestSync`` writes what sources report inside the transaction it opens to
apply a snapshot, except that a failed fetch is recorded on its own. Each admin
operation runs in a transaction of its own, or joins the caller's.
"""

import hashlib
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import Enum
from typing import cast

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch
from tle.kcpc.features.contests.settings import MANUAL

# The longest error text kept in contest_source_state.last_error.
_MAX_ERROR_LENGTH = 500
_ONLY_MANUAL = 'Only contests added with /kcpc contests add can be removed.'


class ContestStatus(str, Enum):
    SCHEDULED = 'scheduled'
    CANCELLED = 'cancelled'


@dataclass(frozen=True)
class ContestInfo:
    """A contest as its source reports it, before any time an admin set.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted). A contest known only by its date has a ``start_date`` instead
    of a ``start``, and no ``end``. Otherwise ``end``, when known, is after
    ``start``. Anything else raises ``ValueError``.
    """

    platform: str
    external_id: str  # its ID on its platform
    name: str
    start: datetime | None  # None when only the date is known
    start_date: date | None  # required when start is None
    end: datetime | None
    url: str | None

    def __post_init__(self) -> None:
        # Converted here, so that contests built by hand (in tests, say)
        # compare and fingerprint exactly like those read back.
        start = _whole_seconds_or_none(self.start)
        end = _whole_seconds_or_none(self.end)
        key = f'{self.platform}:{self.external_id}'
        if start is None:
            if self.start_date is None:
                raise ValueError(f'Contest {key} needs a start or a start date')
            if end is not None:
                raise ValueError(f'Contest {key} has an end but no start')
        elif end is not None and end <= start:
            raise ValueError(f'Contest {key} must end after it starts')
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)

    def fingerprint(self) -> str:
        """A digest of what members are shown: name, start, date, end and link.

        Fields are encoded as a JSON list, so no two different contests share
        an encoding (a name ending where a link begins, say, or None and '').
        """
        encoded = json.dumps(
            [
                self.name,
                _epoch_or_none(self.start),
                _iso_or_none(self.start_date),
                _epoch_or_none(self.end),
                self.url,
            ],
            separators=(',', ':'),
        )
        # ASCII: json.dumps escapes everything else.
        return hashlib.sha1(encoded.encode('ascii'), usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class StoredContest:
    """A stored contest as members are told of it, with any times an admin set.

    ``start`` and ``end`` are an admin's when ``overridden``, else its
    source's. ``source_start`` is always its source's.
    """

    contest_id: int
    platform: str
    external_id: str
    name: str
    start: datetime | None  # None while only the date is known
    start_date: date | None  # the date its source gave, if any
    end: datetime | None
    url: str | None
    status: ContestStatus
    # Goes up when the start changes, and on cancelling and reinstating, so
    # that reminders sent for an earlier revision can be followed by a notice.
    revision: int
    miss_count: int  # complete snapshots in a row that did not list it
    first_seen: datetime
    last_synced: datetime  # the latest snapshot that listed it
    source_start: datetime | None
    overridden: bool

    @property
    def cancelled(self) -> bool:
        return self.status is ContestStatus.CANCELLED

    @property
    def time_confirmed(self) -> bool:
        """Whether its start time is known, not just its date."""
        return self.start is not None

    @property
    def key(self) -> str:
        """``platform:external_id``, unique: its reminders' subject ID."""
        return f'{self.platform}:{self.external_id}'


@dataclass(frozen=True)
class SourceRecord:
    """A stored contest as its sync sees it: as its source last reported it.

    ``ContestSync`` compares these with a new snapshot, then writes them back
    with ``ContestRepo.save``.
    """

    contest_id: int
    reported: ContestInfo  # what its source last reported
    status: ContestStatus
    revision: int
    miss_count: int
    last_synced: datetime
    override_start: datetime | None  # the start an admin set, if one did

    @property
    def cancelled(self) -> bool:
        return self.status is ContestStatus.CANCELLED

    @property
    def effective_start(self) -> datetime | None:
        """The start that members are told: an admin's, else its source's."""
        if self.override_start is None:
            return self.reported.start
        return self.override_start


@dataclass(frozen=True)
class SourceState:
    """A row of ``contest_source_state``: how syncing one source has gone."""

    source: str
    last_attempt: datetime | None
    last_ok: datetime | None
    last_future_count: int | None  # upcoming contests in the latest healthy one
    consecutive_failures: int
    last_error: str | None  # why the latest attempt failed, if it did


# Every contest, with the times members are told: an admin's if one set a
# start, else its source's. (The bot always sets a start; an override row
# without one would change nothing.)
_CONTESTS = """
    SELECT
        c.contest_id, c.platform, c.external_id, c.name,
        COALESCE(o.start_time, c.start_time) AS start_time,
        c.start_date,
        CASE WHEN o.start_time IS NULL THEN c.end_time ELSE o.end_time END
            AS end_time,
        c.url, c.status, c.revision, c.miss_count, c.first_seen, c.last_synced,
        c.start_time AS source_start,
        o.start_time IS NOT NULL AS overridden
    FROM contest AS c
    LEFT JOIN contest_override AS o
        ON o.platform = c.platform AND o.external_id = c.external_id
"""
_SELECT_CONTESTS = f'SELECT * FROM ({_CONTESTS})'
_BY_START = 'ORDER BY start_time, contest_id'

_SELECT_RECORDS = """
    SELECT
        c.contest_id, c.platform, c.external_id, c.name, c.start_time,
        c.start_date, c.end_time, c.url, c.status, c.revision, c.miss_count,
        c.last_synced, o.start_time AS override_start
    FROM contest AS c
    LEFT JOIN contest_override AS o
        ON o.platform = c.platform AND o.external_id = c.external_id
    WHERE c.platform = ?
    ORDER BY c.contest_id
"""

_INSERT_CONTEST = """
    INSERT INTO contest (
        platform, external_id, name, start_time, start_date, end_time, url,
        fingerprint, first_seen, last_synced
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
_UPDATE_CONTEST = """
    UPDATE contest SET
        name = ?, start_time = ?, start_date = ?, end_time = ?, url = ?,
        status = ?, revision = ?, fingerprint = ?, miss_count = ?, last_synced = ?
    WHERE contest_id = ?
"""
_SET_OVERRIDE = """
    INSERT INTO contest_override (
        platform, external_id, start_time, end_time, set_by, set_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (platform, external_id) DO UPDATE SET
        start_time = excluded.start_time,
        end_time = excluded.end_time,
        set_by = excluded.set_by,
        set_at = excluded.set_at
"""
_NEXT_REVISION = 'UPDATE contest SET revision = revision + 1 WHERE contest_id = ?'
_CANCEL = """
    UPDATE contest SET status = ?, revision = revision + 1, last_synced = ?
    WHERE contest_id = ?
"""

_SELECT_STATE = """
    SELECT source, last_attempt, last_ok, last_future_count,
        consecutive_failures, last_error
    FROM contest_source_state WHERE source = ?
"""
_RECORD_SUCCESS = """
    INSERT INTO contest_source_state (
        source, last_attempt, last_ok, last_future_count,
        consecutive_failures, last_error
    ) VALUES (?, ?, ?, ?, 0, NULL)
    ON CONFLICT (source) DO UPDATE SET
        last_attempt = excluded.last_attempt,
        last_ok = excluded.last_ok,
        last_future_count = excluded.last_future_count,
        consecutive_failures = 0,
        last_error = NULL
"""
_RECORD_FAILURE = """
    INSERT INTO contest_source_state (
        source, last_attempt, consecutive_failures, last_error
    ) VALUES (?, ?, 1, ?)
    ON CONFLICT (source) DO UPDATE SET
        last_attempt = excluded.last_attempt,
        consecutive_failures = consecutive_failures + 1,
        last_error = excluded.last_error
"""


class ContestRepo:
    """Reads and writes ``contest``, ``contest_override`` and their sources' state.

    ``tz`` is the club's time zone, where a contest known only by its date
    starts at midnight. Times are compared in whole seconds, as they are
    stored, but exactly: a contest at 12:00:00 does not start at or after
    12:00:00.5.
    """

    def __init__(self, db: Database, *, tz: tzinfo = UTC) -> None:
        self._db = db
        self._tz = tz

    def day_start(self, day: date) -> datetime:
        """When a contest known only by its date counts as starting.

        That is midnight at the start of ``day`` in the club's time zone, in
        UTC. As in ``timeutil.resolve_local``, a midnight that the clocks
        repeat is the first, and one that they skip lands after the gap: the
        first moment of the day either way.
        """
        return datetime.combine(day, time(), tzinfo=self._tz).astimezone(UTC)

    async def upcoming(
        self, now: datetime, *, platforms: Collection[str], limit: int
    ) -> list[StoredContest]:
        """The first ``limit`` scheduled contests of ``platforms`` from ``now``.

        Those are the contests that start at or after ``now``, or that have
        started but end after it. They come by start, a contest known only by
        its date where its day starts (see ``day_start``), so it is upcoming
        until then.
        """
        names = _platform_list(platforms)
        if not names or limit < 1:
            return []
        sql = (
            f'{_SELECT_CONTESTS} WHERE status = ? AND platform IN ({_marks(names)}) '
            'AND (start_time >= ? OR end_time > ? '
            'OR (start_time IS NULL AND start_date >= ?))'
        )
        params = [
            ContestStatus.SCHEDULED.value,
            *names,
            _ceil_epoch(now),
            to_epoch(now),
            self._first_day_from(now).isoformat(),
        ]
        contests = [
            _stored_contest(row) for row in await self._db.fetchall(sql, params)
        ]
        return sorted(contests, key=self._start_order)[:limit]

    async def live(
        self, now: datetime, *, platforms: Collection[str]
    ) -> list[StoredContest]:
        """The scheduled contests of ``platforms`` running at ``now``, by start.

        Those are the ones with a start time at or before ``now`` and an end
        after it.
        """
        names = _platform_list(platforms)
        if not names:
            return []
        at = to_epoch(now)
        rows = await self._db.fetchall(
            f'{_SELECT_CONTESTS} WHERE status = ? AND platform IN ({_marks(names)}) '
            f'AND start_time <= ? AND end_time > ? {_BY_START}',
            [ContestStatus.SCHEDULED.value, *names, at, at],
        )
        return [_stored_contest(row) for row in rows]

    async def between(
        self,
        start: datetime,
        end: datetime,
        *,
        platforms: Collection[str],
        include_cancelled: bool = False,
    ) -> list[StoredContest]:
        """The contests of ``platforms`` with a start time in ``[start, end)``.

        They come by start. Contests known only by their date are left out,
        and so are cancelled ones unless ``include_cancelled``.
        """
        names = _platform_list(platforms)
        if not names:
            return []
        sql = (
            f'{_SELECT_CONTESTS} WHERE platform IN ({_marks(names)}) '
            'AND start_time >= ? AND start_time < ?'
        )
        params: list[object] = [*names, _ceil_epoch(start), _ceil_epoch(end)]
        if not include_cancelled:
            sql += ' AND status = ?'
            params.append(ContestStatus.SCHEDULED.value)
        rows = await self._db.fetchall(f'{sql} {_BY_START}', params)
        return [_stored_contest(row) for row in rows]

    async def by_id(self, contest_id: int) -> StoredContest | None:
        row = await self._db.fetchone(
            f'{_SELECT_CONTESTS} WHERE contest_id = ?', (contest_id,)
        )
        return None if row is None else _stored_contest(row)

    async def source_state(self, source: str) -> SourceState | None:
        """How syncing the source has gone; None if it was never attempted."""
        row = await self._db.fetchone(_SELECT_STATE, (source,))
        if row is None:
            return None
        return SourceState(
            source=row['source'],
            last_attempt=_datetime_or_none(row['last_attempt']),
            last_ok=_datetime_or_none(row['last_ok']),
            last_future_count=row['last_future_count'],
            consecutive_failures=row['consecutive_failures'],
            last_error=row['last_error'],
        )

    async def source_records(self, platform: str) -> list[SourceRecord]:
        """Every stored contest of the platform, cancelled ones too, by ID."""
        rows = await self._db.fetchall(_SELECT_RECORDS, (platform,))
        return [_source_record(row) for row in rows]

    async def add(self, contests: Sequence[ContestInfo], *, now: datetime) -> None:
        """Store contests new to their platform: scheduled, and seen ``now``.

        A contest already stored raises ``IntegrityError``.
        """
        seen = to_epoch(now)
        await self._db.executemany(
            _INSERT_CONTEST, [_insert_params(info, seen) for info in contests]
        )

    async def save(self, records: Sequence[SourceRecord]) -> None:
        """Write records back, by ``contest_id``.

        Every column is written except the contest's identity (``platform``
        and ``external_id``) and ``first_seen``, which never change.
        """
        await self._db.executemany(
            _UPDATE_CONTEST,
            [
                (
                    record.reported.name,
                    _epoch_or_none(record.reported.start),
                    _iso_or_none(record.reported.start_date),
                    _epoch_or_none(record.reported.end),
                    record.reported.url,
                    record.status.value,
                    record.revision,
                    record.reported.fingerprint(),
                    record.miss_count,
                    to_epoch(record.last_synced),
                    record.contest_id,
                )
                for record in records
            ],
        )

    async def record_success(
        self, source: str, *, now: datetime, future_count: int
    ) -> None:
        """Record a sync that applied a healthy snapshot, ending any failure streak."""
        at = to_epoch(now)
        await self._db.execute(_RECORD_SUCCESS, (source, at, at, future_count))

    async def record_failure(self, source: str, *, now: datetime, error: str) -> int:
        """Record a failed sync attempt; returns how many have failed in a row.

        ``error`` is kept, cut to 500 characters.
        """
        async with self._db.transaction():
            await self._db.execute(
                _RECORD_FAILURE, (source, to_epoch(now), _clipped(error))
            )
            failures = await self._db.fetchval(
                'SELECT consecutive_failures FROM contest_source_state '
                'WHERE source = ?',
                (source,),
            )
        return int(failures)

    async def add_manual(
        self,
        name: str,
        start: datetime,
        end: datetime,
        url: str | None,
        *,
        now: datetime,
    ) -> StoredContest:
        """Store a contest that an admin added, and return it.

        Its platform is ``manual``, and its ``external_id`` is its own
        ``contest_id``, as text. ``end`` must be after ``start``, else
        ``ValueError``.
        """
        # Checked and converted like a source's report. The ID comes next.
        info = ContestInfo(MANUAL, '', name, start, None, end, url)
        async with self._db.transaction():
            inserted = await self._db.execute(
                _INSERT_CONTEST, _insert_params(info, to_epoch(now))
            )
            contest_id = inserted.lastrowid
            if contest_id is None:  # an INSERT that adds a row always has one
                raise RuntimeError('SQLite gave the new contest no ID')
            await self._db.execute(
                'UPDATE contest SET external_id = ? WHERE contest_id = ?',
                (str(contest_id), contest_id),
            )
            return await self._existing(contest_id)

    async def set_time(
        self,
        contest_id: int,
        start: datetime,
        end: datetime | None,
        *,
        by: str,
        now: datetime,
    ) -> StoredContest:
        """Set a contest's times, whatever its source reports, and return it.

        This writes or replaces the contest's override, which syncing never
        touches, recording ``by`` as who set it. It works for a contest of any
        platform. ``end`` None keeps the contest's duration if it has one, and
        leaves its end unknown otherwise. If the start that members are told
        changes, the contest gets a new revision, so that members reminded of
        the old start hear of the new one.

        Raises ``KcpcUserError`` if there is no such contest or if it is
        cancelled, and ``ValueError`` if ``end`` is not after ``start``.
        """
        start = _whole_seconds(start)
        async with self._db.transaction():
            contest = await self._existing(contest_id)
            if contest.cancelled:
                # Members are reminded of no cancelled contest, so a time
                # set for it would change nothing they see.
                raise KcpcUserError(
                    'That contest is cancelled, so it gets no reminders. To '
                    'remind members of it, add it as a club contest with '
                    '/kcpc contests add.'
                )
            if end is None and contest.start is not None and contest.end is not None:
                end = start + (contest.end - contest.start)
            end = _whole_seconds_or_none(end)
            if end is not None and end <= start:
                raise ValueError(f'Contest {contest.key} must end after it starts')
            await self._db.execute(
                _SET_OVERRIDE,
                (
                    contest.platform,
                    contest.external_id,
                    to_epoch(start),
                    _epoch_or_none(end),
                    by,
                    to_epoch(now),
                ),
            )
            if start != contest.start:
                await self._db.execute(_NEXT_REVISION, (contest_id,))
            return await self._existing(contest_id)

    async def cancel_manual(self, contest_id: int, *, now: datetime) -> StoredContest:
        """Cancel a contest that an admin added, and return it.

        It gets a new revision, so that members reminded of it get a notice.
        Raises ``KcpcUserError`` if there is no such contest, if it came from
        a site rather than an admin, or if it was already cancelled.
        """
        async with self._db.transaction():
            contest = await self._existing(contest_id)
            if contest.platform != MANUAL:
                raise KcpcUserError(_ONLY_MANUAL)
            if contest.cancelled:
                raise KcpcUserError('That contest has already been removed.')
            # An admin is a manual contest's source, so this is its last sync.
            await self._db.execute(
                _CANCEL, (ContestStatus.CANCELLED.value, to_epoch(now), contest_id)
            )
            return await self._existing(contest_id)

    async def _existing(self, contest_id: int) -> StoredContest:
        """The contest, else ``KcpcUserError``."""
        contest = await self.by_id(contest_id)
        if contest is None:
            raise KcpcUserError(f'There is no contest with ID {contest_id}.')
        return contest

    def _first_day_from(self, now: datetime) -> date:
        """The first date whose day (see ``day_start``) starts at or after ``now``."""
        today = ensure_utc(now).astimezone(self._tz).date()
        return today if self.day_start(today) >= now else today + timedelta(days=1)

    def _start_order(self, contest: StoredContest) -> tuple[datetime, int]:
        """Where a contest comes by start: one known only by its date, at its day's."""
        if contest.start is not None:
            return contest.start, contest.contest_id
        # The table holds a date wherever it has no start.
        return self.day_start(cast(date, contest.start_date)), contest.contest_id


def _stored_contest(row: Row) -> StoredContest:
    return StoredContest(
        contest_id=row['contest_id'],
        platform=row['platform'],
        external_id=row['external_id'],
        name=row['name'],
        start=_datetime_or_none(row['start_time']),
        start_date=_date_or_none(row['start_date']),
        end=_datetime_or_none(row['end_time']),
        url=row['url'],
        status=ContestStatus(row['status']),
        revision=row['revision'],
        miss_count=row['miss_count'],
        first_seen=from_epoch(row['first_seen']),
        last_synced=from_epoch(row['last_synced']),
        source_start=_datetime_or_none(row['source_start']),
        overridden=bool(row['overridden']),
    )


def _source_record(row: Row) -> SourceRecord:
    reported = ContestInfo(
        platform=row['platform'],
        external_id=row['external_id'],
        name=row['name'],
        start=_datetime_or_none(row['start_time']),
        start_date=_date_or_none(row['start_date']),
        end=_datetime_or_none(row['end_time']),
        url=row['url'],
    )
    return SourceRecord(
        contest_id=row['contest_id'],
        reported=reported,
        status=ContestStatus(row['status']),
        revision=row['revision'],
        miss_count=row['miss_count'],
        last_synced=from_epoch(row['last_synced']),
        override_start=_datetime_or_none(row['override_start']),
    )


def _insert_params(info: ContestInfo, seen: int) -> tuple[object, ...]:
    return (
        info.platform,
        info.external_id,
        info.name,
        _epoch_or_none(info.start),
        _iso_or_none(info.start_date),
        _epoch_or_none(info.end),
        info.url,
        info.fingerprint(),
        seen,
        seen,
    )


def _platform_list(platforms: Collection[str]) -> list[str]:
    # One name is a collection of strings too, its letters, which would
    # quietly match nothing.
    if isinstance(platforms, str):
        raise TypeError(f'Expected a collection of platforms, got {platforms!r}')
    return list(platforms)


def _marks(values: Sequence[object]) -> str:
    """One SQL parameter mark per value, for ``IN (...)``."""
    return ', '.join('?' * len(values))


def _ceil_epoch(moment: datetime) -> int:
    """The first whole second at or after ``moment``, as stored.

    A start in whole seconds is at or after ``moment`` exactly when it is at or
    after this, and before ``moment`` exactly when it is before this.
    """
    floor = to_epoch(moment)
    return floor if ensure_utc(moment).microsecond == 0 else floor + 1


def _whole_seconds(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds as ``to_epoch`` floors."""
    return ensure_utc(moment).replace(microsecond=0)


def _whole_seconds_or_none(moment: datetime | None) -> datetime | None:
    return None if moment is None else _whole_seconds(moment)


def _epoch_or_none(moment: datetime | None) -> int | None:
    return None if moment is None else to_epoch(moment)


def _datetime_or_none(seconds: int | None) -> datetime | None:
    return None if seconds is None else from_epoch(seconds)


def _iso_or_none(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _date_or_none(text: str | None) -> date | None:
    return None if text is None else date.fromisoformat(text)


def _clipped(text: str) -> str:
    """``text`` cut to fit ``contest_source_state.last_error``."""
    if len(text) <= _MAX_ERROR_LENGTH:
        return text
    return text[: _MAX_ERROR_LENGTH - 1] + '…'
