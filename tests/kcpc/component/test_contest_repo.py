"""Component tests for the contests feature's ContestRepo and settings, on kcpc.db."""

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.settings import (
    FeatureRegistry,
    FeatureSettings,
    FeatureSpec,
    GuildSettingsRepo,
)
from tle.kcpc.core.timeutil import to_epoch, zone
from tle.kcpc.features.contests.repo import (
    ContestInfo,
    ContestRepo,
    ContestStatus,
    SourceRecord,
    SourceState,
    StoredContest,
)
from tle.kcpc.features.contests.settings import (
    CONTESTS,
    MANUAL,
    PLATFORMS,
    SPEC,
    ContestSettings,
    contest_settings,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)
DURATION = timedelta(minutes=100)
LONDON = zone('Europe/London')
UKIEPC_DAY = date(2026, 10, 17)
ADMIN = 'admin-1234'
ONLY_MANUAL = 'Only contests added with /kcpc contests add can be removed.'


def at(**offset: float) -> datetime:
    """``NOW`` moved by ``offset``, e.g. ``at(days=7)``."""
    return NOW + timedelta(**offset)


def contest(
    external_id: str, start: datetime, *, platform: str = 'atcoder', **changes: Any
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


def dated(external_id: str, day: date = UKIEPC_DAY) -> ContestInfo:
    """An ICPC contest known only by its date."""
    return ContestInfo(
        platform='icpc',
        external_id=external_id,
        name=f'Regional {external_id}',
        start=None,
        start_date=day,
        end=None,
        url='https://icpc.global/',
    )


@pytest.fixture
def repo(db: Database) -> ContestRepo:
    return ContestRepo(db)


async def record_of(repo: ContestRepo, info: ContestInfo) -> SourceRecord:
    [record] = [
        record
        for record in await repo.source_records(info.platform)
        if record.reported.external_id == info.external_id
    ]
    return record


async def stored(repo: ContestRepo, info: ContestInfo) -> StoredContest:
    contest = await repo.by_id((await record_of(repo, info)).contest_id)
    assert contest is not None
    return contest


async def cancel(repo: ContestRepo, info: ContestInfo) -> None:
    record = await record_of(repo, info)
    await repo.save([replace(record, status=ContestStatus.CANCELLED)])


def ids(contests: list[StoredContest]) -> list[str]:
    return [contest.external_id for contest in contests]


class TestContestInfo:
    def test_times_are_converted_to_whole_seconds_in_utc(self) -> None:
        tokyo = zone('Asia/Tokyo')
        start = datetime(2026, 10, 3, 21, 0, 0, 999_999, tzinfo=tokyo)

        info = contest('abc478', start)

        assert info.start == datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
        assert info.end == info.start + DURATION
        assert info.start.tzinfo is UTC
        assert info == contest('abc478', datetime(2026, 10, 3, 12, 0, tzinfo=UTC))

    @pytest.mark.parametrize(
        ('changes', 'message'),
        [
            ({'start': None}, 'atcoder:abc478 needs a start or a start date'),
            (
                {'start': None, 'start_date': UKIEPC_DAY},
                'atcoder:abc478 has an end but no start',
            ),
            ({'end': NOW}, 'atcoder:abc478 must end after it starts'),
            ({'end': NOW - SECOND}, 'atcoder:abc478 must end after it starts'),
            ({'start': datetime(2026, 10, 1, 12, 0)}, 'timezone-aware'),
        ],
        ids=[
            'no-start-or-date',
            'end-without-start',
            'no-length',
            'ends-first',
            'naive',
        ],
    )
    def test_an_impossible_contest_is_refused(
        self, changes: dict[str, Any], message: str
    ) -> None:
        info = contest('abc478', NOW)

        with pytest.raises(ValueError, match=message):
            replace(info, **changes)

    def test_a_contest_known_only_by_its_date_has_no_times(self) -> None:
        info = dated('9584')

        assert (info.start, info.start_date, info.end) == (None, UKIEPC_DAY, None)

    @pytest.mark.parametrize(
        'changes',
        [
            {'name': 'Contest abc479'},
            {'start': at(minutes=1), 'end': at(minutes=1) + DURATION},
            {'start_date': UKIEPC_DAY},
            {'end': NOW + DURATION + SECOND},
            {'end': None},
            {'url': None},
            {'url': ''},
        ],
        ids=['name', 'start', 'date', 'end', 'no-end', 'no-url', 'empty-url'],
    )
    def test_the_fingerprint_changes_with_what_members_see(
        self, changes: dict[str, Any]
    ) -> None:
        info = contest('abc478', NOW)

        assert replace(info, **changes).fingerprint() != info.fingerprint()

    def test_the_fingerprint_is_unambiguous_and_stable(self) -> None:
        info = contest('abc478', NOW)
        moved_text = replace(info, name=f'{info.name}h', url='ttps://example.com/x')

        assert info.fingerprint() == contest('abc478', NOW).fingerprint()
        assert (
            moved_text.fingerprint()
            != replace(info, url='https://example.com/x').fingerprint()
        )
        assert (
            replace(info, url=None).fingerprint() != replace(info, url='').fingerprint()
        )
        assert len(info.fingerprint()) == 40


class TestStoredContest:
    def test_properties(self) -> None:
        stored = StoredContest(
            contest_id=7,
            platform='icpc',
            external_id='9584',
            name='UKIEPC',
            start=None,
            start_date=UKIEPC_DAY,
            end=None,
            url=None,
            status=ContestStatus.SCHEDULED,
            revision=0,
            miss_count=0,
            first_seen=NOW,
            last_synced=NOW,
            source_start=None,
            overridden=False,
        )

        assert (stored.key, stored.cancelled, stored.time_confirmed) == (
            'icpc:9584',
            False,
            False,
        )
        confirmed = replace(stored, start=NOW, status=ContestStatus.CANCELLED)
        assert (confirmed.cancelled, confirmed.time_confirmed) == (True, True)


class TestAddAndRead:
    async def test_an_added_contest_reads_back_with_every_column(
        self, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=2))

        await repo.add([info], now=NOW)

        assert await repo.by_id(1) == StoredContest(
            contest_id=1,
            platform='atcoder',
            external_id='abc478',
            name='Contest abc478',
            start=at(days=2),
            start_date=None,
            end=at(days=2) + DURATION,
            url='https://example.com/contests/abc478',
            status=ContestStatus.SCHEDULED,
            revision=0,
            miss_count=0,
            first_seen=NOW,
            last_synced=NOW,
            source_start=at(days=2),
            overridden=False,
        )
        assert await repo.source_records('atcoder') == [
            SourceRecord(
                contest_id=1,
                reported=info,
                status=ContestStatus.SCHEDULED,
                revision=0,
                miss_count=0,
                last_synced=NOW,
                override_start=None,
            )
        ]

    async def test_a_contest_known_only_by_its_date_reads_back(
        self, repo: ContestRepo
    ) -> None:
        await repo.add([dated('9584')], now=NOW)

        stored = await repo.by_id(1)

        assert stored is not None
        assert (stored.start, stored.start_date, stored.end) == (None, UKIEPC_DAY, None)
        assert not stored.time_confirmed
        assert (await record_of(repo, dated('9584'))).reported == dated('9584')

    async def test_times_are_epoch_seconds_and_dates_iso_text(
        self, repo: ContestRepo, db: Database
    ) -> None:
        info = contest('abc478', at(days=2))
        await repo.add([info, dated('9584')], now=NOW)

        rows = await db.fetchall(
            'SELECT start_time, start_date, end_time, fingerprint, first_seen, '
            'last_synced FROM contest ORDER BY contest_id'
        )

        assert [tuple(row) for row in rows] == [
            (
                to_epoch(at(days=2)),
                None,
                to_epoch(at(days=2) + DURATION),
                info.fingerprint(),
                to_epoch(NOW),
                to_epoch(NOW),
            ),
            (
                None,
                '2026-10-17',
                None,
                dated('9584').fingerprint(),
                to_epoch(NOW),
                to_epoch(NOW),
            ),
        ]

    async def test_adding_a_stored_contest_again_fails_and_adds_nothing(
        self, repo: ContestRepo
    ) -> None:
        await repo.add([contest('abc478', at(days=2))], now=NOW)

        with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
            await repo.add(
                [contest('abc479', at(days=9)), contest('abc478', at(days=3))],
                now=NOW,
            )

        assert [
            r.reported.external_id for r in await repo.source_records('atcoder')
        ] == ['abc478']

    async def test_one_id_on_two_platforms_is_two_contests(
        self, repo: ContestRepo
    ) -> None:
        await repo.add(
            [contest('1', at(days=1)), contest('1', at(days=2), platform='codeforces')],
            now=NOW,
        )

        assert [r.reported.start for r in await repo.source_records('atcoder')] == [
            at(days=1)
        ]
        assert [r.reported.start for r in await repo.source_records('codeforces')] == [
            at(days=2)
        ]
        assert await repo.source_records('icpc') == []

    async def test_an_unknown_contest_is_none(self, repo: ContestRepo) -> None:
        assert await repo.by_id(1) is None

    async def test_save_writes_back_all_but_the_identity_and_first_seen(
        self, repo: ContestRepo, db: Database
    ) -> None:
        await repo.add([contest('abc478', at(days=2)), dated('9584')], now=NOW)
        record = await record_of(repo, contest('abc478', NOW))
        reported = contest('abc478', at(days=3), name='ABC 478', url=None)

        await repo.save(
            [
                replace(
                    record,
                    reported=reported,
                    status=ContestStatus.CANCELLED,
                    revision=4,
                    miss_count=2,
                    last_synced=at(hours=1),
                )
            ]
        )

        stored = await repo.by_id(record.contest_id)
        assert stored is not None
        assert (stored.name, stored.start, stored.end, stored.url) == (
            'ABC 478',
            at(days=3),
            at(days=3) + DURATION,
            None,
        )
        assert (stored.status, stored.revision, stored.miss_count) == (
            ContestStatus.CANCELLED,
            4,
            2,
        )
        assert (stored.first_seen, stored.last_synced) == (NOW, at(hours=1))
        assert (await record_of(repo, reported)).reported == reported
        assert (
            await db.fetchval(
                'SELECT fingerprint FROM contest WHERE contest_id = ?',
                (record.contest_id,),
            )
            == reported.fingerprint()
        )
        # The other contest is untouched.
        assert (await record_of(repo, dated('9584'))).reported == dated('9584')


