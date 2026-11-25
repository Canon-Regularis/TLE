"""Tests for the duel commands and the buttons of a challenge (tle.cogs.duel).

The cog runs on a real in-memory user database (the ``user_db`` fixture),
holding real duelists, handles and duels. Its commands are run by calling
their callbacks, as discord.py does once it has parsed the arguments. The bot
is a real one with an access service and TLE's own rule table, as TLEBot has,
so the buttons of a challenge are checked as members press them. Discord
itself (the server, its members, the interaction) is mocked.
"""

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.access.help import describe_cooldown, split_help
from tle.access.rules import Limit, Who
from tle.access.service import OFF_TEXT, AccessService
from tle.cogs import duel
from tle.cogs.duel import (
    DuelChallengeView,
    DuelCogError,
    Dueling,
    check_if_allow_self_register,
)
from tle.util import codeforces_api as cf, codeforces_common as cf_common, paginator
from tle.util.db.user_db_conn import Duel, DuelType, UserDbConn, Winner
from tle.util.discord_common import NOT_ALLOWED_MESSAGE, embed_alert

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
MODERATOR_ROLE_ID = 1_300_000_000_000_000_002
# Members of the server, and members of another server only, with the
# Codeforces handles they linked in this server.
ALICE, BOB, CAROL, DAVE, ERIN = (1_400_000_000_000_000_001 + n for n in range(5))
XAVIER, YVONNE = 1_400_000_000_000_000_101, 1_400_000_000_000_000_102
NAMES = {
    ALICE: 'alice',
    BOB: 'bob',
    CAROL: 'carol',
    DAVE: 'dave',
    ERIN: 'erin',
    XAVIER: 'xavier',
    YVONNE: 'yvonne',
}
IN_SERVER = (ALICE, BOB, CAROL, DAVE, ERIN)
# A line of duel ongoing: '[alice](url) vs [bob](url): [problem](url) [1500] 1m 0s'.
ONGOING_LINE = re.compile(r'^\[(\w+)\]\(\S+\) vs \[(\w+)\]\(\S+\):')


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles, as the access service reads them: by name."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
def problem(make_problem: Any) -> cf.Problem:
    """The problem of every duel here."""
    problem: cf.Problem = make_problem(contestId=1, index='A', name='Test Problem')
    return problem


class DuelBot(commands.Bot):
    """A bot with what the cog reads, as TLEBot has: the user database, the
    Codeforces cache and the access service.
    """

    user_db: UserDbConn
    cf_cache: Any
    access: AccessService


@pytest.fixture
async def bot(
    user_db: UserDbConn, problem: cf.Problem, make_contest: Any
) -> AsyncIterator[DuelBot]:
    """The bot, whose access service has TLE's own rule table."""
    bot = DuelBot(command_prefix=';', intents=discord.Intents.none())
    bot.user_db = user_db
    bot.cf_cache = SimpleNamespace(
        problem_cache=SimpleNamespace(problem_by_name={problem.name: problem}),
        contest_cache=SimpleNamespace(get_contest=lambda _: make_contest(id=1)),
    )
    bot.access = AccessService(bot)
    yield bot
    await bot.close()


@pytest.fixture
def cog(bot: DuelBot) -> Dueling:
    return Dueling(bot)


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose members are those of IN_SERVER that tests add."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.members_by_id = {}
    guild.get_member.side_effect = guild.members_by_id.get
    return guild


@pytest.fixture
async def members(
    guild: MagicMock, user_db: UserDbConn, make_user: Any
) -> dict[int, MagicMock]:
    """The server's members, every one a duelist with a linked handle. The
    members of another server are duelists too.
    """
    for user_id, name in NAMES.items():
        await user_db.register_duelist(user_id)
        await user_db.cache_cf_user(make_user(handle=name))
        if user_id in IN_SERVER:
            await user_db.set_handle(user_id, GUILD_ID, name)
            guild.members_by_id[user_id] = make_member(guild, user_id)
    found: dict[int, MagicMock] = guild.members_by_id
    return found


