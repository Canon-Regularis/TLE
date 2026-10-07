"""Tests for TLEContext, the command context that keeps private answers private.

The contexts are real, and so is discord.py's own Context.send underneath,
which answers a slash command through the interaction's response, then its
followups, and posts a prefix command's answer in the channel. The
interaction is mocked, and so is the channel post (Messageable.send), so the
tests see exactly what would reach Discord and how.
"""

import sys
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, MethodType, ModuleType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.context_managers import Typing
from discord.ext import commands
from discord.ext.commands.context import DeferTyping
from discord.ext.commands.view import StringView

from tle.access import table
from tle.access.context import TLEContext
from tle.access.rules import Decision, Effective, Outcome, Rule, Where, Who
from tle.access.service import AccessService, AccessTree, cache_decision
from tle.access.settings import GuildAccess
from tle.util.discord_common import PrivateAnswerExpired
from tle.util.paginator import paginate

GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
GENERAL_ID = 1_200_000_000_000_000_020
MEMBER_ID = 1_400_000_000_000_000_001

RULES = {
    'ping': Rule(Who.EVERYONE, Where.BOT),
    'clist': Rule(Who.EVERYONE, Where.BOT),
    'clist future': Rule(Who.EVERYONE, Where.BOT),
}
EVERYONE_IN_BOT_CHANNELS = Effective(frozenset({Who.EVERYONE}), Where.BOT)


class Lab(commands.Cog):
    @commands.hybrid_command()
    async def ping(self, ctx: commands.Context[Any]) -> None:
        """Answer."""

    @commands.hybrid_group(fallback='show')
    async def clist(self, ctx: commands.Context[Any]) -> None:
        """A group."""

    @clist.command()
    async def future(self, ctx: commands.Context[Any]) -> None:
        """A subcommand."""


class AccessBot(commands.Bot):
    """A bot that may carry an access service, as TLEBot does."""

    access: AccessService


@pytest.fixture(autouse=True)
def rule_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table, 'RULES', MappingProxyType(RULES))
    monkeypatch.setattr(table, 'TWINS', MappingProxyType({}))


async def make_bot(*, with_access: bool) -> AccessBot:
    bot = AccessBot(
        command_prefix=';',
        intents=discord.Intents.none(),
        help_command=None,
        tree_cls=AccessTree,
    )
    if with_access:
        bot.access = AccessService(bot)
        bot.add_check(bot.access.check)
    await bot.add_cog(Lab())
    return bot


@pytest.fixture
async def bot() -> AsyncIterator[AccessBot]:
    """A bot with an access service, whose check decides on every command."""
    bot = await make_bot(with_access=True)
    yield bot
    await bot.close()


@pytest.fixture
async def plain_bot() -> AsyncIterator[AccessBot]:
    """A bot without an access service."""
    bot = await make_bot(with_access=False)
    yield bot
    await bot.close()


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in the channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers or defers it.
    """

    def __init__(self) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._answer)
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self) -> bool:
        return self.done

    async def _answer(self, **kwargs: Any) -> Any:
        self.done = True
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))

    async def _defer(self, **kwargs: Any) -> None:
        self.done = True


def make_context(
    bot: commands.Bot,
    name: str = 'ping',
    *,
    slash: bool = True,
    expired: bool = False,
    place_id: int = GENERAL_ID,
) -> TLEContext:
    """A context of command ``name``, used by a member in channel ``place_id``.

    With ``slash``, it belongs to an interaction, which has expired with
    ``expired``.
    """
    command = bot.get_command(name)
    assert command is not None
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    author = MagicMock(spec=discord.Member, id=MEMBER_ID, guild=guild)
    author.guild_permissions = discord.Permissions.none()
    author.roles = []
    place = MagicMock(spec=discord.TextChannel, id=place_id, guild=guild)
    message = MagicMock(spec=discord.Message, guild=guild, author=author, channel=place)
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
        view=StringView(''),
        prefix='/' if slash else ';',
        command=command,
        invoked_with=command.name,
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = ctx
    return ctx


def decide(ctx: TLEContext, outcome: Outcome) -> None:
    """Keep ``outcome`` on ``ctx`` as the access check's decision."""
    cache_decision(ctx, Decision(outcome, EVERYONE_IN_BOT_CHANNELS, slash=True))


def response(ctx: TLEContext) -> Response:
    assert ctx.interaction is not None
    answer = ctx.interaction.response
    assert isinstance(answer, Response)
    return answer


def followup(ctx: TLEContext) -> AsyncMock:
    assert ctx.interaction is not None
    return cast(AsyncMock, ctx.interaction.followup.send)


