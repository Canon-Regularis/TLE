"""Component tests for the workshops feature's EventRepo and settings, on kcpc.db."""

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.settings import FeatureRegistry, FeatureSpec, GuildSettingsRepo
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.workshops.repo import (
    CalendarState,
    EventRepo,
    EventStatus,
    StoredEvent,
)
from tle.kcpc.features.workshops.settings import (
    SPEC,
    WORKSHOPS,
    WorkshopSettings,
    effective_calendar,
)
from tle.kcpc.platforms.luma import LumaEvent

CALENDAR = 'cal-ExampleClub001'
OTHER_CALENDAR = 'cal-OtherClub0002'
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)


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


@pytest.fixture
def repo(db: Database) -> EventRepo:
    return EventRepo(db)


async def stored(repo: EventRepo, luma_id: str) -> StoredEvent:
    [event] = [e for e in await repo.events(CALENDAR) if e.luma_id == luma_id]
    return event


async def cancel(repo: EventRepo, luma_id: str) -> None:
    event = await stored(repo, luma_id)
    await repo.save([replace(event, status=EventStatus.CANCELLED)])


class TestAddAndRead:
    async def test_an_added_event_reads_back_with_every_column(
        self, repo: EventRepo
    ) -> None:
        event = luma_event('evt-a', at(days=7))
        await repo.add(CALENDAR, [event], now=NOW)

        [read] = await repo.events(CALENDAR)
        assert read == StoredEvent(
            event_id=read.event_id,
            calendar_id=CALENDAR,
            luma_id='evt-a',
            name='Workshop evt-a',
            start_time=at(days=7),
            end_time=at(days=7, hours=2),
            url='https://luma.com/evt-a',
            location='Room 101',
            status=EventStatus.SCHEDULED,
            revision=0,
            fingerprint=event.fingerprint(),
            miss_count=0,
            first_seen=NOW,
            last_synced=NOW,
        )
        assert isinstance(read.event_id, int)
        assert read.start_time.tzinfo is UTC
        assert not read.cancelled

    async def test_no_end_and_no_location_read_back_as_none(
        self, repo: EventRepo
    ) -> None:
        await repo.add(
            CALENDAR,
            [luma_event('evt-a', at(days=7), end=None, location=None)],
            now=NOW,
        )

        read = await stored(repo, 'evt-a')
        assert (read.end_time, read.location) == (None, None)

    async def test_times_are_stored_as_epoch_seconds(
        self, repo: EventRepo, db: Database
    ) -> None:
        await repo.add(
            CALENDAR, [luma_event('evt-a', at(days=7))], now=at(seconds=0.75)
        )

        row = await db.fetchone(
            'SELECT start_time, end_time, first_seen, last_synced FROM event'
        )
        assert tuple(row or ()) == (
            to_epoch(at(days=7)),
            to_epoch(at(days=7, hours=2)),
            to_epoch(NOW),
            to_epoch(NOW),
        )

    async def test_events_are_listed_by_start_then_id_cancelled_ones_too(
        self, repo: EventRepo
    ) -> None:
        await repo.add(
            CALENDAR,
            [
                luma_event('evt-c', at(days=3)),
                luma_event('evt-b', at(days=1)),
                luma_event('evt-a', at(days=3)),
            ],
            now=NOW,
        )
        await cancel(repo, 'evt-b')

        events = await repo.events(CALENDAR)
        assert [(e.luma_id, e.cancelled) for e in events] == [
            ('evt-b', True),
            ('evt-a', False),
            ('evt-c', False),
        ]

    async def test_adding_a_stored_event_again_fails_and_adds_nothing(
        self, repo: EventRepo
    ) -> None:
        await repo.add(CALENDAR, [luma_event('evt-a', at(days=1))], now=NOW)

        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            await repo.add(
                CALENDAR,
                [luma_event('evt-new', at(days=2)), luma_event('evt-a', at(days=3))],
                now=NOW,
            )
        assert [e.luma_id for e in await repo.events(CALENDAR)] == ['evt-a']

    async def test_nothing_to_write_is_fine(self, repo: EventRepo) -> None:
        await repo.add(CALENDAR, [], now=NOW)
        await repo.save([])
        assert await repo.events(CALENDAR) == []


