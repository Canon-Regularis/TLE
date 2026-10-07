"""Tests for AccessTree, the bot's slash command tree.

Before any command runs, it refuses private messages, servers outside the
allow-list, and application commands that the bot's check can't see. It
offers autocomplete only to members who may use the command where they are,
and answers privately a command that failed before anything answered it. The
bot and its tree are real, and autocomplete runs through discord.py's own tree
code; the interactions are mocked.
"""

import logging
from collections.abc import AsyncIterator
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.access import table
from tle.access.rules import Limit, Rule, Where, Who
from tle.access.service import (
    COMMAND_CHANGED_TEXT,
    NOT_AVAILABLE_TEXT,
    PRIVATE_MESSAGES_TEXT,
    UNCHECKED_COMMAND_TEXT,
    AccessService,
    AccessTree,
)
from tle.access.settings import GuildAccess
from tle.util.discord_common import UNEXPECTED_ERROR_MESSAGE, embed_alert

LOGGER = 'tle.access'
TREE_LOGGER = 'discord.app_commands.tree'
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020
MEMBER_ID = 1_400_000_000_000_000_001
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_005

RULES = {
    'ping': Rule(Who.EVERYONE, Where.BOT),
    'challenge': Rule(Who.EVERYONE, Where.BOT_ONLY),
    'kcpc': Rule(Who.ADMIN, Where.STAFF),
    'kcpc weekly': Rule(Who.ADMIN, Where.STAFF),
    'kcpc weekly unqueue': Rule(Who.ADMIN, Where.STAFF),
}
# What a member types: /kcpc weekly unqueue problem:<typing>.
UNQUEUE = {
    'type': 1,
    'name': 'kcpc',
    'options': [
        {
            'type': 2,
            'name': 'weekly',
            'options': [
                {
                    'type': 1,
                    'name': 'unqueue',
                    'options': [
                        {'type': 3, 'name': 'problem', 'value': 'tw', 'focused': True}
                    ],
                }
            ],
        }
    ],
}
SUGGESTIONS = [app_commands.Choice(name='Two Sum (queued)', value='1A')]


class Weekly(commands.Cog):
    """Admin commands whose autocomplete shows what only admins may see."""

    def __init__(self) -> None:
        self.suggested = 0

    async def queued(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        self.suggested += 1
        return SUGGESTIONS

    @commands.hybrid_group()
    async def kcpc(self, ctx: commands.Context[Any]) -> None:
        """KCPC settings."""

    @kcpc.group()
    async def weekly(self, ctx: commands.Context[Any]) -> None:
        """The weekly problem."""

    @weekly.command()
    @app_commands.autocomplete(problem=queued)
    async def unqueue(self, ctx: commands.Context[Any], problem: str) -> None:
        """Take a problem off the queue."""

    @commands.hybrid_command()
    @app_commands.autocomplete(opponent=queued)
    async def challenge(self, ctx: commands.Context[Any], opponent: str) -> None:
        """Challenge someone, in bot channels only."""

    @commands.hybrid_command()
    async def ping(self, ctx: commands.Context[Any]) -> None:
        """Answer."""


class AccessBot(commands.Bot):
    access: AccessService


@pytest.fixture(autouse=True)
def rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table, 'RULES', MappingProxyType(RULES))
    monkeypatch.setattr(table, 'TWINS', MappingProxyType({}))
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', DEVELOPER_ROLE_ID)


async def make_bot(**options: Any) -> AccessBot:
    bot = AccessBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    if options.get('access', True):
        allowed = options.get('allowed_guilds', frozenset())
        bot.access = AccessService(bot, allowed_guilds=allowed)
        bot.add_check(bot.access.check)
        await bot.access.change(
            GUILD_ID,
            lambda _: GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID),
        )
    await bot.add_cog(Weekly())
    return bot


@pytest.fixture
async def bot() -> AsyncIterator[AccessBot]:
    bot = await make_bot()
    yield bot
    await bot.close()


@pytest.fixture
async def gated_bot() -> AsyncIterator[AccessBot]:
    """A bot that only GUILD_ID may use."""
    bot = await make_bot(allowed_guilds=frozenset({GUILD_ID}))
    yield bot
    await bot.close()


@pytest.fixture
async def plain_bot() -> AsyncIterator[AccessBot]:
    """A bot with the tree but without an access service."""
    bot = await make_bot(access=False)
    yield bot
    await bot.close()


def weekly(bot: commands.Bot) -> Weekly:
    cog = bot.get_cog('Weekly')
    assert isinstance(cog, Weekly)
    return cog


