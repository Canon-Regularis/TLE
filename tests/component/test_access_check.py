"""Tests for AccessService: the check before every command, its refusals, and
the access settings it keeps.

The bot is a real one, with a cog of hybrid, group and prefix commands. The
tests set those commands' rules by replacing the rule table, so that they
don't depend on TLE's own. Contexts are real, for prefix and slash
invocations, and commands go through discord.py's own checks, as when members
use them. Discord itself (the server, its members and channels, the
interaction) is mocked, and so is the bot's application, whose owners the
service asks Discord for.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.access import table
from tle.access.rules import (
    Asker,
    Decision,
    Effective,
    Limit,
    Outcome,
    Rule,
    Spot,
    Where,
    Who,
)
from tle.access.service import (
    A_BOT_CHANNEL_TEXT,
    BOT_CHANNELS_TEXT,
    IN_THE_STAFF_CHANNEL,
    LISTED_CHANNELS,
    NOT_AVAILABLE_TEXT,
    NOT_HERE_TEXT,
    NO_BOT_CHANNEL_ADMIN_PREFIX_TEXT,
    NO_BOT_CHANNEL_ADMIN_TEXT,
    NO_BOT_CHANNEL_TEXT,
    NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT,
    NO_STAFF_CHANNEL_ADMIN_TEXT,
    OFF_TEXT,
    OWNER_RETRY_SECONDS,
    PRIVATE_MESSAGES_TEXT,
    PRIVATE_ONLY_TEXT,
    REPAIR_TEXT,
    SLASH_HINT,
    STAFF_CHANNEL_SLASH_TEXT,
    STAFF_CHANNEL_TEXT,
    UNREADABLE_CHANGE_TEXT,
    UNREADABLE_TEXT,
    AccessService,
    AccessTree,
    SettingsNeedRepair,
    SettingsUnreadable,
    cache_decision,
    cached_decision,
)
from tle.access.settings import GuildAccess, encode
from tle.util import discord_common
from tle.util.discord_common import (
    NOT_ALLOWED_MESSAGE,
    REFUSAL_THROTTLE_SECONDS,
    AccessDenied,
    RefusalThrottle,
    bot_error_handler,
    embed_alert,
)

LOGGER = 'tle.access'
HANDLER_LOGGER = 'tle.util.discord_common'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
SECOND_BOT_CHANNEL_ID = 1_200_000_000_000_000_002
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
THREAD_ID = 1_200_000_000_000_000_030
DM_CHANNEL_ID = 1_200_000_000_000_000_040
GONE_ID = 1_200_000_000_000_000_050  # a channel the server no longer has
MEMBER_ID = 1_400_000_000_000_000_001
OWNER_ID = 1_400_000_000_000_000_009
TEAM_DEVELOPER_ID = 1_400_000_000_000_000_010
TEAM_READER_ID = 1_400_000_000_000_000_011
ADMIN_ROLE_ID = 1_300_000_000_000_000_001
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_005
OTHER_ROLE_ID = 1_300_000_000_000_000_099

RULES = {
    'ping': Rule(Who.EVERYONE, Where.BOT),
    'gimme': Rule(Who.EVERYONE, Where.BOT),  # a prefix command alone
    'challenge': Rule(Who.EVERYONE, Where.BOT_ONLY),
    'clist': Rule(Who.EVERYONE, Where.BOT),  # a group's own callback: /clist show
    'clist future': Rule(Who.EVERYONE, Where.BOT),
    'clist purge': Rule(Who.MODERATOR, Where.BOT),
    'contests': Rule(Who.EVERYONE, Where.BOT),  # /contests upcoming
    'handle show': Rule(Who.EVERYONE, Where.BOT),
    'refer': Rule(Who.TRUSTED, Where.BOT_ONLY),
    'roleupdate': Rule(Who.MODERATOR, Where.STAFF),
    'status': Rule(Who.DEVELOPER, Where.STAFF_ONLY),
    'grandfather': Rule(Who.ADMIN, Where.STAFF),
    'kill': Rule(Who.OWNER, Where.ANYWHERE),
    'whisper': Rule(Who.EVERYONE, Where.BOT, private=True),
    'access': Rule(Who.ADMIN, Where.STAFF),
    'access staff-channel': Rule(Who.ADMIN, Where.STAFF),
    'access bot-channels': Rule(Who.ADMIN, Where.STAFF),
    'access bot-channels add': Rule(Who.ADMIN, Where.STAFF),
}
# The prefix subcommand ;contests upcoming does what /contests upcoming, the
# group's fallback, does; ;handle, the group's own callback, is handle show.
TWINS = {'contests upcoming': 'contests', 'handle': 'handle show'}
BOTH_BOT_CHANNELS = f'<#{BOT_CHANNEL_ID}>, <#{SECOND_BOT_CHANNEL_ID}>'

Context = commands.Context[Any]


class Lab(commands.Cog):
    """Commands of every shape; each records that it ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def _record(self, ctx: Context) -> None:
        assert ctx.command is not None
        self.ran.append(ctx.command.qualified_name)

    @commands.hybrid_command()
    async def ping(self, ctx: Context) -> None:
        """A member command."""
        self._record(ctx)

    @commands.command()
    async def gimme(self, ctx: Context) -> None:
        """A prefix command."""
        self._record(ctx)

    @commands.hybrid_command()
    async def challenge(self, ctx: Context) -> None:
        """A command for bot channels only."""
        self._record(ctx)

    @commands.hybrid_group(fallback='show')
    async def clist(self, ctx: Context) -> None:
        """A group, whose slash form is its fallback."""
        self._record(ctx)

    @clist.command()
    async def future(self, ctx: Context) -> None:
        """A subcommand."""
        self._record(ctx)

    @clist.command()
    async def purge(self, ctx: Context) -> None:
        """A moderator's subcommand."""
        self._record(ctx)

    async def cog_load(self) -> None:
        # Nested here, as KCPC nests its twins: declared in the group,
        # discord.py would take the group's fallback of the same name out of
        # the slash group when it copies the cog's commands.
        self.contests.add_command(self.contests_upcoming)

    @commands.hybrid_group(fallback='upcoming')
    async def contests(self, ctx: Context) -> None:
        """A group whose fallback has a prefix twin."""
        self._record(ctx)

    @commands.hybrid_command(name='upcoming', with_app_command=False)
    async def contests_upcoming(self, ctx: Context) -> None:
        """The prefix twin of /contests upcoming."""
        self._record(ctx)

    @commands.hybrid_group()
    async def handle(self, ctx: Context) -> None:
        """A group without a fallback, the twin of handle show."""
        self._record(ctx)

    @handle.command(name='show')
    async def handle_show(self, ctx: Context) -> None:
        """Show a handle."""
        self._record(ctx)

    @commands.hybrid_command()
    async def refer(self, ctx: Context) -> None:
        """For trusted members."""
        self._record(ctx)

    @commands.hybrid_command()
    async def roleupdate(self, ctx: Context) -> None:
        """For moderators, in the staff channel."""
        self._record(ctx)

    @commands.hybrid_command()
    async def status(self, ctx: Context) -> None:
        """For developers, in the staff channel only."""
        self._record(ctx)

    @commands.hybrid_command()
    async def grandfather(self, ctx: Context) -> None:
        """For admins, in the staff channel."""
        self._record(ctx)

    @commands.hybrid_command()
    async def kill(self, ctx: Context) -> None:
        """For the bot owner."""
        self._record(ctx)

    @commands.hybrid_command()
    async def whisper(self, ctx: Context) -> None:
        """Always answers privately."""
        self._record(ctx)

    @commands.hybrid_command()
    async def mystery(self, ctx: Context) -> None:
        """A command missing from the rule table."""
        self._record(ctx)

    @commands.hybrid_group(fallback='show')
    async def access(self, ctx: Context) -> None:
        """Show the access settings."""
        self._record(ctx)

    @access.command(name='staff-channel')
    async def staff_channel(self, ctx: Context) -> None:
        """Set the staff channel."""
        self._record(ctx)

    @access.group(name='bot-channels')
    async def channels(self, ctx: Context) -> None:
        """Show the bot channels."""
        self._record(ctx)

    @channels.command(name='add')
    async def add_channel(self, ctx: Context) -> None:
        """Add a bot channel."""
        self._record(ctx)


class AccessBot(commands.Bot):
    """A bot that carries an access service, as TLEBot does."""

    access: AccessService


class MonotonicClock:
    """Stands in for time.monotonic; it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles: admin, moderator and trusted by name, developer by id."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', DEVELOPER_ROLE_ID)


@pytest.fixture(autouse=True)
def refusal_throttle(monkeypatch: pytest.MonkeyPatch) -> RefusalThrottle:
    """A fresh throttle of prefix refusals for each test, on a clock that stays."""
    throttle = RefusalThrottle(REFUSAL_THROTTLE_SECONDS, MonotonicClock())
    monkeypatch.setattr(discord_common, 'refusal_throttle', throttle)
    return throttle


@pytest.fixture(autouse=True)
def rule_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table, 'RULES', MappingProxyType(RULES))
    monkeypatch.setattr(table, 'TWINS', MappingProxyType(TWINS))


@pytest.fixture
def clock() -> MonotonicClock:
    return MonotonicClock()


def app_info(*, owner_id: int = OWNER_ID, team: Any = None) -> MagicMock:
    """The bot's application as Discord describes it."""
    app = MagicMock(spec=discord.AppInfo)
    app.owner = MagicMock(spec=discord.User, id=owner_id)
    app.team = team
    return app


def team_of(*members: tuple[int, discord.TeamMemberRole]) -> MagicMock:
    team = MagicMock(spec=discord.Team)
    team.members = [
        MagicMock(spec=discord.TeamMember, id=member_id, role=role)
        for member_id, role in members
    ]
    return team


@pytest.fixture
async def bot(clock: MonotonicClock) -> AsyncIterator[AccessBot]:
    """A bot with the service's check and the Lab cog. Its owner is OWNER_ID,
    as Discord says when asked.
    """
    bot = AccessBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    bot.application_info = AsyncMock(return_value=app_info())  # type: ignore[method-assign]
    install_service(bot, AccessService(bot, clock=clock))
    await bot.add_cog(Lab())
    yield bot
    await bot.close()


def install_service(bot: AccessBot, service: AccessService) -> None:
    """Make ``service`` the bot's, checking every command, as TLEBot does."""
    old = getattr(bot, 'access', None)
    if old is not None:
        bot.remove_check(old.check)
    bot.access = service
    bot.add_check(service.check)


