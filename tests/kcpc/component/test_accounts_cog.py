"""Tests for the accounts cog (tle.kcpc.features.accounts.cog) and its Verify button.

The cog runs on real KCPC services, with a migrated in-memory kcpc.db, on a
real bot whose TLE user database is real and in memory too. So Codeforces
handles are linked through tle.kcpc.bot.codeforces_links into TLE's own table,
rank roles and all. Only Discord and the sites are mocked: AtCoder's profile
pages by FakeAtCoder, Codeforces by FakeCodeforces in place of TLE's
``cf.user.info``. Most tests call a command's callback with a mocked context.
The button is pressed by calling its callback, and once, after a restart,
through discord.py's own dispatch of dynamic items. The users are made up.
"""

import asyncio
import itertools
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tests.kcpc.conftest import CLOCK_START
from tle import constants
from tle.config import Settings
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.embeds import ALERT_COLOR, KCPC_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.pages import PageView
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.accounts import cog as accounts_cog, service as service_module
from tle.kcpc.features.accounts.cog import (
    ATCODER_COLORS,
    CODEFORCES_COLORS,
    PURGE_JOB,
    REFRESH_JOB,
    KcpcAccounts,
    setup,
)
from tle.kcpc.features.accounts.repo import (
    AccountRepo,
    AccountSnapshot,
    HandleTaken,
    LinkChallenge,
    LinkedAccount,
)
from tle.kcpc.features.accounts.views import (
    NOT_YOUR_LINK,
    VERIFY_TEMPLATE,
    VerifyLinkButton,
)
from tle.kcpc.features.admin.cog import setup as add_admin_cog
from tle.kcpc.platforms.atcoder.profile import PROFILE_URL, AtCoderProfile
from tle.kcpc.services import KcpcServices
from tle.util import codeforces_api as cf
from tle.util.db.user_db_conn import UserDbConn

if TYPE_CHECKING:
    # discord.py's payload types are for type checking: at run time, importing
    # them fails on their circular imports.
    from discord.types.components import ActionRow as ActionRowPayload

GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
MEMBER = 1_300_000_000_000_000_001  # the member in ``ctx``
OTHER_MEMBER = 1_300_000_000_000_000_002
ADMIN = 1_300_000_000_000_000_003
LEFT = 1_600_000_000_000_000_002  # not in the guild any more
MESSAGE = 1_400_000_000_000_000_001
NOW = CLOCK_START
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
TOKEN = 'kcpc-a1b2c3'  # the first token each test hands out
DOWN = ExternalServiceError(
    'AtCoder', 'AtCoder is not responding right now. Please try again later.'
)
# Real seconds a healthy teardown needs, many times over.
TEARDOWN_TIMEOUT = 10


class KcpcBot(commands.Bot):
    """A bot with KCPC's services and TLE's user database, as TLEBot has."""

    kcpc: KcpcServices | None = None
    user_db: UserDbConn | None = None
    test_guilds: Sequence[discord.Guild] = ()

    @property
    def guilds(self) -> Sequence[discord.Guild]:
        return self.test_guilds


class FakeAtCoder:
    """AtCoder's profile pages, found whatever the case.

    ``errors`` are raised and ``stalled`` names never answered, by name.
    """

    def __init__(self) -> None:
        self.profiles: dict[str, AtCoderProfile] = {}
        self.errors: dict[str, Exception] = {}
        self.stalled: set[str] = set()
        self.fetched: list[str] = []

    def add(
        self, handle: str, rating: int | None = 1834, *, affiliation: str | None = None
    ) -> None:
        self.profiles[handle.lower()] = AtCoderProfile(
            handle=handle,
            rating=rating,
            highest_rating=None if rating is None else rating + 78,
            rated_matches=0 if rating is None else 27,
            affiliation=affiliation,
            color='unrated' if rating is None else 'cyan',
            url=PROFILE_URL.format(handle=handle),
        )

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        self.fetched.append(handle)
        if handle in self.stalled:
            await asyncio.Event().wait()  # until cancelled
        error = self.errors.get(handle)
        if error is not None:
            raise error
        return self.profiles.get(handle.lower())


class FakeCodeforces:
    """``cf.user.info``: found whatever the case, failing for an unknown handle."""

    def __init__(self) -> None:
        self.users: dict[str, cf.User] = {}
        self.asked: list[list[str]] = []

    def add(
        self, handle: str, rating: int | None = 1700, *, organization: str | None = None
    ) -> None:
        self.users[handle.lower()] = cf.User(
            handle=handle,
            firstName=None,
            lastName=None,
            country=None,
            city=None,
            organization=organization,
            contribution=0,
            rating=rating,
            maxRating=None if rating is None else rating + 100,
            lastOnlineTimeSeconds=1_790_000_000,
            registrationTimeSeconds=1_600_000_000,
            friendOfCount=0,
            titlePhoto='https://userpic.codeforces.org/no-title.jpg',
        )

    async def info(self, *, handles: Sequence[str]) -> list[cf.User]:
        self.asked.append(list(handles))
        found = []
        for handle in handles:
            if handle.lower() not in self.users:
                raise cf.HandleNotFoundError(
                    f'User with handle {handle} not found', handle
                )
            found.append(self.users[handle.lower()])
        return found


class Server:
    """A mocked guild with a role for each Codeforces rank, and its members."""

    def __init__(self, guild_id: int) -> None:
        self.guild = MagicMock(spec=discord.Guild, id=guild_id)
        self.guild.name = 'Test Server'
        self.roles: dict[str, MagicMock] = {}
        for rank in cf.RATED_RANKS:
            role = MagicMock(spec=discord.Role)
            role.name = rank.title
            self.roles[rank.title] = role
        self.guild.roles = list(self.roles.values())
        self.members: dict[int, MagicMock] = {}
        self.guild.get_member.side_effect = self.members.get

    def add_member(self, user_id: int, name: str = 'Fake Member') -> MagicMock:
        """A member of the guild; adding and removing roles updates theirs."""
        member = MagicMock(spec=discord.Member, id=user_id, guild=self.guild)
        member.mention = f'<@{user_id}>'
        member.display_name = name
        member.roles = []

        async def add_roles(*roles: MagicMock, reason: str | None = None) -> None:
            member.roles = [*member.roles, *roles]

        async def remove_roles(*roles: MagicMock, reason: str | None = None) -> None:
            member.roles = [role for role in member.roles if role not in roles]

        member.add_roles = AsyncMock(side_effect=add_roles)
        member.remove_roles = AsyncMock(side_effect=remove_roles)
        self.members[user_id] = member
        return member


