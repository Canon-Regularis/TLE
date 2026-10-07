"""The booted bot's commands, against the access rules and the pages of /help.

A command without a rule is for the bot owner alone, in the staff channel, so
every command members can meet, prefix or slash, must have one. Every rule
must name a command, each twin must be the other form of the command it
shares a rule with, and every cog with commands must have a page of /help.
The bot boots as ``booting.booted`` boots it, with every extension, so these
are all of TLE's and KCPC's commands, with /help and /access.
"""

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tests.kcpc.component.booting import booted
from tle.__main__ import TLEBot
from tle.access.table import (
    CATEGORIES,
    COG_CATEGORY,
    RULES,
    TWINS,
    category_of,
    rule_for,
)

# Discord's types of the options that are commands in a group.
SUBCOMMAND, SUBCOMMAND_GROUP = 1, 2
Command = commands.Command[Any, ..., Any]


@pytest.fixture
async def bot(tmp_path: Path) -> AsyncIterator[TLEBot]:
    """The bot with every extension."""
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        yield bot


def registered(bot: commands.Bot) -> dict[str, Command]:
    """Every prefix command, groups and subcommands, by qualified name."""
    return {command.qualified_name: command for command in bot.walk_commands()}


def slash_paths(bot: commands.Bot) -> Iterator[str]:
    """The path of every slash command in the payload that the sync sends."""
    for command in bot.tree.get_commands():
        yield from _leaves(command.to_dict(bot.tree))


def _leaves(entry: dict[str, Any], prefix: str = '') -> Iterator[str]:
    path = f'{prefix}{entry["name"]}'
    inner = [
        option
        for option in entry.get('options', [])
        if option['type'] in (SUBCOMMAND, SUBCOMMAND_GROUP)
    ]
    if not inner:
        yield path
    for option in inner:
        yield from _leaves(option, f'{path} ')


def tree_command(
    bot: commands.Bot, path: str
) -> app_commands.Command[Any, ..., Any] | None:
    """The slash command at ``path`` in the bot's tree."""
    names = path.split(' ')
    found = bot.tree.get_command(names[0])
    for name in names[1:]:
        if not isinstance(found, app_commands.Group):
            return None
        found = found.get_command(name)
    return found if isinstance(found, app_commands.Command) else None


def slash_form(command: Command) -> Any:
    """A hybrid command's own slash form: None for a prefix command alone."""
    return getattr(command, 'app_command', None)


async def test_the_bot_has_tle_s_kcpc_s_and_the_access_commands(bot: TLEBot) -> None:
    def origin(command: Command) -> str:
        module = command.module or ''
        if module.startswith('tle.kcpc.'):
            return 'kcpc'
        return 'access' if module.startswith('tle.access.') else 'tle'

    found = Counter(origin(command) for command in registered(bot).values())

    assert found == {'tle': 100, 'kcpc': 50, 'access': 8}


async def test_every_command_has_a_rule(bot: TLEBot) -> None:
    unruled = [name for name in registered(bot) if rule_for(name) is None]

    assert unruled == []


async def test_every_slash_command_runs_a_command_with_a_rule(bot: TLEBot) -> None:
    paths = list(slash_paths(bot))

    for path in paths:
        command = tree_command(bot, path)
        assert command is not None, path
        # The bot's check runs only for commands that a slash command wraps;
        # the command tree refuses any other.
        wrapped = getattr(command, 'wrapped', None)
        assert isinstance(wrapped, commands.Command), path
        assert rule_for(wrapped.qualified_name) is not None, path
    assert len(paths) == 107
    # Nor are there context menus, which no prefix command wraps.
    for kind in (discord.AppCommandType.user, discord.AppCommandType.message):
        assert bot.tree.get_commands(type=kind) == []


async def test_every_rule_names_a_command(bot: TLEBot) -> None:
    names = set(registered(bot))

    assert sorted(set(RULES) - names) == []
    # The twins share the rules of the commands they are twins of.
    assert set(RULES) == names - set(TWINS)
    assert len(RULES) == 154


async def test_each_twin_is_the_other_form_of_the_command_it_shares_a_rule_with(
    bot: TLEBot,
) -> None:
    names = registered(bot)
    prefix_fallbacks = {
        name: command.parent.qualified_name
        for name, command in names.items()
        if isinstance(command.parent, commands.HybridGroup)
        and command.parent.fallback == command.name
        and slash_form(command) is None
    }

    # A prefix subcommand named after its group's slash fallback, which runs
    # the group's own command: ;contests upcoming does what /contests
    # upcoming does.
    assert prefix_fallbacks == {
        'contests upcoming': 'contests',
        'weekly current': 'weekly',
        'algo current': 'algo',
    }
    # The handle group has no fallback: its own command, ;handle, shows a
    # member's handles, as /handle show does.
    handle = names['handle']
    assert isinstance(handle, commands.HybridGroup) and handle.fallback is None
    assert names['handle show'].parent is handle
    assert slash_form(names['handle show']) is not None
    assert dict(TWINS) == {**prefix_fallbacks, 'handle': 'handle show'}
    # So a twin's slash form is the one of the command it is a twin of.
    for twin, name in TWINS.items():
        path = bot.access.slash_path(names[name])
        assert path is not None, name
        assert bot.access.slash_path(names[twin]) == path, twin


async def test_every_cog_with_commands_has_a_page_of_help(bot: TLEBot) -> None:
    commands_ = registered(bot)

    assert {command.cog_name for command in commands_.values()} == set(COG_CATEGORY)
    # And every page has commands.
    pages = {
        category_of(name, command.cog_name).key for name, command in commands_.items()
    }
    assert pages == {category.key for category in CATEGORIES}
