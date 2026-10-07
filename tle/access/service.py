"""The access rules at work: every command is checked before it runs.

``AccessService`` holds every server's access settings in memory. It reads
them from the user database once, at start-up, and writes each change there
before it takes effect. Its ``check`` runs before every command, prefix or
slash, and refuses the command, saying why, unless the rules allow it here;
the decision stays on the context, so that ``TLEContext`` can keep a private
answer private. ``decide``, ``slash_path`` and ``listed_slash_path`` also
serve /help and /access, and ``component_allowed`` checks the buttons of
commands' replies.

``AccessTree`` is the bot's slash command tree. It refuses private messages,
servers outside the allow-list and application commands that the check can't
see, and keeps autocomplete from suggesting anything to members who can't use
the command.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.access import rules
from tle.access.policy import effective_for
from tle.access.rules import STAFF_LEVELS, Asker, Decision, Outcome, Spot, Where, Who
from tle.access.settings import GuildAccess, decode, encode
from tle.access.table import canonical, rule_for
from tle.util import discord_common
from tle.util.discord_common import (
    NOT_ALLOWED_MESSAGE,
    PRIVATE_MESSAGES_MESSAGE,
    UNEXPECTED_ERROR_MESSAGE,
    AccessDenied,
)

log = logging.getLogger('tle.access')

# The replies to members. They never name a role or an id, and never show the
# staff channel to a member who isn't staff.
# The error handler's reply to a prefix command in private messages, too.
PRIVATE_MESSAGES_TEXT = PRIVATE_MESSAGES_MESSAGE
NOT_AVAILABLE_TEXT = 'This bot is not available in this server.'
# A slash command or context menu that no prefix command wraps, which the
# access rules therefore can't check.
UNCHECKED_COMMAND_TEXT = "This command isn't available."
OFF_TEXT = 'This command is switched off in this server.'
REPAIR_TEXT = (
    "This server's access settings need repair. An admin can reset them with "
    '`/access reset all`.'
)
# When the bot couldn't read the access settings at all. Admins can't repair
# that: a reset would replace stored settings that were never read.
UNREADABLE_TEXT = (
    "The bot couldn't read this server's access settings. Ask the bot owner to "
    'check the log.'
)
UNREADABLE_CHANGE_TEXT = (
    "The bot couldn't read its access settings, so they can't be changed. Ask "
    'the bot owner to check the log.'
)
NOT_HERE_TEXT = "This command can't be used here."
BOT_CHANNELS_TEXT = 'Use this command in a bot channel: {channels}.'
# When the server has bot channels, but none that the member can see.
A_BOT_CHANNEL_TEXT = 'Use this command in a bot channel.'
# The admins' hints name /access only to admins whose slash list shows it,
# and the ; command to the others, such as admins by role alone.
NO_BOT_CHANNEL_ADMIN_TEXT = (
    'There is no bot channel yet. Add one with `/access bot-channels add`.'
)
NO_BOT_CHANNEL_ADMIN_PREFIX_TEXT = (
    'There is no bot channel yet. Add one with '
    '`;access bot-channels add #channel`{where}.'
)
IN_THE_STAFF_CHANNEL = ' in the staff channel'
NO_BOT_CHANNEL_TEXT = (
    'This command only works in a bot channel, and this server has none yet. '
    'Ask an admin to add one.'
)
STAFF_CHANNEL_TEXT = 'Use this command in the staff channel.'
STAFF_CHANNEL_SLASH_TEXT = 'Use this command in <#{channel_id}>.'
NO_STAFF_CHANNEL_ADMIN_TEXT = (
    'There is no staff channel yet. Set one with `/access staff-channel`.'
)
NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT = (
    'There is no staff channel yet. Set one with `;access staff-channel #channel`.'
)
SLASH_HINT = 'Or use `{path}` here: only you will see the answer.'
PRIVATE_ONLY_TEXT = (
    'In this server only the person who uses this command sees its answer: '
    'use `{path}`.'
)
COMMAND_CHANGED_TEXT = 'This command has changed. Try again in a minute.'

# The most bot channels a refusal lists.
LISTED_CHANNELS = 5
# After failing to find the bot's owners, how long to wait before asking
# Discord again, when an owner's command needs them.
OWNER_RETRY_SECONDS = 5 * 60

_OWNER_ROLES = (discord.TeamMemberRole.admin, discord.TeamMemberRole.developer)
# Where a context keeps its decisions, by the command's qualified name.
_DECISIONS = '_access_decisions'
# The settings of a server without a row, and of every server when no row
# could be read.
_DEFAULT_ACCESS = GuildAccess()
_BROKEN_ACCESS = GuildAccess(broken=True)
# The /access commands that the admins' hints name.
_SET_STAFF_CHANNEL = 'access staff-channel'
_ADD_BOT_CHANNEL = 'access bot-channels add'


class SettingsNeedRepair(AccessDenied):
    """A change to a server's access settings, which are broken.

    Broken settings can't be stored, since what their row held is unknown, so
    the only change they take is a reset of every limit.
    """

    def __init__(self) -> None:
        super().__init__(REPAIR_TEXT)


class SettingsUnreadable(AccessDenied):
    """A change to access settings that the bot couldn't read at all.

    Nothing is stored then: a change would replace the server's stored row,
    which was never read and may be fine.
    """

    def __init__(self) -> None:
        super().__init__(UNREADABLE_CHANGE_TEXT)


class AccessService:
    """Every server's access settings, and the check of every command.

    With ``allowed_guilds``, only those servers can use the bot; empty, any
    server can. Settings stay in memory alone until ``use_user_db`` gives a
    database, as under --nodb and in tests.
    """

    def __init__(
        self,
        bot: commands.Bot,
        *,
        allowed_guilds: frozenset[int] = frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot = bot
        self.allowed_guilds = frozenset(allowed_guilds)
        self._clock = clock
        self._user_db: Any | None = None
        self._settings: dict[int, GuildAccess] = {}
        # The last load failed, so every server's settings are unknown, and
        # none can be changed until a load reads them.
        self._unreadable = False
        # Held while a change reads the settings again after a failed load.
        self._load_lock = asyncio.Lock()
        self._locks: dict[int, asyncio.Lock] = {}
        self._owners_found = False
        self._owners_warned = False
        self._owners_retry_at: float | None = None
        # The commands without a rule whose buttons have been warned about.
        self._unruled_buttons: set[str] = set()

    # Settings

    def use_user_db(self, user_db: Any | None) -> None:
        """Store settings in ``user_db`` from now on; None keeps them in memory."""
        self._user_db = user_db

    @property
    def persistent(self) -> bool:
        """Whether changes are stored, so that they outlive the bot's process."""
        return self._user_db is not None

    async def load(self) -> None:
        """Read every server's settings from the database into memory.

        A server whose row can't be read gets broken settings, and a readable
        row's problems are logged. If the database can't be read at all,
        every server's settings count as broken, and none can be changed (see
        ``ensure_readable``) until a later load reads them.
        """
        if self._user_db is None:
            log.info(
                'There is no database, so access settings are kept in memory '
                'and lost when the bot stops'
            )
            return
        try:
            rows = await self._user_db.get_all_access_settings()
        except Exception as exc:
            self._settings = {}
            self._unreadable = True
            log.error(
                'Could not read the access settings (%s: %s). Until they can be '
                'read, every server refuses every command but /access, /help '
                "and the bot owner's, and no server's settings can be changed. "
                'If a row of the access_settings table in user.db is unreadable, '
                'repair or delete the row with that guild id, then restart the '
                'bot.',
                type(exc).__name__,
                exc,
            )
            return
        settings: dict[int, GuildAccess] = {}
        for guild_id, text in rows:
            access, warnings = decode(text)
            if access.broken:
                log.error(
                    'The access settings of guild %d are unreadable (%s). Until '
                    'an admin there uses /access reset all, it refuses every '
                    "command but /access, /help and the bot owner's.",
                    guild_id,
                    '; '.join(warnings),
                )
            else:
                for warning in warnings:
                    log.warning('Access settings of guild %d: %s', guild_id, warning)
            settings[guild_id] = access
        self._settings = settings
        self._unreadable = False
        count = len(settings)
        log.info(
            'Loaded the access settings of %d %s',
            count,
            'server' if count == 1 else 'servers',
        )

    def guild_access(self, guild_id: int) -> GuildAccess:
        """The access settings of server ``guild_id``, from memory."""
        access = self._settings.get(guild_id)
        if access is not None:
            return access
        return _BROKEN_ACCESS if self._unreadable else _DEFAULT_ACCESS

    def settings_unreadable(self, guild_id: int) -> bool:
        """Whether server ``guild_id``'s settings are unknown because the last
        load couldn't read the access settings at all, rather than because its
        own row is broken.
        """
        return self._unreadable and guild_id not in self._settings

    def broken_text(self, guild_id: int) -> str:
        """What refusals say when server ``guild_id``'s settings are broken: an
        admin can repair the server's own row, but only the bot owner can fix
        a database that couldn't be read at all.
        """
        return UNREADABLE_TEXT if self.settings_unreadable(guild_id) else REPAIR_TEXT

    async def ensure_readable(self, guild_id: int) -> None:
        """Make sure that server ``guild_id``'s settings were read, before a
        change to them.

        After a load that couldn't read the settings at all, they are read
        again, once; ``SettingsUnreadable`` if they still can't be, as a
        change would replace the server's row, which was never read.
        """
        if not self.settings_unreadable(guild_id):
            return
        async with self._load_lock:
            # Another change may have read them while this one waited.
            if self.settings_unreadable(guild_id):
                await self.load()
        if self.settings_unreadable(guild_id):
            raise SettingsUnreadable()

    async def change(
        self, guild_id: int, edit: Callable[[GuildAccess], GuildAccess]
    ) -> GuildAccess:
        """Change server ``guild_id``'s settings to ``edit`` of them; the result.

        The database is written first, so if that fails nothing changes.
        ``SettingsUnreadable`` if the settings couldn't be read at all (see
        ``ensure_readable``), and ``SettingsNeedRepair`` if the result is
        still broken: only ``GuildAccess.without_limits`` repairs a server's
        broken settings.
        """
        await self.ensure_readable(guild_id)
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        async with lock:
            new = edit(self.guild_access(guild_id))
            if new.broken:
                raise SettingsNeedRepair()
            if self._user_db is not None:
                await self._user_db.set_access_settings(guild_id, encode(new))
            self._settings[guild_id] = new
        log.info('Changed the access settings of guild %d', guild_id)
        return new

    # Who asks

    async def resolve_owners(self) -> None:
        """Find the bot's owners, as discord.py's ``Bot.is_owner`` counts them.

        That is the application's owner, or the admins and developers of the
        team that owns it. If Discord can't be asked, one warning is logged,
        and an owner's command asks again, at most every OWNER_RETRY_SECONDS.
        """
        if self._owners_found or self._owners_known():
            self._owners_found = True
            return
        self._owners_retry_at = self._clock() + OWNER_RETRY_SECONDS
        try:
            app = await self.bot.application_info()
        except Exception as exc:
            if self._owners_warned:
                log.debug("Still can't find the bot's owners: %s", exc)
            else:
                self._owners_warned = True
                log.warning(
                    "Could not find the bot's owners (%s), so nobody can use "
                    "the bot owner's commands for now. An owner's command tries "
                    'again, at most every %d minutes.',
                    exc,
                    OWNER_RETRY_SECONDS // 60,
                )
            return
        self._owners_found = True
        if app.team:
            self.bot.owner_ids = {
                member.id for member in app.team.members if member.role in _OWNER_ROLES
            }
            if not self.bot.owner_ids:
                log.warning(
                    "The application's team has no admins or developers, so "
                    "nobody can use the bot owner's commands"
                )
        else:
            self.bot.owner_id = app.owner.id
        log.info("Found the bot's owners")

    def _owners_known(self) -> bool:
        return bool(self.bot.owner_id) or bool(self.bot.owner_ids)

    async def _find_owners_if_due(self) -> None:
        """Ask Discord for the owners again, if they're still unknown and the
        last attempt was long enough ago.
        """
        if self._owners_found or self._owners_known():
            return
        retry_at = self._owners_retry_at
        if retry_at is None or self._clock() >= retry_at:
            await self.resolve_owners()

    def is_owner(self, user: discord.abc.User) -> bool:
        """Whether ``user`` owns the bot. Never asks Discord: until the owners
        are found, nobody does.
        """
        owner_id = self.bot.owner_id
        if owner_id:
            return user.id == owner_id
        owner_ids = self.bot.owner_ids
        if not owner_ids:
            return False
        return user.id in owner_ids

    def asker(self, member: discord.Member | discord.User) -> Asker:
        """What counts about ``member`` for access, read now.

        TLE's admin, moderator and trusted roles are named in ``constants`` by
        id or name, and its developer role by id alone. A user who isn't a
        member of the server has no roles and no permissions there.
        """
        owner = self.is_owner(member)
        if not isinstance(member, discord.Member):
            return Asker(owner=owner)
        developer = constants.TLE_DEVELOPER
        return Asker(
            manage_guild=member.guild_permissions.manage_guild,
            admin_role=discord_common.has_role(member, constants.TLE_ADMIN),
            moderator_role=discord_common.has_role(member, constants.TLE_MODERATOR),
            trusted_role=discord_common.has_role(member, constants.TLE_TRUSTED),
            developer_role=(
                developer is not None and discord_common.has_role(member, developer)
            ),
            owner=owner,
        )

    # Decisions

    def guild_allowed(self, guild_id: int | None) -> bool:
        """Whether server ``guild_id`` may use the bot; outside a server, no."""
        if guild_id is None:
            return False
        return not self.allowed_guilds or guild_id in self.allowed_guilds

    def spot(self, channel: object, guild_id: int, *, slash: bool) -> Spot:
        """Where a command is used: ``channel``, which for a thread is the
        channel it is in, and the server's channels that matter there.
        """
        return _spot(channel, self._live_access(guild_id), slash=slash)

    def _live_access(self, guild_id: int) -> GuildAccess:
        """Server ``guild_id``'s settings as the rules apply them now: a staff
        channel that the server no longer has counts as none.

        So /access works in any channel again, as before there was a staff
        channel, and refusals never send anyone to a channel that is gone.
        Until the bot has the server, the stored staff channel stands.
        """
        access = self.guild_access(guild_id)
        staff_channel = access.staff_channel
        if staff_channel is None:
            return access
        guild = self.bot.get_guild(guild_id)
        if guild is None or guild.get_channel(staff_channel) is not None:
            return access
        return replace(access, staff_channel=None)

    async def decide(
        self,
        command: commands.Command[Any, ..., Any],
        member: discord.Member,
        channel: object,
        *,
        slash: bool,
    ) -> Decision:
        """Whether and how ``command`` runs for ``member`` in ``channel``.

        Unlike ``check``, it refuses nothing and keeps nothing, so it can
        answer for any command, as /help asks.
        """
        return await self._decide(command, member, channel, member.guild.id, slash)

    async def _decide(
        self,
        command: commands.Command[Any, ..., Any],
        user: discord.Member | discord.User,
        channel: object,
        guild_id: int,
        slash: bool,
    ) -> Decision:
        access = self._live_access(guild_id)
        rule = effective_for(command.qualified_name, access)
        if Who.OWNER in rule.who:
            await self._find_owners_if_due()
        spot = _spot(channel, access, slash=slash)
        return rules.decide(rule, self.asker(user), spot)

    async def check(self, ctx: commands.Context[Any]) -> bool:
        """The bot-wide check of every command, prefix or slash.

        True if the command may run; otherwise ``AccessDenied``, whose text
        says why, or ``NoPrivateMessage`` outside a server. The decision is
        kept on ``ctx`` for ``TLEContext``; nothing else changes, so running
        the check again gives the same answer.
        """
        guild = ctx.guild
        if guild is None:
            raise commands.NoPrivateMessage(PRIVATE_MESSAGES_TEXT)
        if not self.guild_allowed(guild.id):
            raise AccessDenied(None, silent=True)
        command = ctx.command
        if command is None:
            raise AccessDenied(None, silent=True)
        slash = ctx.interaction is not None
        decision = await self._decide(command, ctx.author, ctx.channel, guild.id, slash)
        cache_decision(ctx, decision)
        if decision.allowed:
            return True
        raise self.denial(ctx, decision)

    def denial(self, ctx: commands.Context[Any], decision: Decision) -> AccessDenied:
        """The refusal of ``ctx``'s command, as ``decision`` refuses it.

        A prefix command that the member may not use is refused silently, as
        if it didn't exist. ``ValueError`` if ``decision`` allows the command.
        """
        outcome = decision.outcome
        if outcome is Outcome.NOT_ALLOWED:
            if decision.slash:
                return AccessDenied(NOT_ALLOWED_MESSAGE)
            return AccessDenied(None, silent=True)
        if outcome is Outcome.OFF:
            return AccessDenied(OFF_TEXT)
        if outcome is Outcome.BROKEN:
            guild = ctx.guild
            text = REPAIR_TEXT if guild is None else self.broken_text(guild.id)
            return AccessDenied(text)
        if outcome is Outcome.PRIVATE_ONLY:
            path = self._listed_path_of(ctx)
            if path is None:
                return AccessDenied(NOT_HERE_TEXT)
            return AccessDenied(PRIVATE_ONLY_TEXT.format(path=path))
        if outcome is Outcome.WRONG_CHANNEL:
            return AccessDenied(self._wrong_channel_text(ctx, decision))
        raise ValueError(f'{outcome} is not a refusal')

    def _wrong_channel_text(
        self, ctx: commands.Context[Any], decision: Decision
    ) -> str:
        """Where the command works instead, as far as the member may know.

        A prefix command that its slash command could replace here, as it
        answers privately outside the command's place, also names the slash
        command, if the member's slash list shows it.
        """
        guild = ctx.guild
        where = decision.rule.where
        if guild is None or where is Where.ANYWHERE:
            return NOT_HERE_TEXT
        access = self._live_access(guild.id)
        user = ctx.author
        asker = self.asker(user)
        if where.scope == Where.STAFF.scope:
            text = self._staff_place_text(access, asker, user, slash=decision.slash)
        else:
            text = self._bot_place_text(access, asker, guild, user)
        if not decision.slash and not where.refuses_outside:
            path = self._listed_path_of(ctx)
            if path is not None:
                text = f'{text} {SLASH_HINT.format(path=path)}'
        return text

    def _listed_path_of(self, ctx: commands.Context[Any]) -> str | None:
        if ctx.command is None:
            return None
        return self.listed_slash_path(ctx.command, ctx.author)

    def _staff_place_text(
        self,
        access: GuildAccess,
        asker: Asker,
        user: discord.Member | discord.User,
        *,
        slash: bool,
    ) -> str:
        staff_channel = access.staff_channel
        if staff_channel is None:
            if not rules.satisfies(asker, Who.ADMIN):
                return NOT_HERE_TEXT
            if self._lists(_SET_STAFF_CHANNEL, user):
                return NO_STAFF_CHANNEL_ADMIN_TEXT
            return NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT
        if not any(rules.satisfies(asker, level) for level in STAFF_LEVELS):
            return NOT_HERE_TEXT
        if slash:
            # Only the member sees a slash refusal.
            return STAFF_CHANNEL_SLASH_TEXT.format(channel_id=staff_channel)
        # A prefix refusal is seen by the channel, so it doesn't link the
        # staff channel.
        return STAFF_CHANNEL_TEXT

    def _bot_place_text(
        self,
        access: GuildAccess,
        asker: Asker,
        guild: discord.Guild,
        user: discord.Member | discord.User,
    ) -> str:
        if not access.bot_channels:
            if not rules.satisfies(asker, Who.ADMIN):
                return NO_BOT_CHANNEL_TEXT
            if self._lists(_ADD_BOT_CHANNEL, user):
                return NO_BOT_CHANNEL_ADMIN_TEXT
            # With a staff channel, ;access works only there.
            where = '' if access.staff_channel is None else IN_THE_STAFF_CHANNEL
            return NO_BOT_CHANNEL_ADMIN_PREFIX_TEXT.format(where=where)
        shown = _visible_channels(guild, user, access.bot_channels)
        if not shown:
            return A_BOT_CHANNEL_TEXT
        mentions = ', '.join(channel.mention for channel in shown)
        return BOT_CHANNELS_TEXT.format(channels=mentions)

    def _lists(self, name: str, user: discord.Member | discord.User) -> bool:
        """Whether ``user``'s slash list shows the bot's command ``name``."""
        command = self.bot.get_command(name)
        return command is not None and self.listed_slash_path(command, user) is not None

    def slash_path(self, command: commands.Command[Any, ..., Any]) -> str | None:
        """How to use ``command`` as a slash command, such as '/clist show', or
        None if it has no slash form in the bot's tree right now.

        A group's own callback is used through the group's fallback, and a
        twin through the command it is a twin of.
        """
        app = self._slash_command(command)
        return None if app is None else f'/{app.qualified_name}'

    def listed_slash_path(
        self,
        command: commands.Command[Any, ..., Any],
        member: discord.Member | discord.User,
    ) -> str | None:
        """``slash_path``, but only if ``member``'s slash list shows the command.

        A command hidden behind default permissions shows only to members who
        have them all, unless a server's override says otherwise, which the
        bot can't see; it then counts as hidden.
        """
        app = self._slash_command(command)
        if app is None or not _listed_for(app, member):
            return None
        return f'/{app.qualified_name}'

    def _slash_command(
        self, command: commands.Command[Any, ..., Any]
    ) -> app_commands.Command[Any, ..., Any] | None:
        app = _slash_form(command)
        name = canonical(command.qualified_name)
        if app is None and name != command.qualified_name:
            twin = self.bot.get_command(name)
            if twin is not None:
                app = _slash_form(twin)
        if app is None or not self._in_tree(app):
            return None
        return app

    def _in_tree(self, app: app_commands.Command[Any, ..., Any]) -> bool:
        """Whether the bot's tree holds ``app`` now: something may have taken
        it, or a group above it, out.
        """
        names = app.qualified_name.split(' ')
        found = self.bot.tree.get_command(names[0])
        for name in names[1:]:
            if not isinstance(found, app_commands.Group):
                return False
            found = found.get_command(name)
        return found is app

    async def component_allowed(
        self, interaction: discord.Interaction, name: str
    ) -> bool:
        """Whether the member may press a button of command ``name``'s reply.

        The rule's who, a limit that switched the command off, broken settings
        and the allow-list count; channels don't, since the reply was posted
        where the command could answer. If not, the member is told privately.
        A command without a rule is warned about once.
        """
        guild_id = interaction.guild_id
        if guild_id is None:
            text = PRIVATE_MESSAGES_TEXT
        elif not self.guild_allowed(guild_id):
            text = NOT_AVAILABLE_TEXT
        else:
            if rule_for(name) is None and name not in self._unruled_buttons:
                self._unruled_buttons.add(name)
                log.warning('No access rule for the buttons of command %s', name)
            rule = effective_for(name, self.guild_access(guild_id))
            if Who.OWNER in rule.who:
                await self._find_owners_if_due()
            anywhere = replace(rule, where=Where.ANYWHERE, private=False)
            spot = Spot(slash=True, channel_id=None)
            decision = rules.decide(anywhere, self.asker(interaction.user), spot)
            if decision.allowed:
                return True
            if decision.outcome is Outcome.BROKEN:
                text = self.broken_text(guild_id)
            else:
                text = _BUTTON_REFUSALS.get(decision.outcome, NOT_ALLOWED_MESSAGE)
        await _answer_privately(interaction, text)
        return False

    def report_unruled(
        self, commands_: Iterable[commands.Command[Any, ..., Any]]
    ) -> list[str]:
        """The names of ``commands_`` that have no rule, each logged once.

        A command without a rule is for the bot owner alone, in the staff
        channel.
        """
        unruled: list[str] = []
        for command in commands_:
            name = command.qualified_name
            if rule_for(name) is None and name not in unruled:
                unruled.append(name)
                log.warning(
                    'Command %s has no access rule, so only the bot owner can '
                    'use it, and only in the staff channel',
                    name,
                )
        return unruled


