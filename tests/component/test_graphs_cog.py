"""Tests for the plot commands (tle.cogs.graphs).

They cover how often each plot can be used, the slow plots of rating
distributions answering slash commands in time, the plots of a server that has
nothing to plot yet, and the help of the plot group. The cog is real, on a bot
of its own, and so is the user database, in memory. Most tests call a command's
callback, as discord.py does once it has checked and parsed the command, with a
real ``TLEContext``: a slash command's belongs to a mocked interaction, whose
deferral the tests see, and a prefix command's to a mocked channel, whose
typing indicator they see. The cooldown tests prepare each command as
discord.py does before it runs one.
"""

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle.access.context import TLEContext
from tle.access.help import describe_cooldown, split_help
from tle.access.rules import Decision, Effective, Outcome, Where, Who
from tle.access.service import cache_decision
from tle.cogs import graphs
from tle.cogs.graphs import (
    EXTREMES_LEFT_OUT_NOTE,
    EXTREMES_LEFT_OUT_ONE_NOTE,
    NO_COUNTRIES_MESSAGE,
    NO_EXTREMES_MESSAGE,
    NO_RATED_EXTREMES_MESSAGE,
    NO_RATED_MEMBERS_MESSAGE,
    PLOT_COOLDOWN_SECONDS,
    GraphCogError,
    Graphs,
)
from tle.util import codeforces_api as cf
from tle.util.cache import ContestNotFound, ProblemsetNotCached
from tle.util.db.user_db_conn import UserDbConn

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
CHANNEL_ID = 1_200_000_000_000_000_001
MEMBER_ID = 1_400_000_000_000_000_001
OTHER_MEMBER_ID = 1_400_000_000_000_000_002
LEFT_MEMBER_ID = 1_400_000_000_000_000_003
TYPO_MEMBER_ID = 1_400_000_000_000_000_004  # who mistypes a plot
# When the commands are used: the cooldown tests use them all at once.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

PLOTS = [
    'rating',
    'extreme',
    'solved',
    'hist',
    'curve',
    'scatter',
    'distrib',
    'cfdistrib',
    'centile',
    'howgud',
    'country',
    'visualrank',
    'speed',
]
EVERYONE_IN_BOT_CHANNELS = Effective(frozenset({Who.EVERYONE}), Where.BOT)
SERVER_TITLE = 'Rating distribution of server members'


class GraphsBot(commands.Bot):
    """A bot with what the plots read, as TLEBot has it."""

    user_db: Any
    cf_cache: Any


@pytest.fixture
async def bot(user_db: UserDbConn) -> AsyncIterator[GraphsBot]:
    """A bot with the plot commands, the user database and a stand-in for the
    Codeforces cache.
    """
    bot = GraphsBot(command_prefix=';', intents=discord.Intents.none())
    bot.user_db = user_db
    bot.cf_cache = MagicMock()
    await bot.add_cog(Graphs(bot))
    yield bot
    await bot.close()


@pytest.fixture
def cog(bot: GraphsBot) -> Graphs:
    found = bot.get_cog('Graphs')
    assert isinstance(found, Graphs)
    return found


@pytest.fixture
def events() -> list[str]:
    """What reached Discord or the data, in order."""
    return []


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in a channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