@pytest.fixture(autouse=True)
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens are handed out in order: kcpc-a1b2c3, then kcpc-000000, ..."""
    hexes = itertools.chain(['a1b2c3'], (f'{n:06x}' for n in itertools.count()))
    monkeypatch.setattr(service_module.secrets, 'token_hex', lambda size: next(hexes))


@pytest.fixture
def atcoder(monkeypatch: pytest.MonkeyPatch) -> FakeAtCoder:
    fake = FakeAtCoder()
    monkeypatch.setattr(accounts_cog, 'AtCoderProfileClient', lambda http: fake)
    return fake


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.user, 'info', fake.info)
    return fake


@pytest.fixture
async def services(
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> AsyncIterator[KcpcServices]:
    publisher = DiscordPublisher(
        MagicMock(spec=commands.Bot), guild_settings, ledger, clock
    )
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
    yield services
    # The db fixture closes the database.
    await asyncio.wait_for(services.scheduler.stop(), TEARDOWN_TIMEOUT)
    await asyncio.wait_for(services.http.close(), TEARDOWN_TIMEOUT)


async def make_bot(services: KcpcServices, user_db: UserDbConn) -> KcpcBot:
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    bot.user_db = user_db
    return bot


async def load_accounts(bot: commands.Bot) -> KcpcAccounts:
    """Add the accounts cog as its extension does."""
    await setup(bot)
    cog = bot.get_cog('KcpcAccounts')
    assert isinstance(cog, KcpcAccounts)
    return cog


@pytest.fixture
async def bot(
    services: KcpcServices,
    user_db: UserDbConn,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> AsyncIterator[KcpcBot]:
    """A real bot with the accounts cog loaded."""
    bot = await make_bot(services, user_db)
    await load_accounts(bot)
    yield bot
    await bot.close()


@pytest.fixture
async def admin_bot(
    services: KcpcServices,
    user_db: UserDbConn,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> AsyncIterator[KcpcBot]:
    """A real bot with the admin cog, then the accounts cog, as at startup."""
    bot = await make_bot(services, user_db)
    await add_admin_cog(bot)
    await load_accounts(bot)
    yield bot
    await bot.close()


@pytest.fixture
def server() -> Server:
    return Server(GUILD)


@pytest.fixture
def member(server: Server) -> MagicMock:
    return server.add_member(MEMBER, 'Fake Member')


@pytest.fixture
def ctx(server: Server, member: MagicMock) -> MagicMock:
    """The context of a command the member runs; replies are recorded."""
    return make_ctx(server, member)


def make_ctx(server: Server, author: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = server.guild
    ctx.author = author
    ctx.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    ctx.defer = AsyncMock()
    return ctx


def add_admin(server: Server) -> MagicMock:
    """A member of the guild with the Manage Server permission."""
    admin = server.add_member(ADMIN, 'Fake Admin')
    admin.guild_permissions = discord.Permissions(manage_guild=True)
    return admin


def real_context(
    bot: commands.Bot, server: Server, author: MagicMock, *, slash: bool = False
) -> commands.Context[commands.Bot]:
    """A real context in the guild, to run checks on; with ``slash``, a slash one."""
    message = MagicMock(spec=discord.Message, guild=server.guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
    return context


@pytest.fixture
def repo(db: Database) -> AccountRepo:
    return AccountRepo(db)


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


async def run(
    bot: commands.Bot, name: str, ctx: MagicMock, *args: object, **kwargs: object
) -> None:
    """Call the callback of the command ``name`` with parsed arguments."""
    command = command_named(bot, name)
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args, **kwargs)


def reply(ctx: MagicMock, **kwargs: object) -> discord.Embed:
    """The embed of the one reply, sent with ``kwargs`` besides."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, **kwargs)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def stamp(moment: datetime, style: str) -> str:
    return f'<t:{to_epoch(moment)}:{style}>'


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 5 s of real time) until ``condition()`` holds."""
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


def registered_items(bot: commands.Bot) -> list[type]:
    """The dynamic items whose presses the bot answers.

    discord.py keeps no public list of them.
    """
    return list(bot._connection._view_store._dynamic_items.values())


def snapshot(
    platform: str,
    handle: str,
    rating: int | None,
    *,
    rank: str | None = None,
    at: datetime = NOW,
) -> AccountSnapshot:
    matches = 27 if platform == 'atcoder' and rating is not None else None
    peak = None if rating is None else rating + 100
    return AccountSnapshot(platform, handle, rating, peak, rank, matches, at)


async def start_linking(
    bot: commands.Bot, server: Server, member: MagicMock, platform: str, handle: str
) -> VerifyLinkButton:
    """Run /link <platform> <handle> as ``member``; returns the Verify button sent."""
    ctx = make_ctx(server, member)
    await run(bot, f'link {platform}', ctx, handle)
    send = cast(AsyncMock, ctx.send)
    assert send.await_args is not None
    view = send.await_args.kwargs['view']
    (button,) = view.children
    assert isinstance(button, VerifyLinkButton)
    return button


def make_interaction(bot: commands.Bot, server: Server, user: MagicMock) -> MagicMock:
    """A press of a button by ``user``; deferring marks it answered."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.client = bot
    interaction.guild = server.guild
    interaction.user = user
    interaction.data = {}
    answered: list[bool] = []

    async def defer(**kwargs: object) -> None:
        answered.append(True)

    interaction.response.defer = AsyncMock(side_effect=defer)
    interaction.response.is_done = MagicMock(side_effect=lambda: bool(answered))
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def followup(interaction: MagicMock) -> discord.Embed:
    """The one ephemeral follow-up of a press, after an ephemeral deferral."""
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    interaction.followup.send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    embed = interaction.followup.send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


# Loading and unloading


async def test_loading_adds_the_commands_the_verify_button_and_the_jobs(
    bot: KcpcBot, services: KcpcServices
) -> None:
    assert set(bot.all_commands) == {'help', 'link', 'unlink', 'profile', 'rank'}
    link = command_named(bot, 'link')
    assert isinstance(link, commands.HybridGroup)
    assert sorted(link.all_commands) == ['atcoder', 'codeforces', 'verify']
    tree = {command.name: command for command in bot.tree.get_commands()}
    assert set(tree) == {'link', 'unlink', 'profile', 'rank'}
    assert all(command.guild_only for command in tree.values())
    slash_link = tree['link']
    assert isinstance(slash_link, app_commands.Group)
    assert sorted(c.name for c in slash_link.commands) == [
        'atcoder',
        'codeforces',
        'verify',
    ]

    assert registered_items(bot) == [VerifyLinkButton]
    assert [
        (job.name, job.description, job.persistent)
        for job in services.scheduler.status()
    ] == [(PURGE_JOB, 'every 1h', False), (REFRESH_JOB, 'every 6h', False)]