# The texts of a button's refusals; broken settings have the service's own.
_BUTTON_REFUSALS = {
    Outcome.NOT_ALLOWED: NOT_ALLOWED_MESSAGE,
    Outcome.OFF: OFF_TEXT,
}


def cache_decision(ctx: commands.Context[Any], decision: Decision) -> None:
    """Keep ``decision`` on ``ctx``, for its command."""
    command = ctx.command
    if command is None:
        return
    decisions = getattr(ctx, _DECISIONS, None)
    if not isinstance(decisions, dict):
        decisions = {}
        setattr(ctx, _DECISIONS, decisions)
    decisions[command.qualified_name] = decision


def cached_decision(ctx: commands.Context[Any]) -> Decision | None:
    """The decision kept on ``ctx`` for its command now, if any."""
    command = ctx.command
    decisions = getattr(ctx, _DECISIONS, None)
    if command is None or not isinstance(decisions, dict):
        return None
    decision = decisions.get(command.qualified_name)
    return decision if isinstance(decision, Decision) else None


def _spot(channel: object, access: GuildAccess, *, slash: bool) -> Spot:
    """Where a command is used: ``channel``, in a server with ``access``."""
    return Spot(
        slash=slash,
        channel_id=_place_of(channel),
        bot_channels=access.bot_channels,
        staff_channel=access.staff_channel,
    )


