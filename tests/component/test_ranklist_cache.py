"""Tests for the ranklist cache (tle.util.cache.ranklist) and the ranklists it
makes (tle.util.ranklist): one request for a contest's standings, Codeforces'
official ranks once the rating changes are out, the finished contests it
keeps, the monitor of running contests, and the standings of rated virtual
contests.

Codeforces' standings and rating changes are faked, and record each request.
Its public standings leave rated contestants out, as the live ones do: they
list only those it trusts, ranked among themselves, so a listed rank can be
better than the official one. The cache's clock is the test's.
"""

import asyncio
import gc
import logging
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from tle.util import codeforces_api as cf, codeforces_common as cf_common, tasks
from tle.util.cache.ranklist import RanklistCache, RanklistNotMonitored
from tle.util.codeforces_api import (
    Contest,
    Member,
    Party,
    Problem,
    RanklistRow,
    RatingChange,
    Submission,
)
from tle.util.ranklist import Ranklist, virtual
from tle.util.ranklist.ranklist import UNLISTED, HandleNotPresentError

MINUTE = 60
HOUR = 60 * MINUTE
# When the tests start, on the cache's clock.
NOW = 1_000_000.0
START = 1_791_743_700  # when the contests started

# Shaped like the live contests of October 2026.
DIV1 = 2273  # 321 rows for 325 rating changes
DIV2 = 2274
SOLO = 2271  # Div. 1 + 2, on its own
DIV3 = 2275  # ICPC format
EDUCATIONAL = 2260
TEAMS = 2250  # a contest for teams
RUNNING = 2280
IOI = 2290  # a format the bot can't score


def make_contest(
    contest_id: int,
    name: str,
    *,
    kind: str = 'CF',
    phase: str = 'FINISHED',
    length: int = 2 * HOUR,
) -> Contest:
    return Contest(
        id=contest_id,
        name=name,
        startTimeSeconds=START,
        durationSeconds=length,
        type=kind,
        phase=phase,
        preparedBy=None,
    )


CONTESTS = {
    contest.id: contest
    for contest in (
        make_contest(DIV1, 'Codeforces Round 1127 (Div. 1)'),
        make_contest(DIV2, 'Codeforces Round 1127 (Div. 2)'),
        make_contest(SOLO, 'DeepInfra Round 1 (Div. 1 + Div. 2)', length=3 * HOUR),
        make_contest(
            DIV3, 'Codeforces Round 1125 (Div. 3)', kind='ICPC', length=150 * MINUTE
        ),
        make_contest(
            EDUCATIONAL,
            'Educational Codeforces Round 183 (Rated for Div. 2)',
            kind='ICPC',
        ),
        make_contest(TEAMS, 'Team Olympiad Mirror', kind='ICPC'),
        make_contest(RUNNING, 'Codeforces Round 1130 (Div. 2)', phase='CODING'),
        make_contest(IOI, 'Olympiad Mirror (IOI format)', kind='IOI'),
    )
}


def make_problem(contest_id: int, index: str, points: float | None) -> Problem:
    """A problem without a rating: Codeforces rates them days later."""
    return Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=f'Problem {index}',
        type='PROGRAMMING',
        points=points,
        rating=None,
        tags=[],
    )


def make_party(
    handle: str,
    kind: str = 'CONTESTANT',
    *,
    team: Sequence[str] = (),
    start: int | None = None,
) -> Party:
    """``handle``'s party, or with ``team``, the team ``handle`` of those
    members.
    """
    return Party(
        contestId=None,
        members=[Member(handle=member) for member in team or [handle]],
        participantType=kind,
        teamId=1 if team else None,
        teamName=handle if team else None,
        ghost=False,
        room=None,
        startTimeSeconds=start,
    )


def make_row(
    handle: str,
    points: float,
    penalty: int = 0,
    *,
    rank: int,
    kind: str = 'CONTESTANT',
    team: Sequence[str] = (),
) -> RanklistRow:
    return RanklistRow(
        party=make_party(handle, kind, team=team),
        rank=rank,
        points=points,
        penalty=penalty,
        problemResults=[],
    )


def make_change(
    handle: str, rank: int, *, old: int = 1500, delta: int = 0
) -> RatingChange:
    return RatingChange(
        contestId=0,
        contestName='',
        handle=handle,
        rank=rank,
        ratingUpdateTimeSeconds=0,
        oldRating=old,
        newRating=old + delta,
    )


