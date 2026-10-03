"""Keeps each platform's stored contests in step with what its source lists.

A source (Codeforces, AtCoder, ICPC or a site on clist.by) gives a snapshot of
the contests it lists, which may or may not be every upcoming contest of its
platform (``complete``). A sync compares it with the contests stored for that
platform:

- Contests are matched by their ID on the platform. One whose fingerprint (what
  members are shown) changed is updated, and if that changes the start members
  are told, its revision goes up, so that reminders already sent for the old
  time get a notice. So it does when a contest known only by its date gets a
  start time. A start that an admin set hides the source's, so the source
  moving the contest then changes no revision. If the source moves it away
  from the admin's start, a warning says so, once.
- Only a complete snapshot can show that a contest is gone. An upcoming contest
  that complete snapshots miss is cancelled, with a new revision, once three in
  a row have missed it, the last of them at least 20 minutes after a snapshot
  last listed it. Syncing by hand, or restarting, can't hurry that. A contest
  that has started, by its source's time or by an admin's, is never missed:
  AtCoder stops listing contests once they start. One that comes back after
  being cancelled is reinstated, with another revision.
- A complete snapshot with fewer than half of the four or more upcoming
  contests that the last healthy one had (those that haven't started or been
  cancelled since) fails the health check: a glitch at the source would look
  just like that. The sync counts as failed, but what the snapshot lists is
  still applied, and the contests it misses are cancelled only once six
  snapshots in a row have missed them, the last at least 50 minutes after one
  last listed them. An incomplete snapshot (such as ICPC's, of the contests it
  was asked for) shows nothing about the contests it leaves out, so it misses
  none and is not checked.
- Contests that admins add (platform ``manual``) have no source, so syncing
  never touches them.

A contest known only by its date counts as starting when its day does, in the
club's time zone (see ``ContestRepo.day_start``).

The snapshot is fetched first, outside any transaction. Then one transaction
checks it, applies it and records how the sync went, so a sync applies
completely or not at all. Syncs of one source take turns, so a snapshot is
never applied after a newer one.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.timeutil import from_epoch, to_epoch
from tle.kcpc.features.contests.repo import (
    ContestInfo,
    ContestRepo,
    ContestStatus,
    SourceRecord,
    SourceState,
)
from tle.kcpc.features.contests.settings import MANUAL

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Patience:
    """When an upcoming contest that snapshots keep missing counts as cancelled.

    Both must hold: ``misses`` snapshots in a row have missed it, and the last
    one that listed it was at least ``missing_for`` ago. The time is what stops
    syncs in quick succession (by hand, or on restarting) from hurrying a
    cancellation.
    """

    misses: int
    missing_for: timedelta


# Complete snapshots in a row that must miss an upcoming contest before it
# counts as cancelled, if they are healthy.
MISSES_TO_CANCEL = 3
# How long that takes depends on how often the source syncs (every 5 minutes,
# every 30 or every 6 hours), so each patience is a number of syncs and a time
# that must both pass.
# A healthy snapshot: 3 syncs, the last at least 20 minutes after the contest
# was last listed.
_PATIENCE = _Patience(MISSES_TO_CANCEL, timedelta(minutes=20))
# A snapshot that fails the health check: 6 syncs and at least 50 minutes, so
# that a glitch at the source that ends sooner cancels nothing. The contests
# cog tells admins this (``_describe_report``): change it there too.
_PATIENCE_SHRUNK = _Patience(6, timedelta(minutes=50))
# The health check needs at least this many upcoming contests to judge a
# snapshot by; a platform with few can lose half of them for real.
_HEALTH_CHECK_MINIMUM = 4
# A source's failures are logged at INFO, except the one that makes this many
# in a row, at WARNING (which reaches the Discord log channel): once a streak.
_FAILURES_TO_WARN = 3


@dataclass(frozen=True)
class SourceSnapshot:
    """The contests a source lists, and whether that is all of them.

    ``complete`` means that ``contests`` has every upcoming contest of the
    source's platform, so a contest left out is gone. Otherwise it has only
    some of them, such as those of the ICPC contest codes asked for.
    """

    contests: Sequence[ContestInfo]
    complete: bool


class ContestSource(Protocol):
    """Where a sync gets a platform's contests, such as Codeforces' list."""

    @property
    def name(self) -> str:
        """Its name, which its sync state is kept under: 'codeforces', say."""
        ...

    @property
    def platform(self) -> str:
        """The platform of every contest it lists."""
        ...

    async def fetch(self) -> SourceSnapshot:
        """The contests it lists now.

        Raises ``ExternalServiceError`` if they can't be fetched or read.
        """
        ...


@dataclass(frozen=True)
class SyncReport:
    """What one sync of a source did.

    Each contest counts once at most: as reinstated, else moved (a new start
    that members are told), else updated (any other change). ``future_count``
    is the number of upcoming contests in the snapshot, and ``error`` says why
    a sync isn't ``ok``. A snapshot that failed the health check was still
    ``applied``, with more patience for cancelling, and what it changed is
    counted; one that couldn't be fetched or read wasn't, and changed nothing.
    """

    source: str
    ok: bool
    added: int = 0
    updated: int = 0
    moved: int = 0
    cancelled: int = 0
    reinstated: int = 0
    future_count: int = 0
    error: str | None = None
    applied: bool = True

    @property
    def changed(self) -> bool:
        """Whether any contest was added, updated, moved, cancelled or reinstated."""
        return any(
            (self.added, self.updated, self.moved, self.cancelled, self.reinstated)
        )


class ContestSync:
    """Syncs contest sources into a ``ContestRepo``; see the module docstring."""

    def __init__(self, db: Database, repo: ContestRepo, clock: Clock) -> None:
        self._db = db
        self._repo = repo
        self._clock = clock
        # Held from fetch to commit. Two syncs of a source could otherwise
        # apply out of order, and the one that fetched first would put back
        # what the other had just changed (moving a contest back, say).
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def sync(self, source: ContestSource) -> SyncReport:
        """Fetch the source's snapshot and bring its stored contests up to date.

        If the source can't be reached, or sends a list that can't be read,
        the report has ``ok`` False and no contest is changed. A snapshot that
        fails the health check is applied with more patience for cancelling
        (see the module docstring), and its report has ``ok`` False too.
        Either way the failed attempt is recorded in the source's state, and
        nothing is raised. Any other exception is a bug, and propagates
        without being recorded, as does a ``ValueError`` for a source of
        ``manual`` contests or for a snapshot listing another platform's.

        A sync of a source that is already syncing waits for that one to
        finish, then fetches afresh. Raises ``RuntimeError`` inside a database
        transaction: the fetch would hold the database lock until the source
        answers, and the sync's writes would commit or roll back with the
        caller's.
        """
        if self._db.in_transaction():
            raise RuntimeError(
                'Contest sources must be synced outside any database transaction'
            )
        if source.platform == MANUAL:
            raise ValueError('Contests that admins add have no source to sync')
        async with self._locks[source.name]:
            try:
                snapshot = await source.fetch()
            except ExternalServiceError as exc:
                error = str(exc)
                await self._record_failure(source.name, error, cause=exc.__cause__)
                return SyncReport(source.name, ok=False, error=error, applied=False)
            fetched = _first_of_each(source.platform, snapshot.contests)
            async with self._db.transaction():
                return await self._apply(source, fetched, complete=snapshot.complete)

    async def _apply(
        self,
        source: ContestSource,
        fetched: Mapping[str, ContestInfo],
        *,
        complete: bool,
    ) -> SyncReport:
        """Check the snapshot and apply it, in the sync's transaction."""
        now = self._now()
        future_count = sum(
            1 for info in fetched.values() if self._starts_after(info, now)
        )
        # Read here, inside the transaction, so that an admin or a sync
        # running at the same time can't change them before the writes.
        stored = await self._repo.source_records(source.platform)
        state = await self._repo.source_state(source.name)
        upcoming = {
            record.contest_id
            for record in stored
            if not record.cancelled and self._lies_ahead(record, now)
        }
        # Only a complete snapshot can miss contests, so only one is checked.
        expected = _expected_future_count(state, len(upcoming)) if complete else 0
        healthy = expected < _HEALTH_CHECK_MINIMUM or 2 * future_count >= expected
        patience = _PATIENCE if healthy else _PATIENCE_SHRUNK
        changes = _compare(
            stored, fetched, now, patience if complete else None, upcoming
        )
        await self._repo.add(changes.new, now=now)
        await self._repo.save(changes.saved)
        self._warn_of_hidden_moves(stored, fetched)
        if not healthy:
            error = (
                f'contest list shrank from {expected} to {future_count} '
                'upcoming contests'
            )
            await self._record_failure(source.name, error)
            report = changes.report(source.name, future_count, error=error)
            _log_changes(report)
            return report
        await self._repo.record_success(source.name, now=now, future_count=future_count)
        report = changes.report(source.name, future_count)
        _log_success(report, state)
        return report

    def _starts_after(self, info: ContestInfo, now: datetime) -> bool:
        """Whether the source has the contest start after ``now``."""
        if info.start is not None:
            return info.start > now
        return (
            info.start_date is not None and self._repo.day_start(info.start_date) > now
        )

    def _lies_ahead(self, record: SourceRecord, now: datetime) -> bool:
        """Whether a contest is still to start, by its source's and any admin's time."""
        override = record.override_start
        return self._starts_after(record.reported, now) and (
            override is None or override > now
        )

    def _warn_of_hidden_moves(
        self, stored: Iterable[SourceRecord], fetched: Mapping[str, ContestInfo]
    ) -> None:
        """Warn of each contest that its source moved away from an admin's start.

        Members are still told the admin's start, so this warning (which
        reaches the Discord log channel) is how admins hear that the source now
        disagrees. It comes once a move: the next sync compares with the report
        that this one saved.
        """
        for old in stored:
            info = fetched.get(old.reported.external_id)
            override = old.override_start
            if (
                info is None
                or override is None
                or (info.start, info.start_date)
                == (old.reported.start, old.reported.start_date)
                or self._agrees(info, override)
            ):
                continue
            logger.warning(
                'Contest %s (%s) moved on its site to %s, but members are still '
                'told the time an admin set, %s: change it with '
                '/kcpc contests settime %d',
                info.name,
                f'{info.platform}:{info.external_id}',
                info.start if info.start is not None else info.start_date,
                override,
                old.contest_id,
            )

    def _agrees(self, info: ContestInfo, start: datetime) -> bool:
        """Whether the source has the contest start at ``start``.

        A contest known only by its date agrees with any start on that day.
        """
        if info.start is not None:
            return info.start == start
        day = info.start_date
        return day is not None and (
            self._repo.day_start(day)
            <= start
            < self._repo.day_start(day + timedelta(days=1))
        )

    async def _record_failure(
        self, source: str, error: str, *, cause: BaseException | None = None
    ) -> None:
        """Record and log a failed attempt."""
        failures = await self._repo.record_failure(source, now=self._now(), error=error)
        detail = error if cause is None else f'{error} ({_describe(cause)})'
        logger.log(
            logging.WARNING if failures == _FAILURES_TO_WARN else logging.INFO,
            'Could not sync contest source %s (consecutive failures: %d): %s',
            source,
            failures,
            detail,
        )

    def _now(self) -> datetime:
        """The current time in whole seconds, as stored."""
        return from_epoch(to_epoch(self._clock.now()))