def _place_of(channel: object) -> int | None:
    """The id of the channel whose rules apply in ``channel``: a thread's
    are those of the channel it is in.
    """
    if isinstance(channel, discord.Thread):
        return channel.parent_id
    channel_id = getattr(channel, 'id', None)
    return channel_id if isinstance(channel_id, int) else None


def _visible_channels(
    guild: discord.Guild,
    user: discord.Member | discord.User,
    channel_ids: Iterable[int],
) -> list[discord.abc.GuildChannel]:
    """The first few of ``channel_ids`` that ``user`` can see in ``guild``."""
    if not isinstance(user, discord.Member):
        return []
    shown: list[discord.abc.GuildChannel] = []
    for channel_id in sorted(channel_ids):
        channel = guild.get_channel(channel_id)
        if channel is not None and channel.permissions_for(user).view_channel:
            shown.append(channel)
            if len(shown) == LISTED_CHANNELS:
                break
    return shown


def _slash_form(
    command: commands.Command[Any, ..., Any],
) -> app_commands.Command[Any, ..., Any] | None:
    """The slash command that runs ``command``: its own, or the fallback of a
    group's own callback; None if it has neither.
    """
    if isinstance(command, commands.HybridGroup):
        group = command.app_command
        if not group or command.fallback is None:
            return None
        fallback = group.get_command(command.fallback)
        return fallback if isinstance(fallback, app_commands.Command) else None
    if isinstance(command, commands.HybridCommand):
        return command.app_command
    return None


