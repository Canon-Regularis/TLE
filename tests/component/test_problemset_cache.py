"""Tests for the problems of each contest (tle.util.cache.problemset): the
hourly update, which also runs at boot, and the bot owner's
;cache problemsets, one contest or all.

The cache runs on a real in-memory cache database and a real contest list.
Codeforces' problem list (problemset.problems) and the standings are faked,
and record each request; the cache's clock is the test's. The problem list
leaves out problems as Codeforces' does: of a round's two divisions, held in
parallel, it lists those they share under the Div. 1 only. Some tests use
contests shaped like the four whose standings the hourly update asked for
every hour on 12 October 2026.
"""

import asyncio
import logging
from collections.abc import Iterable
from typing import Any
from unittest.mock import patch

import pytest

from tle.util import codeforces_api as cf, codeforces_common as cf_common, tasks
from tle.util.cache import ContestNotFound
from tle.util.cache.cache_system import CacheSystem
from tle.util.cache.problemset import ProblemsetCache, ProblemsetUpdate
from tle.util.codeforces_api import Contest, Problem
from tle.util.db.cache_db_conn import CacheDbConn
from tle.util.events import EventSystem

HOUR = 60 * 60
DAY = 24 * HOUR
# When the tests run, on the cache's clock.
NOW = 1_790_000_000
LENGTH = 2 * HOUR

# A round's two divisions, held in parallel, and a round on its own; each
# ended a few days ago.
DIV1, DIV2 = 2101, 2102
SOLO = 2103
# A contest the problem list leaves out, such as Kotlin Heroes.
UNLISTED = 2104
# The same, long ago.
OLD_DIV1, OLD_DIV2 = 2001, 2002
OLD_SOLO = 2003
OLD_UNLISTED = 2004
# A contest still running.
RUNNING = 2105
# A contest that finishes while a test runs, and the list leaves out.
LATER = 2106
# A contest that the contest list no longer has.
GONE = 1500


def make_contest(
    contest_id: int, name: str, *, ended_days_ago: int, phase: str = 'FINISHED'
) -> Contest:
    end = NOW - ended_days_ago * DAY
    return Contest(
        id=contest_id,
        name=name,
        startTimeSeconds=end - LENGTH,
        durationSeconds=LENGTH,
        type='CF',
        phase=phase,
        preparedBy=None,
    )


CONTESTS = [
    make_contest(OLD_UNLISTED, 'Kotlin Heroes: Episode 19', ended_days_ago=150),
    make_contest(OLD_SOLO, 'Codeforces Round 990 (Div. 3)', ended_days_ago=120),
    make_contest(OLD_DIV1, 'Codeforces Round 995 (Div. 1)', ended_days_ago=100),
    make_contest(OLD_DIV2, 'Codeforces Round 995 (Div. 2)', ended_days_ago=100),
    make_contest(UNLISTED, 'Kotlin Heroes: Episode 20', ended_days_ago=4),
    make_contest(SOLO, 'Codeforces Round 1002 (Div. 3)', ended_days_ago=3),
    make_contest(DIV1, 'Codeforces Round 1001 (Div. 1)', ended_days_ago=2),
    make_contest(DIV2, 'Codeforces Round 1001 (Div. 2)', ended_days_ago=2),
    Contest(
        id=RUNNING,
        name='Codeforces Round 1003 (Div. 2)',
        startTimeSeconds=NOW - HOUR,
        durationSeconds=LENGTH,
        type='CF',
        phase='CODING',
        preparedBy=None,
    ),
]
CONTEST_BY_ID = {contest.id: contest for contest in CONTESTS}
PAIR_START = CONTEST_BY_ID[DIV1].startTimeSeconds
OLD_PAIR_START = CONTEST_BY_ID[OLD_DIV1].startTimeSeconds


def problem(
    contest_id: int,
    index: str,
    name: str,
    *,
    points: float | None = None,
    rating: int | None = None,
    tags: Iterable[str] = (),
) -> Problem:
    return Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=points,
        rating=rating,
        tags=list(tags),
    )


# What each contest's standings show: points, and ratings once Codeforces has
# rated the problems, which it hasn't for the recent rounds yet.
STANDINGS = {
    DIV1: [
        problem(DIV1, 'A', 'Grid Walk', points=500.0),
        problem(DIV1, 'B', 'Tree Paint', points=1000.0),
        problem(DIV1, 'C', 'Hard Queries', points=1750.0),
    ],
    # Its C and D are the Div. 1's A and B, worth more points here.
    DIV2: [
        problem(DIV2, 'A', 'Two Buttons', points=500.0),
        problem(DIV2, 'B', 'Three Lamps', points=1000.0),
        problem(DIV2, 'C', 'Grid Walk', points=1500.0),
        problem(DIV2, 'D', 'Tree Paint', points=2000.0),
    ],
    SOLO: [
        problem(SOLO, 'A', 'Lucky Sum', rating=900, tags=['math']),
        problem(SOLO, 'B', 'Even Split', rating=1300, tags=['greedy']),
    ],
    UNLISTED: [
        problem(UNLISTED, 'A', 'Kotlin Start'),
        problem(UNLISTED, 'B', 'Kotlin Finish'),
    ],
    OLD_DIV1: [
        problem(OLD_DIV1, 'A', 'Old Shared', points=500.0),
        problem(OLD_DIV1, 'B', 'Old Hard', points=1000.0),
    ],
    OLD_DIV2: [
        problem(OLD_DIV2, 'A', 'Old Easy', points=500.0),
        problem(OLD_DIV2, 'B', 'Old Shared', points=1000.0),
    ],
    OLD_SOLO: [problem(OLD_SOLO, 'A', 'Old Sum', rating=1000, tags=['math'])],
    OLD_UNLISTED: [problem(OLD_UNLISTED, 'A', 'Old Kotlin', rating=1200)],
    RUNNING: [problem(RUNNING, 'A', 'Live One', points=500.0)],
}
# Codeforces' problem list, with ratings and tags.
LISTED = [
    # The Div. 2 lists only its own problems.
    problem(DIV2, 'A', 'Two Buttons', points=500.0, rating=800, tags=['math']),
    problem(DIV2, 'B', 'Three Lamps', points=1000.0, rating=1000, tags=['greedy']),
    problem(DIV1, 'A', 'Grid Walk', points=500.0, rating=1400, tags=['dp']),
    problem(DIV1, 'B', 'Tree Paint', points=1000.0, rating=1900, tags=['trees']),
    problem(DIV1, 'C', 'Hard Queries', points=1750.0, rating=2600, tags=['graphs']),
    problem(SOLO, 'A', 'Lucky Sum', rating=900, tags=['math']),
    problem(SOLO, 'B', 'Even Split', rating=1300, tags=['greedy']),
    problem(OLD_DIV2, 'A', 'Old Easy', points=500.0, rating=800, tags=['math']),
    problem(OLD_DIV1, 'A', 'Old Shared', points=500.0, rating=1600, tags=['dp']),
    problem(OLD_DIV1, 'B', 'Old Hard', points=1000.0, rating=2400, tags=['flows']),
    problem(OLD_SOLO, 'A', 'Old Sum', rating=1000, tags=['math']),
    # A problem of a contest the bot doesn't know.
    problem(9999, 'A', 'Unknown', rating=1500),
]