def lab(bot: commands.Bot) -> Lab:
    cog = bot.get_cog('Lab')
    assert isinstance(cog, Lab)
    return cog


def known_to(bot: commands.Bot, guild: MagicMock) -> None:
    """Let ``bot`` find ``guild``, as a bot with the guilds intent finds its
    servers.
    """
    bot.get_guild = {guild.id: guild}.get  # type: ignore[method-assign,assignment]


def hide_access(bot: commands.Bot) -> None:
    """Show /access only to members with Manage Server, as the slash pass does."""
    access = bot.tree.get_command('access')
    assert access is not None
    access.default_permissions = discord.Permissions(manage_guild=True)


def make_channel(
    guild: MagicMock, channel_id: int, *, visible: bool = True
) -> MagicMock:
    """A text channel in ``guild``, which members can see unless not ``visible``."""
    channel = MagicMock(
        spec=discord.TextChannel, id=channel_id, guild=guild, mention=f'<#{channel_id}>'
    )
    channel.permissions_for.return_value = discord.Permissions(view_channel=visible)
    guild.channels_by_id[channel_id] = channel
    return channel


def make_guild(guild_id: int = GUILD_ID) -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=guild_id)
    guild.channels_by_id = {}
    guild.get_channel.side_effect = guild.channels_by_id.get
    for channel_id in (BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID, STAFF_CHANNEL_ID):
        make_channel(guild, channel_id)
    make_channel(guild, GENERAL_ID)
    return guild


@pytest.fixture
def guild() -> MagicMock:
    return make_guild()


def channel(guild: MagicMock, channel_id: int) -> MagicMock:
    found: MagicMock = guild.channels_by_id[channel_id]
    return found


def thread_in(guild: MagicMock, parent_id: int) -> MagicMock:
    return MagicMock(
        spec=discord.Thread, id=THREAD_ID, parent_id=parent_id, guild=guild
    )


def make_role(identifier: str | int) -> MagicMock:
    """A role, named ``identifier`` or with ``identifier`` as its id."""
    role = MagicMock(spec=discord.Role)
    if isinstance(identifier, int):
        role.id, role.name = identifier, 'Some role'
    else:
        role.id, role.name = OTHER_ROLE_ID, identifier
    return role


def make_member(
    guild: MagicMock,
    *roles: str | int,
    manage_guild: bool = False,
    member_id: int = MEMBER_ID,
) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = [make_role(role) for role in roles]
    return member


def make_admin(guild: MagicMock) -> MagicMock:
    return make_member(guild, manage_guild=True)


def make_owner(guild: MagicMock) -> MagicMock:
    """The bot's owner, who has no role in ``guild``."""
    return make_member(guild, member_id=OWNER_ID)


def make_context(
    bot: commands.Bot,
    name: str,
    author: MagicMock,
    place: MagicMock,
    *,
    slash: bool = False,
    guild: Any = 'author',
) -> Context:
    """A real context of command ``name``, used by ``author`` in ``place``.

    With ``slash``, it belongs to an interaction, as for a slash command. Its
    server is the author's unless ``guild`` says otherwise. Replies are
    recorded by an ``AsyncMock`` in place of ``send``.
    """
    command = bot.get_command(name)
    assert command is not None, name
    server = author.guild if guild == 'author' else guild
    message = MagicMock(
        spec=discord.Message, guild=server, author=author, channel=place
    )
    message.content = f';{name}'
    message.jump_url = 'https://discord.com/channels/1/2/3'
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = False
    ctx: Context = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        command=command,
        invoked_with=command.name,
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = ctx  # where discord.py keeps a slash command's context
    ctx.send = AsyncMock()  # type: ignore[method-assign]
    return ctx


async def refusal(ctx: Context) -> AccessDenied | None:
    """None if discord.py lets ``ctx``'s command run; otherwise its refusal."""
    assert ctx.command is not None
    try:
        allowed = await ctx.command.can_run(ctx)
    except AccessDenied as denied:
        return denied
    assert allowed
    return None


async def refusal_text(ctx: Context) -> str | None:
    """The text of the refusal of ``ctx``'s command, which must be refused
    with one.
    """
    denied = await refusal(ctx)
    assert denied is not None and not denied.silent
    return denied.text


def decided(ctx: Context) -> Decision:
    decision = cached_decision(ctx)
    assert decision is not None
    return decision


async def outcome(
    bot: commands.Bot,
    name: str,
    author: MagicMock,
    place: MagicMock,
    *,
    slash: bool = False,
) -> Outcome:
    """What the check decides for ``author`` using ``name`` in ``place``."""
    ctx = make_context(bot, name, author, place, slash=slash)
    await refusal(ctx)
    return decided(ctx).outcome


async def configure(
    bot: AccessBot,
    *,
    guild_id: int = GUILD_ID,
    bot_channels: tuple[int, ...] = (BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID),
    staff_channel: int | None = STAFF_CHANNEL_ID,
    limits: dict[str, Limit] | None = None,
) -> None:
    """Give the server these access settings."""
    settings = GuildAccess(frozenset(bot_channels), staff_channel, limits or {})
    await bot.access.change(guild_id, lambda _: settings)


