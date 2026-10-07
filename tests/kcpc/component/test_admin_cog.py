"""Tests for the /kcpc admin commands (tle.kcpc.features.admin.cog).

The cog runs on real KCPC services built from the shared fixtures (database,
settings, ledger, scheduler), attached to the bot as ``bot.kcpc``. Discord
itself (context, guild, channel, role) is mocked. Most tests call a command's
callback, as discord.py does once it has parsed the arguments; the rest go
through discord.py on a real bot, to check how it parses arguments and runs
each command's check for prefix and slash invocations.
"""

import asyncio
import logging
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
from tle.kcpc.bot.checks import (
    NotKcpcAdmin,
    NotKcpcDeveloper,
    ensure_kcpc_admin,
    ensure_kcpc_developer,
)
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
from tle.kcpc.features.admin.cog import (
    OWNER_ONLY_NOTE,
    OWNER_PREFIX_NOTE,
    KcpcAdmin,
    setup,
)
from tle.kcpc.services import KcpcServices

ADMIN_COG_LOGGER = 'tle.kcpc.features.admin.cog'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
CHANNEL_ID = 1_200_000_000_000_000_001
ROLE_ID = 1_300_000_000_000_000_001
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_002
OWNER_ID = 1_400_000_000_000_000_001
OTHER_OWNER_ID = 1_400_000_000_000_000_002
MEMBER_ID = 1_400_000_000_000_000_003

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
FEATURE_OPTION = 'The KCPC feature, such as workshops: pick one as you type'
SCHEMA = f'Schema version {ALL_MIGRATIONS[-1].version}'
LAST_SLOT = datetime(2026, 9, 25, 11, 0, tzinfo=UTC)
# A job of each kind that /kcpc status describes.
JOBS = [
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
        last_slot=LAST_SLOT,
        failures=2,
        last_error='RuntimeError: `boom`',
    ),
]
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
    # The member using a command doesn't own the bot (see the owner fixture).
    bot.is_owner = AsyncMock(return_value=False)
    return bot


@pytest.fixture
def owner(bot: MagicMock) -> None:
    """The member using a command owns the bot."""
    bot.is_owner.return_value = True


@pytest.fixture
def developer_role(monkeypatch: pytest.MonkeyPatch) -> int:
    """TLE's developer role, an id, as TLE_DEVELOPER sets it; the admin role
    is called Committee.
    """
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', DEVELOPER_ROLE_ID)
    return DEVELOPER_ROLE_ID


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
    """A real bot with the cog added through its extension's setup.

    It knows its owner, as TLEBot does once it has asked Discord, so
    ``is_owner`` never asks.
    """
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none(), owner_id=OWNER_ID)
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
    member = MagicMock(spec=discord.Member, id=MEMBER_ID)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = [make_role(name=name) for name in roles]
    return member


