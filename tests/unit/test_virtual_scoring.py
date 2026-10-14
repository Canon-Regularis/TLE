"""Tests for the scoring of rated virtual contests (tle.util.ranklist.virtual).

A member's virtual participation is rebuilt from their own submissions into a
standings row, by the rules that reproduce Codeforces' official results, and
ranked among the official rows. The data points are official results of
three rounds: 2269, Codeforces Round 1124 (Div. 2), 150 minutes long; 2267,
Round 1123 (Div. 2), 135 minutes long; and 2266, Round 1122 (Div. 3), in ICPC
format.

The fixture contest_status_virtual.json holds a member's submissions to 2267,
as contest.status gives them, newest first: a virtual participation on their
own, a team's virtual participation, a practice submission, and an older
virtual participation that only failed to compile.

The bot runs these functions in worker threads, so they must be pure.
"""

import copy
import itertools
import json
import math
import random
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest

from tle.util import codeforces_api as cf
from tle.util.ranklist import virtual
from tle.util.ranklist.rating_calculator import CodeforcesRatingCalculator

FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'fixtures' / 'cf_api_responses'

MINUTE = 60
HANDLE = 'Alice_VC'
# When the fixture's virtual participations started: the member's own, the
# team's (of Alice_VC and Bob_VC), and the old one that only failed to compile.
START = 1_760_000_000
TEAM_START = 1_759_999_800
OLD_START = 1_750_000_000
# 2267 is 135 minutes long, and its decay constant is 250, not
# 25D/720 = 281.25. 2269 is 150 minutes long, and its decay constant is
# 25D/720 = 312.5.
LENGTH_2267 = 135 * MINUTE
K_2267 = Fraction(250)
K_2269 = Fraction(625, 2)
# The window of a rated virtual contest on 2267: from 10 minutes before it
# starts until it finishes.
WINDOW = {'earliest': START - 10 * MINUTE, 'latest': START + LENGTH_2267}


def contest(
    type_: str = 'CF', *, length: int | None = LENGTH_2267, contest_id: int = 2267
) -> cf.Contest:
    return cf.Contest(
        id=contest_id,
        name=f'Contest {contest_id}',
        startTimeSeconds=START,
        durationSeconds=length,
        type=type_,
        phase='FINISHED',
        preparedBy=None,
    )


def problem(index: str, points: float | None = None) -> cf.Problem:
    return cf.Problem(
        contestId=2267,
        problemsetName=None,
        index=index,
        name=f'Problem {index}',
        type='PROGRAMMING',
        points=points,
        rating=None,
        tags=[],
    )


def party(
    *handles: str,
    kind: str = 'VIRTUAL',
    start: int | None = START,
    team_id: int | None = None,
) -> cf.Party:
    return cf.Party(
        contestId=2267,
        members=[cf.Member(handle) for handle in handles],
        participantType=kind,
        teamId=team_id,
        teamName=None if team_id is None else 'Team',
        ghost=False,
        room=None,
        startTimeSeconds=start,
    )


SOLO = party(HANDLE)


def submission(
    index: str,
    seconds: int,
    verdict: str | None = 'OK',
    *,
    passed: int | None = 1,
    author: cf.Party = SOLO,
    submission_id: int | None = None,
) -> cf.Submission:
    """A submission to problem ``index``, ``seconds`` after its author
    started, that passed ``passed`` tests.
    """
    created = (author.startTimeSeconds or START) + seconds
    return cf.Submission(
        id=created if submission_id is None else submission_id,
        contestId=2267,
        problem=problem(index),
        author=author,
        programmingLanguage='C++20 (GCC 13-64)',
        verdict=verdict,
        creationTimeSeconds=created,
        relativeTimeSeconds=seconds,
        passedTestCount=passed,
    )


def participation(*submissions: cf.Submission) -> virtual.VirtualParticipation:
    return virtual.VirtualParticipation(SOLO, submissions)


def result(
    points: float = 0.0, seconds: int | None = None, rejected: int = 0
) -> cf.ProblemResult:
    return cf.ProblemResult(
        points=points,
        penalty=None,
        rejectedAttemptCount=rejected,
        type='FINAL',
        bestSubmissionTimeSeconds=seconds,
    )


def row(
    points: float,
    penalty: int = 0,
    *,
    handle: str = 'someone',
    results: Sequence[cf.ProblemResult] = (),
) -> cf.RanklistRow:
    """An official contestant's row."""
    return cf.RanklistRow(
        party=party(handle, kind='CONTESTANT'),
        rank=0,
        points=points,
        penalty=penalty,
        problemResults=list(results),
    )


def change(handle: str, rank: int, old_rating: int) -> cf.RatingChange:
    return cf.RatingChange(
        contestId=2269,
        contestName='Codeforces Round 1124 (Div. 2)',
        handle=handle,
        rank=rank,
        ratingUpdateTimeSeconds=START,
        oldRating=old_rating,
        newRating=old_rating,
    )


def fixture_submissions() -> list[cf.Submission]:
    with open(FIXTURES_DIR / 'contest_status_virtual.json') as f:
        return cf._parse_submissions(json.load(f))