def make_member(*roles: str | int, manage_guild: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=MEMBER_ID)
    member.guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    for identifier in roles:
        role = MagicMock(spec=discord.Role)
        role.id = identifier if isinstance(identifier, int) else 1
        role.name = identifier if isinstance(identifier, str) else 'Some role'
        member.roles.append(role)
    return member


def make_place(channel_id: int = GENERAL_ID) -> MagicMock:
    return MagicMock(spec=discord.TextChannel, id=channel_id)


class Response:
    """Stands in for an interaction's response."""

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)
        self.autocomplete = AsyncMock(side_effect=self._answer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, *args: Any, **kwargs: Any) -> None:
        self.done = True


def make_interaction(
    bot: commands.Bot,
    *,
    autocomplete: bool = False,
    user: Any = None,
    guild_id: int | None = GUILD_ID,
    place: Any = None,
    command: Any = None,
    data: Any = None,
) -> MagicMock:
    """An interaction with ``bot``'s tree: a slash command, or with
    ``autocomplete`` a member typing one of its options.
    """
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = (
        discord.InteractionType.autocomplete
        if autocomplete
        else discord.InteractionType.application_command
    )
    interaction.guild_id = guild_id
    interaction.user = make_member() if user is None else user
    interaction.channel = make_place() if place is None else place
    interaction.command = command
    interaction.data = data
    interaction.is_expired.return_value = False
    interaction.response = Response()
    interaction.followup.send = AsyncMock()
    return interaction


def unqueue_typed(bot: commands.Bot, user: Any, place: Any = None) -> MagicMock:
    """A member typing the problem option of /kcpc weekly unqueue."""
    command = bot.tree.get_command('kcpc')
    assert isinstance(command, app_commands.Group)
    group = command.get_command('weekly')
    assert isinstance(group, app_commands.Group)
    return make_interaction(
        bot,
        autocomplete=True,
        user=user,
        place=place,
        command=group.get_command('unqueue'),
        data=UNQUEUE,
    )


def answer(interaction: MagicMock) -> str | None:
    """The text of the one private alert that answered ``interaction``."""
    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.await_args.kwargs['ephemeral'] is True
    embed = send.await_args.kwargs['embed']
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    description: str | None = embed.description
    return description


def unanswered(interaction: MagicMock) -> None:
    interaction.response.send_message.assert_not_awaited()
    interaction.response.autocomplete.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


# Private messages and the allow-list


async def test_a_slash_command_in_private_messages_is_refused_privately(
    bot: AccessBot,
) -> None:
    interaction = make_interaction(bot, guild_id=None)

    assert not await bot.tree.interaction_check(interaction)
    assert (
        answer(interaction)
        == PRIVATE_MESSAGES_TEXT
        == ('Commands work only in servers, not in private messages.')
    )


async def test_a_slash_command_outside_the_allowed_servers_is_refused_privately(
    gated_bot: AccessBot,
) -> None:
    outside = make_interaction(gated_bot, guild_id=OTHER_GUILD_ID)
    inside = make_interaction(gated_bot)

    assert not await gated_bot.tree.interaction_check(outside)
    assert (
        answer(outside)
        == NOT_AVAILABLE_TEXT
        == ('This bot is not available in this server.')
    )
    assert await gated_bot.tree.interaction_check(inside)
    unanswered(inside)


@pytest.mark.parametrize('guild_id', [None, OTHER_GUILD_ID], ids=['dm', 'outside'])
async def test_autocomplete_there_is_refused_without_an_answer(
    gated_bot: AccessBot, guild_id: int | None
) -> None:
    interaction = unqueue_typed(gated_bot, make_member(manage_guild=True))
    interaction.guild_id = guild_id

    assert not await gated_bot.tree.interaction_check(interaction)
    unanswered(interaction)


async def test_a_slash_command_in_an_allowed_server_goes_on_to_the_bot_s_check(
    bot: AccessBot,
) -> None:
    # Even for a member who may not use it: the check refuses it with its own
    # reply.
    command = bot.tree.get_command('challenge')
    interaction = make_interaction(bot, command=command, user=make_member())

    assert await bot.tree.interaction_check(interaction)
    unanswered(interaction)


async def unwrapped(interaction: discord.Interaction, text: str) -> None:
    """A slash command alone, which no prefix command wraps."""