# What the bot stores for each contest: the standings' problems, each with the
# rating and tags the problem list has for it, by index or else by name.
EXPECTED = {
    DIV1: [
        problem(DIV1, 'A', 'Grid Walk', points=500.0, rating=1400, tags=['dp']),
        problem(DIV1, 'B', 'Tree Paint', points=1000.0, rating=1900, tags=['trees']),
        problem(DIV1, 'C', 'Hard Queries', points=1750.0, rating=2600, tags=['graphs']),
    ],
    DIV2: [
        problem(DIV2, 'A', 'Two Buttons', points=500.0, rating=800, tags=['math']),
        problem(DIV2, 'B', 'Three Lamps', points=1000.0, rating=1000, tags=['greedy']),
        # Rated and tagged by name, from the Div. 1, but worth its own points.
        problem(DIV2, 'C', 'Grid Walk', points=1500.0, rating=1400, tags=['dp']),
        problem(DIV2, 'D', 'Tree Paint', points=2000.0, rating=1900, tags=['trees']),
    ],
    SOLO: [
        problem(SOLO, 'A', 'Lucky Sum', rating=900, tags=['math']),
        problem(SOLO, 'B', 'Even Split', rating=1300, tags=['greedy']),
    ],
    UNLISTED: STANDINGS[UNLISTED],
    OLD_DIV1: [
        problem(OLD_DIV1, 'A', 'Old Shared', points=500.0, rating=1600, tags=['dp']),
        problem(OLD_DIV1, 'B', 'Old Hard', points=1000.0, rating=2400, tags=['flows']),
    ],
    OLD_DIV2: [
        problem(OLD_DIV2, 'A', 'Old Easy', points=500.0, rating=800, tags=['math']),
        problem(OLD_DIV2, 'B', 'Old Shared', points=1000.0, rating=1600, tags=['dp']),
    ],
    OLD_SOLO: STANDINGS[OLD_SOLO],
    OLD_UNLISTED: STANDINGS[OLD_UNLISTED],
}

# The four contests of 12 October 2026, as Codeforces had them: a combined
# round and a Div. 3 round, each held alone, and the two divisions of Round
# 1127, held in parallel. None of their problems was rated yet. The problem
# names are made up, and so is the Div. 2's standings beyond the A and B that
# the problem list has: its C to F are the Div. 1's A to D, worth more points,
# as in other rounds.
COMBINED, ROUND_DIV1, ROUND_DIV2, ROUND_DIV3 = 2271, 2273, 2274, 2275
# When Round 1127 started: 2026-10-11 18:35 UTC.
ROUND_START = 1_791_743_700
# An hourly update on that day, at 23:07 UTC.
OCTOBER_12 = 1_791_846_420


def contest_of_october(
    contest_id: int, name: str, *, start: int, length: int, kind: str
) -> Contest:
    return Contest(
        id=contest_id,
        name=name,
        startTimeSeconds=start,
        durationSeconds=length,
        type=kind,
        phase='FINISHED',
        preparedBy=None,
    )


OCTOBER_CONTESTS = [
    contest_of_october(
        ROUND_DIV3,
        'Codeforces Round 1125 (Div. 3)',
        start=1_791_384_300,
        length=9000,
        kind='ICPC',
    ),
    contest_of_october(
        COMBINED,
        'DeepInfra Round 1 (Codeforces Round 1126, Div. 1 + Div. 2)',
        start=1_791_644_700,
        length=10800,
        kind='CF',
    ),
    contest_of_october(
        ROUND_DIV1,
        'Codeforces Round 1127 (Div. 1, Based on Mexican Olympiad in Informatics 2026)',
        start=ROUND_START,
        length=7200,
        kind='CF',
    ),
    contest_of_october(
        ROUND_DIV2,
        'Codeforces Round 1127 (Div. 2, Based on Mexican Olympiad in Informatics 2026)',
        start=ROUND_START,
        length=7200,
        kind='CF',
    ),
]
# The Div. 1's problems, as its standings and the problem list have them.
ROUND_DIV1_PROBLEMS = [
    problem(ROUND_DIV1, index, name, points=points, tags=tags)
    for index, name, points, tags in (
        ('A', 'Pyramid Steps', 500.0, ['greedy']),
        ('B', 'Cactus Garden', 1000.0, ['constructive algorithms']),
        ('C', 'Painted Tiles', 1250.0, ['dp']),
        ('D', 'Lucky Rivers', 2000.0, ['graphs', 'trees']),
        ('E', 'Market Day', 2250.0, ['data structures']),
        ('F', 'Volcano Paths', 3500.0, ['math']),
    )
]
ROUND_DIV2_OWN = [
    problem(ROUND_DIV2, 'A', 'Two Pesos', points=500.0, tags=['implementation']),
    problem(ROUND_DIV2, 'B', 'Folded Map', points=1250.0, tags=['brute force']),
]
OCTOBER_STANDINGS = {
    COMBINED: [
        problem(COMBINED, index, f'Round 1126 {index}', points=points)
        for index, points in (
            ('A', 500.0),
            ('B', 1000.0),
            ('C', 1250.0),
            ('D', 1750.0),
            ('E', 2250.0),
            ('F', 2500.0),
            ('G', 3500.0),
            ('H1', 3000.0),
            ('H2', 2000.0),
        )
    ],
    ROUND_DIV1: ROUND_DIV1_PROBLEMS,
    ROUND_DIV2: [
        *ROUND_DIV2_OWN,
        *(
            shared._replace(contestId=ROUND_DIV2, index=index, points=points)
            for shared, index, points in zip(
                ROUND_DIV1_PROBLEMS[:4],
                'CDEF',
                (1500.0, 2000.0, 2250.0, 3000.0),
                strict=True,
            )
        ),
    ],
    # In ICPC format, without points.
    ROUND_DIV3: [
        problem(ROUND_DIV3, index, f'Round 1125 {index}') for index in 'ABCDEFGH'
    ],
}
# What the problem list had: the Div. 2 only with its A and B.
OCTOBER_LISTED = [
    *OCTOBER_STANDINGS[COMBINED],
    *ROUND_DIV1_PROBLEMS,
    *ROUND_DIV2_OWN,
    *OCTOBER_STANDINGS[ROUND_DIV3],
]