class TestDayStart:
    @pytest.mark.parametrize(
        ('tz', 'day', 'expected'),
        [
            (None, UKIEPC_DAY, datetime(2026, 10, 17, 0, 0, tzinfo=UTC)),
            ('Europe/London', UKIEPC_DAY, datetime(2026, 10, 16, 23, 0, tzinfo=UTC)),
            (
                'Europe/London',
                date(2026, 12, 5),
                datetime(2026, 12, 5, 0, 0, tzinfo=UTC),
            ),
            # The clocks skip from midnight to 01:00 (CDT): the day starts then.
            (
                'America/Havana',
                date(2026, 3, 8),
                datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            ),
            # 00:00 to 01:00 happens twice: the first midnight (CDT) counts.
            (
                'America/Havana',
                date(2026, 11, 1),
                datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            ),
        ],
        ids=['utc-by-default', 'bst', 'gmt', 'skipped-midnight', 'repeated-midnight'],
    )
    async def test_is_midnight_in_the_clubs_time_zone(
        self, db: Database, tz: str | None, day: date, expected: datetime
    ) -> None:
        repo = ContestRepo(db) if tz is None else ContestRepo(db, tz=zone(tz))

        assert repo.day_start(day) == expected
        assert repo.day_start(day).tzinfo is UTC