FIXTURE_PROBLEMS = [problem('A', 500.0), problem('B', 1000.0), problem('C', 1500.0)]


def fixture_participation() -> virtual.VirtualParticipation:
    found = virtual.find_participation(fixture_submissions(), HANDLE, **WINDOW)
    assert found is not None
    return found


def ids(found: virtual.VirtualParticipation | None) -> list[int]:
    assert found is not None
    return [submission.id for submission in found.submissions]


# Points in Codeforces format


@pytest.mark.parametrize(
    ('max_points', 'seconds', 'rejected', 'decay', 'points'),
    [
        (500, 242, 0, K_2269, 494),
        (2000, 1078, 0, K_2269, 1892),
        (2750, 135 * MINUTE, 26, K_2269, 825),
        (250, 151, 0, K_2267, 248),
        (1000, 720, 1, K_2267, 902),
    ],
    ids=['2269 A', '2269 D', '2269 E', '2267 A', '2267 C'],
)
def test_points_match_the_official_results(
    max_points: float, seconds: int, rejected: int, decay: Fraction, points: float
) -> None:
    assert virtual.cf_problem_points(max_points, seconds, rejected, decay) == points


def test_points_are_lost_for_whole_minutes_rounded_down() -> None:
    # 2269's D, at 17 minutes 58 seconds, loses 2000 * 17 / 312.5 = 108.8
    # points, rounded down: not 109, nor 2000 * (1078 / 60) / 312.5 = 114.98.
    assert virtual.cf_problem_points(2000, 1078, 0, K_2269) == 1892
    # Nothing is lost in the first minute.
    assert virtual.cf_problem_points(500, 59, 0, K_2267) == 500
    assert virtual.cf_problem_points(500, 60, 0, K_2267) == 498


def test_each_penalised_attempt_costs_50_points() -> None:
    assert [
        virtual.cf_problem_points(1000, 720, rejected, K_2267) for rejected in range(4)
    ] == [952, 902, 852, 802]


def test_three_tenths_of_the_points_are_kept_after_the_penalties() -> None:
    # 2269's E, solved at the end after 26 penalised attempts: 2750 - 1188 -
    # 26 * 50 = 262 is less than 3/10 of 2750. Keeping 3/10 before the
    # penalties would give 262 too.
    assert virtual.cf_problem_points(2750, 135 * MINUTE, 26, K_2269) == 825
    # However many attempts a solve took.
    assert virtual.cf_problem_points(500, 0, 6, K_2267) == 200
    assert virtual.cf_problem_points(500, 0, 10, K_2267) == 150


def test_points_are_exact_where_floating_point_would_be_one_off() -> None:
    # A round of 140 minutes has 25D/720 = 875/3. At 35 minutes, 500 points
    # lose exactly 500 * 35 * 3 / 875 = 60, which in floating point comes out
    # as 59.99999999999999.
    decay = Fraction(25 * 140 * MINUTE, 720)
    assert virtual.cf_problem_points(500, 35 * MINUTE, 0, decay) == 440


# Penalty in ICPC format


def test_icpc_penalty_matches_an_official_row() -> None:
    # A contestant of 2266 solved A at 20:47 after one penalised attempt, B at
    # 11:24, and C at 59:19 after one: penalty 30 + 11 + 69 = 110.
    assert virtual.icpc_penalty(1247, 1) == 30
    assert virtual.icpc_penalty(684, 0) == 11
    assert virtual.icpc_penalty(3559, 1) == 69


def test_an_icpc_row_has_a_point_per_solve_and_the_penalty_of_the_solves() -> None:
    # The same solves, and a failed attempt at D.
    icpc = contest('ICPC', length=150 * MINUTE, contest_id=2266)
    problems = [problem('A'), problem('B'), problem('C'), problem('D')]
    scored = virtual.score(
        icpc,
        problems,
        participation(
            submission('A', 600, 'WRONG_ANSWER', passed=3),
            submission('B', 684),
            submission('A', 1247),
            submission('C', 2000, 'TIME_LIMIT_EXCEEDED', passed=5),
            submission('C', 3559),
            submission('D', 5000, 'WRONG_ANSWER', passed=2),
        ),
        decay=None,
    )

    assert (scored.points, scored.penalty) == (3.0, 110)
    assert scored.problemResults == [
        cf.ProblemResult(1.0, None, 1, 'FINAL', 1247),
        cf.ProblemResult(1.0, None, 0, 'FINAL', 684),
        cf.ProblemResult(1.0, None, 1, 'FINAL', 3559),
        cf.ProblemResult(0.0, None, 1, 'FINAL', None),
    ]


def test_an_icpc_solve_scores_one_whatever_the_problem_s_points() -> None:
    scored = virtual.score(
        contest('ICPC'),
        [problem('A', 500.0), problem('B', 1000.0)],
        participation(submission('A', 100), submission('B', 200)),
        decay=None,
    )
    assert [result.points for result in scored.problemResults] == [1.0, 1.0]
    assert (scored.points, scored.penalty) == (2.0, 1 + 3)


