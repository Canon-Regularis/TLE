"""Contest results as stored: when they started, the contests the bot has
worked on, and the rating changes it found in them.

``contest_result_start`` holds when contest results started on this install,
``contest_result`` a row per Codeforces or AtCoder contest, by its platform
and its ID there (``external_id``, as ``contest`` and the delivery keys have
it), and ``contest_result_entry`` a row per contest and handle. An AtCoder
contest's row and entries are written as the bot takes its handles'
baselines, in the contest's last half hour, and a Codeforces contest's when
the bot reads its rating changes. ``results`` describes how the rows move from
'watching' to 'posting' to 'done'.

Handles compare case-insensitively, and times are stored as whole seconds.
Each method that writes runs in a transaction of its own, or joins the
caller's.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch

CODEFORCES = 'codeforces'
ATCODER = 'atcoder'


class ResultStatus(str, Enum):
    # AtCoder: the handles' baselines are taken, and their profiles are read
    # until their ratings change.
    WATCHING = 'watching'
    # The rating changes are in, and a server's post is still to go out.
    POSTING = 'posting'
    DONE = 'done'


class ResultOutcome(str, Enum):
    POSTED = 'posted'  # a server got a post
    NOBODY = 'nobody'  # no server had a member whose rating changed
    # Nothing to look for: the contest ended before contest results started on
    # this install, AtCoder no longer has any of the handles watched, or the
    # job wasn't running when the contest's 6 hours of reads ran out.
    MISSED = 'missed'
    EXPIRED = 'expired'  # a post still couldn't be delivered 48 hours on


@dataclass(frozen=True)
class ResultContest:
    """A contest whose results the bot works on, as it first stores it.

    ``end`` is converted to UTC in whole seconds, as it is stored.
    """

    platform: str
    external_id: str  # its ID on its platform: '2051', 'abc478'
    name: str
    url: str | None
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'end', _whole_seconds(self.end))

    @property
    def key(self) -> str:
        """``platform:external_id``, as the contest's deliveries name it."""
        return f'{self.platform}:{self.external_id}'


@dataclass(frozen=True)
class ResultRecord:
    """A row of ``contest_result``: how far the bot got with a contest."""

    platform: str
    external_id: str
    name: str
    url: str | None
    end: datetime
    status: ResultStatus
    checks: int  # AtCoder: reads of the profiles since the contest ended
    next_check: datetime | None  # AtCoder, while watching: when to read next
    found_at: datetime | None  # when the rating changes were found
    outcome: ResultOutcome | None  # once done
    updated_at: datetime

    @property
    def key(self) -> str:
        """``platform:external_id``, as the contest's deliveries name it."""
        return f'{self.platform}:{self.external_id}'


@dataclass(frozen=True)
class ResultEntry:
    """A row of ``contest_result_entry``: a handle in a contest.

    On Codeforces it is a linked handle's rating change, with its place. On
    AtCoder the old values are the handle's baseline, and the new ones are
    set once its rated matches go up. Times are converted to UTC in whole
    seconds, as they are stored.
    """

    handle: str
    # None: unrated at the baseline (AtCoder). Codeforces gives 0 instead.
    old_rating: int | None
    new_rating: int | None  # None until a change is seen
    noted_at: datetime  # when the baseline was taken, or the change read
    changed_at: datetime | None = None  # when the change was seen
    place: int | None = None  # the Codeforces rank in the contest
    old_matches: int | None = None  # AtCoder's rated matches at the baseline
    new_matches: int | None = None
    old_highest: int | None = None  # AtCoder's highest rating at the baseline

    def __post_init__(self) -> None:
        object.__setattr__(self, 'noted_at', _whole_seconds(self.noted_at))
        if self.changed_at is not None:
            object.__setattr__(self, 'changed_at', _whole_seconds(self.changed_at))

    @property
    def changed(self) -> bool:
        """Whether the handle's rating changed in the contest."""
        return self.changed_at is not None


