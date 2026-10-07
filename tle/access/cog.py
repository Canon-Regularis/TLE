"""/access: a server's bot channels, staff channel and command limits.

Member commands answer publicly in a server's bot channels, and staff
commands work in its staff channel (``tle.access.rules`` has the details).
The server's admins choose both here, and can tighten any command's rule in
the server with a limit, which never loosens it. Discord shows /access only to
members with Manage Server, the access rules let in the server's admins alone,
and every slash answer is private. Until the server has a staff channel, or
once it is gone, /access works in any channel, so that an admin can set one.

Admins name a command as they type it: ``duel register``, or the slash
fallback that runs a group's own command, such as ``clist show``. A limit on
a group covers its subcommands too, unless the admin says otherwise; a limit
on a fallback, or on the prefix twin of one, covers the group's own command
alone, unless the admin asks for the subcommands.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Optional, Union, cast

import discord
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.access.policy import (
    describe_limit,
    describe_where,
    describe_who,
    effective_for,
)
from tle.access.rules import (
    LIMIT_WHERE,
    LIMIT_WHO,
    Effective,
    Limit,
    Where,
    Who,
    satisfies,
)
from tle.access.service import AccessService, SettingsNeedRepair
from tle.access.settings import MAX_BOT_CHANNELS, GuildAccess
from tle.access.table import canonical, is_protected, limit_keys, rule_for
from tle.util import db, discord_common, paginator

logger = logging.getLogger(__name__)

# The channels that can be bot channels or the staff channel: those where
# members use commands. A thread counts as the channel it is in, so a forum's
# posts count as the forum.
CommandChannel = Union[
    discord.TextChannel,
    discord.VoiceChannel,
    discord.StageChannel,
    discord.ForumChannel,
]

# /access staff-channel's optional channel. It converts as a plain channel
# rather than an Optional one, because a prefix command reads text that names
# no channel as "no channel" for an Optional parameter, which here would clear
# the staff channel.
_OPTIONAL_CHANNEL = commands.parameter(converter=CommandChannel, default=None)

if TYPE_CHECKING:
    WhoName = str
    WhereName = str
else:
    # What a limit's who and where take, as admins type them.
    WhoName = Literal[tuple(who.value for who in LIMIT_WHO)]
    WhereName = Literal[tuple(where.value for where in LIMIT_WHERE)]

ALL = 'all'  # what /access reset takes to clear every limit
_MAX_CHOICES = 25  # the most autocomplete suggestions Discord shows
_PAGES_WAIT_TIME = 5 * 60  # how long the pages of /access show can be turned
_SHOWN = 50  # the most characters of what an admin typed that a reply repeats
_LISTED = 10  # the most commands a warning names
# What admins may put around a command's name.
_QUOTES = '"\'`'

# Discord's limits on an embed.
_FIELD_LIMIT = 1024
_DESCRIPTION_LIMIT = 4096
_EMBED_LIMIT = 6000

# The settings in .env that give TLE's roles, and the role each gives. The
# developer role is given by id alone.
_ROLE_SETTINGS = (
    ('TLE_ADMIN', 'admin'),
    ('TLE_MODERATOR', 'moderator'),
    ('TLE_TRUSTED', 'trusted'),
    ('TLE_DEVELOPER', 'developer'),
)
# The name of every server's default role, whose ID is the server's.
_EVERYONE_NAME = '@everyone'

# The replies. They never name a role or an id: only the settings in .env.
NO_BOT_CHANNEL_WARNING = (
    '**Warning:** there is no bot channel yet, so {where}member commands '
    "don't work with `;`, and slash commands answer only the person who uses "
    'them. Add one with `/access bot-channels add`.'
)
OUTSIDE_STAFF_CHANNEL = 'outside the staff channel, '
NO_STAFF_CHANNEL_WARNING = (
    "**Warning:** there is no staff channel yet, so staff commands don't work "
    'with `;`, and a few not at all. Set one with `/access staff-channel`.'
)
STAFF_CHANNEL_GONE_WARNING = (
    '**Warning:** the staff channel no longer exists. Set another with '
    '`/access staff-channel`.'
)
PUBLIC_STAFF_CHANNEL_WARNING = (
    '**Warning:** everyone can read {channel}, so anyone can see the answers of '
    'staff commands there. Hide it from @everyone.'
)
BOT_CHANNEL_GONE_WARNING = (
    '**Warning:** one of the bot channels no longer exists. Adding or removing '
    'a bot channel tidies it away.'
)
BOT_CHANNELS_GONE_WARNING = (
    '**Warning:** {count} of the bot channels no longer exist. Adding or '
    'removing a bot channel tidies them away.'
)
ROLE_NAME_MISSING_WARNING = (
    '**Warning:** no role here has the name that `{setting}` gives, so nobody '
    'here has the {role} role. In `.env`, set it to the ID of the role you want.'
)
ROLE_NAME_SHARED_WARNING = (
    '**Warning:** {count} roles here have the name that `{setting}` gives, and '
    'each of them counts as the {role} role. In `.env`, set it to the ID of the '
    'one you want.'
)
ROLE_ID_MISSING_WARNING = (
    '**Warning:** no role here has the ID that `{setting}` gives, so nobody '
    'here has the {role} role.'
)
# The server's default role, by its ID (the server's) or its name: every
# member has it, so access never counts it as one of TLE's roles.
EVERYONE_ROLE_WARNING = (
    '**Warning:** `{setting}` names @everyone, so it is ignored and nobody has '
    'the {role} role through it. In `.env`, set it to the ID of the role you want.'
)
RATED_VC_WARNING = (
    "**Warning:** `;ratedvc` works only in {channel}, which isn't a bot "
    "channel, so it can't be used. Add it with `/access bot-channels add`."
)
# The staff channel counts as a bot channel, but members can't read it.
RATED_VC_STAFF_WARNING = (
    "**Warning:** {channel} is the staff channel, which members can't read, so "
    "they can't use `;ratedvc`. Use `/set_ratedvc_channel` in a bot channel "
    'instead.'
)
NOT_STORED_WARNING = (
    '**Warning:** the bot is running without its database, so changes to these '
    'settings are lost when it stops.'
)
NOT_STORED_NOTE = (
    '**Note:** the bot is running without its database, so this change is lost '
    'when it stops.'
)
BROKEN_WARNING = (
    "**Warning:** this server's access settings could not be read, so every "
    "command but /access, /help and the bot owner's is refused. Repair them "
    'with `/access reset all`, then set the bot channels and the staff channel '
    'again.'
)
# When the bot couldn't read any server's settings: a reset would replace
# stored settings that were never read, so the bot owner must fix the database.
UNREADABLE_WARNING = (
    "**Warning:** the bot couldn't read its access settings, so every command "
    "but /access, /help and the bot owner's is refused, and these settings "
    "can't be changed. Ask the bot owner to check the log."
)
ANY_SERVER_NOTE = (
    "**Note:** `ALLOWED_GUILD_IDS` isn't set in `.env`, so the bot works in any "
    'server it is added to.'
)
SLASH_LIST_NOTE = (
    '**Note:** members still see these commands in their slash list, which '
    'limits never change. To hide one there, use Server Settings → '
    'Integrations.'
)
UNCHANGED_NOTE = (
    '**Note:** this changes nothing for now: the commands already work that way.'
)
STAFF_PLACE_WARNING = (
    '**Warning:** there is no staff channel yet, so commands limited to it '
    "don't work with `;`, and slash commands answer only the person who uses "
    'them. Set one with `/access staff-channel`.'
)
STAFF_ONLY_PLACE_WARNING = (
    '**Warning:** there is no staff channel yet, so commands limited to it '
    "can't be used at all. Set one with `/access staff-channel`."
)
BOT_PLACE_WARNING = (
    '**Warning:** there is no bot channel or staff channel yet, so commands '
    "limited to bot channels don't work with `;`, and slash commands answer "
    'only the person who uses them. Add one with `/access bot-channels add`.'
)
BOT_ONLY_PLACE_WARNING = (
    '**Warning:** there is no bot channel or staff channel yet, so commands '
    "limited to bot channels can't be used at all. Add one with "
    '`/access bot-channels add`.'
)
BOT_CHANNEL_ADDED_TEXT = (
    '{channel} is now a bot channel: member commands answer publicly there.'
)
BOT_CHANNEL_REMOVED_TEXT = '{channel} is no longer a bot channel.'
ALREADY_BOT_CHANNEL_TEXT = '{channel} is already a bot channel.'
NOT_BOT_CHANNEL_TEXT = "{channel} isn't a bot channel."
STAFF_COUNTS_TEXT = (
    'It is the staff channel, which works as a bot channel too. To change that, '
    'set another staff channel with `/access staff-channel`.'
)
BOT_CHANNELS_TEXT = 'Bot channels: {channels}'
TOO_MANY_BOT_CHANNELS_TEXT = (
    'This server already has {count} bot channels, the most it can have. '
    'Remove one first.'
)
TIDIED_ONE_TEXT = 'Also removed a bot channel that no longer exists.'
TIDIED_TEXT = 'Also removed {count} bot channels that no longer exist.'
RATED_VC_REMOVED_WARNING = (
    "**Warning:** `;ratedvc` works only in {channel}, so it can't be used now. "
    'Add the channel again, or make a bot channel the rated virtual contest '
    'channel with `/set_ratedvc_channel`.'
)
STAFF_CHANNEL_SET_TEXT = (
    '{channel} is now the staff channel, where staff commands work and answer publicly.'
)
ACCESS_MOVED_TEXT = 'From now on, `;access` works only there.'
STAFF_CHANNEL_CLEARED_TEXT = (
    "There is no staff channel now, so staff commands don't work with `;`, and "
    'a few not at all. Until you set one, `;access` works in any channel.'
)
SAME_STAFF_CHANNEL_TEXT = '{channel} is already the staff channel.'
NO_STAFF_CHANNEL_SHOW_TEXT = (
    "There is no staff channel yet, so `;access` won't show this server's "
    'settings where everyone can read them. See them with `/access show`, which '
    'answers only you, or set a staff channel with `/access staff-channel` or '
    '`;access staff-channel #channel`, then use `;access` there.'
)
STAFF_CHANNEL_GONE_SHOW_TEXT = (
    "The staff channel no longer exists, so `;access` won't show this server's "
    'settings where everyone can read them. See them with `/access show`, which '
    'answers only you, or set another staff channel with `/access staff-channel` '
    'or `;access staff-channel #channel`, then use `;access` there.'
)
NO_STAFF_CHANNEL_TEXT = 'There is no staff channel to clear.'
NO_COMMAND_TEXT = 'Name a command.'
NO_COMMAND_NAMED_TEXT = 'Name a command, or all.'
UNKNOWN_COMMAND_TEXT = 'There is no command called `{name}`.'
PROTECTED_TEXT = "/help and /access can't be limited: admins need them to undo limits."
OWNER_TEXT = '`{name}` is for the bot owner alone, so it takes no limits.'
NO_SUBCOMMANDS_TEXT = '`{name}` has no subcommands.'
NO_SLASH_TEXT = (
    "`{name}` has no slash command, so its answers can't be made private. To "
    'stop it working, use off instead.'
)
NONE_SLASH_TEXT = (
    "None of these commands has a slash command, so their answers can't be "
    'made private. To stop them working, use off instead.'
)
SOME_NOT_SLASH_WARNING = (
    "**Warning:** these have no slash command, so they can't be used now: {names}."
)
LIMIT_TEXT = '{key}: {limit}.'
NO_LIMIT_TEXT = '{key} has no limit.'
SAME_LIMIT_TEXT = '{key} already has this limit: {limit}.'
ASK_FOR_CHANGE_TEXT = 'To limit it, give who, where, private or off.'
NO_OWN_LIMIT_TEXT = '`{name}` has no limit of its own.'
CLEARED_TEXT = 'Cleared: {keys}.'
STILL_LIMITED_TEXT = 'Still limited by:\n{limits}'
NO_LIMITS_TEXT = 'This server has no limits.'
CLEARED_ONE_TEXT = 'Cleared the only limit.'
CLEARED_ALL_TEXT = 'Cleared all {count} limits.'
REPAIRED_TEXT = (
    "This server's access settings are repaired. Its bot channels and staff "
    'channel could not be read: set them again with `/access bot-channels add` '
    'and `/access staff-channel`.'
)


class AccessCogError(commands.CommandError):
    """A problem with what an admin asked of /access; its text tells them."""


class LimitFlags(commands.FlagConverter, case_insensitive=True):
    """What /access limit changes; each option left out stays as it is."""

    command: str = commands.flag(
        positional=True, description='The command to limit, such as duel register'
    )
    who: Optional[WhoName] = commands.flag(
        description='Let only these members (and admins) use it; unchanged if left out'
    )
    where: Optional[WhereName] = commands.flag(
        description=(
            'Where it answers publicly; the -only places also refuse it '
            'elsewhere; unchanged if left out'
        )
    )
    private: Optional[bool] = commands.flag(
        description=(
            'Whether answers are private, which turns off its ; form; unchanged '
            'if left out'
        )
    )
    off: Optional[bool] = commands.flag(
        description='Whether it is switched off in this server; unchanged if left out'
    )
    subcommands: Optional[bool] = commands.flag(
        description=(
            'Whether the limit also covers the subcommands of a group you name; '
            'yes if left out'
        )
    )


@dataclass(frozen=True)
class _Target:
    """The command an admin named, and the group a limit on it can cover."""

    command: commands.Command[Any, ..., Any]  # for a slash fallback, its group
    name: str  # the command's canonical name, the key of its own limit
    group: str | None  # the group whose subcommands a limit can cover too
    # Whether the admin named the group itself, so that a limit covers its
    # subcommands unless they say otherwise.
    whole: bool


class Access(commands.Cog):
    """/access: this server's bot channels, staff channel and command limits.

    Every command checks that the member is one of the server's admins, on top
    of the access rules, which give each of them that rule anyway.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _service(self) -> AccessService:
        """The bot's access service; ``RuntimeError`` without one."""
        service = getattr(self.bot, 'access', None)
        if not isinstance(service, AccessService):
            raise RuntimeError('The bot has no access service')
        return service

    async def cog_check(self, ctx: commands.Context[Any]) -> bool:
        # The access rules let in admins alone already; this holds even if
        # nothing applied them.
        service = getattr(self.bot, 'access', None)
        author = ctx.author
        if not isinstance(service, AccessService) or not isinstance(
            author, discord.Member
        ):
            return False
        return satisfies(service.asker(author), Who.ADMIN)

    @discord_common.send_error_if(AccessCogError)
    async def cog_command_error(
        self, ctx: commands.Context[Any], error: Exception
    ) -> None:
        # A command typed wrongly gets a hint of how to type it; anything else
        # is left to bot_error_handler.
        problem = _input_problem(ctx.command, error)
        if problem is not None:
            cast(Any, error).handled = True
            await self.cog_command_error(ctx, AccessCogError(problem))

    async def limit_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the commands that take limits whose names hold what was
        typed.
        """
        names = {
            command.qualified_name
            for command in self.bot.walk_commands()
            if _takes_limits(command.qualified_name)
        }
        return _choices(names, current)

    async def reset_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests all, then the commands with limits in this server."""
        service = getattr(self.bot, 'access', None)
        guild_id = interaction.guild_id
        if guild_id is None or not isinstance(service, AccessService):
            return []
        limits = service.guild_access(guild_id).limits
        names = {key.removesuffix(' *') for key in limits}
        everything = _command_name(current).lower() in ALL
        first = [app_commands.Choice(name=ALL, value=ALL)] if everything else []
        return (first + _choices(names, current))[:_MAX_CHOICES]

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(  # type: ignore[arg-type]
        name='access', brief="Show this server's access settings", fallback='show'
    )
    @app_commands.default_permissions(manage_guild=True)
    async def access(self, ctx: commands.Context[Any]) -> None:
        """Show this server's bot channels, staff channel and command limits.

        It also warns about anything that stops commands working as they should.
        ;access shows them only in the staff channel, so until there is one, or
        once it is gone, it says how to see them.

        Examples:
            /access show
            ;access
        """
        guild = _guild(ctx)
        service = self._service()
        settings = service.guild_access(guild.id)
        if ctx.interaction is None and not _has_staff_channel(guild, settings):
            # A prefix answer is public, and until there is a staff channel,
            # or once it is gone, ;access works in any channel.
            text = (
                NO_STAFF_CHANNEL_SHOW_TEXT
                if settings.staff_channel is None
                else STAFF_CHANNEL_GONE_SHOW_TEXT
            )
            await _send(ctx, discord_common.embed_neutral(text))
            return
        rated_vc = await self._rated_vc_channel(guild.id)
        pages = _settings_pages(self.bot, service, guild, settings, rated_vc)
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=_PAGES_WAIT_TIME,
            set_pagenum_footers=True,
            ctx=ctx,
            ephemeral=True,
        )

    @access.group(  # type: ignore[arg-type]
        name='bot-channels', brief='Show or change the bot channels'
    )
    async def channels(self, ctx: commands.Context[Any]) -> None:
        """Show the bot channels, where member commands answer publicly.

        Examples:
            ;access bot-channels
        """
        guild = _guild(ctx)
        service = self._service()
        settings = service.guild_access(guild.id)
        if settings.broken:
            text = _broken_warning(service, guild.id)
        else:
            channels = _channels_text(settings.bot_channels)
            text = (
                f'{BOT_CHANNELS_TEXT.format(channels=channels)}\n\nChange them with '
                '`/access bot-channels add` and `/access bot-channels remove`.'
            )
        await _send(ctx, discord.Embed(title='Bot channels', description=text))

    @channels.command(  # type: ignore[arg-type]
        name='add', brief='Add a bot channel, where member commands answer publicly'
    )
    @app_commands.describe(
        channel='A text, voice, stage or forum channel; its threads count too'
    )
    async def add_channel(
        self, ctx: commands.Context[Any], channel: CommandChannel
    ) -> None:
        """Add a bot channel. Member commands answer publicly there.

        A server can have up to 25 bot channels. Its staff channel works as one
        too, without being added.

        Examples:
            /access bot-channels add #bot-commands
            ;access bot-channels add #bot-commands
        """
        guild = _guild(ctx)
        service = self._service()
        before = await _changeable(service, guild)
        if channel.id in before.bot_channels:
            text = ALREADY_BOT_CHANNEL_TEXT.format(channel=channel.mention)
            await _send(ctx, _unchanged([text]))
            return
        tidied: set[int] = set()

        def edit(current: GuildAccess) -> GuildAccess:
            kept = _existing(guild, current.bot_channels, tidied) | {channel.id}
            if len(kept) > MAX_BOT_CHANNELS:
                raise AccessCogError(
                    TOO_MANY_BOT_CHANNELS_TEXT.format(count=MAX_BOT_CHANNELS)
                )
            return replace(current, bot_channels=frozenset(kept))

        after = await service.change(guild.id, edit)
        _log_change(ctx, guild, f'added the bot channel {channel.id}')
        lines = [BOT_CHANNEL_ADDED_TEXT.format(channel=channel.mention)]
        lines += _tidied_lines(tidied)
        channels = _channels_text(after.bot_channels)
        lines.append(BOT_CHANNELS_TEXT.format(channels=channels))
        await _send(ctx, self._changed('Bot channel added', lines))

    @channels.command(  # type: ignore[arg-type]
        name='remove', brief='Remove a bot channel'
    )
    @app_commands.describe(channel='The bot channel to remove')
    async def remove_channel(
        self, ctx: commands.Context[Any], channel: CommandChannel
    ) -> None:
        """Remove a bot channel. Member commands then answer there only the
        person who uses them.

        Examples:
            /access bot-channels remove #general
            ;access bot-channels remove #general
        """
        guild = _guild(ctx)
        service = self._service()
        before = await _changeable(service, guild)
        if channel.id not in before.bot_channels:
            lines = [NOT_BOT_CHANNEL_TEXT.format(channel=channel.mention)]
            if channel.id == before.staff_channel:
                lines.append(STAFF_COUNTS_TEXT)
            await _send(ctx, _unchanged(lines))
            return
        tidied: set[int] = set()

        def edit(current: GuildAccess) -> GuildAccess:
            kept = _existing(guild, current.bot_channels - {channel.id}, tidied)
            return replace(current, bot_channels=frozenset(kept))

        after = await service.change(guild.id, edit)
        _log_change(ctx, guild, f'removed the bot channel {channel.id}')
        lines = [BOT_CHANNEL_REMOVED_TEXT.format(channel=channel.mention)]
        lines += _tidied_lines(tidied)
        channels = _channels_text(after.bot_channels)
        lines.append(BOT_CHANNELS_TEXT.format(channels=channels))
        rated_vc = await self._rated_vc_channel(guild.id)
        if (
            rated_vc is not None
            and _place_of(guild, rated_vc) == channel.id
            and after.staff_channel != channel.id
        ):
            lines.append(RATED_VC_REMOVED_WARNING.format(channel=channel.mention))
        if not after.bot_channels:
            lines.append(_no_bot_channel_warning(after))
        await _send(ctx, self._changed('Bot channel removed', lines))

    @access.command(  # type: ignore[arg-type]
        name='staff-channel', brief='Set or clear the staff channel'
    )
    @app_commands.describe(
        channel='The channel for staff commands, which only staff should read; '
        'cleared if left out'
    )
    async def staff_channel(
        self,
        ctx: commands.Context[Any],
        channel: CommandChannel | None = _OPTIONAL_CHANNEL,
    ) -> None:
        """Set the staff channel, where staff commands work, or leave the channel
        out to clear it.

        Only staff should be able to read it, as staff commands answer there.

        Examples:
            /access staff-channel channel:#staff
            ;access staff-channel #staff
            /access staff-channel
        """
        guild = _guild(ctx)
        service = self._service()
        before = await _changeable(service, guild)
        channel_id = None if channel is None else channel.id
        if before.staff_channel == channel_id:
            if channel is None:
                text = NO_STAFF_CHANNEL_TEXT
            else:
                text = SAME_STAFF_CHANNEL_TEXT.format(channel=channel.mention)
            await _send(ctx, _unchanged([text]))
            return
        await service.change(
            guild.id, lambda current: replace(current, staff_channel=channel_id)
        )
        if channel is None:
            _log_change(ctx, guild, 'cleared the staff channel')
            lines = [STAFF_CHANNEL_CLEARED_TEXT]
            await _send(ctx, self._changed('Staff channel cleared', lines))
            return
        _log_change(ctx, guild, f'set the staff channel to {channel.id}')
        lines = [STAFF_CHANNEL_SET_TEXT.format(channel=channel.mention)]
        if not _has_staff_channel(guild, before):
            # ;access worked in any channel until now.
            lines.append(ACCESS_MOVED_TEXT)
        if _everyone_can_read(guild, channel):
            lines.append(PUBLIC_STAFF_CHANNEL_WARNING.format(channel=channel.mention))
        await _send(ctx, self._changed('Staff channel set', lines))

    @access.command(  # type: ignore[arg-type]
        name='limit',
        brief='Limit who may use a command, where, and who sees its answers',
    )
    @app_commands.autocomplete(command=limit_autocomplete)
    async def limit(self, ctx: commands.Context[Any], *, flags: LimitFlags) -> None:
        """Limit a command in this server. A limit can only tighten its rule.

        Each option left out keeps what the command's limit says already. A
        group's limit covers its subcommands too, unless subcommands is no.

        Examples:
            /access limit command:duel register off:yes
            ;access limit duel register off:yes
            /access limit command:gitgud where:bot-only
            ;access limit plot who:trusted
        """
        guild = _guild(ctx)
        service = self._service()
        before = await _changeable(service, guild)
        if not _command_name(flags.command):
            raise AccessCogError(NO_COMMAND_TEXT)
        target = _find(self.bot, flags.command)
        if target is None:
            name = _shown(flags.command)
            raise AccessCogError(UNKNOWN_COMMAND_TEXT.format(name=name))
        key = _limit_key(target, flags.subcommands)
        covered = _covered(self.bot, key)
        warning = self._check_private(target, key, covered) if flags.private else None
        old = before.limits.get(key)
        if _merged(old, flags) == (old or Limit()):
            embed = self._limit_unchanged(key, old, flags, covered, before)
            await _send(ctx, embed)
            return
        after = await service.change(
            guild.id,
            lambda current: current.with_limit(
                key, _merged(current.limits.get(key), flags)
            ),
        )
        new = after.limits.get(key)
        described = 'none' if new is None else describe_limit(new)
        _log_change(ctx, guild, f'limited {key}: {described}')
        if new is None:
            title = 'Limit removed'
            lines = [NO_LIMIT_TEXT.format(key=_key_text(key))]
        else:
            title = 'Limit set' if old is None else 'Limit changed'
            lines = [LIMIT_TEXT.format(key=_key_text(key), limit=described)]
        if warning is not None:
            lines.append(warning)
        if flags.where is not None:
            rules = (effective_for(cmd.qualified_name, after) for cmd in covered)
            lines += _place_warnings(rules, after)
        if not self._changes_anything(covered, before, after):
            lines.append(UNCHANGED_NOTE)
        if any(service.slash_path(command) is not None for command in covered):
            lines.append(SLASH_LIST_NOTE)
        embed = self._changed(title, lines)
        _add_rules(embed, self._rules(covered, after))
        await _send(ctx, embed)

    @access.command(  # type: ignore[arg-type]
        name='reset', brief="Clear a command's limits, or every limit"
    )
    @app_commands.describe(
        command='The command whose limits to clear, or all for every limit'
    )
    @app_commands.autocomplete(command=reset_autocomplete)
    async def reset(self, ctx: commands.Context[Any], *, command: str) -> None:
        """Clear a command's limits, or every limit in this server with all.

        For a group, this clears its limits but not its subcommands' own.
        Clearing every limit also repairs settings that could not be read.

        Examples:
            /access reset command:duel register
            ;access reset duel register
            /access reset command:all
        """
        guild = _guild(ctx)
        typed = _command_name(command)
        if not typed:
            raise AccessCogError(NO_COMMAND_NAMED_TEXT)
        if typed.lower() == ALL:
            await self._reset_all(ctx, guild)
            return
        service = self._service()
        before = await _changeable(service, guild)
        target = _find(self.bot, typed)
        cleared = [key for key in _own_keys(target, typed) if key in before.limits]
        if not cleared:
            if target is None:
                raise AccessCogError(UNKNOWN_COMMAND_TEXT.format(name=_shown(typed)))
            _check_takes_limits(target.name)
            lines = [NO_OWN_LIMIT_TEXT.format(name=_code(target.name))]
            lines += _still_limited(self.bot, target, before)
            embed = _unchanged(lines)
            _add_rules(embed, self._rules(_subtree(self.bot, target), before))
            await _send(ctx, embed)
            return

        def edit(current: GuildAccess) -> GuildAccess:
            for key in cleared:
                current = current.with_limit(key, None)
            return current

        after = await service.change(guild.id, edit)
        _log_change(ctx, guild, f'cleared the limits {", ".join(sorted(cleared))}')
        keys = '; '.join(_key_text(key) for key in sorted(cleared))
        lines = [CLEARED_TEXT.format(keys=keys)]
        lines += _still_limited(self.bot, target, after)
        embed = self._changed('Limits cleared', lines)
        if target is not None:
            _add_rules(embed, self._rules(_subtree(self.bot, target), after))
        await _send(ctx, embed)

    async def _reset_all(
        self, ctx: commands.Context[Any], guild: discord.Guild
    ) -> None:
        """Clear every limit, which also repairs this server's settings if they
        couldn't be read; never those of a database that couldn't be read at
        all, whose row may be fine.
        """
        service = self._service()
        await service.ensure_readable(guild.id)
        before = service.guild_access(guild.id)
        if not before.broken and not before.limits:
            await _send(ctx, _unchanged([NO_LIMITS_TEXT]))
            return
        await service.change(guild.id, lambda current: current.without_limits())
        _log_change(ctx, guild, 'cleared every limit')
        if before.broken:
            lines = [REPAIRED_TEXT]
        elif len(before.limits) == 1:
            lines = [CLEARED_ONE_TEXT]
        else:
            lines = [CLEARED_ALL_TEXT.format(count=len(before.limits))]
        await _send(ctx, self._changed('Limits cleared', lines))

    def _changed(self, title: str, lines: Sequence[str]) -> discord.Embed:
        """The answer to a change: ``lines``, and a note if it isn't stored."""
        if not self._service().persistent:
            lines = [*lines, NOT_STORED_NOTE]
        embed = discord_common.embed_success(_fit('\n'.join(lines), _DESCRIPTION_LIMIT))
        embed.title = title
        return embed

    def _check_private(
        self,
        target: _Target,
        key: str,
        covered: Sequence[commands.Command[Any, ..., Any]],
    ) -> str | None:
        """Refuse private answers for a command without a slash command, which
        they would switch off. For a group, warn which of its commands they
        switch off, and refuse if that is every one.
        """
        service = self._service()
        if not key.endswith(' *'):
            if service.slash_path(target.command) is None:
                raise AccessCogError(NO_SLASH_TEXT.format(name=_code(target.name)))
            return None
        unslashed = [
            command.qualified_name
            for command in covered
            if service.slash_path(command) is None
        ]
        if len(unslashed) == len(covered):
            raise AccessCogError(NONE_SLASH_TEXT)
        if not unslashed:
            return None
        return SOME_NOT_SLASH_WARNING.format(names=_listed(unslashed))

    def _limit_unchanged(
        self,
        key: str,
        limit: Limit | None,
        flags: LimitFlags,
        covered: Sequence[commands.Command[Any, ..., Any]],
        settings: GuildAccess,
    ) -> discord.Embed:
        """The answer to a limit that changes nothing stored."""
        if limit is None:
            lines = [NO_LIMIT_TEXT.format(key=_key_text(key))]
        else:
            described = describe_limit(limit)
            lines = [SAME_LIMIT_TEXT.format(key=_key_text(key), limit=described)]
        given = (flags.who, flags.where, flags.private, flags.off)
        if all(option is None for option in given):
            lines.append(ASK_FOR_CHANGE_TEXT)
        embed = _unchanged(lines)
        _add_rules(embed, self._rules(covered, settings))
        return embed

    def _changes_anything(
        self,
        covered: Iterable[commands.Command[Any, ..., Any]],
        before: GuildAccess,
        after: GuildAccess,
    ) -> bool:
        """Whether any of ``covered`` works differently with ``after``."""
        service = self._service()
        for command in covered:
            slash = service.slash_path(command) is not None
            name = command.qualified_name
            old = _describe_rule(effective_for(name, before), slash=slash)
            new = _describe_rule(effective_for(name, after), slash=slash)
            if old != new:
                return True
        return False

    def _rules(
        self,
        covered: Iterable[commands.Command[Any, ..., Any]],
        settings: GuildAccess,
    ) -> list[tuple[str, list[str]]]:
        """Each rule that ``covered`` work by with ``settings``, and those
        that work by it, in order, each as an admin would name it.
        """
        service = self._service()
        rules: dict[str, list[str]] = {}
        for command in covered:
            path = service.slash_path(command)
            rule = effective_for(command.qualified_name, settings)
            names = rules.setdefault(_describe_rule(rule, slash=path is not None), [])
            names.append(_label(command.qualified_name, path))
        return list(rules.items())

    async def _rated_vc_channel(self, guild_id: int) -> int | None:
        """The server's rated virtual contest channel; None if it has none, or
        if the database can't say.
        """
        user_db = getattr(self.bot, 'user_db', None)
        if user_db is None:
            return None
        try:
            channel_id = await user_db.get_rated_vc_channel(guild_id)
        except db.DatabaseDisabledError:
            return None
        except Exception as exc:
            logger.warning(
                'Could not read the rated vc channel of guild %d: %s', guild_id, exc
            )
            return None
        return channel_id if isinstance(channel_id, int) else None


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    # The access rules admit server members only, so there is always one.
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


