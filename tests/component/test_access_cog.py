"""Tests for /access (tle.access.cog): the bot channels, the staff channel and
the command limits that a server's admins choose.

The bot is a real one, whose access service checks every command as TLEBot's
does, with the Access cog and a cog of commands of every shape that admins can
name: plain, prefix-only, groups with and without a slash fallback, the prefix
twin of a fallback, the bot owner's, /help and one without a rule. The tests
replace the rule table, so that they don't depend on TLE's own, except where
they check the cog against it. Settings are stored in a real in-memory user
database. Contexts are real, for prefix and slash invocations. Most tests call
a command's callback, as discord.py does once it has parsed the arguments; the
rest go through discord.py, to check its checks and how it parses what admins
type. Discord itself (the server, its channels, roles and members, and the
interaction) is mocked.
"""

import logging
import sqlite3
from collections.abc import AsyncIterator, Mapping
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.access import table
from tle.access.cog import (
    ACCESS_MOVED_TEXT,
    ALREADY_BOT_CHANNEL_TEXT,
    ANY_SERVER_NOTE,
    ASK_FOR_CHANGE_TEXT,
    BOT_CHANNELS_GONE_WARNING,
    BOT_CHANNELS_TEXT,
    BOT_CHANNEL_ADDED_TEXT,
    BOT_CHANNEL_GONE_WARNING,
    BOT_CHANNEL_REMOVED_TEXT,
    BOT_ONLY_PLACE_WARNING,
    BOT_PLACE_WARNING,
    BROKEN_WARNING,
    CLEARED_ALL_TEXT,
    CLEARED_ONE_TEXT,
    CLEARED_TEXT,
    EVERYONE_ROLE_WARNING,
    NONE_SLASH_TEXT,
    NOT_BOT_CHANNEL_TEXT,
    NOT_STORED_NOTE,
    NOT_STORED_WARNING,
    NO_BOT_CHANNEL_WARNING,
    NO_COMMAND_TEXT,
    NO_LIMITS_TEXT,
    NO_LIMIT_TEXT,
    NO_OWN_LIMIT_TEXT,
    NO_SLASH_TEXT,
    NO_STAFF_CHANNEL_SHOW_TEXT,
    NO_STAFF_CHANNEL_TEXT,
    NO_STAFF_CHANNEL_WARNING,
    NO_SUBCOMMANDS_TEXT,
    OUTSIDE_STAFF_CHANNEL,
    OWNER_TEXT,
    PROTECTED_TEXT,
    PUBLIC_STAFF_CHANNEL_WARNING,
    RATED_VC_REMOVED_WARNING,
    RATED_VC_STAFF_WARNING,
    RATED_VC_WARNING,
    REPAIRED_TEXT,
    ROLE_ID_MISSING_WARNING,
    ROLE_NAME_MISSING_WARNING,
    ROLE_NAME_SHARED_WARNING,
    SAME_LIMIT_TEXT,
    SAME_STAFF_CHANNEL_TEXT,
    SLASH_LIST_NOTE,
    SOME_NOT_SLASH_WARNING,
    STAFF_CHANNEL_CLEARED_TEXT,
    STAFF_CHANNEL_GONE_SHOW_TEXT,
    STAFF_CHANNEL_GONE_WARNING,
    STAFF_CHANNEL_SET_TEXT,
    STAFF_COUNTS_TEXT,
    STAFF_ONLY_PLACE_WARNING,
    STAFF_PLACE_WARNING,
    STILL_LIMITED_TEXT,
    TIDIED_ONE_TEXT,
    TIDIED_TEXT,
    TOO_MANY_BOT_CHANNELS_TEXT,
    UNCHANGED_NOTE,
    UNKNOWN_COMMAND_TEXT,
    UNREADABLE_WARNING,
    Access,
    AccessCogError,
    LimitFlags,
)
from tle.access.policy import effective_for
from tle.access.rules import LIMIT_WHERE, LIMIT_WHO, Limit, Rule, Where, Who
from tle.access.service import (
    OFF_TEXT,
    REPAIR_TEXT,
    SLASH_HINT,
    STAFF_CHANNEL_TEXT,
    UNREADABLE_CHANGE_TEXT,
    AccessService,
    AccessTree,
    SettingsNeedRepair,
    SettingsUnreadable,
)
from tle.access.settings import MAX_BOT_CHANNELS, GuildAccess, decode, encode
from tle.access.slash import apply_visibility
from tle.util import db
from tle.util.discord_common import (
    NOT_ALLOWED_MESSAGE,
    REFUSAL_DELETE_AFTER,
    AccessDenied,
    bot_error_handler,
    embed_alert,
)
from tle.util.paginator import PaginatorView

LOGGER = 'tle.access.cog'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
SECOND_BOT_CHANNEL_ID = 1_200_000_000_000_000_002
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
RATED_VC_ID = 1_200_000_000_000_000_030
THREAD_ID = 1_200_000_000_000_000_031  # a thread, in the channel a test puts it in
GONE_ID = 1_200_000_000_000_000_040  # a channel the server no longer has
OTHER_GONE_ID = 1_200_000_000_000_000_041
SPARE_CHANNELS_ID = 1_200_000_000_000_000_100  # where more channels start
MEMBER_ID = 1_400_000_000_000_000_001
ADMIN_ROLE_ID = 1_300_000_000_000_000_001
MODERATOR_ROLE_ID = 1_300_000_000_000_000_002
TRUSTED_ROLE_ID = 1_300_000_000_000_000_003
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_005
MISSING_ROLE_ID = 1_300_000_000_000_000_099

# TLE's own table, before the tests replace it.
REAL_RULES = table.RULES
REAL_TWINS = table.TWINS
ACCESS_COMMANDS = {name for name in REAL_RULES if name.split()[0] == 'access'}
RULES = {
    'gitgud': Rule(Who.EVERYONE, Where.BOT),
    'gimme': Rule(Who.EVERYONE, Where.BOT),  # a prefix command alone
    'clist': Rule(Who.EVERYONE, Where.BOT),  # a group's own command: /clist show
    'clist future': Rule(Who.EVERYONE, Where.BOT),
    'contests': Rule(Who.EVERYONE, Where.BOT),  # /contests upcoming
    'contests live': Rule(Who.EVERYONE, Where.BOT),
    'handle show': Rule(Who.EVERYONE, Where.BOT),
    'handle set': Rule(Who.MODERATOR, Where.BOT),
    'handle list': Rule(Who.EVERYONE, Where.BOT_ONLY),  # a prefix subcommand
    'duel': Rule(Who.EVERYONE, Where.BOT),
    'duel register': Rule(Who.MODERATOR, Where.BOT),
    'duel challenge': Rule(Who.EVERYONE, Where.BOT_ONLY),
    'meta': Rule(Who.EVERYONE, Where.BOT),
    'meta kill': Rule(Who.OWNER, Where.ANYWHERE),
    'meta ping': Rule(Who.EVERYONE, Where.BOT),
    'cache': Rule(Who.OWNER, Where.ANYWHERE),
    'cache contests': Rule(Who.OWNER, Where.ANYWHERE),
    'kcpc': Rule(Who.ADMIN, Where.STAFF),
    'kcpc status': Rule(Who.DEVELOPER, Where.STAFF_ONLY),
    'help': Rule(Who.EVERYONE, Where.BOT),
    # /access itself, as TLE's table has it.
    **{name: REAL_RULES[name] for name in ACCESS_COMMANDS},
}
# The prefix subcommand ;contests upcoming does what /contests upcoming, the
# group's fallback, does; ;handle, the group's own callback, is handle show.
TWINS = {'contests upcoming': 'contests', 'handle': 'handle show'}

Context = commands.Context[Any]


class Club(commands.Cog):
    """Commands of every shape that admins can name."""

    @commands.hybrid_command()
    async def gitgud(self, ctx: Context) -> None:
        """A member command."""

    @commands.command()
    async def gimme(self, ctx: Context) -> None:
        """A prefix command alone."""

    @commands.hybrid_group(fallback='show')
    async def clist(self, ctx: Context) -> None:
        """A group whose own command is /clist show."""

    @clist.command()
    async def future(self, ctx: Context) -> None:
        """A subcommand."""

    async def cog_load(self) -> None:
        # Added here, as KCPC adds its twins: declared in the group, discord.py
        # would take the group's fallback of the same name out of the slash
        # group when it copies the cog's commands.
        self.contests.add_command(self.contests_upcoming)

    @commands.hybrid_group(fallback='upcoming')
    async def contests(self, ctx: Context) -> None:
        """A group whose fallback has a prefix twin."""

    @contests.command()
    async def live(self, ctx: Context) -> None:
        """A subcommand."""

    @commands.hybrid_command(name='upcoming', with_app_command=False)
    async def contests_upcoming(self, ctx: Context) -> None:
        """The prefix twin of /contests upcoming."""

    @commands.hybrid_group()
    async def handle(self, ctx: Context) -> None:
        """A group without a fallback, the twin of handle show."""

    @handle.command(name='show')
    async def handle_show(self, ctx: Context) -> None:
        """Show a handle."""

    @handle.command(name='set')
    async def handle_set(self, ctx: Context) -> None:
        """A moderator's subcommand."""

    @handle.command(name='list', with_app_command=False)
    async def handle_list(self, ctx: Context) -> None:
        """A prefix subcommand alone."""

    @commands.hybrid_group(fallback='show')
    async def duel(self, ctx: Context) -> None:
        """A group of member and moderator commands."""

    @duel.command()
    async def register(self, ctx: Context) -> None:
        """A moderator's subcommand."""

    @duel.command()
    async def challenge(self, ctx: Context) -> None:
        """A subcommand for bot channels only."""

    @commands.hybrid_group(fallback='show')
    async def meta(self, ctx: Context) -> None:
        """A group with a command for the bot owner."""

    @meta.command()
    async def kill(self, ctx: Context) -> None:
        """For the bot owner."""

    @meta.command()
    async def ping(self, ctx: Context) -> None:
        """For everyone."""

    @commands.hybrid_group(fallback='show')
    async def cache(self, ctx: Context) -> None:
        """A group for the bot owner alone."""

    @cache.command(name='contests')
    async def cache_contests(self, ctx: Context) -> None:
        """For the bot owner."""

    @commands.hybrid_group(fallback='show')
    async def kcpc(self, ctx: Context) -> None:
        """A group for admins."""

    @kcpc.command()
    async def status(self, ctx: Context) -> None:
        """For developers, in the staff channel only."""

    @commands.hybrid_command(name='help')
    async def help_command(self, ctx: Context) -> None:
        """Stands in for /help."""

    @commands.hybrid_command()
    async def mystery(self, ctx: Context) -> None:
        """A command missing from the rule table."""


class AccessBot(commands.Bot):
    """A bot that carries an access service and a user database, as TLEBot does."""

    access: AccessService
    user_db: Any = None


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles: admin, moderator and trusted by name, developer by id."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', DEVELOPER_ROLE_ID)


@pytest.fixture(autouse=True)
def rule_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table, 'RULES', MappingProxyType(RULES))
    monkeypatch.setattr(table, 'TWINS', MappingProxyType(TWINS))