@dataclass(frozen=True)
class ProfileReading:
    """What an AtCoder profile said when the bot read it."""

    handle: str  # in AtCoder's case
    rating: int | None  # None if never rated
    highest: int | None
    matches: int  # rated matches: 0 if never rated


@dataclass(frozen=True)
class Claimant:
    """A watched AtCoder contest that could own a rise in a handle's rated
    matches: it has ended, and its baseline of the handle has fewer.
    """

    platform: str
    external_id: str
    end: datetime
    old_matches: int  # the baseline's rated matches
    noted_at: datetime  # when the baseline was read

    @property
    def key(self) -> str:
        """``platform:external_id``, as the contest's deliveries name it."""
        return f'{self.platform}:{self.external_id}'


_SELECT_RESULTS = """
    SELECT platform, external_id, name, url, end_time, status, checks,
        next_check, found_at, outcome, updated_at
    FROM contest_result
"""
_BY_END = 'ORDER BY end_time, platform, external_id'
_SELECT_ENTRIES = """
    SELECT handle, old_rating, new_rating, place, old_matches, new_matches,
        old_highest, noted_at, changed_at
    FROM contest_result_entry
    WHERE platform = ? AND external_id = ?
    ORDER BY handle
"""
# A contest keeps its first row: one stored before is left as it is.
_INSERT_RESULT = """
    INSERT INTO contest_result (
        platform, external_id, name, url, end_time, status, next_check,
        found_at, outcome, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (platform, external_id) DO NOTHING
"""
# And a handle its first entry: an AtCoder baseline is never taken again.
_INSERT_ENTRY = """
    INSERT INTO contest_result_entry (
        platform, external_id, handle, old_rating, new_rating, place,
        old_matches, new_matches, old_highest, noted_at, changed_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (platform, external_id, handle) DO NOTHING
"""
_RECORD_CHANGE = """
    UPDATE contest_result_entry
    SET new_rating = ?, new_matches = ?, changed_at = ?
    WHERE platform = ? AND external_id = ? AND handle = ? AND changed_at IS NULL
"""
# A rated match that one AtCoder contest has claimed raises the handle's
# baseline in the others still watched that don't have it yet, so that none
# claims it again. A baseline with more rated matches than the claiming
# contest's was read after AtCoder rated that contest: it has the match, and
# keeps any rise of its own.
_REBASELINE = """
    UPDATE contest_result_entry
    SET old_rating = ?, old_matches = ?, old_highest = ?, noted_at = ?
    WHERE platform = ? AND handle = ? AND external_id != ?
        AND changed_at IS NULL
        AND old_matches <= (
            SELECT old_matches FROM contest_result_entry
            WHERE platform = ? AND external_id = ? AND handle = ?
        )
        AND external_id IN (
            SELECT external_id FROM contest_result
            WHERE platform = ? AND status = ?
        )
"""
# The watched contests that have ended by a time and have a handle unchanged
# at fewer rated matches than a reading's, by end.
_CLAIMANTS = """
    SELECT r.platform, r.external_id, r.end_time, e.old_matches, e.noted_at
    FROM contest_result r
    JOIN contest_result_entry e
        ON e.platform = r.platform AND e.external_id = r.external_id
    WHERE r.platform = ? AND r.status = ? AND r.end_time <= ?
        AND e.handle = ? AND e.changed_at IS NULL AND e.old_matches < ?
    ORDER BY r.end_time, r.external_id
"""
_CHECK_NOW = """
    UPDATE contest_result SET next_check = ?, updated_at = ?
    WHERE platform = ? AND external_id = ? AND status = ? AND next_check > ?
"""
_DROP_ENTRY = """
    DELETE FROM contest_result_entry
    WHERE platform = ? AND external_id = ? AND handle = ?
"""
_CHECKED = """
    UPDATE contest_result
    SET checks = checks + 1, status = ?, next_check = ?,
        found_at = COALESCE(found_at, ?), outcome = ?, updated_at = ?
    WHERE platform = ? AND external_id = ?
"""
_SET_STATUS = """
    UPDATE contest_result
    SET status = ?, next_check = NULL, found_at = COALESCE(found_at, ?),
        outcome = ?, updated_at = ?
    WHERE platform = ? AND external_id = ?
"""
_STARTED = 'SELECT started_at FROM contest_result_start'
_START = 'INSERT INTO contest_result_start (id, started_at) VALUES (1, ?)'
_FIRST_STORED = 'SELECT MIN(updated_at) FROM contest_result'


