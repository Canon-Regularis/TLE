"""Tests for the problems cog (tle.kcpc.features.problems.cog).

The cog runs on real KCPC services (database, settings, ledger and scheduler),
whose publisher is FakePublisher, on a real bot that also has the admin cog
and TLE's user database, in memory. Only the sites are faked: AtCoder Problems
and AtCoder's editorial pages by FakeSites, in place of the cog's clients, and
Codeforces by FakeCodeforces, in place of TLE's ``cf.problemset.problems``,
``cf.contest.to_list`` and ``cf.user.status``. Members' AtCoder handles come
from FakeHandles, registered as the accounts feature registers its service.
Most tests call a command's callback with a mocked context; the rest go
through discord.py, for its checks, its parsing, its error handling and how it
adds and removes the cog. The users are made up.

The clock starts on Thursday 2026-10-01 at 12:00 UTC. The week's slot was
Friday 2026-09-25 at noon in London (11:00 UTC), and the next is on
2026-10-02; the clocks go back on 2026-10-25.
"""

import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tests.kcpc.conftest import CLOCK_START
from tests.kcpc.fakes import FakePublisher
from tle import constants
from tle.config import Settings
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.cog import UNEXPECTED_ERROR_MESSAGE
from tle.kcpc.bot.embeds import ALERT_COLOR, KCPC_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.pages import PageView
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import Delivery, DeliveryLedger
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo, default_registry
from tle.kcpc.core.timeutil import to_epoch, zone
from tle.kcpc.features.admin.cog import setup as add_admin_cog
from tle.kcpc.features.problems import cog as problems_cog
from tle.kcpc.features.problems.cog import REFRESH_JOB, KcpcProblems, setup
from tle.kcpc.features.problems.repo import QueuedProblem, WeeklyProblem, WeeklyRepo
from tle.kcpc.features.problems.settings import SPEC, WEEKLY, WeeklySettings
from tle.kcpc.features.problems.solved import CODEFORCES_PAUSE
from tle.kcpc.features.problems.weekly import (
    PROBLEM,
    SUBJECT,
    WEEKLY_JOB,
    problem_key,
    solution_key,
)
from tle.kcpc.platforms.atcoder.editorials import AtCoderEditorial, AtCoderEditorials
from tle.kcpc.platforms.atcoder.problems import (
    SUBMISSIONS_PAGE,
    AtCoderProblem,
    AtCoderSubmission,
)
from tle.kcpc.services import KcpcServices
from tle.util import codeforces_api as cf
from tle.util.db.user_db_conn import UserDbConn

T = TypeVar('T')

# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
ADMIN = 1_300_000_000_000_000_001  # the user ID of the admin in ``ctx``
MEMBER = 1_300_000_000_000_000_002  # the user ID of the member in ``member_ctx``

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
SECOND = timedelta(seconds=1)
NOW = CLOCK_START  # Thursday 2026-10-01, 13:00 in London
CLUB = zone('Europe/London')
COG_LOGGER = 'tle.kcpc.features.problems.cog'
ADMIN_LOGGER = 'tle.kcpc.bot.admin'
ADMIN_COMMANDS = ['post-now', 'preview', 'queue', 'rotation', 'solution', 'unqueue']
BLOG = 'https://codeforces.com/blog/entry/90342'
BAD_LINK = 'The link must be a web address starting with https:// or http://.'
BAD_WEEK = "Give the week as its Friday's date, YYYY-MM-DD, such as 2026-10-09."
BAD_DIFFICULTY = (
    'Give a difficulty: easy, medium, hard or expert, or a rating from 800 to 3500.'
)
NOT_SET_UP = (
    "The weekly problem isn't set up here. Turn it on with `/kcpc enable weekly` "
    'and set its channel with `/kcpc channel weekly #channel`, then try again.'
)
SOLVED_AS = "Leaving out problems you've solved as {handle}"
NOT_ALL_CHECKED = "Couldn't check all the problems you've solved"
NOT_LOADED = (
    "{platform} problem list isn't loaded yet. Please try again in a few minutes."
)
LONG_LINK = (
    'The link is too long: it can be at most 300 characters, so that posts and '
    'replies can show it.'
)
CONTEST_PAGE_NOTE = (
    'Its solution post will link the contest page, where Codeforces lists the '
    'editorial under Contest materials, unless you set a link with '
    '`/kcpc weekly solution` once it is posted.'
)
ROTATION_HINT = (
    'Set it with `/kcpc weekly rotation cf easy, ac medium, cf medium graphs, ac '
    'hard`, or go back to the default with `/kcpc weekly rotation default`.'
)
UNREACHABLE = 'AtCoder is not responding right now. Please try again later.'
# Real seconds a healthy teardown needs, many times over. One that hangs then
# fails its test instead of stalling the whole run.
TEARDOWN_TIMEOUT = 10


def friday(day: int, month: int = 10) -> datetime:
    """The slot of Friday ``day`` (of October 2026 unless said): noon in London."""
    return datetime(2026, month, day, 12, 0, tzinfo=CLUB).astimezone(UTC)


def stamp(moment: datetime, style: str) -> str:
    return f'<t:{to_epoch(moment)}:{style}>'


def when(moment: datetime) -> str:
    return f'{stamp(moment, "F")} ({stamp(moment, "R")})'


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


# Codeforces' problems: expert 1520G, hard 1520F1, medium 1520E and 1520D, and
# easy 1520A and 4A (which the weekly problem, unlike /randproblem, never
# picks: its round is too old).
G = cf_problem(1520, 'G', 'To Go Or Not To Go?', 2200, ['dfs and similar', 'graphs'])
F1 = cf_problem(
    1520,
    'F1',
    'Guess the K-th Zero (Easy version)',
    1600,
    ['binary search', 'interactive'],
)
E = cf_problem(1520, 'E', 'Arranging The Sheep', 1400, ['greedy', 'math'])
D = cf_problem(1520, 'D', 'Same Differences', 1200, ['data structures', 'math'])
A = cf_problem(1520, 'A', 'Do Not Be Distracted!', 800, ['implementation'])
WATERMELON = cf_problem(4, 'A', 'Watermelon', 800, ['brute force', 'math'])
CODEFORCES_PROBLEMS = [G, F1, E, D, A, WATERMELON]
CONTESTS = [
    finished(1520, 'Codeforces Round 719 (Div. 3)'),
    finished(4, 'Codeforces Beta Round 4 (Div. 2 Only)'),
]
SOLVED_COUNT = 12_345  # how many solved each problem, as Codeforces says

# AtCoder's problems, with their ratings on Codeforces' scale: easy abc300_a
# (713) and abc301_a (786), medium abc300_d (1467), hard arc150_a (1770),
# expert agc060_a (2224), and a heuristic contest's, which is never picked.
ATCODER_PROBLEMS = {
    problem.problem_id: problem
    for problem in (
        AtCoderProblem('abc300_a', 'abc300', 'A', 'N-choice question', 3),
        AtCoderProblem('abc301_a', 'abc301', 'A', 'Overall Winner', 100),
        AtCoderProblem('abc300_d', 'abc300', 'D', 'AABCC', 1000),
        AtCoderProblem('arc150_a', 'arc150', 'A', 'Continuous 1', 1400),
        AtCoderProblem('agc060_a', 'agc060', 'A', 'No Majority', 2000),
        AtCoderProblem('ahc001_a', 'ahc001', 'A', 'AtCoder Ad', 1800),
    )
}
FIRST_SECOND = 1_700_000_000  # when the made-up AtCoder submissions start


def editorial_url(contest_id: str, number: int) -> str:
    return f'https://atcoder.jp/contests/{contest_id}/editorial/{number}'


def official(url: str) -> AtCoderEditorial:
    """An official English editorial of the task itself."""
    return AtCoderEditorial(url, 'Editorial', True, True, False, 'task')


def by_a_member(url: str) -> AtCoderEditorial:
    return AtCoderEditorial(url, 'Editorial', False, True, False, 'task')


def page(problem_id: str, *editorials: AtCoderEditorial) -> AtCoderEditorials:
    """The editorial page of the AtCoder task ``problem_id``."""
    return AtCoderEditorials(problem_id.rsplit('_', 1)[0], problem_id, editorials)


def submission(number: int, problem_id: str, result: str = 'AC') -> AtCoderSubmission:
    """The ``number``th made-up AtCoder submission, a second after the one before."""
    return AtCoderSubmission(
        submission_id=60_000_000 + number,
        epoch_second=FIRST_SECOND + number,
        problem_id=problem_id,
        result=result,
    )


def accepted(problem: cf.Problem) -> cf.Submission:
    """An accepted Codeforces submission of ``problem``."""
    return cf.Submission(
        id=1,
        contestId=problem.contestId,
        problem=problem,
        author=cf.Party(
            contestId=problem.contestId,
            members=[cf.Member(handle='FakeCoder')],
            participantType='PRACTICE',
            teamId=None,
            teamName=None,
            ghost=False,
            room=None,
            startTimeSeconds=None,
        ),
        programmingLanguage='C++23 (GCC 14-64, msys2)',
        verdict='OK',
        creationTimeSeconds=1_790_000_000,
        relativeTimeSeconds=2_147_483_647,
    )


class KcpcBot(commands.Bot):
    """A bot with KCPC's services and TLE's user database, as TLEBot has.

    It is in the servers ``joined``, as Discord would have said at login.
    """

    kcpc: KcpcServices | None = None
    user_db: UserDbConn | None = None
    joined = frozenset({GUILD, OTHER_GUILD})

    def get_guild(self, id: int, /) -> discord.Guild | None:
        if id not in self.joined:
            return None
        return cast(discord.Guild, MagicMock(spec=discord.Guild, id=id))


class FirstChoice(random.Random):
    """A ``random.Random`` that chooses the first of what it is offered, so
    that a problem that should have been left out is the one picked.
    """

    def choice(self, seq: Sequence[T]) -> T:
        return seq[0]


class FakeSites:
    """AtCoder Problems and AtCoder's editorial pages, as the test sets them.

    ``problems`` is AtCoder Problems' problem set, ``submissions`` each user's
    submissions by name in lower case, oldest first, and ``editorials`` each
    task's editorial page by ID: a task without one is a 404. ``errors`` makes
    a fetch raise: of 'problems', of 'submissions', or of a task's editorials
    by its ID. Once ``pages_until_stall`` pages of submissions have been sent,
    a request for another is never answered, and ``stalled`` is set.
    ``fetched`` lists every fetch: 'problems', 'submissions' or a task's ID.
    """

    def __init__(self) -> None:
        self.problems = dict(ATCODER_PROBLEMS)
        self.submissions: dict[str, list[AtCoderSubmission]] = {}
        self.editorials: dict[str, AtCoderEditorials] = {}
        self.errors: dict[str, Exception] = {}
        self.pages_until_stall: int | None = None
        self.stalled = asyncio.Event()
        self.fetched: list[str] = []

    async def fetch_problem_set(self) -> dict[str, AtCoderProblem]:
        return self._answer('problems', dict(self.problems))

    async def fetch_submissions(
        self, user: str, from_second: int
    ) -> list[AtCoderSubmission]:
        if self.pages_until_stall == 0:
            self.fetched.append('submissions')
            self.stalled.set()
            await asyncio.Event().wait()  # until cancelled
        if self.pages_until_stall is not None:
            self.pages_until_stall -= 1
        listed = self.submissions.get(user.lower(), [])
        later = [item for item in listed if item.epoch_second >= from_second]
        return self._answer('submissions', later[:SUBMISSIONS_PAGE])

    async def fetch(self, contest_id: str, task_id: str) -> AtCoderEditorials | None:
        return self._answer(task_id, self.editorials.get(task_id))

    def _answer(self, what: str, answer: T) -> T:
        self.fetched.append(what)
        error = self.errors.get(what)
        if error is not None:
            raise error
        return answer


