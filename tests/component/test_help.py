"""Tests for /help (tle.access.help): the commands each member can use, and
how to use each one.

The bot is a real one, with the access service's check and tree, cogs of
commands named and shaped like TLE's and KCPC's, the Help cog, and the slash
pass that hides staff commands, as TLEBot sets them up. The tests give those
commands rules by replacing the rule table. Contexts are real TLEContexts, so
answers go through discord.py's own Context.send: a slash command's through
the interaction's response, a prefix command's as a post in the channel. The
interaction and the post are mocked, so the tests see who would see each
answer. Discord itself (the server, its members and channels) is mocked too.

Most tests call ``send_help`` with a context of /help, as the help command
does. Those under "Through the commands" run commands as discord.py runs them:
a prefix command through ``Command.invoke``, and a slash command through the
bot's command tree, from the interaction's data.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.access import help as help_module, service as service_module, table
from tle.access.context import TLEContext
from tle.access.help import (
    ALWAYS_PRIVATE_ON_SLASH_TEXT,
    MAX_SUGGESTIONS,
    MORE_IN_BOT_CHANNELS,
    NO_COMMANDS_TEXT,
    NO_COMMAND_TEXT,
    PAGE_TIMEOUT,
    PREFIX_FOOTER,
    SLASH_FOOTER,
    UNAVAILABLE_TEXT,
    USE_SLASH_HELP_TEXT,
    Help,
    describe_cooldown,
    find_command,
    prefix_usage,
    send_help,
    split_help,
)
from tle.access.rules import Decision, Effective, Limit, Outcome, Rule, Where, Who
from tle.access.service import (
    OFF_TEXT,
    REPAIR_TEXT,
    SLASH_HINT,
    UNREADABLE_TEXT,
    AccessService,
    AccessTree,
    cached_decision,
)
from tle.access.settings import GuildAccess
from tle.access.slash import apply_visibility
from tle.util.discord_common import AccessDenied, PrivateAnswerExpired, embed_alert
from tle.util.paginator import PaginatorView

HELP_LOGGER = 'tle.access.help'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
THREAD_ID = 1_200_000_000_000_000_030
MEMBER_ID = 1_400_000_000_000_000_001
OWNER_ID = 1_400_000_000_000_000_009
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_005
OTHER_ROLE_ID = 1_300_000_000_000_000_099

EVERYONE_BOT = Rule(Who.EVERYONE, Where.BOT)
RULES = {
    'gitgud': EVERYONE_BOT,
    'gimme': EVERYONE_BOT,  # a prefix command alone
    '_nogud': Rule(Who.MODERATOR, Where.BOT),
    'whisper': Rule(Who.EVERYONE, Where.BOT, private=True),
    'clist': EVERYONE_BOT,  # a group's own command: /clist show
    'clist future': EVERYONE_BOT,
    'clist purge': Rule(Who.MODERATOR, Where.BOT),
    'ranklist': Rule(Who.EVERYONE, Where.BOT_ONLY),
    'contests': EVERYONE_BOT,  # /contests upcoming, and ;contests upcoming
    'contests live': EVERYONE_BOT,
    'handle show': EVERYONE_BOT,  # and its twin, the handle group's own command
    'handle set': Rule(Who.MODERATOR, Where.BOT),
    'handle refer': Rule(Who.TRUSTED, Where.BOT_ONLY),
    'roleupdate': Rule(Who.MODERATOR, Where.STAFF),
    'roleupdate now': Rule(Who.MODERATOR, Where.STAFF),
    'duel': EVERYONE_BOT,
    'duel challenge': Rule(Who.EVERYONE, Where.BOT_ONLY),
    'duel register': Rule(Who.MODERATOR, Where.BOT),
    'meta': EVERYONE_BOT,
    'meta ping': EVERYONE_BOT,
    'meta git': Rule(Who.DEVELOPER, Where.STAFF),
    'meta kill': Rule(Who.OWNER, Where.ANYWHERE),
    'kcpc': Rule(Who.ADMIN, Where.STAFF),
    'kcpc status': Rule(Who.DEVELOPER, Where.STAFF_ONLY),
    'help': EVERYONE_BOT,
    'access': Rule(Who.ADMIN, Where.STAFF),
    'access limit': Rule(Who.ADMIN, Where.STAFF),
    'access staff-channel': Rule(Who.ADMIN, Where.STAFF),
}
TWINS = {'contests upcoming': 'contests', 'handle': 'handle show'}
# The table as the bot has it, before any test replaces it.
REAL_RULES = table.RULES

GITGUD_BRIEF = 'Get a problem to solve for gitgud points'
DELTA_OPTION = 'How much harder than your rating; 0 if left out'
MEMBER_OPTION = 'The member whose problem to skip'
BOT_CHANNELS = 'Bot channels; elsewhere the slash command answers only you.'
# The field of a command's help that says what each form does here.
HERE = 'In this channel'

Context = commands.Context[Any]


class Codeforces(commands.Cog):
    @commands.hybrid_command(brief=GITGUD_BRIEF)
    @app_commands.describe(delta=DELTA_OPTION)
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def gitgud(self, ctx: Context, delta: int = 0) -> None:
        """Get a problem to solve, for gitgud points.

        The harder it is, the more
        points it gives.

        Examples:
            /gitgud
            ;gitgud 200
        """

    @commands.command(brief='Get a problem with the tags you choose')
    async def gimme(self, ctx: Context, *tags: str) -> None:
        """Get a problem with the tags you choose."""

    @commands.hybrid_command(name='_nogud', brief="Skip a member's problem")
    @app_commands.describe(member=MEMBER_OPTION)
    async def force_nogud(self, ctx: Context, member: discord.Member) -> None:
        """Skip a member's problem."""

    @commands.hybrid_command(brief='Tell you a secret')
    async def whisper(self, ctx: Context) -> None:
        """Tell you a secret."""