class _Change(Enum):
    """What a sync did to one stored contest."""

    SEEN = 'seen'  # listed again, unchanged
    UPDATED = 'updated'  # listed again, changed but at the same start
    MOVED = 'moved'  # listed again, with a new start that members are told
    REINSTATED = 'reinstated'  # listed again after being cancelled
    MISSED = 'missed'  # upcoming but not listed, not cancelled yet
    CANCELLED = 'cancelled'  # upcoming and not listed for too long


@dataclass(frozen=True)
class _Changes:
    """The writes that bring a platform's stored contests in step with a snapshot."""

    new: list[ContestInfo]
    saved: list[SourceRecord]
    counts: Counter[_Change]

    def report(
        self, source: str, future_count: int, *, error: str | None = None
    ) -> SyncReport:
        return SyncReport(
            source,
            ok=error is None,
            added=len(self.new),
            updated=self.counts[_Change.UPDATED],
            moved=self.counts[_Change.MOVED],
            cancelled=self.counts[_Change.CANCELLED],
            reinstated=self.counts[_Change.REINSTATED],
            future_count=future_count,
            error=error,
        )


def _first_of_each(
    platform: str, contests: Iterable[ContestInfo]
) -> dict[str, ContestInfo]:
    """The contests by ID, keeping the first of any that share one.

    A contest of a platform other than ``platform`` is a bug in the source,
    and raises ``ValueError``.
    """
    by_id: dict[str, ContestInfo] = {}
    for info in contests:
        if info.platform != platform:
            raise ValueError(
                f'The {platform} source listed {info.platform} contest '
                f'{info.external_id}'
            )
        by_id.setdefault(info.external_id, info)
    return by_id