class FakeCodeforces:
    """TLE's Codeforces client, as far as the problems feature asks it.

    ``problems`` and ``contests`` are what problemset.problems and contest.list
    give, and ``solved`` holds the problems each user solved, by handle in
    lower case: a handle it lacks is no Codeforces user's. ``errors`` makes
    'problemset' or 'status' raise. ``asked`` lists every call: 'problemset',
    'contests', or the handle that user.status was asked about. problemset
    takes ``delay`` seconds of real time, as a download of megabytes does.
    user.status sets ``status_asked``, then waits for ``status_gate`` if set.
    """

    def __init__(self) -> None:
        self.problems = list(CODEFORCES_PROBLEMS)
        self.contests = list(CONTESTS)
        self.solved: dict[str, list[cf.Problem]] = {}
        self.errors: dict[str, Exception] = {}
        self.asked: list[str] = []
        self.delay = 0.0
        self.status_asked = asyncio.Event()
        self.status_gate: asyncio.Event | None = None

    async def problemset_problems(
        self, **kwargs: object
    ) -> tuple[list[cf.Problem], list[cf.ProblemStatistics]]:
        self.asked.append('problemset')
        if self.delay:
            await asyncio.sleep(self.delay)
        self._fail('problemset')
        statistics = [
            cf.ProblemStatistics(problem.contestId, problem.index, SOLVED_COUNT)
            for problem in self.problems
        ]
        return list(self.problems), statistics

    async def contest_list(self, **kwargs: object) -> list[cf.Contest]:
        self.asked.append('contests')
        return list(self.contests)

    async def user_status(
        self, *, handle: str, from_: int | None = None, count: int | None = None
    ) -> list[cf.Submission]:
        self.asked.append(handle)
        self.status_asked.set()
        if self.status_gate is not None:
            await self.status_gate.wait()
        self._fail('status')
        if handle.lower() not in self.solved:
            comment = f'handle: User with handle {handle} not found'
            raise cf.HandleNotFoundError(comment, handle)
        return [accepted(problem) for problem in self.solved[handle.lower()]]

    def _fail(self, what: str) -> None:
        error = self.errors.get(what)
        if error is not None:
            raise error


class FakeHandles:
    """Members' AtCoder handles, as the accounts feature's service gives them."""

    def __init__(self) -> None:
        self.handles: dict[tuple[int, int], str] = {}

    async def linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        return self.handles.get((guild_id, user_id))

    async def linked_handles(
        self, guild_id: int, platform: str
    ) -> list[tuple[int, str]]:
        return sorted(
            (user_id, handle)
            for (guild, user_id), handle in self.handles.items()
            if guild == guild_id
        )


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the weekly settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
async def services(
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
) -> AsyncIterator[KcpcServices]:
    services = KcpcServices(
        settings=Settings(),
        clock=clock,
        db=db,
        http=HttpClient(user_agent='kcpc-test', clock=clock),
        features=feature_registry,
        guild_settings=guild_settings,
        ledger=ledger,
        # The weekly problem posts through the publisher itself, so the posts
        # go to the fake.
        publisher=cast(DiscordPublisher, publisher),
        reminders=ReminderEngine(guild_settings, ledger, publisher, clock),
        scheduler=Scheduler(db, clock),
    )
    yield services
    # The db fixture closes the database.
    await asyncio.wait_for(services.scheduler.stop(), TEARDOWN_TIMEOUT)
    await asyncio.wait_for(services.http.close(), TEARDOWN_TIMEOUT)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> FakeSites:
    sites = FakeSites()

    def client(http: HttpClient) -> FakeSites:
        return sites

    monkeypatch.setattr(problems_cog, 'AtCoderProblemsClient', client)
    monkeypatch.setattr(problems_cog, 'AtCoderEditorialsClient', client)
    return sites


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.problemset, 'problems', fake.problemset_problems)
    monkeypatch.setattr(cf.contest, 'to_list', fake.contest_list)
    monkeypatch.setattr(cf.user, 'status', fake.user_status)
    return fake


@pytest.fixture
def handles(services: KcpcServices) -> FakeHandles:
    """Members' AtCoder handles, readable through ``services.handles``."""
    fake = FakeHandles()
    services.handles.register('atcoder', fake)
    return fake


class SolvedWait:
    """Stands in for ``asyncio.wait_for`` where /randproblem waits for what a
    member solved: it gives up once ``stop`` is set, as if its time had run
    out, rather than after real time. ``budgets`` lists the timeouts given.
    """

    def __init__(self) -> None:
        self.stop = asyncio.Event()
        self.budgets: list[float] = []
        self._wait_for = asyncio.wait_for

    async def wait_for(self, awaitable: Awaitable[T], timeout: float) -> T:
        if getattr(awaitable, '__qualname__', None) != 'KcpcProblems._fetch_solved':
            return await self._wait_for(awaitable, timeout)
        self.budgets.append(timeout)
        lookup = asyncio.ensure_future(awaitable)
        stopping = asyncio.ensure_future(self.stop.wait())
        await asyncio.wait({lookup, stopping}, return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        if lookup.done():
            return lookup.result()
        lookup.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await lookup
        raise asyncio.TimeoutError


@pytest.fixture
def solved_wait(monkeypatch: pytest.MonkeyPatch) -> SolvedWait:
    wait = SolvedWait()
    monkeypatch.setattr(asyncio, 'wait_for', wait.wait_for)
    return wait


async def make_bot(services: KcpcServices, user_db: UserDbConn) -> KcpcBot:
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    bot.user_db = user_db
    return bot


@pytest.fixture
async def admin_bot(
    services: KcpcServices,
    user_db: UserDbConn,
    sites: FakeSites,
    codeforces: FakeCodeforces,
) -> AsyncIterator[KcpcBot]:
    """A real bot with the KCPC services and the admin cog, as at startup."""
    bot = await make_bot(services, user_db)
    await add_admin_cog(bot)
    yield bot
    await bot.close()


async def load_problems(bot: commands.Bot) -> KcpcProblems:
    """Add the problems cog as its extension does."""
    await setup(bot)
    cog = bot.get_cog('KcpcProblems')
    assert isinstance(cog, KcpcProblems)
    return cog


@pytest.fixture
async def cog(admin_bot: KcpcBot) -> KcpcProblems:
    return await load_problems(admin_bot)


@pytest.fixture
def bot(admin_bot: KcpcBot, cog: KcpcProblems) -> KcpcBot:
    """The bot with both cogs."""
    return admin_bot


@pytest.fixture
def repo(db: Database) -> WeeklyRepo:
    return WeeklyRepo(db)


def make_member(*, manage_guild: bool, user_id: int = ADMIN) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    return member


@pytest.fixture
def guild() -> MagicMock:
    return MagicMock(spec=discord.Guild, id=GUILD)


def make_ctx(guild: MagicMock, author: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = author
    ctx.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    ctx.defer = AsyncMock()
    return ctx


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    """The context of a command an admin runs; replies are recorded."""
    return make_ctx(guild, make_member(manage_guild=True))


@pytest.fixture
def member_ctx(guild: MagicMock) -> MagicMock:
    """The context of a command a member runs; replies are recorded."""
    return make_ctx(guild, make_member(manage_guild=False, user_id=MEMBER))


def make_context(
    bot: commands.Bot,
    guild: MagicMock,
    author: MagicMock,
    *,
    slash: bool = False,
    args: str = '',
) -> commands.Context[commands.Bot]:
    """A real context in ``guild``; with ``slash``, of a slash command.

    A prefix command reads its arguments from ``args``. Replies are recorded,
    and so is deferring a slash command.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(args),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
        interaction.response.defer = AsyncMock()
    context.send = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(spec=discord.Message)
    )
    return context


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


async def run(
    bot: commands.Bot,
    name: str,
    ctx: MagicMock | commands.Context[Any],
    *args: object,
    **kwargs: object,
) -> None:
    """Call the callback of the command ``name`` with parsed arguments."""
    command = command_named(bot, name)
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args, **kwargs)


async def invoke(bot: commands.Bot, name: str, ctx: commands.Context[Any]) -> None:
    """Run the command as ``Bot.invoke`` does: checks, then its arguments and
    callback, and any error to the command's error handlers.
    """
    command = command_named(bot, name)
    ctx.command = command
    try:
        await command.invoke(ctx)
    except commands.CommandError as error:
        await command.dispatch_error(ctx, error)


def reply(ctx: MagicMock | commands.Context[Any], **kwargs: object) -> discord.Embed:
    """The embed of the one reply, sent with ``kwargs`` besides."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, **kwargs)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def fields(embed: discord.Embed) -> list[tuple[str | None, str | None]]:
    return [(field.name, field.value) for field in embed.fields]


def cog_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == COG_LOGGER]


def titles(publisher: FakePublisher) -> list[str | None]:
    return [post.message.title for post in publisher.posts]


def keys(publisher: FakePublisher) -> list[str]:
    return [key for post in publisher.posts for key in post.keys]


def interaction_in(guild_id: int) -> MagicMock:
    """An autocomplete request from a member of ``guild_id``."""
    return MagicMock(spec=discord.Interaction, guild_id=guild_id)


async def set_up(
    guild_settings: GuildSettingsRepo,
    *rotation: str,
    guild_id: int = GUILD,
    enabled: bool = True,
    channel_id: int | None = CHANNEL,
) -> None:
    """Turn the weekly problem on in the guild, with ``rotation`` if given."""
    await guild_settings.update(
        guild_id, WEEKLY, enabled=enabled, channel_id=channel_id, rotation=rotation
    )


async def load_lists(services: KcpcServices) -> None:
    """Load both platforms' problem lists, as the refresh job does at start."""
    await services.scheduler.run_slot(REFRESH_JOB)


def next_runs(services: KcpcServices) -> dict[str, datetime | None]:
    return {job.name: job.next_run for job in services.scheduler.status()}


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 10 s of real time) until ``condition()`` holds."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


def queued(problem: cf.Problem) -> QueuedProblem:
    """``problem`` as ADMIN queued it for GUILD."""
    assert problem.contestId is not None
    return QueuedProblem(
        guild_id=GUILD,
        source='codeforces',
        problem_id=f'{problem.contestId}{problem.index}',
        contest_id=str(problem.contestId),
        index=problem.index,
        name=problem.name,
        url=f'https://codeforces.com/contest/{problem.contestId}/problem/{problem.index}',
        difficulty=problem.rating,
        band=None,
        solution_url=None,
        queued_by=ADMIN,
        queued_at=NOW,
    )


