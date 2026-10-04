"""Tests for tle.kcpc.features.problems.solved: members' solved problems.

Codeforces is faked in place of TLE's ``cf.user.status``, and AtCoder
Problems' submissions by FakeSubmissions. The users are made up.
"""

import asyncio
import gc
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from typing import cast

import pytest

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.features.problems.catalog import Problem
from tle.kcpc.features.problems.solved import (
    CODEFORCES_PAUSE,
    SOLVED_TTL,
    SolvedProblems,
    SolvedSet,
)
from tle.kcpc.platforms.atcoder.problems import (
    SUBMISSIONS_PAGE,
    AtCoderProblem,
    AtCoderProblemsClient,
    AtCoderSubmission,
)
from tle.kcpc.platforms.codeforces import CodeforcesProblem
from tle.util import codeforces_api as cf

# The second of the first made-up submission.
FIRST_SECOND = 1_700_000_000
NOT_RESPONDING = 'Codeforces is not responding right now. Please try again later.'
PAUSED = "Codeforces failed just now, so it isn't asked again for a while."
ATCODER_DOWN = ExternalServiceError(
    'AtCoder Problems', "AtCoder Problems' list of submissions could not be read."
)


def codeforces_problem(contest_id: int, index: str, name: str) -> Problem:
    return Problem.from_codeforces(
        CodeforcesProblem(contest_id, index, name, 1500, (), None, True)
    )


def atcoder_problem(problem_id: str) -> Problem:
    contest_id, letter = problem_id.rsplit('_', 1)
    return Problem.from_atcoder(
        AtCoderProblem(problem_id, contest_id, letter.upper(), 'Task', 1000)
    )


SAME_DIFFERENCES = codeforces_problem(1520, 'D', 'Same Differences')
# Div. 1 set as 1500A the problem that Div. 2 set as 1501C: the problemset lists
# only 1500A.
GOING_HOME = codeforces_problem(1500, 'A', 'Going Home')
UNSOLVED = codeforces_problem(1520, 'E', 'Arranging The Sheep')


def cf_problem(contest_id: int, index: str, name: str) -> cf.Problem:
    return cf.Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=None,
        rating=1500,
        tags=['implementation'],
    )


def cf_submission(problem: cf.Problem, verdict: str = 'OK') -> cf.Submission:
    """A submission of kcpc_Example's, as TLE's user.status gives it."""
    return cf.Submission(
        id=1,
        contestId=problem.contestId,
        problem=problem,
        author=cf.Party(
            contestId=problem.contestId,
            members=[cf.Member(handle='kcpc_Example')],
            participantType='PRACTICE',
            teamId=None,
            teamName=None,
            ghost=False,
            room=None,
            startTimeSeconds=None,
        ),
        programmingLanguage='C++23 (GCC 14-64, msys2)',
        verdict=verdict,
        creationTimeSeconds=1_790_000_000,
        relativeTimeSeconds=2_147_483_647,
    )


