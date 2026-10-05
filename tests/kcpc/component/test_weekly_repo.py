"""Component tests for the problems feature's WeeklyRepo, on kcpc.db."""

import itertools
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import zone
from tle.kcpc.features.problems.repo import (
    AUTO,
    QUEUED,
    SOURCES,
    ProblemAlreadyQueued,
    ProblemAlreadyUsed,
    QueuedProblem,
    WeeklyProblem,
    WeeklyRepo,
)

CLUB = zone('Europe/London')
SECOND = timedelta(seconds=1)
HALF_SECOND = timedelta(milliseconds=500)

# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
ADMIN = 1_200_000_000_000_000_001
OTHER_ADMIN = 1_200_000_000_000_000_002

# Problems of Codeforces Round 1520, by index.
NAMES = {
    'C': 'Not Adjacent Matrix',
    'D': 'Same Differences',
    'E': 'Arranging The Sheep',
    'G': 'To Go Or Not To Go?',
}
EDITORIAL = 'https://codeforces.com/blog/entry/90342'
ATCODER_EDITORIAL = 'https://atcoder.jp/contests/abc300/editorial/6198'


def friday(day: int) -> datetime:
    """The slot of Friday ``day`` October 2026: noon in the club's time, in UTC."""
    return datetime(2026, 10, day, 12, 0, tzinfo=CLUB).astimezone(UTC)


def week(day: int) -> str:
    return f'2026-10-{day:02d}'


def problem_of(day: int = 9, index: str = 'D', **changes: Any) -> WeeklyProblem:
    """The guild's weekly problem for Friday ``day``: Codeforces 1520 ``index``."""
    problem = WeeklyProblem(
        guild_id=GUILD,
        slot=friday(day),
        week=week(day),
        source='codeforces',
        problem_id=f'1520{index}',
        contest_id='1520',
        index=index,
        name=NAMES[index],
        url=f'https://codeforces.com/contest/1520/problem/{index}',
        topic='math',
        difficulty=1200,
        band='medium',
        selection=AUTO,
        date_selected=friday(day),
        solution_url=None,
        solution_set_by=None,
        solution_posted=False,
        solution_posted_at=None,
    )
    return replace(problem, **changes)


def atcoder_of(day: int = 9, **changes: Any) -> WeeklyProblem:
    """The guild's weekly problem for Friday ``day``: ABC300 D, with its editorial."""
    problem = problem_of(
        day,
        source='atcoder',
        problem_id='abc300_d',
        contest_id='abc300',
        index='D',
        name='AABCC',
        url='https://atcoder.jp/contests/abc300/tasks/abc300_d',
        topic=None,
        difficulty=1293,
        band='hard',
        solution_url=ATCODER_EDITORIAL,
    )
    return replace(problem, **changes)


def queued_of(index: str = 'E', **changes: Any) -> QueuedProblem:
    """Codeforces 1520 ``index``, which an admin queued for the guild."""
    item = QueuedProblem(
        guild_id=GUILD,
        source='codeforces',
        problem_id=f'1520{index}',
        contest_id='1520',
        index=index,
        name=NAMES[index],
        url=f'https://codeforces.com/contest/1520/problem/{index}',
        difficulty=1400,
        band='medium',
        solution_url=None,
        queued_by=ADMIN,
        queued_at=friday(2),
    )
    return replace(item, **changes)


@pytest.fixture
def repo(db: Database) -> WeeklyRepo:
    return WeeklyRepo(db)