def sent(ctx: Context) -> tuple[str | None, dict[str, Any]]:
    """The text of the one alert sent, and what else it was sent with."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once()
    assert send.await_args is not None and not send.await_args.args
    options = dict(send.await_args.kwargs)
    embed = options.pop('embed')
    assert isinstance(embed, discord.Embed)
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    return embed.description, options


def logged(caplog: pytest.LogCaptureFixture, level: int | None = None) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and (level is None or record.levelno == level)
    ]


def with_hint(text: str, path: str) -> str:
    return f'{text} {SLASH_HINT.format(path=path)}'


both_paths = pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])


# Where commands answer


@both_paths
async def test_a_member_command_answers_publicly_in_a_bot_channel(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    member = make_member(guild)

    for place in (BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID):
        assert (
            await outcome(bot, 'ping', member, channel(guild, place), slash=slash)
            is Outcome.PUBLIC
        )


@both_paths
async def test_the_staff_channel_counts_as_a_bot_channel(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)

    assert (
        await outcome(
            bot,
            'ping',
            make_member(guild),
            channel(guild, STAFF_CHANNEL_ID),
            slash=slash,
        )
        is Outcome.PUBLIC
    )


async def test_elsewhere_slash_answers_privately_and_prefix_is_refused(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    member, general = make_member(guild), channel(guild, GENERAL_ID)
    slash = make_context(bot, 'ping', member, general, slash=True)
    prefix = make_context(bot, 'ping', member, general)

    assert await refusal(slash) is None
    assert decided(slash).outcome is Outcome.PRIVATE
    assert await refusal_text(prefix) == with_hint(
        BOT_CHANNELS_TEXT.format(channels=BOTH_BOT_CHANNELS), '/ping'
    )


@pytest.mark.parametrize(
    ('parent_id', 'slash', 'expected'),
    [
        (BOT_CHANNEL_ID, False, Outcome.PUBLIC),
        (BOT_CHANNEL_ID, True, Outcome.PUBLIC),
        (GENERAL_ID, False, Outcome.WRONG_CHANNEL),
        (GENERAL_ID, True, Outcome.PRIVATE),
    ],
    ids=[
        'bot channel, prefix',
        'bot channel, slash',
        'elsewhere, prefix',
        'elsewhere, slash',
    ],
)
async def test_a_thread_counts_as_the_channel_it_is_in(
    bot: AccessBot, guild: MagicMock, parent_id: int, slash: bool, expected: Outcome
) -> None:
    await configure(bot)

    place = thread_in(guild, parent_id)

    assert (
        await outcome(bot, 'ping', make_member(guild), place, slash=slash) is expected
    )


@both_paths
async def test_a_thread_in_the_staff_channel_is_the_staff_channel(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    moderator = make_member(guild, 'Moderator')

    place = thread_in(guild, STAFF_CHANNEL_ID)

    assert await outcome(bot, 'roleupdate', moderator, place, slash=slash) is (
        Outcome.PUBLIC
    )


async def test_the_command_runs_only_if_the_check_allows_it(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    member = make_member(guild)
    allowed = make_context(bot, 'ping', member, channel(guild, BOT_CHANNEL_ID))
    refused = make_context(bot, 'ping', member, channel(guild, GENERAL_ID))

    assert allowed.command is not None and refused.command is not None
    await allowed.command.invoke(allowed)
    with pytest.raises(AccessDenied):
        await refused.command.invoke(refused)

    assert lab(bot).ran == ['ping']


async def test_a_prefix_subcommand_is_checked_by_its_own_rule(
    bot: AccessBot, guild: MagicMock
) -> None:
    # discord.py runs only the subcommand's check, not its group's: hybrid
    # groups invoke their callback only without a subcommand.
    await configure(bot, limits={'clist': Limit(off=True)})
    ctx = make_context(bot, 'clist', make_member(guild), channel(guild, BOT_CHANNEL_ID))
    ctx.view = StringView('future')
    ctx.invoked_with = 'clist'

    assert ctx.command is not None
    await ctx.command.invoke(ctx)

    assert lab(bot).ran == ['clist future']


# Members who may not use a command


async def test_a_prefix_command_the_member_may_not_use_is_refused_silently(
    bot: AccessBot, guild: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    await configure(bot)
    ctx = make_context(
        bot, 'clist purge', make_member(guild), channel(guild, BOT_CHANNEL_ID)
    )

    denied = await refusal(ctx)
    assert denied is not None
    with caplog.at_level(logging.DEBUG, logger=HANDLER_LOGGER):
        await bot_error_handler(ctx, denied)

    assert denied.silent and denied.text is None
    assert decided(ctx).outcome is Outcome.NOT_ALLOWED
    cast(AsyncMock, ctx.send).assert_not_awaited()
    # As if the command didn't exist: a debug log alone.
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == HANDLER_LOGGER
    ] == [
        (
            logging.DEBUG,
            f'Refused command clist purge to member {MEMBER_ID} in guild '
            f'{GUILD_ID} silently',
        )
    ]


async def test_a_slash_command_the_member_may_not_use_is_refused_privately(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    ctx = make_context(
        bot,
        'clist purge',
        make_member(guild),
        channel(guild, BOT_CHANNEL_ID),
        slash=True,
    )

    denied = await refusal(ctx)
    assert denied is not None
    await bot_error_handler(ctx, denied)

    assert not denied.silent
    assert sent(ctx) == (NOT_ALLOWED_MESSAGE, {'ephemeral': True})


@both_paths
async def test_who_is_checked_before_anything_that_would_reveal_the_command(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    # Switched off, and in a channel where it doesn't work: a member learns
    # neither.
    await configure(
        bot, limits={'clist purge': Limit(off=True, where=Where.STAFF_ONLY)}
    )
    ctx = make_context(
        bot, 'clist purge', make_member(guild), channel(guild, GENERAL_ID), slash=slash
    )

    denied = await refusal(ctx)

    assert decided(ctx).outcome is Outcome.NOT_ALLOWED
    assert denied is not None and denied.silent is not slash


@both_paths
@pytest.mark.parametrize(
    'roles',
    [(), ('Developer',), (OTHER_ROLE_ID,)],
    ids=['no role', 'a role named like a level', 'another role'],
)
async def test_members_pass_only_the_levels_their_roles_give(
    bot: AccessBot, guild: MagicMock, roles: tuple[str | int, ...], slash: bool
) -> None:
    await configure(bot)
    staff = channel(guild, STAFF_CHANNEL_ID)

    for name in ('clist purge', 'refer', 'roleupdate', 'status', 'grandfather'):
        assert (
            await outcome(bot, name, make_member(guild, *roles), staff, slash=slash)
            is Outcome.NOT_ALLOWED
        ), name


@both_paths
@pytest.mark.parametrize(
    'admin',
    [
        lambda guild: make_member(guild, manage_guild=True),
        lambda guild: make_member(guild, 'Admin'),
    ],
    ids=['Manage Server', 'admin role'],
)
async def test_admins_pass_by_their_role_or_manage_server(
    bot: AccessBot,
    guild: MagicMock,
    admin: Callable[[MagicMock], MagicMock],
    slash: bool,
) -> None:
    await configure(bot)
    staff = channel(guild, STAFF_CHANNEL_ID)

    for name in ('clist purge', 'refer', 'roleupdate', 'status', 'grandfather'):
        assert (
            await outcome(bot, name, admin(guild), staff, slash=slash) is Outcome.PUBLIC
        ), name


async def test_tle_roles_are_read_when_a_command_is_used(
    bot: AccessBot, guild: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await configure(bot)
    staff = channel(guild, STAFF_CHANNEL_ID)
    by_id = make_member(guild, ADMIN_ROLE_ID)

    before = await outcome(bot, 'grandfather', by_id, staff)
    monkeypatch.setattr(constants, 'TLE_ADMIN', ADMIN_ROLE_ID)
    after = await outcome(bot, 'grandfather', by_id, staff)

    assert (before, after) == (Outcome.NOT_ALLOWED, Outcome.PUBLIC)


@both_paths
async def test_trusted_members_and_moderators(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    place = channel(guild, BOT_CHANNEL_ID)
    trusted, moderator = make_member(guild, 'Trusted'), make_member(guild, 'Moderator')

    assert await outcome(bot, 'refer', trusted, place, slash=slash) is Outcome.PUBLIC
    assert await outcome(bot, 'clist purge', trusted, place, slash=slash) is (
        Outcome.NOT_ALLOWED
    )
    # A moderator is trusted as well.
    assert await outcome(bot, 'refer', moderator, place, slash=slash) is Outcome.PUBLIC
    assert await outcome(bot, 'clist purge', moderator, place, slash=slash) is (
        Outcome.PUBLIC
    )


@both_paths
async def test_the_developer_role_is_known_by_its_id(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    staff = channel(guild, STAFF_CHANNEL_ID)
    developer = make_member(guild, DEVELOPER_ROLE_ID)

    assert await outcome(bot, 'status', developer, staff, slash=slash) is Outcome.PUBLIC
    # A developer is no admin, nor a moderator.
    assert await outcome(bot, 'grandfather', developer, staff, slash=slash) is (
        Outcome.NOT_ALLOWED
    )
    assert await outcome(bot, 'roleupdate', developer, staff, slash=slash) is (
        Outcome.NOT_ALLOWED
    )


async def test_without_a_developer_role_only_admins_pass_the_developer_level(
    bot: AccessBot, guild: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)
    await configure(bot)
    staff = channel(guild, STAFF_CHANNEL_ID)

    developer = await outcome(
        bot, 'status', make_member(guild, DEVELOPER_ROLE_ID), staff
    )
    admin = await outcome(bot, 'status', make_admin(guild), staff)

    assert (developer, admin) == (Outcome.NOT_ALLOWED, Outcome.PUBLIC)


def everyone_role() -> MagicMock:
    """The server's default role, which every member has: its id is the server's."""
    role = MagicMock(spec=discord.Role)
    role.id, role.name = GUILD_ID, '@everyone'
    return role


@both_paths
@pytest.mark.parametrize(
    'value', [GUILD_ID, '@everyone'], ids=['the server id', 'the name @everyone']
)
@pytest.mark.parametrize(
    'setting', ['TLE_ADMIN', 'TLE_MODERATOR', 'TLE_TRUSTED', 'TLE_DEVELOPER']
)
async def test_a_role_setting_that_names_everyone_gives_nobody_its_level(
    bot: AccessBot,
    guild: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str | int,
    slash: bool,
) -> None:
    # Copying the server's id for a role's is an easy slip; it must not make
    # every member staff.
    monkeypatch.setattr(constants, setting, value)
    await configure(bot)
    member = make_member(guild)
    member.roles = [everyone_role()]
    staff = channel(guild, STAFF_CHANNEL_ID)

    assert bot.access.asker(member) == Asker()
    for name in ('clist purge', 'refer', 'roleupdate', 'status', 'grandfather'):
        assert (
            await outcome(bot, name, member, staff, slash=slash) is Outcome.NOT_ALLOWED
        ), name


# The bot owner


async def test_the_owner_is_found_once_at_start_up(bot: AccessBot) -> None:
    ask = cast(AsyncMock, bot.application_info)

    await bot.access.resolve_owners()
    await bot.access.resolve_owners()

    assert bot.owner_id == OWNER_ID
    ask.assert_awaited_once()
    assert bot.access.is_owner(MagicMock(spec=discord.User, id=OWNER_ID))
    assert not bot.access.is_owner(MagicMock(spec=discord.User, id=MEMBER_ID))


async def test_a_team_s_admins_and_developers_own_the_bot(bot: AccessBot) -> None:
    team = team_of(
        (OWNER_ID, discord.TeamMemberRole.admin),
        (TEAM_DEVELOPER_ID, discord.TeamMemberRole.developer),
        (TEAM_READER_ID, discord.TeamMemberRole.read_only),
    )
    bot.application_info = AsyncMock(return_value=app_info(team=team))  # type: ignore[method-assign]

    await bot.access.resolve_owners()

    assert bot.owner_ids == {OWNER_ID, TEAM_DEVELOPER_ID}
    assert bot.owner_id is None
    owners = [
        bot.access.is_owner(MagicMock(spec=discord.User, id=user_id))
        for user_id in (OWNER_ID, TEAM_DEVELOPER_ID, TEAM_READER_ID)
    ]
    assert owners == [True, True, False]


async def test_a_team_without_admins_or_developers_owns_nothing(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    team = team_of((TEAM_READER_ID, discord.TeamMemberRole.read_only))
    bot.application_info = AsyncMock(return_value=app_info(team=team))  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot.access.resolve_owners()
        await bot.access.resolve_owners()

    assert not bot.access.is_owner(MagicMock(spec=discord.User, id=TEAM_READER_ID))
    assert logged(caplog, logging.WARNING) == [
        "The application's team has no admins or developers, so nobody can use "
        "the bot owner's commands"
    ]


async def test_owners_already_known_are_not_asked_for(bot: AccessBot) -> None:
    bot.owner_id = OWNER_ID

    await bot.access.resolve_owners()

    cast(AsyncMock, bot.application_info).assert_not_awaited()


@both_paths
async def test_the_owner_s_commands_are_for_the_owner_alone(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    general = channel(guild, GENERAL_ID)

    owner = make_context(bot, 'kill', make_owner(guild), general, slash=slash)
    admin = make_context(bot, 'kill', make_admin(guild), general, slash=slash)

    assert await refusal(owner) is None
    denied = await refusal(admin)
    assert decided(admin).outcome is Outcome.NOT_ALLOWED
    assert denied is not None and denied.silent is not slash


@both_paths
async def test_the_owner_is_not_an_admin(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)

    result = await outcome(
        bot,
        'grandfather',
        make_owner(guild),
        channel(guild, STAFF_CHANNEL_ID),
        slash=slash,
    )

    assert result is Outcome.NOT_ALLOWED


async def test_a_server_s_limits_never_apply_to_the_owner_s_commands(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(
        bot, limits={'kill': Limit(off=True), 'kill *': Limit(who=Who.ADMIN)}
    )

    result = await outcome(bot, 'kill', make_owner(guild), channel(guild, GENERAL_ID))

    assert result is Outcome.PUBLIC


async def test_is_owner_never_asks_discord(bot: AccessBot) -> None:
    assert not bot.access.is_owner(MagicMock(spec=discord.User, id=OWNER_ID))

    cast(AsyncMock, bot.application_info).assert_not_awaited()


async def test_member_commands_never_ask_for_the_owners(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)

    for name in ('ping', 'grandfather', 'status'):
        await outcome(bot, name, make_owner(guild), channel(guild, STAFF_CHANNEL_ID))

    cast(AsyncMock, bot.application_info).assert_not_awaited()


async def test_until_start_up_finds_the_owners_their_first_command_does(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)

    result = await outcome(bot, 'kill', make_owner(guild), channel(guild, GENERAL_ID))

    assert result is Outcome.PUBLIC
    cast(AsyncMock, bot.application_info).assert_awaited_once()


async def test_if_the_owners_cannot_be_found_an_owner_command_asks_again_later(
    bot: AccessBot,
    guild: MagicMock,
    clock: MonotonicClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await configure(bot)
    error = discord.HTTPException(MagicMock(status=503, reason='Unavailable'), 'down')
    ask = AsyncMock(side_effect=error)
    bot.application_info = ask  # type: ignore[method-assign]
    owner, general = make_owner(guild), channel(guild, GENERAL_ID)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot.access.resolve_owners()
        refused = await outcome(bot, 'kill', owner, general)
        clock.now += OWNER_RETRY_SECONDS - 1
        still_refused = await outcome(bot, 'kill', owner, general)
        asked_by_then = ask.await_count
        clock.now += 1
        await outcome(bot, 'kill', owner, general)  # asks again, and fails again
        asked_again = ask.await_count
        ask.side_effect = None
        ask.return_value = app_info()
        clock.now += OWNER_RETRY_SECONDS
        allowed = await outcome(bot, 'kill', owner, general)

    assert (refused, still_refused, allowed) == (
        Outcome.NOT_ALLOWED,
        Outcome.NOT_ALLOWED,
        Outcome.PUBLIC,
    )
    assert (asked_by_then, asked_again, ask.await_count) == (1, 2, 3)
    assert len(logged(caplog, logging.WARNING)) == 1
    assert logged(caplog, logging.WARNING)[0].startswith(
        "Could not find the bot's owners ("
    )
    assert OWNER_RETRY_SECONDS == 300


# Servers, private messages and the allow-list


async def test_private_messages_are_refused(bot: AccessBot, guild: MagicMock) -> None:
    dm = MagicMock(spec=discord.DMChannel, id=DM_CHANNEL_ID)
    user = MagicMock(spec=discord.User, id=MEMBER_ID)
    ctx = make_context(bot, 'ping', user, dm, guild=None)

    with pytest.raises(commands.NoPrivateMessage) as refused:
        await bot.access.check(ctx)

    # In the words of every other reply to private messages.
    assert str(refused.value) == PRIVATE_MESSAGES_TEXT
    assert cached_decision(ctx) is None


async def test_servers_outside_the_allow_list_are_refused_silently(
    bot: AccessBot, guild: MagicMock
) -> None:
    install_service(bot, AccessService(bot, allowed_guilds=frozenset({GUILD_ID})))
    await configure(bot)
    await configure(bot, guild_id=OTHER_GUILD_ID)
    other = make_guild(OTHER_GUILD_ID)
    outside = make_context(
        bot, 'ping', make_admin(other), channel(other, BOT_CHANNEL_ID)
    )
    inside = make_context(
        bot, 'ping', make_admin(guild), channel(guild, BOT_CHANNEL_ID)
    )

    denied = await refusal(outside)
    assert denied is not None
    await bot_error_handler(outside, denied)

    assert denied.silent
    cast(AsyncMock, outside.send).assert_not_awaited()
    assert await refusal(inside) is None


async def test_without_an_allow_list_every_server_may_use_the_bot(
    bot: AccessBot,
) -> None:
    allow_list = AccessService(bot, allowed_guilds=frozenset({GUILD_ID}))

    assert bot.access.guild_allowed(GUILD_ID)
    assert bot.access.guild_allowed(OTHER_GUILD_ID)
    assert allow_list.guild_allowed(GUILD_ID)
    assert not allow_list.guild_allowed(OTHER_GUILD_ID)
    # Outside a server, no.
    assert not bot.access.guild_allowed(None)
    assert not allow_list.guild_allowed(None)


# Limits


@both_paths
async def test_a_limit_on_a_group_s_own_command_leaves_its_subcommands_alone(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'clist': Limit(off=True)})
    member, place = make_member(guild), channel(guild, BOT_CHANNEL_ID)
    group = make_context(bot, 'clist', member, place, slash=slash)

    assert await refusal_text(group) == OFF_TEXT
    assert await outcome(bot, 'clist future', member, place, slash=slash) is (
        Outcome.PUBLIC
    )


@both_paths
async def test_a_limit_on_a_group_and_its_subcommands(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'clist *': Limit(off=True)})
    moderator, place = make_member(guild, 'Moderator'), channel(guild, BOT_CHANNEL_ID)

    for name in ('clist', 'clist future', 'clist purge'):
        assert await outcome(bot, name, moderator, place, slash=slash) is Outcome.OFF
    assert await outcome(bot, 'ping', moderator, place, slash=slash) is Outcome.PUBLIC


@both_paths
async def test_a_limit_on_a_subcommand_alone(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'clist future': Limit(who=Who.MODERATOR)})
    member, place = make_member(guild), channel(guild, BOT_CHANNEL_ID)
    moderator = make_member(guild, 'Moderator')

    assert await outcome(bot, 'clist future', member, place, slash=slash) is (
        Outcome.NOT_ALLOWED
    )
    assert await outcome(bot, 'clist future', moderator, place, slash=slash) is (
        Outcome.PUBLIC
    )
    assert await outcome(bot, 'clist', member, place, slash=slash) is Outcome.PUBLIC


@pytest.mark.parametrize(
    ('name', 'slash'),
    [('contests', False), ('contests', True), ('contests upcoming', False)],
    ids=[';contests', '/contests upcoming', ';contests upcoming'],
)
@pytest.mark.parametrize('key', ['contests', 'contests *'])
async def test_a_twin_shares_the_limits_of_the_command_it_is_a_twin_of(
    bot: AccessBot, guild: MagicMock, key: str, name: str, slash: bool
) -> None:
    await configure(bot, limits={key: Limit(off=True)})

    result = await outcome(
        bot, name, make_member(guild), channel(guild, BOT_CHANNEL_ID), slash=slash
    )

    assert result is Outcome.OFF


@pytest.mark.parametrize(
    ('key', 'limited'),
    [
        ('handle show', {'handle', 'handle show'}),
        ('handle show *', {'handle', 'handle show'}),
        ('handle *', {'handle', 'handle show'}),
    ],
)
async def test_a_group_s_own_command_shares_its_twin_s_limits(
    bot: AccessBot, guild: MagicMock, key: str, limited: set[str]
) -> None:
    # ;handle, the group's own callback, is the twin of handle show.
    await configure(bot, limits={key: Limit(where=Where.STAFF)})
    member, place = make_member(guild), channel(guild, BOT_CHANNEL_ID)

    refused = {
        name
        for name in ('handle', 'handle show', 'ping')
        if await outcome(bot, name, member, place) is Outcome.WRONG_CHANNEL
    }

    assert refused == limited


async def test_a_private_limit_keeps_slash_answers_private_and_refuses_prefix(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot, limits={'ping': Limit(private=True)})
    member, place = make_member(guild), channel(guild, BOT_CHANNEL_ID)
    prefix = make_context(bot, 'ping', member, place)

    assert await outcome(bot, 'ping', member, place, slash=True) is Outcome.PRIVATE
    assert await refusal_text(prefix) == PRIVATE_ONLY_TEXT.format(path='/ping')


@both_paths
async def test_a_place_limit_narrows_where_a_command_works(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'ping': Limit(where=Where.BOT_ONLY)})
    ctx = make_context(
        bot, 'ping', make_member(guild), channel(guild, GENERAL_ID), slash=slash
    )

    # No hint to use the slash command instead, which is refused too.
    assert await refusal_text(ctx) == BOT_CHANNELS_TEXT.format(
        channels=BOTH_BOT_CHANNELS
    )


async def test_one_server_s_limits_leave_other_servers_alone(
    bot: AccessBot, guild: MagicMock
) -> None:
    other = make_guild(OTHER_GUILD_ID)
    await configure(bot, limits={'ping': Limit(off=True)})
    await configure(bot, guild_id=OTHER_GUILD_ID)

    limited = await outcome(
        bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID)
    )
    unlimited = await outcome(
        bot, 'ping', make_member(other), channel(other, BOT_CHANNEL_ID)
    )

    assert (limited, unlimited) == (Outcome.OFF, Outcome.PUBLIC)


async def test_the_access_commands_take_no_limits(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(
        bot,
        limits={'access *': Limit(off=True), 'access staff-channel': Limit(off=True)},
    )
    admin, staff = make_admin(guild), channel(guild, STAFF_CHANNEL_ID)

    for name in ('access', 'access staff-channel'):
        assert await outcome(bot, name, admin, staff) is Outcome.PUBLIC


# Until a server has a staff channel


@both_paths
async def test_without_a_staff_channel_admins_use_access_anywhere(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, staff_channel=None)
    general = channel(guild, GENERAL_ID)

    for name in ('access', 'access staff-channel'):
        assert await outcome(bot, name, make_admin(guild), general, slash=slash) is (
            Outcome.PUBLIC
        )
        # Still for admins alone.
        assert await outcome(bot, name, make_member(guild), general, slash=slash) is (
            Outcome.NOT_ALLOWED
        )


async def test_with_a_staff_channel_access_belongs_there(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    admin = make_admin(guild)
    elsewhere = make_context(bot, 'access', admin, channel(guild, GENERAL_ID))

    # Its slash command answers privately anywhere, so the refusal offers it.
    assert await refusal_text(elsewhere) == with_hint(
        STAFF_CHANNEL_TEXT, '/access show'
    )
    assert await outcome(bot, 'access', admin, channel(guild, STAFF_CHANNEL_ID)) is (
        Outcome.PUBLIC
    )


async def test_only_the_access_commands_work_anywhere_without_a_staff_channel(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot, staff_channel=None)
    ctx = make_context(
        bot, 'grandfather', make_admin(guild), channel(guild, GENERAL_ID)
    )

    assert await refusal_text(ctx) == with_hint(
        NO_STAFF_CHANNEL_ADMIN_TEXT, '/grandfather'
    )


# Once the staff channel is gone


@both_paths
async def test_a_staff_channel_that_is_gone_counts_as_none(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    # As before there was one, an admin by role alone, who doesn't see
    # /access, can set another with ;access in any channel.
    known_to(bot, guild)
    hide_access(bot)
    await configure(bot, staff_channel=GONE_ID)
    role_admin, member = make_member(guild, 'Admin'), make_member(guild)

    for place in (GENERAL_ID, BOT_CHANNEL_ID):
        for name in ('access', 'access staff-channel'):
            here = channel(guild, place)
            assert await outcome(bot, name, role_admin, here, slash=slash) is (
                Outcome.PUBLIC
            )
            # Still for admins alone.
            assert await outcome(bot, name, member, here, slash=slash) is (
                Outcome.NOT_ALLOWED
            )
    # Settings stay as stored: /access show warns that the channel is gone.
    assert bot.access.guild_access(GUILD_ID).staff_channel == GONE_ID


async def test_staff_commands_never_send_anyone_to_a_staff_channel_that_is_gone(
    bot: AccessBot, guild: MagicMock
) -> None:
    known_to(bot, guild)
    hide_access(bot)
    await configure(bot, staff_channel=GONE_ID)
    general = channel(guild, GENERAL_ID)

    admin = make_context(bot, 'grandfather', make_member(guild, 'Admin'), general)
    moderator = make_context(
        bot, 'roleupdate', make_member(guild, 'Moderator'), general
    )
    developer = make_context(
        bot, 'status', make_member(guild, DEVELOPER_ROLE_ID), general, slash=True
    )

    # Admins learn how to set another; other staff that it can't be used here.
    assert await refusal_text(admin) == with_hint(
        NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT, '/grandfather'
    )
    assert await refusal_text(moderator) == with_hint(NOT_HERE_TEXT, '/roleupdate')
    assert await refusal_text(developer) == NOT_HERE_TEXT


async def test_until_the_bot_has_the_server_its_staff_channel_stands(
    bot: AccessBot, guild: MagicMock
) -> None:
    # The bot can't tell that the channel is gone, so the rules fail closed.
    await configure(bot, staff_channel=GONE_ID)
    admin, general = make_member(guild, 'Admin'), channel(guild, GENERAL_ID)

    assert (
        await outcome(bot, 'access staff-channel', admin, general)
        is Outcome.WRONG_CHANNEL
    )


# The refusals' texts


@both_paths
async def test_a_switched_off_command_says_so(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot, limits={'ping': Limit(off=True)})
    ctx = make_context(
        bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID), slash=slash
    )

    denied = await refusal(ctx)
    assert denied is not None
    await bot_error_handler(ctx, denied)

    text, options = sent(ctx)
    assert text == OFF_TEXT == 'This command is switched off in this server.'
    # A prefix refusal goes after 20 seconds; only the member sees a slash one.
    assert options == (
        {'ephemeral': True} if slash else {'ephemeral': True, 'delete_after': 20}
    )


async def test_only_the_bot_channels_the_member_can_see_are_listed(
    bot: AccessBot, guild: MagicMock
) -> None:
    ids = [BOT_CHANNEL_ID + offset for offset in range(10, 17)]
    for channel_id in ids:
        make_channel(guild, channel_id, visible=channel_id != ids[1])
    await configure(bot, bot_channels=tuple(reversed(ids)))
    ctx = make_context(bot, 'challenge', make_member(guild), channel(guild, GENERAL_ID))

    text = await refusal_text(ctx)

    shown = [ids[0], *ids[2:6]]
    assert len(shown) == LISTED_CHANNELS == 5
    channels = ', '.join(f'<#{channel_id}>' for channel_id in shown)
    assert text == BOT_CHANNELS_TEXT.format(channels=channels)


async def test_bot_channels_the_member_cannot_see_are_not_named(
    bot: AccessBot, guild: MagicMock
) -> None:
    make_channel(guild, BOT_CHANNEL_ID + 10, visible=False)
    # A channel that has gone since it was chosen.
    await configure(bot, bot_channels=(BOT_CHANNEL_ID + 10, BOT_CHANNEL_ID + 11))
    ctx = make_context(bot, 'ping', make_member(guild), channel(guild, GENERAL_ID))

    assert await refusal_text(ctx) == with_hint(A_BOT_CHANNEL_TEXT, '/ping')


@pytest.mark.parametrize(
    ('make', 'text'),
    [(make_admin, NO_BOT_CHANNEL_ADMIN_TEXT), (make_member, NO_BOT_CHANNEL_TEXT)],
    ids=['admin', 'member'],
)
async def test_without_bot_channels_admins_learn_how_to_add_one(
    bot: AccessBot,
    guild: MagicMock,
    make: Callable[[MagicMock], MagicMock],
    text: str,
) -> None:
    await configure(bot, bot_channels=())
    ctx = make_context(bot, 'ping', make(guild), channel(guild, GENERAL_ID))

    assert await refusal_text(ctx) == with_hint(text, '/ping')
    assert NO_BOT_CHANNEL_ADMIN_TEXT == (
        'There is no bot channel yet. Add one with `/access bot-channels add`.'
    )
    assert NO_BOT_CHANNEL_TEXT == (
        'This command only works in a bot channel, and this server has none yet. '
        'Ask an admin to add one.'
    )


@pytest.mark.parametrize(
    'staff_channel', [None, STAFF_CHANNEL_ID], ids=['no staff channel', 'staff channel']
)
async def test_an_admin_who_does_not_see_access_learns_its_prefix_command(
    bot: AccessBot, guild: MagicMock, staff_channel: int | None
) -> None:
    # Discord shows /access only to members with Manage Server, so an admin by
    # role alone sets the server up with ;access, in the staff channel once
    # there is one.
    hide_access(bot)
    await configure(bot, bot_channels=(), staff_channel=staff_channel)
    general = channel(guild, GENERAL_ID)

    by_role = make_context(bot, 'ping', make_member(guild, 'Admin'), general)
    by_permission = make_context(bot, 'ping', make_admin(guild), general)

    where = '' if staff_channel is None else IN_THE_STAFF_CHANNEL
    assert await refusal_text(by_role) == with_hint(
        NO_BOT_CHANNEL_ADMIN_PREFIX_TEXT.format(where=where), '/ping'
    )
    assert await refusal_text(by_permission) == with_hint(
        NO_BOT_CHANNEL_ADMIN_TEXT, '/ping'
    )
    assert NO_BOT_CHANNEL_ADMIN_PREFIX_TEXT.format(where=IN_THE_STAFF_CHANNEL) == (
        'There is no bot channel yet. Add one with '
        '`;access bot-channels add #channel` in the staff channel.'
    )


@both_paths
async def test_a_bot_only_command_gets_no_slash_hint(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    await configure(bot)
    ctx = make_context(
        bot, 'challenge', make_member(guild), channel(guild, GENERAL_ID), slash=slash
    )

    assert await refusal_text(ctx) == BOT_CHANNELS_TEXT.format(
        channels=BOTH_BOT_CHANNELS
    )


async def test_a_prefix_command_without_a_slash_form_gets_no_slash_hint(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    ctx = make_context(bot, 'gimme', make_member(guild), channel(guild, GENERAL_ID))

    assert await refusal_text(ctx) == BOT_CHANNELS_TEXT.format(
        channels=BOTH_BOT_CHANNELS
    )


async def test_a_slash_hint_names_the_command_a_member_would_pick(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot, bot_channels=())
    member, general = make_member(guild), channel(guild, GENERAL_ID)

    texts = [
        await refusal_text(make_context(bot, name, member, general))
        for name in ('clist', 'contests upcoming', 'handle')
    ]

    assert texts == [
        with_hint(NO_BOT_CHANNEL_TEXT, path)
        for path in ('/clist show', '/contests upcoming', '/handle show')
    ]


@pytest.mark.parametrize(
    ('permissions', 'hinted'),
    [
        (discord.Permissions.none(), False),
        (discord.Permissions(manage_messages=True), True),
        (discord.Permissions(administrator=True), True),
    ],
    ids=['moderator by role', 'with Manage Messages', 'administrator'],
)
async def test_a_slash_hint_names_only_a_command_the_member_s_list_shows(
    bot: AccessBot, guild: MagicMock, permissions: discord.Permissions, hinted: bool
) -> None:
    # The whole /clist tree is hidden behind Manage Messages.
    clist = bot.get_command('clist')
    assert isinstance(clist, commands.HybridGroup)
    clist.app_command.default_permissions = discord.Permissions(manage_messages=True)
    await configure(bot)
    moderator = make_member(guild, 'Moderator')
    # What discord.py gives for those permissions, administrator's included.
    moderator.guild_permissions = (
        discord.Permissions.all() if permissions.administrator else permissions
    )
    general = channel(guild, GENERAL_ID)

    text = await refusal_text(make_context(bot, 'clist purge', moderator, general))

    listing = BOT_CHANNELS_TEXT.format(channels=BOTH_BOT_CHANNELS)
    assert text == (with_hint(listing, '/clist purge') if hinted else listing)


async def test_a_private_command_hidden_from_the_member_has_no_slash_form_to_offer(
    bot: AccessBot, guild: MagicMock
) -> None:
    whisper = bot.tree.get_command('whisper')
    assert whisper is not None
    whisper.default_permissions = discord.Permissions(manage_guild=True)
    await configure(bot)
    place = channel(guild, BOT_CHANNEL_ID)

    member = await refusal_text(make_context(bot, 'whisper', make_member(guild), place))
    admin = await refusal_text(make_context(bot, 'whisper', make_admin(guild), place))

    assert member == NOT_HERE_TEXT
    assert admin == PRIVATE_ONLY_TEXT.format(path='/whisper')


async def test_staff_on_prefix_are_sent_to_the_staff_channel_unnamed(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    general = channel(guild, GENERAL_ID)
    moderator = make_context(
        bot, 'roleupdate', make_member(guild, 'Moderator'), general
    )
    developer = make_context(
        bot, 'status', make_member(guild, DEVELOPER_ROLE_ID), general
    )

    # The slash command of a staff command answers privately outside the
    # staff channel, unless it works only there.
    assert await refusal_text(moderator) == with_hint(STAFF_CHANNEL_TEXT, '/roleupdate')
    assert await refusal_text(developer) == STAFF_CHANNEL_TEXT
    assert STAFF_CHANNEL_TEXT == 'Use this command in the staff channel.'


@pytest.mark.parametrize(
    ('permissions', 'hinted'),
    [
        (discord.Permissions.none(), False),
        (discord.Permissions(manage_messages=True), True),
    ],
    ids=['moderator by role', 'with Manage Messages'],
)
async def test_a_staff_refusal_offers_only_a_slash_command_the_member_s_list_shows(
    bot: AccessBot, guild: MagicMock, permissions: discord.Permissions, hinted: bool
) -> None:
    # As the slash pass hides a tree of moderators' commands.
    roleupdate = bot.tree.get_command('roleupdate')
    assert roleupdate is not None
    roleupdate.default_permissions = discord.Permissions(manage_messages=True)
    await configure(bot)
    moderator = make_member(guild, 'Moderator')
    moderator.guild_permissions = permissions
    ctx = make_context(bot, 'roleupdate', moderator, channel(guild, GENERAL_ID))

    text = await refusal_text(ctx)

    assert text == (
        with_hint(STAFF_CHANNEL_TEXT, '/roleupdate') if hinted else STAFF_CHANNEL_TEXT
    )


async def test_staff_on_slash_are_shown_the_staff_channel(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    ctx = make_context(
        bot,
        'status',
        make_member(guild, DEVELOPER_ROLE_ID),
        channel(guild, GENERAL_ID),
        slash=True,
    )

    text = await refusal_text(ctx)

    assert text == STAFF_CHANNEL_SLASH_TEXT.format(channel_id=STAFF_CHANNEL_ID)
    assert text == f'Use this command in <#{STAFF_CHANNEL_ID}>.'


@both_paths
@pytest.mark.parametrize(
    ('make', 'text'),
    [
        (make_admin, NO_STAFF_CHANNEL_ADMIN_TEXT),
        (lambda guild: make_member(guild, DEVELOPER_ROLE_ID), NOT_HERE_TEXT),
    ],
    ids=['admin', 'developer'],
)
async def test_without_a_staff_channel_admins_learn_how_to_set_one(
    bot: AccessBot,
    guild: MagicMock,
    make: Callable[[MagicMock], MagicMock],
    text: str,
    slash: bool,
) -> None:
    await configure(bot, staff_channel=None)
    ctx = make_context(
        bot, 'status', make(guild), channel(guild, GENERAL_ID), slash=slash
    )

    assert await refusal_text(ctx) == text
    assert NO_STAFF_CHANNEL_ADMIN_TEXT == (
        'There is no staff channel yet. Set one with `/access staff-channel`.'
    )


@both_paths
async def test_an_admin_who_does_not_see_access_learns_how_to_set_a_staff_channel(
    bot: AccessBot, guild: MagicMock, slash: bool
) -> None:
    hide_access(bot)
    await configure(bot, staff_channel=None)
    general = channel(guild, GENERAL_ID)

    by_role = make_context(
        bot, 'status', make_member(guild, 'Admin'), general, slash=slash
    )
    by_permission = make_context(bot, 'status', make_admin(guild), general, slash=slash)

    assert await refusal_text(by_role) == NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT
    assert await refusal_text(by_permission) == NO_STAFF_CHANNEL_ADMIN_TEXT
    assert NO_STAFF_CHANNEL_ADMIN_PREFIX_TEXT == (
        'There is no staff channel yet. Set one with `;access staff-channel #channel`.'
    )


@pytest.mark.parametrize(
    ('where', 'slash', 'text'),
    [
        (Where.STAFF_ONLY, True, NOT_HERE_TEXT),
        (Where.STAFF_ONLY, False, NOT_HERE_TEXT),
        # Its slash command answers the member privately here.
        (Where.STAFF, False, with_hint(NOT_HERE_TEXT, '/ping')),
    ],
    ids=['staff-only, slash', 'staff-only, prefix', 'staff, prefix'],
)
async def test_members_are_never_shown_the_staff_channel(
    bot: AccessBot, guild: MagicMock, where: Where, slash: bool, text: str
) -> None:
    await configure(bot, limits={'ping': Limit(where=where)})
    ctx = make_context(
        bot, 'ping', make_member(guild), channel(guild, GENERAL_ID), slash=slash
    )

    assert await refusal_text(ctx) == text
    assert NOT_HERE_TEXT == "This command can't be used here."


async def test_a_private_command_on_prefix_points_to_its_slash_form(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    member, place = make_member(guild), channel(guild, BOT_CHANNEL_ID)

    before = await refusal_text(make_context(bot, 'whisper', member, place))
    bot.tree.remove_command('whisper')
    after = await refusal_text(make_context(bot, 'whisper', member, place))

    assert (
        before
        == PRIVATE_ONLY_TEXT.format(path='/whisper')
        == (
            'In this server only the person who uses this command sees its answer: '
            'use `/whisper`.'
        )
    )
    assert after == NOT_HERE_TEXT


async def test_refusals_never_name_a_role_or_an_id(
    bot: AccessBot, guild: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', ADMIN_ROLE_ID)
    await configure(bot, limits={'ping': Limit(where=Where.STAFF_ONLY)})
    general = channel(guild, GENERAL_ID)
    member = make_member(guild, 'Trusted')
    cases = [
        ('ping', member, False),
        ('ping', member, True),
        ('clist purge', member, True),
        ('roleupdate', make_member(guild, 'Moderator'), False),
        ('status', make_member(guild, DEVELOPER_ROLE_ID), False),
        ('whisper', member, False),
    ]

    texts = [
        await refusal_text(make_context(bot, name, who, general, slash=slash))
        for name, who, slash in cases
    ]

    for text in texts:
        assert text is not None
        for secret in ('Admin', 'Moderator', 'Trusted', 'Developer'):
            assert secret not in text
        for number in (ADMIN_ROLE_ID, DEVELOPER_ROLE_ID, STAFF_CHANNEL_ID, GUILD_ID):
            assert str(number) not in text


async def test_decide_answers_without_refusing_or_keeping_anything(
    bot: AccessBot, guild: MagicMock
) -> None:
    # As /help asks, for every command, whether it works here.
    await configure(bot)
    member, general = make_member(guild), channel(guild, GENERAL_ID)
    decide = bot.access.decide

    results = [
        (await decide(cast(Any, bot.get_command(name)), member, general, slash=slash))
        for name, slash in (('ping', True), ('ping', False), ('clist purge', True))
    ]

    assert [(result.outcome, result.slash) for result in results] == [
        (Outcome.PRIVATE, True),
        (Outcome.WRONG_CHANNEL, False),
        (Outcome.NOT_ALLOWED, True),
    ]
    assert results[0].rule == Effective(frozenset({Who.EVERYONE}), Where.BOT)


async def test_a_user_who_is_not_a_member_has_no_roles(bot: AccessBot) -> None:
    bot.owner_id = OWNER_ID

    stranger = bot.access.asker(MagicMock(spec=discord.User, id=MEMBER_ID))
    owner = bot.access.asker(MagicMock(spec=discord.User, id=OWNER_ID))

    assert stranger == Asker()
    assert owner == Asker(owner=True)


async def test_a_member_s_roles_and_permissions_are_read(
    bot: AccessBot, guild: MagicMock
) -> None:
    member = make_member(
        guild, 'Admin', 'Moderator', 'Trusted', DEVELOPER_ROLE_ID, manage_guild=True
    )

    assert bot.access.asker(member) == Asker(
        manage_guild=True,
        admin_role=True,
        moderator_role=True,
        trusted_role=True,
        developer_role=True,
    )


async def test_the_spot_of_a_thread_is_the_channel_it_is_in(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)

    spot = bot.access.spot(thread_in(guild, BOT_CHANNEL_ID), GUILD_ID, slash=True)
    other = bot.access.spot(object(), OTHER_GUILD_ID, slash=False)

    assert spot == Spot(
        slash=True,
        channel_id=BOT_CHANNEL_ID,
        bot_channels=frozenset({BOT_CHANNEL_ID, SECOND_BOT_CHANNEL_ID}),
        staff_channel=STAFF_CHANNEL_ID,
    )
    assert other == Spot(slash=False, channel_id=None)


async def test_an_allowed_decision_has_no_refusal(
    bot: AccessBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID))
    rule = Effective(frozenset({Who.EVERYONE}), Where.BOT)

    for result in (Outcome.PUBLIC, Outcome.PRIVATE):
        with pytest.raises(ValueError):
            bot.access.denial(ctx, Decision(result, rule, slash=False))


# The check itself


@both_paths
async def test_running_the_check_twice_changes_nothing(
    bot: AccessBot,
    guild: MagicMock,
    refusal_throttle: RefusalThrottle,
    slash: bool,
) -> None:
    await configure(bot)
    settings = bot.access.guild_access(GUILD_ID)
    member = make_member(guild)
    allowed = make_context(
        bot, 'ping', member, channel(guild, BOT_CHANNEL_ID), slash=slash
    )
    refused = make_context(
        bot, 'challenge', member, channel(guild, GENERAL_ID), slash=slash
    )

    assert await bot.access.check(allowed)
    first = decided(allowed)
    assert await bot.access.check(allowed)
    texts = []
    for _ in range(2):
        with pytest.raises(AccessDenied) as denied:
            await bot.access.check(refused)
        texts.append(denied.value.text)

    assert decided(allowed) == first
    assert texts[0] == texts[1] == BOT_CHANNELS_TEXT.format(channels=BOTH_BOT_CHANNELS)
    assert bot.access.guild_access(GUILD_ID) is settings
    # Prefix refusals are throttled by the error handler, never the check.
    assert len(refusal_throttle) == 0
    cast(AsyncMock, bot.application_info).assert_not_awaited()


async def test_only_the_error_handler_throttles_prefix_refusals(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    member, general = make_member(guild), channel(guild, GENERAL_ID)
    first = make_context(bot, 'ping', member, general)
    again = make_context(bot, 'ping', member, general)

    for ctx in (first, again):
        denied = await refusal(ctx)
        assert denied is not None and denied.text is not None
        await bot_error_handler(ctx, denied)

    text, _ = sent(first)
    assert text is not None and text.startswith('Use this command in a bot channel')
    cast(AsyncMock, again.send).assert_not_awaited()


async def test_a_decision_is_kept_for_its_own_command(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    ctx = make_context(
        bot, 'clist', make_member(guild), channel(guild, GENERAL_ID), slash=True
    )

    await refusal(ctx)
    decision = decided(ctx)
    ctx.command = bot.get_command('clist future')

    assert decision.outcome is Outcome.PRIVATE
    assert cached_decision(ctx) is None
    ctx.command = bot.get_command('clist')
    assert cached_decision(ctx) is decision


def test_a_context_without_a_command_keeps_no_decision() -> None:
    ctx = MagicMock(spec=commands.Context)
    ctx.command = None
    rule = Effective(frozenset({Who.EVERYONE}), Where.BOT)

    cache_decision(ctx, Decision(Outcome.PUBLIC, rule, slash=True))

    assert cached_decision(ctx) is None


async def test_a_command_without_a_rule_is_for_the_owner_in_the_staff_channel(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot)
    general, staff = channel(guild, GENERAL_ID), channel(guild, STAFF_CHANNEL_ID)

    assert (
        await outcome(bot, 'mystery', make_admin(guild), staff) is Outcome.NOT_ALLOWED
    )
    assert await outcome(bot, 'mystery', make_owner(guild), staff) is Outcome.PUBLIC
    assert await outcome(bot, 'mystery', make_owner(guild), general, slash=True) is (
        Outcome.WRONG_CHANNEL
    )


async def test_commands_without_a_rule_are_reported_once_each(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        unruled = bot.access.report_unruled(
            [*bot.walk_commands(), *bot.walk_commands()]
        )

    # The twins have their rules through the commands they are twins of.
    assert unruled == ['mystery']
    assert logged(caplog) == [
        'Command mystery has no access rule, so only the bot owner can use it, '
        'and only in the staff channel'
    ]


# How to use a command as a slash command


@pytest.mark.parametrize(
    ('name', 'path'),
    [
        ('ping', '/ping'),
        ('clist', '/clist show'),
        ('clist future', '/clist future'),
        ('contests', '/contests upcoming'),
        ('contests upcoming', '/contests upcoming'),
        ('handle', '/handle show'),
        ('handle show', '/handle show'),
        ('access staff-channel', '/access staff-channel'),
        ('gimme', None),
    ],
)
async def test_slash_path(bot: AccessBot, name: str, path: str | None) -> None:
    command = bot.get_command(name)
    assert command is not None

    assert bot.access.slash_path(command) == path


async def test_a_command_taken_out_of_the_tree_has_no_slash_path(
    bot: AccessBot,
) -> None:
    clist = bot.get_command('clist')
    assert isinstance(clist, commands.HybridGroup)

    clist.app_command.remove_command('future')
    bot.tree.remove_command('ping')

    paths = {
        name: bot.access.slash_path(
            cast(commands.Command[Any, ..., Any], bot.get_command(name))
        )
        for name in ('ping', 'clist', 'clist future')
    }
    assert paths == {'ping': None, 'clist': '/clist show', 'clist future': None}
    # The prefix commands stay.
    assert bot.get_command('clist future') is not None
    bot.tree.remove_command('clist')
    assert bot.access.slash_path(clist) is None


async def test_a_listed_slash_path_is_one_the_member_s_slash_list_shows(
    bot: AccessBot, guild: MagicMock
) -> None:
    hidden = discord.Permissions(manage_guild=True)
    whisper_app = bot.tree.get_command('whisper')
    clist_app = bot.tree.get_command('clist')
    assert whisper_app is not None and clist_app is not None
    whisper_app.default_permissions = hidden
    # A subcommand counts its top-level group's permissions.
    clist_app.default_permissions = hidden
    member, admin = make_member(guild), make_admin(guild)
    user = MagicMock(spec=discord.User, id=MEMBER_ID)

    def listed(name: str, who: Any) -> str | None:
        command = bot.get_command(name)
        assert command is not None
        return bot.access.listed_slash_path(command, who)

    assert [listed(name, member) for name in ('whisper', 'clist future', 'ping')] == [
        None,
        None,
        '/ping',
    ]
    assert [listed(name, admin) for name in ('whisper', 'clist future')] == [
        '/whisper',
        '/clist future',
    ]
    # Someone who isn't a member sees only what nothing hides.
    assert (listed('whisper', user), listed('ping', user)) == (None, '/ping')


async def test_a_group_without_a_fallback_has_no_slash_path_of_its_own(
    bot: AccessBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(table, 'TWINS', MappingProxyType({}))
    handle = bot.get_command('handle')
    assert handle is not None

    assert bot.access.slash_path(handle) is None


# Buttons


def make_interaction(user: MagicMock, *, guild_id: int | None = GUILD_ID) -> MagicMock:
    """A press of a button, by ``user`` in server ``guild_id``."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = guild_id
    interaction.user = user
    interaction.is_expired.return_value = False
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def answered(interaction: MagicMock) -> str | None:
    """The text of the one private alert that answered ``interaction``."""
    send = cast(AsyncMock, interaction.response.send_message)
    send.assert_awaited_once()
    assert send.await_args is not None
    assert send.await_args.kwargs['ephemeral'] is True
    embed = send.await_args.kwargs['embed']
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    description: str | None = embed.description
    return description


