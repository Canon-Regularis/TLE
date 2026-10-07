"""Tests for TLE's Handles cog: its commands' replies, and how they behave.

The access rules alone decide who may use each command, so the cog has no
checks of its own. Slow commands answer within Discord's 3 seconds by typing
first. Mistyped choices get the choices, the self-service ping roles are
checked before they are handed out, and replies never name a role or an id.

The cog reads and writes a real in-memory user database (the ``user_db``
fixture). Discord is mocked, and Codeforces is patched, so no test reaches
either. Commands run as their callbacks, as in the other cog tests, or through
a real bot where discord.py's own argument parsing matters.
"""

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator, Callable
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.util import codeforces_api as cf, codeforces_common as cf_common, oauth
from tle.util.cache import ContestNotFound
from tle.util.db.user_db_conn import UserDbConn
from tle.util.discord_common import NOT_IN_A_THREAD_MESSAGE

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
MEMBER_ID = 1_200_000_000_000_000_001
OTHER_MEMBER_ID = 1_200_000_000_000_000_002
CHANNEL_ID = 1_300_000_000_000_000_001
PING_ROLE_ID = 1_400_000_000_000_000_001
TRUSTED_ROLE_ID = 1_400_000_000_000_000_002
PURGATORY_ROLE_ID = 1_400_000_000_000_000_003
BOT_ROLE_ID = 1_400_000_000_000_000_009
HANDLE = 'Fake_Coder'  # a made-up account
CONTEST_ID = 1950
# The bot's highest role is at this position.
BOT_POSITION = 10
EVERYONE = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True
)
NO_TRUSTED_ROLE = 'This server has no trusted role, so nobody can be made trusted.'
TRUSTED_ROLE_REFUSED = (
    "I can't give the trusted role: it must be below my highest role, and I need "
    'the Manage Roles permission.'
)


@pytest.fixture
def handles() -> ModuleType:
    """tle.cogs.handles, which draws with cairo and Pango through gi.

    Docker and CI have them; a bare virtualenv may not, so these tests skip there.
    """
    pytest.importorskip('gi')
    from tle.cogs import handles

    return handles


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by their default names, whatever the environment says."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_PURGATORY', 'Purgatory')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


def codeforces_user(handle: str = HANDLE, rating: int | None = None) -> cf.User:
    """A made-up account, as cf.user.info returns it; unrated by default, so
    linking it needs no rank role.
    """
    return cf.User(
        handle=handle,
        firstName=None,
        lastName=None,
        country=None,
        city=None,
        organization=None,
        contribution=0,
        rating=rating,
        maxRating=rating,
        lastOnlineTimeSeconds=1_790_000_000,
        registrationTimeSeconds=1_600_000_000,
        friendOfCount=0,
        titlePhoto='https://userpic.codeforces.org/no-title.jpg',
    )


def make_role(
    guild: MagicMock,
    role_id: int,
    name: str,
    *,
    position: int = 1,
    permissions: discord.Permissions | None = None,
) -> discord.Role:
    """A real role, so that discord_common's checks of it run as in a server."""
    return discord.Role(
        guild=guild,
        state=MagicMock(),
        data={
            'id': role_id,
            'name': name,
            'position': position,
            'permissions': str((permissions or EVERYONE).value),
        },
    )


