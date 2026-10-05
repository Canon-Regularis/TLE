"""/notify <feature> on|off: members choose which features' posts ping them.

A feature's posts mention its ping role, which admins set with ``/kcpc role``,
so the members who have that role get pinged. ``/notify`` gives a member the
role or takes it away. For that the bot needs the Manage Roles permission, and
the role must sit below the bot's highest role. The role must also be just for
pings (see ``ping_role_problem``), since every member may have it.
"""

import logging
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot.admin import ping_role_problem
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import info_embed, success_embed
from tle.kcpc.core.errors import KcpcDisabledError, KcpcUserError
from tle.kcpc.core.settings import FeatureSpec

logger = logging.getLogger(__name__)

_MAX_CHOICES = 25  # the most autocomplete suggestions Discord shows
# Longer than any feature key, and short enough that a refusal repeating it
# fits in a reply.
_MAX_FEATURE_LENGTH = 32

NO_ROLE_MESSAGE = (
    'This feature has no ping role. Ask an admin to set one with /kcpc role.'
)


class KcpcNotify(KcpcCog):
    """``/notify``: members turn a feature's pings on or off for themselves."""

    async def feature_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the features whose key or title contains what was typed.

        Only the feature registry is read, in memory: Discord asks again on
        every keystroke.
        """
        try:
            specs = self.services.features.all()
        except KcpcDisabledError:
            return []
        typed = current.strip().lower()
        return [
            app_commands.Choice(name=f'{spec.title} ({spec.key})', value=spec.key)
            for spec in specs
            if typed in spec.key or typed in spec.title.lower()
        ][:_MAX_CHOICES]

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects the callback; hence the type: ignore.
    @commands.hybrid_command(brief="Turn a feature's pings on or off for yourself")  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(
        feature='The feature whose posts should ping you, e.g. workshops',
        state='on to get pinged, off to stop',
    )
    @app_commands.autocomplete(feature=feature_autocomplete)
    async def notify(
        self,
        ctx: commands.Context[Any],
        # Discord refuses a longer slash option; a prefix command raises
        # RangeError, which TLE's error handler shows.
        feature: commands.Range[str, 1, _MAX_FEATURE_LENGTH],
        state: Literal['on', 'off'],
    ) -> None:
        """Turn a feature's pings on or off for yourself, e.g. /notify workshops on.

        A feature's posts ping the members who have its role, so this gives you
        the role or takes it away.
        """
        # Changing a member's roles is a request to Discord, which can be rate
        # limited past the few seconds a slash command has to answer.
        await ctx.defer(ephemeral=True)
        member = _member(ctx)
        spec = self.services.features.get(feature.strip().lower())
        role = await self._ping_role(member.guild, spec)
        wanted = state == 'on'
        if (role in member.roles) == wanted:
            # Nothing to do, whether or not the bot could manage the role.
            await _reply(ctx, info_embed(description=_unchanged(spec, role, wanted)))
            return
        _check_manageable(role, member.guild.me)
        await _set_role(
            member, role, wanted, reason=f'Member used /notify {spec.key} {state}'
        )
        await _reply(ctx, success_embed(_changed(spec, role, wanted)))

    async def _ping_role(self, guild: discord.Guild, spec: FeatureSpec) -> discord.Role:
        """The feature's ping role in ``guild``; ``KcpcUserError`` if it has none."""
        settings = await self.services.guild_settings.get(guild.id, spec.key)
        role = None if settings.role_id is None else guild.get_role(settings.role_id)
        if role is None:
            # Also when the role was deleted after an admin set it.
            raise KcpcUserError(NO_ROLE_MESSAGE)
        return role


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcNotify(bot))


def _member(ctx: commands.Context[Any]) -> discord.Member:
    # guild_only admits commands from servers only, where authors are members.
    if not isinstance(ctx.author, discord.Member):
        raise commands.NoPrivateMessage()
    return ctx.author


def _check_manageable(role: discord.Role, me: discord.Member) -> None:
    """Raise ``KcpcUserError`` unless members may get ``role`` and drop it.

    The bot must be able to give the role and take it away, and the role must
    be just for pings. The message says what an admin must fix. A managed
    role (such as a bot's own, or the booster role) and one that isn't just
    for pings are reported first: only choosing another role fixes those.
    Admins may have chosen the role before it got permissions, so it is
    checked on every change.
    """
    cannot = f"I can't change who has {role.mention}"
    if role.managed:
        raise KcpcUserError(
            f'{cannot}: Discord or an integration manages it. Ask an admin to '
            'choose another ping role with /kcpc role.'
        )
    problem = ping_role_problem(role)
    if problem is not None:
        raise KcpcUserError(
            f"{cannot}: it isn't just for pings ({problem}), and /notify would "
            'let any member give it to themselves or take it away. Ask an admin '
            'to choose a role just for pings with /kcpc role.'
        )
    if not me.guild_permissions.manage_roles:
        raise KcpcUserError(
            f'{cannot}: I need the Manage Roles permission. Ask an admin to give '
            'it to me.'
        )
    if not role < me.top_role:
        raise KcpcUserError(
            f'{cannot}: my highest role must be above it. Ask an admin to move my '
            'role above it in Server Settings → Roles.'
        )


async def _set_role(
    member: discord.Member, role: discord.Role, wanted: bool, *, reason: str
) -> None:
    """Give ``member`` the role, or take it away; ``KcpcUserError`` if refused."""
    try:
        if wanted:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
    except discord.Forbidden as exc:
        # The checks passed on the bot's cached view of the server, which can
        # lag behind a change an admin just made. Not a warning: members could
        # repeat it into the log channel.
        logger.info(
            'Discord refused to change role %d of member %d in guild %d: %s',
            role.id,
            member.id,
            member.guild.id,
            exc.text,
        )
        raise KcpcUserError(
            "Discord didn't let me change your roles. Ask an admin to check that "
            'I have the Manage Roles permission and that my highest role is above '
            f'{role.mention}.'
        ) from exc


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the member sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _changed(spec: FeatureSpec, role: discord.Role, on: bool) -> str:
    if on:
        return (
            f"Done! You have {role.mention} now, so you'll get {spec.title} pings. "
            f'To stop them, use `/notify {spec.key} off`.'
        )
    return (
        f"Done. You don't have {role.mention} any more, so you won't get "
        f'{spec.title} pings. To get them again, use `/notify {spec.key} on`.'
    )


def _unchanged(spec: FeatureSpec, role: discord.Role, on: bool) -> str:
    if on:
        return f'You already get {spec.title} pings: you have {role.mention}.'
    return f"You don't get {spec.title} pings: you don't have {role.mention}."