async def test_a_button_works_for_anyone_who_may_use_its_command_anywhere(
    bot: AccessBot, guild: MagicMock
) -> None:
    # The reply was posted where its command could answer.
    await configure(bot)
    interaction = make_interaction(make_member(guild))
    interaction.channel = channel(guild, GENERAL_ID)

    assert await bot.access.component_allowed(interaction, 'challenge')
    cast(AsyncMock, interaction.response.send_message).assert_not_awaited()


@pytest.mark.parametrize(
    ('limits', 'text'),
    [
        ({}, NOT_ALLOWED_MESSAGE),
        ({'clist *': Limit(off=True)}, NOT_ALLOWED_MESSAGE),  # who comes first
    ],
)
async def test_a_button_refuses_members_who_may_not_use_its_command(
    bot: AccessBot, guild: MagicMock, limits: dict[str, Limit], text: str
) -> None:
    await configure(bot, limits=limits)
    interaction = make_interaction(make_member(guild))

    assert not await bot.access.component_allowed(interaction, 'clist purge')
    assert answered(interaction) == text


async def test_a_button_of_a_switched_off_command_says_so(
    bot: AccessBot, guild: MagicMock
) -> None:
    await configure(bot, limits={'challenge': Limit(off=True)})
    interaction = make_interaction(make_member(guild))

    assert not await bot.access.component_allowed(interaction, 'challenge')
    assert answered(interaction) == OFF_TEXT


