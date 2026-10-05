"""Tests for tle.kcpc.features.problems.catalog: both platforms' problems.

Codeforces is faked in place of TLE's ``cf.problemset.problems`` and
``cf.contest.to_list``, and AtCoder Problems by FakeAtCoderProblems.
"""

import asyncio
from datetime import timedelta
from typing import cast

import pytest

from tests.kcpc.conftest import CLOCK_START
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.features.problems.catalog import (
    ATCODER,
    ATCODER_MAX_AGE,
    CODEFORCES,
    CODEFORCES_MAX_AGE,
    Problem,
    ProblemCatalog,
    ProblemRef,
    describe_difficulty,
    parse_problem_ref,
    platform_name,
    platform_possessive,
    problem_title,
)
from tle.kcpc.platforms.atcoder.problems import AtCoderProblem, AtCoderProblemsClient
from tle.kcpc.platforms.codeforces import CodeforcesProblem
from tle.kcpc.platforms.difficulty import Band
from tle.util import codeforces_api as cf

NOW = CLOCK_START
NOT_A_PROBLEM = (
    "That isn't a problem I know how to read. Give a Codeforces problem as "
    '1520D or its link, or an AtCoder problem as abc300_d or its link.'
)
ATCODER_DOWN = ExternalServiceError(
    'AtCoder Problems', "AtCoder Problems' problem list could not be read."
)


def cf_problem(
    contest_id: int, index: str, name: str, rating: int | None, tags: list[str]
) -> cf.Problem:
    """A problem as TLE's problemset.problems gives it."""
    return cf.Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=None,
        rating=rating,
        tags=tags,
    )


def finished(contest_id: int, name: str) -> cf.Contest:
    """A finished contest as TLE's contest.list gives it."""
    return cf.Contest(
        id=contest_id,
        name=name,
        startTimeSeconds=1_620_000_000,
        durationSeconds=7200,
        type='CF',
        phase='FINISHED',
        preparedBy=None,
    )


SAME_DIFFERENCES = cf_problem(
    1520, 'D', 'Same Differences', 1200, ['data structures', 'hashing', 'math']
)
TO_GO = cf_problem(
    1520, 'G', 'To Go Or Not To Go?', 2200, ['dfs and similar', 'graphs', 'greedy']
)
UNRATED = cf_problem(2269, 'F', 'Rated Soon', None, ['dp'])
KOTLIN = cf_problem(1958, 'A', 'Kotlin Warm-up', 1000, ['implementation'])
SPECIAL = cf_problem(1600, 'A', 'Special Interest', 900, ['*special', 'games'])
LABYRINTH = cf_problem(921, '01', 'Labyrinth-1', 3200, [])
CODEFORCES_PROBLEMS = [SAME_DIFFERENCES, TO_GO, UNRATED, KOTLIN, SPECIAL, LABYRINTH]
CONTESTS = [
    finished(2269, 'Codeforces Round 1069 (Div. 2)'),
    finished(1958, 'Kotlin Heroes: Episode 10'),
    finished(1600, 'Codeforces Round 751 (Div. 1)'),
    finished(1520, 'Codeforces Round 719 (Div. 3)'),
    finished(921, 'VK Cup 2018 - Wild-card Round 1'),
]

AABCC = AtCoderProblem('abc300_d', 'abc300', 'D', 'AABCC', 1000)
N_CHOICE = AtCoderProblem('abc300_a', 'abc300', 'A', 'N-choice question', 3)
HEURISTIC = AtCoderProblem('ahc001_a', 'ahc001', 'A', 'AtCoder Ad', 1800)
NO_MODEL = AtCoderProblem('abc001_1', 'abc001', 'A', '積雪深差', None)
ATCODER_PROBLEMS = {
    problem.problem_id: problem for problem in (N_CHOICE, AABCC, HEURISTIC, NO_MODEL)
}


class FakeProblemset:
    """Stands in for TLE's ``cf.problemset.problems`` and ``cf.contest.to_list``.

    ``fetches`` counts the problem lists sent, and ``error`` fails the next.
    """

    def __init__(self) -> None:
        self.problems = list(CODEFORCES_PROBLEMS)
        self.contests = list(CONTESTS)
        self.fetches = 0
        self.error: Exception | None = None

    async def problemset_problems(
        self, **kwargs: object
    ) -> tuple[list[cf.Problem], list[cf.ProblemStatistics]]:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.fetches += 1
        statistics = [
            cf.ProblemStatistics(problem.contestId, problem.index, 5_000)
            for problem in self.problems
        ]
        return self.problems, statistics

    async def contest_list(self, **kwargs: object) -> list[cf.Contest]:
        return self.contests


