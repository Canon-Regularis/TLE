"""Tests for tle.kcpc.bot.admin: feature admin groups attached under /kcpc.

They run on a real ``commands.Bot`` with the real admin cog, so they also pin
what the module relies on in discord.py: ``Cog._inject`` runs ``cog_load``
before it registers the cog's commands, and registers only those without a
parent; ``HybridGroup.add_command`` nests a hybrid group's slash group too; and
``Cog._eject`` calls ``cog_unload`` while the group is still attached.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.kcpc.bot.admin import (
    ADMIN_GROUP_NAME,
    attach_admin_group,
    detach_admin_group,
    withhold_admin_group,
)
from tle.kcpc.bot.checks import NotKcpcAdmin, kcpc_admin_only
from tle.kcpc.features.admin.cog import KcpcAdmin, setup as add_admin_cog

ADMIN_LOGGER = 'tle.kcpc.bot.admin'
# The admin cog's own subcommands of /kcpc. Slash commands also have `show`,
# the fallback that runs the group itself, as ;kcpc does.
KCPC_COMMANDS = {'channel', 'disable', 'enable', 'role', 'status'}
KCPC_SLASH_COMMANDS = KCPC_COMMANDS | {'show'}
GUILD_ID = 1_100_000_000_000_000_001


class Feature(commands.Cog):
    """A feature with admin commands for /kcpc, and a command for members.

    Its cog_load and cog_unload do what the module docstring prescribes, and
    note what discord.py had done when they ran.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.attached: bool | None = None
        self.registered_at_load: list[str] | None = None
        self.parent_at_unload: object = None

    async def cog_load(self) -> None:
        self.registered_at_load = sorted(self.bot.all_commands)
        self.attached = attach_admin_group(self.bot, self.gadgets)
        if not self.attached:
            withhold_admin_group(self, self.gadgets)

    async def cog_unload(self) -> None:
        self.parent_at_unload = self.gadgets.parent
        detach_admin_group(self.bot, self.gadgets)

    @commands.hybrid_group(brief='Gadget settings')  # type: ignore[arg-type]
    async def gadgets(self, ctx: commands.Context[Any]) -> None:
        pass

    @gadgets.command(brief='Reset the gadgets')  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def reset(self, ctx: commands.Context[Any]) -> None:
        pass

    # Without kcpc_admin_only, which test_the_admin_cogs_check_does_not_apply
    # shows is needed.
    @gadgets.command(brief='Look at the gadgets')  # type: ignore[arg-type]
    async def peek(self, ctx: commands.Context[Any]) -> None:
        pass

    @commands.hybrid_command(brief='Say hello')  # type: ignore[arg-type]
    async def hello(self, ctx: commands.Context[Any]) -> None:
        pass


@pytest.fixture
async def bare_bot() -> AsyncIterator[commands.Bot]:
    """A bot without the admin cog."""
    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    yield bot
    await bot.close()


@pytest.fixture
async def bot(bare_bot: commands.Bot) -> commands.Bot:
    """A bot with the admin cog, added as its extension adds it."""
    await add_admin_cog(bare_bot)
    return bare_bot


@pytest.fixture
async def feature(bot: commands.Bot) -> Feature:
    feature = Feature(bot)
    await bot.add_cog(feature)
    return feature


def kcpc_group(bot: commands.Bot) -> commands.HybridGroup[Any, ..., Any]:
    group = bot.get_command(ADMIN_GROUP_NAME)
    assert isinstance(group, commands.HybridGroup)
    return group


def kcpc_slash_group(bot: commands.Bot) -> app_commands.Group:
    group = bot.tree.get_command(ADMIN_GROUP_NAME)
    assert isinstance(group, app_commands.Group)
    return group


def prefix_names(bot: commands.Bot) -> set[str]:
    return set(bot.all_commands)


def slash_names(bot: commands.Bot) -> set[str]:
    return {command.name for command in bot.tree.get_commands()}


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


def make_member(*, manage_guild: bool) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    return member