async def test_a_button_in_a_server_with_broken_settings_says_so(
    bot: AccessBot, guild: MagicMock, user_db: Any
) -> None:
    await user_db.set_access_settings(GUILD_ID, '{"version": 2}')
    bot.access.use_user_db(user_db)
    await bot.access.load()
    interaction = make_interaction(make_member(guild))

    assert not await bot.access.component_allowed(interaction, 'challenge')
    assert answered(interaction) == REPAIR_TEXT


@pytest.mark.parametrize(
    ('guild_id', 'text'),
    [(None, PRIVATE_MESSAGES_TEXT), (OTHER_GUILD_ID, NOT_AVAILABLE_TEXT)],
    ids=['private message', 'server outside the allow-list'],
)
async def test_a_button_outside_the_allowed_servers_is_refused(
    bot: AccessBot, guild_id: int | None, text: str
) -> None:
    install_service(bot, AccessService(bot, allowed_guilds=frozenset({GUILD_ID})))
    other = make_guild(OTHER_GUILD_ID)
    interaction = make_interaction(make_admin(other), guild_id=guild_id)

    assert not await bot.access.component_allowed(interaction, 'ping')
    assert answered(interaction) == text


async def test_a_button_of_the_owner_s_command_is_for_the_owner(
    bot: AccessBot, guild: MagicMock
) -> None:
    owner = make_interaction(make_owner(guild))
    admin = make_interaction(make_admin(guild))

    assert await bot.access.component_allowed(owner, 'kill')
    assert not await bot.access.component_allowed(admin, 'kill')


