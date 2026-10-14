"""How Codeforces scores and rates a contest, to rebuild the rows of a rated
virtual contest from the members' own submissions.

Codeforces' public standings have official contestants alone, so a virtual
participation is scored here, by rules checked against official results:

- Codeforces format: a problem worth x points, solved t seconds after the
  start after w penalised attempts, scores
  max(3x/10, x - floor(x * floor(t / 60) / k) - 50w), for the contest's decay
  constant k (``decay_constant``). The rows have no penalty.
- ICPC format: a solved problem scores 1, and adds floor(t / 60) + 10w minutes
  of penalty.
- An attempt before the first accepted submission is penalised unless it is
  still being judged, didn't compile or failed on test 1. Whatever comes after
  the first accepted submission doesn't count.

A row's rank is 1 + the number of rows strictly better, so that equal scores
share a rank, as on Codeforces.
"""

import bisect
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from fractions import Fraction
from typing import NamedTuple

from tle.util import codeforces_api as cf
from tle.util.ranklist.rating_calculator import CodeforcesRatingCalculator

# The formats the bot can score: Codeforces' own and ICPC's. IOI's and the
# others aren't scored.
SCORABLE_TYPES = frozenset({'CF', 'ICPC'})
# The verdicts of a submission still being judged.
PENDING: frozenset[str | None] = frozenset({None, 'TESTING', 'SUBMITTED'})
# Failures that cost no penalty: compilation errors, and Codeforces' own
# failures to judge.
NOT_PENALISED = frozenset(
    {'COMPILATION_ERROR', 'FAILED', 'CRASHED', 'INPUT_PREPARATION_CRASHED'}
)
# The rating that Codeforces' calculation counts a new account at, whose
# rating changes show an old rating of 0.
NEW_ACCOUNT_RATING = 1400

_ACCEPTED = 'OK'
_VIRTUAL = 'VIRTUAL'
_ICPC = 'ICPC'
# A solved problem keeps 3/10 of its points, however late it came and however
# many attempts it took.
_FLOOR = Fraction(3, 10)
_POINTS_PER_ATTEMPT = 50  # what a penalised attempt costs, Codeforces format
_MINUTES_PER_ATTEMPT = 10  # what a penalised attempt costs, ICPC format
# The decay constants that rounds use, besides 25D/720: 62.5n, for n from 2
# to 8.
_DECAY_CONSTANTS = tuple(Fraction(125 * n, 2) for n in range(2, 9))
_DEFAULT_DURATION = 2 * 60 * 60  # seconds, for a contest without a length


class VirtualParticipation(NamedTuple):
    """A member's virtual participation in a contest: the party they took part
    as, and its submissions, oldest first.
    """

    party: cf.Party
    submissions: tuple[cf.Submission, ...]

    @property
    def pending(self) -> bool:
        """Whether one of its submissions is still being judged."""
        return any(submission.verdict in PENDING for submission in self.submissions)


def find_participation(
    submissions: Iterable[cf.Submission],
    handle: str,
    *,
    earliest: float,
    latest: float,
) -> VirtualParticipation | None:
    """The virtual participation of ``handle`` on their own that started
    between ``earliest`` and ``latest``, from its submissions among
    ``submissions``; None if there is none.

    Only virtual submissions count, of a party whose one member is ``handle``,
    in any case. Of several such participations, as when a member takes the
    contest virtually again, the one that started first counts.
    """
    by_start: dict[int, list[cf.Submission]] = {}
    for submission in submissions:
        party = submission.author
        start = party.startTimeSeconds
        if (
            party.participantType == _VIRTUAL
            and party.teamId is None
            and len(party.members) == 1
            and party.members[0].handle.lower() == handle.lower()
            and start is not None
            and earliest <= start <= latest
        ):
            by_start.setdefault(start, []).append(submission)
    if not by_start:
        return None
    first = sorted(
        by_start[min(by_start)],
        key=lambda submission: (submission.creationTimeSeconds, submission.id),
    )
    return VirtualParticipation(first[0].author, tuple(first))


