"""Tests for tle.kcpc.platforms.codeforces: upcoming rounds and rating changes
from TLE's cache, users through TLE's user.info, and problems through its
problemset.problems, contest.list and user.status, which the tests stand in
for."""

import asyncio
import dataclasses
import logging
from collections import deque
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.platforms.codeforces import (
    NONSTANDARD_CONTEST_WORDS,
    SPECIAL_TAG,
    CodeforcesContest,
    CodeforcesProblem,
    CodeforcesRatingChange,
    CodeforcesUser,
    SolvedProblems,
    fetch_problems,
    fetch_solved,
    fetch_user,
    fetch_users,
    problem_catalog,
    rank_name,
    rating_changes,
    upcoming_contests,
)
from tle.util import codeforces_api as cf

LONDON = ZoneInfo('Europe/London')
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
TWO_HOURS = timedelta(hours=2)


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


ROUND_START = at(2026, 10, 3, 14, 35)


def contest(
    contest_id: int,
    *,
    start: datetime | None = ROUND_START,
    duration: timedelta | None = TWO_HOURS,
    phase: str = 'BEFORE',
) -> cf.Contest:
    """A contest as TLE caches it from Codeforces' contest list."""
    return cf.Contest(
        id=contest_id,
        name=f'Codeforces Round {contest_id}',
        startTimeSeconds=None if start is None else to_epoch(start),
        durationSeconds=None if duration is None else int(duration.total_seconds()),
        type='CF',
        phase=phase,
        preparedBy=None,
    )


def ids(contests: list[CodeforcesContest]) -> list[int]:
    return [c.contest_id for c in contests]


class TestUpcomingContests:
    def test_a_contest_becomes_a_codeforces_contest(self) -> None:
        cached = contest(
            2150, start=at(2026, 10, 3, 14, 35), duration=timedelta(hours=2, minutes=15)
        )

        [upcoming] = upcoming_contests([cached], now=NOW)

        assert upcoming == CodeforcesContest(
            contest_id=2150,
            name='Codeforces Round 2150',
            start=at(2026, 10, 3, 14, 35),
            end=at(2026, 10, 3, 16, 50),
            url='https://codeforces.com/contests/2150',
        )
        assert upcoming.start.tzinfo is UTC
        assert upcoming.end.tzinfo is UTC

    @pytest.mark.parametrize('phase', ['BEFORE', 'CODING'])
    def test_contests_yet_to_start_or_running_are_kept(self, phase: str) -> None:
        running = contest(2150, start=NOW - timedelta(hours=1), phase=phase)
        assert ids(upcoming_contests([running], now=NOW)) == [2150]

    @pytest.mark.parametrize(
        'phase', ['PENDING_SYSTEM_TEST', 'SYSTEM_TEST', 'FINISHED', 'before']
    )
    def test_contests_in_other_phases_are_left_out(self, phase: str) -> None:
        cached = contest(2150, start=NOW + timedelta(days=1), phase=phase)
        assert upcoming_contests([cached], now=NOW) == []

    @pytest.mark.parametrize(
        ('start', 'duration'),
        [
            (None, TWO_HOURS),
            (at(2026, 10, 3, 14, 35), None),
            (None, None),
            (at(2026, 10, 3, 14, 35), timedelta(0)),
            (at(2026, 10, 3, 14, 35), timedelta(seconds=-1)),
        ],
        ids=['no-start', 'no-duration', 'neither', 'zero-duration', 'negative'],
    )
    def test_contests_without_a_start_and_a_duration_are_left_out(
        self, start: datetime | None, duration: timedelta | None
    ) -> None:
        cached = contest(2150, start=start, duration=duration)
        assert upcoming_contests([cached], now=NOW) == []

    @pytest.mark.parametrize(
        ('end', 'kept'),
        [
            (NOW - timedelta(seconds=1), False),
            (NOW, False),
            (NOW + timedelta(seconds=1), True),
        ],
        ids=['ended-before', 'ends-now', 'ends-after'],
    )
    @pytest.mark.parametrize('phase', ['BEFORE', 'CODING'])
    def test_only_contests_ending_after_now_are_kept(
        self, end: datetime, kept: bool, phase: str
    ) -> None:
        # The cache can lag: a contest it has as CODING, or even BEFORE, may
        # already be over.
        cached = contest(2150, start=end - TWO_HOURS, phase=phase)
        assert ids(upcoming_contests([cached], now=NOW)) == ([2150] if kept else [])

    def test_contests_are_sorted_by_start_then_id(self) -> None:
        div1_and_div2 = at(2026, 10, 3, 14, 35)
        cached = [
            contest(2152, start=at(2026, 10, 9, 14, 35)),
            contest(2151, start=div1_and_div2),
            contest(2160, start=at(2026, 10, 2, 9)),
            contest(2150, start=div1_and_div2),
        ]
        assert ids(upcoming_contests(cached, now=NOW)) == [2160, 2150, 2151, 2152]

    def test_reads_any_iterable(self) -> None:
        cached = (contest(contest_id) for contest_id in (2151, 2150))
        assert ids(upcoming_contests(cached, now=NOW)) == [2150, 2151]

    def test_no_contests_is_an_empty_list(self) -> None:
        assert upcoming_contests([], now=NOW) == []

    def test_now_may_be_in_any_zone(self) -> None:
        cached = contest(2150, start=NOW - TWO_HOURS + timedelta(seconds=1))
        london_now = NOW.astimezone(LONDON)
        assert ids(upcoming_contests([cached], now=london_now)) == [2150]

    def test_a_naive_now_is_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            upcoming_contests([contest(2150)], now=datetime(2026, 10, 1, 12))


