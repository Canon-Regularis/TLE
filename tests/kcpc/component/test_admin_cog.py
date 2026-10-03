"""Tests for the /kcpc admin commands (tle.kcpc.features.admin.cog).

The cog runs on real KCPC services built from the shared fixtures (database,
settings, ledger, scheduler), attached to the bot as ``bot.kcpc``. Discord
itself (context, guild, channel, role) is mocked. Most tests call a command's
callback, as discord.py does once it has parsed the arguments; the rest go
through discord.py on a real bot, to check how it parses arguments and runs the
admin check for prefix and slash invocations.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.config import Settings
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.embeds import KCPC_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcDisabledError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import Delivery, DeliveryLedger
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.migrations import ALL_MIGRATIONS
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import JobStatus, ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureRegistry, FeatureSpec, GuildSettingsRepo
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.admin.cog import KcpcAdmin, setup
from tle.kcpc.services import KcpcServices

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
CHANNEL_ID = 1_200_000_000_000_000_001
ROLE_ID = 1_300_000_000_000_000_001

POST_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, read_message_history=True
)
# Mention @everyone, @here and All Roles alone: server-wide, or in a channel.
MENTION_EVERYONE = discord.Permissions(mention_everyone=True)
WORKSHOPS = 'Luma workshop reminders, 24h and 1h before'
NO_CHANNEL_WARNING = (
    '**Warning:** nothing is posted until a channel is set with '
    '`/kcpc channel {feature} #channel`.'
)
UNNOTIFIED_ROLE_WARNING = (
    f"**Warning:** posts in <#{CHANNEL_ID}> won't notify <@&{ROLE_ID}>. Allow "
    'anyone to mention it (Server Settings → Roles), or give me the "Mention '
    '@everyone, @here and All Roles" permission there.'
)
COMMANDS = ['show', 'status', 'channel', 'role', 'enable', 'disable']
# Real seconds a healthy teardown needs, many times over. One that hangs then
# fails its test instead of stalling the whole run.
TEARDOWN_TIMEOUT = 10


class KcpcBot(commands.Bot):
    """A bot that carries KCPC services, as TLEBot does."""

    kcpc: KcpcServices | None = None


@pytest.fixture
def bot() -> MagicMock:
    bot = MagicMock(spec=KcpcBot)
    bot.extensions = {
        name: ModuleType(name)
        for name in ('tle.cogs.meta', 'tle.kcpc.features.admin.cog', 'tle.kcpcish')
    }
    return bot


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
    # The db fixture closes the database.
    await asyncio.wait_for(services.scheduler.stop(), TEARDOWN_TIMEOUT)
    await asyncio.wait_for(services.http.close(), TEARDOWN_TIMEOUT)


@pytest.fixture
def cog(bot: MagicMock, services: KcpcServices) -> KcpcAdmin:
    return KcpcAdmin(bot)


@pytest.fixture
async def live_bot(services: KcpcServices) -> AsyncIterator[KcpcBot]:
    """A real bot with the cog added through its extension's setup."""
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    bot.kcpc = services
    await setup(bot)
    yield bot
    await bot.close()


@pytest.fixture
def guild() -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.me = MagicMock(spec=discord.Member)
    guild.me.guild_permissions = discord.Permissions.none()
    # Where discord.Guild keeps its roles, which get_role and discord.py's role
    # converter read.
    guild._roles = {}
    guild.get_role.side_effect = guild._roles.get
    # And its channels, which make_channel adds to.
    guild._channels = {}
    guild.get_channel_or_thread.side_effect = guild._channels.get
    return guild