class FakeCodeforces:
    """Codeforces' standings and rating changes of each contest, recording
    each request.
    """

    def __init__(self) -> None:
        self.contests = dict(CONTESTS)
        self.problems: dict[int, list[Problem]] = {}
        self.rows: dict[int, list[RanklistRow]] = {}
        # The rating changes of each contest, or what asking for them raises;
        # none until they are out.
        self.changes: dict[int, list[RatingChange] | Exception] = {}
        # What the next requests for each contest's standings raise.
        self.failures: dict[int, list[Exception]] = {}
        self.requests: list[str] = []
        # While held, a request for standings waits for ``release``.
        self.release: asyncio.Event | None = None
        self.waiting: asyncio.Event | None = None
        # Awaited as each request for rating changes is answered: other
        # callers run meanwhile.
        self.meanwhile: Callable[[], Awaitable[None]] | None = None

    def add(
        self,
        contest_id: int,
        rows: Iterable[RanklistRow],
        changes: Iterable[RatingChange] = (),
        problems: Iterable[Problem] = (),
    ) -> None:
        self.rows[contest_id] = list(rows)
        self.changes[contest_id] = list(changes)
        self.problems[contest_id] = list(problems)

    def hold(self) -> None:
        """Hold the requests for standings until ``release`` is set."""
        self.release, self.waiting = asyncio.Event(), asyncio.Event()

    async def standings(
        self, *, contest_id: int
    ) -> tuple[Contest, list[Problem], list[RanklistRow]]:
        # Keyword-only, and nothing but the contest: Codeforces refuses any
        # other parameter.
        self.requests.append(f'contest.standings {contest_id}')
        if self.release is not None and self.waiting is not None:
            self.waiting.set()
            await self.release.wait()
        failures = self.failures.get(contest_id)
        if failures:
            raise failures.pop(0)
        return (
            self.contests[contest_id],
            list(self.problems[contest_id]),
            list(self.rows[contest_id]),
        )

    async def rating_changes(self, *, contest_id: int) -> list[RatingChange]:
        self.requests.append(f'contest.ratingChanges {contest_id}')
        if self.meanwhile is not None:
            await self.meanwhile()
        changes = self.changes[contest_id]
        if isinstance(changes, Exception):
            raise changes
        return list(changes)

    def standings_requests(self) -> list[int]:
        prefix = 'contest.standings '
        return [
            int(request.removeprefix(prefix))
            for request in self.requests
            if request.startswith(prefix)
        ]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.contest, 'standings', fake.standings)
    monkeypatch.setattr(cf.contest, 'ratingChanges', fake.rating_changes)
    return fake