async def inspect_member(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    """A context menu, which no prefix command wraps either."""


@pytest.mark.parametrize(
    'command',
    [
        app_commands.Command(name='plain', description='Plain', callback=unwrapped),
        app_commands.ContextMenu(name='Inspect', callback=inspect_member),
    ],
    ids=['slash command', 'context menu'],
)
async def test_a_command_the_bot_s_check_cannot_see_is_refused_privately(
    bot: AccessBot, command: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # The bot's check runs for prefix commands alone, and for the slash forms
    # that wrap them, so nothing else would refuse it.
    admin = make_member('Admin', manage_guild=True)
    interactions = [
        make_interaction(bot, command=command, user=admin, place=make_place(place))
        for place in (STAFF_CHANNEL_ID, BOT_CHANNEL_ID)
    ]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for interaction in interactions:
            assert not await bot.tree.interaction_check(interaction)

    for interaction in interactions:
        assert answer(interaction) == UNCHECKED_COMMAND_TEXT
    assert UNCHECKED_COMMAND_TEXT == "This command isn't available."
    # Once, however often it is used.
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        f'Application command {command.qualified_name} has no prefix command, so '
        'the access rules cannot check it, and it is refused'
    ]


async def test_a_command_the_tree_does_not_know_goes_on_to_fail_there(
    bot: AccessBot,
) -> None:
    # discord.py then raises CommandNotFound, which on_error answers.
    interaction = make_interaction(bot, command=None, user=make_member())

    assert await bot.tree.interaction_check(interaction)
    unanswered(interaction)


@pytest.mark.parametrize('autocomplete', [False, True], ids=['command', 'autocomplete'])
async def test_without_an_access_service_the_tree_checks_nothing(
    plain_bot: AccessBot, autocomplete: bool
) -> None:
    interaction = make_interaction(plain_bot, autocomplete=autocomplete, guild_id=None)

    assert await plain_bot.tree.interaction_check(interaction)
    unanswered(interaction)


async def test_a_refusal_that_cannot_be_sent_is_logged(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = make_interaction(bot, guild_id=None)
    interaction.response.send_message.side_effect = discord.NotFound(
        MagicMock(status=404, reason='Not Found'), 'Unknown interaction'
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert not await bot.tree.interaction_check(interaction)

    assert [record.levelno for record in caplog.records if record.name == LOGGER] == [
        logging.WARNING
    ]


async def test_an_expired_interaction_gets_no_answer(bot: AccessBot) -> None:
    interaction = make_interaction(bot, guild_id=None)
    interaction.is_expired.return_value = True

    assert not await bot.tree.interaction_check(interaction)
    unanswered(interaction)


# The autocomplete gate


async def test_an_admin_gets_suggestions_and_a_developer_none(
    bot: AccessBot,
) -> None:
    # Through discord.py's own tree: the gate comes before the suggestions.
    admin = unqueue_typed(bot, make_member('Admin'))
    developer = unqueue_typed(bot, make_member(DEVELOPER_ROLE_ID))

    await bot.tree._call(admin)
    await bot.tree._call(developer)

    admin.response.autocomplete.assert_awaited_once_with(SUGGESTIONS)
    unanswered(developer)
    assert weekly(bot).suggested == 1


@pytest.mark.parametrize(
    ('channel_id', 'suggested'),
    [(BOT_CHANNEL_ID, True), (STAFF_CHANNEL_ID, True), (GENERAL_ID, False)],
    ids=['bot channel', 'staff channel', 'elsewhere'],
)
async def test_suggestions_only_where_the_command_works(
    bot: AccessBot, channel_id: int, suggested: bool
) -> None:
    # /challenge works in bot channels alone.
    interaction = make_interaction(
        bot,
        autocomplete=True,
        place=make_place(channel_id),
        command=bot.tree.get_command('challenge'),
    )

    assert await bot.tree.interaction_check(interaction) is suggested
    unanswered(interaction)


async def test_a_thread_suggests_as_the_channel_it_is_in(bot: AccessBot) -> None:
    thread = MagicMock(spec=discord.Thread, id=1, parent_id=BOT_CHANNEL_ID)
    interaction = make_interaction(
        bot, autocomplete=True, place=thread, command=bot.tree.get_command('challenge')
    )

    assert await bot.tree.interaction_check(interaction)


async def test_no_suggestions_for_a_switched_off_command(bot: AccessBot) -> None:
    await bot.access.change(
        GUILD_ID, lambda access: access.with_limit('challenge', Limit(off=True))
    )
    interaction = make_interaction(
        bot,
        autocomplete=True,
        place=make_place(BOT_CHANNEL_ID),
        command=bot.tree.get_command('challenge'),
    )

    assert not await bot.tree.interaction_check(interaction)


async def plain(interaction: discord.Interaction, text: str) -> None:
    """A slash command alone, which no prefix command wraps."""


@pytest.mark.parametrize(
    'command',
    [None, app_commands.Command(name='plain', description='Plain', callback=plain)],
    ids=['unknown command', 'not a hybrid command'],
)
async def test_no_suggestions_for_a_command_the_rules_do_not_know(
    bot: AccessBot, command: Any
) -> None:
    interaction = make_interaction(
        bot,
        autocomplete=True,
        user=make_member('Admin'),
        place=make_place(STAFF_CHANNEL_ID),
        command=command,
    )

    assert not await bot.tree.interaction_check(interaction)


async def test_no_suggestions_for_a_user_who_is_not_a_member(bot: AccessBot) -> None:
    interaction = make_interaction(
        bot,
        autocomplete=True,
        user=MagicMock(spec=discord.User, id=MEMBER_ID),
        place=make_place(BOT_CHANNEL_ID),
        command=bot.tree.get_command('challenge'),
    )

    assert not await bot.tree.interaction_check(interaction)


async def test_a_gate_that_fails_gives_no_suggestions(
    bot: AccessBot, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        bot.access, 'decide', AsyncMock(side_effect=RuntimeError('no settings'))
    )
    interaction = make_interaction(
        bot,
        autocomplete=True,
        place=make_place(BOT_CHANNEL_ID),
        command=bot.tree.get_command('challenge'),
    )

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        assert not await bot.tree.interaction_check(interaction)

    (record,) = [record for record in caplog.records if record.name == LOGGER]
    assert record.getMessage() == 'Could not check the autocomplete of challenge'


# Errors


def tree_errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == TREE_LOGGER and record.levelno == logging.ERROR
    ]


@pytest.mark.parametrize(
    ('error', 'text'),
    [
        (app_commands.CommandNotFound('gone', []), COMMAND_CHANGED_TEXT),
        (app_commands.AppCommandError('boom'), UNEXPECTED_ERROR_MESSAGE),
    ],
    ids=['unknown command', 'other error'],
)
async def test_an_unanswered_command_that_failed_is_answered_privately(
    bot: AccessBot,
    error: app_commands.AppCommandError,
    text: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = make_interaction(bot)

    with caplog.at_level(logging.ERROR, logger=TREE_LOGGER):
        await bot.tree.on_error(interaction, error)

    assert answer(interaction) == text
    # discord.py's own log of the error.
    (record,) = tree_errors(caplog)
    assert record.exc_info is not None and record.exc_info[1] is error
    assert COMMAND_CHANGED_TEXT == 'This command has changed. Try again in a minute.'


async def test_a_command_whose_options_changed_is_answered_like_an_unknown_one(
    bot: AccessBot,
) -> None:
    command = bot.tree.get_command('ping')
    assert isinstance(command, app_commands.Command)
    interaction = make_interaction(bot, command=command)

    await bot.tree.on_error(interaction, app_commands.CommandSignatureMismatch(command))

    assert answer(interaction) == COMMAND_CHANGED_TEXT


async def test_a_command_the_bot_no_longer_has_is_answered_through_the_tree(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    # As discord.py runs the tree for each interaction (CommandTree._from_interaction).
    interaction = make_interaction(bot, data={'type': 1, 'name': 'gone', 'options': []})

    with caplog.at_level(logging.ERROR, logger=TREE_LOGGER):
        with pytest.raises(app_commands.CommandNotFound) as raised:
            await bot.tree._call(interaction)
        await bot.tree._dispatch_error(interaction, raised.value)

    assert answer(interaction) == COMMAND_CHANGED_TEXT
    assert len(tree_errors(caplog)) == 1


@pytest.mark.parametrize(
    'case', ['answered', 'autocomplete', 'expired'], ids=lambda case: case
)
async def test_no_second_answer_and_none_to_autocomplete(
    bot: AccessBot, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = make_interaction(bot, autocomplete=case == 'autocomplete')
    interaction.response.done = case == 'answered'
    interaction.is_expired.return_value = case == 'expired'
    error = app_commands.AppCommandError('boom')

    with caplog.at_level(logging.ERROR, logger=TREE_LOGGER):
        await bot.tree.on_error(interaction, error)

    unanswered(interaction)
    # Logged all the same.
    assert len(tree_errors(caplog)) == 1


async def test_an_answer_to_an_error_that_cannot_be_sent_is_logged(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = make_interaction(bot)
    interaction.response.send_message.side_effect = discord.HTTPException(
        MagicMock(status=500, reason='Server Error'), 'oops'
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await bot.tree.on_error(interaction, app_commands.AppCommandError('boom'))

    messages = [
        record.getMessage() for record in caplog.records if record.name == LOGGER
    ]
    assert len(messages) == 1
    assert messages[0].startswith(
        f'Could not answer an interaction ({UNEXPECTED_ERROR_MESSAGE})'
    )
    assert cast(AsyncMock, interaction.response.send_message).await_count == 1
