import asyncio
import functools
import itertools
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple, NoReturn

import aiohttp
from discord.ext import commands

# ruff: noqa: N815

API_BASE_URL = 'https://codeforces.com/api/'
CONTEST_BASE_URL = 'https://codeforces.com/contest/'
CONTESTS_BASE_URL = 'https://codeforces.com/contests/'
GYM_BASE_URL = 'https://codeforces.com/gym/'
PROFILE_BASE_URL = 'https://codeforces.com/profile/'
ACMSGURU_BASE_URL = 'https://codeforces.com/problemsets/acmsguru/'
GYM_ID_THRESHOLD = 100000
DEFAULT_RATING = 1500

logger = logging.getLogger(__name__)


class Rank(NamedTuple):
    """Codeforces rank."""

    low: int | None
    high: int | None
    title: str
    title_abbr: str | None
    color_graph: str | None
    color_embed: int | None


RATED_RANKS = (
    Rank(-(10**9), 1200, 'Newbie', 'N', '#CCCCCC', 0x808080),
    Rank(1200, 1400, 'Pupil', 'P', '#77FF77', 0x008000),
    Rank(1400, 1600, 'Specialist', 'S', '#77DDBB', 0x03A89E),
    Rank(1600, 1900, 'Expert', 'E', '#AAAAFF', 0x0000FF),
    Rank(1900, 2100, 'Candidate Master', 'CM', '#FF88FF', 0xAA00AA),
    Rank(2100, 2300, 'Master', 'M', '#FFCC88', 0xFF8C00),
    Rank(2300, 2400, 'International Master', 'IM', '#FFBB55', 0xF57500),
    Rank(2400, 2600, 'Grandmaster', 'GM', '#FF7777', 0xFF3030),
    Rank(2600, 3000, 'International Grandmaster', 'IGM', '#FF3333', 0xFF0000),
    Rank(3000, 10**9, 'Legendary Grandmaster', 'LGM', '#AA0000', 0xCC0000),
)
UNRATED_RANK = Rank(None, None, 'Unrated', None, None, None)


def rating2rank(rating: int | None) -> Rank:
    """Returns the rank corresponding to the given rating."""
    if rating is None:
        return UNRATED_RANK
    for rank in RATED_RANKS:
        assert rank.low is not None and rank.high is not None
        if rank.low <= rating < rank.high:
            return rank
    raise ValueError(f'Rating {rating} outside range of known ranks.')


# Data classes


class User(NamedTuple):
    """Codeforces user."""

    handle: str
    firstName: str | None
    lastName: str | None
    country: str | None
    city: str | None
    organization: str | None
    contribution: int
    rating: int | None
    maxRating: int | None
    lastOnlineTimeSeconds: int
    registrationTimeSeconds: int
    friendOfCount: int
    titlePhoto: str

    @property
    def effective_rating(self) -> int:
        """Returns the effective rating of the user."""
        return self.rating if self.rating is not None else DEFAULT_RATING

    @property
    def rank(self) -> Rank:
        """Returns the rank corresponding to the user's rating."""
        return rating2rank(self.rating)

    @property
    def url(self) -> str:
        """Returns the URL of the user's profile."""
        return f'{PROFILE_BASE_URL}{self.handle}'


def fix_urls(user: 'User') -> 'User':
    """Fix protocol-relative URLs in user's titlePhoto."""
    if user.titlePhoto.startswith('//'):
        user = user._replace(titlePhoto='https:' + user.titlePhoto)
    return user


class RatingChange(NamedTuple):
    """Codeforces rating change."""

    contestId: int
    contestName: str
    handle: str
    rank: int
    ratingUpdateTimeSeconds: int
    oldRating: int
    newRating: int


