"""Tests for tle.kcpc.features.problems.weekly: the weekly problem.

The service runs on a migrated in-memory kcpc.db with the real delivery ledger,
and posts through FakePublisher. Codeforces is faked in place of TLE's
``cf.problemset.problems`` and ``cf.contest.to_list``, AtCoder Problems by
FakeAtCoderProblems, and AtCoder's editorial pages by FakeEditorials. A seeded
``random.Random`` makes picks repeatable; ``InOrder`` keeps candidates in the
order the sites list them, for tests that say which comes first.

The clock starts on Thursday 2026-10-01 at 12:00 UTC. Friday noon in London is
11:00 UTC until the clocks go back on 2026-10-25, and 12:00 UTC after.
"""

import asyncio
import logging
import random
from collections import deque
from collections.abc import AsyncIterator, Callable, MutableSequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from tests.kcpc.conftest import CLOCK_START
from tests.kcpc.fakes import FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.schedule import Weekly
from tle.kcpc.core.scheduler import JobStatus, ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo, default_registry
from tle.kcpc.core.timeutil import from_epoch, to_epoch, zone
from tle.kcpc.features.problems.catalog import ProblemCatalog
from tle.kcpc.features.problems.editorials import (
    EditorialFinder,
    SolutionLink,
    solution_links,
)
from tle.kcpc.features.problems.repo import (
    ProblemAlreadyUsed,
    QueuedProblem,
    WeeklyProblem,
    WeeklyRepo,
)
from tle.kcpc.features.problems.rotation import DEFAULT_ROTATION, RotationEntry
from tle.kcpc.features.problems.settings import SPEC, WEEKLY
from tle.kcpc.features.problems.weekly import (
    FOOTER,
    FRIDAY,
    POST_TIME,
    WEEKLY_JOB,
    InGuild,
    NoWeeklyProblem,
    PostResult,
    WeeklyPlan,
    WeeklyReport,
    WeeklyService,
    problem_key,
    solution_key,
)
from tle.kcpc.platforms.atcoder.editorials import (
    AtCoderEditorial,
    AtCoderEditorials,
    AtCoderEditorialsClient,
)
from tle.kcpc.platforms.atcoder.problems import AtCoderProblem, AtCoderProblemsClient
from tle.kcpc.platforms.difficulty import Band
from tle.util import codeforces_api as cf

LOGGER = 'tle.kcpc.features.problems.weekly'
CLUB = zone('Europe/London')
SCHEDULE = Weekly(FRIDAY, POST_TIME, CLUB)
NOW = CLOCK_START
# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
ADMIN = 1_300_000_000_000_000_001
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
STOP_TIMEOUT = 10  # real seconds
BLOG = 'https://codeforces.com/blog/entry/90342'


def friday(day: int, month: int = 10) -> datetime:
    """The slot of Friday ``day`` (of October 2026 unless said): noon in London."""
    return datetime(2026, month, day, 12, 0, tzinfo=CLUB).astimezone(UTC)


def stamp(moment: datetime, style: str) -> str:
    return f'<t:{to_epoch(moment)}:{style}>'


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


# Codeforces' problems, one band at a time: easy 4A (too old to pick) and
# 1520A; medium 1520D and 1520E; hard 1520F1; expert 1520G.
CODEFORCES_PROBLEMS = [
    cf_problem(1520, 'G', 'To Go Or Not To Go?', 2200, ['dfs and similar', 'graphs']),
    cf_problem(
        1520,
        'F1',
        'Guess the K-th Zero (Easy version)',
        1600,
        ['binary search', 'interactive'],
    ),
    cf_problem(1520, 'E', 'Arranging The Sheep', 1400, ['greedy', 'math']),
    cf_problem(1520, 'D', 'Same Differences', 1200, ['data structures', 'math']),
    cf_problem(1520, 'A', 'Do Not Be Distracted!', 800, ['implementation']),
    cf_problem(4, 'A', 'Watermelon', 800, ['brute force', 'math']),
]
CONTESTS = [
    finished(1520, 'Codeforces Round 719 (Div. 3)'),
    finished(4, 'Codeforces Beta Round 4 (Div. 2 Only)'),
]

# AtCoder's problems, with their ratings on Codeforces' scale: easy abc300_a
# (713), medium abc300_d (1467), hard ones from 1694 to 1808, expert agc060_a
# (2224), and a heuristic contest's, which is never picked.
ATCODER_PROBLEMS = {
    problem.problem_id: problem
    for problem in (
        AtCoderProblem('abc300_a', 'abc300', 'A', 'N-choice question', 3),
        AtCoderProblem('abc300_d', 'abc300', 'D', 'AABCC', 1000),
        AtCoderProblem('arc150_a', 'arc150', 'A', 'Continuous 1', 1400),
        AtCoderProblem('arc151_a', 'arc151', 'A', 'Equal Hamming Distances', 1300),
        AtCoderProblem('agc061_a', 'agc061', 'A', 'Long Shuffle', 1450),
        AtCoderProblem('arc152_a', 'arc152', 'A', 'Seat Occupation', 1350),
        AtCoderProblem('arc153_a', 'arc153', 'A', 'AABCDDEFE', 1420),
        AtCoderProblem('arc154_a', 'arc154', 'A', 'Swap Digit', 1380),
        AtCoderProblem('agc060_a', 'agc060', 'A', 'No Majority', 2000),
        AtCoderProblem('ahc001_a', 'ahc001', 'A', 'AtCoder Ad', 1800),
    )
}
HARD_ATCODER = ['arc150_a', 'arc151_a', 'agc061_a', 'arc152_a', 'arc153_a', 'arc154_a']


def editorial_url(contest_id: str, number: int) -> str:
    return f'https://atcoder.jp/contests/{contest_id}/editorial/{number}'


def official(url: str, *, english: bool = True) -> AtCoderEditorial:
    return AtCoderEditorial(url, 'Editorial', True, english, False, 'task')


def by_a_member(url: str) -> AtCoderEditorial:
    return AtCoderEditorial(url, 'Editorial', False, True, False, 'task')


def page(problem_id: str, *editorials: AtCoderEditorial) -> AtCoderEditorials:
    contest_id = problem_id.rsplit('_', 1)[0]
    return AtCoderEditorials(contest_id, problem_id, editorials)


def all_editorials(contest_id: str, problem_id: str) -> str:
    return (
        f'https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}/editorial?lang=en'
    )


class FakeProblemset:
    """Stands in for TLE's ``cf.problemset.problems`` and ``cf.contest.to_list``."""

    async def problemset_problems(
        self, **kwargs: object
    ) -> tuple[list[cf.Problem], list[cf.ProblemStatistics]]:
        statistics = [
            cf.ProblemStatistics(problem.contestId, problem.index, 10_000)
            for problem in CODEFORCES_PROBLEMS
        ]
        return list(CODEFORCES_PROBLEMS), statistics

    async def contest_list(self, **kwargs: object) -> list[cf.Contest]:
        return list(CONTESTS)


class FakeAtCoderProblems:
    """Stands in for ``AtCoderProblemsClient``'s problem set."""

    async def fetch_problem_set(self) -> dict[str, AtCoderProblem]:
        return dict(ATCODER_PROBLEMS)


class AtCoderProblemsDown:
    """An ``AtCoderProblemsClient`` whose problem set can't be fetched."""

    async def fetch_problem_set(self) -> dict[str, AtCoderProblem]:
        raise ExternalServiceError(
            'AtCoder Problems', 'AtCoder Problems is not responding right now.'
        )


class FakeEditorials:
    """Stands in for ``AtCoderEditorialsClient``: ``pages`` by task ID.

    A task without a page is a 404 (None). ``requests`` lists each task asked
    about, and ``errors`` fail the next requests. A request whose number (from
    0) is in ``gates`` waits for that event first.
    """

    def __init__(self) -> None:
        self.pages: dict[str, AtCoderEditorials] = {}
        self.requests: list[str] = []
        self.errors: deque[Exception] = deque()
        self.gates: dict[int, asyncio.Event] = {}

    def add(self, *pages: AtCoderEditorials) -> None:
        for found in pages:
            self.pages[found.task_id] = found

    async def fetch(self, contest_id: str, task_id: str) -> AtCoderEditorials | None:
        gate = self.gates.get(len(self.requests))
        self.requests.append(task_id)
        if gate is not None:
            await gate.wait()
        if self.errors:
            raise self.errors.popleft()
        found = self.pages.get(task_id)
        if found is not None:
            assert found.contest_id == contest_id
        return found


class InOrder(random.Random):
    """A ``random.Random`` that leaves candidates in the order listed."""

    def shuffle(self, x: MutableSequence[Any], *args: Any) -> None:
        pass


