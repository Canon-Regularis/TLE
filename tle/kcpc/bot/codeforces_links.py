"""Members' Codeforces handles, which KCPC shares with TLE.

TLE keeps each member's Codeforces handle in its user database, and its own
features (gitgud, duels, rank roles) read them from there. KCPC reads and links
handles only through this module: it is the one KCPC module that may use TLE's
``tle.util.handle_linking`` and ``tle.util.codeforces_api``. A handle linked
here is linked for TLE too, rank role included, as with ``;handle set``.
"""

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from tle.kcpc.core.errors import ExternalServiceError, KcpcDisabledError, KcpcUserError
from tle.util import codeforces_api as cf, handle_linking

logger = logging.getLogger(__name__)

# Shown in the server's audit log for the rank role changes.
_LINK_REASON = 'Codeforces handle verified with /link'
_NOT_RESPONDING = 'Codeforces is not responding right now. Please try again later.'


class RankRoleRefused(KcpcUserError):
    """The handle was linked, but Discord refused to change the rank roles."""


async def linked_handle(bot: commands.Bot, guild_id: int, user_id: int) -> str | None:
    """The member's Codeforces handle, as TLE has it, or None."""
    handle: str | None = await _user_db(bot).get_handle(user_id, guild_id)
    return handle


async def guild_handles(bot: commands.Bot, guild_id: int) -> list[tuple[int, str]]:
    """``(user_id, handle)`` for each active member of the guild with a handle.

    TLE marks the members who left the guild inactive.
    """
    handles: list[tuple[int, str]] = await _user_db(bot).get_handles_for_guild(guild_id)
    return handles


async def handle_holder(bot: commands.Bot, guild_id: int, handle: str) -> int | None:
    """The id of whoever has ``handle`` in the guild in TLE's table, or None.

    It ignores case, as Codeforces does, and counts members who left the guild:
    TLE keeps their handles, marked inactive, and won't link those to anyone else.
    """
    user_id: int | None = await _user_db(bot).get_user_id(handle.strip(), guild_id)
    return user_id


def check_rank_role(guild: discord.Guild, rating: int | None) -> None:
    """Check that the guild has the role for a Codeforces rating's rank.

    It is the check ``link`` makes before linking anything, for a caller that
    knows the rating already. Raises ``KcpcUserError`` if the role is missing.
    An unrated account (``rating`` None) needs no role.
    """
    try:
        handle_linking.role_for_rank(guild, cf.rating2rank(rating))
    except handle_linking.HandleLinkError as exc:
        raise KcpcUserError(str(exc)) from exc


async def link(
    bot: commands.Bot, guild: discord.Guild, member: discord.Member, handle: str
) -> None:
    """Link ``member`` to the Codeforces account ``handle``, for TLE and KCPC alike.

    It does what TLE's ``;handle set`` does: the account's handle, in its
    canonical case, replaces any the member had, and the member gets the role
    for its rank. A caller that must not replace a handle checks
    ``linked_handle`` first.

    Raises ``KcpcUserError`` if Codeforces has no such user, if another member
    has the handle, or if the server has no role for the account's rank; then
    nothing is changed. Raises ``ExternalServiceError`` if Codeforces fails.
    Raises ``RankRoleRefused`` if Discord refuses to change the member's roles,
    which comes after the handle is linked: the handle stays linked.
    """
    user_db = _user_db(bot)
    user = await _fetch_user(handle)
    try:
        await handle_linking.link_handle(
            user_db, guild, member, user, reason=_LINK_REASON
        )
    except handle_linking.HandleLinkError as exc:
        raise KcpcUserError(str(exc)) from exc
    except discord.Forbidden as exc:
        # Only the role change, after the handle is stored, asks Discord for
        # anything (the Trusted role's own refusal is logged and skipped). Not
        # a warning: members could repeat it into the log channel.
        logger.info(
            'Discord refused to change the rank roles of member %d in guild %d: %s',
            member.id,
            guild.id,
            exc.text,
        )
        raise RankRoleRefused(
            'Your Codeforces account '
            f'{discord.utils.escape_markdown(user.handle)} is linked, but Discord '
            "didn't let me change your rank roles. Ask an admin to check that I "
            'have the Manage Roles permission and that my highest role is above '
            'the rank roles.'
        ) from exc


async def _fetch_user(handle: str) -> cf.User:
    try:
        (user,) = await cf.user.info(handles=[handle])
    except cf.HandleNotFoundError:
        raise KcpcUserError(f'No Codeforces user called {handle}.') from None
    # TLE's client lets its session's timeout through uncaught.
    except (cf.CodeforcesApiError, asyncio.TimeoutError) as exc:
        raise ExternalServiceError('Codeforces', _NOT_RESPONDING) from exc
    return user


def _user_db(bot: commands.Bot) -> Any:
    """TLE's user database, which TLE attaches to the bot before KCPC starts.

    It is typed loosely because its class lives in ``tle.util.db``, which KCPC
    may not import.
    """
    user_db = getattr(bot, 'user_db', None)
    if user_db is None:
        raise KcpcDisabledError()
    return user_db
