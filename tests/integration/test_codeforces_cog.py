"""Integration tests for tle.cogs.codeforces — Codeforces cog commands."""

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from tle.access.help import prefix_usage
from tle.cogs.codeforces import Codeforces, CodeforcesCogError
from tle.util import codeforces_api as cf, codeforces_common as cf_common
from tle.util.codeforces_api import Contest, Member, Party, Problem, Submission, User

pytestmark = pytest.mark.integration


def _make_user(handle='tourist', rating=3000):
    return User(
        handle=handle,
        firstName=None,
        lastName=None,
        country=None,
        city=None,
        organization=None,
        contribution=0,
        rating=rating,
        maxRating=rating,
        lastOnlineTimeSeconds=0,
        registrationTimeSeconds=0,
        friendOfCount=0,
        titlePhoto='https://example.com/photo.jpg',
    )


def _make_contest(id=1, name='Round #1', start=1_000_000):
    return Contest(
        id=id,
        name=name,
        startTimeSeconds=start,
        durationSeconds=7200,
        type='CF',
        phase='FINISHED',
        preparedBy=None,
    )


def _make_problem(contestId=1, index='A', name='Problem A', rating=1500, tags=None):
    return Problem(
        contestId=contestId,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=None,
        rating=rating,
        tags=tags or [],
    )


def _make_submission(problem, verdict='OK'):
    party = Party(
        contestId=problem.contestId,
        members=[Member(handle='tourist')],
        participantType='CONTESTANT',
        teamId=None,
        teamName=None,
        ghost=False,
        room=None,
        startTimeSeconds=None,
    )
    return Submission(
        id=1,
        contestId=problem.contestId,
        problem=problem,
        author=party,
        programmingLanguage='C++',
        verdict=verdict,
        creationTimeSeconds=1_000_000,
        relativeTimeSeconds=0,
    )


@pytest.fixture
async def cog_env(user_db):
    """Set up a Codeforces cog with mocked bot and services."""
    bot = MagicMock()
    bot.user_db = user_db

    # Register a handle for our test user
    await user_db.set_handle(12345, 1, 'tourist')
    cf_user = _make_user(handle='tourist', rating=3000)
    await user_db.cache_cf_user(cf_user)

    # Set up cf_cache mock
    contest = _make_contest(id=1)
    problems = [
        _make_problem(contestId=1, index='A', name='Easy', rating=3000),
        _make_problem(contestId=1, index='B', name='Medium', rating=3100),
        _make_problem(contestId=1, index='C', name='Hard', rating=3200),
    ]

    cf_cache = MagicMock()
    cf_cache.problem_cache.problems = problems
    cf_cache.contest_cache.get_contest.return_value = contest
    cf_cache.contest_cache.contest_by_id = {1: contest}
    bot.cf_cache = cf_cache

    cog = Codeforces(bot)

    # Mock context
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.author = MagicMock()
    ctx.author.id = 12345
    ctx.author.__str__ = MagicMock(return_value='TestUser#1234')
    ctx.message = MagicMock()
    ctx.message.author = ctx.author
    ctx.guild = MagicMock()
    ctx.guild.id = 1

    return cog, ctx, bot, problems


# --- _validate_gitgud_status ---