class Contest(NamedTuple):
    """Codeforces contest."""

    id: int
    name: str
    startTimeSeconds: int | None
    durationSeconds: int | None
    type: str
    phase: str
    preparedBy: str | None

    @property
    def end_time(self) -> int | None:
        """Returns the end time of the contest."""
        if self.startTimeSeconds is None or self.durationSeconds is None:
            return None
        return self.startTimeSeconds + self.durationSeconds

    @property
    def url(self) -> str:
        """Returns the URL of the contest."""
        if self.id < GYM_ID_THRESHOLD:
            return f'{CONTEST_BASE_URL}{self.id}'
        return f'{GYM_BASE_URL}{self.id}'

    @property
    def register_url(self) -> str:
        """Returns the URL to register for the contest."""
        return f'{CONTESTS_BASE_URL}{self.id}'

    def matches(self, markers: Iterable[str]) -> bool:
        """Returns whether the contest matches any of the given markers."""

        def filter_and_normalize(s: str) -> str:
            return ''.join(x for x in s.lower() if x.isalnum())

        return any(
            filter_and_normalize(marker) in filter_and_normalize(self.name)
            for marker in markers
        )


class Member(NamedTuple):
    """Codeforces party member."""

    handle: str


class Party(NamedTuple):
    """Codeforces party."""

    contestId: int | None
    members: list[Member]
    participantType: str
    teamId: int | None
    teamName: str | None
    ghost: bool
    room: int | None
    startTimeSeconds: int | None


CONTEST_PHASES = 'BEFORE CODING PENDING_SYSTEM_TEST SYSTEM_TEST FINISHED'.split()

PARTICIPANT_TYPES = (
    'CONTESTANT',
    'PRACTICE',
    'VIRTUAL',
    'MANAGER',
    'OUT_OF_COMPETITION',
)


class Problem(NamedTuple):
    """Codeforces problem."""

    contestId: int | None
    problemsetName: str | None
    index: str
    name: str
    type: str
    points: float | None
    rating: int | None
    tags: list[str]

    @property
    def contest_identifier(self) -> str:
        """Returns a string identifying the contest."""
        return f'{self.contestId}{self.index}'

    @property
    def url(self) -> str:
        """Returns the URL of the problem."""
        if self.contestId is None:
            assert self.problemsetName == 'acmsguru', (
                f'Unknown problemset {self.problemsetName}'
            )
            return f'{ACMSGURU_BASE_URL}problem/99999/{self.index}'
        base = CONTEST_BASE_URL if self.contestId < GYM_ID_THRESHOLD else GYM_BASE_URL
        return f'{base}{self.contestId}/problem/{self.index}'

    def has_metadata(self) -> bool:
        """Returns whether the problem has metadata."""
        return self.contestId is not None and self.rating is not None

    def _matching_tags_dict(self, match_tags: Iterable[str]) -> dict[str, list[str]]:
        """Returns a dict with matching tags."""
        tags = defaultdict(list)
        for match_tag in match_tags:
            for tag in self.tags:
                if match_tag in tag:
                    tags[match_tag].append(tag)
        return dict(tags)

    def matches_all_tags(self, match_tags: Iterable[str]) -> bool:
        """Returns whether the problem matches all of the given tags."""
        match_tags = set(match_tags)
        return len(self._matching_tags_dict(match_tags)) == len(match_tags)

    def matches_any_tag(self, match_tags: Iterable[str]) -> bool:
        """Returns whether the problem matches any of the given tags."""
        match_tags = set(match_tags)
        return len(self._matching_tags_dict(match_tags)) > 0

    def get_matched_tags(self, match_tags: Iterable[str]) -> list[str]:
        """Returns a list of tags that match any of the given tags."""
        return [
            tag
            for tags in self._matching_tags_dict(match_tags).values()
            for tag in tags
        ]


class ProblemStatistics(NamedTuple):
    """Codeforces problem statistics."""

    contestId: int | None
    index: str
    solvedCount: int