def make_member(member_id: int = MEMBER_ID) -> MagicMock:
    """A member with no role."""
    member = MagicMock(spec=discord.Member, id=member_id)
    member.roles = []
    return member


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose members are MEMBER_ID and OTHER_MEMBER_ID; the member
    LEFT_MEMBER_ID has left it.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    members = {
        member_id: make_member(member_id) for member_id in (MEMBER_ID, OTHER_MEMBER_ID)
    }
    guild.get_member.side_effect = members.get
    return guild


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers or defers it; deferring it counts as an event.
    """

    def __init__(self, events: list[str]) -> None:
        self.done = False
        self.events = events
        self.send_message = AsyncMock(side_effect=self._answer)
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))

    async def _defer(self, **kwargs: Any) -> None:
        self.done = True
        self.events.append('deferred')


def make_context(
    bot: GraphsBot,
    guild: MagicMock,
    name: str,
    events: list[str],
    *,
    slash: bool,
    private: bool = False,
) -> TLEContext:
    """A context of command ``name``, used by MEMBER_ID in a channel.

    With ``slash``, it belongs to an interaction, and the access check decided
    that it answers only the member with ``private``, or the whole channel.
    Without it, it belongs to a message, whose channel sends a typing
    indicator as an event.
    """
    command = bot.get_command(name)
    assert command is not None
    author = guild.get_member(MEMBER_ID)
    channel = MagicMock(spec=discord.TextChannel, id=CHANNEL_ID, guild=guild)

    async def typing(channel_id: int) -> None:
        events.append('typing')

    channel._state.http.send_typing = AsyncMock(side_effect=typing)
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    message.channel = channel
    # The typing indicator keeps itself going in a task on the bot's loop.
    message._state.loop = asyncio.get_running_loop()
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = False
        interaction.response = Response(events)
        interaction.followup.send = AsyncMock(
            return_value=MagicMock(spec=discord.WebhookMessage)
        )
    ctx = TLEContext(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        command=command,
        invoked_with=command.name,
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = ctx
        outcome = Outcome.PRIVATE if private else Outcome.PUBLIC
        cache_decision(ctx, Decision(outcome, EVERYONE_IN_BOT_CHANNELS, slash=True))
    return ctx


def followups(ctx: TLEContext) -> AsyncMock:
    assert ctx.interaction is not None
    send = ctx.interaction.followup.send
    assert isinstance(send, AsyncMock)
    return send


def response(ctx: TLEContext) -> Response:
    assert ctx.interaction is not None
    answer = ctx.interaction.response
    assert isinstance(answer, Response)
    return answer


def recorded(
    events: list[str], name: str, read: Callable[..., Any]
) -> Callable[..., Any]:
    """``read``, which counts as the event ``name`` when it is called."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        events.append(name)
        return await read(*args, **kwargs)

    return wrapper


async def link(
    user_db: UserDbConn,
    make_user: Callable[..., Any],
    member_id: int,
    handle: str,
    **fields: Any,
) -> None:
    """Link ``handle`` to member ``member_id``, its Codeforces user being
    ``fields``.
    """
    await user_db.set_handle(member_id, GUILD_ID, handle)
    await user_db.cache_cf_user(make_user(handle=handle, **fields))