CONTEST = CodeforcesContest(
    contest_id=2150,
    name='Codeforces Round 2150',
    start=at(2026, 10, 3, 14, 35),
    end=at(2026, 10, 3, 16, 35),
    url='https://codeforces.com/contests/2150',
)


class TestCodeforcesContest:
    def test_times_become_utc_in_whole_seconds(self) -> None:
        contest = dataclasses.replace(
            CONTEST,
            start=datetime(2026, 10, 3, 15, 35, 0, 999_999, tzinfo=LONDON),
            end=datetime(2026, 10, 3, 17, 35, 30, 1, tzinfo=LONDON),
        )
        assert (contest.start, contest.end) == (
            at(2026, 10, 3, 14, 35),
            datetime(2026, 10, 3, 16, 35, 30, tzinfo=UTC),
        )
        assert contest.start.tzinfo is UTC
        assert contest.end.tzinfo is UTC

    def test_naive_times_are_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, start=datetime(2026, 10, 3, 14, 35))
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, end=datetime(2026, 10, 3, 16, 35))

    @pytest.mark.parametrize(
        'end', [at(2026, 10, 3, 14, 35), at(2026, 10, 3, 14, 34)], ids=['at', 'before']
    )
    def test_the_end_must_be_after_the_start(self, end: datetime) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            dataclasses.replace(CONTEST, end=end)


LOGGER = 'tle.kcpc.platforms.codeforces'
NOT_RESPONDING = r'^Codeforces is not responding right now\. Please try again later\.$'


def cf_user(
    handle: str,
    *,
    rating: int | None = None,
    max_rating: int | None = None,
    organization: str | None = None,
) -> cf.User:
    """A user as TLE's user.info gives them."""
    return cf.User(
        handle=handle,
        firstName=None,
        lastName=None,
        country=None,
        city=None,
        organization=organization,
        contribution=0,
        rating=rating,
        maxRating=max_rating,
        lastOnlineTimeSeconds=1_790_000_000,
        registrationTimeSeconds=1_700_000_000,
        friendOfCount=0,
        titlePhoto='https://userpic.codeforces.org/no-title.jpg',
    )


EXAMPLE = cf_user(
    'kcpc_Example', rating=1834, max_rating=1912, organization='Example University'
)
NEWCOMER = cf_user('kcpc.newcomer', organization='')
LINKER = cf_user('Kcpc-Linker', rating=1012, max_rating=1105)


def handles(users: list[CodeforcesUser]) -> list[str]:
    return [user.handle for user in users]


def numbered(count: int) -> list[cf.User]:
    return [cf_user(f'kcpc_{number:03}') for number in range(count)]


class FakeUserInfo:
    """Stands in for TLE's ``cf.user.info``, answering as Codeforces does.

    It knows its users by handle, whatever the case. Like Codeforces, it fails
    a whole request with ``cf.HandleNotFoundError`` if any handle in it is
    unknown, naming the first. It records the handles of every request, and
    ``fail_next`` makes the next requests raise instead.
    """

    def __init__(self) -> None:
        self.requests: list[list[str]] = []
        self._users: dict[str, cf.User] = {}
        self._failures: deque[BaseException] = deque()

    def add(self, *users: cf.User) -> None:
        self._users.update((user.handle.lower(), user) for user in users)

    def fail_next(self, *errors: BaseException) -> None:
        self._failures.extend(errors)

    async def info(self, *, handles: Sequence[str]) -> list[cf.User]:
        self.requests.append(list(handles))
        if self._failures:
            raise self._failures.popleft()
        for handle in handles:
            if handle.lower() not in self._users:
                comment = f'handles: User with handle {handle} not found'
                raise cf.HandleNotFoundError(comment, handle)
        return [self._users[handle.lower()] for handle in handles]


@pytest.fixture
def user_info(monkeypatch: pytest.MonkeyPatch) -> FakeUserInfo:
    fake = FakeUserInfo()
    fake.add(EXAMPLE, NEWCOMER, LINKER)
    monkeypatch.setattr(cf.user, 'info', fake.info)
    return fake


