"""/kcpc: each server's KCPC feature settings, and KCPC's health, for admins.

Every admin command lives under ``/kcpc`` because Discord can only hide
top-level commands: the group asks Discord to show it only to members with
Manage Server. Each command also has a check of its own, which discord.py runs
on every invocation: ``kcpc_admin_only``, or ``kcpc_status_only`` for
``/kcpc status``, which TLE's developers may use too. Only the bot owner sees
KCPC's extensions and jobs in the status, since they concern every server the
bot is in, and only on ``/kcpc status``, whose answer is private: ``;kcpc
status`` answers the whole staff channel.
"""

import logging
from collections.abc import Mapping
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot.admin import permission_names, ping_role_problem
from tle.kcpc.bot.checks import kcpc_admin_only, kcpc_status_only
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import success_embed, to_embed
from tle.kcpc.bot.publisher import PostChannel, can_ping_role, missing_post_permissions
from tle.kcpc.core.errors import KcpcDisabledError, KcpcUserError
from tle.kcpc.core.ledger import DeliveryRecord, DeliveryStatus
from tle.kcpc.core.messages import EmbedField, OutgoingMessage
from tle.kcpc.core.migrations import schema_version
from tle.kcpc.core.scheduler import JobStatus
from tle.kcpc.core.settings import FeatureSettings, FeatureSpec
from tle.kcpc.core.timeutil import discord_timestamp

logger = logging.getLogger(__name__)

_MAX_CHOICES = 25  # the most autocomplete suggestions Discord shows
_RECENT_SKIPS = 5
_KCPC_MODULE_PREFIX = 'tle.kcpc.'
_FEATURE_DESCRIPTION = 'The KCPC feature, such as workshops: pick one as you type'

# `/kcpc role`'s optional role. It converts as a plain Role rather than as an
# Optional one, because a prefix command reads text that names no role as "no
# role" for an Optional parameter, which here would clear the setting.
_OPTIONAL_ROLE = commands.parameter(converter=discord.Role, default=None)

# Ledger statuses as /kcpc status shows them. A claimed post is waiting for
# confirmation that it reached Discord.
_STATUS_LABELS = (
    (DeliveryStatus.SENT, 'sent'),
    (DeliveryStatus.SKIPPED, 'skipped'),
    (DeliveryStatus.CLAIMED, 'pending'),
)

# How to let the bot notify a role it mentions, for replies about the role.
_MENTION_FIX = (
    'Allow anyone to mention it (Server Settings → Roles), or give me the '
    '"Mention @everyone, @here and All Roles" permission'
)

# What /kcpc status says in place of what only the bot owner sees: to others,
# and to the owner on ;kcpc status, which the staff channel sees.
OWNER_ONLY_NOTE = (
    'Extensions, jobs and their last errors are shown only to the bot owner.'
)
OWNER_PREFIX_NOTE = "Use /kcpc status to see KCPC's extensions and jobs privately."


