"""Tests for how commands find the Codeforces handles they are about
(resolve_handles in tle.util.codeforces_common, and the commands that call it).

Handles are linked in a real in-memory user database (the ``user_db`` fixture),
and names are looked up by discord.py's own converter, in a real
``discord.Guild`` with real members, several of whom share a name. Commands run
by calling their callbacks, as discord.py does once it has parsed the arguments.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from tle.cogs.codeforces import Codeforces, CodeforcesCogError
from tle.cogs.duel import Dueling
from tle.cogs.graphs import Graphs
from tle.util import codeforces_api as cf, codeforces_common as cf_common
from tle.util.codeforces_common import (
    AmbiguousMemberError,
    FindMemberFailedError,
    HandleCountOutOfBoundsError,
    HandleIsVjudgeError,
    HandleNotRegisteredError,
    ResolveHandleError,
    SubFilter,
    resolve_handle_pairs,
    resolve_handles,
)
from tle.util.db.user_db_conn import UserDbConn

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
# The username, global name and nickname of a member who took one of Alice's
# names: what she is called in str(), global_name and display_name.
FIRST_ID = 1_400_000_000_000_000_001
IMPOSTORS = {
    'username': ('alice', None, None),
    'nickname like a username': ('mallory1', None, 'alice'),
    'global name': ('mallory2', 'Alice', None),
    'nickname like a global name': ('mallory3', None, 'Alice'),
    'nickname': ('mallory4', None, 'Ally'),
}
ALICE, BOB, CAROL, DAVE = (1_400_000_000_000_000_100 + n for n in range(4))
# Each member's username, global name, nickname and handle.
ORDINARY = {
    ALICE: ('alice', 'Alice', 'Ally', 'alice_cf'),
    BOB: ('bob', None, None, 'Bob_CF'),
    CAROL: ('carol', 'Carol', 'CAZ', 'carol_cf'),
    DAVE: ('dave', None, None, None),
}


def member_data(
    member_id: int, name: str, global_name: str | None, nick: str | None
) -> Any:
    return {
        'user': {
            'id': member_id,
            'username': name,
            'global_name': global_name,
            'discriminator': '0',
            'avatar': None,
        },
        'nick': nick,
        'roles': [],
        'joined_at': None,
        'flags': 0,
    }


@pytest.fixture
def state() -> MagicMock:
    """discord.py's state, which makes the users of real members."""
    state = MagicMock()
    state.store_user.side_effect = lambda data, **_: discord.User(
        state=state, data=data
    )
    # A member who isn't in the cache isn't on the server either.
    state.query_members = AsyncMock(return_value=[])
    return state


@pytest.fixture
def guild(state: MagicMock) -> discord.Guild:
    """A server with the ordinary members, each with their own names."""
    guild = discord.Guild(
        data={'id': GUILD_ID, 'name': 'Server', 'member_count': 4},  # type: ignore[typeddict-item]
        state=state,
    )
    for member_id, (name, global_name, nick, _) in ORDINARY.items():
        add_member(guild, member_id, name, global_name, nick)
    return guild


def add_member(
    guild: discord.Guild,
    member_id: int,
    name: str,
    global_name: str | None = None,
    nick: str | None = None,
) -> discord.Member:
    member = discord.Member(
        data=member_data(member_id, name, global_name, nick),
        guild=guild,
        state=guild._state,
    )
    guild._add_member(member)
    return member


def put_first(guild: discord.Guild, names: tuple[str, str | None, str | None]) -> None:
    """Add a member with ``names`` before all others, where discord.py's lookup
    by name finds them first.
    """
    others = guild._members
    guild._members = {}
    add_member(guild, FIRST_ID, *names)
    guild._members.update(others)


