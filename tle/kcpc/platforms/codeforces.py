"""Upcoming Codeforces rounds from TLE's contest cache, Codeforces users, and
Codeforces' problems.

TLE already polls Codeforces' contest list and caches it, so for contests this
module asks Codeforces nothing itself: the caller passes the cached ``Contest``
objects in. Codeforces gives a contest's start and duration in seconds, and its
phase: BEFORE until it starts, CODING while it runs, then on to FINISHED.

Users are fetched with TLE's ``user.info`` call, which keeps to the limit on
requests to the Codeforces API that TLE's own commands share. Problems, and
the problems a user has solved, are fetched through TLE's client too, with
``problemset.problems``, ``contest.list`` and ``user.status``. No page of
codeforces.com is ever fetched: its robots.txt rules out the contest pages,
and a bot check guards the problem pages.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.timeutil import ensure_utc, from_epoch
from tle.util import codeforces_api as cf

logger = logging.getLogger(__name__)

PLATFORM = 'codeforces'
PROBLEM_URL = 'https://codeforces.com/contest/{contest_id}/problem/{index}'
CONTEST_URL = 'https://codeforces.com/contest/{contest_id}'
# Copied from tle.util.codeforces_common, which KCPC may not import. A
# contest whose name, lowercased, holds one of these words isn't a standard
# round: an April Fools contest, an ICPC mirror, a Kotlin Heroes round...
NONSTANDARD_CONTEST_WORDS = (
    'wild',
    'fools',
    'unrated',
    'surprise',
    'unknown',
    'friday',
    'q#',
    'testing',
    'marathon',
    'kotlin',
    'onsite',
    'experimental',
    'abbyy',
    'icpc',
)
# The tag of problems that aren't standard, wherever they were set.
SPECIAL_TAG = '*special'

# The phases of a contest that is yet to start or still running.
_UPCOMING_PHASES = frozenset({'BEFORE', 'CODING'})

_SERVICE = 'Codeforces'
_NOT_RESPONDING = 'Codeforces is not responding right now. Please try again later.'
# The most handles that one user.info request asks about.
_BATCH_SIZE = 300
# The characters of Codeforces handles, dots included. Handles with any other
# character are never sent: user.info quietly drops such characters, so that
# 'tou!rist' would find 'tourist', and splits its list of handles at ';'.
# Lengths aren't checked, as Codeforces just finds no one with a handle that is
# too short or too long.
_HANDLE = re.compile(r'[A-Za-z0-9_.-]+')
_ACCEPTED = 'OK'  # the verdict of a submission that passed every test


@dataclass(frozen=True)
class CodeforcesContest:
    """A Codeforces contest that is yet to start or still running.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted), and ``end`` is after ``start``.
    """

    contest_id: int
    name: str
    start: datetime
    end: datetime
    url: str  # https://codeforces.com/contests/<id>, which also lets you register

    def __post_init__(self) -> None:
        # Converted here too, so that contests built by hand (in tests, say)
        # compare exactly like those read from the cache.
        start = ensure_utc(self.start).replace(microsecond=0)
        end = ensure_utc(self.end).replace(microsecond=0)
        if end <= start:
            raise ValueError(
                f'Codeforces contest {self.contest_id} must end after it starts'
            )
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)


def upcoming_contests(
    contests: Iterable[cf.Contest], *, now: datetime
) -> list[CodeforcesContest]:
    """The contests that are yet to start or still running, by start then ID.

    That is the contests in phase BEFORE or CODING that have a start time and
    a positive duration, and end after ``now``: a contest the cache still has
    as CODING may have ended since it was cached.
    """
    now = ensure_utc(now)
    upcoming: list[CodeforcesContest] = []
    for contest in contests:
        start_seconds = contest.startTimeSeconds
        duration_seconds = contest.durationSeconds
        if contest.phase not in _UPCOMING_PHASES:
            continue
        if start_seconds is None or duration_seconds is None or duration_seconds <= 0:
            continue
        end = from_epoch(start_seconds + duration_seconds)
        if end <= now:
            continue
        upcoming.append(
            CodeforcesContest(
                contest_id=contest.id,
                name=contest.name,
                start=from_epoch(start_seconds),
                end=end,
                url=contest.register_url,
            )
        )
    upcoming.sort(key=lambda contest: (contest.start, contest.contest_id))
    return upcoming


@dataclass(frozen=True)
class CodeforcesUser:
    """A Codeforces user's public profile, as user.info gives it."""

    handle: str  # as Codeforces has it, in its canonical case
    rating: int | None  # None if never rated
    max_rating: int | None  # None if never rated
    rank: str | None  # the rating's rank as Codeforces names it ('expert'), if rated
    organization: str | None  # whitespace runs as one space; None if empty
    url: str  # https://codeforces.com/profile/<handle>