def make_member(*roles: str, manage_guild: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = [make_role(name=name) for name in roles]
    return member


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = make_member(manage_guild=True)
    ctx.send = AsyncMock()
    return ctx


def make_context(
    bot: commands.Bot,
    guild: MagicMock,
    author: MagicMock,
    arguments: str = '',
    *,
    slash: bool = False,
) -> commands.Context[commands.Bot]:
    """A real context for a command in ``guild``, given ``arguments`` as text.

    With ``slash``, it belongs to an interaction, as for a slash command.
    Replies are recorded by an ``AsyncMock`` in place of ``send``.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(arguments),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def make_channel(
    guild: MagicMock, permissions: discord.Permissions = POST_PERMISSIONS
) -> MagicMock:
    """A text channel in ``guild`` where the bot has ``permissions``.

    By default the bot can post there but not mention everyone, as when an
    overwrite denies it that in the channel.
    """
    channel = MagicMock(
        spec=discord.TextChannel, id=CHANNEL_ID, guild=guild, mention=f'<#{CHANNEL_ID}>'
    )
    channel.permissions_for.return_value = permissions
    guild._channels[CHANNEL_ID] = channel
    return channel


def make_role(
    *,
    name: str = 'Workshops',
    mentionable: bool = True,
    default: bool = False,
    permissions: discord.Permissions | None = None,
) -> MagicMock:
    """A role in a server whose @everyone has no permissions and no channel
    overwrites it: by default, one just for pings.
    """
    role = MagicMock(
        spec=discord.Role, id=ROLE_ID, mention=f'<@&{ROLE_ID}>', mentionable=mentionable
    )
    role.name = name  # not MagicMock(name=...), which names the mock itself
    role.is_default.return_value = default
    role.permissions = permissions or discord.Permissions.none()
    # What ping_role_problem reads besides the role itself.
    role.guild = MagicMock(spec=discord.Guild, id=GUILD_ID, channels=[])
    role.guild.default_role = MagicMock(
        spec=discord.Role, permissions=discord.Permissions.none()
    )
    return role


async def run(cog: KcpcAdmin, name: str, ctx: MagicMock, *args: object) -> None:
    """Call the callback of ``/kcpc <name>`` with parsed arguments ``args``.

    ``show`` is the group's own callback, which slash commands reach through
    its fallback subcommand.
    """
    command: commands.Command[Any, ..., Any] | None = (
        cog.kcpc if name == 'show' else cog.kcpc.get_command(name)
    )
    assert command is not None, name
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(cog, ctx, *args)


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


def reply(ctx: MagicMock | commands.Context[Any]) -> discord.Embed:
    """The embed of the one reply, which only the admin sees."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def fields_of(embed: discord.Embed) -> dict[str, str | None]:
    return {str(field.name): field.value for field in embed.fields}


def lines(*parts: str) -> str:
    return '\n'.join(parts)


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 5 s of real time) until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def test_setup_adds_one_kcpc_group_hidden_from_non_admins(
    live_bot: KcpcBot,
) -> None:
    assert isinstance(live_bot.get_cog('KcpcAdmin'), KcpcAdmin)
    group = live_bot.tree.get_command('kcpc')
    assert isinstance(group, app_commands.Group)
    assert group.description == 'KCPC club settings'
    # Discord hides /kcpc from members without Manage Server. That only works
    # on top-level commands, hence one group for every admin command.
    assert group.default_permissions == discord.Permissions(manage_guild=True)
    assert sorted(command.name for command in group.commands) == sorted(COMMANDS)
    for name in ('channel', 'role', 'enable', 'disable'):
        command = group.get_command(name)
        assert isinstance(command, app_commands.Command)
        (feature,) = [p for p in command.parameters if p.name == 'feature']
        assert feature.required and feature.autocomplete, name
    role_command = group.get_command('role')
    assert isinstance(role_command, app_commands.Command)
    (role,) = [p for p in role_command.parameters if p.name == 'role']
    assert role.type is discord.AppCommandOptionType.role
    assert not role.required

    prefix_group = live_bot.get_command('kcpc')
    assert isinstance(prefix_group, commands.HybridGroup)
    assert prefix_group.invoke_without_command  # ;kcpc shows the settings