class Contests(commands.Cog):
    @commands.hybrid_group(brief='Show the contest list commands', fallback='show')
    async def clist(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @clist.command(brief='List future contests')
    async def future(self, ctx: Context) -> None:
        """List the contests that haven't started."""

    @clist.command(brief='Forget the cached contests')
    async def purge(self, ctx: Context) -> None:
        """Forget the cached contests."""

    @commands.command(brief="Show a contest's ranklist")
    @commands.cooldown(1, 30, commands.BucketType.guild)
    async def ranklist(self, ctx: Context, contest_id: int) -> None:
        """Show a contest's ranklist."""


class KcpcContests(commands.Cog):
    async def cog_load(self) -> None:
        # Nested here, as KCPC nests its twins: declared in the group,
        # discord.py would take the group's fallback of the same name out of
        # the slash group.
        self.contests.add_command(self.contests_upcoming)

    @commands.hybrid_group(fallback='upcoming', brief='Show the upcoming contests')
    async def contests(self, ctx: Context) -> None:
        """Show the upcoming contests."""

    @commands.hybrid_command(
        name='upcoming', with_app_command=False, brief='Show the upcoming contests'
    )
    async def contests_upcoming(self, ctx: Context) -> None:
        """Show the upcoming contests."""

    @contests.command(brief='Show the contests running now')
    async def live(self, ctx: Context) -> None:
        """Show the contests running now."""


class Handles(commands.Cog):
    @commands.hybrid_group(brief="Show a member's handles")
    async def handle(self, ctx: Context, member: discord.Member | None = None) -> None:
        """Show a member's handles: yours, if you name no one."""

    @handle.command(name='show', brief="Show a member's handles")
    async def handle_show(self, ctx: Context) -> None:
        """Show a member's handles."""

    @handle.command(name='set', brief="Set a member's handle")
    async def handle_set(self, ctx: Context) -> None:
        """Set a member's handle."""

    @handle.command(name='refer', brief='Make a member trusted')
    async def handle_refer(self, ctx: Context) -> None:
        """Make a member trusted."""

    @commands.hybrid_group(brief='Show the rank role commands', fallback='show')
    async def roleupdate(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @roleupdate.command(name='now', brief='Update the rank roles now')
    async def roleupdate_now(self, ctx: Context) -> None:
        """Update the rank roles now."""


class Dueling(commands.Cog):
    @commands.hybrid_group(brief='Show the duel commands', fallback='show')
    async def duel(self, ctx: Context) -> None:
        # By name, as some of TLE's groups ask.
        await ctx.send_help('duel')

    @duel.command(brief='Challenge a member to a duel')
    async def challenge(self, ctx: Context) -> None:
        """Challenge a member to a duel."""

    @duel.command(brief='Register a member as a duelist')
    async def register(self, ctx: Context) -> None:
        """Register a member as a duelist."""


class Meta(commands.Cog):
    @commands.hybrid_group(brief='Show the bot commands', fallback='show')
    async def meta(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @meta.command(brief='Check that the bot answers')
    async def ping(self, ctx: Context) -> None:
        """Check that the bot answers."""

    @meta.command(brief="Show the bot's version")
    async def git(self, ctx: Context) -> None:
        """Show the bot's version."""

    @meta.command(brief='Stop the bot')
    async def kill(self, ctx: Context) -> None:
        """Stop the bot."""


class KcpcAdmin(commands.Cog):
    @commands.hybrid_group(brief="Show this server's KCPC settings", fallback='show')
    @app_commands.default_permissions(manage_guild=True)
    async def kcpc(self, ctx: Context) -> None:
        await ctx.send_help(ctx.command)

    @kcpc.command(brief='Show KCPC health')
    async def status(self, ctx: Context) -> None:
        """Show KCPC health."""


class LimitFlags(commands.FlagConverter):
    """Flags shaped like those of /access limit."""

    command: str = commands.flag(positional=True, description='The command to limit')
    who: Literal['trusted', 'moderator'] | None = commands.flag(
        default=None, description='Who else must be allowed'
    )
    off: bool | None = commands.flag(default=None, description='Switch it off')


class Access(commands.Cog):
    @commands.hybrid_group(brief="Show this server's access settings", fallback='show')
    @app_commands.default_permissions(manage_guild=True)
    async def access(self, ctx: Context) -> None:
        """Show this server's access settings."""

    @access.command(name='staff-channel', brief='Set the staff channel')
    async def staff_channel(self, ctx: Context) -> None:
        """Set the staff channel."""

    @access.command(brief='Limit a command in this server')
    async def limit(self, ctx: Context, *, flags: LimitFlags) -> None:
        """Limit a command in this server.

        Examples:
            /access limit command:gitgud off:True
            ;access limit duel challenge who: trusted
        """


COGS: tuple[type[commands.Cog], ...] = (
    Codeforces,
    Contests,
    KcpcContests,
    Handles,
    Dueling,
    Meta,
    KcpcAdmin,
    Access,
)


class KcpcAccounts(commands.Cog):
    """A group shaped like KCPC's /link, which tests add when they need it."""

    @commands.hybrid_group(brief='Link your accounts')
    async def link(self, ctx: Context) -> None:
        """Link your accounts."""

    @link.command(name='codeforces', brief='Link your Codeforces account')
    async def link_codeforces(self, ctx: Context) -> None:
        """Link your Codeforces account."""


class AccessBot(commands.Bot):
    """A bot that carries an access service and makes TLEContexts, as TLEBot
    does.
    """

    access: AccessService

    async def get_context(self, origin: Any, /, *, cls: Any = None) -> Any:
        return await super().get_context(origin, cls=cls or TLEContext)


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles: admin, moderator and trusted by name, developer by id."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', DEVELOPER_ROLE_ID)


@pytest.fixture
def rule_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, Rule]:
    """The rules of the test cogs' commands, in place of TLE's table."""
    rules = dict(RULES)
    monkeypatch.setattr(table, 'RULES', MappingProxyType(rules))
    monkeypatch.setattr(table, 'TWINS', MappingProxyType(TWINS))
    return rules


async def make_bot(
    *, access: bool = True, owner_id: int | None = OWNER_ID
) -> AccessBot:
    """A bot set up as TLEBot sets itself up: the access service's check, the
    cogs and Help, and then the slash pass. Its owner is ``owner_id``; with
    None, the owners are still unknown.
    """
    bot = AccessBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
        owner_id=owner_id,
    )
    if access:
        bot.access = AccessService(bot)
        bot.add_check(bot.access.check)
    for cog in COGS:
        await bot.add_cog(cog())
    await bot.add_cog(Help(bot))
    apply_visibility(bot)
    return bot


@pytest.fixture
async def bot(rule_table: dict[str, Rule]) -> AsyncIterator[AccessBot]:
    """The bot, in a server with a bot channel and a staff channel."""
    bot = await make_bot()
    await configure(bot)
    yield bot
    await bot.close()


async def configure(
    bot: AccessBot,
    *,
    bot_channels: tuple[int, ...] = (BOT_CHANNEL_ID,),
    staff_channel: int | None = STAFF_CHANNEL_ID,
    limits: dict[str, Limit] | None = None,
) -> None:
    """Give the server these access settings."""
    settings = GuildAccess(frozenset(bot_channels), staff_channel, limits or {})
    await bot.access.change(GUILD_ID, lambda _: settings)


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in the channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


@pytest.fixture
def guild() -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.channels_by_id = {}
    guild.get_channel.side_effect = guild.channels_by_id.get
    for channel_id in (BOT_CHANNEL_ID, STAFF_CHANNEL_ID, GENERAL_ID):
        channel = MagicMock(
            spec=discord.TextChannel,
            id=channel_id,
            guild=guild,
            mention=f'<#{channel_id}>',
        )
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        guild.channels_by_id[channel_id] = channel
    return guild


def place(guild: MagicMock, channel_id: int) -> MagicMock:
    found: MagicMock = guild.channels_by_id[channel_id]
    return found


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
    permissions: discord.Permissions | None = None,
    member_id: int = MEMBER_ID,
) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild)
    member.guild_permissions = permissions or discord.Permissions.none()
    member.roles = [make_role(role) for role in roles]
    return member


def make_admin(guild: MagicMock) -> MagicMock:
    """An admin by Manage Server, who also sees the slash commands it hides."""
    return make_member(guild, permissions=discord.Permissions(manage_guild=True))


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers it.
    """

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)
        self.autocomplete = AsyncMock(side_effect=self._answer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, *args: Any, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))


def make_context(
    bot: commands.Bot,
    author: Any,
    channel: Any,
    *,
    slash: bool = True,
    name: str = 'help',
    arguments: str = '',
    expired: bool = False,
) -> TLEContext:
    """A context of command ``name``, used by ``author`` in ``channel``; with
    ``slash``, a slash command's, whose interaction may have ``expired``.
    """
    command = bot.get_command(name)
    assert command is not None, name
    message = MagicMock(
        spec=discord.Message, guild=author.guild, author=author, channel=channel
    )
    message.content = f';{name} {arguments}'
    message.jump_url = 'https://discord.com/channels/1/2/3'
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = expired
        interaction.response = Response()
        interaction.followup.send = AsyncMock(
            return_value=MagicMock(spec=discord.WebhookMessage)
        )
    ctx = TLEContext(
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
    return ctx


def help_data(command: str | None = None) -> dict[str, Any]:
    """The data of /help, with ``command`` typed in its option."""
    options = []
    if command is not None:
        options.append({'type': 3, 'name': 'command', 'value': command})
    return {'type': 1, 'name': 'help', 'options': options}


def fallback_data(group: str) -> dict[str, Any]:
    """The data of a group's /<group> show, which runs the group's own command."""
    return {
        'type': 1,
        'name': group,
        'options': [{'type': 1, 'name': 'show', 'options': []}],
    }


async def use_slash(
    bot: commands.Bot, user: Any, channel: Any, data: dict[str, Any]
) -> TLEContext:
    """Run the slash command that ``data`` names, as ``user`` in ``channel``,
    through the bot's command tree; the context discord.py made for it.
    """
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = discord.InteractionType.application_command
    interaction.guild_id = GUILD_ID
    interaction.user = user
    interaction.channel = channel
    interaction.data = data
    interaction.command_failed = False
    # What a real interaction works out from its data: the command, and the
    # options given.
    command, options = bot.tree._get_app_command_options(data)
    interaction.command = command
    interaction.namespace = app_commands.Namespace(interaction, {}, options)
    # discord.py makes the context's message up from the interaction.
    interaction.message = MagicMock(
        spec=discord.Message, guild=user.guild, author=user, channel=channel
    )
    interaction.is_expired.return_value = False
    interaction.response = Response()
    interaction.followup.send = AsyncMock(
        return_value=MagicMock(spec=discord.WebhookMessage)
    )

    await bot.tree._call(interaction)

    ctx = interaction._baton
    assert isinstance(ctx, TLEContext)
    return ctx


@dataclass
class Answer:
    """What the help sent: its pages, and whether only the member sees them."""

    pages: list[discord.Embed]
    private: bool
    view: Any


def answer(ctx: TLEContext, posted: AsyncMock) -> Answer:
    """The one message ``ctx``'s command sent: privately in answer to the
    interaction, or as a post in the channel.
    """
    if ctx.interaction is None:
        posted.assert_awaited_once()
        assert posted.await_args is not None
        options = posted.await_args.kwargs
        private = False
    else:
        posted.assert_not_awaited()
        send = cast(Response, ctx.interaction.response).send_message
        send.assert_awaited_once()
        assert send.await_args is not None
        options = send.await_args.kwargs
        private = options['ephemeral']
    view = options.get('view')
    if isinstance(view, PaginatorView):
        pages = [embed for _, embed in view.pages]
        assert options['embed'] is pages[0]
    else:
        pages = [options['embed']]
    return Answer(pages, private, view)


def alert(ctx: TLEContext, posted: AsyncMock) -> str:
    """The text of the one alert ``ctx``'s command sent."""
    sent = answer(ctx, posted)
    (embed,) = sent.pages
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    assert embed.description is not None
    return embed.description


def listed(sent: Answer) -> list[str]:
    """The commands the overview lists, as it shows them, in order."""
    return [
        line.split('`')[1]
        for page in sent.pages
        for line in (page.description or '').splitlines()
        if line.startswith('`')
    ]


def titles(sent: Answer) -> list[str | None]:
    return [page.title for page in sent.pages]


def fields(page: discord.Embed) -> dict[str, str | None]:
    return {field.name or '': field.value for field in page.fields}


async def overview(
    bot: commands.Bot,
    posted: AsyncMock,
    author: Any,
    channel: Any,
    *,
    slash: bool = True,
) -> Answer:
    """The overview that ``author`` gets in ``channel``."""
    ctx = make_context(bot, author, channel, slash=slash)
    await send_help(ctx)
    return answer(ctx, posted)


async def detail(
    bot: commands.Bot,
    posted: AsyncMock,
    author: Any,
    channel: Any,
    name: str,
    *,
    slash: bool = True,
) -> discord.Embed:
    """The one page of the help of command ``name`` that ``author`` gets."""
    ctx = make_context(bot, author, channel, slash=slash)
    await send_help(ctx, name)
    (page,) = answer(ctx, posted).pages
    return page


async def refusal(
    bot: commands.Bot,
    posted: AsyncMock,
    author: Any,
    channel: Any,
    name: str,
    *,
    slash: bool = True,
) -> str:
    """The reply ``author`` gets for help with command ``name``, which isn't
    its help.
    """
    ctx = make_context(bot, author, channel, slash=slash)
    await send_help(ctx, name)
    return alert(ctx, posted)