class Shuffled(InOrder):
    """An ``InOrder`` that lists the IDs of what it was asked to shuffle."""

    def __init__(self) -> None:
        super().__init__()
        self.shuffled: list[list[str]] = []

    def shuffle(self, x: MutableSequence[Any], *args: Any) -> None:
        self.shuffled.append([problem.problem_id for problem in x])


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the weekly settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture(autouse=True)
def problemset(monkeypatch: pytest.MonkeyPatch) -> FakeProblemset:
    fake = FakeProblemset()
    monkeypatch.setattr(cf.problemset, 'problems', fake.problemset_problems)
    monkeypatch.setattr(cf.contest, 'to_list', fake.contest_list)
    return fake


@pytest.fixture
def editorials() -> FakeEditorials:
    return FakeEditorials()


@pytest.fixture
async def catalog(clock: FakeClock) -> ProblemCatalog:
    """Both platforms' lists, loaded."""
    loaded = ProblemCatalog(cast(AtCoderProblemsClient, FakeAtCoderProblems()), clock)
    assert await loaded.refresh() == {}
    return loaded


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
def repo(db: Database) -> WeeklyRepo:
    return WeeklyRepo(db)


@pytest.fixture
def make_service(
    repo: WeeklyRepo,
    catalog: ProblemCatalog,
    editorials: FakeEditorials,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
    clock: FakeClock,
) -> Callable[..., WeeklyService]:
    def make(
        *,
        rng: random.Random | None = None,
        problems: ProblemCatalog | None = None,
        schedule: Weekly = SCHEDULE,
        in_guild: InGuild | None = None,
    ) -> WeeklyService:
        # Only a test of who the bot is with passes in_guild.
        options = {} if in_guild is None else {'in_guild': in_guild}
        return WeeklyService(
            repo,
            catalog if problems is None else problems,
            EditorialFinder(cast(AtCoderEditorialsClient, editorials)),
            guild_settings,
            ledger,
            publisher,
            clock,
            schedule,
            rng=random.Random(2026) if rng is None else rng,
            **options,
        )

    return make


@pytest.fixture
def service(make_service: Callable[..., WeeklyService]) -> WeeklyService:
    return make_service()


@pytest.fixture(autouse=True)
def weekly_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)


async def set_up(
    guild_settings: GuildSettingsRepo,
    guild_id: int = GUILD,
    *rotation: str,
    enabled: bool = True,
    channel_id: int | None = CHANNEL,
) -> None:
    """Turn the weekly problem on in the guild, with ``rotation`` if given."""
    await guild_settings.update(
        guild_id, WEEKLY, enabled=enabled, channel_id=channel_id, rotation=rotation
    )


def keys(publisher: FakePublisher) -> list[str]:
    return [key for post in publisher.posts for key in post.keys]


def logged(caplog: pytest.LogCaptureFixture, level: int = logging.INFO) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == level
    ]


def queued(
    problem_id: str = '1520G', *, source: str = 'codeforces', **changes: Any
) -> QueuedProblem:
    """A problem that ADMIN queued for GUILD."""
    if source == 'codeforces':
        contest_id, index = problem_id[:4], problem_id[4:]
        item = QueuedProblem(
            guild_id=GUILD,
            source=source,
            problem_id=problem_id,
            contest_id=contest_id,
            index=index,
            name=f'Problem {problem_id}',
            url=f'https://codeforces.com/contest/{contest_id}/problem/{index}',
            difficulty=2200,
            band='expert',
            solution_url=None,
            queued_by=ADMIN,
            queued_at=NOW,
        )
    else:
        contest_id = problem_id.rsplit('_', 1)[0]
        item = QueuedProblem(
            guild_id=GUILD,
            source=source,
            problem_id=problem_id,
            contest_id=contest_id,
            index=problem_id.rsplit('_', 1)[1].upper(),
            name=f'Task {problem_id}',
            url=f'https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}',
            difficulty=1467,
            band='medium',
            solution_url=None,
            queued_by=ADMIN,
            queued_at=NOW,
        )
    return replace(item, **changes)


async def row_of(repo: WeeklyRepo, slot: datetime) -> WeeklyProblem:
    row = await repo.get(GUILD, slot)
    assert row is not None
    return row


async def until(condition: Callable[[], bool]) -> None:
    """Wait (up to 10 s of real time) until ``condition()`` holds."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError('The condition never held')


async def settle() -> None:
    """Give the other tasks a tenth of a second of real time to go on."""
    for _ in range(20):
        await asyncio.sleep(0.005)


async def posted_problem(
    publisher: FakePublisher, guild_id: int, week: str
) -> PublishResult:
    """Record the guild's problem of ``week`` as posted, as a run would."""
    delivery = Delivery(problem_key(guild_id, week), guild_id, WEEKLY)
    return await publisher.publish([delivery], OutgoingMessage(title='Earlier'))