class FakeAtCoderProblems:
    """Stands in for ``AtCoderProblemsClient``: ``problems`` by ID.

    ``fetches`` counts the problem sets sent, and ``error`` fails the next.
    While ``gate`` is set, a fetch waits for it to open.
    """

    def __init__(self) -> None:
        self.problems = dict(ATCODER_PROBLEMS)
        self.fetches = 0
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def fetch_problem_set(self) -> dict[str, AtCoderProblem]:
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.fetches += 1
        return dict(self.problems)


@pytest.fixture
def problemset(monkeypatch: pytest.MonkeyPatch) -> FakeProblemset:
    fake = FakeProblemset()
    monkeypatch.setattr(cf.problemset, 'problems', fake.problemset_problems)
    monkeypatch.setattr(cf.contest, 'to_list', fake.contest_list)
    return fake


@pytest.fixture
def atcoder() -> FakeAtCoderProblems:
    return FakeAtCoderProblems()


@pytest.fixture
def catalog(
    clock: FakeClock, problemset: FakeProblemset, atcoder: FakeAtCoderProblems
) -> ProblemCatalog:
    return ProblemCatalog(cast(AtCoderProblemsClient, atcoder), clock)


def ids(problems: object) -> list[str]:
    assert isinstance(problems, (list, tuple))
    return [problem.problem_id for problem in problems]


class TestProblems:
    def test_a_codeforces_problem(self) -> None:
        problem = Problem.from_codeforces(
            CodeforcesProblem(
                1520, 'D', 'Same Differences', 1200, ('math',), 41_234, True
            )
        )

        assert problem == Problem(
            platform=CODEFORCES,
            problem_id='1520D',
            contest_id='1520',
            index='D',
            name='Same Differences',
            title='1520D - Same Differences',
            url='https://codeforces.com/contest/1520/problem/D',
            contest_url='https://codeforces.com/contest/1520',
            rating=1200,
            difficulty=None,
            tags=('math',),
            solved_count=41_234,
            in_pool=True,
        )
        assert problem.band is Band.MEDIUM

    @pytest.mark.parametrize(
        ('rating', 'standard', 'in_pool'),
        [(1200, False, False), (None, True, False), (None, False, False)],
    )
    def test_codeforces_picks_rated_problems_of_standard_rounds(
        self, rating: int | None, standard: bool, in_pool: bool
    ) -> None:
        problem = Problem.from_codeforces(
            CodeforcesProblem(1520, 'D', 'Same Differences', rating, (), None, standard)
        )

        assert problem.in_pool is in_pool
        assert problem.band is (None if rating is None else Band.MEDIUM)

    def test_an_atcoder_problem(self) -> None:
        problem = Problem.from_atcoder(AABCC)

        assert problem == Problem(
            platform=ATCODER,
            problem_id='abc300_d',
            contest_id='abc300',
            index='D',
            name='AABCC',
            title='ABC300 D - AABCC',
            url='https://atcoder.jp/contests/abc300/tasks/abc300_d',
            contest_url='https://atcoder.jp/contests/abc300',
            rating=1467,  # trunc(3900 * (1000 + 940) / 5155)
            difficulty=1000,
            tags=(),
            solved_count=None,
            in_pool=True,
        )
        assert problem.band is Band.MEDIUM
        assert not Problem.from_atcoder(HEURISTIC).in_pool
        assert Problem.from_atcoder(NO_MODEL).band is None

    def test_titles_and_platform_names(self) -> None:
        assert problem_title(CODEFORCES, '1520', 'D', 'Same Differences') == (
            '1520D - Same Differences'
        )
        assert problem_title(ATCODER, 'abc300', 'Ex', 'X') == 'ABC300 Ex - X'
        assert platform_name(CODEFORCES) == 'Codeforces'
        assert platform_name(ATCODER) == 'AtCoder'
        assert platform_possessive(CODEFORCES) == "Codeforces'"
        assert platform_possessive(ATCODER) == "AtCoder's"

    def test_difficulties_as_posts_say_them(self) -> None:
        assert describe_difficulty(CODEFORCES, 1600, None) == '1600 (hard)'
        assert describe_difficulty(CODEFORCES, None, None) == 'unrated'
        assert describe_difficulty(ATCODER, 1752, 1376) == (
            '1376 on AtCoder (about 1752 on Codeforces, hard)'
        )
        assert describe_difficulty(ATCODER, 1752, None) == (
            'about 1752 on Codeforces (hard)'
        )
        assert describe_difficulty(ATCODER, None, None) == 'unrated'