class TestValidateGitgudStatus:
    # Each refusal comes before any request to Codeforces, so it gives back
    # the use that the command's cooldown counted.

    async def test_invalid_delta_not_multiple_of_100(self, cog_env):
        cog, ctx, _, _ = cog_env
        with pytest.raises(CodeforcesCogError, match='multiple of 100'):
            await cog._validate_gitgud_status(ctx, delta=50)
        ctx.command.reset_cooldown.assert_called_once_with(ctx)

    async def test_delta_too_large(self, cog_env):
        cog, ctx, _, _ = cog_env
        with pytest.raises(CodeforcesCogError, match='Delta must range'):
            await cog._validate_gitgud_status(ctx, delta=400)
        ctx.command.reset_cooldown.assert_called_once_with(ctx)

    async def test_delta_too_negative(self, cog_env):
        cog, ctx, _, _ = cog_env
        with pytest.raises(CodeforcesCogError, match='Delta must range'):
            await cog._validate_gitgud_status(ctx, delta=-400)
        ctx.command.reset_cooldown.assert_called_once_with(ctx)

    async def test_active_challenge_raises(self, cog_env):
        cog, ctx, bot, problems = cog_env
        # Create an active challenge
        p = problems[0]
        await bot.user_db.new_challenge(
            12345, datetime.datetime.now().timestamp(), p, 0
        )
        with pytest.raises(CodeforcesCogError, match='active challenge'):
            await cog._validate_gitgud_status(ctx, delta=0)
        ctx.command.reset_cooldown.assert_called_once_with(ctx)

    async def test_delta_none_skips_delta_checks(self, cog_env):
        cog, ctx, _, _ = cog_env
        # delta=None should skip delta validation (used by upsolve)
        await cog._validate_gitgud_status(ctx, delta=None)
        ctx.command.reset_cooldown.assert_not_called()


# --- _gitgud ---


class TestGitgud:
    async def test_creates_challenge_and_sends_embed(self, cog_env):
        cog, ctx, bot, problems = cog_env
        problem = problems[0]
        await cog._gitgud(ctx, 'tourist', problem, 0)

        # Should have sent a message with embed
        ctx.send.assert_awaited_once()
        call_args = ctx.send.call_args
        assert 'tourist' in call_args.args[0]
        assert call_args.kwargs['embed'] is not None

        # Should have stored challenge in DB
        active = await bot.user_db.check_challenge(12345)
        assert active is not None


# --- gimme ---


class TestGimme:
    @patch('tle.cogs.codeforces.cf')
    @patch('tle.cogs.codeforces.cf_common')
    async def test_returns_embed(self, mock_cf_common, mock_cf, cog_env):
        cog, ctx, bot, problems = cog_env

        # Mock resolve_handles
        mock_cf_common.resolve_handles = AsyncMock(return_value=['tourist'])
        mock_cf_common.parse_tags.return_value = []
        mock_cf_common.parse_rating.return_value = 3000
        mock_cf_common.is_contest_writer.return_value = False
        mock_cf_common.user_guard = MagicMock(side_effect=lambda **kwargs: lambda f: f)
        mock_cf_common.active_groups = {}

        # Mock cf.user.status — return no solved submissions
        mock_cf.user.status = AsyncMock(return_value=[])

        # Need to rebind fetch_cf_user since we need the handle
        bot.user_db.fetch_cf_user = AsyncMock(
            return_value=_make_user(handle='tourist', rating=3000)
        )

        # Call the underlying callback directly
        await cog.gimme.callback(cog, ctx)
        ctx.send.assert_awaited_once()
        call_args = ctx.send.call_args
        assert 'tourist' in call_args.args[0]

    @patch('tle.cogs.codeforces.cf')
    @patch('tle.cogs.codeforces.cf_common')
    async def test_no_problems_raises(self, mock_cf_common, mock_cf, cog_env):
        cog, ctx, bot, _ = cog_env

        mock_cf_common.resolve_handles = AsyncMock(return_value=['tourist'])
        mock_cf_common.parse_tags.return_value = []
        mock_cf_common.parse_rating.return_value = 9999  # impossible rating
        mock_cf_common.is_contest_writer.return_value = False
        mock_cf_common.user_guard = MagicMock(side_effect=lambda **kwargs: lambda f: f)
        mock_cf_common.active_groups = {}

        mock_cf.user.status = AsyncMock(return_value=[])

        bot.user_db.fetch_cf_user = AsyncMock(
            return_value=_make_user(handle='tourist', rating=9999)
        )

        with pytest.raises(CodeforcesCogError, match='not found'):
            await cog.gimme.callback(cog, ctx)


MEMBER_ID = 12345  # the member who uses the commands (see cog_env)
OTHER_ID = 67890  # another member of the server
# When a challenge was issued: long enough ago for nogud to skip it.
ISSUED = 1_000.0
# The reply to a gitgud command used while another of the member's is running.
RUNNING = 'You already have a gitgud command running. Try again when it finishes.'


