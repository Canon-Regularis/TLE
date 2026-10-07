"""Tests for the starboard (tle.cogs.starboard): its commands, and the
listener that reposts a message once it gets enough reactions.

The cog keeps its settings in a real user database in memory (the ``user_db``
fixture). Most tests call a command's callback, as discord.py does once it has
parsed the arguments, or the listener, as discord.py does when a member
reacts. The rest run commands as admins and members use them, on a bot set up
as TLEBot sets itself up: the access service's check and command tree, the
bot's error handler, real TLEContexts and TLE's own rule table. Discord itself
(the server, its channels and members, messages and interactions) is mocked.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.access.context import TLEContext
from tle.access.service import AccessService, AccessTree
from tle.access.settings import GuildAccess
from tle.cogs import starboard
from tle.cogs.starboard import (
    ADDED_WITHOUT_CHANNEL_TEXT,
    BAD_COLOUR_TEXT,
    Starboard,
    StarboardCogError,
    parse_colour,
)
from tle.util import discord_common
from tle.util.db.user_db_conn import UserDbConn
from tle.util.discord_common import NOT_ALLOWED_MESSAGE, NOT_IN_A_THREAD_MESSAGE

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither the staff channel nor a starboard
STARBOARD_ID = 1_200_000_000_000_000_030
OTHER_STARBOARD_ID = 1_200_000_000_000_000_031
THREAD_ID = 1_200_000_000_000_000_040
MODS_ID = 1_200_000_000_000_000_050  # a channel, not the staff channel
MODERATORS_ROLE_ID = 1_250_000_000_000_000_001
MESSAGE_ID = 1_300_000_000_000_000_001
REPOST_ID = 1_300_000_000_000_000_002
ADMIN_ID = 1_400_000_000_000_000_001
MEMBER_ID = 1_400_000_000_000_000_002
BOT_USER_ID = 1_400_000_000_000_000_050
STAR = '\N{WHITE MEDIUM STAR}'
GOLD = 0xFFD700
DEFAULT_COLOUR = constants._DEFAULT_COLOR  # 0xFFAA10
SUBCOMMANDS = [
    'add',
    'clear',
    'delete',
    'edit_color',
    'edit_threshold',
    'here',
    'remove',
]


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
def bot(user_db: UserDbConn) -> MagicMock:
    """TLE's bot, without an access service until a test gives it one."""
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    return bot


@pytest.fixture
def cog(bot: MagicMock) -> Starboard:
    return Starboard(bot)


@pytest.fixture
def ctx() -> MagicMock:
    """The context of a command an admin uses in the starboard channel; replies
    are recorded.
    """
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    ctx.channel = text_channel(STARBOARD_ID)
    ctx.send = AsyncMock()
    return ctx


def text_channel(channel_id: int) -> MagicMock:
    return MagicMock(
        spec=discord.TextChannel, id=channel_id, mention=f'<#{channel_id}>'
    )


async def run(cog: Starboard, name: str, ctx: MagicMock, *args: object) -> None:
    """Run the starboard command ``name``, as discord.py does once it has
    parsed ``args``.
    """
    await getattr(Starboard, name).callback(cog, ctx, *args)


def reply(ctx: MagicMock) -> str | None:
    """The text of the one reply to ``ctx``, which says the command worked."""
    ctx.send.assert_awaited_once_with(embed=ANY)
    embed = ctx.send.await_args.kwargs['embed']
    assert embed.colour == discord_common.embed_success('').colour
    return cast(str | None, embed.description)


async def refusal(cog: Starboard, name: str, ctx: MagicMock, *args: object) -> str:
    """The text of the error that command ``name`` raises for ``args``; the
    cog's error handler shows it to the admin.
    """
    with pytest.raises(StarboardCogError) as raised:
        await run(cog, name, ctx, *args)
    ctx.send.assert_not_awaited()
    return str(raised.value)