@pytest.fixture
def guild() -> MagicMock:
    """A server where @everyone may read and talk, and the bot has a role of
    its own; members and roles are added by the tests.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.name = 'Test Server'
    guild.default_role = make_role(guild, GUILD_ID, '@everyone', position=0)
    guild.channels = []
    guild.roles = [guild.default_role]
    guild.me = MagicMock(spec=discord.Member)
    guild.me.top_role = make_role(
        guild, BOT_ROLE_ID, 'TLE', position=BOT_POSITION, permissions=EVERYONE
    )
    guild._members = {}
    guild.get_member.side_effect = guild._members.get
    guild.get_role.side_effect = lambda role_id: next(
        (role for role in guild.roles if role.id == role_id), None
    )
    return guild


def make_member(
    guild: MagicMock,
    member_id: int = MEMBER_ID,
    *roles: discord.Role,
    joined_at: dt.datetime | None = None,
) -> MagicMock:
    """A member of ``guild`` with ``roles``; adding and removing roles updates them."""
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild)
    member.mention = f'<@{member_id}>'
    member.display_name = f'Member {member_id % 1000}'
    member.name = member.display_name
    member.roles = list(roles)
    member.joined_at = joined_at

    async def add_roles(*added: discord.Role, reason: str | None = None) -> None:
        member.roles = [*member.roles, *added]

    async def remove_roles(*removed: discord.Role, reason: str | None = None) -> None:
        member.roles = [role for role in member.roles if role not in removed]

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    member.send = AsyncMock()
    guild._members[member_id] = member
    return member


@pytest.fixture
def member(guild: MagicMock) -> MagicMock:
    """The member who uses the commands."""
    return make_member(guild)


@pytest.fixture
def bot(user_db: UserDbConn) -> MagicMock:
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    return bot


@pytest.fixture
def cog(handles: ModuleType, bot: MagicMock) -> Any:
    return handles.Handles(bot)


@pytest.fixture
def ctx(guild: MagicMock, member: MagicMock) -> MagicMock:
    """The context of a prefix command that ``member`` uses; replies, and the
    message a reply returns, are recorded.
    """
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = member
    ctx.channel = MagicMock(spec=discord.TextChannel, id=CHANNEL_ID)
    ctx.interaction = None
    ctx.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    ctx.defer = AsyncMock()
    return ctx


def replies(ctx: MagicMock) -> list[str]:
    """What ``ctx``'s command sent: each message's text, or its embed's."""
    texts = []
    for sent in ctx.send.await_args_list:
        embed = sent.kwargs.get('embed')
        texts.append(embed.description if embed is not None else sent.args[0])
    return texts


def record_typing(ctx: MagicMock, steps: list[str]) -> None:
    """Record in ``steps`` when ``ctx``'s typing starts and when it ends."""

    @contextlib.asynccontextmanager
    async def typing(**kwargs: Any) -> AsyncIterator[None]:
        steps.append('typing')
        try:
            yield
        finally:
            steps.append('typed')

    ctx.typing = typing


def step(steps: list[str], name: str, result: object = None) -> AsyncMock:
    """A stand-in for a slow request that records ``name`` in ``steps``."""

    async def run(*args: Any, **kwargs: Any) -> object:
        steps.append(name)
        return result

    return AsyncMock(side_effect=run)


async def link(user_db: UserDbConn, member_id: int = MEMBER_ID) -> None:
    await user_db.set_handle(member_id, GUILD_ID, HANDLE)


# The access rules alone decide who may use each command


def test_no_command_checks_roles_of_its_own(cog: Any) -> None:
    # /help lists what the access rules allow, so a role check of a command's
    # own would refuse what /help offers.
    commands_ = list(cog.walk_commands())

    assert len(commands_) == 20
    assert [command.qualified_name for command in commands_ if command.checks] == []


def test_gudgitters_can_be_used_once_every_20_seconds_by_each_member(
    cog: Any,
) -> None:
    command = cog.gudgitters

    assert command.cooldown is not None
    assert (command.cooldown.rate, command.cooldown.per) == (1, 20)
    assert command._buckets.type is commands.BucketType.user


# Slow commands type first, so that a slash command answers in time