class TestUpcoming:
    async def test_is_scheduled_contests_from_now_and_running_ones_by_start(
        self, repo: ContestRepo
    ) -> None:
        await repo.add(
            [
                contest('later', at(days=2)),
                contest('ended', at(hours=-3)),
                contest('running', at(minutes=-30)),
                contest('no-end', at(hours=-1), end=None),
                contest('now', NOW),
                contest('cancelled', at(days=1)),
                contest('soon', at(hours=1)),
                contest('also-soon', at(hours=1), platform='codeforces'),
            ],
            now=NOW,
        )
        await cancel(repo, contest('cancelled', NOW))

        upcoming = await repo.upcoming(NOW, platforms=PLATFORMS, limit=10)

        assert ids(upcoming) == ['running', 'now', 'soon', 'also-soon', 'later']
        assert ids(await repo.upcoming(NOW, platforms=PLATFORMS, limit=2)) == [
            'running',
            'now',
        ]

    async def test_compares_exactly_in_whole_seconds(self, repo: ContestRepo) -> None:
        await repo.add([contest('a', NOW, end=NOW + SECOND)], now=NOW)

        assert ids(await repo.upcoming(NOW, platforms=PLATFORMS, limit=5)) == ['a']
        # Started half a second ago, but still running.
        later = NOW + timedelta(milliseconds=500)
        assert ids(await repo.upcoming(later, platforms=PLATFORMS, limit=5)) == ['a']
        assert await repo.upcoming(NOW + SECOND, platforms=PLATFORMS, limit=5) == []

    async def test_a_contest_known_only_by_its_date_comes_where_its_day_starts(
        self, db: Database
    ) -> None:
        repo = ContestRepo(db, tz=LONDON)  # the day starts at 23:00 UTC
        day_start = datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
        await repo.add(
            [
                contest('before', day_start - SECOND),
                dated('9584'),
                contest('after', day_start + SECOND, platform='codeforces'),
            ],
            now=NOW,
        )

        upcoming = await repo.upcoming(NOW, platforms=PLATFORMS, limit=5)

        assert ids(upcoming) == ['before', '9584', 'after']

    @pytest.mark.parametrize(
        ('now', 'listed'),
        [
            (datetime(2026, 10, 16, 22, 59, 59, tzinfo=UTC), True),
            (datetime(2026, 10, 16, 23, 0, 0, tzinfo=UTC), True),
            (datetime(2026, 10, 16, 23, 0, 0, 1, tzinfo=UTC), False),
            (datetime(2026, 10, 17, 12, 0, tzinfo=UTC), False),
        ],
        ids=['just-before', 'as-it-starts', 'just-after', 'that-noon'],
    )
    async def test_a_contest_known_only_by_its_date_is_upcoming_until_its_day(
        self, db: Database, now: datetime, listed: bool
    ) -> None:
        repo = ContestRepo(db, tz=LONDON)
        await repo.add([dated('9584')], now=NOW)

        upcoming = await repo.upcoming(now, platforms=PLATFORMS, limit=5)

        assert ids(upcoming) == (['9584'] if listed else [])

    async def test_has_only_the_platforms_asked_for(self, repo: ContestRepo) -> None:
        await repo.add(
            [
                contest('a', at(days=1)),
                contest('c', at(days=2), platform='codeforces'),
                dated('9584'),
            ],
            now=NOW,
        )

        assert ids(
            await repo.upcoming(NOW, platforms=('codeforces', 'icpc'), limit=5)
        ) == ['c', '9584']
        assert await repo.upcoming(NOW, platforms=(), limit=5) == []
        assert await repo.upcoming(NOW, platforms=PLATFORMS, limit=0) == []
        with pytest.raises(TypeError, match='collection of platforms'):
            await repo.upcoming(NOW, platforms='atcoder', limit=5)