async def stored(user_db: UserDbConn) -> dict[str, tuple[int, int]]:
    """The server's starboard emojis, each with its threshold and colour."""
    cursor = await user_db.conn.execute(
        'SELECT emoji, threshold, color FROM starboard_emoji_v1 WHERE guild_id = ?',
        (GUILD_ID,),
    )
    return {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}


def fields(embed: discord.Embed) -> dict[str, str | None]:
    return {field.name or '': field.value for field in embed.fields}


# Colours


@pytest.mark.parametrize('typed', ['#ffd700', 'ffd700', '0xFFD700', ' #FfD700 '])
def test_a_colour_is_six_hex_digits_after_an_optional_hash_or_0x(typed: str) -> None:
    assert parse_colour(typed) == GOLD


@pytest.mark.parametrize(
    'typed',
    [
        'gold',
        '#fd0',
        'fd0',  # not CSS's short form: int() read it as 0x000fd0
        'ffd7000',  # beyond the colours Discord takes
        '#ffd70g',
        '',
        '#',
        '0x',
        '##ffd700',
        '-ffd700',
        'ffd 700',
        '#ffd700ff',
    ],
)
def test_anything_else_gets_a_friendly_error(typed: str) -> None:
    with pytest.raises(StarboardCogError) as raised:
        parse_colour(typed)

    assert str(raised.value) == BAD_COLOUR_TEXT
    assert BAD_COLOUR_TEXT == 'Give the colour as six hex digits, such as `#ffd700`.'


# The commands


def test_the_access_rules_alone_decide_who_uses_the_starboard() -> None:
    # No role check of the starboard's own: /help offers its commands by the
    # access rules, which give them to admins, by role or by Manage Server.
    cog = Starboard(MagicMock(spec=commands.Bot))
    names = sorted(command.qualified_name for command in cog.walk_commands())

    assert names == ['starboard'] + [f'starboard {name}' for name in SUBCOMMANDS]
    for command in cog.walk_commands():
        assert command.checks == [], command.qualified_name


async def test_add_stores_the_emoji_with_its_threshold_and_colour(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)

    await run(cog, 'add', ctx, STAR, 5, '#ffd700')

    assert await user_db.get_starboard_entry(GUILD_ID, STAR) == (STARBOARD_ID, 5, GOLD)
    assert reply(ctx) == (
        f'Added {STAR}: a message that gets 5 {STAR} reactions is reposted in '
        f'<#{STARBOARD_ID}>, in colour `#ffd700`.'
    )