def _expected_future_count(state: SourceState | None, still_upcoming: int) -> int:
    """How many upcoming contests a healthy snapshot should have, give or take.

    That is the number the last healthy one had, but no more than the stored
    contests still upcoming: contests that have started since then, or have
    been cancelled, can't be upcoming in this one. Without that cap, a platform
    whose contests all started at once, or that wasn't synced for a while,
    would fail the health check for good, and so would one that lost most of
    its contests for real, even once they were cancelled.
    """
    if state is None or state.last_future_count is None:
        return 0
    return min(state.last_future_count, still_upcoming)


def _compare(
    stored: Sequence[SourceRecord],
    fetched: Mapping[str, ContestInfo],
    now: datetime,
    patience: _Patience | None,
    upcoming: Set[int],
) -> _Changes:
    """The writes that bring ``stored`` in step with ``fetched``.

    ``upcoming`` has the IDs of the scheduled contests still to start, and
    ``patience`` says when one of them that the snapshot misses counts as
    cancelled, or is None if the snapshot is incomplete and so misses none.
    Other contests are left alone when the snapshot misses them.
    """
    saved: list[SourceRecord] = []
    counts: Counter[_Change] = Counter()
    for old in stored:
        info = fetched.get(old.reported.external_id)
        if info is not None:
            row, change = _listed_again(old, info, now)
        elif patience is not None and old.contest_id in upcoming:
            row, change = _missed(old, now, patience)
        else:
            continue
        saved.append(row)
        counts[change] += 1
    known = {record.reported.external_id for record in stored}
    new = [info for external_id, info in fetched.items() if external_id not in known]
    return _Changes(new=new, saved=saved, counts=counts)