async def fetch_users(handles: Sequence[str]) -> list[CodeforcesUser]:
    """The Codeforces users called ``handles``, in order, leaving out unknown ones.

    A handle finds its user whatever its case, and each user is asked about
    once. Handles go in batches of at most 300 to TLE's ``user.info``, which
    fails a whole batch if one of its handles is unknown; such a batch is asked
    about again a handle at a time. Handles with characters that no handle has
    are left out without asking. Raises ``ExternalServiceError`` if Codeforces
    fails in any other way, after TLE's own retries.
    """
    wanted = _distinct_handles(handles)
    users: list[CodeforcesUser] = []
    for start in range(0, len(wanted), _BATCH_SIZE):
        users += await _fetch_batch(wanted[start : start + _BATCH_SIZE])
    return users


async def fetch_user(handle: str) -> CodeforcesUser | None:
    """The Codeforces user called ``handle``, whatever its case; None if none is.

    Raises ``ExternalServiceError`` as ``fetch_users`` does.
    """
    users = await fetch_users([handle])
    return users[0] if users else None


def _distinct_handles(handles: Iterable[str]) -> list[str]:
    """``handles`` that can be Codeforces handles, without repeats in any case."""
    distinct: dict[str, str] = {}
    for handle in handles:
        if _HANDLE.fullmatch(handle) is None:
            logger.debug('%r cannot be a Codeforces handle', handle)
            continue
        distinct.setdefault(handle.lower(), handle)
    return list(distinct.values())


async def _fetch_batch(handles: list[str]) -> list[CodeforcesUser]:
    """The users called ``handles``, asking again one by one if one is unknown."""
    try:
        return await _user_info(handles)
    except cf.HandleNotFoundError:
        if len(handles) == 1:
            logger.debug('Codeforces has no user %s', handles[0])
            return []
    # Codeforces names only the first unknown handle, in words TLE has to pick
    # apart, so each handle is asked about on its own. That happens outside the
    # except block, so that a failure here isn't chained to the first one.
    users: list[CodeforcesUser] = []
    for handle in handles:
        try:
            users += await _user_info([handle])
        except cf.HandleNotFoundError:
            logger.debug('Codeforces has no user %s', handle)
    return users


async def _user_info(handles: list[str]) -> list[CodeforcesUser]:
    """TLE's ``user.info`` for ``handles``.

    Its ``HandleNotFoundError`` is let through, for the caller to handle. Its
    other errors, and timeouts, which TLE doesn't catch, become
    ``ExternalServiceError``.
    """
    try:
        users = await cf.user.info(handles=handles)
    except cf.HandleNotFoundError:
        raise
    except (cf.CodeforcesApiError, asyncio.TimeoutError) as exc:
        raise ExternalServiceError(_SERVICE, _NOT_RESPONDING) from exc
    return [_codeforces_user(user) for user in users]


def _codeforces_user(user: cf.User) -> CodeforcesUser:
    # TLE keeps no rank from user.info, but works it out from the rating with
    # Codeforces' own table: its titles are Codeforces' ranks, capitalised.
    rank = None if user.rating is None else user.rank.title.lower()
    return CodeforcesUser(
        handle=user.handle,
        rating=user.rating,
        max_rating=user.maxRating,
        rank=rank,
        organization=' '.join((user.organization or '').split()) or None,
        url=user.url,
    )


@dataclass(frozen=True)
class CodeforcesProblem:
    """A problem in Codeforces' problemset.

    Unlike TLE's ``cf.Problem``, it can go in sets and be a key: its tags are
    a tuple.
    """

    contest_id: int
    index: str  # as Codeforces gives it: 'A', 'D1', '01'
    name: str
    rating: int | None  # 800 to 3500, in steps of 100; None if unrated
    tags: tuple[str, ...]  # e.g. ('dp', 'greedy'); '*special' included
    solved_count: int | None  # how many have solved it; None if not listed
    # Whether it is a problem of a standard round: its contest is known, and
    # its name has none of NONSTANDARD_CONTEST_WORDS, and the problem hasn't
    # the SPECIAL_TAG.
    standard: bool

    @property
    def problem_id(self) -> str:
        """The problem's ID on Codeforces: '1520D'."""
        return f'{self.contest_id}{self.index}'

    @property
    def url(self) -> str:
        """The problem's page."""
        return PROBLEM_URL.format(contest_id=self.contest_id, index=self.index)

    @property
    def contest_url(self) -> str:
        """The contest's page, which lists its editorial under Contest materials."""
        return CONTEST_URL.format(contest_id=self.contest_id)