class TestFetchUsers:
    async def test_a_user_becomes_a_codeforces_user(
        self, user_info: FakeUserInfo
    ) -> None:
        assert await fetch_users(['kcpc_Example']) == [
            CodeforcesUser(
                handle='kcpc_Example',
                rating=1834,
                max_rating=1912,
                rank='expert',
                organization='Example University',
                url='https://codeforces.com/profile/kcpc_Example',
            )
        ]
        assert user_info.requests == [['kcpc_Example']]

    async def test_an_unrated_user_has_no_rating_or_rank(
        self, user_info: FakeUserInfo
    ) -> None:
        [user] = await fetch_users(['kcpc.newcomer'])
        assert user == CodeforcesUser(
            handle='kcpc.newcomer',
            rating=None,
            max_rating=None,
            rank=None,
            organization=None,
            url='https://codeforces.com/profile/kcpc.newcomer',
        )

    async def test_handles_find_users_whatever_their_case(
        self, user_info: FakeUserInfo
    ) -> None:
        users = await fetch_users(['KCPC_EXAMPLE', 'kcpc-linker'])

        assert handles(users) == ['kcpc_Example', 'Kcpc-Linker']
        assert [user.url for user in users] == [
            'https://codeforces.com/profile/kcpc_Example',
            'https://codeforces.com/profile/Kcpc-Linker',
        ]
        assert user_info.requests == [['KCPC_EXAMPLE', 'kcpc-linker']]

    @pytest.mark.parametrize(
        ('rating', 'rank'),
        [
            (-24, 'newbie'),
            (0, 'newbie'),
            (1199, 'newbie'),
            (1200, 'pupil'),
            (1400, 'specialist'),
            (1600, 'expert'),
            (1900, 'candidate master'),
            (2100, 'master'),
            (2300, 'international master'),
            (2400, 'grandmaster'),
            (2600, 'international grandmaster'),
            (2999, 'international grandmaster'),
            (3000, 'legendary grandmaster'),
            (3979, 'legendary grandmaster'),
        ],
    )
    async def test_the_rank_is_codeforces_name_for_the_rating(
        self, user_info: FakeUserInfo, rating: int, rank: str
    ) -> None:
        user_info.add(cf_user('kcpc_rated', rating=rating, max_rating=3979))
        [user] = await fetch_users(['kcpc_rated'])
        assert user.rank == rank

    async def test_the_rank_is_for_the_rating_not_the_highest(
        self, user_info: FakeUserInfo
    ) -> None:
        user_info.add(cf_user('kcpc_fallen', rating=1500, max_rating=2000))
        [user] = await fetch_users(['kcpc_fallen'])
        assert (user.rating, user.max_rating, user.rank) == (1500, 2000, 'specialist')

    @pytest.mark.parametrize(
        ('organization', 'shown'),
        [
            ('Example University', 'Example University'),
            ('KCPC kcpc-5e1f0a', 'KCPC kcpc-5e1f0a'),
            ('  Example \t University\n', 'Example University'),
            ('', None),
            (' \n ', None),
            (None, None),
        ],
        ids=['plain', 'token', 'whitespace', 'empty', 'blank', 'absent'],
    )
    async def test_the_organization_as_shown(
        self, user_info: FakeUserInfo, organization: str | None, shown: str | None
    ) -> None:
        user_info.add(cf_user('kcpc_member', organization=organization))
        [user] = await fetch_users(['kcpc_member'])
        assert user.organization == shown

    async def test_users_come_in_the_order_asked(self, user_info: FakeUserInfo) -> None:
        users = await fetch_users(('Kcpc-Linker', 'kcpc.newcomer', 'kcpc_Example'))
        assert handles(users) == ['Kcpc-Linker', 'kcpc.newcomer', 'kcpc_Example']

    @pytest.mark.parametrize(
        ('count', 'sizes'),
        [(1, [1]), (299, [299]), (300, [300]), (301, [300, 1]), (650, [300, 300, 50])],
    )
    async def test_asks_in_batches_of_at_most_300(
        self, user_info: FakeUserInfo, count: int, sizes: list[int]
    ) -> None:
        many = [user.handle for user in numbered(count)]
        user_info.add(*numbered(count))

        users = await fetch_users(many)

        assert handles(users) == many
        assert [len(request) for request in user_info.requests] == sizes
        assert [h for request in user_info.requests for h in request] == many

    async def test_a_batch_with_an_unknown_handle_is_asked_about_handle_by_handle(
        self, user_info: FakeUserInfo
    ) -> None:
        asked = ['kcpc_Example', 'kcpc_nobody', 'Kcpc-Linker']

        users = await fetch_users(asked)

        assert handles(users) == ['kcpc_Example', 'Kcpc-Linker']
        assert user_info.requests == [
            asked,
            ['kcpc_Example'],
            ['kcpc_nobody'],
            ['Kcpc-Linker'],
        ]

    async def test_only_a_batch_with_an_unknown_handle_is_asked_about_again(
        self, user_info: FakeUserInfo
    ) -> None:
        many = [user.handle for user in numbered(301)]
        user_info.add(*numbered(301))

        users = await fetch_users([*many, 'kcpc_nobody'])

        assert handles(users) == many
        assert user_info.requests == [
            many[:300],
            ['kcpc_300', 'kcpc_nobody'],
            ['kcpc_300'],
            ['kcpc_nobody'],
        ]

    async def test_an_unknown_handle_alone_is_asked_about_once(
        self, user_info: FakeUserInfo
    ) -> None:
        assert await fetch_users(['kcpc_nobody']) == []
        assert user_info.requests == [['kcpc_nobody']]

    async def test_every_unknown_handle_is_left_out(
        self, user_info: FakeUserInfo
    ) -> None:
        users = await fetch_users(['kcpc_nobody', 'kcpc_Example', 'kcpc_no_one'])

        assert handles(users) == ['kcpc_Example']
        assert user_info.requests[1:] == [
            ['kcpc_nobody'],
            ['kcpc_Example'],
            ['kcpc_no_one'],
        ]

    async def test_unknown_handles_are_logged_at_debug(
        self, user_info: FakeUserInfo, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        await fetch_users(['kcpc_nobody'])
        await fetch_users(['kcpc_nobody', 'kcpc_Example', 'kcpc_no_one'])
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * 3

    async def test_each_user_is_asked_about_once(self, user_info: FakeUserInfo) -> None:
        users = await fetch_users(
            ['kcpc_example', 'Kcpc-Linker', 'KCPC_EXAMPLE', 'kcpc_Example']
        )

        assert handles(users) == ['kcpc_Example', 'Kcpc-Linker']
        assert user_info.requests == [['kcpc_example', 'Kcpc-Linker']]

    @pytest.mark.parametrize(
        'handle',
        [
            '',
            'kcpc_Example;Kcpc-Linker',
            'kcpc Example',
            'kcpc!Example',
            'kcpc/Example',
            'kcpc_Example\n',
            'naïve',
            'ｋcpc_Example',  # a full-width k
        ],
        ids=[
            'empty',
            'separator',
            'space',
            'punctuation',
            'slash',
            'newline',
            'accent',
            'full-width',
        ],
    )
    async def test_handles_that_cannot_be_codeforces_handles_are_never_sent(
        self, user_info: FakeUserInfo, handle: str
    ) -> None:
        # Codeforces would drop the odd character, and so answer for another
        # user ('kcpc!Example' is kcpcExample), or split the list at the ';'.
        users = await fetch_users([handle, 'kcpc.newcomer'])

        assert handles(users) == ['kcpc.newcomer']
        assert user_info.requests == [['kcpc.newcomer']]

    async def test_handles_of_any_length_are_sent(
        self, user_info: FakeUserInfo
    ) -> None:
        # Codeforces answers that no one has them, rather than refusing them.
        asked = ['k', 'kc', 'kcpc_' + 'x' * 30]

        assert await fetch_users(asked) == []
        assert user_info.requests[0] == asked

    @pytest.mark.parametrize('asked', [[], ['', 'kcpc Example']], ids=['none', 'bad'])
    async def test_no_handles_to_send_means_no_request(
        self, user_info: FakeUserInfo, asked: list[str]
    ) -> None:
        assert await fetch_users(asked) == []
        assert user_info.requests == []

    @pytest.mark.parametrize(
        'error',
        [
            cf.ClientError(),
            cf.CallLimitExceededError('Call limit exceeded'),
            cf.TrueApiError('HTTP Error 503, Service Unavailable'),
            cf.CodeforcesApiError(),
            asyncio.TimeoutError(),
        ],
        ids=['client', 'call-limit', 'api', 'not-json', 'timeout'],
    )
    async def test_other_failures_are_external_service_errors(
        self, user_info: FakeUserInfo, error: Exception
    ) -> None:
        user_info.fail_next(error)

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING) as excinfo:
            await fetch_users(['kcpc_Example', 'Kcpc-Linker'])

        assert (excinfo.value.service, excinfo.value.status) == ('Codeforces', None)
        assert excinfo.value.__cause__ is error

    async def test_a_failure_when_asking_handle_by_handle_fails_the_whole_call(
        self, user_info: FakeUserInfo
    ) -> None:
        user_info.fail_next(
            cf.HandleNotFoundError('handles: User with handle x not found', 'x'),
            cf.ClientError(),
        )

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING):
            await fetch_users(['kcpc_Example', 'Kcpc-Linker'])
        assert user_info.requests == [
            ['kcpc_Example', 'Kcpc-Linker'],
            ['kcpc_Example'],
        ]