def _listed_again(
    old: SourceRecord, info: ContestInfo, now: datetime
) -> tuple[SourceRecord, _Change]:
    """``old`` brought up to date with ``info``, its listing in the snapshot."""
    row = replace(old, reported=info, miss_count=0, last_synced=now)
    if old.cancelled:
        # One new revision, even if the start moved too: the notice that it is
        # back on shows the new time.
        reinstated = replace(
            row, status=ContestStatus.SCHEDULED, revision=old.revision + 1
        )
        return reinstated, _Change.REINSTATED
    if row.effective_start != old.effective_start:
        return replace(row, revision=old.revision + 1), _Change.MOVED
    if info.fingerprint() != old.reported.fingerprint():
        return row, _Change.UPDATED
    return row, _Change.SEEN


def _missed(
    old: SourceRecord, now: datetime, patience: _Patience
) -> tuple[SourceRecord, _Change]:
    """``old``, an upcoming scheduled contest, missed by one more snapshot.

    Once ``patience`` runs out it is cancelled, with a new revision.
    """
    misses = old.miss_count + 1
    if misses < patience.misses or now - old.last_synced < patience.missing_for:
        return replace(old, miss_count=misses), _Change.MISSED
    cancelled = replace(
        old,
        miss_count=misses,
        status=ContestStatus.CANCELLED,
        revision=old.revision + 1,
    )
    return cancelled, _Change.CANCELLED


def _log_success(report: SyncReport, previous: SourceState | None) -> None:
    if previous is not None and previous.consecutive_failures >= _FAILURES_TO_WARN:
        logger.info(
            'Contest source %s synced again after %d failed attempts',
            report.source,
            previous.consecutive_failures,
        )
    _log_changes(report)


def _log_changes(report: SyncReport) -> None:
    """Log what a sync changed: at INFO if anything, else at DEBUG."""
    logger.log(
        logging.INFO if report.changed else logging.DEBUG,
        '%s contest source %s: %d added, %d updated, %d moved, %d cancelled, '
        '%d reinstated; %d upcoming',
        'Synced' if report.ok else 'Partly synced',
        report.source,
        report.added,
        report.updated,
        report.moved,
        report.cancelled,
        report.reinstated,
        report.future_count,
    )


def _describe(error: BaseException) -> str:
    """'TypeName: message', for the logs."""
    message = str(error)
    return f'{type(error).__name__}: {message}' if message else type(error).__name__