class TestLive:
    async def test_is_scheduled_contests_running_now_by_start(
        self, repo: ContestRepo
    ) -> None:
        await repo.add(
            [
                contest('b', at(minutes=-10)),
                contest('a', at(minutes=-90)),
                contest('starts-now', NOW),
                contest('ends-now', NOW - DURATION),
                contest('no-end', at(minutes=-5), end=None),
                contest('cancelled', at(minutes=-20)),
                contest('soon', at(minutes=1)),
                contest('cf', at(minutes=-1), platform='codeforces'),
                dated('9584', date(2026, 10, 1)),
            ],
            now=NOW,
        )
        await cancel(repo, contest('cancelled', NOW))

        live = await repo.live(NOW, platforms=('atcoder', 'icpc'))

        assert ids(live) == ['a', 'b', 'starts-now']
        assert ids(await repo.live(NOW, platforms=PLATFORMS)) == [
            'a',
            'b',
            'cf',
            'starts-now',
        ]
        assert await repo.live(NOW, platforms=()) == []

    async def test_compares_exactly_in_whole_seconds(self, repo: ContestRepo) -> None:
        await repo.add([contest('a', NOW, end=NOW + SECOND)], now=NOW)

        assert ids(await repo.live(NOW - MICROSECOND, platforms=PLATFORMS)) == []
        assert ids(await repo.live(NOW + MICROSECOND, platforms=PLATFORMS)) == ['a']
        later = NOW + SECOND - MICROSECOND
        assert ids(await repo.live(later, platforms=PLATFORMS)) == ['a']
        assert ids(await repo.live(NOW + SECOND, platforms=PLATFORMS)) == []