class Submission(NamedTuple):
    """Codeforces submission for a problem."""

    id: int
    contestId: int | None
    problem: Problem
    author: Party
    programmingLanguage: str
    verdict: str | None
    creationTimeSeconds: int
    relativeTimeSeconds: int
    # How many tests it passed: a failure on test 1 costs no penalty.
    passedTestCount: int | None = None


class RanklistRow(NamedTuple):
    """Codeforces ranklist row."""

    party: Party
    rank: int
    points: float
    penalty: int
    problemResults: list['ProblemResult']


class ProblemResult(NamedTuple):
    """Codeforces problem result."""

    points: float
    penalty: int | None
    rejectedAttemptCount: int
    type: str
    bestSubmissionTimeSeconds: int | None


def make_from_dict(namedtuple_cls: Any, dict_: dict[str, Any]) -> Any:
    """Creates a namedtuple from a subset of values in a dict."""
    field_vals = [dict_.get(field) for field in namedtuple_cls._fields]
    return namedtuple_cls._make(field_vals)


def _parse_party(party_dict: dict[str, Any]) -> Party:
    party_dict['members'] = [
        make_from_dict(Member, member) for member in party_dict['members']
    ]
    party: Party = make_from_dict(Party, party_dict)
    return party


def _parse_standings(
    resp: dict[str, Any],
) -> tuple[Contest, list[Problem], list[RanklistRow]]:
    """The contest, problems and rows of a contest.standings result."""
    contest_ = make_from_dict(Contest, resp['contest'])
    problems = [
        make_from_dict(Problem, problem_dict) for problem_dict in resp['problems']
    ]
    for row in resp['rows']:
        row['party'] = _parse_party(row['party'])
        row['problemResults'] = [
            make_from_dict(ProblemResult, problem_result)
            for problem_result in row['problemResults']
        ]
    ranklist = [make_from_dict(RanklistRow, row_dict) for row_dict in resp['rows']]
    return contest_, problems, ranklist


def _parse_submissions(resp: list[dict[str, Any]]) -> list[Submission]:
    """The submissions of a user.status or contest.status result."""
    for submission in resp:
        submission['problem'] = make_from_dict(Problem, submission['problem'])
        submission['author'] = _parse_party(submission['author'])
    return [make_from_dict(Submission, submission_dict) for submission_dict in resp]


# Error classes


class CodeforcesApiError(commands.CommandError):
    """Base class for all API related errors."""

    def __init__(self, message: str | None = None):
        super().__init__(message or 'Codeforces API error')


class TrueApiError(CodeforcesApiError):
    """An error originating from a valid response of the API."""

    def __init__(self, comment: str, message: str | None = None):
        super().__init__(message)
        self.comment = comment


class ClientError(CodeforcesApiError):
    """An error caused by a request to the API failing."""

    def __init__(self) -> None:
        super().__init__('Error connecting to Codeforces API')


class BadAnswer(Exception):
    """An answer that is retried, as the cause of a ``ClientError``: one that
    isn't a JSON object, a 200 without a result, or a server error. Its
    message names the HTTP status, as in 'HTTP 502 without JSON'.
    """


class HandleNotFoundError(TrueApiError):
    """An error caused by a handle not being found on Codeforces."""

    def __init__(self, comment: str, handle: str):
        super().__init__(comment, f'Handle `{handle}` not found on Codeforces')
        self.handle = handle


class HandleInvalidError(TrueApiError):
    """An error caused by a handle not being valid on Codeforces."""

    def __init__(self, comment: str, handle: str):
        super().__init__(comment, f'`{handle}` is not a valid Codeforces handle')
        self.handle = handle


class CallLimitExceededError(TrueApiError):
    """An error caused by the call limit being exceeded."""

    def __init__(self, comment: str):
        super().__init__(comment, 'Codeforces API call limit exceeded')


class ContestNotFoundError(TrueApiError):
    """An error caused by a contest not being found on Codeforces."""

    def __init__(self, comment: str, contest_id: Any):
        super().__init__(
            comment, f'Contest with ID `{contest_id}` not found on Codeforces'
        )