def weekly_row(weeks_ago: int, **changes: Any) -> WeeklyProblem:
    """A Codeforces problem of GUILD, ``weeks_ago`` weeks before the 2026-09-25
    slot: 1500A for that week, 1501A for the one before, and so on.
    """
    slot = (
        datetime(2026, 9, 25, 12, 0, tzinfo=CLUB) - timedelta(weeks=weeks_ago)
    ).astimezone(UTC)
    contest_id = str(1500 + weeks_ago)
    row = WeeklyProblem(
        guild_id=GUILD,
        slot=slot,
        week=slot.astimezone(CLUB).date().isoformat(),
        source='codeforces',
        problem_id=f'{contest_id}A',
        contest_id=contest_id,
        index='A',
        name=f'Problem {weeks_ago}',
        url=f'https://codeforces.com/contest/{contest_id}/problem/A',
        topic=None,
        difficulty=800,
        band='easy',
        selection='auto',
        date_selected=slot,
        solution_url=None,
        solution_set_by=None,
        solution_posted=False,
        solution_posted_at=None,
    )
    return replace(row, **changes)


async def posted(
    repo: WeeklyRepo, publisher: FakePublisher, row: WeeklyProblem
) -> WeeklyProblem:
    """Store ``row`` and record its problem as posted, as a run would."""
    stored = await repo.create(row)
    delivery = Delivery(
        problem_key(row.guild_id, row.week),
        row.guild_id,
        WEEKLY,
        subject=SUBJECT,
        subject_id=row.week,
        kind=PROBLEM,
        occurrence_start=row.slot,
    )
    result = await publisher.publish([delivery], OutgoingMessage(title='Earlier'))
    assert result.outcome is PublishOutcome.SENT
    return stored


async def test_loading_adds_the_commands_and_the_jobs(
    bot: KcpcBot, cog: KcpcProblems, services: KcpcServices
) -> None:
    jobs = [
        (job.name, job.description, job.persistent)
        for job in services.scheduler.status()
    ]
    assert jobs == [
        ('problems.refresh', 'every 30m', False),
        ('weekly.post', 'every Friday at 12:00 (Europe/London)', True),
    ]
    # It posts by itself, not through the reminder engine.
    assert services.reminders.features == []

    # /randproblem and /weekly for members, kept out of DMs; and
    # ;weekly current, which the slash command's fallback doesn't give
    # prefix commands.
    randproblem = bot.tree.get_command('randproblem')
    assert isinstance(randproblem, app_commands.Command)
    assert randproblem.guild_only
    members = bot.tree.get_command('weekly')
    assert isinstance(members, app_commands.Group)
    assert members.guild_only
    assert sorted(command.name for command in members.commands) == [
        'current',
        'history',
    ]
    group = command_named(bot, 'weekly')
    assert isinstance(group, commands.HybridGroup)
    assert sorted(group.all_commands) == ['current', 'history']
    current = command_named(bot, 'weekly current')
    assert isinstance(current, commands.HybridCommand)
    assert current.app_command is None

    # /kcpc weekly, for admins, on both paths and nowhere else.
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    admin = kcpc.get_command('weekly')
    assert isinstance(admin, app_commands.Group)
    assert sorted(command.name for command in admin.commands) == ADMIN_COMMANDS
    for name in ADMIN_COMMANDS:
        assert command_named(bot, f'kcpc weekly {name}').cog is cog
    assert set(bot.all_commands) == {'help', 'kcpc', 'randproblem', 'weekly'}
    assert {command.name for command in bot.tree.get_commands()} == {
        'kcpc',
        'randproblem',
        'weekly',
    }


async def test_the_lists_load_at_start_and_the_first_post_waits_for_friday(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    sites: FakeSites,
    codeforces: FakeCodeforces,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')

    services.scheduler.start()

    # The lists load at once; a fresh install posts nothing until Friday.
    await eventually(
        lambda: next_runs(services)
        == {REFRESH_JOB: NOW + 30 * MINUTE, WEEKLY_JOB: friday(2)},
        'the jobs wait for their next slots',
    )
    assert sites.fetched == ['problems']
    assert codeforces.asked == ['problemset', 'contests']
    assert publisher.posts == []

    # Each list is fetched again only once it is older than its max age.
    await clock.advance(30 * MINUTE)

    assert sites.fetched == ['problems']
    assert codeforces.asked == ['problemset', 'contests']

    await clock.advance_to(friday(2))

    await eventually(
        lambda: next_runs(services)[WEEKLY_JOB] == friday(9),
        'the weekly job waits for the Friday after',
    )
    assert titles(publisher) == ['Weekly problem: 1520G - To Go Or Not To Go?']
    assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]
    # Codeforces' list was 6 hours old at 18:00, and AtCoder's is not 24 yet.
    assert codeforces.asked.count('problemset') == 4
    assert sites.fetched == ['problems']


@pytest.mark.parametrize(
    ('late', 'posts'),
    [(6 * HOUR - SECOND, True), (6 * HOUR + SECOND, False)],
    ids=['within 6 hours', 'later'],
)
async def test_a_friday_missed_while_the_bot_was_down_is_posted_within_6_hours(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
    late: timedelta,
    posts: bool,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    services.scheduler.start()
    await eventually(
        lambda: next_runs(services)[WEEKLY_JOB] == friday(2),
        'the weekly job waits for Friday',
    )
    await services.scheduler.stop()

    await clock.advance_to(friday(2) + late)  # the bot was down at noon
    services.scheduler.start()

    await eventually(
        lambda: next_runs(services)[WEEKLY_JOB] == friday(9),
        'the weekly job waits for the Friday after',
    )
    expected = ['Weekly problem: 1520G - To Go Or Not To Go?'] if posts else []
    assert titles(publisher) == expected
    assert (await repo.get(GUILD, friday(2)) is not None) is posts


async def test_a_friday_caught_up_after_a_restart_waits_for_the_lists(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    codeforces: FakeCodeforces,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    services.scheduler.start()
    await eventually(
        lambda: next_runs(services)[WEEKLY_JOB] == friday(2),
        'the weekly job waits for Friday',
    )
    await services.scheduler.stop()
    # A restart, 4 minutes before the grace ends: a new cog has no list
    # loaded, and Codeforces' takes a while to download.
    await bot.remove_cog('KcpcProblems')
    await clock.advance_to(friday(2) + 6 * HOUR - 4 * MINUTE)
    codeforces.delay = 0.2
    await load_problems(bot)

    services.scheduler.start()

    await eventually(
        lambda: next_runs(services).get(WEEKLY_JOB) == friday(9),
        'the weekly job waits for the Friday after',
    )
    assert titles(publisher) == ['Weekly problem: 1520G - To Go Or Not To Go?']
    [weekly] = [job for job in services.scheduler.status() if job.name == WEEKLY_JOB]
    assert weekly.failures == 0  # on its first try


async def test_removing_the_cog_undoes_everything_loading_did(
    bot: KcpcBot, services: KcpcServices
) -> None:
    await bot.remove_cog('KcpcProblems')

    assert services.scheduler.status() == []
    assert bot.get_command('kcpc weekly') is None
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('weekly') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}

    # So the extension can be loaded again.
    again = await load_problems(bot)
    assert command_named(bot, 'kcpc weekly post-now').cog is again
    assert command_named(bot, 'weekly history').cog is again
    assert command_named(bot, 'randproblem').cog is again
    assert len(services.scheduler.status()) == 2


async def other_weekly(ctx: commands.Context[Any]) -> None:
    """Another /kcpc weekly."""


def assert_nothing_left_by_a_failed_load(bot: commands.Bot) -> None:
    assert bot.get_cog('KcpcProblems') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}