class FakeCodeforces:
    """Codeforces' problem list and standings, recording each request."""

    def __init__(self) -> None:
        self.contests = dict(CONTEST_BY_ID)
        self.listed = list(LISTED)
        self.standings = {key: list(value) for key, value in STANDINGS.items()}
        self.list_fails = False
        # The contests whose standings Codeforces fails to give, and those
        # whose standings break with an error that isn't Codeforces'.
        self.failing: set[int] = set()
        self.broken: dict[int, Exception] = {}
        # The contests whose standings never come, and what is set once they
        # are asked for.
        self.hanging: set[int] = set()
        self.hung = asyncio.Event()
        self.requests: list[str] = []

    def have(
        self,
        contests: Iterable[Contest],
        standings: dict[int, list[Problem]],
        listed: Iterable[Problem],
    ) -> None:
        """Have only ``contests``, with ``standings``, and list ``listed``."""
        self.contests = {contest.id: contest for contest in contests}
        self.standings = {key: list(value) for key, value in standings.items()}
        self.listed = list(listed)

    async def problems(
        self, **options: Any
    ) -> tuple[list[Problem], list[cf.ProblemStatistics]]:
        assert options == {}, 'the whole list'
        self.requests.append('problemset.problems')
        if self.list_fails:
            raise cf.ClientError()
        return list(self.listed), []

    async def contest_standings(
        self, *, contest_id: int
    ) -> tuple[Contest, list[Problem], list[cf.RanklistRow]]:
        # Keyword-only, and nothing but the contest: Codeforces refuses any
        # other parameter.
        self.requests.append(f'contest.standings {contest_id}')
        if contest_id in self.failing:
            raise cf.TrueApiError('HTTP Error 503, Service Unavailable')
        if contest_id in self.broken:
            raise self.broken[contest_id]
        if contest_id in self.hanging:
            self.hung.set()
            await asyncio.Event().wait()
        return self.contests[contest_id], list(self.standings[contest_id]), []

    def standings_requests(self) -> list[int]:
        prefix = 'contest.standings '
        return [
            int(request.removeprefix(prefix))
            for request in self.requests
            if request.startswith(prefix)
        ]


class Clock:
    def __init__(self) -> None:
        self.now: float = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.problemset, 'problems', fake.problems)
    monkeypatch.setattr(cf.contest, 'standings', fake.contest_standings)
    return fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def load_contests(system: CacheSystem, contests: list[Contest]) -> None:
    """Load ``contests`` into the contest list, as from disk at boot."""
    with patch.object(cf_common, 'event_sys', EventSystem()):
        await system.contest_cache._update(list(contests), from_api=False)


async def make_cache(
    cache_db: CacheDbConn, clock: Clock, contests: list[Contest]
) -> ProblemsetCache:
    """The problemset cache of a bot whose contest list has ``contests``."""
    system = CacheSystem(cache_db)
    await load_contests(system, contests)
    return ProblemsetCache(system, clock=clock)


@pytest.fixture
async def cache(cache_db: CacheDbConn, clock: Clock) -> ProblemsetCache:
    """The problemset cache of a bot whose contest list has CONTESTS."""
    return await make_cache(cache_db, clock, CONTESTS)


@pytest.fixture
def saves(cache_db: CacheDbConn, monkeypatch: pytest.MonkeyPatch) -> list[Problem]:
    """Every problem saved to the table by an update, in order."""
    saved: list[Problem] = []
    save = cache_db.cache_problemset

    async def recorded(problems: list[Problem]) -> int:
        saved.extend(problems)
        return await save(problems)

    monkeypatch.setattr(cache_db, 'cache_problemset', recorded)
    return saved


async def hourly(cache: ProblemsetCache) -> None:
    """Run the hourly update once, as its task does, raising what it raises."""
    task = cache._update_task
    assert isinstance(task, tasks.Task)
    await task.func(cache, None)


async def stored(cache_db: CacheDbConn) -> dict[int, list[Problem]]:
    """The stored problems, by contest, in index order."""
    found: dict[int, list[Problem]] = {}
    for row in sorted(
        await cache_db.fetch_problems2(), key=lambda row: (row.contestId, row.index)
    ):
        assert row.contestId is not None
        found.setdefault(row.contestId, []).append(row)
    return found


def expected(*contest_ids: int) -> dict[int, list[Problem]]:
    return {contest_id: EXPECTED[contest_id] for contest_id in contest_ids}


def by_contest_and_index(problems: list[Problem]) -> list[Problem]:
    return sorted(problems, key=lambda row: (row.contestId, row.index))