class TestRunGuild:
    async def test_post_now_posts_the_weeks_problem_once(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        slot = SCHEDULE.prev_at_or_before(clock.now())
        assert slot == friday(25, 9)

        report = await service.run_guild(GUILD, slot)

        row = await row_of(repo, slot)
        assert report == WeeklyReport(
            configured=True,
            solutions=(),
            problem=PostResult(row, PublishOutcome.SENT, None),
        )
        assert not report.retry_later
        assert keys(publisher) == ['weekly:1100000000000000001:problem:2026-09-25']
        assert (row.week, row.source, row.band, row.selection) == (
            '2026-09-25',
            'codeforces',
            'medium',
            'auto',
        )
        assert row.problem_id in ('1520D', '1520E')
        assert row.date_selected == NOW
        assert await service.problem_posted(row)

        again = await service.run_guild(GUILD, slot)

        assert again.problem == PostResult(row, PublishOutcome.ALREADY_HANDLED, None)
        assert len(publisher.posts) == 1
        assert await repo.history(GUILD, at_or_before=NOW) == [row]

    async def test_the_problem_post_says_what_and_when_its_solution_comes(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')

        await service.run_guild(GUILD, friday(2))

        [post] = publisher.posts
        assert post.deliveries == (
            Delivery(
                key='weekly:1100000000000000001:problem:2026-10-02',
                guild_id=GUILD,
                feature=WEEKLY,
                subject='weekly',
                subject_id='2026-10-02',
                kind='problem',
                occurrence_start=friday(2),
                expires_at=friday(9),
            ),
        )
        assert post.message == OutgoingMessage(
            title='Weekly problem: 1520F1 - Guess the K-th Zero (Easy version)',
            description=(
                '**Platform:** Codeforces\n'
                '**Difficulty:** 1600 (hard)\n'
                f'**Solution:** {stamp(friday(9), "F")} ({stamp(friday(9), "R")})'
            ),
            url='https://codeforces.com/contest/1520/problem/F1',
            footer=FOOTER,
            mention_role=True,
        )
        row = await row_of(repo, friday(2))
        assert (row.difficulty, row.band, row.topic, row.solution_url) == (
            1600,
            'hard',
            None,
            None,
        )

    async def test_a_rotation_topic_is_in_the_row_and_the_post(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:expert:graphs')

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.problem_id, row.topic) == ('1520G', 'graphs')
        description = publisher.posts[0].message.description
        assert description is not None
        assert description.splitlines()[:3] == [
            '**Platform:** Codeforces',
            '**Difficulty:** 2200 (expert)',
            '**Topic:** graphs',
        ]

    async def test_an_atcoder_problem_is_picked_with_its_official_editorial(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        english = editorial_url('abc300', 6198)
        editorials.add(
            page(
                'abc300_d',
                by_a_member('https://example.com/blog/abc300-d'),
                official(editorial_url('abc300', 6100), english=False),
                official(english),
            )
        )

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.source, row.problem_id, row.contest_id, row.index) == (
            'atcoder',
            'abc300_d',
            'abc300',
            'D',
        )
        assert (row.name, row.url) == (
            'AABCC',
            'https://atcoder.jp/contests/abc300/tasks/abc300_d',
        )
        assert (row.difficulty, row.band, row.solution_url) == (1467, 'medium', english)
        assert row.solution_set_by is None
        assert publisher.posts[0].message == OutgoingMessage(
            title='Weekly problem: ABC300 D - AABCC',
            description=(
                '**Platform:** AtCoder\n'
                '**Difficulty:** 1000 on AtCoder (about 1467 on Codeforces, medium)\n'
                f'**Solution:** {stamp(friday(9), "F")} ({stamp(friday(9), "R")})'
            ),
            url='https://atcoder.jp/contests/abc300/tasks/abc300_d',
            footer=FOOTER,
            mention_role=True,
        )

    async def test_the_next_slot_posts_the_solution_then_the_new_problem(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(2))
        first = await row_of(repo, friday(2))
        await clock.advance_to(friday(9))

        report = await service.run_guild(GUILD, friday(9))

        second = await row_of(repo, friday(9))
        assert {first.problem_id, second.problem_id} == {'1520D', '1520E'}
        assert keys(publisher) == [
            'weekly:1100000000000000001:problem:2026-10-02',
            'weekly:1100000000000000001:solution:2026-10-02',
            'weekly:1100000000000000001:problem:2026-10-09',
        ]
        assert report == WeeklyReport(
            True,
            (PostResult(first, PublishOutcome.SENT, None),),
            PostResult(second, PublishOutcome.SENT, None),
        )
        marked = await row_of(repo, friday(2))
        assert (marked.solution_posted, marked.solution_posted_at) == (True, friday(9))
        [solution] = publisher.posts[1].deliveries
        assert solution == Delivery(
            key='weekly:1100000000000000001:solution:2026-10-02',
            guild_id=GUILD,
            feature=WEEKLY,
            subject='weekly',
            subject_id='2026-10-02',
            kind='solution',
            occurrence_start=friday(2),
            expires_at=friday(16),
        )

        await service.run_guild(GUILD, friday(9))
        assert len(publisher.posts) == 3

    async def test_a_codeforces_solution_without_a_link_points_at_the_contest(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await service.run_guild(GUILD, friday(2))
        await set_up(guild_settings, GUILD, 'codeforces:expert:any')
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        assert publisher.posts[1].message == OutgoingMessage(
            title='Solution: 1520F1 - Guess the K-th Zero (Easy version)',
            description=(
                "Last week's problem: [1520F1 - Guess the K-th Zero (Easy version)]"
                '(https://codeforces.com/contest/1520/problem/F1)\n'
                'Codeforces lists the editorial under **Contest materials** on '
                '[the contest page](https://codeforces.com/contest/1520).'
            ),
            url='https://codeforces.com/contest/1520',
            footer=FOOTER,
            mention_role=False,
        )

    async def test_a_codeforces_solution_links_what_an_admin_set(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await service.run_guild(GUILD, friday(2))
        await repo.set_solution(GUILD, friday(2), BLOG, set_by=ADMIN)
        await set_up(guild_settings, GUILD, 'codeforces:expert:any')
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        message = publisher.posts[1].message
        assert (message.title, message.url) == (
            'Solution: 1520F1 - Guess the K-th Zero (Easy version)',
            BLOG,
        )
        assert message.description == (
            "Last week's problem: [1520F1 - Guess the K-th Zero (Easy version)]"
            '(https://codeforces.com/contest/1520/problem/F1)\n'
            f'[Editorial]({BLOG})'
        )

    async def test_links_and_their_text_are_kept_whole(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await repo.enqueue(
            queued(
                '1700A',
                name='Sum *of* [Two]_',
                solution_url='https://example.com/editorial_(1)',
            )
        )
        await repo.enqueue(
            queued('1701A', solution_url='https://example.com/' + 'x' * 600)
        )
        for day in (2, 9, 16):
            await clock.advance_to(friday(day))
            await service.run_guild(GUILD, friday(day))

        escaped = publisher.posts[1].message
        assert escaped.url == 'https://example.com/editorial_(1)'
        assert escaped.description == (
            "Last week's problem: [1700A - Sum \\*of\\* \\[Two\\]\\_]"
            '(https://codeforces.com/contest/1700/problem/A)\n'
            '[Editorial](https://example.com/editorial_%281%29)'
        )
        # Too long a link for the text: the title still has it.
        long_link = publisher.posts[3].message
        assert long_link.url == 'https://example.com/' + 'x' * 600
        assert long_link.description is not None
        assert long_link.description.endswith(')\nEditorial')

    async def test_a_link_too_long_for_replies_is_not_linked_in_posts_either(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        long_link = 'https://example.com/' + 'x' * 381  # 401 characters
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await repo.enqueue(queued('1700A', solution_url=long_link))
        await service.run_guild(GUILD, friday(2))
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        solution = publisher.posts[1].message
        assert solution.url == long_link  # the title still links it
        assert solution.description is not None
        assert solution.description.endswith(')\nEditorial')

    async def test_an_instant_that_isnt_a_slot_runs_the_slot_before_it(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')

        await service.run_guild(GUILD, friday(2) + 3 * HOUR + timedelta(microseconds=5))

        assert (await row_of(repo, friday(2))).slot == friday(2)
        assert keys(publisher) == ['weekly:1100000000000000001:problem:2026-10-02']

    @pytest.mark.parametrize(
        ('enabled', 'channel_id'), [(False, CHANNEL), (True, None), (False, None)]
    )
    async def test_a_server_not_set_up_gets_nothing_and_nothing_is_written(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        ledger: DeliveryLedger,
        enabled: bool,
        channel_id: int | None,
    ) -> None:
        await set_up(guild_settings, GUILD, enabled=enabled, channel_id=channel_id)

        report = await service.run_guild(GUILD, friday(2))

        assert report == WeeklyReport(configured=False, solutions=(), problem=None)
        assert await repo.get(GUILD, friday(2)) is None
        assert publisher.posts == []
        assert await ledger.status_counts(GUILD) == dict.fromkeys(DeliveryStatus, 0)

    async def test_runs_of_one_server_take_turns(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')

        reports = await asyncio.gather(
            service.run_guild(GUILD, friday(2)), service.run_guild(GUILD, friday(2))
        )

        outcomes = [report.problem.outcome for report in reports if report.problem]
        assert outcomes == [PublishOutcome.SENT, PublishOutcome.ALREADY_HANDLED]
        assert reports[0].problem is not None and reports[1].problem is not None
        assert reports[0].problem.row == reports[1].problem.row
        assert len(publisher.posts) == 1

    async def test_a_second_run_waits_while_the_first_picks(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        publisher: FakePublisher,
    ) -> None:
        service = make_service(rng=InOrder())
        await set_up(guild_settings, GUILD, 'atcoder:hard:any')
        editorials.add(page('arc150_a', official(editorial_url('arc150', 1))))
        editorials.gates[0] = asyncio.Event()
        first = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await until(lambda: editorials.requests == ['arc150_a'])

        second = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await settle()
        assert editorials.requests == ['arc150_a']  # the second hasn't picked
        editorials.gates[0].set()
        reports = await asyncio.gather(first, second)

        outcomes = [report.problem.outcome for report in reports if report.problem]
        assert outcomes == [PublishOutcome.SENT, PublishOutcome.ALREADY_HANDLED]
        assert len(publisher.posts) == 1

    async def test_a_week_keeps_its_problem_when_its_slot_moves_earlier(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        # The club's time zone was UTC, then London: Friday noon moves from
        # 12:00 to 11:00 UTC.
        in_utc = make_service(schedule=Weekly(FRIDAY, POST_TIME, UTC))
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        noon_in_utc = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
        await clock.advance_to(noon_in_utc)
        await in_utc.run_guild(GUILD, noon_in_utc)
        [first] = await repo.history(GUILD, at_or_before=noon_in_utc)
        await clock.advance(HOUR)

        report = await make_service().run_guild(GUILD, clock.now())

        assert report.problem == PostResult(first, PublishOutcome.ALREADY_HANDLED, None)
        assert await repo.history(GUILD, at_or_before=clock.now()) == [first]
        assert len(publisher.posts) == 1

    async def test_a_slot_that_moves_later_posts_no_solution_of_its_own_week(
        self,
        service: WeeklyService,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        # London, then New York: Friday noon moves from 11:00 to 16:00 UTC.
        new_york = zone('America/New_York')
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(friday(9))
        await service.run_guild(GUILD, friday(9))
        first = await row_of(repo, friday(9))
        later = datetime(2026, 10, 9, 12, 0, tzinfo=new_york)
        await clock.advance_to(later)

        moved = make_service(schedule=Weekly(FRIDAY, POST_TIME, new_york))
        report = await moved.run_guild(GUILD, later)

        assert report.solutions == ()
        assert report.problem == PostResult(first, PublishOutcome.ALREADY_HANDLED, None)
        assert keys(publisher) == [problem_key(GUILD, '2026-10-09')]

    async def test_the_default_rotation_follows_the_calendar(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD)  # no rotation stored
        editorials.add(
            *(
                page(problem_id, official(f'https://e/{problem_id}'))
                for problem_id in HARD_ATCODER
            )
        )

        # Week 39 of the default rotation: AtCoder hard; then Codeforces easy.
        await service.run_guild(GUILD, friday(2))
        await service.run_guild(GUILD, friday(9))

        assert DEFAULT_ROTATION[3] == RotationEntry('atcoder', Band.HARD, 'any')
        october_2 = await row_of(repo, friday(2))
        assert (october_2.source, october_2.band) == ('atcoder', 'hard')
        october_9 = await row_of(repo, friday(9))
        assert (october_9.source, october_9.problem_id) == ('codeforces', '1520A')


class TestPicking:
    async def test_a_queued_problem_wins_over_the_rotation_and_leaves_the_queue(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await repo.enqueue(queued('1520G'))
        await repo.enqueue(queued('1520F1', difficulty=1600, band='hard'))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.problem_id, row.selection, row.topic) == ('1520G', 'queued', None)
        assert (row.difficulty, row.band, row.name) == (2200, 'expert', 'Problem 1520G')
        assert (row.solution_url, row.solution_set_by) == (None, None)
        assert [item.problem_id for item in await repo.queue(GUILD)] == ['1520F1']
        assert (
            publisher.posts[0].message.title == 'Weekly problem: 1520G - Problem 1520G'
        )
        assert publisher.posts[0].message.description is not None
        assert '**Topic:**' not in publisher.posts[0].message.description

    async def test_a_queued_problems_link_is_the_admins_who_queued_it(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await repo.enqueue(queued('1520G', solution_url=BLOG))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.solution_url, row.solution_set_by) == (BLOG, ADMIN)

    async def test_a_queued_atcoder_problem_gets_its_editorial_found(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD)
        editorials.add(page('abc300_d', official(editorial_url('abc300', 6198))))
        await repo.enqueue(queued('abc300_d', source='atcoder'))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.solution_url, row.solution_set_by) == (
            editorial_url('abc300', 6198),
            None,
        )
        assert editorials.requests == ['abc300_d']

    async def test_a_queued_atcoder_problem_goes_out_when_its_editorial_cant_be_found(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        publisher: FakePublisher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD)
        editorials.errors.append(
            ExternalServiceError(
                'AtCoder', "AtCoder's editorial page could not be read."
            )
        )
        await repo.enqueue(queued('abc300_d', source='atcoder'))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.problem_id, row.solution_url) == ('abc300_d', None)
        assert len(publisher.posts) == 1
        assert (
            'Could not look up the editorials of AtCoder abc300_d for guild '
            f"{GUILD}: AtCoder's editorial page could not be read."
        ) in logged(caplog)

    async def test_a_queued_problem_the_server_has_had_is_dropped(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await repo.enqueue(queued('1520G'))
        await repo.enqueue(queued('1520F1'))
        # Picked by the rotation for an earlier week while it was being queued.
        earlier = WeeklyProblem(
            guild_id=GUILD,
            slot=friday(25, 9),
            week='2026-09-25',
            source='codeforces',
            problem_id='1520G',
            contest_id='1520',
            index='G',
            name='To Go Or Not To Go?',
            url='https://codeforces.com/contest/1520/problem/G',
            topic=None,
            difficulty=2200,
            band='expert',
            selection='auto',
            date_selected=friday(25, 9),
            solution_url=None,
            solution_set_by=None,
            solution_posted=False,
            solution_posted_at=None,
        )
        await repo.create(earlier)

        await service.run_guild(GUILD, friday(2))

        assert (await row_of(repo, friday(2))).problem_id == '1520F1'
        assert await repo.queue(GUILD) == []
        assert (
            f"Took codeforces 1520G out of guild {GUILD}'s weekly queue: it was the "
            'weekly problem of 2026-09-25'
        ) in logged(caplog)

    async def test_problems_the_server_has_had_are_never_picked_again(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        # Codeforces has one easy problem from round 1000 on (4A is older), so
        # the next week falls back to AtCoder's easy one.
        await set_up(guild_settings, GUILD, 'codeforces:easy:any')
        editorials.add(page('abc300_a', official(editorial_url('abc300', 6001))))

        await service.run_guild(GUILD, friday(2))
        await service.run_guild(GUILD, friday(9))

        assert (await row_of(repo, friday(2))).problem_id == '1520A'
        fallback = await row_of(repo, friday(9))
        assert (fallback.problem_id, fallback.topic, fallback.band) == (
            'abc300_a',
            None,
            'easy',
        )

    async def test_queued_problems_are_left_for_their_turn(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
    ) -> None:
        # Only a run's own queue head is taken; the rest wait for later weeks.
        service = make_service(rng=InOrder())
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await repo.enqueue(queued('1520E', difficulty=1400, band='medium'))
        await repo.enqueue(queued('1520D', difficulty=1200, band='medium'))

        await service.run_guild(GUILD, friday(2))

        assert (await row_of(repo, friday(2))).problem_id == '1520E'
        assert [item.problem_id for item in await repo.queue(GUILD)] == ['1520D']

        await service.run_guild(GUILD, friday(9))

        assert (await row_of(repo, friday(9))).problem_id == '1520D'
        assert await repo.queue(GUILD) == []
        # The rotation can't pick either now: both are used.
        with pytest.raises(NoWeeklyProblem, match='no medium Codeforces problem'):
            await service.run_guild(GUILD, friday(16))

    async def test_atcoder_problems_without_an_official_editorial_are_skipped(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        service = make_service(rng=InOrder())
        await set_up(guild_settings, GUILD, 'atcoder:hard:any')
        english = editorial_url('agc061', 5741)
        editorials.add(
            page('arc150_a', by_a_member('https://example.com/blog/arc150-a')),
            # arc151_a has no editorial page: a 404.
            page('agc061_a', official(english)),
        )

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.problem_id, row.solution_url) == ('agc061_a', english)
        assert editorials.requests == ['arc150_a', 'arc151_a', 'agc061_a']

    async def test_at_most_five_atcoder_problems_are_looked_up(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        service = make_service(rng=InOrder())
        await set_up(guild_settings, GUILD, 'atcoder:hard:any')
        editorials.add(page('arc154_a', official(editorial_url('arc154', 1))))

        await service.run_guild(GUILD, friday(2))

        assert editorials.requests == HARD_ATCODER[:5]
        row = await row_of(repo, friday(2))
        assert (row.source, row.problem_id, row.band) == (
            'codeforces',
            '1520F1',
            'hard',
        )

    async def test_an_atcoder_failure_switches_to_codeforces(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:hard:any')
        editorials.add(
            *(page(problem_id, official('https://e/x')) for problem_id in HARD_ATCODER)
        )
        editorials.errors.append(
            ExternalServiceError('AtCoder', 'AtCoder is not responding right now.')
        )

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.source, row.problem_id, row.topic, row.solution_url) == (
            'codeforces',
            '1520F1',
            None,
            None,
        )
        assert len(editorials.requests) == 1
        [message] = [m for m in logged(caplog) if m.startswith('Could not look up')]
        assert message == (
            f'Could not look up the editorials of AtCoder {editorials.requests[0]} '
            f'for guild {GUILD}, so no AtCoder problem is picked: AtCoder is not '
            'responding right now.'
        )

    async def test_a_codeforces_entry_falls_back_to_atcoder_on_any_topic(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:expert:geometry')
        editorials.add(page('agc060_a', official(editorial_url('agc060', 9))))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.source, row.problem_id, row.topic, row.band) == (
            'atcoder',
            'agc060_a',
            None,
            'expert',
        )

    async def test_a_platform_whose_list_isnt_loaded_is_skipped(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def codeforces_down(**kwargs: object) -> Any:
            raise cf.TrueApiError('HTTP Error 503, Service Unavailable')

        monkeypatch.setattr(cf.problemset, 'problems', codeforces_down)
        atcoder_only = ProblemCatalog(
            cast(AtCoderProblemsClient, FakeAtCoderProblems()), clock
        )
        assert list(await atcoder_only.refresh()) == ['codeforces']
        service = make_service(problems=atcoder_only)
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        editorials.add(page('abc300_d', official(editorial_url('abc300', 6198))))

        await service.run_guild(GUILD, friday(2))

        row = await row_of(repo, friday(2))
        assert (row.source, row.problem_id, row.topic) == ('atcoder', 'abc300_d', None)

    async def test_with_no_list_loaded_nothing_is_picked(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        publisher: FakePublisher,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The pick tries to load the lists, but both sites are down.
        async def codeforces_down(**kwargs: object) -> Any:
            raise cf.TrueApiError('HTTP Error 503, Service Unavailable')

        monkeypatch.setattr(cf.problemset, 'problems', codeforces_down)
        unloaded = ProblemCatalog(
            cast(AtCoderProblemsClient, AtCoderProblemsDown()), clock
        )
        service = make_service(problems=unloaded)
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')

        with pytest.raises(NoWeeklyProblem) as raised:
            await service.run_guild(GUILD, friday(2))

        assert str(raised.value) == (
            "Couldn't pick this week's problem: Codeforces' problem list isn't "
            "loaded yet; AtCoder's problem list isn't loaded yet."
        )
        assert await repo.get(GUILD, friday(2)) is None
        assert publisher.posts == []

    async def test_a_pick_waits_for_the_lists_the_job_is_loading(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        problemset: FakeProblemset,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Just after a restart, the refresh job is fetching the lists when the
        # weekly job catches up on a Friday it missed.
        fetched = asyncio.Event()
        downloaded = asyncio.Event()
        fetches: list[str] = []

        async def slow_problemset(**kwargs: object) -> Any:
            fetches.append('problemset')
            fetched.set()
            await downloaded.wait()
            return await problemset.problemset_problems()

        monkeypatch.setattr(cf.problemset, 'problems', slow_problemset)
        lists = ProblemCatalog(
            cast(AtCoderProblemsClient, FakeAtCoderProblems()), clock
        )
        service = make_service(problems=lists)
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        refreshing = asyncio.create_task(lists.refresh())
        await fetched.wait()

        running = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await settle()
        assert not running.done()  # it waits for the lists
        downloaded.set()
        report = await running

        assert await refreshing == {}
        assert report.problem is not None
        assert report.problem.outcome is PublishOutcome.SENT
        assert (await row_of(repo, friday(2))).problem_id in ('1520D', '1520E')
        assert fetches == ['problemset']  # the pick fetched nothing again

    async def test_the_candidates_are_shuffled(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
    ) -> None:
        rng = Shuffled()
        service = make_service(rng=rng)
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')

        await service.run_guild(GUILD, friday(2))

        # Codeforces' medium problems from round 1000 on, in the site's order.
        assert rng.shuffled == [['1520E', '1520D']]

    async def test_codeforces_picks_start_at_contest_1000(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        problems = [
            cf_problem(1000, 'A', 'Round 1000', 800, ['math']),
            cf_problem(999, 'A', 'Round 999', 800, ['math']),
        ]
        contests = [
            finished(1000, 'Codeforces Round 492 (Div. 2)'),
            finished(999, 'Codeforces Round 491 (Div. 2)'),
        ]

        async def problemset_problems(**kwargs: object) -> Any:
            statistics = [
                cf.ProblemStatistics(problem.contestId, problem.index, 100)
                for problem in problems
            ]
            return problems, statistics

        async def contest_list(**kwargs: object) -> list[cf.Contest]:
            return contests

        monkeypatch.setattr(cf.problemset, 'problems', problemset_problems)
        monkeypatch.setattr(cf.contest, 'to_list', contest_list)
        lists = ProblemCatalog(
            cast(AtCoderProblemsClient, FakeAtCoderProblems()), clock
        )
        assert await lists.refresh() == {}
        service = make_service(problems=lists)
        await set_up(guild_settings, GUILD, 'codeforces:easy:any')

        await service.run_guild(GUILD, friday(2))

        assert (await row_of(repo, friday(2))).problem_id == '1000A'
        # Contest 999's is too old, and AtCoder's easy one has no editorial.
        with pytest.raises(NoWeeklyProblem, match='no easy Codeforces problem'):
            await service.run_guild(GUILD, friday(9))

    async def test_nothing_left_to_pick_says_why(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        publisher: FakePublisher,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:expert:geometry')

        with pytest.raises(NoWeeklyProblem) as raised:
            await service.run_guild(GUILD, friday(2))

        assert str(raised.value) == (
            "Couldn't pick this week's problem: no expert Codeforces problem about "
            "geometry is left that this server hasn't had; none of the 1 expert "
            'AtCoder problems tried has an official editorial.'
        )
        assert editorials.requests == ['agc060_a']
        assert await repo.get(GUILD, friday(2)) is None
        assert publisher.posts == []

    async def test_an_atcoder_entry_with_nothing_left_says_so(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:expert:any')
        editorials.add(page('agc060_a', official(editorial_url('agc060', 9))))
        await service.run_guild(GUILD, friday(2))  # agc060_a, then 1520G
        await service.run_guild(GUILD, friday(9))

        with pytest.raises(NoWeeklyProblem) as raised:
            await service.run_guild(GUILD, friday(16))

        assert str(raised.value) == (
            "Couldn't pick this week's problem: no expert AtCoder problem is left "
            "that this server hasn't had; no expert Codeforces problem is left "
            "that this server hasn't had."
        )


class TestSolutions:
    async def test_an_atcoder_solution_gets_the_best_editorial_there_is_now(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        japanese = editorial_url('abc300', 6100)
        editorials.add(page('abc300_d', official(japanese, english=False)))
        await service.run_guild(GUILD, friday(2))
        assert (await row_of(repo, friday(2))).solution_url == japanese
        english = editorial_url('abc300', 6198)
        editorials.add(
            page('abc300_d', official(japanese, english=False), official(english))
        )
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        stored = await row_of(repo, friday(2))
        assert (stored.solution_url, stored.solution_set_by) == (english, None)
        assert publisher.posts[1].message == OutgoingMessage(
            title='Solution: ABC300 D - AABCC',
            description=(
                "Last week's problem: [ABC300 D - AABCC]"
                '(https://atcoder.jp/contests/abc300/tasks/abc300_d)\n'
                f'[Editorial]({english})\n'
                f'[All editorials]({all_editorials("abc300", "abc300_d")})'
            ),
            url=english,
            footer=FOOTER,
            mention_role=False,
        )

    async def test_an_atcoder_solution_an_admin_set_is_kept(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        editorials.add(page('abc300_d', official(editorial_url('abc300', 6198))))
        await service.run_guild(GUILD, friday(2))
        await repo.set_solution(GUILD, friday(2), BLOG, set_by=ADMIN)
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        assert editorials.requests == ['abc300_d']  # only when it was picked
        assert publisher.posts[1].message.url == BLOG

    async def test_an_atcoder_solution_keeps_its_link_if_atcoder_fails(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        editorials: FakeEditorials,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        first = editorial_url('abc300', 6198)
        editorials.add(page('abc300_d', official(first)))
        await service.run_guild(GUILD, friday(2))
        editorials.errors.append(ExternalServiceError('AtCoder', 'Down.'))
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        assert (await row_of(repo, friday(2))).solution_url == first
        assert publisher.posts[1].message.url == first
        assert (
            f'Could not look up the editorials of AtCoder abc300_d for guild {GUILD}: '
            'Down.'
        ) in logged(caplog)

    async def test_an_atcoder_solution_without_an_editorial_links_the_task_page(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await repo.enqueue(queued('abc300_d', source='atcoder'))
        await service.run_guild(GUILD, friday(2))  # no editorial page yet
        await clock.advance_to(friday(9))

        await service.run_guild(GUILD, friday(9))

        page_url = all_editorials('abc300', 'abc300_d')
        message = publisher.posts[1].message
        assert message.url == page_url
        assert message.description is not None
        assert message.description.splitlines()[1:] == [f'[All editorials]({page_url})']

    async def test_a_solution_whose_problem_never_went_out_is_left_alone(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        publisher.fail_next(PublishOutcome.SKIPPED)  # Discord refused it
        report = await service.run_guild(GUILD, friday(2))
        assert report.problem is not None
        assert report.problem.outcome is PublishOutcome.SKIPPED
        await clock.advance_to(friday(9))

        report = await service.run_guild(GUILD, friday(9))

        assert report.solutions == ()
        assert keys(publisher) == ['weekly:1100000000000000001:problem:2026-10-09']
        assert not (await row_of(repo, friday(2))).solution_posted

    async def test_solutions_more_than_four_weeks_old_are_left_alone(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
    ) -> None:
        # Problems went out four and five weeks before, and then the bot was
        # down. Run in this order, neither run posts the other's solution.
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await service.run_guild(GUILD, friday(4, 9))
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(28, 8))
        await set_up(guild_settings, GUILD, 'codeforces:expert:any')
        publisher.posts.clear()

        report = await service.run_guild(GUILD, friday(2))

        assert [result.row.week for result in report.solutions] == ['2026-09-04']
        assert keys(publisher) == [
            'weekly:1100000000000000001:solution:2026-09-04',
            'weekly:1100000000000000001:problem:2026-10-02',
        ]

    async def test_due_solutions_go_out_oldest_first(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(18, 9))
        await repo.mark_solution_posted(GUILD, friday(18, 9), NOW)
        await service.run_guild(GUILD, friday(25, 9))
        await repo.create(
            replace(
                await row_of(repo, friday(25, 9)),
                slot=friday(11, 9),
                week='2026-09-11',
                problem_id='1520A',
                index='A',
            )
        )
        await posted_problem(publisher, GUILD, '2026-09-11')
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        publisher.posts.clear()

        await service.run_guild(GUILD, friday(2))

        assert keys(publisher) == [
            'weekly:1100000000000000001:solution:2026-09-11',
            'weekly:1100000000000000001:solution:2026-09-25',
            'weekly:1100000000000000001:problem:2026-10-02',
        ]

    @pytest.mark.parametrize(
        'outcome', [PublishOutcome.PENDING, PublishOutcome.SKIPPED]
    )
    async def test_a_solution_that_may_have_gone_out_counts_as_posted(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
        outcome: PublishOutcome,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(2))
        await clock.advance_to(friday(9))
        publisher.fail_next(outcome)

        report = await service.run_guild(GUILD, friday(9))

        assert [result.outcome for result in report.solutions] == [outcome]
        assert report.problem is not None
        assert report.problem.outcome is PublishOutcome.SENT
        assert (await row_of(repo, friday(2))).solution_posted

    async def test_an_undeliverable_solution_holds_the_problem_back(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(2))
        await clock.advance_to(friday(9))
        publisher.undeliverable_next()

        report = await service.run_guild(GUILD, friday(9))

        first = await row_of(repo, friday(2))
        assert report == WeeklyReport(
            True,
            (PostResult(first, PublishOutcome.UNDELIVERABLE, 'guild-unavailable'),),
            None,
        )
        assert report.retry_later
        assert not first.solution_posted
        assert await repo.get(GUILD, friday(9)) is None

        await clock.advance(5 * MINUTE)
        again = await service.run_guild(GUILD, friday(9))

        assert [result.outcome for result in again.solutions] == [PublishOutcome.SENT]
        assert keys(publisher)[1:] == [
            'weekly:1100000000000000001:solution:2026-10-02',
            'weekly:1100000000000000001:problem:2026-10-09',
        ]

    async def test_a_failed_pick_still_reports_the_solutions_it_posted(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await service.run_guild(GUILD, friday(2))
        # Nothing is left to pick: no geometry problem, and AtCoder's expert
        # one has no editorial.
        await set_up(guild_settings, GUILD, 'codeforces:expert:geometry')
        await clock.advance_to(friday(9))

        with pytest.raises(NoWeeklyProblem) as raised:
            await service.run_guild(GUILD, friday(9))

        assert [
            (result.row.week, result.outcome) for result in raised.value.solutions
        ] == [('2026-10-02', PublishOutcome.SENT)]
        assert keys(publisher)[1:] == [solution_key(GUILD, '2026-10-02')]
        # The job still counts it a failure, and tries the slot again.
        with pytest.raises(RuntimeError) as job:
            await service.run_slot(friday(9))
        assert isinstance(job.value.__cause__, NoWeeklyProblem)
        assert job.value.__cause__.solutions == ()  # posted already

    async def test_a_solution_handled_already_counts_as_posted(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(2))
        # Its post went out, but the run stopped before marking it.
        solution = Delivery(solution_key(GUILD, '2026-10-02'), GUILD, WEEKLY)
        await publisher.publish([solution], OutgoingMessage(title='Solution'))
        await clock.advance_to(friday(9))

        report = await service.run_guild(GUILD, friday(9))

        outcomes = [result.outcome for result in report.solutions]
        assert outcomes == [PublishOutcome.ALREADY_HANDLED]
        assert (await row_of(repo, friday(2))).solution_posted

    async def test_a_feature_turned_off_mid_run_stops_it(
        self,
        service: WeeklyService,
        repo: WeeklyRepo,
        catalog: ProblemCatalog,
        editorials: FakeEditorials,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.run_guild(GUILD, friday(2))
        await clock.advance_to(friday(9))
        # The settings said yes, but by the time of the post an admin said no.
        not_configured = ScriptedPublisher(
            PublishResult(PublishOutcome.NOT_CONFIGURED, reason='disabled')
        )
        stopping = WeeklyService(
            repo,
            catalog,
            EditorialFinder(cast(AtCoderEditorialsClient, editorials)),
            guild_settings,
            ledger,
            not_configured,
            clock,
            SCHEDULE,
        )

        report = await stopping.run_guild(GUILD, friday(9))

        first = await row_of(repo, friday(2))
        assert report == WeeklyReport(
            False,
            (PostResult(first, PublishOutcome.NOT_CONFIGURED, 'disabled'),),
            None,
        )
        assert not first.solution_posted
        assert await repo.get(GUILD, friday(9)) is None


class ScriptedPublisher:
    """A ``Publisher`` that answers every post with ``result``, posting nothing."""

    def __init__(self, result: PublishResult) -> None:
        self.result = result
        self.calls: list[tuple[list[Delivery], OutgoingMessage]] = []

    async def publish(self, deliveries: Any, message: OutgoingMessage) -> PublishResult:
        self.calls.append((list(deliveries), message))
        return self.result


class TestRunSlot:
    async def test_every_server_with_the_feature_on_gets_its_problem(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await set_up(guild_settings, OTHER_GUILD, 'codeforces:hard:any')
        await set_up(guild_settings, 1_100_000_000_000_000_003, enabled=False)

        await service.run_slot(friday(2))

        assert keys(publisher) == [
            'weekly:1100000000000000001:problem:2026-10-02',
            'weekly:1100000000000000002:problem:2026-10-02',
        ]

    async def test_a_server_that_fails_is_retried_after_the_others_run(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:expert:geometry')
        await set_up(guild_settings, OTHER_GUILD, 'codeforces:hard:any')

        with pytest.raises(RuntimeError) as raised:
            await service.run_slot(friday(2))

        assert str(raised.value) == (
            f'The weekly problem was not posted in guilds {GUILD}'
        )
        assert isinstance(raised.value.__cause__, NoWeeklyProblem)
        assert keys(publisher) == ['weekly:1100000000000000002:problem:2026-10-02']
        assert logged(caplog)[0].startswith(
            f'Could not run the weekly problem of guild {GUILD} for its '
            "2026-10-02 11:00:00+00:00 slot: Couldn't pick this week's problem: "
        )

    async def test_a_bug_in_one_server_is_logged_with_its_traceback(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await set_up(guild_settings, OTHER_GUILD, 'codeforces:hard:any')
        run_guild = service.run_guild
        bug = ZeroDivisionError('a bug')

        async def failing(guild_id: int, slot: datetime) -> WeeklyReport:
            if guild_id == GUILD:
                raise bug
            return await run_guild(guild_id, slot)

        monkeypatch.setattr(service, 'run_guild', failing)

        with pytest.raises(RuntimeError) as raised:
            await service.run_slot(friday(2))

        assert raised.value.__cause__ is bug
        [record] = [r for r in caplog.records if r.name == LOGGER and r.exc_info]
        assert record.levelno == logging.INFO
        assert record.getMessage() == (
            f'Could not run the weekly problem of guild {GUILD} for its '
            '2026-10-02 11:00:00+00:00 slot'
        )
        assert keys(publisher) == ['weekly:1100000000000000002:problem:2026-10-02']

    async def test_an_undeliverable_problem_is_retried_with_the_same_problem(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        ledger: DeliveryLedger,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(friday(2))
        publisher.undeliverable_next()

        with pytest.raises(RuntimeError) as raised:
            await service.run_slot(friday(2))

        assert str(raised.value) == (
            f'The weekly problem was not posted in guilds {GUILD}'
        )
        assert raised.value.__cause__ is None
        picked = await row_of(repo, friday(2))
        assert publisher.posts == []
        assert await ledger.get(problem_key(GUILD, '2026-10-02')) is None
        assert (
            f'A weekly post of guild {GUILD} for its 2026-10-02 11:00:00+00:00 slot '
            'could not be delivered yet; the slot will be tried again'
        ) in logged(caplog)

        await clock.advance(5 * MINUTE)
        await service.run_slot(friday(2))

        assert await row_of(repo, friday(2)) == picked
        assert picked.date_selected == friday(2)
        [post] = publisher.posts
        assert post.message.url == picked.url

    async def test_a_post_an_admin_can_fix_is_retried(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        # The bot lacks Embed Links in the channel at noon; an admin fixes it.
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(friday(2))
        publisher.undeliverable_next(reason='missing-permissions: embed_links')

        with pytest.raises(RuntimeError, match='not posted in guilds'):
            await service.run_slot(friday(2))

        picked = await row_of(repo, friday(2))
        await clock.advance(5 * MINUTE)
        await service.run_slot(friday(2))
        [post] = publisher.posts
        assert post.message.url == picked.url
        assert await row_of(repo, friday(2)) == picked

    async def test_a_server_the_bot_isnt_in_is_skipped(
        self,
        make_service: Callable[..., WeeklyService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The bot left OTHER_GUILD, whose settings stay: no admin there can
        # turn the feature off.
        service = make_service(in_guild=lambda guild_id: guild_id != OTHER_GUILD)
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await set_up(guild_settings, OTHER_GUILD, 'codeforces:medium:any')

        await service.run_slot(friday(2))  # nothing to retry

        assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]
        assert await repo.history(OTHER_GUILD, at_or_before=friday(2)) == []
        assert (
            f'Skipping the weekly problem of guild {OTHER_GUILD} for its '
            '2026-10-02 11:00:00+00:00 slot: the bot is not in it'
        ) in logged(caplog)

    async def test_the_previous_slot_is_right_across_the_clock_change(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        # The clocks go back on 2026-10-25: Friday noon in London is 11:00 UTC
        # on the 23rd and 12:00 UTC on the 30th, not a week after 11:00.
        october_23, october_30 = friday(23), friday(30)
        assert (october_23.hour, october_30.hour) == (11, 12)
        assert SCHEDULE.prev_at_or_before(october_30 - SECOND) == october_23
        assert october_30 - timedelta(weeks=1) != october_23
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(october_23)
        await service.run_slot(october_23)
        await clock.advance_to(october_30)

        await service.run_slot(october_30)

        problem, solution, new_problem = publisher.posts
        assert problem.deliveries[0].expires_at == october_30
        assert problem.message.description is not None
        assert problem.message.description.endswith(
            f'**Solution:** {stamp(october_30, "F")} ({stamp(october_30, "R")})'
        )
        assert solution.deliveries == (
            Delivery(
                key=solution_key(GUILD, '2026-10-23'),
                guild_id=GUILD,
                feature=WEEKLY,
                subject='weekly',
                subject_id='2026-10-23',
                kind='solution',
                occurrence_start=october_23,
                expires_at=friday(6, 11),
            ),
        )
        assert new_problem.keys == (problem_key(GUILD, '2026-10-30'),)
        assert new_problem.deliveries[0].occurrence_start == october_30


class TestAdminChanges:
    """Admins' changes to the queue and solution links, which take turns with
    the server's runs.
    """

    @pytest.mark.parametrize('failure', ['refused', 'undelivered'])
    async def test_a_problem_whose_post_never_went_out_can_be_queued_again(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
        failure: str,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await service.enqueue(queued('1520G'))
        if failure == 'refused':
            publisher.fail_next(PublishOutcome.SKIPPED)
        else:
            publisher.undeliverable_next(reason='channel-missing')
        await service.run_guild(GUILD, friday(2))
        assert (await row_of(repo, friday(2))).problem_id == '1520G'
        # Its week isn't over: a run of its slot would post it.
        with pytest.raises(ProblemAlreadyUsed):
            await service.enqueue(queued('1520G'))
        await clock.advance_to(friday(9))
        await service.run_guild(GUILD, friday(9))

        again = await service.enqueue(queued('1520G'))

        assert await repo.queue(GUILD) == [again]
        assert await repo.get(GUILD, friday(2)) is None
        assert ('codeforces', '1520G') not in await repo.used_problems(GUILD)
        # One whose post went out stays the server's.
        posted = await row_of(repo, friday(9))
        with pytest.raises(ProblemAlreadyUsed):
            await service.enqueue(queued(posted.problem_id))

    async def test_the_queue_holds_at_most_25_even_when_queued_at_once(
        self, service: WeeklyService, repo: WeeklyRepo
    ) -> None:
        for number in range(24):
            await service.enqueue(queued(f'{2000 + number}A'))

        results = await asyncio.gather(
            service.enqueue(queued('1520D')),
            service.enqueue(queued('1520E')),
            return_exceptions=True,
        )

        [error] = [result for result in results if isinstance(result, Exception)]
        assert isinstance(error, KcpcUserError)
        assert str(error) == (
            'The queue is full: it holds at most 25 problems. Take one out with '
            '`/kcpc weekly unqueue` first.'
        )
        assert len(await repo.queue(GUILD)) == 25

    async def test_queueing_waits_for_a_run_that_is_picking(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await repo.enqueue(queued('abc300_d', source='atcoder'))
        editorials.gates[0] = asyncio.Event()
        running = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await until(lambda: editorials.requests == ['abc300_d'])

        queueing = asyncio.create_task(service.enqueue(queued('1520G')))
        await settle()
        assert not queueing.done()
        editorials.gates[0].set()
        await running

        stored = await queueing
        assert await repo.queue(GUILD) == [stored]

    async def test_an_unqueue_waits_for_a_run_that_is_picking(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        repo: WeeklyRepo,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await repo.enqueue(queued('abc300_d', source='atcoder'))
        editorials.gates[0] = asyncio.Event()
        running = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await until(lambda: editorials.requests == ['abc300_d'])

        unqueueing = asyncio.create_task(service.unqueue(GUILD, 'atcoder', 'abc300_d'))
        await settle()
        assert not unqueueing.done()
        editorials.gates[0].set()
        report = await running

        # The run took it first, so the admin isn't told it was taken out.
        assert await unqueueing is None
        assert report.problem is not None
        assert report.problem.row.problem_id == 'abc300_d'

    async def test_a_problem_unqueued_while_it_was_being_picked_isnt_posted(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        repo: WeeklyRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await repo.enqueue(queued('abc300_d', source='atcoder'))
        editorials.gates[0] = asyncio.Event()
        running = asyncio.create_task(service.run_guild(GUILD, friday(2)))
        await until(lambda: editorials.requests == ['abc300_d'])
        # Taken out by something that doesn't take turns with the run.
        assert await repo.dequeue(GUILD, 'atcoder', 'abc300_d') is not None
        editorials.gates[0].set()

        report = await running

        row = await row_of(repo, friday(2))
        assert (row.source, row.selection) == ('codeforces', 'auto')
        assert report.problem is not None and report.problem.row == row
        assert (
            f"atcoder abc300_d left guild {GUILD}'s weekly queue while it was being "
            'picked'
        ) in logged(caplog)

    async def test_a_solution_link_set_during_a_run_waits_for_it(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        found = editorial_url('abc300', 6198)
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        editorials.add(page('abc300_d', official(found)))
        await service.run_guild(GUILD, friday(2))
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(friday(9))
        editorials.gates[1] = asyncio.Event()  # the solution's lookup
        running = asyncio.create_task(service.run_guild(GUILD, friday(9)))
        await until(lambda: len(editorials.requests) == 2)

        setting = asyncio.create_task(
            service.set_solution(GUILD, friday(2), BLOG, set_by=ADMIN)
        )
        await settle()
        assert not setting.done()
        editorials.gates[1].set()
        await running

        # Posted first, with the link it had: too late to change.
        stored = await setting
        assert stored is not None
        assert (stored.solution_posted, stored.solution_url) == (True, found)
        assert publisher.posts[1].message.url == found

    async def test_the_solution_post_links_what_is_stored_when_it_goes_out(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        editorials: FakeEditorials,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'atcoder:medium:any')
        editorials.add(page('abc300_d', official(editorial_url('abc300', 6198))))
        await service.run_guild(GUILD, friday(2))
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        await clock.advance_to(friday(9))
        editorials.gates[1] = asyncio.Event()  # the solution's lookup
        running = asyncio.create_task(service.run_guild(GUILD, friday(9)))
        await until(lambda: len(editorials.requests) == 2)
        # Set by something that doesn't take turns with the run.
        await repo.set_solution(GUILD, friday(2), BLOG, set_by=ADMIN)
        editorials.gates[1].set()

        await running

        assert publisher.posts[1].message.url == BLOG
        assert (await row_of(repo, friday(2))).solution_url == BLOG


class TestReading:
    async def test_current_and_history_are_the_problems_that_went_out(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        assert await service.current(GUILD) is None
        assert await service.history(GUILD) == []
        await service.run_guild(GUILD, friday(11, 9))
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        await service.run_guild(GUILD, friday(18, 9))
        await set_up(guild_settings, GUILD, 'codeforces:expert:any')
        # So that the next post is the problem's, which never goes out.
        await repo.mark_solution_posted(GUILD, friday(18, 9), NOW)
        publisher.fail_next(PublishOutcome.SKIPPED)
        await service.run_guild(GUILD, friday(25, 9))

        september_11 = await row_of(repo, friday(11, 9))
        september_18 = await row_of(repo, friday(18, 9))
        assert await service.history(GUILD) == [september_18, september_11]
        assert await service.current(GUILD) == september_18

        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        publisher.fail_next(PublishOutcome.PENDING)  # may have gone out
        await clock.advance_to(friday(2))
        await service.run_guild(GUILD, friday(2))

        october_2 = await row_of(repo, friday(2))
        assert await service.current(GUILD) == october_2
        assert [row.week for row in await service.history(GUILD)] == [
            '2026-10-02',
            '2026-09-18',
            '2026-09-11',
        ]
        assert await service.history(OTHER_GUILD) == []

    async def test_a_problem_posted_is_one_that_went_out_or_may_have(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
    ) -> None:
        # Each run's first post is its problem's: no solution is due before it.
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        publisher.fail_next(PublishOutcome.SKIPPED)  # Discord refused it
        await service.run_guild(GUILD, friday(11, 9))
        await set_up(guild_settings, GUILD, 'codeforces:hard:any')
        publisher.fail_next(PublishOutcome.PENDING)  # it may have gone out
        await service.run_guild(GUILD, friday(18, 9))
        await set_up(guild_settings, GUILD, 'codeforces:expert:any')
        await service.run_guild(GUILD, friday(25, 9))

        posted = [
            await service.problem_posted(await row_of(repo, friday(day, 9)))
            for day in (11, 18, 25)
        ]

        assert posted == [False, True, True]
        unposted = replace(await row_of(repo, friday(25, 9)), week='2026-09-04')
        assert not await service.problem_posted(unposted)

    async def test_a_problems_difficulty_is_as_its_post_says_it(
        self,
        service: WeeklyService,
        make_service: Callable[..., WeeklyService],
        clock: FakeClock,
    ) -> None:
        unloaded = make_service(
            problems=ProblemCatalog(
                cast(AtCoderProblemsClient, FakeAtCoderProblems()), clock
            )
        )

        assert service.difficulty(stored('codeforces')) == '1200 (medium)'
        assert service.difficulty(stored('codeforces', difficulty=None)) == 'unrated'
        assert service.difficulty(stored('atcoder')) == (
            '1000 on AtCoder (about 1467 on Codeforces, medium)'
        )
        # Without AtCoder's list, its own difficulty isn't known.
        assert unloaded.difficulty(stored('atcoder')) == (
            'about 1467 on Codeforces (medium)'
        )
        unknown = stored('atcoder', problem_id='abc999_a', contest_id='abc999')
        assert service.difficulty(unknown) == 'about 1467 on Codeforces (medium)'

    async def test_the_plan_is_the_next_slots_queued_problem_or_entry(
        self,
        service: WeeklyService,
        guild_settings: GuildSettingsRepo,
        repo: WeeklyRepo,
        ledger: DeliveryLedger,
    ) -> None:
        await set_up(guild_settings, GUILD)
        empty = await service.plan_next(GUILD)
        assert empty == WeeklyPlan(
            slot=friday(2),
            queued=None,
            entry=RotationEntry('atcoder', Band.HARD, 'any'),
            queue=(),
            rotation=DEFAULT_ROTATION,
        )

        rotation = ('codeforces:easy:any', 'codeforces:hard:graphs')
        await set_up(guild_settings, GUILD, *rotation)
        used = await repo.enqueue(queued('1520G'))
        waiting = await repo.enqueue(queued('1520F1'))
        await repo.create(
            WeeklyProblem(
                guild_id=GUILD,
                slot=friday(25, 9),
                week='2026-09-25',
                source='codeforces',
                problem_id='1520G',
                contest_id='1520',
                index='G',
                name='To Go Or Not To Go?',
                url='https://codeforces.com/contest/1520/problem/G',
                topic=None,
                difficulty=2200,
                band='expert',
                selection='auto',
                date_selected=NOW,
                solution_url=None,
                solution_set_by=None,
                solution_posted=False,
                solution_posted_at=None,
            )
        )

        plan = await service.plan_next(GUILD)

        assert plan == WeeklyPlan(
            slot=friday(2),
            queued=waiting,
            entry=RotationEntry('codeforces', Band.HARD, 'graphs'),
            queue=(used, waiting),
            rotation=(
                RotationEntry('codeforces', Band.EASY, 'any'),
                RotationEntry('codeforces', Band.HARD, 'graphs'),
            ),
        )
        assert len(await repo.queue(GUILD)) == 2  # planning writes nothing
        assert await ledger.status_counts(GUILD) == dict.fromkeys(DeliveryStatus, 0)


def stored(source: str, **changes: Any) -> WeeklyProblem:
    """GUILD's problem for 2026-10-02: 1520D on Codeforces, abc300_d on AtCoder."""
    codeforces = source == 'codeforces'
    row = WeeklyProblem(
        guild_id=GUILD,
        slot=friday(2),
        week='2026-10-02',
        source=source,
        problem_id='1520D' if codeforces else 'abc300_d',
        contest_id='1520' if codeforces else 'abc300',
        index='D',
        name='Same Differences' if codeforces else 'AABCC',
        url='https://example.com/problem',
        topic=None,
        difficulty=1200 if codeforces else 1467,
        band='medium',
        selection='auto',
        date_selected=NOW,
        solution_url=None,
        solution_set_by=None,
        solution_posted=False,
        solution_posted_at=None,
    )
    return replace(row, **changes)


class TestSolutionLinks:
    def test_codeforces_links_its_editorial_else_its_contest(self) -> None:
        assert solution_links(stored('codeforces', solution_url=BLOG)) == (
            SolutionLink(BLOG, 'Editorial'),
        )
        assert solution_links(stored('codeforces')) == (
            SolutionLink('https://codeforces.com/contest/1520', 'Contest materials'),
        )

    def test_atcoder_links_its_editorial_and_always_the_task_page(self) -> None:
        url = editorial_url('abc300', 6198)
        task_page = all_editorials('abc300', 'abc300_d')

        assert solution_links(stored('atcoder', solution_url=url)) == (
            SolutionLink(url, 'Editorial'),
            SolutionLink(task_page, 'All editorials'),
        )
        assert solution_links(stored('atcoder')) == (
            SolutionLink(task_page, 'All editorials'),
        )

    async def test_the_finder_asks_atcoder_and_passes_its_errors_on(
        self, editorials: FakeEditorials
    ) -> None:
        finder = EditorialFinder(cast(AtCoderEditorialsClient, editorials))
        found = page('abc300_d', official(editorial_url('abc300', 1)))
        editorials.add(found)

        assert await finder.atcoder('abc300', 'abc300_d') == found
        assert await finder.atcoder('abc300', 'abc300_z') is None
        editorials.errors.append(KcpcUserError("That isn't a valid AtCoder problem."))
        with pytest.raises(KcpcUserError):
            await finder.atcoder('abc300', 'abc300_d')
        assert editorials.requests == ['abc300_d', 'abc300_z', 'abc300_d']


async def test_the_fake_publisher_can_make_posts_undeliverable(
    publisher: FakePublisher, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> None:
    await set_up(guild_settings, GUILD)
    delivery = Delivery('k1', GUILD, WEEKLY)
    message = OutgoingMessage(title='Hi')
    await publisher.publish([delivery], message)
    publisher.undeliverable_next()
    publisher.undeliverable_next(reason='channel-missing')

    # Before the claim: a key handled already doesn't stop it.
    assert await publisher.publish([delivery], message) == PublishResult(
        PublishOutcome.UNDELIVERABLE, reason='guild-unavailable'
    )
    assert await publisher.publish([Delivery('k2', GUILD, WEEKLY)], message) == (
        PublishResult(PublishOutcome.UNDELIVERABLE, reason='channel-missing')
    )
    assert await ledger.get('k2') is None
    result = await publisher.publish([Delivery('k2', GUILD, WEEKLY)], message)
    assert result.outcome is PublishOutcome.SENT


class TestTheJob:
    """The weekly job as the cog adds it: persistent, with 6 hours' grace."""

    @pytest.fixture
    async def scheduler(
        self, db: Database, clock: FakeClock, service: WeeklyService
    ) -> AsyncIterator[Scheduler]:
        scheduler = Scheduler(db, clock)
        scheduler.add(
            ScheduledJob(
                WEEKLY_JOB,
                SCHEDULE,
                service.run_slot,
                catch_up_grace=timedelta(hours=6),
            )
        )
        yield scheduler
        await asyncio.wait_for(scheduler.stop(), STOP_TIMEOUT)

    async def test_the_first_start_posts_nothing_until_the_next_slot(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        db: Database,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')

        scheduler.start()

        await parked(scheduler, friday(2))
        assert publisher.posts == []
        assert await last_slot(db) == friday(25, 9)

        await clock.advance_to(friday(2))
        await parked(scheduler, friday(9))
        assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]

    async def test_a_slot_missed_by_less_than_six_hours_is_caught_up(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        scheduler.start()
        await parked(scheduler, friday(2))
        await scheduler.stop()

        late = friday(2) + 6 * HOUR - SECOND
        await clock.advance_to(late)  # the bot was down at noon
        scheduler.start()

        await parked(scheduler, friday(9))
        assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]
        assert (await row_of(repo, friday(2))).date_selected == late

    async def test_a_slot_missed_by_more_than_six_hours_is_skipped(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        db: Database,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        scheduler.start()
        await parked(scheduler, friday(2))
        await scheduler.stop()

        await clock.advance_to(friday(2) + 6 * HOUR + SECOND)
        scheduler.start()

        await parked(scheduler, friday(9))
        assert publisher.posts == []
        assert await repo.get(GUILD, friday(2)) is None
        assert await last_slot(db) == friday(2)

    @pytest.mark.parametrize(
        'reason',
        # Discord hasn't sent the server yet; an admin fixes the permissions.
        ['guild-unavailable', 'missing-permissions: embed_links'],
    )
    async def test_an_undeliverable_post_is_retried_within_the_grace(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: WeeklyRepo,
        clock: FakeClock,
        reason: str,
    ) -> None:
        await set_up(guild_settings, GUILD, 'codeforces:medium:any')
        publisher.undeliverable_next(reason=reason)
        scheduler.start()
        await parked(scheduler, friday(2))

        await clock.advance_to(friday(2))

        await parked(scheduler, friday(2) + 5 * MINUTE)
        assert publisher.posts == []
        assert job_status(scheduler).failures == 1
        picked = await row_of(repo, friday(2))

        await clock.advance_to(friday(2) + 5 * MINUTE)

        await parked(scheduler, friday(9))
        assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]
        assert await row_of(repo, friday(2)) == picked
        assert job_status(scheduler).failures == 0


def job_status(scheduler: Scheduler) -> JobStatus:
    [status] = scheduler.status()
    return status


async def parked(scheduler: Scheduler, until: datetime) -> None:
    """Wait (up to 10 s of real time) until the job waits to run at ``until``."""
    for _ in range(2000):
        if job_status(scheduler).next_run == until:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until the job waits for {until}')


async def last_slot(db: Database) -> datetime | None:
    seconds = await db.fetchval(
        'SELECT last_slot FROM job_state WHERE job = ?', (WEEKLY_JOB,)
    )
    return None if seconds is None else from_epoch(seconds)