class TestSave:
    async def test_writes_back_all_but_the_identity_and_first_seen(
        self, repo: EventRepo
    ) -> None:
        await repo.add(CALENDAR, [luma_event('evt-a', at(days=7))], now=NOW)
        original = await stored(repo, 'evt-a')
        changed = replace(
            original,
            name='Renamed',
            start_time=at(days=8),
            end_time=None,
            url='https://luma.com/renamed',
            location=None,
            status=EventStatus.CANCELLED,
            revision=3,
            fingerprint='0' * 40,
            miss_count=2,
            last_synced=at(hours=1),
        )

        await repo.save(
            [
                replace(
                    changed,
                    calendar_id=OTHER_CALENDAR,
                    luma_id='evt-z',
                    first_seen=at(days=-1),
                )
            ]
        )

        assert await repo.events(CALENDAR) == [changed]
        assert changed.cancelled
        assert await repo.events(OTHER_CALENDAR) == []

    async def test_saves_several_at_once(self, repo: EventRepo) -> None:
        await repo.add(
            CALENDAR,
            [luma_event('evt-a', at(days=1)), luma_event('evt-b', at(days=2))],
            now=NOW,
        )
        events = await repo.events(CALENDAR)

        await repo.save([replace(e, miss_count=1) for e in events])

        assert [e.miss_count for e in await repo.events(CALENDAR)] == [1, 1]


class TestCalendarsAreKeptApart:
    async def test_one_luma_event_on_two_calendars_is_stored_twice(
        self, repo: EventRepo
    ) -> None:
        await repo.add(CALENDAR, [luma_event('evt-a', at(days=1))], now=NOW)
        await repo.add(
            OTHER_CALENDAR, [luma_event('evt-a', at(days=2), name='Other')], now=NOW
        )

        [mine] = await repo.events(CALENDAR)
        [theirs] = await repo.events(OTHER_CALENDAR)
        assert mine.event_id != theirs.event_id
        assert (mine.name, theirs.name) == ('Workshop evt-a', 'Other')
        assert await repo.between(CALENDAR, NOW, at(days=30)) == [mine]
        assert await repo.next_from(OTHER_CALENDAR, NOW) == theirs

    async def test_an_unknown_calendar_has_nothing(self, repo: EventRepo) -> None:
        await repo.add(CALENDAR, [luma_event('evt-a', at(days=1))], now=NOW)

        assert await repo.events(OTHER_CALENDAR) == []
        assert await repo.between(OTHER_CALENDAR, NOW, at(days=30)) == []
        assert await repo.next_from(OTHER_CALENDAR, NOW) is None
        assert await repo.calendar_state(OTHER_CALENDAR) is None


class TestRevisions:
    async def test_events_are_added_at_revision_0_unless_given_one(
        self, repo: EventRepo
    ) -> None:
        await repo.add(
            CALENDAR,
            [luma_event('evt-a', at(days=1)), luma_event('evt-b', at(days=2))],
            now=NOW,
            revisions={'evt-b': 3, 'evt-elsewhere': 7},
        )

        assert [(e.luma_id, e.revision) for e in await repo.events(CALENDAR)] == [
            ('evt-a', 0),
            ('evt-b', 3),
        ]

    async def test_the_highest_revisions_are_over_every_calendar(
        self, repo: EventRepo
    ) -> None:
        assert await repo.highest_revisions() == {}
        await repo.add(
            CALENDAR,
            [luma_event('evt-a', at(days=1)), luma_event('evt-b', at(days=2))],
            now=NOW,
            revisions={'evt-a': 2},
        )
        await repo.add(
            OTHER_CALENDAR,
            [luma_event('evt-a', at(days=1)), luma_event('evt-c', at(days=3))],
            now=NOW,
            revisions={'evt-a': 1, 'evt-c': 4},
        )
        assert await repo.highest_revisions() == {'evt-a': 2, 'evt-b': 0, 'evt-c': 4}

        [_, evt_c] = await repo.events(OTHER_CALENDAR)  # by start
        await repo.save([replace(evt_c, revision=5)])  # cancelled, say

        assert await repo.highest_revisions() == {'evt-a': 2, 'evt-b': 0, 'evt-c': 5}