def _member(member_id=OTHER_ID):
    member = MagicMock(spec=discord.Member, id=member_id)
    member.mention = f'<@{member_id}>'
    return member


class Typing:
    """Stands in for ``ctx.typing()``, which defers a slash command's answer;
    it records whether the command is typing.
    """

    def __init__(self):
        self.on = False
        self.times = 0

    def __call__(self, *, ephemeral=False):
        return self

    async def __aenter__(self):
        self.on = True
        self.times += 1

    async def __aexit__(self, *exc_info):
        self.on = False


@pytest.fixture
def codeforces(cog_env, monkeypatch):
    """Codeforces as the cog sees it, answered without a request: no
    submissions and no rated contests, until a test says otherwise. Each
    request records whether the command was typing as it asked.
    """
    _, ctx, bot, _ = cog_env
    ctx.typing = Typing()
    api = SimpleNamespace(asked_while_typing=[])

    def ask(**kwargs):
        api.asked_while_typing.append(ctx.typing.on)
        return DEFAULT

    for name in ('info', 'rating', 'status'):
        request = AsyncMock(return_value=[], side_effect=ask)
        setattr(api, name, request)
        monkeypatch.setattr(cf.user, name, request)
    # The member's handle, and the problems' contest for is_nonstandard_problem.
    monkeypatch.setattr(
        cf_common, 'resolve_handles', AsyncMock(return_value=['tourist'])
    )
    monkeypatch.setattr(cf_common, 'cf_cache', bot.cf_cache)
    return api


@pytest.fixture
def gitgud_running():
    """The member already has a gitgud command running."""
    running = cf_common.active_groups['gitgud']
    running.add(MEMBER_ID)
    yield
    running.discard(MEMBER_ID)


# --- _nogud ---


class TestForcedNogud:
    """_nogud, with which staff skip a member's gitgud challenge for them."""

    async def test_a_member_without_a_challenge_gets_a_friendly_error(self, cog_env):
        cog, ctx, _, _ = cog_env

        with pytest.raises(CodeforcesCogError) as caught:
            await Codeforces._nogud.callback(cog, ctx, _member())

        assert str(caught.value) == f'<@{OTHER_ID}> has no gitgud challenge to skip.'
        ctx.send.assert_not_awaited()

    async def test_so_does_a_member_whose_challenges_are_all_finished(self, cog_env):
        cog, ctx, bot, problems = cog_env
        await bot.user_db.new_challenge(OTHER_ID, ISSUED, problems[0], 0)
        challenge_id, *_ = await bot.user_db.check_challenge(OTHER_ID)
        await bot.user_db.complete_challenge(OTHER_ID, challenge_id, ISSUED + 60, 8)

        with pytest.raises(CodeforcesCogError, match='has no gitgud challenge'):
            await Codeforces._nogud.callback(cog, ctx, _member())

        # The finished challenge stays in their history.
        assert len(await bot.user_db.gitlog(OTHER_ID)) == 1

    async def test_an_active_challenge_is_skipped_and_leaves_the_history(self, cog_env):
        cog, ctx, bot, problems = cog_env
        await bot.user_db.new_challenge(OTHER_ID, ISSUED, problems[0], 0)

        await Codeforces._nogud.callback(cog, ctx, _member())

        ctx.send.assert_awaited_once_with('Challenge skip forced.')
        assert await bot.user_db.check_challenge(OTHER_ID) is None
        assert await bot.user_db.gitlog(OTHER_ID) == []
        # Unlike after their own nogud, gitgud may give them the problem again.
        assert await bot.user_db.get_noguds(OTHER_ID) == set()


# --- One gitgud command at a time ---