class RatingChangesUnavailableError(TrueApiError):
    """An error caused by rating changes being unavailable for a contest."""

    def __init__(self, comment: str, contest_id: Any):
        super().__init__(
            comment, f'Rating changes unavailable for contest with ID `{contest_id}`'
        )


# Codeforces API query methods

_session: aiohttp.ClientSession | None = None
_CALL_SPACING_SECONDS = 1  # read on each call by cf_ratelimit; tests set it to 0
# Standings and user.ratedList are megabytes: a whole request may take five
# minutes, but half a minute without a connection, or a minute without a byte,
# means Codeforces stopped answering.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5 * 60, sock_connect=30, sock_read=60)
# Refusals that callers turn into errors of their own, such as an unknown handle
# or contest, or rating changes not out yet, are logged at INFO. Any other is a
# WARNING, because it can mean that Codeforces changed its rules.
_EXPECTED_REFUSALS = ('not found', 'should contain', 'Rating changes are unavailable')


async def initialize() -> None:
    """Initialization for the Codeforces API module."""
    global _session
    _session = aiohttp.ClientSession()


def _bool_to_str(value: bool) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    raise TypeError(f'Expected bool, got {value} of type {type(value)}')


# How long every call must have failed, or every call of one method while other
# calls are answered, before one WARNING reaches the log channel. A shorter
# outage leaves only INFO lines.
OUTAGE_WARNING_AFTER = 15 * 60
# A streak of failures whose latest failure is this old is over: the next
# failure starts a new one, and an answer logs no end for it. Some methods are
# called days apart, only around contests or by commands. It is longer than the
# hourly problem-list run, so a method down alone and called that often is
# still reported.
_STREAK_GAP = 3 * 60 * 60