class TestBetween:
    @pytest.fixture
    async def added(self, repo: ContestRepo) -> None:
        await repo.add(
            [
                contest('c', at(hours=2)),
                contest('a', at(hours=1)),
                contest('b', at(hours=1), platform='codeforces'),
                contest('cancelled', at(hours=1, minutes=30)),
                contest('edge', at(hours=3)),
                dated('9584', date(2026, 10, 1)),
            ],
            now=NOW,
        )
        await cancel(repo, contest('cancelled', NOW))

    @pytest.mark.usefixtures('added')
    async def test_is_confirmed_contests_from_start_inclusive_to_end_exclusive(
        self, repo: ContestRepo
    ) -> None:
        found = await repo.between(at(hours=1), at(hours=3), platforms=PLATFORMS)

        assert ids(found) == ['a', 'b', 'c']
        assert ids(
            await repo.between(
                at(hours=1, microseconds=1),
                at(hours=3, microseconds=1),
                platforms=PLATFORMS,
            )
        ) == ['c', 'edge']

    @pytest.mark.usefixtures('added')
    async def test_leaves_out_cancelled_contests_unless_asked(
        self, repo: ContestRepo
    ) -> None:
        found = await repo.between(
            NOW, at(days=1), platforms=('atcoder',), include_cancelled=True
        )

        assert ids(found) == ['a', 'cancelled', 'c', 'edge']
        assert found[1].cancelled
        assert await repo.between(NOW, at(days=1), platforms=()) == []


