"""Feature admin commands under /kcpc, and the roles admins may choose to ping.

Every admin command lives under /kcpc, the group that the kcpc.admin extension
adds and that Discord shows only to members with Manage Server: Discord can
hide only top-level commands. A feature declares its admin commands as a
hybrid group on its own cog, such as /kcpc workshops, and attaches it in
``cog_load``::

    if not attach_admin_group(self.bot, self.admin_group):
        withhold_admin_group(self, self.admin_group)

detaching it again in ``cog_unload``. This relies on how discord.py adds a cog
(``Cog._inject``): it runs ``cog_load`` first, and then registers only the
cog's commands that have no parent. An attached group has one, so it appears
under /kcpc and nowhere else. A group that could not be attached is withheld,
so that it doesn't become a top-level command shown to every member.

Each attached command needs ``kcpc_admin_only()``, as the admin cog's own
commands have it: discord.py never runs the checks of the groups a command is
in, and the command belongs to the feature's cog, not the admin cog.

A feature's posts mention the ping role an admin chose for it, and /notify
lets every member give themselves that role or take it away.
``ping_role_problem`` says why a role must not be used that way.
"""

import logging
from collections.abc import Iterable
from typing import Any

import discord
from discord.ext import commands

from tle import constants

logger = logging.getLogger(__name__)

ADMIN_GROUP_NAME = 'kcpc'
# Why a role that is one of TLE's isn't just for pings, in a reason members
# see: it doesn't say which of TLE's roles it is, as TLE's own self-service
# role commands don't.
TLE_ROLE_FOR_MEMBERS = 'the bot uses it to decide what members may do'


def attach_admin_group(
    bot: commands.Bot, group: commands.HybridGroup[Any, ..., Any]
) -> bool:
    """Add ``group`` under /kcpc, as a prefix and a slash subcommand group.

    Returns False, logging why, if the bot has no /kcpc because the kcpc.admin
    extension isn't loaded. If /kcpc already has a command with the group's
    name, raises discord.py's error and changes nothing.
    """
    parent = bot.get_command(ADMIN_GROUP_NAME)
    if not isinstance(parent, commands.HybridGroup):
        logger.info(
            'Not adding /%s %s: the kcpc.admin extension is not loaded',
            ADMIN_GROUP_NAME,
            group.name,
        )
        return False
    if group.name in parent.all_commands:
        # Checked here because discord.py nests the slash group before it
        # finds the name taken by a prefix command, and leaves it nested.
        raise commands.CommandRegistrationError(group.name)
    parent.add_command(group)
    return True


def detach_admin_group(
    bot: commands.Bot, group: commands.HybridGroup[Any, ..., Any]
) -> None:
    """Take ``group`` out of /kcpc again, on both paths. Idempotent.

    The group leaves the parent it was attached to even if that is no longer
    the bot's /kcpc: when the bot closes, discord.py removes the admin cog
    first.
    """
    parent = group.parent
    if (
        isinstance(parent, commands.HybridGroup)
        and parent.all_commands.get(group.name) is group
    ):
        parent.remove_command(group.name)  # the prefix and the slash command
    group.parent = None
    if group.app_command:
        group.app_command.parent = None


def withhold_admin_group(
    cog: commands.Cog, group: commands.HybridGroup[Any, ..., Any]
) -> None:
    """Keep discord.py from registering ``cog``'s ``group``, which isn't attached.

    For ``cog_load``, when ``attach_admin_group`` returns False. discord.py
    would otherwise register the group as a top-level command, outside /kcpc.
    It registers the commands listed in the cog's ``__cog_commands__`` once
    ``cog_load`` returns, so the group and its subcommands are taken out of
    that list. The cog's other commands are unaffected.
    """
    cog.__cog_commands__ = [
        command
        for command in cog.__cog_commands__
        if command is not group and command.root_parent is not group
    ]


def ping_role_problem(role: discord.Role, *, for_members: bool = False) -> str | None:
    """Why members must not give themselves ``role``, or None if it's just for pings.

    A role just for pings is none of TLE's roles (``tle.constants``, read at
    call time), grants no permission that @everyone lacks, and changes no
    permissions in any channel. Having any other role changes what a member
    can do, as a server's member or verified role does. The reason reads like
    "it is TLE's admin role", for admins. With ``for_members``, for a reason
    that members see, it doesn't say which of TLE's roles the role is
    (``TLE_ROLE_FOR_MEMBERS``).
    """
    for purpose, configured in _tle_roles():
        if _is_role(role, configured):
            if for_members:
                return TLE_ROLE_FOR_MEMBERS
            return f"it is TLE's {purpose} role"
    everyone = role.guild.default_role.permissions
    extra = discord.Permissions(role.permissions.value & ~everyone.value)
    granted = [name for name, value in extra if value]
    if granted:
        return f'it grants {permission_names(granted)}'
    for channel in role.guild.channels:
        if not channel.overwrites_for(role).is_empty():
            return f'it changes what members can do in {channel.mention}'
    return None


def permission_names(names: Iterable[str]) -> str:
    """Permissions as Discord names them: ``['embed_links']`` as 'Embed Links'."""
    return ', '.join(name.replace('_', ' ').title() for name in names)


def _tle_roles() -> tuple[tuple[str, str | int], ...]:
    """What each of TLE's roles is for, and its name or id.

    The developer role, an id, is there only when the bot has one.
    """
    roles: list[tuple[str, str | int]] = [
        ('admin', constants.TLE_ADMIN),
        ('moderator', constants.TLE_MODERATOR),
        ('trusted', constants.TLE_TRUSTED),
        ('purgatory', constants.TLE_PURGATORY),
    ]
    developer = constants.TLE_DEVELOPER
    if developer is not None:
        roles.append(('developer', developer))
    return tuple(roles)


def _is_role(role: discord.Role, configured: str | int) -> bool:
    """Whether ``role`` is the one configured by id, or else by name.

    The same rule as TLE's ``discord_common.has_role``.
    """
    if isinstance(configured, int):
        return role.id == configured
    return role.name == configured
