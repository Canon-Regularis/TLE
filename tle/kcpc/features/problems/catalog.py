"""Codeforces' and AtCoder's problems, kept in memory to pick from and to find.

Codeforces' problems come from its API, through TLE's client
(``codeforces.fetch_problems``), and AtCoder's from AtCoder Problems
(``AtCoderProblemsClient``). A job refreshes each list once it is older than
its max age: Codeforces rates a new round's problems within days, and AtCoder
Problems estimates difficulties within hours of a contest, so a few hours' lag
changes little. A refresh that fails keeps the list there was, so a site that
is down changes nothing that members see.

Both lists hold ``Problem``s, one shape for both platforms. Each platform's
pool is what /randproblem and the weekly problem pick from: Codeforces'
rated problems of standard rounds, and AtCoder Problems' problems of ABC, ARC
and AGC with a difficulty.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from urllib.parse import urlsplit

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.platforms import codeforces
from tle.kcpc.platforms.atcoder import problems as atcoder
from tle.kcpc.platforms.atcoder.problems import AtCoderProblem, AtCoderProblemsClient
from tle.kcpc.platforms.codeforces import CodeforcesProblem
from tle.kcpc.platforms.difficulty import Band, band_of

logger = logging.getLogger(__name__)

CODEFORCES = codeforces.PLATFORM
ATCODER = atcoder.PLATFORM
PLATFORMS = (CODEFORCES, ATCODER)

# How old each platform's list may get before a refresh fetches it again.
CODEFORCES_MAX_AGE = timedelta(hours=6)
ATCODER_MAX_AGE = timedelta(hours=24)

_MAX_AGES: Mapping[str, timedelta] = MappingProxyType(
    {CODEFORCES: CODEFORCES_MAX_AGE, ATCODER: ATCODER_MAX_AGE}
)
# How replies and posts name each platform.
_PLATFORM_NAMES: Mapping[str, str] = MappingProxyType(
    {CODEFORCES: 'Codeforces', ATCODER: 'AtCoder'}
)
_PLATFORM_POSSESSIVES: Mapping[str, str] = MappingProxyType(
    {CODEFORCES: "Codeforces'", ATCODER: "AtCoder's"}
)
_NOT_LOADED = (
    "{platform} problem list isn't loaded yet. Please try again in a few minutes."
)
_EMPTY_LIST = '{platform} problem list came back empty.'
_NOT_A_PROBLEM = (
    "That isn't a problem I know how to read. Give a Codeforces problem as "
    '1520D or its link, or an AtCoder problem as abc300_d or its link.'
)

# A Codeforces problem as members write it: '1520D', '1520 D' or '1520/D'.
# Contest 921's problems are '01' to '14', so an index may start with a digit,
# but only after a space or slash: '92101' is no problem.
_CODEFORCES_SPACED = re.compile(r'(\d{1,6})(?:\s*/\s*|\s+)([A-Za-z0-9]{1,3})', re.ASCII)
_CODEFORCES_JOINED = re.compile(r'(\d{1,6})([A-Za-z][A-Za-z0-9]{0,2})', re.ASCII)
# Its page: /contest/1520/problem/D or /problemset/problem/1520/D.
_CODEFORCES_PATHS = (
    re.compile(r'/contest/(\d{1,6})/problem/([A-Za-z0-9]{1,3})/?', re.ASCII),
    re.compile(r'/problemset/problem/(\d{1,6})/([A-Za-z0-9]{1,3})/?', re.ASCII),
)
# AtCoder Problems' ID, such as 'abc300_d': letters, digits and '_', with a
# '_' between the contest and the letter.
_ATCODER_ID = re.compile(r'[A-Za-z0-9]\w*_\w*', re.ASCII)
# A few IDs have no '_', such as 'joi2011ho1': a word that nothing else reads
# may be one.
_ATCODER_WORD = re.compile(r'[A-Za-z][A-Za-z0-9]*', re.ASCII)
_ATCODER_ID_LIMIT = 64
# A task's page: /contests/abc300/tasks/abc300_d.
_ATCODER_PATH = re.compile(r'/contests/([\w-]{1,64})/tasks/(\w{1,64})/?', re.ASCII)
_ATCODER_HOSTS = ('atcoder.jp', 'www.atcoder.jp')
_CODEFORCES_HOST = 'codeforces.com'  # also its mirrors, such as m1.codeforces.com


@dataclass(frozen=True)
class Problem:
    """A problem on either platform, as /randproblem and the weekly problem see it."""

    platform: str  # CODEFORCES or ATCODER
    problem_id: str  # '1520D', or AtCoder Problems' 'abc300_d' in its case
    contest_id: str  # '1520', or 'abc300': the contest that set it
    index: str  # its letter in that contest: 'D', 'F2', 'Ex'
    name: str
    title: str  # as posts name it (``problem_title``)
    url: str
    contest_url: str
    rating: int | None  # on Codeforces' scale; None if not rated
    difficulty: int | None  # AtCoder's own, clipped; None on Codeforces
    tags: tuple[str, ...]  # Codeforces' tags; none on AtCoder
    solved_count: int | None  # how many have solved it, if Codeforces says
    in_pool: bool  # whether problems are picked from it (see the module docstring)

    @property
    def band(self) -> Band | None:
        """The band its rating is in; None if it has none."""
        return None if self.rating is None else band_of(self.rating)

    @classmethod
    def from_codeforces(cls, problem: CodeforcesProblem) -> 'Problem':
        contest_id = str(problem.contest_id)
        return cls(
            platform=CODEFORCES,
            problem_id=problem.problem_id,
            contest_id=contest_id,
            index=problem.index,
            name=problem.name,
            title=problem_title(CODEFORCES, contest_id, problem.index, problem.name),
            url=problem.url,
            contest_url=problem.contest_url,
            rating=problem.rating,
            difficulty=None,
            tags=problem.tags,
            solved_count=problem.solved_count,
            in_pool=problem.rating is not None and problem.standard,
        )

    @classmethod
    def from_atcoder(cls, problem: AtCoderProblem) -> 'Problem':
        return cls(
            platform=ATCODER,
            problem_id=problem.problem_id,
            contest_id=problem.contest_id,
            index=problem.index,
            name=problem.name,
            title=problem.title,
            url=problem.url,
            contest_url=problem.contest_url,
            rating=problem.rating,
            difficulty=problem.difficulty,
            tags=(),
            solved_count=None,
            in_pool=problem.in_pool,
        )


def platform_name(platform: str) -> str:
    """The platform as replies and posts name it: 'Codeforces' or 'AtCoder'."""
    return _PLATFORM_NAMES.get(platform, platform)


def platform_possessive(platform: str) -> str:
    """The platform's name as replies say what is its: "Codeforces'" or
    "AtCoder's".
    """
    return _PLATFORM_POSSESSIVES.get(platform, f"{platform}'s")


def problem_title(platform: str, contest_id: str, index: str, name: str) -> str:
    """A problem as posts name it: '1520D - Same Differences' on Codeforces,
    'ABC300 D - AABCC' on AtCoder.
    """
    if platform == ATCODER:
        return f'{contest_id.upper()} {index} - {name}'
    return f'{contest_id}{index} - {name}'


def describe_difficulty(
    platform: str, rating: int | None, difficulty: int | None
) -> str:
    """How hard a problem is, as posts say it.

    '1600 (hard)' on Codeforces. On AtCoder, its own difficulty then its
    rating on Codeforces' scale: '1376 on AtCoder (about 1752 on Codeforces,
    hard)', or 'about 1752 on Codeforces (hard)' without its own. 'unrated'
    without a rating.
    """
    if rating is None:
        return 'unrated'
    band = band_of(rating).value
    if platform != ATCODER:
        return f'{rating} ({band})'
    if difficulty is None:
        return f'about {rating} on Codeforces ({band})'
    return f'{difficulty} on AtCoder (about {rating} on Codeforces, {band})'


@dataclass(frozen=True)
class ProblemRef:
    """A problem that someone named, before it is looked up."""

    platform: str
    problem_id: str  # '1520D', or the AtCoder ID as given: 'abc300_d'
    contest_id: str
    index: str | None  # Codeforces' index; None on AtCoder, whose IDs say it


def parse_problem_ref(text: str) -> ProblemRef:
    """The problem that ``text`` names, on Codeforces or AtCoder.

    Codeforces: '1520D', '1520 D', '1520/D', or a link to
    codeforces.com/contest/1520/problem/D or /problemset/problem/1520/D; the
    index is upper-cased. AtCoder: an ID with a '_', such as 'abc300_d', or a
    link to atcoder.jp/contests/abc300/tasks/abc300_d; else a word that may be
    an ID without a '_', such as 'joi2011ho1', which only looking it up tells.
    Any space counts as one, a no-break space say, and a link may be in
    <...>, as Discord users put one to keep it from showing a preview. Raises
    ``KcpcUserError``, with examples, for anything else.
    """
    text = ' '.join(text.split())
    if text.startswith('<') and text.endswith('>'):
        text = text[1:-1].strip()
    for pattern in (_CODEFORCES_SPACED, _CODEFORCES_JOINED):
        match = pattern.fullmatch(text)
        if match is not None:
            return _codeforces_ref(match.group(1), match.group(2))
    if len(text) <= _ATCODER_ID_LIMIT and _ATCODER_ID.fullmatch(text) is not None:
        # The contest is a guess: 'cf17_final_a' is in 'cf17-final'. Finding
        # the problem needs only its ID.
        return ProblemRef(ATCODER, text, text.rsplit('_', 1)[0], None)
    ref = _ref_in_link(text)
    if ref is not None:
        return ref
    if len(text) <= _ATCODER_ID_LIMIT and _ATCODER_WORD.fullmatch(text) is not None:
        return ProblemRef(ATCODER, text, text, None)  # the contest a guess again
    raise KcpcUserError(_NOT_A_PROBLEM)


class ProblemCatalog:
    """Each platform's problems, refreshed by ``refresh``; see the module docstring.

    Until a platform's list is first loaded, reading it raises
    ``KcpcUserError`` asking members to try again in a few minutes.
    """

    def __init__(self, atcoder: AtCoderProblemsClient, clock: Clock) -> None:
        self._atcoder = atcoder
        self._clock = clock
        self._lists: dict[str, _ProblemList] = {}
        # Refreshes take turns, so that one started while another is fetching
        # finds the lists fresh instead of fetching them again.
        self._lock = asyncio.Lock()

    def loaded(self, platform: str) -> bool:
        """Whether the platform's list has been loaded."""
        _check_platform(platform)
        return platform in self._lists

    def refreshed_at(self, platform: str) -> datetime | None:
        """When the platform's list was last loaded; None if it hasn't been."""
        _check_platform(platform)
        problems = self._lists.get(platform)
        return None if problems is None else problems.refreshed_at

    async def refresh(self, *, force: bool = False) -> dict[str, Exception]:
        """Fetch each platform's list that isn't loaded or is older than its
        max age, or every list if ``force``, one platform after the other.

        A platform whose fetch fails, or comes back empty, keeps the list it
        had. Returns those failures by platform, for the caller to report;
        none are logged here.
        """
        async with self._lock:
            failures: dict[str, Exception] = {}
            for platform in PLATFORMS:
                if not force and not self._stale(platform):
                    continue
                try:
                    problems = await self._fetch(platform)
                except Exception as exc:
                    failures[platform] = exc
                    continue
                self._lists[platform] = _ProblemList.of(problems, self._clock.now())
                logger.info(
                    'Loaded %d %s problems, %d of them to pick from',
                    len(problems),
                    platform_name(platform),
                    len(self._lists[platform].pool),
                )
            return failures

    def problems(self, platform: str) -> Sequence[Problem]:
        """Every problem of the platform, in its site's order.

        Raises ``KcpcUserError`` if the platform's list isn't loaded yet.
        """
        return self._list(platform).problems

    def pool(self, platform: str) -> Sequence[Problem]:
        """The platform's problems to pick from, in its site's order.

        Raises ``KcpcUserError`` if the platform's list isn't loaded yet.
        """
        return self._list(platform).pool

    def find(self, ref: ProblemRef) -> Problem | None:
        """The problem that ``ref`` names; None if its platform has no such
        problem. AtCoder IDs match whatever their case, as AtCoder's do.

        Raises ``KcpcUserError`` if the platform's list isn't loaded yet.
        """
        problems = self._list(ref.platform)
        return problems.by_key.get(_key(ref.platform, ref.problem_id))

    def lists(self, platform: str, problem_id: str) -> bool:
        """Whether the platform's list has a problem under ``problem_id``, as
        ``find`` matches it; False while the list isn't loaded.
        """
        _check_platform(platform)
        problems = self._lists.get(platform)
        return problems is not None and _key(platform, problem_id) in problems.by_key

    def tags(self) -> frozenset[str]:
        """Every tag of Codeforces' problems but '*special'; none until loaded."""
        problems = self._lists.get(CODEFORCES)
        return frozenset() if problems is None else problems.tags

    def _stale(self, platform: str) -> bool:
        problems = self._lists.get(platform)
        if problems is None:
            return True
        return self._clock.now() - problems.refreshed_at >= _MAX_AGES[platform]

    async def _fetch(self, platform: str) -> list[Problem]:
        if platform == CODEFORCES:
            # Through the module, so that tests can stand in for TLE's client.
            fetched = await codeforces.fetch_problems()
            problems = [Problem.from_codeforces(problem) for problem in fetched]
        else:
            problem_set = await self._atcoder.fetch_problem_set()
            problems = [
                Problem.from_atcoder(problem) for problem in problem_set.values()
            ]
        if not problems:
            # Neither site has ever had no problems: keep the list there was.
            name = platform_name(platform)
            owner = platform_possessive(platform)
            raise ExternalServiceError(name, _EMPTY_LIST.format(platform=owner))
        return problems

    def _list(self, platform: str) -> '_ProblemList':
        _check_platform(platform)
        problems = self._lists.get(platform)
        if problems is None:
            owner = platform_possessive(platform)
            raise KcpcUserError(_NOT_LOADED.format(platform=owner))
        return problems