class TestSetTime:
    async def test_the_times_an_admin_sets_are_what_every_read_gives(
        self, repo: ContestRepo
    ) -> None:
        info = contest('abc478', at(days=2))
        await repo.add([info], now=NOW)

        set_to = await repo.set_time(
            1, at(hours=1), at(hours=3), by=ADMIN, now=at(minutes=5)
        )

        assert set_to == await stored(repo, info)
        assert (set_to.start, set_to.end, set_to.source_start) == (
            at(hours=1),
            at(hours=3),
            at(days=2),
        )
        assert (set_to.overridden, set_to.revision) == (True, 1)
        assert ids(await repo.between(NOW, at(hours=2), platforms=PLATFORMS)) == [
            'abc478'
        ]
        assert ids(await repo.live(at(hours=2), platforms=PLATFORMS)) == ['abc478']
        assert ids(await repo.upcoming(at(hours=4), platforms=PLATFORMS, limit=5)) == []
        record = await record_of(repo, info)
        assert (record.reported, record.override_start) == (info, at(hours=1))
        assert record.effective_start == at(hours=1)

    async def test_the_override_records_who_set_it_and_when(
        self, repo: ContestRepo, db: Database
    ) -> None:
        await repo.add([contest('abc478', at(days=2))], now=NOW)

        await repo.set_time(1, at(days=1), at(days=1, hours=2), by=ADMIN, now=NOW)
        await repo.set_time(1, at(days=1), None, by='admin-5678', now=at(hours=1))

        rows = await db.fetchall('SELECT * FROM contest_override')
        assert [tuple(row) for row in rows] == [
            (
                'atcoder',
                'abc478',
                to_epoch(at(days=1)),
                to_epoch(at(days=1, hours=2)),
                'admin-5678',
                to_epoch(at(hours=1)),
            )
        ]

    async def test_only_a_new_start_is_a_new_revision(self, repo: ContestRepo) -> None:
        await repo.add([contest('abc478', at(days=2))], now=NOW)

        same_start = await repo.set_time(
            1, at(days=2), at(days=2, hours=3), by=ADMIN, now=NOW
        )
        moved = await repo.set_time(1, at(days=3), None, by=ADMIN, now=NOW)
        moved_again = await repo.set_time(1, at(days=4), None, by=ADMIN, now=NOW)
        set_back = await repo.set_time(1, at(days=2), None, by=ADMIN, now=NOW)

        assert [c.revision for c in (same_start, moved, moved_again, set_back)] == [
            0,
            1,
            2,
            3,
        ]
        assert same_start.end == at(days=2, hours=3)

    async def test_without_an_end_the_contest_keeps_its_duration(
        self, repo: ContestRepo
    ) -> None:
        await repo.add([contest('abc478', at(days=2))], now=NOW)

        kept = await repo.set_time(1, at(days=3), None, by=ADMIN, now=NOW)
        set_end = await repo.set_time(
            1, at(days=3), at(days=3, hours=5), by=ADMIN, now=NOW
        )
        kept_again = await repo.set_time(1, at(days=4), None, by=ADMIN, now=NOW)

        assert kept.end == at(days=3) + DURATION
        assert set_end.end == at(days=3, hours=5)
        assert kept_again.end == at(days=4, hours=5)  # the duration an admin set

    async def test_a_contest_known_only_by_its_date_gets_a_time(
        self, repo: ContestRepo
    ) -> None:
        await repo.add([dated('9584')], now=NOW)
        start = datetime(2026, 10, 17, 9, 0, 30, 500_000, tzinfo=LONDON)

        no_end = await repo.set_time(1, start, None, by=ADMIN, now=NOW)
        with_end = await repo.set_time(
            1, start, start + timedelta(hours=5), by=ADMIN, now=NOW
        )

        expected_start = datetime(2026, 10, 17, 8, 0, 30, tzinfo=UTC)
        assert (no_end.start, no_end.end, no_end.revision) == (expected_start, None, 1)
        assert no_end.time_confirmed
        assert (no_end.start_date, no_end.source_start) == (UKIEPC_DAY, None)
        assert (with_end.end, with_end.revision) == (
            expected_start + timedelta(hours=5),
            1,
        )
        upcoming = await repo.upcoming(NOW, platforms=PLATFORMS, limit=5)
        assert [c.start for c in upcoming] == [expected_start]

    @pytest.mark.parametrize('end', [NOW, NOW - SECOND], ids=['no-length', 'early'])
    async def test_an_end_not_after_the_start_is_refused(
        self, repo: ContestRepo, end: datetime
    ) -> None:
        await repo.add([contest('abc478', at(days=2))], now=NOW)
        before = await repo.by_id(1)

        with pytest.raises(ValueError, match='must end after it starts'):
            await repo.set_time(1, NOW, end, by=ADMIN, now=NOW)

        assert await repo.by_id(1) == before

    async def test_an_unknown_contest_is_a_user_error(self, repo: ContestRepo) -> None:
        with pytest.raises(KcpcUserError, match='^There is no contest with ID 9.$'):
            await repo.set_time(9, NOW, None, by=ADMIN, now=NOW)

    async def test_a_cancelled_contest_is_a_user_error(
        self, repo: ContestRepo, db: Database
    ) -> None:
        # Members are reminded of neither, so success would mislead the admin.
        added = await repo.add_manual(
            'Club contest', at(days=3), at(days=3, hours=2), None, now=NOW
        )
        removed = await repo.cancel_manual(added.contest_id, now=NOW)
        info = contest('abc478', at(days=2))
        await repo.add([info], now=NOW)
        await cancel(repo, info)  # as a sync does once the site drops it
        dropped = await stored(repo, info)

        for gone in (removed, dropped):
            with pytest.raises(KcpcUserError, match='^That contest is cancelled'):
                await repo.set_time(
                    gone.contest_id, at(days=4), None, by=ADMIN, now=NOW
                )
            assert await repo.by_id(gone.contest_id) == gone
        assert await db.fetchval('SELECT COUNT(*) FROM contest_override') == 0