async def test_add_without_a_channel_says_how_to_choose_one(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await run(cog, 'add', ctx, STAR, 1, None)

    # Without a colour, the default one.
    assert await stored(user_db) == {STAR: (1, DEFAULT_COLOUR)}
    assert reply(ctx) == (
        f'Added {STAR}: once you use `/starboard here` in a channel, a message '
        f'that gets 1 {STAR} reaction is reposted there, in colour `#ffaa10`.'
    )


async def test_adding_an_emoji_again_replaces_its_threshold_and_colour(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await run(cog, 'add', ctx, STAR, 5, '#ffd700')
    await run(cog, 'add', ctx, STAR, 3, None)

    assert await stored(user_db) == {STAR: (3, DEFAULT_COLOUR)}


@pytest.mark.parametrize(
    ('name', 'args'),
    [('add', (STAR, 7, 'gold')), ('edit_color', (STAR, 'gold'))],
    ids=['add', 'edit_color'],
)
async def test_a_bad_colour_changes_nothing(
    cog: Starboard,
    ctx: MagicMock,
    user_db: UserDbConn,
    name: str,
    args: tuple[object, ...],
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)

    assert await refusal(cog, name, ctx, *args) == BAD_COLOUR_TEXT
    assert await stored(user_db) == {STAR: (5, GOLD)}


async def test_delete_removes_the_emoji_and_its_settings_but_not_its_reposts(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)
    await user_db.add_starboard_message(MESSAGE_ID, REPOST_ID, GUILD_ID, STAR)

    await run(cog, 'delete', ctx, STAR)

    assert await stored(user_db) == {}
    assert await user_db.get_starboard_entry(GUILD_ID, STAR) is None
    assert await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)
    assert reply(ctx) == f'Deleted {STAR} and its settings. Its reposts stay.'


async def test_delete_of_an_emoji_that_isn_t_on_the_starboard_says_so(
    cog: Starboard, ctx: MagicMock
) -> None:
    # It used to say that it had removed the emoji.
    assert await refusal(cog, 'delete', ctx, STAR) == (
        f"{STAR} isn't a starboard emoji."
    )


async def test_edit_threshold_changes_how_many_reactions_a_message_needs(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)

    await run(cog, 'edit_threshold', ctx, STAR, 10)

    assert await stored(user_db) == {STAR: (10, GOLD)}
    assert reply(ctx) == f'A message now needs 10 {STAR} reactions to be reposted.'


async def test_edit_color_changes_the_colour_of_new_reposts(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, DEFAULT_COLOUR)

    await run(cog, 'edit_color', ctx, STAR, '0xFFD700')

    assert await stored(user_db) == {STAR: (5, GOLD)}
    assert reply(ctx) == f'New reposts for {STAR} are in colour `#ffd700`.'


@pytest.mark.parametrize(
    ('name', 'args'),
    [('edit_threshold', (10,)), ('edit_color', ('#ffd700',))],
    ids=['edit_threshold', 'edit_color'],
)
async def test_editing_an_emoji_that_wasn_t_added_says_to_add_it(
    cog: Starboard,
    ctx: MagicMock,
    user_db: UserDbConn,
    name: str,
    args: tuple[object, ...],
) -> None:
    # Choosing its channel doesn't add an emoji. These used to say that they
    # had changed it.
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)

    assert await refusal(cog, name, ctx, STAR, *args) == (
        f"{STAR} isn't a starboard emoji. Add it with `/starboard add`."
    )
    assert await stored(user_db) == {}


async def test_here_makes_this_channel_the_emoji_s_starboard_channel(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)
    await user_db.set_starboard_channel(GUILD_ID, STAR, OTHER_STARBOARD_ID)

    await run(cog, 'here', ctx, STAR)

    # It replaces the emoji's earlier channel, and keeps its threshold and colour.
    assert await user_db.get_starboard_entry(GUILD_ID, STAR) == (STARBOARD_ID, 5, GOLD)
    assert reply(ctx) == f'Reposts for {STAR} now go to <#{STARBOARD_ID}>.'


async def test_here_refuses_a_thread(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # The listener finds the starboard channel among the server's channels,
    # which leaves threads out, so nothing was ever reposted there.
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)
    ctx.channel = MagicMock(
        spec=discord.Thread,
        id=THREAD_ID,
        parent_id=STARBOARD_ID,
        mention=f'<#{THREAD_ID}>',
    )

    assert await refusal(cog, 'here', ctx, STAR) == NOT_IN_A_THREAD_MESSAGE
    assert await user_db.get_starboard_entry(GUILD_ID, STAR) is None


def test_here_takes_an_emoji_alone_as_its_help_says() -> None:
    # Its help used to offer a colour, which it never took.
    here = Starboard.here

    assert list(here.clean_params) == ['emoji']
    assert 'colo' not in f'{here.brief} {here.help}'.lower()


async def test_clear_stops_reposts_but_keeps_the_threshold_and_colour(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)

    await run(cog, 'clear', ctx, STAR)

    assert await user_db.get_starboard_entry(GUILD_ID, STAR) is None
    assert await stored(user_db) == {STAR: (5, GOLD)}
    assert reply(ctx) == (
        f'Stopped posting the starred messages of {STAR}. Its threshold and '
        'colour stay, ready for `/starboard here`.'
    )
    # As its help now says: it used to say that the colour goes too.
    assert 'keeps its threshold and colour' in (Starboard.clear.help or '')


