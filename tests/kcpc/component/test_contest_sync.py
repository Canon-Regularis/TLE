"""Component tests for the contests feature's ContestSync.

The sources are fakes; the database and the repo are real, and time is a
FakeClock, so each test reads back exactly what each sync stored.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.features.contests.repo import (
    ContestInfo,
    ContestRepo,
    ContestStatus,
    SourceRecord,
    SourceState,
    StoredContest,
)
from tle.kcpc.features.contests.settings import MANUAL
from tle.kcpc.features.contests.sync import ContestSync, SourceSnapshot, SyncReport

ATCODER = 'atcoder'
CODEFORCES = 'codeforces'
ICPC = 'icpc'
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # where the clock fixture starts
SYNC_INTERVAL = timedelta(minutes=10)
DURATION = timedelta(minutes=100)
UKIEPC_DAY = date(2026, 10, 17)
ADMIN = 'admin-1234'
SYNC_LOGGER = 'tle.kcpc.features.contests.sync'
UNREACHABLE = 'AtCoder is not responding right now. Please try again later.'


def at(**offset: float) -> datetime:
    """``NOW`` moved by ``offset``, e.g. ``at(days=7)``."""
    return NOW + timedelta(**offset)


def contest(
    external_id: str, start: datetime, *, platform: str = ATCODER, **changes: Any
) -> ContestInfo:
    info = ContestInfo(
        platform=platform,
        external_id=external_id,
        name=f'Contest {external_id}',
        start=start,
        start_date=None,
        end=start + DURATION,
        url=f'https://example.com/contests/{external_id}',
    )
    return replace(info, **changes)


def dated(
    external_id: str, day: date = UKIEPC_DAY, *, platform: str = ICPC
) -> ContestInfo:
    """A contest known only by its date, as ICPC's are."""
    return ContestInfo(
        platform=platform,
        external_id=external_id,
        name=f'Regional {external_id}',
        start=None,
        start_date=day,
        end=None,
        url='https://icpc.global/',
    )


def upcoming(count: int, *, first_day: int = 1) -> list[ContestInfo]:
    """``count`` AtCoder contests a day apart, the first ``first_day`` days on."""
    return [
        contest(f'abc{day}', at(days=day))
        for day in range(first_day, first_day + count)
    ]


class FakeSource:
    """A ``ContestSource`` serving whatever contests it is given.

    After ``fail``, fetching raises the error, until it is served again.
    """

    def __init__(self, platform: str = ATCODER, *, complete: bool = True) -> None:
        self.name = platform
        self.platform = platform
        self.complete = complete
        self.fetches = 0
        self._contests: list[ContestInfo] = []
        self._error: Exception | None = None

    def serve(self, contests: Sequence[ContestInfo]) -> None:
        self._contests = list(contests)
        self._error = None

    def fail(self, error: Exception) -> None:
        self._error = error

    async def fetch(self) -> SourceSnapshot:
        self.fetches += 1
        if self._error is not None:
            raise self._error
        return SourceSnapshot(list(self._contests), complete=self.complete)


class SlowSource(FakeSource):
    """A ``FakeSource`` whose first fetch is slow.

    It takes the contests served at that moment, but returns them only once
    ``release`` is set.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def fetch(self) -> SourceSnapshot:
        snapshot = await super().fetch()
        if self.fetches == 1:
            await self.release.wait()
        return snapshot


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def icpc() -> FakeSource:
    """ICPC's source: it lists only the contests it was asked for."""
    return FakeSource(ICPC, complete=False)


@pytest.fixture
def repo(db: Database) -> ContestRepo:
    return ContestRepo(db)


@pytest.fixture
def sync(db: Database, repo: ContestRepo, clock: FakeClock) -> ContestSync:
    return ContestSync(db, repo, clock)


class Syncer:
    """Runs syncs one after another, as a source's job does.

    They are ten minutes apart by default: the jobs run every 5 minutes
    (Codeforces), every 30 minutes (AtCoder and clist.by) or every 6 hours
    (icpc.global).
    """

    def __init__(self, sync: ContestSync, source: FakeSource, clock: FakeClock) -> None:
        self.sync = sync
        self.source = source
        self.clock = clock

    async def now(
        self,
        contests: Sequence[ContestInfo] | None = None,
        source: FakeSource | None = None,
    ) -> SyncReport:
        """Sync at the current time, serving ``contests`` first if given."""
        source = source or self.source
        if contests is not None:
            source.serve(contests)
        return await self.sync.sync(source)

    async def later(
        self,
        contests: Sequence[ContestInfo] | None = None,
        source: FakeSource | None = None,
        *,
        after: timedelta = SYNC_INTERVAL,
    ) -> SyncReport:
        """Sync ``after`` from now, serving ``contests`` first if given."""
        await self.clock.advance(after)
        return await self.now(contests, source)