START = at(days=2)


class TestBetween:
    @pytest.mark.parametrize(
        ('start', 'end', 'included'),
        [
            (START, START + SECOND, True),
            (START - SECOND, START, False),
            (START - MICROSECOND, START + MICROSECOND, True),
            (START + MICROSECOND, START + SECOND, False),
            (START - SECOND, START - MICROSECOND, False),
            (START - SECOND, START + MICROSECOND, True),
        ],
        ids=[
            'from-its-start',
            'until-its-start',
            'around-it',
            'from-just-after',
            'until-just-before',
            'until-just-after',
        ],
    )
    async def test_is_from_start_inclusive_to_end_exclusive(
        self, repo: EventRepo, start: datetime, end: datetime, included: bool
    ) -> None:
        await repo.add(CALENDAR, [luma_event('evt-a', START)], now=NOW)

        found = await repo.between(CALENDAR, start, end)
        assert [e.luma_id for e in found] == (['evt-a'] if included else [])

    async def test_leaves_out_cancelled_events_unless_asked(
        self, repo: EventRepo
    ) -> None:
        await repo.add(
            CALENDAR,
            [
                luma_event('evt-c', at(days=3)),
                luma_event('evt-b', at(days=1)),
                luma_event('evt-a', at(days=2)),
                luma_event('evt-later', at(days=40)),
            ],
            now=NOW,
        )
        await cancel(repo, 'evt-a')

        scheduled = await repo.between(CALENDAR, NOW, at(days=30))
        everything = await repo.between(
            CALENDAR, NOW, at(days=30), include_cancelled=True
        )
        assert [e.luma_id for e in scheduled] == ['evt-b', 'evt-c']
        assert [e.luma_id for e in everything] == ['evt-b', 'evt-a', 'evt-c']


class TestNextFrom:
    async def test_is_the_first_scheduled_event_at_or_after(
        self, repo: EventRepo
    ) -> None:
        await repo.add(
            CALENDAR,
            [
                luma_event('evt-past', at(days=-1)),
                luma_event('evt-cancelled', at(days=1)),
                luma_event('evt-next', START),
                luma_event('evt-after', at(days=3)),
            ],
            now=NOW,
        )
        await cancel(repo, 'evt-cancelled')

        assert await repo.next_from(CALENDAR, NOW) == await stored(repo, 'evt-next')

    @pytest.mark.parametrize(
        ('when', 'expected'),
        [
            (START - SECOND, 'evt-next'),
            (START, 'evt-next'),
            (START + MICROSECOND, 'evt-after'),
            (at(days=3) + SECOND, None),
        ],
        ids=['before', 'at-its-start', 'just-after', 'after-everything'],
    )
    async def test_boundaries(
        self, repo: EventRepo, when: datetime, expected: str | None
    ) -> None:
        await repo.add(
            CALENDAR,
            [luma_event('evt-next', START), luma_event('evt-after', at(days=3))],
            now=NOW,
        )

        found = await repo.next_from(CALENDAR, when)
        assert (found.luma_id if found else None) == expected