def warnings_and_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records of any logger at WARNING or above: those that reach the
    Discord log channel.
    """
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


async def assert_asked_again_after_24_hours(
    cache: ProblemsetCache,
    codeforces: FakeCodeforces,
    clock: Clock,
    contest_id: int,
    failed_at: float,
) -> None:
    """Assert that the hourly update asks for the standings of contest
    ``contest_id``, which failed at ``failed_at``, only once more than 24
    hours have passed.
    """
    asked = codeforces.standings_requests().count(contest_id)
    for later in (HOUR, 23 * HOUR, DAY):
        clock.now = failed_at + later
        await hourly(cache)
        assert codeforces.standings_requests().count(contest_id) == asked
    clock.now = failed_at + DAY + 1
    await hourly(cache)
    assert codeforces.standings_requests().count(contest_id) == asked + 1


# The hourly update


async def test_the_hourly_update_fills_in_the_contests_it_can(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    await hourly(cache)

    # From the problem list: the round on its own, however old. From one
    # request for the standings each: the recent contests that the list leaves
    # incomplete, which are those held in parallel and those it leaves out.
    assert await stored(cache_db) == expected(OLD_SOLO, SOLO, UNLISTED, DIV1, DIV2)
    assert codeforces.requests[0] == 'problemset.problems'
    assert sorted(codeforces.standings_requests()) == [DIV1, DIV2, UNLISTED]


async def test_a_round_s_divisions_get_their_standings_once_and_quietly(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await hourly(cache)
        codeforces.requests.clear()

        await hourly(cache)
        await hourly(cache)

    # Their stored problems are complete: each later update makes 1 request,
    # for the problem list.
    assert codeforces.requests == ['problemset.problems'] * 2
    assert (await stored(cache_db))[DIV2] == EXPECTED[DIV2]
    # Nothing reaches the log channel.
    assert warnings_and_errors(caplog) == []


async def test_a_problem_both_divisions_share_belongs_to_both_contests(
    cache: ProblemsetCache, codeforces: FakeCodeforces
) -> None:
    # As the checks of the problems a member has tried in a contest read it.
    await hourly(cache)

    assert sorted(cache.problem_to_contests['Grid Walk', PAIR_START]) == [DIV1, DIV2]
    assert cache.problem_to_contests['Two Buttons', PAIR_START] == [DIV2]
    assert {problem.name for problem in cache.problems} >= {'Lucky Sum', 'Old Sum'}


async def test_older_contests_and_unfinished_ones_are_left_alone(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    await hourly(cache)

    left = {OLD_DIV1, OLD_DIV2, OLD_UNLISTED, RUNNING}
    assert left.isdisjoint(await stored(cache_db))
    assert left.isdisjoint(codeforces.standings_requests())


@pytest.mark.parametrize(
    ('since_end', 'fetched'),
    [(14 * DAY - 1, True), (14 * DAY, True), (14 * DAY + 1, False)],
    ids=['before 14 days', 'at 14 days', 'after 14 days'],
)
async def test_standings_are_fetched_for_14_days_after_a_contest_ends(
    cache: ProblemsetCache,
    codeforces: FakeCodeforces,
    clock: Clock,
    since_end: int,
    fetched: bool,
) -> None:
    end = CONTEST_BY_ID[UNLISTED].end_time
    assert end is not None
    clock.now = end + since_end

    await hourly(cache)

    assert (UNLISTED in codeforces.standings_requests()) is fetched


async def test_a_codeforces_error_waits_24_hours_and_keeps_the_rest(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codeforces.failing = {UNLISTED}
    failed_at = clock.now = NOW + 10

    with caplog.at_level(logging.INFO):
        await hourly(cache)

    # The others are saved. The error is only information here: the API
    # client logs what Codeforces answered, and says when an outage is long.
    assert await stored(cache_db) == expected(OLD_SOLO, SOLO, DIV1, DIV2)
    assert (
        "Standings of contest 2104 not fetched. TrueApiError('Codeforces API error')"
    ) in caplog.messages
    assert warnings_and_errors(caplog) == []

    # Not asked for again until more than 24 hours have passed.
    codeforces.failing = set()
    await assert_asked_again_after_24_hours(
        cache, codeforces, clock, UNLISTED, failed_at
    )
    assert (await stored(cache_db))[UNLISTED] == EXPECTED[UNLISTED]


@pytest.mark.parametrize(
    'error',
    [RuntimeError('broken response'), KeyError('problems'), asyncio.TimeoutError()],
    ids=['RuntimeError', 'KeyError', 'timeout'],
)
async def test_any_other_error_is_a_warning_and_waits_24_hours_too(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    # Such as an answer that the API client can't read, or a bug.
    codeforces.broken = {UNLISTED: error}
    failed_at = clock.now = NOW + 10

    with caplog.at_level(logging.INFO):
        # It doesn't reach the task, which would log it again, every hour.
        await hourly(cache)

    # The others are saved, and the warning that reaches the log channel has
    # the traceback.
    assert await stored(cache_db) == expected(OLD_SOLO, SOLO, DIV1, DIV2)
    (warning,) = warnings_and_errors(caplog)
    assert warning.levelno == logging.WARNING
    assert warning.getMessage() == 'Problems of contest 2104 not saved.'
    assert warning.exc_info is not None and warning.exc_info[1] is error

    codeforces.broken = {}
    await assert_asked_again_after_24_hours(
        cache, codeforces, clock, UNLISTED, failed_at
    )
    assert (await stored(cache_db))[UNLISTED] == EXPECTED[UNLISTED]


async def test_standings_without_problems_wait_24_hours_too(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Codeforces answers, but with nothing to save.
    codeforces.standings[UNLISTED] = []
    failed_at = clock.now = NOW + 10

    with caplog.at_level(logging.INFO):
        await hourly(cache)

    assert await stored(cache_db) == expected(OLD_SOLO, SOLO, DIV1, DIV2)
    assert 'Standings of contest 2104 have no problems.' in caplog.messages
    assert warnings_and_errors(caplog) == []

    # Not asked for again every hour.
    codeforces.standings[UNLISTED] = list(STANDINGS[UNLISTED])
    await assert_asked_again_after_24_hours(
        cache, codeforces, clock, UNLISTED, failed_at
    )
    assert (await stored(cache_db))[UNLISTED] == EXPECTED[UNLISTED]


async def test_each_contest_waits_24_hours_from_its_own_failure(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    clock: Clock,
) -> None:
    codeforces.failing = {UNLISTED}
    first = clock.now = NOW + 10
    await hourly(cache)
    # A contest that finishes 11 hours later, whose standings fail too.
    later = Contest(
        id=LATER,
        name='Kotlin Heroes: Practice 20',
        startTimeSeconds=first + 11 * HOUR - LENGTH,
        durationSeconds=LENGTH,
        type='ICPC',
        phase='FINISHED',
        preparedBy=None,
    )
    codeforces.contests[LATER] = later
    codeforces.standings[LATER] = [problem(LATER, 'A', 'Kotlin Practice')]
    await load_contests(cache.cache_master, [*CONTESTS, later])
    codeforces.failing = {UNLISTED, LATER}
    second = clock.now = first + 12 * HOUR
    await hourly(cache)
    assert codeforces.standings_requests().count(UNLISTED) == 1
    assert codeforces.standings_requests().count(LATER) == 1

    # Each is asked for again once 24 hours have passed since its own failure.
    codeforces.failing = set()
    clock.now = first + DAY + 1
    await hourly(cache)
    assert codeforces.standings_requests().count(UNLISTED) == 2
    assert codeforces.standings_requests().count(LATER) == 1
    clock.now = second + DAY + 1
    await hourly(cache)
    assert codeforces.standings_requests().count(LATER) == 2

    found = await stored(cache_db)
    assert found[UNLISTED] == EXPECTED[UNLISTED]
    assert found[LATER] == [problem(LATER, 'A', 'Kotlin Practice')]


@pytest.mark.parametrize(
    ('failing', 'summary'),
    [
        (
            {UNLISTED},
            'Problems of contest 2104 still missing; the hourly update tries'
            ' again after 24 hours, until 14 days after the contest ended.',
        ),
        (
            {UNLISTED, DIV2},
            'Problems of 2 contests still missing: 2102, 2104. The hourly update'
            ' tries each again after 24 hours, until 14 days after it ended.',
        ),
    ],
    ids=['one contest', 'two contests'],
)
async def test_the_hourly_update_sums_up_the_contests_still_missing(
    cache: ProblemsetCache,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
    failing: set[int],
    summary: str,
) -> None:
    codeforces.failing = failing

    with caplog.at_level(logging.INFO):
        await hourly(cache)

    # Last, and only as information: each failure is logged as it happens.
    assert caplog.records[-1].getMessage() == summary
    assert caplog.records[-1].levelno == logging.INFO
    assert warnings_and_errors(caplog) == []


async def test_the_hourly_update_saves_only_new_or_changed_problems(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    saves: list[Problem],
) -> None:
    await hourly(cache)
    assert by_contest_and_index(saves) == [
        row for rows in (await stored(cache_db)).values() for row in rows
    ]
    saves.clear()

    await hourly(cache)
    assert saves == []

    # Codeforces rates the Div. 1's A again, and tags a problem of the round
    # on its own anew.
    codeforces.listed = [
        listed._replace(rating=1500)
        if (listed.contestId, listed.index) == (DIV1, 'A')
        else listed._replace(tags=['greedy', 'sortings'])
        if (listed.contestId, listed.index) == (SOLO, 'B')
        else listed
        for listed in LISTED
    ]
    await hourly(cache)

    # The A of each division, by index and by name, and the retagged one.
    assert by_contest_and_index(saves) == [
        EXPECTED[DIV1][0]._replace(rating=1500),
        EXPECTED[DIV2][2]._replace(rating=1500),
        EXPECTED[SOLO][1]._replace(tags=['greedy', 'sortings']),
    ]


async def test_problems_stored_for_parallel_contests_are_complete(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
) -> None:
    # As earlier versions of the bot stored them, from the standings, before
    # Codeforces rated the problems.
    await cache_db.cache_problemset(STANDINGS[DIV1] + STANDINGS[DIV2])

    await hourly(cache)

    # Not fetched again, but rated and tagged.
    assert DIV1 not in codeforces.standings_requests()
    assert DIV2 not in codeforces.standings_requests()
    assert await stored(cache_db) == expected(OLD_SOLO, SOLO, UNLISTED, DIV1, DIV2)


async def test_a_rating_or_tags_the_list_lacks_never_replace_known_ones(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
) -> None:
    await cache_db.cache_problemset(EXPECTED[DIV1] + EXPECTED[DIV2])
    codeforces.listed = [
        listed._replace(rating=None, tags=[])
        if listed.contestId in (DIV1, DIV2)
        else listed
        for listed in LISTED
    ]

    await hourly(cache)

    stored_now = await stored(cache_db)
    assert stored_now[DIV1] == EXPECTED[DIV1]
    assert stored_now[DIV2] == EXPECTED[DIV2]


async def test_a_name_match_gives_only_the_rating_and_tags(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    # The Div. 1's problem of the same name differs in everything else.
    codeforces.listed = [
        listed._replace(points=1.0, type='QUESTION', index='Z')
        if (listed.contestId, listed.name) == (DIV1, 'Grid Walk')
        else listed
        for listed in LISTED
    ]

    await hourly(cache)

    assert (await stored(cache_db))[DIV2][2] == problem(
        DIV2, 'C', 'Grid Walk', points=1500.0, rating=1400, tags=['dp']
    )


@pytest.mark.parametrize('unrated_first', [True, False])
async def test_a_name_match_prefers_a_rated_problem(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    unrated_first: bool,
) -> None:
    # The list has the problem twice in the round, rated once.
    unrated = problem(DIV1, 'Z', 'Grid Walk', points=500.0)
    codeforces.listed = [unrated, *LISTED] if unrated_first else [*LISTED, unrated]

    await hourly(cache)

    assert (await stored(cache_db))[DIV2] == EXPECTED[DIV2]


async def test_a_name_match_looks_only_at_contests_that_started_together(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    # A problem of the same name in a contest held on another day.
    codeforces.listed = [
        problem(OLD_SOLO, 'B', 'Two Buttons', rating=3000, tags=['fft']),
        *(listed for listed in LISTED if listed.contestId != DIV2),
    ]

    await hourly(cache)

    assert (await stored(cache_db))[DIV2][0] == STANDINGS[DIV2][0]


# Three contests held together, such as a round's three divisions. The list
# has each problem they share under the hardest division that has it only.
TRIPLE_DIV1, TRIPLE_DIV2, TRIPLE_DIV3 = 2301, 2302, 2303
TRIPLE_CONTESTS = [
    make_contest(contest_id, f'Codeforces Round 1200 ({division})', ended_days_ago=1)
    for contest_id, division in (
        (TRIPLE_DIV1, 'Div. 1'),
        (TRIPLE_DIV2, 'Div. 2'),
        (TRIPLE_DIV3, 'Div. 3'),
    )
]
TRIPLE_START = TRIPLE_CONTESTS[0].startTimeSeconds
TRIPLE_STANDINGS = {
    TRIPLE_DIV1: [
        problem(TRIPLE_DIV1, 'A', 'Bridges', points=750.0),
        problem(TRIPLE_DIV1, 'B', 'Towers', points=1500.0),
    ],
    TRIPLE_DIV2: [
        problem(TRIPLE_DIV2, 'A', 'Coins', points=500.0),
        problem(TRIPLE_DIV2, 'B', 'Bridges', points=1000.0),
        problem(TRIPLE_DIV2, 'C', 'Towers', points=2000.0),
    ],
    TRIPLE_DIV3: [
        problem(TRIPLE_DIV3, 'A', 'Apples', points=500.0),
        problem(TRIPLE_DIV3, 'B', 'Coins', points=750.0),
        problem(TRIPLE_DIV3, 'C', 'Bridges', points=1250.0),
    ],
}
TRIPLE_LISTED = [
    problem(TRIPLE_DIV1, 'A', 'Bridges', points=750.0, rating=1700, tags=['graphs']),
    problem(TRIPLE_DIV1, 'B', 'Towers', points=1500.0, rating=2300, tags=['dp']),
    problem(TRIPLE_DIV2, 'A', 'Coins', points=500.0, rating=1100, tags=['greedy']),
    problem(TRIPLE_DIV3, 'A', 'Apples', points=500.0, rating=800, tags=['math']),
]


async def test_three_contests_held_together_each_get_their_standings_once(
    cache_db: CacheDbConn, codeforces: FakeCodeforces, clock: Clock
) -> None:
    codeforces.have(TRIPLE_CONTESTS, TRIPLE_STANDINGS, TRIPLE_LISTED)
    cache = await make_cache(cache_db, clock, TRIPLE_CONTESTS)

    await hourly(cache)

    # The list has none of them whole, even the Div. 1, whose problems it all
    # has: the others might share more with it.
    assert codeforces.standings_requests() == [TRIPLE_DIV3, TRIPLE_DIV2, TRIPLE_DIV1]
    # Rated and tagged by name from the division that the list has each
    # problem under, each keeping its own points.
    assert await stored(cache_db) == {
        TRIPLE_DIV1: TRIPLE_LISTED[:2],
        TRIPLE_DIV2: [
            TRIPLE_LISTED[2],
            problem(
                TRIPLE_DIV2, 'B', 'Bridges', points=1000.0, rating=1700, tags=['graphs']
            ),
            problem(
                TRIPLE_DIV2, 'C', 'Towers', points=2000.0, rating=2300, tags=['dp']
            ),
        ],
        TRIPLE_DIV3: [
            TRIPLE_LISTED[3],
            problem(
                TRIPLE_DIV3, 'B', 'Coins', points=750.0, rating=1100, tags=['greedy']
            ),
            problem(
                TRIPLE_DIV3, 'C', 'Bridges', points=1250.0, rating=1700, tags=['graphs']
            ),
        ],
    }
    assert sorted(cache.problem_to_contests['Bridges', TRIPLE_START]) == [
        TRIPLE_DIV1,
        TRIPLE_DIV2,
        TRIPLE_DIV3,
    ]

    codeforces.requests.clear()
    await hourly(cache)
    assert codeforces.requests == ['problemset.problems']


async def test_without_finished_contests_nothing_is_requested(
    cache_db: CacheDbConn, codeforces: FakeCodeforces, clock: Clock
) -> None:
    cache = await make_cache(cache_db, clock, [CONTEST_BY_ID[RUNNING]])

    await hourly(cache)

    assert codeforces.requests == []


async def test_when_the_problem_list_fails_nothing_is_fetched_or_changed(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await cache_db.cache_problemset(STANDINGS[DIV2])
    codeforces.list_fails = True

    with caplog.at_level(logging.INFO, logger='ProblemsetCache'):
        await hourly(cache)

    assert codeforces.requests == ['problemset.problems']
    assert await stored(cache_db) == {DIV2: STANDINGS[DIV2]}
    # Logged by the API client as it fails: here only as information.
    assert [record.levelno for record in caplog.records] == [logging.INFO]
    assert caplog.records[0].getMessage().startswith('Problems not updated.')


# The contests of 12 October 2026


@pytest.fixture
async def october(
    cache_db: CacheDbConn, codeforces: FakeCodeforces, clock: Clock
) -> ProblemsetCache:
    """The problemset cache of a bot whose contest list has the contests of
    12 October 2026, at an hourly update that day, with nothing stored for
    them.
    """
    codeforces.have(OCTOBER_CONTESTS, OCTOBER_STANDINGS, OCTOBER_LISTED)
    clock.now = OCTOBER_12
    return await make_cache(cache_db, clock, OCTOBER_CONTESTS)


# The ratings that Codeforces gives the problems it lists, days later.
OCTOBER_RATINGS = {
    (listed.contestId, listed.index): 800 + 100 * n
    for n, listed in enumerate(OCTOBER_LISTED)
}
OCTOBER_RATED = [
    listed._replace(rating=OCTOBER_RATINGS[listed.contestId, listed.index])
    for listed in OCTOBER_LISTED
]
# The Div. 2's problems, rated: its own by index, the others by name.
ROUND_DIV2_RATED = [
    rated._replace(rating=OCTOBER_RATINGS[source])
    for rated, source in zip(
        OCTOBER_STANDINGS[ROUND_DIV2],
        [
            (ROUND_DIV2, 'A'),
            (ROUND_DIV2, 'B'),
            (ROUND_DIV1, 'A'),
            (ROUND_DIV1, 'B'),
            (ROUND_DIV1, 'C'),
            (ROUND_DIV1, 'D'),
        ],
        strict=True,
    )
]


async def test_the_contests_of_12_october_need_the_standings_of_the_pair_once(
    october: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        await hourly(october)
        first = list(codeforces.requests)
        codeforces.requests.clear()
        await hourly(october)

    # The rounds held alone come whole from the problem list. The divisions
    # of Round 1127 need their standings, the newest contest first: once.
    assert first == [
        'problemset.problems',
        f'contest.standings {ROUND_DIV2}',
        f'contest.standings {ROUND_DIV1}',
    ]
    assert codeforces.requests == ['problemset.problems']
    # Without ratings yet, and the Div. 2 with all its problems, some of
    # which are the Div. 1's too.
    assert await stored(cache_db) == {
        COMBINED: OCTOBER_STANDINGS[COMBINED],
        ROUND_DIV1: ROUND_DIV1_PROBLEMS,
        ROUND_DIV2: OCTOBER_STANDINGS[ROUND_DIV2],
        ROUND_DIV3: OCTOBER_STANDINGS[ROUND_DIV3],
    }
    assert sorted(october.problem_to_contests['Pyramid Steps', ROUND_START]) == [
        ROUND_DIV1,
        ROUND_DIV2,
    ]
    # The old update logged 8 warnings an hour for them; this one, none.
    assert warnings_and_errors(caplog) == []


async def test_the_problems_of_12_october_get_ratings_once_codeforces_has_them(
    october: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    clock: Clock,
    saves: list[Problem],
) -> None:
    await hourly(october)
    saves.clear()
    codeforces.requests.clear()

    codeforces.listed = OCTOBER_RATED
    clock.now = OCTOBER_12 + 3 * DAY
    await hourly(october)

    # From the problem list alone, by index, and for the Div. 2's problems
    # that the list has under the Div. 1, by name; points stay the Div. 2's.
    assert codeforces.requests == ['problemset.problems']
    found = await stored(cache_db)
    for contest_id in (COMBINED, ROUND_DIV1, ROUND_DIV3):
        assert found[contest_id] == [
            rated for rated in OCTOBER_RATED if rated.contestId == contest_id
        ]
    assert found[ROUND_DIV2] == ROUND_DIV2_RATED
    assert len(saves) == sum(len(rows) for rows in found.values())


async def test_fetching_the_div_2_of_12_october_again_rates_it_by_name(
    october: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    codeforces.listed = OCTOBER_RATED

    count = await october.update_for_contest(ROUND_DIV2)

    assert codeforces.requests == [
        f'contest.standings {ROUND_DIV2}',
        'problemset.problems',
    ]
    assert count == 6
    assert await stored(cache_db) == {ROUND_DIV2: ROUND_DIV2_RATED}


# At boot


async def test_boot_maps_the_stored_problems_before_the_first_update(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await cache_db.cache_problemset(EXPECTED[DIV2])
    codeforces.list_fails = True

    await cache.run()
    try:
        # At once, from disk.
        assert cache.problem_to_contests['Grid Walk', PAIR_START] == [DIV2]
        # The first update runs at boot, fails, and leaves the map.
        for _ in range(100):
            if codeforces.requests:
                break
            await asyncio.sleep(0)
        assert codeforces.requests == ['problemset.problems']
        assert cache.problem_to_contests['Grid Walk', PAIR_START] == [DIV2]
    finally:
        task = cache._update_task
        assert isinstance(task, tasks.Task)
        await task.stop()
    assert not [record for record in caplog.records if record.exc_info]


async def test_boot_with_nothing_stored_says_how_the_problems_fill_in(
    cache: ProblemsetCache,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codeforces.list_fails = True

    await cache.run()
    task = cache._update_task
    assert isinstance(task, tasks.Task)
    await task.stop()

    assert [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ] == [
        'Problemset cache on disk is empty. The hourly update fills in the'
        " contests that Codeforces' problem list has; run ;cache problemsets all"
        ' for the others.'
    ]


async def test_stopping_the_hourly_update_stops_it_where_it_is(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    # As at shutdown, while the Div. 1's standings are on their way; the
    # Div. 2's came first.
    codeforces.hanging = {DIV1}
    await cache.run()
    await codeforces.hung.wait()
    task = cache._update_task
    assert isinstance(task, tasks.Task)
    running = task.asyncio_task
    assert running is not None

    try:
        await task.stop()
        for _ in range(100):
            if running.done():
                break
            await asyncio.sleep(0)
        assert running.cancelled()
    finally:
        running.cancel()
    assert await stored(cache_db) == expected(DIV2)


# ;cache problemsets <contest ID>


async def test_fetching_a_parallel_contest_again_replaces_its_problems(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    # Stored, but with a problem the contest no longer has.
    await cache_db.cache_problemset(
        [*STANDINGS[DIV2], problem(DIV2, 'E', 'Removed Problem')]
    )
    await cache_db.cache_problemset(EXPECTED[DIV1])

    count = await cache.update_for_contest(DIV2)

    # Its standings, though it had problems stored, then the problem list.
    assert codeforces.requests == [f'contest.standings {DIV2}', 'problemset.problems']
    assert count == 4
    assert await stored(cache_db) == expected(DIV1, DIV2)
    assert sorted(cache.problem_to_contests['Grid Walk', PAIR_START]) == [DIV1, DIV2]


@pytest.mark.parametrize('contest_id', [SOLO, OLD_SOLO])
async def test_fetching_a_contest_the_list_has_whole_again_reads_its_standings(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    contest_id: int,
) -> None:
    # The list may be wrong about it: the bot owner can always put it right.
    await cache_db.cache_problemset([problem(contest_id, 'C', 'Stale Problem')])

    count = await cache.update_for_contest(contest_id)

    assert codeforces.requests == [
        f'contest.standings {contest_id}',
        'problemset.problems',
    ]
    assert count == len(EXPECTED[contest_id])
    assert await stored(cache_db) == expected(contest_id)


@pytest.mark.parametrize('contest_id', [UNLISTED, OLD_UNLISTED, OLD_DIV2])
async def test_fetching_a_contest_the_list_leaves_incomplete_reads_its_standings(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    contest_id: int,
) -> None:
    # However old, and whether its standings failed lately or not.
    codeforces.failing = {contest_id}
    await hourly(cache)
    codeforces.failing = set()
    codeforces.requests.clear()

    count = await cache.update_for_contest(contest_id)

    assert codeforces.standings_requests() == [contest_id]
    assert count == len(EXPECTED[contest_id])
    assert (await stored(cache_db))[contest_id] == EXPECTED[contest_id]


@pytest.mark.parametrize(
    ('failure', 'error', 'requests'),
    [
        ('the standings', cf.TrueApiError, [f'contest.standings {DIV2}']),
        ('a bug', KeyError, [f'contest.standings {DIV2}']),
        (
            'the problem list',
            cf.ClientError,
            [f'contest.standings {DIV2}', 'problemset.problems'],
        ),
    ],
    ids=['the standings', 'a bug', 'the problem list'],
)
async def test_a_failed_fetch_of_one_contest_keeps_its_problems(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    failure: str,
    error: type[Exception],
    requests: list[str],
) -> None:
    await cache_db.cache_problemset(EXPECTED[DIV2])
    if failure == 'the problem list':
        codeforces.list_fails = True
    elif failure == 'a bug':
        codeforces.broken = {DIV2: KeyError('problems')}
    else:
        codeforces.failing = {DIV2}

    # The error handler answers the bot owner with it.
    with pytest.raises(error):
        await cache.update_for_contest(DIV2)

    assert codeforces.requests == requests
    assert await stored(cache_db) == expected(DIV2)


async def test_standings_without_problems_keep_the_stored_ones(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    await cache_db.cache_problemset(EXPECTED[DIV2])
    codeforces.standings[DIV2] = []

    assert await cache.update_for_contest(DIV2) == 0

    assert codeforces.requests == [f'contest.standings {DIV2}']
    assert await stored(cache_db) == expected(DIV2)


async def test_a_contest_missing_from_the_list_is_refused_before_any_request(
    cache: ProblemsetCache, codeforces: FakeCodeforces
) -> None:
    with pytest.raises(ContestNotFound):
        await cache.update_for_contest(GONE)

    assert codeforces.requests == []


# ;cache problemsets all


async def test_updating_every_contest_fetches_what_the_bot_lacks_however_old(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    update = await cache.update_for_all()

    # Newest first, each contest's standings once.
    assert codeforces.standings_requests() == [
        DIV2,
        DIV1,
        UNLISTED,
        OLD_DIV2,
        OLD_DIV1,
        OLD_UNLISTED,
    ]
    assert await stored(cache_db) == EXPECTED
    assert update == ProblemsetUpdate(
        problems=sum(len(rows) for rows in EXPECTED.values()), failed=[]
    )
    assert sorted(cache.problem_to_contests['Old Shared', OLD_PAIR_START]) == [
        OLD_DIV1,
        OLD_DIV2,
    ]

    # A second time, there is nothing more to fetch or save.
    codeforces.requests.clear()
    assert await cache.update_for_all() == ProblemsetUpdate(problems=0, failed=[])
    assert codeforces.requests == ['problemset.problems']


async def test_updating_every_contest_reports_failures_and_deletes_nothing(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    gone = problem(GONE, 'A', 'From A Contest Long Gone', rating=1200)
    await cache_db.cache_problemset([gone, *STANDINGS[OLD_DIV1]])
    codeforces.failing = {OLD_UNLISTED, DIV2}

    update = await cache.update_for_all()

    # By contest ID.
    assert update.failed == [OLD_UNLISTED, DIV2]
    found = await stored(cache_db)
    assert DIV2 not in found and OLD_UNLISTED not in found
    # What the bot had stays, and is updated, not fetched again.
    assert found[GONE] == [gone]
    assert found[OLD_DIV1] == EXPECTED[OLD_DIV1]
    assert OLD_DIV1 not in codeforces.standings_requests()
    assert update.problems == sum(len(rows) for rows in found.values()) - 1


async def test_updating_every_contest_goes_on_past_any_failure_of_a_contest(
    cache: ProblemsetCache,
    cache_db: CacheDbConn,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A Codeforces error, standings without problems, and a bug.
    codeforces.failing = {OLD_UNLISTED}
    codeforces.standings[UNLISTED] = []
    codeforces.broken = {DIV2: KeyError('problems')}

    with caplog.at_level(logging.INFO):
        update = await cache.update_for_all()

    # By contest ID.
    assert update.failed == [OLD_UNLISTED, DIV2, UNLISTED]
    others = {OLD_SOLO, OLD_DIV1, OLD_DIV2, SOLO, DIV1}
    assert await stored(cache_db) == expected(*others)
    assert update.problems == sum(len(EXPECTED[contest_id]) for contest_id in others)
    # Only the bug reaches the log channel.
    assert [record.getMessage() for record in warnings_and_errors(caplog)] == [
        'Problems of contest 2102 not saved.'
    ]


async def test_updating_every_contest_ignores_the_hourly_backoff(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    codeforces.failing = {UNLISTED}
    await hourly(cache)
    codeforces.failing = set()

    await cache.update_for_all()

    assert codeforces.standings_requests().count(UNLISTED) == 2
    assert (await stored(cache_db))[UNLISTED] == EXPECTED[UNLISTED]


async def test_updating_every_contest_keeps_what_it_fetched_if_cancelled(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    # The Div. 2 comes first, the Div. 1 next, whose standings never come.
    codeforces.hanging = {DIV1}
    update = asyncio.create_task(cache.update_for_all())
    await codeforces.hung.wait()

    update.cancel()

    # Cancelling stops the update, which keeps what it has saved.
    with pytest.raises(asyncio.CancelledError):
        await update
    assert await stored(cache_db) == expected(DIV2)
    assert not cache.update_lock.locked()


async def test_updating_every_contest_fails_whole_when_the_problem_list_fails(
    cache: ProblemsetCache, cache_db: CacheDbConn, codeforces: FakeCodeforces
) -> None:
    await cache_db.cache_problemset(EXPECTED[DIV2])
    codeforces.list_fails = True

    with pytest.raises(cf.ClientError):
        await cache.update_for_all()

    assert codeforces.requests == ['problemset.problems']
    assert await stored(cache_db) == expected(DIV2)
