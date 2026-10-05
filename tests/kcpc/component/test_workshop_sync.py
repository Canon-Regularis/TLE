"""Component tests for the workshops feature's EventSync.

The feed is a fake; the database and the repo are real, and time is a
FakeClock, so each test reads back exactly what each sync stored.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.features.workshops.repo import (
    CalendarState,
    EventRepo,
    EventStatus,
    StoredEvent,
)
from tle.kcpc.features.workshops.sync import EventSync, SyncReport
from tle.kcpc.platforms.luma import CalendarNotFound, LumaEvent, parse_calendar

CALENDAR = 'cal-ExampleClub001'
OTHER_CALENDAR = 'cal-OtherClub0002'
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # where the clock fixture starts
SYNC_INTERVAL = timedelta(minutes=10)
SYNC_LOGGER = 'tle.kcpc.features.workshops.sync'
UNREACHABLE = 'Luma is not responding right now. Please try again later.'


def at(**offset: float) -> datetime:
    """``NOW`` moved by ``offset``, e.g. ``at(days=7)``."""
    return NOW + timedelta(**offset)


def luma_event(luma_id: str, start: datetime, **changes: Any) -> LumaEvent:
    event = LumaEvent(
        luma_id=luma_id,
        name=f'Workshop {luma_id}',
        start=start,
        end=start + timedelta(hours=2),
        url=f'https://luma.com/{luma_id}',
        location='Room 101',
    )
    return replace(event, **changes)


def upcoming(count: int, *, first_day: int = 1) -> list[LumaEvent]:
    """``count`` events a day apart, the first ``first_day`` days from now."""
    return [
        luma_event(f'evt-{day}', at(days=day))
        for day in range(first_day, first_day + count)
    ]


class FakeFeed:
    """A ``CalendarFeed`` serving whatever events each calendar is given.

    A calendar that was never served is not found. After ``fail``, fetching
    that calendar raises the error, until it is served again.
    """

    def __init__(self) -> None:
        self.fetches: list[str] = []
        self._events: dict[str, list[LumaEvent]] = {}
        self._errors: dict[str, Exception] = {}

    def serve(self, calendar_id: str, events: Sequence[LumaEvent]) -> None:
        self._events[calendar_id] = list(events)
        self._errors.pop(calendar_id, None)

    def fail(self, calendar_id: str, error: Exception) -> None:
        self._errors[calendar_id] = error

    async def fetch(self, calendar_id: str) -> list[LumaEvent]:
        self.fetches.append(calendar_id)
        error = self._errors.get(calendar_id)
        if error is not None:
            raise error
        if calendar_id not in self._events:
            raise CalendarNotFound(calendar_id)
        return list(self._events[calendar_id])


class SlowFeed(FakeFeed):
    """A ``FakeFeed`` whose first fetch is slow.

    It takes the events served at that moment, but returns them only once
    ``release`` is set.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def fetch(self, calendar_id: str) -> list[LumaEvent]:
        events = await super().fetch(calendar_id)
        if len(self.fetches) == 1:
            await self.release.wait()
        return events


@pytest.fixture
def feed() -> FakeFeed:
    return FakeFeed()


@pytest.fixture
def repo(db: Database) -> EventRepo:
    return EventRepo(db)


@pytest.fixture
def sync(db: Database, repo: EventRepo, feed: FakeFeed, clock: FakeClock) -> EventSync:
    return EventSync(db, repo, feed, clock)


class Syncer:
    """Runs syncs the way the job does, ten minutes apart by default."""

    def __init__(self, sync: EventSync, feed: FakeFeed, clock: FakeClock) -> None:
        self.sync = sync
        self.feed = feed
        self.clock = clock

    async def now(
        self, events: Sequence[LumaEvent] | None = None, calendar_id: str = CALENDAR
    ) -> SyncReport:
        """Sync at the current time, serving ``events`` first if given."""
        if events is not None:
            self.feed.serve(calendar_id, events)
        return await self.sync.sync(calendar_id)

    async def later(
        self,
        events: Sequence[LumaEvent] | None = None,
        calendar_id: str = CALENDAR,
        *,
        after: timedelta = SYNC_INTERVAL,
    ) -> SyncReport:
        """Sync ``after`` from now, serving ``events`` first if given."""
        await self.clock.advance(after)
        return await self.now(events, calendar_id)


