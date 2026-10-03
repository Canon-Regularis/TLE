"""Linking members to Codeforces handles: TLE's handle table and rank roles.

TLE's ``;handle set`` and ``;handle identify`` (through ``tle.util.oauth``) and
KCPC's ``/link codeforces`` (through ``tle.kcpc.bot.codeforces_links``) all link
handles here, so a linked member gets the same table entry and roles whichever
way they linked.

The functions take a ``log`` to warn on, so that TLE's ``Handles`` cog keeps
logging through its own logger.
"""

import datetime as dt
import logging

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_api as cf, db, discord_common

logger = logging.getLogger(__name__)


class HandleLinkError(commands.CommandError):
    """A handle could not be linked, for a reason to show the user.

    It is raised before anything is written, so the member is left as they were.
    """


class HandleTakenError(HandleLinkError):
    """Another member of the guild has the handle already."""


def rank_role_for(guild: discord.Guild, user: cf.User) -> discord.Role | None:
    """The role for ``user``'s Codeforces rank in ``guild``, or None if unrated.

    Raises ``HandleLinkError`` if the guild has no role named after the rank.
    """
    return role_for_rank(guild, user.rank)


def role_for_rank(guild: discord.Guild, rank: cf.Rank) -> discord.Role | None:
    """The role for the Codeforces ``rank`` in ``guild``, or None if unrated.

    Raises ``HandleLinkError`` if the guild has no role named after the rank.
    """
    if rank == cf.UNRATED_RANK:
        return None
    role = discord.utils.get(guild.roles, name=rank.title)
    if role is None:
        raise HandleLinkError(f'Role for rank `{rank.title}` not present in the server')
    return role


async def link_handle(
    user_db: db.UserDbConn,
    guild: discord.Guild,
    member: discord.Member,
    user: cf.User,
    *,
    reason: str = 'New handle set for user',
    log: logging.Logger = logger,
) -> None:
    """Link ``member`` to the Codeforces account ``user`` in ``guild``.

    The member's handle becomes ``user.handle``, replacing any other, the
    account is cached, and the member gets the role for its rank in place of
    any other rank role (see ``update_member_rank_role``). The audit log shows
    ``reason`` for the role changes.

    The role is found before anything is written, so a guild without it leaves
    the member unlinked rather than linked without a role. Raises
    ``HandleLinkError`` for that, and ``HandleTakenError`` if another member of
    the guild has the handle.
    """
    role_to_assign = rank_role_for(guild, user)
    try:
        await user_db.set_handle(member.id, guild.id, user.handle)
    except db.UniqueConstraintFailed as exc:
        raise HandleTakenError(
            f'The handle `{user.handle}` is already associated with another user.'
        ) from exc
    await user_db.cache_cf_user(user)
    await update_member_rank_role(
        member, role_to_assign, reason=reason, user_db=user_db, log=log
    )


async def update_member_rank_role(
    member: discord.Member,
    role_to_assign: discord.Role | None,
    *,
    reason: str,
    user_db: db.UserDbConn,
    log: logging.Logger = logger,
) -> None:
    """Sets the `member` to only have the rank role of `role_to_assign`.

    All other rank roles on the member, if any, will be removed. If
    `role_to_assign` is None all existing rank roles on the member will be
    removed. A rank of Candidate Master or above also takes the member out of
    Purgatory, and may make them Trusted (see `maybe_add_trusted_role`).
    """
    role_names_to_remove = {rank.title for rank in cf.RATED_RANKS}
    should_remove_purgatory = False
    if role_to_assign is not None:
        role_names_to_remove.discard(role_to_assign.name)
        if role_to_assign.name not in ['Newbie', 'Pupil', 'Specialist', 'Expert']:
            should_remove_purgatory = True
            await maybe_add_trusted_role(member, user_db=user_db, log=log)
    to_remove = [role for role in member.roles if role.name in role_names_to_remove]
    if should_remove_purgatory and discord_common.has_role(
        member, constants.TLE_PURGATORY
    ):
        purg_role = discord_common.get_role(member.guild, constants.TLE_PURGATORY)
        if purg_role:
            to_remove.append(purg_role)
    if to_remove:
        await member.remove_roles(*to_remove, reason=reason)
    if role_to_assign is not None and role_to_assign not in member.roles:
        await member.add_roles(role_to_assign, reason=reason)


async def maybe_add_trusted_role(
    member: discord.Member,
    *,
    user_db: db.UserDbConn,
    log: logging.Logger = logger,
) -> None:
    """Add trusted role for eligible users.

    Condition: `member` has been 1900+ for any amount of time before o1 release.
    """
    handle = await user_db.get_handle(member.id, member.guild.id)
    if not handle:
        log.warning(
            f'WARN: handle not found in guild {member.guild.name} ({member.guild.id})'
        )
        return
    trusted_role = discord.utils.get(member.guild.roles, name=constants.TLE_TRUSTED)
    if not trusted_role:
        log.warning(
            "WARN: 'Trusted' role not found in guild"
            f' {member.guild.name} ({member.guild.id})'
        )
        return

    if trusted_role not in member.roles:
        # o1 released sept 12 2024
        cutoff_timestamp = dt.datetime(2024, 9, 11, tzinfo=dt.timezone.utc).timestamp()
        try:
            rating_changes = await cf.user.rating(handle=handle)
        except cf.HandleNotFoundError:
            # User rating info not found via API, ignore for trusted check
            log.info(
                f'INFO: Rating history not found for handle {handle}'
                ' during trusted check.'
            )
            return
        except cf.CodeforcesApiError as e:
            log.warning(
                f'WARN: API Error fetching rating for {handle}'
                f' during trusted check: {e}'
            )
            return

        if any(
            change.newRating >= 1900
            and change.ratingUpdateTimeSeconds < cutoff_timestamp
            for change in rating_changes
        ):
            try:
                await member.add_roles(
                    trusted_role, reason='Historical rating >= 1900 before Aug 2024'
                )
            except discord.Forbidden:
                log.warning(
                    f'WARN: Missing permissions to add Trusted role to'
                    f' {member.display_name} in {member.guild.name}'
                )
            except discord.HTTPException as e:
                log.warning(
                    f'WARN: Failed to add Trusted role to'
                    f' {member.display_name} in {member.guild.name}: {e}'
                )