async def test_show_lists_every_feature_with_its_settings(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(
        GUILD_ID, 'workshops', enabled=True, channel_id=CHANNEL_ID, role_id=ROLE_ID
    )
    await guild_settings.update(GUILD_ID, 'contests', enabled=True)
    await guild_settings.update(OTHER_GUILD_ID, 'weekly', enabled=True)

    await run(cog, 'show', ctx)

    embed = reply(ctx)
    assert embed.title == 'KCPC settings'
    assert embed.colour == discord.Colour(KCPC_COLOR)
    fields = fields_of(embed)
    assert list(fields) == [
        'Algorithm of the month (`algo`)',
        'Contests (`contests`)',
        'Weekly problem (`weekly`)',
        'Workshops (`workshops`)',
    ]
    assert fields['Workshops (`workshops`)'] == lines(
        WORKSHOPS,
        'Status: enabled',
        f'Channel: <#{CHANNEL_ID}>',
        f'Role: <@&{ROLE_ID}>',
    )
    assert fields['Contests (`contests`)'] == lines(
        'Contest reminders and results',
        'Status: enabled',
        'Channel: not set',
        'Role: none',
        NO_CHANNEL_WARNING.format(feature='contests'),
    )
    # Enabled only in another server.
    assert fields['Weekly problem (`weekly`)'] == lines(
        'Friday problem, solution the Friday after',
        'Status: disabled',
        'Channel: not set',
        'Role: none',
    )


async def test_show_warns_about_a_role_that_posts_in_the_channel_would_not_notify(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    # The bot may mention everyone server-wide, but not in the channel.
    guild.me.guild_permissions = MENTION_EVERYONE
    guild._roles[ROLE_ID] = make_role(mentionable=False)
    make_channel(guild)
    await guild_settings.update(
        GUILD_ID, 'workshops', enabled=True, channel_id=CHANNEL_ID, role_id=ROLE_ID
    )

    await run(cog, 'show', ctx)

    assert fields_of(reply(ctx))['Workshops (`workshops`)'] == lines(
        WORKSHOPS,
        'Status: enabled',
        f'Channel: <#{CHANNEL_ID}>',
        f'Role: <@&{ROLE_ID}>',
        UNNOTIFIED_ROLE_WARNING,
    )


async def test_show_outside_a_server_is_refused(cog: KcpcAdmin, ctx: MagicMock) -> None:
    ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await run(cog, 'show', ctx)


async def test_status_shows_the_database_zone_and_kcpc_extensions(
    cog: KcpcAdmin, ctx: MagicMock
) -> None:
    await run(cog, 'status', ctx)

    embed = reply(ctx)
    assert embed.title == 'KCPC status'
    fields = fields_of(embed)
    assert fields['Database'] == (
        f'`:memory:`, schema version {ALL_MIGRATIONS[-1].version}'
    )
    assert fields['Time zone'] == 'Europe/London'
    assert fields['Extensions'] == '`tle.kcpc.features.admin.cog`'
    assert fields['Posts in this server'] == 'sent: 0 · skipped: 0 · pending: 0'
    assert fields['Recently skipped posts'] == 'none'


async def test_status_shows_the_scheduled_jobs(
    cog: KcpcAdmin, ctx: MagicMock, services: KcpcServices, clock: FakeClock
) -> None:
    async def noop(slot: datetime) -> None:
        pass

    scheduler = services.scheduler
    scheduler.add(
        ScheduledJob(
            'kcpc.reconcile', Every(timedelta(minutes=2)), noop, persistent=False
        )
    )
    scheduler.start()
    next_run = clock.now() + timedelta(minutes=2)
    await eventually(
        lambda: scheduler.status()[0].next_run == next_run, 'the job waits'
    )

    await run(cog, 'status', ctx)

    assert fields_of(reply(ctx))['Job `kcpc.reconcile`'] == lines(
        'every 2m',
        f'Next run: <t:{to_epoch(next_run)}:R>',
        'Last slot: never',
        'Failures: 0',
    )


async def test_status_describes_running_and_failing_jobs(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcAdmin,
    ctx: MagicMock,
    services: KcpcServices,
) -> None:
    last_slot = datetime(2026, 9, 25, 11, 0, tzinfo=UTC)
    statuses = [
        JobStatus(
            name='a.running',
            description='every 2m',
            persistent=False,
            running=True,
            next_run=None,
            last_slot=None,
            failures=0,
            last_error=None,
        ),
        JobStatus(
            name='b.failing',
            description='every Friday at 12:00 (Europe/London)',
            persistent=True,
            running=False,
            next_run=None,
            last_slot=last_slot,
            failures=2,
            last_error='RuntimeError: `boom`',
        ),
    ]
    monkeypatch.setattr(services.scheduler, 'status', lambda: statuses)

    await run(cog, 'status', ctx)

    fields = fields_of(reply(ctx))
    assert fields['Job `a.running`'] == lines(
        'every 2m', 'Next run: running now', 'Last slot: never', 'Failures: 0'
    )
    assert fields['Job `b.failing`'] == lines(
        'every Friday at 12:00 (Europe/London)',
        'Next run: not scheduled',
        f'Last slot: <t:{to_epoch(last_slot)}:R>',
        'Failures: 2',
        "Last error: `RuntimeError: 'boom'`",
    )


async def test_status_counts_this_servers_posts_and_lists_its_latest_skips(
    cog: KcpcAdmin, ctx: MagicMock, ledger: DeliveryLedger, clock: FakeClock
) -> None:
    message = OutgoingMessage(title='Graphs 101')
    _, batch = await ledger.claim(
        [Delivery('sent', GUILD_ID, 'workshops')],
        channel_id=CHANNEL_ID,
        message=message,
    )
    assert batch is not None
    await ledger.confirm(batch, message_id=1)
    await ledger.claim(
        [Delivery('pending', GUILD_ID, 'workshops')],
        channel_id=CHANNEL_ID,
        message=message,
    )
    skipped_at = []
    for index in range(6):
        skipped_at.append(clock.now())
        delivery = Delivery(f'remind:{index}', GUILD_ID, 'contests')
        await ledger.record_skip(delivery, f'reason-{index}')
        await clock.advance(timedelta(minutes=1))
    await ledger.record_skip(Delivery('elsewhere', OTHER_GUILD_ID, 'contests'), 'x')

    await run(cog, 'status', ctx)

    fields = fields_of(reply(ctx))
    assert fields['Posts in this server'] == 'sent: 1 · skipped: 6 · pending: 1'
    # The latest five, newest first.
    assert fields['Recently skipped posts'] == lines(
        *(
            f'<t:{to_epoch(skipped_at[index])}:R> · contests · `reason-{index}` · '
            f'`remind:{index}`'
            for index in (5, 4, 3, 2, 1)
        )
    )


async def test_channel_sets_where_a_feature_posts(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    channel = make_channel(guild)

    await run(cog, 'channel', ctx, 'workshops', channel)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).channel_id == CHANNEL_ID
    channel.permissions_for.assert_called_once_with(guild.me)
    embed = reply(ctx)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == lines(
        f'Workshops posts go to <#{CHANNEL_ID}>.',
        '',
        WORKSHOPS,
        'Status: disabled',
        f'Channel: <#{CHANNEL_ID}>',
        'Role: none',
    )


@pytest.mark.parametrize(
    ('permissions', 'missing'),
    [
        (
            discord.Permissions.none(),
            'View Channel, Send Messages, Embed Links, Read Message History',
        ),
        (
            discord.Permissions(view_channel=True, send_messages=True),
            'Embed Links, Read Message History',
        ),
        (
            discord.Permissions(
                view_channel=True, send_messages=True, embed_links=True
            ),
            'Read Message History',
        ),
    ],
)
async def test_channel_refuses_a_channel_the_bot_cannot_post_in(
    cog: KcpcAdmin,
    ctx: MagicMock,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    permissions: discord.Permissions,
    missing: str,
) -> None:
    channel = make_channel(guild, permissions)

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, 'channel', ctx, 'workshops', channel)

    assert str(raised.value) == (
        f"I can't post in <#{CHANNEL_ID}>. Give me these permissions there, then "
        f'try again: {missing}.'
    )
    assert (await guild_settings.get(GUILD_ID, 'workshops')).channel_id is None
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize(
    ('mentionable', 'permissions', 'warned'),
    [
        (False, POST_PERMISSIONS, True),
        (True, POST_PERMISSIONS, False),
        (False, POST_PERMISSIONS | MENTION_EVERYONE, False),
    ],
    ids=['not notified', 'mentionable', 'the bot may mention everyone there'],
)
async def test_channel_warns_if_its_posts_would_not_notify_the_role(
    cog: KcpcAdmin,
    ctx: MagicMock,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    mentionable: bool,
    permissions: discord.Permissions,
    warned: bool,
) -> None:
    # The role was accepted while the bot could mention everyone server-wide;
    # the new channel denies it that. The channel is set all the same.
    guild.me.guild_permissions = MENTION_EVERYONE
    guild._roles[ROLE_ID] = make_role(mentionable=mentionable)
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    channel = make_channel(guild, permissions)

    await run(cog, 'channel', ctx, 'workshops', channel)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).channel_id == CHANNEL_ID
    embed = reply(ctx)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == lines(
        f'Workshops posts go to <#{CHANNEL_ID}>.',
        '',
        WORKSHOPS,
        'Status: disabled',
        f'Channel: <#{CHANNEL_ID}>',
        f'Role: <@&{ROLE_ID}>',
        *([UNNOTIFIED_ROLE_WARNING] if warned else []),
    )