@pytest.fixture
def syncer(sync: EventSync, feed: FakeFeed, clock: FakeClock) -> Syncer:
    return Syncer(sync, feed, clock)


async def stored(
    repo: EventRepo, luma_id: str, calendar_id: str = CALENDAR
) -> StoredEvent:
    [event] = [e for e in await repo.events(calendar_id) if e.luma_id == luma_id]
    return event


async def state_of(repo: EventRepo, calendar_id: str = CALENDAR) -> CalendarState:
    state = await repo.calendar_state(calendar_id)
    assert state is not None
    return state


class TestSyncReport:
    @pytest.mark.parametrize(
        'report',
        [
            SyncReport(CALENDAR, ok=True, added=1),
            SyncReport(CALENDAR, ok=True, updated=1),
            SyncReport(CALENDAR, ok=True, moved=1),
            SyncReport(CALENDAR, ok=True, cancelled=1),
            SyncReport(CALENDAR, ok=True, reinstated=1),
        ],
        ids=['added', 'updated', 'moved', 'cancelled', 'reinstated'],
    )
    def test_changed_by_any_change(self, report: SyncReport) -> None:
        assert report.changed

    def test_not_changed_otherwise(self) -> None:
        assert not SyncReport(CALENDAR, ok=True, future_count=5).changed
        assert not SyncReport(CALENDAR, ok=False, error='feed shrank').changed