# Which attempts are penalised


def score_a(*submissions: cf.Submission) -> tuple[cf.ProblemResult, int]:
    """Problem A's result in Codeforces format, where it is worth 1000 points
    with k = 250, and its penalty in ICPC format.
    """
    problems = [problem('A', 1000.0)]
    attempts = participation(*submissions)
    in_cf = virtual.score(contest('CF'), problems, attempts, decay=K_2267)
    in_icpc = virtual.score(contest('ICPC'), problems, attempts, decay=None)
    return in_cf.problemResults[0], in_icpc.penalty


# Solved at 12 minutes: 1000 - 48 points and 12 minutes, after a penalised
# attempt 50 points and 10 minutes more.
FREE = (cf.ProblemResult(952.0, None, 0, 'FINAL', 720), 12)
PENALISED = (cf.ProblemResult(902.0, None, 1, 'FINAL', 720), 22)


@pytest.mark.parametrize('passed', [None, 0, 3])
@pytest.mark.parametrize(
    'verdict',
    [
        None,
        'TESTING',
        'SUBMITTED',
        'COMPILATION_ERROR',
        'FAILED',
        'CRASHED',
        'INPUT_PREPARATION_CRASHED',
    ],
)
def test_an_attempt_still_judged_or_never_judged_costs_nothing(
    verdict: str | None, passed: int | None
) -> None:
    # Whatever tests it passed.
    attempts = submission('A', 100, verdict, passed=passed), submission('A', 720)
    assert score_a(*attempts) == FREE


@pytest.mark.parametrize(
    'verdict',
    ['WRONG_ANSWER', 'TIME_LIMIT_EXCEEDED', 'MEMORY_LIMIT_EXCEEDED', 'RUNTIME_ERROR'],
)
def test_a_failure_on_test_1_costs_nothing(verdict: str) -> None:
    attempts = submission('A', 100, verdict, passed=0), submission('A', 720)
    assert score_a(*attempts) == FREE


@pytest.mark.parametrize('passed', [None, 1, 12])
@pytest.mark.parametrize(
    'verdict',
    [
        'WRONG_ANSWER',
        'TIME_LIMIT_EXCEEDED',
        'MEMORY_LIMIT_EXCEEDED',
        'RUNTIME_ERROR',
        'IDLENESS_LIMIT_EXCEEDED',
        'PRESENTATION_ERROR',
        'CHALLENGED',
        'SKIPPED',
    ],
)
def test_any_other_failure_is_penalised(verdict: str, passed: int | None) -> None:
    # Even without a count of the tests it passed: only a failure known to be
    # on test 1 costs nothing.
    attempts = submission('A', 100, verdict, passed=passed), submission('A', 720)
    assert score_a(*attempts) == PENALISED


def test_only_the_first_accepted_submission_counts() -> None:
    # What comes after it, accepted or not, changes nothing.
    assert (
        score_a(
            submission('A', 100, 'WRONG_ANSWER', passed=4),
            submission('A', 720),
            submission('A', 900, 'WRONG_ANSWER', passed=4),
            submission('A', 1000),
        )
        == PENALISED
    )


def test_an_unsolved_problem_keeps_its_penalised_attempts() -> None:
    # With no points, and no penalty in ICPC format.
    assert score_a(
        submission('A', 100, 'WRONG_ANSWER', passed=4),
        submission('A', 200, 'COMPILATION_ERROR', passed=0),
        submission('A', 300, 'TIME_LIMIT_EXCEEDED', passed=9),
    ) == (cf.ProblemResult(0.0, None, 2, 'FINAL', None), 0)


# Rows in Codeforces format


def test_a_codeforces_format_row_sums_the_points_and_has_no_penalty() -> None:
    problems = [problem('A', 500.0), problem('B', 1000.0), problem('C', 1500.0)]
    attempts = participation(
        submission('B', 720, 'WRONG_ANSWER', passed=2),
        submission('A', 242),
        submission('B', 900),
    )

    scored = virtual.score(contest(), problems, attempts, decay=K_2267)

    # A at 4 minutes loses 8 points; B at 15 minutes 60, and 50 for the
    # wrong answer. Every problem has a result, in the contest's order.
    assert scored == cf.RanklistRow(
        party=SOLO,
        rank=0,
        points=492.0 + 890.0,
        penalty=0,
        problemResults=[
            cf.ProblemResult(492.0, None, 0, 'FINAL', 242),
            cf.ProblemResult(890.0, None, 1, 'FINAL', 900),
            cf.ProblemResult(0.0, None, 0, 'FINAL', None),
        ],
    )


def test_a_problem_without_points_adds_nothing() -> None:
    problems = [problem('A', 500.0), problem('B')]
    scored = virtual.score(
        contest(),
        problems,
        participation(submission('A', 242), submission('B', 300)),
        decay=K_2267,
    )
    assert scored.points == 492.0
    assert scored.problemResults[1].points == 0.0