class ResultRepo:
    """Reads and writes ``contest_result_start``, ``contest_result`` and
    ``contest_result_entry``.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def is_empty(self) -> bool:
        """Whether no contest's results have been stored yet."""
        return await self._db.fetchval('SELECT 1 FROM contest_result LIMIT 1') is None

    async def started_at(self) -> datetime | None:
        """When contest results started on this install; None until they do."""
        started = await self._db.fetchval(_STARTED)
        return None if started is None else from_epoch(started)

    async def start(
        self, missed: Sequence[ResultContest], *, now: datetime
    ) -> tuple[datetime, int | None]:
        """Note when contest results started on this install, and return it,
        with how many of ``missed`` were stored if they start now.

        On a new install, which has stored no contest, they start ``now``,
        and ``missed`` are stored as done, missed, in the same transaction.
        Once they have started, nothing is stored and the count is None. So
        it is if contests were stored before any start was: the start is
        taken as the time the first of them was stored.
        """
        async with self._db.transaction():
            started = await self.started_at()
            if started is not None:
                return started, None
            first = await self._db.fetchval(_FIRST_STORED)
            if first is not None:
                await self._db.execute(_START, (first,))
                return from_epoch(first), None
            at = to_epoch(now)
            await self._db.execute(_START, (at,))
            added = await self.add_done(missed, ResultOutcome.MISSED, now=now)
            return from_epoch(at), added

    async def get(self, platform: str, external_id: str) -> ResultRecord | None:
        row = await self._db.fetchone(
            f'{_SELECT_RESULTS} WHERE platform = ? AND external_id = ?',
            (platform, external_id),
        )
        return None if row is None else _record(row)

    async def with_status(self, status: ResultStatus) -> list[ResultRecord]:
        """The contests in ``status``, by end."""
        rows = await self._db.fetchall(
            f'{_SELECT_RESULTS} WHERE status = ? {_BY_END}', (status.value,)
        )
        return [_record(row) for row in rows]

    async def due(self, now: datetime) -> list[ResultRecord]:
        """The watched contests whose profiles are due a read at ``now``, by end."""
        rows = await self._db.fetchall(
            f'{_SELECT_RESULTS} WHERE status = ? AND next_check <= ? {_BY_END}',
            (ResultStatus.WATCHING.value, to_epoch(now)),
        )
        return [_record(row) for row in rows]

    async def claimants(
        self, platform: str, handle: str, matches: int, *, now: datetime
    ) -> list[Claimant]:
        """The watched contests that could own a rise of the handle's rated
        matches to ``matches``, read ``now``: those that have ended, with the
        handle unchanged at fewer rated matches. By end, then by ID.
        """
        rows = await self._db.fetchall(
            _CLAIMANTS,
            (platform, ResultStatus.WATCHING.value, to_epoch(now), handle, matches),
        )
        return [
            Claimant(
                platform=row['platform'],
                external_id=row['external_id'],
                end=from_epoch(row['end_time']),
                old_matches=row['old_matches'],
                noted_at=from_epoch(row['noted_at']),
            )
            for row in rows
        ]

    async def check_now(
        self, platform: str, external_id: str, *, now: datetime
    ) -> None:
        """Have a watched contest's profiles read from ``now`` on, if they
        weren't due by then.
        """
        at = to_epoch(now)
        await self._db.execute(
            _CHECK_NOW,
            (at, at, platform, external_id, ResultStatus.WATCHING.value, at),
        )

    async def entries(self, platform: str, external_id: str) -> list[ResultEntry]:
        """The contest's entries, by handle."""
        rows = await self._db.fetchall(_SELECT_ENTRIES, (platform, external_id))
        return [_entry(row) for row in rows]

    async def add_done(
        self,
        contests: Sequence[ResultContest],
        outcome: ResultOutcome,
        *,
        now: datetime,
    ) -> int:
        """Store the contests as done with ``outcome``, without entries.

        A contest stored before is left as it is. Returns how many were added.
        """
        at = to_epoch(now)
        added = 0
        async with self._db.transaction():
            for contest in contests:
                result = await self._db.execute(
                    _INSERT_RESULT,
                    _result_params(contest, ResultStatus.DONE, outcome=outcome, at=at),
                )
                added += result.rowcount
        return added

    async def start_codeforces(
        self,
        contest: ResultContest,
        entries: Sequence[ResultEntry],
        *,
        now: datetime,
    ) -> ResultRecord:
        """Store a Codeforces contest's linked handles' rating changes, found
        ``now``, as being posted, and return the contest's row.

        If the contest was stored before, its row is returned as it is, and
        ``entries`` are dropped.
        """
        at = to_epoch(now)
        async with self._db.transaction():
            inserted = await self._db.execute(
                _INSERT_RESULT,
                _result_params(contest, ResultStatus.POSTING, found_at=at, at=at),
            )
            if inserted.rowcount:
                await self._insert_entries(contest, entries)
            return await self._existing(contest.platform, contest.external_id)

    async def save_baselines(
        self,
        contest: ResultContest,
        baselines: Sequence[ProfileReading],
        *,
        next_check: datetime,
        now: datetime,
    ) -> int:
        """Store the baselines of AtCoder handles in ``contest``, taken ``now``.

        The contest is stored as watched, with its first read of the profiles
        at ``next_check``, unless it was stored before. A handle that has a
        baseline already keeps it. Returns how many baselines were added.
        """
        at = to_epoch(now)
        entries = [
            ResultEntry(
                handle=baseline.handle,
                old_rating=baseline.rating,
                new_rating=None,
                noted_at=now,
                old_matches=baseline.matches,
                old_highest=baseline.highest,
            )
            for baseline in baselines
        ]
        async with self._db.transaction():
            await self._db.execute(
                _INSERT_RESULT,
                _result_params(
                    contest,
                    ResultStatus.WATCHING,
                    next_check=to_epoch(next_check),
                    at=at,
                ),
            )
            return await self._insert_entries(contest, entries)

    async def record_check(
        self,
        record: ResultRecord,
        changes: Sequence[ProfileReading],
        dropped: Sequence[str],
        *,
        now: datetime,
        next_check: datetime | None = None,
        found: bool = False,
        outcome: ResultOutcome | None = None,
    ) -> ResultRecord:
        """Record a read of a watched AtCoder contest's profiles, made ``now``.

        ``changes`` are the handles whose rated matches went up in this
        contest: their new rating and matches are stored, and in every other
        contest still watched whose baseline of the handle has no more
        matches than this contest's, the baseline is raised to these, so that
        only this contest claims the match.
        ``dropped`` handles lose their entries: AtCoder has no such user now.
        Then the contest is done with ``outcome`` if one is given; else being
        posted if ``found``; else still watched, with its next read at
        ``next_check``. Returns the contest's row.
        """
        at = to_epoch(now)
        if outcome is not None:
            status = ResultStatus.DONE
        elif found:
            status = ResultStatus.POSTING
        elif next_check is not None:
            status = ResultStatus.WATCHING
        else:
            raise ValueError('A contest still watched needs its next check')
        async with self._db.transaction():
            for change in changes:
                await self._db.execute(
                    _RECORD_CHANGE,
                    (
                        change.rating,
                        change.matches,
                        at,
                        record.platform,
                        record.external_id,
                        change.handle,
                    ),
                )
                await self._db.execute(
                    _REBASELINE,
                    (
                        change.rating,
                        change.matches,
                        change.highest,
                        at,
                        record.platform,
                        change.handle,
                        record.external_id,
                        record.platform,
                        record.external_id,
                        change.handle,
                        record.platform,
                        ResultStatus.WATCHING.value,
                    ),
                )
            for handle in dropped:
                await self._db.execute(
                    _DROP_ENTRY, (record.platform, record.external_id, handle)
                )
            await self._db.execute(
                _CHECKED,
                (
                    status.value,
                    None if status is not ResultStatus.WATCHING else _epoch(next_check),
                    at if found else None,
                    None if outcome is None else outcome.value,
                    at,
                    record.platform,
                    record.external_id,
                ),
            )
            return await self._existing(record.platform, record.external_id)

    async def start_posting(
        self, record: ResultRecord, *, now: datetime
    ) -> ResultRecord:
        """Mark the contest as being posted, its rating changes found ``now``."""
        return await self._set_status(record, ResultStatus.POSTING, None, now)

    async def finish(
        self, record: ResultRecord, outcome: ResultOutcome, *, now: datetime
    ) -> ResultRecord:
        """Mark the contest as done with ``outcome``."""
        return await self._set_status(record, ResultStatus.DONE, outcome, now)

    async def _set_status(
        self,
        record: ResultRecord,
        status: ResultStatus,
        outcome: ResultOutcome | None,
        now: datetime,
    ) -> ResultRecord:
        at = to_epoch(now)
        found_at = at if status is ResultStatus.POSTING else None
        async with self._db.transaction():
            await self._db.execute(
                _SET_STATUS,
                (
                    status.value,
                    found_at,
                    None if outcome is None else outcome.value,
                    at,
                    record.platform,
                    record.external_id,
                ),
            )
            return await self._existing(record.platform, record.external_id)

    async def _insert_entries(
        self, contest: ResultContest, entries: Sequence[ResultEntry]
    ) -> int:
        added = 0
        for entry in entries:
            result = await self._db.execute(
                _INSERT_ENTRY,
                (
                    contest.platform,
                    contest.external_id,
                    entry.handle,
                    entry.old_rating,
                    entry.new_rating,
                    entry.place,
                    entry.old_matches,
                    entry.new_matches,
                    entry.old_highest,
                    to_epoch(entry.noted_at),
                    _epoch(entry.changed_at),
                ),
            )
            added += result.rowcount
        return added

    async def _existing(self, platform: str, external_id: str) -> ResultRecord:
        record = await self.get(platform, external_id)
        if record is None:  # written in the caller's transaction
            raise RuntimeError(f'Contest result {platform}:{external_id} not found')
        return record