class TestManual:
    async def test_an_added_contest_is_manual_with_its_own_id(
        self, repo: ContestRepo
    ) -> None:
        await repo.add([contest('abc478', at(days=1))], now=NOW)

        added = await repo.add_manual(
            'Club contest', at(days=3), at(days=3, hours=2), None, now=at(minutes=1)
        )
        second = await repo.add_manual(
            'Mock ICPC',
            at(days=4),
            at(days=4, hours=5),
            'https://example.com/mock',
            now=at(minutes=2),
        )

        assert added == StoredContest(
            contest_id=2,
            platform=MANUAL,
            external_id='2',
            name='Club contest',
            start=at(days=3),
            start_date=None,
            end=at(days=3, hours=2),
            url=None,
            status=ContestStatus.SCHEDULED,
            revision=0,
            miss_count=0,
            first_seen=at(minutes=1),
            last_synced=at(minutes=1),
            source_start=at(days=3),
            overridden=False,
        )
        assert (second.contest_id, second.key, second.url) == (
            3,
            'manual:3',
            'https://example.com/mock',
        )
        assert await repo.by_id(3) == second

    async def test_an_added_contest_must_end_after_it_starts(
        self, repo: ContestRepo
    ) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            await repo.add_manual('Club contest', NOW, NOW, None, now=NOW)

        assert await repo.source_records(MANUAL) == []

    async def test_removing_cancels_with_a_new_revision(
        self, repo: ContestRepo
    ) -> None:
        added = await repo.add_manual(
            'Club contest', at(days=3), at(days=3, hours=2), None, now=NOW
        )

        removed = await repo.cancel_manual(added.contest_id, now=at(hours=1))

        assert removed == replace(
            added,
            status=ContestStatus.CANCELLED,
            revision=1,
            last_synced=at(hours=1),
        )
        assert await repo.upcoming(NOW, platforms=PLATFORMS, limit=5) == []
        with pytest.raises(KcpcUserError, match='already been removed'):
            await repo.cancel_manual(added.contest_id, now=at(hours=2))
        assert await repo.by_id(added.contest_id) == removed

    async def test_only_manual_contests_can_be_removed(self, repo: ContestRepo) -> None:
        await repo.add([contest('abc478', at(days=1))], now=NOW)
        before = await repo.by_id(1)

        with pytest.raises(KcpcUserError) as excinfo:
            await repo.cancel_manual(1, now=NOW)

        assert str(excinfo.value) == ONLY_MANUAL
        assert await repo.by_id(1) == before
        with pytest.raises(KcpcUserError, match='^There is no contest with ID 2.$'):
            await repo.cancel_manual(2, now=NOW)

    async def test_admin_writes_join_the_callers_transaction(
        self, repo: ContestRepo, db: Database
    ) -> None:
        await repo.add([contest('abc478', at(days=1))], now=NOW)
        before = await repo.by_id(1)

        with pytest.raises(RuntimeError, match='undo'):
            async with db.transaction():
                added = await repo.add_manual(
                    'Club contest', at(days=3), at(days=4), None, now=NOW
                )
                await repo.set_time(1, at(days=2), None, by=ADMIN, now=NOW)
                await repo.cancel_manual(added.contest_id, now=NOW)
                raise RuntimeError('undo')

        assert await repo.by_id(1) == before
        assert await repo.source_records(MANUAL) == []
        assert await db.fetchval('SELECT COUNT(*) FROM contest_override') == 0