@pytest.fixture
async def rated_members(
    user_db: UserDbConn,
    make_user: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    """Two members with rated handles, and reading their ratings as an event."""
    await link(user_db, make_user, MEMBER_ID, 'alice', rating=1500)
    await link(user_db, make_user, OTHER_MEMBER_ID, 'bob', rating=2100)
    read = recorded(events, 'read ratings', user_db.get_cf_users_for_guild)
    monkeypatch.setattr(user_db, 'get_cf_users_for_guild', read)


@pytest.fixture
def codeforces_users(bot: GraphsBot, events: list[str]) -> AsyncMock:
    """Three Codeforces users in the cache, and finding them as an event."""
    ratings = {'tourist': 3800, 'alice': 1500, 'bob': 2100}
    cache = bot.cf_cache.rating_changes_cache

    async def find(time_cutoff: int, contest_cutoff: int) -> list[str]:
        events.append('find users')
        return list(ratings)

    cache.get_users_with_more_than_n_contests = AsyncMock(side_effect=find)
    cache.get_current_rating.side_effect = ratings.get
    return cache.get_users_with_more_than_n_contests


def plot_sent(send: AsyncMock) -> tuple[discord.Embed, dict[str, Any]]:
    """The one plot sent, and what else it was sent with."""
    send.assert_awaited_once()
    assert send.await_args is not None
    options = dict(send.await_args.kwargs)
    embed = options.pop('embed')
    file = options.pop('file')
    assert isinstance(file, discord.File) and file.filename == 'plot.png'
    assert embed.image.url == 'attachment://plot.png'
    return embed, options


# How often the plots can be used


def prefix_context(
    bot: GraphsBot, author_id: int, typed: str = ''
) -> commands.Context[Any]:
    """A context of a prefix command used by ``author_id`` at NOW, with the
    arguments ``typed``.
    """
    author = make_member(author_id)
    message = MagicMock(spec=discord.Message, author=author, edited_at=None)
    message.created_at = NOW
    return commands.Context(
        message=message, bot=bot, view=StringView(typed), prefix=';'
    )


async def prepare(
    command: commands.Command[Any, ..., Any], author_id: int, typed: str = ''
) -> None:
    """Prepare ``command`` for ``author_id`` as discord.py does before running
    it: checks, arguments, then cooldown. Arguments that need more than these
    contexts give, such as a member, are left out.
    """
    with contextlib.suppress(commands.UserInputError):
        await command.prepare(prefix_context(command.cog.bot, author_id, typed))


# What each plot that needs arguments is given.
ARGUMENTS = {'visualrank': '1950'}


@pytest.mark.parametrize('name', PLOTS)
async def test_each_plot_can_be_used_once_every_20_seconds_by_each_member(
    bot: GraphsBot, name: str
) -> None:
    command = bot.get_command(f'plot {name}')
    assert command is not None
    typed = ARGUMENTS.get(name, '')

    await prepare(command, MEMBER_ID, typed)
    with pytest.raises(commands.CommandOnCooldown) as raised:
        await prepare(command, MEMBER_ID, typed)
    # Another member's use isn't held back.
    await prepare(command, OTHER_MEMBER_ID, typed)

    assert raised.value.retry_after == pytest.approx(PLOT_COOLDOWN_SECONDS)
    assert raised.value.type is commands.BucketType.user
    # As /help says it.
    assert describe_cooldown(command) == 'Once every 20 seconds for each member'


async def test_a_mistyped_plot_spends_no_cooldown(bot: GraphsBot) -> None:
    command = bot.get_command('plot visualrank')
    assert command is not None
    # A member of its own: every copy of a plot shares its cooldown, which the
    # test above spends for MEMBER_ID and OTHER_MEMBER_ID.
    member_id = TYPO_MEMBER_ID

    with pytest.raises(commands.BadArgument):
        await command.prepare(prefix_context(bot, member_id, 'soon'))
    await prepare(command, member_id, '1950')
    with pytest.raises(commands.CommandOnCooldown):
        await prepare(command, member_id, '1950')


async def test_the_cooldown_test_covers_every_plot(bot: GraphsBot) -> None:
    plot = bot.get_command('plot')
    assert isinstance(plot, commands.Group)

    assert sorted(command.name for command in plot.commands) == sorted(PLOTS)


async def test_the_plot_group_s_own_command_shows_its_help_without_a_cooldown(
    bot: GraphsBot, cog: Graphs, guild: MagicMock, events: list[str]
) -> None:
    plot = bot.get_command('plot')
    assert plot is not None
    ctx = make_context(bot, guild, 'plot', events, slash=True)
    ctx.send_help = AsyncMock()  # type: ignore[method-assign]

    await Graphs.plot.callback(cog, ctx)
    for _ in range(3):
        await prepare(plot, MEMBER_ID)

    ctx.send_help.assert_awaited_once()
    assert ctx.send_help.await_args is not None
    (shown,) = ctx.send_help.await_args.args
    assert getattr(shown, 'qualified_name', shown) == 'plot'
    assert plot.cooldown is None


# The rating distributions answer slash commands in time


@pytest.mark.usefixtures('rated_members')
@pytest.mark.parametrize('private', [True, False])
async def test_the_server_s_distribution_defers_a_slash_answer_before_reading(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    private: bool,
) -> None:
    ctx = make_context(bot, guild, 'plot distrib', events, slash=True, private=private)

    await Graphs.distrib.callback(cog, ctx)

    # Deferred first, as only the deferral must come within 3 seconds; it is
    # private when the answer is.
    assert events == ['deferred', 'read ratings']
    response(ctx).defer.assert_awaited_once_with(ephemeral=private)
    response(ctx).send_message.assert_not_awaited()
    embed, options = plot_sent(followups(ctx))
    assert embed.title == SERVER_TITLE
    assert options['ephemeral'] is private
    posted.assert_not_awaited()


@pytest.mark.usefixtures('rated_members')
async def test_the_server_s_distribution_shows_typing_on_prefix_before_reading(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
) -> None:
    ctx = make_context(bot, guild, 'plot distrib', events, slash=False)

    await Graphs.distrib.callback(cog, ctx)

    assert events == ['typing', 'read ratings']
    embed, options = plot_sent(posted)
    assert embed.title == SERVER_TITLE
    assert options['reference'] is ctx.message


@pytest.mark.parametrize('private', [True, False])
async def test_the_codeforces_distribution_defers_a_slash_answer_before_finding_users(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    codeforces_users: AsyncMock,
    private: bool,
) -> None:
    ctx = make_context(
        bot, guild, 'plot cfdistrib', events, slash=True, private=private
    )

    await Graphs.cfdistrib.callback(cog, ctx, 'normal', 'all', 10)

    assert events == ['deferred', 'find users']
    response(ctx).defer.assert_awaited_once_with(ephemeral=private)
    # Users with at least 10 rated contests, however long ago.
    codeforces_users.assert_awaited_once_with(0, 10)
    embed, options = plot_sent(followups(ctx))
    assert embed.title == 'Rating distribution of all Codeforces users (normal scale)'
    assert options['ephemeral'] is private


async def test_the_codeforces_distribution_shows_typing_on_prefix_before_finding_users(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    codeforces_users: AsyncMock,
    posted: AsyncMock,
) -> None:
    ctx = make_context(bot, guild, 'plot cfdistrib', events, slash=False)

    await Graphs.cfdistrib.callback(cog, ctx)

    assert events == ['typing', 'find users']
    embed, _ = plot_sent(posted)
    assert embed.title == 'Rating distribution of active Codeforces users (log scale)'


@pytest.mark.parametrize(
    ('mode', 'activity', 'error'),
    [
        ('linear', 'active', 'Mode should be either `log` or `normal`'),
        ('log', 'recent', 'Activity should be either `active` or `all`'),
    ],
)
async def test_a_wrong_choice_is_refused_before_the_answer_is_deferred(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    codeforces_users: AsyncMock,
    mode: str,
    activity: str,
    error: str,
) -> None:
    # So the refusal is the slash command's first answer, which the error
    # handler can still make private.
    ctx = make_context(bot, guild, 'plot cfdistrib', events, slash=True)

    with pytest.raises(GraphCogError, match=error):
        await Graphs.cfdistrib.callback(cog, ctx, mode, activity, 5)

    assert events == []
    response(ctx).defer.assert_not_awaited()
    codeforces_users.assert_not_awaited()


# Plots of a server with nothing to plot


async def test_the_server_s_distribution_says_so_when_nobody_linked_a_handle(
    bot: GraphsBot, cog: Graphs, guild: MagicMock, events: list[str]
) -> None:
    ctx = make_context(bot, guild, 'plot distrib', events, slash=True)

    with pytest.raises(GraphCogError) as raised:
        await Graphs.distrib.callback(cog, ctx)

    assert str(raised.value) == NO_RATED_MEMBERS_MESSAGE
    assert NO_RATED_MEMBERS_MESSAGE == (
        'No member of this server has linked a rated Codeforces handle yet.'
    )
    followups(ctx).assert_not_awaited()


async def test_the_server_s_distribution_counts_only_rated_members_still_here(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    user_db: UserDbConn,
    make_user: Callable[..., Any],
) -> None:
    # An unrated handle, and a rated one of a member who has left.
    await link(user_db, make_user, MEMBER_ID, 'alice', rating=None)
    await link(user_db, make_user, LEFT_MEMBER_ID, 'carol', rating=1900)
    ctx = make_context(bot, guild, 'plot distrib', events, slash=True)

    with pytest.raises(GraphCogError, match=NO_RATED_MEMBERS_MESSAGE):
        await Graphs.distrib.callback(cog, ctx)


async def test_the_plot_by_country_says_so_when_no_handle_has_a_country(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    user_db: UserDbConn,
    make_user: Callable[..., Any],
    posted: AsyncMock,
) -> None:
    await link(user_db, make_user, MEMBER_ID, 'alice', country=None)
    ctx = make_context(bot, guild, 'plot country', events, slash=False)

    with pytest.raises(GraphCogError) as raised:
        await Graphs.country.callback(cog, ctx)

    assert str(raised.value) == NO_COUNTRIES_MESSAGE
    assert NO_COUNTRIES_MESSAGE == (
        'No member of this server has linked a Codeforces handle with a country yet.'
    )
    posted.assert_not_awaited()


async def test_the_plot_by_country_counts_the_members_of_each_country(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    user_db: UserDbConn,
    make_user: Callable[..., Any],
    posted: AsyncMock,
) -> None:
    await link(user_db, make_user, MEMBER_ID, 'alice', country='Poland')
    await link(user_db, make_user, OTHER_MEMBER_ID, 'bob', country='Poland')
    ctx = make_context(bot, guild, 'plot country', events, slash=False)

    await Graphs.country.callback(cog, ctx)

    embed, _ = plot_sent(posted)
    assert embed.title == 'Distribution of server members by country'


# The plot of extremes, from the problems the bot has

# The rated contests of the handle plotted: the bot has the problems of
# CACHED, knows the contest UNCACHED without its problems, and has no contest
# UNKNOWN in its contest list.
CACHED, OTHER_CACHED, UNCACHED, UNKNOWN = 2201, 2202, 2203, 2204
EXTREME_HANDLE = 'tourist'


def extreme_contest(contest_id: int) -> cf.Contest:
    return cf.Contest(
        id=contest_id,
        name=f'Codeforces Round {contest_id}',
        startTimeSeconds=1_700_000_000 + (contest_id - CACHED) * 7 * 24 * 60 * 60,
        durationSeconds=2 * 60 * 60,
        type='CF',
        phase='FINISHED',
        preparedBy=None,
    )


def extreme_problem(contest_id: int, index: str, rating: int) -> cf.Problem:
    return cf.Problem(
        contestId=contest_id,
        problemsetName=None,
        index=index,
        name=f'Problem {contest_id}{index}',
        type='PROGRAMMING',
        points=None,
        rating=rating,
        tags=[],
    )


# The problems the bot has: A, B and C of each contest it has.
EXTREME_PROBLEMS = {
    contest_id: [
        extreme_problem(contest_id, index, rating)
        for index, rating in (('A', 800), ('B', 1400), ('C', 2000))
    ]
    for contest_id in (CACHED, OTHER_CACHED)
}


def unrated(contest_id: int) -> list[cf.Problem]:
    """The problems of ``contest_id`` that the bot has, as they are until
    Codeforces rates them, days after the contest.
    """
    return [problem._replace(rating=None) for problem in EXTREME_PROBLEMS[contest_id]]


def contest_submission(problem: cf.Problem, verdict: str) -> cf.Submission:
    """``EXTREME_HANDLE``'s submission to ``problem`` during its contest."""
    assert problem.contestId is not None
    return cf.Submission(
        id=problem.contestId * 1000 + ord(problem.index),
        contestId=problem.contestId,
        problem=problem,
        author=cf.Party(
            contestId=problem.contestId,
            members=[cf.Member(EXTREME_HANDLE)],
            participantType='CONTESTANT',
            teamId=None,
            teamName=None,
            ghost=False,
            room=None,
            startTimeSeconds=None,
        ),
        programmingLanguage='C++',
        verdict=verdict,
        creationTimeSeconds=0,
        relativeTimeSeconds=0,
    )


def rated_in(contest_id: int, new_rating: int) -> cf.RatingChange:
    return cf.RatingChange(
        contestId=contest_id,
        contestName=f'Codeforces Round {contest_id}',
        handle=EXTREME_HANDLE,
        rank=100,
        ratingUpdateTimeSeconds=1_700_000_000 + contest_id,
        oldRating=new_rating - 10,
        newRating=new_rating,
    )


class ExtremesCodeforces:
    """What the plot of extremes reads: the handle's rating changes and
    submissions from Codeforces, and the bot's contests and problems.
    """

    def __init__(
        self,
        bot: GraphsBot,
        monkeypatch: pytest.MonkeyPatch,
        events: list[str],
        contest_ids: list[int],
        submissions: list[cf.Submission],
    ) -> None:
        changes = [
            rated_in(contest_id, 1500 + n) for n, contest_id in enumerate(contest_ids)
        ]
        self.rating = AsyncMock(return_value=changes)
        self.status = AsyncMock(
            side_effect=recorded(events, 'read submissions', self._status)
        )
        self.submissions = submissions
        monkeypatch.setattr(cf.user, 'rating', self.rating)
        monkeypatch.setattr(cf.user, 'status', self.status)

        def get_contest(contest_id: int) -> cf.Contest:
            if contest_id == UNKNOWN:
                raise ContestNotFound(contest_id)
            return extreme_contest(contest_id)

        async def get_problemset(contest_id: int) -> list[cf.Problem]:
            if contest_id not in EXTREME_PROBLEMS:
                raise ProblemsetNotCached(contest_id)
            return EXTREME_PROBLEMS[contest_id]

        bot.cf_cache.contest_cache.get_contest.side_effect = get_contest
        bot.cf_cache.problemset_cache.get_problemset = AsyncMock(
            side_effect=get_problemset
        )
        # What each plot was drawn from: (contest ID, submissions) by contest.
        self.plotted: list[list[tuple[int, list[cf.Submission]]]] = []
        plot = graphs._plot_extreme

        def drawn(
            handle: str,
            rating: int,
            packed: list[tuple[cf.Contest, list[cf.Problem], list[cf.Submission]]],
            *args: Any,
        ) -> None:
            self.plotted.append([(contest.id, subs) for contest, _, subs in packed])
            plot(handle, rating, packed, *args)

        monkeypatch.setattr(graphs, '_plot_extreme', drawn)

    async def _status(self, *, handle: str) -> list[cf.Submission]:
        assert handle == EXTREME_HANDLE
        return self.submissions


SOLVED_A_AND_B = [
    contest_submission(EXTREME_PROBLEMS[CACHED][0], 'OK'),
    contest_submission(EXTREME_PROBLEMS[CACHED][1], 'OK'),
    contest_submission(EXTREME_PROBLEMS[CACHED][2], 'WRONG_ANSWER'),
]


@pytest.mark.parametrize(
    ('contest_ids', 'note'),
    [
        ([CACHED, UNCACHED, UNKNOWN], EXTREMES_LEFT_OUT_NOTE.format(count=2)),
        ([CACHED, UNCACHED], EXTREMES_LEFT_OUT_ONE_NOTE),
        ([UNKNOWN, CACHED], EXTREMES_LEFT_OUT_ONE_NOTE),
        ([CACHED], None),
    ],
    ids=['two left out', 'one without problems', 'one unknown', 'none left out'],
)
async def test_the_extremes_plot_leaves_out_contests_whose_problems_it_lacks(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    contest_ids: list[int],
    note: str | None,
) -> None:
    # A submission to a contest left out, which the plot doesn't read.
    elsewhere = contest_submission(extreme_problem(UNCACHED, 'A', 800), 'OK')
    codeforces = ExtremesCodeforces(
        bot, monkeypatch, events, contest_ids, [*SOLVED_A_AND_B, elsewhere]
    )
    ctx = make_context(bot, guild, 'plot extreme', events, slash=False)

    await Graphs.extreme.callback(cog, ctx, EXTREME_HANDLE)

    assert codeforces.plotted == [[(CACHED, SOLVED_A_AND_B)]]
    embed, _ = plot_sent(posted)
    assert embed.title == 'Codeforces extremes graph'
    assert embed.description == note


async def test_the_extremes_plot_says_so_when_the_bot_has_no_problems_to_plot(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codeforces = ExtremesCodeforces(
        bot, monkeypatch, events, [UNCACHED, UNKNOWN], SOLVED_A_AND_B
    )
    ctx = make_context(bot, guild, 'plot extreme', events, slash=False)

    with pytest.raises(GraphCogError) as raised:
        await Graphs.extreme.callback(cog, ctx, EXTREME_HANDLE)

    assert str(raised.value) == (
        "The bot doesn't have the problems of `tourist`'s contests yet, so there "
        'is nothing to plot.'
    )
    assert str(raised.value) == NO_EXTREMES_MESSAGE.format(handle=EXTREME_HANDLE)
    # Refused before the submissions are asked for.
    codeforces.status.assert_not_awaited()
    assert codeforces.plotted == []
    posted.assert_not_awaited()


@pytest.mark.parametrize(
    'contest_ids',
    [[CACHED], [CACHED, OTHER_CACHED]],
    ids=['one contest', 'two contests'],
)
async def test_the_extremes_plot_says_so_when_codeforces_has_not_rated_the_problems(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    contest_ids: list[int],
) -> None:
    # The bot has the problems of every contest of the handle, but they ended
    # too recently to be rated: in the bot's problems and in the submissions.
    # Such as a newcomer's first contest, for days after it.
    for contest_id in contest_ids:
        monkeypatch.setitem(EXTREME_PROBLEMS, contest_id, unrated(contest_id))
    submissions = [
        contest_submission(problem, 'OK')
        for contest_id in contest_ids
        for problem in EXTREME_PROBLEMS[contest_id][:2]
    ]
    codeforces = ExtremesCodeforces(bot, monkeypatch, events, contest_ids, submissions)
    ctx = make_context(bot, guild, 'plot extreme', events, slash=False)

    with pytest.raises(GraphCogError) as raised:
        await Graphs.extreme.callback(cog, ctx, EXTREME_HANDLE)

    assert str(raised.value) == (
        "Codeforces hasn't rated the problems of `tourist`'s contests yet, so "
        'there is nothing to plot.'
    )
    assert str(raised.value) == NO_RATED_EXTREMES_MESSAGE.format(handle=EXTREME_HANDLE)
    # Refused before the submissions are asked for, rather than posting a
    # plot without a point.
    codeforces.status.assert_not_awaited()
    assert codeforces.plotted == []
    posted.assert_not_awaited()


async def test_the_extremes_plot_is_sent_when_any_contest_has_rated_problems(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OTHER_CACHED's problems aren't rated yet, so it gives no point, but
    # CACHED's are. The note counts only UNCACHED, whose problems the bot
    # doesn't have.
    monkeypatch.setitem(EXTREME_PROBLEMS, OTHER_CACHED, unrated(OTHER_CACHED))
    recent = [contest_submission(EXTREME_PROBLEMS[OTHER_CACHED][0], 'OK')]
    codeforces = ExtremesCodeforces(
        bot,
        monkeypatch,
        events,
        [CACHED, OTHER_CACHED, UNCACHED],
        [*SOLVED_A_AND_B, *recent],
    )
    ctx = make_context(bot, guild, 'plot extreme', events, slash=False)

    await Graphs.extreme.callback(cog, ctx, EXTREME_HANDLE)

    assert codeforces.plotted == [[(CACHED, SOLVED_A_AND_B), (OTHER_CACHED, recent)]]
    embed, _ = plot_sent(posted)
    assert embed.title == 'Codeforces extremes graph'
    assert embed.description == EXTREMES_LEFT_OUT_ONE_NOTE


def test_the_notes_about_contests_left_out_count_them() -> None:
    assert EXTREMES_LEFT_OUT_NOTE.format(count=3) == (
        "Leaves out 3 contests whose problems the bot doesn't have yet."
    )
    assert EXTREMES_LEFT_OUT_ONE_NOTE == (
        "Leaves out 1 contest whose problems the bot doesn't have yet."
    )


@pytest.mark.parametrize('legend', [True, False])
async def test_the_extremes_plot_of_contests_all_solved_or_none_solved(
    bot: GraphsBot,
    cog: Graphs,
    guild: MagicMock,
    events: list[str],
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    legend: bool,
) -> None:
    # Every problem of one contest, and none of the other: no contest has
    # both a hardest solved and an easiest unsolved problem.
    submissions = [
        contest_submission(each, 'OK') for each in EXTREME_PROBLEMS[CACHED]
    ] + [contest_submission(EXTREME_PROBLEMS[OTHER_CACHED][0], 'WRONG_ANSWER')]
    codeforces = ExtremesCodeforces(
        bot, monkeypatch, events, [CACHED, OTHER_CACHED], submissions
    )
    ctx = make_context(bot, guild, 'plot extreme', events, slash=False)
    args = [EXTREME_HANDLE] if legend else [EXTREME_HANDLE, '+nolegend']

    # Plotted, without matplotlib's warning about a legend with no labels.
    with warnings.catch_warnings():
        warnings.filterwarnings('error', 'No artists with labels')
        await Graphs.extreme.callback(cog, ctx, *args)

    assert [contest_id for contest_id, _ in codeforces.plotted[0]] == [
        CACHED,
        OTHER_CACHED,
    ]
    embed, _ = plot_sent(posted)
    assert embed.description is None


# The help of the plot group


async def test_the_plot_group_s_help_explains_how_to_name_handles_and_filters(
    bot: GraphsBot,
) -> None:
    plot = bot.get_command('plot')
    assert plot is not None

    description, examples = split_help(plot.help)

    # Members by name, with quotes around a name with spaces.
    assert '`!Alice`' in description
    assert '`"!Alice Smith"`' in description
    # /help shows each filter on a line of its own.
    lines = description.splitlines()
    for token in (
        '`+contest`',
        '`+team`',
        '`+dp`',
        '`~math`',
        '`r>=1500`',
        '`d>=2024`',
        '`d<01062025`',
        '`c+div2`',
        '`i+A`',
    ):
        assert [line for line in lines if line.startswith('- ') and token in line]
    assert 'yyyy, mmyyyy or ddmmyyyy' in description
    assert '/plot distrib' in examples
    assert ';plot rating tourist !Alice' in examples


async def test_the_options_of_the_codeforces_distribution_say_their_defaults(
    bot: GraphsBot,
) -> None:
    command = bot.get_command('plot cfdistrib')
    assert isinstance(command, commands.HybridCommand)
    app = command.app_command
    assert app is not None

    # The slash command's options are as they were: only their descriptions
    # are new.
    assert [
        (option.name, option.type, option.required) for option in app.parameters
    ] == [
        ('mode', discord.AppCommandOptionType.string, False),
        ('activity', discord.AppCommandOptionType.string, False),
        ('contest_cutoff', discord.AppCommandOptionType.integer, False),
    ]
    descriptions = {option.name: option.description for option in app.parameters}
    assert descriptions['mode'].endswith('log or normal; log if left out')
    assert descriptions['activity'].endswith('active if left out')
    assert descriptions['contest_cutoff'].endswith('5 if left out')
    # discord.py works the prefix usage out for itself.
    assert command.usage is None
    assert command.signature == '[mode=log] [activity=active] [contest_cutoff=5]'