@pytest.fixture
def real_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's own rule table, in place of the tests' one."""
    monkeypatch.setattr(table, 'RULES', REAL_RULES)
    monkeypatch.setattr(table, 'TWINS', REAL_TWINS)


@pytest.fixture
async def bot(user_db: Any) -> AsyncIterator[AccessBot]:
    """A bot that only GUILD_ID may use, which stores its settings in
    ``user_db``, with the Access and Club cogs.
    """
    bot = AccessBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    bot.access = AccessService(bot, allowed_guilds=frozenset({GUILD_ID}))
    bot.access.use_user_db(user_db)
    bot.add_check(bot.access.check)
    bot.user_db = user_db
    await bot.add_cog(Access(bot))
    await bot.add_cog(Club())
    yield bot
    await bot.close()


def make_role(name: str, role_id: int) -> MagicMock:
    role = MagicMock(spec=discord.Role, id=role_id, mention=f'<@&{role_id}>')
    role.name = name  # not MagicMock(name=...), which names the mock itself
    return role


def make_channel(
    guild: MagicMock,
    channel_id: int,
    *,
    public: bool = True,
    kind: type[discord.abc.GuildChannel] = discord.TextChannel,
) -> MagicMock:
    """A channel in ``guild``, which @everyone can read if ``public``."""
    channel = MagicMock(
        spec=kind, id=channel_id, guild=guild, mention=f'<#{channel_id}>'
    )
    channel.name = f'channel-{channel_id % 1000}'

    def permissions_for(target: object) -> discord.Permissions:
        if target is guild.default_role:
            return discord.Permissions(view_channel=public)
        return discord.Permissions(view_channel=True)

    channel.permissions_for.side_effect = permissions_for
    guild.channels_by_id[channel_id] = channel
    # Where discord.py's channel converters look channels up by name.
    listed = {
        discord.TextChannel: guild.text_channels,
        discord.VoiceChannel: guild.voice_channels,
        discord.StageChannel: guild.stage_channels,
        discord.ForumChannel: guild.forums,
    }
    listed[kind].append(channel)
    return channel


def make_guild() -> MagicMock:
    """A server with TLE's roles, two bot channels to be, a private staff
    channel to be, and a general and a rated virtual contest channel.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.default_role = make_role('@everyone', GUILD_ID)
    guild.roles = [
        guild.default_role,
        make_role('Admin', ADMIN_ROLE_ID),
        make_role('Moderator', MODERATOR_ROLE_ID),
        make_role('Trusted', TRUSTED_ROLE_ID),
        make_role('Developers', DEVELOPER_ROLE_ID),
    ]
    guild.get_role.side_effect = lambda role_id: next(
        (role for role in guild.roles if role.id == role_id), None
    )
    guild.channels_by_id = {}
    guild.get_channel.side_effect = guild.channels_by_id.get
    guild.text_channels = []
    guild.voice_channels = []
    guild.stage_channels = []
    guild.forums = []
    for channel_id in (BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID, GENERAL_ID, RATED_VC_ID):
        make_channel(guild, channel_id)
    make_channel(guild, STAFF_CHANNEL_ID, public=False)
    return guild


@pytest.fixture
def guild() -> MagicMock:
    return make_guild()


def make_thread(guild: MagicMock, thread_id: int, parent_id: int) -> MagicMock:
    """A thread of ``guild``, in its channel ``parent_id``."""
    thread = MagicMock(
        spec=discord.Thread, id=thread_id, parent_id=parent_id, guild=guild
    )
    guild.get_channel_or_thread.side_effect = lambda channel_id: (
        thread if channel_id == thread_id else guild.channels_by_id.get(channel_id)
    )
    return thread


def channel(guild: MagicMock, channel_id: int) -> MagicMock:
    found: MagicMock = guild.channels_by_id[channel_id]
    return found


def known_to(bot: commands.Bot, guild: MagicMock) -> None:
    """Let ``bot`` find ``guild``, as a bot with the guilds intent finds its
    servers.
    """
    bot.get_guild = {guild.id: guild}.get  # type: ignore[method-assign,assignment]


def make_member(guild: MagicMock, *roles: str, manage_guild: bool = False) -> MagicMock:
    """A member of ``guild`` with the roles of these names."""
    member = MagicMock(spec=discord.Member, id=MEMBER_ID, guild=guild)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = [role for role in guild.roles if role.name in roles]
    return member


@pytest.fixture
def admin(guild: MagicMock) -> MagicMock:
    """An admin through the Manage Server permission."""
    return make_member(guild, manage_guild=True)


def make_context(
    bot: commands.Bot,
    name: str,
    author: MagicMock,
    place: MagicMock,
    *,
    slash: bool = False,
    arguments: str = '',
) -> Context:
    """A real context of command ``name``, used by ``author`` in ``place``,
    with ``arguments`` left to parse.

    With ``slash``, it belongs to an interaction, as for a slash command.
    Replies are recorded by an ``AsyncMock`` in place of ``send``.
    """
    command = bot.get_command(name)
    assert command is not None, name
    message = MagicMock(
        spec=discord.Message, guild=author.guild, author=author, channel=place
    )
    message.content = f';{name} {arguments}'
    message.jump_url = 'https://discord.com/channels/1/2/3'
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = False
    ctx: Context = commands.Context(
        message=message,
        bot=bot,
        view=StringView(arguments),
        prefix='/' if slash else ';',
        command=command,
        invoked_with=command.name,
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = ctx  # where discord.py keeps a slash command's context
    ctx.send = AsyncMock()  # type: ignore[method-assign]
    return ctx


def access_cog(bot: commands.Bot) -> Access:
    cog = bot.get_cog('Access')
    assert isinstance(cog, Access)
    return cog


async def run(bot: commands.Bot, ctx: Context, *args: Any, **kwargs: Any) -> None:
    """Call the callback of ``ctx``'s command, as discord.py does once it has
    parsed the arguments, ``args`` and ``kwargs``.
    """
    assert ctx.command is not None
    await ctx.command.callback(access_cog(bot), ctx, *args, **kwargs)


async def invoke(
    bot: commands.Bot,
    name: str,
    author: MagicMock,
    place: MagicMock,
    arguments: str = '',
) -> Context:
    """Run ``;name arguments`` as discord.py does: the checks, the parsing of
    the arguments, the callback, and on an error the cog's handler and then
    the bot's, which discord.py would schedule on the running bot's loop.
    """
    ctx = make_context(bot, name, author, place, arguments=arguments)
    assert ctx.command is not None
    try:
        await ctx.command.invoke(ctx)
    except commands.CommandError as error:
        await handle_error(bot, ctx, error)
    return ctx


async def handle_error(
    bot: commands.Bot, ctx: Context, error: commands.CommandError
) -> None:
    """Handle ``error`` as discord.py does: the cog's handler, then the bot's."""
    await access_cog(bot).cog_command_error(ctx, error)
    await bot_error_handler(ctx, error)


def flags(command: str, **options: Any) -> LimitFlags:
    """/access limit's options, as the slash command builds them: each one
    left out is None.
    """
    built = LimitFlags.__new__(LimitFlags)
    built.command = command
    for name in ('who', 'where', 'private', 'off', 'subcommands'):
        setattr(built, name, options.pop(name, None))
    assert not options, options
    return built


async def limit(
    bot: commands.Bot,
    admin: MagicMock,
    command: str,
    *,
    slash: bool = False,
    **options: Any,
) -> discord.Embed:
    """/access limit command with ``options``, and its answer."""
    place = channel(admin.guild, STAFF_CHANNEL_ID)
    ctx = make_context(bot, 'access limit', admin, place, slash=slash)
    await run(bot, ctx, flags=flags(command, **options))
    return reply(ctx)


async def reset(
    bot: commands.Bot, admin: MagicMock, command: str, *, slash: bool = False
) -> discord.Embed:
    """/access reset command, and its answer."""
    place = channel(admin.guild, STAFF_CHANNEL_ID)
    ctx = make_context(bot, 'access reset', admin, place, slash=slash)
    await run(bot, ctx, command=command)
    return reply(ctx)


async def call(
    bot: commands.Bot, admin: MagicMock, name: str, *args: Any, slash: bool = False
) -> discord.Embed:
    """/``name`` with ``args``, and its answer."""
    place = channel(admin.guild, STAFF_CHANNEL_ID)
    ctx = make_context(bot, name, admin, place, slash=slash)
    await run(bot, ctx, *args)
    return reply(ctx)


def sent(ctx: Context) -> tuple[discord.Embed, dict[str, Any]]:
    """The embed of the one reply, and what else it was sent with."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once()
    assert send.await_args is not None and not send.await_args.args
    options = dict(send.await_args.kwargs)
    embed = options.pop('embed')
    assert isinstance(embed, discord.Embed)
    return embed, options


def reply(ctx: Context) -> discord.Embed:
    """The embed of the one reply, which only the admin sees on slash."""
    embed, options = sent(ctx)
    assert options == {'ephemeral': True}
    return embed


def alert(ctx: Context, *, delete_after: float | None = None) -> str:
    """The text of the one alert that answered ``ctx``, privately on slash,
    and deleted after ``delete_after`` seconds if given.
    """
    embed, options = sent(ctx)
    expected: dict[str, Any] = {'ephemeral': True}
    if delete_after is not None:
        expected['delete_after'] = delete_after
    assert options == expected
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    return str(embed.description)


def lines(embed: discord.Embed) -> list[str]:
    return str(embed.description).split('\n')


def fields(embed: discord.Embed) -> dict[str, str]:
    return {str(field.name): str(field.value) for field in embed.fields}


async def configure(
    bot: AccessBot,
    *,
    bot_channels: tuple[int, ...] = (BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID),
    staff_channel: int | None = STAFF_CHANNEL_ID,
    limits: Mapping[str, Limit] | None = None,
) -> None:
    """Give the server these access settings."""
    access = GuildAccess(frozenset(bot_channels), staff_channel, limits or {})
    await bot.access.change(GUILD_ID, lambda _: access)


def settings(bot: AccessBot) -> GuildAccess:
    return bot.access.guild_access(GUILD_ID)


async def break_settings(bot: AccessBot, user_db: Any) -> None:
    """Store settings that can't be read, as the bot finds them at start-up."""
    await user_db.set_access_settings(GUILD_ID, '{"version": 2}')
    await bot.access.load()
    assert settings(bot).broken


def without_storage(bot: AccessBot) -> None:
    """Keep the settings in memory alone, as under --nodb."""
    bot.access.use_user_db(None)


def logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


both_paths = pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])


# The commands


def payload(bot: commands.Bot) -> dict[str, Any]:
    """What syncing sends Discord for /access."""
    command = bot.tree.get_command('access')
    assert command is not None
    data: dict[str, Any] = command.to_dict(bot.tree)
    return data


def options_of(data: Mapping[str, Any], *path: str) -> dict[str, dict[str, Any]]:
    """The options of the subcommand at ``path`` in ``data``, by name."""
    for name in path:
        data = next(option for option in data['options'] if option['name'] == name)
    return {option['name']: option for option in data['options']}