class TestFetchUser:
    async def test_the_user_whatever_the_case(self, user_info: FakeUserInfo) -> None:
        user = await fetch_user('KCPC_example')

        assert user is not None and user.handle == 'kcpc_Example'
        assert user_info.requests == [['KCPC_example']]

    async def test_none_for_an_unknown_handle(self, user_info: FakeUserInfo) -> None:
        assert await fetch_user('kcpc_nobody') is None
        assert user_info.requests == [['kcpc_nobody']]

    async def test_none_unasked_for_what_cannot_be_a_handle(
        self, user_info: FakeUserInfo
    ) -> None:
        assert await fetch_user('kcpc_Example;Kcpc-Linker') is None
        assert user_info.requests == []

    async def test_failures_are_external_service_errors(
        self, user_info: FakeUserInfo
    ) -> None:
        user_info.fail_next(cf.ClientError())

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING):
            await fetch_user('kcpc_Example')


def cf_problem(
    contest_id: int | None,
    index: str,
    name: str,
    *,
    rating: int | None = 1500,
    tags: list[str] | None = None,
) -> cf.Problem:
    """A problem as TLE's problemset.problems and user.status give it."""
    return cf.Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=None,
        rating=rating,
        tags=['implementation'] if tags is None else tags,
    )


def solved_count(
    contest_id: int | None, index: str, count: int
) -> cf.ProblemStatistics:
    """A problem's statistics as TLE's problemset.problems gives them."""
    return cf.ProblemStatistics(contestId=contest_id, index=index, solvedCount=count)