async def test_handle_set_types_while_it_asks_codeforces(
    cog: Any,
    ctx: MagicMock,
    member: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps: list[str] = []
    record_typing(ctx, steps)
    info = step(steps, 'request', [codeforces_user()])
    monkeypatch.setattr(cf.user, 'info', info)

    await type(cog).set.callback(cog, ctx, member, HANDLE.lower())

    assert steps == ['typing', 'request', 'typed']
    assert await user_db.get_handle(MEMBER_ID, GUILD_ID) == HANDLE


async def test_handle_unmagic_types_while_it_asks_codeforces(
    cog: Any, ctx: MagicMock, user_db: UserDbConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    await link(user_db)
    steps: list[str] = []
    record_typing(ctx, steps)
    monkeypatch.setattr(cf, 'resolve_redirects', step(steps, 'request', {}))

    await type(cog).unmagic.callback(cog, ctx)

    assert steps == ['typing', 'request', 'typed']
    assert replies(ctx) == ['No linked handle has changed on Codeforces.']


async def test_handle_unmagic_without_a_linked_handle_says_so(
    handles: ModuleType, cog: Any, ctx: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    steps: list[str] = []
    record_typing(ctx, steps)
    resolve = step(steps, 'request', {})
    monkeypatch.setattr(cf, 'resolve_redirects', resolve)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).unmagic.callback(cog, ctx)

    assert str(raised.value) == (
        "You haven't linked a Codeforces handle, so there is none to update."
    )
    # It used to ask Codeforces about the handle None.
    assert steps == []


async def test_handle_unmagic_all_types_while_it_asks_codeforces(
    cog: Any, ctx: MagicMock, user_db: UserDbConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    await link(user_db)
    steps: list[str] = []
    record_typing(ctx, steps)
    resolve = step(steps, 'request', {})
    monkeypatch.setattr(cf, 'resolve_redirects', resolve)

    await type(cog).unmagic_all.callback(cog, ctx)

    assert steps == ['typing', 'request', 'typed']
    resolve.assert_awaited_once_with([HANDLE])


async def test_gudgitters_types_while_it_ranks_members(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps: list[str] = []
    record_typing(ctx, steps)
    monkeypatch.setattr(user_db, 'get_gudgitters', step(steps, 'request', []))

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).gudgitters.callback(cog, ctx)

    assert steps == ['typing', 'request', 'typed']
    assert str(raised.value) == (
        'Nobody here has solved a gitgud problem yet. Get one with `/gitgud`, and '
        'say you solved it with `/gotgud`.'
    )


async def test_roleupdate_now_types_while_it_asks_codeforces(
    cog: Any, ctx: MagicMock, user_db: UserDbConn, monkeypatch: pytest.MonkeyPatch
) -> None:
    await link(user_db)
    steps: list[str] = []
    record_typing(ctx, steps)
    monkeypatch.setattr(cf.user, 'info', step(steps, 'request', [codeforces_user()]))

    await type(cog).now.callback(cog, ctx)

    assert steps == ['typing', 'request', 'typed']
    assert replies(ctx) == [
        'Updated the rank role of every member with a linked handle.'
    ]


async def test_roleupdate_now_names_the_rank_roles_the_server_lacks(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link(user_db)
    await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, 'Other_Coder')
    make_member(ctx.guild, OTHER_MEMBER_ID)
    users = [codeforces_user(rating=1700), codeforces_user('Other_Coder', 2100)]
    monkeypatch.setattr(cf.user, 'info', AsyncMock(return_value=users))

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).now.callback(cog, ctx)

    assert str(raised.value) == (
        'This server has no role for the ranks `Expert`, `Master`. Add a role '
        'named after each rank, then try again.'
    )


# Handles


async def test_handle_rget_finds_who_linked_a_handle(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    other = make_member(ctx.guild, OTHER_MEMBER_ID)
    await link(user_db, OTHER_MEMBER_ID)
    await user_db.cache_cf_user(codeforces_user())

    await type(cog).rget.callback(cog, ctx, HANDLE.lower())

    assert replies(ctx) == [
        f'Handle for {other.mention} is currently set to'
        f' **[{HANDLE}](https://codeforces.com/profile/{HANDLE})**'
    ]


async def test_handle_rget_of_a_member_who_left_names_no_id(
    handles: ModuleType, cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # TLE keeps the handles of members who leave.
    await link(user_db, OTHER_MEMBER_ID)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).rget.callback(cog, ctx, HANDLE)

    assert str(raised.value) == (
        f'`{HANDLE}` was linked by someone who has left this server.'
    )
    assert str(OTHER_MEMBER_ID) not in str(raised.value)


async def test_handle_rget_of_a_handle_nobody_linked_says_so(
    handles: ModuleType, cog: Any, ctx: MagicMock
) -> None:
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).rget.callback(cog, ctx, 'tourist')

    assert str(raised.value) == 'No member of this server has linked `tourist`.'


async def test_a_member_named_wrongly_is_answered_privately(
    cog: Any, ctx: MagicMock
) -> None:
    # ;handle remove !nobody: discord.py's handler used to log it as a bug.
    error = cf_common.FindMemberFailedError('nobody')

    await cog.cog_command_error(ctx, error)

    assert error.handled is True
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.kwargs['ephemeral'] is True
    assert replies(ctx) == ['Unable to convert `nobody` to a server member']


async def test_updatestatus_says_how_many_members_it_marked(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await link(user_db)
    ctx.guild.members = [ctx.author, make_member(ctx.guild, OTHER_MEMBER_ID)]

    await type(cog)._updatestatus.callback(cog, ctx)

    assert replies(ctx) == ['Marked 1 member with a linked handle as active.']


@pytest.mark.parametrize(
    ('countries', 'text'),
    [
        ((), 'No member of this server has linked a Codeforces handle.'),
        (
            ('Croatia',),
            'No member of this server from those countries has linked a '
            'Codeforces handle.',
        ),
    ],
    ids=['everyone', 'by country'],
)
async def test_handle_list_without_members_says_so(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    countries: tuple[str, ...],
    text: str,
) -> None:
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).list.callback(cog, ctx, *countries)

    assert str(raised.value) == text


# Signing in to Codeforces


@pytest.fixture
def signing_in(
    bot: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> oauth.OAuthStateStore:
    """Signing in to Codeforces set up, as OAUTH_* settings set it up."""
    monkeypatch.setattr(constants, 'OAUTH_CONFIGURED', True)
    monkeypatch.setattr(constants, 'OAUTH_CLIENT_ID', 'test-client')
    monkeypatch.setattr(constants, 'OAUTH_REDIRECT_URI', 'https://bot.test/callback')
    bot.oauth_state_store = oauth.OAuthStateStore()
    return bot.oauth_state_store


def pending_sign_in(store: oauth.OAuthStateStore) -> oauth.OAuthPending:
    (pending,) = store._pending.values()
    return pending


async def test_slash_identify_answers_privately_and_keeps_its_interaction(
    cog: Any, ctx: MagicMock, member: MagicMock, signing_in: oauth.OAuthStateStore
) -> None:
    ctx.interaction = MagicMock(spec=discord.Interaction)

    await type(cog).identify.callback(cog, ctx)

    # How it went is told through the interaction, as privately.
    assert pending_sign_in(signing_in).interaction is ctx.interaction
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.kwargs['ephemeral'] is True
    assert isinstance(ctx.send.await_args.kwargs['view'], discord.ui.View)
    member.send.assert_not_awaited()


async def test_prefix_identify_sends_the_link_by_direct_message(
    cog: Any, ctx: MagicMock, member: MagicMock, signing_in: oauth.OAuthStateStore
) -> None:
    await type(cog).identify.callback(cog, ctx)

    assert pending_sign_in(signing_in).interaction is None
    member.send.assert_awaited_once()
    assert member.send.await_args.args == (
        'Press the button to sign in to Codeforces and link your account. The link '
        "works for 5 minutes, and I'll tell you here how it went.",
    )
    assert replies(ctx) == ["I've sent you the sign-in link in a direct message."]


async def test_prefix_identify_with_direct_messages_closed_says_so(
    cog: Any, ctx: MagicMock, member: MagicMock, signing_in: oauth.OAuthStateStore
) -> None:
    member.send.side_effect = discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'no'
    )

    await type(cog).identify.callback(cog, ctx)

    assert signing_in._pending == {}
    assert replies(ctx) == [
        "I couldn't send you a direct message. Allow direct messages from this "
        "server's members and try again, or use `/handle identify`."
    ]


async def test_identify_without_sign_in_set_up_says_so_plainly(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(constants, 'OAUTH_CONFIGURED', False)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).identify.callback(cog, ctx)

    assert str(raised.value) == (
        "Signing in to Codeforces isn't set up for this bot. Ask a moderator to "
        'link your handle.'
    )


# /roleupdate


async def test_roleupdate_auto_turns_updates_on_and_off(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await type(cog).auto.callback(cog, ctx, 'on')
    assert await user_db.has_auto_role_update_enabled(GUILD_ID)

    await type(cog).auto.callback(cog, ctx, 'off')
    assert not await user_db.has_auto_role_update_enabled(GUILD_ID)

    assert replies(ctx) == [
        'Automatic rank role updates are on.',
        'Automatic rank role updates are off.',
    ]


async def test_roleupdate_auto_refuses_another_value_politely(
    handles: ModuleType, cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # It used to raise a ValueError, which members saw as a bug.
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).auto.callback(cog, ctx, 'maybe')

    assert str(raised.value) == 'Choose `on` or `off`.'
    assert not await user_db.has_auto_role_update_enabled(GUILD_ID)


async def test_roleupdate_publish_here_posts_rank_changes_in_this_channel(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await type(cog).publish.callback(cog, ctx, 'here')

    assert await user_db.get_rankup_channel(GUILD_ID) == CHANNEL_ID
    assert replies(ctx) == [
        'Rank changes will be posted in this channel after each rated contest.'
    ]


async def test_roleupdate_publish_here_refuses_a_thread(
    handles: ModuleType, cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # The posts go to the channel found among the server's channels, which
    # leave threads out, so they never came.
    ctx.channel = MagicMock(
        spec=discord.Thread, id=CHANNEL_ID + 1, parent_id=CHANNEL_ID
    )

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).publish.callback(cog, ctx, 'here')

    assert str(raised.value) == NOT_IN_A_THREAD_MESSAGE
    assert await user_db.get_rankup_channel(GUILD_ID) is None
    ctx.send.assert_not_awaited()


async def test_roleupdate_publish_off_stops_the_posts(
    handles: ModuleType, cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.set_rankup_channel(GUILD_ID, CHANNEL_ID)

    await type(cog).publish.callback(cog, ctx, 'off')

    assert await user_db.get_rankup_channel(GUILD_ID) is None
    assert replies(ctx) == ['Rank changes will no longer be posted after contests.']
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).publish.callback(cog, ctx, 'off')
    assert str(raised.value) == (
        "Rank changes aren't posted after contests, so there is nothing to stop."
    )


async def test_roleupdate_publish_refuses_another_value_politely(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    user_db: UserDbConn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rating_changes = AsyncMock()
    monkeypatch.setattr(cf.contest, 'ratingChanges', rating_changes)

    # It used to raise a ValueError, which members saw as a bug.
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).publish.callback(cog, ctx, 'soon')

    assert str(raised.value) == (
        'Choose `here`, `off` or a contest ID, such as `1950`.'
    )
    assert await user_db.get_rankup_channel(GUILD_ID) is None
    rating_changes.assert_not_awaited()
    ctx.defer.assert_not_awaited()


async def test_roleupdate_publish_of_an_unknown_contest_says_so(
    handles: ModuleType, cog: Any, ctx: MagicMock, bot: MagicMock
) -> None:
    bot.cf_cache = MagicMock()
    bot.cf_cache.contest_cache.get_contest.side_effect = ContestNotFound(CONTEST_ID)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).publish.callback(cog, ctx, str(CONTEST_ID))

    # ID as the bot's other texts write it.
    assert str(raised.value) == f'Contest with ID `{CONTEST_ID}` not found.'
    ctx.defer.assert_not_awaited()


@pytest.fixture
def contest(bot: MagicMock, make_contest: Callable[..., cf.Contest]) -> cf.Contest:
    """A finished contest, in the bot's cache of Codeforces contests."""
    contest = make_contest(id=CONTEST_ID, name='Codeforces Round 1950')
    bot.cf_cache = MagicMock()
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    return contest


@pytest.mark.usefixtures('contest')
async def test_roleupdate_publish_of_a_contest_defers_before_asking_codeforces(
    cog: Any,
    ctx: MagicMock,
    user_db: UserDbConn,
    make_rating_change: Callable[..., cf.RatingChange],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A slash command must answer within 3 seconds; Codeforces may be slower.
    await link(user_db)
    steps: list[str] = []
    ctx.defer.side_effect = lambda **kwargs: steps.append('defer')
    change = make_rating_change(
        contestId=CONTEST_ID, handle=HANDLE, oldRating=1550, newRating=1650
    )
    monkeypatch.setattr(cf.contest, 'ratingChanges', step(steps, 'request', [change]))

    await type(cog).publish.callback(cog, ctx, str(CONTEST_ID))

    assert steps == ['defer', 'request']
    # The rank changes answer the command, in one message.
    ctx.send.assert_awaited_once()
    heading, ranks, increases = ctx.send.await_args.kwargs['embeds']
    assert heading.title == 'Codeforces Round 1950'
    assert ranks.description == (
        f'<@{MEMBER_ID}> [{HANDLE}](https://codeforces.com/profile/{HANDLE}):'
        ' Specialist \N{LONG RIGHTWARDS ARROW} Expert'
    )
    assert increases.author.name == 'Top rating increases'
    ctx.channel.send.assert_not_awaited()


@pytest.mark.usefixtures('contest')
async def test_roleupdate_publish_sends_the_rank_changes_in_as_few_messages_as_fit(
    cog: Any,
    ctx: MagicMock,
    make_rating_change: Callable[..., cf.RatingChange],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changes = [make_rating_change(contestId=CONTEST_ID)]
    monkeypatch.setattr(cf.contest, 'ratingChanges', AsyncMock(return_value=changes))
    embeds = [discord.Embed(description=f'Rank changes {n}') for n in range(12)]
    cog._make_rankup_embeds = AsyncMock(return_value=embeds)

    await type(cog).publish.callback(cog, ctx, str(CONTEST_ID))

    assert ctx.send.await_args_list == [
        call(embeds=embeds[:10]),
        call(embeds=embeds[10:]),
    ]
    ctx.channel.send.assert_not_awaited()


def long_embed(characters: int) -> discord.Embed:
    return discord.Embed(description='x' * characters)


def test_embed_batches_hold_at_most_10_embeds(handles: ModuleType) -> None:
    embeds = [long_embed(10) for _ in range(25)]

    batches = handles._embed_batches(embeds)

    assert [len(batch) for batch in batches] == [10, 10, 5]
    assert [embed for batch in batches for embed in batch] == embeds


def test_embed_batches_hold_at_most_6000_characters(handles: ModuleType) -> None:
    embeds = [long_embed(1000) for _ in range(7)] + [long_embed(5000), long_embed(1)]

    batches = handles._embed_batches(embeds)

    # 6000 characters fit exactly; the next embed starts a new message.
    assert [len(batch) for batch in batches] == [6, 2, 1]
    assert [sum(len(embed) for embed in batch) for batch in batches] == [
        6000,
        6000,
        1,
    ]
    assert [embed for batch in batches for embed in batch] == embeds


def test_no_embeds_make_no_batches(handles: ModuleType) -> None:
    assert handles._embed_batches([]) == []


# /role


def ping_role(guild: MagicMock, **kwargs: Any) -> discord.Role:
    """The role pinged about duels, in ``guild``: below the bot's highest role,
    and granting nothing more than @everyone has, unless ``kwargs`` say so.
    """
    kwargs.setdefault('position', 3)
    role = make_role(guild, PING_ROLE_ID, 'Duelist', **kwargs)
    guild.roles = [*guild.roles, role]
    return role


async def test_role_give_hands_out_a_role_for_pings(
    cog: Any, ctx: MagicMock, member: MagicMock
) -> None:
    role = ping_role(ctx.guild)

    await type(cog).role.callback(cog, ctx, 'give', 'duel')

    member.add_roles.assert_awaited_once_with(
        role, reason='Member asked for duel pings'
    )
    assert replies(ctx) == ['You now have the role for duel pings.']


@pytest.mark.parametrize(
    ('kwargs', 'problem'),
    [
        (
            {'permissions': discord.Permissions(manage_messages=True)},
            'it grants permissions beyond what everyone has',
        ),
        ({'position': BOT_POSITION + 1}, 'it is not below my highest role'),
    ],
    ids=['permissions', 'above the bot'],
)
async def test_role_give_refuses_a_role_that_is_more_than_pings(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    member: MagicMock,
    kwargs: dict[str, Any],
    problem: str,
) -> None:
    ping_role(ctx.guild, **kwargs)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).role.callback(cog, ctx, 'give', 'duel')

    assert str(raised.value) == (
        f"I can't give you the role for duel pings: {problem}. Ask an admin to "
        'fix the role.'
    )
    member.add_roles.assert_not_awaited()


async def test_role_give_refuses_a_role_the_bot_uses_for_access(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    member: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The trusted role, set by its id, happens to be called Duelist.
    ping_role(ctx.guild)
    monkeypatch.setattr(constants, 'TLE_TRUSTED', PING_ROLE_ID)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).role.callback(cog, ctx, 'give', 'duel')

    assert 'the bot uses it to decide what members may do' in str(raised.value)
    assert str(PING_ROLE_ID) not in str(raised.value)
    member.add_roles.assert_not_awaited()


async def test_role_remove_takes_the_role_back(
    cog: Any, ctx: MagicMock, member: MagicMock
) -> None:
    role = ping_role(ctx.guild)
    member.roles = [role]

    await type(cog).role.callback(cog, ctx, 'remove', 'duel')

    assert member.roles == []
    assert replies(ctx) == ['You no longer have the role for duel pings.']


@pytest.mark.parametrize(
    ('action', 'holds', 'text'),
    [
        ('give', True, 'You already have the role for duel pings.'),
        ('remove', False, "You don't have the role for duel pings."),
    ],
)
async def test_role_says_when_there_is_nothing_to_change(
    cog: Any,
    ctx: MagicMock,
    member: MagicMock,
    action: str,
    holds: bool,
    text: str,
) -> None:
    role = ping_role(ctx.guild)
    member.roles = [role] if holds else []

    await type(cog).role.callback(cog, ctx, action, 'duel')

    assert replies(ctx) == [text]
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


@pytest.mark.parametrize(
    ('action', 'which', 'text'),
    [
        ('take', 'duel', 'Choose `give` to take the role, or `remove` to drop it.'),
        ('give', 'chess', 'Choose `duel` or `vc`.'),
        ('give', 'vc', 'This server has no role for virtual contest pings.'),
    ],
    ids=['action', 'which', 'missing role'],
)
async def test_role_refuses_what_it_cannot_do_without_naming_roles(
    handles: ModuleType,
    cog: Any,
    ctx: MagicMock,
    action: str,
    which: str,
    text: str,
) -> None:
    ping_role(ctx.guild)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).role.callback(cog, ctx, action, which)

    assert str(raised.value) == text


# Mistyped choices, through discord.py's argument parsing


@pytest.fixture
async def live_bot(handles: ModuleType, user_db: UserDbConn) -> AsyncIterator[Any]:
    """A real bot with the cog, which answers errors as TLE's bot does."""
    from tle.util import discord_common

    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    await bot.add_cog(handles.Handles(bot))
    bot.user_db = user_db
    bot.add_listener(discord_common.bot_error_handler, 'on_command_error')
    yield bot
    await bot.close()


async def run(bot: commands.Bot, member: MagicMock, typed: str) -> Any:
    """Run the prefix command ``typed``, as ;typed, by ``member``; its context,
    whose replies are recorded.
    """
    message = MagicMock(spec=discord.Message, guild=member.guild, author=member)
    message.content = f';{typed}'
    view = StringView(typed)
    invoked = view.get_word()
    ctx = commands.Context(
        message=message, bot=bot, view=view, prefix=';', invoked_with=invoked
    )
    ctx.command = bot.get_command(invoked)
    ctx.send = AsyncMock()  # type: ignore[method-assign]
    await bot.invoke(ctx)
    # Wait for the error handler, which discord.py runs as a task.
    pending = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith('discord.py:')
    ]
    await asyncio.gather(*pending)
    return ctx


@pytest.mark.parametrize(
    ('typed', 'text'),
    [
        ('roleupdate auto maybe', 'Choose `on` or `off`.'),
        ('roleupdate auto', 'Choose `on` or `off`.'),
        (
            'roleupdate publish',
            'Choose `here`, `off` or a contest ID, such as `1950`.',
        ),
        ('role', 'Choose `give` to take the role, or `remove` to drop it.'),
        ('role give', 'Choose `duel` or `vc`.'),
    ],
)
async def test_a_choice_typed_wrongly_gets_the_choices_once(
    live_bot: commands.Bot, member: MagicMock, typed: str, text: str
) -> None:
    ctx = await run(live_bot, member, typed)

    # discord.py's own reply would name the option, such as arg, instead.
    ctx.send.assert_awaited_once()
    assert ctx.send.await_args.kwargs['ephemeral'] is True
    assert ctx.send.await_args.kwargs['embed'].description == text


async def test_roleupdate_auto_offers_on_and_off_as_a_slash_command(
    live_bot: commands.Bot,
) -> None:
    group = live_bot.tree.get_command('roleupdate')
    assert isinstance(group, discord.app_commands.Group)
    auto = group.get_command('auto')
    assert isinstance(auto, discord.app_commands.Command)

    (option,) = auto.parameters

    assert option.name == 'arg'
    assert [choice.value for choice in option.choices] == ['on', 'off']
    assert option.description == (
        'on to update rank roles after each rated contest; off to stop'
    )


# The trusted role, which replies never name


@pytest.fixture
def trusted_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE_TRUSTED holds a role's id, which no reply may show."""
    monkeypatch.setattr(constants, 'TLE_TRUSTED', TRUSTED_ROLE_ID)


@pytest.mark.usefixtures('trusted_by_id')
async def test_handle_refer_without_a_trusted_role_names_no_id(
    handles: ModuleType, cog: Any, ctx: MagicMock
) -> None:
    other = make_member(ctx.guild, OTHER_MEMBER_ID)

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).refer.callback(cog, ctx, other)

    assert str(raised.value) == NO_TRUSTED_ROLE


@pytest.mark.usefixtures('trusted_by_id')
async def test_handle_refer_makes_another_member_trusted(
    cog: Any, ctx: MagicMock, member: MagicMock
) -> None:
    trusted = make_role(ctx.guild, TRUSTED_ROLE_ID, 'Veterans', position=2)
    ctx.guild.roles = [*ctx.guild.roles, trusted]
    other = make_member(ctx.guild, OTHER_MEMBER_ID)

    await type(cog).refer.callback(cog, ctx, other)

    assert other.roles == [trusted]
    assert replies(ctx) == [
        f'{other.mention} is now trusted, referred by {member.mention}.'
    ]


@pytest.mark.usefixtures('trusted_by_id')
async def test_handle_refer_that_discord_refuses_says_why(
    handles: ModuleType, cog: Any, ctx: MagicMock
) -> None:
    trusted = make_role(ctx.guild, TRUSTED_ROLE_ID, 'Veterans', position=2)
    ctx.guild.roles = [*ctx.guild.roles, trusted]
    other = make_member(ctx.guild, OTHER_MEMBER_ID)
    other.add_roles.side_effect = discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'Missing Permissions'
    )

    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).refer.callback(cog, ctx, other)

    assert str(raised.value) == TRUSTED_ROLE_REFUSED
    ctx.send.assert_not_awaited()