async def _changeable(service: AccessService, guild: discord.Guild) -> GuildAccess:
    """The server's settings, which a change starts from.

    ``SettingsUnreadable`` if the bot couldn't read the settings at all (see
    ``AccessService.ensure_readable``), and ``SettingsNeedRepair`` if this
    server's could not be read: only clearing every limit repairs them.
    """
    await service.ensure_readable(guild.id)
    settings = service.guild_access(guild.id)
    if settings.broken:
        raise SettingsNeedRepair()
    return settings


def _has_staff_channel(guild: discord.Guild, settings: GuildAccess) -> bool:
    """Whether ``guild`` has the staff channel that ``settings`` name. The
    access rules treat one that is gone as none.
    """
    staff_channel = settings.staff_channel
    return staff_channel is not None and guild.get_channel(staff_channel) is not None


def _broken_warning(service: AccessService, guild_id: int) -> str:
    """The warning that server ``guild_id``'s settings couldn't be read."""
    if service.settings_unreadable(guild_id):
        return UNREADABLE_WARNING
    return BROKEN_WARNING


async def _send(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's answer; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _unchanged(lines: Sequence[str]) -> discord.Embed:
    embed = discord_common.embed_neutral(_fit('\n'.join(lines), _DESCRIPTION_LIMIT))
    embed.title = 'Nothing changed'
    return embed


def _log_change(ctx: commands.Context[Any], guild: discord.Guild, what: str) -> None:
    logger.info('Member %d in guild %d %s', ctx.author.id, guild.id, what)


def _command_name(text: str) -> str:
    """``text`` as a command's name: single spaces, without quotes around it,
    and without a leading / or ;.
    """
    name = text.strip().strip(_QUOTES).strip()
    if name[:1] in ('/', ';'):
        name = name[1:].strip().strip(_QUOTES)
    return ' '.join(name.split())


def _find(bot: commands.Bot, text: str) -> _Target | None:
    """The command called ``text``, as admins type it; None if there is none.

    A slash fallback, such as clist show, names its group's own command, and
    so does the prefix twin of one, such as contests upcoming.
    """
    name = _command_name(text)
    if not name:
        return None
    for typed in dict.fromkeys((name, name.lower())):
        command = _get_command(bot, typed)
        if command is not None:
            return _target(bot, command)
        parent_name, _, last = typed.rpartition(' ')
        parent = _get_command(bot, parent_name) if parent_name else None
        if isinstance(parent, commands.HybridGroup) and parent.fallback == last:
            name_of_group = canonical(parent.qualified_name)
            return _Target(parent, name_of_group, _group_name(parent), whole=False)
    return None


def _target(bot: commands.Bot, command: commands.Command[Any, ..., Any]) -> _Target:
    name = canonical(command.qualified_name)
    group = _group_name(command)
    if group is not None:
        return _Target(command, name, group, whole=True)
    # A twin, such as contests upcoming, is its group's own command.
    twin_of = _get_command(bot, name) if name != command.qualified_name else None
    group = None if twin_of is None else _group_name(twin_of)
    return _Target(command, name, group, whole=False)


def _group_name(command: commands.Command[Any, ..., Any]) -> str | None:
    """``command``'s name, if it is a group with subcommands."""
    if isinstance(command, commands.Group) and command.commands:
        return command.qualified_name
    return None


def _get_command(
    bot: commands.Bot, name: str
) -> commands.Command[Any, ..., Any] | None:
    """``bot.get_command(name)``, but None unless every word of ``name`` is
    part of the command's name: discord.py ignores the words after a command
    that isn't a group.
    """
    command = bot.get_command(name)
    if command is None or len(command.qualified_name.split()) != len(name.split()):
        return None
    return command


def _takes_limits(name: str) -> bool:
    """Whether command ``name`` takes limits: the bot owner's commands, those
    without a rule, /help and /access take none.
    """
    rule = rule_for(name)
    return rule is not None and rule.who is not Who.OWNER and not is_protected(name)


def _check_takes_limits(name: str) -> None:
    """``AccessCogError``, saying why, unless command ``name`` takes limits."""
    if is_protected(name):
        raise AccessCogError(PROTECTED_TEXT)
    if not _takes_limits(name):
        raise AccessCogError(OWNER_TEXT.format(name=_code(name)))


def _limit_key(target: _Target, subcommands: bool | None) -> str:
    """The key of the limit that /access limit sets on ``target``, with its
    group's subcommands if ``subcommands`` (by default, if the group itself
    was named).

    ``AccessCogError`` if the command takes no limits.
    """
    if subcommands is None:
        subcommands = target.whole
    if subcommands and target.group is None:
        raise AccessCogError(NO_SUBCOMMANDS_TEXT.format(name=_code(target.name)))
    name = target.group if subcommands and target.group is not None else target.name
    _check_takes_limits(name)
    return f'{name} *' if subcommands else name


def _own_keys(target: _Target | None, typed: str) -> list[str]:
    """The keys of the limits that /access reset clears for ``typed``: those
    /access limit can set on it.

    For a command that is no longer the bot's, they are its name alone and
    with its subcommands, so that its limits can still be cleared.
    """
    if target is None:
        names = dict.fromkeys((canonical(typed), canonical(typed.lower())))
        return [key for name in names for key in (name, f'{name} *')]
    if target.whole:
        return [target.name, f'{target.group} *']
    if target.group is None:
        # /access limit never sets such a key, but it limits just this command.
        return [target.name, f'{target.name} *']
    return [target.name]


def _covered(bot: commands.Bot, key: str) -> list[commands.Command[Any, ..., Any]]:
    """The commands that the limit under ``key`` applies to, by name."""
    found = (
        command
        for command in bot.walk_commands()
        if key in limit_keys(command.qualified_name)
        and _takes_limits(command.qualified_name)
    )
    return sorted(found, key=lambda command: command.qualified_name)


def _subtree(
    bot: commands.Bot, target: _Target
) -> list[commands.Command[Any, ..., Any]]:
    """The commands whose rules /access reset shows for ``target``."""
    if target.whole:
        return _covered(bot, f'{target.group} *')
    return _covered(bot, target.name)


def _still_limited(
    bot: commands.Bot, target: _Target | None, settings: GuildAccess
) -> list[str]:
    """A line naming the limits that still apply to ``target``'s commands."""
    if target is None:
        return []
    keys = {
        key
        for command in _subtree(bot, target)
        for key in limit_keys(command.qualified_name)
        if key in settings.limits
    }
    if not keys:
        return []
    limits = '\n'.join(
        _limit_line(bot, key, settings.limits[key]) for key in sorted(keys)
    )
    return [STILL_LIMITED_TEXT.format(limits=limits)]


def _merged(old: Limit | None, flags: LimitFlags) -> Limit:
    """``old`` with the options given in ``flags``."""
    base = old or Limit()
    return Limit(
        who=base.who if flags.who is None else Who(flags.who),
        where=base.where if flags.where is None else Where(flags.where),
        private=base.private if flags.private is None else flags.private,
        off=base.off if flags.off is None else flags.off,
    )


def _place_warnings(rules: Iterable[Effective], settings: GuildAccess) -> list[str]:
    """A warning if commands with ``rules`` need a channel that the server,
    with ``settings``, lacks.
    """
    wheres = {rule.where for rule in rules if not rule.off}
    if settings.staff_channel is not None:
        return []
    warnings: list[str] = []
    if Where.STAFF in wheres:
        warnings.append(STAFF_PLACE_WARNING)
    elif Where.STAFF_ONLY in wheres:
        warnings.append(STAFF_ONLY_PLACE_WARNING)
    # The staff channel counts as a bot channel, and there is none.
    if not settings.bot_channels:
        if Where.BOT in wheres:
            warnings.append(BOT_PLACE_WARNING)
        elif Where.BOT_ONLY in wheres:
            warnings.append(BOT_ONLY_PLACE_WARNING)
    return warnings


def _describe_rule(rule: Effective, *, slash: bool) -> str:
    """Who may use a command by ``rule``, and where, such as 'Everyone · Bot
    channels only'; ``slash`` says whether the command has a slash command.
    """
    if rule.off:
        return 'Switched off'
    if rule.private and not slash:
        return (
            "Can't be used: only the person who uses it may see its answers, "
            'and it has no slash command'
        )
    who = describe_who(rule.who)
    if not rule.private:
        return f'{who} · {describe_where(rule.where, slash=slash)}'
    # Outside its place, a private slash command answers privately anyway,
    # unless the place refuses it there.
    if rule.where.refuses_outside:
        place = describe_where(rule.where, slash=False)
    else:
        place = describe_where(Where.ANYWHERE)
    return f'{who} · {place} · Only the person who uses it sees the answer'


def _label(name: str, path: str | None) -> str:
    """Command ``name`` in a code span, and its slash command ``path`` too if
    that is named otherwise, as /clist show runs clist.
    """
    label = f'`{_code(name)}`'
    if path is not None and path != f'/{name}':
        label += f' (`{_code(path)}`)'
    return label


def _add_rules(embed: discord.Embed, rules: Sequence[tuple[str, list[str]]]) -> None:
    """Add a field to ``embed`` for each rule, naming the commands that work
    by it, as long as they fit.
    """
    for rule, labels in rules:
        value = _fit(', '.join(labels), _FIELD_LIMIT)
        if len(embed) + len(rule) + len(value) > _EMBED_LIMIT:
            break
        embed.add_field(name=rule, value=value, inline=False)


def _key_text(key: str) -> str:
    """The commands that a limit's key names, such as '`duel` and its
    subcommands'.
    """
    if key.endswith(' *'):
        return f'`{_code(key[:-2])}` and its subcommands'
    return f'`{_code(key)}`'


def _code(text: str) -> str:
    """``text`` ready for a code span, which a backtick would end early."""
    return text.replace('`', "'")


def _shown(text: str) -> str:
    """What an admin typed as a command's name, ready to repeat in a code span."""
    name = _code(_command_name(text))
    return name if len(name) <= _SHOWN else f'{name[: _SHOWN - 1]}…'


def _listed(names: Iterable[str]) -> str:
    """The first few ``names``, in code spans."""
    listed = sorted(names)
    shown = ', '.join(f'`{_code(name)}`' for name in listed[:_LISTED])
    if len(listed) > _LISTED:
        shown += f' and {len(listed) - _LISTED} more'
    return shown


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else f'{text[: limit - 1]}…'


def _choices(names: Iterable[str], current: str) -> list[app_commands.Choice[str]]:
    """Choices for the ``names`` that hold what was typed, in order."""
    typed = _command_name(current).lower()
    found = sorted(name for name in names if typed in name)[:_MAX_CHOICES]
    return [app_commands.Choice(name=name, value=name) for name in found]


def _existing(
    guild: discord.Guild, channel_ids: Iterable[int], gone: set[int]
) -> set[int]:
    """The ``channel_ids`` of channels that ``guild`` still has; the others
    are added to ``gone``.
    """
    kept = set()
    for channel_id in channel_ids:
        if guild.get_channel(channel_id) is None:
            gone.add(channel_id)
        else:
            kept.add(channel_id)
    return kept


def _tidied_lines(tidied: set[int]) -> list[str]:
    if not tidied:
        return []
    if len(tidied) == 1:
        return [TIDIED_ONE_TEXT]
    return [TIDIED_TEXT.format(count=len(tidied))]


def _channels_text(channel_ids: Iterable[int]) -> str:
    mentions = [f'<#{channel_id}>' for channel_id in sorted(channel_ids)]
    return ', '.join(mentions) or 'none yet'


def _everyone_can_read(guild: discord.Guild, channel: Any) -> bool:
    """Whether @everyone can see ``channel``, a channel of ``guild``."""
    return bool(channel.permissions_for(guild.default_role).view_channel)


def _no_bot_channel_warning(settings: GuildAccess) -> str:
    """The warning that there is no bot channel, for a server with ``settings``."""
    where = '' if settings.staff_channel is None else OUTSIDE_STAFF_CHANNEL
    return NO_BOT_CHANNEL_WARNING.format(where=where)


def _settings_pages(
    bot: commands.Bot,
    service: AccessService,
    guild: discord.Guild,
    settings: GuildAccess,
    rated_vc: int | None,
) -> list[paginator.Page]:
    """/access show's pages: the settings and their warnings, and the limits on
    further pages if they don't fit on the first.
    """
    warnings = _warnings(service, guild, settings, rated_vc)
    embed = discord.Embed(
        title='Access settings', description='\n'.join(warnings) or None
    )
    if settings.broken:
        return [(None, embed)]
    embed.add_field(
        name='Bot channels', value=_channels_text(settings.bot_channels), inline=False
    )
    staff = settings.staff_channel
    embed.add_field(
        name='Staff channel',
        value='none yet' if staff is None else f'<#{staff}>',
        inline=False,
    )
    embed.add_field(name='Developer role', value=_developer_role(guild), inline=False)
    lines = [
        _limit_line(bot, key, settings.limits[key]) for key in sorted(settings.limits)
    ]
    limits = '\n'.join(lines) or 'none'
    if len(limits) <= _FIELD_LIMIT:
        embed.add_field(name='Limits', value=limits, inline=False)
        return [(None, embed)]
    embed.add_field(
        name='Limits', value=f'{len(lines)}, on the next pages', inline=False
    )
    pages: list[paginator.Page] = [(None, embed)]
    for chunk in _chunks(lines, _DESCRIPTION_LIMIT):
        pages.append((None, discord.Embed(title='Limits', description=chunk)))
    return pages


def _chunks(lines: Sequence[str], limit: int) -> list[str]:
    """``lines`` joined into texts of at most ``limit`` characters, in order."""
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        line = _fit(line, limit)
        if current and len('\n'.join([*current, line])) > limit:
            chunks.append('\n'.join(current))
            current = []
        current.append(line)
    if current:
        chunks.append('\n'.join(current))
    return chunks


def _limit_line(bot: commands.Bot, key: str, limit: Limit) -> str:
    line = LIMIT_TEXT.format(key=_key_text(key), limit=describe_limit(limit))
    if _get_command(bot, key.removesuffix(' *')) is None:
        line = f'{line[:-1]} (no such command now).'
    return line


def _developer_role(guild: discord.Guild) -> str:
    """TLE's developer role, which only .env can change."""
    role_id = constants.TLE_DEVELOPER
    if role_id is None:
        return "none: `TLE_DEVELOPER` isn't set in `.env`"
    if role_id == guild.id:
        # The default role, which never counts.
        return 'none: `TLE_DEVELOPER` names @everyone'
    if guild.get_role(role_id) is None:
        return 'none in this server'
    return f'<@&{role_id}>, from `.env`'


def _warnings(
    service: AccessService,
    guild: discord.Guild,
    settings: GuildAccess,
    rated_vc: int | None,
) -> list[str]:
    """What /access show warns about: anything that stops commands working as
    they should, then a note if any server may use the bot.
    """
    warnings: list[str] = []
    if not service.persistent:
        warnings.append(NOT_STORED_WARNING)
    if settings.broken:
        warnings.append(_broken_warning(service, guild.id))
    else:
        warnings += _channel_warnings(guild, settings, rated_vc)
    warnings += _role_warnings(guild)
    if not service.allowed_guilds:
        warnings.append(ANY_SERVER_NOTE)
    return warnings


def _channel_warnings(
    guild: discord.Guild, settings: GuildAccess, rated_vc: int | None
) -> list[str]:
    warnings: list[str] = []
    if not settings.bot_channels:
        warnings.append(_no_bot_channel_warning(settings))
    gone: set[int] = set()
    _existing(guild, settings.bot_channels, gone)
    if len(gone) == 1:
        warnings.append(BOT_CHANNEL_GONE_WARNING)
    elif gone:
        warnings.append(BOT_CHANNELS_GONE_WARNING.format(count=len(gone)))
    staff_id = settings.staff_channel
    staff = None if staff_id is None else guild.get_channel(staff_id)
    if staff_id is None:
        warnings.append(NO_STAFF_CHANNEL_WARNING)
    elif staff is None:
        warnings.append(STAFF_CHANNEL_GONE_WARNING)
    elif _everyone_can_read(guild, staff):
        warnings.append(PUBLIC_STAFF_CHANNEL_WARNING.format(channel=staff.mention))
    # ;ratedvc works only in its own channel, and only in bot channels: the
    # staff channel counts as one, but members can't read it.
    if rated_vc is not None:
        place = _place_of(guild, rated_vc)
        if place == staff_id:
            warnings.append(RATED_VC_STAFF_WARNING.format(channel=f'<#{place}>'))
        elif place not in settings.bot_channels:
            warnings.append(RATED_VC_WARNING.format(channel=f'<#{rated_vc}>'))
    return warnings


def _place_of(guild: discord.Guild, channel_id: int) -> int:
    """The channel whose rules apply in channel ``channel_id`` of ``guild``:
    for a thread, the channel it is in, as the access rules count it.
    """
    found = guild.get_channel_or_thread(channel_id)
    if isinstance(found, discord.Thread):
        return found.parent_id
    return channel_id


def _role_warnings(guild: discord.Guild) -> list[str]:
    """A warning for each of TLE's roles that names no role here, several, or
    @everyone, which doesn't count.
    """
    warnings: list[str] = []
    for setting, role in _ROLE_SETTINGS:
        value: str | int | None = getattr(constants, setting)
        if value is None:
            continue
        if value == guild.id or value == _EVERYONE_NAME:
            warnings.append(EVERYONE_ROLE_WARNING.format(setting=setting, role=role))
            continue
        if isinstance(value, int):
            if guild.get_role(value) is None:
                text = ROLE_ID_MISSING_WARNING.format(setting=setting, role=role)
                warnings.append(text)
            continue
        count = sum(1 for each in guild.roles if each.name == value)
        if count == 0:
            text = ROLE_NAME_MISSING_WARNING.format(setting=setting, role=role)
            warnings.append(text)
        elif count > 1:
            text = ROLE_NAME_SHARED_WARNING.format(
                setting=setting, count=count, role=role
            )
            warnings.append(text)
    return warnings


# The values that /access limit's who takes, to tell its errors from where's.
_WHO_NAMES = frozenset(who.value for who in LIMIT_WHO)


def _input_problem(
    command: commands.Command[Any, ..., Any] | None, error: Exception
) -> str | None:
    """How to type ``command``, if ``error`` is a mistake in how it was typed
    that this says better than discord.py; otherwise None.
    """
    example = _example(command)
    like = '' if example is None else f', as in `{example}`'
    if isinstance(error, commands.MissingRequiredArgument):
        missing = 'a channel' if error.param.name == 'channel' else 'a command'
        return f'Name {missing}{like}.'
    if isinstance(error, commands.MissingRequiredFlag):
        return f'Name a command{like}.'
    if isinstance(error, (commands.BadUnionArgument, commands.ChannelNotFound)):
        return (
            "I can't find that channel here. Name a text, voice, stage or forum "
            f'channel{like}.'
        )
    if isinstance(error, commands.BadLiteralArgument):
        literals = [str(literal) for literal in error.literals]
        option = 'who' if set(literals) == _WHO_NAMES else 'where'
        return f'`{option}` takes {_joined(literals)}.'
    if isinstance(error, commands.BadBoolArgument):
        return f'Use yes or no, not `{_shown(error.argument)}`.'
    if isinstance(error, commands.TooManyFlags):
        return f'Give `{error.flag.name}` only once.'
    if isinstance(error, commands.MissingFlagArgument):
        return f'Give `{error.flag.name}` a value{like}.'
    return None


def _joined(items: Sequence[str]) -> str:
    """``items`` as a list in a sentence: 'a, b or c'."""
    if len(items) < 2:
        return ''.join(items)
    return f'{", ".join(items[:-1])} or {items[-1]}'


def _example(command: commands.Command[Any, ..., Any] | None) -> str | None:
    """The first prefix example in ``command``'s help, else its first example."""
    if command is None or not command.help:
        return None
    _, _, block = command.help.partition('Examples:')
    examples = [line.strip() for line in block.splitlines() if line.strip()]
    for line in examples:
        if line.startswith(';'):
            return line
    return examples[0] if examples else None