def decay_constant(
    contest: cf.Contest,
    problems: Sequence[cf.Problem],
    official: Sequence[cf.RanklistRow],
) -> Fraction:
    """The decay constant k of ``contest``, in Codeforces format.

    Codeforces publishes 25D/720 for a contest of D seconds, but rounds of 2
    hours 15 minutes, for one, use 250. So k is the one of 62.5n, for n from 2
    to 8, and 25D/720 that reproduces the most results of the ``official``
    rows that tell them apart: problems solved a minute or more after the
    start, for more than the 3/10 that any solve keeps. A tie goes to the one
    nearest 25D/720, and without such results k is 25D/720.
    """
    published = _published_decay(contest)
    # Each result that tells the constants apart, as (maximum points, minutes,
    # penalised attempts, points), with how many rows have it.
    results: Counter[tuple[float, int, int, float]] = Counter()
    for row in official:
        for problem, result in zip(problems, row.problemResults, strict=False):
            seconds = result.bestSubmissionTimeSeconds
            if (
                problem.points
                and seconds is not None
                and seconds >= 60
                and _FLOOR * Fraction(problem.points) < result.points
            ):
                minutes, attempts = seconds // 60, result.rejectedAttemptCount
                results[problem.points, minutes, attempts, result.points] += 1
    if not results:
        return published

    def matches(decay: Fraction) -> int:
        return sum(
            count
            for (max_points, minutes, attempts, points), count in results.items()
            if _cf_points(Fraction(max_points), minutes, attempts, decay) == points
        )

    # The nearest to 25D/720 first, as max keeps the first of equals.
    candidates = sorted(
        {*_DECAY_CONSTANTS, published},
        key=lambda decay: (abs(decay - published), decay),
    )
    return max(candidates, key=matches)