class TestParseProblemRef:
    @pytest.mark.parametrize(
        'text',
        [
            '1520D',
            '1520d',
            ' 1520 D ',
            '1520/D',
            '1520 / d',
            'codeforces.com/contest/1520/problem/D',
            'https://codeforces.com/contest/1520/problem/D',
            'https://codeforces.com/contest/1520/problem/D/?locale=en#statement',
            'http://www.codeforces.com/problemset/problem/1520/D',
            'https://m1.codeforces.com/problemset/problem/1520/d',
        ],
    )
    def test_codeforces_problems(self, text: str) -> None:
        assert parse_problem_ref(text) == ProblemRef(CODEFORCES, '1520D', '1520', 'D')

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('1520F2', ProblemRef(CODEFORCES, '1520F2', '1520', 'F2')),
            ('207D10', ProblemRef(CODEFORCES, '207D10', '207', 'D10')),
            ('921 01', ProblemRef(CODEFORCES, '92101', '921', '01')),
            ('921/14', ProblemRef(CODEFORCES, '92114', '921', '14')),
            ('01520D', ProblemRef(CODEFORCES, '1520D', '1520', 'D')),
        ],
    )
    def test_codeforces_indexes_of_every_shape(
        self, text: str, expected: ProblemRef
    ) -> None:
        assert parse_problem_ref(text) == expected

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('abc300_d', ProblemRef(ATCODER, 'abc300_d', 'abc300', None)),
            ('ABC300_D', ProblemRef(ATCODER, 'ABC300_D', 'ABC300', None)),
            ('cf17_final_a', ProblemRef(ATCODER, 'cf17_final_a', 'cf17_final', None)),
            (
                'https://atcoder.jp/contests/abc300/tasks/abc300_d',
                ProblemRef(ATCODER, 'abc300_d', 'abc300', None),
            ),
            (
                'atcoder.jp/contests/cf17-final/tasks/cf17_final_a?lang=en',
                ProblemRef(ATCODER, 'cf17_final_a', 'cf17-final', None),
            ),
            (
                'https://www.atcoder.jp/contests/adt_all_20260715_1/tasks/abc212_d/',
                ProblemRef(ATCODER, 'abc212_d', 'adt_all_20260715_1', None),
            ),
        ],
    )
    def test_atcoder_problems(self, text: str, expected: ProblemRef) -> None:
        assert parse_problem_ref(text) == expected

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            # AtCoder's few IDs without a '_': looking them up tells.
            ('joi2011ho1', ProblemRef(ATCODER, 'joi2011ho1', 'joi2011ho1', None)),
            ('abc300', ProblemRef(ATCODER, 'abc300', 'abc300', None)),
            ('D', ProblemRef(ATCODER, 'D', 'D', None)),
        ],
    )
    def test_a_word_nothing_else_reads_may_be_an_atcoder_id(
        self, text: str, expected: ProblemRef
    ) -> None:
        assert parse_problem_ref(text) == expected

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            (
                '<https://codeforces.com/contest/1520/problem/D>',
                ProblemRef(CODEFORCES, '1520D', '1520', 'D'),
            ),
            ('< 1520D >', ProblemRef(CODEFORCES, '1520D', '1520', 'D')),
            ('1520\u00a0D', ProblemRef(CODEFORCES, '1520D', '1520', 'D')),
            ('\u3000921\u2003/\u200314 ', ProblemRef(CODEFORCES, '92114', '921', '14')),
            (
                '<atcoder.jp/contests/abc300/tasks/abc300_d>',
                ProblemRef(ATCODER, 'abc300_d', 'abc300', None),
            ),
        ],
        ids=[
            'link in brackets',
            'id in brackets',
            'no-break space',
            'wide spaces',
            'atcoder',
        ],
    )
    def test_brackets_and_any_space_are_read_as_discord_users_type_them(
        self, text: str, expected: ProblemRef
    ) -> None:
        assert parse_problem_ref(text) == expected

    @pytest.mark.parametrize(
        'text',
        [
            '',
            '1520',
            '92101',
            '1520 DDDD',
            'abc300 d',
            '_',
            'x' * 60 + '_' + 'y' * 10,
            'https://codeforces.com/contest/1520',
            'https://codeforces.com/gym/102000/problem/A',
            'https://codeforces.com.evil.example/contest/1520/problem/D',
            'https://atcoder.jp/contests/abc300',
            'ftp://atcoder.jp/contests/abc300/tasks/abc300_d',
            'https://[atcoder.jp/contests/abc300/tasks/abc300_d',
            'https://example.com/contests/abc300/tasks/abc300_d',
        ],
    )
    def test_anything_else_is_refused_with_examples(self, text: str) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_problem_ref(text)

        assert str(raised.value) == NOT_A_PROBLEM