def _wrapped(command: object) -> commands.Command[Any, ..., Any] | None:
    """The prefix command that application command ``command`` runs: that of
    a hybrid command's slash form, or for a group's fallback the group; None
    for any other.
    """
    wrapped = getattr(command, 'wrapped', None)
    return wrapped if isinstance(wrapped, commands.Command) else None


def _listed_for(
    app: app_commands.Command[Any, ..., Any], user: discord.Member | discord.User
) -> bool:
    """Whether Discord shows ``app`` in ``user``'s slash list: only a top-level
    command's default permissions count.
    """
    root = app.root_parent or app
    needed = root.default_permissions
    if needed is None:
        return True
    if not isinstance(user, discord.Member):
        return False
    return needed.is_subset(user.guild_permissions)


async def _answer_privately(interaction: discord.Interaction, text: str) -> None:
    """Tell the member who interacted ``text``, in an alert only they see.

    Once the interaction has expired nothing is sent, and a failure to send
    is only logged.
    """
    if interaction.is_expired() is True:
        log.debug('Not answering an expired interaction: %s', text)
        return
    embed = discord_common.embed_alert(text)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning('Could not answer an interaction (%s): %s', text, exc)


class AccessTree(app_commands.CommandTree[Any]):
    """The bot's slash command tree, which checks every interaction first.

    It refuses private messages and servers outside the allow-list, telling
    the member so, and offers autocomplete only to members who may use the
    command where they are. Every other check is the bot's own, which slash
    commands run as prefix commands do, so it also refuses a slash command or
    context menu that no prefix command wraps: nothing else would check it.
    It reads the bot's access service when each interaction comes; a bot
    without one gets discord.py's tree.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # The commands that no prefix command wraps, each warned about once.
        self._unchecked: set[str] = set()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        access: AccessService | None = getattr(self.client, 'access', None)
        if access is None:
            return True
        # An autocomplete can't be answered with a message.
        autocomplete = interaction.type is discord.InteractionType.autocomplete
        guild_id = interaction.guild_id
        if guild_id is None or not access.guild_allowed(guild_id):
            if not autocomplete:
                text = PRIVATE_MESSAGES_TEXT if guild_id is None else NOT_AVAILABLE_TEXT
                await _answer_privately(interaction, text)
            return False
        if autocomplete:
            return await _autocomplete_allowed(access, interaction)
        command = interaction.command
        # A command the tree doesn't know fails there, and on_error says that
        # it has changed.
        if command is None or _wrapped(command) is not None:
            return True
        name = command.qualified_name
        if name not in self._unchecked:
            self._unchecked.add(name)
            log.warning(
                'Application command %s has no prefix command, so the access '
                'rules cannot check it, and it is refused',
                name,
            )
        await _answer_privately(interaction, UNCHECKED_COMMAND_TEXT)
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Log ``error``, and tell the member privately if nothing answered
        their command.

        Errors in commands themselves go to the bot's error handler first, so
        these are mostly a command that Discord still lists but the bot no
        longer has, or has with other options.
        """
        await super().on_error(interaction, error)
        if interaction.type is not discord.InteractionType.application_command:
            return
        if interaction.response.is_done():
            return
        changed = (app_commands.CommandNotFound, app_commands.CommandSignatureMismatch)
        if isinstance(error, changed):
            text = COMMAND_CHANGED_TEXT
        else:
            text = UNEXPECTED_ERROR_MESSAGE
        await _answer_privately(interaction, text)


async def _autocomplete_allowed(
    access: AccessService, interaction: discord.Interaction
) -> bool:
    """Whether the member may use the command they are typing, where they are;
    if not, they get no suggestions, which could reveal what it works on.
    """
    command = _wrapped(interaction.command)
    user = interaction.user
    if command is None or not isinstance(user, discord.Member):
        return False
    try:
        decision = await access.decide(command, user, interaction.channel, slash=True)
    except Exception:
        log.exception('Could not check the autocomplete of %s', command)
        return False
    return decision.allowed
