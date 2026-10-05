"""Keeps each Luma calendar's stored events in step with its feed.

Luma's feed has no change numbers, and it drops a cancelled event rather than
marking it, so a sync compares the events it fetched with those stored:

- Events are matched by Luma ID. One whose fingerprint (what members are
  shown) changed is updated, and if its start moved its revision goes up, so
  that reminders already sent for the old time get a notice.
- An upcoming event missing from the feed is cancelled, with a new revision,
  once three fetches in a row have missed it, the last of them at least 20
  minutes after a feed last listed it: about half an hour, syncing every ten
  minutes. Syncing by hand, or restarting, can't hurry that. One that comes
  back after that is reinstated, with another revision.
- A feed with fewer than half of the four or more upcoming events that the
  last healthy feed had (those that haven't started or been cancelled since)
  fails the health check: a glitch at Luma would look just like that. The
  sync counts as failed, but what the feed lists is still applied, and the
  events it misses are cancelled only once six fetches in a row have missed
  them, the last at least 50 minutes after a feed last listed them: about an
  hour. So a shorter glitch cancels nothing, while the club really cancelling
  most of its workshops is noticed within the hour, after which feeds pass
  the check again. With fewer than four, any feed that parses is healthy,
  even an empty one, so cancelling the club's only upcoming workshop is
  noticed like any other.
- A Luma event that several calendars list is stored once for each, and each
  new revision of it is above every revision it has on any of them. A server
  that switches calendars keeps its ledger, which knows posts by Luma ID and
  revision, so a revision reused for another start or state would pass for
  one already handled.

The feed is fetched first, outside any transaction. Then one transaction
checks it, applies it and records how the sync went, so a sync applies
completely or not at all. Syncs of one calendar take turns, so a feed is never
applied after a newer one.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.timeutil import from_epoch, to_epoch
from tle.kcpc.features.workshops.repo import (
    CalendarState,
    EventRepo,
    EventStatus,
    StoredEvent,
)
from tle.kcpc.platforms.luma import CalendarNotFound, LumaEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Patience:
    """When an upcoming event that feeds keep missing counts as cancelled.

    Both must hold: ``misses`` fetches in a row have missed it, and the last
    feed that listed it was at least ``missing_for`` ago. The time is what
    stops syncs in quick succession (by hand, or on restarting) from hurrying
    a cancellation.
    """

    misses: int
    missing_for: timedelta


# Fetches in a row that must miss an upcoming event before it counts as
# cancelled, if the feed is healthy.
MISSES_TO_CANCEL = 3
# A healthy feed: about half an hour, syncing every ten minutes.
_PATIENCE = _Patience(MISSES_TO_CANCEL, timedelta(minutes=20))
# A feed that fails the health check: about an hour, so that a glitch at Luma
# shorter than that cancels nothing.
_PATIENCE_SHRUNK = _Patience(6, timedelta(minutes=50))
# The health check needs at least this many upcoming events to judge a feed
# by; a small calendar can lose half its events for real.
_HEALTH_CHECK_MINIMUM = 4
# A calendar's failures are logged at INFO, except the one that makes this many
# in a row, at WARNING (which reaches the Discord log channel): once a streak.
_FAILURES_TO_WARN = 3


class CalendarFeed(Protocol):
    """Where a sync gets a calendar's events, such as ``LumaCalendarClient``."""

    async def fetch(self, calendar_id: str) -> Sequence[LumaEvent]:
        """The calendar's events.

        Raises ``CalendarNotFound`` if there is no such calendar, and
        ``ExternalServiceError`` if the feed can't be fetched or read.
        """
        ...


@dataclass(frozen=True)
class SyncReport:
    """What one sync of a calendar did.

    Each event counts once at most: as reinstated, else moved (a new start),
    else updated (any other change). ``future_count`` is the number of
    upcoming events in the feed, and ``error`` says why a sync isn't ``ok``.
    A feed that failed the health check was still ``applied``, with more
    patience for cancelling, and what it changed is counted; a feed that
    couldn't be fetched or read wasn't, and changed no events.
    """

    calendar_id: str
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
        """Whether any event was added, updated, moved, cancelled or reinstated."""
        return any(
            (self.added, self.updated, self.moved, self.cancelled, self.reinstated)
        )