@dataclass(frozen=True)
class SolvedProblems:
    """The problems a Codeforces user has solved."""

    ids: frozenset[str]  # the problem ID of each accepted submission: '1520D'
    # Their names as well, each with its problem's ID ('' for a problem
    # without a contest). A problem that a Div. 1 and a Div. 2 round shared is
    # in the problemset once, under one of the two, so a member who solved it
    # in the other round is found only by its name.
    named: frozenset[tuple[str, str]]


def problem_catalog(
    problems: Iterable[cf.Problem],
    statistics: Iterable[cf.ProblemStatistics],
    contests: Iterable[cf.Contest],
) -> list[CodeforcesProblem]:
    """Codeforces' problems as ``CodeforcesProblem``s, in the order given.

    ``statistics`` and ``contests`` are what Codeforces lists along with them.
    A problem's solved count is the statistics' for its contest and index,
    and it is standard if ``contests`` has its contest under a standard name
    and it hasn't the ``SPECIAL_TAG``: problems of unknown contests are not.
    Problems without a contest ID (acmsguru's) are left out.
    """
    contest_names = {contest.id: contest.name for contest in contests}
    solved_counts = {
        (stat.contestId, stat.index): stat.solvedCount
        for stat in statistics
        if stat.contestId is not None
    }
    catalog: list[CodeforcesProblem] = []
    for problem in problems:
        contest_id = problem.contestId
        if contest_id is None:
            continue
        contest_name = contest_names.get(contest_id)
        tags = tuple(problem.tags or ())  # None if Codeforces left them out
        catalog.append(
            CodeforcesProblem(
                contest_id=contest_id,
                index=problem.index,
                name=problem.name,
                rating=problem.rating,
                tags=tags,
                solved_count=solved_counts.get((contest_id, problem.index)),
                standard=(
                    contest_name is not None
                    and not _is_nonstandard_contest(contest_name)
                    and SPECIAL_TAG not in tags
                ),
            )
        )
    return catalog


async def fetch_problems() -> list[CodeforcesProblem]:
    """Every problem in Codeforces' problemset, as ``problem_catalog`` makes them.

    Asks TLE's client for the problemset and for the contests, without gyms.
    Raises ``ExternalServiceError`` if Codeforces fails, after TLE's own
    retries.
    """
    try:
        problems, statistics = await cf.problemset.problems()
        contests = await cf.contest.to_list(gym=False)
    except (cf.CodeforcesApiError, asyncio.TimeoutError) as exc:
        raise ExternalServiceError(_SERVICE, _NOT_RESPONDING) from exc
    return problem_catalog(problems, statistics, contests)


async def fetch_solved(handle: str) -> SolvedProblems | None:
    """The problems that the Codeforces user ``handle`` has solved: those of
    their submissions with the verdict OK. None if Codeforces has no such user.

    A handle with characters that no handle has is not asked about. Raises
    ``ExternalServiceError`` if Codeforces fails in any other way, after TLE's
    own retries.
    """
    if _HANDLE.fullmatch(handle) is None:
        logger.debug('%r cannot be a Codeforces handle', handle)
        return None
    try:
        submissions = await cf.user.status(handle=handle)
    # Before CodeforcesApiError, of which both are kinds.
    except (cf.HandleNotFoundError, cf.HandleInvalidError):
        logger.debug('Codeforces has no user %s', handle)
        return None
    except (cf.CodeforcesApiError, asyncio.TimeoutError) as exc:
        raise ExternalServiceError(_SERVICE, _NOT_RESPONDING) from exc
    solved = [sub.problem for sub in submissions if sub.verdict == _ACCEPTED]
    named = frozenset(
        (
            '' if problem.contestId is None else f'{problem.contestId}{problem.index}',
            problem.name,
        )
        for problem in solved
    )
    return SolvedProblems(
        ids=frozenset(problem_id for problem_id, _ in named if problem_id),
        named=named,
    )


def _is_nonstandard_contest(name: str) -> bool:
    """Whether a contest called ``name`` isn't a standard round, as TLE judges."""
    lowered = name.lower()
    return any(word in lowered for word in NONSTANDARD_CONTEST_WORDS)
