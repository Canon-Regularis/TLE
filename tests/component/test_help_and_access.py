"""/help and /access together, as TLEBot sets them up: the real Help and
Access cogs, the access service's check and command tree, and TLE's own rule
table.

Each cog's own tests use a stand-in for the other: those of /help a model of
/access limit, and those of /access a /help that does nothing. These tests
check what /help says about /access against the real cog: the options of
/access limit, flag by flag, that /access answers only the admin on slash,
even where the access rules would let it answer the whole channel, and that
both describe a limit in the same words. Commands
run through the bot's command tree, from the interaction's data, with real
TLEContexts. Discord itself (the server, its members and channels, and the
interaction) is mocked.
"""

from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.access.cog import Access, LimitFlags
from tle.access.context import TLEContext
from tle.access.help import Help
from tle.access.rules import Limit, Outcome
from tle.access.service import AccessService, AccessTree, cached_decision
from tle.access.settings import GuildAccess
from tle.access.slash import apply_visibility

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
ADMIN_ID = 1_400_000_000_000_000_001

Context = commands.Context[Any]


class Codeforces(commands.Cog):
    """A member command for /access limit to name, ruled by TLE's table."""

    @commands.hybrid_command(brief='Get a problem to solve for gitgud points')
    async def gitgud(self, ctx: Context) -> None:
        """Get a problem to solve, for gitgud points."""


class TLELikeBot(commands.Bot):
    """A bot that carries an access service and makes TLEContexts, as TLEBot
    does.
    """

    access: AccessService

    async def get_context(self, origin: Any, /, *, cls: Any = None) -> Any:
        return await super().get_context(origin, cls=cls or TLEContext)


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
async def bot() -> AsyncIterator[TLELikeBot]:
    """The bot, in a server with a bot channel and a staff channel."""
    bot = TLELikeBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    bot.access = AccessService(bot)
    bot.add_check(bot.access.check)
    await bot.add_cog(Access(bot))
    await bot.add_cog(Help(bot))
    await bot.add_cog(Codeforces())
    apply_visibility(bot)
    settings = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
    await bot.access.change(GUILD_ID, lambda _: settings)
    yield bot
    await bot.close()


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose staff channel only staff can read."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.roles = []
    channels = {}
    for channel_id, public in ((BOT_CHANNEL_ID, True), (STAFF_CHANNEL_ID, False)):
        channel = MagicMock(
            spec=discord.TextChannel,
            id=channel_id,
            guild=guild,
            mention=f'<#{channel_id}>',
        )
        channel.permissions_for.return_value = discord.Permissions(view_channel=public)
        channels[channel_id] = channel
    guild.get_channel.side_effect = channels.get
    return guild


@pytest.fixture
def admin(guild: MagicMock) -> MagicMock:
    """An admin through the Manage Server permission."""
    member = MagicMock(spec=discord.Member, id=ADMIN_ID, guild=guild)
    member.guild_permissions = discord.Permissions(manage_guild=True)
    member.roles = []
    return member


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in the channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers it.
    """

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, *args: Any, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))


def slash_data(
    name: str, subcommand: str | None = None, **options: str | bool
) -> dict[str, Any]:
    """The data of the slash command ``name``, or of its ``subcommand``, with
    the ``options`` given.
    """
    given = [
        {'type': 5 if isinstance(value, bool) else 3, 'name': option, 'value': value}
        for option, value in options.items()
    ]
    if subcommand is not None:
        given = [{'type': 1, 'name': subcommand, 'options': given}]
    return {'type': 1, 'name': name, 'options': given}


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


def answer(ctx: TLEContext) -> tuple[discord.Embed, bool]:
    """The one answer to ``ctx``'s interaction, and whether it was private."""
    assert ctx.interaction is not None
    send = cast(Response, ctx.interaction.response).send_message
    send.assert_awaited_once()
    assert send.await_args is not None
    options = send.await_args.kwargs
    return options['embed'], options['ephemeral']


def fields(page: discord.Embed) -> dict[str, str | None]:
    return {field.name or '': field.value for field in page.fields}


async def test_the_help_of_access_limit_spells_out_its_flags(
    bot: TLELikeBot, guild: MagicMock, admin: MagicMock, posted: AsyncMock
) -> None:
    staff_channel = guild.get_channel(STAFF_CHANNEL_ID)

    ctx = await use_slash(
        bot, admin, staff_channel, slash_data('help', command='access limit')
    )

    page, private = answer(ctx)
    assert private
    posted.assert_not_awaited()
    assert page.title == 'access limit'
    assert fields(page)['Usage'] == (
        '`/access limit <command> [who] [where] [private] [off] [subcommands]`\n'
        '`;access limit <command> [who: trusted|moderator|developer|admin] '
        '[where: bot|bot-only|staff|staff-only] [private: yes|no] [off: yes|no] '
        '[subcommands: yes|no]`'
    )
    # Each option as the flag describes it, in the flags' order.
    assert fields(page)['Options'] == '\n'.join(
        f'`{flag.name}`: {flag.description}' for flag in LimitFlags.get_flags().values()
    )


async def test_access_answers_only_the_admin_as_its_help_says(
    bot: TLELikeBot, guild: MagicMock, admin: MagicMock, posted: AsyncMock
) -> None:
    staff_channel = guild.get_channel(STAFF_CHANNEL_ID)

    helped = await use_slash(
        bot, admin, staff_channel, slash_data('help', command='access')
    )
    shown = await use_slash(bot, admin, staff_channel, slash_data('access', 'show'))
    limited = await use_slash(
        bot,
        admin,
        staff_channel,
        slash_data('access', 'limit', command='gitgud', off=True),
    )

    page, _ = answer(helped)
    assert fields(page)['Where'] == (
        'The staff channel. The slash command works in any channel, and always '
        'answers only you.'
    )
    assert fields(page)['In this channel'] == (
        '`/access show` answers only you; `;access` answers everyone in this channel.'
    )
    # In the staff channel the access rules let /access answer the channel,
    # but the cog answers only the admin.
    for ctx in (shown, limited):
        decision = cached_decision(ctx)
        assert decision is not None and decision.outcome is Outcome.PUBLIC
        _, private = answer(ctx)
        assert private
    posted.assert_not_awaited()
    # The flags came through the slash command's options.
    assert dict(bot.access.guild_access(GUILD_ID).limits) == {'gitgud': Limit(off=True)}


async def test_help_shows_a_limit_in_the_words_of_access(
    bot: TLELikeBot, guild: MagicMock, admin: MagicMock, posted: AsyncMock
) -> None:
    staff_channel = guild.get_channel(STAFF_CHANNEL_ID)

    limited = await use_slash(
        bot,
        admin,
        staff_channel,
        slash_data(
            'access',
            'limit',
            command='gitgud',
            who='trusted',
            where='bot-only',
            private=True,
        ),
    )
    helped = await use_slash(
        bot, admin, staff_channel, slash_data('help', command='gitgud')
    )

    reply, _ = answer(limited)
    page, _ = answer(helped)
    described = (
        '`gitgud`: for trusted members, moderators and admins only; in bot '
        'channels only; answers only the person who uses it.'
    )
    assert (reply.description or '').splitlines()[0] == described
    assert fields(page)["This server's limits"] == described
    posted.assert_not_awaited()