class TestCatalog:
    def test_before_loading_nothing_can_be_read(self, catalog: ProblemCatalog) -> None:
        for platform, owner in ((CODEFORCES, "Codeforces'"), (ATCODER, "AtCoder's")):
            assert not catalog.loaded(platform)
            assert catalog.refreshed_at(platform) is None
            message = (
                f"{owner} problem list isn't loaded yet. Please try again in a "
                'few minutes.'
            )
            with pytest.raises(KcpcUserError) as raised:
                catalog.problems(platform)
            assert str(raised.value) == message
            with pytest.raises(KcpcUserError, match='loaded yet'):
                catalog.pool(platform)
        with pytest.raises(KcpcUserError, match="^AtCoder's problem list"):
            catalog.find(ProblemRef(ATCODER, 'abc300_d', 'abc300', None))
        assert catalog.tags() == frozenset()

    async def test_a_refresh_loads_both_lists_with_their_pools(
        self, catalog: ProblemCatalog, clock: FakeClock
    ) -> None:
        assert await catalog.refresh() == {}

        assert catalog.loaded(CODEFORCES) and catalog.loaded(ATCODER)
        assert catalog.refreshed_at(CODEFORCES) == NOW
        assert catalog.refreshed_at(ATCODER) == NOW
        assert ids(catalog.problems(CODEFORCES)) == [
            '1520D',
            '1520G',
            '2269F',
            '1958A',
            '1600A',
            '92101',
        ]
        assert ids(catalog.pool(CODEFORCES)) == ['1520D', '1520G']
        assert ids(catalog.problems(ATCODER)) == [
            'abc300_a',
            'abc300_d',
            'ahc001_a',
            'abc001_1',
        ]
        assert ids(catalog.pool(ATCODER)) == ['abc300_a', 'abc300_d']
        assert catalog.problems(CODEFORCES)[0].solved_count == 5_000

    async def test_tags_are_codeforces_tags_but_special(
        self, catalog: ProblemCatalog
    ) -> None:
        await catalog.refresh()

        assert catalog.tags() == {
            'data structures',
            'hashing',
            'math',
            'dfs and similar',
            'graphs',
            'greedy',
            'dp',
            'implementation',
            'games',
        }

    async def test_find_looks_problems_up_on_either_platform(
        self, catalog: ProblemCatalog
    ) -> None:
        await catalog.refresh()

        found = catalog.find(parse_problem_ref('1520 d'))
        assert found is not None and found.title == '1520D - Same Differences'
        labyrinth = catalog.find(parse_problem_ref('921 01'))
        assert labyrinth is not None and labyrinth.name == 'Labyrinth-1'
        assert catalog.find(parse_problem_ref('1520A')) is None
        aabcc = catalog.find(parse_problem_ref('ABC300_D'))
        assert aabcc is not None and aabcc.problem_id == 'abc300_d'
        url = 'https://atcoder.jp/contests/ahc001/tasks/ahc001_a'
        heuristic = catalog.find(parse_problem_ref(url))
        assert heuristic is not None and not heuristic.in_pool
        assert catalog.find(ProblemRef(ATCODER, 'abc999_z', 'abc999', None)) is None

    async def test_lists_says_whether_a_platform_has_a_problem(
        self, catalog: ProblemCatalog
    ) -> None:
        assert not catalog.lists(CODEFORCES, '1520D')  # nothing loaded yet

        assert await catalog.refresh() == {}

        assert catalog.lists(CODEFORCES, '1520D')
        assert not catalog.lists(CODEFORCES, '1520d')
        assert not catalog.lists(CODEFORCES, '1501C')
        assert catalog.lists(ATCODER, 'ABC300_D')
        with pytest.raises(ValueError):
            catalog.lists('leetcode', '1')

    async def test_each_list_is_fetched_again_once_older_than_its_max_age(
        self,
        catalog: ProblemCatalog,
        clock: FakeClock,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        assert (CODEFORCES_MAX_AGE, ATCODER_MAX_AGE) == (
            timedelta(hours=6),
            timedelta(hours=24),
        )
        await catalog.refresh()

        await clock.advance(CODEFORCES_MAX_AGE - timedelta(seconds=1))
        await catalog.refresh()
        assert (problemset.fetches, atcoder.fetches) == (1, 1)

        await clock.advance(timedelta(seconds=1))
        problemset.problems = [SAME_DIFFERENCES]
        await catalog.refresh()
        assert (problemset.fetches, atcoder.fetches) == (2, 1)
        assert ids(catalog.problems(CODEFORCES)) == ['1520D']
        assert catalog.refreshed_at(CODEFORCES) == NOW + CODEFORCES_MAX_AGE
        assert catalog.refreshed_at(ATCODER) == NOW

        await clock.advance_to(NOW + ATCODER_MAX_AGE)
        await catalog.refresh()
        assert (problemset.fetches, atcoder.fetches) == (3, 2)
        assert catalog.refreshed_at(ATCODER) == NOW + ATCODER_MAX_AGE

    async def test_a_forced_refresh_fetches_every_list(
        self,
        catalog: ProblemCatalog,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        await catalog.refresh()

        assert await catalog.refresh(force=True) == {}

        assert (problemset.fetches, atcoder.fetches) == (2, 2)

    async def test_a_failed_refresh_keeps_the_lists_there_were(
        self,
        catalog: ProblemCatalog,
        clock: FakeClock,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        await catalog.refresh()
        await clock.advance(timedelta(hours=1))
        codeforces_down = cf.TrueApiError('HTTP Error 503, Service Unavailable')
        problemset.error = codeforces_down
        atcoder.error = ATCODER_DOWN

        failures = await catalog.refresh(force=True)

        assert list(failures) == ['codeforces', 'atcoder']
        assert isinstance(failures[CODEFORCES], ExternalServiceError)
        assert failures[CODEFORCES].__cause__ is codeforces_down
        assert failures[ATCODER] is ATCODER_DOWN
        assert ids(catalog.pool(CODEFORCES)) == ['1520D', '1520G']
        assert ids(catalog.pool(ATCODER)) == ['abc300_a', 'abc300_d']
        assert catalog.refreshed_at(CODEFORCES) == NOW
        assert catalog.refreshed_at(ATCODER) == NOW

    async def test_a_list_that_failed_to_load_is_tried_again_alone(
        self,
        catalog: ProblemCatalog,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        atcoder.error = ATCODER_DOWN

        assert list(await catalog.refresh()) == [ATCODER]
        assert catalog.loaded(CODEFORCES) and not catalog.loaded(ATCODER)

        assert await catalog.refresh() == {}
        assert catalog.loaded(ATCODER)
        assert (problemset.fetches, atcoder.fetches) == (1, 1)

    async def test_an_empty_list_counts_as_a_failure(
        self,
        catalog: ProblemCatalog,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        await catalog.refresh()
        problemset.problems = []
        atcoder.problems = {}

        failures = await catalog.refresh(force=True)

        assert [str(failure) for failure in failures.values()] == [
            "Codeforces' problem list came back empty.",
            "AtCoder's problem list came back empty.",
        ]
        services = [
            failure.service
            for failure in failures.values()
            if isinstance(failure, ExternalServiceError)
        ]
        assert services == ['Codeforces', 'AtCoder']
        assert len(catalog.problems(CODEFORCES)) == 6
        assert len(catalog.problems(ATCODER)) == 4

    async def test_refreshes_take_turns(
        self,
        catalog: ProblemCatalog,
        problemset: FakeProblemset,
        atcoder: FakeAtCoderProblems,
    ) -> None:
        atcoder.gate = asyncio.Event()
        first = asyncio.create_task(catalog.refresh())
        second = asyncio.create_task(catalog.refresh())
        for _ in range(20):
            await asyncio.sleep(0)
        assert problemset.fetches == 1  # the first waits for AtCoder

        atcoder.gate.set()

        assert await asyncio.wait_for(asyncio.gather(first, second), 5) == [{}, {}]
        assert (problemset.fetches, atcoder.fetches) == (1, 1)

    def test_only_codeforces_and_atcoder_have_problems(
        self, catalog: ProblemCatalog
    ) -> None:
        with pytest.raises(ValueError, match="no problems of 'leetcode'"):
            catalog.loaded('leetcode')
        with pytest.raises(ValueError, match="no problems of 'leetcode'"):
            catalog.problems('leetcode')
