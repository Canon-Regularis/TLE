"""Tests for the CacheControl cog (tle.cogs.cache_control): the bot owner's
commands that update the bot's Codeforces caches now.

The tests call each command's callback, as discord.py does once the access
check has let the command run, with a real context whose replies are
recorded. The caches are mocked.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle.cogs.cache_control import (
    NOT_A_PROBLEMSETS_MODE,
    NOT_A_RATING_CHANGES_MODE,
    UNKNOWN_CONTEST_TEXT,
    CacheControl,
)

# A contest in the bot's Codeforces contest list.
CONTEST_ID = 1950
COMMANDS = (
    'cache',
    'cache contests',
    'cache problems',
    'cache ratingchanges',
    'cache problemsets',
)


@pytest.fixture
def bot() -> MagicMock:
    """A bot with Codeforces caches: each fetch says how much it fetched."""
    bot = MagicMock(spec=commands.Bot)
    bot.cf_cache = MagicMock()
    bot.cf_cache.contest_cache.contest_by_id = {CONTEST_ID: MagicMock()}
    changes = bot.cf_cache.rating_changes_cache
    changes.fetch_contest = AsyncMock(return_value=7)
    changes.fetch_all_contests = AsyncMock(return_value=70)
    changes.fetch_missing_contests = AsyncMock(return_value=17)
    problemsets = bot.cf_cache.problemset_cache
    problemsets.update_for_contest = AsyncMock(return_value=6)
    problemsets.update_for_all = AsyncMock(return_value=60)
    return bot


@pytest.fixture
def cog(bot: MagicMock) -> CacheControl:
    return CacheControl(bot)


@pytest.fixture
def ctx(bot: MagicMock) -> commands.Context[Any]:
    """A real context of a prefix command, whose replies are recorded by an
    ``AsyncMock`` in place of ``send``: /cache has no slash form.
    """
    message = MagicMock(spec=discord.Message)
    context: commands.Context[Any] = commands.Context(
        message=message, bot=bot, view=StringView(''), prefix=';'
    )
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def callback(name: str) -> Callable[..., Awaitable[None]]:
    """The callback of /cache's command ``name``, which discord.py calls with
    the cog, the context and the parsed arguments.
    """
    found: Callable[..., Awaitable[None]] = getattr(CacheControl, name).callback
    return found


def replies(ctx: commands.Context[Any]) -> list[str]:
    send: AsyncMock = ctx.send  # type: ignore[assignment]
    return [call.args[0] for call in send.await_args_list]


def test_only_the_access_rules_decide_who_uses_the_commands(
    cog: CacheControl,
) -> None:
    # No role check of their own: the caches are shared by every server, so
    # the commands are for the bot owner, whatever their roles.
    checks = {command.qualified_name: command.checks for command in cog.walk_commands()}
    assert checks == {name: [] for name in COMMANDS}


def test_each_command_says_what_it_does(cog: CacheControl) -> None:
    # /help lists each command with its brief, and its page shows the text.
    for command in cog.walk_commands():
        assert command.brief, command.qualified_name
        assert command.help, command.qualified_name


@pytest.mark.parametrize(
    ('argument', 'fetched', 'replied'),
    [
        ('missing', 'fetch_missing_contests', 17),
        ('all', 'fetch_all_contests', 70),
    ],
)
async def test_ratingchanges_fetches_the_contests_asked_for(
    cog: CacheControl,
    bot: MagicMock,
    ctx: commands.Context[Any],
    argument: str,
    fetched: str,
    replied: int,
) -> None:
    await callback('ratingchanges')(cog, ctx, argument)

    getattr(bot.cf_cache.rating_changes_cache, fetched).assert_awaited_once_with()
    assert f'Done, fetched {replied} changes and recached handle ratings' in replies(
        ctx
    )


async def test_ratingchanges_fetches_one_contest(
    cog: CacheControl, bot: MagicMock, ctx: commands.Context[Any]
) -> None:
    await callback('ratingchanges')(cog, ctx, str(CONTEST_ID))

    bot.cf_cache.rating_changes_cache.fetch_contest.assert_awaited_once_with(CONTEST_ID)
    assert replies(ctx)[:2] == [
        'Running...',
        'Done, fetched 7 changes and recached handle ratings',
    ]
    assert replies(ctx)[2].startswith('Completed in ')


async def test_problemsets_fetches_one_contest_or_all(
    cog: CacheControl, bot: MagicMock, ctx: commands.Context[Any]
) -> None:
    problemsets = bot.cf_cache.problemset_cache

    await callback('problemsets')(cog, ctx, str(CONTEST_ID))
    await callback('problemsets')(cog, ctx, 'all')

    problemsets.update_for_contest.assert_awaited_once_with(CONTEST_ID)
    problemsets.update_for_all.assert_awaited_once_with()
    assert 'Done, fetched 6 problems' in replies(ctx)
    assert 'Done, fetched 60 problems' in replies(ctx)


@pytest.mark.parametrize(
    ('name', 'argument', 'text'),
    [
        ('ratingchanges', '19x50', NOT_A_RATING_CHANGES_MODE),
        ('ratingchanges', str(CONTEST_ID + 1), UNKNOWN_CONTEST_TEXT),
        ('problemsets', 'everything', NOT_A_PROBLEMSETS_MODE),
        ('problemsets', str(CONTEST_ID + 1), UNKNOWN_CONTEST_TEXT),
    ],
    ids=[
        'ratingchanges, not a contest ID',
        'ratingchanges, an unknown contest',
        'problemsets, not a contest ID',
        'problemsets, an unknown contest',
    ],
)
async def test_a_contest_that_cannot_be_fetched_gets_a_polite_answer(
    cog: CacheControl,
    bot: MagicMock,
    ctx: commands.Context[Any],
    name: str,
    argument: str,
    text: str,
) -> None:
    # Not a silent 'Completed', nor an error logged as unexpected: a user
    # error, which the error handler answers with its text.
    with pytest.raises(commands.BadArgument) as refused:
        await callback(name)(cog, ctx, argument)

    assert str(refused.value) == text
    assert replies(ctx) == ['Running...']
    bot.cf_cache.rating_changes_cache.fetch_contest.assert_not_awaited()
    bot.cf_cache.problemset_cache.update_for_contest.assert_not_awaited()


def test_the_answers_to_a_bad_contest_say_what_to_give() -> None:
    assert NOT_A_RATING_CHANGES_MODE == 'Give a contest ID, `all` or `missing`.'
    assert NOT_A_PROBLEMSETS_MODE == 'Give a contest ID or `all`.'
    assert UNKNOWN_CONTEST_TEXT == (
        "No contest in the bot's Codeforces contest list has that ID. If the "
        'contest is new, reload the list with `;cache contests` first.'
    )


def test_the_help_and_options_write_id_in_capitals(cog: CacheControl) -> None:
    # As the rest of the bot's texts, the README and .env.example do.
    texts = []
    for command in cog.walk_commands():
        texts.append(command.help or '')
        app = getattr(command, 'app_command', None)
        if isinstance(app, app_commands.Command):
            texts += [parameter.description for parameter in app.parameters]

    assert [text for text in texts if 'contest ID' in text]
    assert [text for text in texts if re.search(r'\bid\b', text)] == []