class TestOneGitgudCommandAtATime:
    """The gitgud commands and gimme run one at a time for each member. One
    used while another is running says so, where it used to do nothing.
    """

    @pytest.mark.parametrize(
        'name', ['gitgud', 'upsolve', 'gotgud', 'nogud', '_nogud', 'gimme']
    )
    async def test_another_one_meanwhile_is_refused_with_the_reason(
        self, cog_env, codeforces, gitgud_running, name
    ):
        cog, ctx, bot, problems = cog_env
        # An old challenge, which each of them would act on if it ran.
        await bot.user_db.new_challenge(MEMBER_ID, ISSUED, problems[0], 0)
        arguments = (_member(MEMBER_ID),) if name == '_nogud' else ()

        with pytest.raises(CodeforcesCogError) as caught:
            await getattr(Codeforces, name).callback(cog, ctx, *arguments)

        assert str(caught.value) == RUNNING
        # It did nothing: no answer, no request, and the challenge is as it was.
        ctx.send.assert_not_awaited()
        assert codeforces.asked_while_typing == []
        assert await bot.user_db.check_challenge(MEMBER_ID) is not None
        # The command already running still holds the member.
        assert MEMBER_ID in cf_common.active_groups['gitgud']

    async def test_the_command_already_running_carries_on(self, cog_env, codeforces):
        cog, ctx, bot, problems = cog_env
        await bot.user_db.new_challenge(MEMBER_ID, ISSUED, problems[0], 0)
        asked, release = asyncio.Event(), asyncio.Event()

        async def slow_status(**kwargs):
            asked.set()
            await release.wait()
            return [_make_submission(problems[0])]

        codeforces.status.side_effect = slow_status
        running = asyncio.create_task(Codeforces.gotgud.callback(cog, ctx))
        await asked.wait()
        try:
            with pytest.raises(CodeforcesCogError) as caught:
                await Codeforces.nogud.callback(cog, ctx)
        finally:
            release.set()
            await running

        assert str(caught.value) == RUNNING
        # gotgud claimed the points, and then let the member go again.
        ctx.send.assert_awaited_once()
        assert 'tourist gained 8 points' in ctx.send.await_args.args[0]
        assert MEMBER_ID not in cf_common.active_groups['gitgud']


# --- Typing ---


class TestSlowCommandsTypeWhileTheyAskCodeforces:
    """gitgud, upsolve and gotgud ask Codeforces, which can take longer than
    the 3 seconds a slash command has to answer. They ask while typing, which
    defers the answer, and only once their quick checks have passed.
    """

    async def test_gitgud(self, cog_env, codeforces):
        cog, ctx, _, _ = cog_env

        await Codeforces.gitgud.callback(cog, ctx)

        assert codeforces.asked_while_typing == [True]
        assert ctx.send.await_args.args[0] == 'Challenge problem for `tourist`'

    async def test_upsolve(self, cog_env, codeforces, make_rating_change):
        cog, ctx, _, _ = cog_env
        codeforces.rating.return_value = [make_rating_change(contestId=1)]

        await Codeforces.upsolve.callback(cog, ctx)

        assert codeforces.asked_while_typing == [True, True]
        ctx.send.assert_awaited_once()

    async def test_gotgud(self, cog_env, codeforces):
        cog, ctx, bot, problems = cog_env
        await bot.user_db.new_challenge(MEMBER_ID, ISSUED, problems[0], 0)
        codeforces.status.return_value = [_make_submission(problems[0])]

        await Codeforces.gotgud.callback(cog, ctx)

        assert codeforces.asked_while_typing == [True]
        assert 'tourist gained 8 points' in ctx.send.await_args.args[0]

    @pytest.mark.parametrize(
        ('name', 'arguments', 'error'),
        [
            ('gitgud', (50,), 'Delta must be a multiple of 100'),
            ('gitgud', (400,), 'Delta must range'),
            ('gotgud', (), 'You do not have an active challenge'),
        ],
    )
    async def test_a_mistake_is_answered_before_typing(
        self, cog_env, codeforces, name, arguments, error
    ):
        cog, ctx, _, _ = cog_env

        with pytest.raises(CodeforcesCogError, match=error):
            await getattr(Codeforces, name).callback(cog, ctx, *arguments)

        assert ctx.typing.times == 0
        assert codeforces.asked_while_typing == []

    @pytest.mark.parametrize('name', ['gitgud', 'upsolve'])
    async def test_so_is_a_challenge_already_active(self, cog_env, codeforces, name):
        cog, ctx, bot, problems = cog_env
        await bot.user_db.new_challenge(MEMBER_ID, ISSUED, problems[0], 0)

        with pytest.raises(CodeforcesCogError, match='You have an active challenge'):
            await getattr(Codeforces, name).callback(cog, ctx)

        assert ctx.typing.times == 0
        assert codeforces.asked_while_typing == []