def test_without_a_decay_constant_25d_over_720_is_used() -> None:
    # 140 minutes: 25D/720 = 875/3, rather than the 250 such rounds use.
    problems = [problem('A', 500.0)]
    attempts = participation(submission('A', 35 * MINUTE))
    long = contest(length=140 * MINUTE)
    assert virtual.score(long, problems, attempts, decay=None).points == 440.0
    assert virtual.score(long, problems, attempts, decay=K_2267).points == 430.0


def test_only_codeforces_and_icpc_formats_are_scored() -> None:
    assert virtual.SCORABLE_TYPES == {'CF', 'ICPC'}
    with pytest.raises(ValueError, match='IOI'):
        virtual.score(
            contest('IOI'),
            [problem('A', 100.0)],
            participation(submission('A', 60)),
            decay=None,
        )


# The fixture


def test_the_fixture_s_participation_is_the_member_s_own_virtual_one() -> None:
    found = fixture_participation()

    assert found.party.participantType == 'VIRTUAL'
    assert found.party.teamId is None
    assert found.party.startTimeSeconds == START
    assert [member.handle for member in found.party.members] == [HANDLE]
    # Oldest first, without the team's submission or the practice one.
    assert ids(found) == [
        340000001,
        340000002,
        340000003,
        340000004,
        340000005,
        340000006,
    ]
    assert not found.pending


def test_the_fixture_scores_1380_in_codeforces_format() -> None:
    scored = virtual.score(
        contest(), FIXTURE_PROBLEMS, fixture_participation(), decay=K_2267
    )

    # A at 5:30 after a wrong answer: 500 - 10 - 50. B at 15:30: 1000 - 60,
    # as its compilation error and its failure on test 1 cost nothing.
    results = scored.problemResults
    assert [result.points for result in results] == [440.0, 940.0, 0.0]
    assert [result.rejectedAttemptCount for result in results] == [1, 0, 0]
    assert (scored.points, scored.penalty) == (1380.0, 0)


def test_the_fixture_has_penalty_30_in_icpc_format() -> None:
    scored = virtual.score(
        contest('ICPC'), FIXTURE_PROBLEMS, fixture_participation(), decay=None
    )

    # A: 5 minutes, and 10 for the wrong answer. B: 15 minutes.
    assert (scored.points, scored.penalty) == (2.0, 30)


# Which participation counts


@pytest.mark.parametrize('handle', ['alice_vc', 'ALICE_VC'])
def test_handles_match_in_any_case(handle: str) -> None:
    found = virtual.find_participation(fixture_submissions(), handle, **WINDOW)
    assert ids(found) == ids(fixture_participation())


@pytest.mark.parametrize(
    ('earliest', 'latest', 'found'),
    [
        (START, START + LENGTH_2267, True),
        (START - 10 * MINUTE, START, True),
        (START + 1, START + LENGTH_2267, False),
        (START - 10 * MINUTE, START - 1, False),
    ],
    ids=['starts the window', 'ends the window', 'just before', 'just after'],
)
def test_the_window_holds_both_its_ends(
    earliest: int, latest: int, found: bool
) -> None:
    participation_ = virtual.find_participation(
        fixture_submissions(), HANDLE, earliest=earliest, latest=latest
    )
    assert (participation_ is not None) == found


def test_of_two_participations_in_the_window_the_earliest_counts() -> None:
    # The member took the contest virtually again, two hours later.
    again = party(HANDLE, start=START + 2 * 60 * MINUTE)
    submissions = fixture_submissions() + [
        submission('C', 100, author=again),
        submission('A', 50, author=again),
    ]
    found = virtual.find_participation(
        submissions, HANDLE, earliest=START, latest=START + 3 * 60 * MINUTE
    )
    assert ids(found) == ids(fixture_participation())

    # A window from long before: the old participation, which only failed to
    # compile, comes first.
    found = virtual.find_participation(
        fixture_submissions(), HANDLE, earliest=OLD_START, latest=START
    )
    assert found is not None
    assert found.party.startTimeSeconds == OLD_START
    assert ids(found) == [300000001]


def test_a_team_s_participation_never_counts() -> None:
    # Bob_VC took part only in the team.
    assert (
        virtual.find_participation(
            fixture_submissions(),
            'Bob_VC',
            earliest=TEAM_START,
            latest=START + LENGTH_2267,
        )
        is None
    )
    # Alice_VC's team started then, but she didn't on her own.
    assert (
        virtual.find_participation(
            fixture_submissions(), HANDLE, earliest=TEAM_START, latest=TEAM_START
        )
        is None
    )


@pytest.mark.parametrize(
    'author',
    [
        party(HANDLE, kind='PRACTICE'),
        party(HANDLE, kind='CONTESTANT'),
        party(HANDLE, kind='OUT_OF_COMPETITION'),
        party(HANDLE, team_id=7),
        party(HANDLE, 'Bob_VC'),
        party('Bob_VC'),
        party(HANDLE, start=None),
    ],
    ids=[
        'practice',
        'contestant',
        'out of competition',
        'a team of one',
        'two members without a team',
        'another member',
        'no start',
    ],
)
def test_only_the_member_s_own_virtual_submissions_count(author: cf.Party) -> None:
    own = [submission('A', 60)]
    other = [submission('A', 60, author=author)]
    assert virtual.find_participation(own, HANDLE, **WINDOW) is not None
    assert virtual.find_participation(other, HANDLE, **WINDOW) is None