async def test_clear_of_an_emoji_without_a_channel_says_so(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 5, GOLD)

    assert await refusal(cog, 'clear', ctx, STAR) == (
        f'{STAR} has no starboard channel.'
    )


async def test_remove_forgets_a_repost_so_that_the_message_can_be_reposted(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.add_starboard_message(MESSAGE_ID, REPOST_ID, GUILD_ID, STAR)

    await run(cog, 'remove', ctx, STAR, MESSAGE_ID)

    assert not await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)
    assert reply(ctx) == (
        f"Forgot that message's repost for {STAR}: it can be reposted again."
    )


async def test_remove_of_a_message_without_a_repost_says_so(
    cog: Starboard, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # The repost's own ID, which isn't the one the command takes.
    await user_db.add_starboard_message(MESSAGE_ID, REPOST_ID, GUILD_ID, STAR)

    assert await refusal(cog, 'remove', ctx, STAR, REPOST_ID) == (
        f"That message hasn't been reposted with {STAR}."
    )
    assert await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)


# The listener


@pytest.fixture
async def star(user_db: UserDbConn) -> None:
    """STAR is a starboard emoji: 3 reactions repost a message, in gold, in the
    starboard channel.
    """
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 3, GOLD)
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)


@pytest.fixture
def channels(bot: MagicMock) -> dict[int, MagicMock]:
    """The server's channels by id, as discord.py's cache finds them; the
    starboard channel records what is posted in it.
    """
    found: dict[int, MagicMock] = {}
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.get_channel.side_effect = found.get
    bot.get_guild.return_value = guild
    bot.get_channel.side_effect = found.get
    for channel_id in (STAFF_CHANNEL_ID, GENERAL_ID, STARBOARD_ID):
        found[channel_id] = text_channel(channel_id)
    found[STARBOARD_ID].send = AsyncMock(
        return_value=MagicMock(spec=discord.Message, id=REPOST_ID)
    )
    return found


@pytest.fixture
async def staff_channel(bot: MagicMock) -> AccessService:
    """The bot's access service, which knows the server's staff channel."""
    access = AccessService(bot)
    bot.access = access
    await access.change(GUILD_ID, lambda _: GuildAccess(staff_channel=STAFF_CHANNEL_ID))
    return access


def place(
    channels: dict[int, MagicMock],
    channel_id: int,
    *,
    thread: bool,
    private: bool = False,
) -> MagicMock:
    """The channel ``channel_id``, or a thread in it that the cache holds,
    public unless ``private``.
    """
    if not thread:
        return channels[channel_id]
    found = MagicMock(
        spec=discord.Thread,
        id=THREAD_ID,
        parent_id=channel_id,
        mention=f'<#{THREAD_ID}>',
    )
    # Not as MagicMock(parent=...), which sets the mock's own parent instead.
    found.parent = channels[channel_id]
    found.is_private.return_value = private
    channels[THREAD_ID] = found
    return found


@pytest.fixture
def roles(bot: MagicMock, channels: dict[int, MagicMock]) -> list[MagicMock]:
    """The server's roles, @everyone and Moderators, and MODS_ID, a channel
    that isn't the staff channel. Until a test says who can see a channel,
    every role can.
    """
    everyone = MagicMock(spec=discord.Role, id=GUILD_ID)
    moderators = MagicMock(spec=discord.Role, id=MODERATORS_ROLE_ID)
    bot.get_guild.return_value.roles = [everyone, moderators]
    channels[MODS_ID] = text_channel(MODS_ID)
    for channel in channels.values():
        seen_by(channel, everyone, moderators)
    return [everyone, moderators]


def seen_by(channel: MagicMock, *roles: MagicMock) -> None:
    """Let ``roles``, and no other role, see ``channel``."""
    channel.permissions_for.side_effect = lambda role: discord.Permissions(
        view_channel=role in roles
    )