def walk(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every command, group and option in ``data``."""
    found = [data]
    for option in data.get('options', []):
        found += walk(option)
    return found


async def test_the_cog_has_the_access_commands_of_the_rule_table(
    bot: AccessBot, real_table: None
) -> None:
    names = {command.qualified_name for command in access_cog(bot).walk_commands()}

    assert names == ACCESS_COMMANDS
    assert len(names) == 7
    assert bot.access.report_unruled(access_cog(bot).walk_commands()) == []


async def test_access_needs_manage_server_to_be_seen(bot: AccessBot) -> None:
    data = payload(bot)

    assert data['default_member_permissions'] == (
        discord.Permissions(manage_guild=True).value
    )
    assert list(options_of(data)) == [
        'show',
        'bot-channels',
        'staff-channel',
        'limit',
        'reset',
    ]
    assert list(options_of(data, 'bot-channels')) == ['add', 'remove']


async def test_the_slash_pass_keeps_every_access_command(
    bot: AccessBot, real_table: None
) -> None:
    visibility = apply_visibility(bot)

    assert visibility.hidden['access'] == 'manage_guild'
    assert not [name for name in visibility.removed if name.startswith('access')]
    for name in ACCESS_COMMANDS - {'access bot-channels'}:
        command = bot.get_command(name)
        assert command is not None
        path = '/access show' if name == 'access' else f'/{name}'
        assert bot.access.slash_path(command) == path


async def test_every_text_discord_shows_is_short_and_described(
    bot: AccessBot,
) -> None:
    for item in walk(payload(bot)):
        description = item['description']
        assert description and description != '…', item['name']
        assert len(description) <= 100, item['name']
    for command in access_cog(bot).walk_commands():
        brief = command.brief
        assert brief is not None and len(brief) <= 80, command.qualified_name
        assert brief[0].isupper() and not brief.endswith('.'), brief
        assert command.help and 'Examples:' in command.help, command.qualified_name


def prefix_command(
    bot: commands.Bot, words: list[str]
) -> commands.Command[Any, ..., Any] | None:
    """The command that ``words`` run as a prefix command: the longest start
    of them that names one.
    """
    for end in range(len(words), 0, -1):
        found = bot.get_command(' '.join(words[:end]))
        if found is not None and len(found.qualified_name.split()) == end:
            return found
    return None


def slash_command(bot: commands.Bot, words: list[str]) -> Any:
    """The slash command or group that ``words`` run."""
    found: Any = bot.tree.get_command(words[0])
    for word in words[1:]:
        if not isinstance(found, app_commands.Group):
            break
        found = found.get_command(word)
    return found


async def test_every_example_runs_the_command_it_is_an_example_of(
    bot: AccessBot,
) -> None:
    for command in access_cog(bot).walk_commands():
        assert command.help is not None
        examples = command.help.partition('Examples:')[2].split('\n')
        for example in filter(None, (line.strip() for line in examples)):
            words = example[1:].split()
            if example.startswith('/'):
                found = slash_command(bot, words)
                assert getattr(found, 'wrapped', None) is command, example
            else:
                assert example.startswith(';'), example
                assert prefix_command(bot, words) is command, example


async def test_channels_can_be_text_voice_stage_or_forum_channels(
    bot: AccessBot,
) -> None:
    data = payload(bot)
    # Text and announcement, voice, stage, forum and media channels.
    kinds = [0, 5, 2, 13, 15, 16]

    for path in (('bot-channels', 'add'), ('bot-channels', 'remove')):
        option = options_of(data, *path)['channel']
        assert option['channel_types'] == kinds
        assert option['required'] is True
    staff = options_of(data, 'staff-channel')['channel']
    assert staff['channel_types'] == kinds
    assert staff['required'] is False


async def test_limit_takes_what_a_limit_can_set(bot: AccessBot) -> None:
    options = options_of(payload(bot), 'limit')

    assert list(options) == ['command', 'who', 'where', 'private', 'off', 'subcommands']
    assert options['command']['required'] is True
    assert options['command']['autocomplete'] is True
    assert [choice['value'] for choice in options['who']['choices']] == [
        'trusted',
        'moderator',
        'developer',
        'admin',
    ]
    assert [choice['value'] for choice in options['who']['choices']] == [
        who.value for who in LIMIT_WHO
    ]
    assert [choice['value'] for choice in options['where']['choices']] == [
        where.value for where in LIMIT_WHERE
    ]
    for name in ('who', 'where', 'private', 'off', 'subcommands'):
        assert options[name]['required'] is False, name
    for name in ('private', 'off', 'subcommands'):
        assert options[name]['type'] == discord.AppCommandOptionType.boolean.value


async def test_reset_takes_a_command_it_suggests(bot: AccessBot) -> None:
    option = options_of(payload(bot), 'reset')['command']

    assert option['required'] is True
    assert option['autocomplete'] is True


# Who may use /access


@both_paths
async def test_members_cannot_use_access(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    member = make_member(guild, 'Moderator', 'Trusted')
    ctx = make_context(
        bot, 'access limit', member, channel(guild, STAFF_CHANNEL_ID), slash=slash
    )

    with pytest.raises(AccessDenied) as refused:
        await ctx.command.can_run(ctx)  # type: ignore[union-attr]

    if slash:
        assert refused.value.text == NOT_ALLOWED_MESSAGE
    else:
        assert refused.value.silent


@pytest.mark.parametrize(
    'roles, manage_guild',
    [(('Admin',), False), ((), True)],
    ids=['admin-role', 'manage-server'],
)
async def test_admins_can_use_access(
    bot: AccessBot, guild: MagicMock, roles: tuple[str, ...], manage_guild: bool
) -> None:
    await configure(bot)
    admin = make_member(guild, *roles, manage_guild=manage_guild)
    ctx = make_context(bot, 'access limit', admin, channel(guild, STAFF_CHANNEL_ID))

    assert await ctx.command.can_run(ctx)  # type: ignore[union-attr]


async def test_the_cog_lets_in_admins_alone_itself(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    place = channel(guild, STAFF_CHANNEL_ID)
    cog = access_cog(bot)

    assert await cog.cog_check(make_context(bot, 'access', admin, place))
    assert await cog.cog_check(
        make_context(bot, 'access', make_member(guild, 'Admin'), place)
    )
    member = make_member(guild, 'Moderator')
    assert not await cog.cog_check(make_context(bot, 'access', member, place))
    user = MagicMock(spec=discord.User, id=MEMBER_ID, guild=guild)
    assert not await cog.cog_check(make_context(bot, 'access', user, place))
    del bot.access
    assert not await cog.cog_check(make_context(bot, 'access', admin, place))


async def test_access_by_prefix_works_anywhere_until_there_is_a_staff_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    general = channel(guild, GENERAL_ID)
    await configure(bot, staff_channel=None)

    # ;access itself says how to see the settings (see the next test), and its
    # subcommands work.
    ctx = await invoke(bot, 'access', admin, general)
    assert reply(ctx).description == NO_STAFF_CHANNEL_SHOW_TEXT
    ctx = await invoke(bot, 'access bot-channels', admin, general)
    assert reply(ctx).title == 'Bot channels'

    await configure(bot)
    ctx = await invoke(bot, 'access', admin, general)
    assert alert(ctx, delete_after=REFUSAL_DELETE_AFTER) == (
        f'{STAFF_CHANNEL_TEXT} {SLASH_HINT.format(path="/access show")}'
    )
    ctx = await invoke(bot, 'access', admin, channel(guild, STAFF_CHANNEL_ID))
    cast(AsyncMock, ctx.send).assert_awaited_once_with(
        None, embed=ANY, delete_after=None, ephemeral=True
    )


async def test_access_by_prefix_shows_no_settings_until_there_is_a_staff_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    # Its answer is public, and could be in any channel: it points to the
    # private slash command and to the staff channel instead.
    await configure(bot, staff_channel=None, limits={'gitgud': Limit(off=True)})

    ctx = await invoke(bot, 'access', admin, channel(guild, GENERAL_ID))

    embed = reply(ctx)
    assert embed.description == NO_STAFF_CHANNEL_SHOW_TEXT
    assert embed.title is None and embed.fields == []
    assert '`/access show`, which answers only you' in NO_STAFF_CHANNEL_SHOW_TEXT
    assert '`/access staff-channel`' in NO_STAFF_CHANNEL_SHOW_TEXT
    # The slash command shows them, to the admin alone.
    embed = await show(bot, admin, slash=True)
    assert fields(embed)['Limits'] == '`gitgud`: switched off.'


# /access show


def shown(ctx: Context) -> discord.Embed:
    """The first page of /access show's answer, a single page that only the
    admin sees on slash.
    """
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(None, embed=ANY, delete_after=None, ephemeral=True)
    assert send.await_args is not None
    embed = send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    assert embed.title == 'Access settings'
    return embed


async def show(
    bot: AccessBot, admin: MagicMock, *, slash: bool = False
) -> discord.Embed:
    ctx = make_context(
        bot, 'access', admin, channel(admin.guild, STAFF_CHANNEL_ID), slash=slash
    )
    await run(bot, ctx)
    return shown(ctx)


def warnings_of(embed: discord.Embed) -> list[str]:
    return [] if embed.description is None else lines(embed)


@both_paths
async def test_show_lists_the_channels_the_developer_role_and_the_limits(
    bot: AccessBot, admin: MagicMock, slash: bool
) -> None:
    await configure(
        bot,
        limits={
            'duel register': Limit(off=True),
            'clist *': Limit(who=Who.TRUSTED, where=Where.BOT_ONLY),
            'gitgud': Limit(private=True),
        },
    )

    embed = await show(bot, admin, slash=slash)

    assert embed.description is None  # nothing to warn about
    assert fields(embed) == {
        'Bot channels': f'<#{BOT_CHANNEL_ID}>, <#{SECOND_BOT_CHANNEL_ID}>',
        'Staff channel': f'<#{STAFF_CHANNEL_ID}>',
        'Developer role': f'<@&{DEVELOPER_ROLE_ID}>, from `.env`',
        'Limits': (
            '`clist` and its subcommands: for trusted members, moderators and '
            'admins only; in bot channels only.\n'
            '`duel register`: switched off.\n'
            '`gitgud`: answers only the person who uses it.'
        ),
    }


async def test_show_of_a_server_with_nothing_set_up_warns_of_it(
    bot: AccessBot, admin: MagicMock
) -> None:
    # On slash: ;access shows nothing until there is a staff channel.
    embed = await show(bot, admin, slash=True)

    assert warnings_of(embed) == [
        NO_BOT_CHANNEL_WARNING.format(where=''),
        NO_STAFF_CHANNEL_WARNING,
    ]
    assert warnings_of(embed)[0] == (
        "**Warning:** there is no bot channel yet, so member commands don't work "
        'with `;`, and slash commands answer only the person who uses them. Add '
        'one with `/access bot-channels add`.'
    )
    assert fields(embed) == {
        'Bot channels': 'none yet',
        'Staff channel': 'none yet',
        'Developer role': f'<@&{DEVELOPER_ROLE_ID}>, from `.env`',
        'Limits': 'none',
    }


async def test_show_warns_that_member_commands_work_only_in_the_staff_channel(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, bot_channels=())

    embed = await show(bot, admin)

    assert warnings_of(embed) == [
        NO_BOT_CHANNEL_WARNING.format(where=OUTSIDE_STAFF_CHANNEL)
    ]
    assert 'so outside the staff channel, member commands' in warnings_of(embed)[0]


async def test_show_warns_when_everyone_can_read_the_staff_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    make_channel(guild, STAFF_CHANNEL_ID, public=True)
    await configure(bot)

    embed = await show(bot, admin)

    assert warnings_of(embed) == [
        PUBLIC_STAFF_CHANNEL_WARNING.format(channel=f'<#{STAFF_CHANNEL_ID}>')
    ]


async def test_show_warns_when_the_staff_channel_is_gone(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, staff_channel=GONE_ID)

    # On slash: ;access shows nothing once the staff channel is gone.
    embed = await show(bot, admin, slash=True)

    assert warnings_of(embed) == [STAFF_CHANNEL_GONE_WARNING]
    assert fields(embed)['Staff channel'] == f'<#{GONE_ID}>'


async def test_access_by_prefix_shows_no_settings_once_the_staff_channel_is_gone(
    bot: AccessBot, guild: MagicMock
) -> None:
    # The access rules then let ;access work in any channel again, so that an
    # admin by role alone can set another: its answer would be public.
    known_to(bot, guild)
    await configure(bot, staff_channel=GONE_ID, limits={'gitgud': Limit(off=True)})
    admin = make_member(guild, 'Admin')

    ctx = await invoke(bot, 'access', admin, channel(guild, GENERAL_ID))

    embed = reply(ctx)
    assert embed.description == STAFF_CHANNEL_GONE_SHOW_TEXT
    assert embed.title is None and embed.fields == []
    assert STAFF_CHANNEL_GONE_SHOW_TEXT.startswith(
        "The staff channel no longer exists, so `;access` won't show this "
        "server's settings"
    )


async def test_an_admin_by_role_sets_another_staff_channel_once_it_is_gone(
    bot: AccessBot, guild: MagicMock
) -> None:
    known_to(bot, guild)
    await configure(bot, staff_channel=GONE_ID)
    admin = make_member(guild, 'Admin')
    general = channel(guild, GENERAL_ID)

    ctx = await invoke(bot, 'access staff-channel', admin, general, f'<#{GENERAL_ID}>')

    assert settings(bot).staff_channel == GENERAL_ID
    # ;access worked in any channel until now.
    assert lines(reply(ctx)) == [
        STAFF_CHANNEL_SET_TEXT.format(channel=f'<#{GENERAL_ID}>'),
        ACCESS_MOVED_TEXT,
        PUBLIC_STAFF_CHANNEL_WARNING.format(channel=f'<#{GENERAL_ID}>'),
    ]


@pytest.mark.parametrize(
    'gone, warning',
    [
        ((GONE_ID,), BOT_CHANNEL_GONE_WARNING),
        ((GONE_ID, OTHER_GONE_ID), BOT_CHANNELS_GONE_WARNING.format(count=2)),
    ],
    ids=['one', 'two'],
)
async def test_show_warns_of_bot_channels_that_are_gone(
    bot: AccessBot, admin: MagicMock, gone: tuple[int, ...], warning: str
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, *gone))

    embed = await show(bot, admin)

    assert warnings_of(embed) == [warning]


@pytest.mark.parametrize(
    'setting, value, role, kind',
    [
        ('TLE_ADMIN', 'Committee', 'admin', 'missing'),
        ('TLE_MODERATOR', 'Mods', 'moderator', 'missing'),
        ('TLE_TRUSTED', 'Regulars', 'trusted', 'missing'),
        ('TLE_ADMIN', 'Shared', 'admin', 'shared'),
        ('TLE_TRUSTED', 'Shared', 'trusted', 'shared'),
        ('TLE_ADMIN', MISSING_ROLE_ID, 'admin', 'missing id'),
        ('TLE_DEVELOPER', MISSING_ROLE_ID, 'developer', 'missing id'),
    ],
)
async def test_show_warns_of_tles_roles_that_match_no_role_or_several(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str | int,
    role: str,
    kind: str,
) -> None:
    guild.roles += [make_role('Shared', 1), make_role('Shared', 2)]
    monkeypatch.setattr(constants, setting, value)
    await configure(bot)

    embed = await show(bot, admin)

    if kind == 'missing':
        text = ROLE_NAME_MISSING_WARNING.format(setting=setting, role=role)
    elif kind == 'shared':
        text = ROLE_NAME_SHARED_WARNING.format(setting=setting, count=2, role=role)
    else:
        text = ROLE_ID_MISSING_WARNING.format(setting=setting, role=role)
    assert warnings_of(embed) == [text]
    assert str(value) not in text.replace(setting, '')
    # Spelt as the README and .env.example spell it.
    assert ' ID ' in text and ' id ' not in text


@pytest.mark.parametrize(
    'setting, value, role',
    [
        ('TLE_ADMIN', GUILD_ID, 'admin'),
        ('TLE_ADMIN', '@everyone', 'admin'),
        ('TLE_MODERATOR', GUILD_ID, 'moderator'),
        ('TLE_TRUSTED', '@everyone', 'trusted'),
        ('TLE_DEVELOPER', GUILD_ID, 'developer'),
    ],
)
async def test_show_warns_of_tles_roles_that_name_everyone(
    bot: AccessBot,
    admin: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str | int,
    role: str,
) -> None:
    # The server's default role, by its id (the server's) or its name: every
    # member has it, so it never counts as one of TLE's roles.
    monkeypatch.setattr(constants, setting, value)
    await configure(bot)

    embed = await show(bot, admin)

    assert warnings_of(embed) == [
        EVERYONE_ROLE_WARNING.format(setting=setting, role=role)
    ]
    assert warnings_of(embed)[0].endswith(
        f'names @everyone, so it is ignored and nobody has the {role} role through '
        'it. In `.env`, set it to the ID of the role you want.'
    )
    if setting == 'TLE_DEVELOPER':
        assert fields(embed)['Developer role'] == (
            'none: `TLE_DEVELOPER` names @everyone'
        )


async def test_show_names_no_developer_role_when_there_is_none(
    bot: AccessBot, admin: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await configure(bot)
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)

    unset = await show(bot, admin)
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', MISSING_ROLE_ID)
    elsewhere = await show(bot, admin)

    assert warnings_of(unset) == []
    assert (
        fields(unset)['Developer role'] == "none: `TLE_DEVELOPER` isn't set in `.env`"
    )
    assert fields(elsewhere)['Developer role'] == 'none in this server'


async def test_show_notes_that_any_server_can_use_the_bot(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)
    bot.access.allowed_guilds = frozenset()

    embed = await show(bot, admin)

    assert warnings_of(embed) == [ANY_SERVER_NOTE]


async def test_show_warns_that_changes_are_not_stored_without_a_database(
    bot: AccessBot, admin: MagicMock
) -> None:
    without_storage(bot)
    await configure(bot)

    embed = await show(bot, admin)

    assert warnings_of(embed) == [NOT_STORED_WARNING]


async def test_show_warns_when_ratedvc_works_only_outside_the_bot_channels(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot)
    await user_db.set_rated_vc_channel(GUILD_ID, RATED_VC_ID)

    embed = await show(bot, admin)

    assert warnings_of(embed) == [RATED_VC_WARNING.format(channel=f'<#{RATED_VC_ID}>')]


async def test_show_does_not_warn_when_ratedvc_works_in_its_channel(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot)
    await user_db.set_rated_vc_channel(GUILD_ID, SECOND_BOT_CHANNEL_ID)

    embed = await show(bot, admin)

    assert warnings_of(embed) == []


async def test_show_warns_when_ratedvc_works_only_in_the_staff_channel(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    # It counts as a bot channel, so ;ratedvc works there, but only for staff:
    # members can't read it.
    await configure(bot)
    await user_db.set_rated_vc_channel(GUILD_ID, STAFF_CHANNEL_ID)

    embed = await show(bot, admin)

    assert warnings_of(embed) == [
        RATED_VC_STAFF_WARNING.format(channel=f'<#{STAFF_CHANNEL_ID}>')
    ]


@pytest.mark.parametrize(
    'parent, warning, named',
    [
        (SECOND_BOT_CHANNEL_ID, None, None),
        (STAFF_CHANNEL_ID, RATED_VC_STAFF_WARNING, STAFF_CHANNEL_ID),
        (GENERAL_ID, RATED_VC_WARNING, THREAD_ID),
    ],
    ids=['bot channel', 'staff channel', 'elsewhere'],
)
async def test_show_counts_a_ratedvc_thread_as_the_channel_it_is_in(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    user_db: Any,
    parent: int,
    warning: str | None,
    named: int | None,
) -> None:
    # As the access rules count it: ;ratedvc works in the thread exactly when
    # the thread's channel is a bot channel, which members can read unless it
    # is the staff channel. (A thread can no longer be chosen, but one chosen
    # earlier stays.)
    make_thread(guild, THREAD_ID, parent)
    await configure(bot)
    await user_db.set_rated_vc_channel(GUILD_ID, THREAD_ID)

    embed = await show(bot, admin)

    expected = [] if warning is None else [warning.format(channel=f'<#{named}>')]
    assert warnings_of(embed) == expected


async def test_show_carries_on_if_the_database_cannot_name_the_ratedvc_channel(
    bot: AccessBot, admin: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    await configure(bot)
    bot.user_db = MagicMock()
    bot.user_db.get_rated_vc_channel = AsyncMock(
        side_effect=sqlite3.OperationalError('database is locked')
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        embed = await show(bot, admin)

    assert warnings_of(embed) == []
    assert logged(caplog) == [
        f'Could not read the rated vc channel of guild {GUILD_ID}: database is locked'
    ]


async def test_show_without_the_user_database_says_nothing_of_ratedvc(
    bot: AccessBot, admin: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    await configure(bot)
    bot.user_db = db.DummyUserDbConn()  # as under --nodb

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        embed = await show(bot, admin)

    assert warnings_of(embed) == []
    assert logged(caplog) == []


async def test_show_of_broken_settings_says_how_to_repair_them(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await break_settings(bot, user_db)

    # On slash: broken settings have no staff channel, so ;access shows nothing.
    embed = await show(bot, admin, slash=True)

    assert warnings_of(embed) == [BROKEN_WARNING]
    assert fields(embed) == {}


async def test_show_marks_limits_of_commands_the_bot_no_longer_has(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(
        bot,
        limits={
            'gitgud extra': Limit(off=True),  # gitgud has no subcommands
            'old *': Limit(who=Who.ADMIN),
        },
    )

    embed = await show(bot, admin)

    assert fields(embed)['Limits'] == (
        '`gitgud extra`: switched off (no such command now).\n'
        '`old` and its subcommands: for admins only (no such command now).'
    )


async def test_show_puts_many_limits_on_pages_only_the_admin_can_turn(
    bot: AccessBot, admin: MagicMock
) -> None:
    every_option = Limit(Who.TRUSTED, Where.STAFF_ONLY, private=True, off=True)
    keys = [f'command{number:03} *' for number in range(80)]
    await configure(bot, limits=dict.fromkeys(keys, every_option))
    ctx = make_context(bot, 'access', admin, channel(admin.guild, STAFF_CHANNEL_ID))

    await run(bot, ctx)

    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(
        None, embed=ANY, view=ANY, delete_after=None, ephemeral=True
    )
    assert send.await_args is not None
    view = send.await_args.kwargs['view']
    assert isinstance(view, PaginatorView)
    assert view.owner_id == admin.id
    first, *rest = [embed for _, embed in view.pages]
    assert send.await_args.kwargs['embed'] is first
    assert fields(first)['Limits'] == '80, on the next pages'
    assert len(rest) > 1
    listed = [line for page in rest for line in lines(page)]
    assert [line.split('`')[1] for line in listed] == [key[:-2] for key in keys]
    for page in rest:
        assert page.title == 'Limits'
        assert len(str(page.description)) <= 4096
        assert len(page) <= 6000


# /access bot-channels


@both_paths
async def test_add_makes_a_channel_a_bot_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, slash: bool
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID,))

    embed = await call(
        bot,
        admin,
        'access bot-channels add',
        channel(guild, SECOND_BOT_CHANNEL_ID),
        slash=slash,
    )

    assert settings(bot).bot_channels == {BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}
    assert embed.title == 'Bot channel added'
    assert lines(embed) == [
        BOT_CHANNEL_ADDED_TEXT.format(channel=f'<#{SECOND_BOT_CHANNEL_ID}>'),
        BOT_CHANNELS_TEXT.format(
            channels=f'<#{BOT_CHANNEL_ID}>, <#{SECOND_BOT_CHANNEL_ID}>'
        ),
    ]


@pytest.mark.parametrize(
    'kind', [discord.VoiceChannel, discord.StageChannel, discord.ForumChannel]
)
async def test_voice_stage_and_forum_channels_can_be_bot_channels(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    kind: type[discord.abc.GuildChannel],
) -> None:
    place = make_channel(guild, SPARE_CHANNELS_ID, kind=kind)

    await call(bot, admin, 'access bot-channels add', place)

    assert settings(bot).bot_channels == {SPARE_CHANNELS_ID}


async def test_adding_a_bot_channel_again_changes_nothing(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot)
    stored = await user_db.get_all_access_settings()

    embed = await call(
        bot, admin, 'access bot-channels add', channel(guild, BOT_CHANNEL_ID)
    )

    assert embed.title == 'Nothing changed'
    assert lines(embed) == [
        ALREADY_BOT_CHANNEL_TEXT.format(channel=f'<#{BOT_CHANNEL_ID}>')
    ]
    assert await user_db.get_all_access_settings() == stored


async def test_a_server_has_at_most_25_bot_channels(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    spare = [
        make_channel(guild, SPARE_CHANNELS_ID + number)
        for number in range(MAX_BOT_CHANNELS + 1)
    ]
    await configure(
        bot, bot_channels=tuple(place.id for place in spare[: MAX_BOT_CHANNELS - 1])
    )

    await call(bot, admin, 'access bot-channels add', spare[MAX_BOT_CHANNELS - 1])
    assert len(settings(bot).bot_channels) == MAX_BOT_CHANNELS == 25

    full = settings(bot)
    with pytest.raises(AccessCogError) as refused:
        await call(bot, admin, 'access bot-channels add', spare[MAX_BOT_CHANNELS])
    assert str(refused.value) == TOO_MANY_BOT_CHANNELS_TEXT.format(count=25)
    assert settings(bot) == full


@pytest.mark.parametrize(
    'gone, tidied',
    [
        ((GONE_ID,), TIDIED_ONE_TEXT),
        ((GONE_ID, OTHER_GONE_ID), TIDIED_TEXT.format(count=2)),
    ],
    ids=['one', 'two'],
)
async def test_adding_a_bot_channel_tidies_away_those_that_are_gone(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    gone: tuple[int, ...],
    tidied: str,
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, *gone))

    embed = await call(
        bot, admin, 'access bot-channels add', channel(guild, SECOND_BOT_CHANNEL_ID)
    )

    assert settings(bot).bot_channels == {BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}
    assert lines(embed)[1] == tidied


async def test_channels_that_are_gone_leave_room_for_another(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    spare = [
        make_channel(guild, SPARE_CHANNELS_ID + number)
        for number in range(MAX_BOT_CHANNELS)
    ]
    kept = tuple(place.id for place in spare[: MAX_BOT_CHANNELS - 1])
    await configure(bot, bot_channels=(*kept, GONE_ID))

    await call(bot, admin, 'access bot-channels add', spare[-1])

    assert settings(bot).bot_channels == {*kept, spare[-1].id}


@both_paths
async def test_remove_makes_a_bot_channel_an_ordinary_one(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, slash: bool
) -> None:
    await configure(bot)

    embed = await call(
        bot,
        admin,
        'access bot-channels remove',
        channel(guild, BOT_CHANNEL_ID),
        slash=slash,
    )

    assert settings(bot).bot_channels == {SECOND_BOT_CHANNEL_ID}
    assert embed.title == 'Bot channel removed'
    assert lines(embed) == [
        BOT_CHANNEL_REMOVED_TEXT.format(channel=f'<#{BOT_CHANNEL_ID}>'),
        BOT_CHANNELS_TEXT.format(channels=f'<#{SECOND_BOT_CHANNEL_ID}>'),
    ]


async def test_removing_a_bot_channel_tidies_away_those_that_are_gone(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID, GONE_ID))

    embed = await call(
        bot, admin, 'access bot-channels remove', channel(guild, BOT_CHANNEL_ID)
    )

    assert settings(bot).bot_channels == {SECOND_BOT_CHANNEL_ID}
    assert lines(embed)[1] == TIDIED_ONE_TEXT


async def test_removing_a_channel_that_is_no_bot_channel_changes_nothing(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    general = await call(
        bot, admin, 'access bot-channels remove', channel(guild, GENERAL_ID)
    )
    staff = await call(
        bot, admin, 'access bot-channels remove', channel(guild, STAFF_CHANNEL_ID)
    )

    assert general.title == staff.title == 'Nothing changed'
    assert lines(general) == [NOT_BOT_CHANNEL_TEXT.format(channel=f'<#{GENERAL_ID}>')]
    assert lines(staff) == [
        NOT_BOT_CHANNEL_TEXT.format(channel=f'<#{STAFF_CHANNEL_ID}>'),
        STAFF_COUNTS_TEXT,
    ]
    assert settings(bot).bot_channels == {BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}


@pytest.mark.parametrize(
    'staff_channel, where',
    [(STAFF_CHANNEL_ID, OUTSIDE_STAFF_CHANNEL), (None, '')],
    ids=['staff-channel', 'none'],
)
async def test_removing_the_last_bot_channel_warns(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    staff_channel: int | None,
    where: str,
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID,), staff_channel=staff_channel)

    embed = await call(
        bot, admin, 'access bot-channels remove', channel(guild, BOT_CHANNEL_ID)
    )

    assert lines(embed)[-1] == NO_BOT_CHANNEL_WARNING.format(where=where)


async def test_removing_the_ratedvc_channel_warns(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, RATED_VC_ID))
    await user_db.set_rated_vc_channel(GUILD_ID, RATED_VC_ID)

    embed = await call(
        bot, admin, 'access bot-channels remove', channel(guild, RATED_VC_ID)
    )

    assert lines(embed)[-1] == RATED_VC_REMOVED_WARNING.format(
        channel=f'<#{RATED_VC_ID}>'
    )


async def test_removing_the_channel_of_a_ratedvc_thread_warns(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    make_thread(guild, THREAD_ID, RATED_VC_ID)
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, RATED_VC_ID))
    await user_db.set_rated_vc_channel(GUILD_ID, THREAD_ID)

    embed = await call(
        bot, admin, 'access bot-channels remove', channel(guild, RATED_VC_ID)
    )

    assert lines(embed)[-1] == RATED_VC_REMOVED_WARNING.format(
        channel=f'<#{RATED_VC_ID}>'
    )


async def test_removing_another_channel_or_the_staff_channel_does_not_warn_of_ratedvc(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID, STAFF_CHANNEL_ID, RATED_VC_ID))
    await user_db.set_rated_vc_channel(GUILD_ID, STAFF_CHANNEL_ID)

    other = await call(
        bot, admin, 'access bot-channels remove', channel(guild, RATED_VC_ID)
    )
    staff = await call(
        bot, admin, 'access bot-channels remove', channel(guild, STAFF_CHANNEL_ID)
    )

    assert not any('ratedvc' in line for line in lines(other) + lines(staff))
    assert settings(bot).bot_channels == {BOT_CHANNEL_ID}


async def test_removing_a_bot_channel_carries_on_if_the_database_cannot_say(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)
    bot.user_db = db.DummyUserDbConn()  # as under --nodb

    embed = await call(
        bot, admin, 'access bot-channels remove', channel(guild, BOT_CHANNEL_ID)
    )

    assert embed.title == 'Bot channel removed'
    assert settings(bot).bot_channels == {SECOND_BOT_CHANNEL_ID}


async def test_bot_channels_by_prefix_lists_them(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    ctx = await invoke(
        bot, 'access bot-channels', admin, channel(guild, STAFF_CHANNEL_ID)
    )

    embed = reply(ctx)
    assert embed.title == 'Bot channels'
    assert lines(embed)[0] == BOT_CHANNELS_TEXT.format(
        channels=f'<#{BOT_CHANNEL_ID}>, <#{SECOND_BOT_CHANNEL_ID}>'
    )


@pytest.mark.parametrize(
    'arguments',
    [f'<#{SECOND_BOT_CHANNEL_ID}>', str(SECOND_BOT_CHANNEL_ID), 'channel-2'],
    ids=['mention', 'id', 'name'],
)
async def test_add_by_prefix_finds_the_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, arguments: str
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID,))

    ctx = await invoke(
        bot,
        'access bot-channels add',
        admin,
        channel(guild, STAFF_CHANNEL_ID),
        arguments,
    )

    assert reply(ctx).title == 'Bot channel added'
    assert settings(bot).bot_channels == {BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}


@pytest.mark.parametrize(
    'arguments, text',
    [
        (
            'nowhere',
            "I can't find that channel here. Name a text, voice, stage or forum "
            'channel, as in `;access bot-channels add #bot-commands`.',
        ),
        ('', 'Name a channel, as in `;access bot-channels add #bot-commands`.'),
    ],
    ids=['unknown', 'missing'],
)
async def test_add_by_prefix_without_a_channel_it_finds_explains(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, arguments: str, text: str
) -> None:
    await configure(bot, bot_channels=(BOT_CHANNEL_ID,))

    ctx = await invoke(
        bot,
        'access bot-channels add',
        admin,
        channel(guild, STAFF_CHANNEL_ID),
        arguments,
    )

    assert alert(ctx) == text
    assert settings(bot).bot_channels == {BOT_CHANNEL_ID}


# /access staff-channel


@both_paths
async def test_staff_channel_sets_it(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, slash: bool
) -> None:
    await configure(bot, staff_channel=None)

    embed = await call(
        bot,
        admin,
        'access staff-channel',
        channel(guild, STAFF_CHANNEL_ID),
        slash=slash,
    )

    assert settings(bot).staff_channel == STAFF_CHANNEL_ID
    assert embed.title == 'Staff channel set'
    assert lines(embed) == [
        STAFF_CHANNEL_SET_TEXT.format(channel=f'<#{STAFF_CHANNEL_ID}>'),
        ACCESS_MOVED_TEXT,
    ]


async def test_moving_the_staff_channel_does_not_say_access_moved(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    other = make_channel(guild, SPARE_CHANNELS_ID, public=False)
    await configure(bot)

    embed = await call(bot, admin, 'access staff-channel', other)

    assert settings(bot).staff_channel == SPARE_CHANNELS_ID
    assert lines(embed) == [STAFF_CHANNEL_SET_TEXT.format(channel=other.mention)]


async def test_a_staff_channel_everyone_can_read_gets_a_warning(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await call(bot, admin, 'access staff-channel', channel(guild, GENERAL_ID))

    assert settings(bot).staff_channel == GENERAL_ID
    assert lines(embed)[-1] == PUBLIC_STAFF_CHANNEL_WARNING.format(
        channel=f'<#{GENERAL_ID}>'
    )


async def test_the_same_staff_channel_changes_nothing(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await call(
        bot, admin, 'access staff-channel', channel(guild, STAFF_CHANNEL_ID)
    )

    assert embed.title == 'Nothing changed'
    assert lines(embed) == [
        SAME_STAFF_CHANNEL_TEXT.format(channel=f'<#{STAFF_CHANNEL_ID}>')
    ]


@both_paths
async def test_staff_channel_without_a_channel_clears_it(
    bot: AccessBot, admin: MagicMock, slash: bool
) -> None:
    await configure(bot)

    embed = await call(bot, admin, 'access staff-channel', None, slash=slash)

    assert settings(bot).staff_channel is None
    assert embed.title == 'Staff channel cleared'
    assert lines(embed) == [STAFF_CHANNEL_CLEARED_TEXT]


async def test_clearing_no_staff_channel_changes_nothing(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, staff_channel=None)

    embed = await call(bot, admin, 'access staff-channel', None)

    assert embed.title == 'Nothing changed'
    assert lines(embed) == [NO_STAFF_CHANNEL_TEXT]


async def test_staff_channel_by_prefix_without_a_channel_clears_it(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    ctx = await invoke(
        bot, 'access staff-channel', admin, channel(guild, STAFF_CHANNEL_ID)
    )

    assert reply(ctx).title == 'Staff channel cleared'
    assert settings(bot).staff_channel is None


async def test_staff_channel_by_prefix_with_a_channel_it_cannot_find_keeps_it(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    # Text that names no channel must not read as no channel, which would
    # clear the staff channel.
    await configure(bot)

    ctx = await invoke(
        bot,
        'access staff-channel',
        admin,
        channel(guild, STAFF_CHANNEL_ID),
        'staff',
    )

    assert alert(ctx) == (
        "I can't find that channel here. Name a text, voice, stage or forum "
        'channel, as in `;access staff-channel #staff`.'
    )
    assert settings(bot).staff_channel == STAFF_CHANNEL_ID


async def test_staff_channel_by_prefix_finds_the_channel(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    other = make_channel(guild, SPARE_CHANNELS_ID, public=False)
    await configure(bot)

    ctx = await invoke(
        bot,
        'access staff-channel',
        admin,
        channel(guild, STAFF_CHANNEL_ID),
        other.mention,
    )

    assert reply(ctx).title == 'Staff channel set'
    assert settings(bot).staff_channel == SPARE_CHANNELS_ID


# /access limit


@both_paths
async def test_limit_limits_a_command(
    bot: AccessBot, admin: MagicMock, slash: bool
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'gitgud', where='bot-only', slash=slash)

    assert settings(bot).limits == {'gitgud': Limit(where=Where.BOT_ONLY)}
    assert embed.title == 'Limit set'
    assert lines(embed) == [
        '`gitgud`: in bot channels only.',
        SLASH_LIST_NOTE,
    ]
    assert fields(embed) == {'Everyone · Bot channels only': '`gitgud`'}


async def test_a_limit_on_a_group_covers_its_subcommands(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'duel', who='trusted')

    assert settings(bot).limits == {'duel *': Limit(who=Who.TRUSTED)}
    assert lines(embed)[0] == (
        '`duel` and its subcommands: for trusted members, moderators and admins only.'
    )
    assert fields(embed) == {
        'Trusted members, moderators and admins · Bot channels; elsewhere the '
        'slash command answers only you': '`duel` (`/duel show`)',
        'Trusted members, moderators and admins · Bot channels only': (
            '`duel challenge`'
        ),
        'Moderators and admins · Bot channels; elsewhere the slash command '
        'answers only you': '`duel register`',
    }


async def test_a_limit_on_a_group_without_its_subcommands_covers_its_own_command(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'duel', subcommands=False, off=True)

    assert settings(bot).limits == {'duel': Limit(off=True)}
    assert lines(embed)[0] == '`duel`: switched off.'
    assert fields(embed) == {'Switched off': '`duel` (`/duel show`)'}


async def test_a_fallback_names_the_groups_own_command(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'clist show', off=True)

    assert settings(bot).limits == {'clist': Limit(off=True)}
    assert fields(embed) == {'Switched off': '`clist` (`/clist show`)'}


async def test_a_fallback_with_subcommands_covers_the_whole_group(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'clist show', subcommands=True, off=True)

    assert settings(bot).limits == {'clist *': Limit(off=True)}
    assert fields(embed) == {'Switched off': '`clist` (`/clist show`), `clist future`'}


async def test_a_prefix_twin_shares_its_groups_limit(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'contests upcoming', who='trusted')

    assert settings(bot).limits == {'contests': Limit(who=Who.TRUSTED)}
    assert list(fields(embed).values()) == [
        '`contests` (`/contests upcoming`), `contests upcoming`'
    ]


async def test_a_prefix_twin_with_subcommands_covers_the_whole_group(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'contests upcoming', subcommands=True, off=True)

    assert settings(bot).limits == {'contests *': Limit(off=True)}
    assert fields(embed) == {
        'Switched off': (
            '`contests` (`/contests upcoming`), `contests live`, `contests upcoming`'
        )
    }


async def test_the_handle_group_is_the_twin_of_handle_show(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    whole = await limit(bot, admin, 'handle', who='trusted')
    own = await limit(bot, admin, 'handle', subcommands=False, off=True)
    show = await limit(bot, admin, 'handle show', who='moderator')

    assert settings(bot).limits == {
        'handle *': Limit(who=Who.TRUSTED),
        'handle show': Limit(who=Who.MODERATOR, off=True),
    }
    assert lines(whole)[0].startswith('`handle` and its subcommands:')
    assert fields(own) == {'Switched off': '`handle` (`/handle show`), `handle show`'}
    assert lines(show)[0] == (
        '`handle show`: switched off; for moderators and admins only.'
    )


@pytest.mark.parametrize(
    'typed',
    [
        'Duel Register',
        '  duel   register ',
        '/duel register',
        ';duel register',
        '"duel register"',
        '`duel register`',
        "'duel register'",
        '; "duel register"',
    ],
)
async def test_names_may_be_typed_loosely(
    bot: AccessBot, admin: MagicMock, typed: str
) -> None:
    await configure(bot)

    await limit(bot, admin, typed, off=True)

    assert settings(bot).limits == {'duel register': Limit(off=True)}


async def test_each_limit_merges_what_it_gives_into_the_commands_limit(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    first = await limit(bot, admin, 'gitgud', who='trusted')
    second = await limit(bot, admin, 'gitgud', where='staff', private=True)
    third = await limit(bot, admin, 'gitgud', who='developer', private=False)

    assert first.title == 'Limit set'
    assert second.title == third.title == 'Limit changed'
    assert settings(bot).limits == {
        'gitgud': Limit(who=Who.DEVELOPER, where=Where.STAFF)
    }
    assert lines(third)[0] == (
        '`gitgud`: for developers and admins only; in the staff channel.'
    )


async def test_a_limit_left_with_nothing_is_removed(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(off=True)})

    embed = await limit(bot, admin, 'gitgud', off=False)

    assert settings(bot).limits == {}
    assert embed.title == 'Limit removed'
    assert lines(embed)[0] == NO_LIMIT_TEXT.format(key='`gitgud`')


async def test_a_limit_that_changes_nothing_stored_says_so(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot, limits={'gitgud': Limit(who=Who.TRUSTED)})
    stored = await user_db.get_all_access_settings()

    same = await limit(bot, admin, 'gitgud', who='trusted', private=False)
    nothing = await limit(bot, admin, 'gitgud')
    unlimited = await limit(bot, admin, 'gimme', off=False)

    assert same.title == nothing.title == unlimited.title == 'Nothing changed'
    described = 'for trusted members, moderators and admins only'
    assert lines(same) == [SAME_LIMIT_TEXT.format(key='`gitgud`', limit=described)]
    assert lines(nothing) == [
        SAME_LIMIT_TEXT.format(key='`gitgud`', limit=described),
        ASK_FOR_CHANGE_TEXT,
    ]
    assert lines(unlimited) == [NO_LIMIT_TEXT.format(key='`gimme`')]
    assert fields(nothing) == {
        'Trusted members, moderators and admins · Bot channels; elsewhere the '
        'slash command answers only you': '`gitgud`'
    }
    assert await user_db.get_all_access_settings() == stored


async def test_a_limit_that_tightens_no_rule_says_so(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    already = await limit(bot, admin, 'kcpc', subcommands=False, who='admin')
    tighter = await limit(bot, admin, 'gitgud', who='admin')

    assert UNCHANGED_NOTE in lines(already)
    assert UNCHANGED_NOTE not in lines(tighter)


async def test_a_limit_on_a_group_leaves_the_bot_owners_commands_alone(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'meta', off=True)

    assert fields(embed) == {'Switched off': '`meta` (`/meta show`), `meta ping`'}
    assert not effective_for('meta kill', settings(bot)).off


async def test_unknown_commands_take_no_limits(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    for typed in ('nothing', 'gitgud hard', 'duel nothing'):
        with pytest.raises(AccessCogError) as refused:
            await limit(bot, admin, typed, off=True)
        assert str(refused.value) == UNKNOWN_COMMAND_TEXT.format(name=typed)
    for typed in ('', ' ', '""'):
        with pytest.raises(AccessCogError) as refused:
            await limit(bot, admin, typed, off=True)
        assert str(refused.value) == NO_COMMAND_TEXT

    assert settings(bot).limits == {}


@pytest.mark.parametrize(
    'command, text',
    [
        ('help', PROTECTED_TEXT),
        ('access', PROTECTED_TEXT),
        ('access show', PROTECTED_TEXT),
        ('access limit', PROTECTED_TEXT),
        ('access bot-channels add', PROTECTED_TEXT),
        ('meta kill', OWNER_TEXT.format(name='meta kill')),
        ('cache', OWNER_TEXT.format(name='cache')),
        ('cache contests', OWNER_TEXT.format(name='cache contests')),
        ('mystery', OWNER_TEXT.format(name='mystery')),
        ('gitgud', NO_SUBCOMMANDS_TEXT.format(name='gitgud')),
    ],
)
async def test_some_commands_take_no_limits(
    bot: AccessBot, admin: MagicMock, command: str, text: str
) -> None:
    await configure(bot)
    subcommands = True if command == 'gitgud' else None

    with pytest.raises(AccessCogError) as refused:
        await limit(bot, admin, command, off=True, subcommands=subcommands)

    assert str(refused.value) == text
    assert settings(bot).limits == {}


def test_the_protected_and_owner_refusals_name_no_role() -> None:
    assert PROTECTED_TEXT == (
        "/help and /access can't be limited: admins need them to undo limits."
    )
    assert OWNER_TEXT.format(name='cache') == (
        '`cache` is for the bot owner alone, so it takes no limits.'
    )


@pytest.mark.parametrize('command', ['gimme', 'handle list'])
async def test_private_answers_need_a_slash_command(
    bot: AccessBot, admin: MagicMock, command: str
) -> None:
    await configure(bot)

    with pytest.raises(AccessCogError) as refused:
        await limit(bot, admin, command, private=True)

    assert str(refused.value) == NO_SLASH_TEXT.format(name=command)
    assert settings(bot).limits == {}


async def test_private_answers_for_a_group_warn_of_its_prefix_commands(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await limit(bot, admin, 'handle', private=True)

    assert settings(bot).limits == {'handle *': Limit(private=True)}
    assert lines(embed)[1] == SOME_NOT_SLASH_WARNING.format(names='`handle list`')
    assert (
        fields(embed)[
            "Can't be used: only the person who uses it may see its answers, and it "
            'has no slash command'
        ]
        == '`handle list`'
    )


async def test_private_answers_are_refused_where_the_slash_pass_took_them_out(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)
    # It takes the moderators' duel register out of the members' /duel.
    apply_visibility(bot)

    with pytest.raises(AccessCogError) as refused:
        await limit(bot, admin, 'duel register', private=True)

    assert str(refused.value) == NO_SLASH_TEXT.format(name='duel register')
    await limit(bot, admin, 'duel challenge', private=True)
    assert settings(bot).limits == {'duel challenge': Limit(private=True)}


async def test_private_answers_for_a_group_of_prefix_commands_alone_are_refused(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)
    bot.tree.remove_command('meta')

    with pytest.raises(AccessCogError) as refused:
        await limit(bot, admin, 'meta', private=True)

    assert str(refused.value) == NONE_SLASH_TEXT


@pytest.mark.parametrize(
    'command, where, staff_channel, bot_channels, warning',
    [
        ('gitgud', 'staff', None, (BOT_CHANNEL_ID,), STAFF_PLACE_WARNING),
        ('gitgud', 'staff-only', None, (BOT_CHANNEL_ID,), STAFF_ONLY_PLACE_WARNING),
        ('gitgud', 'bot', None, (), BOT_PLACE_WARNING),
        ('gitgud', 'bot-only', None, (), BOT_ONLY_PLACE_WARNING),
        # kcpc status stays in the staff channel alone.
        ('kcpc', 'bot', None, (BOT_CHANNEL_ID,), STAFF_PLACE_WARNING),
        ('gitgud', 'staff', STAFF_CHANNEL_ID, (), None),
        ('gitgud', 'bot-only', STAFF_CHANNEL_ID, (), None),
        ('gitgud', 'bot-only', None, (BOT_CHANNEL_ID,), None),
    ],
)
async def test_a_limit_warns_when_the_server_lacks_the_channel_it_needs(
    bot: AccessBot,
    admin: MagicMock,
    command: str,
    where: str,
    staff_channel: int | None,
    bot_channels: tuple[int, ...],
    warning: str | None,
) -> None:
    await configure(bot, bot_channels=bot_channels, staff_channel=staff_channel)

    embed = await limit(bot, admin, command, where=where)

    warnings = [line for line in lines(embed) if line.startswith('**Warning:**')]
    assert warnings == ([] if warning is None else [warning])


async def test_a_limit_without_a_place_does_not_warn_of_channels(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, bot_channels=(), staff_channel=None)

    embed = await limit(bot, admin, 'gitgud', who='trusted')

    assert not [line for line in lines(embed) if line.startswith('**Warning:**')]


async def test_a_limit_on_broken_settings_is_refused(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await break_settings(bot, user_db)

    with pytest.raises(SettingsNeedRepair):
        await limit(bot, admin, 'gitgud', off=True)

    assert settings(bot).broken


async def test_a_limit_takes_effect_at_once(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)
    place = channel(guild, BOT_CHANNEL_ID)
    moderator = make_member(guild, 'Moderator')
    member = make_member(guild)

    await limit(bot, admin, 'duel register', off=True)
    await limit(bot, admin, 'gitgud', who='trusted')

    off = make_context(bot, 'duel register', moderator, place)
    with pytest.raises(AccessDenied) as refused:
        await off.command.can_run(off)  # type: ignore[union-attr]
    assert refused.value.text == OFF_TEXT
    untrusted = make_context(bot, 'gitgud', member, place)
    with pytest.raises(AccessDenied) as refused:
        await untrusted.command.can_run(untrusted)  # type: ignore[union-attr]
    assert refused.value.silent
    trusted = make_context(bot, 'gitgud', make_member(guild, 'Trusted'), place)
    assert await trusted.command.can_run(trusted)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    'arguments, limits',
    [
        ('duel register off:yes', {'duel register': Limit(off=True)}),
        ('duel who:admin where:staff', {'duel *': Limit(Who.ADMIN, Where.STAFF)}),
        ('command:duel subcommands:no off:true', {'duel': Limit(off=True)}),
        ('clist show private:yes', {'clist': Limit(private=True)}),
        ('"duel register" WHO:moderator', {'duel register': Limit(Who.MODERATOR)}),
    ],
)
async def test_limit_by_prefix_reads_the_command_and_its_options(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    arguments: str,
    limits: dict[str, Limit],
) -> None:
    await configure(bot)

    ctx = await invoke(
        bot, 'access limit', admin, channel(guild, STAFF_CHANNEL_ID), arguments
    )

    assert reply(ctx).title == 'Limit set'
    assert settings(bot).limits == limits


@pytest.mark.parametrize(
    'arguments, text',
    [
        ('duel who:boss', '`who` takes trusted, moderator, developer or admin.'),
        ('duel where:here', '`where` takes bot, bot-only, staff or staff-only.'),
        ('duel private:maybe', 'Use yes or no, not `maybe`.'),
        ('duel who:trusted who:admin', 'Give `who` only once.'),
        ('duel register command:gitgud', 'Give `command` only once.'),
        (
            'duel off:',
            'Give `off` a value, as in `;access limit duel register off:yes`.',
        ),
        ('off:yes', 'Name a command, as in `;access limit duel register off:yes`.'),
        ('', 'Name a command, as in `;access limit duel register off:yes`.'),
    ],
)
async def test_limit_by_prefix_explains_what_was_mistyped(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, arguments: str, text: str
) -> None:
    await configure(bot)

    ctx = await invoke(
        bot, 'access limit', admin, channel(guild, STAFF_CHANNEL_ID), arguments
    )

    assert alert(ctx) == text
    assert settings(bot).limits == {}


async def test_limit_by_prefix_refuses_an_unknown_command_privately(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    ctx = await invoke(
        bot,
        'access limit',
        admin,
        channel(guild, STAFF_CHANNEL_ID),
        'dule register off:yes',
    )

    assert alert(ctx) == UNKNOWN_COMMAND_TEXT.format(name='dule register')


# /access reset


@both_paths
async def test_reset_clears_a_commands_limit(
    bot: AccessBot, admin: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'gitgud': Limit(off=True), 'gimme': Limit(off=True)})

    embed = await reset(bot, admin, 'gitgud', slash=slash)

    assert settings(bot).limits == {'gimme': Limit(off=True)}
    assert embed.title == 'Limits cleared'
    assert lines(embed) == [CLEARED_TEXT.format(keys='`gitgud`')]
    assert fields(embed) == {
        'Everyone · Bot channels; elsewhere the slash command answers only you': (
            '`gitgud`'
        )
    }


async def test_reset_of_a_group_clears_its_limits_but_not_its_subcommands(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(
        bot,
        limits={
            'duel': Limit(off=True),
            'duel *': Limit(who=Who.TRUSTED),
            'duel register': Limit(where=Where.STAFF),
        },
    )

    embed = await reset(bot, admin, 'duel')

    assert settings(bot).limits == {'duel register': Limit(where=Where.STAFF)}
    assert embed.description == '\n'.join(
        [
            CLEARED_TEXT.format(keys='`duel`; `duel` and its subcommands'),
            STILL_LIMITED_TEXT.format(limits='`duel register`: in the staff channel.'),
        ]
    )
    assert lines(embed)[1:] == [
        'Still limited by:',
        '`duel register`: in the staff channel.',
    ]


async def test_reset_of_a_fallback_clears_the_groups_own_limit(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(
        bot, limits={'clist': Limit(off=True), 'clist *': Limit(who=Who.TRUSTED)}
    )

    embed = await reset(bot, admin, 'clist show')

    assert settings(bot).limits == {'clist *': Limit(who=Who.TRUSTED)}
    assert embed.description == '\n'.join(
        [
            CLEARED_TEXT.format(keys='`clist`'),
            STILL_LIMITED_TEXT.format(
                limits='`clist` and its subcommands: for trusted members, '
                'moderators and admins only.'
            ),
        ]
    )


async def test_reset_of_a_prefix_twin_clears_the_groups_own_limit(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(
        bot, limits={'contests': Limit(off=True), 'contests *': Limit(who=Who.ADMIN)}
    )

    embed = await reset(bot, admin, 'contests upcoming')

    assert settings(bot).limits == {'contests *': Limit(who=Who.ADMIN)}
    assert lines(embed)[0] == CLEARED_TEXT.format(keys='`contests`')


async def test_reset_of_the_handle_group_clears_its_twins_limit_too(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(
        bot,
        limits={
            'handle show': Limit(off=True),
            'handle *': Limit(who=Who.TRUSTED),
            'handle set': Limit(off=True),
        },
    )

    await reset(bot, admin, 'handle')

    assert settings(bot).limits == {'handle set': Limit(off=True)}


async def test_reset_of_a_command_without_a_limit_of_its_own_says_what_limits_it(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot, limits={'duel *': Limit(who=Who.TRUSTED)})
    stored = await user_db.get_all_access_settings()

    embed = await reset(bot, admin, 'duel register')

    assert embed.title == 'Nothing changed'
    assert embed.description == '\n'.join(
        [
            NO_OWN_LIMIT_TEXT.format(name='duel register'),
            STILL_LIMITED_TEXT.format(
                limits='`duel` and its subcommands: for trusted members, '
                'moderators and admins only.'
            ),
        ]
    )
    assert fields(embed) == {
        'Moderators and admins · Bot channels; elsewhere the slash command '
        'answers only you': '`duel register`'
    }
    assert await user_db.get_all_access_settings() == stored


async def test_reset_clears_the_limits_of_a_command_the_bot_no_longer_has(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, limits={'old': Limit(off=True), 'old *': Limit(off=True)})

    embed = await reset(bot, admin, 'old')

    assert settings(bot).limits == {}
    assert lines(embed) == [
        CLEARED_TEXT.format(keys='`old`; `old` and its subcommands')
    ]
    assert fields(embed) == {}


async def test_reset_refuses_an_unknown_command(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(off=True)})

    with pytest.raises(AccessCogError) as refused:
        await reset(bot, admin, 'nothing')

    assert str(refused.value) == UNKNOWN_COMMAND_TEXT.format(name='nothing')


@pytest.mark.parametrize(
    'command, text',
    [
        ('help', PROTECTED_TEXT),
        ('access limit', PROTECTED_TEXT),
        ('meta kill', OWNER_TEXT.format(name='meta kill')),
    ],
)
async def test_reset_refuses_commands_that_take_no_limits(
    bot: AccessBot, admin: MagicMock, command: str, text: str
) -> None:
    await configure(bot)

    with pytest.raises(AccessCogError) as refused:
        await reset(bot, admin, command)

    assert str(refused.value) == text


@pytest.mark.parametrize('typed', ['all', 'ALL', ' All '])
async def test_reset_all_clears_every_limit(
    bot: AccessBot, admin: MagicMock, typed: str
) -> None:
    await configure(
        bot, limits={'gitgud': Limit(off=True), 'duel *': Limit(who=Who.ADMIN)}
    )

    embed = await reset(bot, admin, typed)

    assert settings(bot) == GuildAccess(
        frozenset({BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}), STAFF_CHANNEL_ID
    )
    assert embed.title == 'Limits cleared'
    assert lines(embed) == [CLEARED_ALL_TEXT.format(count=2)]
    assert lines(embed) == ['Cleared all 2 limits.']


async def test_reset_all_of_one_limit_says_so(bot: AccessBot, admin: MagicMock) -> None:
    await configure(bot, limits={'gitgud': Limit(off=True)})

    embed = await reset(bot, admin, 'all')

    assert lines(embed) == [CLEARED_ONE_TEXT]


async def test_reset_all_without_limits_changes_nothing(
    bot: AccessBot, admin: MagicMock
) -> None:
    await configure(bot)

    embed = await reset(bot, admin, 'all')

    assert embed.title == 'Nothing changed'
    assert lines(embed) == [NO_LIMITS_TEXT]


async def test_reset_all_repairs_settings_that_could_not_be_read(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await break_settings(bot, user_db)

    embed = await reset(bot, admin, 'all')

    assert settings(bot) == GuildAccess()
    assert lines(embed) == [REPAIRED_TEXT]
    [(guild_id, stored)] = await user_db.get_all_access_settings()
    assert guild_id == GUILD_ID
    assert decode(stored) == (GuildAccess(), ())


@pytest.mark.parametrize(
    'name, place',
    [
        ('access bot-channels add', BOT_CHANNEL_ID),
        ('access bot-channels remove', BOT_CHANNEL_ID),
        ('access staff-channel', STAFF_CHANNEL_ID),
        ('access staff-channel', None),
    ],
    ids=['add', 'remove', 'staff-channel', 'clear-staff-channel'],
)
async def test_channel_changes_on_broken_settings_are_refused(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    user_db: Any,
    name: str,
    place: int | None,
) -> None:
    # Even those that would change nothing on settings that could be read.
    await break_settings(bot, user_db)

    with pytest.raises(SettingsNeedRepair):
        await call(bot, admin, name, None if place is None else channel(guild, place))

    assert settings(bot).broken


async def test_reset_of_one_command_on_broken_settings_is_refused(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await break_settings(bot, user_db)

    with pytest.raises(SettingsNeedRepair):
        await reset(bot, admin, 'gitgud')


@both_paths
async def test_a_change_to_broken_settings_gets_the_repair_text(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any, slash: bool
) -> None:
    await break_settings(bot, user_db)
    place = channel(guild, GENERAL_ID)
    ctx = make_context(bot, 'access bot-channels add', admin, place, slash=slash)

    with pytest.raises(SettingsNeedRepair) as refused:
        await run(bot, ctx, channel(guild, BOT_CHANNEL_ID))
    await handle_error(bot, ctx, refused.value)

    delete_after = None if slash else REFUSAL_DELETE_AFTER
    assert alert(ctx, delete_after=delete_after) == REPAIR_TEXT
    assert settings(bot).broken


async def break_the_table(bot: AccessBot, user_db: Any, settings: GuildAccess) -> None:
    """Store ``settings`` for this server, and a row the bot can't read at all,
    as a hand-edited database might hold; then load them, as at start-up.
    """
    await user_db.set_access_settings(GUILD_ID, encode(settings))
    await user_db.conn.execute(
        "INSERT INTO access_settings (guild_id, settings) VALUES ('not-a-guild', '{}')"
    )
    await user_db.conn.commit()
    await bot.access.load()
    assert bot.access.settings_unreadable(GUILD_ID)


async def stored_rows(user_db: Any) -> list[tuple[str, str]]:
    """Every row of the access_settings table, as stored."""
    cursor = await user_db.conn.execute(
        'SELECT guild_id, settings FROM access_settings ORDER BY guild_id'
    )
    return [(row[0], row[1]) for row in await cursor.fetchall()]


@both_paths
async def test_reset_all_never_replaces_settings_that_were_not_read(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any, slash: bool
) -> None:
    # One unreadable row makes the whole table unreadable, but this server's
    # own row is fine: a reset would wipe its channels and limits.
    good = GuildAccess(
        frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID, {'gitgud': Limit(off=True)}
    )
    await break_the_table(bot, user_db, good)
    before = await stored_rows(user_db)
    place = channel(guild, GENERAL_ID)
    ctx = make_context(bot, 'access reset', admin, place, slash=slash)

    with pytest.raises(SettingsUnreadable) as refused:
        await run(bot, ctx, command='all')
    await handle_error(bot, ctx, refused.value)

    delete_after = None if slash else REFUSAL_DELETE_AFTER
    assert alert(ctx, delete_after=delete_after) == UNREADABLE_CHANGE_TEXT
    assert await stored_rows(user_db) == before
    assert (str(GUILD_ID), encode(good)) in before
    assert settings(bot).broken


@pytest.mark.parametrize(
    'name, place',
    [
        ('access bot-channels add', BOT_CHANNEL_ID),
        ('access staff-channel', STAFF_CHANNEL_ID),
    ],
    ids=['add', 'staff-channel'],
)
async def test_no_change_is_stored_while_no_settings_can_be_read(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    user_db: Any,
    name: str,
    place: int,
) -> None:
    await break_the_table(bot, user_db, GuildAccess())
    before = await stored_rows(user_db)

    with pytest.raises(SettingsUnreadable):
        await call(bot, admin, name, channel(guild, place))
    with pytest.raises(SettingsUnreadable):
        await limit(bot, admin, 'gitgud', off=True)

    assert await stored_rows(user_db) == before


async def test_show_says_when_no_settings_could_be_read(
    bot: AccessBot, admin: MagicMock, user_db: Any
) -> None:
    await break_the_table(bot, user_db, GuildAccess())

    embed = await show(bot, admin, slash=True)

    assert warnings_of(embed) == [UNREADABLE_WARNING]
    assert fields(embed) == {}
    assert UNREADABLE_WARNING == (
        "**Warning:** the bot couldn't read its access settings, so every command "
        "but /access, /help and the bot owner's is refused, and these settings "
        "can't be changed. Ask the bot owner to check the log."
    )


@pytest.mark.parametrize(
    'arguments, cleared',
    [('duel register', 'duel register'), ('"duel register"', 'duel register')],
)
async def test_reset_by_prefix_reads_the_rest_of_the_line(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, arguments: str, cleared: str
) -> None:
    await configure(bot, limits={cleared: Limit(off=True)})

    ctx = await invoke(
        bot, 'access reset', admin, channel(guild, STAFF_CHANNEL_ID), arguments
    )

    assert reply(ctx).title == 'Limits cleared'
    assert settings(bot).limits == {}


async def test_reset_by_prefix_without_a_command_explains(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    ctx = await invoke(bot, 'access reset', admin, channel(guild, STAFF_CHANNEL_ID))

    assert alert(ctx) == 'Name a command, as in `;access reset duel register`.'


# Autocomplete


def interaction_in(guild_id: int | None) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    return interaction


def values(choices: list[Any]) -> list[str]:
    assert all(choice.name == choice.value for choice in choices)
    return [choice.value for choice in choices]


async def test_limit_suggests_the_commands_that_take_limits(bot: AccessBot) -> None:
    cog = access_cog(bot)

    everything = await cog.limit_autocomplete(interaction_in(GUILD_ID), '')
    contests = await cog.limit_autocomplete(interaction_in(GUILD_ID), ' CON ')

    assert values(everything) == [
        'clist',
        'clist future',
        'contests',
        'contests live',
        'contests upcoming',
        'duel',
        'duel challenge',
        'duel register',
        'gimme',
        'gitgud',
        'handle',
        'handle list',
        'handle set',
        'handle show',
        'kcpc',
        'kcpc status',
        'meta',
        'meta ping',
    ]
    assert values(contests) == ['contests', 'contests live', 'contests upcoming']


async def nothing(ctx: Context) -> None:
    """A command that does nothing."""


async def test_limit_suggests_at_most_25_commands(
    bot: AccessBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [f'command{number:02}' for number in range(30)]
    member_rule = Rule(Who.EVERYONE, Where.BOT)
    ruled = {**RULES, **dict.fromkeys(names, member_rule)}
    monkeypatch.setattr(table, 'RULES', MappingProxyType(ruled))
    for name in names:
        bot.add_command(commands.Command(nothing, name=name))

    suggested = await access_cog(bot).limit_autocomplete(
        interaction_in(GUILD_ID), 'command'
    )

    assert values(suggested) == names[:25]


async def test_reset_suggests_all_then_the_limited_commands(bot: AccessBot) -> None:
    await configure(bot, limits={'duel *': Limit(off=True), 'gitgud': Limit(off=True)})
    cog = access_cog(bot)

    everything = await cog.reset_autocomplete(interaction_in(GUILD_ID), '')
    git = await cog.reset_autocomplete(interaction_in(GUILD_ID), 'git')
    al = await cog.reset_autocomplete(interaction_in(GUILD_ID), 'AL')

    assert values(everything) == ['all', 'duel', 'gitgud']
    assert values(git) == ['gitgud']
    assert values(al) == ['all']


async def test_reset_suggests_nothing_outside_a_server(bot: AccessBot) -> None:
    await configure(bot, limits={'gitgud': Limit(off=True)})

    assert await access_cog(bot).reset_autocomplete(interaction_in(None), '') == []


# Storage and the log


async def test_changes_are_stored_in_the_database(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    await call(bot, admin, 'access staff-channel', channel(guild, STAFF_CHANNEL_ID))
    await call(bot, admin, 'access bot-channels add', channel(guild, BOT_CHANNEL_ID))
    await limit(bot, admin, 'duel', who='trusted')
    await limit(bot, admin, 'gitgud', off=True)
    await reset(bot, admin, 'gitgud')

    expected = GuildAccess(
        frozenset({BOT_CHANNEL_ID}),
        STAFF_CHANNEL_ID,
        {'duel *': Limit(who=Who.TRUSTED)},
    )
    assert settings(bot) == expected
    assert await user_db.get_all_access_settings() == [(GUILD_ID, encode(expected))]
    restarted = AccessService(bot)
    restarted.use_user_db(user_db)
    await restarted.load()
    assert restarted.guild_access(GUILD_ID) == expected


async def test_a_change_the_database_refuses_changes_nothing(
    bot: AccessBot, guild: MagicMock, admin: MagicMock, user_db: Any
) -> None:
    await configure(bot)
    before = settings(bot)
    user_db.set_access_settings = AsyncMock(
        side_effect=sqlite3.OperationalError('disk I/O error')
    )

    with pytest.raises(sqlite3.OperationalError):
        await limit(bot, admin, 'gitgud', off=True)
    with pytest.raises(sqlite3.OperationalError):
        await call(bot, admin, 'access staff-channel', None)

    assert settings(bot) == before


@pytest.mark.parametrize(
    'name, arguments',
    [
        ('access bot-channels add', (SPARE_CHANNELS_ID,)),
        ('access bot-channels remove', (BOT_CHANNEL_ID,)),
        ('access staff-channel', (SPARE_CHANNELS_ID,)),
        ('access staff-channel', (None,)),
        ('access limit', ()),
        ('access reset', ()),
        ('access reset all', ()),
    ],
)
async def test_changes_say_when_they_are_not_stored(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    name: str,
    arguments: tuple[int | None, ...],
) -> None:
    make_channel(guild, SPARE_CHANNELS_ID, public=False)
    without_storage(bot)
    await configure(bot, limits={'gitgud': Limit(off=True)})

    if name == 'access limit':
        embed = await limit(bot, admin, 'duel', off=True)
    elif name == 'access reset':
        embed = await reset(bot, admin, 'gitgud')
    elif name == 'access reset all':
        embed = await reset(bot, admin, 'all')
    else:
        places = [
            None if place is None else channel(guild, place) for place in arguments
        ]
        embed = await call(bot, admin, name, *places)

    assert lines(embed)[-1] == NOT_STORED_NOTE


async def test_changes_that_are_stored_say_nothing_of_it(
    bot: AccessBot, guild: MagicMock, admin: MagicMock
) -> None:
    await configure(bot)

    embeds = [
        await call(
            bot, admin, 'access bot-channels remove', channel(guild, BOT_CHANNEL_ID)
        ),
        await limit(bot, admin, 'duel', off=True),
        await reset(bot, admin, 'all'),
    ]

    for embed in embeds:
        assert NOT_STORED_NOTE not in lines(embed)


async def test_each_change_is_logged(
    bot: AccessBot,
    guild: MagicMock,
    admin: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await configure(bot, bot_channels=())

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await call(
            bot, admin, 'access bot-channels add', channel(guild, BOT_CHANNEL_ID)
        )
        await call(
            bot, admin, 'access bot-channels remove', channel(guild, BOT_CHANNEL_ID)
        )
        await call(bot, admin, 'access staff-channel', None)
        await call(bot, admin, 'access staff-channel', channel(guild, STAFF_CHANNEL_ID))
        await limit(bot, admin, 'duel', who='trusted', off=True)
        await reset(bot, admin, 'duel')
        await limit(bot, admin, 'gitgud', off=True)
        await reset(bot, admin, 'all')

    member = f'Member {MEMBER_ID} in guild {GUILD_ID}'
    assert logged(caplog) == [
        f'{member} added the bot channel {BOT_CHANNEL_ID}',
        f'{member} removed the bot channel {BOT_CHANNEL_ID}',
        f'{member} cleared the staff channel',
        f'{member} set the staff channel to {STAFF_CHANNEL_ID}',
        f'{member} limited duel *: switched off; for trusted members, moderators '
        'and admins only',
        f'{member} cleared the limits duel *',
        f'{member} limited gitgud: switched off',
        f'{member} cleared every limit',
    ]