def test_submissions_come_oldest_first_whatever_their_order() -> None:
    submissions = fixture_submissions()
    random.Random(2267).shuffle(submissions)
    found = virtual.find_participation(submissions, HANDLE, **WINDOW)
    assert ids(found) == sorted(ids(found))
    assert ids(found) == ids(fixture_participation())

    # Submissions in the same second come in the order of their ids.
    found = virtual.find_participation(
        [
            submission('B', 60, submission_id=12),
            submission('A', 60, submission_id=11),
        ],
        HANDLE,
        **WINDOW,
    )
    assert ids(found) == [11, 12]


def test_without_a_virtual_participation_there_is_none() -> None:
    submissions = fixture_submissions()
    assert virtual.find_participation([], HANDLE, **WINDOW) is None
    assert virtual.find_participation(submissions, 'Carol_VC', **WINDOW) is None


@pytest.mark.parametrize('verdict', [None, 'TESTING', 'SUBMITTED'])
def test_a_participation_is_pending_while_a_submission_is_judged(
    verdict: str | None,
) -> None:
    assert participation(
        submission('A', 60), submission('B', 120, verdict, passed=0)
    ).pending


@pytest.mark.parametrize(
    'verdict', ['OK', 'WRONG_ANSWER', 'COMPILATION_ERROR', 'FAILED', 'SKIPPED']
)
def test_a_judged_participation_is_not_pending(verdict: str) -> None:
    assert not participation(
        submission('A', 60), submission('B', 120, verdict, passed=0)
    ).pending


# The decay constant

ROUND_PROBLEMS = [
    problem('A', 500.0),
    problem('B', 1000.0),
    problem('C', 1500.0),
    problem('D', 2000.0),
    problem('E', 2500.0),
]