async def test_the_jobs_wait_for_their_slots_when_the_bot_starts(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    # Work for each job, were it to run at start: a member's account to
    # refresh, and an expired token to delete.
    bot.test_guilds = [server.guild]
    await repo.link(
        LinkedAccount(GUILD, MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW)
    )
    atcoder.add('FakeAtCoder')
    expired = LinkChallenge(
        GUILD, MEMBER, 'codeforces', 'FakeCoder', TOKEN, NOW - HOUR, NOW - MINUTE
    )
    await repo.save_challenge(expired)

    services.scheduler.start()
    await eventually(
        lambda: clock.pending_sleepers == 2, 'both jobs wait for their slots'
    )

    # Neither job runs at start: each waits for its first slot.
    assert [
        (job.name, job.next_run, job.last_slot, job.failures)
        for job in services.scheduler.status()
    ] == [
        (PURGE_JOB, NOW + HOUR, None, 0),
        # 18:00 UTC: every 6 hours from midnight
        (REFRESH_JOB, NOW + 6 * HOUR, None, 0),
    ]
    assert atcoder.fetched == []
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') == expired


async def test_removing_the_cog_undoes_everything_loading_did(
    bot: KcpcBot, services: KcpcServices
) -> None:
    await bot.remove_cog('KcpcAccounts')

    assert services.scheduler.status() == []
    assert registered_items(bot) == []
    assert set(bot.all_commands) == {'help'}
    assert bot.tree.get_commands() == []

    # So the extension can be loaded again.
    again = await load_accounts(bot)
    assert command_named(bot, 'link verify').cog is again
    assert registered_items(bot) == [VerifyLinkButton]
    assert len(services.scheduler.status()) == 2


async def test_a_load_that_cannot_add_every_job_is_undone(
    services: KcpcServices, user_db: UserDbConn, atcoder: FakeAtCoder
) -> None:
    async def other(slot: datetime) -> None:
        pass

    services.scheduler.add(
        ScheduledJob(PURGE_JOB, Every(HOUR), other, persistent=False)
    )
    bot = await make_bot(services, user_db)
    try:
        with pytest.raises(ValueError, match='already scheduled'):
            await load_accounts(bot)

        assert bot.get_cog('KcpcAccounts') is None
        assert [job.name for job in services.scheduler.status()] == [PURGE_JOB]
        assert registered_items(bot) == []
        assert set(bot.all_commands) == {'help'}
    finally:
        await bot.close()


def kcpc_slash(bot: commands.Bot) -> app_commands.Group:
    group = bot.tree.get_command('kcpc')
    assert isinstance(group, app_commands.Group)
    return group


async def test_the_admin_commands_join_kcpc_and_leave_with_the_cog(
    admin_bot: KcpcBot,
) -> None:
    unlink = command_named(admin_bot, 'kcpc accounts unlink')
    assert unlink.cog is admin_bot.get_cog('KcpcAccounts')
    slash = kcpc_slash(admin_bot).get_command('accounts')
    assert isinstance(slash, app_commands.Group)
    assert [command.name for command in slash.commands] == ['unlink']
    # Not top-level commands, which every member would be shown.
    assert set(admin_bot.all_commands) == {
        'help',
        'kcpc',
        'link',
        'unlink',
        'profile',
        'rank',
    }

    await admin_bot.remove_cog('KcpcAccounts')

    assert admin_bot.get_command('kcpc accounts') is None
    assert kcpc_slash(admin_bot).get_command('accounts') is None


async def test_a_load_that_fails_takes_the_admin_commands_away_again(
    services: KcpcServices, user_db: UserDbConn, atcoder: FakeAtCoder
) -> None:
    async def other(slot: datetime) -> None:
        pass

    services.scheduler.add(
        ScheduledJob(PURGE_JOB, Every(HOUR), other, persistent=False)
    )
    bot = await make_bot(services, user_db)
    try:
        await add_admin_cog(bot)

        with pytest.raises(ValueError, match='already scheduled'):
            await load_accounts(bot)

        assert bot.get_command('kcpc accounts') is None
        assert kcpc_slash(bot).get_command('accounts') is None
        assert registered_items(bot) == []
    finally:
        await bot.close()


def test_the_codeforces_colours_are_tles() -> None:
    assert CODEFORCES_COLORS == {
        rank.title.lower(): rank.color_embed for rank in cf.RATED_RANKS
    }
    assert set(ATCODER_COLORS) == {
        'gray',
        'brown',
        'green',
        'cyan',
        'blue',
        'yellow',
        'orange',
        'red',
    }


# /link


async def test_link_atcoder_gives_the_steps_and_a_verify_button(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')

    await run(bot, 'link atcoder', ctx, 'fakeatcoder')

    ctx.defer.assert_awaited_once_with(ephemeral=True)
    embed = reply(ctx, view=ANY, ephemeral=True)
    assert embed.title == 'Link your AtCoder account FakeAtCoder'
    assert embed.description == '\n'.join(
        [
            '1. Open <https://atcoder.jp/settings> and add this token anywhere in '
            f'your **Affiliation**: `{TOKEN}`',
            '2. Save your settings.',
            '3. Press **Verify** below, or use `/link verify atcoder`.',
            '',
            f'The token expires in 10 minutes, at {stamp(NOW + 10 * MINUTE, "t")}. '
            'Once your account is linked, remove it again.',
        ]
    )
    assert embed.colour == discord.Colour(KCPC_COLOR)
    view = ctx.send.await_args.kwargs['view']
    assert view.timeout is None  # the button lasts as long as the token
    (button,) = view.children
    assert isinstance(button, VerifyLinkButton)
    assert button.custom_id == f'kcpc:link:atcoder:{MEMBER}'
    assert button.item.label == 'Verify'
    assert button.item.style is discord.ButtonStyle.success
    challenge = await repo.get_challenge(GUILD, MEMBER, 'atcoder')
    assert challenge is not None and challenge.token == TOKEN


async def test_link_codeforces_says_to_use_the_organization(
    bot: KcpcBot, ctx: MagicMock, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')

    await run(bot, 'link codeforces', ctx, 'fakecoder')

    embed = reply(ctx, view=ANY, ephemeral=True)
    assert embed.title == 'Link your Codeforces account FakeCoder'
    assert embed.description is not None
    assert embed.description.startswith(
        '1. Open <https://codeforces.com/settings/social> and add this token '
        f'anywhere in your **Organization**: `{TOKEN}`\n'
    )
    assert '`/link verify codeforces`' in embed.description
    (button,) = ctx.send.await_args.kwargs['view'].children
    assert button.custom_id == f'kcpc:link:codeforces:{MEMBER}'


async def test_link_codeforces_refuses_a_member_who_has_a_handle(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    await user_db.set_handle(MEMBER, GUILD, 'OldCoder')
    codeforces.add('FakeCoder')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'link codeforces', ctx, 'FakeCoder')

    assert str(raised.value) == (
        'Your Codeforces handle is already set to OldCoder. '
        'Ask an Admin or Moderator if you wish to change it.'
    )
    ctx.send.assert_not_awaited()
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None


async def test_link_codeforces_refuses_a_handle_another_member_has(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    await user_db.set_handle(OTHER_MEMBER, GUILD, 'FakeCoder')
    codeforces.add('FakeCoder')

    with pytest.raises(HandleTaken):
        await run(bot, 'link codeforces', ctx, 'fakecoder')

    assert codeforces.asked == []
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None


async def test_link_codeforces_refuses_a_handle_a_member_who_left_has(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    await user_db.set_handle(OTHER_MEMBER, GUILD, 'FakeCoder')
    # TLE keeps the handle of a member who leaves, marked inactive, and won't
    # link it to anyone else.
    await user_db.set_inactive([(str(GUILD), str(OTHER_MEMBER))])
    assert await user_db.get_handles_for_guild(GUILD) == []
    codeforces.add('FakeCoder')

    with pytest.raises(HandleTaken, match='ask an Admin or Moderator to remove it'):
        await run(bot, 'link codeforces', ctx, 'fakecoder')

    assert codeforces.asked == []
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None


async def test_link_codeforces_refuses_an_account_without_a_rank_role_here(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    server.guild.roles = [r for r in server.guild.roles if r.name != 'Expert']
    codeforces.add('FakeCoder', 1700)  # Expert

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'link codeforces', ctx, 'fakecoder')

    # As TLE refuses it, but before the member has edited their profile.
    assert str(raised.value) == 'Role for rank `Expert` not present in the server'
    ctx.send.assert_not_awaited()
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None
    # An unrated account needs no rank role.
    codeforces.add('NewCoder', None)
    await run(bot, 'link codeforces', ctx, 'newcoder')
    challenge = await repo.get_challenge(GUILD, MEMBER, 'codeforces')
    assert challenge is not None and challenge.handle == 'NewCoder'


async def test_link_atcoder_refuses_a_handle_another_member_linked(
    admin_bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    server.add_member(OTHER_MEMBER, 'Other Member')
    atcoder.add('FakeAtCoder')
    await repo.link(
        LinkedAccount(
            GUILD, OTHER_MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
        )
    )

    with pytest.raises(HandleTaken, match='ask an admin to unlink it'):
        await run(admin_bot, 'link atcoder', ctx, 'FakeAtCoder')

    ctx.send.assert_not_awaited()
    assert atcoder.fetched == []


async def test_without_kcpc_admin_a_taken_atcoder_handle_is_left_to_its_member(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    # The kcpc.admin extension is switched off: there is no /kcpc accounts
    # unlink for an admin to free the handle with.
    assert bot.get_command('kcpc') is None
    atcoder.add('FakeAtCoder')
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    # Another member links the account first.
    server.add_member(OTHER_MEMBER, 'Other Member')
    theirs = LinkedAccount(
        GUILD, OTHER_MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
    )
    await repo.link(theirs)
    taken = (
        'The handle FakeAtCoder is already linked to someone else in this server. '
        'If it is yours, ask the member who linked it to unlink it with '
        '/unlink atcoder.'
    )

    with pytest.raises(HandleTaken) as at_verify:
        await run(bot, 'link verify', ctx, 'atcoder')
    with pytest.raises(HandleTaken) as at_link:
        await run(bot, 'link atcoder', make_ctx(server, member), 'FakeAtCoder')

    assert str(at_verify.value) == taken
    assert str(at_link.value) == taken
    assert await repo.links_for_guild(GUILD, 'atcoder') == [theirs]


async def test_an_atcoder_account_linked_by_a_member_who_left_goes_to_its_prover(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    clock: FakeClock,
) -> None:
    atcoder.add('FakeAtCoder')
    await repo.link(
        LinkedAccount(GUILD, LEFT, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW)
    )

    # The member who left doesn't stop anyone proving the account is theirs.
    await start_linking(bot, server, member, 'atcoder', 'fakeatcoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    await clock.advance(MINUTE)
    await run(bot, 'link verify', ctx, 'atcoder')

    assert reply(ctx, ephemeral=True).colour == discord.Colour(SUCCESS_COLOR)
    assert await repo.links_for_guild(GUILD, 'atcoder') == [
        LinkedAccount(
            GUILD, MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW + MINUTE
        )
    ]


async def test_a_member_who_comes_back_before_verify_keeps_their_atcoder_link(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    theirs = LinkedAccount(
        GUILD, LEFT, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
    )
    await repo.link(theirs)
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    server.add_member(LEFT, 'Back Again')

    with pytest.raises(HandleTaken):
        await run(bot, 'link verify', ctx, 'atcoder')

    assert await repo.links_for_guild(GUILD, 'atcoder') == [theirs]
    assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is not None


async def test_until_the_bot_has_every_member_nobody_counts_as_having_left(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    theirs = LinkedAccount(
        GUILD, LEFT, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
    )
    await repo.link(theirs)
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    # The bot reconnects to Discord, and has only some of the server's members
    # until Discord has sent it the rest: whoever linked the account may be a
    # member it hasn't been sent yet.
    server.guild.chunked = False

    with pytest.raises(HandleTaken):
        await run(bot, 'link verify', ctx, 'atcoder')
    with pytest.raises(HandleTaken):
        await run(bot, 'link atcoder', make_ctx(server, member), 'FakeAtCoder')
    ranked = make_ctx(server, member)
    await run(bot, 'rank', ranked, 'atcoder')

    assert await repo.links_for_guild(GUILD, 'atcoder') == [theirs]
    assert f'<@{LEFT}>' in str(reply(ranked, ephemeral=False).description)
    # Once the bot has every member, the holder has left, and the token holds.
    server.guild.chunked = True
    await run(bot, 'link verify', ctx, 'atcoder')
    assert reply(ctx, ephemeral=True).colour == discord.Colour(SUCCESS_COLOR)
    assert await repo.links_for_guild(GUILD, 'atcoder') == [
        LinkedAccount(GUILD, MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW)
    ]


async def test_link_works_as_a_prefix_command_and_its_errors_get_a_reply(
    bot: KcpcBot, server: Server, member: MagicMock, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder')

    async def invoke(args: str) -> AsyncMock:
        message = MagicMock(spec=discord.Message, guild=server.guild, author=member)
        context: commands.Context[commands.Bot] = commands.Context(
            message=message, bot=bot, view=StringView(args), prefix=';'
        )
        send = AsyncMock()
        context.send = send  # type: ignore[method-assign]
        link = command_named(bot, 'link')
        context.command = link
        try:
            await link.invoke(context)
        except commands.CommandError as error:
            await link.dispatch_error(context, error)
        return send

    started = await invoke('atcoder FakeAtCoder')
    refused = await invoke('verify atcoder')

    started.assert_awaited_once_with(embed=ANY, view=ANY, ephemeral=True)
    refused.assert_awaited_once_with(embed=ANY, ephemeral=True)
    assert refused.await_args is not None
    alert = refused.await_args.kwargs['embed']
    assert alert.colour == discord.Colour(ALERT_COLOR)
    assert alert.description == (
        f"The token {TOKEN} isn't in the Affiliation of FakeAtCoder yet. "
        'Put it there at <https://atcoder.jp/settings>, save, then verify again.'
    )


# /link verify


async def test_verify_atcoder_links_the_account(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    clock: FakeClock,
) -> None:
    atcoder.add('FakeAtCoder')
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=f'KCPC {TOKEN}')
    await clock.advance(5 * MINUTE)

    await run(bot, 'link verify', ctx, 'atcoder')

    ctx.defer.assert_awaited_once_with(ephemeral=True)
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        'Linked your AtCoder account '
        '[FakeAtCoder](https://atcoder.jp/users/FakeAtCoder), rated 1834.\n'
        'Please remove the token from your Affiliation now.'
    )
    done = NOW + 5 * MINUTE
    assert await repo.get_link(GUILD, MEMBER, 'atcoder') == LinkedAccount(
        GUILD, MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', done
    )
    assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is None
    assert await repo.snapshot('atcoder', 'FakeAtCoder') == AccountSnapshot(
        'atcoder', 'FakeAtCoder', 1834, 1912, 'cyan', 27, done
    )


async def test_verify_codeforces_links_the_handle_for_tle_too(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    codeforces.add('FakeCoder')
    await start_linking(bot, server, member, 'codeforces', 'fakecoder')
    codeforces.add('FakeCoder', organization=TOKEN.upper())

    await run(bot, 'link verify', ctx, 'codeforces')

    assert reply(ctx, ephemeral=True).description == (
        'Linked your Codeforces account '
        '[FakeCoder](https://codeforces.com/profile/FakeCoder), rated 1700.\n'
        'Please remove the token from your Organization now.\n'
        "TLE's commands, such as gitgud, duels and rank roles, use it too."
    )
    # In TLE's table, with the rank role, as ;handle set would.
    assert await user_db.get_handle(MEMBER, GUILD) == 'FakeCoder'
    expert = server.roles['Expert']
    assert member.roles == [expert]
    member.add_roles.assert_awaited_once_with(
        expert, reason='Codeforces handle verified with /link'
    )
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None
    assert await repo.snapshot('codeforces', 'FakeCoder') == AccountSnapshot(
        'codeforces', 'FakeCoder', 1700, 1800, 'expert', None, NOW
    )


async def test_verify_with_the_token_missing_links_nothing(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder', affiliation='kcpc-0b0b0b')
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')

    with pytest.raises(KcpcUserError, match="The token kcpc-a1b2c3 isn't in the"):
        await run(bot, 'link verify', ctx, 'atcoder')

    assert await repo.get_link(GUILD, MEMBER, 'atcoder') is None
    assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is not None


async def test_verify_after_the_token_expired_says_to_start_again(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    clock: FakeClock,
) -> None:
    atcoder.add('FakeAtCoder')
    await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    await clock.advance(10 * MINUTE)

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'link verify', ctx, 'atcoder')

    assert str(raised.value) == (
        'Your token for FakeAtCoder has expired. Get a new one with '
        '/link atcoder FakeAtCoder.'
    )
    assert await repo.get_link(GUILD, MEMBER, 'atcoder') is None


async def test_verify_codeforces_refuses_once_a_moderator_set_a_handle(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    user_db: UserDbConn,
    codeforces: FakeCodeforces,
) -> None:
    codeforces.add('FakeCoder', organization=TOKEN)
    await start_linking(bot, server, member, 'codeforces', 'FakeCoder')
    await user_db.set_handle(MEMBER, GUILD, 'ModsChoice')

    with pytest.raises(KcpcUserError, match='already set to ModsChoice'):
        await run(bot, 'link verify', ctx, 'codeforces')

    assert await user_db.get_handle(MEMBER, GUILD) == 'ModsChoice'


async def test_verify_codeforces_links_nothing_if_tle_refuses_the_handle(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    member: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    codeforces.add('FakeCoder', organization=TOKEN)
    await start_linking(bot, server, member, 'codeforces', 'FakeCoder')
    # Another member gets the handle meanwhile, e.g. from a moderator.
    await user_db.set_handle(OTHER_MEMBER, GUILD, 'FakeCoder')

    with pytest.raises(KcpcUserError, match='already associated with another user'):
        await run(bot, 'link verify', ctx, 'codeforces')

    assert await user_db.get_handle(MEMBER, GUILD) is None
    assert member.roles == []
    # The member can try again once the handle is theirs.
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is not None
    assert await repo.snapshot('codeforces', 'FakeCoder') is None


# The Verify button


async def test_pressing_verify_links_the_account(
    bot: KcpcBot,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    button = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    interaction = make_interaction(bot, server, member)

    await button.callback(interaction)

    embed = followup(interaction)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description is not None
    assert embed.description.startswith('Linked your AtCoder account')
    assert await repo.get_link(GUILD, MEMBER, 'atcoder') is not None


async def test_a_verify_button_pressed_by_someone_else_is_refused(
    bot: KcpcBot,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    button = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    stranger = server.add_member(OTHER_MEMBER, 'Stranger')
    interaction = make_interaction(bot, server, stranger)

    await button.callback(interaction)

    embed = followup(interaction)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NOT_YOUR_LINK
    assert await repo.links_for_guild(GUILD, 'atcoder') == []
    assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is not None
    assert atcoder.fetched == ['FakeAtCoder']  # only when the link started


async def test_a_verify_button_that_fails_says_why(
    bot: KcpcBot, server: Server, member: MagicMock, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder')
    button = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.errors['FakeAtCoder'] = DOWN
    interaction = make_interaction(bot, server, member)

    await button.callback(interaction)

    assert followup(interaction).description == str(DOWN)


async def test_a_codeforces_link_is_completed_if_discord_refuses_the_rank_roles(
    bot: KcpcBot,
    server: Server,
    member: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    codeforces: FakeCodeforces,
) -> None:
    codeforces.add('Fake_Coder', organization=TOKEN)
    button = await start_linking(bot, server, member, 'codeforces', 'Fake_Coder')
    member.add_roles.side_effect = discord.Forbidden(
        MagicMock(status=403, reason='Forbidden'), 'Missing Permissions'
    )
    interaction = make_interaction(bot, server, member)

    await button.callback(interaction)

    # What an admin must fix, not an apology for an unexpected error.
    embed = followup(interaction)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == (
        r'Your Codeforces account Fake\_Coder is linked, but Discord '
        "didn't let me change your rank roles. Ask an admin to check that I "
        'have the Manage Roles permission and that my highest role is above '
        'the rank roles.'
    )
    # The handle is linked all the same, so the link is completed.
    assert await user_db.get_handle(MEMBER, GUILD) == 'Fake_Coder'
    assert await repo.get_challenge(GUILD, MEMBER, 'codeforces') is None
    assert await repo.snapshot('codeforces', 'Fake_Coder') == AccountSnapshot(
        'codeforces', 'Fake_Coder', 1700, 1800, 'expert', None, NOW
    )


async def test_a_verify_button_pressed_without_the_cog_says_it_is_unavailable(
    bot: KcpcBot, server: Server, member: MagicMock, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    button = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    await bot.remove_cog('KcpcAccounts')
    interaction = make_interaction(bot, server, member)

    await button.callback(interaction)

    assert followup(interaction).description == (
        'KCPC features are not available right now.'
    )


async def test_a_new_button_made_from_the_custom_id_verifies_the_stored_challenge(
    bot: KcpcBot,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    sent = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation=TOKEN)
    custom_id = sent.custom_id
    interaction = make_interaction(bot, server, member)
    match = re.fullmatch(VERIFY_TEMPLATE, custom_id)
    assert match is not None

    fresh = await VerifyLinkButton.from_custom_id(
        interaction, discord.ui.Button(custom_id=custom_id), match
    )
    await fresh.callback(interaction)

    assert fresh is not sent
    assert (fresh.platform, fresh.user_id, fresh.custom_id) == (
        'atcoder',
        MEMBER,
        custom_id,
    )
    assert followup(interaction).colour == discord.Colour(SUCCESS_COLOR)
    assert await repo.get_link(GUILD, MEMBER, 'atcoder') is not None


async def test_a_restart_mid_verification_still_verifies_through_discord_py(
    bot: KcpcBot,
    services: KcpcServices,
    user_db: UserDbConn,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    atcoder.add('FakeAtCoder')
    sent = await start_linking(bot, server, member, 'atcoder', 'FakeAtCoder')
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyLinkButton('atcoder', MEMBER))
    # discord.py types the rows a view sends as plain dicts.
    components = cast('list[ActionRowPayload]', view.to_components())
    # The bot stops, and a new process starts it again: only the database and
    # the message on Discord are left from before.
    await bot.remove_cog('KcpcAccounts')
    restarted = await make_bot(services, user_db)
    try:
        await load_accounts(restarted)
        atcoder.add('FakeAtCoder', affiliation=TOKEN)
        interaction = make_interaction(restarted, server, member)
        message = MagicMock(spec=discord.Message, id=MESSAGE)
        message.flags = discord.MessageFlags()
        message.components = [discord.ActionRow(row) for row in components]
        interaction.message = message

        restarted._connection._view_store.dispatch_view(
            discord.ComponentType.button.value, sent.custom_id, interaction
        )
        await eventually(
            lambda: interaction.followup.send.await_count == 1, 'the press is answered'
        )

        assert followup(interaction).colour == discord.Colour(SUCCESS_COLOR)
        assert await repo.get_link(GUILD, MEMBER, 'atcoder') is not None
        assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is None
    finally:
        await restarted.close()


# /unlink


async def test_unlink_atcoder_removes_the_link(
    bot: KcpcBot, ctx: MagicMock, repo: AccountRepo
) -> None:
    await repo.link(
        LinkedAccount(
            GUILD, MEMBER, 'atcoder', 'Fake_AtCoder', 'affiliation-token', NOW
        )
    )

    await run(bot, 'unlink', ctx, 'atcoder')

    embed = reply(ctx, ephemeral=True)
    assert embed.description == r'Unlinked your AtCoder account Fake\_AtCoder.'
    assert await repo.get_link(GUILD, MEMBER, 'atcoder') is None


async def test_unlink_atcoder_without_a_link_says_so(
    bot: KcpcBot, ctx: MagicMock
) -> None:
    with pytest.raises(KcpcUserError, match="You haven't linked an AtCoder account."):
        await run(bot, 'unlink', ctx, 'atcoder')


async def test_unlink_codeforces_points_to_the_moderators(
    bot: KcpcBot, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'unlink', ctx, 'codeforces')

    assert str(raised.value) == (
        "Your Codeforces handle is shared with TLE's commands, such as gitgud, "
        'duels and rank roles, so an Admin or Moderator unlinks it, with '
        '/handle remove.'
    )
    assert await user_db.get_handle(MEMBER, GUILD) == 'FakeCoder'


# /kcpc accounts


async def test_an_admin_frees_an_atcoder_handle_a_member_linked_wrongly(
    admin_bot: KcpcBot,
    server: Server,
    member: MagicMock,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
) -> None:
    # The token proves only that the other member could edit the profile.
    server.add_member(OTHER_MEMBER, 'Other Member')
    atcoder.add('Fake_AtCoder')
    await repo.link(
        LinkedAccount(
            GUILD, OTHER_MEMBER, 'atcoder', 'Fake_AtCoder', 'affiliation-token', NOW
        )
    )
    with pytest.raises(HandleTaken, match='ask an admin to unlink it'):
        await start_linking(admin_bot, server, member, 'atcoder', 'fake_atcoder')
    ctx = make_ctx(server, add_admin(server))

    await run(admin_bot, 'kcpc accounts unlink', ctx, 'FAKE_ATCODER')

    ctx.defer.assert_awaited_once_with(ephemeral=True)
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        rf'Unlinked the AtCoder account Fake\_AtCoder from <@{OTHER_MEMBER}>.'
    )
    assert await repo.links_for_guild(GUILD, 'atcoder') == []
    # So the account's owner can link it now.
    await start_linking(admin_bot, server, member, 'atcoder', 'fake_atcoder')
    challenge = await repo.get_challenge(GUILD, MEMBER, 'atcoder')
    assert challenge is not None
    assert (challenge.handle, challenge.token) == ('Fake_AtCoder', TOKEN)


@pytest.mark.parametrize(
    ('name', 'slash'),
    [
        ('kcpc accounts', False),  # Discord can't run a slash group on its own
        ('kcpc accounts unlink', False),
        ('kcpc accounts unlink', True),
    ],
)
async def test_the_admin_commands_are_for_admins_only(
    monkeypatch: pytest.MonkeyPatch,
    admin_bot: KcpcBot,
    server: Server,
    member: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # Their cog is this one, so the admin cog's check doesn't cover them.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    member.guild_permissions = discord.Permissions(manage_guild=False)
    command = command_named(admin_bot, name)

    assert await command.can_run(
        real_context(admin_bot, server, add_admin(server), slash=slash)
    )
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(real_context(admin_bot, server, member, slash=slash))


# /profile


async def link_both(user_db: UserDbConn, repo: AccountRepo, user_id: int) -> None:
    """Link FakeCoder on Codeforces and FakeAtCoder on AtCoder to the member."""
    await user_db.set_handle(user_id, GUILD, 'FakeCoder')
    await repo.link(
        LinkedAccount(
            GUILD, user_id, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
        )
    )


def fields(embed: discord.Embed) -> list[tuple[str | None, str | None]]:
    return [(field.name, field.value) for field in embed.fields]


def profile_embeds(ctx: MagicMock) -> list[discord.Embed]:
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embeds=ANY)
    assert send.await_args is not None
    embeds: list[discord.Embed] = send.await_args.kwargs['embeds']
    return embeds


async def test_profile_shows_each_account_in_its_ranks_colour(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    await link_both(user_db, repo, MEMBER)
    await repo.save_snapshots(
        [
            snapshot('codeforces', 'FakeCoder', 1700, rank='expert'),
            snapshot('atcoder', 'FakeAtCoder', 1834, rank='cyan'),
        ]
    )
    await clock.advance(HOUR)  # an hour old is fresh enough

    await run(bot, 'profile', ctx)

    ctx.defer.assert_awaited_once_with()
    first, second = profile_embeds(ctx)
    assert (first.title, first.url) == (
        'Codeforces: FakeCoder',
        'https://codeforces.com/profile/FakeCoder',
    )
    assert first.author.name == 'Fake Member'
    assert first.colour == discord.Colour(CODEFORCES_COLORS['expert'])
    assert fields(first) == [('Rating', '1700'), ('Peak', '1800'), ('Rank', 'Expert')]
    assert first.footer.text == 'Ratings as of'
    assert first.timestamp == NOW
    assert (second.title, second.url) == (
        'AtCoder: FakeAtCoder',
        'https://atcoder.jp/users/FakeAtCoder',
    )
    assert second.colour == discord.Colour(ATCODER_COLORS['cyan'])
    assert fields(second) == [
        ('Rating', '1834'),
        ('Peak', '1934'),
        ('Colour', 'Cyan'),
        ('Rated matches', '27'),
    ]
    assert codeforces.asked == atcoder.fetched == []


async def test_profile_refreshes_ratings_over_an_hour_old_first(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    await link_both(user_db, repo, MEMBER)
    await repo.save_snapshots(
        [
            snapshot('codeforces', 'FakeCoder', 1500, rank='specialist'),
            snapshot('atcoder', 'FakeAtCoder', 1834, rank='cyan', at=NOW + 2 * MINUTE),
        ]
    )
    codeforces.add('FakeCoder', 2000)
    await clock.advance(HOUR + MINUTE)

    await run(bot, 'profile', ctx)

    first, second = profile_embeds(ctx)
    assert codeforces.asked == [['FakeCoder']]
    assert atcoder.fetched == []  # under an hour old
    assert first.colour == discord.Colour(CODEFORCES_COLORS['candidate master'])
    assert fields(first) == [
        ('Rating', '2000'),
        ('Peak', '2100'),
        ('Rank', 'Candidate Master'),
    ]
    assert first.timestamp == NOW + HOUR + MINUTE
    assert fields(second)[0] == ('Rating', '1834')


async def test_profile_shows_stale_ratings_with_a_note_if_they_cant_be_refreshed(
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    await link_both(user_db, repo, MEMBER)
    await user_db.set_handle(MEMBER, GUILD, 'Fake_Coder')
    await repo.save_snapshots(
        [
            snapshot('codeforces', 'Fake_Coder', 1500, rank='specialist'),
            snapshot('atcoder', 'FakeAtCoder', 1834, rank='cyan'),
        ]
    )
    atcoder.errors['FakeAtCoder'] = DOWN  # and Codeforces has no Fake_Coder now
    await clock.advance(2 * HOUR)

    await run(bot, 'profile', ctx)

    first, second = profile_embeds(ctx)
    assert first.title == r'Codeforces: Fake\_Coder'
    assert fields(first)[0] == ('Rating', '1500')
    # A footer shows its text as it is, so the handle isn't escaped there.
    assert first.footer.text == (
        'Codeforces has no user called Fake_Coder now. Ratings as of'
    )
    assert first.timestamp == NOW
    assert fields(second)[0] == ('Rating', '1834')
    assert second.footer.text == "Couldn't refresh the ratings just now. Ratings as of"
    assert second.timestamp == NOW


async def test_profile_waits_only_so_long_for_a_site_that_doesnt_answer(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    ctx: MagicMock,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    monkeypatch.setattr(accounts_cog, '_PROFILE_REFRESH_WAIT', 0.05)
    await link_both(user_db, repo, MEMBER)
    await repo.save_snapshots(
        [
            snapshot('codeforces', 'FakeCoder', 1500, rank='specialist'),
            snapshot('atcoder', 'FakeAtCoder', 1834, rank='cyan'),
        ]
    )
    codeforces.add('FakeCoder', 2000)
    atcoder.stalled.add('FakeAtCoder')
    await clock.advance(2 * HOUR)

    # Real seconds, many times the wait: a /profile that waits for AtCoder
    # fails here rather than hanging.
    await asyncio.wait_for(run(bot, 'profile', ctx), 10)

    first, second = profile_embeds(ctx)
    # Codeforces was refreshed before the wait ran out: no note.
    assert fields(first)[0] == ('Rating', '2000')
    assert first.footer.text == 'Ratings as of'
    assert first.timestamp == NOW + 2 * HOUR
    assert atcoder.fetched == ['FakeAtCoder']
    assert fields(second)[0] == ('Rating', '1834')
    assert second.footer.text == "Couldn't refresh the ratings just now. Ratings as of"
    assert second.timestamp == NOW


async def test_profile_of_accounts_without_ratings(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> None:
    other = server.add_member(OTHER_MEMBER, 'Other Member')
    await link_both(user_db, repo, OTHER_MEMBER)
    codeforces.add('FakeCoder', None)
    atcoder.errors['FakeAtCoder'] = DOWN

    await run(bot, 'profile', ctx, other)

    first, second = profile_embeds(ctx)
    assert first.author.name == 'Other Member'
    assert (first.description, first.fields) == ('Unrated.', [])
    assert first.colour == discord.Colour(KCPC_COLOR)
    assert (second.description, second.footer.text) == (
        'No ratings yet.',
        "Couldn't refresh the ratings just now.",
    )
    assert second.timestamp is None


async def test_profile_leaves_out_a_peak_it_doesnt_know(
    bot: KcpcBot, ctx: MagicMock, user_db: UserDbConn, repo: AccountRepo
) -> None:
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    await repo.save_snapshots(
        [AccountSnapshot('codeforces', 'FakeCoder', 1700, None, 'expert', None, NOW)]
    )

    await run(bot, 'profile', ctx)

    (embed,) = profile_embeds(ctx)
    assert fields(embed) == [('Rating', '1700'), ('Rank', 'Expert')]


@pytest.mark.parametrize('whose', ['own', 'other'])
async def test_profile_of_a_member_who_linked_nothing_says_so(
    bot: KcpcBot, ctx: MagicMock, server: Server, whose: str
) -> None:
    other = server.add_member(OTHER_MEMBER)
    target = None if whose == 'own' else other

    with pytest.raises(KcpcUserError) as raised:
        await run(bot, 'profile', ctx, target)

    assert str(raised.value) == (
        "You haven't linked any accounts yet. Link one with /link codeforces "
        '<handle> or /link atcoder <handle>.'
        if whose == 'own'
        else f"<@{OTHER_MEMBER}> hasn't linked any accounts yet."
    )


# /rank


async def add_ranked(
    server: Server, user_db: UserDbConn, repo: AccountRepo, count: int
) -> None:
    """``count`` members with Codeforces handles, rated 2000, 1990, ..."""
    for n in range(count):
        user_id = 1_500_000_000_000_000_000 + n
        server.add_member(user_id)
        await user_db.set_handle(user_id, GUILD, f'Coder{n:02}')
        await repo.save_snapshots(
            [snapshot('codeforces', f'Coder{n:02}', 2000 - 10 * n, rank='master')]
        )


async def test_rank_lists_members_by_rating_unrated_last_and_where_you_are(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    user_db: UserDbConn,
    repo: AccountRepo,
) -> None:
    server.add_member(OTHER_MEMBER)
    await user_db.set_handle(MEMBER, GUILD, 'Fake_Coder')
    await user_db.set_handle(OTHER_MEMBER, GUILD, 'TopCoder')
    await user_db.set_handle(1_600_000_000_000_000_001, GUILD, 'Unfetched')
    await user_db.set_handle(LEFT, GUILD, 'LeftCoder')
    await repo.save_snapshots(
        [
            snapshot('codeforces', 'fake_coder', 1700, rank='expert'),
            snapshot('codeforces', 'TopCoder', 2400, rank='grandmaster'),
            snapshot('codeforces', 'LeftCoder', 3000, rank='legendary grandmaster'),
        ]
    )
    server.add_member(1_600_000_000_000_000_001)

    await run(bot, 'rank', ctx)

    embed = reply(ctx, ephemeral=False)
    assert embed.title == 'Codeforces leaderboard'
    assert embed.description == '\n'.join(
        [
            f'**1.** <@{OTHER_MEMBER}> · '
            '[TopCoder](https://codeforces.com/profile/TopCoder) · '
            '**2400** Grandmaster',
            f'**2.** <@{MEMBER}> · '
            r'[Fake\_Coder](https://codeforces.com/profile/Fake_Coder) · '
            '**1700** Expert',
            '**3.** <@1600000000000000001> · '
            '[Unfetched](https://codeforces.com/profile/Unfetched) · no ratings yet',
        ]
    )
    assert embed.footer.text == 'Your position: 2 of 3'


async def test_rank_pages_ten_members_at_a_time(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    user_db: UserDbConn,
    repo: AccountRepo,
) -> None:
    await add_ranked(server, user_db, repo, 12)

    await run(bot, 'rank', ctx)

    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, view=ANY, ephemeral=False)
    assert send.await_args is not None
    view = send.await_args.kwargs['view']
    assert isinstance(view, PageView)
    assert view.owner_id == MEMBER
    first, second = view.pages
    assert send.await_args.kwargs['embed'] is first
    assert first.description is not None and second.description is not None
    assert len(first.description.splitlines()) == 10
    assert second.description.splitlines() == [
        '**11.** <@1500000000000000010> · '
        '[Coder10](https://codeforces.com/profile/Coder10) · **1900** Master',
        '**12.** <@1500000000000000011> · '
        '[Coder11](https://codeforces.com/profile/Coder11) · **1890** Master',
    ]
    footer = "You aren't on it: link your Codeforces account with /link codeforces."
    assert first.footer.text == f'{footer} · Page 1 of 2'
    assert second.footer.text == f'{footer} · Page 2 of 2'


async def test_rank_atcoder_ranks_the_atcoder_links(
    bot: KcpcBot,
    ctx: MagicMock,
    server: Server,
    user_db: UserDbConn,
    repo: AccountRepo,
) -> None:
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')  # not on this one
    server.add_member(OTHER_MEMBER)
    for user_id, handle in ((MEMBER, 'FakeAtCoder'), (OTHER_MEMBER, 'Better')):
        await repo.link(
            LinkedAccount(GUILD, user_id, 'atcoder', handle, 'affiliation-token', NOW)
        )
    await repo.save_snapshots(
        [
            snapshot('atcoder', 'FakeAtCoder', 1834, rank='cyan'),
            snapshot('atcoder', 'Better', None, rank='unrated'),
        ]
    )

    await run(bot, 'rank', ctx, 'atcoder')

    embed = reply(ctx, ephemeral=False)
    assert embed.title == 'AtCoder leaderboard'
    assert embed.description == '\n'.join(
        [
            f'**1.** <@{MEMBER}> · '
            '[FakeAtCoder](https://atcoder.jp/users/FakeAtCoder) · **1834** Cyan',
            f'**2.** <@{OTHER_MEMBER}> · '
            '[Better](https://atcoder.jp/users/Better) · unrated',
        ]
    )
    assert embed.footer.text == 'Your position: 1 of 2'


async def test_rank_with_nobody_linked_says_how_to_link(
    bot: KcpcBot, ctx: MagicMock
) -> None:
    await run(bot, 'rank', ctx, 'atcoder')

    embed = reply(ctx, ephemeral=False)
    assert embed.title == 'AtCoder leaderboard'
    assert embed.description == (
        'Nobody here has linked their AtCoder account yet. Link yours with '
        '/link atcoder <handle>.'
    )


# The jobs


async def test_the_refresh_job_refreshes_the_accounts_of_the_bots_guilds_members(
    bot: KcpcBot,
    services: KcpcServices,
    user_db: UserDbConn,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> None:
    first, second = Server(GUILD), Server(OTHER_GUILD)
    first.add_member(MEMBER)
    second.add_member(MEMBER)
    second.add_member(OTHER_MEMBER)
    bot.test_guilds = [first.guild, second.guild]
    await user_db.set_handle(MEMBER, GUILD, 'FakeCoder')
    await user_db.set_handle(MEMBER, OTHER_GUILD, 'FakeCoder')
    await user_db.set_handle(OTHER_MEMBER, OTHER_GUILD, 'OtherCoder')
    for guild_id in (GUILD, OTHER_GUILD):
        await repo.link(
            LinkedAccount(
                guild_id, MEMBER, 'atcoder', 'FakeAtCoder', 'affiliation-token', NOW
            )
        )
    # Nothing shows the ratings of someone who left, so they aren't fetched.
    await user_db.set_handle(LEFT, GUILD, 'LeftCoder')
    await repo.link(
        LinkedAccount(GUILD, LEFT, 'atcoder', 'LeftAtCoder', 'affiliation-token', NOW)
    )
    codeforces.add('FakeCoder')
    codeforces.add('OtherCoder', 1300)
    codeforces.add('LeftCoder')
    atcoder.add('FakeAtCoder')
    atcoder.add('LeftAtCoder')

    await services.scheduler.run_slot(REFRESH_JOB)

    # Each account once, however many guilds link it.
    assert codeforces.asked == [['FakeCoder', 'OtherCoder']]
    assert atcoder.fetched == ['FakeAtCoder']
    assert set(await repo.snapshots('codeforces', ['FakeCoder', 'OtherCoder'])) == {
        'FakeCoder',
        'OtherCoder',
    }
    assert await repo.snapshot('atcoder', 'FakeAtCoder') is not None


async def test_the_purge_job_deletes_expired_tokens(
    bot: KcpcBot, services: KcpcServices, repo: AccountRepo
) -> None:
    await repo.save_challenge(
        LinkChallenge(
            GUILD, MEMBER, 'atcoder', 'FakeAtCoder', TOKEN, NOW - HOUR, NOW - MINUTE
        )
    )

    await services.scheduler.run_slot(PURGE_JOB)

    assert await repo.get_challenge(GUILD, MEMBER, 'atcoder') is None