def finished(contest_id: int, name: str | None = None) -> cf.Contest:
    """A finished contest as TLE's contest.list gives it."""
    return cf.Contest(
        id=contest_id,
        name=f'Codeforces Round {contest_id} (Div. 2)' if name is None else name,
        startTimeSeconds=1_620_000_000,
        durationSeconds=7200,
        type='CF',
        phase='FINISHED',
        preparedBy=None,
    )


def problem_ids(catalog: list[CodeforcesProblem]) -> list[str]:
    return [problem.problem_id for problem in catalog]


@pytest.fixture
def codeforces_common() -> ModuleType:
    """TLE's module that KCPC's rule for standard rounds was copied from.

    KCPC may not import it, but a test may, where TLE's own dependencies,
    numpy among them, are installed; elsewhere the test is skipped.
    """
    module: ModuleType = pytest.importorskip('tle.util.codeforces_common')
    return module


def one_problem_in(contest: cf.Contest, tags: list[str]) -> CodeforcesProblem:
    """The catalog's problem for a problem of ``contest`` with ``tags``."""
    [problem] = problem_catalog(
        [cf_problem(contest.id, 'A', 'Example', tags=tags)], [], [contest]
    )
    return problem


STANDARD_NAMES = [
    'Codeforces Round 1000 (Div. 2)',
    'Educational Codeforces Round 170 (Rated for Div. 2)',
    'Codeforces Round 2150 (Div. 1 + Div. 2)',
    'Codeforces Global Round 27',
    'Example University Programming Contest 2026 (Div. 2, Online Mirror)',
]
NONSTANDARD_NAMES = [
    'April Fools Day Contest 2026',
    'VK Cup 2019 Wild-card Round 1',
    'Codeforces Round 999 (Unrated, Div. 2)',
    'Kotlin Heroes: Episode 12',
    'Microsoft Q# Coding Contest - Summer 2026',
    'Codeforces Testing Round 20',
    'ABBYY Cup 3.0 - Finals',
    'Example Onsite Finals (Online Mirror)',
    '2026-2027 ICPC, Example Regional Contest (Online Mirror)',
]


