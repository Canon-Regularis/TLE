"""Tests for /notify (tle.kcpc.features.notify.cog).

The cog runs on real KCPC services built from the shared fixtures, attached to
the bot as ``bot.kcpc``. Discord is mocked, except for roles: they are real
``discord.Role`` objects, so that the bot's place in the role hierarchy is
judged as discord.py judges it. Most tests call the command's callback, as
discord.py does once it has parsed the arguments; the rest go through
discord.py on a real bot, to check how it builds the slash command and parses
the prefix one.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock, Mock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView
from discord.types.role import Role as RolePayload

from tle import constants
from tle.config import Settings
from tle.kcpc.bot.embeds import ALERT_COLOR, KCPC_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcDisabledError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.scheduler import Scheduler
from tle.kcpc.core.settings import FeatureRegistry, FeatureSpec, GuildSettingsRepo
from tle.kcpc.features.notify.cog import NO_ROLE_MESSAGE, KcpcNotify, setup
from tle.kcpc.services import KcpcServices

if TYPE_CHECKING:
    # Only for type checking: importing it first fails on a cycle in
    # discord.types.
    from discord.types.channel import TextChannel as TextChannelPayload

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
ROLE_ID = 1_300_000_000_000_000_001
BOT_ROLE_ID = 1_300_000_000_000_000_002
OTHER_ROLE_ID = 1_300_000_000_000_000_003
MEMBER_ID = 1_400_000_000_000_000_001
BOT_ID = 1_400_000_000_000_000_002
CHANNEL_ID = 1_500_000_000_000_000_001

BOT_ROLE_POSITION = 10  # the bot's highest role
PING_ROLE_POSITION = 3
PING_ROLE = f'<@&{ROLE_ID}>'

TURNED_ON = (
    f"Done! You have {PING_ROLE} now, so you'll get Workshops pings. To stop "
    'them, use `/notify workshops off`.'
)
TURNED_OFF = (
    f"Done. You don't have {PING_ROLE} any more, so you won't get Workshops "
    'pings. To get them again, use `/notify workshops on`.'
)
ALREADY_ON = f'You already get Workshops pings: you have {PING_ROLE}.'
ALREADY_OFF = f"You don't get Workshops pings: you don't have {PING_ROLE}."
CANNOT = f"I can't change who has {PING_ROLE}: "
MANAGED = (
    f'{CANNOT}Discord or an integration manages it. Ask an admin to choose '
    'another ping role with /kcpc role.'
)
NO_MANAGE_ROLES = (
    f'{CANNOT}I need the Manage Roles permission. Ask an admin to give it to me.'
)
BELOW_THE_BOT = (
    f'{CANNOT}my highest role must be above it. Ask an admin to move my role '
    'above it in Server Settings → Roles.'
)
REFUSED = (
    "Discord didn't let me change your roles. Ask an admin to check that I have "
    f'the Manage Roles permission and that my highest role is above {PING_ROLE}.'
)
# What @everyone may do in the test server, and so anyone who joins it.
EVERYONE = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True
)
IN_THE_CHANNEL = f'it changes what members can do in <#{CHANNEL_ID}>'
# Why one of TLE's roles isn't just for pings, as members are told: not which
# of them it is, as /kcpc role tells admins.
TLE_ROLE = 'the bot uses it to decide what members may do'


def not_just_for_pings(problem: str) -> str:
    return (
        f"{CANNOT}it isn't just for pings ({problem}), and /notify would let any "
        'member give it to themselves or take it away. Ask an admin to choose a '
        'role just for pings with /kcpc role.'
    )


class KcpcBot(commands.Bot):
    """A bot that carries KCPC services, as TLEBot does."""

    kcpc: KcpcServices | None = None


@pytest.fixture
def bot() -> MagicMock:
    return MagicMock(spec=KcpcBot)


@pytest.fixture
async def services(
    bot: MagicMock,
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> AsyncIterator[KcpcServices]:
    publisher = DiscordPublisher(bot, guild_settings, ledger, clock)
    services = KcpcServices(
        settings=Settings(),
        clock=clock,
        db=db,
        http=HttpClient(user_agent='kcpc-test', clock=clock),
        features=feature_registry,
        guild_settings=guild_settings,
        ledger=ledger,
        publisher=publisher,
        reminders=ReminderEngine(guild_settings, ledger, publisher, clock),
        scheduler=Scheduler(db, clock),
    )
    bot.kcpc = services
    yield services
    # The db fixture closes the database; the scheduler was never started.
    await services.http.close()


@pytest.fixture
def cog(bot: MagicMock, services: KcpcServices) -> KcpcNotify:
    return KcpcNotify(bot)


@pytest.fixture
async def live_bot(services: KcpcServices) -> AsyncIterator[KcpcBot]:
    """A real bot with the cog added through its extension's setup."""
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    bot.kcpc = services
    await setup(bot)
    yield bot
    await bot.close()