both_paths = pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])


# The overview


async def test_a_member_in_a_bot_channel_gets_every_command_for_members(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    sent = await overview(bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID))

    assert sent.private
    # One page per category, in the categories' order; twins are left out.
    assert titles(sent) == [
        'Practice',
        'Contests',
        'Handles and accounts',
        'Duels',
        'Bot',
    ]
    assert listed(sent) == [
        ';gimme',
        '/gitgud',
        '/whisper',
        '/clist show',
        '/clist future',
        '/contests upcoming',
        '/contests live',
        ';ranklist',
        '/handle show',
        '/duel show',
        '/duel challenge',
        '/help',
        '/meta show',
        '/meta ping',
    ]


async def test_each_page_names_its_category_and_each_command_says_what_it_does(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    sent = await overview(bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID))

    practice = sent.pages[0]
    assert practice.title == 'Practice'
    assert practice.description == (
        'Problems to solve, gitgud challenges and the weekly problem.\n\n'
        '`;gimme`: Get a problem with the tags you choose\n'
        f'`/gitgud`: {GITGUD_BRIEF}\n'
        '`/whisper`: Tell you a secret'
    )
    assert [page.footer.text for page in sent.pages] == [
        f'{SLASH_FOOTER} Page {number} / 5' for number in range(1, 6)
    ]


async def test_only_the_member_who_asked_can_turn_the_pages(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    sent = await overview(bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID))

    assert isinstance(sent.view, PaginatorView)
    assert sent.view.owner_id == MEMBER_ID
    assert sent.view.timeout == PAGE_TIMEOUT