def cf_problem_points(
    max_points: float, seconds: int, rejected: int, decay: Fraction
) -> float:
    """The points of a problem worth ``max_points`` in Codeforces format,
    solved ``seconds`` after the start after ``rejected`` penalised attempts,
    for the decay constant ``decay``, computed exactly.
    """
    return float(_cf_points(Fraction(max_points), seconds // 60, rejected, decay))


def _cf_points(
    max_points: Fraction, minutes: int, rejected: int, decay: Fraction
) -> Fraction:
    lost = math.floor(max_points * minutes / decay)
    # The floor comes last: it holds however many attempts the solve took.
    return max(_FLOOR * max_points, max_points - lost - _POINTS_PER_ATTEMPT * rejected)


def icpc_penalty(seconds: int, rejected: int) -> int:
    """The penalty minutes of a problem solved ``seconds`` after the start
    after ``rejected`` penalised attempts, in ICPC format.
    """
    return seconds // 60 + _MINUTES_PER_ATTEMPT * rejected


def score(
    contest: cf.Contest,
    problems: Sequence[cf.Problem],
    participation: VirtualParticipation,
    *,
    decay: Fraction | None,
) -> cf.RanklistRow:
    """The standings row that ``participation`` earns in ``contest``, with rank
    0 until ``rank_among`` ranks it.

    ``decay`` is the decay constant of a contest in Codeforces format, or None
    for 25D/720. Raises ValueError for a format that the bot can't score.
    """
    if contest.type not in SCORABLE_TYPES:
        raise ValueError(
            f"Contest {contest.id} is in {contest.type} format, which the bot can't "
            'score'
        )
    if decay is None:
        decay = _published_decay(contest)
    by_problem: dict[str, list[cf.Submission]] = {}
    for submission in participation.submissions:
        by_problem.setdefault(submission.problem.index, []).append(submission)

    total, penalty = Fraction(0), 0
    results = []
    for problem in problems:
        solved_at, rejected = _first_accepted(by_problem.get(problem.index, []))
        points = Fraction(0)
        if solved_at is not None and contest.type == _ICPC:
            points = Fraction(1)
            penalty += icpc_penalty(solved_at, rejected)
        elif solved_at is not None and problem.points:
            points = _cf_points(
                Fraction(problem.points), solved_at // 60, rejected, decay
            )
        total += points
        results.append(
            cf.ProblemResult(
                points=float(points),
                penalty=None,
                rejectedAttemptCount=rejected,
                type='FINAL',
                bestSubmissionTimeSeconds=solved_at,
            )
        )
    return cf.RanklistRow(
        party=participation.party,
        rank=0,
        points=float(total),
        penalty=penalty,
        problemResults=results,
    )


def _first_accepted(submissions: Iterable[cf.Submission]) -> tuple[int | None, int]:
    """When the first accepted one of ``submissions`` came, in seconds after
    the start (None if none was accepted), and how many penalised attempts
    came before it (all of them, if none was accepted).
    """
    rejected = 0
    for submission in submissions:
        if submission.verdict == _ACCEPTED:
            return submission.relativeTimeSeconds, rejected
        if not (
            submission.verdict in PENDING
            or submission.verdict in NOT_PENALISED
            or submission.passedTestCount == 0
        ):
            rejected += 1
    return None, rejected


def rank_among(
    official: Sequence[cf.RanklistRow], rows: Sequence[cf.RanklistRow]
) -> list[cf.RanklistRow]:
    """``rows``, ranked among the ``official`` rows and each other.

    A row's rank is 1 + the number of rows strictly better than it, with more
    points, or as many and less penalty. Equal scores share a rank.
    """
    official_keys = sorted(_score_key(row) for row in official)
    row_keys = sorted(_score_key(row) for row in rows)
    return [
        row._replace(
            rank=1
            + bisect.bisect_left(official_keys, _score_key(row))
            + bisect.bisect_left(row_keys, _score_key(row))
        )
        for row in rows
    ]


def official_rank(
    row: cf.RanklistRow,
    official: Sequence[cf.RanklistRow],
    changes: Sequence[cf.RatingChange],
) -> tuple[int, bool]:
    """The official rank that ``row``'s score earns among everyone Codeforces
    rated, and whether it ties with a listed row.

    The public standings can leave rated contestants out, so their ranks count
    only those listed. But each of their rated rows has an official rank in
    ``changes``: ``row`` takes that of the first listed rated row that isn't
    better than it. Worse than every listed rated row, it comes last, after
    everyone rated.
    """
    rank_by_handle = {change.handle.lower(): change.rank for change in changes}
    rated = []
    for listed in official:
        if len(listed.party.members) != 1:
            continue
        rank = rank_by_handle.get(listed.party.members[0].handle.lower())
        if rank is not None:
            rated.append((_score_key(listed), rank))
    rated.sort()
    key = _score_key(row)
    i = bisect.bisect_left([listed_key for listed_key, _ in rated], key)
    if i == len(rated):
        return len(changes) + 1, False
    listed_key, rank = rated[i]
    return rank, listed_key == key


def predict_delta(
    handle: str,
    row: cf.RanklistRow,
    rating: int,
    official: Sequence[cf.RanklistRow],
    changes: Sequence[cf.RatingChange],
) -> int:
    """The rating change that ``row``'s score would have brought ``handle``,
    rated ``rating``, among everyone Codeforces rated in the contest.

    The field is Codeforces' own, from its rating changes ``changes``: each
    contestant at their official rank and old rating, a new account at 1400.
    ``handle`` joins it at the rank of ``official_rank``, and Codeforces'
    calculation gives the change.
    """
    rank, tied = official_rank(row, official, changes)
    # The calculator ranks by points: -rank keeps each official rank, and
    # half a place more puts a score that beats a row's just ahead of it.
    field: list[tuple[str, float, int, int]] = [
        (change.handle, -change.rank, 0, change.oldRating or NEW_ACCOUNT_RATING)
        for change in changes
        if change.handle.lower() != handle.lower()
    ]
    field.append((handle, -rank if tied else 0.5 - rank, 0, rating))
    return CodeforcesRatingCalculator(field).calculate_rating_changes()[handle]


def _published_decay(contest: cf.Contest) -> Fraction:
    """25D/720, for a contest of D seconds: the decay constant that Codeforces
    publishes.
    """
    return Fraction(25 * (contest.durationSeconds or _DEFAULT_DURATION), 720)


def _score_key(row: cf.RanklistRow) -> tuple[float, int]:
    """Sorts better rows first."""
    return -row.points, row.penalty