class EventSync:
    """Syncs Luma calendars into an ``EventRepo``; see the module docstring."""

    def __init__(
        self, db: Database, repo: EventRepo, client: CalendarFeed, clock: Clock
    ) -> None:
        self._db = db
        self._repo = repo
        self._client = client
        self._clock = clock
        # Held from fetch to commit. Two syncs of a calendar could otherwise
        # apply out of order, and the one that fetched first would put back
        # what the other had just changed (moving an event back, say).
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def sync(self, calendar_id: str) -> SyncReport:
        """Fetch the calendar's feed and bring its stored events up to date.

        If Luma can't be reached, or sends a feed that can't be read, the
        report has ``ok`` False and no event is changed. A feed that fails the
        health check is applied with more patience for cancelling (see the
        module docstring), and its report has ``ok`` False too. If Luma has no
        such calendar, ``CalendarNotFound`` is raised, so that a command can
        say so. Either way the failed attempt is recorded in the calendar's
        state. Any other exception is a bug, and propagates without being
        recorded.

        A sync of a calendar that is already syncing waits for that one to
        finish, then fetches afresh. Raises ``RuntimeError`` inside a database
        transaction: the fetch would hold the database lock until Luma
        answers, and the sync's writes would commit or roll back with the
        caller's.
        """
        if self._db.in_transaction():
            raise RuntimeError(
                'Luma calendars must be synced outside any database transaction'
            )
        async with self._locks[calendar_id]:
            try:
                events = await self._client.fetch(calendar_id)
            except CalendarNotFound as exc:
                await self._record_failure(calendar_id, str(exc))
                raise
            except ExternalServiceError as exc:
                error = str(exc)
                await self._record_failure(calendar_id, error, cause=exc.__cause__)
                return SyncReport(calendar_id, ok=False, error=error, applied=False)
            async with self._db.transaction():
                return await self._apply(calendar_id, events)

    async def _apply(self, calendar_id: str, events: Iterable[LumaEvent]) -> SyncReport:
        """Check the fetched events and apply them, in the sync's transaction."""
        now = self._now()
        fetched = _first_of_each(events)
        future_count = sum(1 for event in fetched.values() if event.start > now)
        # Read here, inside the transaction, so that a sync running at the same
        # time can't change the events between this check and the writes.
        stored = await self._repo.events(calendar_id)
        state = await self._repo.calendar_state(calendar_id)
        revisions = await self._repo.highest_revisions()
        expected = _expected_future_count(state, stored, now)
        healthy = expected < _HEALTH_CHECK_MINIMUM or 2 * future_count >= expected
        patience = _PATIENCE if healthy else _PATIENCE_SHRUNK
        changes = _compare(stored, fetched, now, revisions, patience)
        await self._repo.add(
            calendar_id, changes.new, now=now, revisions=changes.new_revisions
        )
        await self._repo.save(changes.saved)
        if not healthy:
            error = f'feed shrank from {expected} to {future_count} upcoming events'
            await self._record_failure(calendar_id, error)
            report = changes.report(calendar_id, future_count, error=error)
            _log_changes(report)
            return report
        await self._repo.record_success(calendar_id, now=now, future_count=future_count)
        report = changes.report(calendar_id, future_count)
        _log_success(report, state)
        return report

    async def _record_failure(
        self, calendar_id: str, error: str, *, cause: BaseException | None = None
    ) -> None:
        """Record and log a failed attempt."""
        failures = await self._repo.record_failure(
            calendar_id, now=self._now(), error=error
        )
        detail = error if cause is None else f'{error} ({_describe(cause)})'
        logger.log(
            logging.WARNING if failures == _FAILURES_TO_WARN else logging.INFO,
            'Could not sync Luma calendar %s (consecutive failures: %d): %s',
            calendar_id,
            failures,
            detail,
        )

    def _now(self) -> datetime:
        """The current time in whole seconds, as stored."""
        return from_epoch(to_epoch(self._clock.now()))


class _Change(Enum):
    """What a sync did to one stored event."""

    SEEN = 'seen'  # listed again, unchanged
    UPDATED = 'updated'  # listed again, changed but at the same start
    MOVED = 'moved'  # listed again, with a new start
    REINSTATED = 'reinstated'  # listed again after being cancelled
    MISSED = 'missed'  # upcoming but not listed, not cancelled yet
    CANCELLED = 'cancelled'  # upcoming and not listed for too long


@dataclass(frozen=True)
class _Changes:
    """The writes that bring a calendar's stored events in step with a feed."""

    new: list[LumaEvent]
    new_revisions: dict[str, int]  # each new event's revision, by Luma ID
    saved: list[StoredEvent]
    counts: Counter[_Change]

    def report(
        self, calendar_id: str, future_count: int, *, error: str | None = None
    ) -> SyncReport:
        return SyncReport(
            calendar_id,
            ok=error is None,
            added=len(self.new),
            updated=self.counts[_Change.UPDATED],
            moved=self.counts[_Change.MOVED],
            cancelled=self.counts[_Change.CANCELLED],
            reinstated=self.counts[_Change.REINSTATED],
            future_count=future_count,
            error=error,
        )


def _first_of_each(events: Iterable[LumaEvent]) -> dict[str, LumaEvent]:
    """The events by Luma ID, keeping the first of any that share one."""
    by_id: dict[str, LumaEvent] = {}
    for event in events:
        by_id.setdefault(event.luma_id, event)
    return by_id