def ephemeral_flags(ctx: TLEContext) -> list[bool]:
    """Whether each answer sent, first the response's then each followup's,
    was private.
    """
    answers = response(ctx).send_message.await_args_list + followup(ctx).await_args_list
    return [call.kwargs['ephemeral'] for call in answers]


def nothing_sent(ctx: TLEContext, posted: AsyncMock) -> None:
    response(ctx).send_message.assert_not_awaited()
    response(ctx).defer.assert_not_awaited()
    followup(ctx).assert_not_awaited()
    posted.assert_not_awaited()


# Prefix commands


async def test_a_prefix_answer_replies_without_pinging(
    bot: AccessBot, posted: AsyncMock
) -> None:
    ctx = make_context(bot, slash=False)

    await ctx.send('hi')

    assert posted.await_args is not None
    assert posted.await_args.kwargs['reference'] is ctx.message
    assert posted.await_args.kwargs['mention_author'] is False


async def test_a_prefix_answer_can_reply_to_another_message(
    bot: AccessBot, posted: AsyncMock
) -> None:
    ctx = make_context(bot, slash=False)
    other = MagicMock(spec=discord.Message)

    await ctx.send('hi', reference=other)

    assert posted.await_args is not None
    assert posted.await_args.kwargs['reference'] is other
    assert posted.await_args.kwargs['mention_author'] is None


async def test_prefix_typing_shows_the_typing_indicator(bot: AccessBot) -> None:
    ctx = make_context(bot, slash=False)

    assert isinstance(ctx.typing(), Typing)


# Slash commands


async def test_a_public_decision_leaves_answers_public(bot: AccessBot) -> None:
    ctx = make_context(bot)
    decide(ctx, Outcome.PUBLIC)

    await ctx.send('first')
    await ctx.send('second')
    await ctx.send('a private one', ephemeral=True)

    assert ephemeral_flags(ctx) == [False, False, True]


async def test_a_private_decision_makes_every_answer_private(bot: AccessBot) -> None:
    ctx = make_context(bot)
    decide(ctx, Outcome.PRIVATE)

    await ctx.send('first')
    await ctx.send('second', embed=discord.Embed(title='more'))
    # Never public, whatever the command asks for.
    await ctx.send('third', ephemeral=False)

    assert ephemeral_flags(ctx) == [True, True, True]
    assert response(ctx).send_message.await_args is not None
    assert response(ctx).send_message.await_args.kwargs['content'] == 'first'


@pytest.mark.parametrize(
    ('outcome', 'asked', 'deferred'),
    [
        (Outcome.PRIVATE, False, True),
        (Outcome.PUBLIC, False, False),
        (Outcome.PUBLIC, True, True),
    ],
    ids=['private decision', 'public decision', 'asked to be private'],
)
async def test_defer_is_private_when_the_answer_will_be(
    bot: AccessBot, outcome: Outcome, asked: bool, deferred: bool
) -> None:
    ctx = make_context(bot)
    decide(ctx, outcome)

    await ctx.defer(ephemeral=asked)
    await ctx.send('later')

    response(ctx).defer.assert_awaited_once_with(ephemeral=deferred)
    response(ctx).send_message.assert_not_awaited()
    # Only the decision makes later answers private. (Discord keeps the first
    # answer after a private deferral private by itself.)
    assert ephemeral_flags(ctx) == [outcome is Outcome.PRIVATE]


@pytest.mark.parametrize(
    ('outcome', 'asked', 'deferred'),
    [
        (Outcome.PRIVATE, False, True),
        (Outcome.PUBLIC, False, False),
        (Outcome.PUBLIC, True, True),
    ],
    ids=['private decision', 'public decision', 'asked to be private'],
)
async def test_typing_defers_privately_when_the_answer_will_be(
    bot: AccessBot, outcome: Outcome, asked: bool, deferred: bool
) -> None:
    ctx = make_context(bot)
    decide(ctx, outcome)

    typing = ctx.typing(ephemeral=asked)
    async with typing:
        pass
    await ctx.send('done')

    # discord.py's DeferTyping, which defers a slash command.
    assert isinstance(typing, DeferTyping)
    assert typing.ephemeral is deferred
    response(ctx).defer.assert_awaited_once_with(ephemeral=deferred)
    # Only the decision makes later answers private. (Discord keeps the first
    # answer after a private deferral private by itself.)
    assert ephemeral_flags(ctx) == [outcome is Outcome.PRIVATE]


async def test_awaiting_typing_defers_too(bot: AccessBot) -> None:
    ctx = make_context(bot)
    decide(ctx, Outcome.PRIVATE)

    await ctx.typing()

    response(ctx).defer.assert_awaited_once_with(ephemeral=True)


# Answers that come too late