class FakeUserStatus:
    """Stands in for TLE's ``cf.user.status``: ``requests`` lists each handle
    asked about, and ``errors`` fail the next requests. A request whose number
    (from 0) is in ``gates`` waits for that event first.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.errors: deque[Exception] = deque()
        self.submissions: dict[str, list[cf.Submission]] = {}
        self.gates: dict[int, asyncio.Event] = {}

    async def status(
        self, *, handle: str, from_: int | None = None, count: int | None = None
    ) -> list[cf.Submission]:
        gate = self.gates.get(len(self.requests))
        self.requests.append(handle)
        if gate is not None:
            await gate.wait()
        if self.errors:
            raise self.errors.popleft()
        if handle.lower() not in self.submissions:
            comment = f'handle: User with handle {handle} not found'
            raise cf.HandleNotFoundError(comment, handle)
        return self.submissions[handle.lower()]


def submission(
    number: int, problem_id: str, result: str = 'AC', *, second: int | None = None
) -> AtCoderSubmission:
    """The ``number``th made-up submission, a second after the one before."""
    return AtCoderSubmission(
        submission_id=60_000_000 + number,
        epoch_second=FIRST_SECOND + number if second is None else second,
        problem_id=problem_id,
        result=result,
    )


class FakeSubmissions:
    """Stands in for ``AtCoderProblemsClient.fetch_submissions``.

    It answers as AtCoder Problems does: a user's submissions from a second
    on, that second included, oldest first, at most 500. ``requests`` lists
    each (user, from_second); ``errors`` fail the next requests; a request
    whose number (from 0) is in ``gates`` waits for that event first.
    """

    def __init__(self) -> None:
        self.submissions: dict[str, list[AtCoderSubmission]] = {}
        self.requests: list[tuple[str, int]] = []
        self.errors: deque[Exception] = deque()
        self.gates: dict[int, asyncio.Event] = {}

    def add(self, user: str, *submissions: AtCoderSubmission) -> None:
        listed = self.submissions.setdefault(user.lower(), [])
        listed += submissions
        listed.sort(key=lambda listed: listed.epoch_second)

    async def fetch_submissions(
        self, user: str, from_second: int
    ) -> list[AtCoderSubmission]:
        gate = self.gates.get(len(self.requests))
        self.requests.append((user, from_second))
        if gate is not None:
            await gate.wait()
        if self.errors:
            raise self.errors.popleft()
        submissions = self.submissions.get(user.lower(), [])
        later = [found for found in submissions if found.epoch_second >= from_second]
        return later[:SUBMISSIONS_PAGE]


@pytest.fixture
def user_status(monkeypatch: pytest.MonkeyPatch) -> FakeUserStatus:
    fake = FakeUserStatus()
    monkeypatch.setattr(cf.user, 'status', fake.status)
    return fake


@pytest.fixture
def atcoder() -> FakeSubmissions:
    return FakeSubmissions()


@pytest.fixture
def solved(clock: FakeClock, atcoder: FakeSubmissions) -> SolvedProblems:
    return SolvedProblems(cast(AtCoderProblemsClient, atcoder), clock)


def problem_ids(count: int) -> list[str]:
    """Made-up AtCoder problem IDs, six to a contest."""
    return [f'abc{100 + n // 6:03d}_{"abcdef"[n % 6]}' for n in range(count)]


def froms(fake: FakeSubmissions) -> list[int]:
    """Each request's from_second, as seconds after the first submission."""
    return [
        from_second - FIRST_SECOND if from_second else 0
        for _, from_second in fake.requests
    ]


class TestSolvedSet:
    def test_codeforces_problems_are_found_by_id_or_by_name(self) -> None:
        solved = SolvedSet(
            frozenset({'1520D', '1501C'}),
            frozenset({'Same Differences', 'Going Home'}),
            complete=True,
        )

        assert solved.contains(SAME_DIFFERENCES)
        assert solved.contains(GOING_HOME)  # solved in Div. 2, as 1501C
        assert not solved.contains(UNSOLVED)

    def test_atcoder_problems_are_found_by_id_whatever_its_case(self) -> None:
        upper = SolvedSet(frozenset({'ABC300_D'}), frozenset(), complete=True)
        lower = SolvedSet(frozenset({'abc300_d'}), frozenset(), complete=True)

        assert upper.contains(atcoder_problem('abc300_d'))
        assert lower.contains(atcoder_problem('ABC300_D'))
        assert not lower.contains(atcoder_problem('abc300_e'))

    def test_atcoder_problems_are_never_found_by_name(self) -> None:
        solved = SolvedSet(frozenset(), frozenset({'Task'}), complete=True)

        assert not solved.contains(atcoder_problem('abc300_d'))

    def test_sets_compare_by_their_fields(self) -> None:
        assert SolvedSet(frozenset({'a_b'}), frozenset(), True) == SolvedSet(
            frozenset({'a_b'}), frozenset(), True
        )