def starred(
    channel: MagicMock, reactions: int, content: str = 'A neat trick'
) -> MagicMock:
    """Message MESSAGE_ID in ``channel``, with ``reactions`` STAR reactions,
    which the channel fetches.
    """
    message = MagicMock(
        spec=discord.Message,
        id=MESSAGE_ID,
        channel=channel,
        content=content,
        type=discord.MessageType.default,
        attachments=[],
        embeds=[],
        jump_url=f'https://discord.com/channels/{GUILD_ID}/{channel.id}/{MESSAGE_ID}',
        created_at=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
    )
    reaction = MagicMock(spec=discord.Reaction, count=reactions)
    reaction.__str__.return_value = STAR
    message.reactions = [reaction]
    message.author.__str__.return_value = 'fake_member'
    channel.fetch_message = AsyncMock(return_value=message)
    return message


async def react(cog: Starboard, channel_id: int) -> None:
    """A member reacts with STAR to message MESSAGE_ID, as discord.py tells
    the cog.
    """
    payload = MagicMock(
        spec=discord.RawReactionActionEvent,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        message_id=MESSAGE_ID,
        emoji=discord.PartialEmoji(name=STAR),
    )
    await cog.on_raw_reaction_add(payload)


def repost(channels: dict[int, MagicMock]) -> discord.Embed:
    """The one repost in the starboard channel."""
    send = channels[STARBOARD_ID].send
    send.assert_awaited_once_with(embed=ANY)
    embed = send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


@pytest.mark.usefixtures('star', 'staff_channel')
@pytest.mark.parametrize('thread', [False, True], ids=['channel', 'thread'])
async def test_a_message_that_gets_enough_reactions_is_reposted(
    cog: Starboard,
    channels: dict[int, MagicMock],
    user_db: UserDbConn,
    thread: bool,
) -> None:
    channel = place(channels, GENERAL_ID, thread=thread)
    starred(channel, reactions=3)

    await react(cog, channel.id)

    embed = repost(channels)
    assert embed.colour == discord.Colour(GOLD)
    assert fields(embed)['Content'] == 'A neat trick'
    assert await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)


@pytest.mark.usefixtures('staff_channel')
async def test_a_black_starboard_emoji_reposts_in_black(
    cog: Starboard, channels: dict[int, MagicMock], user_db: UserDbConn
) -> None:
    # #000000 is 0, which used to count as no colour: the reposts came in the
    # default one, though the reply to the admin had said black.
    await user_db.add_starboard_emoji(GUILD_ID, STAR, 3, 0x000000)
    await user_db.set_starboard_channel(GUILD_ID, STAR, STARBOARD_ID)
    starred(channels[GENERAL_ID], reactions=3)

    await react(cog, GENERAL_ID)

    assert repost(channels).colour == discord.Colour(0x000000)


@pytest.mark.usefixtures('star', 'staff_channel')
@pytest.mark.parametrize('thread', [False, True], ids=['channel', 'thread'])
async def test_a_message_in_the_staff_channel_is_never_reposted(
    cog: Starboard,
    channels: dict[int, MagicMock],
    user_db: UserDbConn,
    thread: bool,
) -> None:
    channel = place(channels, STAFF_CHANNEL_ID, thread=thread)
    starred(channel, reactions=10)

    await react(cog, channel.id)

    channels[STARBOARD_ID].send.assert_not_awaited()
    # Nor even fetched: nothing of the staff channel's leaves it.
    channel.fetch_message.assert_not_awaited()
    assert not await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)


@pytest.mark.usefixtures('star', 'staff_channel')
@pytest.mark.parametrize('thread', [False, True], ids=['channel', 'thread'])
async def test_a_message_that_some_readers_of_the_starboard_cannot_see_stays_put(
    cog: Starboard,
    channels: dict[int, MagicMock],
    roles: list[MagicMock],
    user_db: UserDbConn,
    thread: bool,
) -> None:
    # A channel for moderators alone, or a public thread in it: everyone can
    # read the starboard, so a repost would show it to them all.
    _, moderators = roles
    seen_by(channels[MODS_ID], moderators)
    channel = place(channels, MODS_ID, thread=thread)
    starred(channel, reactions=10)

    await react(cog, channel.id)

    channels[STARBOARD_ID].send.assert_not_awaited()
    channel.fetch_message.assert_not_awaited()
    assert not await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)


