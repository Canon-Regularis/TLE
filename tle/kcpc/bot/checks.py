"""Who may manage KCPC: checks for admin commands and components.

discord.py runs a command's own checks, never those of the groups it is in:
``;kcpc status`` and ``/kcpc status`` run the checks of status alone, not
those of /kcpc. So every admin command carries its own check:
``kcpc_admin_only()``, or ``kcpc_status_only()`` for /kcpc status, which TLE's
developers may use too.
"""

from collections.abc import Callable
from typing import Any, TypeVar

import discord
from discord.ext import commands

from tle import constants

T = TypeVar('T')


class NotKcpcAdmin(commands.CheckFailure):
    """The user may not manage KCPC (see ``is_kcpc_admin``).

    TLE's ``bot_error_handler`` answers a command's failed check with a short
    refusal of its own: members see that, not this message. Neither names a
    role.
    """

    def __init__(self) -> None:
        super().__init__('Only server admins can do that.')


class NotKcpcDeveloper(commands.CheckFailure):
    """The user may not see how KCPC is doing (see ``is_kcpc_developer``).

    Answered like ``NotKcpcAdmin``.
    """

    def __init__(self) -> None:
        super().__init__('Only server admins and developers can do that.')


def is_kcpc_admin(user: discord.abc.User) -> bool:
    """Whether ``user`` is a server member with Manage Server or TLE's admin role.

    The admin role (``constants.TLE_ADMIN``, a name or an id) is read at call
    time. Users outside a server, e.g. in DMs, are never admins.
    """
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild or _has_role(user, constants.TLE_ADMIN)


def is_kcpc_developer(user: discord.abc.User) -> bool:
    """Whether ``user`` is a KCPC admin or a server member with TLE's developer role.

    The developer role (``constants.TLE_DEVELOPER``, an id) is read at call
    time. When it is None the bot has no developer role, and only admins pass.
    """
    if is_kcpc_admin(user):
        return True
    developer = constants.TLE_DEVELOPER
    return (
        developer is not None
        and isinstance(user, discord.Member)
        and _has_role(user, developer)
    )


def _has_role(member: discord.Member, role: str | int) -> bool:
    """Whether ``member`` has a role, given by id or by name.

    The same rule as TLE's ``discord_common.has_role``, copied for the reason
    given in ``tle.kcpc.bot.embeds``: the server's default role, whose id is
    the server's, never counts, as every member has it.
    """
    everyone = member.guild.id
    if isinstance(role, int):
        return any(
            member_role.id != everyone and member_role.id == role
            for member_role in member.roles
        )
    return any(
        member_role.id != everyone and member_role.name == role
        for member_role in member.roles
    )


async def ensure_kcpc_admin(ctx: commands.Context[Any]) -> bool:
    """A command check passing KCPC admins and raising ``NotKcpcAdmin`` otherwise.

    discord.py runs a hybrid command's checks for both prefix and slash
    invocations.
    """
    if is_kcpc_admin(ctx.author):
        return True
    raise NotKcpcAdmin()


async def ensure_kcpc_developer(ctx: commands.Context[Any]) -> bool:
    """A command check passing KCPC admins and TLE's developers, and raising
    ``NotKcpcDeveloper`` otherwise.
    """
    if is_kcpc_developer(ctx.author):
        return True
    raise NotKcpcDeveloper()


def kcpc_admin_only() -> Callable[[T], T]:
    """A decorator restricting a command to KCPC admins."""
    return commands.check(ensure_kcpc_admin)


def kcpc_status_only() -> Callable[[T], T]:
    """A decorator restricting a command to KCPC admins and TLE's developers.

    For /kcpc status, which shows how KCPC is doing.
    """
    return commands.check(ensure_kcpc_developer)