class TestCodeforces:
    async def test_the_ids_and_names_of_accepted_submissions(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        user_status.submissions['kcpc_example'] = [
            cf_submission(cf_problem(1520, 'D', 'Same Differences')),
            cf_submission(cf_problem(1501, 'C', 'Going Home')),
            cf_submission(cf_problem(1520, 'E', 'Arranging The Sheep'), 'WRONG_ANSWER'),
        ]

        found = await solved.codeforces('kcpc_Example')

        assert found == SolvedSet(
            frozenset({'1520D', '1501C'}),
            frozenset({'Same Differences', 'Going Home'}),
            complete=True,
        )
        assert found.contains(GOING_HOME)

    async def test_no_such_user_is_none(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        assert await solved.codeforces('kcpc_nobody') is None

    async def test_each_handle_is_kept_whatever_its_case_for_a_while(
        self, solved: SolvedProblems, user_status: FakeUserStatus, clock: FakeClock
    ) -> None:
        assert SOLVED_TTL == timedelta(minutes=15)
        user_status.submissions['kcpc_example'] = [
            cf_submission(cf_problem(1520, 'D', 'Same Differences'))
        ]
        first = await solved.codeforces('kcpc_Example')

        await clock.advance(SOLVED_TTL - timedelta(seconds=1))
        assert await solved.codeforces('KCPC_EXAMPLE') == first
        assert await solved.codeforces('kcpc_nobody') is None
        assert await solved.codeforces('kcpc_nobody') is None
        assert user_status.requests == ['kcpc_Example', 'kcpc_nobody']

        await clock.advance(timedelta(seconds=1))
        await solved.codeforces('kcpc_example')
        assert user_status.requests == ['kcpc_Example', 'kcpc_nobody', 'kcpc_example']

    async def test_after_a_failure_codeforces_is_left_alone_for_a_while(
        self, solved: SolvedProblems, user_status: FakeUserStatus, clock: FakeClock
    ) -> None:
        assert CODEFORCES_PAUSE == timedelta(minutes=2)
        user_status.submissions['kcpc_example'] = []
        user_status.errors.append(cf.TrueApiError('HTTP Error 503, Unavailable'))

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING):
            await solved.codeforces('kcpc_example')

        # Nothing is kept, and no one's lookup asks Codeforces for a while.
        for handle in ('kcpc_example', 'kcpc_other'):
            with pytest.raises(ExternalServiceError) as paused:
                await solved.codeforces(handle)
            assert str(paused.value) == PAUSED
        assert user_status.requests == ['kcpc_example']

        await clock.advance(CODEFORCES_PAUSE)
        assert await solved.codeforces('kcpc_example') == SolvedSet(
            frozenset(), frozenset(), complete=True
        )
        assert user_status.requests == ['kcpc_example', 'kcpc_example']

    async def test_a_caller_that_stops_waiting_leaves_the_lookup_to_finish(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        user_status.submissions['kcpc_example'] = [
            cf_submission(cf_problem(1520, 'D', 'Same Differences'))
        ]
        user_status.submissions['kcpc_other'] = []
        user_status.gates[0] = asyncio.Event()  # a long history
        waiting = asyncio.create_task(solved.codeforces('kcpc_Example'))
        await _until(lambda: user_status.requests == ['kcpc_Example'])

        waiting.cancel()  # as /randproblem gives up after 10 seconds
        with pytest.raises(asyncio.CancelledError):
            await waiting

        # Not a failure of Codeforces: others' lookups go on.
        assert await solved.codeforces('kcpc_other') is not None
        user_status.gates[0].set()
        await _until(lambda: user_status.requests == ['kcpc_Example', 'kcpc_other'])
        found = await asyncio.wait_for(solved.codeforces('kcpc_example'), 5)
        assert found is not None and found.ids == {'1520D'}
        assert user_status.requests == ['kcpc_Example', 'kcpc_other']

    async def test_lookups_of_one_handle_share_one_request(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        user_status.submissions['kcpc_example'] = []
        user_status.gates[0] = asyncio.Event()
        first = asyncio.create_task(solved.codeforces('kcpc_example'))
        second = asyncio.create_task(solved.codeforces('KCPC_EXAMPLE'))
        await _until(lambda: len(user_status.requests) == 1)
        for _ in range(10):
            await asyncio.sleep(0)

        user_status.gates[0].set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)

        assert results[0] == results[1]
        assert user_status.requests == ['kcpc_example']

    async def test_close_stops_the_lookups_still_running(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        user_status.gates[0] = asyncio.Event()  # never answered
        waiting = asyncio.create_task(solved.codeforces('kcpc_example'))
        await _until(lambda: user_status.requests == ['kcpc_example'])

        await asyncio.wait_for(solved.close(), 5)

        with pytest.raises(asyncio.CancelledError):
            await waiting

    async def test_a_failure_no_one_waits_for_is_not_left_unread(
        self, solved: SolvedProblems, user_status: FakeUserStatus
    ) -> None:
        unread: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: unread.append(context))
        try:
            user_status.gates[0] = asyncio.Event()
            user_status.errors.append(cf.TrueApiError('HTTP Error 503, Unavailable'))
            waiting = asyncio.create_task(solved.codeforces('kcpc_example'))
            await _until(lambda: user_status.requests == ['kcpc_example'])
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting

            user_status.gates[0].set()
            for _ in range(10):
                await asyncio.sleep(0)
            gc.collect()
        finally:
            loop.set_exception_handler(None)

        assert unread == []
        with pytest.raises(ExternalServiceError, match='failed just now'):
            await solved.codeforces('kcpc_example')

    async def test_names_count_only_for_problems_the_problemset_doesnt_list(
        self, clock: FakeClock, atcoder: FakeSubmissions, user_status: FakeUserStatus
    ) -> None:
        listed = {'2266A', '1500A', '1295F'}
        solved = SolvedProblems(
            cast(AtCoderProblemsClient, atcoder), clock, listed=listed.__contains__
        )
        user_status.submissions['kcpc_example'] = [
            cf_submission(cf_problem(2266, 'A', 'Good Contest')),
            # Div. 2's copy of 1500A, which the problemset lists only as 1500A.
            cf_submission(cf_problem(1501, 'C', 'Going Home')),
        ]

        found = await solved.codeforces('kcpc_example')

        assert found == SolvedSet(
            frozenset({'2266A', '1501C'}), frozenset({'Going Home'}), complete=True
        )
        assert found.contains(GOING_HOME)
        # A problem of another round that has the name of one solved isn't.
        assert not found.contains(codeforces_problem(1295, 'F', 'Good Contest'))


class TestAtCoder:
    async def test_pages_are_read_until_one_is_short(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        ids = problem_ids(1203)
        results = ['AC' if n % 3 == 0 else 'WA' for n in range(1203)]
        atcoder.add(
            'kcpc_example',
            *(submission(n, ids[n], results[n]) for n in range(1203)),
        )

        found = await solved.atcoder('kcpc_Example')

        accepted = {ids[n] for n in range(1203) if results[n] == 'AC'}
        assert found == SolvedSet(frozenset(accepted), frozenset(), complete=True)
        # Each page starts at the second of the last one's last submission.
        assert froms(atcoder) == [0, 499, 998]
        assert {user for user, _ in atcoder.requests} == {'kcpc_Example'}

    async def test_the_second_a_page_ends_on_is_read_again(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        # The 500th and 501st submissions share a second: the page that the
        # 500th ends has no room for the 501st.
        last = FIRST_SECOND + 499
        atcoder.add(
            'kcpc_example',
            *(submission(n, 'abc100_a', 'WA') for n in range(499)),
            submission(499, 'abc100_a', 'WA', second=last),
            submission(500, 'abc200_b', second=last),
        )

        found = await solved.atcoder('kcpc_example')

        assert found.ids == {'abc200_b'}
        assert froms(atcoder) == [0, 499]

    async def test_a_full_page_in_one_second_is_stepped_past(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        second = FIRST_SECOND + 10
        atcoder.add(
            'kcpc_example',
            *(submission(n, 'abc100_a', second=second) for n in range(500)),
            submission(500, 'abc200_b', second=second + 5),
        )

        found = await solved.atcoder('kcpc_example')

        assert found.ids == {'abc100_a', 'abc200_b'}
        assert froms(atcoder) == [0, 11]

    async def test_an_unknown_user_has_solved_nothing(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        found = await solved.atcoder('kcpc_nobody')

        assert found == SolvedSet(frozenset(), frozenset(), complete=True)
        assert solved.cached_atcoder('kcpc_nobody') == found

    async def test_what_was_read_is_kept_then_read_on_from_where_it_ended(
        self, solved: SolvedProblems, atcoder: FakeSubmissions, clock: FakeClock
    ) -> None:
        atcoder.add(
            'kcpc_example', submission(0, 'abc100_a'), submission(1, 'abc100_b')
        )
        first = await solved.atcoder('kcpc_example')
        atcoder.add('kcpc_example', submission(2, 'abc100_c'))

        await clock.advance(SOLVED_TTL - timedelta(seconds=1))
        assert await solved.atcoder('KCPC_Example') == first
        assert froms(atcoder) == [0]

        await clock.advance(timedelta(seconds=1))
        found = await solved.atcoder('kcpc_example')

        assert found.ids == {'abc100_a', 'abc100_b', 'abc100_c'}
        assert found.complete
        assert froms(atcoder) == [0, 1]

    async def test_a_caller_that_stops_waiting_leaves_what_was_read(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        ids = problem_ids(1100)
        atcoder.add('kcpc_example', *(submission(n, ids[n]) for n in range(1100)))
        atcoder.gates[2] = asyncio.Event()
        assert solved.cached_atcoder('kcpc_example') is None

        # As /randproblem waits: the third page never comes in time.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(solved.atcoder('kcpc_example'), 0.5)

        partial = solved.cached_atcoder('KCPC_example')
        assert partial == SolvedSet(frozenset(ids[:999]), frozenset(), complete=False)
        assert froms(atcoder) == [0, 499, 998]

        atcoder.gates.clear()
        found = await solved.atcoder('kcpc_example')

        assert found == SolvedSet(frozenset(ids), frozenset(), complete=True)
        assert froms(atcoder) == [0, 499, 998, 998]
        assert solved.cached_atcoder('kcpc_example') == found

    async def test_a_failure_is_raised_and_the_next_call_tries_again(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        ids = problem_ids(700)
        atcoder.add('kcpc_example', *(submission(n, ids[n]) for n in range(700)))
        atcoder.errors.append(ATCODER_DOWN)

        with pytest.raises(ExternalServiceError) as raised:
            await solved.atcoder('kcpc_example')

        assert raised.value is ATCODER_DOWN
        assert solved.cached_atcoder('kcpc_example') is None
        found = await solved.atcoder('kcpc_example')
        assert found == SolvedSet(frozenset(ids), frozenset(), complete=True)
        assert froms(atcoder) == [0, 0, 499]

    async def test_a_failure_part_way_keeps_the_pages_before_it(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        ids = problem_ids(700)
        atcoder.add('kcpc_example', *(submission(n, ids[n]) for n in range(700)))
        atcoder.gates[1] = asyncio.Event()
        task = asyncio.create_task(solved.atcoder('kcpc_example'))
        await _until(lambda: len(atcoder.requests) == 2)
        atcoder.errors.append(ATCODER_DOWN)
        atcoder.gates[1].set()

        with pytest.raises(ExternalServiceError):
            await task

        assert solved.cached_atcoder('kcpc_example') == SolvedSet(
            frozenset(ids[:500]), frozenset(), complete=False
        )
        found = await solved.atcoder('kcpc_example')
        assert found == SolvedSet(frozenset(ids), frozenset(), complete=True)
        assert froms(atcoder) == [0, 499, 499]

    async def test_lookups_of_one_user_take_turns(
        self, solved: SolvedProblems, atcoder: FakeSubmissions
    ) -> None:
        ids = problem_ids(600)
        atcoder.add('kcpc_example', *(submission(n, ids[n]) for n in range(600)))
        atcoder.gates[0] = asyncio.Event()
        first = asyncio.create_task(solved.atcoder('kcpc_example'))
        second = asyncio.create_task(solved.atcoder('KCPC_EXAMPLE'))
        await _until(lambda: len(atcoder.requests) == 1)
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(atcoder.requests) == 1  # the second waits for the first

        atcoder.gates[0].set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 5)

        assert results[0] == results[1]
        assert results[0].ids == set(ids)
        assert froms(atcoder) == [0, 499]


async def _until(condition: Callable[[], bool]) -> None:
    """Let other tasks run until ``condition()`` holds (up to 1000 turns)."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError('The condition never held')