class TestProblemCatalog:
    def test_a_problem_becomes_a_codeforces_problem(self) -> None:
        [problem] = problem_catalog(
            [cf_problem(1520, 'D', 'Same Differences', rating=1200, tags=['math'])],
            [solved_count(1520, 'D', 41_234)],
            [finished(1520)],
        )

        assert problem == CodeforcesProblem(
            contest_id=1520,
            index='D',
            name='Same Differences',
            rating=1200,
            tags=('math',),
            solved_count=41_234,
            standard=True,
        )
        assert problem.problem_id == '1520D'
        assert problem.url == 'https://codeforces.com/contest/1520/problem/D'
        assert problem.contest_url == 'https://codeforces.com/contest/1520'

    @pytest.mark.parametrize('name', STANDARD_NAMES)
    def test_problems_of_standard_rounds_are_standard(self, name: str) -> None:
        assert one_problem_in(finished(1000, name), ['math']).standard

    @pytest.mark.parametrize('name', NONSTANDARD_NAMES)
    def test_problems_of_other_rounds_are_not(self, name: str) -> None:
        assert not one_problem_in(finished(1000, name), ['math']).standard

    @pytest.mark.parametrize('word', NONSTANDARD_CONTEST_WORDS)
    def test_a_contest_named_with_a_nonstandard_word_isnt_standard(
        self, word: str
    ) -> None:
        # In any case: the name is lowercased first.
        contest = finished(1000, f'Codeforces {word.upper()} Round 1000')
        assert not one_problem_in(contest, ['math']).standard

    @pytest.mark.parametrize('name', STANDARD_NAMES + NONSTANDARD_NAMES)
    def test_contests_are_judged_as_tle_judges_them(
        self, codeforces_common: ModuleType, name: str
    ) -> None:
        contest = finished(1000, name)
        standard = one_problem_in(contest, ['math']).standard
        assert standard is not codeforces_common.is_nonstandard_contest(contest)

    def test_the_words_are_tles(self, codeforces_common: ModuleType) -> None:
        assert NONSTANDARD_CONTEST_WORDS == tuple(
            codeforces_common._NONSTANDARD_CONTEST_INDICATORS
        )

    def test_a_problem_with_the_special_tag_isnt_standard(self) -> None:
        assert SPECIAL_TAG == '*special'
        special = one_problem_in(finished(1000), ['*special', 'implementation'])
        assert not special.standard
        assert special.tags == ('*special', 'implementation')

    def test_a_problem_of_an_unknown_contest_isnt_standard(self) -> None:
        catalog = problem_catalog(
            [cf_problem(1001, 'A', 'Known'), cf_problem(1002, 'A', 'Unknown')],
            [],
            [finished(1001)],
        )
        assert [(p.problem_id, p.standard) for p in catalog] == [
            ('1001A', True),
            ('1002A', False),
        ]

    def test_solved_counts_go_by_contest_and_index(self) -> None:
        catalog = problem_catalog(
            [
                cf_problem(1520, 'D', 'Same Differences'),
                cf_problem(1520, 'C', 'Not Adjacent Matrix'),
                cf_problem(1500, 'A', 'Going Home'),
            ],
            # In another order, with one missing and others for no problem.
            [
                solved_count(1500, 'A', 5_000),
                solved_count(1520, 'D', 41_234),
                solved_count(1501, 'C', 9_999),
                solved_count(None, 'C', 7),
            ],
            [finished(1500), finished(1520)],
        )
        assert [(p.problem_id, p.solved_count) for p in catalog] == [
            ('1520D', 41_234),
            ('1520C', None),
            ('1500A', 5_000),
        ]

    def test_unrated_problems_are_kept_without_a_rating(self) -> None:
        [problem] = problem_catalog(
            [cf_problem(2269, 'C', 'New', rating=None)], [], [finished(2269)]
        )
        assert (problem.rating, problem.standard) == (None, True)

    def test_problems_without_a_contest_are_left_out(self) -> None:
        catalog = problem_catalog(
            [cf_problem(None, 'A', 'From acmsguru'), cf_problem(1000, 'A', 'Kept')],
            [solved_count(None, 'A', 10)],
            [finished(1000)],
        )
        assert problem_ids(catalog) == ['1000A']

    def test_keeps_the_order_given_and_reads_any_iterables(self) -> None:
        given = [
            cf_problem(2269, 'B', 'B'),
            cf_problem(2269, 'A', 'A'),
            cf_problem(1, 'A', 'First'),
        ]
        catalog = problem_catalog(
            (problem for problem in given),
            (solved_count(p.contestId, p.index, 1) for p in given),
            (finished(contest_id) for contest_id in (1, 2269)),
        )
        assert problem_ids(catalog) == ['2269B', '2269A', '1A']
        assert [problem.solved_count for problem in catalog] == [1, 1, 1]
        assert all(problem.standard for problem in catalog)

    def test_problems_can_go_in_sets(self) -> None:
        tags = ['dp', 'greedy']
        [problem] = problem_catalog(
            [cf_problem(1000, 'A', 'Example', tags=tags)], [], [finished(1000)]
        )
        tags.append('math')  # TLE's list stays TLE's

        assert problem.tags == ('dp', 'greedy')
        assert {problem, problem} == {problem}

    def test_a_problem_whose_tags_codeforces_left_out_has_none(self) -> None:
        # TLE makes a field that Codeforces leaves out None.
        untagged = cf.make_from_dict(
            cf.Problem,
            {'contestId': 1000, 'index': 'A', 'name': 'Example', 'type': 'PROGRAMMING'},
        )
        [problem] = problem_catalog([untagged], [], [finished(1000)])
        assert (problem.tags, problem.rating, problem.standard) == ((), None, True)