@pytest.fixture(autouse=True)
def ratings(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Codeforces' current ratings by handle, which predictions start from;
    1500 for anyone left out.
    """
    found: dict[str, int] = {}

    async def effective_ratings(*, activeOnly: bool | None = None) -> dict[str, int]:
        return dict(found)

    monkeypatch.setattr(
        'tle.util.cache.ranklist.getUsersEffectiveRating', effective_ratings
    )
    return found


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rated() -> set[int]:
    """The contests whose rating changes the bot has saved: none at first."""
    return set()


@pytest.fixture
def contest_list() -> dict[str, list[Contest]]:
    """The running and the finished contests that the bot's last refresh of
    its contest list found: none at first.
    """
    return {'_RUNNING': [], 'FINISHED': []}


@pytest.fixture
def cache(
    clock: Clock, rated: set[int], contest_list: dict[str, list[Contest]]
) -> RanklistCache:
    """The ranklist cache of a bot that knows CONTESTS, and whose last refresh
    of its contest list found ``contest_list``. Of its finished contests, those
    in ``rated`` have their rating changes saved, and the others count as newly
    finished without them.
    """

    async def without_rating_changes(contest: Contest) -> bool:
        return contest.id not in rated

    master = SimpleNamespace(
        contest_cache=SimpleNamespace(
            get_contest=CONTESTS.__getitem__, contests_by_phase=contest_list
        ),
        rating_changes_cache=SimpleNamespace(
            is_newly_finished_without_rating_changes=without_rating_changes
        ),
    )
    return RanklistCache(master, clock=clock)  # type: ignore[arg-type]


def handles(ranklist: Ranklist) -> list[str]:
    return [Ranklist.get_ranklist_lookup_key(row) for row in ranklist.standings]


def ranks(ranklist: Ranklist) -> dict[str, int]:
    return {
        Ranklist.get_ranklist_lookup_key(row): row.rank for row in ranklist.standings
    }


# A ranklist takes one request for the standings


async def test_a_ranklist_with_rating_changes_takes_one_request_for_the_standings(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    codeforces.add(
        SOLO,
        [make_row('tourist', 3000, rank=1), make_row('jiangly', 2500, rank=2)],
        [make_change('tourist', 1, delta=20), make_change('jiangly', 2, delta=-5)],
    )

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert codeforces.requests == [
        f'contest.ratingChanges {SOLO}',
        f'contest.standings {SOLO}',
    ]
    assert ranklist.official
    assert ranklist.deltas_status == 'Final'
    assert ranklist.get_delta('tourist') == 20


async def test_a_running_contest_s_predicted_ranklist_takes_one_request(
    cache: RanklistCache, codeforces: FakeCodeforces, ratings: dict[str, int]
) -> None:
    codeforces.add(
        RUNNING, [make_row('tourist', 1000, rank=1), make_row('jiangly', 500, rank=2)]
    )
    ratings.update(tourist=3800, jiangly=3700)

    ranklist = await cache.generate_ranklist(RUNNING, predict_changes=True)

    assert codeforces.requests == [f'contest.standings {RUNNING}']
    assert ranklist.deltas_status == 'Predicted'
    assert not ranklist.official
    assert ranklist.delta_by_handle is not None
    assert set(ranklist.delta_by_handle) == {'tourist', 'jiangly'}


# Official ranks


LISTED = 321
RATED = 325


def div1_standings() -> tuple[list[RanklistRow], list[RatingChange]]:
    """Rows shaped like 2273's: 321 contestants listed, ranked 1 to 321, and
    325 rating changes. The 4 left out tied last, so the listed ranks are
    the official ones.
    """
    rows = [
        make_row(f'listed{rank:03}', 3000 - 5 * rank, rank=rank)
        for rank in range(1, LISTED + 1)
    ]
    changes = [
        make_change(f'listed{rank:03}', rank, old=2400, delta=60 - rank // 3)
        for rank in range(1, LISTED + 1)
    ]
    changes += [
        make_change(f'unlisted{number}', LISTED + 1, old=1950, delta=-50 - number)
        for number in range(1, RATED - LISTED + 1)
    ]
    return rows, changes


async def test_a_div_1_round_shows_the_rated_contestants_it_leaves_out(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    rows, changes = div1_standings()
    codeforces.add(DIV1, rows, changes, [make_problem(DIV1, 'A', 500)])

    ranklist = await cache.generate_ranklist(DIV1, fetch_changes=True)

    assert ranklist.official
    # The listed rows keep their ranks, which are the official ones here.
    assert [row.rank for row in ranklist.standings] == list(range(1, LISTED + 1))
    assert len(ranklist.standings) == LISTED
    for number in range(1, RATED - LISTED + 1):
        # In any case, as members' handles are matched.
        row = ranklist.get_standing_row(f'UNLISTED{number}')
        assert row.party.participantType == UNLISTED
        assert row.party.members == [Member(handle=f'unlisted{number}')]
        assert (row.rank, row.points, row.penalty, row.problemResults) == (
            LISTED + 1,
            0.0,
            0,
            [],
        )
        assert ranklist.get_delta(f'Unlisted{number}') == -50 - number
    assert ranklist.get_delta('listed001') == 60
    with pytest.raises(HandleNotPresentError):
        ranklist.get_standing_row('nobody')


async def test_every_listed_row_takes_its_official_rank(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    # A Div. 2 lists only the contestants it trusts, ranked among themselves.
    codeforces.add(
        DIV2,
        [
            make_row('alice', 2000, rank=1),
            make_row('bob', 1500, rank=2),
            make_row('carol', 1500, rank=2),
            make_row('dave', 900, rank=4),
        ],
        [
            make_change('newbie', 1),
            make_change('alice', 2),
            make_change('bob', 3),
            make_change('carol', 3),
            make_change('cheater', 5),
            make_change('dave', 6),
        ],
    )

    ranklist = await cache.generate_ranklist(DIV2, fetch_changes=True)

    assert ranks(ranklist) == {'alice': 2, 'bob': 3, 'carol': 3, 'dave': 6}
    assert ranklist.get_standing_row('newbie').rank == 1
    assert ranklist.get_standing_row('cheater').rank == 5


def educational_standings() -> tuple[list[RanklistRow], list[RatingChange]]:
    """An Educational round in ICPC format, which rates only those below 2100.
    Its listed rows include unrated ones, and leave rated ones out.
    """
    rows = [
        make_row('cherry', 6, 100, rank=1),
        make_row('div1star', 6, 100, rank=1),  # unrated, tied with cherry
        make_row('strong', 5, 50, rank=3),  # unrated, between two rated groups
        make_row('bob', 5, 80, rank=4),
        make_row('carol', 4, 30, rank=5),
        make_row('weak', 1, 10, rank=6),  # unrated, below everyone rated
    ]
    changes = [
        make_change('cherry', 1, delta=90),
        make_change('hidden', 2, delta=70),
        make_change('bob', 3, delta=10),
        make_change('carol', 4, delta=-20),
    ]
    return rows, changes


async def test_an_educational_round_places_its_unrated_rows_among_the_rated(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    rows, changes = educational_standings()
    codeforces.add(EDUCATIONAL, rows, changes)

    ranklist = await cache.generate_ranklist(EDUCATIONAL, fetch_changes=True)

    # An unrated row takes the rank of a rated row it ties with, or else that
    # of the nearest rated rows below it, or else the rank after everyone
    # rated.
    assert ranks(ranklist) == {
        'cherry': 1,
        'div1star': 1,
        'strong': 3,
        'bob': 3,
        'carol': 4,
        'weak': 5,
    }
    assert ranklist.get_standing_row('hidden').rank == 2
    assert ranklist.get_delta('hidden') == 70
    assert ranklist.get_delta('div1star') is None


async def test_plus_official_leaves_out_the_unrated_and_keeps_official_ranks(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    rows, changes = educational_standings()
    codeforces.add(EDUCATIONAL, rows, changes)

    ranklist = await cache.generate_ranklist(
        EDUCATIONAL, fetch_changes=True, rated_only=True
    )

    assert ranks(ranklist) == {'cherry': 1, 'bob': 3, 'carol': 4}
    # Those left out of the standings still show.
    assert ranklist.get_standing_row('hidden').rank == 2


async def test_plus_official_changes_only_educational_rounds(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    rows, changes = educational_standings()
    codeforces.add(SOLO, rows, changes)

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True, rated_only=True)

    assert len(ranklist.standings) == len(rows)


async def test_until_the_rating_changes_are_out_plus_official_ranks_the_rated_again(
    cache: RanklistCache, codeforces: FakeCodeforces, ratings: dict[str, int]
) -> None:
    rows, _ = educational_standings()
    codeforces.add(EDUCATIONAL, rows)
    ratings.update(div1star=2400, strong=2200, cherry=1900, bob=1800)

    ranklist = await cache.generate_ranklist(
        EDUCATIONAL, fetch_changes=True, rated_only=True
    )

    # Ranked among those left: the listed ranks count only those listed.
    assert ranks(ranklist) == {'cherry': 1, 'bob': 2, 'carol': 3, 'weak': 4}
    assert ranklist.deltas_status == 'Predicted'


async def test_a_group_of_equal_scores_takes_its_best_official_rank(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    # Tied rated rows share an official rank. Were one ever worse, an unrated
    # row tied with them would still take the best.
    codeforces.add(
        EDUCATIONAL,
        [
            make_row('first', 3, 20, rank=1),
            make_row('later', 2, 40, rank=2),
            make_row('sooner', 2, 40, rank=2),
            make_row('unrated', 2, 40, rank=2),
        ],
        [
            make_change('first', 1),
            make_change('later', 3),
            make_change('sooner', 2),
        ],
    )

    ranklist = await cache.generate_ranklist(EDUCATIONAL, fetch_changes=True)

    assert ranks(ranklist)['unrated'] == 2


async def test_a_team_contest_gets_its_rating_changes_and_keeps_its_listed_ranks(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    codeforces.add(
        TEAMS,
        [
            make_row('Red', 5, 300, rank=1, team=['alice', 'bob']),
            make_row('Blue', 4, 200, rank=2, team=['carol', 'dave']),
        ],
        [
            make_change('alice', 2, delta=30),
            make_change('bob', 2, delta=30),
            make_change('zed', 1, delta=50),
            make_change('carol', 3, delta=-10),
            make_change('dave', 3, delta=-10),
        ],
    )

    ranklist = await cache.generate_ranklist(TEAMS, fetch_changes=True)

    assert ranklist.deltas_status == 'Final'
    assert not ranklist.official
    assert ranks(ranklist) == {'Red': 1, 'Blue': 2}
    with pytest.raises(HandleNotPresentError):
        ranklist.get_standing_row('zed')


@pytest.mark.parametrize(
    'changes',
    [
        cf.RatingChangesUnavailableError(
            'contestId: Rating changes are unavailable for this contest', SOLO
        ),
        [],
    ],
    ids=['unavailable', 'not out yet'],
)
async def test_without_rating_changes_a_ranklist_has_predicted_ones(
    cache: RanklistCache,
    codeforces: FakeCodeforces,
    changes: Exception | list[RatingChange],
) -> None:
    codeforces.add(SOLO, [make_row('tourist', 3000, rank=1)])
    codeforces.changes[SOLO] = changes

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert ranklist.deltas_status == 'Predicted'
    assert not ranklist.official
    assert codeforces.standings_requests() == [SOLO]


async def test_a_contest_that_just_finished_has_a_ranklist_without_problem_ratings(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    # Codeforces rates a contest's problems days after it ends.
    problems = [
        make_problem(SOLO, index, 500.0 * (n + 1)) for n, index in enumerate('ABC')
    ]
    codeforces.add(
        SOLO,
        [make_row('tourist', 3000, rank=1), make_row('jiangly', 1500, rank=2)],
        problems=problems,
    )

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert ranklist.problems == problems
    assert [problem.rating for problem in ranklist.problems] == [None] * 3
    assert ranklist.deltas_status == 'Predicted'
    assert ranklist.delta_by_handle is not None
    assert set(ranklist.delta_by_handle) == {'tourist', 'jiangly'}


# The finished contests it keeps


def add_finished(codeforces: FakeCodeforces, *contest_ids: int) -> None:
    for contest_id in contest_ids:
        codeforces.add(contest_id, [make_row('tourist', 1000, rank=1)])


async def predicted(cache: RanklistCache, *contest_ids: int) -> None:
    """Ask for each contest's ranklist, as the monitor of running and newly
    finished contests does.
    """
    for contest_id in contest_ids:
        await cache.generate_ranklist(contest_id, predict_changes=True)


async def test_it_keeps_the_three_finished_contests_used_last(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    add_finished(codeforces, DIV1, DIV2, SOLO, DIV3)
    await predicted(cache, DIV1, DIV2, SOLO)
    await predicted(cache, DIV1)  # now the one used last

    await predicted(cache, DIV3)  # DIV2, used least recently, goes

    codeforces.requests.clear()
    await predicted(cache, DIV1, SOLO, DIV3)
    assert codeforces.requests == []
    await predicted(cache, DIV2)
    assert codeforces.requests == [f'contest.standings {DIV2}']


async def test_it_keeps_a_finished_contest_for_30_minutes(
    cache: RanklistCache, codeforces: FakeCodeforces, clock: Clock
) -> None:
    add_finished(codeforces, SOLO)
    await predicted(cache, SOLO)

    clock.now += 30 * MINUTE - 1
    await predicted(cache, SOLO)
    assert codeforces.standings_requests() == [SOLO]

    clock.now += 1
    await predicted(cache, SOLO)
    assert codeforces.standings_requests() == [SOLO, SOLO]


async def test_expired_contests_are_dropped_when_it_next_reads(
    cache: RanklistCache, codeforces: FakeCodeforces, clock: Clock
) -> None:
    add_finished(codeforces, SOLO, DIV1)
    await predicted(cache, SOLO)
    clock.now += 20 * MINUTE
    await predicted(cache, DIV1)

    clock.now += 15 * MINUTE
    await predicted(cache, DIV1)

    # Only what it holds tells: SOLO's megabytes are gone already.
    assert list(cache._finished) == [DIV1]


async def test_it_keeps_no_running_contest(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    add_finished(codeforces, RUNNING)

    await predicted(cache, RUNNING, RUNNING)

    assert codeforces.standings_requests() == [RUNNING, RUNNING]


async def test_what_it_keeps_is_safe_from_the_ranklists_it_makes(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    codeforces.add(
        SOLO,
        [make_row('tourist', 1000, rank=1), make_row('jiangly', 900, rank=2)],
        problems=[make_problem(SOLO, 'A', 500)],
    )
    first = await cache.generate_ranklist(SOLO, predict_changes=True)
    first.standings.clear()
    first.problems.clear()

    again = await cache.generate_ranklist(SOLO, predict_changes=True)

    assert handles(again) == ['tourist', 'jiangly']
    assert [problem.index for problem in again.problems] == ['A']
    assert codeforces.standings_requests() == [SOLO]


async def test_callers_that_come_together_share_one_fetch(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    codeforces.add(
        SOLO, [make_row('tourist', 1000, rank=1)], [make_change('tourist', 1)]
    )
    codeforces.hold()
    assert codeforces.release is not None and codeforces.waiting is not None
    first = asyncio.create_task(cache.generate_ranklist(SOLO, fetch_changes=True))
    await codeforces.waiting.wait()
    # A ;ranklist and a rated virtual contest's tick, say.
    second = asyncio.create_task(cache.generate_ranklist(SOLO, fetch_changes=True))
    for _ in range(5):
        await asyncio.sleep(0)

    codeforces.release.set()
    ranklists = await asyncio.gather(first, second)

    assert codeforces.requests == [
        f'contest.ratingChanges {SOLO}',
        f'contest.standings {SOLO}',
    ]
    assert all(ranklist.official for ranklist in ranklists)


async def test_a_caller_that_waited_for_a_failed_fetch_tries_itself(
    cache: RanklistCache, codeforces: FakeCodeforces, caplog: pytest.LogCaptureFixture
) -> None:
    add_finished(codeforces, SOLO)
    codeforces.failures[SOLO] = [cf.ClientError()]
    codeforces.hold()
    assert codeforces.release is not None and codeforces.waiting is not None
    first = asyncio.create_task(cache.generate_ranklist(SOLO, predict_changes=True))
    await codeforces.waiting.wait()
    second = asyncio.create_task(cache.generate_ranklist(SOLO, predict_changes=True))
    for _ in range(5):
        await asyncio.sleep(0)

    codeforces.release.set()
    failed, ranklist = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(failed, cf.ClientError)
    assert isinstance(ranklist, Ranklist)
    assert codeforces.standings_requests() == [SOLO, SOLO]
    # Nobody was left with an error nobody retrieved, which asyncio would log
    # at ERROR.
    gc.collect()
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == []


async def test_standings_kept_from_before_the_rating_changes_are_fetched_again(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    # The contest has just finished, and the monitor fetches its standings.
    codeforces.add(
        SOLO,
        [
            make_row('alice', 2000, rank=1),
            make_row('cheater', 1800, rank=2),
            make_row('bob', 1500, rank=3),
        ],
    )
    await cache.generate_ranklist(SOLO, predict_changes=True)
    # Then the rating changes come out, and Codeforces has removed the
    # cheater it skipped.
    codeforces.rows[SOLO] = [
        make_row('alice', 2000, rank=1),
        make_row('bob', 1500, rank=2),
    ]
    codeforces.changes[SOLO] = [
        make_change('alice', 1, delta=40),
        make_change('bob', 2, delta=-10),
    ]

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert handles(ranklist) == ['alice', 'bob']
    assert codeforces.standings_requests() == [SOLO, SOLO]
    # From then on it keeps both the standings and the rating changes.
    codeforces.requests.clear()
    again = await cache.generate_ranklist(SOLO, fetch_changes=True)
    assert codeforces.requests == []
    assert again.official
    assert again.get_delta('bob') == -10


@pytest.mark.parametrize(
    ('minutes', 'others'),
    [(2, [DIV1]), (2, [RUNNING]), (0, [DIV1, DIV2, DIV3])],
    ids=['expired', 'expired, as a running contest is read', 'least recently used'],
)
async def test_standings_dropped_while_rating_changes_are_requested_are_fetched_again(
    cache: RanklistCache,
    codeforces: FakeCodeforces,
    clock: Clock,
    minutes: int,
    others: list[int],
) -> None:
    # The monitor kept SOLO's standings, from before its rating changes came
    # out. Then they come out, and a ;ranklist asks for them.
    add_finished(codeforces, SOLO, DIV1, DIV2, DIV3, RUNNING)
    await predicted(cache, SOLO)
    clock.now += 29 * MINUTE
    codeforces.changes[SOLO] = [make_change('tourist', 1, delta=20)]

    async def other_callers() -> None:
        # Codeforces is slow to answer, and the reads of other contests drop
        # SOLO's standings meanwhile.
        clock.now += minutes * MINUTE
        await predicted(cache, *others)
        assert SOLO not in cache._finished

    codeforces.meanwhile = other_callers

    ranklist = await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert ranklist.official
    assert ranklist.get_delta('tourist') == 20
    assert codeforces.standings_requests() == [SOLO, *others, SOLO]


async def test_rating_changes_that_are_not_out_are_asked_for_each_time(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    add_finished(codeforces, SOLO)

    await cache.generate_ranklist(SOLO, fetch_changes=True)
    await cache.generate_ranklist(SOLO, fetch_changes=True)

    assert codeforces.requests == [
        f'contest.ratingChanges {SOLO}',
        f'contest.standings {SOLO}',
        f'contest.ratingChanges {SOLO}',
    ]


async def test_rating_changes_are_kept_as_long_as_their_standings(
    cache: RanklistCache, codeforces: FakeCodeforces, clock: Clock
) -> None:
    codeforces.add(
        SOLO, [make_row('tourist', 1000, rank=1)], [make_change('tourist', 1)]
    )
    await cache.generate_ranklist(SOLO, fetch_changes=True)
    clock.now += 30 * MINUTE

    await cache.generate_ranklist(SOLO, fetch_changes=True)

    both = [f'contest.ratingChanges {SOLO}', f'contest.standings {SOLO}']
    assert codeforces.requests == both + both


# The monitor of running contests


async def refresh(cache: RanklistCache) -> None:
    """Run the monitor of active contests once, as its task does."""
    task = cache._monitor_task
    assert isinstance(task, tasks.Task)
    await task.func(cache, None)


async def update(cache: RanklistCache) -> bool:
    """Choose the contests to monitor once, as each refresh of the contest
    list does, and tell whether that started the monitor. The monitor is
    stopped before it runs: the tests run it with ``refresh``.
    """
    task, monitor = cache._update_task, cache._monitor_task
    assert isinstance(task, tasks.Task) and isinstance(monitor, tasks.Task)
    await task.func(cache, None)
    started = monitor.running
    await monitor.stop()
    return started


async def test_the_monitor_drops_a_contest_whose_rating_changes_are_saved(
    cache: RanklistCache, codeforces: FakeCodeforces, rated: set[int]
) -> None:
    # Both finished, without rating changes yet, so both are monitored.
    add_finished(codeforces, DIV1, EDUCATIONAL)
    cache.monitored_contests = [CONTESTS[DIV1], CONTESTS[EDUCATIONAL]]
    await refresh(cache)
    # The Div. 1's rating changes come out first, and the bot saves them.
    rated.add(DIV1)

    await refresh(cache)

    assert cache.monitored_contests == [CONTESTS[EDUCATIONAL]]
    # So ;ranklist asks Codeforces, for the official ranks.
    with pytest.raises(RanklistNotMonitored):
        cache.get_ranklist(CONTESTS[DIV1], False)
    assert list(cache.ranklist_by_contest) == [EDUCATIONAL]
    rated.add(EDUCATIONAL)
    await refresh(cache)
    assert cache.ranklist_by_contest == {}


async def test_a_contest_list_refresh_drops_the_contests_it_stops_monitoring(
    cache: RanklistCache,
    codeforces: FakeCodeforces,
    contest_list: dict[str, list[Contest]],
    rated: set[int],
) -> None:
    # A Div. 1 and its parallel Div. 2, monitored from while they ran.
    add_finished(codeforces, DIV1, DIV2)
    contest_list['_RUNNING'] = [
        CONTESTS[contest_id]._replace(phase='CODING') for contest_id in (DIV1, DIV2)
    ]
    assert await update(cache)
    await refresh(cache)
    div2 = cache.get_ranklist(CONTESTS[DIV2], False)
    # Both have finished, and the Div. 1's rating changes are saved first.
    contest_list['_RUNNING'] = []
    contest_list['FINISHED'] = [CONTESTS[DIV1], CONTESTS[DIV2]]
    rated.add(DIV1)

    assert await update(cache)

    assert cache.monitored_contests == [CONTESTS[DIV2]]
    with pytest.raises(RanklistNotMonitored):
        cache.get_ranklist(CONTESTS[DIV1], False)
    assert cache.get_ranklist(CONTESTS[DIV2], False) is div2
    # Once none is left, the monitor isn't started again, and no ranklist is
    # kept.
    rated.add(DIV2)
    assert not await update(cache)
    assert cache.monitored_contests == []
    assert cache.ranklist_by_contest == {}


async def test_a_contest_that_fails_leaves_the_others_ranklists(
    cache: RanklistCache,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    down, broken, fine = (
        make_contest(contest_id, f'Codeforces Round {contest_id}', phase='CODING')
        for contest_id in (RUNNING, RUNNING + 1, RUNNING + 2)
    )
    for contest in (down, broken, fine):
        codeforces.contests[contest.id] = contest
        codeforces.add(contest.id, [make_row('tourist', 100, rank=1)])
    codeforces.failures[down.id] = [cf.ClientError()]
    codeforces.failures[broken.id] = [RuntimeError('broken row')]
    cache.monitored_contests = [down, broken, fine]
    caplog.set_level(logging.INFO, logger='RanklistCache')

    await refresh(cache)

    assert list(cache.ranklist_by_contest) == [fine.id]
    logged = [
        (record.levelno, record.getMessage(), record.exc_info is not None)
        for record in caplog.records
        if record.name == 'RanklistCache'
    ]
    assert logged == [
        (
            logging.INFO,
            f'Ranklist fetch failed for contest {down.id}. '
            "ClientError('Error connecting to Codeforces API')",
            False,
        ),
        (
            logging.WARNING,
            f'Ranklist fetch failed for contest {broken.id}. '
            "RuntimeError('broken row')",
            True,
        ),
        (logging.INFO, f'Ranklist fetched for contest {fine.id}', False),
    ]


# Rated virtual contests


class VcRatings:
    """The rated virtual contest rating of each member, by id; 1500 for those
    who have none.
    """

    def __init__(self, rating_by_member: dict[str, int]) -> None:
        self.rating_by_member = rating_by_member

    async def get_vc_rating(
        self, user_id: str, default_if_not_exist: bool = True
    ) -> int:
        return self.rating_by_member.get(user_id, 1500)


@pytest.fixture
def vc_ratings(monkeypatch: pytest.MonkeyPatch) -> VcRatings:
    ratings = VcRatings({'1': 1650})
    monkeypatch.setattr(cf_common, 'user_db', ratings)
    return ratings


def make_submission(
    handle: str,
    index: str,
    minutes: int,
    verdict: str = 'OK',
    *,
    start: int = START + 30 * 24 * HOUR,
) -> Submission:
    """``handle``'s virtual submission for ``index``, ``minutes`` after their
    virtual start; only its problem's index counts.
    """
    return Submission(
        id=minutes,
        contestId=None,
        problem=make_problem(0, index, None),
        author=make_party(handle, 'VIRTUAL', start=start),
        programmingLanguage='C++',
        verdict=verdict,
        creationTimeSeconds=start + 60 * minutes,
        relativeTimeSeconds=60 * minutes,
        passedTestCount=10,
    )


def participation(*submissions: Submission) -> virtual.VirtualParticipation:
    return virtual.VirtualParticipation(submissions[0].author, submissions)


# A Codeforces-format contest of 2 hours, whose decay constant is 250, as
# its official rows don't tell the constants apart.
VC_PROBLEMS = [make_problem(DIV2, 'A', 500.0), make_problem(DIV2, 'B', 1000.0)]


def vc_standings(codeforces: FakeCodeforces) -> None:
    codeforces.add(
        DIV2,
        [
            make_row('first', 1200, rank=1),
            make_row('second', 480, rank=2),
            make_row('third', 300, rank=3),
            # Not official: they don't count.
            make_row('virtual', 1500, rank=0, kind='VIRTUAL'),
            make_row('outside', 1400, rank=0, kind='OUT_OF_COMPETITION'),
        ],
        [make_change('first', 1), make_change('second', 2), make_change('third', 3)],
        VC_PROBLEMS,
    )


class Recorded:
    """Calls ``function`` and records each call's arguments and thread."""

    def __init__(self, function: Callable[..., Any]) -> None:
        self.function = function
        self.calls: list[tuple[Any, ...]] = []
        self.threads: list[int] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        self.threads.append(threading.get_ident())
        return self.function(*args, **kwargs)


@pytest.fixture
def predictions(monkeypatch: pytest.MonkeyPatch) -> Recorded:
    """``virtual.predict_delta``, recorded, which predicts 37 for everyone."""
    recorded = Recorded(lambda *args: 37)
    monkeypatch.setattr(virtual, 'predict_delta', recorded)
    return recorded


@pytest.mark.usefixtures('vc_ratings')
async def test_a_member_s_virtual_row_is_ranked_among_the_official_rows(
    cache: RanklistCache, codeforces: FakeCodeforces, predictions: Recorded
) -> None:
    vc_standings(codeforces)
    # A at 10 minutes: 500 - 500 * 10 / 250.
    alice = participation(make_submission('alice', 'A', 10))

    ranklist = await cache.generate_vc_ranklist(DIV2, {'alice': '1'}, {'alice': alice})

    row = ranklist.get_standing_row('ALICE')
    assert (row.party, row.points, row.rank) == (alice.party, 480.0, 2)
    assert ranklist.get_delta('alice') == 37
    # From the member's rated virtual contest rating, among the official
    # rows and everyone rated.
    [(handle, predicted_row, rating, official, changes)] = predictions.calls
    assert (handle, predicted_row, rating) == ('alice', row, 1650)
    assert [Ranklist.get_ranklist_lookup_key(row) for row in official] == [
        'first',
        'second',
        'third',
    ]
    assert [change.handle for change in changes] == ['first', 'second', 'third']
    assert ranklist.deltas_status == 'Predicted'
    assert codeforces.requests == [
        f'contest.ratingChanges {DIV2}',
        f'contest.standings {DIV2}',
    ]


@pytest.mark.usefixtures('vc_ratings', 'predictions')
async def test_until_a_member_takes_part_codeforces_is_not_asked(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    vc_standings(codeforces)

    ranklist = await cache.generate_vc_ranklist(DIV2, {'alice': '1'}, {})

    assert ranklist.standings == []
    assert ranklist.contest == CONTESTS[DIV2]
    assert codeforces.requests == []
    with pytest.raises(HandleNotPresentError):
        ranklist.get_delta('alice')


@pytest.mark.usefixtures('vc_ratings', 'predictions')
async def test_a_contest_the_bot_cannot_score_has_no_rows(
    cache: RanklistCache, codeforces: FakeCodeforces
) -> None:
    alice = participation(make_submission('alice', 'A', 10))

    ranklist = await cache.generate_vc_ranklist(IOI, {'alice': '1'}, {'alice': alice})

    assert ranklist.standings == []
    assert codeforces.requests == []


@pytest.mark.usefixtures('vc_ratings', 'predictions')
async def test_a_finished_contest_s_decay_constant_is_worked_out_once(
    cache: RanklistCache, codeforces: FakeCodeforces, monkeypatch: pytest.MonkeyPatch
) -> None:
    vc_standings(codeforces)
    decay = Recorded(virtual.decay_constant)
    monkeypatch.setattr(virtual, 'decay_constant', decay)
    alice = participation(make_submission('alice', 'A', 10))

    for _ in range(2):
        await cache.generate_vc_ranklist(DIV2, {'alice': '1'}, {'alice': alice})

    assert len(decay.calls) == 1
    # The standings and the rating changes were kept too.
    assert codeforces.requests == [
        f'contest.ratingChanges {DIV2}',
        f'contest.standings {DIV2}',
    ]


@pytest.mark.usefixtures('vc_ratings', 'predictions')
async def test_an_icpc_contest_needs_no_decay_constant(
    cache: RanklistCache, codeforces: FakeCodeforces, monkeypatch: pytest.MonkeyPatch
) -> None:
    codeforces.add(
        DIV3,
        [make_row('first', 2, 40, rank=1), make_row('second', 1, 30, rank=2)],
        [make_change('first', 1), make_change('second', 2)],
        [make_problem(DIV3, 'A', None), make_problem(DIV3, 'B', None)],
    )
    decay = Recorded(virtual.decay_constant)
    monkeypatch.setattr(virtual, 'decay_constant', decay)
    # A at 20 minutes after one penalised attempt: 20 + 10 minutes.
    alice = participation(
        make_submission('alice', 'A', 5, 'WRONG_ANSWER'),
        make_submission('alice', 'A', 20),
    )

    ranklist = await cache.generate_vc_ranklist(DIV3, {'alice': '1'}, {'alice': alice})

    row = ranklist.get_standing_row('alice')
    assert (row.points, row.penalty, row.rank) == (1.0, 30, 2)
    assert decay.calls == []


@pytest.mark.usefixtures('vc_ratings')
async def test_scoring_and_rating_predictions_run_off_the_event_loop(
    cache: RanklistCache,
    codeforces: FakeCodeforces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # They take seconds in a big contest, which would hold up the bot.
    vc_standings(codeforces)
    codeforces.add(RUNNING, [make_row('tourist', 100, rank=1)])
    recorded = {
        name: Recorded(getattr(virtual, name))
        for name in ('decay_constant', 'score', 'predict_delta')
    }
    for name, function in recorded.items():
        monkeypatch.setattr(virtual, name, function)
    predict = Recorded(Ranklist.predict)
    monkeypatch.setattr(Ranklist, 'predict', lambda *args: predict(*args))
    alice = participation(make_submission('alice', 'A', 10))

    await cache.generate_vc_ranklist(DIV2, {'alice': '1'}, {'alice': alice})
    await cache.generate_ranklist(RUNNING, predict_changes=True)

    loop_thread = threading.get_ident()
    for function in (*recorded.values(), predict):
        assert function.threads
        assert loop_thread not in function.threads
