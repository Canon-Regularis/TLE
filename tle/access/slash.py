"""Which slash commands members see in their slash list.

Discord shows every member each slash command the bot syncs, unless the member
lacks a permission that the top-level command needs: its default permissions,
which Discord ignores on subcommands. So, after every cog has loaded and
before the commands are synced, ``apply_visibility`` goes through each
top-level slash command by the rules of the commands in it:

- a command or group whose commands are all for the bot owner is removed,
  since the owner uses the prefix forms, and so is a group without any slash
  command, which Discord can't run;
- one that the code already gives default permissions keeps them, with all
  its commands;
- one whose commands are all for staff (``STAFF_LEVELS``) needs a permission
  to be seen: Manage Messages if they are all for moderators, and Manage
  Server otherwise. All its commands stay, the bot owner's too;
- from any other group, the commands for staff are removed, and then the
  subgroups left empty.

Only the slash list changes: every prefix command stays, and the access rules
still decide who may use what. A group's own rule counts through its slash
fallback, such as /clist show, which runs the group.
"""

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tle.access import table
from tle.access.rules import FAIL_CLOSED, STAFF_LEVELS, Rule, Who

logger = logging.getLogger(__name__)

# A slash command or group, as the command tree holds them.
_Slash = app_commands.Command[Any, ..., Any] | app_commands.Group


def default_rule(name: str) -> Rule:
    """Command ``name``'s default rule from the table, or ``FAIL_CLOSED``."""
    return table.rule_for(name) or FAIL_CLOSED


@dataclass
class Visibility:
    """What ``apply_visibility`` changed in the slash list.

    ``removed`` holds the qualified names of the slash commands and groups it
    removed, in the order it removed them; a top-level command removed whole
    is listed without the commands in it. ``hidden`` maps each top-level
    command that members need a permission to see, whether the code or
    ``apply_visibility`` gave it that permission, to the permission's name,
    such as 'manage_guild'.
    """

    removed: list[str] = field(default_factory=list)
    hidden: dict[str, str] = field(default_factory=dict)


def apply_visibility(
    bot: commands.Bot, rule_of: Callable[[str], Rule] = default_rule
) -> Visibility:
    """Remove and hide ``bot``'s slash commands by their rules, as above.

    ``rule_of`` gives a command's rule by its qualified name as a prefix
    command; by default, the table's rule. Only the global slash commands
    change, which are the ones the bot syncs. Run it after every cog has
    loaded and before the commands are synced; a second run changes nothing
    more.

    A slash command taken out of a group keeps its ``parent``, and a hybrid
    command its ``app_command``: only the tree tells whether a slash form is
    still there.
    """
    visibility = Visibility()
    tree = bot.tree
    for command in tree.get_commands(type=discord.AppCommandType.chat_input):
        levels = {
            slash.qualified_name: rule_of(_rule_name(slash)).who
            for slash in _commands_in(command)
        }
        # True too of a group without any slash command, which Discord can't run.
        if all(level is Who.OWNER for level in levels.values()):
            tree.remove_command(command.name)
            visibility.removed.append(command.qualified_name)
        elif command.default_permissions is not None:
            visibility.hidden[command.name] = _names(command.default_permissions)
        elif all(level in STAFF_LEVELS for level in levels.values()):
            if all(level is Who.MODERATOR for level in levels.values()):
                permissions = discord.Permissions(manage_messages=True)
            else:
                permissions = discord.Permissions(manage_guild=True)
            command.default_permissions = permissions
            visibility.hidden[command.name] = _names(permissions)
        elif isinstance(command, app_commands.Group):
            _remove_staff(command, levels, visibility.removed)
    _log(visibility)
    return visibility


def _commands_in(command: _Slash) -> Iterator[app_commands.Command[Any, ..., Any]]:
    """The slash commands in group ``command``, or ``command`` itself."""
    if isinstance(command, app_commands.Group):
        for child in command.walk_commands():
            if isinstance(child, app_commands.Command):
                yield child
    else:
        yield command


def _rule_name(command: app_commands.Command[Any, ..., Any]) -> str:
    """The name of slash command ``command``'s rule.

    A hybrid command's slash form has the rule of the prefix command it
    wraps; a group's fallback, such as /clist show, wraps the group, clist.
    Any other slash command has the rule of its own name.
    """
    wrapped = getattr(command, 'wrapped', None)
    if isinstance(wrapped, commands.Command):
        return wrapped.qualified_name
    return command.qualified_name


def _remove_staff(
    group: app_commands.Group, levels: Mapping[str, Who], removed: list[str]
) -> None:
    """Remove the commands for staff from ``group``, then its subgroups left empty.

    ``levels`` maps each slash command's qualified name to the level its rule
    requires. Only the slash group changes: a hybrid group keeps every prefix
    subcommand. A subgroup's default permissions don't count, as Discord
    ignores them.
    """
    for child in group.commands:
        if isinstance(child, app_commands.Group):
            _remove_staff(child, levels, removed)
            if child.commands:
                continue
        elif levels[child.qualified_name] not in STAFF_LEVELS:
            continue
        removed.append(child.qualified_name)
        group.remove_command(child.name)


def _names(permissions: discord.Permissions) -> str:
    """The permissions a member needs, such as 'manage_guild'.

    Default permissions with none set leave a command to administrators.
    """
    names = [name for name, value in permissions if value]
    return ' and '.join(names) or 'administrator'


def _log(visibility: Visibility) -> None:
    """Log what ``apply_visibility`` changed, if anything."""
    if visibility.hidden:
        logger.info(
            'Slash commands that members need a permission to see: %s',
            ', '.join(
                f'/{name} ({permission})'
                for name, permission in visibility.hidden.items()
            ),
        )
    if visibility.removed:
        logger.info(
            'Slash commands removed, leaving their prefix forms: %s',
            ', '.join(f'/{name}' for name in visibility.removed),
        )