@pytest.fixture
async def linked(user_db: UserDbConn, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary members' handles, linked in the server. The member whom
    ``put_first`` adds has linked 'impostor'.
    """
    monkeypatch.setattr(cf_common, 'user_db', user_db)
    for member_id, (*_, handle) in ORDINARY.items():
        if handle is not None:
            await user_db.set_handle(member_id, GUILD_ID, handle)
    await user_db.set_handle(FIRST_ID, GUILD_ID, 'impostor')


@pytest.fixture
def ctx(guild: discord.Guild) -> MagicMock:
    """The context of a command that Alice uses."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = guild.get_member(ALICE)
    ctx.message = MagicMock(spec=discord.Message, author=ctx.author, mentions=[])
    ctx.bot = MagicMock(spec=commands.Bot)
    ctx.bot._get_websocket.return_value.is_ratelimited.return_value = False
    ctx.command = MagicMock(spec=commands.Command)
    ctx.send = AsyncMock()
    return ctx


Resolve = Callable[..., Any]


@pytest.fixture
def resolve(ctx: MagicMock, linked: None) -> Resolve:
    """Resolves names in ``ctx``."""

    async def resolve(*names: str | discord.Member, **bounds: Any) -> list[str]:
        return await resolve_handles(ctx, commands.MemberConverter(), names, **bounds)

    return resolve


@pytest.fixture
def pairs(ctx: MagicMock, linked: None) -> Resolve:
    """Pairs names with their handles in ``ctx``."""

    async def pairs(*names: str | discord.Member, **bounds: Any) -> Any:
        return await resolve_handle_pairs(
            ctx, commands.MemberConverter(), names, **bounds
        )

    return pairs


# Order and duplicates


async def test_handles_keep_the_order_they_were_given_in(resolve: Resolve) -> None:
    names = ['zed', '!carol', 'Petr', 'tourist', '!bob', 'ecnerwala']

    found = await resolve(*names, maxcnt=None)

    assert found == ['zed', 'carol_cf', 'Petr', 'tourist', 'Bob_CF', 'ecnerwala']
    assert await resolve(*names[::-1], maxcnt=None) == found[::-1]


async def test_each_handle_comes_once_at_its_first_place(resolve: Resolve) -> None:
    found = await resolve('tourist', '!bob', 'tourist', 'Bob_CF', '!bob', 'Petr')

    assert found == ['tourist', 'Bob_CF', 'Petr']


async def test_spellings_are_kept_apart(resolve: Resolve) -> None:
    # The standings find a handle only as Codeforces spells it.
    assert await resolve('tourist', 'Tourist') == ['tourist', 'Tourist']


async def test_pairs_tell_what_each_name_means(
    pairs: Resolve, guild: discord.Guild
) -> None:
    alice = guild.get_member(ALICE)

    assert await pairs('tourist', alice, '!bob', 'Bob_CF', 'tourist') == [
        ('tourist', 'tourist'),
        (alice, 'alice_cf'),
        ('!bob', 'Bob_CF'),
        ('Bob_CF', 'Bob_CF'),
    ]


async def test_the_number_of_names_is_counted_without_duplicates(
    resolve: Resolve,
) -> None:
    assert await resolve('a', 'b', 'a', 'b', maxcnt=2) == ['a', 'b']
    assert await resolve(mincnt=0) == []

    for names in ((), ('a', 'b', 'c')):
        with pytest.raises(HandleCountOutOfBoundsError):
            await resolve(*names, maxcnt=2)


async def test_at_most_five_names_by_default(resolve: Resolve, pairs: Resolve) -> None:
    for find in (resolve, pairs):
        assert len(await find(*'abcde')) == 5

        with pytest.raises(HandleCountOutOfBoundsError):
            await find(*'abcdef')


def test_the_filters_of_the_plots_keep_the_handles_in_order() -> None:
    names = ['zed', '!carol', 'r>=1500', 'Petr', '+dp', 'tourist', 'zed']

    assert SubFilter().parse(names) == ['zed', '!carol', 'Petr', 'tourist']


async def test_the_server_s_handles_come_after_those_named(pairs: Resolve) -> None:
    found = await pairs('+server', 'tourist', 'Bob_CF', maxcnt=None)

    assert found[:2] == [('tourist', 'tourist'), ('Bob_CF', 'Bob_CF')]
    assert sorted(found[2:]) == [
        ('+server', 'alice_cf'),
        ('+server', 'carol_cf'),
        ('+server', 'impostor'),
    ]


async def test_the_server_s_handles_leave_out_those_named_otherwise(
    pairs: Resolve, guild: discord.Guild
) -> None:
    found = await pairs('!bob', guild.get_member(CAROL), '+server', maxcnt=None)

    assert found[:2] == [('!bob', 'Bob_CF'), (guild.get_member(CAROL), 'carol_cf')]
    assert sorted(found[2:]) == [('+server', 'alice_cf'), ('+server', 'impostor')]


async def test_the_server_s_handles_count_towards_the_bounds(resolve: Resolve) -> None:
    assert len(await resolve('+server', 'tourist', maxcnt=5)) == 5
    assert len(await resolve(default_to_all_server=True)) == 4
    assert await resolve('tourist', default_to_all_server=True) == ['tourist']

    with pytest.raises(HandleCountOutOfBoundsError):
        await resolve('+server', 'tourist', maxcnt=4)


async def test_a_shared_judge_account_is_refused_however_it_is_named(
    resolve: Resolve, user_db: UserDbConn
) -> None:
    with pytest.raises(HandleIsVjudgeError):
        await resolve('tourist', 'vjudge1')

    await user_db.set_handle(DAVE, GUILD_ID, 'vjudge2')
    for name in ('!dave', '+server'):
        with pytest.raises(HandleIsVjudgeError):
            await resolve(name)


# Members


@pytest.mark.parametrize('names', IMPOSTORS.values(), ids=IMPOSTORS.keys())
async def test_a_member_is_never_looked_up_by_name(
    resolve: Resolve,
    guild: discord.Guild,
    names: tuple[str, str | None, str | None],
) -> None:
    alice = guild.get_member(ALICE)
    put_first(guild, names)

    assert await resolve(alice, guild.get_member(BOB)) == ['alice_cf', 'Bob_CF']
    # By that name, as the commands were told who uses them, she could be either.
    with pytest.raises(AmbiguousMemberError):
        await resolve(f'!{names[2] or names[1] or names[0]}')


@pytest.mark.parametrize('name', ['alice', 'Alice', 'Ally'])
async def test_a_member_is_found_by_each_name_that_is_theirs_alone(
    resolve: Resolve, name: str
) -> None:
    assert await resolve(f'!{name}') == ['alice_cf']


@pytest.mark.parametrize(
    ('typed', 'names'),
    [
        # In the first member's: username, global name and nickname.
        ('alice', ('alice', None, None)),
        ('alice', ('mallory', 'alice', None)),
        ('alice', ('mallory', None, 'alice')),
        ('Alice', ('mallory', 'Alice', None)),
        ('Alice', ('mallory', None, 'Alice')),
        ('Ally', ('mallory', 'Ally', None)),
        ('Ally', ('mallory', None, 'Ally')),
    ],
)
@pytest.mark.parametrize('first', [True, False], ids=['before', 'after'])
async def test_a_name_that_several_members_have_is_refused(
    resolve: Resolve,
    guild: discord.Guild,
    typed: str,
    names: tuple[str, str | None, str | None],
    first: bool,
) -> None:
    if first:
        put_first(guild, names)
    else:
        add_member(guild, FIRST_ID, *names)

    with pytest.raises(AmbiguousMemberError) as refused:
        await resolve('tourist', f'!{typed}')

    assert str(refused.value) == (
        f'More than one member is called {typed}. Put ! before a mention of the one'
        ' you mean, or before their ID.'
    )
    # The cogs send every ResolveHandleError to the member.
    assert isinstance(refused.value, ResolveHandleError)


@pytest.mark.parametrize('form', ['{}', '<@{}>', '<@!{}>'])
async def test_an_id_or_mention_means_one_member_whatever_others_are_called(
    resolve: Resolve, guild: discord.Guild, form: str
) -> None:
    identifier = form.format(ALICE)
    put_first(guild, ('alice', 'Alice', identifier))

    assert await resolve(f'!{identifier}') == ['alice_cf']


async def test_a_name_that_looks_like_part_of_a_mention_can_be_ambiguous(
    resolve: Resolve, guild: discord.Guild
) -> None:
    name = f'{BOB}>'
    put_first(guild, ('mallory', None, name))
    add_member(guild, FIRST_ID + 1, 'trudy', None, name)

    with pytest.raises(AmbiguousMemberError):
        await resolve(f'!{name}')


async def test_names_differ_by_case(resolve: Resolve, guild: discord.Guild) -> None:
    put_first(guild, ('mallory', None, 'ALICE'))

    assert await resolve('!Alice', '!alice') == ['alice_cf']
    assert await resolve('!ALICE') == ['impostor']


async def test_the_refusal_shows_the_name_as_typed_not_formatted(
    resolve: Resolve, guild: discord.Guild
) -> None:
    for member_id in (FIRST_ID, FIRST_ID + 1):
        add_member(guild, member_id, f'user{member_id}', None, '**bold**')

    with pytest.raises(AmbiguousMemberError) as refused:
        await resolve('!**bold**')

    assert r'called \*\*bold\*\*.' in str(refused.value)


async def test_a_name_that_nobody_has_is_refused(resolve: Resolve) -> None:
    with pytest.raises(FindMemberFailedError, match='`Nobody`'):
        await resolve('!Nobody')


async def test_a_member_without_a_handle_is_refused(
    resolve: Resolve, guild: discord.Guild
) -> None:
    for name in ('!dave', guild.get_member(DAVE)):
        with pytest.raises(HandleNotRegisteredError, match=f'<@{DAVE}>'):
            await resolve(name)


# Commands about the member who uses them


class StoppedError(Exception):
    """Raised in place of the lookup, where each command is stopped."""


@pytest.mark.parametrize(
    ('cog_type', 'name'),
    [
        (Codeforces, 'upsolve'),
        (Codeforces, 'gimme'),
        (Codeforces, 'stalk'),
        (Codeforces, 'mashup'),
        (Codeforces, 'gitgud'),
        (Codeforces, 'gotgud'),
        (Codeforces, 'nogud'),
        (Codeforces, 'vc'),
        (Codeforces, 'fullsolve'),
        (Codeforces, 'teamrate'),
        (Graphs, 'rating'),
        (Graphs, 'extreme'),
        (Graphs, 'solved'),
        (Graphs, 'hist'),
        (Graphs, 'curve'),
        (Graphs, 'scatter'),
        (Graphs, 'centile'),
        (Graphs, 'speed'),
    ],
)
async def test_a_command_about_its_user_gives_the_member_not_their_name(
    ctx: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
    cog_type: Any,
    name: str,
) -> None:
    given: list[Any] = []

    async def lookup(ctx: Any, converter: Any, names: Any, **bounds: Any) -> Any:
        given.extend(names)
        raise StoppedError

    monkeypatch.setattr(cf_common, 'resolve_handles', lookup)
    monkeypatch.setattr(cf_common, 'resolve_handle_pairs', lookup)
    bot = MagicMock()
    bot.user_db = user_db
    bot.cf_cache.rating_changes_cache.get_all_ratings.return_value = [1500]

    with pytest.raises(StoppedError):
        await getattr(cog_type, name).callback(cog_type(bot), ctx)

    assert len(given) == 1
    assert given[0] is ctx.author


@pytest.mark.parametrize(
    ('cog_type', 'name', 'args', 'names'),
    [
        (Codeforces, 'stalk', ('zed', '+dp', '!bob'), ['zed', '!bob']),
        (Codeforces, 'mashup', ('zed', '+dp', '!bob', '~math'), ['zed', '!bob']),
        (Codeforces, 'vc', ('zed', '!bob', '+edu'), ['zed', '!bob']),
        (Codeforces, 'teamrate', ('Zed*2', '!Bob', '+peak'), ['zed', '!Bob']),
        (Graphs, 'rating', ('zed', '+zoom', '!bob'), ['zed', '!bob']),
        (Graphs, 'extreme', ('!bob', '+solved'), ['!bob']),
        (Graphs, 'solved', ('zed', 'r>=1500', '!bob'), ['zed', '!bob']),
        (Graphs, 'hist', ('zed', 'phase_days=7', '!bob'), ['zed', '!bob']),
        (Graphs, 'curve', ('zed', '!bob', '+dp'), ['zed', '!bob']),
        (Graphs, 'scatter', ('!bob', 'b=5'), ['!bob']),
        (Graphs, 'centile', ('zed', '+exact', '!bob'), ['zed', '!bob']),
        (Graphs, 'speed', ('zed', 's=5', '!bob'), ['zed', '!bob']),
    ],
)
async def test_a_command_about_others_gives_their_names_in_order(
    ctx: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
    cog_type: Any,
    name: str,
    args: tuple[str, ...],
    names: list[str],
) -> None:
    given: list[Any] = []

    async def lookup(ctx: Any, converter: Any, names: Any, **bounds: Any) -> Any:
        given.extend(names)
        raise StoppedError

    monkeypatch.setattr(cf_common, 'resolve_handles', lookup)
    monkeypatch.setattr(cf_common, 'resolve_handle_pairs', lookup)
    bot = MagicMock()
    bot.user_db = user_db
    bot.cf_cache.rating_changes_cache.get_all_ratings.return_value = [1500]

    with pytest.raises(StoppedError):
        await getattr(cog_type, name).callback(cog_type(bot), ctx, *args)

    assert given == names


@pytest.mark.parametrize(
    ('name', 'maxcnt'), [('vc', 25), ('teamrate', 1000), ('centile', 50)]
)
async def test_commands_that_take_many_handles_say_how_many(
    ctx: MagicMock, monkeypatch: pytest.MonkeyPatch, name: str, maxcnt: int
) -> None:
    lookup = AsyncMock(side_effect=StoppedError)
    monkeypatch.setattr(cf_common, 'resolve_handles', lookup)
    monkeypatch.setattr(cf_common, 'resolve_handle_pairs', lookup)
    cog_type: Any = Graphs if name == 'centile' else Codeforces

    with pytest.raises(StoppedError):
        await getattr(cog_type, name).callback(cog_type(MagicMock()), ctx, 'zed')

    assert lookup.await_args is not None
    assert lookup.await_args.kwargs['maxcnt'] == maxcnt


async def test_centile_marks_the_handles_in_the_order_given(
    ctx: MagicMock, resolve: Resolve, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = AsyncMock(side_effect=StoppedError)
    monkeypatch.setattr(cf.user, 'info', info)
    bot = MagicMock()
    bot.cf_cache.rating_changes_cache.get_all_ratings.return_value = [1500]

    with pytest.raises(StoppedError):
        await Graphs.centile.callback(Graphs(bot), ctx, 'zed', '!bob', 'zed', 'Petr')

    info.assert_awaited_once_with(handles=['zed', 'Bob_CF', 'Petr'])


async def test_a_challenge_gives_both_members_not_their_names(
    ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = AsyncMock(side_effect=StoppedError)
    monkeypatch.setattr(cf_common, 'resolve_handles', lookup)
    bob = ctx.guild.get_member(BOB)

    with pytest.raises(StoppedError):
        await Dueling.challenge.callback(Dueling(MagicMock()), ctx, bob)

    assert lookup.await_args is not None
    (challenger, opponent) = lookup.await_args.args[2]
    assert challenger is ctx.author
    assert opponent is bob


# Team ratings


RATINGS = {
    'alice_cf': 1000,
    'bob_cf': 2000,
    'carol_cf': None,
    'tourist': 3500,
    'petr': 3000,
}


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch, make_user: Any) -> list[str]:
    """The handles Codeforces was asked about, which it answers about in order."""
    asked: list[str] = []

    async def info(*, handles: list[str]) -> list[cf.User]:
        asked.extend(handles)
        return [
            make_user(handle=handle.title(), rating=RATINGS.get(handle), maxRating=3800)
            for handle in handles
        ]

    monkeypatch.setattr(cf.user, 'info', info)
    return asked


@pytest.fixture
def teamrate(
    ctx: MagicMock, linked: None, asked: list[str], monkeypatch: pytest.MonkeyPatch
) -> Resolve:
    """Rates a team: its title, and the ratings and multipliers it is made of."""
    composed: list[Any] = []

    def compose(left: float, right: float, ratings: list[Any]) -> int:
        composed.extend(ratings)
        return 1500

    monkeypatch.setattr(Codeforces, 'composeRatings', staticmethod(compose))

    async def teamrate(*args: str) -> tuple[str, list[tuple[int, int]]]:
        composed.clear()
        ctx.send.reset_mock()
        await Codeforces.teamrate.callback(Codeforces(MagicMock()), ctx, *args)
        return ctx.send.await_args.kwargs['embed'].title, list(composed)

    return teamrate


@pytest.mark.parametrize(
    ('args', 'title', 'ratings'),
    [
        ((), 'alice_cf', [(1000, 1)]),
        (
            ('tourist*2', 'Petr*3', '!bob*4', '!Alice'),
            'tourist*2, petr*3, bob_cf*4, alice_cf',
            [(3500, 2), (3000, 3), (2000, 4), (1000, 1)],
        ),
        (
            ('!Alice', '!bob*4', 'Petr*3', 'tourist*2'),
            'alice_cf, bob_cf*4, petr*3, tourist*2',
            [(1000, 1), (2000, 4), (3000, 3), (3500, 2)],
        ),
        # A name keeps its capitals: nobody is called caz.
        (('!CAZ', '!CAZ*2', 'tourist'), 'carol_cf*2, tourist', [(3500, 1)]),
        (
            (f'!{BOB}*2', f'!<@{ALICE}>*3'),
            'bob_cf*2, alice_cf*3',
            [(2000, 2), (1000, 3)],
        ),
        # A player named more than once counts once, with the last multiplier.
        (('tourist*5', 'TOURIST*2'), 'tourist*2', [(3500, 2)]),
        (('tourist*5', 'TOURIST*2', 'tourist*3'), 'tourist*3', [(3500, 3)]),
        (('!bob*5', 'petr', 'bob_cf*2'), 'bob_cf*2, petr', [(2000, 2), (3000, 1)]),
        (('!bob*5', 'bob_cf*2', '!bob*3'), 'bob_cf*3', [(2000, 3)]),
        (('bob_cf*2', '!bob*5', 'Bob_CF*3'), 'bob_cf*3', [(2000, 3)]),
        (
            ('a', 'b', 'c', 'd', 'e', 'tourist'),
            'a, b, c, d, e, tourist',
            [(3500, 1)],
        ),
        (('tourist', '+peak'), 'tourist', [(3800, 1)]),
    ],
)
async def test_each_multiplier_goes_with_its_own_player(
    teamrate: Resolve,
    asked: list[str],
    args: tuple[str, ...],
    title: str,
    ratings: list[tuple[int, int]],
) -> None:
    assert await teamrate(*args) == (title, ratings)
    assert asked == [part.partition('*')[0] for part in title.split(', ')]


@pytest.mark.parametrize(
    'args',
    [
        ('+server*2', 'tourist', '!bob*3'),
        ('!bob*3', 'tourist', '+server*2'),
        ('+server*2', 'Bob_CF*3', 'tourist'),
        ('bob_cf*3', '+server*5', 'tourist', '+server*2'),
    ],
)
async def test_a_multiplier_of_the_server_s_handles_goes_with_the_others(
    teamrate: Resolve, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    monkeypatch.setitem(RATINGS, 'impostor', None)

    title, ratings = await teamrate(*args)

    # Alice only through +server; Bob named too, with his own multiplier.
    assert 'alice_cf*2' in title.split(', ')
    assert sorted(ratings) == [(1000, 2), (2000, 3), (3500, 1)]


@pytest.mark.parametrize(
    ('arg', 'refusal'),
    [
        ('tourist*x', "Can't multiply by non-integer"),
        ('tourist*', "Can't multiply by non-integer"),
        ('tourist*2*3', "Can't multiply by non-integer"),
        ('tourist*0', 'How can you have nonpositive members in team?'),
        ('tourist*-1', 'How can you have nonpositive members in team?'),
        # A space before the *.
        ('*2', 'Name a player before each *.'),
        ('', 'Name a player before each *.'),
        ('!*2', 'Name a player before each *.'),
    ],
)
async def test_a_bad_multiplier_is_refused_before_codeforces_is_asked(
    teamrate: Resolve, asked: list[str], arg: str, refusal: str
) -> None:
    with pytest.raises(CodeforcesCogError) as refused:
        await teamrate('Petr', arg)

    assert str(refused.value) == refusal
    assert asked == []


async def test_a_shared_judge_account_is_refused_in_any_case(
    teamrate: Resolve, asked: list[str]
) -> None:
    with pytest.raises(HandleIsVjudgeError):
        await teamrate('Petr', 'VJudge1')

    assert asked == []