# --- upsolve ---


class TestUpsolve:
    """upsolve lists problems to upsolve, or takes one as the challenge."""

    @pytest.fixture
    def rated(self, codeforces, make_rating_change):
        """The member took part in contest 1, rated, and has solved nothing."""
        codeforces.rating.return_value = [make_rating_change(contestId=1)]

    async def test_without_a_number_it_lists_the_problems(self, cog_env, rated):
        cog, ctx, bot, _ = cog_env

        await Codeforces.upsolve.callback(cog, ctx)

        embed = ctx.send.await_args.kwargs['embed']
        assert embed.title == 'Select a problem to upsolve (1-3):'
        assert await bot.user_db.check_challenge(MEMBER_ID) is None

    async def test_a_number_from_the_list_makes_that_problem_the_challenge(
        self, cog_env, rated
    ):
        cog, ctx, bot, _ = cog_env

        await Codeforces.upsolve.callback(cog, ctx, 2)

        _, _, name, _, _, delta = await bot.user_db.check_challenge(MEMBER_ID)
        assert (name, delta) == ('Medium', 100)

    @pytest.mark.parametrize('choice', [0, -1, 4])
    async def test_any_other_number_shows_the_list(self, cog_env, rated, choice):
        cog, ctx, bot, _ = cog_env

        await Codeforces.upsolve.callback(cog, ctx, choice)

        embed = ctx.send.await_args.kwargs['embed']
        assert embed.title == 'Select a problem to upsolve (1-3):'
        assert await bot.user_db.check_challenge(MEMBER_ID) is None

    def test_its_prefix_form_shows_no_default_number(self):
        # As /help shows how to type it.
        assert prefix_usage(Codeforces.upsolve) == ';upsolve [choice]'


# --- teamrate ---


class TestTeamrate:
    async def test_handles_without_a_rating_are_answered_in_plain_words(
        self, cog_env, codeforces
    ):
        cog, ctx, _, _ = cog_env
        codeforces.info.return_value = [_make_user(handle='tourist', rating=None)]

        with pytest.raises(CodeforcesCogError) as caught:
            await Codeforces.teamrate.callback(cog, ctx, 'tourist')

        # It used to say "No CF usernames with ratings passed in."
        assert str(caught.value) == 'None of these Codeforces handles has a rating.'


# --- Cooldowns and checks ---

# The commands with a cooldown, and the seconds each member waits between two
# uses of each; the cog's other commands have none.
COOLDOWNS = {
    'gitgud': 10,
    'upsolve': 10,
    'gimme': 10,
    'stalk': 20,
    'mashup': 20,
    'vc': 20,
    'fullsolve': 20,
    'teamrate': 20,
}
COMMANDS = sorted(command.name for command in Codeforces.__cog_commands__)


@pytest.mark.parametrize('name', COMMANDS)
def test_the_heavy_commands_have_a_cooldown_for_each_member(name):
    command = getattr(Codeforces, name)
    if name not in COOLDOWNS:
        assert command.cooldown is None
        return
    assert (command.cooldown.rate, command.cooldown.per) == (1, COOLDOWNS[name])
    assert command._buckets.type is commands.BucketType.user


@pytest.mark.parametrize('name', COMMANDS)
def test_only_the_access_rules_decide_who_may_use_each_command(name):
    # A check of the command's own, such as a role check, would refuse members
    # whom the rules, and so /help, let use it.
    assert getattr(Codeforces, name).checks == []
