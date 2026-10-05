"""Component tests for the contest results' ResultRepo, on kcpc.db."""

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database
from tle.kcpc.core.timeutil import zone
from tle.kcpc.features.contests.results_repo import (
    ATCODER,
    CODEFORCES,
    Claimant,
    ProfileReading,
    ResultContest,
    ResultEntry,
    ResultOutcome,
    ResultRecord,
    ResultRepo,
    ResultStatus,
)

NOW = datetime(2026, 10, 3, 13, 40, tzinfo=UTC)  # as an ABC ends
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)

ABC = ResultContest(
    ATCODER,
    'abc478',
    'AtCoder Beginner Contest 478',
    'https://atcoder.jp/contests/abc478',
    NOW,
)
ARC = ResultContest(
    ATCODER,
    'arc231',
    'AtCoder Regular Contest 231',
    'https://atcoder.jp/contests/arc231',
    NOW + HOUR,
)
ROUND = ResultContest(
    CODEFORCES,
    '2051',
    'Codeforces Round 1050 (Div. 2)',
    'https://codeforces.com/contest/2051',
    NOW - HOUR,
)


def reading(handle: str, rating: int | None, matches: int) -> ProfileReading:
    """A profile read, with a highest rating 100 above the rating."""
    highest = None if rating is None else rating + 100
    return ProfileReading(handle, rating, highest, matches)


def change(handle: str, place: int, old: int, new: int) -> ResultEntry:
    """A Codeforces handle's rating change, read at NOW."""
    return ResultEntry(
        handle=handle,
        old_rating=old,
        new_rating=new,
        noted_at=NOW,
        changed_at=NOW,
        place=place,
    )


@pytest.fixture
def repo(db: Database) -> ResultRepo:
    return ResultRepo(db)


async def record_of(repo: ResultRepo, contest: ResultContest) -> ResultRecord:
    record = await repo.get(contest.platform, contest.external_id)
    assert record is not None
    return record


async def watched(
    repo: ResultRepo, contest: ResultContest, *baselines: ProfileReading
) -> ResultRecord:
    """The contest, watched from NOW with ``baselines``, first read 15 minutes
    after its end."""
    await repo.save_baselines(
        contest, baselines, next_check=contest.end + 15 * MINUTE, now=NOW
    )
    return await record_of(repo, contest)


class TestRecords:
    def test_times_are_converted_to_whole_seconds_in_utc(self) -> None:
        # 13:40:00.5 UTC, 22:40:00.5 in Tokyo.
        local = datetime(2026, 10, 3, 22, 40, 0, 500_000, tzinfo=zone('Asia/Tokyo'))

        contest = ResultContest(ATCODER, 'abc478', 'ABC 478', None, local)
        entry = ResultEntry('Amber_Owl', 1200, None, noted_at=local, changed_at=local)

        assert contest.end == NOW and contest.end.tzinfo is UTC
        assert (entry.noted_at, entry.changed_at) == (NOW, NOW)
        assert contest.key == 'atcoder:abc478'
        assert entry.changed

    @pytest.mark.parametrize(
        'build',
        [
            lambda: ResultContest(
                ATCODER, 'abc478', 'ABC', None, datetime(2026, 10, 3)
            ),
            lambda: ResultEntry('Amber_Owl', None, None, datetime(2026, 10, 3)),
        ],
        ids=['contest end', 'entry noted_at'],
    )
    def test_naive_times_are_refused(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValueError):
            build()