async def test_a_load_that_cannot_attach_the_admin_commands_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    kcpc = command_named(admin_bot, 'kcpc')
    assert isinstance(kcpc, commands.HybridGroup)
    clashing: commands.HybridGroup[Any, ..., Any] = commands.hybrid_group(
        name='weekly'
    )(other_weekly)
    kcpc.add_command(clashing)

    with pytest.raises(commands.CommandRegistrationError):
        await load_problems(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc weekly') is clashing
    assert services.scheduler.status() == []


async def test_a_load_that_cannot_add_every_job_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    # The refresh job is added before it.
    services.scheduler.add(
        ScheduledJob(WEEKLY_JOB, Every(HOUR), other, persistent=False)
    )

    with pytest.raises(ValueError, match='already scheduled'):
        await load_problems(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert [job.name for job in services.scheduler.status()] == [WEEKLY_JOB]
    assert admin_bot.get_command('kcpc weekly') is None
    kcpc = admin_bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('weekly') is None


async def test_without_the_admin_cog_the_feature_runs_without_its_admin_commands(
    services: KcpcServices,
    user_db: UserDbConn,
    sites: FakeSites,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = await make_bot(services, user_db)
    try:
        with caplog.at_level(logging.INFO, logger=ADMIN_LOGGER):
            await load_problems(bot)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == ADMIN_LOGGER
        ] == ['Not adding /kcpc weekly: the kcpc.admin extension is not loaded']
        # Not a top-level command that every member would be shown.
        assert set(bot.all_commands) == {'help', 'randproblem', 'weekly'}
        assert {command.name for command in bot.tree.get_commands()} == {
            'randproblem',
            'weekly',
        }
        assert len(services.scheduler.status()) == 2

        await bot.remove_cog('KcpcProblems')

        assert set(bot.all_commands) == {'help'}
        assert services.scheduler.status() == []
    finally:
        await bot.close()


async def test_randproblem_picks_a_problem_of_the_topic_and_difficulty(
    bot: KcpcBot, member_ctx: MagicMock, services: KcpcServices
) -> None:
    await load_lists(services)

    await run(bot, 'randproblem', member_ctx, ' Graphs ', 'expert')

    # Everyone sees it, with its tags hidden until clicked.
    embed = reply(member_ctx)
    assert embed.title == '1520G - To Go Or Not To Go?'
    assert embed.url == 'https://codeforces.com/contest/1520/problem/G'
    assert embed.description == '\n'.join(
        [
            '**Difficulty:** 2200 (expert)',
            '**Topics:** ||dfs and similar, graphs||',
            '**Solved by:** 12345 people',
        ]
    )
    assert embed.colour == discord.Colour(KCPC_COLOR)
    assert embed.footer.text is None  # the member linked no account
    cast(AsyncMock, member_ctx.defer).assert_awaited_once_with()


@pytest.mark.parametrize(
    ('platform', 'difficulty', 'title', 'lines'),
    [
        (
            'codeforces',
            '1600',
            '1520F1 - Guess the K-th Zero (Easy version)',
            [
                '**Difficulty:** 1600 (hard)',
                '**Topics:** ||binary search, interactive||',
                '**Solved by:** 12345 people',
            ],
        ),
        # Nothing is rated 1700 to 1900, but 1520F1 is within 200.
        (
            'codeforces',
            '1800',
            '1520F1 - Guess the K-th Zero (Easy version)',
            [
                '**Difficulty:** 1600 (hard)',
                '**Topics:** ||binary search, interactive||',
                '**Solved by:** 12345 people',
                'Nothing left was rated 1800, so this is one of those within 200 '
                'of it.',
            ],
        ),
        # Within 50 of the rating asked for, as close as AtCoder's come.
        (
            'atcoder',
            '1500',
            'ABC300 D - AABCC',
            ['**Difficulty:** 1000 on AtCoder (about 1467 on Codeforces, medium)'],
        ),
        (
            'atcoder',
            '1700',
            'ARC150 A - Continuous 1',
            [
                '**Difficulty:** 1400 on AtCoder (about 1770 on Codeforces, hard)',
                'Nothing left was rated 1700, so this is one of those within 100 '
                'of it.',
            ],
        ),
    ],
    ids=['rating', 'widened', 'atcoder rating', 'atcoder widened'],
)
async def test_randproblem_takes_a_rating_and_says_when_it_had_to_widen_it(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    platform: str,
    difficulty: str,
    title: str,
    lines: list[str],
) -> None:
    await load_lists(services)

    await run(bot, 'randproblem', member_ctx, 'any', difficulty, platform)

    embed = reply(member_ctx)
    assert embed.title == title
    assert embed.description == '\n'.join(lines)


async def test_randproblem_takes_an_atcoder_problem_50_away_as_near_enough(
    bot: KcpcBot, member_ctx: MagicMock, services: KcpcServices, sites: FakeSites
) -> None:
    # About 1650 on Codeforces' scale: AtCoder's first window holds it.
    sites.problems['arc151_a'] = AtCoderProblem(
        'arc151_a', 'arc151', 'A', 'Equal Hamming Distances', 1241
    )
    await load_lists(services)

    await run(bot, 'randproblem', member_ctx, 'any', '1600', 'atcoder')

    embed = reply(member_ctx)
    assert embed.title == 'ARC151 A - Equal Hamming Distances'
    assert embed.description == (
        '**Difficulty:** 1241 on AtCoder (about 1650 on Codeforces, hard)'
    )


@pytest.mark.parametrize(
    'solved',
    [[A], [cf_problem(1521, 'A', 'Do Not Be Distracted!', 800, ['implementation'])]],
    ids=['by its ID', 'as the same problem in a Div. 2 round'],
)
async def test_randproblem_leaves_out_the_codeforces_problems_a_member_solved(
    bot: KcpcBot,
    cog: KcpcProblems,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
    solved: list[cf.Problem],
) -> None:
    cog._rng = FirstChoice()  # 1520A, unless it is left out
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = solved

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    # Of 1520A and 4A, only 4A is left.
    embed = reply(member_ctx)
    assert embed.title == '4A - Watermelon'
    assert embed.footer.text == SOLVED_AS.format(handle='FakeCoder')
    assert codeforces.asked[-1] == 'FakeCoder'


async def test_randproblem_leaves_out_the_atcoder_problems_a_member_solved(
    bot: KcpcBot,
    cog: KcpcProblems,
    member_ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    handles: FakeHandles,
) -> None:
    cog._rng = FirstChoice()  # abc300_a, unless it is left out
    await load_lists(services)
    handles.handles[GUILD, MEMBER] = 'Fake_AtCoder'
    sites.submissions['fake_atcoder'] = [
        submission(1, 'abc301_a', 'WA'),
        submission(2, 'ABC300_A'),  # IDs match whatever their case
    ]

    await run(bot, 'randproblem', member_ctx, 'any', 'easy', 'atcoder')

    embed = reply(member_ctx)
    assert embed.title == 'ABC301 A - Overall Winner'
    assert embed.footer.text == SOLVED_AS.format(handle='Fake_AtCoder')


async def test_randproblem_uses_what_it_read_of_a_long_atcoder_history_in_time(
    bot: KcpcBot,
    cog: KcpcProblems,
    member_ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    handles: FakeHandles,
    solved_wait: SolvedWait,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cog._rng = FirstChoice()  # abc300_a, unless it is left out
    solved_wait.stop = sites.stalled  # time runs out on the second page
    await load_lists(services)
    handles.handles[GUILD, MEMBER] = 'Fake_AtCoder'
    # A full first page, with abc300_a solved; the second never comes.
    sites.submissions['fake_atcoder'] = [submission(0, 'abc300_a')] + [
        submission(number, 'abc301_a', 'WA') for number in range(1, SUBMISSIONS_PAGE)
    ]
    sites.pages_until_stall = 1

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        await run(bot, 'randproblem', member_ctx, 'any', 'easy', 'atcoder')

    embed = reply(member_ctx)
    assert embed.title == 'ABC301 A - Overall Winner'
    assert embed.footer.text == NOT_ALL_CHECKED
    assert sites.fetched[-2:] == ['submissions', 'submissions']
    assert solved_wait.budgets == [10.0]
    assert cog_messages(caplog) == [
        'Gave up reading the problems that AtCoder user Fake_AtCoder solved after '
        '10 seconds'
    ]


@pytest.mark.parametrize(
    ('platform', 'failure'),
    [
        ('codeforces', 'down'),
        ('codeforces', 'no such user'),
        ('atcoder', 'down'),
        ('atcoder', 'stalled'),
    ],
)
async def test_randproblem_still_picks_when_the_solved_problems_cant_be_read(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    codeforces: FakeCodeforces,
    handles: FakeHandles,
    user_db: UserDbConn,
    platform: str,
    failure: str,
) -> None:
    monkeypatch.setattr(problems_cog, '_SOLVED_WAIT', 0.05)
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    handles.handles[GUILD, MEMBER] = 'Fake_AtCoder'
    codeforces.solved['fakecoder'] = [G]
    sites.submissions['fake_atcoder'] = [submission(1, 'agc060_a')]
    if failure == 'down':
        codeforces.errors['status'] = cf.CodeforcesApiError('boom')
        sites.errors['submissions'] = ExternalServiceError('AtCoder Problems', 'down')
    elif failure == 'no such user':
        del codeforces.solved['fakecoder']
    else:
        sites.pages_until_stall = 0

    await run(bot, 'randproblem', member_ctx, 'any', 'expert', platform)

    # Nothing was left out: each was solved, but the bot couldn't tell.
    embed = reply(member_ctx)
    expected = {
        'codeforces': '1520G - To Go Or Not To Go?',
        'atcoder': 'AGC060 A - No Majority',
    }
    assert embed.title == expected[platform]
    assert embed.footer.text == NOT_ALL_CHECKED


async def test_randproblem_matches_names_only_of_problems_codeforces_doesnt_list(
    bot: KcpcBot,
    cog: KcpcProblems,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
) -> None:
    # Round 1500 has a problem of 1520A's name, but it is another problem.
    namesake = cf_problem(1500, 'A', 'Do Not Be Distracted!', 800, ['math'])
    codeforces.problems.append(namesake)
    codeforces.contests.append(finished(1500, 'Codeforces Round 707 (Div. 1)'))
    cog._rng = FirstChoice()  # 1520A, unless it is left out
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = [namesake]

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    assert reply(member_ctx).title == '1520A - Do Not Be Distracted!'


async def test_randproblem_leaves_codeforces_alone_for_a_while_after_it_fails(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
) -> None:
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = [A]
    codeforces.errors['status'] = cf.CodeforcesApiError('Call limit exceeded')
    await run(bot, 'randproblem', member_ctx, 'any', 'easy')
    del codeforces.errors['status']
    cast(AsyncMock, member_ctx.send).reset_mock()

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    assert codeforces.asked.count('FakeCoder') == 1
    assert reply(member_ctx).footer.text == NOT_ALL_CHECKED
    cast(AsyncMock, member_ctx.send).reset_mock()

    await clock.advance(CODEFORCES_PAUSE)
    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    assert codeforces.asked.count('FakeCoder') == 2
    assert reply(member_ctx).footer.text == SOLVED_AS.format(handle='FakeCoder')


async def test_a_slow_codeforces_lookup_is_kept_for_the_next_randproblem(
    bot: KcpcBot,
    cog: KcpcProblems,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
    solved_wait: SolvedWait,
) -> None:
    cog._rng = FirstChoice()  # 1520A, unless it is left out
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = [A]
    codeforces.status_gate = asyncio.Event()
    solved_wait.stop = codeforces.status_asked  # a long history

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    # Its time ran out, so nothing was left out, but the lookup went on.
    assert reply(member_ctx).footer.text == NOT_ALL_CHECKED
    assert reply(member_ctx).title == '1520A - Do Not Be Distracted!'
    cast(AsyncMock, member_ctx.send).reset_mock()
    solved_wait.stop = asyncio.Event()
    codeforces.status_gate.set()

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    assert reply(member_ctx).title == '4A - Watermelon'
    assert reply(member_ctx).footer.text == SOLVED_AS.format(handle='FakeCoder')
    assert codeforces.asked.count('FakeCoder') == 1
    assert solved_wait.budgets == [10.0, 10.0]


async def test_removing_the_cog_stops_the_codeforces_lookups_still_running(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
    solved_wait: SolvedWait,
) -> None:
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = []
    codeforces.status_gate = asyncio.Event()  # never answered
    solved_wait.stop = codeforces.status_asked
    await run(bot, 'randproblem', member_ctx, 'any', 'easy')
    [lookup] = [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), '__qualname__', None)
        == 'SolvedProblems._fetch_codeforces'
    ]

    await bot.remove_cog('KcpcProblems')

    assert lookup.cancelled()


@pytest.mark.parametrize(
    ('args', 'error'),
    [
        (
            ('graphs', 'easy', 'atcoder'),
            'AtCoder problems have no topics: use any with platform atcoder.',
        ),
        (('any', 'hardest'), BAD_DIFFICULTY),
        (('any', '3550'), BAD_DIFFICULTY),  # 3600, rounded half up
        (
            ('graphs', 'easy'),
            "There's no Codeforces problem about graphs at easy difficulty.",
        ),
        (
            ('any', '3500', 'atcoder'),
            "There's no AtCoder problem at a rating near 3500.",
        ),
    ],
)
async def test_randproblem_says_what_it_cant_do(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    args: tuple[str, ...],
    error: str,
) -> None:
    await load_lists(services)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'randproblem', member_ctx, *args)

    assert str(raised.value) == error
    cast(AsyncMock, member_ctx.send).assert_not_awaited()


async def test_randproblem_answers_an_unknown_topic_privately(
    bot: KcpcBot, cog: KcpcProblems, guild: MagicMock, services: KcpcServices
) -> None:
    await load_lists(services)
    member = make_member(manage_guild=False, user_id=MEMBER)
    ctx = make_context(bot, guild, member, slash=True)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'randproblem', ctx, 'grafs', 'easy')
    # As discord.py hands the error over.
    await cog.cog_command_error(ctx, raised.value)

    # Not deferred, so the alert is only the member's to see: after a public
    # defer, Discord would show it to everyone.
    assert ctx.interaction is not None
    cast(AsyncMock, ctx.interaction.response.defer).assert_not_awaited()
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description is not None
    assert embed.description.startswith("There's no topic called 'grafs'.")


async def test_randproblem_suggests_close_topics_for_an_unknown_one(
    bot: KcpcBot, member_ctx: MagicMock, services: KcpcServices
) -> None:
    await load_lists(services)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'randproblem', member_ctx, 'grafs', 'easy')

    assert str(raised.value).startswith("There's no topic called 'grafs'. Did you mean")
    assert 'graphs' in str(raised.value)


async def test_randproblem_says_when_nothing_unsolved_is_left(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
) -> None:
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = [G]

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'randproblem', member_ctx, 'graphs', 'expert')

    assert str(raised.value) == (
        "There's no Codeforces problem about graphs at expert difficulty that you "
        "haven't solved."
    )


@pytest.mark.parametrize(
    ('platform', 'owner'), [('codeforces', "Codeforces'"), ('atcoder', "AtCoder's")]
)
async def test_randproblem_waits_for_the_platforms_list_to_load(
    bot: KcpcBot, member_ctx: MagicMock, platform: str, owner: str
) -> None:
    # The refresh job hasn't run yet.
    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'randproblem', member_ctx, 'any', 'easy', platform)

    assert str(raised.value) == NOT_LOADED.format(platform=owner)