@pytest.fixture
def syncer(sync: ContestSync, source: FakeSource, clock: FakeClock) -> Syncer:
    return Syncer(sync, source, clock)


async def record(
    repo: ContestRepo, external_id: str, platform: str = ATCODER
) -> SourceRecord:
    [found] = [
        record
        for record in await repo.source_records(platform)
        if record.reported.external_id == external_id
    ]
    return found


async def stored(
    repo: ContestRepo, external_id: str, platform: str = ATCODER
) -> StoredContest:
    contest = await repo.by_id((await record(repo, external_id, platform)).contest_id)
    assert contest is not None
    return contest


async def state_of(repo: ContestRepo, source: str = ATCODER) -> SourceState:
    state = await repo.source_state(source)
    assert state is not None
    return state


def misses(row: StoredContest) -> tuple[int, ContestStatus, int]:
    return row.miss_count, row.status, row.revision


def warned(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the syncs have logged at WARNING, which reaches the Discord log channel."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == SYNC_LOGGER and record.levelno == logging.WARNING
    ]


class TestSyncReport:
    @pytest.mark.parametrize(
        'report',
        [
            SyncReport(ATCODER, ok=True, added=1),
            SyncReport(ATCODER, ok=True, updated=1),
            SyncReport(ATCODER, ok=True, moved=1),
            SyncReport(ATCODER, ok=True, cancelled=1),
            SyncReport(ATCODER, ok=True, reinstated=1),
        ],
        ids=['added', 'updated', 'moved', 'cancelled', 'reinstated'],
    )
    def test_changed_by_any_change(self, report: SyncReport) -> None:
        assert report.changed

    def test_not_changed_otherwise(self) -> None:
        assert not SyncReport(ATCODER, ok=True, future_count=5).changed
        assert not SyncReport(ATCODER, ok=False, error='list shrank').changed