async def test_a_button_of_a_command_without_a_rule_is_for_the_owner(
    bot: AccessBot, guild: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        member = await bot.access.component_allowed(
            make_interaction(make_admin(guild)), 'duel accept'
        )
        owner = await bot.access.component_allowed(
            make_interaction(make_owner(guild)), 'duel accept'
        )

    assert (member, owner) == (False, True)
    # Once, however often its buttons are pressed.
    assert logged(caplog) == ['No access rule for the buttons of command duel accept']


async def test_a_refusal_that_cannot_be_sent_still_refuses(
    bot: AccessBot, guild: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    interaction = make_interaction(make_member(guild))
    interaction.response.send_message.side_effect = discord.NotFound(
        MagicMock(status=404, reason='Not Found'), 'Unknown interaction'
    )

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        allowed = await bot.access.component_allowed(interaction, 'clist purge')

    assert not allowed
    assert len(logged(caplog, logging.WARNING)) == 1


# Loading and changing the settings


async def test_settings_are_read_once_at_start_up(bot: AccessBot, user_db: Any) -> None:
    first = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
    second = GuildAccess(limits={'ping': Limit(off=True)})
    await user_db.set_access_settings(GUILD_ID, encode(first))
    await user_db.set_access_settings(OTHER_GUILD_ID, encode(second))
    bot.access.use_user_db(user_db)

    await bot.access.load()

    assert bot.access.guild_access(GUILD_ID) == first
    assert bot.access.guild_access(OTHER_GUILD_ID) == second
    # A server without a row has the defaults.
    assert bot.access.guild_access(OTHER_GUILD_ID + 1) == GuildAccess()


async def test_a_change_is_stored_and_read_back_at_the_next_start(
    bot: AccessBot, user_db: Any
) -> None:
    bot.access.use_user_db(user_db)
    await bot.access.load()

    changed = await bot.access.change(
        GUILD_ID, lambda access: access.with_limit('ping *', Limit(who=Who.TRUSTED))
    )
    restarted = AccessService(bot)
    restarted.use_user_db(user_db)
    await restarted.load()

    assert changed == GuildAccess(limits={'ping *': Limit(who=Who.TRUSTED)})
    assert bot.access.guild_access(GUILD_ID) == changed
    assert await user_db.get_all_access_settings() == [(GUILD_ID, encode(changed))]
    assert restarted.guild_access(GUILD_ID) == changed
    assert bot.access.persistent and restarted.persistent


async def test_a_change_that_cannot_be_stored_changes_nothing(
    bot: AccessBot, user_db: Any
) -> None:
    bot.access.use_user_db(user_db)
    await configure(bot)
    before = bot.access.guild_access(GUILD_ID)
    user_db.set_access_settings = AsyncMock(
        side_effect=sqlite3.OperationalError('database is locked')
    )

    with pytest.raises(sqlite3.OperationalError):
        await bot.access.change(GUILD_ID, lambda access: access.without_limits())
    with pytest.raises(ValueError):
        await bot.access.change(
            GUILD_ID, lambda access: access.with_limit('not *a key', Limit(off=True))
        )

    assert bot.access.guild_access(GUILD_ID) is before


async def test_changes_to_one_server_wait_for_each_other(bot: AccessBot) -> None:
    # Each change edits the settings it finds, so the second must find the
    # first's.
    release = asyncio.Event()
    writes: list[tuple[int, str]] = []

    async def slow_write(guild_id: int, settings: str) -> None:
        writes.append((guild_id, settings))
        if len(writes) == 1:
            await release.wait()

    store = MagicMock()
    store.set_access_settings = AsyncMock(side_effect=slow_write)
    bot.access.use_user_db(store)

    def limit(name: str) -> Callable[[GuildAccess], GuildAccess]:
        return lambda access: access.with_limit(name, Limit(off=True))

    first = asyncio.create_task(bot.access.change(GUILD_ID, limit('ping')))
    second = asyncio.create_task(bot.access.change(GUILD_ID, limit('gimme')))
    for _ in range(5):
        await asyncio.sleep(0)
    # Another server's change doesn't wait.
    await bot.access.change(OTHER_GUILD_ID, limit('ping'))
    stored_while_waiting = len(writes)
    release.set()
    await asyncio.gather(first, second)

    assert stored_while_waiting == 2
    assert set(bot.access.guild_access(GUILD_ID).limits) == {'ping', 'gimme'}
    assert [guild_id for guild_id, _ in writes] == [GUILD_ID, OTHER_GUILD_ID, GUILD_ID]


async def test_without_a_database_settings_live_in_memory(
    bot: AccessBot, caplog: pytest.LogCaptureFixture
) -> None:
    bot.access.use_user_db(None)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot.access.load()
        changed = await bot.access.change(
            GUILD_ID, lambda _: GuildAccess(staff_channel=1)
        )

    assert not bot.access.persistent
    assert bot.access.guild_access(GUILD_ID) is changed
    assert logged(caplog) == [
        'There is no database, so access settings are kept in memory and lost '
        'when the bot stops',
        f'Changed the access settings of guild {GUILD_ID}',
    ]


async def test_a_bad_part_of_a_row_is_logged_with_its_server(
    bot: AccessBot, guild: MagicMock, user_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    row = (
        '{"version": 1, "bot_channels": [%d, "x"], "staff_channel": null, '
        '"limits": {"ping": {"who": "everyone", "where": null, "private": false, '
        '"off": false}}}' % BOT_CHANNEL_ID
    )
    await user_db.set_access_settings(GUILD_ID, row)
    bot.access.use_user_db(user_db)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot.access.load()

    assert logged(caplog, logging.WARNING) == [
        f"Access settings of guild {GUILD_ID}: bot_channels: dropped ['x'], not "
        'channel ids',
        f"Access settings of guild {GUILD_ID}: limit 'ping': who is 'everyone'; "
        'switched off',
    ]
    assert logged(caplog, logging.INFO) == ['Loaded the access settings of 1 server']
    # Read in the way that tightens.
    assert bot.access.guild_access(GUILD_ID).bot_channels == {BOT_CHANNEL_ID}
    assert (
        await outcome(bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID))
        is Outcome.OFF
    )


# Broken settings


@pytest.fixture
async def broken(bot: AccessBot, user_db: Any, caplog: pytest.LogCaptureFixture) -> Any:
    """GUILD_ID's stored settings are unreadable; OTHER_GUILD_ID's are fine."""
    await user_db.set_access_settings(GUILD_ID, 'not json')
    await user_db.set_access_settings(
        OTHER_GUILD_ID, encode(GuildAccess(frozenset({BOT_CHANNEL_ID})))
    )
    bot.access.use_user_db(user_db)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot.access.load()
    return user_db


@both_paths
async def test_a_server_with_unreadable_settings_refuses_its_commands(
    bot: AccessBot, guild: MagicMock, broken: Any, slash: bool
) -> None:
    ctx = make_context(
        bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID), slash=slash
    )

    denied = await refusal(ctx)
    assert denied is not None
    await bot_error_handler(ctx, denied)

    assert decided(ctx).outcome is Outcome.BROKEN
    text, _ = sent(ctx)
    assert (
        text
        == REPAIR_TEXT
        == (
            "This server's access settings need repair. An admin can reset them "
            'with `/access reset all`.'
        )
    )


async def test_unreadable_settings_are_logged_as_an_error(
    broken: Any, caplog: pytest.LogCaptureFixture
) -> None:
    (error,) = [
        record
        for record in caplog.get_records('setup')
        if record.name == LOGGER and record.levelno == logging.ERROR
    ]

    assert error.getMessage().startswith(
        f'The access settings of guild {GUILD_ID} are unreadable (not valid JSON'
    )
    assert error.getMessage().endswith(
        '. Until an admin there uses /access reset all, it refuses every command '
        "but /access, /help and the bot owner's."
    )


async def test_with_unreadable_settings_admins_can_still_repair_them(
    bot: AccessBot, guild: MagicMock, broken: Any
) -> None:
    general = channel(guild, GENERAL_ID)

    # With no staff channel known, anywhere.
    assert await outcome(bot, 'access staff-channel', make_admin(guild), general) is (
        Outcome.PUBLIC
    )
    assert await outcome(bot, 'kill', make_owner(guild), general) is Outcome.PUBLIC
    other = make_guild(OTHER_GUILD_ID)
    assert (
        await outcome(bot, 'ping', make_member(other), channel(other, BOT_CHANNEL_ID))
        is Outcome.PUBLIC
    )


async def test_unreadable_settings_take_no_change_but_a_reset(
    bot: AccessBot, guild: MagicMock, broken: Any
) -> None:
    edits: list[Callable[[GuildAccess], GuildAccess]] = [
        lambda access: access.with_limit('ping', Limit(off=True)),
        lambda access: access.with_limit('ping', None),
        lambda access: GuildAccess(broken=True),
    ]
    for edit in edits:
        with pytest.raises(SettingsNeedRepair) as refused:
            await bot.access.change(GUILD_ID, edit)
        assert isinstance(refused.value, AccessDenied)
        assert refused.value.text == REPAIR_TEXT
    still = await broken.get_all_access_settings()

    repaired = await bot.access.change(GUILD_ID, GuildAccess.without_limits)

    assert still[0] == (GUILD_ID, 'not json')
    assert repaired == GuildAccess()
    assert (await broken.get_all_access_settings())[0] == (GUILD_ID, encode(repaired))
    assert (
        await outcome(bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID))
        is Outcome.WRONG_CHANNEL
    )  # no bot channel yet, but no longer broken


