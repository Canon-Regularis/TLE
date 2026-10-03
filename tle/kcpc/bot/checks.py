"""Who may manage KCPC: checks for admin commands and components."""

from collections.abc import Callable
from typing import Any, TypeVar

import discord
from discord.ext import commands

from tle import constants

T = TypeVar('T')


class NotKcpcAdmin(commands.CheckFailure):
    """The user may not manage KCPC (see ``is_kcpc_admin``).

    It is a ``CheckFailure``, so TLE's ``bot_error_handler`` shows its message.
    """

    def __init__(self) -> None:
        super().__init__(
            'You need the Manage Server permission or the '
            f'{_role_label(constants.TLE_ADMIN)} role to do that.'
        )


def _role_label(role: str | int) -> str:
    # A role id is shown as a mention, which Discord renders as the role's name.
    return f'<@&{role}>' if isinstance(role, int) else role


def is_kcpc_admin(user: discord.abc.User) -> bool:
    """Whether ``user`` is a server member with Manage Server or TLE's admin role.

    The admin role (``constants.TLE_ADMIN``, a name or an id) is read at call
    time. Users outside a server, e.g. in DMs, are never admins.
    """
    if not isinstance(user, discord.Member):
        return False
    return user.guild_permissions.manage_guild or _has_role(user, constants.TLE_ADMIN)


def _has_role(member: discord.Member, role: str | int) -> bool:
    """Whether ``member`` has a role, given by id or by name.

    The same rule as TLE's ``discord_common.has_role``, copied for the reason
    given in ``tle.kcpc.bot.embeds``.
    """
    if isinstance(role, int):
        return any(member_role.id == role for member_role in member.roles)
    return any(member_role.name == role for member_role in member.roles)


async def ensure_kcpc_admin(ctx: commands.Context[Any]) -> bool:
    """A command check passing KCPC admins and raising ``NotKcpcAdmin`` otherwise.

    Suits ``cog_check``, which hybrid commands run for both prefix and slash
    invocations.
    """
    if is_kcpc_admin(ctx.author):
        return True
    raise NotKcpcAdmin()


def kcpc_admin_only() -> Callable[[T], T]:
    """A decorator restricting a command to KCPC admins."""
    return commands.check(ensure_kcpc_admin)