def make_member(guild: MagicMock, user_id: int, *roles: str) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=user_id, guild=guild)
    member.mention = f'<@{user_id}>'
    member.display_name = NAMES[user_id]
    member.guild_permissions = discord.Permissions.none()
    member.roles = []
    for name in roles:
        role = MagicMock(spec=discord.Role, id=MODERATOR_ROLE_ID)
        role.name = name  # not MagicMock(name=...), which names the mock itself
        member.roles.append(role)
    return member


def make_ctx(author: MagicMock) -> MagicMock:
    """The context of a command ``author`` uses; replies are recorded."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = author.guild
    ctx.author = author
    ctx.channel = MagicMock(spec=discord.TextChannel)
    ctx.command = MagicMock(spec=commands.Command)
    ctx.send = AsyncMock()
    return ctx


def make_context(
    bot: DuelBot, author: MagicMock, events: list[str], *, slash: bool
) -> commands.Context[Any]:
    """A real context of a command ``author`` uses, prefix or slash.

    ``events`` records the typing indicator that a prefix command shows, or
    the deferral that a slash command sends instead. Replies are recorded by
    an ``AsyncMock`` in place of ``send``.
    """

    async def send_typing(channel_id: int) -> None:
        events.append('typing')

    async def defer(*, ephemeral: bool = False) -> None:
        events.append('deferred')

    channel = MagicMock(spec=discord.TextChannel, id=1)
    channel._state = SimpleNamespace(http=SimpleNamespace(send_typing=send_typing))
    message = MagicMock(
        spec=discord.Message, guild=author.guild, author=author, channel=channel
    )
    message._state = SimpleNamespace(loop=asyncio.get_running_loop())
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = False
        interaction.response.is_done.return_value = False
        interaction.response.defer = defer
    ctx: commands.Context[Any] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    ctx.send = AsyncMock()  # type: ignore[method-assign]
    return ctx


async def add_duel(
    user_db: UserDbConn,
    problem: cf.Problem,
    challenger: int,
    challengee: int,
    status: Duel,
    *,
    start: float | None = None,
) -> int:
    """A new duel: pending, ongoing since ``start``, or finished 5 minutes
    after ``start`` and won by its challenger.
    """
    duelid = await user_db.create_duel(
        challenger, challengee, time.time(), problem, DuelType.OFFICIAL
    )
    assert duelid is not None
    if status is Duel.PENDING:
        return duelid
    start = time.time() if start is None else start
    await user_db.start_duel(duelid, start)
    if status is Duel.COMPLETE:
        await user_db.complete_duel(
            duelid, Winner.CHALLENGER, start + 300, dtype=DuelType.UNOFFICIAL
        )
    return duelid


def sent(ctx: MagicMock) -> tuple[str | None, discord.Embed]:
    """The text and embed of the one reply to ``ctx``."""
    ctx.send.assert_awaited_once()
    args, kwargs = ctx.send.await_args
    embed = kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return (args[0] if args else None), embed


def duel_ids(embed: discord.Embed) -> list[int]:
    """The ids of the duels on a page of duel recent, in order."""
    assert embed.description is not None
    return [int(line.split(':', 1)[0]) for line in embed.description.splitlines()]


def shown_pairs(ctx: MagicMock) -> list[tuple[str, str]]:
    """The duelists of each duel that duel ongoing showed, in order."""
    content, embed = sent(ctx)
    assert content == 'Ongoing duels in this server'
    assert embed.description is not None
    pairs = []
    for line in embed.description.splitlines():
        found = ONGOING_LINE.match(line)
        assert found is not None, line
        pairs.append((found[1], found[2]))
    return pairs


# Who may use each command


def test_the_access_rules_alone_decide_who_uses_each_command(cog: Dueling) -> None:
    # duel register and duel _invalidate checked TLE's roles themselves. The
    # rule table now decides, so that /help offers what the member can use.
    checked = {
        command.qualified_name: command.checks
        for command in cog.walk_commands()
        if command.checks
    }

    assert checked == {'duel selfregister': [check_if_allow_self_register]}


def test_self_registration_can_still_be_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(constants, 'ALLOW_DUEL_SELF_REGISTER', False)

    with pytest.raises(DuelCogError) as refused:
        check_if_allow_self_register(MagicMock(spec=commands.Context))

    assert str(refused.value) == (
        'Self-registration is switched off. Ask a moderator to register you.'
    )


@pytest.mark.parametrize('name', ['duel', 'selfregister'])
def test_the_help_says_who_registers_you_while_self_registration_is_off(
    name: str,
) -> None:
    # As it is unless .env switches it on: the help used to send members to
    # /duel selfregister alone, which then refused them.
    description, _ = split_help(getattr(Dueling, name).help)

    assert 'ask a moderator to register you' in description


# Texts


async def test_declining_mentions_both_duelists(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    await add_duel(user_db, problem, ALICE, BOB, Duel.PENDING)
    ctx = make_ctx(members[BOB])

    await type(cog).decline.callback(cog, ctx)

    _, embed = sent(ctx)
    # Mentions, not code spans that show their raw text.
    assert embed.description == f'<@{BOB}> declined a challenge by <@{ALICE}>.'
    assert await user_db.check_duel_decline(BOB) is None


async def test_withdrawing_mentions_both_duelists(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    await add_duel(user_db, problem, ALICE, BOB, Duel.PENDING)
    ctx = make_ctx(members[ALICE])

    await type(cog).withdraw.callback(cog, ctx)

    _, embed = sent(ctx)
    assert embed.description == f'<@{ALICE}> withdrew a challenge to <@{BOB}>.'
    assert await user_db.check_duel_withdraw(ALICE) is None


@pytest.mark.parametrize(
    ('name', 'quoted'),
    [
        ('challenge', f'rated about {-duel._DUEL_RATING_DELTA} below the lower'),
        ('challenge', 'a rating: a multiple of 100 from 800 to 3500.'),
        (
            'challenge',
            'The challenge expires after '
            f'{cf_common.pretty_time_format(duel._DUEL_EXPIRY_TIME)}.',
        ),
        (
            'draw',
            'once the duel has lasted '
            f'{cf_common.pretty_time_format(duel._DUEL_NO_DRAW_TIME)}.',
        ),
        (
            'invalidate',
            'You can invalidate a duel only in its first '
            f'{cf_common.pretty_time_format(duel._DUEL_INVALIDATE_TIME)}.',
        ),
    ],
)
def test_the_help_quotes_the_numbers_the_cog_uses(name: str, quoted: str) -> None:
    # As /help shows it: the lines of a paragraph joined.
    description, examples = split_help(getattr(Dueling, name).help)

    assert quoted in description
    assert examples


async def test_invalidating_too_late_says_how_long_a_duel_allows(
    cog: Dueling,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window comes from the constant, whatever it is.
    monkeypatch.setattr(duel, '_DUEL_INVALIDATE_TIME', 5 * 60)
    await add_duel(
        user_db, problem, ALICE, BOB, Duel.ONGOING, start=time.time() - 6 * 60
    )

    with pytest.raises(DuelCogError) as refused:
        await type(cog).invalidate.callback(cog, make_ctx(members[ALICE]))

    assert str(refused.value) == (
        f'<@{ALICE}>, you can invalidate a duel only in its first 5 minutes.'
    )
    assert await user_db.check_duel_complete(ALICE) is not None


async def test_invalidating_in_time_ends_the_duel(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    await add_duel(user_db, problem, ALICE, BOB, Duel.ONGOING, start=time.time())
    ctx = make_ctx(members[BOB])

    await type(cog).invalidate.callback(cog, ctx)

    ctx.send.assert_awaited_once_with(
        f'Duel between <@{ALICE}> and <@{BOB}> has been invalidated.'
    )
    assert await user_db.check_duel_complete(ALICE) is None


async def test_ending_a_duel_that_already_ended_says_so(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    duelid = await add_duel(user_db, problem, ALICE, BOB, Duel.COMPLETE)

    with pytest.raises(DuelCogError) as refused:
        await cog._complete_duel(
            duelid,
            GUILD_ID,
            Winner.CHALLENGEE,
            members[BOB],
            members[ALICE],
            time.time(),
            1,
            DuelType.OFFICIAL,
        )

    assert str(refused.value) == 'This duel has already ended.'


# duel recent and duel ongoing: this server's duels only


async def test_recent_shows_the_latest_duels_between_this_server_s_members(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    alice_bob = await add_duel(user_db, problem, ALICE, BOB, Duel.COMPLETE, start=10)
    bob_carol = await add_duel(user_db, problem, BOB, CAROL, Duel.COMPLETE, start=20)
    # Newer duels, with a member of another server or between two of them,
    # more than the list holds.
    await add_duel(user_db, problem, ALICE, XAVIER, Duel.COMPLETE, start=30)
    await add_duel(user_db, problem, XAVIER, DAVE, Duel.COMPLETE, start=40)
    for start in range(50, 50 + duel._RECENT_DUELS + 1):
        await add_duel(user_db, problem, XAVIER, YVONNE, Duel.COMPLETE, start=start)
    # An ongoing duel isn't finished yet.
    await add_duel(user_db, problem, CAROL, DAVE, Duel.ONGOING, start=100)
    ctx = make_ctx(members[ERIN])

    await type(cog).recent.callback(cog, ctx)

    content, embed = sent(ctx)
    assert content == 'Recent duels in this server'
    assert duel_ids(embed) == [bob_carol, alice_bob]


async def test_recent_shows_at_most_its_number_of_duels_the_newest_first(
    cog: Dueling,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = [(ALICE, BOB), (CAROL, DAVE), (ERIN, ALICE), (BOB, CAROL)]
    duels = [
        await add_duel(user_db, problem, *pairs[n % 4], Duel.COMPLETE, start=n)
        for n in range(duel._RECENT_DUELS + 2)
    ]
    shown: list[int] = []

    async def paginate(
        channel: Any, pages: list[paginator.Page], **kwargs: Any
    ) -> None:
        # Every page, not only the first that is sent.
        for _, embed in pages:
            shown.extend(duel_ids(embed))

    monkeypatch.setattr(paginator, 'paginate', paginate)

    await type(cog).recent.callback(cog, make_ctx(members[ALICE]))

    assert shown == duels[::-1][: duel._RECENT_DUELS]


async def test_recent_without_duels_in_this_server_says_so(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    await add_duel(user_db, problem, ALICE, XAVIER, Duel.COMPLETE, start=10)

    with pytest.raises(DuelCogError, match='^There are no duels to show.$'):
        await type(cog).recent.callback(cog, make_ctx(members[ALICE]))


@pytest.fixture
async def ongoing(
    user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    """Duels going on between members, with a member of another server, and
    between members of another server.
    """
    now = time.time()
    await add_duel(user_db, problem, ALICE, BOB, Duel.ONGOING, start=now - 60)
    await add_duel(user_db, problem, CAROL, DAVE, Duel.ONGOING, start=now - 120)
    await add_duel(user_db, problem, ERIN, XAVIER, Duel.ONGOING, start=now - 180)
    await add_duel(user_db, problem, YVONNE, XAVIER, Duel.ONGOING, start=now - 240)


@pytest.mark.usefixtures('ongoing')
async def test_ongoing_shows_the_duels_between_this_server_s_members(
    cog: Dueling, members: dict[int, Any]
) -> None:
    ctx = make_ctx(members[ALICE])

    # Without a member, every duel here, not only that of the member asking.
    await type(cog).ongoing.callback(cog, ctx)

    assert shown_pairs(ctx) == [('alice', 'bob'), ('carol', 'dave')]


@pytest.mark.usefixtures('ongoing')
async def test_ongoing_with_a_member_shows_their_duel_alone(
    cog: Dueling, members: dict[int, Any]
) -> None:
    ctx = make_ctx(members[ALICE])

    await type(cog).ongoing.callback(cog, ctx, members[DAVE])

    assert shown_pairs(ctx) == [('carol', 'dave')]


@pytest.mark.usefixtures('ongoing')
async def test_ongoing_with_a_member_without_a_duel_here_says_so(
    cog: Dueling, members: dict[int, Any]
) -> None:
    # Erin's duel is with a member of another server.
    with pytest.raises(DuelCogError) as refused:
        await type(cog).ongoing.callback(cog, make_ctx(members[ALICE]), members[ERIN])

    assert str(refused.value) == f'<@{ERIN}> has no ongoing duel in this server.'


async def test_ongoing_without_duels_in_this_server_says_so(
    cog: Dueling, user_db: UserDbConn, problem: cf.Problem, members: dict[int, Any]
) -> None:
    await add_duel(user_db, problem, ALICE, XAVIER, Duel.ONGOING)

    with pytest.raises(DuelCogError) as refused:
        await type(cog).ongoing.callback(cog, make_ctx(members[BOB]))

    assert str(refused.value) == 'There are no ongoing duels in this server.'


# Cooldowns, and slash commands that need more than 3 seconds


@pytest.mark.parametrize(
    ('name', 'cooldown'),
    [
        ('challenge', 'Once every 20 seconds for each member'),
        ('complete', 'Once every 10 seconds for each member'),
        ('rating', 'Once every 20 seconds for each member'),
    ],
)
def test_commands_that_ask_codeforces_or_plot_have_cooldowns(
    name: str, cooldown: str
) -> None:
    assert describe_cooldown(getattr(Dueling, name)) == cooldown


@pytest.mark.parametrize('name', ['challenge', 'rating'])
def test_a_mistyped_command_spends_no_cooldown(name: str) -> None:
    # discord.py parses the arguments first, and counts the use only then.
    assert getattr(Dueling, name).cooldown_after_parsing


@pytest.mark.parametrize(
    ('challenger', 'opponent', 'refusal'),
    [
        (ERIN, BOB, 'you are not a registered duelist!'),
        (BOB, ERIN, 'is not a registered duelist!'),
        (BOB, BOB, 'you cannot challenge yourself!'),
        (ALICE, BOB, 'you are currently in a duel!'),
        (CAROL, ALICE, 'is currently in a duel!'),
    ],
    ids=[
        'not a duelist',
        'opponent not a duelist',
        'yourself',
        'in a duel',
        'opponent in a duel',
    ],
)
async def test_a_refused_challenge_asks_codeforces_nothing(
    cog: Dueling,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    monkeypatch: pytest.MonkeyPatch,
    challenger: int,
    opponent: int,
    refusal: str,
) -> None:
    # Codeforces is asked through one limiter for the whole bot, so refused
    # challenges, as repeated ones are, must not queue requests there.
    await add_duel(user_db, problem, ALICE, DAVE, Duel.PENDING)
    await user_db.conn.execute('DELETE FROM duelist WHERE user_id = ?', (ERIN,))
    await user_db.conn.commit()
    status = AsyncMock(return_value=[])
    monkeypatch.setattr(cf.user, 'status', status)
    monkeypatch.setattr(cf_common, 'resolve_handles', AsyncMock())
    ctx = make_ctx(members[challenger])

    with pytest.raises(DuelCogError, match=refusal):
        await type(cog).challenge.callback(cog, ctx, members[opponent])

    status.assert_not_awaited()
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_complete_shows_it_is_working_before_it_asks_codeforces(
    bot: DuelBot,
    cog: Dueling,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    monkeypatch: pytest.MonkeyPatch,
    slash: bool,
) -> None:
    await add_duel(user_db, problem, ALICE, BOB, Duel.ONGOING, start=time.time())
    events: list[str] = []

    async def status(*, handle: str) -> list[cf.Submission]:
        events.append(f'asked Codeforces about {handle}')
        return []

    monkeypatch.setattr(cf.user, 'status', status)
    ctx = make_context(bot, members[ALICE], events, slash=slash)

    await type(cog).complete.callback(cog, ctx)

    # A slash command has 3 seconds to answer, so it defers first.
    working = 'deferred' if slash else 'typing'
    assert events == [
        working,
        'asked Codeforces about alice',
        'asked Codeforces about bob',
    ]
    send = ctx.send
    assert isinstance(send, AsyncMock)
    send.assert_awaited_once_with('Nobody solved the problem yet.')


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_complete_outside_a_duel_answers_at_once(
    bot: DuelBot, cog: Dueling, members: dict[int, Any], slash: bool
) -> None:
    events: list[str] = []
    ctx = make_context(bot, members[ALICE], events, slash=slash)

    with pytest.raises(DuelCogError, match='you are not in a duel'):
        await type(cog).complete.callback(cog, ctx)

    assert events == []


# The rating of a challenge


RATING_REFUSAL = (
    'Give at most one rating, a multiple of 100 from 800 to 3500.'
    ' Tags start with + or ~.'
)


class CountingProblemCache:
    """A problem cache that counts how often its problems are read."""

    def __init__(self, problems: list[cf.Problem]) -> None:
        self._problems = problems
        self.reads = 0

    @property
    def problems(self) -> list[cf.Problem]:
        self.reads += 1
        return self._problems


@pytest.fixture
def status(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Codeforces' answer about a member's submissions: none."""
    status = AsyncMock(return_value=[])
    monkeypatch.setattr(cf.user, 'status', status)
    return status