@pytest.mark.parametrize(
    'error',
    [
        ValueError("Unreadable access_settings row for guild 'twelve'"),
        sqlite3.OperationalError('no such table: access_settings'),
    ],
    ids=['unreadable row', 'database error'],
)
async def test_if_no_settings_can_be_read_every_server_is_broken(
    bot: AccessBot,
    guild: MagicMock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    store = MagicMock()
    store.get_all_access_settings = AsyncMock(side_effect=error)
    store.set_access_settings = AsyncMock()
    bot.access.use_user_db(store)
    await configure(bot)  # known before the load, but not since

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot.access.load()

    error_text = (
        f'Could not read the access settings ({type(error).__name__}: {error}). '
        'Until they can be read, every server refuses every command but /access, '
        "/help and the bot owner's, and no server's settings can be changed. If a "
        'row of the access_settings table in user.db is unreadable, repair or '
        'delete the row with that guild id, then restart the bot.'
    )
    assert logged(caplog, logging.ERROR) == [error_text]
    assert bot.access.guild_access(GUILD_ID).broken
    assert bot.access.guild_access(OTHER_GUILD_ID).broken
    ctx = make_context(bot, 'ping', make_member(guild), channel(guild, BOT_CHANNEL_ID))
    # Not the repair text: a reset can't help, so admins aren't sent to one.
    assert await refusal_text(ctx) == UNREADABLE_TEXT
    assert decided(ctx).outcome is Outcome.BROKEN
    assert UNREADABLE_TEXT == (
        "The bot couldn't read this server's access settings. Ask the bot owner "
        'to check the log.'
    )
    # A change would replace rows that were never read: the settings are read
    # again, and as they still can't be, nothing is stored.
    store.set_access_settings.reset_mock()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(SettingsUnreadable) as refused:
            await bot.access.change(GUILD_ID, GuildAccess.without_limits)

    assert isinstance(refused.value, AccessDenied)
    assert refused.value.text == UNREADABLE_CHANGE_TEXT
    assert UNREADABLE_CHANGE_TEXT == (
        "The bot couldn't read its access settings, so they can't be changed. Ask "
        'the bot owner to check the log.'
    )
    store.set_access_settings.assert_not_awaited()
    assert store.get_all_access_settings.await_count == 2
    assert logged(caplog, logging.ERROR) == [error_text]
    assert bot.access.guild_access(GUILD_ID).broken


async def test_a_change_reads_the_settings_again_after_they_could_not_be_read(
    bot: AccessBot, guild: MagicMock
) -> None:
    # As after a database that was locked when the bot started.
    stored = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
    store = MagicMock()
    store.get_all_access_settings = AsyncMock(
        side_effect=[
            sqlite3.OperationalError('database is locked'),
            [(GUILD_ID, encode(stored))],
        ]
    )
    store.set_access_settings = AsyncMock()
    bot.access.use_user_db(store)
    await bot.access.load()
    assert bot.access.settings_unreadable(GUILD_ID)

    changed = await bot.access.change(
        GUILD_ID, lambda access: access.with_limit('ping', Limit(off=True))
    )

    # The change starts from what is stored, and every server is readable again.
    assert changed == GuildAccess(
        frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID, {'ping': Limit(off=True)}
    )
    store.set_access_settings.assert_awaited_once_with(GUILD_ID, encode(changed))
    assert not bot.access.settings_unreadable(OTHER_GUILD_ID)
    assert bot.access.guild_access(OTHER_GUILD_ID) == GuildAccess()


async def test_a_reset_never_replaces_rows_that_were_not_read(
    bot: AccessBot, user_db: Any
) -> None:
    # One row that can't be read makes the whole table unreadable; the other
    # servers' rows are fine and must stay as they are.
    good = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
    await user_db.set_access_settings(GUILD_ID, encode(good))
    await user_db.set_access_settings(OTHER_GUILD_ID, encode(good))
    await user_db.conn.execute(
        "INSERT INTO access_settings (guild_id, settings) VALUES ('not-a-guild', '{}')"
    )
    await user_db.conn.commit()
    bot.access.use_user_db(user_db)
    await bot.access.load()

    with pytest.raises(SettingsUnreadable):
        await bot.access.change(GUILD_ID, GuildAccess.without_limits)

    cursor = await user_db.conn.execute(
        'SELECT guild_id, settings FROM access_settings ORDER BY guild_id'
    )
    rows = [tuple(row) for row in await cursor.fetchall()]
    assert rows == [
        (str(GUILD_ID), encode(good)),
        (str(OTHER_GUILD_ID), encode(good)),
        ('not-a-guild', '{}'),
    ]
    assert bot.access.guild_access(GUILD_ID).broken


async def test_a_button_says_when_no_settings_could_be_read(
    bot: AccessBot, guild: MagicMock
) -> None:
    store = MagicMock()
    store.get_all_access_settings = AsyncMock(side_effect=ValueError('bad row'))
    bot.access.use_user_db(store)
    await bot.access.load()
    interaction = make_interaction(make_member(guild))

    assert not await bot.access.component_allowed(interaction, 'challenge')
    assert answered(interaction) == UNREADABLE_TEXT


async def test_a_successful_load_after_a_failed_one_repairs_every_server(
    bot: AccessBot, user_db: Any
) -> None:
    failing = MagicMock()
    failing.get_all_access_settings = AsyncMock(side_effect=ValueError('bad row'))
    bot.access.use_user_db(failing)
    await bot.access.load()
    bot.access.use_user_db(user_db)

    await bot.access.load()

    assert bot.access.guild_access(GUILD_ID) == GuildAccess()