def _result_params(
    contest: ResultContest,
    status: ResultStatus,
    *,
    at: int,
    next_check: int | None = None,
    found_at: int | None = None,
    outcome: ResultOutcome | None = None,
) -> tuple[object, ...]:
    return (
        contest.platform,
        contest.external_id,
        contest.name,
        contest.url,
        to_epoch(contest.end),
        status.value,
        next_check,
        found_at,
        None if outcome is None else outcome.value,
        at,
    )


def _record(row: Row) -> ResultRecord:
    outcome = row['outcome']
    return ResultRecord(
        platform=row['platform'],
        external_id=row['external_id'],
        name=row['name'],
        url=row['url'],
        end=from_epoch(row['end_time']),
        status=ResultStatus(row['status']),
        checks=row['checks'],
        next_check=_datetime(row['next_check']),
        found_at=_datetime(row['found_at']),
        outcome=None if outcome is None else ResultOutcome(outcome),
        updated_at=from_epoch(row['updated_at']),
    )


def _entry(row: Row) -> ResultEntry:
    return ResultEntry(
        handle=row['handle'],
        old_rating=row['old_rating'],
        new_rating=row['new_rating'],
        noted_at=from_epoch(row['noted_at']),
        changed_at=_datetime(row['changed_at']),
        place=row['place'],
        old_matches=row['old_matches'],
        new_matches=row['new_matches'],
        old_highest=row['old_highest'],
    )


def _whole_seconds(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds as ``to_epoch`` floors."""
    return ensure_utc(moment).replace(microsecond=0)


def _epoch(moment: datetime | None) -> int | None:
    return None if moment is None else to_epoch(moment)


def _datetime(seconds: int | None) -> datetime | None:
    return None if seconds is None else from_epoch(seconds)