class TestSourceState:
    async def test_none_until_a_sync_is_attempted(self, repo: ContestRepo) -> None:
        assert await repo.source_state('atcoder') is None

    async def test_failures_are_counted_until_a_success(
        self, repo: ContestRepo
    ) -> None:
        assert await repo.record_failure('atcoder', now=NOW, error='first') == 1
        assert (
            await repo.record_failure('atcoder', now=at(minutes=30), error='2nd') == 2
        )
        assert await repo.source_state('atcoder') == SourceState(
            source='atcoder',
            last_attempt=at(minutes=30),
            last_ok=None,
            last_future_count=None,
            consecutive_failures=2,
            last_error='2nd',
        )

        await repo.record_success('atcoder', now=at(hours=1), future_count=7)

        assert await repo.source_state('atcoder') == SourceState(
            source='atcoder',
            last_attempt=at(hours=1),
            last_ok=at(hours=1),
            last_future_count=7,
            consecutive_failures=0,
            last_error=None,
        )
        assert await repo.record_failure('atcoder', now=at(hours=2), error='x') == 1
        state = await repo.source_state('atcoder')
        assert state is not None
        assert (state.last_ok, state.last_future_count) == (at(hours=1), 7)
        assert await repo.source_state('codeforces') is None

    async def test_errors_are_cut_to_500_characters(self, repo: ContestRepo) -> None:
        await repo.record_failure('atcoder', now=NOW, error='x' * 600)

        state = await repo.source_state('atcoder')

        assert state is not None and state.last_error == 'x' * 499 + '…'

    async def test_writes_join_the_callers_transaction(
        self, repo: ContestRepo, db: Database
    ) -> None:
        with pytest.raises(RuntimeError, match='undo'):
            async with db.transaction():
                await repo.add([contest('abc478', at(days=1))], now=NOW)
                await repo.record_failure('atcoder', now=NOW, error='failed')
                raise RuntimeError('undo')

        assert await repo.source_records('atcoder') == []
        assert await repo.source_state('atcoder') is None


class TestContestSettings:
    def test_defaults_follow_every_platform_and_remind_an_hour_before(self) -> None:
        settings = ContestSettings()

        assert (settings.enabled, settings.platforms) == (False, PLATFORMS)
        assert PLATFORMS == (
            'codeforces',
            'atcoder',
            'codechef',
            'leetcode',
            'topcoder',
            'icpc',
            'manual',
        )
        assert (settings.reminder_minutes, settings.start_posts) == ((60,), False)
        # Results don't ping anyone, and come only when members took part.
        assert settings.results_posts
        assert SPEC == FeatureSpec(
            CONTESTS,
            'Contests',
            'Contest reminders: Codeforces, AtCoder, CodeChef, LeetCode, '
            'TopCoder, ICPC and club contests',
            ContestSettings,
        )

    def test_contest_settings_are_the_features_own_or_a_type_error(self) -> None:
        settings = ContestSettings(start_posts=True)

        assert contest_settings(settings) is settings
        with pytest.raises(TypeError, match='Expected ContestSettings'):
            contest_settings(FeatureSettings())

    async def test_round_trip_through_guild_settings(
        self, db: Database, clock: FakeClock
    ) -> None:
        registry = FeatureRegistry()
        registry.register(SPEC)
        guild_settings = GuildSettingsRepo(db, clock, registry)

        await guild_settings.update(
            1234,
            CONTESTS,
            platforms=('atcoder', 'icpc'),
            reminder_minutes=(30, 10),
            start_posts=True,
            results_posts=False,
        )

        assert await guild_settings.get_typed(
            1234, CONTESTS, ContestSettings
        ) == ContestSettings(
            platforms=('atcoder', 'icpc'),
            reminder_minutes=(30, 10),
            start_posts=True,
            results_posts=False,
        )
