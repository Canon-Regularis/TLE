"""The bot owner's meta and cache commands in the booted bot.

They concern every server the bot is in, so the bot owner alone may use
them, whatever their roles; TLE's admin role no longer lets anyone in. The bot
boots as ``booting.booted`` boots it, with every extension and TLE's own rule
table, and its owner is found as it starts. Prefix commands run from their
messages, as ``process_commands`` runs them, and refusals are answered by the
bot's own error handler. Discord itself (the server, its channels, the
members and the messages) is mocked, and so are posts in a channel.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tests.kcpc.component.booting import OWNER_ID, booted
from tle import constants
from tle.__main__ import TLEBot
from tle.access.context import TLEContext
from tle.access.rules import Outcome
from tle.access.service import cached_decision
from tle.access.settings import GuildAccess
from tle.cogs.cache_control import NOT_A_RATING_CHANGES_MODE

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
ADMIN_ID = 1_400_000_000_000_000_001
GUILD_OWNER_ID = 1_400_000_000_000_000_002
BOT_USER_ID = 1_400_000_000_000_000_050


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role, as without a .env."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose channels every member can see."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID, owner_id=GUILD_OWNER_ID)
    guild.name = 'KCPC'  # not MagicMock(name=...), which names the mock itself
    guild.icon = None
    channels = {
        channel_id: MagicMock(spec=discord.TextChannel, id=channel_id, guild=guild)
        for channel_id in (BOT_CHANNEL_ID, STAFF_CHANNEL_ID, GENERAL_ID)
    }
    for channel in channels.values():
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
    guild.get_channel.side_effect = channels.get
    return guild


@pytest.fixture
async def bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guild: MagicMock
) -> AsyncIterator[TLEBot]:
    """The bot with every extension, in ``guild``, which has a bot channel and
    a staff channel.
    """
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        # What discord.py learns as the bot logs in: who it is, and its
        # servers.
        me = MagicMock(spec=discord.ClientUser, id=BOT_USER_ID)
        monkeypatch.setattr(TLEBot, 'user', property(lambda _: me))
        monkeypatch.setattr(TLEBot, 'guilds', property(lambda _: [guild]))
        settings = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
        await bot.access.change(GUILD_ID, lambda _: settings)
        yield bot


@pytest.fixture
def cf_cache(bot: TLEBot, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The Codeforces caches, which TLE's database setup would start and
    booting skips, with an empty contest list; the bot closes their database
    as it stops.
    """
    cf_cache = MagicMock()
    cf_cache.contest_cache.reload_now = AsyncMock()
    cf_cache.contest_cache.contest_by_id = {}
    cf_cache.conn.close = AsyncMock()
    monkeypatch.setattr(bot, 'cf_cache', cf_cache, raising=False)
    return cf_cache


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in a channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


def make_member(
    guild: MagicMock,
    member_id: int,
    *roles: str,
    permissions: discord.Permissions | None = None,
) -> MagicMock:
    """A member with roles named ``roles``; direct messages to them are
    recorded by their ``send``.
    """
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild, bot=False)
    member.guild_permissions = permissions or discord.Permissions.none()
    member.roles = []
    for name in roles:
        role = MagicMock(spec=discord.Role)
        role.name = name
        member.roles.append(role)
    return member


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


async def use_prefix(
    bot: TLEBot, member: MagicMock, channel_id: int, content: str
) -> TLEContext:
    """Send ``content`` as ``member`` in a channel; the context discord.py
    made of it, once everything that it set off has run.
    """
    channel = member.guild.get_channel(channel_id)
    message = MagicMock(
        spec=discord.Message,
        content=content,
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


async def test_the_bot_owner_gets_the_server_list_without_any_role(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock
) -> None:
    owner = make_member(guild, OWNER_ID)

    ctx = await use_prefix(bot, owner, GENERAL_ID, ';meta guilds')

    decision = cached_decision(ctx)
    assert decision is not None and decision.outcome is Outcome.PUBLIC
    owner.send.assert_awaited_once_with(
        f'```\nGuild ID: {GUILD_ID} | Name: KCPC | Owner: {GUILD_OWNER_ID} | '
        'Icon: None\n```'
    )
    posted.assert_not_awaited()


async def test_the_bot_owner_reloads_a_cache_without_any_role(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock, cf_cache: MagicMock
) -> None:
    owner = make_member(guild, OWNER_ID)

    await use_prefix(bot, owner, BOT_CHANNEL_ID, ';cache contests')

    cf_cache.contest_cache.reload_now.assert_awaited_once_with()
    sent = [call.kwargs['content'] for call in posted.await_args_list]
    assert sent[0] == 'Running...'
    assert sent[1].startswith('Completed in ')


async def test_the_bot_owner_is_told_which_contests_can_be_fetched(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock, cf_cache: MagicMock
) -> None:
    owner = make_member(guild, OWNER_ID)

    await use_prefix(bot, owner, BOT_CHANNEL_ID, ';cache ratingchanges 19x50')

    running, answer = posted.await_args_list
    assert running.kwargs['content'] == 'Running...'
    # The error handler's answer to a mistake, not "Something went wrong".
    assert answer.kwargs['embed'].description == NOT_A_RATING_CHANGES_MODE
    cf_cache.rating_changes_cache.fetch_contest.assert_not_called()


async def test_tle_s_admins_get_no_reply_from_the_bot_owner_s_commands(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Should meta kill run, it must not stop the tests.
    exit_ = MagicMock()
    monkeypatch.setattr(sys, 'exit', exit_)
    admin = make_member(
        guild, ADMIN_ID, 'Admin', permissions=discord.Permissions(manage_guild=True)
    )

    for content in (';meta guilds', ';meta kill', ';cache', ';cache contests'):
        ctx = await use_prefix(bot, admin, STAFF_CHANNEL_ID, content)
        # As if the command didn't exist.
        decision = cached_decision(ctx)
        assert decision is not None, content
        assert decision.outcome is Outcome.NOT_ALLOWED, content

    posted.assert_not_awaited()
    admin.send.assert_not_awaited()
    exit_.assert_not_called()
