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

from tle.access.help import split_help
from tle.cogs.cache_control import (
    NOT_A_PROBLEMSETS_MODE,
    NOT_A_RATING_CHANGES_MODE,
    PROBLEMSETS_FAILED_ONE_TEXT,
    PROBLEMSETS_FAILED_TEXT,
    PROBLEMSETS_SAVED_ONE_TEXT,
    PROBLEMSETS_SAVED_TEXT,
    UNKNOWN_CONTEST_TEXT,
    CacheControl,
)

# codeforces_common is imported before tle.util.cache: it is the module that
# brings the cache in, and importing the cache first runs into their cycle.
from tle.util import codeforces_api as cf, codeforces_common  # noqa: F401
from tle.util.cache.problemset import ProblemsetUpdate

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
    problemsets.update_for_all = AsyncMock(
        return_value=ProblemsetUpdate(problems=60, failed=[])
    )
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
    assert replies(ctx)[:2] == ['Running...', 'Done, fetched 6 problems']
    assert replies(ctx)[3:6] == [
        'Running...',
        'This will take a while',
        'Done, saved 60 new or changed problems',
    ]
    # Nothing failed, so nothing more is said.
    assert len(replies(ctx)) == 7
    assert replies(ctx)[6].startswith('Completed in ')


@pytest.mark.parametrize(
    ('problems', 'reply'),
    [
        (0, 'Done, saved 0 new or changed problems'),
        (1, 'Done, saved 1 new or changed problem'),
        (2, 'Done, saved 2 new or changed problems'),
    ],
    ids=['none', 'one', 'two'],
)
async def test_problemsets_all_says_how_many_problems_it_saved(
    cog: CacheControl,
    bot: MagicMock,
    ctx: commands.Context[Any],
    problems: int,
    reply: str,
) -> None:
    # After the hourly update, a run usually saves none, and one rating that
    # changed since saves one.
    bot.cf_cache.problemset_cache.update_for_all.return_value = ProblemsetUpdate(
        problems=problems, failed=[]
    )

    await callback('problemsets')(cog, ctx, 'all')

    assert replies(ctx)[:3] == ['Running...', 'This will take a while', reply]
    assert replies(ctx)[3].startswith('Completed in ')
    assert len(replies(ctx)) == 4


def test_the_answers_about_saved_problems_count_them() -> None:
    assert PROBLEMSETS_SAVED_TEXT.format(count=3) == (
        'Done, saved 3 new or changed problems'
    )
    assert PROBLEMSETS_SAVED_ONE_TEXT == 'Done, saved 1 new or changed problem'


@pytest.mark.parametrize(
    ('failed', 'reply'),
    [
        (
            [2004],
            "Codeforces didn't give the problems of contest 2004, so the bot "
            'still lacks them.',
        ),
        (
            [2004, 2102],
            "Codeforces didn't give the problems of 2 contests, so the bot still "
            'lacks them: 2004, 2102.',
        ),
        (
            list(range(1001, 1021)),
            "Codeforces didn't give the problems of 20 contests, so the bot still "
            'lacks them: ' + ', '.join(map(str, range(1001, 1021))) + '.',
        ),
        (
            list(range(1001, 1026)),
            "Codeforces didn't give the problems of 25 contests, so the bot still "
            'lacks them: ' + ', '.join(map(str, range(1001, 1021))) + ' and 5 more.',
        ),
    ],
    ids=['one contest', 'two contests', '20 contests', 'more than 20 contests'],
)
async def test_problemsets_all_names_the_contests_codeforces_failed_for(
    cog: CacheControl,
    bot: MagicMock,
    ctx: commands.Context[Any],
    failed: list[int],
    reply: str,
) -> None:
    bot.cf_cache.problemset_cache.update_for_all.return_value = ProblemsetUpdate(
        problems=3, failed=failed
    )

    await callback('problemsets')(cog, ctx, 'all')

    assert replies(ctx)[2:4] == ['Done, saved 3 new or changed problems', reply]
    assert replies(ctx)[4].startswith('Completed in ')
    assert len(replies(ctx)) == 5


@pytest.mark.parametrize('argument', [str(CONTEST_ID), 'all'])
async def test_problemsets_passes_codeforces_errors_on(
    cog: CacheControl, bot: MagicMock, ctx: commands.Context[Any], argument: str
) -> None:
    # The error handler answers with Codeforces' error, such as the problem
    # list's; the cache kept the problems it had, so the command says nothing
    # was done.
    failure = cf.TrueApiError('HTTP Error 503, Service Unavailable')
    problemsets = bot.cf_cache.problemset_cache
    problemsets.update_for_contest.side_effect = failure
    problemsets.update_for_all.side_effect = failure

    with pytest.raises(cf.TrueApiError) as raised:
        await callback('problemsets')(cog, ctx, argument)

    assert raised.value is failure
    if argument == 'all':
        assert replies(ctx) == ['Running...', 'This will take a while']
    else:
        assert replies(ctx) == ['Running...']


def test_the_answers_about_failed_contests_say_what_the_bot_lacks() -> None:
    assert PROBLEMSETS_FAILED_TEXT == (
        "Codeforces didn't give the problems of {count} contests, so the bot still "
        'lacks them: {ids}.'
    )
    assert PROBLEMSETS_FAILED_ONE_TEXT == (
        "Codeforces didn't give the problems of contest {id}, so the bot still "
        'lacks them.'
    )


def test_problemsets_says_how_the_bot_fills_the_problems_in(cog: CacheControl) -> None:
    (command,) = [
        command
        for command in cog.walk_commands()
        if command.qualified_name == 'cache problemsets'
    ]
    description, examples = split_help(command.help)

    assert ' '.join(description.split()) == (
        'Fetch the problems of one Codeforces contest again, or with `all` those '
        'of every finished contest that the bot lacks. Every hour the bot updates '
        "them from Codeforces' problem list itself, and fetches the standings of "
        'new contests that the list leaves incomplete.'
    )
    assert examples == (';cache problemsets 1950', ';cache problemsets all')


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