@dataclass(frozen=True)
class _ProblemList:
    """One platform's problems as last loaded."""

    problems: tuple[Problem, ...]
    pool: tuple[Problem, ...]
    by_key: Mapping[str, Problem]  # see _key
    tags: frozenset[str]  # every tag but '*special'
    refreshed_at: datetime

    @classmethod
    def of(cls, problems: Iterable[Problem], refreshed_at: datetime) -> '_ProblemList':
        listed = tuple(problems)
        by_key: dict[str, Problem] = {}
        for problem in listed:
            by_key.setdefault(_key(problem.platform, problem.problem_id), problem)
        return cls(
            problems=listed,
            pool=tuple(problem for problem in listed if problem.in_pool),
            by_key=MappingProxyType(by_key),
            tags=frozenset(
                tag
                for problem in listed
                for tag in problem.tags
                if tag != codeforces.SPECIAL_TAG
            ),
            refreshed_at=refreshed_at,
        )


def _key(platform: str, problem_id: str) -> str:
    """What a problem is found by: its ID, lowercased on AtCoder."""
    return problem_id.lower() if platform == ATCODER else problem_id


def _check_platform(platform: str) -> None:
    if platform not in PLATFORMS:
        raise ValueError(f'There are no problems of {platform!r}')


def _codeforces_ref(contest: str, index: str) -> ProblemRef:
    contest_id = str(int(contest))  # without leading zeros
    index = index.upper()
    return ProblemRef(CODEFORCES, f'{contest_id}{index}', contest_id, index)


def _ref_in_link(text: str) -> ProblemRef | None:
    """The problem that a link to its page names; None if ``text`` isn't one."""
    try:
        parts = urlsplit(text if '://' in text else f'https://{text}')
        host = parts.hostname or ''
    except ValueError:  # e.g. an unclosed [ in the host
        return None
    if parts.scheme not in ('http', 'https'):
        return None
    if host == _CODEFORCES_HOST or host.endswith(f'.{_CODEFORCES_HOST}'):
        for pattern in _CODEFORCES_PATHS:
            match = pattern.fullmatch(parts.path)
            if match is not None:
                return _codeforces_ref(match.group(1), match.group(2))
    elif host in _ATCODER_HOSTS:
        match = _ATCODER_PATH.fullmatch(parts.path)
        if match is not None:
            return ProblemRef(ATCODER, match.group(2), match.group(1), None)
    return None