async def test_randproblem_suggests_topics_and_difficulties(
    bot: KcpcBot, cog: KcpcProblems, services: KcpcServices
) -> None:
    command = command_named(bot, 'randproblem')
    assert isinstance(command, commands.HybridCommand)
    assert isinstance(command.app_command, app_commands.Command)
    for name in ('topic', 'difficulty'):
        parameter = command.app_command.get_parameter(name)
        assert parameter is not None and parameter.autocomplete
    await load_lists(services)
    interaction = interaction_in(GUILD)

    topics = await cog.topic_autocomplete(interaction, 'gra')
    difficulties = await cog.difficulty_autocomplete(interaction, '')
    expert = await cog.difficulty_autocomplete(interaction, 'EXP')

    assert [choice.value for choice in topics] == ['graphs', 'graph matchings']
    assert len(difficulties) == 25
    assert [(choice.name, choice.value) for choice in expert] == [
        ('expert (2000 and up)', 'expert')
    ]


async def test_randproblem_defers_publicly_before_asking_any_site(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    codeforces: FakeCodeforces,
    user_db: UserDbConn,
) -> None:
    # Reading what a member solved can take seconds, longer than Discord
    # waits for a slash command's first answer.
    await load_lists(services)
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    codeforces.solved['fakecoder'] = []
    asked_before = list(codeforces.asked)
    when_deferred: list[list[str]] = []
    cast(AsyncMock, member_ctx.defer).side_effect = lambda: when_deferred.append(
        list(codeforces.asked)
    )

    await run(bot, 'randproblem', member_ctx, 'any', 'easy')

    cast(AsyncMock, member_ctx.defer).assert_awaited_once_with()
    assert when_deferred == [asked_before]
    assert codeforces.asked[-1] == 'FakeCoder'
    reply(member_ctx)


async def test_randproblem_works_as_a_prefix_command_too(
    bot: KcpcBot, guild: MagicMock, services: KcpcServices
) -> None:
    await load_lists(services)
    ctx = make_context(
        bot,
        guild,
        make_member(manage_guild=False, user_id=MEMBER),
        args='"binary search" hard',
    )

    await invoke(bot, 'randproblem', ctx)

    assert reply(ctx).title == '1520F1 - Guess the K-th Zero (Easy version)'