def make_role(
    guild: MagicMock,
    role_id: int,
    *,
    name: str = 'Workshops',
    position: int = PING_ROLE_POSITION,
    managed: bool = False,
    permissions: discord.Permissions = EVERYONE,
) -> discord.Role:
    """A real role in ``guild``, where ``guild.get_role`` finds it.

    Its permissions are by default @everyone's, as Discord creates a role.
    """
    data: RolePayload = {
        'id': role_id,
        'name': name,
        'color': 0,
        'colors': {'primary_color': 0, 'secondary_color': None, 'tertiary_color': None},
        'hoist': False,
        'position': position,
        'permissions': str(permissions.value),
        'managed': managed,
        'mentionable': True,
        'flags': 0,
    }
    role = discord.Role(guild=guild, state=MagicMock(), data=data)
    guild._roles[role_id] = role
    return role


def make_channel(
    guild: MagicMock, role: discord.Role, overwrite: discord.PermissionOverwrite
) -> discord.TextChannel:
    """A real channel in ``guild``, with ``overwrite`` for ``role``."""
    allow, deny = overwrite.pair()
    data: TextChannelPayload = {
        'id': CHANNEL_ID,
        'type': 0,
        'guild_id': GUILD_ID,
        'name': 'general',
        'position': 0,
        'nsfw': False,
        'parent_id': None,
        'permission_overwrites': [
            {
                'id': role.id,
                'type': 0,
                'allow': str(allow.value),
                'deny': str(deny.value),
            }
        ],
    }
    return discord.TextChannel(state=MagicMock(), guild=guild, data=data)


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles, by their default names, whatever the environment says."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_PURGATORY', 'Purgatory')


@pytest.fixture
def guild() -> MagicMock:
    """A server where the bot may manage roles below its own, at position 10."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    # Where discord.Guild keeps its roles, which get_role reads.
    guild._roles = {}
    guild.get_role.side_effect = guild._roles.get
    guild.default_role = make_role(guild, GUILD_ID, name='@everyone', position=0)
    guild.channels = []
    guild.me = MagicMock(spec=discord.Member, id=BOT_ID, guild=guild)
    guild.me.guild_permissions = discord.Permissions(manage_roles=True)
    # A role that admins gave the bot, above the one its integration manages.
    guild.me.top_role = make_role(
        guild, BOT_ROLE_ID, name='Bots', position=BOT_ROLE_POSITION
    )
    return guild


@pytest.fixture
async def ping_role(
    guild: MagicMock, guild_settings: GuildSettingsRepo
) -> discord.Role:
    """The workshops ping role, as an admin sets it with /kcpc role."""
    role = make_role(guild, ROLE_ID)
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    return role


@pytest.fixture
def member(guild: MagicMock) -> MagicMock:
    """The member running /notify, who has no roles yet."""
    member = MagicMock(spec=discord.Member, id=MEMBER_ID, guild=guild)
    member.roles = []
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


@pytest.fixture
def ctx(guild: MagicMock, member: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = member
    ctx.send = AsyncMock()
    ctx.defer = AsyncMock()
    return ctx


def make_context(
    bot: commands.Bot,
    guild: MagicMock | None,
    author: MagicMock,
    arguments: str = '',
) -> commands.Context[commands.Bot]:
    """A real prefix command context in ``guild`` (None: a DM), given ``arguments``.

    Replies are recorded by an ``AsyncMock`` in place of ``send``.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    context: commands.Context[commands.Bot] = commands.Context(
        message=message, bot=bot, view=StringView(arguments), prefix=';'
    )
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