class FakeProblemset:
    """Stands in for TLE's ``cf.problemset.problems`` and ``cf.contest.to_list``.

    They answer with ``problems``, ``statistics`` and ``contests``, and record
    each call with its arguments. ``fail`` makes a call raise instead.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.problems: list[cf.Problem] = []
        self.statistics: list[cf.ProblemStatistics] = []
        self.contests: list[cf.Contest] = []
        self._failures: dict[str, BaseException] = {}

    def fail(self, call: str, error: BaseException) -> None:
        self._failures[call] = error

    async def problemset_problems(
        self, **kwargs: object
    ) -> tuple[list[cf.Problem], list[cf.ProblemStatistics]]:
        self._call('problemset.problems', kwargs)
        return self.problems, self.statistics

    async def contest_list(self, **kwargs: object) -> list[cf.Contest]:
        self._call('contest.list', kwargs)
        return self.contests

    def _call(self, name: str, kwargs: dict[str, object]) -> None:
        self.calls.append((name, kwargs))
        if name in self._failures:
            raise self._failures[name]


@pytest.fixture
def problemset(monkeypatch: pytest.MonkeyPatch) -> FakeProblemset:
    fake = FakeProblemset()
    monkeypatch.setattr(cf.problemset, 'problems', fake.problemset_problems)
    monkeypatch.setattr(cf.contest, 'to_list', fake.contest_list)
    return fake


API_FAILURES = [
    cf.ClientError(),
    cf.CallLimitExceededError('Call limit exceeded'),
    cf.TrueApiError('HTTP Error 503, Service Unavailable'),
    cf.CodeforcesApiError(),
    asyncio.TimeoutError(),
]
API_FAILURE_IDS = ['client', 'call-limit', 'api', 'not-json', 'timeout']


class TestFetchProblems:
    async def test_asks_for_the_problemset_then_for_the_contests_without_gyms(
        self, problemset: FakeProblemset
    ) -> None:
        problemset.problems = [
            cf_problem(1520, 'D', 'Same Differences', rating=1200),
            cf_problem(1500, 'A', 'Going Home', rating=1800),
        ]
        problemset.statistics = [
            solved_count(1520, 'D', 41_234),
            solved_count(1500, 'A', 5_000),
        ]
        problemset.contests = [finished(1520), finished(1500, 'Codeforces Unrated')]

        catalog = await fetch_problems()

        assert problemset.calls == [
            ('problemset.problems', {}),
            ('contest.list', {'gym': False}),
        ]
        assert catalog == [
            CodeforcesProblem(
                1520, 'D', 'Same Differences', 1200, ('implementation',), 41_234, True
            ),
            CodeforcesProblem(
                1500, 'A', 'Going Home', 1800, ('implementation',), 5_000, False
            ),
        ]

    @pytest.mark.parametrize('call', ['problemset.problems', 'contest.list'])
    @pytest.mark.parametrize('error', API_FAILURES, ids=API_FAILURE_IDS)
    async def test_failures_are_external_service_errors(
        self, problemset: FakeProblemset, call: str, error: Exception
    ) -> None:
        problemset.fail(call, error)

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING) as excinfo:
            await fetch_problems()

        assert (excinfo.value.service, excinfo.value.status) == ('Codeforces', None)
        assert excinfo.value.__cause__ is error


def submission(
    problem: cf.Problem, verdict: str | None = 'OK', *, submission_id: int = 1
) -> cf.Submission:
    """A submission of kcpc_Example's, as TLE's user.status gives it."""
    return cf.Submission(
        id=submission_id,
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


SAME_DIFFERENCES = cf_problem(1520, 'D', 'Same Differences', rating=1200)
# Div. 2's C, which Div. 1 set as its A: the problemset lists only 1500A.
GOING_HOME_DIV2 = cf_problem(1501, 'C', 'Going Home', rating=1800)


class FakeUserStatus:
    """Stands in for TLE's ``cf.user.status``, answering as Codeforces does.

    It knows its users' submissions by handle, whatever the case, and raises
    ``cf.HandleNotFoundError`` for any other handle. It records the handle of
    every request, and ``fail_next`` makes the next requests raise instead.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self._submissions: dict[str, list[cf.Submission]] = {}
        self._failures: deque[BaseException] = deque()

    def add(self, handle: str, *submissions: cf.Submission) -> None:
        self._submissions[handle.lower()] = list(submissions)

    def fail_next(self, *errors: BaseException) -> None:
        self._failures.extend(errors)

    async def status(
        self, *, handle: str, from_: int | None = None, count: int | None = None
    ) -> list[cf.Submission]:
        self.requests.append(handle)
        if self._failures:
            raise self._failures.popleft()
        if handle.lower() not in self._submissions:
            comment = f'handle: User with handle {handle} not found'
            raise cf.HandleNotFoundError(comment, handle)
        return self._submissions[handle.lower()]


@pytest.fixture
def user_status(monkeypatch: pytest.MonkeyPatch) -> FakeUserStatus:
    fake = FakeUserStatus()
    monkeypatch.setattr(cf.user, 'status', fake.status)
    return fake