async def test_elsewhere_only_what_works_there_is_listed(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # Outside the bot channels, prefix commands and bot-only commands don't
    # work; the slash commands answer privately.
    sent = await overview(bot, posted, make_member(guild), place(guild, GENERAL_ID))

    assert listed(sent) == [
        '/gitgud',
        '/whisper',
        '/clist show',
        '/clist future',
        '/contests upcoming',
        '/contests live',
        '/handle show',
        '/duel show',
        '/help',
        '/meta show',
        '/meta ping',
    ]
    footer = sent.pages[0].footer.text
    assert footer is not None
    assert footer.startswith(f'{SLASH_FOOTER} {MORE_IN_BOT_CHANNELS} Page 1 /')


async def test_a_thread_lists_what_its_channel_does(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    thread = MagicMock(
        spec=discord.Thread, id=THREAD_ID, parent_id=BOT_CHANNEL_ID, guild=guild
    )
    member = make_member(guild)

    in_thread = await overview(bot, posted, member, thread)
    posted.reset_mock()
    in_channel = await overview(bot, posted, member, place(guild, BOT_CHANNEL_ID))

    assert listed(in_thread) == listed(in_channel)
    assert MORE_IN_BOT_CHANNELS not in (in_thread.pages[0].footer.text or '')


async def test_without_bot_channels_the_overview_does_not_send_members_to_them(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, bot_channels=())

    sent = await overview(bot, posted, make_member(guild), place(guild, GENERAL_ID))

    assert '/gitgud' in listed(sent)
    assert sent.pages[0].footer.text == f'{SLASH_FOOTER} Page 1 / 5'


async def test_a_moderator_also_gets_the_moderators_commands(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # By role alone: the slash commands that need Manage Messages to be seen
    # are listed as prefix commands, as are those the slash pass removed.
    moderator = make_member(guild, 'Moderator')

    sent = await overview(bot, posted, moderator, place(guild, BOT_CHANNEL_ID))

    assert listed(sent) == [
        ';gimme',
        '/gitgud',
        ';_nogud',
        '/whisper',
        '/clist show',
        '/clist future',
        ';clist purge',
        '/contests upcoming',
        '/contests live',
        ';ranklist',
        '/handle refer',
        ';handle set',
        '/handle show',
        '/duel show',
        '/duel challenge',
        ';duel register',
        '/help',
        '/meta show',
        '/meta ping',
    ]


async def test_a_moderator_who_sees_hidden_slash_commands_gets_them(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    moderator = make_member(
        guild, 'Moderator', permissions=discord.Permissions(manage_messages=True)
    )

    sent = await overview(bot, posted, moderator, place(guild, BOT_CHANNEL_ID))

    forms = listed(sent)
    assert '/_nogud' in forms and ';_nogud' not in forms
    # The staff channel's commands answer privately elsewhere.
    assert '/roleupdate show' in forms and '/roleupdate now' in forms
    # Removed from /clist, whatever the member's permissions.
    assert ';clist purge' in forms


async def test_an_admin_also_gets_server_setup(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    admin = make_admin(guild)

    in_bot_channel = await overview(bot, posted, admin, place(guild, BOT_CHANNEL_ID))
    posted.reset_mock()
    in_staff_channel = await overview(
        bot, posted, admin, place(guild, STAFF_CHANNEL_ID)
    )

    def setup_page(sent: Answer) -> list[str]:
        (page,) = [page for page in sent.pages if page.title == 'Server setup']
        return [
            line.split('`')[1]
            for line in (page.description or '').splitlines()
            if line.startswith('`')
        ]

    # /kcpc status works in the staff channel alone; ;meta git there too.
    assert setup_page(in_bot_channel) == [
        '/access show',
        '/access limit',
        '/access staff-channel',
        '/kcpc show',
    ]
    assert setup_page(in_staff_channel) == [
        '/access show',
        '/access limit',
        '/access staff-channel',
        '/kcpc show',
        '/kcpc status',
    ]
    assert ';meta git' in listed(in_staff_channel)
    assert ';meta git' not in listed(in_bot_channel)


async def test_a_developer_gets_their_commands_in_the_staff_channel(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    developer = make_member(guild, DEVELOPER_ROLE_ID)

    sent = await overview(bot, posted, developer, place(guild, STAFF_CHANNEL_ID))

    forms = listed(sent)
    # /kcpc is hidden from members without Manage Server.
    assert ';kcpc status' in forms and ';meta git' in forms
    assert ';kcpc' not in forms and '/kcpc show' not in forms


async def test_the_bot_owner_gets_the_owners_commands_on_a_page_of_their_own(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    owner = make_member(guild, member_id=OWNER_ID)

    sent = await overview(bot, posted, owner, place(guild, GENERAL_ID))

    assert titles(sent)[-1] == 'Bot owner'
    assert sent.pages[-1].description == (
        'Commands for the bot owner alone.\n\n`;meta kill`: Stop the bot'
    )


async def test_others_never_get_staff_or_owner_commands(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    sent = await overview(
        bot, posted, make_member(guild, 'Trusted'), place(guild, STAFF_CHANNEL_ID)
    )

    forms = listed(sent)
    assert '/handle refer' in forms
    for staff in ('_nogud', 'clist purge', 'roleupdate', 'meta kill', 'kcpc', 'access'):
        assert not [form for form in forms if form.lstrip('/;').startswith(staff)]


async def test_a_server_limit_on_who_hides_the_command_from_others(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(who=Who.TRUSTED)})
    channel = place(guild, BOT_CHANNEL_ID)

    member = await overview(bot, posted, make_member(guild), channel)
    posted.reset_mock()
    trusted = await overview(bot, posted, make_member(guild, 'Trusted'), channel)

    assert '/gitgud' not in listed(member)
    assert '/gitgud' in listed(trusted)


async def test_a_command_switched_off_is_not_listed(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # clist * switches off the group's own command and its subcommands.
    await configure(bot, limits={'clist *': Limit(off=True)})

    sent = await overview(bot, posted, make_admin(guild), place(guild, BOT_CHANNEL_ID))

    assert not [form for form in listed(sent) if 'clist' in form]


async def test_a_server_limit_on_where_counts_here(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(where=Where.BOT_ONLY)})
    member = make_member(guild)

    elsewhere = await overview(bot, posted, member, place(guild, GENERAL_ID))
    posted.reset_mock()
    in_bot_channel = await overview(bot, posted, member, place(guild, BOT_CHANNEL_ID))

    assert '/gitgud' not in listed(elsewhere)
    assert '/gitgud' in listed(in_bot_channel)


async def test_with_broken_settings_the_overview_says_so(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    user_db = MagicMock()
    user_db.get_all_access_settings = AsyncMock(return_value=[(GUILD_ID, 'nonsense')])
    bot.access.use_user_db(user_db)
    await bot.access.load()

    sent = await overview(bot, posted, make_member(guild), place(guild, GENERAL_ID))

    # Only /help and /access, and the bot owner's commands, still work.
    assert listed(sent) == ['/help']
    assert sent.pages[0].description == (
        f'{REPAIR_TEXT}\n\nHelp, and how the bot is doing.\n\n'
        '`/help`: Show the commands you can use, or how to use one'
    )


async def test_hidden_and_disabled_commands_are_not_listed(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    gitgud, gimme = bot.get_command('gitgud'), bot.get_command('gimme')
    assert gitgud is not None and gimme is not None
    gitgud.hidden = True
    gimme.enabled = False

    sent = await overview(bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID))

    assert '/gitgud' not in listed(sent) and ';gimme' not in listed(sent)


async def test_a_long_category_takes_several_pages(
    bot: AccessBot,
    guild: MagicMock,
    posted: AsyncMock,
    rule_table: dict[str, Rule],
) -> None:
    async def plot(ctx: Context) -> None:
        """Plot something."""

    for number in range(45):
        name = f'plot{number:02}'
        bot.add_command(commands.Command(plot, name=name, brief='Plot ' + 'x' * 90))
        rule_table[name] = EVERYONE_BOT

    sent = await overview(bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID))

    # The commands without a cog are the bot's: 45 and the cogs' 3.
    bot_pages = [page for page in sent.pages if page.title == 'Bot']
    assert len(bot_pages) == 3
    assert [
        len([line for line in (page.description or '').splitlines() if '`' in line])
        for page in bot_pages
    ] == [20, 20, 8]
    count = len(sent.pages)
    assert [page.footer.text for page in sent.pages] == [
        f'{SLASH_FOOTER} Page {number} / {count}' for number in range(1, count + 1)
    ]
    for page in sent.pages:
        assert len(page) <= 6000 and len(page.description or '') <= 4096


# The overview of ;help, which the whole channel sees


async def test_the_prefix_overview_is_public_and_lists_only_commands_for_everyone(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # Even for a moderator: the channel sees it.
    moderator = make_member(
        guild, 'Moderator', permissions=discord.Permissions(manage_messages=True)
    )

    sent = await overview(
        bot, posted, moderator, place(guild, BOT_CHANNEL_ID), slash=False
    )

    assert not sent.private
    assert listed(sent) == [
        ';gimme',
        '/gitgud',
        '/whisper',
        '/clist show',
        '/clist future',
        '/contests upcoming',
        '/contests live',
        ';ranklist',
        '/handle show',
        '/duel show',
        '/duel challenge',
        '/help',
        '/meta show',
        '/meta ping',
    ]
    assert sent.pages[0].footer.text == f'{PREFIX_FOOTER} Page 1 / 5'


async def test_the_prefix_overview_leaves_out_commands_a_limit_keeps_from_some(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(who=Who.TRUSTED)})

    sent = await overview(
        bot,
        posted,
        make_member(guild, 'Trusted'),
        place(guild, BOT_CHANNEL_ID),
        slash=False,
    )

    assert '/gitgud' not in listed(sent)


async def test_the_prefix_overview_replies_to_the_message(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), slash=False
    )

    await send_help(ctx)

    assert posted.await_args is not None
    assert posted.await_args.kwargs['reference'] is ctx.message
    assert posted.await_args.kwargs['mention_author'] is False


# The help of one command


async def test_a_commands_help_says_what_it_does_and_how_to_use_it(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'gitgud'
    )

    assert page.title == 'gitgud'
    # The docstring without its examples, each paragraph on one line.
    assert page.description == (
        'Get a problem to solve, for gitgud points.\n\n'
        'The harder it is, the more points it gives.'
    )
    assert fields(page) == {
        'Usage': '`/gitgud [delta]`\n`;gitgud [delta=0]`',
        'Options': f'`delta`: {DELTA_OPTION}',
        'Examples': '`/gitgud`\n`;gitgud 200`',
        'Who': 'Everyone.',
        'Where': BOT_CHANNELS,
        HERE: '`/gitgud` and `;gitgud` answer everyone in this channel.',
        'Cooldown': 'Once every 10 seconds for each member.',
    }
    assert page.footer.text is None


async def test_a_commands_help_says_how_it_answers_here(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_member(guild), place(guild, GENERAL_ID), 'gitgud'
    )

    assert fields(page)[HERE] == (
        "`/gitgud` answers only you; `;gitgud` doesn't work here."
    )


@pytest.mark.parametrize(
    ('name', 'channel_id', 'where', 'here'),
    [
        (
            'ranklist',
            GENERAL_ID,
            'Bot channels only.',
            "`;ranklist` doesn't work here.",
        ),
        (
            'ranklist',
            BOT_CHANNEL_ID,
            'Bot channels only.',
            '`;ranklist` answers everyone in this channel.',
        ),
        (
            'duel challenge',
            GENERAL_ID,
            'Bot channels only.',
            "`/duel challenge` and `;duel challenge` don't work here.",
        ),
        (
            'whisper',
            GENERAL_ID,
            'Only the slash command works, in any channel, and it always answers '
            'only you.',
            '`/whisper` answers only you.',
        ),
        (
            'help',
            BOT_CHANNEL_ID,
            'Bot channels. The slash command works in any channel, and always '
            'answers only you.',
            '`/help` answers only you; `;help` answers everyone in this channel.',
        ),
        (
            'help',
            GENERAL_ID,
            'Bot channels. The slash command works in any channel, and always '
            'answers only you.',
            "`/help` answers only you; `;help` doesn't work here.",
        ),
    ],
    ids=[
        'prefix only, elsewhere',
        'prefix only, bot channel',
        'bot channels only, elsewhere',
        'always private',
        'help, bot channel',
        'help, elsewhere',
    ],
)
async def test_where_and_here_follow_the_rule(
    bot: AccessBot,
    guild: MagicMock,
    posted: AsyncMock,
    name: str,
    channel_id: int,
    where: str,
    here: str,
) -> None:
    page = await detail(bot, posted, make_member(guild), place(guild, channel_id), name)

    assert (fields(page)['Where'], fields(page)[HERE]) == (where, here)


async def test_access_always_answers_privately_on_slash(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_admin(guild), place(guild, BOT_CHANNEL_ID), 'access'
    )

    assert fields(page)['Where'] == (
        'The staff channel. The slash command works in any channel, and always '
        'answers only you.'
    )
    assert fields(page)[HERE] == (
        "`/access show` answers only you; `;access` doesn't work here."
    )


async def test_until_there_is_a_staff_channel_access_works_anywhere(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, staff_channel=None)

    page = await detail(
        bot, posted, make_admin(guild), place(guild, GENERAL_ID), 'access'
    )

    assert fields(page)['Where'] == f'Any channel. {ALWAYS_PRIVATE_ON_SLASH_TEXT}'
    assert ALWAYS_PRIVATE_ON_SLASH_TEXT == 'The slash command always answers only you.'
    assert fields(page)[HERE] == (
        '`/access show` answers only you; `;access` answers everyone in this channel.'
    )
    # The default rule is still the staff channel's.
    assert fields(page)['Default rule'] == (
        'Admins. The staff channel; elsewhere the slash command answers only you.'
    )


async def test_a_command_whose_answers_are_private_shows_only_its_slash_form(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # Its prefix form is refused everywhere.
    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'whisper'
    )

    assert fields(page)['Usage'] == '`/whisper`'
    assert fields(page)[HERE] == '`/whisper` answers only you.'


async def test_a_limit_that_makes_answers_private_shows_only_the_slash_form(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'clist *': Limit(private=True, where=Where.BOT_ONLY)})
    member = make_member(guild)

    page = await detail(bot, posted, member, place(guild, GENERAL_ID), 'clist future')
    posted.reset_mock()
    in_bot_channel = await detail(
        bot, posted, member, place(guild, BOT_CHANNEL_ID), 'clist future'
    )

    assert fields(page)['Usage'] == '`/clist future`'
    assert fields(page)['Where'] == (
        'Only the slash command works, in bot channels only, and it always answers '
        'only you.'
    )
    assert fields(page)[HERE] == "`/clist future` doesn't work here."
    assert fields(in_bot_channel)[HERE] == '`/clist future` answers only you.'


async def test_private_answers_without_a_slash_form_for_the_member(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # /_nogud needs Manage Messages to be seen, which a moderator by role lacks,
    # and the limit refuses ;_nogud: the help says why it doesn't work.
    await configure(bot, limits={'_nogud': Limit(private=True)})

    page = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        '_nogud',
    )

    assert fields(page)['Usage'] == '`;_nogud <member>`'
    assert fields(page)['Where'] == (
        'Bot channels. Its answers are private, so it works only as a slash command.'
    )
    assert fields(page)[HERE] == "`;_nogud` doesn't work here."


async def test_private_answers_for_a_command_without_any_slash_form(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # ;gimme has no slash command, and the limit refuses its ; form: it can't
    # be used at all, so the help mustn't send the member to a slash command.
    await configure(bot, limits={'gimme': Limit(private=True)})

    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'gimme'
    )

    assert fields(page)['Usage'] == '`;gimme [tags...]`'
    assert fields(page)['Where'] == (
        'Bot channels. Its answers must be private, and it has no slash command, so '
        "it can't be used in this server."
    )
    assert fields(page)[HERE] == "`;gimme` doesn't work here."


async def test_a_command_whose_slash_answers_are_always_private_says_so(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, rule_table: dict[str, Rule]
) -> None:
    # Like every /kcpc command, /link answers privately in any channel, while
    # its ; form answers the channel.
    rule_table.update({'link': EVERYONE_BOT, 'link codeforces': EVERYONE_BOT})
    await bot.add_cog(KcpcAccounts())
    bot_channel, staff = place(guild, BOT_CHANNEL_ID), place(guild, STAFF_CHANNEL_ID)

    member = await detail(
        bot, posted, make_member(guild), bot_channel, 'link codeforces'
    )
    posted.reset_mock()
    admin = await detail(bot, posted, make_admin(guild), staff, 'kcpc')

    assert fields(member)['Where'] == (
        'Bot channels. The slash command works in any channel, and always answers '
        'only you.'
    )
    assert fields(member)[HERE] == (
        '`/link codeforces` answers only you; `;link codeforces` answers everyone '
        'in this channel.'
    )
    assert fields(admin)[HERE] == (
        '`/kcpc show` answers only you; `;kcpc` answers everyone in this channel.'
    )


async def test_a_command_that_answers_by_direct_message_says_so(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As ;meta guilds does.
    monkeypatch.setattr(table, 'BY_DIRECT_MESSAGE', frozenset({'meta kill'}))
    owner = make_member(guild, member_id=OWNER_ID)

    page = await detail(bot, posted, owner, place(guild, BOT_CHANNEL_ID), 'meta kill')

    assert fields(page)[HERE] == '`;meta kill` answers you by direct message.'


async def test_the_owners_commands_that_need_an_admin_say_so(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As the club contest commands, which keep KCPC's admin check.
    monkeypatch.setattr(table, 'OWNER_AND_ADMIN', frozenset({'meta kill'}))
    admin_owner = make_member(
        guild, permissions=discord.Permissions(manage_guild=True), member_id=OWNER_ID
    )
    here = place(guild, BOT_CHANNEL_ID)

    owner = make_member(guild, member_id=OWNER_ID)

    page = await detail(bot, posted, admin_owner, here, 'meta kill')
    posted.reset_mock()
    refused = await refusal(bot, posted, owner, here, 'meta kill')

    assert fields(page)['Who'] == 'The bot owner, who must also be an admin.'
    assert fields(page)['Default rule'] == (
        'The bot owner, who must also be an admin. Any channel.'
    )
    # The owner who isn't an admin here can't use it, so it isn't theirs.
    assert refused == NO_COMMAND_TEXT.format(name='meta kill')


async def test_a_command_switched_off_or_with_broken_settings_says_so(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    member, channel = make_member(guild), place(guild, BOT_CHANNEL_ID)
    await configure(bot, limits={'duel *': Limit(off=True)})

    off = await detail(bot, posted, member, channel, 'duel challenge')
    user_db = MagicMock()
    user_db.get_all_access_settings = AsyncMock(return_value=[(GUILD_ID, '[]')])
    bot.access.use_user_db(user_db)
    await bot.access.load()
    posted.reset_mock()
    broken = await detail(bot, posted, member, channel, 'duel challenge')

    assert fields(off)[HERE] == OFF_TEXT
    assert fields(broken)[HERE] == REPAIR_TEXT


async def test_a_command_with_a_limit_shows_its_rule_in_this_server(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(who=Who.TRUSTED, where=Where.STAFF)})

    page = await detail(
        bot, posted, make_member(guild, 'Trusted'), place(guild, GENERAL_ID), 'gitgud'
    )

    assert fields(page)['Who'] == 'Trusted members, moderators and admins.'
    assert fields(page)['Where'] == (
        'The staff channel; elsewhere the slash command answers only you.'
    )


async def test_a_command_without_a_slash_form_for_the_member_shows_the_prefix_form(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # /_nogud needs Manage Messages to be seen, which a moderator by role lacks.
    page = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        '_nogud',
    )

    assert fields(page)['Usage'] == '`;_nogud <member>`'
    # The slash form's options are the prefix command's parameters too.
    assert fields(page)['Options'] == f'`member`: {MEMBER_OPTION}'
    assert fields(page)['Where'] == 'Bot channels.'
    assert fields(page)[HERE] == '`;_nogud` answers everyone in this channel.'


async def test_a_command_without_a_description_shows_its_brief(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'clist'
    )

    assert page.description == 'Show the contest list commands'
    assert 'Examples' not in fields(page) and 'Cooldown' not in fields(page)


async def test_the_cooldown_of_a_command_per_server(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'ranklist'
    )

    assert fields(page)['Cooldown'] == 'Once every 30 seconds in this server.'
    assert fields(page)['Usage'] == '`;ranklist <contest_id>`'


async def test_a_flags_parameter_is_spelt_out_flag_by_flag(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    command = bot.get_command('access limit')
    assert command is not None
    # What discord.py's signature makes of it.
    assert command.signature == '<flags>'

    page = await detail(
        bot, posted, make_admin(guild), place(guild, STAFF_CHANNEL_ID), 'access limit'
    )

    assert fields(page)['Usage'] == (
        '`/access limit <command> [who] [off]`\n'
        '`;access limit <command> [who: trusted|moderator] [off: yes|no]`'
    )
    assert fields(page)['Options'] == (
        '`command`: The command to limit\n'
        '`who`: Who else must be allowed\n'
        '`off`: Switch it off'
    )


class CountFlags(commands.FlagConverter, prefix='--', delimiter=' '):
    """Flags with their own syntax, one of them required."""

    count: int = commands.flag(description='How many')
    note: str | None = commands.flag(default=None, description='A note')


class ListFlags(commands.FlagConverter):
    """Flags that all have defaults."""

    first: str = commands.flag(positional=True, default='all', description='From')
    quiet: bool = commands.flag(default=False, description='Say less')


async def counted(ctx: Context, *, flags: CountFlags) -> None:
    """Count."""


async def listed_flags(ctx: Context, *, flags: ListFlags | None = None) -> None:
    """List."""


@pytest.mark.parametrize(
    ('command', 'usage'),
    [
        (commands.Command(counted, name='count'), ';count <--count …> [--note …]'),
        (commands.Command(listed_flags, name='list'), ';list [first] [quiet: yes|no]'),
        (
            commands.Command(counted, name='count', usage='<count> [note]'),
            ';count <count> [note]',
        ),
    ],
    ids=['own syntax', 'all optional', 'usage by hand'],
)
def test_flags_are_spelt_out_as_their_converter_reads_them(
    command: commands.Command[Any, ..., Any], usage: str
) -> None:
    assert prefix_usage(command) == usage


# Groups


async def test_a_groups_help_lists_the_commands_the_member_can_use_here(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    channel = place(guild, BOT_CHANNEL_ID)

    member = await detail(bot, posted, make_member(guild), channel, 'clist')
    posted.reset_mock()
    moderator = await detail(
        bot, posted, make_member(guild, 'Moderator'), channel, 'clist'
    )

    assert fields(member)['Usage'] == '`/clist show`\n`;clist`'
    assert fields(member)['Commands'] == '`/clist future`: List future contests'
    assert fields(moderator)['Commands'] == (
        '`/clist future`: List future contests\n'
        '`;clist purge`: Forget the cached contests'
    )


async def test_a_groups_help_marks_what_works_only_in_the_bot_channels(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    elsewhere = await detail(
        bot, posted, make_member(guild), place(guild, GENERAL_ID), 'duel'
    )
    posted.reset_mock()
    in_bot_channel = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'duel'
    )

    # The slash command, which works there.
    assert fields(elsewhere)['Commands'] == (
        '`/duel challenge`: Challenge a member to a duel (bot channels only)'
    )
    assert fields(in_bot_channel)['Commands'] == (
        '`/duel challenge`: Challenge a member to a duel'
    )


async def test_a_groups_help_marks_what_works_only_in_the_staff_channel(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    developer = make_member(guild, DEVELOPER_ROLE_ID)

    page = await detail(bot, posted, developer, place(guild, GENERAL_ID), 'meta')

    # Its slash form is gone from members' /meta, so the prefix one.
    assert fields(page)['Commands'] == (
        "`;meta git`: Show the bot's version (staff channel only)\n"
        '`/meta ping`: Check that the bot answers'
    )


async def test_a_groups_help_names_the_staff_channel_to_staff_alone(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # A limit moves a command for everyone to the staff channel.
    await configure(bot, limits={'meta ping': Limit(where=Where.STAFF_ONLY)})
    general = place(guild, GENERAL_ID)

    member = await detail(bot, posted, make_member(guild), general, 'meta')
    posted.reset_mock()
    moderator = await detail(
        bot, posted, make_member(guild, 'Moderator'), general, 'meta'
    )
    posted.reset_mock()
    in_public = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        'meta',
        slash=False,
    )

    assert 'Commands' not in fields(member)
    assert fields(moderator)['Commands'] == (
        '`/meta ping`: Check that the bot answers (staff channel only)'
    )
    # The channel sees a prefix help, so it never names the staff channel.
    assert 'Commands' not in fields(in_public)


async def test_a_groups_help_leaves_out_what_works_nowhere(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'duel challenge': Limit(off=True)})

    for channel_id in (GENERAL_ID, BOT_CHANNEL_ID):
        posted.reset_mock()
        page = await detail(
            bot, posted, make_member(guild), place(guild, channel_id), 'duel'
        )

        assert 'Commands' not in fields(page), channel_id


async def test_a_groups_prefix_help_lists_only_commands_for_everyone(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        'clist',
        slash=False,
    )

    assert fields(page)['Commands'] == '`/clist future`: List future contests'


async def test_a_group_whose_own_command_is_a_twin(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # ;handle shows handles, as /handle show does; its help lists the group.
    page = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        'handle',
    )

    assert page.title == 'handle'
    assert page.description == "Show a member's handles: yours, if you name no one."
    assert fields(page)['Usage'] == '`/handle show`\n`;handle [member]`'
    assert fields(page)['Commands'] == (
        '`/handle refer`: Make a member trusted\n'
        "`;handle set`: Set a member's handle\n"
        "`/handle show`: Show a member's handles"
    )


async def test_a_groups_twin_is_not_listed_as_its_command(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # ;contests upcoming is /contests upcoming, the group's own command.
    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'contests'
    )

    assert fields(page)['Usage'] == '`/contests upcoming`\n`;contests`'
    assert fields(page)['Commands'] == '`/contests live`: Show the contests running now'


async def test_a_long_help_takes_several_pages(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, rule_table: dict[str, Rule]
) -> None:
    group = bot.get_command('duel')
    assert isinstance(group, commands.Group)

    async def fight(ctx: Context) -> None:
        """Fight."""

    for number in range(80):
        name = f'fight{number:02}'
        group.add_command(
            commands.HybridCommand(
                fight, name=name, brief='y' * 100, with_app_command=False
            )
        )
        rule_table[f'duel {name}'] = EVERYONE_BOT
    ctx = make_context(bot, make_member(guild), place(guild, BOT_CHANNEL_ID))

    await send_help(ctx, 'duel')

    sent = answer(ctx, posted)
    assert len(sent.pages) == 2
    assert titles(sent) == ['duel', 'duel, continued']
    assert [page.footer.text for page in sent.pages] == ['Page 1 / 2', 'Page 2 / 2']
    lines = [
        line
        for page in sent.pages
        for field in page.fields
        if (field.name or '').startswith('Commands')
        for line in (field.value or '').splitlines()
    ]
    assert lines == [
        '`/duel challenge`: Challenge a member to a duel',
        *(f'`;duel fight{number:02}`: {"y" * 100}' for number in range(80)),
    ]
    for page in sent.pages:
        assert len(page) <= 6000 and len(page.fields) <= 25
        assert all(len(field.value or '') <= 1024 for field in page.fields)


# What admins see


async def test_admins_also_see_the_default_rule_and_this_servers_limits(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(
        bot,
        limits={
            'duel *': Limit(off=True),
            'duel challenge': Limit(who=Who.TRUSTED, where=Where.STAFF_ONLY),
            'duel register': Limit(private=True),
        },
    )

    page = await detail(
        bot, posted, make_admin(guild), place(guild, BOT_CHANNEL_ID), 'duel challenge'
    )

    assert fields(page)['Default rule'] == 'Everyone. Bot channels only.'
    # In the words of /access.
    assert fields(page)["This server's limits"] == (
        '`duel challenge`: for trusted members, moderators and admins only; in '
        'the staff channel only.\n'
        '`duel *`: switched off.'
    )


async def test_without_limits_admins_are_told_how_to_add_one(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot, posted, make_admin(guild), place(guild, BOT_CHANNEL_ID), 'gitgud'
    )

    assert list(fields(page)) == [
        'Usage',
        'Options',
        'Examples',
        'Who',
        'Where',
        HERE,
        'Cooldown',
        'Default rule',
        "This server's limits",
    ]
    assert fields(page)['Default rule'] == f'Everyone. {BOT_CHANNELS}'
    assert fields(page)["This server's limits"] == (
        'None. Add one with `/access limit`.'
    )


@pytest.mark.parametrize('name', ['help', 'access', 'meta kill'])
async def test_admins_are_told_when_limits_do_not_apply(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, name: str
) -> None:
    # /help and /access, so that admins can always undo limits, and the bot
    # owner's commands.
    owner_admin = make_member(
        guild, permissions=discord.Permissions(manage_guild=True), member_id=OWNER_ID
    )

    page = await detail(bot, posted, owner_admin, place(guild, STAFF_CHANNEL_ID), name)

    assert fields(page)["This server's limits"] == (
        "Limits don't apply to this command."
    )


@pytest.mark.parametrize(
    ('every_row', 'text'),
    [
        # This server's own row: an admin's reset repairs it.
        (False, REPAIR_TEXT),
        # No row could be read: a reset would replace rows that were never read.
        (True, UNREADABLE_TEXT),
    ],
    ids=['its row', 'every row'],
)
async def test_with_broken_settings_admins_are_told_what_to_do(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, every_row: bool, text: str
) -> None:
    user_db = MagicMock()
    if every_row:
        user_db.get_all_access_settings = AsyncMock(side_effect=RuntimeError('gone'))
    else:
        user_db.get_all_access_settings = AsyncMock(return_value=[(GUILD_ID, '[]')])
    bot.access.use_user_db(user_db)
    await bot.access.load()

    page = await detail(
        bot, posted, make_admin(guild), place(guild, BOT_CHANNEL_ID), 'gitgud'
    )

    assert fields(page)["This server's limits"] == text
    assert fields(page)[HERE] == text


async def test_others_never_see_the_default_rule(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    page = await detail(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        'gitgud',
    )

    assert 'Default rule' not in fields(page)
    assert "This server's limits" not in fields(page)


async def test_a_prefix_help_never_shows_the_default_rule(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # The channel sees it, so not even an admin's shows the server's limits.
    page = await detail(
        bot,
        posted,
        make_admin(guild),
        place(guild, BOT_CHANNEL_ID),
        'gitgud',
        slash=False,
    )

    assert 'Default rule' not in fields(page)
    assert "This server's limits" not in fields(page)


# Which command


@pytest.mark.parametrize(
    ('typed', 'name'),
    [
        ('gitgud', 'gitgud'),
        ('/gitgud', 'gitgud'),
        (';gitgud', 'gitgud'),
        ('GitGud', 'gitgud'),
        ('  clist   future ', 'clist future'),
        ('clist show', 'clist'),
        ('/clist show', 'clist'),
        ('/contests upcoming', 'contests upcoming'),
        ('contests', 'contests'),
        ('handle', 'handle'),
        ('handle show', 'handle show'),
        ('meta kill', 'meta kill'),
        ('clist purge', 'clist purge'),
    ],
)
async def test_a_command_is_found_by_any_of_its_names(
    bot: AccessBot, typed: str, name: str
) -> None:
    found = find_command(bot, typed)

    assert found is not None and found.qualified_name == name


@pytest.mark.parametrize(
    'typed',
    ['', ' ', '/', 'nope', 'gitgud extra', 'clist nope', 'clist future more'],
)
async def test_nothing_else_is_a_command(bot: AccessBot, typed: str) -> None:
    assert find_command(bot, typed) is None


async def test_a_groups_fallback_finds_the_group_without_a_prefix_twin(
    bot: AccessBot,
) -> None:
    # /contests upcoming runs ;contests, also when ;contests upcoming is gone.
    # (HybridGroup.remove_command would take the slash fallback out too.)
    group = bot.get_command('contests')
    assert isinstance(group, commands.Group)
    commands.GroupMixin.remove_command(group, 'upcoming')

    found = find_command(bot, '/contests upcoming')

    assert found is group


# Commands the member may not use


@both_paths
async def test_an_unknown_command_gets_a_short_reply(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, slash: bool
) -> None:
    text = await refusal(
        bot,
        posted,
        make_member(guild),
        place(guild, BOT_CHANNEL_ID),
        'nope',
        slash=slash,
    )

    assert text == NO_COMMAND_TEXT.format(name='nope')
    assert text == 'No command called `nope` that you can use here.'


@both_paths
@pytest.mark.parametrize('name', ['kcpc status', 'clist purge', 'meta kill', 'access'])
async def test_a_command_the_member_may_not_use_is_as_unknown(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, slash: bool, name: str
) -> None:
    text = await refusal(
        bot,
        posted,
        make_member(guild),
        place(guild, STAFF_CHANNEL_ID),
        name,
        slash=slash,
    )

    assert text == NO_COMMAND_TEXT.format(name=name)


async def test_the_bot_owners_commands_are_unknown_even_to_admins(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    text = await refusal(
        bot, posted, make_admin(guild), place(guild, STAFF_CHANNEL_ID), 'meta kill'
    )

    assert text == NO_COMMAND_TEXT.format(name='meta kill')


async def test_a_disabled_command_is_as_unknown(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    gimme = bot.get_command('gimme')
    assert gimme is not None
    gimme.enabled = False

    text = await refusal(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'gimme'
    )

    assert text == NO_COMMAND_TEXT.format(name='gimme')


async def test_a_hidden_command_still_has_its_help(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    gimme = bot.get_command('gimme')
    assert gimme is not None
    gimme.hidden = True

    page = await detail(
        bot, posted, make_member(guild), place(guild, BOT_CHANNEL_ID), 'gimme'
    )

    assert page.title == 'gimme'


async def test_the_name_in_a_reply_cannot_break_out_of_its_code_span(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    channel = place(guild, BOT_CHANNEL_ID)

    ticks = await refusal(bot, posted, make_member(guild), channel, 'a`b @everyone')
    posted.reset_mock()
    long = await refusal(bot, posted, make_member(guild), channel, 'x' * 100)

    assert ticks == NO_COMMAND_TEXT.format(name="a'b @everyone")
    assert long == NO_COMMAND_TEXT.format(name=f'{"x" * 59}…')


async def test_a_prefix_help_of_a_command_not_for_everyone_points_to_slash(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # The channel would see it; only the moderator asking may use it.
    text = await refusal(
        bot,
        posted,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        'clist purge',
        slash=False,
    )

    assert text == USE_SLASH_HELP_TEXT.format(name='clist purge')
    assert text == 'Use `/help clist purge` for this command.'


async def test_a_prefix_help_of_a_command_a_limit_keeps_from_some_points_to_slash(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    await configure(bot, limits={'gitgud': Limit(who=Who.TRUSTED)})

    text = await refusal(
        bot,
        posted,
        make_member(guild, 'Trusted'),
        place(guild, BOT_CHANNEL_ID),
        '/gitgud',
        slash=False,
    )

    assert text == USE_SLASH_HELP_TEXT.format(name='gitgud')


# Through the commands


async def test_slash_help_with_a_command_shows_its_help_privately(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = await use_slash(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), help_data('clist future')
    )

    # The access check lets /help answer in public in a bot channel, but the
    # help is private all the same.
    decision = cached_decision(ctx)
    assert decision is not None and decision.outcome is Outcome.PUBLIC
    sent = answer(ctx, posted)
    assert sent.private
    assert titles(sent) == ['clist future']


@pytest.mark.parametrize(
    ('typed', 'text'),
    [
        ('nope', NO_COMMAND_TEXT.format(name='nope')),
        ('kcpc status', NO_COMMAND_TEXT.format(name='kcpc status')),
    ],
    ids=['unknown', 'not for the member'],
)
async def test_slash_help_answers_privately_when_it_has_no_help_to_show(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock, typed: str, text: str
) -> None:
    # In a bot channel, where the check lets /help answer in public.
    ctx = await use_slash(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), help_data(typed)
    )

    assert answer(ctx, posted).private
    assert alert(ctx, posted) == text


async def test_slash_help_without_a_command_shows_the_overview_privately(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = await use_slash(
        bot, make_member(guild), place(guild, GENERAL_ID), help_data()
    )

    sent = answer(ctx, posted)
    assert sent.private
    assert isinstance(sent.view, PaginatorView) and sent.view.owner_id == MEMBER_ID
    assert '/help' in listed(sent)


async def test_slash_help_works_while_the_settings_need_repair(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    user_db = MagicMock()
    user_db.get_all_access_settings = AsyncMock(side_effect=RuntimeError('gone'))
    bot.access.use_user_db(user_db)
    await bot.access.load()

    ctx = await use_slash(
        bot, make_member(guild), place(guild, GENERAL_ID), help_data('gitgud')
    )

    sent = answer(ctx, posted)
    assert sent.private
    # No settings could be read at all, which only the bot owner can fix.
    assert fields(sent.pages[0])[HERE] == UNREADABLE_TEXT


async def test_prefix_help_takes_the_rest_of_the_message(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(
        bot,
        make_member(guild),
        place(guild, BOT_CHANNEL_ID),
        slash=False,
        arguments='clist   future',
    )
    assert ctx.command is not None

    await ctx.command.invoke(ctx)

    sent = answer(ctx, posted)
    assert not sent.private
    assert titles(sent) == ['clist future']


async def test_prefix_help_outside_bot_channels_points_to_slash_help(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(bot, make_member(guild), place(guild, GENERAL_ID), slash=False)
    assert ctx.command is not None

    with pytest.raises(AccessDenied) as denied:
        await ctx.command.invoke(ctx)

    assert denied.value.text == (
        'Use this command in a bot channel: <#1200000000000000001>. '
        + SLASH_HINT.format(path='/help')
    )
    posted.assert_not_awaited()


async def test_a_groups_own_slash_command_shows_its_help_privately(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # /clist show answers in public in a bot channel, but its help is private.
    ctx = await use_slash(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), fallback_data('clist')
    )

    decision = cached_decision(ctx)
    assert decision is not None and decision.outcome is Outcome.PUBLIC
    sent = answer(ctx, posted)
    assert sent.private
    assert titles(sent) == ['clist']
    assert fields(sent.pages[0])['Commands'] == '`/clist future`: List future contests'


async def test_a_group_that_asks_for_its_help_by_name_gets_it(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    # /duel show asks for the help of 'duel'.
    ctx = await use_slash(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), fallback_data('duel')
    )

    sent = answer(ctx, posted)
    assert sent.private
    assert titles(sent) == ['duel']
    assert fields(sent.pages[0])['Commands'] == (
        '`/duel challenge`: Challenge a member to a duel'
    )


async def test_a_groups_own_prefix_command_shows_its_help_to_the_channel(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(
        bot,
        make_member(guild, 'Moderator'),
        place(guild, BOT_CHANNEL_ID),
        slash=False,
        name='duel',
    )
    assert ctx.command is not None

    await ctx.command.invoke(ctx)

    sent = answer(ctx, posted)
    assert not sent.private
    assert fields(sent.pages[0])['Commands'] == (
        '`/duel challenge`: Challenge a member to a duel'
    )


async def test_a_staff_groups_own_prefix_command_points_to_slash_help(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(
        bot,
        make_member(guild, 'Moderator'),
        place(guild, STAFF_CHANNEL_ID),
        slash=False,
        name='roleupdate',
    )
    assert ctx.command is not None

    await ctx.command.invoke(ctx)

    assert alert(ctx, posted) == 'Use `/help roleupdate` for this command.'


async def test_a_staff_groups_own_slash_command_shows_its_help(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    moderator = make_member(
        guild, 'Moderator', permissions=discord.Permissions(manage_messages=True)
    )

    ctx = await use_slash(
        bot, moderator, place(guild, STAFF_CHANNEL_ID), fallback_data('roleupdate')
    )

    sent = answer(ctx, posted)
    assert sent.private
    assert fields(sent.pages[0])['Commands'] == (
        '`/roleupdate now`: Update the rank roles now'
    )


async def test_the_help_keeps_no_decision_but_that_of_its_own_command(
    bot: AccessBot,
    guild: MagicMock,
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the access check keeps a decision, for TLEContext; the help asks
    # the access service about every command without keeping any.
    kept: list[str] = []
    keep = service_module.cache_decision

    def spy(ctx: Any, decision: Decision) -> None:
        kept.append(ctx.command.qualified_name)
        keep(ctx, decision)

    monkeypatch.setattr(service_module, 'cache_decision', spy)
    admin, channel = make_admin(guild), place(guild, STAFF_CHANNEL_ID)

    first = await use_slash(bot, admin, channel, help_data())
    second = await use_slash(bot, admin, channel, help_data('kcpc'))

    assert kept == ['help', 'help']
    assert answer(first, posted).private and answer(second, posted).private


async def test_unknown_owners_are_asked_for_once_and_their_commands_left_out(
    rule_table: dict[str, Rule], guild: MagicMock, posted: AsyncMock
) -> None:
    bot = await make_bot(owner_id=None)
    application_info = AsyncMock(
        side_effect=discord.HTTPException(
            MagicMock(status=503, reason='Service Unavailable'), 'down'
        )
    )
    try:
        await configure(bot)
        bot.application_info = application_info
        owner = make_member(guild, member_id=OWNER_ID)
        channel = place(guild, GENERAL_ID)

        first = await overview(bot, posted, owner, channel)
        posted.reset_mock()
        second = await overview(bot, posted, owner, channel)
    finally:
        await bot.close()

    assert 'Bot owner' not in titles(first)
    assert ';meta kill' not in listed(first)
    assert listed(second) == listed(first)
    # Not once per owner's command: the service waits before asking again.
    application_info.assert_awaited_once()


async def test_the_help_never_asks_discord_py_whether_commands_can_run(
    bot: AccessBot,
    guild: MagicMock,
    posted: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # can_run would run the access check, which caches its decision on the
    # context: the help decides through the access service alone.
    monkeypatch.setattr(
        commands.Command, 'can_run', AsyncMock(side_effect=AssertionError('can_run'))
    )
    admin, channel = make_admin(guild), place(guild, STAFF_CHANNEL_ID)

    await send_help(make_context(bot, admin, channel))
    await send_help(make_context(bot, admin, channel), 'kcpc')

    assert posted.await_count == 0


async def test_a_private_help_that_comes_too_late_is_never_posted(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    ctx = make_context(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), expired=True
    )

    with pytest.raises(PrivateAnswerExpired):
        await send_help(ctx, 'gitgud')

    posted.assert_not_awaited()


async def test_a_user_who_is_not_a_member_gets_nothing(
    bot: AccessBot, guild: MagicMock, posted: AsyncMock
) -> None:
    user = MagicMock(spec=discord.User, id=MEMBER_ID, guild=guild)
    channel = place(guild, BOT_CHANNEL_ID)

    overview_ctx = make_context(bot, user, channel)
    await send_help(overview_ctx)
    command_ctx = make_context(bot, user, channel)
    await send_help(command_ctx, 'gitgud')

    assert alert(overview_ctx, posted) == NO_COMMANDS_TEXT
    assert alert(command_ctx, posted) == NO_COMMAND_TEXT.format(name='gitgud')


async def test_without_an_access_service_the_help_shows_nothing(
    rule_table: dict[str, Rule],
    guild: MagicMock,
    posted: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = await make_bot(access=False)
    try:
        ctx = make_context(bot, make_member(guild), place(guild, BOT_CHANNEL_ID))

        with caplog.at_level(logging.WARNING, logger=HELP_LOGGER):
            await send_help(ctx)
    finally:
        await bot.close()

    assert alert(ctx, posted) == UNAVAILABLE_TEXT
    assert [
        record.getMessage() for record in caplog.records if record.name == HELP_LOGGER
    ] == ['There is no access service, so /help can show nothing']


# Suggestions


def typing_help(bot: commands.Bot, user: Any, channel: Any, typed: str) -> MagicMock:
    """A member typing the command option of /help."""
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = discord.InteractionType.autocomplete
    interaction.guild_id = GUILD_ID
    interaction.user = user
    interaction.channel = channel
    interaction.command = bot.tree.get_command('help')
    interaction.data = {
        'type': 1,
        'name': 'help',
        'options': [{'type': 3, 'name': 'command', 'value': typed, 'focused': True}],
    }
    interaction.is_expired.return_value = False
    interaction.response = Response()
    return interaction


async def suggested(
    bot: commands.Bot, user: Any, channel: Any, typed: str
) -> list[app_commands.Choice[str]]:
    """What /help suggests as ``user`` types ``typed``, through discord.py's
    tree and the access service's gate.
    """
    interaction = typing_help(bot, user, channel, typed)
    await bot.tree._call(interaction)
    autocomplete = interaction.response.autocomplete
    autocomplete.assert_awaited_once()
    choices: list[app_commands.Choice[str]] = autocomplete.await_args.args[0]
    return choices


async def test_suggestions_are_the_commands_the_member_can_use_here(
    bot: AccessBot, guild: MagicMock
) -> None:
    choices = await suggested(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), 'cl'
    )

    assert choices == [
        app_commands.Choice(
            name='/clist show: Show the contest list commands', value='clist'
        ),
        app_commands.Choice(
            name='/clist future: List future contests', value='clist future'
        ),
    ]


async def test_suggestions_that_start_with_what_was_typed_come_first(
    bot: AccessBot, guild: MagicMock
) -> None:
    member, channel = make_member(guild), place(guild, BOT_CHANNEL_ID)

    by_slash_path = await suggested(bot, member, channel, '/con')
    by_word = await suggested(bot, member, channel, 'u')

    assert [choice.value for choice in by_slash_path] == ['contests', 'contests live']
    # The form that works here counts too: /contests upcoming has a word that
    # starts with u, the others only hold one.
    assert [choice.value for choice in by_word] == [
        'contests',
        'clist future',
        'duel',
        'duel challenge',
        'gitgud',
    ]


def usable_as(name: str, slash_path: str | None = None) -> help_module.Usable:
    """Command ``name``, which works here; through ``slash_path`` if given."""

    async def callback(ctx: Context) -> None:
        """Do it."""

    rule = Effective(frozenset({Who.EVERYONE}), Where.ANYWHERE)
    slash = None if slash_path is None else Decision(Outcome.PUBLIC, rule, True)
    prefix = Decision(Outcome.PUBLIC, rule, False)
    return help_module.Usable(
        commands.Command(callback, name=name), slash_path, slash, prefix
    )


@pytest.mark.parametrize(
    ('typed', 'values'),
    [
        ('nog', ['nogud', '_nogud']),
        ('po', ['postal', 'kcpc contests start-posts']),
        # The last holds g in its form, /contests upcoming.
        ('g', ['gitgud', 'meta git', 'contests', 'duel challenge', '_nogud', 'nogud']),
        ('UP', ['contests']),
        (
            '',
            [
                'contests',
                'duel challenge',
                'gitgud',
                'kcpc contests start-posts',
                'meta git',
                '_nogud',
                'nogud',
                'postal',
            ],
        ),
    ],
    ids=['after _', 'after -', 'three tiers', 'any case', 'nothing typed'],
)
def test_suggestions_rank_names_then_words_then_the_rest(
    typed: str, values: list[str]
) -> None:
    found = [
        usable_as('nogud'),
        usable_as('_nogud'),
        usable_as('gitgud'),
        usable_as('meta git'),
        usable_as('duel challenge'),
        usable_as('kcpc contests start-posts'),
        usable_as('postal'),
        usable_as('contests', '/contests upcoming'),
    ]

    choices = help_module.suggestions(found, typed)

    assert [choice.value for choice in choices] == values


async def test_suggestions_depend_on_who_asks_and_where(
    bot: AccessBot, guild: MagicMock
) -> None:
    member, admin = make_member(guild), make_admin(guild)
    staff, general = place(guild, STAFF_CHANNEL_ID), place(guild, GENERAL_ID)

    assert await suggested(bot, member, staff, 'kc') == []
    assert [choice.value for choice in await suggested(bot, admin, staff, 'kc')] == [
        'kcpc',
        'kcpc status',
    ]
    assert [choice.value for choice in await suggested(bot, admin, general, 'kc')] == [
        'kcpc'
    ]
    # Not even admins get the bot owner's.
    assert await suggested(bot, admin, staff, 'kill') == []


async def test_at_most_25_suggestions_each_within_discords_limit(
    bot: AccessBot, guild: MagicMock, rule_table: dict[str, Rule]
) -> None:
    async def plot(ctx: Context) -> None:
        """Plot something."""

    for number in range(30):
        name = f'plot{number:02}'
        bot.add_command(commands.Command(plot, name=name, brief='z' * 120))
        rule_table[name] = EVERYONE_BOT

    choices = await suggested(
        bot, make_member(guild), place(guild, BOT_CHANNEL_ID), 'plot'
    )

    assert len(choices) == MAX_SUGGESTIONS == 25
    assert choices[0] == app_commands.Choice(
        name=f';plot00: {"z" * 90}…', value='plot00'
    )
    assert all(len(choice.name) <= 100 for choice in choices)


async def test_suggestions_that_fail_are_none_and_logged(
    bot: AccessBot,
    guild: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    help_cog = bot.get_cog('Help')
    assert isinstance(help_cog, Help)
    interaction = typing_help(bot, make_member(guild), place(guild, BOT_CHANNEL_ID), '')
    monkeypatch.setattr(
        help_module, 'usable_here', AsyncMock(side_effect=RuntimeError('boom'))
    )

    with caplog.at_level(logging.ERROR, logger=HELP_LOGGER):
        choices = await help_cog.command_autocomplete(interaction, '')

    assert choices == []
    assert [
        record.getMessage() for record in caplog.records if record.name == HELP_LOGGER
    ] == ['Could not suggest commands for /help']


async def test_no_suggestions_without_an_access_service_or_for_a_non_member(
    rule_table: dict[str, Rule], guild: MagicMock
) -> None:
    plain = await make_bot(access=False)
    gated = await make_bot()
    try:
        plain_cog, gated_cog = plain.get_cog('Help'), gated.get_cog('Help')
        assert isinstance(plain_cog, Help) and isinstance(gated_cog, Help)
        channel = place(guild, BOT_CHANNEL_ID)
        user = MagicMock(spec=discord.User, id=MEMBER_ID)

        without = await plain_cog.command_autocomplete(
            typing_help(plain, make_member(guild), channel, ''), ''
        )
        stranger = await gated_cog.command_autocomplete(
            typing_help(gated, user, channel, ''), ''
        )
    finally:
        await plain.close()
        await gated.close()

    assert without == [] and stranger == []


# The help command itself


async def test_help_is_a_slash_command_whose_option_suggests_commands(
    bot: AccessBot,
) -> None:
    command = bot.tree.get_command('help')
    assert isinstance(command, app_commands.Command)

    payload = command.to_dict(bot.tree)

    assert payload['description'] == 'Show the commands you can use, or how to use one'
    (option,) = payload['options']
    assert option == {
        'type': discord.AppCommandOptionType.string.value,
        'name': 'command',
        'description': (
            'A command, such as gitgud or clist future; all you can use here if left '
            'out'
        ),
        'required': False,
        'autocomplete': True,
    }
    assert len(option['description']) <= 100
    # It is in no group's slash list: /help stays visible to every member.
    assert command.default_permissions is None


async def test_the_help_commands_texts_follow_the_style_guide(bot: AccessBot) -> None:
    command = bot.get_command('help')
    assert command is not None and command.brief is not None

    assert len(command.brief) <= 80
    assert command.brief[0].isupper() and not command.brief.endswith('.')
    description, examples = split_help(command.help)
    assert description and examples
    for example in examples:
        assert example.split()[0] in ('/help', ';help')


def test_the_real_table_has_helps_rule_and_category() -> None:
    assert REAL_RULES['help'] == Rule(Who.EVERYONE, Where.BOT)
    assert table.category_of('help', 'Help').key == 'bot'


# Help texts


@pytest.mark.parametrize(
    ('text', 'description', 'examples'),
    [
        (None, '', ()),
        ('', '', ()),
        ('Do it.', 'Do it.', ()),
        (
            'Do it, all of it,\nnow.\n\nThen rest.',
            'Do it, all of it, now.\n\nThen rest.',
            (),
        ),
        (
            'Do it.\n\nExamples:\n    ;do\n    /do it\n',
            'Do it.',
            (';do', '/do it'),
        ),
        ('Examples:\n;do', '', (';do',)),
        (
            # As gitgud's: the columns line up only in a code block.
            'Get a problem, for\npoints:\ndelta  | -100 | +100\npoints |   5  |  12\n'
            'The rest\nfollows.',
            'Get a problem, for points:\n'
            '```\ndelta  | -100 | +100\npoints |   5  |  12\n```\n'
            'The rest follows.',
            (),
        ),
        (
            'Modes:\n- all\n- some\nor\nnone.',
            'Modes:\n- all\n- some\nor none.',
            (),
        ),
        ('Say on|off, or\n  here.', 'Say on|off, or\n  here.', ()),
    ],
    ids=[
        'none',
        'empty',
        'one line',
        'wrapped paragraphs',
        'with examples',
        'examples alone',
        'a table',
        'a list',
        'a line set in',
    ],
)
def test_help_texts_split_into_the_description_and_the_examples(
    text: str | None, description: str, examples: tuple[str, ...]
) -> None:
    assert split_help(text) == (description, examples)


def cooled(
    rate: int, per: float, bucket: commands.BucketType
) -> commands.Command[Any, ..., Any]:
    async def callback(ctx: Context) -> None:
        """Wait."""

    command = commands.Command(callback, name='wait')
    return commands.cooldown(rate, per, bucket)(command)


USER = commands.BucketType.user


@pytest.mark.parametrize(
    ('command', 'text'),
    [
        (cooled(1, 10, USER), 'Once every 10 seconds for each member'),
        (
            cooled(1, 30, commands.BucketType.guild),
            'Once every 30 seconds in this server',
        ),
        (
            cooled(2, 60, commands.BucketType.member),
            'Twice every minute for each member',
        ),
        (
            cooled(3, 120, commands.BucketType.default),
            '3 times every 2 minutes, for everyone together',
        ),
        (
            cooled(1, 1, commands.BucketType.channel),
            'Once every second in each channel',
        ),
        (cooled(1, 1.5, USER), 'Once every 1.5 seconds for each member'),
        (cooled(1, 90, USER), 'Once every 90 seconds for each member'),
    ],
    ids=['user', 'server', 'twice', 'shared', 'channel', 'fraction', 'not minutes'],
)
def test_a_cooldown_is_said_in_words(
    command: commands.Command[Any, ..., Any], text: str
) -> None:
    assert describe_cooldown(command) == text


def test_without_a_cooldown_there_is_nothing_to_say() -> None:
    async def callback(ctx: Context) -> None:
        """Go."""

    assert describe_cooldown(commands.Command(callback, name='go')) is None


def test_a_cooldown_of_another_kind_says_only_how_often() -> None:
    async def callback(ctx: Context) -> None:
        """Go."""

    command = commands.Command(callback, name='go')
    command._buckets = commands.CooldownMapping.from_cooldown(1, 5, lambda ctx: 1)

    assert describe_cooldown(command) == 'Once every 5 seconds'