@pytest.mark.usefixtures('star', 'staff_channel', 'roles')
async def test_a_message_in_a_private_thread_stays_put(
    cog: Starboard, channels: dict[int, MagicMock], user_db: UserDbConn
) -> None:
    # Its channel is public, but only those added to the thread can read it.
    thread = place(channels, GENERAL_ID, thread=True, private=True)
    starred(thread, reactions=10)

    await react(cog, THREAD_ID)

    channels[STARBOARD_ID].send.assert_not_awaited()
    thread.fetch_message.assert_not_awaited()
    assert not await user_db.check_exists_starboard_message(MESSAGE_ID, STAR)


@pytest.mark.usefixtures('star', 'staff_channel')
@pytest.mark.parametrize(
    ('source', 'thread', 'starboard_for_moderators'),
    [(GENERAL_ID, False, False), (GENERAL_ID, True, False), (MODS_ID, False, True)],
    ids=['a public channel', 'a public thread', 'both for moderators'],
)
async def test_a_message_every_reader_of_the_starboard_can_see_is_reposted(
    cog: Starboard,
    channels: dict[int, MagicMock],
    roles: list[MagicMock],
    source: int,
    thread: bool,
    starboard_for_moderators: bool,
) -> None:
    _, moderators = roles
    seen_by(channels[MODS_ID], moderators)
    if starboard_for_moderators:
        seen_by(channels[STARBOARD_ID], moderators)
    channel = place(channels, source, thread=thread)
    starred(channel, reactions=3)

    await react(cog, channel.id)

    assert fields(repost(channels))['Content'] == 'A neat trick'


@pytest.mark.usefixtures('star')
@pytest.mark.parametrize('service', [False, True], ids=['no service', 'no channel'])
async def test_with_no_staff_channel_known_any_channel_s_messages_are_reposted(
    cog: Starboard, bot: MagicMock, channels: dict[int, MagicMock], service: bool
) -> None:
    # Without an access service, as before the access rules, or without a
    # staff channel set, no channel is the staff channel.
    if service:
        bot.access = AccessService(bot)
    starred(channels[STAFF_CHANNEL_ID], reactions=3)

    await react(cog, STAFF_CHANNEL_ID)

    repost(channels)


@pytest.mark.usefixtures('star')
async def test_while_the_access_settings_need_repair_nothing_is_reposted(
    cog: Starboard,
    bot: MagicMock,
    channels: dict[int, MagicMock],
    user_db: UserDbConn,
) -> None:
    # The server's stored settings can't be read, so its staff channel is
    # unknown: any channel might be it.
    await user_db.set_access_settings(GUILD_ID, 'not json')
    access = AccessService(bot)
    access.use_user_db(user_db)
    await access.load()
    bot.access = access
    assert access.guild_access(GUILD_ID).broken
    channel = channels[GENERAL_ID]
    starred(channel, reactions=10)

    await react(cog, GENERAL_ID)

    channels[STARBOARD_ID].send.assert_not_awaited()
    channel.fetch_message.assert_not_awaited()


@pytest.mark.usefixtures('star', 'staff_channel')
async def test_a_message_whose_channel_the_bot_can_t_find_is_left_alone(
    cog: Starboard, channels: dict[int, MagicMock]
) -> None:
    # Such as a thread no longer in discord.py's cache: the reaction used to
    # raise AttributeError in the listener.
    await react(cog, THREAD_ID)

    channels[STARBOARD_ID].send.assert_not_awaited()