class KcpcAdmin(KcpcCog):
    """Admin commands: show, status, channel, role, enable and disable.

    Each has its own check, which discord.py runs for prefix and slash
    invocations alike, so it holds even where an admin has shown /kcpc to
    other members.
    """

    async def feature_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the features whose key or title contains what was typed."""
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
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(  # type: ignore[arg-type]
        brief="Show this server's settings for every KCPC feature", fallback='show'
    )
    @app_commands.default_permissions(manage_guild=True)
    @kcpc_admin_only()
    async def kcpc(self, ctx: commands.Context[Any]) -> None:
        """Show this server's settings for every KCPC feature.

        Each feature shows whether it is on, its channel and its ping role, with
        a warning for each setup problem.

        Examples:
            /kcpc show
            ;kcpc
        """
        guild = _guild(ctx)
        services = self.services
        stored = await services.guild_settings.all_for_guild(guild.id)
        fields = tuple(
            EmbedField(
                f'{spec.title} (`{spec.key}`)',
                _describe_feature(spec, stored[spec.key], guild),
            )
            for spec in services.features.all()
        )
        message = OutgoingMessage(
            title='KCPC settings',
            description='Change them with `/kcpc channel`, `/kcpc role`, '
            '`/kcpc enable` and `/kcpc disable`.',
            fields=fields,
        )
        await _reply(ctx, to_embed(message))

    @kcpc.command(brief='Show KCPC health: post counts and skipped posts')  # type: ignore[arg-type]
    @kcpc_status_only()
    async def status(self, ctx: commands.Context[Any]) -> None:
        """Show how KCPC is doing in this server.

        You see this server's post counts and latest skipped posts, the
        database's schema version and the time zone. The bot owner also sees
        KCPC's extensions and jobs, with their last errors, on /kcpc status,
        whose answer only they see.

        Examples:
            /kcpc status
        """
        guild = _guild(ctx)
        services = self.services
        version = await schema_version(services.db)
        counts = await services.ledger.status_counts(guild.id)
        skips = await services.ledger.recent_skips(guild.id, limit=_RECENT_SKIPS)
        owner = await _is_owner(self.bot, ctx.author)
        # Only a slash command's answer is private: the staff channel sees the
        # answer to ;kcpc status, and the internals concern every server.
        internals = owner and ctx.interaction is not None
        description: str | None = None
        if not internals:
            description = OWNER_PREFIX_NOTE if owner else OWNER_ONLY_NOTE
        fields = (
            EmbedField('Database', f'Schema version {version}'),
            EmbedField('Time zone', services.settings.kcpc_timezone, inline=True),
            *(self._internals() if internals else ()),
            EmbedField('Posts in this server', _describe_counts(counts)),
            EmbedField(
                'Recently skipped posts',
                '\n'.join(_describe_skip(record) for record in skips) or 'none',
            ),
        )
        message = OutgoingMessage(
            title='KCPC status', description=description, fields=fields
        )
        await _reply(ctx, to_embed(message))

    def _internals(self) -> tuple[EmbedField, ...]:
        """KCPC's extensions and jobs, with their last errors, for the bot owner."""
        extensions = sorted(
            name for name in self.bot.extensions if name.startswith(_KCPC_MODULE_PREFIX)
        )
        return (
            EmbedField(
                'Extensions',
                ', '.join(f'`{name}`' for name in extensions) or 'none',
                inline=True,
            ),
            *(
                EmbedField(f'Job `{job.name}`', _describe_job(job))
                for job in self.services.scheduler.status()
            ),
        )

    @kcpc.command(brief='Set the channel a feature posts in')  # type: ignore[arg-type]
    @app_commands.describe(
        feature=_FEATURE_DESCRIPTION, channel='The channel its posts go to'
    )
    @app_commands.autocomplete(feature=feature_autocomplete)
    @kcpc_admin_only()
    async def channel(
        self, ctx: commands.Context[Any], feature: str, channel: discord.TextChannel
    ) -> None:
        """Set the channel a feature posts in, once the bot can post there.

        The reply warns if posts there would not notify the feature's ping role.

        Examples:
            /kcpc channel workshops #workshops
        """
        guild = _guild(ctx)
        spec = self._feature(feature)
        missing = missing_post_permissions(channel)
        if missing:
            raise KcpcUserError(
                f"I can't post in {channel.mention}. Give me these permissions "
                f'there, then try again: {permission_names(missing)}.'
            )
        settings = await self.services.guild_settings.update(
            guild.id, spec.key, channel_id=channel.id
        )
        change = f'{spec.title} posts go to {channel.mention}.'
        await _reply(ctx, _updated(spec, settings, change, guild))

    @kcpc.command(brief="Set or clear the role a feature's posts mention")  # type: ignore[arg-type]
    @app_commands.describe(
        feature=_FEATURE_DESCRIPTION,
        role='A role just for pings, for its posts to mention; none if left out',
    )
    @app_commands.autocomplete(feature=feature_autocomplete)
    @kcpc_admin_only()
    async def role(
        self,
        ctx: commands.Context[Any],
        feature: str,
        role: discord.Role | None = _OPTIONAL_ROLE,
    ) -> None:
        """Set the role a feature's posts mention; without a role, they mention none.

        It must be a role just for pings, as every member can give it to
        themselves with /notify. The bot must be able to notify it: in the
        feature's channel once that is set, or server-wide until then.

        Examples:
            /kcpc role workshops role:@Workshops
            /kcpc role workshops
        """
        guild = _guild(ctx)
        spec = self._feature(feature)
        guild_settings = self.services.guild_settings
        if role is not None:
            current = await guild_settings.get(guild.id, spec.key)
            _check_mentionable(role, guild, _post_channel(guild, current.channel_id))
            _check_pings_only(role)
        settings = await guild_settings.update(
            guild.id, spec.key, role_id=None if role is None else role.id
        )
        if role is None:
            change = f'{spec.title} posts mention no role.'
        else:
            change = (
                f'{spec.title} posts mention {role.mention}. Members get or drop '
                f'it with `/notify {spec.key} on|off`.'
            )
        await _reply(ctx, _updated(spec, settings, change, guild))

    @kcpc.command(brief='Turn a feature on')  # type: ignore[arg-type]
    @app_commands.describe(feature=_FEATURE_DESCRIPTION)
    @app_commands.autocomplete(feature=feature_autocomplete)
    @kcpc_admin_only()
    async def enable(self, ctx: commands.Context[Any], feature: str) -> None:
        """Turn a feature on. It posts once its channel is set too.

        Examples:
            /kcpc enable workshops
        """
        guild = _guild(ctx)
        spec = self._feature(feature)
        settings = await self.services.guild_settings.update(
            guild.id, spec.key, enabled=True
        )
        await _reply(ctx, _updated(spec, settings, f'{spec.title} is on.', guild))

    @kcpc.command(brief='Turn a feature off')  # type: ignore[arg-type]
    @app_commands.describe(feature=_FEATURE_DESCRIPTION)
    @app_commands.autocomplete(feature=feature_autocomplete)
    @kcpc_admin_only()
    async def disable(self, ctx: commands.Context[Any], feature: str) -> None:
        """Turn a feature off. Its settings are kept.

        Examples:
            /kcpc disable workshops
        """
        guild = _guild(ctx)
        spec = self._feature(feature)
        settings = await self.services.guild_settings.update(
            guild.id, spec.key, enabled=False
        )
        await _reply(ctx, _updated(spec, settings, f'{spec.title} is off.', guild))

    def _feature(self, key: str) -> FeatureSpec:
        """The feature called ``key``; ``KcpcUserError`` if there is none."""
        return self.services.features.get(key.strip().lower())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcAdmin(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    # The commands' checks admit server members only, so there is always a
    # guild.
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


async def _is_owner(bot: commands.Bot, user: discord.abc.User) -> bool:
    """Whether ``user`` owns the bot; False, with a warning, if Discord can't say.

    discord.py asks Discord who owns the bot when it doesn't know yet.
    """
    try:
        return await bot.is_owner(user)
    except discord.HTTPException as exc:
        logger.warning('Could not tell whether user %s owns the bot: %s', user.id, exc)
        return False


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _updated(
    spec: FeatureSpec, settings: FeatureSettings, change: str, guild: discord.Guild
) -> discord.Embed:
    """The reply to a settings change: what changed, then the new settings."""
    return success_embed(f'{change}\n\n{_describe_feature(spec, settings, guild)}')


def _describe_feature(
    spec: FeatureSpec, settings: FeatureSettings, guild: discord.Guild
) -> str:
    """A feature's settings in ``guild``, with a warning for each setup problem."""
    channel = 'not set' if settings.channel_id is None else f'<#{settings.channel_id}>'
    role = 'none' if settings.role_id is None else f'<@&{settings.role_id}>'
    lines = [
        spec.description,
        f'Status: {"enabled" if settings.enabled else "disabled"}',
        f'Channel: {channel}',
        f'Role: {role}',
    ]
    if settings.enabled and settings.channel_id is None:
        lines.append(
            '**Warning:** nothing is posted until a channel is set with '
            f'`/kcpc channel {spec.key} #channel`.'
        )
    unnotified = _unnotified_role_warning(settings, guild)
    if unnotified is not None:
        lines.append(unnotified)
    return '\n'.join(lines)


def _unnotified_role_warning(
    settings: FeatureSettings, guild: discord.Guild
) -> str | None:
    """A warning if posts in the feature's channel can't notify its role.

    Posts there still mention the role, but Discord notifies nobody; the
    publisher reports this too, when it posts.
    """
    channel = _post_channel(guild, settings.channel_id)
    role = None if settings.role_id is None else guild.get_role(settings.role_id)
    if channel is None or role is None or can_ping_role(channel, role):
        return None
    return (
        f"**Warning:** posts in {channel.mention} won't notify {role.mention}. "
        f'{_MENTION_FIX} there.'
    )


def _post_channel(guild: discord.Guild, channel_id: int | None) -> PostChannel | None:
    """The guild's channel ``channel_id``, if it exists and can take posts."""
    channel = None if channel_id is None else guild.get_channel_or_thread(channel_id)
    return channel if isinstance(channel, PostChannel) else None


def _check_mentionable(
    role: discord.Role, guild: discord.Guild, channel: PostChannel | None
) -> None:
    """Raise ``KcpcUserError`` unless posts can notify ``role``.

    ``channel`` is where the feature posts, if it is set and still exists.
    Discord notifies a mentioned role that is mentionable, or when the bot may
    mention everyone in that channel, after its permission overwrites
    (``can_ping_role``). Without a channel the bot's server-wide permission
    stands in, and ``/kcpc channel`` warns if the channel chosen later can't
    notify the role.
    """
    if role.is_default():
        raise KcpcUserError(
            'Posts cannot mention @everyone. Choose a role that members join.'
        )
    if channel is not None:
        if not can_ping_role(channel, role):
            raise KcpcUserError(
                f"I can't mention {role.mention} in {channel.mention}. "
                f'{_MENTION_FIX} there.'
            )
    elif not role.mentionable and not guild.me.guild_permissions.mention_everyone:
        raise KcpcUserError(f"I can't mention {role.mention}. {_MENTION_FIX}.")


def _check_pings_only(role: discord.Role) -> None:
    """Raise ``KcpcUserError`` unless ``role`` is just for pings.

    Every member can give themselves a feature's ping role with /notify, so
    any other role would let them change what they can do (see
    ``ping_role_problem``).
    """
    problem = ping_role_problem(role)
    if problem is not None:
        raise KcpcUserError(
            f"{role.mention} can't be a ping role: it isn't just for pings "
            f"({problem}), and every member can give themselves a feature's "
            'ping role with /notify. Choose a pings-only role.'
        )


def _describe_job(job: JobStatus) -> str:
    if job.running:
        next_run = 'running now'
    elif job.next_run is None:
        next_run = 'not scheduled'
    else:
        next_run = discord_timestamp(job.next_run, 'R')
    last_slot = (
        'never' if job.last_slot is None else discord_timestamp(job.last_slot, 'R')
    )
    lines = [
        job.description,
        f'Next run: {next_run}',
        f'Last slot: {last_slot}',
        f'Failures: {job.failures}',
    ]
    if job.last_error is not None:
        lines.append(f'Last error: {_code(job.last_error)}')
    return '\n'.join(lines)


def _describe_counts(counts: Mapping[DeliveryStatus, int]) -> str:
    return ' · '.join(f'{label}: {counts[status]}' for status, label in _STATUS_LABELS)


def _describe_skip(record: DeliveryRecord) -> str:
    reason = record.reason or 'no reason given'
    return (
        f'{discord_timestamp(record.claimed_at, "R")} · {record.feature} · '
        f'{_code(reason)} · {_code(record.key)}'
    )


def _code(text: str) -> str:
    """``text`` as inline code; backticks would end it early, so they go."""
    return '`' + text.replace('`', "'") + '`'