def make_context(
    bot: commands.Bot, author: MagicMock, *, slash: bool
) -> commands.Context[commands.Bot]:
    """A real context in a guild; with ``slash``, of a slash command."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


async def test_cog_load_runs_before_the_cogs_commands_are_registered(
    bot: commands.Bot, feature: Feature
) -> None:
    assert feature.registered_at_load == ['help', 'kcpc']
    assert prefix_names(bot) == {'help', 'kcpc', 'hello'}


async def test_an_attached_group_is_under_kcpc_for_prefix_commands(
    bot: commands.Bot, feature: Feature
) -> None:
    kcpc = kcpc_group(bot)
    assert feature.attached is True
    assert bot.get_command('kcpc gadgets') is feature.gadgets
    assert feature.gadgets.parent is kcpc
    reset = command_named(bot, 'kcpc gadgets reset')
    assert reset is feature.reset
    assert reset.qualified_name == 'kcpc gadgets reset'
    # The admin cog's own subcommands are still there.
    assert {command.name for command in kcpc.commands} == KCPC_COMMANDS | {'gadgets'}


async def test_an_attached_group_is_under_kcpc_for_slash_commands(
    bot: commands.Bot, feature: Feature
) -> None:
    kcpc = kcpc_slash_group(bot)
    gadgets = kcpc.get_command('gadgets')
    assert gadgets is feature.gadgets.app_command
    assert isinstance(gadgets, app_commands.Group)
    assert gadgets.parent is kcpc
    assert gadgets.qualified_name == 'kcpc gadgets'
    assert sorted(command.name for command in gadgets.commands) == ['peek', 'reset']
    assert sorted(command.name for command in kcpc.commands) == sorted(
        KCPC_SLASH_COMMANDS | {'gadgets'}
    )


async def test_discord_is_sent_the_group_inside_kcpc(
    bot: commands.Bot, feature: Feature
) -> None:
    # What tree.sync sends: a subcommand group (type 2) of /kcpc, whose own
    # commands are subcommands (type 1). Only /kcpc says who may see it.
    payload = kcpc_slash_group(bot).to_dict(bot.tree)

    assert payload['default_member_permissions'] == (
        discord.Permissions(manage_guild=True).value
    )
    (gadgets,) = [
        option for option in payload['options'] if option['name'] == 'gadgets'
    ]
    assert gadgets['type'] == discord.AppCommandOptionType.subcommand_group.value
    assert 'default_member_permissions' not in gadgets
    assert [(option['name'], option['type']) for option in gadgets['options']] == [
        ('reset', discord.AppCommandOptionType.subcommand.value),
        ('peek', discord.AppCommandOptionType.subcommand.value),
    ]


async def test_an_attached_group_is_not_registered_at_top_level(
    bot: commands.Bot, feature: Feature
) -> None:
    assert bot.get_command('gadgets') is None
    assert bot.tree.get_command('gadgets') is None
    assert prefix_names(bot) == {'help', 'kcpc', 'hello'}
    assert slash_names(bot) == {'kcpc', 'hello'}


async def test_attached_commands_belong_to_the_feature_cog(
    bot: commands.Bot, feature: Feature
) -> None:
    reset = command_named(bot, 'kcpc gadgets reset')
    assert reset.cog is feature
    slash_reset = feature.gadgets.app_command.get_command('reset')
    assert isinstance(slash_reset, app_commands.Command)
    assert slash_reset.binding is feature


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_non_admins_are_refused_attached_admin_commands(
    monkeypatch: pytest.MonkeyPatch, bot: commands.Bot, feature: Feature, slash: bool
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    reset = command_named(bot, 'kcpc gadgets reset')
    admin = make_member(manage_guild=True)

    assert await reset.can_run(make_context(bot, admin, slash=slash))
    with pytest.raises(NotKcpcAdmin):
        await reset.can_run(
            make_context(bot, make_member(manage_guild=False), slash=slash)
        )


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_the_admin_cogs_check_does_not_apply_to_attached_commands(
    monkeypatch: pytest.MonkeyPatch, bot: commands.Bot, feature: Feature, slash: bool
) -> None:
    # The admin cog's cog_check guards only its own commands, so every
    # attached command needs kcpc_admin_only; peek lacks it.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    member = make_context(bot, make_member(manage_guild=False), slash=slash)

    assert await command_named(bot, 'kcpc gadgets peek').can_run(member)
    with pytest.raises(NotKcpcAdmin):
        await command_named(bot, 'kcpc status').can_run(member)


async def test_removing_the_cog_detaches_the_group(
    bot: commands.Bot, feature: Feature
) -> None:
    kcpc = kcpc_group(bot)

    await bot.remove_cog('Feature')

    # discord.py calls cog_unload after removing the top-level commands, while
    # the group is still attached.
    assert feature.parent_at_unload is kcpc
    assert bot.get_command('kcpc gadgets') is None
    assert kcpc_slash_group(bot).get_command('gadgets') is None
    assert feature.gadgets.parent is None
    assert feature.gadgets.app_command.parent is None
    assert {command.name for command in kcpc.commands} == KCPC_COMMANDS
    assert prefix_names(bot) == {'help', 'kcpc'}
    assert slash_names(bot) == {'kcpc'}


async def test_detaching_is_idempotent(bot: commands.Bot, feature: Feature) -> None:
    detach_admin_group(bot, feature.gadgets)
    detach_admin_group(bot, feature.gadgets)

    assert bot.get_command('kcpc gadgets') is None
    assert feature.gadgets.parent is None
    assert sorted(command.name for command in kcpc_slash_group(bot).commands) == (
        sorted(KCPC_SLASH_COMMANDS)
    )


async def test_a_feature_can_be_added_again_after_being_removed(
    bot: commands.Bot, feature: Feature
) -> None:
    await bot.remove_cog('Feature')
    again = Feature(bot)

    await bot.add_cog(again)

    assert again.attached is True
    assert bot.get_command('kcpc gadgets reset') is again.reset
    assert kcpc_slash_group(bot).get_command('gadgets') is again.gadgets.app_command


async def test_the_group_leaves_kcpc_even_after_the_admin_cog_has_gone(
    bot: commands.Bot, feature: Feature
) -> None:
    # As when the bot closes: discord.py removes the admin cog first.
    kcpc = kcpc_group(bot)

    await bot.remove_cog('KcpcAdmin')
    await bot.remove_cog('Feature')

    assert kcpc.get_command('gadgets') is None
    assert kcpc.app_command.get_command('gadgets') is None
    assert feature.gadgets.parent is None
    assert feature.gadgets.app_command.parent is None


async def test_attaching_without_the_admin_cog_returns_false(
    bare_bot: commands.Bot, caplog: pytest.LogCaptureFixture
) -> None:
    feature = Feature(bare_bot)

    with caplog.at_level(logging.INFO, logger=ADMIN_LOGGER):
        assert attach_admin_group(bare_bot, feature.gadgets) is False

    assert feature.gadgets.parent is None
    (record,) = [r for r in caplog.records if r.name == ADMIN_LOGGER]
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        'Not adding /kcpc gadgets: the kcpc.admin extension is not loaded'
    )


async def test_a_group_withheld_without_the_admin_cog_is_not_registered(
    bare_bot: commands.Bot,
) -> None:
    feature = Feature(bare_bot)

    await bare_bot.add_cog(feature)

    assert feature.attached is False
    # Not a top-level command that every member would be shown, on either path.
    assert prefix_names(bare_bot) == {'help', 'hello'}
    assert slash_names(bare_bot) == {'hello'}
    assert [command.name for command in feature.get_commands()] == ['hello']
    assert feature.gadgets.parent is None

    await bare_bot.remove_cog('Feature')

    assert prefix_names(bare_bot) == {'help'}
    assert slash_names(bare_bot) == set()


async def other_gadgets(interaction: discord.Interaction) -> None:
    """Another slash command called gadgets."""


async def other_gadgets_command(ctx: commands.Context[Any]) -> None:
    """Another command called gadgets."""


@pytest.mark.parametrize(
    'taken_on', ['both paths', 'the prefix path', 'the slash path']
)
async def test_attaching_a_group_whose_name_is_taken_raises_and_changes_nothing(
    bot: commands.Bot, taken_on: str
) -> None:
    kcpc, kcpc_slash = kcpc_group(bot), kcpc_slash_group(bot)
    error: type[Exception]
    if taken_on == 'the slash path':
        kcpc_slash.add_command(
            app_commands.Command(
                name='gadgets', description='Other gadgets', callback=other_gadgets
            )
        )
        error = app_commands.CommandAlreadyRegistered
    else:
        with_app_command = taken_on == 'both paths'
        kcpc.add_command(
            commands.hybrid_command(name='gadgets', with_app_command=with_app_command)(
                other_gadgets_command
            )
        )
        error = commands.CommandRegistrationError
    prefix_commands = dict(kcpc.all_commands)
    slash_commands = list(kcpc_slash.commands)
    gadgets: commands.HybridGroup[Any, ..., Any] = Feature(bot).gadgets

    with pytest.raises(error):
        attach_admin_group(bot, gadgets)

    assert kcpc.all_commands == prefix_commands
    assert kcpc_slash.commands == slash_commands
    assert gadgets.parent is None
    assert gadgets.app_command.parent is None


async def test_attaching_the_same_group_twice_raises(
    bot: commands.Bot, feature: Feature
) -> None:
    with pytest.raises(commands.CommandRegistrationError):
        attach_admin_group(bot, feature.gadgets)

    assert bot.get_command('kcpc gadgets') is feature.gadgets
    assert kcpc_slash_group(bot).get_command('gadgets') is feature.gadgets.app_command


async def test_the_admin_cog_is_the_one_its_extension_adds(bot: commands.Bot) -> None:
    # The name attach_admin_group looks for is the admin cog's group.
    admin = bot.get_cog('KcpcAdmin')
    assert isinstance(admin, KcpcAdmin)
    assert bot.get_command(ADMIN_GROUP_NAME) is admin.kcpc