@pytest.fixture
def resolve_handles(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    resolve_handles = AsyncMock()
    monkeypatch.setattr(cf_common, 'resolve_handles', resolve_handles)
    return resolve_handles


@pytest.fixture
def offer(
    bot: DuelBot,
    status: AsyncMock,
    resolve_handles: AsyncMock,
    make_problem: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., CountingProblemCache]:
    """Makes the problem set: a problem of each rating given, named after it."""

    def offer(*ratings: int | None) -> CountingProblemCache:
        cache = CountingProblemCache(
            [make_problem(name=f'P{rating}', rating=rating) for rating in ratings]
        )
        bot.cf_cache.problem_cache = cache
        return cache

    monkeypatch.setattr(cf_common, 'cf_cache', bot.cf_cache)
    return offer


async def challenge_bob(cog: Dueling, ctx: MagicMock, *args: str) -> None:
    """Challenge Bob with the arguments ``args``."""
    bob = ctx.guild.get_member(BOB)
    await type(cog).challenge.callback(cog, ctx, bob, *args)


async def stored_duels(user_db: UserDbConn) -> list[tuple[str, DuelType]]:
    """The problem and type of every duel stored."""
    cursor = await user_db.conn.execute('SELECT problem_name, type FROM duel')
    return [(name, DuelType(dtype)) for name, dtype in await cursor.fetchall()]


@pytest.mark.parametrize(
    'args',
    [
        ('0',),
        ('400',),
        ('700',),
        ('799',),
        ('801',),
        ('1550',),
        ('3499',),
        ('3501',),
        ('3600',),
        ('9' * 4000,),
        ('-1600',),
        ('1600.0',),
        ('1e3',),
        ('1_600',),
        ('01600',),
        ('²',),  # a superscript 2
        ('１６００',),  # 1600 in fullwidth digits
        (' 1600',),
        ('',),
        ('greedy',),
        ('1600', '1700'),
        ('+dp', '1600', 'greedy'),
    ],
    ids=lambda args: ascii(' '.join(args)[:20]),
)
async def test_a_challenge_with_anything_but_a_rating_and_tags_is_refused(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    offer: Callable[..., CountingProblemCache],
    status: AsyncMock,
    resolve_handles: AsyncMock,
    args: tuple[str, ...],
) -> None:
    cache = offer(800, 1600)
    ctx = make_ctx(members[ALICE])

    with pytest.raises(DuelCogError) as refused:
        await challenge_bob(cog, ctx, *args)

    assert str(refused.value) == RATING_REFUSAL
    resolve_handles.assert_not_awaited()
    status.assert_not_awaited()
    assert cache.reads == 0
    assert await stored_duels(user_db) == []
    # It cost nothing, so it doesn't hold the member back.
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


@pytest.mark.parametrize('rating', [800, 900, 3400, 3500])
async def test_a_challenge_takes_ratings_up_to_each_bound(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    offer: Callable[..., CountingProblemCache],
    rating: int,
) -> None:
    offer(800, 900, 3400, 3500)
    ctx = make_ctx(members[ALICE])

    await challenge_bob(cog, ctx, str(rating))

    assert [name for name, _ in await stored_duels(user_db)] == [f'P{rating}']
    assert f'{rating} rated duel!' in ctx.send.await_args.args[0]
    ctx.command.reset_cooldown.assert_not_called()


@pytest.mark.parametrize(
    'args',
    [
        ('1600', '+dp', '~math'),
        ('+dp', '1600', '~math'),
        ('+dp', '~math', '1600'),
    ],
)
async def test_tags_go_before_or_after_the_rating(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    offer: Callable[..., CountingProblemCache],
    make_problem: Any,
    monkeypatch: pytest.MonkeyPatch,
    args: tuple[str, ...],
) -> None:
    # The last candidate is chosen, so one let through by mistake would be.
    monkeypatch.setattr(duel.random, 'randrange', lambda stop: stop - 1)
    offer().problems.extend(
        [
            make_problem(name='wanted', rating=1600, tags=['dp']),
            make_problem(name='no tag', rating=1600),
            make_problem(name='left out', rating=1600, tags=['dp', 'math']),
        ]
    )

    await challenge_bob(cog, make_ctx(members[ALICE]), *args)

    assert await stored_duels(user_db) == [('wanted', DuelType.UNOFFICIAL)]


@pytest.mark.parametrize(
    ('lower', 'rating'),
    [
        (None, 800),
        (900, 800),
        (1200, 800),
        (1300, 900),
        (3900, 3500),
        (4000, 3500),
    ],
)
async def test_the_default_rating_is_one_that_problems_have(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    make_user: Any,
    offer: Callable[..., CountingProblemCache],
    lower: int | None,
    rating: int,
) -> None:
    await user_db.cache_cf_user(make_user(handle='alice', rating=4000))
    await user_db.cache_cf_user(make_user(handle='bob', rating=lower))
    offer(800, 900, 3400, 3500)
    ctx = make_ctx(members[ALICE])

    await challenge_bob(cog, ctx)

    assert await stored_duels(user_db) == [(f'P{rating}', DuelType.OFFICIAL)]
    assert f'{rating} rated duel!' in ctx.send.await_args.args[0]


@pytest.mark.parametrize(
    ('rating', 'dtype'),
    [(2500, DuelType.OFFICIAL), (2600, DuelType.OFFICIAL), (2700, DuelType.UNOFFICIAL)],
)
async def test_a_rating_above_the_default_makes_the_duel_unofficial(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    offer: Callable[..., CountingProblemCache],
    rating: int,
    dtype: DuelType,
) -> None:
    # Both members are rated 3000.
    offer(rating)

    await challenge_bob(cog, make_ctx(members[ALICE]), str(rating))

    assert await stored_duels(user_db) == [(f'P{rating}', dtype)]


async def test_a_challenge_falls_back_to_the_hardest_easier_problem(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    status: AsyncMock,
    offer: Callable[..., CountingProblemCache],
    make_problem: Any,
) -> None:
    # In no order, with gaps; a problem may have no rating.
    cache = offer(900, None, 1700, 1300, 1600)
    status.return_value = [
        SimpleNamespace(problem=make_problem(name='P1600'), verdict='WRONG_ANSWER')
    ]

    await challenge_bob(cog, make_ctx(members[ALICE]), '1600')

    assert await stored_duels(user_db) == [('P1300', DuelType.OFFICIAL)]
    assert cache.reads == 1


@pytest.mark.parametrize('ratings', [(), (None,), (1700, 3500), (800, 1600)], ids=str)
async def test_a_challenge_without_a_problem_says_so(
    cog: Dueling,
    user_db: UserDbConn,
    members: dict[int, Any],
    status: AsyncMock,
    offer: Callable[..., CountingProblemCache],
    make_problem: Any,
    ratings: tuple[int | None, ...],
) -> None:
    cache = offer(*ratings)
    status.return_value = [
        SimpleNamespace(problem=make_problem(name=f'P{rating}'), verdict='OK')
        for rating in (800, 1600)
    ]
    ctx = make_ctx(members[ALICE])

    with pytest.raises(DuelCogError) as refused:
        await challenge_bob(cog, ctx, '1600')

    assert str(refused.value) == (
        f'No unsolved 1600 rated problems left for <@{ALICE}> vs <@{BOB}>.'
    )
    assert cache.reads == 1
    assert await stored_duels(user_db) == []
    # Codeforces was asked, so the cooldown stays.
    assert status.await_count == 2
    ctx.command.reset_cooldown.assert_not_called()


# The buttons of a challenge follow their commands' access rules


BUTTONS = [
    ('accept_button', 'duel accept', BOB),
    ('decline_button', 'duel decline', BOB),
    ('withdraw_button', 'duel withdraw', ALICE),
]
# Where each button leaves the challenge when it works.
AFTER = {
    'accept_button': Duel.ONGOING,
    'decline_button': Duel.DECLINED,
    'withdraw_button': Duel.WITHDRAWN,
}


def press_by(bot: DuelBot, member: MagicMock) -> MagicMock:
    """A press of a button of a challenge, by ``member`` in the server."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.guild_id = GUILD_ID
    interaction.guild = member.guild
    interaction.user = member
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.send = AsyncMock()
    interaction.is_expired.return_value = False
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def answered(interaction: MagicMock) -> str | None:
    """The text of the one private alert that answered ``interaction``."""
    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.await_args.kwargs['ephemeral'] is True
    embed = send.await_args.kwargs['embed']
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    description: str | None = embed.description
    return description


async def status_of(user_db: UserDbConn, duelid: int) -> Duel:
    cursor = await user_db.conn.execute(
        'SELECT status FROM duel WHERE id = ?', (duelid,)
    )
    row = await cursor.fetchone()
    assert row is not None
    return Duel(row[0])


async def limit(bot: DuelBot, key: str, new: Limit) -> None:
    """Set this server's limit ``key``, as /access limit does."""
    await bot.access.change(GUILD_ID, lambda settings: settings.with_limit(key, new))


async def press(
    bot: DuelBot,
    user_db: UserDbConn,
    problem: cf.Problem,
    button: str,
    member: MagicMock,
) -> tuple[int, MagicMock]:
    """Challenge Bob for Alice, and have ``member`` press ``button`` under the
    challenge: the duel's id and the press.
    """
    duelid = await add_duel(user_db, problem, ALICE, BOB, Duel.PENDING)
    view = DuelChallengeView(bot, duelid, ALICE, BOB, problem.name, timeout=60)
    interaction = press_by(bot, member)
    with patch('tle.cogs.duel.asyncio.sleep', new_callable=AsyncMock):
        await getattr(view, button).callback(interaction)
    return duelid, interaction


@pytest.mark.parametrize(('button', 'command', 'duelist'), BUTTONS)
async def test_a_button_of_a_switched_off_command_leaves_the_challenge(
    bot: DuelBot,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    button: str,
    command: str,
    duelist: int,
) -> None:
    await limit(bot, command, Limit(off=True))

    duelid, interaction = await press(bot, user_db, problem, button, members[duelist])

    assert answered(interaction) == OFF_TEXT
    assert await status_of(user_db, duelid) is Duel.PENDING
    interaction.response.edit_message.assert_not_awaited()
    interaction.channel.send.assert_not_awaited()


@pytest.mark.parametrize(('button', 'command', 'duelist'), BUTTONS)
async def test_a_button_works_while_the_other_buttons_commands_are_off(
    bot: DuelBot,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    button: str,
    command: str,
    duelist: int,
) -> None:
    for _, other, _ in BUTTONS:
        if other != command:
            await limit(bot, other, Limit(off=True))

    duelid, interaction = await press(bot, user_db, problem, button, members[duelist])

    assert await status_of(user_db, duelid) is AFTER[button]
    interaction.response.send_message.assert_not_awaited()
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.parametrize(
    ('roles', 'after'), [((), Duel.PENDING), (('Moderator',), Duel.DECLINED)]
)
async def test_a_button_is_for_those_its_command_is_for(
    bot: DuelBot,
    user_db: UserDbConn,
    problem: cf.Problem,
    members: dict[int, Any],
    guild: MagicMock,
    roles: tuple[str, ...],
    after: Duel,
) -> None:
    # This server keeps duels for its moderators.
    await limit(bot, 'duel *', Limit(who=Who.MODERATOR))
    bob = make_member(guild, BOB, *roles)

    duelid, interaction = await press(bot, user_db, problem, 'decline_button', bob)

    assert await status_of(user_db, duelid) is after
    if after is Duel.PENDING:
        assert answered(interaction) == NOT_ALLOWED_MESSAGE