class TestRecords:
    def test_times_are_converted_to_whole_seconds_in_utc(self) -> None:
        # 11:00:00.999999 UTC, noon in London.
        local = datetime(2026, 10, 9, 20, 0, 0, 999_999, tzinfo=zone('Asia/Tokyo'))

        problem = problem_of(slot=local, date_selected=local, solution_posted_at=local)
        item = queued_of(queued_at=local)

        assert (problem.slot, problem.date_selected) == (friday(9), friday(9))
        assert problem.solution_posted_at == friday(9)
        assert problem.slot.tzinfo is UTC
        assert item.queued_at == friday(9)
        assert item.queued_at.tzinfo is UTC
        assert problem == problem_of(solution_posted_at=friday(9))

    @pytest.mark.parametrize(
        'build',
        [
            lambda: problem_of(slot=datetime(2026, 10, 9, 12)),
            lambda: problem_of(date_selected=datetime(2026, 10, 9, 12)),
            lambda: problem_of(solution_posted_at=datetime(2026, 10, 9, 12)),
            lambda: queued_of(queued_at=datetime(2026, 10, 9, 12)),
        ],
        ids=['slot', 'date_selected', 'solution_posted_at', 'queued_at'],
    )
    def test_naive_times_are_refused(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            build()


class TestProblems:
    async def test_a_problem_reads_back_as_stored(
        self, repo: WeeklyRepo, db: Database
    ) -> None:
        problem = problem_of(
            date_selected=friday(9) - HALF_SECOND,
            solution_url=EDITORIAL,
            solution_set_by=ADMIN,
            solution_posted=True,
            solution_posted_at=friday(16) + HALF_SECOND,
        )

        assert await repo.create(problem) == problem

        assert await repo.get(GUILD, friday(9)) == problem
        assert await repo.get(GUILD, friday(16)) is None
        assert await repo.get(OTHER_GUILD, friday(9)) is None
        # Discord IDs are stored as text, which holds them exactly.
        row = await db.fetchone('SELECT guild_id, solution_set_by FROM weekly_problem')
        assert tuple(row or ()) == (str(GUILD), str(ADMIN))

    async def test_a_problem_without_its_optional_details_reads_back_as_stored(
        self, repo: WeeklyRepo
    ) -> None:
        queued = problem_of(selection=QUEUED, topic=None, difficulty=None, band=None)

        assert await repo.create(queued) == queued
        assert await repo.get(GUILD, friday(9)) == queued

    async def test_every_source_and_selection_can_be_stored(
        self, repo: WeeklyRepo
    ) -> None:
        kinds = itertools.product(SOURCES, (AUTO, QUEUED))

        for day, (source, selection) in zip((2, 9, 16, 23), kinds, strict=True):
            problem = problem_of(
                day, source=source, problem_id=f'p{day}', selection=selection
            )
            assert await repo.create(problem) == problem

    async def test_a_slot_keeps_the_problem_stored_first(
        self, repo: WeeklyRepo
    ) -> None:
        first = problem_of(9, 'D')
        await repo.create(first)

        # Another run of the slot that picked something else gets the first.
        assert await repo.create(problem_of(9, 'E')) == first

        assert await repo.get(GUILD, friday(9)) == first
        assert await repo.used_problems(GUILD) == {('codeforces', '1520D'): week(9)}

    async def test_a_week_keeps_the_problem_stored_first_whatever_its_slot(
        self, repo: WeeklyRepo
    ) -> None:
        first = problem_of(9, 'D')
        await repo.create(first)

        # The club's time zone changed, and with it the time of the week's slot.
        moved = problem_of(9, 'E', slot=friday(9) - timedelta(hours=5))
        assert await repo.create(moved) == first

        assert await repo.get_week(GUILD, week(9)) == first
        assert await repo.get(GUILD, moved.slot) is None
        assert await repo.history(GUILD, at_or_before=friday(23)) == [first]

    async def test_a_week_is_found_whatever_its_slot(self, repo: WeeklyRepo) -> None:
        stored = await repo.create(problem_of(9, 'D'))
        await repo.create(problem_of(9, 'D', guild_id=OTHER_GUILD))

        assert await repo.get_week(GUILD, week(9)) == stored
        assert await repo.get_week(GUILD, week(16)) is None

    async def test_a_deleted_problem_is_one_the_guild_never_had(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(problem_of(2, 'D'))
        kept = await repo.create(problem_of(9, 'E'))
        others = await repo.create(problem_of(2, 'D', guild_id=OTHER_GUILD))

        await repo.delete(GUILD, week(2))

        assert await repo.get_week(GUILD, week(2)) is None
        assert await repo.used_problems(GUILD) == {('codeforces', '1520E'): week(9)}
        assert await repo.get_week(GUILD, week(9)) == kept
        assert await repo.get_week(OTHER_GUILD, week(2)) == others
        queued = await repo.enqueue(queued_of('D'))
        assert await repo.queue(GUILD) == [queued]

    async def test_a_retry_of_the_slot_gets_its_problem_back(
        self, repo: WeeklyRepo
    ) -> None:
        problem = problem_of(9, 'D')
        await repo.create(problem)

        # The same problem again is no repeat: it is the slot's own.
        retried = replace(problem, date_selected=friday(9) + SECOND)
        assert await repo.create(retried) == problem

    async def test_a_problem_the_guild_had_before_is_refused(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(problem_of(2, 'D'))

        with pytest.raises(ProblemAlreadyUsed) as caught:
            await repo.create(problem_of(9, 'D'))

        error = caught.value
        assert str(error) == (
            "1520D - Same Differences was already this server's weekly problem "
            'on 2026-10-02.'
        )
        assert error.week == week(2)
        assert isinstance(error, KcpcUserError)
        assert await repo.get(GUILD, friday(9)) is None

    async def test_an_atcoder_problem_is_named_with_its_contest_in_capitals(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(atcoder_of(2))

        with pytest.raises(ProblemAlreadyUsed) as caught:
            await repo.create(atcoder_of(9))

        assert str(caught.value) == (
            "ABC300 D - AABCC was already this server's weekly problem on 2026-10-02."
        )

    async def test_another_guild_may_have_the_same_problem(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(problem_of(2, 'D'))
        others = problem_of(9, 'D', guild_id=OTHER_GUILD)

        assert await repo.create(others) == others
        assert await repo.used_problems(OTHER_GUILD) == {
            ('codeforces', '1520D'): week(9)
        }

    async def test_used_problems_are_the_guilds_with_their_weeks(
        self, repo: WeeklyRepo
    ) -> None:
        for problem in [
            problem_of(2, 'D'),
            problem_of(9, 'E', selection=QUEUED, topic=None),
            atcoder_of(16),
            problem_of(16, 'G', guild_id=OTHER_GUILD),
        ]:
            await repo.create(problem)

        assert await repo.used_problems(GUILD) == {
            ('codeforces', '1520D'): week(2),
            ('codeforces', '1520E'): week(9),
            ('atcoder', 'abc300_d'): week(16),
        }
        assert await repo.used_problems(OTHER_GUILD) == {
            ('codeforces', '1520G'): week(16)
        }

    async def test_latest_is_the_last_slot_at_or_before_a_time(
        self, repo: WeeklyRepo
    ) -> None:
        stored = {day: problem_of(day, index) for day, index in [(2, 'C'), (9, 'D')]}
        for problem in stored.values():
            await repo.create(problem)
        await repo.create(problem_of(16, 'E', guild_id=OTHER_GUILD))

        just_after, just_before = friday(9) + HALF_SECOND, friday(9) - HALF_SECOND

        assert await repo.latest(GUILD, at_or_before=friday(9)) == stored[9]
        assert await repo.latest(GUILD, at_or_before=friday(16)) == stored[9]
        assert await repo.latest(GUILD, at_or_before=just_after) == stored[9]
        assert await repo.latest(GUILD, at_or_before=just_before) == stored[2]
        assert await repo.latest(GUILD, at_or_before=friday(2) - SECOND) is None
        assert await repo.latest(OTHER_GUILD, at_or_before=friday(9)) is None

    async def test_history_is_newest_first_up_to_a_time(self, repo: WeeklyRepo) -> None:
        stored = [problem_of(2, 'C'), problem_of(9, 'D'), atcoder_of(16)]
        for problem in stored:
            await repo.create(problem)
        await repo.create(problem_of(9, 'E', guild_id=OTHER_GUILD))
        first, second, third = stored

        assert await repo.history(GUILD, at_or_before=friday(23)) == [
            third,
            second,
            first,
        ]
        assert await repo.history(GUILD, at_or_before=friday(16) - SECOND) == [
            second,
            first,
        ]
        assert await repo.history(GUILD, at_or_before=friday(23), limit=2) == [
            third,
            second,
        ]
        assert await repo.history(GUILD, at_or_before=friday(23), limit=0) == []
        assert await repo.history(GUILD, at_or_before=friday(2) - SECOND) == []

    async def test_history_holds_the_newest_200_unless_asked_for_more(
        self, repo: WeeklyRepo
    ) -> None:
        first = friday(2)
        for n in range(201):
            slot = first + timedelta(weeks=n)
            await repo.create(
                problem_of(problem_id=f'{1000 + n}A', slot=slot, week=f'week {n}')
            )
        latest = first + timedelta(weeks=200)

        history = await repo.history(GUILD, at_or_before=latest)

        assert len(history) == 200
        assert (history[0].problem_id, history[-1].problem_id) == ('1200A', '1001A')
        assert len(await repo.history(GUILD, at_or_before=latest, limit=500)) == 201


class TestSolutions:
    async def test_unposted_solutions_are_those_of_the_window_oldest_first(
        self, repo: WeeklyRepo
    ) -> None:
        stored = {
            day: await repo.create(problem_of(day, index))
            for day, index in [(2, 'C'), (9, 'D'), (16, 'E'), (23, 'G')]
        }
        await repo.mark_solution_posted(GUILD, friday(9), friday(16))
        await repo.create(problem_of(16, 'C', guild_id=OTHER_GUILD))
        unposted = repo.unposted_solutions

        assert await unposted(GUILD, before=friday(23), since=friday(2)) == [
            stored[2],
            stored[16],
        ]
        # Exactly: since is in the window, before isn't.
        late_2, late_16 = friday(2) + HALF_SECOND, friday(16) + HALF_SECOND
        early_2 = friday(2) - HALF_SECOND
        assert await unposted(GUILD, before=late_16, since=late_2) == [stored[16]]
        assert await unposted(GUILD, before=friday(16), since=early_2) == [stored[2]]
        assert await unposted(GUILD, before=friday(2), since=friday(2)) == []

    async def test_a_solution_marked_posted_keeps_the_first_time(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(problem_of(9, 'D'))
        other = await repo.create(problem_of(9, 'D', guild_id=OTHER_GUILD))
        posted_at = friday(16) + HALF_SECOND

        await repo.mark_solution_posted(GUILD, friday(9), posted_at)
        await repo.mark_solution_posted(GUILD, friday(9), friday(23))

        assert await repo.get(GUILD, friday(9)) == problem_of(
            9, 'D', solution_posted=True, solution_posted_at=friday(16)
        )
        assert await repo.get(OTHER_GUILD, friday(9)) == other
        # A slot without a problem is no error: there is nothing to mark.
        await repo.mark_solution_posted(GUILD, friday(16), friday(23))
        assert await repo.get(GUILD, friday(16)) is None

    async def test_set_solution_stores_the_link_and_who_set_it(
        self, repo: WeeklyRepo
    ) -> None:
        problem = await repo.create(problem_of(9, 'D'))
        other = await repo.create(problem_of(9, 'D', guild_id=OTHER_GUILD))

        updated = await repo.set_solution(GUILD, friday(9), EDITORIAL, set_by=ADMIN)

        expected = replace(problem, solution_url=EDITORIAL, solution_set_by=ADMIN)
        assert updated == expected
        assert await repo.get(GUILD, friday(9)) == expected
        assert await repo.get(OTHER_GUILD, friday(9)) == other
        assert (
            await repo.set_solution(GUILD, friday(16), EDITORIAL, set_by=ADMIN) is None
        )

    async def test_a_link_the_bot_found_never_replaces_an_admins(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(atcoder_of(9))
        mine = 'https://atcoder.jp/contests/abc300/editorial/6203'
        set_by_admin = await repo.set_solution(GUILD, friday(9), mine, set_by=ADMIN)

        found = await repo.set_solution(
            GUILD, friday(9), ATCODER_EDITORIAL, set_by=None
        )

        assert found == set_by_admin
        assert found is not None
        assert (found.solution_url, found.solution_set_by) == (mine, ADMIN)
        # Another admin's link does.
        theirs = await repo.set_solution(
            GUILD, friday(9), ATCODER_EDITORIAL, set_by=OTHER_ADMIN
        )
        assert theirs is not None
        assert (theirs.solution_url, theirs.solution_set_by) == (
            ATCODER_EDITORIAL,
            OTHER_ADMIN,
        )

    async def test_the_bot_may_replace_a_link_it_found(self, repo: WeeklyRepo) -> None:
        await repo.create(atcoder_of(9))
        better = 'https://atcoder.jp/contests/abc300/editorial/6200'

        updated = await repo.set_solution(GUILD, friday(9), better, set_by=None)

        assert updated == atcoder_of(9, solution_url=better)

    async def test_a_posted_solution_keeps_its_link(self, repo: WeeklyRepo) -> None:
        await repo.create(problem_of(9, 'D'))
        await repo.mark_solution_posted(GUILD, friday(9), friday(16))
        posted = await repo.get(GUILD, friday(9))

        updated = await repo.set_solution(GUILD, friday(9), EDITORIAL, set_by=ADMIN)

        # The row returned says so: its solution is posted, without the link.
        assert updated == posted
        assert updated is not None
        assert updated.solution_posted
        assert updated.solution_url is None


class TestQueue:
    async def test_enqueue_adds_the_problem_to_the_end_of_the_queue(
        self, repo: WeeklyRepo
    ) -> None:
        first = await repo.enqueue(queued_of('E'))
        second = await repo.enqueue(queued_of('G', queued_by=OTHER_ADMIN))

        assert isinstance(first.queue_id, int)
        assert isinstance(second.queue_id, int)
        assert replace(first, queue_id=None) == queued_of('E')
        assert await repo.queue(GUILD) == [first, second]
        assert await repo.queue(OTHER_GUILD) == []

    async def test_a_queued_problem_is_refused_again(self, repo: WeeklyRepo) -> None:
        queued = await repo.enqueue(queued_of('E'))

        with pytest.raises(ProblemAlreadyQueued) as caught:
            await repo.enqueue(queued_of('E', queued_by=OTHER_ADMIN))

        assert str(caught.value) == (
            "1520E - Arranging The Sheep is already in this server's queue."
        )
        assert isinstance(caught.value, KcpcUserError)
        assert await repo.queue(GUILD) == [queued]
        # Another guild may queue it.
        others = await repo.enqueue(queued_of('E', guild_id=OTHER_GUILD))
        assert await repo.queue(OTHER_GUILD) == [others]

    async def test_a_problem_the_guild_had_is_refused(self, repo: WeeklyRepo) -> None:
        await repo.create(problem_of(2, 'D'))

        with pytest.raises(ProblemAlreadyUsed) as caught:
            await repo.enqueue(queued_of('D'))

        assert caught.value.week == week(2)
        assert str(caught.value) == (
            "1520D - Same Differences was already this server's weekly problem "
            'on 2026-10-02.'
        )
        assert await repo.queue(GUILD) == []

    async def test_a_stored_queue_id_is_ignored(self, repo: WeeklyRepo) -> None:
        queued = await repo.enqueue(queued_of('E', queue_id=1000))

        assert queued.queue_id != 1000
        assert await repo.queue(GUILD) == [queued]

    async def test_dequeue_takes_the_problem_out_and_returns_it(
        self, repo: WeeklyRepo
    ) -> None:
        sheep = await repo.enqueue(queued_of('E'))
        go = await repo.enqueue(queued_of('G'))
        others = await repo.enqueue(queued_of('E', guild_id=OTHER_GUILD))

        assert await repo.dequeue(GUILD, 'codeforces', '1520E') == sheep

        assert await repo.queue(GUILD) == [go]
        assert await repo.queue(OTHER_GUILD) == [others]
        assert await repo.dequeue(GUILD, 'codeforces', '1520E') is None
        assert await repo.dequeue(GUILD, 'atcoder', '1520G') is None
        # Once out, it may be queued again, at the end.
        again = await repo.enqueue(queued_of('E'))
        assert await repo.queue(GUILD) == [go, again]

    async def test_the_queue_keeps_its_order_once_the_newest_leaves(
        self, repo: WeeklyRepo
    ) -> None:
        first = await repo.enqueue(queued_of('C'))
        second = await repo.enqueue(queued_of('D'))
        newest = await repo.enqueue(queued_of('E'))
        await repo.dequeue(GUILD, 'codeforces', '1520E')

        last = await repo.enqueue(queued_of('G'))

        # SQLite gave it the ID of the newest one, which comes after the rest.
        assert last.queue_id == newest.queue_id
        assert await repo.queue(GUILD) == [first, second, last]

    async def test_all_queues_are_each_guilds_by_guild_id(
        self, repo: WeeklyRepo
    ) -> None:
        assert await repo.all_queues() == {}
        others = await repo.enqueue(queued_of('G', guild_id=OTHER_GUILD))
        first = await repo.enqueue(queued_of('E'))
        second = await repo.enqueue(queued_of('C'))

        queues = await repo.all_queues()

        assert queues == {GUILD: [first, second], OTHER_GUILD: [others]}
        assert list(queues) == [GUILD, OTHER_GUILD]


class TestTransaction:
    async def test_storing_a_queued_problem_and_dequeuing_it_commit_together(
        self, repo: WeeklyRepo, db: Database
    ) -> None:
        queued = await repo.enqueue(queued_of('E'))
        picked = problem_of(9, 'E', selection=QUEUED, topic=None)

        async with repo.transaction():
            await repo.create(picked)
            assert await repo.dequeue(GUILD, 'codeforces', '1520E') == queued
            assert db.in_transaction()

        assert not db.in_transaction()
        assert await repo.get(GUILD, friday(9)) == picked
        assert await repo.queue(GUILD) == []

    async def test_an_error_rolls_back_every_write_in_the_block(
        self, repo: WeeklyRepo
    ) -> None:
        await repo.create(problem_of(2, 'D'))
        queued = await repo.enqueue(queued_of('E'))

        with pytest.raises(ProblemAlreadyUsed):
            async with repo.transaction():
                await repo.dequeue(GUILD, 'codeforces', '1520E')
                await repo.create(problem_of(9, 'E', selection=QUEUED, topic=None))
                await repo.set_solution(GUILD, friday(2), EDITORIAL, set_by=ADMIN)
                await repo.create(problem_of(16, 'D'))  # 1520D was 2 October's

        assert await repo.queue(GUILD) == [queued]
        assert await repo.get(GUILD, friday(9)) is None
        assert await repo.get(GUILD, friday(2)) == problem_of(2, 'D')