class TestNewInstall:
    async def test_no_contest_has_been_worked_on_at_first(
        self, repo: ResultRepo
    ) -> None:
        assert await repo.is_empty()

        await repo.add_done([ROUND], ResultOutcome.MISSED, now=NOW)

        assert not await repo.is_empty()

    async def test_contests_can_be_stored_as_done_without_entries(
        self, repo: ResultRepo
    ) -> None:
        assert await repo.add_done([ROUND, ABC], ResultOutcome.MISSED, now=NOW) == 2

        record = await record_of(repo, ROUND)
        assert record == ResultRecord(
            platform=CODEFORCES,
            external_id='2051',
            name='Codeforces Round 1050 (Div. 2)',
            url='https://codeforces.com/contest/2051',
            end=NOW - HOUR,
            status=ResultStatus.DONE,
            checks=0,
            next_check=None,
            found_at=None,
            outcome=ResultOutcome.MISSED,
            updated_at=NOW,
        )
        assert record.key == 'codeforces:2051'
        assert await repo.entries(CODEFORCES, '2051') == []
        assert (await record_of(repo, ABC)).status is ResultStatus.DONE

    async def test_a_contest_stored_before_is_left_as_it_is(
        self, repo: ResultRepo
    ) -> None:
        await repo.start_codeforces(
            ROUND, [change('Amber_Owl', 3, 1500, 1600)], now=NOW
        )

        added = await repo.add_done([ROUND, ABC], ResultOutcome.MISSED, now=NOW + HOUR)

        assert added == 1
        record = await record_of(repo, ROUND)
        assert (record.status, record.outcome) == (ResultStatus.POSTING, None)

    async def test_results_start_once(self, repo: ResultRepo) -> None:
        assert await repo.started_at() is None

        assert await repo.start([ROUND], now=NOW) == (NOW, 1)

        assert await repo.started_at() == NOW
        record = await record_of(repo, ROUND)
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.MISSED,
        )

        # Starting again, as after a restart, stores nothing.
        assert await repo.start([ABC], now=NOW + HOUR) == (NOW, None)

        assert await repo.started_at() == NOW
        assert await repo.get(ATCODER, 'abc478') is None

    async def test_contests_stored_before_any_start_started_them(
        self, repo: ResultRepo
    ) -> None:
        await repo.add_done([ROUND], ResultOutcome.POSTED, now=NOW - HOUR)

        assert await repo.start([ABC], now=NOW) == (NOW - HOUR, None)

        assert await repo.started_at() == NOW - HOUR
        assert await repo.get(ATCODER, 'abc478') is None


class TestCodeforces:
    async def test_a_contests_changes_are_stored_as_being_posted(
        self, repo: ResultRepo
    ) -> None:
        entries = [
            change('Zebra_Fox', 120, 1300, 1250),
            change('Amber_Owl', 3, 0, 1100),
        ]

        record = await repo.start_codeforces(ROUND, entries, now=NOW)

        assert record == await record_of(repo, ROUND)
        assert (record.status, record.found_at, record.outcome) == (
            ResultStatus.POSTING,
            NOW,
            None,
        )
        # By handle.
        assert await repo.entries(CODEFORCES, '2051') == entries[::-1]
        assert await repo.entries(ATCODER, '2051') == []

    async def test_a_contest_keeps_the_changes_stored_first(
        self, repo: ResultRepo
    ) -> None:
        first = [change('Amber_Owl', 3, 1500, 1600)]
        await repo.start_codeforces(ROUND, first, now=NOW)

        record = await repo.start_codeforces(
            ROUND, [change('Zebra_Fox', 9, 1500, 1400)], now=NOW + HOUR
        )

        assert record.found_at == NOW
        assert await repo.entries(CODEFORCES, '2051') == first