@pytest.mark.parametrize(
    ('outcome', 'asked'),
    [(Outcome.PRIVATE, False), (Outcome.PUBLIC, True)],
    ids=['private decision', 'asked to be private'],
)
async def test_a_private_answer_after_the_interaction_expired_is_dropped(
    bot: AccessBot, posted: AsyncMock, outcome: Outcome, asked: bool
) -> None:
    ctx = make_context(bot, expired=True)
    decide(ctx, outcome)

    with pytest.raises(PrivateAnswerExpired):
        await ctx.send('too late', ephemeral=asked)

    # discord.py would have posted it in the channel, for everyone to see.
    nothing_sent(ctx, posted)


async def test_expiry_is_read_from_the_clock_by_discord_py(
    bot: AccessBot, posted: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # discord.py's own Interaction.is_expired: 15 minutes after the command.
    ctx = make_context(bot)
    decide(ctx, Outcome.PRIVATE)
    interaction = cast(MagicMock, ctx.interaction)
    used = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    interaction.expires_at = used + timedelta(minutes=15)
    interaction.is_expired = MethodType(discord.Interaction.is_expired, interaction)
    clock = {'now': used + timedelta(minutes=14, seconds=59)}
    monkeypatch.setattr(discord.utils, 'utcnow', lambda: clock['now'])

    await ctx.send('in time')
    clock['now'] = used + timedelta(minutes=15)
    with pytest.raises(PrivateAnswerExpired):
        await ctx.send('too late')

    assert ephemeral_flags(ctx) == [True]
    posted.assert_not_awaited()


async def test_a_public_answer_after_expiry_goes_to_the_channel_as_before(
    bot: AccessBot, posted: AsyncMock
) -> None:
    ctx = make_context(bot, expired=True)
    decide(ctx, Outcome.PUBLIC)

    await ctx.send('late but public')

    posted.assert_awaited_once()
    response(ctx).send_message.assert_not_awaited()


# Without a decision


async def test_without_a_decision_answers_are_private(bot: AccessBot) -> None:
    # The check didn't decide on these commands, so they fail closed.
    answered, deferred, typed = (make_context(bot) for _ in range(3))

    await answered.send('hi')
    await deferred.defer()
    typing = typed.typing()

    assert ephemeral_flags(answered) == [True]
    response(deferred).defer.assert_awaited_once_with(ephemeral=True)
    assert isinstance(typing, DeferTyping) and typing.ephemeral


async def test_without_an_access_service_answers_are_as_asked(
    plain_bot: AccessBot,
) -> None:
    ctx = make_context(plain_bot)

    await ctx.send('public')
    await ctx.send('private', ephemeral=True)

    assert ephemeral_flags(ctx) == [False, True]


async def test_a_decision_on_another_command_does_not_count(bot: AccessBot) -> None:
    ctx = make_context(bot, 'clist')
    decide(ctx, Outcome.PUBLIC)
    ctx.command = bot.get_command('clist future')

    await ctx.send('hi')

    assert ephemeral_flags(ctx) == [True]


# With the access check


@pytest.mark.parametrize(
    ('place_id', 'private'),
    [(BOT_CHANNEL_ID, False), (GENERAL_ID, True)],
    ids=['bot channel', 'elsewhere'],
)
async def test_the_check_s_decision_decides_who_sees_the_answers(
    bot: AccessBot, place_id: int, private: bool
) -> None:
    await bot.access.change(
        GUILD_ID, lambda _: GuildAccess(bot_channels=frozenset({BOT_CHANNEL_ID}))
    )
    ctx = make_context(bot, place_id=place_id)
    assert ctx.command is not None

    assert await ctx.command.can_run(ctx)
    await ctx.send('first')
    await ctx.send('second')

    assert ephemeral_flags(ctx) == [private, private]


async def test_the_pages_of_a_private_answer_are_private(bot: AccessBot) -> None:
    ctx = make_context(bot)
    decide(ctx, Outcome.PRIVATE)
    pages = [(None, discord.Embed(title=f'Page {i}')) for i in range(3)]

    await paginate(ctx.channel, pages, wait_time=60, ctx=ctx)

    assert ephemeral_flags(ctx) == [True]


# Help


async def test_send_help_goes_to_the_help_module(
    bot: AccessBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = ModuleType('tle.access.help')
    shown = AsyncMock(return_value='shown')
    module.send_help = shown  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'tle.access.help', module)
    ctx = make_context(bot)
    command = bot.get_command('clist')

    assert await ctx.send_help(command) == 'shown'
    assert await ctx.send_help('clist future') == 'shown'
    assert await ctx.send_help() == 'shown'

    assert [call.args for call in shown.await_args_list] == [
        (ctx, command),
        (ctx, 'clist future'),
        (ctx, None),
    ]