class TestFetchSolved:
    async def test_the_ids_and_names_of_accepted_submissions(
        self, user_status: FakeUserStatus
    ) -> None:
        tried = cf_problem(1000, 'A', 'Tried')
        user_status.add(
            'kcpc_Example',
            submission(SAME_DIFFERENCES, 'WRONG_ANSWER', submission_id=1),
            submission(SAME_DIFFERENCES, 'OK', submission_id=2),
            submission(SAME_DIFFERENCES, 'OK', submission_id=3),
            submission(GOING_HOME_DIV2, 'OK', submission_id=4),
            submission(tried, 'WRONG_ANSWER', submission_id=5),
        )

        solved = await fetch_solved('kcpc_Example')

        assert solved == SolvedProblems(
            ids=frozenset({'1520D', '1501C'}),
            named=frozenset({('1520D', 'Same Differences'), ('1501C', 'Going Home')}),
        )
        assert user_status.requests == ['kcpc_Example']

    async def test_a_problem_solved_in_the_other_division_is_found_by_name(
        self, user_status: FakeUserStatus
    ) -> None:
        user_status.add('kcpc_Example', submission(GOING_HOME_DIV2))
        [listed] = problem_catalog(
            [cf_problem(1500, 'A', 'Going Home')], [], [finished(1500)]
        )

        solved = await fetch_solved('kcpc_Example')

        assert solved is not None
        assert listed.problem_id not in solved.ids
        assert ('1501C', listed.name) in solved.named

    @pytest.mark.parametrize(
        'verdict',
        [
            'WRONG_ANSWER',
            'TIME_LIMIT_EXCEEDED',
            'COMPILATION_ERROR',
            'PARTIAL',
            'TESTING',
            'ok',
            None,
        ],
    )
    async def test_only_the_verdict_ok_counts(
        self, user_status: FakeUserStatus, verdict: str | None
    ) -> None:
        user_status.add('kcpc_Example', submission(SAME_DIFFERENCES, verdict))
        assert await fetch_solved('kcpc_Example') == SolvedProblems(
            frozenset(), frozenset()
        )

    async def test_a_problem_without_a_contest_counts_by_its_name(
        self, user_status: FakeUserStatus
    ) -> None:
        user_status.add('kcpc_Example', submission(cf_problem(None, 'A', 'Guru')))
        assert await fetch_solved('kcpc_Example') == SolvedProblems(
            frozenset(), frozenset({('', 'Guru')})
        )

    async def test_the_handle_is_asked_about_as_given(
        self, user_status: FakeUserStatus
    ) -> None:
        user_status.add('kcpc_Example', submission(SAME_DIFFERENCES))

        solved = await fetch_solved('KCPC_example')

        assert solved is not None and solved.ids == frozenset({'1520D'})
        assert user_status.requests == ['KCPC_example']

    async def test_none_for_a_handle_codeforces_has_no_user_with(
        self, user_status: FakeUserStatus
    ) -> None:
        assert await fetch_solved('kcpc_nobody') is None
        assert user_status.requests == ['kcpc_nobody']

    async def test_none_for_a_handle_codeforces_calls_invalid(
        self, user_status: FakeUserStatus
    ) -> None:
        comment = 'handle: Field should contain only Latin letters, digits'
        user_status.fail_next(cf.HandleInvalidError(comment, 'kcpc_Example'))

        assert await fetch_solved('kcpc_Example') is None

    @pytest.mark.parametrize(
        'handle',
        ['', 'kcpc Example', 'kcpc;Example', 'kcpc!Example', 'naïve'],
        ids=['empty', 'space', 'separator', 'punctuation', 'accent'],
    )
    async def test_none_unasked_for_what_cannot_be_a_handle(
        self, user_status: FakeUserStatus, handle: str
    ) -> None:
        assert await fetch_solved(handle) is None
        assert user_status.requests == []

    async def test_unknown_handles_are_logged_at_debug(
        self, user_status: FakeUserStatus, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        await fetch_solved('kcpc_nobody')
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG]

    @pytest.mark.parametrize('error', API_FAILURES, ids=API_FAILURE_IDS)
    async def test_other_failures_are_external_service_errors(
        self, user_status: FakeUserStatus, error: Exception
    ) -> None:
        user_status.fail_next(error)

        with pytest.raises(ExternalServiceError, match=NOT_RESPONDING) as excinfo:
            await fetch_solved('kcpc_Example')

        assert (excinfo.value.service, excinfo.value.status) == ('Codeforces', None)
        assert excinfo.value.__cause__ is error


class TestRatingChanges:
    def test_tles_rating_changes_are_read_in_order(self) -> None:
        changes = [
            cf.RatingChange(2051, 'Codeforces Round 1050', 'tourist', 1, 0, 3700, 3750),
            cf.RatingChange(2051, 'Codeforces Round 1050', 'kcpc_New', 900, 0, 0, 380),
        ]

        read = rating_changes(changes)

        assert read == [
            CodeforcesRatingChange('tourist', 1, 3700, 3750),
            CodeforcesRatingChange('kcpc_New', 900, 0, 380),
        ]
        assert [change.first_rated for change in read] == [False, True]
        assert read[0].url == 'https://codeforces.com/profile/tourist'

    @pytest.mark.parametrize(
        ('rating', 'name'),
        [
            (0, 'Newbie'),
            (1199, 'Newbie'),
            (1200, 'Pupil'),
            (1899, 'Expert'),
            (1900, 'Candidate Master'),
            (2400, 'Grandmaster'),
            (3000, 'Legendary Grandmaster'),
        ],
    )
    def test_a_ratings_rank_is_named_as_codeforces_names_it(
        self, rating: int, name: str
    ) -> None:
        assert rank_name(rating) == name