async def test_role_sets_the_role_posts_mention(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await run(cog, 'role', ctx, 'workshops', make_role())

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID
    assert reply(ctx).description == lines(
        f'Workshops posts mention <@&{ROLE_ID}>. Members get or drop it with '
        '`/notify workshops on|off`.',
        '',
        WORKSHOPS,
        'Status: disabled',
        'Channel: not set',
        f'Role: <@&{ROLE_ID}>',
    )


async def test_role_without_a_role_clears_it(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)

    await run(cog, 'role', ctx, 'workshops', None)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None
    description = reply(ctx).description
    assert description is not None
    assert description.startswith('Workshops posts mention no role.\n')


async def test_role_refuses_everyone(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    everyone = make_role(name='@everyone', default=True)

    with pytest.raises(KcpcUserError, match='^Posts cannot mention @everyone'):
        await run(cog, 'role', ctx, 'workshops', everyone)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None


@pytest.mark.parametrize(
    ('role', 'problem'),
    [
        (
            {'name': 'Helpers', 'permissions': discord.Permissions(administrator=True)},
            'it grants Administrator',
        ),
        ({'name': 'Committee'}, "it is TLE's admin role"),
    ],
    ids=['an administrator role', "TLE's admin role"],
)
async def test_role_refuses_a_role_that_is_not_just_for_pings(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcAdmin,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    role: dict[str, Any],
    problem: str,
) -> None:
    # Every member can give themselves a feature's ping role with /notify.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, 'role', ctx, 'workshops', make_role(**role))

    assert str(raised.value) == (
        f"<@&{ROLE_ID}> can't be a ping role: it isn't just for pings "
        f"({problem}), and every member can give themselves a feature's ping "
        'role with /notify. Choose a pings-only role.'
    )
    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_role_refuses_a_role_the_bot_cannot_mention(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await run(cog, 'role', ctx, 'workshops', make_role(mentionable=False))

    assert str(raised.value) == (
        f"I can't mention <@&{ROLE_ID}>. Allow anyone to mention it (Server "
        'Settings → Roles), or give me the "Mention @everyone, @here and All '
        'Roles" permission.'
    )
    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None


async def test_role_accepts_any_role_if_the_bot_may_mention_everyone(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    guild.me.guild_permissions = discord.Permissions(mention_everyone=True)

    await run(cog, 'role', ctx, 'workshops', make_role(mentionable=False))

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID


async def test_role_refuses_a_role_the_bot_cannot_mention_in_the_features_channel(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    # Server-wide the bot may mention everyone, but an overwrite in the
    # feature's channel denies it that, so Discord would notify nobody there.
    guild.me.guild_permissions = MENTION_EVERYONE
    channel = make_channel(guild)
    await guild_settings.update(GUILD_ID, 'workshops', channel_id=CHANNEL_ID)

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, 'role', ctx, 'workshops', make_role(mentionable=False))

    assert str(raised.value) == (
        f"I can't mention <@&{ROLE_ID}> in <#{CHANNEL_ID}>. Allow anyone to "
        'mention it (Server Settings → Roles), or give me the "Mention '
        '@everyone, @here and All Roles" permission there.'
    )
    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None
    channel.permissions_for.assert_called_with(guild.me)
    ctx.send.assert_not_awaited()


@pytest.mark.parametrize(
    ('mentionable', 'permissions'),
    [(True, POST_PERMISSIONS), (False, POST_PERMISSIONS | MENTION_EVERYONE)],
    ids=['mentionable', 'the bot may mention everyone there'],
)
async def test_role_accepts_a_role_the_bot_can_notify_in_the_features_channel(
    cog: KcpcAdmin,
    ctx: MagicMock,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    mentionable: bool,
    permissions: discord.Permissions,
) -> None:
    # Server-wide the bot may not mention everyone; the channel decides.
    make_channel(guild, permissions)
    await guild_settings.update(GUILD_ID, 'workshops', channel_id=CHANNEL_ID)

    await run(cog, 'role', ctx, 'workshops', make_role(mentionable=mentionable))

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID
    assert reply(ctx).colour == discord.Colour(SUCCESS_COLOR)


async def test_role_checks_server_wide_if_the_features_channel_is_gone(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    guild.me.guild_permissions = MENTION_EVERYONE
    await guild_settings.update(GUILD_ID, 'workshops', channel_id=CHANNEL_ID)

    await run(cog, 'role', ctx, 'workshops', make_role(mentionable=False))

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID


@pytest.mark.parametrize('given', ['Workshops', f'<@&{ROLE_ID}>', str(ROLE_ID)])
async def test_role_by_prefix_finds_the_role_by_name_mention_or_id(
    live_bot: KcpcBot,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    given: str,
) -> None:
    guild._roles[ROLE_ID] = make_role()
    ctx = make_context(
        live_bot, guild, make_member(manage_guild=True), f'workshops {given}'
    )

    await command_named(live_bot, 'kcpc role').invoke(ctx)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID
    assert reply(ctx).colour == discord.Colour(SUCCESS_COLOR)


async def test_role_by_prefix_without_a_role_clears_it(
    live_bot: KcpcBot, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    ctx = make_context(live_bot, guild, make_member(manage_guild=True), 'workshops')

    await command_named(live_bot, 'kcpc role').invoke(ctx)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id is None


async def test_role_by_prefix_with_a_role_it_cannot_find_changes_nothing(
    live_bot: KcpcBot, guild: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    # A mistyped role must not read as "no role", which would clear the setting.
    await guild_settings.update(GUILD_ID, 'workshops', role_id=ROLE_ID)
    guild._roles[ROLE_ID] = make_role()
    ctx = make_context(
        live_bot, guild, make_member(manage_guild=True), 'workshops Workshop'
    )

    with pytest.raises(commands.RoleNotFound, match='Role "Workshop" not found.'):
        await command_named(live_bot, 'kcpc role').invoke(ctx)

    assert (await guild_settings.get(GUILD_ID, 'workshops')).role_id == ROLE_ID
    cast(AsyncMock, ctx.send).assert_not_awaited()


async def test_enable_without_a_channel_warns_that_nothing_is_posted(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await run(cog, 'enable', ctx, 'weekly')

    assert (await guild_settings.get(GUILD_ID, 'weekly')).enabled
    assert not (await guild_settings.get(OTHER_GUILD_ID, 'weekly')).enabled
    embed = reply(ctx)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == lines(
        'Weekly problem is on.',
        '',
        'Friday problem, solution the Friday after',
        'Status: enabled',
        'Channel: not set',
        'Role: none',
        NO_CHANNEL_WARNING.format(feature='weekly'),
    )


async def test_enable_with_a_channel_does_not_warn(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(GUILD_ID, 'weekly', channel_id=CHANNEL_ID)

    await run(cog, 'enable', ctx, 'weekly')

    description = reply(ctx).description
    assert description is not None
    assert 'Warning' not in description
    assert f'Channel: <#{CHANNEL_ID}>' in description


async def test_disable_turns_a_feature_off_and_keeps_its_settings(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(
        GUILD_ID, 'workshops', enabled=True, channel_id=CHANNEL_ID, role_id=ROLE_ID
    )

    await run(cog, 'disable', ctx, 'workshops')

    settings = await guild_settings.get(GUILD_ID, 'workshops')
    assert not settings.enabled
    assert (settings.channel_id, settings.role_id) == (CHANNEL_ID, ROLE_ID)
    description = reply(ctx).description
    assert description is not None
    assert description.startswith('Workshops is off.\n')


async def test_a_feature_name_may_have_capitals_and_spaces(
    cog: KcpcAdmin, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await run(cog, 'enable', ctx, ' Workshops ')

    assert (await guild_settings.get(GUILD_ID, 'workshops')).enabled


@pytest.mark.parametrize('command', ['channel', 'role', 'enable', 'disable'])
async def test_an_unknown_feature_is_refused_with_the_known_ones(
    cog: KcpcAdmin, ctx: MagicMock, guild: MagicMock, command: str
) -> None:
    extra: dict[str, tuple[object, ...]] = {
        'channel': (make_channel(guild),),
        'role': (None,),
    }

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, command, ctx, 'nope', *extra.get(command, ()))

    assert str(raised.value) == (
        "Unknown feature 'nope'. Known features: algo, contests, weekly, workshops."
    )
    ctx.send.assert_not_awaited()


async def test_commands_need_the_kcpc_services(
    bot: MagicMock, cog: KcpcAdmin, ctx: MagicMock
) -> None:
    bot.kcpc = None

    with pytest.raises(KcpcDisabledError):
        await run(cog, 'show', ctx)


async def test_a_user_error_from_a_command_is_replied_to_privately(
    cog: KcpcAdmin, ctx: MagicMock
) -> None:
    error = commands.CommandInvokeError(KcpcUserError("I can't post there."))

    await cog.cog_command_error(ctx, error)

    assert reply(ctx).description == "I can't post there."


async def test_cog_check_admits_a_member_who_can_manage_the_server(
    cog: KcpcAdmin, ctx: MagicMock
) -> None:
    ctx.author = make_member(manage_guild=True)

    assert await cog.cog_check(ctx)


async def test_cog_check_admits_a_member_with_the_admin_role(
    monkeypatch: pytest.MonkeyPatch, cog: KcpcAdmin, ctx: MagicMock
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    ctx.author = make_member('Committee')

    assert await cog.cog_check(ctx)


async def test_cog_check_refuses_other_members(
    monkeypatch: pytest.MonkeyPatch, cog: KcpcAdmin, ctx: MagicMock
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    ctx.author = make_member('Workshops')

    with pytest.raises(NotKcpcAdmin):
        await cog.cog_check(ctx)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', COMMANDS)
async def test_discord_py_runs_the_admin_check_for_every_command(
    monkeypatch: pytest.MonkeyPatch,
    live_bot: KcpcBot,
    guild: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # A hybrid command checks a slash invocation on a path of its own; both must
    # run cog_check. (`show` is the group itself: /kcpc show, or ;kcpc.)
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    command = command_named(live_bot, 'kcpc' if name == 'show' else f'kcpc {name}')

    for admin in (make_member(manage_guild=True), make_member('Committee')):
        assert await command.can_run(make_context(live_bot, guild, admin, slash=slash))
    member = make_context(live_bot, guild, make_member('Workshops'), slash=slash)
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(member)


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
    cog: KcpcAdmin, typed: str, keys: list[str]
) -> None:
    choices = await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), typed)

    assert [choice.value for choice in choices] == keys


async def test_feature_autocomplete_names_each_feature(cog: KcpcAdmin) -> None:
    choices = await cog.feature_autocomplete(
        MagicMock(spec=discord.Interaction), 'work'
    )

    assert choices == [
        app_commands.Choice(name='Workshops (workshops)', value='workshops')
    ]


async def test_feature_autocomplete_suggests_at_most_25(
    cog: KcpcAdmin, feature_registry: FeatureRegistry
) -> None:
    for index in range(30):
        feature_registry.register(FeatureSpec(f'extra-{index:02}', 'Extra', 'More'))

    choices = await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), '')

    assert len(choices) == 25


async def test_feature_autocomplete_without_services_suggests_nothing(
    bot: MagicMock, cog: KcpcAdmin
) -> None:
    bot.kcpc = None

    assert await cog.feature_autocomplete(MagicMock(spec=discord.Interaction), '') == []