class TestAdding:
    async def test_a_first_sync_stores_every_event(
        self, syncer: Syncer, repo: EventRepo, clock: FakeClock
    ) -> None:
        past = luma_event('evt-past', at(days=-7))
        coming = luma_event('evt-coming', at(days=7))
        await clock.advance(0.5)  # times are stored in whole seconds

        report = await syncer.now([past, coming])

        assert report == SyncReport(CALENDAR, ok=True, added=2, future_count=1)
        for event in (past, coming):
            row = await stored(repo, event.luma_id)
            assert (row.name, row.start_time, row.end_time) == (
                event.name,
                event.start,
                event.end,
            )
            assert (row.url, row.location) == (event.url, event.location)
            assert (row.status, row.revision, row.miss_count) == (
                EventStatus.SCHEDULED,
                0,
                0,
            )
            assert row.fingerprint == event.fingerprint()
            assert row.first_seen == row.last_synced == NOW
        assert await state_of(repo) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=NOW,
            last_ok=NOW,
            last_future_count=1,
            consecutive_failures=0,
            last_error=None,
        )

    async def test_an_empty_feed_is_fine_at_first(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        report = await syncer.now([])

        assert report == SyncReport(CALENDAR, ok=True)
        assert (await state_of(repo)).last_future_count == 0

    async def test_only_events_after_now_are_upcoming(self, syncer: Syncer) -> None:
        events = [
            luma_event('evt-before', at(seconds=-1)),
            luma_event('evt-now', NOW),
            luma_event('evt-after', at(seconds=1)),
        ]

        assert (await syncer.now(events)).future_count == 1

    async def test_events_new_to_a_later_feed_are_added_then(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        first = luma_event('evt-first', at(days=3))
        await syncer.now([first])

        report = await syncer.later([first, luma_event('evt-new', at(days=4))])

        assert (report.added, report.changed) == (1, True)
        assert (await stored(repo, 'evt-first')).first_seen == NOW
        new = await stored(repo, 'evt-new')
        assert new.first_seen == new.last_synced == at(minutes=10)


class TestListedAgain:
    async def test_an_unchanged_event_is_only_marked_as_seen(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await syncer.now([event])
        before = await stored(repo, 'evt-a')

        report = await syncer.later([event])

        assert report == SyncReport(CALENDAR, ok=True, future_count=1)
        assert await stored(repo, 'evt-a') == replace(
            before, last_synced=at(minutes=10)
        )

    @pytest.mark.parametrize(
        'changes',
        [
            {'name': 'Intro to DP, part 2'},
            {'end': at(days=3, hours=3)},
            {'end': None},
            {'url': 'https://luma.com/new-link'},
            {'location': 'Room 102'},
            {'location': None},
        ],
        ids=['name', 'end', 'no-end', 'url', 'location', 'no-location'],
    )
    async def test_a_change_at_the_same_start_is_an_update(
        self, syncer: Syncer, repo: EventRepo, changes: dict[str, Any]
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await syncer.now([event])
        changed = replace(event, **changes)

        report = await syncer.later([changed])

        assert (report.updated, report.moved, report.changed) == (1, 0, True)
        row = await stored(repo, 'evt-a')
        assert (row.name, row.end_time, row.url, row.location) == (
            changed.name,
            changed.end,
            changed.url,
            changed.location,
        )
        assert (row.fingerprint, row.revision) == (changed.fingerprint(), 0)

    @pytest.mark.parametrize(
        'delay',
        [timedelta(seconds=1), timedelta(minutes=10), timedelta(days=7)],
        ids=['a-second', 'ten-minutes', 'a-week'],
    )
    async def test_a_new_start_is_a_move_with_a_new_revision(
        self, syncer: Syncer, repo: EventRepo, delay: timedelta
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await syncer.now([event])
        moved = replace(event, start=event.start + delay, end=None)

        report = await syncer.later([moved])

        assert (report.moved, report.updated, report.changed) == (1, 0, True)
        row = await stored(repo, 'evt-a')
        assert (row.start_time, row.end_time, row.revision) == (moved.start, None, 1)

    async def test_each_move_is_a_new_revision(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await syncer.now([event])
        await syncer.later([replace(event, start=at(days=4), end=None)])
        await syncer.later([replace(event, start=at(days=5), end=None)])
        await syncer.later([replace(event, start=at(days=5), end=None)])

        assert (await stored(repo, 'evt-a')).revision == 2

    async def test_a_listing_resets_the_misses(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event, other = luma_event('evt-a', at(days=3)), luma_event('evt-b', at(days=4))
        await syncer.now([event, other])
        await syncer.later([other])
        await syncer.later([other])
        assert (await stored(repo, 'evt-a')).miss_count == 2

        report = await syncer.later([event, other])

        assert not report.changed
        row = await stored(repo, 'evt-a')
        assert (row.miss_count, row.revision, row.status) == (
            0,
            0,
            EventStatus.SCHEDULED,
        )
        assert row.last_synced == at(minutes=30)


class TestCancelling:
    async def test_an_upcoming_event_is_cancelled_when_three_feeds_miss_it(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        gone, kept = (
            luma_event('evt-gone', at(days=3)),
            luma_event('evt-kept', at(days=4)),
        )
        await syncer.now([gone, kept])

        first = await syncer.later([kept])
        second = await syncer.later()
        assert (first.changed, second.changed) == (False, False)
        row = await stored(repo, 'evt-gone')
        assert (row.miss_count, row.status, row.revision) == (
            2,
            EventStatus.SCHEDULED,
            0,
        )

        third = await syncer.later()

        assert (third.cancelled, third.changed) == (1, True)
        row = await stored(repo, 'evt-gone')
        assert (row.miss_count, row.status, row.revision) == (
            3,
            EventStatus.CANCELLED,
            1,
        )
        assert row.cancelled
        assert row.last_synced == NOW  # the last feed that listed it

        # Missing a cancelled event changes nothing more.
        fourth = await syncer.later()
        assert not fourth.changed
        assert await stored(repo, 'evt-gone') == row

    @pytest.mark.parametrize(
        ('listed', 'missing_for'),
        [(5, timedelta(minutes=20)), (2, timedelta(minutes=50))],
        ids=['healthy-feed', 'shrunken-feed'],
    )
    async def test_syncs_in_quick_succession_cannot_hurry_a_cancellation(
        self,
        syncer: Syncer,
        repo: EventRepo,
        clock: FakeClock,
        listed: int,
        missing_for: timedelta,
    ) -> None:
        # Syncing by hand, or restarting, can miss an event often enough in
        # seconds; it is cancelled once it has been missing for long enough.
        events = upcoming(6)
        await syncer.now(events)
        for _ in range(6):
            report = await syncer.later(events[:listed], after=timedelta(seconds=1))
            assert not report.changed
        await clock.advance_to(NOW + missing_for - timedelta(seconds=1))
        assert not (await syncer.now()).changed
        row = await stored(repo, 'evt-6')
        assert (row.miss_count, row.status, row.revision) == (
            7,
            EventStatus.SCHEDULED,
            0,
        )

        report = await syncer.later(after=timedelta(seconds=1))

        assert report.cancelled == 6 - listed
        row = await stored(repo, 'evt-6')
        assert (row.miss_count, row.status, row.revision) == (
            8,
            EventStatus.CANCELLED,
            1,
        )

    async def test_the_clubs_only_upcoming_workshop_going_missing_is_noticed(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        # Too few upcoming events for the health check, so an empty feed is
        # believed and the workshop is cancelled like any other.
        past = luma_event('evt-past', at(days=-7))
        only = luma_event('evt-only', at(days=2))
        await syncer.now([past, only])

        reports = [await syncer.later([past]) for _ in range(3)]

        assert [r.ok for r in reports] == [True, True, True]
        assert [r.cancelled for r in reports] == [0, 0, 1]
        assert (await stored(repo, 'evt-only')).cancelled
        assert not (await stored(repo, 'evt-past')).cancelled

    async def test_past_events_missing_from_the_feed_are_left_alone(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        past = luma_event('evt-past', at(days=-1))
        await syncer.now([past])
        before = await stored(repo, 'evt-past')

        for _ in range(4):
            await syncer.later([])

        assert await stored(repo, 'evt-past') == before

    async def test_an_event_that_starts_while_missing_is_never_cancelled(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        soon = luma_event('evt-soon', at(minutes=15))
        await syncer.now([soon])

        await syncer.later([])  # at 12:10, before it starts: a miss
        for _ in range(3):  # from 12:20, after it started
            await syncer.later([])

        row = await stored(repo, 'evt-soon')
        assert (row.miss_count, row.status) == (1, EventStatus.SCHEDULED)

    async def test_missing_at_the_very_moment_it_starts_is_no_miss(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        await syncer.now([luma_event('evt-a', at(minutes=10))])

        await syncer.later([])  # at 12:10, exactly its start

        assert (await stored(repo, 'evt-a')).miss_count == 0

    async def test_an_event_marked_cancelled_in_the_feed_counts_as_missing(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        def ics(status: str) -> bytes:
            return (
                'BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n'
                'UID:evt-a@events.lu.ma\nDTSTART:20261003T170000Z\n'
                f'SUMMARY:Intro to DP\nSTATUS:{status}\nEND:VEVENT\nEND:VCALENDAR'
            ).encode()

        london = ZoneInfo('Europe/London')
        await syncer.now(parse_calendar(ics('TENTATIVE'), tz=london))
        for _ in range(3):
            await syncer.later(parse_calendar(ics('CANCELLED'), tz=london))

        assert (await stored(repo, 'evt-a')).cancelled


async def cancel_by_three_misses(syncer: Syncer, event: LumaEvent) -> None:
    await syncer.now([event])
    for _ in range(3):
        await syncer.later([])


class TestReinstating:
    async def test_a_cancelled_event_that_comes_back_is_reinstated(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await cancel_by_three_misses(syncer, event)

        report = await syncer.later([event])

        assert (report.reinstated, report.moved, report.updated) == (1, 0, 0)
        row = await stored(repo, 'evt-a')
        assert (row.status, row.revision, row.miss_count) == (
            EventStatus.SCHEDULED,
            2,
            0,
        )
        assert row.last_synced == at(minutes=40)

    async def test_coming_back_at_a_new_time_is_still_one_new_revision(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        await cancel_by_three_misses(syncer, event)
        back = replace(event, start=at(days=10), end=None, name='Back on')

        report = await syncer.later([back])

        assert (report.reinstated, report.moved, report.updated) == (1, 0, 0)
        row = await stored(repo, 'evt-a')
        assert (row.revision, row.start_time, row.name) == (2, at(days=10), 'Back on')
        assert row.fingerprint == back.fingerprint()


class TestHealthCheck:
    async def test_a_feed_that_lost_over_half_fails_but_what_it_lists_applies(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)
        moved = replace(events[0], start=at(days=1, hours=2), end=None)

        report = await syncer.later([moved, luma_event('evt-new', at(days=20))])

        assert report == SyncReport(
            CALENDAR,
            ok=False,
            added=1,
            moved=1,
            future_count=2,
            error='feed shrank from 6 to 2 upcoming events',
        )
        assert report.changed
        row = await stored(repo, 'evt-1')
        assert (row.start_time, row.revision, row.last_synced) == (
            moved.start,
            1,
            at(minutes=10),
        )
        assert (await stored(repo, 'evt-new')).first_seen == at(minutes=10)
        for event in events[1:]:  # missed once, nothing more yet
            row = await stored(repo, event.luma_id)
            assert (row.miss_count, row.status, row.revision) == (
                1,
                EventStatus.SCHEDULED,
                0,
            )
        assert await state_of(repo) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=at(minutes=10),
            last_ok=NOW,
            last_future_count=6,
            consecutive_failures=1,
            last_error='feed shrank from 6 to 2 upcoming events',
        )

    @pytest.mark.parametrize(
        ('before', 'after', 'accepted'),
        [
            (4, 2, True),
            (4, 1, False),
            (5, 3, True),
            (5, 2, False),
            (8, 4, True),
            (8, 3, False),
            (3, 0, True),
            (1, 0, True),
        ],
        ids=lambda value: str(value),
    )
    async def test_fewer_than_half_of_four_or_more_is_too_few(
        self, syncer: Syncer, before: int, after: int, accepted: bool
    ) -> None:
        events = upcoming(before)
        await syncer.now(events)

        report = await syncer.later(events[:after])

        assert report.ok is accepted

    async def test_new_events_count_towards_the_feed(self, syncer: Syncer) -> None:
        events = upcoming(6)
        await syncer.now(events)

        report = await syncer.later(events[:2] + upcoming(1, first_day=20))

        assert report.ok  # 3 of 6 is half, not under it

    async def test_a_real_bulk_cancellation_is_believed_within_the_hour(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        # The club cancels 6 of its 8 upcoming workshops, and adds another.
        events = upcoming(8)
        await syncer.now(events)
        kept = [*events[:2], luma_event('evt-new', at(days=1, hours=6))]

        reports = [await syncer.later(kept) for _ in range(6)]

        # Every feed fails the check, but the new workshop is added at once,
        # and the missing ones are cancelled on the sixth miss, an hour on.
        assert [report.ok for report in reports] == [False] * 6
        assert [report.added for report in reports] == [1, 0, 0, 0, 0, 0]
        assert [report.cancelled for report in reports] == [0, 0, 0, 0, 0, 6]
        assert reports[-1].changed
        for event in events[2:]:
            row = await stored(repo, event.luma_id)
            assert (row.miss_count, row.status, row.revision) == (
                6,
                EventStatus.CANCELLED,
                1,
            )
        assert (await state_of(repo)).consecutive_failures == 6

        # They no longer count as upcoming, so the feed is healthy again.
        report = await syncer.later(kept)

        assert (report.ok, report.changed, report.future_count) == (True, False, 3)
        state = await state_of(repo)
        assert (state.last_future_count, state.consecutive_failures) == (3, 0)

    async def test_a_shrunken_feed_for_under_an_hour_cancels_nothing(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)
        for _ in range(5):  # a glitch at Luma, for 50 minutes
            report = await syncer.later(events[:2])
            assert (report.ok, report.changed) == (False, False)

        report = await syncer.later(events)

        assert (report.ok, report.changed) == (True, False)
        for event in events:
            row = await stored(repo, event.luma_id)
            assert (row.miss_count, row.status, row.revision) == (
                0,
                EventStatus.SCHEDULED,
                0,
            )

    async def test_misses_from_shrunken_feeds_count_towards_a_healthy_ones(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)
        for _ in range(2):
            assert not (await syncer.later(events[:2])).ok

        # Missing for half an hour, the last time from a healthy feed.
        report = await syncer.later(events[:5])

        assert (report.ok, report.cancelled) == (True, 1)
        assert (await stored(repo, 'evt-6')).cancelled
        assert (await stored(repo, 'evt-3')).miss_count == 0

    async def test_the_check_compares_with_the_last_healthy_feed(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        events = upcoming(8)
        await syncer.now(events)
        assert (await syncer.later(events[:4])).ok  # half of 8
        assert (await state_of(repo)).last_future_count == 4

        assert not (await syncer.later(events[:1])).ok  # under half of 4
        # Half of 4, though under half of the 8 still stored as upcoming.
        assert (await syncer.later(events[:2])).ok

    async def test_events_that_have_started_since_are_not_expected(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        # Four workshops all starting at 12:05. By the next sync, at 12:10,
        # none can still be upcoming, so a feed with none upcoming is right.
        at_once = [luma_event(f'evt-{n}', at(minutes=5)) for n in range(4)]
        await syncer.now(at_once)

        report = await syncer.later(at_once)

        assert (report.ok, report.future_count) == (True, 0)
        assert (await state_of(repo)).last_future_count == 0

    async def test_after_a_long_gap_only_events_still_upcoming_are_expected(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)

        # Offline for four and a half days: only evt-5 and evt-6 are still
        # upcoming, so the feed is not judged against the 6 of last time.
        report = await syncer.later(events, after=timedelta(days=4, hours=12))

        assert (report.ok, report.future_count) == (True, 2)
        assert (await state_of(repo)).last_future_count == 2


class TestFailures:
    async def test_an_unknown_calendar_raises_and_is_recorded(
        self, sync: EventSync, repo: EventRepo
    ) -> None:
        with pytest.raises(CalendarNotFound) as excinfo:
            await sync.sync(CALENDAR)

        assert excinfo.value.calendar_id == CALENDAR
        assert await repo.events(CALENDAR) == []
        assert await state_of(repo) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=NOW,
            last_ok=None,
            last_future_count=None,
            consecutive_failures=1,
            last_error=str(excinfo.value),
        )

    async def test_a_calendar_that_disappears_keeps_its_events(
        self, syncer: Syncer, feed: FakeFeed, repo: EventRepo, clock: FakeClock
    ) -> None:
        await syncer.now(upcoming(2))
        before = await repo.events(CALENDAR)
        feed.fail(CALENDAR, CalendarNotFound(CALENDAR))
        await clock.advance(SYNC_INTERVAL)

        with pytest.raises(CalendarNotFound):
            await syncer.now()

        assert await repo.events(CALENDAR) == before
        state = await state_of(repo)
        assert (state.last_ok, state.consecutive_failures) == (NOW, 1)

    async def test_a_failed_fetch_is_reported_and_recorded(
        self, syncer: Syncer, feed: FakeFeed, repo: EventRepo, clock: FakeClock
    ) -> None:
        await syncer.now(upcoming(2))
        before = await repo.events(CALENDAR)
        feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))
        await clock.advance(SYNC_INTERVAL)

        report = await syncer.now()

        assert report == SyncReport(
            CALENDAR, ok=False, error=UNREACHABLE, applied=False
        )
        assert await repo.events(CALENDAR) == before
        assert await state_of(repo) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=at(minutes=10),
            last_ok=NOW,
            last_future_count=2,
            consecutive_failures=1,
            last_error=UNREACHABLE,
        )

    async def test_failures_in_a_row_are_counted_until_a_success(
        self, syncer: Syncer, feed: FakeFeed, repo: EventRepo
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)
        feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))
        await syncer.later()
        await syncer.later()
        await syncer.later(events[:1])  # served but unhealthy: a failure too
        assert (await state_of(repo)).consecutive_failures == 3

        await syncer.later(events)

        state = await state_of(repo)
        assert (state.consecutive_failures, state.last_error) == (0, None)
        assert state.last_attempt == state.last_ok == at(minutes=40)

    async def test_an_unexpected_error_propagates_and_changes_nothing(
        self, syncer: Syncer, feed: FakeFeed, repo: EventRepo
    ) -> None:
        feed.fail(CALENDAR, RuntimeError('a bug'))

        with pytest.raises(RuntimeError, match='a bug'):
            await syncer.now()

        assert await repo.calendar_state(CALENDAR) is None

    async def test_a_failure_while_applying_writes_nothing(
        self, syncer: Syncer, repo: EventRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await syncer.now([luma_event('evt-a', at(days=1))])
        before = (await repo.events(CALENDAR), await repo.calendar_state(CALENDAR))

        async def fail_to_save(events: Sequence[StoredEvent]) -> None:
            raise RuntimeError('disk full')

        monkeypatch.setattr(repo, 'save', fail_to_save)
        with pytest.raises(RuntimeError, match='disk full'):
            await syncer.later([luma_event('evt-b', at(days=2))])

        after = (await repo.events(CALENDAR), await repo.calendar_state(CALENDAR))
        assert after == before  # evt-b's insert was rolled back too


class TestLogging:
    async def test_the_third_failure_in_a_row_warns_once_a_streak(
        self,
        syncer: Syncer,
        feed: FakeFeed,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        events = upcoming(6)
        await syncer.now(events)
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)

        feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))
        await syncer.later()
        await syncer.later()
        await syncer.later(events[:1])  # rejected as unhealthy
        feed.fail(CALENDAR, CalendarNotFound(CALENDAR))
        with pytest.raises(CalendarNotFound):
            await syncer.later()
        await syncer.later(events)  # a success ends the streak
        feed.fail(CALENDAR, ExternalServiceError('Luma', UNREACHABLE))
        for _ in range(3):
            await syncer.later()

        failures = [
            (record.levelno, record.getMessage())
            for record in caplog.records
            if record.name == SYNC_LOGGER and 'Could not sync' in record.getMessage()
        ]
        assert [level for level, _ in failures] == [
            logging.INFO,
            logging.INFO,
            logging.WARNING,
            logging.INFO,
            logging.INFO,
            logging.INFO,
            logging.WARNING,
        ]
        assert 'feed shrank from 6 to 1 upcoming events' in failures[2][1]
        assert '(consecutive failures: 3)' in failures[2][1]
        assert any(
            'synced again after 4 failed attempts' in record.getMessage()
            for record in caplog.records
        )

    async def test_syncs_that_change_something_are_logged_at_info(
        self, syncer: Syncer, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=SYNC_LOGGER)
        event = luma_event('evt-a', at(days=1))
        await syncer.now([event])
        await syncer.later([event])

        synced = [
            record.levelno
            for record in caplog.records
            if record.name == SYNC_LOGGER
            and 'Synced Luma calendar' in record.getMessage()
        ]
        assert synced == [logging.INFO, logging.DEBUG]

    async def test_why_a_feed_could_not_be_read_is_logged_not_reported(
        self, syncer: Syncer, feed: FakeFeed, caplog: pytest.LogCaptureFixture
    ) -> None:
        with pytest.raises(ExternalServiceError) as excinfo:
            parse_calendar(b'<!DOCTYPE html>', tz=ZoneInfo('Europe/London'))
        feed.fail(CALENDAR, excinfo.value)
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)

        report = await syncer.now()

        assert report.error == "Luma's calendar feed could not be read."
        assert not (report.ok or report.applied or report.changed)
        [record] = [r for r in caplog.records if r.name == SYNC_LOGGER]
        assert 'ValueError' in record.getMessage()


class TestCalendarsAreKeptApart:
    async def test_each_calendar_syncs_on_its_own(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        shared = luma_event('evt-shared', at(days=2))
        await syncer.now([shared], CALENDAR)
        await syncer.now([shared, luma_event('evt-other', at(days=3))], OTHER_CALENDAR)

        for _ in range(3):
            await syncer.later([], CALENDAR)

        assert (await stored(repo, 'evt-shared', CALENDAR)).cancelled
        theirs = await stored(repo, 'evt-shared', OTHER_CALENDAR)
        assert (theirs.status, theirs.miss_count) == (EventStatus.SCHEDULED, 0)
        assert (await state_of(repo, OTHER_CALENDAR)).last_ok == NOW

    async def test_each_calendar_has_its_own_health_check_and_failures(
        self, syncer: Syncer, feed: FakeFeed, repo: EventRepo
    ) -> None:
        big = upcoming(6)
        await syncer.now(big, CALENDAR)
        await syncer.now([luma_event('evt-small', at(days=1))], OTHER_CALENDAR)

        assert (await syncer.later([], OTHER_CALENDAR)).ok
        assert not (await syncer.later(big[:2], CALENDAR)).ok

        assert (await state_of(repo, CALENDAR)).consecutive_failures == 1
        assert (await state_of(repo, OTHER_CALENDAR)).consecutive_failures == 0


class TestRevisions:
    async def test_on_one_calendar_each_change_is_the_next_revision(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=3))
        moved = replace(event, start=at(days=4), end=None)
        revisions = []
        # Added, moved, missed three times (cancelled), then back.
        for listing in ([event], [moved], [], [], [], [moved]):
            await syncer.later(listing)
            revisions.append((await stored(repo, 'evt-a')).revision)

        assert revisions == [0, 1, 1, 1, 2, 3]

    async def test_an_event_new_to_a_calendar_goes_above_its_revisions_elsewhere(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        # A server that followed CALENDAR was told of revisions 0 and 1, so a
        # server switching to OTHER_CALENDAR must not get either back.
        event = luma_event('evt-shared', at(days=3))
        moved = replace(event, start=at(days=4), end=None)
        await syncer.now([event], CALENDAR)
        await syncer.later([moved], CALENDAR)

        await syncer.later([moved, luma_event('evt-own', at(days=5))], OTHER_CALENDAR)

        assert (await stored(repo, 'evt-shared', OTHER_CALENDAR)).revision == 2
        assert (await stored(repo, 'evt-own', OTHER_CALENDAR)).revision == 0

    async def test_each_change_goes_above_the_events_revisions_elsewhere(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        event = luma_event('evt-shared', at(days=3))
        moved = replace(event, start=at(days=4), end=None)

        async def revisions() -> tuple[int, int]:
            mine = await stored(repo, 'evt-shared', CALENDAR)
            theirs = await stored(repo, 'evt-shared', OTHER_CALENDAR)
            return mine.revision, theirs.revision

        await syncer.now([event], CALENDAR)
        await syncer.now([event], OTHER_CALENDAR)
        assert await revisions() == (0, 1)
        await syncer.later([moved], CALENDAR)
        assert await revisions() == (2, 1)
        await syncer.now([moved], OTHER_CALENDAR)
        assert await revisions() == (2, 3)
        for _ in range(3):
            await syncer.later([], OTHER_CALENDAR)
        assert (await stored(repo, 'evt-shared', OTHER_CALENDAR)).cancelled
        assert await revisions() == (2, 4)
        await syncer.later([moved], OTHER_CALENDAR)  # reinstated
        assert await revisions() == (2, 5)
        await syncer.now([event], CALENDAR)  # moved back
        assert await revisions() == (6, 5)


class TestRobustness:
    async def test_a_feed_listing_an_event_twice_keeps_the_first(
        self, syncer: Syncer, repo: EventRepo
    ) -> None:
        first = luma_event('evt-a', at(days=1), name='First')
        second = luma_event('evt-a', at(days=2), name='Second')

        assert (await syncer.now([first, second])).added == 1
        again = await syncer.later([first, second])

        assert not again.changed  # no back and forth between the two
        row = await stored(repo, 'evt-a')
        assert (row.name, row.revision) == ('First', 0)

    async def test_two_syncs_at_once_apply_one_after_the_other(
        self, sync: EventSync, feed: FakeFeed, repo: EventRepo
    ) -> None:
        feed.serve(CALENDAR, upcoming(2))

        reports = await asyncio.gather(sync.sync(CALENDAR), sync.sync(CALENDAR))

        assert sorted(report.added for report in reports) == [0, 2]
        assert len(await repo.events(CALENDAR)) == 2

    async def test_syncs_of_one_calendar_take_turns(
        self, db: Database, repo: EventRepo, clock: FakeClock
    ) -> None:
        # Otherwise the first sync, whose fetch is slow, would apply its feed
        # after the second's newer one and move the event back.
        feed = SlowFeed()
        sync = EventSync(db, repo, feed, clock)
        event = luma_event('evt-a', at(days=3))
        feed.serve(CALENDAR, [event])
        first = asyncio.create_task(sync.sync(CALENDAR))
        await clock.settle()
        feed.serve(CALENDAR, [replace(event, start=at(days=4), end=None)])
        second = asyncio.create_task(sync.sync(CALENDAR))
        await clock.settle()
        fetched_while_the_first_was_slow = list(feed.fetches)

        feed.release.set()
        reports = await asyncio.gather(first, second)

        assert fetched_while_the_first_was_slow == [CALENDAR]
        assert [(report.added, report.moved) for report in reports] == [(1, 0), (0, 1)]
        assert (await stored(repo, 'evt-a')).start_time == at(days=4)

    async def test_other_calendars_do_not_wait_their_turn(
        self, db: Database, repo: EventRepo, clock: FakeClock
    ) -> None:
        feed = SlowFeed()
        sync = EventSync(db, repo, feed, clock)
        feed.serve(CALENDAR, upcoming(1))
        feed.serve(OTHER_CALENDAR, upcoming(2))
        slow = asyncio.create_task(sync.sync(CALENDAR))
        await clock.settle()

        try:
            other = await asyncio.wait_for(sync.sync(OTHER_CALENDAR), timeout=5)
        finally:
            feed.release.set()

        assert (other.added, (await slow).added) == (2, 1)

    async def test_refuses_to_run_inside_a_transaction(
        self, sync: EventSync, feed: FakeFeed, db: Database, repo: EventRepo
    ) -> None:
        feed.serve(CALENDAR, upcoming(1))

        async with db.transaction():
            with pytest.raises(RuntimeError, match='outside any database transaction'):
                await sync.sync(CALENDAR)

        assert feed.fetches == []
        assert await repo.calendar_state(CALENDAR) is None