async def test_weekly_shows_this_weeks_problem_and_when_its_solution_comes(
    bot: KcpcBot,
    ctx: MagicMock,
    member_ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:graphs')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)

    await run(bot, 'weekly', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Weekly problem: 1520G - To Go Or Not To Go?'
    assert embed.url == 'https://codeforces.com/contest/1520/problem/G'
    assert embed.description == '\n'.join(
        [
            '**Platform:** Codeforces',
            '**Difficulty:** 2200 (expert)',
            '**Topic:** graphs',
            f'**Posted:** {when(NOW)}',  # by post-now, after its slot
            f'**Solution:** {when(friday(2))}',
        ]
    )
    assert embed.footer.text == 'KCPC weekly problem'


async def test_weekly_shows_the_solution_links_once_it_is_out(
    bot: KcpcBot,
    ctx: MagicMock,
    member_ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    sites: FakeSites,
) -> None:
    await set_up(guild_settings, 'atcoder:medium:any')
    sites.editorials['abc300_d'] = page(
        'abc300_d', official(editorial_url('abc300', 6076))
    )
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    # The solution's time has come, though the server didn't get its post.
    await clock.advance_to(friday(2))

    await run(bot, 'weekly current', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Weekly problem: ABC300 D - AABCC'
    assert embed.description is not None
    assert embed.description.splitlines()[1:] == [
        '**Difficulty:** 1000 on AtCoder (about 1467 on Codeforces, medium)',
        f'**Posted:** {when(NOW)}',
        '**Solution:** [Editorial](https://atcoder.jp/contests/abc300/editorial/6076)'
        ' · [All editorials](https://atcoder.jp/contests/abc300/tasks/abc300_d/'
        'editorial?lang=en)',
    ]


async def test_weekly_gives_the_solution_time_across_the_clock_change(
    bot: KcpcBot,
    member_ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Friday noon in London is 11:00 UTC on 2026-10-23, and 12:00 UTC on the
    # 30th, after the clocks go back.
    assert (friday(23).hour, friday(30).hour) == (11, 12)
    await set_up(guild_settings)
    row = replace(
        weekly_row(0), slot=friday(23), week='2026-10-23', date_selected=friday(23)
    )
    await clock.advance_to(friday(23) + HOUR)
    await posted(repo, publisher, row)

    await run(bot, 'weekly', member_ctx)

    description = reply(member_ctx).description
    assert description is not None
    assert description.splitlines()[-1] == f'**Solution:** {when(friday(30))}'


async def test_weekly_says_when_nothing_has_been_posted(
    bot: KcpcBot, member_ctx: MagicMock
) -> None:
    await run(bot, 'weekly', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Weekly problem'
    assert embed.description == 'No weekly problem has been posted here yet.'


async def test_weekly_history_lists_the_weeks_newest_first_ten_a_page(
    bot: KcpcBot,
    member_ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(0))
    await posted(
        repo,
        publisher,
        weekly_row(1, solution_url=BLOG, solution_posted=True, solution_posted_at=NOW),
    )
    for weeks_ago in range(2, 12):
        await posted(repo, publisher, weekly_row(weeks_ago))
    await repo.create(weekly_row(12))  # whose problem never went out

    await run(bot, 'weekly history', member_ctx)

    send = cast(AsyncMock, member_ctx.send)
    send.assert_awaited_once_with(embed=ANY, view=ANY, ephemeral=False)
    assert send.await_args is not None
    view = send.await_args.kwargs['view']
    assert isinstance(view, PageView)
    first, second = view.pages
    assert first.title == 'Weekly problems'
    assert first.description is not None and second.description is not None
    lines = first.description.splitlines()
    assert len(lines) == 10
    assert lines[:3] == [
        '`2026-09-25` [1500A - Problem 0](https://codeforces.com/contest/1500/problem/A)'
        f' · solution {stamp(friday(2), "R")}',
        '`2026-09-18` [1501A - Problem 1](https://codeforces.com/contest/1501/problem/A)'
        f' · [Editorial]({BLOG})',
        # Its solution's time has come, though it wasn't posted.
        '`2026-09-11` [1502A - Problem 2](https://codeforces.com/contest/1502/problem/A)'
        ' · [Contest materials](https://codeforces.com/contest/1502)',
    ]
    assert [line.split()[0] for line in second.description.splitlines()] == [
        '`2026-07-17`',
        '`2026-07-10`',
    ]
    assert first.footer.text == 'Page 1 of 2'
    assert second.footer.text == 'Page 2 of 2'


async def test_weekly_history_without_any_week_says_so(
    bot: KcpcBot, member_ctx: MagicMock
) -> None:
    await run(bot, 'weekly history', member_ctx)

    embed = reply(member_ctx, ephemeral=False)
    assert embed.title == 'Weekly problems'
    assert embed.description == 'No weekly problem has been posted here yet.'


@pytest.mark.parametrize(
    ('args', 'title', 'kwargs'),
    [
        ('', 'Weekly problem', {}),
        ('current', 'Weekly problem', {}),
        ('history', 'Weekly problems', {'ephemeral': False}),
    ],
)
async def test_weekly_works_as_a_prefix_command_too(
    bot: KcpcBot, guild: MagicMock, args: str, title: str, kwargs: dict[str, object]
) -> None:
    ctx = make_context(
        bot, guild, make_member(manage_guild=False, user_id=MEMBER), args=args
    )

    await invoke(bot, 'weekly', ctx)

    assert reply(ctx, **kwargs).title == title


@pytest.mark.parametrize(
    ('name', 'args'),
    [
        ('randproblem', ('any', 'easy')),
        ('weekly', ()),
        ('weekly current', ()),
        ('weekly history', ()),
    ],
)
async def test_member_commands_need_a_server(
    bot: KcpcBot, member_ctx: MagicMock, name: str, args: tuple[str, ...]
) -> None:
    member_ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await run(bot, name, member_ctx, *args)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize(
    'name', ['randproblem', 'weekly', 'weekly current', 'weekly history']
)
async def test_member_commands_are_for_everyone(
    bot: KcpcBot, guild: MagicMock, name: str, slash: bool
) -> None:
    member = make_context(
        bot, guild, make_member(manage_guild=False, user_id=MEMBER), slash=slash
    )

    assert await command_named(bot, name).can_run(member)


async def test_post_now_posts_the_weeks_problem_once(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)

    await run(bot, 'kcpc weekly post-now', ctx)

    assert titles(publisher) == ['Weekly problem: 1520G - To Go Or Not To Go?']
    assert keys(publisher) == [problem_key(GUILD, '2026-09-25')]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        "Posted this week's problem, **1520G - To Go Or Not To Go?**."
    )
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly post-now', ctx)

    assert len(publisher.posts) == 1
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        "This week's problem, **1520G - To Go Or Not To Go?**, is already posted."
    )


async def test_post_now_posts_last_weeks_solution_then_the_new_problem(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
) -> None:
    # Across the clock change: Friday noon is 11:00 UTC on 2026-10-23, and
    # 12:00 UTC on 2026-10-30.
    assert (friday(23).hour, friday(30).hour) == (11, 12)
    await set_up(guild_settings, 'codeforces:expert:any', 'codeforces:hard:any')
    await clock.advance_to(datetime(2026, 10, 23, 13, 0, tzinfo=UTC))
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await clock.advance_to(datetime(2026, 10, 30, 12, 30, tzinfo=UTC))
    await run(bot, 'kcpc weekly post-now', ctx)

    assert keys(publisher) == [
        problem_key(GUILD, '2026-10-23'),
        solution_key(GUILD, '2026-10-23'),
        problem_key(GUILD, '2026-10-30'),
    ]
    assert titles(publisher)[1:] == [
        'Solution: 1520G - To Go Or Not To Go?',
        'Weekly problem: 1520F1 - Guess the K-th Zero (Easy version)',
    ]
    # No admin set a link, so the contest page, which lists the editorial.
    assert publisher.posts[1].message.description == '\n'.join(
        [
            "Last week's problem: [1520G - To Go Or Not To Go?]"
            '(https://codeforces.com/contest/1520/problem/G)',
            'Codeforces lists the editorial under **Contest materials** on '
            '[the contest page](https://codeforces.com/contest/1520).',
        ]
    )
    row = await repo.get(GUILD, friday(23))
    assert row is not None and row.solution_posted
    assert reply(ctx, ephemeral=True).description == '\n'.join(
        [
            'Posted the solution of **1520G - To Go Or Not To Go?**.',
            "Posted this week's problem, **1520F1 - Guess the K-th Zero "
            '(Easy version)**.',
        ]
    )


async def test_post_now_posts_a_queued_problem_before_the_rotation(
    bot: KcpcBot,
    cog: KcpcProblems,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly queue', ctx, '1520D')
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly post-now', ctx)

    assert titles(publisher) == ['Weekly problem: 1520D - Same Differences']
    # It leaves the queue, and unqueue no longer suggests it.
    assert await repo.queue(GUILD) == []
    assert await cog.queued_autocomplete(interaction_in(GUILD), '') == []


@pytest.mark.parametrize(
    ('enabled', 'channel_id'), [(False, CHANNEL), (True, None)], ids=['off', 'channel']
)
async def test_post_now_says_how_to_set_the_feature_up(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
    enabled: bool,
    channel_id: int | None,
) -> None:
    await set_up(guild_settings, enabled=enabled, channel_id=channel_id)
    await load_lists(services)

    await run(bot, 'kcpc weekly post-now', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NOT_SET_UP
    assert publisher.posts == []
    assert await repo.latest(GUILD, at_or_before=NOW) is None


@pytest.mark.parametrize(
    ('failure', 'text'),
    [
        (
            'guild-unavailable',
            "Couldn't post this week's problem, **1520G - To Go Or Not To Go?**: "
            "Discord hasn't sent this server's channels yet. Please try again in a "
            'minute.',
        ),
        (
            'channel-missing',
            "Couldn't post this week's problem, **1520G - To Go Or Not To Go?**: "
            '`channel-missing`. Check the channel and my permissions there, e.g. '
            'with `/kcpc channel weekly #channel`.',
        ),
        (
            PublishOutcome.PENDING,
            "Posted this week's problem, **1520G - To Go Or Not To Go?**, but "
            "Discord didn't confirm it; I'll check within a few minutes.",
        ),
        (
            PublishOutcome.SKIPPED,
            "Discord refused to post this week's problem, **1520G - To Go Or Not To "
            'Go?**: `discord-403`.',
        ),
    ],
    ids=['guild unavailable', 'channel missing', 'pending', 'skipped'],
)
async def test_post_now_says_what_kept_a_post_from_going_out(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    failure: str | PublishOutcome,
    text: str,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    if isinstance(failure, PublishOutcome):
        publisher.fail_next(failure)
    else:
        publisher.undeliverable_next(reason=failure)

    await run(bot, 'kcpc weekly post-now', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == text


async def test_post_now_says_when_discord_refused_this_weeks_problem_earlier(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    publisher.fail_next(PublishOutcome.SKIPPED)
    await run(bot, 'kcpc weekly post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly post-now', ctx)

    assert publisher.posts == []
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == (
        "Discord refused to post this week's problem, **1520G - To Go Or Not To "
        "Go?**, earlier: `discord-403`. It can't be posted again this week."
    )


async def test_post_now_is_green_only_if_every_post_went_out(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any', 'codeforces:hard:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()
    await clock.advance_to(friday(2))
    publish = publisher.publish

    async def refusing_problems(
        deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> Any:
        if deliveries[0].kind == PROBLEM:
            publisher.fail_next(PublishOutcome.SKIPPED)
        return await publish(deliveries, message)

    monkeypatch.setattr(publisher, 'publish', refusing_problems)

    await run(bot, 'kcpc weekly post-now', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == '\n'.join(
        [
            'Posted the solution of **1520G - To Go Or Not To Go?**.',
            "Discord refused to post this week's problem, **1520F1 - Guess the "
            'K-th Zero (Easy version)**: `discord-403`.',
        ]
    )


async def test_post_now_tells_of_the_solution_it_posted_when_no_problem_can_be_picked(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)  # 1520G, the one expert problem
    cast(AsyncMock, ctx.send).reset_mock()
    await clock.advance_to(friday(2))

    await run(bot, 'kcpc weekly post-now', ctx)

    assert titles(publisher)[1:] == ['Solution: 1520G - To Go Or Not To Go?']
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == '\n'.join(
        [
            'Posted the solution of **1520G - To Go Or Not To Go?**.',
            "Couldn't pick this week's problem: no expert Codeforces problem is "
            "left that this server hasn't had; none of the 1 expert AtCoder "
            'problems tried has an official editorial. Please try again in a few '
            'minutes, or queue a problem with `/kcpc weekly queue`.',
        ]
    )


async def test_post_now_holds_the_problem_back_behind_an_undelivered_solution(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()
    await clock.advance_to(friday(2))
    publisher.undeliverable_next()

    await run(bot, 'kcpc weekly post-now', ctx)

    assert len(publisher.posts) == 1
    assert reply(ctx, ephemeral=True).description == '\n'.join(
        [
            "Couldn't post the solution of **1520G - To Go Or Not To Go?**: Discord "
            "hasn't sent this server's channels yet. Please try again in a minute.",
            "This week's problem goes out after that solution, so it wasn't posted "
            'either.',
        ]
    )


async def test_post_now_says_why_no_problem_could_be_picked(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    sites: FakeSites,
    codeforces: FakeCodeforces,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')  # no list loaded yet
    # Nor can the pick load them: neither site answers.
    codeforces.errors['problemset'] = cf.CodeforcesApiError('HTTP Error 503')
    sites.errors['problems'] = ExternalServiceError('AtCoder Problems', 'Down.')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly post-now', ctx)

    assert str(raised.value) == (
        "Couldn't pick this week's problem: Codeforces' problem list isn't loaded "
        "yet; AtCoder's problem list isn't loaded yet."
    )


async def test_queue_queues_a_problem_with_an_admins_solution_link(
    bot: KcpcBot,
    cog: KcpcProblems,
    ctx: MagicMock,
    services: KcpcServices,
    repo: WeeklyRepo,
) -> None:
    await load_lists(services)

    await run(
        bot,
        'kcpc weekly queue',
        ctx,
        'https://codeforces.com/problemset/problem/1520/D',
        f'<{BLOG}>',
    )

    assert await repo.queue(GUILD) == [
        QueuedProblem(
            guild_id=GUILD,
            source='codeforces',
            problem_id='1520D',
            contest_id='1520',
            index='D',
            name='Same Differences',
            url='https://codeforces.com/contest/1520/problem/D',
            difficulty=1200,
            band='medium',
            solution_url=BLOG,
            queued_by=ADMIN,
            queued_at=NOW,
            queue_id=1,
        )
    ]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        'Queued **1520D - Same Differences**, number 1 in the queue. Its solution '
        f'post will link [the link you gave]({BLOG}).'
    )
    # Suggested at once by unqueue.
    suggested = await cog.queued_autocomplete(interaction_in(GUILD), 'same')
    assert [(choice.name, choice.value) for choice in suggested] == [
        ('1520D - Same Differences', '1520/D')
    ]


@pytest.mark.parametrize(
    ('problem', 'editorials', 'note'),
    [
        ('1520D', None, CONTEST_PAGE_NOTE),
        (
            'abc300_d',
            page(
                'abc300_d',
                by_a_member('https://example.org/abc300_d'),
                official(editorial_url('abc300', 6076)),
            ),
            "Its solution post will link AtCoder's [official editorial]"
            "(https://atcoder.jp/contests/abc300/editorial/6076), and the task's "
            'other editorials.',
        ),
        (
            'ABC300_D',
            page('abc300_d', by_a_member('https://example.org/abc300_d')),
            'AtCoder lists no official editorial for it yet. Its solution post will '
            "link the task's editorials, and the official one if there is one by "
            'then.',
        ),
        (
            'https://atcoder.jp/contests/abc300/tasks/abc300_d',
            ExternalServiceError('AtCoder', UNREACHABLE),
            "I couldn't check AtCoder's editorials just now. Its solution post will "
            "link the task's editorials, and the official one if there is one by "
            'then.',
        ),
    ],
    ids=['codeforces', 'atcoder editorial', 'no official editorial', 'atcoder down'],
)
async def test_queue_says_what_the_solution_post_will_link(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    repo: WeeklyRepo,
    sites: FakeSites,
    problem: str,
    editorials: AtCoderEditorials | Exception | None,
    note: str,
) -> None:
    await load_lists(services)
    if isinstance(editorials, Exception):
        sites.errors['abc300_d'] = editorials
    elif editorials is not None:
        sites.editorials['abc300_d'] = editorials

    await run(bot, 'kcpc weekly queue', ctx, problem)

    (item,) = await repo.queue(GUILD)
    # Only an admin's link is stored: the bot looks again when it posts.
    assert item.solution_url is None
    description = reply(ctx, ephemeral=True).description
    assert description is not None and description.endswith(f'queue. {note}')


@pytest.mark.parametrize(
    ('problem', 'solution', 'error'),
    [
        (
            '9999Z',
            None,
            "Codeforces' problemset has no problem **9999Z**. A problem that a "
            'Div. 2 round shares with a Div. 1 round held alongside it is listed '
            "under the Div. 1 round's number: give it that way, or link it from "
            'the Div. 1 contest.',
        ),
        ('abc999_z', None, 'AtCoder has no problem **abc999\\_z**.'),
        (
            'not a problem',
            None,
            "That isn't a problem I know how to read. Give a Codeforces problem as "
            '1520D or its link, or an AtCoder problem as abc300_d or its link.',
        ),
        ('1520D', 'ftp://example.org/editorial', BAD_LINK),
        ('1520D', 'codeforces.com/blog/entry/90342', BAD_LINK),
        ('1520D', 'https://example.com/' + 'x' * 281, LONG_LINK),
    ],
)
async def test_queue_needs_a_known_problem_and_a_web_link(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    repo: WeeklyRepo,
    problem: str,
    solution: str | None,
    error: str,
) -> None:
    await load_lists(services)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly queue', ctx, problem, solution)

    assert str(raised.value) == error
    assert await repo.queue(GUILD) == []


async def test_queue_refuses_a_problem_queued_or_posted_before(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)  # 1520G
    await run(bot, 'kcpc weekly queue', ctx, '1520D')

    with pytest.raises(KcpcUserError) as queued_twice:
        await run(bot, 'kcpc weekly queue', ctx, '1520d')
    with pytest.raises(KcpcUserError) as posted_before:
        await run(bot, 'kcpc weekly queue', ctx, '1520G')

    assert str(queued_twice.value) == (
        "1520D - Same Differences is already in this server's queue."
    )
    assert str(posted_before.value) == (
        "1520G - To Go Or Not To Go? was already this server's weekly problem on "
        '2026-09-25.'
    )


async def test_queue_holds_at_most_25_problems(
    bot: KcpcBot, ctx: MagicMock, services: KcpcServices, repo: WeeklyRepo
) -> None:
    await load_lists(services)
    for number in range(25):
        await repo.enqueue(queued(cf_problem(2000 + number, 'A', 'Filler', 800, [])))

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly queue', ctx, '1520D')

    assert str(raised.value) == (
        'The queue is full: it holds at most 25 problems. Take one out with '
        '`/kcpc weekly unqueue` first.'
    )
    assert len(await repo.queue(GUILD)) == 25


async def test_queue_waits_for_the_lists_to_load(bot: KcpcBot, ctx: MagicMock) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly queue', ctx, '1520D')

    assert str(raised.value) == NOT_LOADED.format(platform="Codeforces'")


async def test_unqueue_takes_a_problem_out_of_the_queue(
    bot: KcpcBot,
    cog: KcpcProblems,
    ctx: MagicMock,
    services: KcpcServices,
    repo: WeeklyRepo,
) -> None:
    await load_lists(services)
    for problem in ('1520D', 'abc300_d'):
        await run(bot, 'kcpc weekly queue', ctx, problem)
    interaction = interaction_in(GUILD)
    suggested = await cog.queued_autocomplete(interaction, '')
    assert [(choice.name, choice.value) for choice in suggested] == [
        ('1520D - Same Differences', '1520/D'),
        ('ABC300 D - AABCC', 'abc300_d'),
    ]
    cast(AsyncMock, ctx.send).reset_mock()

    # As a suggestion fills it in; AtCoder's IDs in any case.
    await run(bot, 'kcpc weekly unqueue', ctx, suggested[0].value)
    await run(bot, 'kcpc weekly unqueue', ctx, 'ABC300_D')

    assert await repo.queue(GUILD) == []
    assert await cog.queued_autocomplete(interaction, '') == []
    send = cast(AsyncMock, ctx.send)
    assert [call.kwargs['embed'].description for call in send.await_args_list] == [
        'Took **1520D - Same Differences** out of the queue.',
        'Took **ABC300 D - AABCC** out of the queue.',
    ]


async def test_unqueue_needs_a_queued_problem(
    bot: KcpcBot, ctx: MagicMock, services: KcpcServices
) -> None:
    await load_lists(services)
    await run(bot, 'kcpc weekly queue', ctx, '1520D')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly unqueue', ctx, '1520E')

    assert str(raised.value) == "**1520E** isn't in this server's queue."


async def test_unqueue_suggests_the_servers_own_queue_from_memory(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    cog: KcpcProblems,
    ctx: MagicMock,
    db: Database,
    services: KcpcServices,
) -> None:
    command = command_named(bot, 'kcpc weekly unqueue')
    assert isinstance(command, commands.HybridCommand)
    assert isinstance(command.app_command, app_commands.Command)
    parameter = command.app_command.get_parameter('problem')
    assert parameter is not None and parameter.autocomplete
    await load_lists(services)
    await run(bot, 'kcpc weekly queue', ctx, '1520D')
    # Discord asks on every keystroke: no query is made.
    failing = AsyncMock(side_effect=AssertionError('autocomplete queried kcpc.db'))
    monkeypatch.setattr(db, 'fetchall', failing)
    monkeypatch.setattr(db, 'fetchone', failing)

    here = await cog.queued_autocomplete(interaction_in(GUILD), '1520d')
    elsewhere = await cog.queued_autocomplete(interaction_in(OTHER_GUILD), '')

    assert [choice.value for choice in here] == ['1520/D']
    assert elsewhere == []


async def test_solution_sets_the_link_of_the_latest_problem_with_its_solution_to_come(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any', 'codeforces:hard:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly solution', ctx, f' {BLOG} ')

    row = await repo.get(GUILD, friday(25, 9))
    assert row is not None
    assert (row.solution_url, row.solution_set_by) == (BLOG, ADMIN)
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        'The solution post of **1520G - To Go Or Not To Go?** will link '
        f'[this link]({BLOG}). It goes out {when(friday(2))}.'
    )

    # The solution post gives it, rather than the contest page.
    await clock.advance_to(friday(2))
    await run(bot, 'kcpc weekly post-now', ctx)

    assert titles(publisher)[1] == 'Solution: 1520G - To Go Or Not To Go?'
    solution = publisher.posts[1].message
    assert solution.url == BLOG
    assert solution.description is not None
    assert solution.description.splitlines()[1] == f'[Editorial]({BLOG})'


async def test_solution_can_name_the_week(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(0))
    await posted(repo, publisher, weekly_row(1))

    await run(bot, 'kcpc weekly solution', ctx, BLOG, '2026-09-18')

    older = await repo.get(GUILD, weekly_row(1).slot)
    newer = await repo.get(GUILD, weekly_row(0).slot)
    assert older is not None and older.solution_url == BLOG
    assert newer is not None and newer.solution_url is None
    # Its time has passed: it goes out with the next post.
    assert reply(ctx, ephemeral=True).description == (
        f'The solution post of **1501A - Problem 1** will link [this link]({BLOG}).'
    )


async def test_solution_without_a_week_takes_the_newest_whose_solution_is_to_come(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(1))
    await posted(repo, publisher, weekly_row(0))

    await run(bot, 'kcpc weekly solution', ctx, BLOG)

    newer = await repo.get(GUILD, weekly_row(0).slot)
    older = await repo.get(GUILD, weekly_row(1).slot)
    assert newer is not None and newer.solution_url == BLOG
    assert older is not None and older.solution_url is None


async def test_solution_without_a_week_skips_problems_that_never_went_out(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(1))
    await repo.create(weekly_row(0))  # Discord refused its post, say

    await run(bot, 'kcpc weekly solution', ctx, BLOG)

    older = await repo.get(GUILD, weekly_row(1).slot)
    newer = await repo.get(GUILD, weekly_row(0).slot)
    assert older is not None and older.solution_url == BLOG
    assert newer is not None and newer.solution_url is None
    assert reply(ctx, ephemeral=True).description == (
        f'The solution post of **1501A - Problem 1** will link [this link]({BLOG}).'
    )


async def test_solution_without_a_week_sets_no_link_that_wont_be_posted(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    # 2026-08-28: by the next post, 2026-10-02, five weeks old.
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(4))

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly solution', ctx, BLOG)

    assert str(raised.value) == (
        "No weekly problem here has a solution that's still to come."
    )


@pytest.mark.parametrize(
    ('weeks_ago', 'went_out', 'error'),
    [
        (
            0,
            False,
            "The problem of 2026-09-25, **1500A - Problem 0**, wasn't posted, so "
            "its solution won't be either.",
        ),
        (
            4,
            True,
            "The solution of **1504A - Problem 4** won't be posted: by the next "
            'post, its problem will be more than 4 weeks old.',
        ),
    ],
    ids=['never posted', 'too old'],
)
async def test_solution_refuses_a_week_whose_solution_wont_be_posted(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    weeks_ago: int,
    went_out: bool,
    error: str,
) -> None:
    await set_up(guild_settings)
    row = weekly_row(weeks_ago)
    if went_out:
        await posted(repo, publisher, row)
    else:
        await repo.create(row)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly solution', ctx, BLOG, row.week)

    assert str(raised.value) == error
    stored = await repo.get(GUILD, row.slot)
    assert stored is not None and stored.solution_url is None


async def test_solution_takes_a_week_four_weeks_before_the_next_post(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    # 2026-09-04, four weeks before the next post: its run looks that far back.
    await set_up(guild_settings)
    row = await posted(repo, publisher, weekly_row(3))

    await run(bot, 'kcpc weekly solution', ctx, BLOG, row.week)

    stored = await repo.get(GUILD, row.slot)
    assert stored is not None and stored.solution_url == BLOG


@pytest.mark.parametrize(
    ('url', 'week', 'error'),
    [
        (BLOG, '2026-09-11', 'This server had no weekly problem on 2026-09-11.'),
        (BLOG, '25/09/2026', BAD_WEEK),
        (BLOG, '2026-02-30', BAD_WEEK),
        (
            BLOG,
            '2026-09-18',
            'The solution of **1501A - Problem 1** is posted already, so its link '
            "can't change.",
        ),
        ('   ', None, BAD_LINK),
        ('javascript:alert(1)', None, BAD_LINK),
        ('https://example.com/(' + 'x' * 278 + ')', None, LONG_LINK),
    ],
    ids=[
        'no such week',
        'format',
        'no such date',
        'posted',
        'blank',
        'scheme',
        'too long',
    ],
)
async def test_solution_needs_a_week_with_a_solution_to_come_and_a_web_link(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    url: str,
    week: str | None,
    error: str,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(0))
    await posted(
        repo,
        publisher,
        weekly_row(1, solution_url=BLOG, solution_posted=True, solution_posted_at=NOW),
    )

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly solution', ctx, url, week)

    assert str(raised.value) == error
    newer = await repo.get(GUILD, weekly_row(0).slot)
    assert newer is not None and newer.solution_url is None


async def test_solution_needs_a_problem_whose_solution_is_still_to_come(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(
        repo,
        publisher,
        weekly_row(1, solution_url=BLOG, solution_posted=True, solution_posted_at=NOW),
    )

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly solution', ctx, BLOG)

    assert str(raised.value) == (
        "No weekly problem here has a solution that's still to come."
    )


async def test_rotation_shows_the_rotation_with_the_next_posts_entry(
    bot: KcpcBot, ctx: MagicMock
) -> None:
    await run(bot, 'kcpc weekly rotation', ctx)

    # Week 39 since 2026-01-02 is 2026-10-02's, the default rotation's 4th.
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(KCPC_COLOR)
    assert embed.title == 'Weekly rotation'
    assert embed.description == '\n'.join(
        [
            'This server has the default rotation:',
            '1. Codeforces · easy · any topic',
            '2. AtCoder · medium · any topic',
            '3. Codeforces · medium · any topic',
            '**4. AtCoder · hard · any topic** (next post)',
            '',
            ROTATION_HINT,
        ]
    )


async def test_rotation_sets_the_rotation_and_goes_back_to_the_default(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await run(
        bot, 'kcpc weekly rotation', ctx, entries='cf easy; ac MEDIUM, cf hard graphs'
    )

    settings = await guild_settings.get_typed(GUILD, WEEKLY, WeeklySettings)
    assert settings.rotation == (
        'codeforces:easy:any',
        'atcoder:medium:any',
        'codeforces:hard:graphs',
    )
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    # Week 39 takes the first of three.
    assert embed.description == '\n'.join(
        [
            'The rotation is now:',
            '**1. Codeforces · easy · any topic** (next post)',
            '2. AtCoder · medium · any topic',
            '3. Codeforces · hard · graphs',
        ]
    )
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly rotation', ctx, entries=' Default ')

    settings = await guild_settings.get_typed(GUILD, WEEKLY, WeeklySettings)
    assert settings.rotation == ()
    description = reply(ctx, ephemeral=True).description
    assert description is not None
    assert description.splitlines()[0] == 'The rotation is the default again:'


async def test_rotation_refuses_an_entry_it_cannot_read(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await set_up(guild_settings, 'atcoder:hard:any')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'kcpc weekly rotation', ctx, entries='cf easy, xx hard')

    assert str(raised.value) == (
        "Entry 2 ('xx hard') has no platform I know: use cf (Codeforces) or ac "
        '(AtCoder).'
    )
    settings = await guild_settings.get_typed(GUILD, WEEKLY, WeeklySettings)
    assert settings.rotation == ('atcoder:hard:any',)


async def test_rotation_as_a_prefix_command_takes_the_rest_of_the_message(
    bot: KcpcBot, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    ctx = make_context(
        bot,
        guild,
        make_member(manage_guild=True),
        args='cf easy dfs and similar, ac hard',
    )

    await invoke(bot, 'kcpc weekly rotation', ctx)

    reply(ctx, ephemeral=True)
    settings = await guild_settings.get_typed(GUILD, WEEKLY, WeeklySettings)
    assert settings.rotation == ('codeforces:easy:dfs and similar', 'atcoder:hard:any')


async def test_preview_shows_the_next_post_this_week_the_queue_and_the_rotation(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:graphs')
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    await run(bot, 'kcpc weekly queue', ctx, '1520D')
    await run(bot, 'kcpc weekly queue', ctx, '1520E')
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly preview', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.title == 'Weekly problem preview'
    assert fields(embed) == [
        ('Next post', f'{when(friday(2))} in <#{CHANNEL}>'),
        (
            'This week',
            '[1520G - To Go Or Not To Go?](https://codeforces.com/contest/1520/'
            'problem/G)\n**Solution:** the contest page until you set one with '
            '`/kcpc weekly solution`',
        ),
        (
            'Next problem',
            '[1520D - Same Differences](https://codeforces.com/contest/1520/problem/'
            f'D), queued by <@{ADMIN}>',
        ),
        (
            'Queue (2)',
            f'1. 1520D - Same Differences · queued by <@{ADMIN}>\n'
            f'2. 1520E - Arranging The Sheep · queued by <@{ADMIN}>',
        ),
        ('Rotation', '**1. Codeforces · expert · graphs** (next post)'),
    ]


async def test_preview_of_a_server_not_set_up_says_what_to_do(
    bot: KcpcBot, ctx: MagicMock
) -> None:
    await run(bot, 'kcpc weekly preview', ctx)

    embed = reply(ctx, ephemeral=True)
    assert fields(embed) == [
        (
            'Next post',
            f'{when(friday(2))}, once you turn it on with `/kcpc enable weekly` and '
            'set its channel with `/kcpc channel weekly #channel`. Until then '
            'nothing is posted.',
        ),
        ('This week', 'Nothing has been posted yet.'),
        (
            'Next problem',
            'From the rotation: AtCoder · hard · any topic, picked when it posts',
        ),
        ('Queue (0)', 'Empty. Add a problem with `/kcpc weekly queue`.'),
        (
            'Rotation (the default)',
            '1. Codeforces · easy · any topic\n'
            '2. AtCoder · medium · any topic\n'
            '3. Codeforces · medium · any topic\n'
            '**4. AtCoder · hard · any topic** (next post)',
        ),
    ]


async def test_preview_promises_no_solution_post_too_old_to_go_out(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, weekly_row(4))

    await run(bot, 'kcpc weekly preview', ctx)

    this_week = fields(reply(ctx, ephemeral=True))[1][1]
    assert this_week is not None
    assert this_week.splitlines()[1] == (
        "**Solution:** won't be posted: by the next post, the problem will be "
        'more than 4 weeks old'
    )


async def test_preview_fits_a_long_rotation_around_the_next_posts_entry(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    tags = ['chinese remainder theorem', 'string suffix structures', 'dfs and similar']
    rotation = [f'codeforces:hard:{tags[number % 3]}' for number in range(52)]
    await set_up(guild_settings, *rotation)

    await run(bot, 'kcpc weekly preview', ctx)

    name, value = fields(reply(ctx, ephemeral=True))[4]
    assert name == 'Rotation' and value is not None
    assert len(value) <= 1024
    lines = value.splitlines()
    # Week 39 since 2026-01-02 takes the 40th entry.
    marked = '**40. Codeforces · hard · chinese remainder theorem** (next post)'
    assert marked in lines
    first, *shown, last = lines
    earlier = int(first.removeprefix('…').removesuffix(' earlier'))
    more = int(last.removeprefix('…and ').removesuffix(' more'))
    assert [line.lstrip('*').split('.')[0] for line in shown] == [
        str(number) for number in range(earlier + 1, 53 - more)
    ]


async def test_preview_fits_a_long_queue_and_says_how_many_more_there_are(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: WeeklyRepo,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    name = 'A Problem With A Long Name, As Some Of Them Have, ' * 2
    for number in range(25):
        await repo.enqueue(queued(cf_problem(2000 + number, 'A', name, 800, [])))

    await run(bot, 'kcpc weekly preview', ctx)

    title, value = fields(reply(ctx, ephemeral=True))[3]
    assert title == 'Queue (25)' and value is not None
    assert len(value) <= 1024
    *listed, count = value.splitlines()
    assert count == f'…and {25 - len(listed)} more'
    assert listed[0] == (
        '1. 2000A - A Problem With A Long Name, As Some Of Them Have, A…'
        f' · queued by <@{ADMIN}>'
    )


@pytest.mark.parametrize(
    ('rotation', 'solution', 'status'),
    [
        (
            'codeforces:expert:any',
            BLOG,
            f'[this link]({BLOG}), set by <@{ADMIN}>',
        ),
        (
            'atcoder:medium:any',
            None,
            "AtCoder's [official editorial]"
            '(https://atcoder.jp/contests/abc300/editorial/6076)',
        ),
    ],
    ids=['an admins link', 'atcoders editorial'],
)
async def test_preview_says_where_this_weeks_solution_link_comes_from(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    sites: FakeSites,
    rotation: str,
    solution: str | None,
    status: str,
) -> None:
    await set_up(guild_settings, rotation)
    sites.editorials['abc300_d'] = page(
        'abc300_d', official(editorial_url('abc300', 6076))
    )
    await load_lists(services)
    await run(bot, 'kcpc weekly post-now', ctx)
    if solution is not None:
        await run(bot, 'kcpc weekly solution', ctx, solution)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc weekly preview', ctx)

    this_week = fields(reply(ctx, ephemeral=True))[1][1]
    assert this_week is not None
    assert this_week.splitlines()[1] == f'**Solution:** {status}'


async def test_the_refresh_job_keeps_a_list_it_cannot_fetch_and_fails(
    bot: KcpcBot,
    member_ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sites.errors['problems'] = ExternalServiceError(
        'AtCoder Problems', 'AtCoder Problems is not responding right now.'
    )

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        with pytest.raises(RuntimeError) as raised:
            await load_lists(services)

    assert str(raised.value) == 'Could not refresh the problem lists of: AtCoder'
    assert raised.value.__cause__ is sites.errors['problems']
    assert cog_messages(caplog) == [
        "Could not refresh AtCoder's problem list: AtCoder Problems is not "
        'responding right now.'
    ]
    # Codeforces' list loaded all the same.
    await run(bot, 'randproblem', member_ctx, 'graphs', 'expert')
    assert reply(member_ctx).title == '1520G - To Go Or Not To Go?'


async def test_the_refresh_job_logs_a_bug_with_its_traceback(
    services: KcpcServices,
    bot: KcpcBot,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codeforces.errors['problemset'] = RuntimeError('boom')

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        with pytest.raises(RuntimeError, match='problem lists of: Codeforces$'):
            await load_lists(services)

    (record,) = [r for r in caplog.records if r.name == COG_LOGGER]
    assert record.getMessage() == "Could not refresh Codeforces' problem list"
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'


async def test_the_weekly_job_retries_a_post_it_couldnt_deliver_with_the_same_problem(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
) -> None:
    await set_up(guild_settings, 'codeforces:medium:any')  # 1520D or 1520E
    await set_up(guild_settings, 'codeforces:medium:any', guild_id=OTHER_GUILD)
    await load_lists(services)
    await clock.advance_to(friday(2))
    publisher.undeliverable_next()

    # The scheduler retries the slot within its grace.
    with pytest.raises(RuntimeError, match='not posted in guilds'):
        await services.scheduler.run_slot(WEEKLY_JOB)

    assert keys(publisher) == [problem_key(OTHER_GUILD, '2026-10-02')]
    picked = await repo.get(GUILD, friday(2))
    assert picked is not None

    await services.scheduler.run_slot(WEEKLY_JOB)

    assert keys(publisher)[1:] == [problem_key(GUILD, '2026-10-02')]
    assert publisher.posts[1].message.url == picked.url
    assert await repo.get(GUILD, friday(2)) == picked


async def test_the_weekly_job_takes_its_picks_out_of_what_unqueue_suggests(
    bot: KcpcBot,
    cog: KcpcProblems,
    ctx: MagicMock,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await run(bot, 'kcpc weekly queue', ctx, '1520D')
    await clock.advance_to(friday(2))

    await services.scheduler.run_slot(WEEKLY_JOB)

    assert titles(publisher) == ['Weekly problem: 1520D - Same Differences']
    assert await cog.queued_autocomplete(interaction_in(GUILD), '') == []


async def test_the_weekly_job_skips_a_server_the_bot_has_left(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: WeeklyRepo,
) -> None:
    left = 1_100_000_000_000_000_009
    await set_up(guild_settings, 'codeforces:expert:any')
    await set_up(guild_settings, 'codeforces:expert:any', guild_id=left)
    await load_lists(services)
    await clock.advance_to(friday(2))

    await services.scheduler.run_slot(WEEKLY_JOB)  # nothing to retry

    assert keys(publisher) == [problem_key(GUILD, '2026-10-02')]
    assert await repo.latest(left, at_or_before=friday(2)) is None


ADMIN_CALLS = [
    ('kcpc weekly queue', ('abc300_d',), {}),
    ('kcpc weekly unqueue', ('1520/D',), {}),
    ('kcpc weekly solution', (BLOG,), {}),
    ('kcpc weekly rotation', (), {'entries': 'cf easy, ac hard'}),
    ('kcpc weekly preview', (), {}),
    ('kcpc weekly post-now', (), {}),
]


@pytest.mark.parametrize(('name', 'args', 'kwargs'), ADMIN_CALLS)
async def test_admin_commands_defer_before_anything_else(
    bot: KcpcBot,
    ctx: MagicMock,
    services: KcpcServices,
    sites: FakeSites,
    repo: WeeklyRepo,
    guild_settings: GuildSettingsRepo,
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    # Queueing an AtCoder problem asks AtCoder for its editorials, longer than
    # Discord waits for a slash command's first answer; the others answer as
    # fast, deferred alike.
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    await services.scheduler.run_slot(WEEKLY_JOB)  # this week's problem
    await repo.enqueue(queued(D))
    fetched = list(sites.fetched)
    when_deferred: list[tuple[list[str], int]] = []
    send = cast(AsyncMock, ctx.send)
    cast(AsyncMock, ctx.defer).side_effect = lambda **_: when_deferred.append(
        (list(sites.fetched), send.await_count)
    )

    await run(bot, name, ctx, *args, **kwargs)

    cast(AsyncMock, ctx.defer).assert_awaited_once_with(ephemeral=True)
    assert when_deferred == [(fetched, 0)]
    reply(ctx, ephemeral=True)


async def test_a_slash_admin_command_defers_its_interaction(
    bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), slash=True)

    await run(bot, 'kcpc weekly preview', ctx)

    assert ctx.interaction is not None
    defer = cast(AsyncMock, ctx.interaction.response.defer)
    defer.assert_awaited_once_with(ephemeral=True)
    reply(ctx, ephemeral=True)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ADMIN_COMMANDS)
async def test_admin_commands_are_for_admins_only(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    guild: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # Their cog is this one, so the admin cog's check doesn't cover them.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    command = command_named(bot, f'kcpc weekly {name}')
    admin = make_context(bot, guild, make_member(manage_guild=True), slash=slash)
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command.can_run(admin)
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(member)


async def test_the_admin_group_on_its_own_is_for_admins_only(
    monkeypatch: pytest.MonkeyPatch, bot: KcpcBot, guild: MagicMock
) -> None:
    # ;kcpc weekly; Discord can't run a slash group on its own.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    group = command_named(bot, 'kcpc weekly')
    member = make_context(bot, guild, make_member(manage_guild=False))

    with pytest.raises(NotKcpcAdmin):
        await group.can_run(member)


async def test_a_user_error_gets_a_private_reply(
    bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), args='1520D')

    await invoke(bot, 'kcpc weekly unqueue', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == "**1520D** isn't in this server's queue."


async def test_a_bug_gets_an_apology_and_is_logged(
    bot: KcpcBot,
    guild: MagicMock,
    services: KcpcServices,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(guild_settings, 'codeforces:expert:any')
    await load_lists(services)
    publisher.fail_next(RuntimeError('boom'))
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc weekly post-now', ctx)

    assert reply(ctx, ephemeral=True).description == UNEXPECTED_ERROR_MESSAGE
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'