async def test_handle_refer_leaves_members_in_purgatory_alone(
    cog: Any, ctx: MagicMock
) -> None:
    purgatory = make_role(ctx.guild, PURGATORY_ROLE_ID, 'Purgatory', position=2)
    ctx.guild.roles = [*ctx.guild.roles, purgatory]
    other = make_member(ctx.guild, OTHER_MEMBER_ID, purgatory)

    await type(cog).refer.callback(cog, ctx, other)

    assert replies(ctx) == [
        f"{other.mention} is in purgatory, so they can't be made trusted."
    ]
    other.add_roles.assert_not_awaited()


@pytest.mark.usefixtures('trusted_by_id')
async def test_handle_grandfather_without_a_trusted_role_names_no_id(
    handles: ModuleType, cog: Any, ctx: MagicMock
) -> None:
    with pytest.raises(handles.HandleCogError) as raised:
        await type(cog).grandfather.callback(cog, ctx)

    assert str(raised.value) == NO_TRUSTED_ROLE


@pytest.mark.usefixtures('trusted_by_id')
async def test_handle_grandfather_reports_what_it_did(cog: Any, ctx: MagicMock) -> None:
    trusted = make_role(ctx.guild, TRUSTED_ROLE_ID, 'Veterans', position=2)
    purgatory = make_role(ctx.guild, PURGATORY_ROLE_ID, 'Purgatory', position=2)
    ctx.guild.roles = [*ctx.guild.roles, trusted, purgatory]
    early = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    late = dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc)
    old = make_member(ctx.guild, 1, joined_at=early)
    ctx.guild.members = [
        old,
        make_member(ctx.guild, 2, joined_at=late),
        make_member(ctx.guild, 3, trusted, joined_at=early),
        make_member(ctx.guild, 4, purgatory, joined_at=early),
    ]

    await type(cog).grandfather.callback(cog, ctx)

    assert old.roles == [trusted]
    assert replies(ctx) == [
        'Checking 4 members, to make those who joined before 21 April 2025 trusted…'
    ]
    status = ctx.send.return_value
    status.edit.assert_awaited_once_with(
        content='Done: I checked 4 members.\n'
        '- Made trusted: 1\n'
        '- Already trusted: 1\n'
        '- Joined on or after 21 April 2025, or unknown: 1\n'
        '- Could not be made trusted: 0\n'
        '- In purgatory: 1\n'
    )