class TestCalendarState:
    async def test_none_until_a_sync_is_attempted(self, repo: EventRepo) -> None:
        assert await repo.calendar_state(CALENDAR) is None

    async def test_failures_are_counted_until_a_success(self, repo: EventRepo) -> None:
        assert await repo.record_failure(CALENDAR, now=NOW, error='first') == 1
        assert await repo.record_failure(CALENDAR, now=at(minutes=10), error='2nd') == 2
        assert await repo.calendar_state(CALENDAR) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=at(minutes=10),
            last_ok=None,
            last_future_count=None,
            consecutive_failures=2,
            last_error='2nd',
        )

        await repo.record_success(CALENDAR, now=at(minutes=20), future_count=5)
        assert await repo.calendar_state(CALENDAR) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=at(minutes=20),
            last_ok=at(minutes=20),
            last_future_count=5,
            consecutive_failures=0,
            last_error=None,
        )

        assert await repo.record_failure(CALENDAR, now=at(minutes=30), error='3rd') == 1
        assert await repo.calendar_state(CALENDAR) == CalendarState(
            calendar_id=CALENDAR,
            last_attempt=at(minutes=30),
            last_ok=at(minutes=20),
            last_future_count=5,
            consecutive_failures=1,
            last_error='3rd',
        )

    async def test_a_first_success_creates_the_state(self, repo: EventRepo) -> None:
        await repo.record_success(CALENDAR, now=NOW, future_count=0)

        state = await repo.calendar_state(CALENDAR)
        assert state is not None
        assert (state.last_ok, state.last_future_count) == (NOW, 0)
        assert (state.consecutive_failures, state.last_error) == (0, None)

    @pytest.mark.parametrize(
        ('error', 'kept'),
        [
            ('x' * 500, 'x' * 500),
            ('x' * 501, 'x' * 499 + '…'),
            ('x' * 5000, 'x' * 499 + '…'),
        ],
        ids=['500', '501', '5000'],
    )
    async def test_errors_are_cut_to_500_characters(
        self, repo: EventRepo, error: str, kept: str
    ) -> None:
        await repo.record_failure(CALENDAR, now=NOW, error=error)

        state = await repo.calendar_state(CALENDAR)
        assert state is not None and state.last_error == kept

    async def test_writes_join_the_callers_transaction(
        self, repo: EventRepo, db: Database
    ) -> None:
        with pytest.raises(RuntimeError, match='undo'):
            async with db.transaction():
                await repo.add(CALENDAR, [luma_event('evt-a', at(days=1))], now=NOW)
                await repo.record_failure(CALENDAR, now=NOW, error='failed')
                raise RuntimeError('undo')

        assert await repo.events(CALENDAR) == []
        assert await repo.calendar_state(CALENDAR) is None


class TestWorkshopSettings:
    def test_reminders_go_out_a_day_and_an_hour_before_by_default(self) -> None:
        settings = WorkshopSettings()

        assert (settings.enabled, settings.calendar_id) == (False, None)
        assert settings.reminder_minutes == (1440, 60)
        assert SPEC == FeatureSpec(
            WORKSHOPS,
            'Workshops',
            'Luma workshop reminders, 24h and 1h before',
            WorkshopSettings,
        )

    @pytest.mark.parametrize(
        ('own', 'default', 'expected'),
        [
            ('cal-Own', 'cal-Default', 'cal-Own'),
            ('cal-Own', None, 'cal-Own'),
            (None, 'cal-Default', 'cal-Default'),
            ('', 'cal-Default', 'cal-Default'),
            (None, None, None),
            ('', '', None),
        ],
        ids=['own', 'own-only', 'default', 'empty-is-unset', 'neither', 'both-empty'],
    )
    def test_the_effective_calendar_is_the_servers_own_else_the_default(
        self, own: str | None, default: str | None, expected: str | None
    ) -> None:
        settings = WorkshopSettings(calendar_id=own)

        assert effective_calendar(settings, default) == expected

    async def test_round_trip_through_guild_settings(
        self, db: Database, clock: FakeClock
    ) -> None:
        registry = FeatureRegistry()
        registry.register(SPEC)
        guild_settings = GuildSettingsRepo(db, clock, registry)

        await guild_settings.update(
            1234, WORKSHOPS, calendar_id=CALENDAR, reminder_minutes=(30, 10)
        )

        assert await guild_settings.get_typed(
            1234, WORKSHOPS, WorkshopSettings
        ) == WorkshopSettings(calendar_id=CALENDAR, reminder_minutes=(30, 10))