@pytest.mark.parametrize(
    ('length', 'shown'),
    [(1024, 'x' * 1024), (1025, 'x' * 1023 + '…'), (2000, 'x' * 1023 + '…')],
    ids=['1024', '1025', '2000'],
)
def test_a_long_message_is_cut_short_in_its_repost(length: int, shown: str) -> None:
    # Discord refuses a field longer than 1024 characters, so such a message
    # was never reposted. The repost links to the whole of it.
    message = starred(text_channel(GENERAL_ID), reactions=3, content='x' * length)

    embed = Starboard.prepare_embed(message, GOLD)

    assert fields(embed)['Content'] == shown
    assert fields(embed)['Jump to'] == f'[Original]({message.jump_url})'


# As admins and members use the commands


class TLELikeBot(commands.Bot):
    """A bot that carries an access service and a user database, and makes
    TLEContexts, as TLEBot does.
    """

    access: AccessService
    user_db: UserDbConn

    async def get_context(self, origin: Any, /, *, cls: Any = None) -> Any:
        return await super().get_context(origin, cls=cls or TLEContext)


@pytest.fixture
async def live_bot(
    user_db: UserDbConn, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[TLELikeBot]:
    """The bot with the starboard, set up as TLEBot sets itself up, in a server
    whose staff channel is STAFF_CHANNEL_ID.
    """
    bot = TLELikeBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    # The context manager sets the bot up for the running loop, as logging in
    # would, so that discord.py can schedule events such as command errors.
    async with bot:
        bot.access = AccessService(bot)
        bot.add_check(bot.access.check)
        bot.add_listener(discord_common.bot_error_handler, name='on_command_error')
        bot.user_db = user_db
        await starboard.setup(bot)
        settings = GuildAccess(staff_channel=STAFF_CHANNEL_ID)
        await bot.access.change(GUILD_ID, lambda _: settings)
        # Who the bot is, which discord.py learns as it logs in.
        me = MagicMock(spec=discord.ClientUser, id=BOT_USER_ID)
        monkeypatch.setattr(TLELikeBot, 'user', property(lambda _: me))
        yield bot


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose channels every member can see."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    found = {}
    for channel_id in (STAFF_CHANNEL_ID, GENERAL_ID):
        found[channel_id] = text_channel(channel_id)
        found[channel_id].guild = guild
    guild.get_channel.side_effect = found.get
    return guild


def make_member(guild: MagicMock, member_id: int, *, manage_guild: bool) -> MagicMock:
    """A member without roles, who has Manage Server if ``manage_guild``."""
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild, bot=False)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    return member


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in a channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