async def run(cog: KcpcNotify, ctx: MagicMock, feature: str, state: str) -> None:
    """Call the callback of /notify with parsed arguments."""
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = cog.notify.callback
    await callback(cog, ctx, feature, state)


def notify_command(bot: commands.Bot) -> commands.Command[Any, ..., Any]:
    command = bot.get_command('notify')
    assert command is not None
    return command


def reply(ctx: MagicMock | commands.Context[Any]) -> discord.Embed:
    """The embed of the one reply, which only the member sees."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def assert_roles_unchanged(member: MagicMock) -> None:
    member.add_roles.assert_not_awaited()
    member.remove_roles.assert_not_awaited()


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'),
        {'code': 50013, 'message': 'Missing Permissions'},
    )


async def test_setup_adds_notify_for_every_member_in_servers(
    live_bot: KcpcBot,
) -> None:
    assert isinstance(live_bot.get_cog('KcpcNotify'), KcpcNotify)
    command = live_bot.tree.get_command('notify')
    assert isinstance(command, app_commands.Command)
    assert command.description == "Turn a feature's pings on or off for yourself"
    # Members see it (no default permissions), but not in DMs.
    assert command.default_permissions is None
    assert command.guild_only
    feature, state = command.parameters
    assert (feature.name, feature.type) == (
        'feature',
        discord.AppCommandOptionType.string,
    )
    assert feature.required and feature.autocomplete
    # Discord refuses longer text: no feature is longer, and a refusal that
    # repeats it fits in a reply.
    sent = command.to_dict(live_bot.tree)['options'][0]
    assert (sent['name'], sent['min_length'], sent['max_length']) == (
        'feature',
        1,
        32,
    )
    assert (state.name, state.type) == ('state', discord.AppCommandOptionType.string)
    assert state.required and not state.autocomplete
    assert state.choices == [
        app_commands.Choice(name='on', value='on'),
        app_commands.Choice(name='off', value='off'),
    ]
    # Described, rather than shown with Discord's placeholder.
    assert '…' not in (feature.description, state.description)

    assert isinstance(notify_command(live_bot), commands.HybridCommand)


async def test_on_gives_the_member_the_ping_role(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    await run(cog, ctx, 'workshops', 'on')

    member.add_roles.assert_awaited_once_with(
        ping_role, reason='Member used /notify workshops on'
    )
    member.remove_roles.assert_not_awaited()
    embed = reply(ctx)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == TURNED_ON


async def test_off_takes_the_ping_role_away(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    member.roles = [ping_role]

    await run(cog, ctx, 'workshops', 'off')

    member.remove_roles.assert_awaited_once_with(
        ping_role, reason='Member used /notify workshops off'
    )
    member.add_roles.assert_not_awaited()
    embed = reply(ctx)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == TURNED_OFF


async def test_notify_defers_privately_before_changing_roles(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    calls = Mock()
    calls.attach_mock(ctx.defer, 'defer')
    calls.attach_mock(member.add_roles, 'add_roles')
    calls.attach_mock(ctx.send, 'send')

    await run(cog, ctx, 'workshops', 'on')

    assert [name for name, _, _ in calls.mock_calls] == ['defer', 'add_roles', 'send']
    ctx.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.parametrize(
    ('state', 'has_role', 'expected'),
    [('on', True, ALREADY_ON), ('off', False, ALREADY_OFF)],
)
async def test_asking_for_the_current_state_changes_nothing(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
    state: str,
    has_role: bool,
    expected: str,
) -> None:
    member.roles = [make_role(guild, OTHER_ROLE_ID, name='Other')]
    if has_role:
        member.roles.append(ping_role)
    # Nothing to change, so the bot doesn't need to be able to change it.
    guild.me.guild_permissions = discord.Permissions.none()

    await run(cog, ctx, 'workshops', state)

    assert_roles_unchanged(member)
    embed = reply(ctx)
    assert embed.colour == discord.Colour(KCPC_COLOR)
    assert embed.description == expected


async def test_a_feature_without_a_ping_role_is_refused(
    cog: KcpcNotify,
    ctx: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    await guild_settings.update(GUILD_ID, 'workshops', enabled=True)

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', 'on')

    assert str(raised.value) == NO_ROLE_MESSAGE
    assert NO_ROLE_MESSAGE == (
        'This feature has no ping role. Ask an admin to set one with /kcpc role.'
    )
    assert_roles_unchanged(member)
    ctx.send.assert_not_awaited()


async def test_a_deleted_ping_role_counts_as_none(
    cog: KcpcNotify,
    ctx: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Set with /kcpc role, then deleted in Discord.
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)

    with pytest.raises(KcpcUserError, match=f'^{NO_ROLE_MESSAGE}$'):
        await run(cog, ctx, 'workshops', 'on')

    assert_roles_unchanged(member)


async def test_only_this_servers_ping_role_counts(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    make_role(guild, ROLE_ID)
    await guild_settings.update(OTHER_GUILD_ID, 'workshops', role_id=ROLE_ID)

    with pytest.raises(KcpcUserError, match=f'^{NO_ROLE_MESSAGE}$'):
        await run(cog, ctx, 'workshops', 'on')

    assert_roles_unchanged(member)


async def test_each_feature_has_its_own_ping_role(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
    ping_role: discord.Role,
) -> None:
    contests = make_role(guild, OTHER_ROLE_ID, name='Contests')
    await guild_settings.update(GUILD_ID, 'contests', role_id=OTHER_ROLE_ID)

    await run(cog, ctx, 'contests', 'on')

    member.add_roles.assert_awaited_once_with(
        contests, reason='Member used /notify contests on'
    )
    assert reply(ctx).description == (
        f"Done! You have <@&{OTHER_ROLE_ID}> now, so you'll get Contests pings. "
        'To stop them, use `/notify contests off`.'
    )


@pytest.mark.parametrize('state', ['on', 'off'])
@pytest.mark.parametrize(
    ('problem', 'expected'),
    [
        ('no Manage Roles', NO_MANAGE_ROLES),
        ('the role is above the bot', BELOW_THE_BOT),
        ("the role is the bot's highest", BELOW_THE_BOT),
        ('the role is managed', MANAGED),
        ('the role is managed, and no Manage Roles', MANAGED),
    ],
)
async def test_the_bot_must_be_able_to_manage_the_role(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
    problem: str,
    expected: str,
    state: str,
) -> None:
    role: discord.Role
    if "the bot's highest" in problem:
        role = guild.me.top_role
        await guild_settings.update(GUILD_ID, 'workshops', role_id=BOT_ROLE_ID)
    else:
        position = BOT_ROLE_POSITION + 1 if 'above' in problem else PING_ROLE_POSITION
        role = make_role(
            guild, ROLE_ID, position=position, managed='managed' in problem
        )
        await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    if 'no Manage Roles' in problem:
        # Everything else (Administrator would imply Manage Roles).
        permissions = discord.Permissions.all()
        permissions.update(administrator=False, manage_roles=False)
        guild.me.guild_permissions = permissions
    if state == 'off':
        member.roles = [role]

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', state)

    assert str(raised.value) == expected.replace(PING_ROLE, role.mention)
    assert_roles_unchanged(member)
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize('state', ['on', 'off'])
@pytest.mark.parametrize(
    ('name', 'permissions', 'overwrite', 'problem'),
    [
        pytest.param(
            'Workshops',
            discord.Permissions(administrator=True),
            None,
            'it grants Administrator',
            id='administrator',
        ),
        pytest.param(
            'Workshops',
            EVERYONE | discord.Permissions(manage_messages=True),
            None,
            'it grants Manage Messages',
            id='a permission @everyone lacks',
        ),
        pytest.param(
            'Workshops',
            discord.Permissions(kick_members=True, ban_members=True),
            None,
            'it grants Kick Members, Ban Members',
            id='permissions @everyone lacks',
        ),
        pytest.param(
            'Workshops',
            EVERYONE,
            discord.PermissionOverwrite(manage_messages=True),
            IN_THE_CHANNEL,
            id='allowed more in a channel',
        ),
        pytest.param(
            'Workshops',
            EVERYONE,
            discord.PermissionOverwrite(send_messages=False),
            IN_THE_CHANNEL,
            id='allowed less in a channel',
        ),
        pytest.param('Admin', EVERYONE, None, TLE_ROLE, id='admin'),
        pytest.param('Moderator', EVERYONE, None, TLE_ROLE, id='moderator'),
        pytest.param('Trusted', EVERYONE, None, TLE_ROLE, id='trusted'),
        pytest.param('Purgatory', EVERYONE, None, TLE_ROLE, id='purgatory'),
    ],
)
async def test_a_role_that_is_not_just_for_pings_is_refused(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
    name: str,
    permissions: discord.Permissions,
    overwrite: discord.PermissionOverwrite | None,
    problem: str,
    state: str,
) -> None:
    # Members could otherwise give themselves an admin's permissions, get past
    # a server's verification, or leave TLE's purgatory.
    role = make_role(guild, ROLE_ID, name=name, permissions=permissions)
    if overwrite is not None:
        guild.channels = [make_channel(guild, role, overwrite)]
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    if state == 'off':
        member.roles = [role]

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', state)

    assert str(raised.value) == not_just_for_pings(problem)
    assert_roles_unchanged(member)
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize('state', ['on', 'off'])
async def test_tles_roles_set_by_id_are_refused_too(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcNotify,
    ctx: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
    state: str,
) -> None:
    monkeypatch.setattr(constants, 'TLE_MODERATOR', ROLE_ID)
    if state == 'off':
        member.roles = [ping_role]

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', state)

    assert str(raised.value) == not_just_for_pings(TLE_ROLE)
    assert_roles_unchanged(member)


@pytest.mark.parametrize('state', ['on', 'off'])
async def test_tles_developer_role_is_refused_too(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcNotify,
    ctx: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
    state: str,
) -> None:
    # It is set by id alone. Members who gave it to themselves could use the
    # developer commands.
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', ROLE_ID)
    if state == 'off':
        member.roles = [ping_role]

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', state)

    assert str(raised.value) == not_just_for_pings(TLE_ROLE)
    assert_roles_unchanged(member)
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize(
    'setting',
    ['TLE_ADMIN', 'TLE_MODERATOR', 'TLE_TRUSTED', 'TLE_PURGATORY', 'TLE_DEVELOPER'],
)
async def test_a_refusal_never_says_which_of_tles_roles_the_role_is(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcNotify,
    ctx: MagicMock,
    ping_role: discord.Role,
    setting: str,
) -> None:
    # A ping role that an admin later made one of TLE's roles, say by setting
    # TLE_DEVELOPER to its ID. Members see the refusal, on ;notify the whole
    # channel, so it doesn't tell them which role it is: /kcpc role, which only
    # admins see, does.
    monkeypatch.setattr(constants, setting, ROLE_ID)

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', 'on')

    text = str(raised.value)
    assert text == not_just_for_pings(TLE_ROLE)
    assert f"TLE's {setting.removeprefix('TLE_').lower()} role" not in text


async def test_a_role_as_discord_creates_it_is_just_for_pings(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
) -> None:
    # It has @everyone's permissions, and a channel may list it while
    # changing nothing for it.
    assert ping_role.permissions == guild.default_role.permissions
    guild.channels = [make_channel(guild, ping_role, discord.PermissionOverwrite())]

    await run(cog, ctx, 'workshops', 'on')

    member.add_roles.assert_awaited_once_with(ping_role, reason=ANY)


async def test_a_role_just_below_the_bots_highest_can_be_managed(
    cog: KcpcNotify,
    ctx: MagicMock,
    guild: MagicMock,
    member: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    role = make_role(guild, ROLE_ID, position=BOT_ROLE_POSITION - 1)
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)

    await run(cog, ctx, 'workshops', 'on')

    member.add_roles.assert_awaited_once_with(role, reason=ANY)


@pytest.mark.parametrize('state', ['on', 'off'])
async def test_discord_refusing_gets_a_friendly_error(
    cog: KcpcNotify,
    ctx: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
    caplog: pytest.LogCaptureFixture,
    state: str,
) -> None:
    error = forbidden()
    if state == 'on':
        member.add_roles.side_effect = error
    else:
        member.roles = [ping_role]
        member.remove_roles.side_effect = error

    with caplog.at_level(logging.INFO), pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', state)

    assert str(raised.value) == REFUSED
    assert raised.value.__cause__ is error
    ctx.send.assert_not_awaited()
    # Logged for the operator, but quietly: members could repeat it at will.
    (record,) = [r for r in caplog.records if r.name == 'tle.kcpc.features.notify.cog']
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        f'Discord refused to change role {ROLE_ID} of member {MEMBER_ID} in guild '
        f'{GUILD_ID}: Missing Permissions'
    )


async def test_other_discord_errors_are_not_disguised(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    # Only a refusal says what an admin can fix; anything else is a failure
    # that the base cog logs and apologises for.
    error = discord.HTTPException(MagicMock(status=500, reason='Server Error'), '')
    member.add_roles.side_effect = error

    with pytest.raises(discord.HTTPException) as raised:
        await run(cog, ctx, 'workshops', 'on')

    assert raised.value is error


async def test_a_refusal_is_replied_to_privately(
    cog: KcpcNotify, ctx: MagicMock
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'workshops', 'on')

    error = commands.CommandInvokeError(raised.value)
    await cog.cog_command_error(ctx, error)

    embed = reply(ctx)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NO_ROLE_MESSAGE
    assert getattr(error, 'handled', False)  # so TLE's handler stays quiet


async def test_an_unknown_feature_is_refused_with_the_known_ones(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(cog, ctx, 'nope', 'on')

    assert str(raised.value) == (
        "Unknown feature 'nope'. Known features: algo, contests, weekly, workshops."
    )
    assert_roles_unchanged(member)


async def test_a_feature_name_may_have_capitals_and_spaces(
    cog: KcpcNotify, ctx: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    await run(cog, ctx, ' Workshops ', 'on')

    member.add_roles.assert_awaited_once_with(ping_role, reason=ANY)


async def test_notify_needs_the_kcpc_services(
    bot: MagicMock, cog: KcpcNotify, ctx: MagicMock, member: MagicMock
) -> None:
    bot.kcpc = None

    with pytest.raises(KcpcDisabledError):
        await run(cog, ctx, 'workshops', 'on')

    assert_roles_unchanged(member)


async def test_the_prefix_command_parses_the_feature_and_state(
    live_bot: KcpcBot, guild: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    ctx = make_context(live_bot, guild, member, 'workshops on')

    await notify_command(live_bot).invoke(ctx)

    member.add_roles.assert_awaited_once_with(
        ping_role, reason='Member used /notify workshops on'
    )
    assert reply(ctx).description == TURNED_ON


async def test_the_prefix_command_refuses_a_feature_too_long_to_be_one(
    live_bot: KcpcBot, guild: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    ctx = make_context(live_bot, guild, member, f'{"w" * 33} on')

    with pytest.raises(commands.RangeError) as raised:
        await notify_command(live_bot).invoke(ctx)

    # TLE's error handler shows this message, which doesn't repeat the input.
    assert str(raised.value) == (
        'value must be between 1 and 32 characters but received 33 characters'
    )
    assert_roles_unchanged(member)
    cast(AsyncMock, ctx.send).assert_not_awaited()


@pytest.mark.parametrize('state', ['yes', 'onn', 'enable'])
async def test_the_prefix_command_accepts_only_on_or_off(
    live_bot: KcpcBot,
    guild: MagicMock,
    member: MagicMock,
    ping_role: discord.Role,
    state: str,
) -> None:
    ctx = make_context(live_bot, guild, member, f'workshops {state}')

    with pytest.raises(commands.BadLiteralArgument) as raised:
        await notify_command(live_bot).invoke(ctx)

    # TLE's error handler shows this message.
    assert str(raised.value) == (
        "Could not convert \"state\" into the literal 'on' or 'off'."
    )
    assert_roles_unchanged(member)
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_the_prefix_command_needs_a_state(
    live_bot: KcpcBot, guild: MagicMock, member: MagicMock, ping_role: discord.Role
) -> None:
    ctx = make_context(live_bot, guild, member, 'workshops')

    with pytest.raises(commands.MissingRequiredArgument):
        await notify_command(live_bot).invoke(ctx)

    assert_roles_unchanged(member)


async def test_notify_is_refused_in_dms(live_bot: KcpcBot) -> None:
    ctx = make_context(live_bot, None, MagicMock(spec=discord.User), 'workshops on')

    with pytest.raises(commands.NoPrivateMessage):
        await notify_command(live_bot).can_run(ctx)


@pytest.mark.parametrize(
    ('typed', 'keys'),
    [
        ('', ['algo', 'contests', 'weekly', 'workshops']),
        ('w', ['weekly', 'workshops']),
        ('work', ['workshops']),
        ('WEEK', ['weekly']),
        (' con ', ['contests']),
        ('month', ['algo']),  # in the title
        ('nope', []),
    ],
)
async def test_feature_autocomplete_suggests_matching_features(
    cog: KcpcNotify, typed: str, keys: list[str]
) -> None:
    choices = await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), typed)

    assert [choice.value for choice in choices] == keys


async def test_feature_autocomplete_names_each_feature(cog: KcpcNotify) -> None:
    choices = await cog.feature_autocomplete(
        MagicMock(spec=discord.Interaction), 'work'
    )

    assert choices == [
        app_commands.Choice(name='Workshops (workshops)', value='workshops')
    ]


async def test_feature_autocomplete_reads_only_memory(
    cog: KcpcNotify, db: Database
) -> None:
    # Discord asks on every keystroke, so it must not wait on the database.
    await db.close()

    choices = await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), '')

    assert len(choices) == 4


async def test_feature_autocomplete_suggests_at_most_25(
    cog: KcpcNotify, feature_registry: FeatureRegistry
) -> None:
    for index in range(30):
        feature_registry.register(FeatureSpec(f'extra-{index:02}', 'Extra', 'More'))

    choices = await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), '')

    assert len(choices) == 25


async def test_feature_autocomplete_without_services_suggests_nothing(
    bot: MagicMock, cog: KcpcNotify
) -> None:
    bot.kcpc = None

    assert await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), '') == []