def official_points(
    max_points: float, seconds: int, rejected: int, decay: Fraction
) -> float:
    """Codeforces' points for a solve, worked out here on their own."""
    x = Fraction(max_points)
    lost = math.floor(x * (seconds // MINUTE) / decay)
    return float(max(x * 3 / 10, x - lost - 50 * rejected))


def solve(
    problems: Sequence[cf.Problem],
    index: str,
    seconds: int,
    rejected: int,
    points: float,
    *,
    handle: str = 'someone',
) -> cf.RanklistRow:
    """An official row that solved only problem ``index``."""
    return row(
        points,
        handle=handle,
        results=[
            result(points, seconds, rejected) if problem.index == index else result()
            for problem in problems
        ],
    )


def rows_scored_with(decay: Fraction) -> list[cf.RanklistRow]:
    """Official rows of a round whose problems lose points with ``decay``: two
    solves of each problem, at times within 2 hours, the second after a
    penalised attempt. Their handles are h0, h1 and so on.
    """
    rows = []
    for i, problem_ in enumerate(ROUND_PROBLEMS):
        assert problem_.points is not None
        for attempts in range(2):
            seconds = (3 + 19 * i + 29 * attempts) * MINUTE + 17
            points = official_points(problem_.points, seconds, attempts, decay)
            rows.append(
                solve(
                    ROUND_PROBLEMS,
                    problem_.index,
                    seconds,
                    attempts,
                    points,
                    handle=f'h{len(rows)}',
                )
            )
    return rows


@pytest.mark.parametrize(
    ('minutes', 'decay'),
    [
        (120, Fraction(250)),
        (125, Fraction(250)),
        (130, Fraction(250)),
        (135, Fraction(250)),
        (140, Fraction(250)),
        (145, Fraction(250)),
        (160, Fraction(250)),
        (150, Fraction(625, 2)),
        (165, Fraction(625, 2)),
        (180, Fraction(375)),
        (195, Fraction(375)),
    ],
)
def test_the_decay_constant_is_the_one_rounds_of_that_length_use(
    minutes: int, decay: Fraction
) -> None:
    # Even where 25D/720 or another constant is nearer, as for 140 or 160
    # minutes.
    long = contest(length=minutes * MINUTE)
    rows = rows_scored_with(decay)
    assert virtual.decay_constant(long, ROUND_PROBLEMS, rows) == decay


@pytest.mark.parametrize('n', range(2, 9))
def test_any_multiple_of_62_5_from_125_to_500_can_be_the_decay_constant(
    n: int,
) -> None:
    decay = Fraction(125 * n, 2)
    rows = rows_scored_with(decay)
    assert virtual.decay_constant(contest(), ROUND_PROBLEMS, rows) == decay


def test_25d_over_720_can_be_the_decay_constant_too() -> None:
    # For 140 minutes it is 875/3, which isn't a multiple of 62.5.
    decay = Fraction(25 * 140 * MINUTE, 720)
    rows = rows_scored_with(decay)
    long = contest(length=140 * MINUTE)
    assert virtual.decay_constant(long, ROUND_PROBLEMS, rows) == decay


def test_2267_s_results_give_250_rather_than_25d_over_720() -> None:
    # A 250 at 2:31 for 248, and C 1000 at 12:00 after a penalised attempt
    # for 902. 281.25 would give 249 and 908.
    problems = [problem('A', 250.0), problem('C', 1000.0)]
    rows = [solve(problems, 'A', 151, 0, 248.0), solve(problems, 'C', 720, 1, 902.0)]
    assert virtual.decay_constant(contest(), problems, rows) == 250


def test_2269_s_results_give_312_5() -> None:
    # 150 minutes. E, at the floor, tells the constants nothing.
    problems = [problem('A', 500.0), problem('D', 2000.0), problem('E', 2750.0)]
    rows = [
        solve(problems, 'A', 242, 0, 494.0),
        solve(problems, 'D', 1078, 0, 1892.0),
        solve(problems, 'E', 135 * MINUTE, 26, 825.0),
    ]
    long = contest(length=150 * MINUTE, contest_id=2269)
    assert virtual.decay_constant(long, problems, rows) == K_2269


def test_without_results_that_tell_them_apart_k_is_25d_over_720() -> None:
    published = Fraction(25 * LENGTH_2267, 720)
    problems = [*ROUND_PROBLEMS, problem('F')]
    assert virtual.decay_constant(contest(), problems, []) == published

    rows = [
        # Nothing solved.
        row(0.0, results=[result(rejected=2) for _ in problems]),
        # Solved in the first minute, after a penalised attempt.
        solve(problems, 'A', 45, 1, 450.0),
        # At the floor of 3/10, which a fast enough decay would reproduce.
        solve(problems, 'B', 130 * MINUTE, 3, 300.0),
        # A problem without points.
        solve(problems, 'F', 10 * MINUTE, 0, 1.0),
    ]
    assert virtual.decay_constant(contest(), problems, rows) == published


def test_a_contest_without_a_length_counts_as_2_hours_long() -> None:
    # 25D/720 is then 250: A, worth 500, solved at 35 minutes keeps 430.
    no_length = contest(length=None)
    assert virtual.decay_constant(no_length, ROUND_PROBLEMS, []) == 250
    attempts = participation(submission('A', 35 * MINUTE))
    scored = virtual.score(no_length, [problem('A', 500.0)], attempts, decay=None)
    assert scored.points == 430.0


def test_a_solve_in_the_second_minute_tells_the_constants_apart() -> None:
    # B, worth 1000, solved at 1:00 for 996, which only 250 gives, in a round
    # of 3 hours, whose 25D/720 is 375.
    problems = [problem('A', 500.0), problem('B', 1000.0)]
    rows = [solve(problems, 'B', MINUTE, 0, 996.0)]
    assert virtual.decay_constant(contest(length=180 * MINUTE), problems, rows) == 250


def test_results_at_the_floor_do_not_count() -> None:
    # A 2-hour round scored with 250, and three results at the floor of 3/10
    # that only 125 reproduces.
    problems = [problem('A', 500.0), problem('B', 1000.0)]
    rows = [
        solve(problems, 'A', 10 * MINUTE, 0, 480.0),
        solve(problems, 'B', 20 * MINUTE, 1, 870.0),
        solve(problems, 'A', 90 * MINUTE, 0, 150.0),
        solve(problems, 'A', 100 * MINUTE, 0, 150.0),
        solve(problems, 'B', 95 * MINUTE, 0, 300.0),
    ]
    assert virtual.decay_constant(contest(length=120 * MINUTE), problems, rows) == 250


def test_each_row_counts_even_with_the_same_result() -> None:
    # 160 minutes. Three contestants solved A at 2 minutes, for 496, which
    # only 250 gives; two others at 4 and 5 minutes, for 495 and 494, which
    # only 375 gives.
    problems = [problem('A', 500.0)]
    rows = 3 * [solve(problems, 'A', 2 * MINUTE, 0, 496.0)] + [
        solve(problems, 'A', 4 * MINUTE, 0, 495.0),
        solve(problems, 'A', 5 * MINUTE, 0, 494.0),
    ]
    assert virtual.decay_constant(contest(length=160 * MINUTE), problems, rows) == 250


def test_a_tie_goes_to_the_constant_nearest_25d_over_720() -> None:
    # 160 minutes: 25D/720 = 333.33. Each of these results only one constant
    # reproduces: 500 points at 2, 5 and 4 minutes.
    problems = [problem('A', 500.0)]
    only_250 = solve(problems, 'A', 2 * MINUTE, 0, 496.0)
    only_312_5 = solve(problems, 'A', 5 * MINUTE, 0, 492.0)
    only_375 = solve(problems, 'A', 4 * MINUTE, 0, 495.0)
    long = contest(length=160 * MINUTE)

    # 312.5 is nearer than 375, and 375 nearer than 250.
    assert virtual.decay_constant(long, problems, [only_375, only_312_5]) == K_2269
    assert virtual.decay_constant(long, problems, [only_250, only_375]) == 375


@pytest.mark.parametrize(
    ('minutes', 'decay', 'only_smaller', 'only_larger'),
    [
        (135, Fraction(250), (2, 496.0), (4, 494.0)),
        (165, Fraction(625, 2), (2, 497.0), (5, 494.0)),
        (195, Fraction(375), (4, 495.0), (9, 490.0)),
    ],
)
def test_a_tie_between_two_constants_as_near_goes_to_the_smaller(
    minutes: int,
    decay: Fraction,
    only_smaller: tuple[int, float],
    only_larger: tuple[int, float],
) -> None:
    # 25D/720 lies halfway between two constants: for 135 minutes, 281.25
    # lies between 250 and 312.5. Two contestants solved A, worth 500: one at
    # a time and for points that only the smaller reproduces, the other only
    # the larger. The smaller is the one that rounds of these lengths use.
    problems = [problem('A', 500.0)]
    rows = [
        solve(problems, 'A', minute * MINUTE, 0, points)
        for minute, points in (only_smaller, only_larger)
    ]
    long = contest(length=minutes * MINUTE)
    assert virtual.decay_constant(long, problems, rows) == decay


def test_the_decay_constant_comes_from_the_rows_each_time() -> None:
    # Nothing is kept from one contest to the next, even with the same id.
    for decay in (Fraction(375), K_2267, Fraction(500)):
        rows = rows_scored_with(decay)
        assert virtual.decay_constant(contest(), ROUND_PROBLEMS, rows) == decay


# Ranks


def test_a_row_ranks_after_the_rows_strictly_better() -> None:
    official = [row(3000.0), row(2500.0), row(2500.0), row(2000.0)]

    def rank(points: float) -> int:
        return virtual.rank_among(official, [row(points)])[0].rank

    # Equal scores share a rank.
    assert [rank(points) for points in (3500.0, 3000.0, 2600.0, 2500.0)] == [1, 1, 2, 2]
    assert [rank(points) for points in (2400.0, 2000.0, 0.0)] == [4, 4, 5]


def test_rebuilt_rows_rank_among_each_other_too() -> None:
    official = [row(3000.0), row(2500.0)]
    rows = [row(2400.0, handle='c'), row(2600.0, handle='a'), row(2600.0, handle='b')]

    ranked = virtual.rank_among(official, rows)

    # In the order given, and otherwise unchanged.
    assert [ranked_row.rank for ranked_row in ranked] == [5, 2, 2]
    assert [ranked_row._replace(rank=0) for ranked_row in ranked] == rows


def test_icpc_rows_with_as_many_solves_rank_by_penalty() -> None:
    official = [row(5.0, 300), row(5.0, 350), row(4.0, 100)]

    def rank(points: float, penalty: int) -> int:
        return virtual.rank_among(official, [row(points, penalty)])[0].rank

    five_solved = [rank(5.0, penalty) for penalty in (299, 300, 320, 350, 351)]
    assert five_solved == [1, 1, 2, 2, 3]
    assert [rank(4.0, penalty) for penalty in (0, 100, 101)] == [3, 3, 4]
    assert rank(6.0, 999) == 1


def test_a_rank_is_one_more_than_the_rows_strictly_better() -> None:
    # Every score of a small grid, against a plain count.
    scores = list(itertools.product((0.0, 1.0, 2.0), (0, 10, 20)))
    official = [row(points, penalty) for points, penalty in scores for _ in range(2)]
    rows = [row(points, penalty) for points, penalty in reversed(scores)]

    for ranked in virtual.rank_among(official, rows):
        better = [
            other
            for other in official + rows
            if (other.points, -other.penalty) > (ranked.points, -ranked.penalty)
        ]
        assert ranked.rank == 1 + len(better)


# Rating changes among everyone Codeforces rated

# Everyone Codeforces rated in a contest, at their official ranks. The public
# standings leave out 'top' and 'hidden', and 'c' is a new account.
CHANGES = [
    change('top', 1, 2100),
    change('a', 2, 1900),
    change('hidden', 3, 1700),
    change('b', 4, 1500),
    change('c', 4, 0),
    change('d', 6, 1200),
]
# The public standings, which also list 'x', whom the contest doesn't rate.
LISTED = [
    row(3000.0, handle='A'),
    row(2800.0, handle='x'),
    row(2500.0, handle='b'),
    row(2500.0, handle='c'),
    row(1000.0, handle='d'),
]


@pytest.mark.parametrize(
    ('points', 'expected'),
    [
        (3500.0, (2, False)),
        (3000.0, (2, True)),
        (2800.0, (4, False)),
        (2700.0, (4, False)),
        (2500.0, (4, True)),
        (1000.0, (6, True)),
        (500.0, (7, False)),
    ],
    ids=[
        'beats every listed row',
        'ties a row listed in another case',
        'ties an unrated row',
        'between two rows',
        'ties two rows',
        'ties the last row',
        'below every row',
    ],
)
def test_a_score_takes_the_official_rank_of_the_first_listed_row_not_better(
    points: float, expected: tuple[int, bool]
) -> None:
    # Below every listed row, it comes after everyone rated.
    assert virtual.official_rank(row(points), LISTED, CHANGES) == expected


def delta_in_field(place: float, rating: int = 1600) -> int:
    """The rating change of a member rated ``rating``, placed at ``place``
    among everyone rated in CHANGES, whose places are 60 down to 10.
    """
    field = [
        ('top', 60, 0, 2100),
        ('a', 50, 0, 1900),
        ('hidden', 40, 0, 1700),
        ('b', 30, 0, 1500),
        # A new account counts at 1400.
        ('c', 30, 0, 1400),
        ('d', 10, 0, 1200),
        ('alice', place, 0, rating),
    ]
    return CodeforcesRatingCalculator(field).calculate_rating_changes()['alice']


@pytest.mark.parametrize(
    ('points', 'place'),
    [
        (3500.0, 55),
        (3000.0, 50),
        (2700.0, 35),
        (2500.0, 30),
        (1000.0, 10),
        (500.0, 5),
    ],
    ids=[
        'just ahead of a',
        'tied with a',
        'just ahead of b and c',
        'tied with b and c',
        'tied with d',
        'last',
    ],
)
def test_the_rating_change_is_reckoned_among_everyone_rated(
    points: float, place: float
) -> None:
    delta = virtual.predict_delta('alice', row(points), 1600, LISTED, CHANGES)
    assert delta == delta_in_field(place)


def predict(points: float, changes: Sequence[cf.RatingChange]) -> int:
    return virtual.predict_delta('alice', row(points), 1600, LISTED, changes)


def test_a_new_account_counts_at_1400() -> None:
    at_1400 = [c._replace(oldRating=c.oldRating or 1400) for c in CHANGES]
    for points in (3500.0, 2700.0, 500.0):
        assert predict(points, CHANGES) == predict(points, at_1400)


def test_the_member_s_own_rating_change_is_left_out() -> None:
    # As if they had taken part in the contest itself, under another case.
    with_own = [*CHANGES, change('ALICE', 5, 1600)]
    assert predict(2700.0, with_own) == predict(2700.0, CHANGES)


def test_a_better_score_never_brings_a_smaller_change() -> None:
    scores = (500.0, 1000.0, 2500.0, 2700.0, 3000.0, 3500.0)
    deltas = [predict(points, CHANGES) for points in scores]
    assert deltas == sorted(deltas)
    assert deltas[0] < deltas[-1]


# Pure functions, for worker threads


def ranked_by_points(rows: Sequence[cf.RanklistRow]) -> list[cf.RatingChange]:
    """Rating changes for ``rows``, at their ranks by points, some of them for
    new accounts.
    """
    ordered = sorted(rows, key=lambda official_row: -official_row.points)
    return [
        change(
            official_row.party.members[0].handle,
            rank,
            0 if rank % 4 == 0 else 1100 + 50 * rank,
        )
        for rank, official_row in enumerate(ordered, start=1)
    ]


def rebuild(
    attempts: virtual.VirtualParticipation,
    problems: Sequence[cf.Problem],
    official: Sequence[cf.RanklistRow],
    changes: Sequence[cf.RatingChange],
) -> tuple[Fraction, cf.RanklistRow, tuple[int, bool], int]:
    """Everything the bot works out for a member's participation."""
    decay = virtual.decay_constant(contest(), problems, official)
    scored = virtual.score(contest(), problems, attempts, decay=decay)
    [ranked] = virtual.rank_among(official, [scored])
    return (
        decay,
        ranked,
        virtual.official_rank(ranked, official, changes),
        virtual.predict_delta(HANDLE, ranked, 1500, official, changes),
    )


def test_the_functions_leave_their_arguments_alone() -> None:
    submissions = fixture_submissions()
    problems = [*ROUND_PROBLEMS]
    # Worst first, as no function may sort them.
    official = sorted(
        rows_scored_with(K_2267), key=lambda official_row: official_row.points
    )
    changes = ranked_by_points(official)
    before = copy.deepcopy((submissions, problems, official, changes))

    found = virtual.find_participation(submissions, HANDLE, **WINDOW)
    assert found is not None
    rebuild(found, problems, official, changes)

    assert (submissions, problems, official, changes) == before


def test_the_functions_give_the_same_results_in_worker_threads() -> None:
    # As asyncio.to_thread runs them: several at once, on the same standings.
    official = rows_scored_with(K_2267)
    changes = ranked_by_points(official)
    members = [
        participation(submission('A', minutes * MINUTE), submission('C', 7200))
        for minutes in (5, 30, 60, 90, 120)
    ] * 4

    def rebuild_for(attempts: virtual.VirtualParticipation) -> tuple[object, ...]:
        return rebuild(attempts, ROUND_PROBLEMS, official, changes)

    alone = [rebuild_for(attempts) for attempts in members]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(rebuild_for, members)) == alone