def make_developer(member_id: int = MEMBER_ID) -> MagicMock:
    """A member whose only role is TLE's developer role (see developer_role)."""
    member = make_member()
    member.id = member_id
    member.roles = [MagicMock(spec=discord.Role, id=DEVELOPER_ROLE_ID)]
    member.roles[0].name = 'Developers'
    return member


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    """The context of a prefix command; see as_slash."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = make_member(manage_guild=True)
    ctx.interaction = None
    ctx.send = AsyncMock()
    return ctx


def as_slash(ctx: MagicMock) -> MagicMock:
    """``ctx`` as the context of a slash command, whose answer is private."""
    ctx.interaction = MagicMock(spec=discord.Interaction)
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
    assert group.description == "Show this server's settings for every KCPC feature"
    # Discord hides /kcpc from members without Manage Server. That only works
    # on top-level commands, hence one group for every admin command.
    assert group.default_permissions == discord.Permissions(manage_guild=True)
    assert sorted(command.name for command in group.commands) == sorted(COMMANDS)
    # What Discord shows for each command.
    assert {command.name: command.description for command in group.commands} == {
        'show': "Show this server's settings for every KCPC feature",
        # Only the bot owner sees the jobs, so the brief leaves them out.
        'status': 'Show KCPC health: post counts and skipped posts',
        'channel': 'Set the channel a feature posts in',
        'role': "Set or clear the role a feature's posts mention",
        'enable': 'Turn a feature on',
        'disable': 'Turn a feature off',
    }
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


async def test_every_option_is_described(live_bot: KcpcBot) -> None:
    # Rather than shown with Discord's placeholder, as tree.sync sends them.
    group = live_bot.tree.get_command('kcpc')
    assert isinstance(group, app_commands.Group)
    payload = group.to_dict(live_bot.tree)

    described = {
        (command['name'], option['name']): option['description']
        for command in payload['options']
        for option in command.get('options', [])
    }

    assert described == {
        ('channel', 'feature'): FEATURE_OPTION,
        ('channel', 'channel'): 'The channel its posts go to',
        ('role', 'feature'): FEATURE_OPTION,
        ('role', 'role'): (
            'A role just for pings, for its posts to mention; none if left out'
        ),
        ('enable', 'feature'): FEATURE_OPTION,
        ('disable', 'feature'): FEATURE_OPTION,
    }
    assert all(len(text) <= 100 for text in described.values())


@pytest.mark.parametrize('name', COMMANDS)
def test_each_commands_examples_use_it(cog: KcpcAdmin, name: str) -> None:
    command = cog.kcpc if name == 'show' else cog.kcpc.get_command(name)
    assert command is not None and command.help is not None
    description, _, examples = command.help.partition('\n\nExamples:\n')

    lines = [line.strip() for line in examples.splitlines()]

    assert description.strip() and lines and all(lines)
    # ;kcpc is the group itself, which /kcpc show runs.
    forms = (f'/kcpc {name}', ';kcpc' if name == 'show' else f';kcpc {name}')
    for line in lines:
        assert any(line == form or line.startswith(f'{form} ') for form in forms)


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


async def test_status_shows_the_schema_zone_and_this_servers_posts(
    bot: MagicMock, cog: KcpcAdmin, ctx: MagicMock
) -> None:
    await run(cog, 'status', ctx)

    bot.is_owner.assert_awaited_once_with(ctx.author)
    embed = reply(ctx)
    assert embed.title == 'KCPC status'
    # Not the database's path, nor what only the bot owner sees: they are told
    # that they don't see it.
    assert fields_of(embed) == {
        'Database': SCHEMA,
        'Time zone': 'Europe/London',
        'Posts in this server': 'sent: 0 · skipped: 0 · pending: 0',
        'Recently skipped posts': 'none',
    }
    assert embed.description == OWNER_ONLY_NOTE
    assert OWNER_ONLY_NOTE == (
        'Extensions, jobs and their last errors are shown only to the bot owner.'
    )
    assert ':memory:' not in str(embed.to_dict())


@pytest.mark.usefixtures('owner')
async def test_status_shows_the_bot_owner_the_kcpc_extensions_too(
    cog: KcpcAdmin, ctx: MagicMock
) -> None:
    await run(cog, 'status', as_slash(ctx))

    embed = reply(ctx)
    fields = fields_of(embed)
    assert list(fields) == [
        'Database',
        'Time zone',
        'Extensions',
        'Posts in this server',
        'Recently skipped posts',
    ]
    assert fields['Database'] == SCHEMA
    assert fields['Extensions'] == '`tle.kcpc.features.admin.cog`'
    assert embed.description is None
    assert ':memory:' not in str(embed.to_dict())


@pytest.mark.usefixtures('owner')
async def test_the_owners_prefix_status_keeps_the_internals_out_of_the_channel(
    cog: KcpcAdmin,
    ctx: MagicMock,
    services: KcpcServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole staff channel sees the answer to ;kcpc status, and the jobs'
    # errors concern every server the bot is in.
    monkeypatch.setattr(services.scheduler, 'status', lambda: JOBS)

    await run(cog, 'status', ctx)

    embed = reply(ctx)
    assert list(fields_of(embed)) == [
        'Database',
        'Time zone',
        'Posts in this server',
        'Recently skipped posts',
    ]
    assert 'boom' not in str(embed.to_dict())
    assert embed.description == OWNER_PREFIX_NOTE
    assert OWNER_PREFIX_NOTE == (
        "Use /kcpc status to see KCPC's extensions and jobs privately."
    )


async def test_status_hides_the_jobs_and_their_errors_from_others(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcAdmin,
    ctx: MagicMock,
    services: KcpcServices,
) -> None:
    monkeypatch.setattr(services.scheduler, 'status', lambda: JOBS)

    await run(cog, 'status', ctx)

    embed = reply(ctx)
    assert list(fields_of(embed)) == [
        'Database',
        'Time zone',
        'Posts in this server',
        'Recently skipped posts',
    ]
    shown = str(embed.to_dict())
    for hidden in ('a.running', 'b.failing', 'boom', 'every 2m', 'admin.cog'):
        assert hidden not in shown


async def test_status_hides_the_internals_if_discord_cannot_say_who_owns_the_bot(
    bot: MagicMock,
    cog: KcpcAdmin,
    ctx: MagicMock,
    services: KcpcServices,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # discord.py asks Discord when it doesn't know the owners yet. The status
    # still answers, as for anyone but the owner.
    monkeypatch.setattr(services.scheduler, 'status', lambda: JOBS)
    bot.is_owner.side_effect = discord.HTTPException(
        MagicMock(status=503, reason='Service Unavailable'), 'down'
    )

    with caplog.at_level(logging.WARNING, logger=ADMIN_COG_LOGGER):
        await run(cog, 'status', ctx)

    embed = reply(ctx)
    assert embed.description == OWNER_ONLY_NOTE
    assert 'Extensions' not in fields_of(embed)
    assert 'boom' not in str(embed.to_dict())
    (record,) = [r for r in caplog.records if r.name == ADMIN_COG_LOGGER]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f'Could not tell whether user {MEMBER_ID} owns the bot: 503 Service '
        'Unavailable (error code: 0): down'
    )


@pytest.mark.usefixtures('owner')
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

    await run(cog, 'status', as_slash(ctx))

    assert fields_of(reply(ctx))['Job `kcpc.reconcile`'] == lines(
        'every 2m',
        f'Next run: <t:{to_epoch(next_run)}:R>',
        'Last slot: never',
        'Failures: 0',
    )


@pytest.mark.usefixtures('owner')
async def test_status_describes_running_and_failing_jobs(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcAdmin,
    ctx: MagicMock,
    services: KcpcServices,
) -> None:
    monkeypatch.setattr(services.scheduler, 'status', lambda: JOBS)

    await run(cog, 'status', as_slash(ctx))

    fields = fields_of(reply(ctx))
    assert fields['Job `a.running`'] == lines(
        'every 2m', 'Next run: running now', 'Last slot: never', 'Failures: 0'
    )
    assert fields['Job `b.failing`'] == lines(
        'every Friday at 12:00 (Europe/London)',
        'Next run: not scheduled',
        f'Last slot: <t:{to_epoch(LAST_SLOT)}:R>',
        'Failures: 2',
        "Last error: `RuntimeError: 'boom'`",
    )


@pytest.mark.parametrize('owns_the_bot', [False, True], ids=['member', 'owner'])
async def test_status_counts_this_servers_posts_and_lists_its_latest_skips(
    bot: MagicMock,
    cog: KcpcAdmin,
    ctx: MagicMock,
    ledger: DeliveryLedger,
    clock: FakeClock,
    owns_the_bot: bool,
) -> None:
    # Whoever may see the status sees this server's posts.
    bot.is_owner.return_value = owns_the_bot
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


async def test_role_refuses_tles_developer_role(
    monkeypatch: pytest.MonkeyPatch,
    cog: KcpcAdmin,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Set by id alone; every member could otherwise use the developer commands.
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', ROLE_ID)

    with pytest.raises(KcpcUserError) as raised:
        await run(cog, 'role', ctx, 'workshops', make_role(name='Developers'))

    assert str(raised.value) == (
        f"<@&{ROLE_ID}> can't be a ping role: it isn't just for pings (it is "
        "TLE's developer role), and every member can give themselves a feature's "
        'ping role with /notify. Choose a pings-only role.'
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


def test_each_command_has_a_check_of_its_own(cog: KcpcAdmin) -> None:
    # discord.py never runs a group's checks for its subcommands, and the cog
    # has none for all of them.
    checks = {command.name: command.checks for command in cog.kcpc.commands}
    checks['show'] = cog.kcpc.checks

    assert checks == {
        'show': [ensure_kcpc_admin],
        'status': [ensure_kcpc_developer],
        'channel': [ensure_kcpc_admin],
        'role': [ensure_kcpc_admin],
        'enable': [ensure_kcpc_admin],
        'disable': [ensure_kcpc_admin],
    }
    assert type(cog).cog_check is commands.Cog.cog_check


@pytest.mark.usefixtures('developer_role')
@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', COMMANDS)
async def test_discord_py_runs_each_commands_check(
    live_bot: KcpcBot, guild: MagicMock, name: str, slash: bool
) -> None:
    # A hybrid command checks a slash invocation on a path of its own; both must
    # run the command's check. (`show` is the group itself: /kcpc show, or
    # ;kcpc.) Admins may use every command, and developers only the status.
    command = command_named(live_bot, 'kcpc' if name == 'show' else f'kcpc {name}')

    async def can_run(member: MagicMock) -> bool:
        return await command.can_run(make_context(live_bot, guild, member, slash=slash))

    for admin in (make_member(manage_guild=True), make_member('Committee')):
        assert await can_run(admin)
    refusal: type[commands.CheckFailure]
    if name == 'status':
        assert await can_run(make_developer())
        refusal = NotKcpcDeveloper
    else:
        with pytest.raises(NotKcpcAdmin):
            await can_run(make_developer())
        refusal = NotKcpcAdmin
    with pytest.raises(refusal):
        await can_run(make_member('Workshops'))


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_without_a_developer_role_only_admins_see_the_status(
    monkeypatch: pytest.MonkeyPatch, live_bot: KcpcBot, guild: MagicMock, slash: bool
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)
    status = command_named(live_bot, 'kcpc status')
    admin = make_context(live_bot, guild, make_member(manage_guild=True), slash=slash)

    assert await status.can_run(admin)
    with pytest.raises(NotKcpcDeveloper):
        await status.can_run(
            make_context(live_bot, guild, make_developer(), slash=slash)
        )


@pytest.mark.usefixtures('developer_role')
async def test_a_developer_sees_the_status_past_the_groups_admin_check(
    live_bot: KcpcBot, guild: MagicMock
) -> None:
    # discord.py runs a subcommand's own check alone, so ;kcpc being for
    # admins doesn't keep developers from ;kcpc status.
    group = command_named(live_bot, 'kcpc')
    with pytest.raises(NotKcpcAdmin):
        await group.invoke(make_context(live_bot, guild, make_developer()))
    ctx = make_context(live_bot, guild, make_developer(), 'status')

    await group.invoke(ctx)

    embed = reply(ctx)
    assert embed.title == 'KCPC status'
    assert embed.description == OWNER_ONLY_NOTE


@pytest.mark.usefixtures('developer_role')
async def test_a_member_is_refused_the_status_by_its_own_check(
    live_bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(live_bot, guild, make_member('Workshops'), 'status')

    with pytest.raises(NotKcpcDeveloper):
        await command_named(live_bot, 'kcpc').invoke(ctx)

    cast(AsyncMock, ctx.send).assert_not_awaited()


@pytest.mark.usefixtures('developer_role')
@pytest.mark.parametrize('team', [False, True], ids=['owner', 'team'])
async def test_status_asks_the_bot_who_owns_it(
    live_bot: KcpcBot, guild: MagicMock, team: bool
) -> None:
    # As TLEBot sets them once it has asked Discord: the owner of the bot's
    # application, or the members of its team who may manage it.
    if team:
        live_bot.owner_id = None
        live_bot.owner_ids = {OWNER_ID, OTHER_OWNER_ID}
    owner = make_context(live_bot, guild, make_developer(OWNER_ID), 'status')
    other = make_context(live_bot, guild, make_developer(MEMBER_ID), 'status')

    for ctx in (owner, other):
        await command_named(live_bot, 'kcpc').invoke(ctx)

    # On ;kcpc status, which the channel sees, the owner is told where to see
    # the internals (see the tests above), and others that they are the owner's.
    assert 'Extensions' not in fields_of(reply(owner))
    assert reply(owner).description == OWNER_PREFIX_NOTE
    assert 'Extensions' not in fields_of(reply(other))
    assert reply(other).description == OWNER_ONLY_NOTE


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