class TestAdding:
    async def test_a_first_sync_stores_every_contest(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        contests = upcoming(2)

        report = await syncer.now(contests)

        assert report == SyncReport(ATCODER, ok=True, added=2, future_count=2)
        for info in contests:
            row = await record(repo, info.external_id)
            assert (row.reported, row.status, row.revision) == (
                info,
                ContestStatus.SCHEDULED,
                0,
            )
            assert (row.miss_count, row.last_synced) == (0, NOW)
        assert await state_of(repo) == SourceState(
            source=ATCODER,
            last_attempt=NOW,
            last_ok=NOW,
            last_future_count=2,
            consecutive_failures=0,
            last_error=None,
        )

    async def test_only_contests_still_to_start_are_upcoming(
        self, syncer: Syncer
    ) -> None:
        running = contest('arc200', at(minutes=-10))

        report = await syncer.now([running, contest('abc478', at(days=1))])

        assert (report.added, report.future_count) == (2, 1)

    async def test_contests_known_only_by_their_date_are_upcoming_until_their_day(
        self, syncer: Syncer, icpc: FakeSource, repo: ContestRepo
    ) -> None:
        today = dated('9583', NOW.date())  # its day started at midnight

        report = await syncer.now([today, dated('9584')], icpc)

        assert report == SyncReport(ICPC, ok=True, added=2, future_count=1)
        assert (await stored(repo, '9584', ICPC)).start_date == UKIEPC_DAY

    async def test_contests_new_to_a_later_list_are_added_then(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now(upcoming(1))

        report = await syncer.later(upcoming(2))

        assert (report.added, report.changed) == (1, True)
        assert (await stored(repo, 'abc2')).first_seen == at(minutes=10)


class TestListedAgain:
    async def test_an_unchanged_contest_is_only_marked_as_seen(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        contests = upcoming(1)
        await syncer.now(contests)

        report = await syncer.later(contests)

        assert report == SyncReport(ATCODER, ok=True, future_count=1)
        row = await stored(repo, 'abc1')
        assert (row.last_synced, row.first_seen, row.revision) == (
            at(minutes=10),
            NOW,
            0,
        )

    async def test_a_change_at_the_same_start_is_an_update(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=1))
        await syncer.now([info])
        renamed = replace(info, name='AtCoder Beginner Contest 478', url=None)

        report = await syncer.later([renamed])

        assert (report.updated, report.moved, report.changed) == (1, 0, True)
        assert (await record(repo, 'abc478')).reported == renamed
        assert (await stored(repo, 'abc478')).revision == 0

    async def test_a_new_start_is_a_move_with_a_new_revision(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc478', at(days=1))])

        first = await syncer.later([contest('abc478', at(days=1, hours=1))])
        second = await syncer.later([contest('abc478', at(days=2))])

        assert [(r.moved, r.updated) for r in (first, second)] == [(1, 0), (1, 0)]
        row = await stored(repo, 'abc478')
        assert (row.start, row.end, row.revision) == (
            at(days=2),
            at(days=2) + DURATION,
            2,
        )

    async def test_a_listing_resets_the_misses(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        contests = upcoming(2)
        await syncer.now(contests)
        await syncer.later(contests[:1])
        await syncer.later(contests[:1])
        assert (await stored(repo, 'abc2')).miss_count == 2

        report = await syncer.later(contests)

        assert not report.changed
        assert misses(await stored(repo, 'abc2')) == (0, ContestStatus.SCHEDULED, 0)

    async def test_a_contest_known_only_by_its_date_that_gets_a_time_moves(
        self, syncer: Syncer, icpc: FakeSource, repo: ContestRepo
    ) -> None:
        await syncer.now([dated('9584')], icpc)
        new_day = dated('9584', date(2026, 10, 18))
        start = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)

        redated = await syncer.later([new_day], icpc)
        timed = await syncer.later([replace(new_day, start=start)], icpc)

        # A new date alone changes nothing members could be reminded of.
        assert (redated.updated, redated.moved) == (1, 0)
        assert (timed.updated, timed.moved) == (0, 1)
        row = await stored(repo, '9584', ICPC)
        assert (row.start, row.start_date, row.revision) == (
            start,
            date(2026, 10, 18),
            1,
        )
        assert row.time_confirmed


class TestCancelling:
    async def test_an_upcoming_contest_is_cancelled_when_three_lists_miss_it(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        gone, kept = contest('abc478', at(days=3)), contest('abc479', at(days=4))
        await syncer.now([gone, kept])

        first = await syncer.later([kept])
        second = await syncer.later()
        assert (first.changed, second.changed) == (False, False)
        assert misses(await stored(repo, 'abc478')) == (2, ContestStatus.SCHEDULED, 0)

        third = await syncer.later()

        assert (third.cancelled, third.changed) == (1, True)
        row = await stored(repo, 'abc478')
        assert misses(row) == (3, ContestStatus.CANCELLED, 1)
        assert row.cancelled
        assert row.last_synced == NOW  # the last list that had it

        # Missing a cancelled contest changes nothing more.
        fourth = await syncer.later()
        assert not fourth.changed
        assert await stored(repo, 'abc478') == row

    @pytest.mark.parametrize(
        ('listed', 'missing_for'),
        [(5, timedelta(minutes=20)), (2, timedelta(minutes=50))],
        ids=['healthy-list', 'shrunken-list'],
    )
    async def test_syncs_in_quick_succession_cannot_hurry_a_cancellation(
        self,
        syncer: Syncer,
        repo: ContestRepo,
        clock: FakeClock,
        listed: int,
        missing_for: timedelta,
    ) -> None:
        contests = upcoming(6)
        await syncer.now(contests)
        missed = contests[-1].external_id

        # Six syncs a second apart, as by hand: enough misses, too soon.
        for _ in range(6):
            assert not (
                await syncer.later(contests[:listed], after=timedelta(seconds=1))
            ).cancelled
        assert misses(await stored(repo, missed)) == (6, ContestStatus.SCHEDULED, 0)

        await clock.advance(missing_for - timedelta(seconds=7))
        assert not (await syncer.now()).cancelled
        report = await syncer.later(after=timedelta(seconds=1))

        assert report.cancelled == 6 - listed
        assert (await stored(repo, missed)).cancelled

    async def test_a_contest_that_leaves_the_list_as_it_starts_is_not_cancelled(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        # AtCoder lists only contests still to start.
        starting = contest('abc478', at(minutes=25))
        later = contest('abc479', at(days=7))
        await syncer.now([starting, later])
        await syncer.later()
        await syncer.later()

        reports = [await syncer.later([later]) for _ in range(6)]

        assert not any(report.changed for report in reports)
        assert misses(await stored(repo, 'abc478')) == (0, ContestStatus.SCHEDULED, 0)

    async def test_a_contest_that_starts_while_missing_is_never_cancelled(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc478', at(minutes=15))])

        reports = [await syncer.later([]) for _ in range(5)]

        assert not any(report.changed for report in reports)
        assert misses(await stored(repo, 'abc478')) == (1, ContestStatus.SCHEDULED, 0)

    async def test_missing_at_the_very_moment_it_starts_is_no_miss(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc478', at(minutes=10))])

        await syncer.later([])

        assert (await stored(repo, 'abc478')).miss_count == 0

    async def test_past_contests_missing_from_the_list_are_left_alone(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc477', at(days=-1)), *upcoming(1)])
        before = await stored(repo, 'abc477')

        for _ in range(4):
            await syncer.later(upcoming(1))

        assert await stored(repo, 'abc477') == before

    async def test_a_contest_known_only_by_its_date_is_missed_until_its_day(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        # Complete lists give start times, but dates follow the same rules.
        today = dated('today', NOW.date(), platform=ATCODER)
        tomorrow = dated('tomorrow', date(2026, 10, 2), platform=ATCODER)
        await syncer.now([today, tomorrow])

        reports = [await syncer.later([]) for _ in range(3)]

        assert [report.cancelled for report in reports] == [0, 0, 1]
        assert misses(await stored(repo, 'tomorrow')) == (3, ContestStatus.CANCELLED, 1)
        assert misses(await stored(repo, 'today')) == (0, ContestStatus.SCHEDULED, 0)

    async def test_a_list_of_only_some_contests_misses_none(
        self, syncer: Syncer, icpc: FakeSource, repo: ContestRepo
    ) -> None:
        await syncer.now([dated('9584'), dated('9585')], icpc)

        reports = [await syncer.later([dated('9585')], icpc) for _ in range(8)]

        assert not any(report.changed for report in reports)
        assert all(report.ok for report in reports)
        assert misses(await stored(repo, '9584', ICPC)) == (
            0,
            ContestStatus.SCHEDULED,
            0,
        )

    async def test_manual_contests_and_other_platforms_are_left_alone(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        manual = await repo.add_manual(
            'Club contest', at(days=1), at(days=1, hours=2), None, now=NOW
        )
        codeforces = FakeSource(CODEFORCES)
        await syncer.now([contest('2145', at(days=2), platform=CODEFORCES)], codeforces)
        await syncer.now(upcoming(1))
        before = [
            await repo.by_id(manual.contest_id),
            await stored(repo, '2145', CODEFORCES),
        ]

        reports = [await syncer.later([]) for _ in range(6)]

        assert sum(report.cancelled for report in reports) == 1  # abc1 alone
        after = [
            await repo.by_id(manual.contest_id),
            await stored(repo, '2145', CODEFORCES),
        ]
        assert after == before

    async def test_there_is_no_source_of_manual_contests(
        self, sync: ContestSync, repo: ContestRepo
    ) -> None:
        manual = FakeSource(MANUAL)

        with pytest.raises(ValueError, match='no source to sync'):
            await sync.sync(manual)

        assert manual.fetches == 0
        assert await repo.source_state(MANUAL) is None


class TestReinstating:
    async def test_a_cancelled_contest_that_comes_back_is_reinstated(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=2))
        await syncer.now([info])
        for _ in range(3):
            await syncer.later([])
        assert (await stored(repo, 'abc478')).cancelled

        report = await syncer.later([info])

        assert (report.reinstated, report.moved, report.updated) == (1, 0, 0)
        row = await stored(repo, 'abc478')
        assert misses(row) == (0, ContestStatus.SCHEDULED, 2)
        assert row.last_synced == at(minutes=40)

    async def test_coming_back_at_a_new_time_is_still_one_new_revision(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc478', at(days=2))])
        for _ in range(3):
            await syncer.later([])

        report = await syncer.later([contest('abc478', at(days=3))])

        assert (report.reinstated, report.moved) == (1, 0)
        row = await stored(repo, 'abc478')
        assert (row.start, row.status, row.revision) == (
            at(days=3),
            ContestStatus.SCHEDULED,
            2,
        )


class TestAdminTimes:
    async def test_an_admins_time_survives_syncs(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=2))
        await syncer.now([info])
        contest_id = (await record(repo, 'abc478')).contest_id
        await repo.set_time(contest_id, at(days=2, hours=1), None, by=ADMIN, now=NOW)

        reports = [await syncer.later([info]) for _ in range(3)]

        assert not any(report.changed for report in reports)
        row = await stored(repo, 'abc478')
        assert (row.start, row.end, row.source_start) == (
            at(days=2, hours=1),
            at(days=2, hours=1) + DURATION,
            at(days=2),
        )
        assert (row.overridden, row.revision) == (True, 1)

    async def test_a_move_that_an_admins_time_hides_is_no_new_revision(
        self, syncer: Syncer, repo: ContestRepo, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)
        await syncer.now([contest('abc478', at(days=2))])
        await repo.set_time(1, at(days=2, hours=1), None, by=ADMIN, now=NOW)

        report = await syncer.later([contest('abc478', at(days=3))])
        again = await syncer.later([contest('abc478', at(days=3))])

        assert (report.updated, report.moved) == (1, 0)
        assert not again.changed
        row = await stored(repo, 'abc478')
        assert (row.start, row.source_start, row.revision) == (
            at(days=2, hours=1),
            at(days=3),
            1,
        )
        # Members are still told the admin's time, but admins hear of the
        # move, once: the second sync's report is the first's.
        [warning] = warned(caplog)
        assert f'(atcoder:abc478) moved on its site to {at(days=3)}, ' in warning
        assert f'the time an admin set, {at(days=2, hours=1)}: ' in warning

    async def test_a_site_moving_a_dated_contest_from_an_admins_time_warns(
        self,
        syncer: Syncer,
        icpc: FakeSource,
        repo: ContestRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)
        await syncer.now([dated('9584')], icpc)
        contest_id = (await record(repo, '9584', ICPC)).contest_id
        admins = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
        await repo.set_time(contest_id, admins, None, by=ADMIN, now=NOW)
        week_later = date(2026, 10, 24)

        report = await syncer.later([dated('9584', week_later)], icpc)
        await syncer.later([dated('9584', week_later)], icpc)

        assert (report.updated, report.moved) == (1, 0)
        row = await stored(repo, '9584', ICPC)
        assert (row.start, row.start_date, row.revision) == (admins, week_later, 1)
        assert warned(caplog) == [
            f'Contest Regional 9584 (icpc:9584) moved on its site to {week_later}, '
            f'but members are still told the time an admin set, {admins}: change '
            f'it with /kcpc contests settime {contest_id}'
        ]

    async def test_a_site_moving_a_contest_to_an_admins_time_or_without_one_is_quiet(
        self,
        syncer: Syncer,
        icpc: FakeSource,
        repo: ContestRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)
        await syncer.now([contest('abc478', at(days=2)), contest('abc479', at(days=4))])
        await syncer.now([dated('9584')], icpc)
        admins_day = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
        for external_id, platform, start in [
            ('abc478', ATCODER, at(days=3)),
            ('9584', ICPC, admins_day),
        ]:
            contest_id = (await record(repo, external_id, platform)).contest_id
            await repo.set_time(contest_id, start, None, by=ADMIN, now=NOW)

        moved = await syncer.later(
            [contest('abc478', at(days=3)), contest('abc479', at(days=5))]
        )
        await syncer.now([dated('9584', admins_day.date())], icpc)

        assert (moved.updated, moved.moved) == (1, 1)
        assert warned(caplog) == []

    async def test_an_admin_changing_the_start_is_a_new_revision(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=2))
        await syncer.now([info])

        await repo.set_time(1, at(days=2), at(days=2, hours=3), by=ADMIN, now=NOW)
        assert (await stored(repo, 'abc478')).revision == 0  # the same start
        await syncer.later([info])
        await repo.set_time(1, at(days=3), None, by=ADMIN, now=at(minutes=10))
        await syncer.later([info])

        row = await stored(repo, 'abc478')
        assert (row.start, row.end, row.revision) == (
            at(days=3),
            at(days=3, hours=3),
            1,
        )

    async def test_a_contest_started_by_an_admins_time_is_never_missed(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        await syncer.now([contest('abc478', at(days=2))])
        await repo.set_time(1, at(minutes=5), None, by=ADMIN, now=NOW)

        reports = [await syncer.later([]) for _ in range(5)]

        assert not any(report.changed for report in reports)
        assert misses(await stored(repo, 'abc478')) == (0, ContestStatus.SCHEDULED, 1)

    async def test_an_admins_time_for_a_dated_contest_outlasts_a_source_time(
        self, syncer: Syncer, icpc: FakeSource, repo: ContestRepo
    ) -> None:
        await syncer.now([dated('9584')], icpc)
        admins = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
        await repo.set_time(1, admins, admins + timedelta(hours=5), by=ADMIN, now=NOW)
        sources = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)

        report = await syncer.later([replace(dated('9584'), start=sources)], icpc)

        assert (report.updated, report.moved) == (1, 0)
        row = await stored(repo, '9584', ICPC)
        assert (row.start, row.source_start, row.revision) == (admins, sources, 1)


class TestHealthCheck:
    async def test_a_list_that_lost_over_half_fails_but_what_it_lists_applies(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        contests = upcoming(6)
        await syncer.now(contests)
        moved = contest('abc1', at(days=1, hours=2))

        report = await syncer.later([moved, contest('abc999', at(days=20))])

        error = 'contest list shrank from 6 to 2 upcoming contests'
        assert report == SyncReport(
            ATCODER, ok=False, added=1, moved=1, future_count=2, error=error
        )
        row = await stored(repo, 'abc1')
        assert (row.start, row.revision, row.last_synced) == (
            moved.start,
            1,
            at(minutes=10),
        )
        assert (await stored(repo, 'abc999')).first_seen == at(minutes=10)
        for info in contests[1:]:  # missed once, nothing more yet
            row = await stored(repo, info.external_id)
            assert misses(row) == (1, ContestStatus.SCHEDULED, 0)
        assert await state_of(repo) == SourceState(
            source=ATCODER,
            last_attempt=at(minutes=10),
            last_ok=NOW,
            last_future_count=6,
            consecutive_failures=1,
            last_error=error,
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
        contests = upcoming(before)
        await syncer.now(contests)

        report = await syncer.later(contests[:after])

        assert report.ok is accepted

    async def test_a_real_bulk_cancellation_is_believed_within_the_hour(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        # 6 of 8 upcoming contests go, and another is announced.
        contests = upcoming(8)
        await syncer.now(contests)
        kept = [*contests[:2], contest('abc999', at(days=1, hours=6))]

        reports = [await syncer.later(kept) for _ in range(6)]

        # Every list fails the check, but the new contest is added at once,
        # and the missing ones are cancelled on the sixth miss, an hour on.
        assert [report.ok for report in reports] == [False] * 6
        assert [report.added for report in reports] == [1, 0, 0, 0, 0, 0]
        assert [report.cancelled for report in reports] == [0, 0, 0, 0, 0, 6]
        for info in contests[2:]:
            row = await stored(repo, info.external_id)
            assert misses(row) == (6, ContestStatus.CANCELLED, 1)

        # They no longer count as upcoming, so the list is healthy again.
        report = await syncer.later(kept)

        assert (report.ok, report.changed, report.future_count) == (True, False, 3)
        state = await state_of(repo)
        assert (state.last_future_count, state.consecutive_failures) == (3, 0)

    async def test_a_shrunken_list_for_under_an_hour_cancels_nothing(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        contests = upcoming(6)
        await syncer.now(contests)
        for _ in range(5):  # a glitch at AtCoder, for 50 minutes
            report = await syncer.later(contests[:2])
            assert (report.ok, report.changed) == (False, False)

        report = await syncer.later(contests)

        assert (report.ok, report.changed) == (True, False)
        for info in contests:
            row = await stored(repo, info.external_id)
            assert misses(row) == (0, ContestStatus.SCHEDULED, 0)

    async def test_contests_that_have_started_since_are_not_expected(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        # Four contests all starting at 12:05: by 12:10 none is upcoming.
        at_once = [contest(f'abc{n}', at(minutes=5)) for n in range(4)]
        await syncer.now(at_once)

        report = await syncer.later([])

        assert (report.ok, report.future_count) == (True, 0)
        assert (await state_of(repo)).last_future_count == 0

    async def test_a_list_of_only_some_contests_is_not_checked(
        self, syncer: Syncer, icpc: FakeSource, repo: ContestRepo
    ) -> None:
        regionals = [dated(str(9580 + n)) for n in range(6)]
        await syncer.now(regionals, icpc)

        report = await syncer.later(regionals[:1], icpc)

        assert (report.ok, report.future_count) == (True, 1)
        assert (await state_of(repo, ICPC)).last_future_count == 1


class TestFailures:
    async def test_a_failed_fetch_is_reported_and_recorded(
        self, syncer: Syncer, source: FakeSource, repo: ContestRepo, clock: FakeClock
    ) -> None:
        await syncer.now(upcoming(2))
        before = await repo.source_records(ATCODER)
        source.fail(ExternalServiceError('AtCoder', UNREACHABLE))
        await clock.advance(SYNC_INTERVAL)

        report = await syncer.now()

        assert report == SyncReport(ATCODER, ok=False, error=UNREACHABLE, applied=False)
        assert await repo.source_records(ATCODER) == before
        assert await state_of(repo) == SourceState(
            source=ATCODER,
            last_attempt=at(minutes=10),
            last_ok=NOW,
            last_future_count=2,
            consecutive_failures=1,
            last_error=UNREACHABLE,
        )

    async def test_failures_in_a_row_are_counted_until_a_success(
        self, syncer: Syncer, source: FakeSource, repo: ContestRepo
    ) -> None:
        contests = upcoming(6)
        await syncer.now(contests)
        source.fail(ExternalServiceError('AtCoder', UNREACHABLE))
        await syncer.later()
        await syncer.later()
        await syncer.later(contests[:1])  # served but unhealthy: a failure too
        assert (await state_of(repo)).consecutive_failures == 3

        await syncer.later(contests)

        state = await state_of(repo)
        assert (state.consecutive_failures, state.last_error) == (0, None)
        assert state.last_attempt == state.last_ok == at(minutes=40)

    async def test_an_unexpected_error_propagates_and_changes_nothing(
        self, syncer: Syncer, source: FakeSource, repo: ContestRepo
    ) -> None:
        source.fail(RuntimeError('a bug'))

        with pytest.raises(RuntimeError, match='a bug'):
            await syncer.now()

        assert await repo.source_state(ATCODER) is None

    async def test_a_contest_of_another_platform_is_a_bug(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        stray = contest('2145', at(days=1), platform=CODEFORCES)

        with pytest.raises(ValueError, match='atcoder source listed codeforces'):
            await syncer.now([*upcoming(1), stray])

        assert await repo.source_records(ATCODER) == []
        assert await repo.source_records(CODEFORCES) == []
        assert await repo.source_state(ATCODER) is None

    async def test_a_failure_while_applying_writes_nothing(
        self, syncer: Syncer, repo: ContestRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await syncer.now(upcoming(1))
        before = (await repo.source_records(ATCODER), await repo.source_state(ATCODER))

        async def fail_to_save(records: Sequence[SourceRecord]) -> None:
            raise RuntimeError('disk full')

        monkeypatch.setattr(repo, 'save', fail_to_save)
        with pytest.raises(RuntimeError, match='disk full'):
            await syncer.later(upcoming(2))

        after = (await repo.source_records(ATCODER), await repo.source_state(ATCODER))
        assert after == before  # abc2's insert was rolled back too


class TestLogging:
    async def test_the_third_failure_in_a_row_warns_once_a_streak(
        self,
        syncer: Syncer,
        source: FakeSource,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        contests = upcoming(6)
        await syncer.now(contests)
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)

        source.fail(ExternalServiceError('AtCoder', UNREACHABLE))
        await syncer.later()
        await syncer.later()
        await syncer.later(contests[:1])  # rejected as unhealthy
        await syncer.later(contests)  # a success ends the streak
        source.fail(ExternalServiceError('AtCoder', UNREACHABLE))
        for _ in range(4):
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
            logging.WARNING,
            logging.INFO,
        ]
        assert 'contest list shrank from 6 to 1 upcoming contests' in failures[2][1]
        assert 'contest source atcoder (consecutive failures: 3)' in failures[2][1]
        assert any(
            'synced again after 3 failed attempts' in record.getMessage()
            for record in caplog.records
        )

    async def test_syncs_that_change_something_are_logged_at_info(
        self, syncer: Syncer, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=SYNC_LOGGER)
        await syncer.now(upcoming(1))
        await syncer.later(upcoming(1))

        synced = [
            record.levelno
            for record in caplog.records
            if record.name == SYNC_LOGGER
            and 'Synced contest source atcoder' in record.getMessage()
        ]
        assert synced == [logging.INFO, logging.DEBUG]

    async def test_why_a_list_could_not_be_read_is_logged_not_reported(
        self,
        syncer: Syncer,
        source: FakeSource,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        unreadable = ExternalServiceError(
            'AtCoder', "AtCoder's contest list could not be read."
        )
        unreadable.__cause__ = ValueError('no contest table')
        source.fail(unreadable)
        caplog.set_level(logging.INFO, logger=SYNC_LOGGER)

        report = await syncer.now()

        assert report.error == "AtCoder's contest list could not be read."
        assert not (report.ok or report.applied or report.changed)
        [logged] = [r for r in caplog.records if r.name == SYNC_LOGGER]
        assert 'ValueError: no contest table' in logged.getMessage()


class TestRobustness:
    async def test_a_list_naming_a_contest_twice_keeps_the_first(
        self, syncer: Syncer, repo: ContestRepo
    ) -> None:
        first = contest('abc478', at(days=1))

        report = await syncer.now([first, contest('abc478', at(days=2))])

        assert (report.added, report.future_count) == (1, 1)
        assert (await record(repo, 'abc478')).reported == first

    async def test_two_syncs_at_once_apply_one_after_the_other(
        self, sync: ContestSync, source: FakeSource, repo: ContestRepo
    ) -> None:
        source.serve(upcoming(2))

        reports = await asyncio.gather(sync.sync(source), sync.sync(source))

        assert sorted(report.added for report in reports) == [0, 2]
        assert len(await repo.source_records(ATCODER)) == 2

    async def test_syncs_of_one_source_take_turns(
        self, db: Database, repo: ContestRepo, clock: FakeClock
    ) -> None:
        # Otherwise the first sync, whose fetch is slow, would apply its list
        # after the second's newer one and move the contest back.
        source = SlowSource()
        sync = ContestSync(db, repo, clock)
        source.serve([contest('abc478', at(days=3))])
        first = asyncio.create_task(sync.sync(source))
        await clock.settle()
        source.serve([contest('abc478', at(days=4))])
        second = asyncio.create_task(sync.sync(source))
        await clock.settle()
        fetched_while_the_first_was_slow = source.fetches

        source.release.set()
        reports = await asyncio.gather(first, second)

        assert fetched_while_the_first_was_slow == 1
        assert [(report.added, report.moved) for report in reports] == [(1, 0), (0, 1)]
        assert (await stored(repo, 'abc478')).start == at(days=4)

    async def test_other_sources_do_not_wait_their_turn(
        self, db: Database, repo: ContestRepo, clock: FakeClock
    ) -> None:
        slow = SlowSource()
        sync = ContestSync(db, repo, clock)
        slow.serve(upcoming(1))
        codeforces = FakeSource(CODEFORCES)
        codeforces.serve([contest('2145', at(days=1), platform=CODEFORCES)])
        held = asyncio.create_task(sync.sync(slow))
        await clock.settle()

        try:
            other = await asyncio.wait_for(sync.sync(codeforces), timeout=5)
        finally:
            slow.release.set()

        assert (other.added, (await held).added) == (1, 1)

    async def test_an_admins_time_set_while_a_sync_fetches_keeps_its_revision(
        self, syncer: Syncer, sync: ContestSync, repo: ContestRepo, clock: FakeClock
    ) -> None:
        # A sync writes revisions back whole, so it must read them after its
        # fetch, in its transaction. Read before, they would undo the admin's
        # new revision, and members reminded of the old start would hear
        # nothing of the new one.
        info = contest('abc478', at(days=2))
        await syncer.now([info])
        contest_id = (await record(repo, 'abc478')).contest_id
        slow = SlowSource()
        slow.serve([info])
        held = asyncio.create_task(sync.sync(slow))
        await clock.settle()
        try:
            fetching = (slow.fetches, held.done())
            await repo.set_time(contest_id, at(days=3), None, by=ADMIN, now=NOW)
        finally:
            slow.release.set()
        report = await held

        assert fetching == (1, False)
        assert not report.changed
        row = await stored(repo, 'abc478')
        assert (row.revision, row.start, row.source_start, row.overridden) == (
            1,
            at(days=3),
            at(days=2),
            True,
        )

    async def test_refuses_to_run_inside_a_transaction(
        self, sync: ContestSync, source: FakeSource, db: Database, repo: ContestRepo
    ) -> None:
        source.serve(upcoming(1))

        async with db.transaction():
            with pytest.raises(RuntimeError, match='outside any database transaction'):
                await sync.sync(source)

        assert source.fetches == 0
        assert await repo.source_state(ATCODER) is None