def _expected_future_count(
    state: CalendarState | None, stored: Sequence[StoredEvent], now: datetime
) -> int:
    """How many upcoming events a healthy feed should have, give or take.

    That is the number the last healthy feed had, but no more than the stored
    events that are still upcoming: events that have started since then, or
    have been cancelled, can't be upcoming in this feed. Without that cap, a
    calendar whose events all started at once, or that wasn't synced for a
    while, would fail the health check for good, and so would one that lost
    most of its events for real, even once they were cancelled.
    """
    if state is None or state.last_future_count is None:
        return 0
    still_upcoming = sum(
        1 for event in stored if not event.cancelled and event.start_time > now
    )
    return min(state.last_future_count, still_upcoming)


def _compare(
    stored: Sequence[StoredEvent],
    fetched: Mapping[str, LumaEvent],
    now: datetime,
    revisions: Mapping[str, int],
    patience: _Patience,
) -> _Changes:
    """The writes that bring ``stored`` in step with ``fetched``.

    ``revisions`` has the highest revision of each Luma ID on any calendar, and
    ``patience`` says when an event that the feed misses counts as cancelled.
    Events that have started, and cancelled ones, are left alone when the feed
    misses them.
    """
    saved: list[StoredEvent] = []
    counts: Counter[_Change] = Counter()
    for old in stored:
        event = fetched.get(old.luma_id)
        revision = _next_revision(revisions, old.luma_id, old.revision)
        if event is not None:
            row, change = _listed_again(old, event, now, revision)
        elif not old.cancelled and old.start_time > now:
            row, change = _missed(old, now, revision, patience)
        else:
            continue
        saved.append(row)
        counts[change] += 1
    known = {event.luma_id for event in stored}
    new = [event for luma_id, event in fetched.items() if luma_id not in known]
    new_revisions = {
        event.luma_id: _next_revision(revisions, event.luma_id) for event in new
    }
    return _Changes(new=new, new_revisions=new_revisions, saved=saved, counts=counts)


def _next_revision(
    revisions: Mapping[str, int], luma_id: str, current: int = -1
) -> int:
    """The revision for an event's next change, or a new event's first one.

    It is above ``current``, the event's own, and above every revision that
    ``revisions`` has for its Luma ID: so never one that the event had on any
    calendar, which a server that switched calendars may have been told of.
    """
    return max(current, revisions.get(luma_id, -1)) + 1


def _listed_again(
    old: StoredEvent, event: LumaEvent, now: datetime, revision: int
) -> tuple[StoredEvent, _Change]:
    """``old`` brought up to date with ``event``, its listing in the feed.

    ``revision`` is its new revision, if it is moved or reinstated.
    """
    row = replace(old, miss_count=0, last_synced=now)
    fingerprint = event.fingerprint()
    changed = fingerprint != old.fingerprint
    if changed:
        row = replace(
            row,
            name=event.name,
            start_time=event.start,
            end_time=event.end,
            url=event.url,
            location=event.location,
            fingerprint=fingerprint,
        )
    if old.cancelled:
        # One new revision, even if the start moved too: the notice that it is
        # back on shows the new time.
        reinstated = replace(row, status=EventStatus.SCHEDULED, revision=revision)
        return reinstated, _Change.REINSTATED
    if changed and event.start != old.start_time:
        return replace(row, revision=revision), _Change.MOVED
    return row, _Change.UPDATED if changed else _Change.SEEN


def _missed(
    old: StoredEvent, now: datetime, revision: int, patience: _Patience
) -> tuple[StoredEvent, _Change]:
    """``old``, an upcoming scheduled event, missed by one more feed.

    Once ``patience`` runs out it is cancelled, as ``revision``.
    """
    misses = old.miss_count + 1
    if misses < patience.misses or now - old.last_synced < patience.missing_for:
        return replace(old, miss_count=misses), _Change.MISSED
    cancelled = replace(
        old, miss_count=misses, status=EventStatus.CANCELLED, revision=revision
    )
    return cancelled, _Change.CANCELLED


def _log_success(report: SyncReport, previous: CalendarState | None) -> None:
    if previous is not None and previous.consecutive_failures >= _FAILURES_TO_WARN:
        logger.info(
            'Luma calendar %s synced again after %d failed attempts',
            report.calendar_id,
            previous.consecutive_failures,
        )
    _log_changes(report)


def _log_changes(report: SyncReport) -> None:
    """Log what a sync changed: at INFO if anything, else at DEBUG."""
    logger.log(
        logging.INFO if report.changed else logging.DEBUG,
        '%s Luma calendar %s: %d added, %d updated, %d moved, %d cancelled, '
        '%d reinstated; %d upcoming',
        'Synced' if report.ok else 'Partly synced',
        report.calendar_id,
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
