"""Tests for the algorithm of the month's cog (tle.kcpc.features.algo.cog).

The cog runs on real KCPC services (database, settings, ledger and scheduler),
whose publisher is FakePublisher, on a real bot that also has the admin cog.
It picks from the real catalog, always the first topic it is offered
(``FirstChoice``): prefix sums, then two pointers. Most tests call a command's
callback with a mocked context; the rest go through discord.py, for its
checks, its parsing, its error handling and how it adds and removes the cog.
The users are made up.

The clock starts on Thursday 2026-10-01 at 12:00 UTC, an hour after October's
slot (noon in London, 11:00 UTC); the next slot is 2026-11-01 at 12:00 UTC.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tests.kcpc.conftest import CLOCK_START
from tests.kcpc.fakes import FakePublisher
from tle import constants
from tle.config import Settings
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.cog import UNEXPECTED_ERROR_MESSAGE
from tle.kcpc.bot.embeds import ALERT_COLOR, SUCCESS_COLOR
from tle.kcpc.bot.pages import PageView
from tle.kcpc.bot.publisher import DiscordPublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.ledger import Delivery, DeliveryLedger
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome
from tle.kcpc.core.reminders import ReminderEngine
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo
from tle.kcpc.core.timeutil import to_epoch, zone
from tle.kcpc.features.admin.cog import setup as add_admin_cog
from tle.kcpc.features.algo import cog as algo_cog
from tle.kcpc.features.algo.catalog import ALGO_TOPICS
from tle.kcpc.features.algo.cog import KcpcAlgo, setup
from tle.kcpc.features.algo.repo import AlgoPick, AlgoRepo
from tle.kcpc.features.algo.service import ALGO, ALGO_JOB, AlgoService, post_key
from tle.kcpc.services import KcpcServices

T = TypeVar('T')

# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
ADMIN = 1_300_000_000_000_000_001  # the user ID of the admin in ``ctx``
MEMBER = 1_300_000_000_000_000_002  # the user ID of the member in ``member_ctx``

SECOND = timedelta(seconds=1)
HOUR = timedelta(hours=1)
NOW = CLOCK_START
CLUB = zone('Europe/London')
COG_LOGGER = 'tle.kcpc.features.algo.cog'
ADMIN_LOGGER = 'tle.kcpc.bot.admin'
ADMIN_COMMANDS = ['post-now', 'preview', 'reroll']
PREFIX_SUMS, TWO_POINTERS = ALGO_TOPICS[:2]
NOT_SET_UP = (
    "The algorithm of the month isn't set up here. Turn it on with "
    '`/kcpc enable algo` and set its channel with `/kcpc channel algo #channel`, '
    'then try again.'
)
NO_OTHER_TOPIC = (
    '**Sprague-Grundy theorem** is the only topic this server has yet to have '
    'before the list starts over, so there is no other to reroll to.'
)
# Real seconds a healthy teardown needs, many times over. One that hangs then
# fails its test instead of stalling the whole run.
TEARDOWN_TIMEOUT = 10


def first(month: int, year: int = 2026) -> datetime:
    """The slot of the 1st of ``month``: noon in London, in UTC."""
    return datetime(year, month, 1, 12, 0, tzinfo=CLUB).astimezone(UTC)


def when(moment: datetime) -> str:
    seconds = to_epoch(moment)
    return f'<t:{seconds}:F> (<t:{seconds}:R>)'


class KcpcBot(commands.Bot):
    """A bot with KCPC's services, as TLEBot has.

    It is in the servers ``joined``, as Discord would have said at login.
    """

    kcpc: KcpcServices | None = None
    joined = frozenset({GUILD, OTHER_GUILD})

    def get_guild(self, id: int, /) -> discord.Guild | None:
        if id not in self.joined:
            return None
        return cast(discord.Guild, MagicMock(spec=discord.Guild, id=id))


class FirstChoice(random.Random):
    """A ``random.Random`` that chooses the first of what it is offered."""

    def choice(self, seq: Sequence[T]) -> T:
        return seq[0]


@pytest.fixture(autouse=True)
def first_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the cog's service pick the first topic it may."""
    real = algo_cog.AlgoService

    def make(*args: Any, **kwargs: Any) -> AlgoService:
        return real(*args, rng=FirstChoice(), **kwargs)

    monkeypatch.setattr(algo_cog, 'AlgoService', make)


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
async def services(
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
) -> AsyncIterator[KcpcServices]:
    services = KcpcServices(
        settings=Settings(),
        clock=clock,
        db=db,
        http=HttpClient(user_agent='kcpc-test', clock=clock),
        features=feature_registry,
        guild_settings=guild_settings,
        ledger=ledger,
        # The algorithm of the month posts through the publisher itself, so
        # the posts go to the fake.
        publisher=cast(DiscordPublisher, publisher),
        reminders=ReminderEngine(guild_settings, ledger, publisher, clock),
        scheduler=Scheduler(db, clock),
    )
    yield services
    # The db fixture closes the database.
    await asyncio.wait_for(services.scheduler.stop(), TEARDOWN_TIMEOUT)
    await asyncio.wait_for(services.http.close(), TEARDOWN_TIMEOUT)