async def handled() -> None:
    """Let the events that discord.py has scheduled, such as the bot's error
    handler, run to the end.
    """
    while True:
        pending = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith('discord.py: ') and not task.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending)


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers it.
    """

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, *args: Any, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))


async def use(
    bot: TLELikeBot,
    member: MagicMock,
    channel_id: int,
    name: str,
    *,
    slash: bool,
    **options: str | int,
) -> TLEContext:
    """Use the starboard command ``name`` with ``options``, as ``member`` in a
    channel: as a prefix command, from its message, as ``process_commands``
    runs it, or as a slash command, from its interaction's data, through the
    bot's command tree. The context discord.py made for it, once everything
    that the command set off has run.
    """
    channel = member.guild.get_channel(channel_id)
    if slash:
        ctx = await use_slash(bot, member, channel, name, options)
    else:
        typed = ' '.join(['starboard', name, *map(str, options.values())])
        message = MagicMock(
            spec=discord.Message,
            content=f';{typed}',
            author=member,
            channel=channel,
            guild=member.guild,
            jump_url='https://discord.com/channels/1/2/3',
        )
        message.mentions = []
        ctx = await bot.get_context(message)
        await bot.invoke(ctx)
    await handled()
    assert isinstance(ctx, TLEContext)
    return ctx


async def use_slash(
    bot: TLELikeBot,
    member: MagicMock,
    channel: MagicMock,
    name: str,
    options: dict[str, str | int],
) -> Any:
    given = [
        {'type': 4 if isinstance(value, int) else 3, 'name': option, 'value': value}
        for option, value in options.items()
    ]
    subcommand = {'type': 1, 'name': name, 'options': given}
    data: dict[str, Any] = {'type': 1, 'name': 'starboard', 'options': [subcommand]}
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = discord.InteractionType.application_command
    interaction.guild_id = GUILD_ID
    interaction.user = member
    interaction.channel = channel
    interaction.data = data
    interaction.command_failed = False
    # What a real interaction works out from its data: the command, and the
    # options given.
    command, found = bot.tree._get_app_command_options(data)
    interaction.command = command
    interaction.namespace = app_commands.Namespace(interaction, {}, found)
    # discord.py makes the context's message up from the interaction.
    interaction.message = MagicMock(
        spec=discord.Message, guild=member.guild, author=member, channel=channel
    )
    interaction.is_expired.return_value = False
    interaction.response = Response()
    interaction.followup.send = AsyncMock(
        return_value=MagicMock(spec=discord.WebhookMessage)
    )
    await bot.tree._call(interaction)
    return interaction._baton


def answers(ctx: TLEContext, posted: AsyncMock) -> list[tuple[str | None, bool]]:
    """The text of each answer to ``ctx``'s command, and whether only the member
    who used it saw it.
    """
    if ctx.interaction is None:
        return [
            (call.kwargs['embed'].description, False) for call in posted.await_args_list
        ]
    send = cast(Response, ctx.interaction.response).send_message
    posted.assert_not_awaited()
    return [
        (call.kwargs['embed'].description, call.kwargs.get('ephemeral', False))
        for call in send.await_args_list
    ]


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_an_admin_by_the_manage_server_permission_alone_can_use_it(
    live_bot: TLELikeBot,
    guild: MagicMock,
    user_db: UserDbConn,
    posted: AsyncMock,
    slash: bool,
) -> None:
    # Without TLE's admin role: the access rules count Manage Server as admin,
    # and the starboard has no role check of its own that would refuse it.
    admin = make_member(guild, ADMIN_ID, manage_guild=True)

    ctx = await use(
        live_bot, admin, STAFF_CHANNEL_ID, 'add', slash=slash, emoji=STAR, threshold=5
    )

    added = ADDED_WITHOUT_CHANNEL_TEXT.format(
        emoji=STAR, reactions=f'5 {STAR} reactions', colour='#ffaa10'
    )
    assert answers(ctx, posted) == [(added, False)]
    assert await stored(user_db) == {STAR: (5, DEFAULT_COLOUR)}


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_members_can_t_use_it(
    live_bot: TLELikeBot,
    guild: MagicMock,
    user_db: UserDbConn,
    posted: AsyncMock,
    slash: bool,
) -> None:
    member = make_member(guild, MEMBER_ID, manage_guild=False)

    ctx = await use(
        live_bot, member, STAFF_CHANNEL_ID, 'add', slash=slash, emoji=STAR, threshold=5
    )

    # On prefix, silently, as if the command didn't exist.
    assert answers(ctx, posted) == ([(NOT_ALLOWED_MESSAGE, True)] if slash else [])
    assert await stored(user_db) == {}


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_a_bad_colour_gets_a_friendly_error(
    live_bot: TLELikeBot,
    guild: MagicMock,
    user_db: UserDbConn,
    posted: AsyncMock,
    caplog: pytest.LogCaptureFixture,
    slash: bool,
) -> None:
    admin = make_member(guild, ADMIN_ID, manage_guild=True)

    with caplog.at_level(logging.INFO):
        ctx = await use(
            live_bot,
            admin,
            STAFF_CHANNEL_ID,
            'add',
            slash=slash,
            emoji=STAR,
            threshold=5,
            color='gold',
        )

    # Not "Something went wrong", with the error logged, as when the colour
    # went to int() as typed. On slash, only the admin sees it.
    assert answers(ctx, posted) == [(BAD_COLOUR_TEXT, slash)]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert await stored(user_db) == {}