class TestAtCoder:
    async def test_baselines_make_a_watched_contest(self, repo: ResultRepo) -> None:
        added = await repo.save_baselines(
            ABC,
            [reading('Zebra_Fox', 1650, 12), reading('Amber_Owl', None, 0)],
            next_check=NOW + 15 * MINUTE,
            now=NOW - 20 * MINUTE,
        )

        assert added == 2
        record = await record_of(repo, ABC)
        assert (record.status, record.next_check, record.checks) == (
            ResultStatus.WATCHING,
            NOW + 15 * MINUTE,
            0,
        )
        assert await repo.entries(ATCODER, 'abc478') == [
            ResultEntry('Amber_Owl', None, None, NOW - 20 * MINUTE, old_matches=0),
            ResultEntry(
                'Zebra_Fox',
                1650,
                None,
                NOW - 20 * MINUTE,
                old_matches=12,
                old_highest=1750,
            ),
        ]

    async def test_a_handle_keeps_its_first_baseline(self, repo: ResultRepo) -> None:
        await repo.save_baselines(
            ABC,
            [reading('Amber_Owl', 1200, 5)],
            next_check=NOW + 15 * MINUTE,
            now=NOW - 30 * MINUTE,
        )

        # Whatever its case. The contest keeps its first read too.
        added = await repo.save_baselines(
            ABC,
            [reading('amber_owl', 1300, 6), reading('Zebra_Fox', 1650, 12)],
            next_check=NOW + HOUR,
            now=NOW - 25 * MINUTE,
        )

        assert added == 1
        assert (await record_of(repo, ABC)).next_check == NOW + 15 * MINUTE
        entries = await repo.entries(ATCODER, 'abc478')
        assert [(entry.handle, entry.old_rating) for entry in entries] == [
            ('Amber_Owl', 1200),
            ('Zebra_Fox', 1650),
        ]

    async def test_due_contests_are_watched_ones_whose_read_is_due(
        self, repo: ResultRepo
    ) -> None:
        await watched(repo, ARC, reading('Amber_Owl', 1200, 5))
        await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        await repo.add_done([ROUND], ResultOutcome.MISSED, now=NOW)

        assert await repo.due(NOW + 14 * MINUTE) == []
        # By end.
        due = await repo.due(NOW + HOUR + 15 * MINUTE)
        assert [record.external_id for record in due] == ['abc478', 'arc231']
        assert [r.external_id for r in await repo.due(NOW + 15 * MINUTE)] == ['abc478']

        await repo.start_posting(await record_of(repo, ABC), now=NOW + HOUR)

        due = await repo.due(NOW + 2 * HOUR)
        assert [record.external_id for record in due] == ['arc231']
        posting = await repo.with_status(ResultStatus.POSTING)
        assert [record.external_id for record in posting] == ['abc478']
        done = await repo.with_status(ResultStatus.DONE)
        assert [record.external_id for record in done] == ['2051']

    async def test_a_check_records_the_changes_and_the_next_read(
        self, repo: ResultRepo
    ) -> None:
        record = await watched(
            repo,
            ABC,
            reading('Amber_Owl', 1200, 5),
            reading('Gone_Owl', 900, 2),
            reading('Zebra_Fox', 1650, 12),
        )
        at = NOW + 15 * MINUTE

        record = await repo.record_check(
            record,
            [reading('amber_owl', 1290, 6)],
            ['gone_owl'],
            now=at,
            next_check=NOW + 45 * MINUTE,
        )

        assert (record.status, record.checks, record.next_check) == (
            ResultStatus.WATCHING,
            1,
            NOW + 45 * MINUTE,
        )
        assert record.found_at is None
        assert await repo.entries(ATCODER, 'abc478') == [
            ResultEntry(
                'Amber_Owl',
                1200,
                1290,
                NOW,
                changed_at=at,
                old_matches=5,
                new_matches=6,
                old_highest=1300,
            ),
            ResultEntry('Zebra_Fox', 1650, None, NOW, old_matches=12, old_highest=1750),
        ]

    async def test_a_check_that_found_changes_makes_the_contest_posted(
        self, repo: ResultRepo
    ) -> None:
        record = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        at = NOW + 45 * MINUTE

        record = await repo.record_check(
            record, [reading('Amber_Owl', 1290, 6)], [], now=at, found=True
        )

        assert (record.status, record.found_at, record.next_check) == (
            ResultStatus.POSTING,
            at,
            None,
        )
        assert record.checks == 1

    async def test_a_last_check_can_finish_the_contest(self, repo: ResultRepo) -> None:
        record = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))

        record = await repo.record_check(
            record, [], [], now=NOW + 6 * HOUR, outcome=ResultOutcome.NOBODY
        )

        assert (record.status, record.outcome, record.next_check) == (
            ResultStatus.DONE,
            ResultOutcome.NOBODY,
            None,
        )
        assert record.found_at is None

    async def test_a_contest_still_watched_needs_its_next_read(
        self, repo: ResultRepo
    ) -> None:
        record = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))

        with pytest.raises(ValueError, match='next check'):
            await repo.record_check(record, [], [], now=NOW + 15 * MINUTE)

        assert (await record_of(repo, ABC)).checks == 0

    async def test_a_claimed_match_raises_the_baselines_of_other_watched_contests(
        self, repo: ResultRepo
    ) -> None:
        abc = await watched(
            repo,
            ABC,
            reading('Amber_Owl', 1200, 5),
            reading('Zebra_Fox', 1650, 12),
        )
        # Another contest, watched, whose baselines were taken at the same
        # time: Zebra_Fox's has the match already.
        await watched(
            repo,
            ARC,
            reading('Amber_Owl', 1200, 5),
            reading('Zebra_Fox', 1700, 13),
        )
        # And one already being posted.
        other = ResultContest(ATCODER, 'agc070', 'AGC 070', None, NOW - HOUR)
        agc = await watched(repo, other, reading('Amber_Owl', 1200, 5))
        await repo.start_posting(agc, now=NOW)
        at = NOW + 15 * MINUTE

        await repo.record_check(
            abc,
            [reading('Amber_Owl', 1290, 6), reading('Zebra_Fox', 1700, 13)],
            [],
            now=at,
            next_check=NOW + 45 * MINUTE,
        )

        arc = {entry.handle: entry for entry in await repo.entries(ATCODER, 'arc231')}
        assert arc['Amber_Owl'] == ResultEntry(
            'Amber_Owl', 1290, None, at, old_matches=6, old_highest=1390
        )
        assert arc['Zebra_Fox'] == ResultEntry(
            'Zebra_Fox', 1700, None, NOW, old_matches=13, old_highest=1800
        )
        (posting,) = await repo.entries(ATCODER, 'agc070')
        assert (posting.old_matches, posting.noted_at) == (5, NOW)

    async def test_a_change_already_seen_is_kept(self, repo: ResultRepo) -> None:
        abc = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        arc = await watched(repo, ARC, reading('Amber_Owl', 1200, 5))
        await repo.record_check(
            arc,
            [reading('Amber_Owl', 1250, 6)],
            [],
            now=NOW + 30 * MINUTE,
            next_check=NOW + HOUR,
        )

        # The ABC's change raises no baseline in the ARC, which saw its own.
        await repo.record_check(
            abc,
            [reading('Amber_Owl', 1310, 7)],
            [],
            now=NOW + 45 * MINUTE,
            next_check=NOW + 75 * MINUTE,
        )

        (entry,) = await repo.entries(ATCODER, 'arc231')
        assert (entry.old_matches, entry.new_matches, entry.new_rating) == (
            5,
            6,
            1250,
        )

    async def test_a_baseline_taken_after_the_claimed_match_is_kept(
        self, repo: ResultRepo
    ) -> None:
        abc = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        # The ARC's baseline was taken once AtCoder had rated the ABC.
        await watched(repo, ARC, reading('Amber_Owl', 1250, 6))

        # By the ABC's read, AtCoder has rated both.
        await repo.record_check(
            abc,
            [reading('Amber_Owl', 1300, 7)],
            [],
            now=NOW + 2 * HOUR,
            next_check=NOW + 150 * MINUTE,
        )

        (entry,) = await repo.entries(ATCODER, 'arc231')
        assert (entry.old_rating, entry.old_matches, entry.noted_at) == (1250, 6, NOW)

    async def test_the_watched_contests_that_could_own_a_rise(
        self, repo: ResultRepo
    ) -> None:
        await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        await repo.save_baselines(
            ARC,
            [reading('amber_owl', 1200, 5)],
            next_check=ARC.end + 15 * MINUTE,
            now=NOW + 40 * MINUTE,
        )
        # One whose baseline has a match more already, and one being posted.
        agc = ResultContest(ATCODER, 'agc070', 'AGC 070', None, NOW - HOUR)
        await watched(repo, agc, reading('Amber_Owl', 1290, 6))
        posted = ResultContest(ATCODER, 'abc477', 'ABC 477', None, NOW - 2 * HOUR)
        await repo.start_posting(
            await watched(repo, posted, reading('Amber_Owl', 1200, 5)), now=NOW
        )

        # The ARC hasn't ended yet.
        assert await repo.claimants(ATCODER, 'AMBER_OWL', 6, now=NOW + 15 * MINUTE) == [
            Claimant(ATCODER, 'abc478', NOW, 5, NOW)
        ]
        # By end.
        assert await repo.claimants(ATCODER, 'Amber_Owl', 7, now=NOW + 2 * HOUR) == [
            Claimant(ATCODER, 'agc070', NOW - HOUR, 6, NOW),
            Claimant(ATCODER, 'abc478', NOW, 5, NOW),
            Claimant(ATCODER, 'arc231', NOW + HOUR, 5, NOW + 40 * MINUTE),
        ]
        assert await repo.claimants(ATCODER, 'Zebra_Fox', 7, now=NOW + 2 * HOUR) == []

        # A contest that saw the handle's change has no claim left.
        await repo.record_check(
            await record_of(repo, ABC),
            [reading('Amber_Owl', 1290, 6)],
            [],
            now=NOW + 15 * MINUTE,
            next_check=NOW + 45 * MINUTE,
        )

        claimants = await repo.claimants(ATCODER, 'Amber_Owl', 7, now=NOW + 2 * HOUR)
        assert [claimant.key for claimant in claimants] == [
            'atcoder:agc070',
            'atcoder:arc231',
        ]

    async def test_a_watched_contest_can_be_read_again_at_once(
        self, repo: ResultRepo
    ) -> None:
        await watched(repo, ABC, reading('Amber_Owl', 1200, 5))

        await repo.check_now(ATCODER, 'abc478', now=NOW + 10 * MINUTE)

        record = await record_of(repo, ABC)
        assert (record.next_check, record.updated_at) == (
            NOW + 10 * MINUTE,
            NOW + 10 * MINUTE,
        )

        # Never later than it was due.
        await repo.check_now(ATCODER, 'abc478', now=NOW + 20 * MINUTE)

        assert (await record_of(repo, ABC)).next_check == NOW + 10 * MINUTE

        # And only while it is watched.
        await repo.start_posting(record, now=NOW + 30 * MINUTE)
        await repo.check_now(ATCODER, 'abc478', now=NOW + 40 * MINUTE)

        assert (await record_of(repo, ABC)).next_check is None

    async def test_finishing_a_contest(self, repo: ResultRepo) -> None:
        record = await watched(repo, ABC, reading('Amber_Owl', 1200, 5))
        record = await repo.start_posting(record, now=NOW + HOUR)

        record = await repo.finish(record, ResultOutcome.POSTED, now=NOW + 2 * HOUR)

        assert record.status is ResultStatus.DONE
        assert record.outcome is ResultOutcome.POSTED
        assert (record.found_at, record.updated_at) == (NOW + HOUR, NOW + 2 * HOUR)


async def test_writes_join_the_callers_transaction(
    db: Database, repo: ResultRepo
) -> None:
    with pytest.raises(RuntimeError, match='undo'):
        async with db.transaction():
            await repo.start([], now=NOW)
            await repo.start_codeforces(
                ROUND, [change('Amber_Owl', 3, 1500, 1600)], now=NOW
            )
            await repo.save_baselines(
                ABC, [reading('Amber_Owl', 1200, 5)], next_check=NOW, now=NOW
            )
            raise RuntimeError('undo')

    assert await repo.is_empty()
    assert await repo.started_at() is None
    assert await repo.entries(CODEFORCES, '2051') == []