def make_bot(services: KcpcServices) -> KcpcBot:
    bot = KcpcBot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.kcpc = services
    return bot


@pytest.fixture
async def admin_bot(services: KcpcServices) -> AsyncIterator[KcpcBot]:
    """A real bot with the KCPC services and the admin cog, as at startup."""
    bot = make_bot(services)
    await add_admin_cog(bot)
    yield bot
    await bot.close()


async def load_algo(bot: commands.Bot) -> KcpcAlgo:
    """Add the cog as its extension does."""
    await setup(bot)
    cog = bot.get_cog('KcpcAlgo')
    assert isinstance(cog, KcpcAlgo)
    return cog


@pytest.fixture
async def cog(admin_bot: KcpcBot) -> KcpcAlgo:
    return await load_algo(admin_bot)


@pytest.fixture
def bot(admin_bot: KcpcBot, cog: KcpcAlgo) -> KcpcBot:
    """The bot with both cogs."""
    return admin_bot


@pytest.fixture
def repo(db: Database) -> AlgoRepo:
    return AlgoRepo(db)


def make_member(*, manage_guild: bool, user_id: int = ADMIN) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = []
    return member


@pytest.fixture
def guild() -> MagicMock:
    return MagicMock(spec=discord.Guild, id=GUILD)


def make_ctx(guild: MagicMock, author: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.author = author
    ctx.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    ctx.defer = AsyncMock()
    return ctx


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    """The context of a command an admin runs; replies are recorded."""
    return make_ctx(guild, make_member(manage_guild=True))


@pytest.fixture
def member_ctx(guild: MagicMock) -> MagicMock:
    """The context of a command a member runs; replies are recorded."""
    return make_ctx(guild, make_member(manage_guild=False, user_id=MEMBER))


def make_context(
    bot: commands.Bot,
    guild: MagicMock,
    author: MagicMock,
    *,
    slash: bool = False,
    args: str = '',
) -> commands.Context[commands.Bot]:
    """A real context in ``guild``; with ``slash``, of a slash command.

    A prefix command reads its arguments from ``args``. Replies are recorded,
    and so is deferring a slash command.
    """
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(args),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
        interaction.response.defer = AsyncMock()
    context.send = AsyncMock(  # type: ignore[method-assign]
        return_value=MagicMock(spec=discord.Message)
    )
    return context


def command_named(bot: commands.Bot, name: str) -> commands.Command[Any, ..., Any]:
    command = bot.get_command(name)
    assert command is not None, name
    return command


async def run(
    bot: commands.Bot,
    name: str,
    ctx: MagicMock | commands.Context[Any],
    *args: object,
    **kwargs: object,
) -> None:
    """Call the callback of the command ``name`` with parsed arguments."""
    command = command_named(bot, name)
    # mypy can't call the callback's declared type (see the cog), but any
    # command callback fits this.
    callback: Callable[..., Awaitable[None]] = command.callback
    await callback(command.cog, ctx, *args, **kwargs)


async def invoke(bot: commands.Bot, name: str, ctx: commands.Context[Any]) -> None:
    """Run the command as ``Bot.invoke`` does: checks, then its arguments and
    callback, and any error to the command's error handlers.
    """
    command = command_named(bot, name)
    ctx.command = command
    try:
        await command.invoke(ctx)
    except commands.CommandError as error:
        await command.dispatch_error(ctx, error)


def reply(ctx: MagicMock | commands.Context[Any], **kwargs: object) -> discord.Embed:
    """The embed of the one reply, sent with ``kwargs`` besides."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once_with(embed=ANY, **kwargs)
    embed = send.await_args_list[0].kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def fields(embed: discord.Embed) -> list[tuple[str | None, str | None]]:
    return [(field.name, field.value) for field in embed.fields]


def titles(publisher: FakePublisher) -> list[str | None]:
    return [post.message.title for post in publisher.posts]


def keys(publisher: FakePublisher) -> list[str]:
    return [key for post in publisher.posts for key in post.keys]


def key(month: str, revision: int = 0, guild_id: int = GUILD) -> str:
    return f'algo:{guild_id}:{month}:r{revision}'


async def set_up(
    guild_settings: GuildSettingsRepo,
    *,
    guild_id: int = GUILD,
    enabled: bool = True,
    channel_id: int | None = CHANNEL,
) -> None:
    """Turn the algorithm of the month on in the guild."""
    await guild_settings.update(guild_id, ALGO, enabled=enabled, channel_id=channel_id)


def next_runs(services: KcpcServices) -> dict[str, datetime | None]:
    return {job.name: job.next_run for job in services.scheduler.status()}


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 10 s of real time) until ``condition()`` holds."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def posted(repo: AlgoRepo, publisher: FakePublisher, pick: AlgoPick) -> AlgoPick:
    """Store ``pick`` and record its post as sent, as a run would."""
    stored = await repo.create(pick)
    delivery = Delivery(
        post_key(stored),
        stored.guild_id,
        ALGO,
        subject='algo',
        subject_id=stored.month,
        kind='topic',
        occurrence_start=stored.slot,
    )
    result = await publisher.publish([delivery], OutgoingMessage(title='Earlier'))
    assert result.outcome is PublishOutcome.SENT
    return stored


def pick_for(months_ago: int, slug: str) -> AlgoPick:
    """GUILD's pick of ``slug``, ``months_ago`` months before October 2026."""
    year, month = divmod(2026 * 12 + 9 - months_ago, 12)
    slot = first(month + 1, year)
    return AlgoPick(GUILD, f'{year}-{month + 1:02d}', slot, slug, 0, slot)


def test_the_first_topics_are_the_catalogs_first() -> None:
    # What the tests below expect FirstChoice to pick.
    assert (PREFIX_SUMS.slug, TWO_POINTERS.slug) == ('prefix-sums', 'two-pointers')
    assert ALGO_TOPICS[-1].name == 'Sprague-Grundy theorem'
    assert pick_for(1, 'x').month == '2026-09'
    assert pick_for(10, 'x').month == '2025-12'


async def test_loading_adds_the_commands_and_the_job(
    bot: KcpcBot, cog: KcpcAlgo, services: KcpcServices
) -> None:
    jobs = [
        (job.name, job.description, job.persistent)
        for job in services.scheduler.status()
    ]
    assert jobs == [
        ('algo.post', 'on day 1 of every month at 12:00 (Europe/London)', True)
    ]
    # It posts by itself, not through the reminder engine.
    assert services.reminders.features == []

    # /algo for members, kept out of DMs; and ;algo current, which the slash
    # command's fallback doesn't give prefix commands.
    members = bot.tree.get_command('algo')
    assert isinstance(members, app_commands.Group)
    assert members.guild_only
    assert sorted(command.name for command in members.commands) == [
        'current',
        'history',
    ]
    group = command_named(bot, 'algo')
    assert isinstance(group, commands.HybridGroup)
    assert sorted(group.all_commands) == ['current', 'history']
    current = command_named(bot, 'algo current')
    assert isinstance(current, commands.HybridCommand)
    assert current.app_command is None

    # /kcpc algo, for admins, on both paths and nowhere else.
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    admin = kcpc.get_command('algo')
    assert isinstance(admin, app_commands.Group)
    assert sorted(command.name for command in admin.commands) == ADMIN_COMMANDS
    for name in ADMIN_COMMANDS:
        assert command_named(bot, f'kcpc algo {name}').cog is cog
    assert set(bot.all_commands) == {'help', 'kcpc', 'algo'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc', 'algo'}


async def test_the_first_post_waits_for_the_1st(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings)

    services.scheduler.start()

    # A fresh install posts nothing until the next 1st.
    await eventually(
        lambda: next_runs(services) == {ALGO_JOB: first(11)},
        'the job waits for November',
    )
    assert publisher.posts == []

    await clock.advance_to(first(11))

    await eventually(
        lambda: next_runs(services) == {ALGO_JOB: first(12)},
        'the job waits for December',
    )
    assert titles(publisher) == ['Algorithm of the month: Prefix sums']
    assert keys(publisher) == [key('2026-11')]


@pytest.mark.parametrize(
    ('late', 'posts'),
    [(24 * HOUR - SECOND, True), (24 * HOUR + SECOND, False)],
    ids=['within a day', 'later'],
)
async def test_a_1st_missed_while_the_bot_was_down_is_posted_within_a_day(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
    late: timedelta,
    posts: bool,
) -> None:
    await set_up(guild_settings)
    services.scheduler.start()
    await eventually(
        lambda: next_runs(services)[ALGO_JOB] == first(11),
        'the job waits for November',
    )
    await services.scheduler.stop()

    await clock.advance_to(first(11) + late)  # the bot was down at noon
    services.scheduler.start()

    await eventually(
        lambda: next_runs(services)[ALGO_JOB] == first(12),
        'the job waits for December',
    )
    expected = ['Algorithm of the month: Prefix sums'] if posts else []
    assert titles(publisher) == expected
    assert (await repo.get(GUILD, '2026-11') is not None) is posts


async def test_removing_the_cog_undoes_everything_loading_did(
    bot: KcpcBot, services: KcpcServices
) -> None:
    await bot.remove_cog('KcpcAlgo')

    assert services.scheduler.status() == []
    assert bot.get_command('kcpc algo') is None
    kcpc = bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('algo') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}

    # So the extension can be loaded again.
    again = await load_algo(bot)
    assert command_named(bot, 'kcpc algo reroll').cog is again
    assert command_named(bot, 'algo history').cog is again
    assert len(services.scheduler.status()) == 1


async def other_algo(ctx: commands.Context[Any]) -> None:
    """Another /kcpc algo."""


def assert_nothing_left_by_a_failed_load(bot: commands.Bot) -> None:
    assert bot.get_cog('KcpcAlgo') is None
    assert set(bot.all_commands) == {'help', 'kcpc'}
    assert {command.name for command in bot.tree.get_commands()} == {'kcpc'}


async def test_a_load_that_cannot_attach_the_admin_commands_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    kcpc = command_named(admin_bot, 'kcpc')
    assert isinstance(kcpc, commands.HybridGroup)
    clashing: commands.HybridGroup[Any, ..., Any] = commands.hybrid_group(name='algo')(
        other_algo
    )
    kcpc.add_command(clashing)

    with pytest.raises(commands.CommandRegistrationError):
        await load_algo(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert admin_bot.get_command('kcpc algo') is clashing
    assert services.scheduler.status() == []


async def test_a_load_that_cannot_add_its_job_is_undone(
    admin_bot: KcpcBot, services: KcpcServices
) -> None:
    async def other(slot: datetime) -> None:
        pass

    services.scheduler.add(ScheduledJob(ALGO_JOB, Every(HOUR), other, persistent=False))

    with pytest.raises(ValueError, match='already scheduled'):
        await load_algo(admin_bot)

    assert_nothing_left_by_a_failed_load(admin_bot)
    assert [job.name for job in services.scheduler.status()] == [ALGO_JOB]
    assert admin_bot.get_command('kcpc algo') is None
    kcpc = admin_bot.tree.get_command('kcpc')
    assert isinstance(kcpc, app_commands.Group)
    assert kcpc.get_command('algo') is None


async def test_without_the_admin_cog_the_feature_runs_without_its_admin_commands(
    services: KcpcServices, caplog: pytest.LogCaptureFixture
) -> None:
    bot = make_bot(services)
    try:
        with caplog.at_level(logging.INFO, logger=ADMIN_LOGGER):
            await load_algo(bot)

        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == ADMIN_LOGGER
        ] == ['Not adding /kcpc algo: the kcpc.admin extension is not loaded']
        # Not a top-level command that every member would be shown: /algo
        # stays the members' group.
        assert set(bot.all_commands) == {'help', 'algo'}
        group = command_named(bot, 'algo')
        assert isinstance(group, commands.HybridGroup)
        assert sorted(group.all_commands) == ['current', 'history']
        assert {command.name for command in bot.tree.get_commands()} == {'algo'}
        assert len(services.scheduler.status()) == 1

        await bot.remove_cog('KcpcAlgo')

        assert set(bot.all_commands) == {'help'}
        assert services.scheduler.status() == []
    finally:
        await bot.close()


async def test_algo_shows_this_months_topic_and_where_to_read_about_it(
    bot: KcpcBot,
    ctx: MagicMock,
    member_ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await run(bot, 'kcpc algo post-now', ctx)

    await run(bot, 'algo', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Algorithm of the month: Prefix sums'
    assert embed.url == PREFIX_SUMS.gfg_url
    assert embed.description == '\n'.join(
        [
            PREFIX_SUMS.summary,
            '**Level:** beginner',
            f'**Read:** [GeeksforGeeks]({PREFIX_SUMS.gfg_url})',
            f'**Posted:** {when(NOW)}',  # by post-now, after its slot
        ]
    )
    assert embed.footer.text == 'KCPC algorithm of the month'


async def test_algo_says_when_nothing_has_been_posted(
    bot: KcpcBot,
    member_ctx: MagicMock,
    repo: AlgoRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, pick_for(1, 'trie'))  # last month's
    await repo.create(pick_for(0, 'knapsack'))  # whose post never went out

    await run(bot, 'algo current', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Algorithm of the month'
    assert embed.description == 'No algorithm of the month has been posted here yet.'


async def test_algo_shows_a_topic_no_longer_in_the_catalog_by_its_id(
    bot: KcpcBot,
    member_ctx: MagicMock,
    repo: AlgoRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    clock: FakeClock,
) -> None:
    await set_up(guild_settings)
    await clock.advance(HOUR)
    await posted(repo, publisher, pick_for(0, 'old-topic'))

    await run(bot, 'algo', member_ctx)

    embed = reply(member_ctx)
    assert embed.title == 'Algorithm of the month: old-topic'
    assert embed.url is None
    assert embed.description == f'**Posted:** {when(NOW + HOUR)}'


async def test_algo_history_lists_the_months_newest_first_a_year_a_page(
    bot: KcpcBot,
    member_ctx: MagicMock,
    repo: AlgoRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, pick_for(0, 'segment-tree'))
    await posted(repo, publisher, pick_for(1, 'old_topic'))
    for months_ago, found in enumerate(ALGO_TOPICS[:11], start=2):
        await posted(repo, publisher, pick_for(months_ago, found.slug))
    await repo.create(pick_for(13, 'trie'))  # whose post never went out

    await run(bot, 'algo history', member_ctx)

    send = cast(AsyncMock, member_ctx.send)
    send.assert_awaited_once_with(embed=ANY, view=ANY, ephemeral=False)
    assert send.await_args is not None
    view = send.await_args.kwargs['view']
    assert isinstance(view, PageView)
    first_page, second_page = view.pages
    assert first_page.title == 'Algorithms of the month'
    assert first_page.description is not None
    assert second_page.description is not None
    lines = first_page.description.splitlines()
    assert len(lines) == 12
    assert lines[:3] == [
        '`2026-10` [Segment tree](https://www.geeksforgeeks.org/dsa/'
        'segment-tree-data-structure/) · intermediate',
        r'`2026-09` old\_topic',  # taken out of the catalog since
        f'`2026-08` [Prefix sums]({PREFIX_SUMS.gfg_url}) · beginner',
    ]
    assert second_page.description.splitlines() == [
        f'`2025-10` [{ALGO_TOPICS[10].name}]({ALGO_TOPICS[10].gfg_url}) · beginner'
    ]
    assert first_page.footer.text == 'Page 1 of 2'
    assert second_page.footer.text == 'Page 2 of 2'


async def test_algo_history_without_any_month_says_so(
    bot: KcpcBot, member_ctx: MagicMock
) -> None:
    await run(bot, 'algo history', member_ctx)

    embed = reply(member_ctx, ephemeral=False)
    assert embed.title == 'Algorithms of the month'
    assert embed.description == 'No algorithm of the month has been posted here yet.'


@pytest.mark.parametrize(
    ('args', 'title', 'kwargs'),
    [
        ('', 'Algorithm of the month', {}),
        ('current', 'Algorithm of the month', {}),
        ('history', 'Algorithms of the month', {'ephemeral': False}),
    ],
)
async def test_algo_works_as_a_prefix_command_too(
    bot: KcpcBot, guild: MagicMock, args: str, title: str, kwargs: dict[str, object]
) -> None:
    ctx = make_context(
        bot, guild, make_member(manage_guild=False, user_id=MEMBER), args=args
    )

    await invoke(bot, 'algo', ctx)

    assert reply(ctx, **kwargs).title == title


@pytest.mark.parametrize('name', ['algo', 'algo current', 'algo history'])
async def test_member_commands_need_a_server(
    bot: KcpcBot, member_ctx: MagicMock, name: str
) -> None:
    member_ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await run(bot, name, member_ctx)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ['algo', 'algo current', 'algo history'])
async def test_member_commands_are_for_everyone(
    bot: KcpcBot, guild: MagicMock, name: str, slash: bool
) -> None:
    member = make_context(
        bot, guild, make_member(manage_guild=False, user_id=MEMBER), slash=slash
    )

    assert await command_named(bot, name).can_run(member)


async def test_post_now_posts_this_months_topic_once(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(guild_settings)

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        await run(bot, 'kcpc algo post-now', ctx)

    assert titles(publisher) == ['Algorithm of the month: Prefix sums']
    assert keys(publisher) == [key('2026-10')]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == "Posted October's topic, **Prefix sums**."
    assert (
        f'Admin {ADMIN} of guild {GUILD} ran the algorithm of the month of '
        '2026-10-01 11:00:00+00:00 now'
    ) in [r.getMessage() for r in caplog.records if r.name == COG_LOGGER]
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo post-now', ctx)

    assert len(publisher.posts) == 1
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == "October's topic, **Prefix sums**, is already posted."


@pytest.mark.parametrize(
    ('enabled', 'channel_id'), [(False, CHANNEL), (True, None)], ids=['off', 'channel']
)
@pytest.mark.parametrize('name', ['kcpc algo post-now', 'kcpc algo reroll'])
async def test_post_now_and_reroll_say_how_to_set_the_feature_up(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
    name: str,
    enabled: bool,
    channel_id: int | None,
) -> None:
    await set_up(guild_settings, enabled=enabled, channel_id=channel_id)

    await run(bot, name, ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NOT_SET_UP
    assert publisher.posts == []
    assert await repo.history(GUILD) == []


@pytest.mark.parametrize(
    ('failure', 'text'),
    [
        (
            'guild-unavailable',
            "Couldn't post October's topic, **Prefix sums**: Discord hasn't sent "
            "this server's channels yet. Please try again in a minute.",
        ),
        (
            'channel-missing',
            "Couldn't post October's topic, **Prefix sums**: `channel-missing`. "
            'Check the channel and my permissions there, e.g. with '
            '`/kcpc channel algo #channel`.',
        ),
        (
            PublishOutcome.PENDING,
            "Posted October's topic, **Prefix sums**, but Discord didn't "
            "confirm it; I'll check within a few minutes.",
        ),
        (
            PublishOutcome.SKIPPED,
            "Discord refused to post October's topic, **Prefix sums**: `discord-403`.",
        ),
    ],
    ids=['guild unavailable', 'channel missing', 'pending', 'skipped'],
)
async def test_post_now_says_what_kept_the_post_from_going_out(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    failure: str | PublishOutcome,
    text: str,
) -> None:
    await set_up(guild_settings)
    if isinstance(failure, PublishOutcome):
        publisher.fail_next(failure)
    else:
        publisher.undeliverable_next(reason=failure)

    await run(bot, 'kcpc algo post-now', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == text


async def test_post_now_says_when_discord_refused_this_months_topic_earlier(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings)
    publisher.fail_next(PublishOutcome.SKIPPED)
    await run(bot, 'kcpc algo post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo post-now', ctx)

    assert publisher.posts == []
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == (
        "Discord refused to post October's topic, **Prefix sums**, earlier: "
        "`discord-403`. It can't be posted again, but `/kcpc algo reroll` posts "
        'another topic.'
    )


async def test_reroll_posts_another_topic_in_place_of_this_months(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(guild_settings)
    await run(bot, 'kcpc algo post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    with caplog.at_level(logging.INFO, logger=COG_LOGGER):
        await run(bot, 'kcpc algo reroll', ctx)

    assert titles(publisher) == [
        'Algorithm of the month: Prefix sums',
        'Algorithm of the month: Two pointers',
    ]
    assert keys(publisher) == [key('2026-10'), key('2026-10', 1)]
    description = publisher.posts[1].message.description
    assert description is not None
    assert description.splitlines()[0] == (
        "This replaces October's earlier pick, **Prefix sums**."
    )
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == (
        "Posted October's new topic, **Two pointers**, in place of **Prefix sums**."
    )
    pick = await repo.get(GUILD, '2026-10')
    assert pick is not None and (pick.slug, pick.revision) == ('two-pointers', 1)
    assert f'Admin {ADMIN} of guild {GUILD} rerolled the algorithm of the month' in [
        r.getMessage() for r in caplog.records if r.name == COG_LOGGER
    ]


async def test_reroll_without_a_topic_yet_posts_one_as_post_now_does(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings)

    await run(bot, 'kcpc algo reroll', ctx)

    assert keys(publisher) == [key('2026-10')]
    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(SUCCESS_COLOR)
    assert embed.description == "Posted October's topic, **Prefix sums**."


async def test_reroll_that_cannot_post_says_its_topic_stands(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings)
    await run(bot, 'kcpc algo post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()
    publisher.undeliverable_next()

    await run(bot, 'kcpc algo reroll', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == (
        "Couldn't post October's new topic, **Two pointers**, in place of "
        "**Prefix sums**: Discord hasn't sent this server's channels yet. Please "
        "try again in a minute. It is October's topic now: post it with "
        '`/kcpc algo post-now` once that is fixed.'
    )
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo post-now', ctx)

    assert titles(publisher)[-1] == 'Algorithm of the month: Two pointers'
    assert reply(ctx, ephemeral=True).description == (
        "Posted October's topic, **Two pointers**."
    )


async def test_after_a_reroll_that_failed_members_see_the_topic_that_went_out(
    bot: KcpcBot,
    ctx: MagicMock,
    member_ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
) -> None:
    await set_up(guild_settings)
    await run(bot, 'kcpc algo post-now', ctx)
    publisher.undeliverable_next()
    await run(bot, 'kcpc algo reroll', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'algo', member_ctx)
    await run(bot, 'algo history', member_ctx)
    await run(bot, 'kcpc algo preview', ctx)

    send = cast(AsyncMock, member_ctx.send)
    current, history = [call.kwargs['embed'] for call in send.await_args_list]
    assert current.title == 'Algorithm of the month: Prefix sums'
    assert history.description == (
        f'`2026-10` [Prefix sums]({PREFIX_SUMS.gfg_url}) · beginner'
    )
    # Admins are shown the topic still to go out.
    assert fields(reply(ctx, ephemeral=True))[1] == (
        'This month',
        f'[Two pointers]({TWO_POINTERS.gfg_url}) (beginner)\nNot posted yet.',
    )


async def test_reroll_waits_while_the_last_post_is_unconfirmed(
    bot: KcpcBot,
    ctx: MagicMock,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
) -> None:
    await set_up(guild_settings)
    publisher.fail_next(PublishOutcome.PENDING)
    await run(bot, 'kcpc algo post-now', ctx)
    context = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc algo reroll', context)

    embed = reply(context, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == (
        "The last post of October's topic, **Prefix sums**, isn't confirmed yet. "
        'Please try again in a few minutes.'
    )
    assert publisher.posts == []
    pick = await repo.get(GUILD, '2026-10')
    assert pick is not None and pick.revision == 0


async def test_before_noon_on_the_1st_post_now_and_reroll_name_the_month_before(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    clock: FakeClock,
) -> None:
    await set_up(guild_settings)
    # Half an hour before November's topic is due, they act on October's.
    await clock.advance_to(datetime(2026, 11, 1, 11, 30, tzinfo=UTC))

    await run(bot, 'kcpc algo post-now', ctx)

    assert keys(publisher) == [key('2026-10')]
    assert reply(ctx, ephemeral=True).description == (
        "Posted October's topic, **Prefix sums**."
    )
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo reroll', ctx)

    assert keys(publisher) == [key('2026-10'), key('2026-10', 1)]
    assert reply(ctx, ephemeral=True).description == (
        "Posted October's new topic, **Two pointers**, in place of **Prefix sums**."
    )
    description = publisher.posts[1].message.description
    assert description is not None
    assert description.splitlines()[0] == (
        "This replaces October's earlier pick, **Prefix sums**."
    )


async def test_preview_shows_the_next_post_this_month_and_the_topics_left(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
) -> None:
    total = len(ALGO_TOPICS)
    await set_up(guild_settings)

    await run(bot, 'kcpc algo preview', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.title == 'Algorithm of the month preview'
    assert fields(embed) == [
        ('Next post', f'{when(first(11))} in <#{CHANNEL}>'),
        ('This month', 'Nothing has been posted yet.'),
        (
            'Left in this cycle',
            f'{total} of {total} topics; the next pick is one of them.',
        ),
    ]
    await run(bot, 'kcpc algo post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo preview', ctx)

    assert fields(reply(ctx, ephemeral=True))[1:] == [
        ('This month', f'[Prefix sums]({PREFIX_SUMS.gfg_url}) (beginner)\nPosted.'),
        (
            'Left in this cycle',
            f'{total - 1} of {total} topics; the next pick is one of them.',
        ),
    ]


@pytest.mark.parametrize(
    ('failure', 'state'),
    [
        (None, 'Posted.'),
        (PublishOutcome.PENDING, "Posted, but Discord hasn't confirmed it yet."),
        ('channel-missing', 'Not posted yet.'),
        (PublishOutcome.SKIPPED, 'Refused by Discord: `discord-403`.'),
    ],
    ids=['posted', 'unconfirmed', 'undeliverable', 'refused'],
)
async def test_preview_shows_this_months_topic_and_whether_it_went_out(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    failure: str | PublishOutcome | None,
    state: str,
) -> None:
    total = len(ALGO_TOPICS)
    await set_up(guild_settings)
    if isinstance(failure, PublishOutcome):
        publisher.fail_next(failure)
    elif failure is not None:
        publisher.undeliverable_next(reason=failure)
    await run(bot, 'kcpc algo post-now', ctx)
    cast(AsyncMock, ctx.send).reset_mock()

    await run(bot, 'kcpc algo preview', ctx)

    # A retry posts the same topic, so the next pick is one of the others.
    assert fields(reply(ctx, ephemeral=True))[1:] == [
        ('This month', f'[Prefix sums]({PREFIX_SUMS.gfg_url}) (beginner)\n{state}'),
        (
            'Left in this cycle',
            f'{total - 1} of {total} topics; the next pick is one of them.',
        ),
    ]


async def test_preview_of_a_server_not_set_up_says_what_to_do(
    bot: KcpcBot, ctx: MagicMock, guild_settings: GuildSettingsRepo
) -> None:
    await set_up(guild_settings, enabled=False, channel_id=None)

    await run(bot, 'kcpc algo preview', ctx)

    assert fields(reply(ctx, ephemeral=True))[0] == (
        'Next post',
        f'{when(first(11))}, once you turn it on with `/kcpc enable algo` and set '
        'its channel with `/kcpc channel algo #channel`. Until then nothing is '
        'posted.',
    )


async def test_preview_shows_a_topic_no_longer_in_the_catalog_by_its_id(
    bot: KcpcBot,
    ctx: MagicMock,
    repo: AlgoRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await set_up(guild_settings)
    await posted(repo, publisher, pick_for(0, 'old_topic'))

    await run(bot, 'kcpc algo preview', ctx)

    assert fields(reply(ctx, ephemeral=True))[1] == (
        'This month',
        'old\\_topic\nPosted.',
    )


async def test_the_job_skips_a_server_the_bot_has_left(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
) -> None:
    left = 1_100_000_000_000_000_009
    await set_up(guild_settings)
    await set_up(guild_settings, guild_id=left)
    await clock.advance_to(first(11))

    await services.scheduler.run_slot(ALGO_JOB)  # nothing to retry

    assert keys(publisher) == [key('2026-11')]
    assert await repo.history(left) == []


async def test_the_job_retries_a_post_it_couldnt_deliver_with_the_same_topic(
    bot: KcpcBot,
    services: KcpcServices,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    repo: AlgoRepo,
) -> None:
    await set_up(guild_settings)
    await clock.advance_to(first(11))
    publisher.undeliverable_next()

    # The scheduler retries the slot within its grace.
    with pytest.raises(RuntimeError, match='not posted in guilds'):
        await services.scheduler.run_slot(ALGO_JOB)

    picked = await repo.get(GUILD, '2026-11')
    assert picked is not None and publisher.posts == []

    await services.scheduler.run_slot(ALGO_JOB)

    assert keys(publisher) == [key('2026-11')]
    assert await repo.get(GUILD, '2026-11') == picked


@pytest.mark.parametrize('name', ADMIN_COMMANDS)
async def test_admin_commands_defer_before_anything_else(
    bot: KcpcBot,
    ctx: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    name: str,
) -> None:
    await set_up(guild_settings)
    when_deferred: list[tuple[int, int]] = []
    send = cast(AsyncMock, ctx.send)
    cast(AsyncMock, ctx.defer).side_effect = lambda **_: when_deferred.append(
        (len(publisher.posts), send.await_count)
    )

    await run(bot, f'kcpc algo {name}', ctx)

    cast(AsyncMock, ctx.defer).assert_awaited_once_with(ephemeral=True)
    assert when_deferred == [(0, 0)]
    reply(ctx, ephemeral=True)


async def test_a_slash_admin_command_defers_its_interaction(
    bot: KcpcBot, guild: MagicMock
) -> None:
    ctx = make_context(bot, guild, make_member(manage_guild=True), slash=True)

    await run(bot, 'kcpc algo preview', ctx)

    assert ctx.interaction is not None
    defer = cast(AsyncMock, ctx.interaction.response.defer)
    defer.assert_awaited_once_with(ephemeral=True)
    reply(ctx, ephemeral=True)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('name', ADMIN_COMMANDS)
async def test_admin_commands_are_for_admins_only(
    monkeypatch: pytest.MonkeyPatch,
    bot: KcpcBot,
    guild: MagicMock,
    name: str,
    slash: bool,
) -> None:
    # Their cog is this one, so the admin cog's check doesn't cover them.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    command = command_named(bot, f'kcpc algo {name}')
    admin = make_context(bot, guild, make_member(manage_guild=True), slash=slash)
    member = make_context(bot, guild, make_member(manage_guild=False), slash=slash)

    assert await command.can_run(admin)
    with pytest.raises(NotKcpcAdmin):
        await command.can_run(member)


async def test_the_admin_group_on_its_own_is_for_admins_only(
    monkeypatch: pytest.MonkeyPatch, bot: KcpcBot, guild: MagicMock
) -> None:
    # ;kcpc algo; Discord can't run a slash group on its own.
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    group = command_named(bot, 'kcpc algo')
    member = make_context(bot, guild, make_member(manage_guild=False))

    with pytest.raises(NotKcpcAdmin):
        await group.can_run(member)


async def test_a_user_error_gets_a_private_reply(
    bot: KcpcBot,
    guild: MagicMock,
    repo: AlgoRepo,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    # This month's topic is the only one the server has yet to have.
    await set_up(guild_settings)
    *had, last = ALGO_TOPICS
    for months_ago, found in enumerate(had, start=1):
        await posted(repo, publisher, pick_for(months_ago, found.slug))
    this_month = await posted(repo, publisher, pick_for(0, last.slug))
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc algo reroll', ctx)

    embed = reply(ctx, ephemeral=True)
    assert embed.colour == discord.Colour(ALERT_COLOR)
    assert embed.description == NO_OTHER_TOPIC
    assert await repo.get(GUILD, '2026-10') == this_month
    assert len(publisher.posts) == len(ALGO_TOPICS)


async def test_a_bug_gets_an_apology_and_is_logged(
    bot: KcpcBot,
    guild: MagicMock,
    guild_settings: GuildSettingsRepo,
    publisher: FakePublisher,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(guild_settings)
    publisher.fail_next(RuntimeError('boom'))
    ctx = make_context(bot, guild, make_member(manage_guild=True))

    await invoke(bot, 'kcpc algo post-now', ctx)

    assert reply(ctx, ephemeral=True).description == UNEXPECTED_ERROR_MESSAGE
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.exc_info is not None and str(record.exc_info[1]) == 'boom'