def _plural(count: int, noun: str) -> str:
    """'1 call' or '2 calls'."""
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _duration(seconds: float) -> str:
    """Reads like '2 hours 5 minutes', in whole minutes."""
    hours, minutes = divmod(int(max(seconds, 0) // 60), 60)
    parts = [_plural(hours, 'hour')] if hours else []
    if minutes or not hours:
        parts.append(_plural(minutes, 'minute'))
    return ' '.join(parts)


def _utc_time(epoch: float) -> str:
    """The time of day of ``epoch`` in UTC, as '23:00'."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%H:%M')


@dataclass
class _Streak:
    """Calls that failed in a row: when the first and the latest failed, how
    many, and whether a WARNING said so.
    """

    since: float
    last: float
    calls: int = 0
    warned: bool = False
    # For one method's calls: when another call was first answered meanwhile
    others_answered: float | None = None
    # For every call: the methods that failed
    paths: set[str] = field(default_factory=set)


def _count_failure(streak: _Streak | None, now: float) -> _Streak:
    """``streak`` with a failure at ``now`` counted, or a new streak in its
    place when there is none or its latest failure is ``_STREAK_GAP`` old.
    """
    if streak is None or now - streak.last >= _STREAK_GAP:
        streak = _Streak(since=now, last=now)
    streak.calls += 1
    streak.last = now
    return streak


class _Outage:
    """Warns once about a long Codeforces outage, and logs its end.

    A call fails when ``cf_ratelimit`` gives up on it, and is answered when
    Codeforces sends JSON: a result or a refusal. One WARNING says so once
    every call has failed for ``OUTAGE_WARNING_AFTER`` seconds, or once every
    call of one method has failed for as long since other calls were answered
    again. The next answer is logged at INFO. ``clock`` gives epoch seconds.

    A method's failures count from the other calls' answer, not from its own
    first failure: otherwise the first failure after an outage of every call,
    while Codeforces recovers, would read as that method alone being down.

    A method is said to fail while other calls work only if no other call
    has failed since the last answer: otherwise every call may be failing.

    Failures far apart are no outage. A streak, of every call or of one
    method, is over once its latest failure is ``_STREAK_GAP`` old: the next
    failure starts a new one, so that two short failures days apart don't
    read as one long outage, and an answer logs no end for it.

    It never raises, so that it can't fail the call it is told about: a fault
    of its own is logged instead.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._all: _Streak | None = None  # every call, since the last answer
        self._by_method: dict[str, _Streak] = {}  # each method's own calls

    def failed(self, path: str, error: BaseException) -> None:
        """Counts a call of ``path`` that was given up on ``error``."""
        try:
            now = self._clock()
            every = self._all = _count_failure(self._all, now)
            every.paths.add(path)
            mine = self._by_method[path] = _count_failure(
                self._by_method.get(path), now
            )
            # Only this method failed since the last answer.
            alone = every.paths == {path}
            # Other calls were answered since this method began to fail, and
            # none failed since: the truer warning, so it is the one given.
            if (
                alone
                and mine.others_answered is not None
                and not every.warned
                and not mine.warned
                and now - mine.others_answered >= OUTAGE_WARNING_AFTER
            ):
                logger.warning(
                    f'Codeforces has failed every {path} call since '
                    f'{_utc_time(mine.since)} UTC ({_plural(mine.calls, "call")}),'
                    f' while other calls work; the last: {error!r}'
                )
                mine.warned = True
            elif (
                not every.warned
                and now - every.since >= OUTAGE_WARNING_AFTER
                # A method already reported alone is no news either.
                and not (alone and mine.warned)
            ):
                logger.warning(
                    'Codeforces has failed every call since '
                    f'{_utc_time(every.since)} UTC ({_plural(every.calls, "call")});'
                    f' the last: {path}: {error!r}'
                )
                every.warned = True
        except Exception:
            logger.warning(
                f'Outage tracker failed on CF API {path}, ignoring.', exc_info=True
            )

    def answered(self, path: str) -> None:
        """Ends the outage, and the failures of ``path``. Methods that still
        fail now fail while other calls work.
        """
        try:
            now = self._clock()
            every, self._all = self._all, None
            mine = self._by_method.pop(path, None)
            if every is not None and every.warned and now - every.last < _STREAK_GAP:
                failing = _duration(now - every.since)
                logger.info(f'Codeforces answers again after {failing} of failures')
            if mine is not None and mine.warned and now - mine.last < _STREAK_GAP:
                failing = _duration(now - mine.since)
                logger.info(f'Codeforces answers {path} again after {failing}')
            for streak in self._by_method.values():
                if streak.others_answered is None:
                    streak.others_answered = now
        except Exception:
            logger.warning(
                f'Outage tracker failed on CF API {path}, ignoring.', exc_info=True
            )


_outage = _Outage()  # tests replace it, with a clock of their own


def cf_ratelimit(f: Callable[..., Any]) -> Callable[..., Any]:
    tries = 3
    per_second = 1
    last = deque([0.0] * per_second)

    @functools.wraps(f)
    async def wrapped(path: str, *args: Any, **kwargs: Any) -> Any:
        limited = 0  # tries that hit the call limit
        for i in itertools.count():
            now = time.time()

            # Next valid slot is _CALL_SPACING_SECONDS after the
            # `per_second`th last request
            next_valid = max(now, _CALL_SPACING_SECONDS + last[0])
            last.append(next_valid)
            last.popleft()

            # Delay as needed
            delay = next_valid - now
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                return await f(path, *args, **kwargs)
            except ClientError as e:
                # Each failure was logged as it came. Giving up is no news
                # either: the outage tracker warns once an outage lasts.
                if i == tries - 1:
                    error = e.__cause__ or e
                    logger.info(
                        f'CF API {path} failed {tries} times, giving up: {error!r}'
                    )
                    _outage.failed(path, error)
                    raise
            except CallLimitExceededError as e:
                limited += 1
                if i == tries - 1:
                    if limited == tries:
                        # Then the spacing between calls is too short.
                        logger.warning(
                            f'CF API {path} hit the call limit {tries} times in a row'
                        )
                    else:
                        logger.info(
                            f'CF API {path} failed {tries} times, giving up: {e!r}'
                        )
                    raise
        raise AssertionError('Unreachable')

    return wrapped


def _transient(path: str, detail: str) -> NoReturn:
    """Logs an answer to retry, and raises it as a ``ClientError``."""
    logger.info(f'Request to CF API {path} failed: {detail}')
    raise ClientError from BadAnswer(detail)


@cf_ratelimit
async def _query_api(
    path: str, data: dict[str, Any] | None = None, *, get: bool = False
) -> Any:
    """The result of the API method ``path``, called with ``data``.

    A POST request, whose size limits ``user_info_chunkify`` counts on, or with
    ``get``, a GET request with ``data`` as its query string.

    A failed request, an answer that isn't a JSON object, a 200 without a
    result and a server error raise ``ClientError``, which ``cf_ratelimit``
    retries. Any other JSON answer is a refusal, raised as ``TrueApiError``.
    """
    url = API_BASE_URL + path
    try:
        logger.info(f'Querying CF API at {url} with {data}')
        # Explicitly state encoding (though aiohttp accepts gzip by default)
        headers = {'Accept-Encoding': 'gzip'}
        assert _session is not None, 'Session not initialized. Call initialize() first.'
        if get:
            request = _session.get(
                url, params=data, headers=headers, timeout=_REQUEST_TIMEOUT
            )
        else:
            request = _session.post(
                url, data=data, headers=headers, timeout=_REQUEST_TIMEOUT
            )
        async with request as resp:
            status = resp.status
            try:
                answer = await resp.json()
            # An error page, such as a 502's, or a broken body
            except (aiohttp.ContentTypeError, ValueError):
                answer = None
    # A timeout is an asyncio.TimeoutError, which on 3.10 is no builtin
    # TimeoutError and no aiohttp.ClientError. Either is retried.
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.info(f'Request to CF API {path} failed: {e!r}')
        raise ClientError from e
    if answer is None:  # also an empty body
        _transient(path, f'HTTP {status} without JSON')
    if not isinstance(answer, dict):
        _transient(path, f'HTTP {status} without a JSON object')
    if status == 200:
        if 'result' not in answer:
            _transient(path, 'HTTP 200 without a result')
        _outage.answered(path)
        return answer['result']
    comment = f'HTTP Error {status}, {answer.get("comment")}'
    # Codeforces may give the call limit a 5xx status.
    if 'limit exceeded' in comment:
        # Retried, so not worth a warning yet.
        _outage.answered(path)
        logger.info(f'Query to CF API {path} failed: {comment}')
        raise CallLimitExceededError(comment)
    if status >= 500:
        _transient(path, f'HTTP {status}: {answer.get("comment")}')
    _outage.answered(path)
    if any(refusal in comment for refusal in _EXPECTED_REFUSALS):
        logger.info(f'Query to CF API {path} failed: {comment}')
    else:
        logger.warning(f'Query to CF API {path} failed: {comment}')
    raise TrueApiError(comment)


class contest:
    @staticmethod
    async def to_list(*, gym: bool | None = None) -> list[Contest]:
        """Returns a list of contests."""
        params = {}
        if gym is not None:
            params['gym'] = _bool_to_str(gym)
        resp = await _query_api('contest.list', params)
        return [make_from_dict(Contest, contest_dict) for contest_dict in resp]

    @staticmethod
    async def ratingChanges(*, contest_id: Any) -> list[RatingChange]:
        """Returns a list of rating changes for a contest."""
        params = {'contestId': contest_id}
        try:
            resp = await _query_api('contest.ratingChanges', params)
        except TrueApiError as e:
            if 'not found' in e.comment:
                raise ContestNotFoundError(e.comment, contest_id)
            if 'Rating changes are unavailable' in e.comment:
                raise RatingChangesUnavailableError(e.comment, contest_id)
            raise
        return [make_from_dict(RatingChange, change_dict) for change_dict in resp]

    @staticmethod
    async def standings(
        *, contest_id: int
    ) -> tuple[Contest, list[Problem], list[RanklistRow]]:
        """Returns a contest, its problems and its standings.

        Codeforces answers only an anonymous GET request whose one parameter is
        contestId: a POST, or any other parameter, is refused. The rows are
        official contestants' alone, without virtual, out-of-competition or
        practice ones. In some rounds, such as Div. 2, Div. 3 and Educational
        rounds, they are only the contestants that Codeforces trusts, ranked
        among themselves, so a rank can be better than the official one.
        """
        params = {'contestId': str(contest_id)}
        try:
            resp = await _query_api('contest.standings', params, get=True)
        except TrueApiError as e:
            if 'not found' in e.comment:
                raise ContestNotFoundError(e.comment, contest_id)
            raise
        return _parse_standings(resp)

    @staticmethod
    async def status(
        *,
        contest_id: int,
        handle: str | None = None,
        from_: int | None = None,
        count: int | None = None,
    ) -> list[Submission]:
        """Returns the submissions to a contest, newest first: everyone's, or
        those of ``handle`` alone.
        """
        params: dict[str, Any] = {'contestId': contest_id}
        if handle is not None:
            params['handle'] = handle
        if from_ is not None:
            params['from'] = from_
        if count is not None:
            params['count'] = count
        try:
            resp = await _query_api('contest.status', params)
        except TrueApiError as e:
            # The comment names the parameter at fault, as in "handle: User
            # with handle X not found" or "contestId: Contest with id N not
            # found".
            if handle is not None and 'handle:' in e.comment:
                if 'not found' in e.comment:
                    raise HandleNotFoundError(e.comment, handle)
                raise HandleInvalidError(e.comment, handle)
            if 'not found' in e.comment:
                raise ContestNotFoundError(e.comment, contest_id)
            raise
        return _parse_submissions(resp)


class problemset:
    @staticmethod
    async def problems(
        *, tags: list[str] | None = None, problemset_name: str | None = None
    ) -> tuple[list[Problem], list[ProblemStatistics]]:
        """Returns a list of problems."""
        params = {}
        if tags is not None:
            params['tags'] = ';'.join(tags)
        if problemset_name is not None:
            params['problemsetName'] = problemset_name
        resp = await _query_api('problemset.problems', params)
        problems = [
            make_from_dict(Problem, problem_dict) for problem_dict in resp['problems']
        ]
        problemstats = [
            make_from_dict(ProblemStatistics, problemstat_dict)
            for problemstat_dict in resp['problemStatistics']
        ]
        return problems, problemstats


def user_info_chunkify(handles: Iterable[str]) -> Iterator[list[str]]:
    """Yields chunks of handles that can be queried with user.info."""
    # Querying user.info using POST requests is limited to 10000 handles or 2**16
    # bytes, so requests might need to be split into chunks
    SIZE_LIMIT = 2**16
    HANDLE_LIMIT = 10000
    chunk: list[str] = []
    size = 0
    for handle in handles:
        if size + len(handle) > SIZE_LIMIT or len(chunk) == HANDLE_LIMIT:
            yield chunk
            chunk = []
            size = 0
        chunk.append(handle)
        size += len(handle) + 1
    if chunk:
        yield chunk


class user:
    @staticmethod
    async def info(*, handles: Sequence[str]) -> list[User]:
        """Returns a list of user info."""
        chunks = list(user_info_chunkify(handles))
        if len(chunks) > 1:
            logger.warning(
                f'cf.info request with {len(handles)} handles,'
                f' will be chunkified into {len(chunks)} requests.'
            )

        result = []
        for chunk in chunks:
            params = {'handles': ';'.join(chunk)}
            try:
                resp = await _query_api('user.info', params)
            except TrueApiError as e:
                if 'not found' in e.comment:
                    # Comment format is "handles: User with handle ***** not found"
                    handle = e.comment.partition('not found')[0].split()[-1]
                    raise HandleNotFoundError(e.comment, handle)
                raise
            result += [make_from_dict(User, user_dict) for user_dict in resp]
        return [fix_urls(user) for user in result]

    @staticmethod
    async def rating(*, handle: str) -> list[RatingChange]:
        """Returns a list of rating changes for a user."""
        params = {'handle': handle}
        try:
            resp = await _query_api('user.rating', params)
        except TrueApiError as e:
            if 'not found' in e.comment:
                raise HandleNotFoundError(e.comment, handle)
            if 'should contain' in e.comment:
                raise HandleInvalidError(e.comment, handle)
            raise
        return [
            make_from_dict(RatingChange, ratingchange_dict)
            for ratingchange_dict in resp
        ]

    @staticmethod
    async def ratedList(*, activeOnly: bool | None = None) -> list[User]:
        """Returns a list of rated users."""
        params = {}
        if activeOnly is not None:
            params['activeOnly'] = _bool_to_str(activeOnly)
        resp = await _query_api('user.ratedList', params)
        return [make_from_dict(User, user_dict) for user_dict in resp]

    @staticmethod
    async def status(
        *, handle: str, from_: int | None = None, count: int | None = None
    ) -> list[Submission]:
        """Returns a list of submissions for a user."""
        params: dict[str, Any] = {'handle': handle}
        if from_ is not None:
            params['from'] = from_
        if count is not None:
            params['count'] = count
        try:
            resp = await _query_api('user.status', params)
        except TrueApiError as e:
            if 'not found' in e.comment:
                raise HandleNotFoundError(e.comment, handle)
            if 'should contain' in e.comment:
                raise HandleInvalidError(e.comment, handle)
            raise
        return _parse_submissions(resp)


async def _resolve_redirect(handle: str) -> str | None:
    url = PROFILE_BASE_URL + handle
    assert _session is not None, 'Session not initialized. Call initialize() first.'
    async with _session.head(url) as r:
        if r.status == 200:
            return handle
        if r.status == 302:
            redirected = r.headers.get('Location')
            if redirected is None or '/profile/' not in redirected:
                # Ended up not on profile page, probably invalid handle
                return None
            return str(redirected.split('/profile/')[-1])
        raise CodeforcesApiError(f'Something went wrong trying to redirect {url}')


async def _resolve_handle_to_new_user(
    handle: str,
) -> User | None:
    new_handle = await _resolve_redirect(handle)
    if new_handle is None:
        return None
    (cf_user,) = await user.info(handles=[new_handle])
    return cf_user


async def _resolve_handles(handles: Iterable[str]) -> dict[str, User]:
    chunks = user_info_chunkify(handles)

    resolved_handles: dict[str, User] = {}
    for handle_chunk in chunks:
        while handle_chunk:
            try:
                cf_users = await user.info(handles=handle_chunk)
                # No failure, all handles resolve to users,
                for handle, cf_user in zip(handle_chunk, cf_users, strict=False):
                    if cf_user is not None:
                        resolved_handles[handle] = cf_user
                break
            except HandleNotFoundError as e:
                # Handle not found, drop it. A typo or a deleted account is no
                # failure, and the client has logged the refusal at INFO too.
                logger.info(f'Handle {e.handle} not found, dropping it.')
                handle_chunk.remove(e.handle)
    return resolved_handles


async def resolve_redirects(
    handles: Iterable[str], skip_filter: bool = False
) -> dict[str, User]:
    """Returns a mapping of handles to their resolved CF users."""
    resolved_handles = await _resolve_handles(handles)
    if skip_filter:
        return resolved_handles

    return {
        handle: cf_user
        for handle, cf_user in resolved_handles.items()
        if cf_user is not None and handle != cf_user.handle
    }
