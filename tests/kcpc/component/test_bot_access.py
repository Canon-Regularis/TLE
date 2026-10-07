"""The access rules at work in the booted bot, for an ordinary member.

The bot boots as ``booting.booted`` boots it, with every extension and TLE's
own rule table, in a server with a bot channel and a staff channel. Commands
run as discord.py runs them: a prefix command from its message, as
``process_commands`` runs it, and a slash command from its interaction's data,
through the bot's command tree. Refusals are answered by the bot's own error
handler, which discord.py schedules as an event. Discord itself (the server,
its channels, the member, the message and the interaction) is mocked, and so
are posts in a channel, so that the tests see who would see each answer.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from tests.kcpc.component.booting import booted
from tle import constants
from tle.__main__ import TLEBot
from tle.access.context import TLEContext
from tle.access.rules import Outcome
from tle.access.service import SLASH_HINT, cached_decision
from tle.access.settings import GuildAccess
from tle.util.discord_common import NOT_ALLOWED_MESSAGE, REFUSAL_DELETE_AFTER

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
MEMBER_ID = 1_400_000_000_000_000_001
BOT_USER_ID = 1_400_000_000_000_000_050
UPCOMING = 'Upcoming contests'


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role, as without a .env."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
async def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TLEBot]:
    """The bot with every extension, in a server with a bot channel and a
    staff channel.
    """
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        # Who the bot is, which discord.py learns as it logs in.
        me = MagicMock(spec=discord.ClientUser, id=BOT_USER_ID)
        monkeypatch.setattr(TLEBot, 'user', property(lambda _: me))
        settings = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
        await bot.access.change(GUILD_ID, lambda _: settings)
        yield bot


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose channels every member can see."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    channels = {
        channel_id: MagicMock(
            spec=discord.TextChannel,
            id=channel_id,
            guild=guild,
            mention=f'<#{channel_id}>',
        )
        for channel_id in (BOT_CHANNEL_ID, STAFF_CHANNEL_ID, GENERAL_ID)
    }
    for channel in channels.values():
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
    guild.get_channel.side_effect = channels.get
    return guild


@pytest.fixture
def member(guild: MagicMock) -> MagicMock:
    """An ordinary member: no role, no permission."""
    member = MagicMock(spec=discord.Member, id=MEMBER_ID, guild=guild, bot=False)
    member.guild_permissions = discord.Permissions.none()
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


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers it.
    """

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)
        self.defer = AsyncMock(side_effect=self._answer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, *args: Any, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))


async def use_slash(
    bot: TLEBot, member: MagicMock, channel_id: int, *path: str
) -> MagicMock:
    """Use the slash command at ``path`` as ``member`` in a channel, through
    the bot's command tree; its interaction, once everything that it set off
    has run.
    """
    channel = member.guild.get_channel(channel_id)
    data: dict[str, Any] = {'type': 1, 'name': path[-1], 'options': []}
    for name in reversed(path[:-1]):
        data = {'type': 1, 'name': name, 'options': [data]}
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = discord.InteractionType.application_command
    interaction.guild_id = GUILD_ID
    interaction.user = member
    interaction.channel = channel
    interaction.data = data
    interaction.command_failed = False
    # What a real interaction works out from its data.
    command, options = bot.tree._get_app_command_options(data)
    interaction.command = command
    interaction.namespace = app_commands.Namespace(interaction, {}, options)
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
    await handled()
    assert isinstance(interaction._baton, TLEContext)
    return interaction


def answer(interaction: MagicMock) -> tuple[discord.Embed, bool]:
    """The one answer to ``interaction``, and whether only the member saw it."""
    send = cast(Response, interaction.response).send_message
    send.assert_awaited_once()
    assert send.await_args is not None
    options = send.await_args.kwargs
    return options['embed'], options.get('ephemeral', False)


def post(posted: AsyncMock) -> tuple[discord.Embed, dict[str, Any]]:
    """The one post in a channel, and what else it was sent with."""
    posted.assert_awaited_once()
    assert posted.await_args is not None
    options = dict(posted.await_args.kwargs)
    return options.pop('embed'), options


async def test_the_bot_owner_s_prefix_command_gets_no_reply(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    ctx = await use_prefix(bot, member, BOT_CHANNEL_ID, ';cache')

    # As if the command didn't exist.
    decision = cached_decision(ctx)
    assert decision is not None and decision.outcome is Outcome.NOT_ALLOWED
    posted.assert_not_awaited()


async def test_a_staff_slash_command_is_refused_privately_outside_the_staff_channel(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    interaction = await use_slash(bot, member, GENERAL_ID, 'kcpc', 'status')

    embed, private = answer(interaction)
    assert private
    assert embed.description == NOT_ALLOWED_MESSAGE == "You can't use this command."
    posted.assert_not_awaited()


async def test_a_member_command_answers_publicly_in_a_bot_channel(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    ctx = await use_prefix(bot, member, BOT_CHANNEL_ID, ';contests')
    interaction = await use_slash(bot, member, BOT_CHANNEL_ID, 'contests', 'upcoming')

    embed, options = post(posted)
    assert embed.title == UPCOMING
    # A reply to the member's message, which doesn't ping them.
    assert options['reference'] is ctx.message
    assert options['mention_author'] is False
    embed, private = answer(interaction)
    assert embed.title == UPCOMING and not private


async def test_elsewhere_its_slash_command_answers_only_the_member(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    interaction = await use_slash(bot, member, GENERAL_ID, 'contests', 'upcoming')

    embed, private = answer(interaction)
    assert embed.title == UPCOMING and private
    posted.assert_not_awaited()


async def test_elsewhere_its_prefix_command_is_refused(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    await use_prefix(bot, member, GENERAL_ID, ';contests')

    embed, options = post(posted)
    assert embed.description == (
        f'Use this command in a bot channel: <#{BOT_CHANNEL_ID}>. '
        + SLASH_HINT.format(path='/contests upcoming')
    )
    # The channel sees it, so it goes after a while.
    assert options['delete_after'] == REFUSAL_DELETE_AFTER


async def test_a_group_s_own_command_shows_the_group_s_help(
    bot: TLEBot, member: MagicMock, posted: AsyncMock
) -> None:
    # /clist show runs the group's own command, which asks discord.py for the
    # help of the group; /help gives it, privately on slash.
    interaction = await use_slash(bot, member, BOT_CHANNEL_ID, 'clist', 'show')
    await use_prefix(bot, member, BOT_CHANNEL_ID, ';clist')

    embed, private = answer(interaction)
    assert embed.title == 'clist' and private
    embed, _ = post(posted)
    assert embed.title == 'clist'
