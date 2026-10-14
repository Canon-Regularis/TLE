"""Tests for the Logging cog (tle.cogs.logging), which posts the bot's
warnings and errors in the log channel.

The log channel and the bot are mocked, and a fake clock tells the time;
records go through the cog's queue and its task, as when the bot runs.
"""

import asyncio
import contextlib
import logging
import random
import threading
import traceback
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import datetime, timezone
from types import TracebackType
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock, call

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView
from discord.gateway import KeepAliveHandler

from tle.cogs import logging as logging_cog
from tle.cogs.logging import Logging, RepeatFilter, setup
from tle.util import discord_common
from tle.util.ansi import ANSI_RED, ANSI_YELLOW, RESET

# Real snowflakes are 64-bit, so use big ones.
LOG_CHANNEL_ID = 1_200_000_000_000_000_001
# Seconds the cog's task may take, many times over.
TASK_TIMEOUT = 5
# How long the first window of repeats lasts and how long a record is
# remembered; and how long the cog stops posting after Discord refused a post.
HOUR = 60 * 60
DAY = 24 * HOUR
TEN_MINUTES = 10 * 60
# Discord's limit on the length of a message.
MESSAGE_LIMIT = 2000
# The logger of the cog's own records.
COG_LOGGER = 'tle.cogs.logging'
# discord.py's warning from its heartbeat thread, logged with arguments.
HEARTBEAT_BLOCKED = 'Shard ID %s heartbeat blocked for more than %s seconds.'
# The loop thread's stack in that warning, stopped on a line with a '%' in it,
# which the warning's arguments then don't fit.
STACK_ON_A_PERCENT = (
    '  File "/app/tle/cogs/handles.py", line 198, in _make_profile_image\n'
    '    draw_bg(y, i % 2)\n'
)
# The KCPC scheduler's warning when a job fails, logged with its name and slot.
SLOT_FAILED = 'Job %s failed on its %s slot'
# TLE's error handler's message for a bug in the command ranklist, and TLE's
# timed tasks' for a bug in the ranklist monitor.
RANKLIST_FAILED = 'Ignoring exception in command ranklist:'
MONITOR_FAILED = 'Exception in task `ranklist.monitor`, ignoring.'
# TLE's client's warning when Codeforces refuses the standings of a contest.
STANDINGS_REFUSED = (
    'Query to CF API contest.standings failed: HTTP Error 400, contestId: Non-gym'
    ' contest standings for non-admin users are available only via anonymous GET'
    ' requests with no extra parameters:'
    ' https://codeforces.com/api/contest.standings?contestId={id}'
)
# What a record too long for one post ends with, in the same message.
CUT_NOTE = '`Check logs for full stack trace`'

E = TypeVar('E', bound=discord.HTTPException)
ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]


class FakeClock:
    """A clock that moves only when told to. It notes the threads that read
    it: the repeat filter reads it as it decides on each record.
    """

    def __init__(self) -> None:
        self.now = 1_000.0
        self.threads: set[int] = set()

    def __call__(self) -> float:
        self.threads.add(threading.get_ident())
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def record(
    message: str,
    *args: object,
    level: int = logging.ERROR,
    name: str = 'tle',
    exc_info: ExcInfo | tuple[None, None, None] | None = None,
    **extra: str,
) -> logging.LogRecord:
    """A record, an error unless ``level`` says otherwise, with the ``extra``
    that the error handler adds.
    """
    found = logging.LogRecord(name, level, __file__, 1, message, args, exc_info)
    for key, value in extra.items():
        setattr(found, key, value)
    return found


def raised(error: Exception) -> ExcInfo:
    """``error`` with its traceback, as a record of it holds it."""
    try:
        raise error
    except Exception as exc:
        # As if raised while no other error was handled. On Python 3.10,
        # pytest-asyncio runs a test in a pure-Python task, which resumes a
        # test that has awaited a cancelled task while still handling its
        # CancelledError; an error raised then would take that as context.
        exc.__context__ = None
        return type(exc), exc, exc.__traceback__


def chained_failure() -> ExcInfo:
    """A failure three causes deep, as when a job finds Codeforces down: the
    first cause with a long traceback, and a RuntimeError last.
    """

    # Frames that alternate, which Python doesn't fold as repeats.
    def ping(depth: int) -> None:
        if depth:
            pong(depth - 1)
        raise ValueError('HTTP 502 without JSON')

    def pong(depth: int) -> None:
        ping(depth)

    def refresh() -> None:
        try:
            try:
                ping(30)
            except ValueError as exc:
                raise ConnectionError('Codeforces failed 3 times') from exc
        except ConnectionError as exc:
            raise LookupError('Could not refresh the codeforces problem list') from exc

    try:
        try:
            refresh()
        except LookupError as exc:
            raise RuntimeError(
                'Could not refresh the problem lists of: codeforces'
            ) from exc
    except RuntimeError as exc:
        return type(exc), exc, exc.__traceback__
    raise AssertionError('The refresh raised nothing')


def caught(bug: Callable[[], object]) -> Exception:
    """What ``bug`` raises, with its traceback."""
    try:
        bug()
    except Exception as exc:
        return exc
    raise AssertionError(f'{bug.__name__} raised nothing')


def raised_from(error: BaseException, cause: BaseException) -> ExcInfo:
    """``error`` raised from ``cause``, as a record of it holds it."""
    try:
        raise error from cause
    except BaseException as exc:
        return type(exc), exc, exc.__traceback__


def missing_rows() -> list[int]:
    """A bug: the KeyError of a contest without rows, raised from this line
    each time.
    """
    rows: dict[int, list[int]] = {}
    return rows[2271]


def missing_name() -> str:
    """Another bug: a KeyError too, but raised from another line."""
    names: dict[int, str] = {}
    return names[2271]


def no_rows() -> float:
    """Another bug: a ZeroDivisionError."""
    rows: list[int] = []
    return sum(rows) / len(rows)


def prefix_failed(bug: Callable[[], object]) -> ExcInfo:
    """What TLE's error handler logs when ``bug`` raises in a prefix command:
    discord.py's CommandInvokeError, raised from the bug's error.
    """
    error = caught(bug)
    return raised_from(commands.CommandInvokeError(error), error)


def slash_failed(bug: Callable[[], object]) -> ExcInfo:
    """The same for a hybrid command run as a slash command: discord.py's
    HybridCommandError, caused by the app command's CommandInvokeError, which
    was raised from the bug's error.
    """
    error = caught(bug)
    command = MagicMock(spec=app_commands.Command)
    command.name = 'ranklist'
    _, inner, _ = raised_from(app_commands.CommandInvokeError(command, error), error)
    outer = commands.HybridCommandError(inner)
    outer.__cause__ = inner
    outer = outer.with_traceback(inner.__traceback__)
    return type(outer), outer, outer.__traceback__


def refresh_failed(bug: Callable[[], object]) -> ExcInfo:
    """What KCPC's problem refresh raises when ``bug`` raises in it, and its
    scheduler logs: a RuntimeError raised from the bug's error.
    """
    error = caught(bug)
    return raised_from(
        RuntimeError('Could not refresh the problem lists of: codeforces'), error
    )


def failed_while(handling: Exception) -> ExcInfo:
    """A ValueError raised from the same line each time, while ``handling``
    was handled: ``handling`` is its context, not its cause.
    """
    try:
        try:
            raise handling
        except Exception:
            raise ValueError('No such contest')
    except ValueError as exc:
        return type(exc), exc, exc.__traceback__


def blocked_heartbeat(total: int) -> logging.LogRecord:
    """discord.py's warning that the loop thread has been blocked for
    ``total`` seconds, with the thread's stack in its template. That stack is
    stopped on a line with a '%' in it, so the arguments don't fit.
    """
    heartbeat = KeepAliveHandler(ws=MagicMock(), interval=41.25, shard_id=None)
    template = (
        f'{heartbeat.block_msg}\n'
        f'Loop thread traceback (most recent call last):\n{STACK_ON_A_PERCENT}'
    )
    return record(template, None, total, level=logging.WARNING, name='discord.gateway')


def monitor_failed() -> logging.LogRecord:
    """A bug in a timed task, with its traceback, as TLE's tasks log it."""
    return record(MONITOR_FAILED, exc_info=raised(KeyError(2271)))


def http_error(cls: type[E], status: int, text: str) -> E:
    """What discord.py raises when Discord answers ``status``."""
    return cls(MagicMock(status=status, reason='Reason'), text)


def text_channel() -> MagicMock:
    return MagicMock(spec=discord.TextChannel)


def make_bot(channel: MagicMock | None) -> MagicMock:
    """A bot whose cache has ``channel`` as the log channel; without one, the
    partial channel it makes from the id is ``partial_channel(bot)``.
    """
    bot = MagicMock(spec=commands.Bot)
    bot.get_channel.return_value = channel
    bot.get_partial_messageable.return_value = MagicMock(
        spec=discord.PartialMessageable
    )
    return bot


def partial_channel(bot: MagicMock) -> MagicMock:
    """The partial channel ``bot`` makes from the log channel's id."""
    partial: MagicMock = bot.get_partial_messageable.return_value
    return partial


def make_cog(bot: MagicMock, clock: FakeClock) -> Logging:
    """The cog, showing records by their message alone."""
    cog = Logging(bot, LOG_CHANNEL_ID, clock=clock)
    cog.setFormatter(logging.Formatter('{message}', style='{'))
    return cog


@contextlib.asynccontextmanager
async def running(cog: Logging) -> AsyncIterator[None]:
    """The cog's task, running until the block ends."""
    task = asyncio.create_task(cog._log_task())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def drain(cog: Logging) -> None:
    """Run the cog's task until it has posted every record queued."""
    async with running(cog):
        await asyncio.wait_for(cog.queue.join(), TASK_TIMEOUT)


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait until ``condition`` holds, for TASK_TIMEOUT seconds at most."""

    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), TASK_TIMEOUT)


async def invoke(bot: commands.Bot, typed: str, message_id: int) -> None:
    """Run the prefix command ``typed`` as a member types it, in a message
    with ``message_id``, and wait for the error handler, which discord.py
    runs as a task.
    """
    message = MagicMock(spec=discord.Message)
    message.content = f';{typed}'
    message.jump_url = f'https://discord.com/channels/1/2/{message_id}'
    view = StringView(typed)
    invoked = view.get_word()
    ctx = commands.Context(
        message=message, bot=bot, view=view, prefix=';', invoked_with=invoked
    )
    ctx.command = bot.get_command(invoked)
    ctx.send = AsyncMock()  # type: ignore[method-assign]
    await bot.invoke(ctx)
    pending = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith('discord.py:')
    ]
    await asyncio.gather(*pending)


def posted(channel: MagicMock) -> list[str]:
    """What the cog sent in ``channel``, in order, failed sends included."""
    return [post.args[0] for post in channel.send.await_args_list]


def pings_nobody(channel: MagicMock) -> bool:
    none = discord.AllowedMentions.none().to_dict()
    return all(
        post.kwargs['allowed_mentions'].to_dict() == none
        for post in channel.send.await_args_list
    )


def block(text: str, color: str = ANSI_RED) -> str:
    """How the log channel shows a record that reads ``text``."""
    return f'```ansi\n{color}{text}{RESET}```'


def repeated(count: int, period: str = 'hour') -> str:
    """The line above the summary of ``count`` repeats held over ``period``."""
    times = 'time' if count == 1 else 'times'
    return f'Repeated {count} more {times} in the last {period}; the last:'


def dropped(count: int) -> str:
    """The post that counts the records the cog couldn't post."""
    if count == 1:
        return '1 log record could not be posted here; the log file has it.'
    return f'{count} log records could not be posted here; the log file has them.'


def timeline(
    clock: FakeClock, times: Iterable[float], hours: int
) -> list[tuple[int, int, int]]:
    """What the log channel shows, over ``hours``, of a record logged at each
    of ``times`` (seconds from now): each full post as (hour, 0, 0) and each
    summary as (hour, count, period in hours), the hours rounded.

    The filter looks for the summaries due a minute after the task's last
    turn, as the cog's task does. While every later record is held, the
    looks come every minute after the first post, which takes a tenth of a
    second; the tenth of a second that each summary's post adds is left out.
    """
    times = list(times)
    repeats = RepeatFilter(clock=clock)
    start = clock.now
    first = min(times) + 0.1
    looks = [first + 60 * minute for minute in range(hours * 60)]
    events = sorted([(at, False) for at in times] + [(at, True) for at in looks])
    shown = []
    for at, look in events:
        clock.now = start + at
        if not look:
            if repeats.admit(record('Standings of contest 2271 failed')):
                shown.append((round(at / HOUR), 0, 0))
            continue
        for count, period, _ in repeats.due():
            shown.append((round(at / HOUR), count, round(period / HOUR)))
    return shown


# Posts


async def test_the_log_channel_pings_nobody() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    bot = MagicMock(spec=commands.Bot)
    bot.get_channel.return_value = channel
    cog = Logging(bot, LOG_CHANNEL_ID)
    cog.setFormatter(logging.Formatter('{message}', style='{'))
    # A failed command that names members, a role and @everyone, with a
    # message too long for one post.
    cog.emit(
        record(
            '@everyone ' + 'x' * 2500,
            message_content=';duel challenge <@1234> <@&5678> @everyone',
            jump_url='https://discord.com/channels/1/2/3',
        )
    )
    cog.emit(record('The next one'))

    await drain(cog)

    # The command, the message cut short with the note that it was, and the
    # next record.
    posts = channel.send.await_args_list
    texts = [post.args[0] for post in posts]
    assert texts[0] == (
        'Original Command: ;duel challenge <@1234> <@&5678> @everyone\n'
        'Jump Url: https://discord.com/channels/1/2/3'
    )
    assert texts[1].startswith(f'```ansi\n{ANSI_RED}@everyone xxx')
    assert len(texts[1]) == 2000
    assert texts[1].endswith(f'xxx{RESET}```\n`Check logs for full stack trace`')
    assert texts[2] == block('The next one')
    assert len(posts) == 3
    for post in posts:
        mentions = post.kwargs['allowed_mentions']
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()


async def test_a_long_command_is_cut_to_fit_one_post(clock: FakeClock) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    # Members with Nitro can send 4000 characters.
    cog.emit(record('Command failed', message_content=';' + 'x' * 3999))

    await drain(cog)

    command, error = posted(channel)
    assert command == ('Original Command: ;' + 'x' * 3999)[:2000]
    assert error == block('Command failed')


async def test_a_long_traceback_keeps_its_first_line_and_its_last_exception(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    failure = record(
        SLOT_FAILED, 'problems.refresh', '23:00', exc_info=chained_failure()
    )
    full = cog.format(failure)
    assert len(full) > 2 * MESSAGE_LIMIT
    cog.emit(failure)

    await drain(cog)

    # One message: the first line, a line to show the cut, and the end.
    [post] = posted(channel)
    assert len(post) <= MESSAGE_LIMIT
    start = f'```ansi\n{ANSI_RED}Job problems.refresh failed on its 23:00 slot\n…\n'
    finish = f'{RESET}```\n{CUT_NOTE}'
    assert post.startswith(start)
    assert post.endswith(
        'RuntimeError: Could not refresh the problem lists of: codeforces' + finish
    )
    # The end is the end of the text, from the start of a line, and fills
    # the message as far as whole lines allow.
    end = post[len(start) : -len(finish)]
    assert full.endswith('\n' + end)
    assert 'The above exception was the direct cause' in end
    line_before = full[: -len(end) - 1].rsplit('\n', 1)[-1]
    assert len(post) + len(line_before) + 1 > MESSAGE_LIMIT


async def test_a_record_that_just_fits_is_posted_whole(clock: FakeClock) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    fits = 'x' * (MESSAGE_LIMIT - len(block('')))
    cog.emit(record(fits))
    cog.emit(record(fits + 'y'))

    await drain(cog)

    whole, cut = posted(channel)
    assert whole == block(fits)
    assert len(whole) == MESSAGE_LIMIT
    assert cut.endswith(f'xy{RESET}```\n{CUT_NOTE}')
    assert len(cut) == MESSAGE_LIMIT


# Repeats


def test_repeats_are_held_then_summed_up_when_the_hour_is_up(
    clock: FakeClock,
) -> None:
    repeats = RepeatFilter(clock=clock)

    assert repeats.admit(record(HEARTBEAT_BLOCKED, 0, 10, level=logging.WARNING))
    clock.advance(10)
    assert not repeats.admit(record(HEARTBEAT_BLOCKED, 0, 20, level=logging.WARNING))
    clock.advance(10)
    assert not repeats.admit(record(HEARTBEAT_BLOCKED, 0, 30, level=logging.WARNING))
    clock.advance(HOUR - 21)
    assert repeats.due() == []
    clock.advance(1)
    [(count, period, last)] = repeats.due()

    # The two repeats, the hour they came in, and the last of them.
    assert count == 2
    assert period == HOUR
    assert last.getMessage() == 'Shard ID 0 heartbeat blocked for more than 30 seconds.'
    # The next window has begun, holding nothing yet.
    assert repeats.due() == []
    assert not repeats.admit(record(HEARTBEAT_BLOCKED, 0, 40, level=logging.WARNING))


def test_a_record_that_keeps_repeating_is_summed_up_once_an_hour(
    clock: FakeClock,
) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))
    summaries = []

    # A repeat every minute for three hours, after a look at the summaries
    # due, as the cog's task looks at least every minute.
    for _ in range(3 * 60):
        clock.advance(60)
        summaries += [(count, period) for count, period, _ in repeats.due()]
        assert not repeats.admit(record('Codeforces is down'))

    assert summaries == [(59, HOUR), (60, HOUR), (60, HOUR)]


def test_an_hourly_record_is_summed_up_after_1_3_7_15_and_31_hours_then_daily(
    clock: FakeClock,
) -> None:
    # A timed task that waits an hour after each run, which takes 4 seconds,
    # and logs the same warning each time, for three days.
    shown = timeline(clock, [run * (HOUR + 4) for run in range(72)], hours=72)

    # Posted in full, then summed up as the windows double, up to a day.
    assert shown == [
        (0, 0, 0),
        (1, 1, 1),
        (3, 2, 2),
        (7, 4, 4),
        (15, 8, 8),
        (31, 15, 16),
        (55, 24, 24),
    ]


def test_an_hourly_record_with_jitter_is_summed_up_as_rarely(
    clock: FakeClock,
) -> None:
    # A warning logged on the hour, give or take half a second.
    jitter = random.Random(2271)
    times = [1 + run * HOUR + jitter.uniform(-0.5, 0.5) for run in range(72)]

    shown = timeline(clock, times, hours=72)

    assert shown == [
        (0, 0, 0),
        (1, 1, 1),
        (3, 2, 2),
        (7, 4, 4),
        (15, 8, 8),
        (31, 16, 16),
        (55, 24, 24),
    ]


@pytest.mark.parametrize('minutes', [1, 2])
def test_a_flood_is_summed_up_every_hour(minutes: int, clock: FakeClock) -> None:
    shown = timeline(clock, range(0, DAY, minutes * 60), hours=24)

    per_hour = 60 // minutes
    assert shown == [(0, 0, 0)] + [(hour, per_hour, 1) for hour in range(1, 24)]


def test_a_flood_after_a_quiet_spell_is_summed_up_every_hour_again(
    clock: FakeClock,
) -> None:
    # Hourly for three hours, then every minute until a quarter past eight.
    hourly = [run * (HOUR + 4) for run in range(4)]
    flood = range(hourly[-1] + 60, 8 * HOUR + 15 * 60, 60)

    shown = timeline(clock, [*hourly, *flood], hours=10)

    # The window grew to 4 hours, then shrinks back to 1.
    assert [(hour, period) for hour, _, period in shown] == [
        (0, 0),
        (1, 1),
        (3, 2),
        (7, 4),
        (8, 1),
        (9, 1),
    ]


@pytest.mark.parametrize('held, next_window', [(1, 2 * HOUR), (2, HOUR)])
def test_a_window_doubles_after_fewer_than_two_repeats_an_hour(
    held: int, next_window: int, clock: FakeClock
) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))
    for _ in range(held):
        assert not repeats.admit(record('Codeforces is down'))
    clock.advance(HOUR)
    assert [count for count, _, _ in repeats.due()] == [held]

    # The next repeat is summed up when the next window ends, not before.
    assert not repeats.admit(record('Codeforces is down'))
    clock.advance(next_window - 1)
    assert repeats.due() == []
    clock.advance(1)
    assert [count for count, _, _ in repeats.due()] == [1]


def test_a_late_repeat_is_summed_up_at_the_next_look_over_the_time_it_took(
    clock: FakeClock,
) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))

    # The window ends with nothing held, and stays open.
    clock.advance(HOUR)
    assert repeats.due() == []
    assert len(repeats) == 1
    # Five hours later the record comes back: it is held, not posted again,
    # and the next look sums it up.
    clock.advance(5 * HOUR)
    assert not repeats.admit(record('Codeforces is down'))
    clock.advance(60)
    [(count, period, last)] = repeats.due()

    assert count == 1
    assert period == 6 * HOUR + 60
    assert last.getMessage() == 'Codeforces is down'


@pytest.mark.parametrize('quiet, posted', [(DAY - 1, False), (DAY, True)])
def test_a_record_unseen_for_a_day_is_posted_in_full_again(
    quiet: int, posted: bool, clock: FakeClock
) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))
    # A repeat, summed up an hour later.
    clock.advance(1)
    assert not repeats.admit(record('Codeforces is down'))
    clock.advance(HOUR)
    assert [count for count, _, _ in repeats.due()] == [1]

    # Then nothing, for ``quiet`` seconds after the repeat.
    clock.advance(quiet - HOUR)

    assert repeats.admit(record('Codeforces is down')) is posted


def test_a_record_unseen_for_a_day_is_forgotten(clock: FakeClock) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))
    clock.advance(HOUR)
    assert repeats.admit(record('AtCoder is down'))

    clock.advance(DAY - HOUR - 1)
    assert repeats.due() == []
    assert len(repeats) == 2
    clock.advance(1)
    assert repeats.due() == []
    assert len(repeats) == 1
    clock.advance(HOUR)
    assert repeats.due() == []
    assert len(repeats) == 0


def test_a_repeat_after_the_hour_joins_the_summary_still_due(
    clock: FakeClock,
) -> None:
    repeats = RepeatFilter(clock=clock)
    assert repeats.admit(record('Codeforces is down'))
    clock.advance(1)
    assert not repeats.admit(record('Codeforces is down'))
    clock.advance(HOUR)

    # The hour is up, but its summary isn't out yet: it counts this one too.
    assert not repeats.admit(record('Codeforces is down'))
    [(count, period, _)] = repeats.due()
    assert count == 2
    assert period == HOUR + 1


def slot(hour: int) -> datetime:
    """A slot of a KCPC job, as the scheduler logs it."""
    return datetime(2026, 10, 12, hour, tzinfo=timezone.utc)


REPEATS = {
    'the same message': (record('Codeforces is down'), record('Codeforces is down')),
    'the same text with other ids': (
        record('Standings of contest 2271 failed'),
        record('Standings of contest 2273 failed'),
    ),
    'a template with other numbers': (
        record(HEARTBEAT_BLOCKED, 0, 10),
        record(HEARTBEAT_BLOCKED, 0, 20),
    ),
    'a job failing on another slot': (
        record(SLOT_FAILED, 'problems.refresh', slot(23), level=logging.WARNING),
        record(SLOT_FAILED, 'problems.refresh', slot(0), level=logging.WARNING),
    ),
    # Formatting them would raise.
    'a template short of arguments': (
        record('Rating changes of %s and %s', 'tourist'),
        record('Rating changes of %s and %s', 'tourist'),
    ),
    'the same first line, whatever follows': (
        record('Codeforces is down\nsince 23:00'),
        record('Codeforces is down\nfor an hour now'),
    ),
    'the same type of exception, whatever it says': (
        record('Query failed', exc_info=raised(ValueError('one'))),
        record('Query failed', exc_info=raised(ValueError('two'))),
    ),
    'the same bug, in a prefix command and in a slash command': (
        record(RANKLIST_FAILED, exc_info=prefix_failed(missing_rows)),
        record(RANKLIST_FAILED, exc_info=slash_failed(missing_rows)),
    ),
    # The error it was raised while handling isn't its cause.
    'the same error, while handling other errors': (
        record('Query failed', exc_info=failed_while(KeyError(2271))),
        record('Query failed', exc_info=failed_while(TypeError(2271))),
    ),
    'no exception, as logged outside an except block': (
        record('Query failed', exc_info=(None, None, None)),
        record('Query failed'),
    ),
}
NOT_REPEATS = {
    'another message': (record('Codeforces is down'), record('AtCoder is down')),
    'the same template with other words': (
        record('Rating changes of %s are out', 'tourist'),
        record('Rating changes of %s are out', 'Petr'),
    ),
    'another job failing': (
        record(SLOT_FAILED, 'problems.refresh', slot(23), level=logging.WARNING),
        record(SLOT_FAILED, 'contests.sync', slot(23), level=logging.WARNING),
    ),
    'another first line, the same after it': (
        record('Codeforces is down\nsince 23:00'),
        record('AtCoder is down\nsince 23:00'),
    ),
    'another logger': (
        record('Codeforces is down', name='tle.cogs.duel'),
        record('Codeforces is down', name='tle.cogs.handles'),
    ),
    'another level': (
        record('Codeforces is down', level=logging.WARNING),
        record('Codeforces is down', level=logging.ERROR),
    ),
    'another type of exception': (
        record('Query failed', exc_info=raised(ValueError('one'))),
        record('Query failed', exc_info=raised(KeyError('one'))),
    ),
    'an exception, then none': (
        record('Query failed', exc_info=raised(ValueError('one'))),
        record('Query failed'),
    ),
    # discord.py wraps both in a CommandInvokeError.
    'another bug in the same command': (
        record(RANKLIST_FAILED, exc_info=prefix_failed(missing_rows)),
        record(RANKLIST_FAILED, exc_info=prefix_failed(no_rows)),
    ),
    # And in two, run as a slash command.
    'another bug in the same slash command': (
        record(RANKLIST_FAILED, exc_info=slash_failed(missing_rows)),
        record(RANKLIST_FAILED, exc_info=slash_failed(no_rows)),
    ),
    'a bug of the same type, raised from another line': (
        record(RANKLIST_FAILED, exc_info=prefix_failed(missing_rows)),
        record(RANKLIST_FAILED, exc_info=prefix_failed(missing_name)),
    ),
    # KCPC raises both as a RuntimeError.
    'another bug in the same job': (
        record(
            SLOT_FAILED,
            'problems.refresh',
            slot(23),
            level=logging.WARNING,
            exc_info=refresh_failed(missing_rows),
        ),
        record(
            SLOT_FAILED,
            'problems.refresh',
            slot(0),
            level=logging.WARNING,
            exc_info=refresh_failed(no_rows),
        ),
    ),
    # The log file lacks the second: formatting it raises.
    'arguments that fit the template, then arguments that do not': (
        record('Query failed\nHTTP %d', 502),
        record('Query failed\nHTTP %d', 'none'),
    ),
}


@pytest.mark.parametrize('first, second', REPEATS.values(), ids=list(REPEATS))
def test_records_repeat_with_the_same_logger_level_first_line_and_exception(
    first: logging.LogRecord, second: logging.LogRecord, clock: FakeClock
) -> None:
    repeats = RepeatFilter(clock=clock)

    assert repeats.admit(first)
    assert not repeats.admit(second)


@pytest.mark.parametrize('first, second', NOT_REPEATS.values(), ids=list(NOT_REPEATS))
def test_records_that_differ_are_each_posted(
    first: logging.LogRecord, second: logging.LogRecord, clock: FakeClock
) -> None:
    repeats = RepeatFilter(clock=clock)

    assert repeats.admit(first)
    assert repeats.admit(second)


@pytest.mark.parametrize('length', [1, 2], ids=['itself', 'each other'])
def test_causes_that_loop_back_still_give_a_key(length: int, clock: FakeClock) -> None:
    def looped() -> logging.LogRecord:
        # As after raise error from error: the causes never end.
        errors = [ValueError(n) for n in range(length)]
        for error, cause in zip(errors, errors[1:] + errors[:1], strict=True):
            error.__cause__ = cause
        return record('Query failed', exc_info=(ValueError, errors[0], None))

    repeats = RepeatFilter(clock=clock)
    admitted: list[bool] = []

    def admit_twice() -> None:
        admitted.extend([repeats.admit(looped()), repeats.admit(looped())])

    # In a thread, so that following the causes forever fails the test rather
    # than hanging it.
    thread = threading.Thread(target=admit_twice, daemon=True)
    thread.start()
    thread.join(TASK_TIMEOUT)

    assert admitted == [True, False]


def test_discord_s_blocked_heartbeat_repeats_whatever_the_stack_in_it(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    # While the loop is blocked, discord.py's heartbeat thread logs its
    # template every 10 seconds, with the loop thread's stack inside.
    heartbeat = KeepAliveHandler(ws=MagicMock(), interval=41.25, shard_id=None)
    gateway = logging.getLogger('discord.gateway')

    def blocked(total: int) -> None:
        stack = ''.join(traceback.format_stack())
        msg = (
            f'{heartbeat.block_msg}\n'
            f'Loop thread traceback (most recent call last):\n{stack}'
        )
        gateway.warning(msg, heartbeat.shard_id, total)

    def on_a_lock() -> None:
        blocked(10)

    def in_a_slow_query() -> None:
        blocked(20)

    with caplog.at_level(logging.WARNING, logger=gateway.name):
        on_a_lock()
        in_a_slow_query()
    first, second = [found for found in caplog.records if found.name == gateway.name]
    repeats = RepeatFilter(clock=clock)

    assert first.getMessage().split('\n', 2)[2] != second.getMessage().split('\n', 2)[2]
    assert repeats.admit(first)
    assert not repeats.admit(second)
    clock.advance(HOUR)
    [(count, _, last)] = repeats.due()
    assert count == 1
    assert last.getMessage().startswith(
        'Shard ID None heartbeat blocked for more than 20 seconds.\n'
        'Loop thread traceback (most recent call last):\n'
    )


@pytest.mark.parametrize(
    'count, period, text',
    [
        (1, HOUR, 'Repeated 1 more time in the last hour; the last:'),
        (3, 2 * HOUR, 'Repeated 3 more times in the last 2 hours; the last:'),
        (2, HOUR + 29 * 60, 'Repeated 2 more times in the last hour; the last:'),
        (2, HOUR + 30 * 60, 'Repeated 2 more times in the last 2 hours; the last:'),
        (1, 6 * HOUR + 60, 'Repeated 1 more time in the last 6 hours; the last:'),
        (24, DAY + 60, 'Repeated 24 more times in the last 24 hours; the last:'),
        # Never "0 hours".
        (5, 60, 'Repeated 5 more times in the last hour; the last:'),
    ],
)
def test_a_summary_says_how_many_repeats_came_and_over_how_many_hours(
    count: int, period: int, text: str
) -> None:
    assert logging_cog._repeats_text(count, period) == text


async def test_a_summary_shows_the_last_repeat_without_its_traceback(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    console = logging.Formatter()

    def timeout(seconds: int) -> logging.LogRecord:
        found = record(
            'No answer from Codeforces in %s seconds',
            seconds,
            level=logging.WARNING,
            exc_info=raised(TimeoutError('timed out')),
        )
        # The console's handler comes first, and formats the traceback.
        console.format(found)
        return found

    cog.emit(timeout(60))
    await drain(cog)
    held = [timeout(61), timeout(62)]
    for found in held:
        cog.emit(found)
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    # The summary is one message.
    first, summary, other = posted(channel)
    assert first.startswith(
        f'```ansi\n{ANSI_YELLOW}No answer from Codeforces in 60 seconds\nTraceback'
    )
    assert 'TimeoutError: timed out' in first
    assert summary == (
        'Repeated 2 more times in the last hour; the last:\n'
        + block('No answer from Codeforces in 62 seconds', ANSI_YELLOW)
    )
    assert other == block('Something else')
    assert pings_nobody(channel)
    # The records themselves keep their tracebacks, for the other handlers.
    assert all(found.exc_info and found.exc_text for found in held)


async def test_a_summary_of_a_command_s_error_names_the_last_command(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)

    def failure(member: int) -> logging.LogRecord:
        return record(
            'Ignoring exception in command duel challenge:',
            message_content=f';duel challenge <@{member}>',
            jump_url=f'https://discord.com/channels/1/2/{member}',
        )

    cog.emit(failure(1))
    await drain(cog)
    cog.emit(failure(2))
    cog.emit(failure(3))
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    # The summary is one message, with the last command above its record.
    assert posted(channel) == [
        'Original Command: ;duel challenge <@1>\n'
        'Jump Url: https://discord.com/channels/1/2/1',
        block('Ignoring exception in command duel challenge:'),
        f'{repeated(2)}\n'
        'Original Command: ;duel challenge <@3>\n'
        'Jump Url: https://discord.com/channels/1/2/3\n'
        + block('Ignoring exception in command duel challenge:'),
        block('Something else'),
    ]
    assert pings_nobody(channel)


async def test_a_summary_of_a_long_command_fits_one_message(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)

    def failure() -> logging.LogRecord:
        # A long message, after a command with Nitro's 4000 characters.
        return record(
            'Ignoring exception in command duel challenge:\n' + 'y' * 2500,
            message_content=';' + 'x' * 3999,
            jump_url='https://discord.com/channels/1/2/3',
        )

    cog.emit(failure())
    await drain(cog)
    cog.emit(failure())
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    _, _, summary, other = posted(channel)
    assert len(summary) == MESSAGE_LIMIT
    # The note and the command take half the message, the record the rest.
    head = f'{repeated(1)}\nOriginal Command: ;'
    assert summary.startswith(head + 'x' * (MESSAGE_LIMIT // 2 - len(head)) + '\n')
    assert summary.endswith(f'yyy{RESET}```\n{CUT_NOTE}')
    assert (
        f'```ansi\n{ANSI_RED}Ignoring exception in command duel challenge:\n…\nyyy'
        in summary
    )
    assert other == block('Something else')


async def test_summaries_are_posted_without_waiting_for_another_record(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The task looks at the summaries due whenever it has waited this long.
    monkeypatch.setattr(logging_cog, 'IDLE_SECONDS', 0.01)
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    for seconds in (10, 20, 30):
        cog.emit(record(HEARTBEAT_BLOCKED, 0, seconds, level=logging.WARNING))

    async with running(cog):
        await eventually(lambda: channel.send.await_count == 1)
        clock.advance(HOUR)
        await eventually(lambda: channel.send.await_count == 2)

    assert posted(channel) == [
        block('Shard ID 0 heartbeat blocked for more than 10 seconds.', ANSI_YELLOW),
        f'{repeated(2)}\n'
        + block('Shard ID 0 heartbeat blocked for more than 30 seconds.', ANSI_YELLOW),
    ]


async def test_a_late_repeat_is_posted_with_the_hours_it_took(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_cog, 'IDLE_SECONDS', 0.01)
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    looks = MagicMock(wraps=cog.repeats.due)
    cog.repeats.due = looks  # type: ignore[method-assign]
    cog.emit(record('Standings of contest 2271 failed', level=logging.WARNING))

    async with running(cog):
        await eventually(lambda: channel.send.await_count == 1)
        # Three hours later, the task has looked at the window, which ended
        # with nothing held, before the same warning comes for another
        # contest.
        clock.advance(3 * HOUR)
        looked = looks.call_count
        await eventually(lambda: looks.call_count > looked)
        cog.emit(record('Standings of contest 2273 failed', level=logging.WARNING))
        await eventually(lambda: channel.send.await_count == 2)

    assert posted(channel) == [
        block('Standings of contest 2271 failed', ANSI_YELLOW),
        f'{repeated(1, "3 hours")}\n'
        + block('Standings of contest 2273 failed', ANSI_YELLOW),
    ]


async def test_the_other_handlers_still_get_every_repeat(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    source = logging.getLogger('tests.component.test_logging_cog')
    source.addHandler(cog)
    try:
        for _ in range(3):
            source.warning('Codeforces is down')
        await drain(cog)
    finally:
        source.removeHandler(cog)

    # As the console and the log file do.
    assert [found.getMessage() for found in caplog.records] == [
        'Codeforces is down'
    ] * 3
    assert posted(channel) == [block('Codeforces is down', ANSI_YELLOW)]


async def test_a_burst_of_one_warning_for_many_contests_is_posted_once(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    # A run over 50 contests, whose standings Codeforces refuses one by one.
    for contest_id in range(2226, 2276):
        cog.emit(record(STANDINGS_REFUSED.format(id=contest_id), level=logging.WARNING))
    assert cog.queue.qsize() == 1

    await drain(cog)
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    assert posted(channel) == [
        block(STANDINGS_REFUSED.format(id=2226), ANSI_YELLOW),
        f'{repeated(49)}\n' + block(STANDINGS_REFUSED.format(id=2275), ANSI_YELLOW),
        block('Something else'),
    ]


async def test_a_new_bug_in_a_command_is_posted_in_full_while_another_repeats(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    # As login() would: discord.py dispatches a command's error as an event.
    bot.loop = asyncio.get_running_loop()
    bot.add_listener(discord_common.bot_error_handler, 'on_command_error')

    @bot.command()
    async def ranklist(ctx: commands.Context[commands.Bot], contest_id: int) -> None:
        # Its bugs: contests 1 and 3 are missing, contest 2 has no rows, and
        # contest 4 has no name.
        rows: list[int] = {2: [], 4: [1]}[contest_id]
        name = {2: 'Round 2'}[contest_id]
        await ctx.send(f'{name}: the average rank is {sum(rows) / len(rows)}')

    # TLE's error handler logs the bugs, which discord.py wraps.
    errors = logging.getLogger(discord_common.__name__)
    errors.addHandler(cog)
    try:
        for contest_id in (1, 3, 2, 4):
            await invoke(bot, f'ranklist {contest_id}', message_id=contest_id)
    finally:
        errors.removeHandler(cog)
        await bot.close()
    await drain(cog)
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    def command(contest_id: int) -> str:
        return (
            f'Original Command: ;ranklist {contest_id}\n'
            f'Jump Url: https://discord.com/channels/1/2/{contest_id}'
        )

    posts = posted(channel)
    # Each bug in full, with its traceback.
    assert posts[0:6:2] == [command(1), command(2), command(4)]
    first, second, third = posts[1:6:2]
    assert 'Command raised an exception: KeyError: 1' in first
    assert 'Command raised an exception: ZeroDivisionError: division' in second
    assert 'Command raised an exception: KeyError: 4' in third
    for bug in (first, second, third):
        assert bug.startswith(f'```ansi\n{ANSI_RED}{RANKLIST_FAILED}\n')
        assert 'Traceback (most recent call last):' in bug
    # The first bug again, for contest 3, summed up.
    assert posts[6:] == [
        f'{repeated(1)}\n{command(3)}\n' + block(RANKLIST_FAILED),
        block('Something else'),
    ]


async def test_a_record_with_no_exception_to_show_is_posted(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    source = logging.getLogger('tests.component.test_logging_cog')
    source.addHandler(cog)
    try:
        # Outside an except block, so its exception is (None, None, None).
        source.exception('Query failed')
        await drain(cog)
    finally:
        source.removeHandler(cog)

    [logged] = caplog.records
    assert logged.exc_info == (None, None, None)
    [post] = posted(channel)
    assert post.startswith(f'```ansi\n{ANSI_RED}Query failed\n')


async def test_a_record_whose_arguments_do_not_fit_its_template_is_posted_with_them(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    first, second = blocked_heartbeat(10), blocked_heartbeat(20)
    # As the console's and the log file's handlers find.
    with pytest.raises((TypeError, ValueError)):
        first.getMessage()

    cog.emit(first)
    await drain(cog)
    cog.emit(second)
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    def shown(total: int) -> str:
        return block(
            f'{HEARTBEAT_BLOCKED}\nLoop thread traceback (most recent call last):\n'
            f'{STACK_ON_A_PERCENT}\nArguments: (None, {total})',
            ANSI_YELLOW,
        )

    # Posted, not counted as lost; and so is the summary of its repeat.
    assert posted(channel) == [
        shown(10),
        f'{repeated(1)}\n{shown(20)}',
        block('Something else'),
    ]


# The queue


async def test_records_past_the_queue_s_limit_are_counted_and_reported(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_cog, 'QUEUE_LIMIT', 2)
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    for site in ('Codeforces', 'AtCoder', 'CodeChef', 'Kattis', 'Luogu'):
        cog.emit(record(f'{site} is down'))
    assert cog.queue.qsize() == 2

    await drain(cog)
    cog.emit(record('Toph is down'))
    await drain(cog)

    # The count comes once, before the next post.
    assert posted(channel) == [
        '3 log records could not be posted here; the log file has them.',
        block('Codeforces is down'),
        block('AtCoder is down'),
        block('Toph is down'),
    ]
    assert pings_nobody(channel)


async def test_a_record_dropped_on_a_full_queue_is_not_seen(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_cog, 'QUEUE_LIMIT', 1)
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Codeforces is down'))
    # No room: counted, but the filter never sees it, nor the repeat.
    cog.emit(record('AtCoder is down'))
    cog.emit(record('Codeforces is down'))
    assert len(cog.repeats) == 1

    await drain(cog)
    # So its next repeat is posted in full, not held for a summary.
    cog.emit(record('AtCoder is down'))
    await drain(cog)
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    assert posted(channel) == [
        '2 log records could not be posted here; the log file has them.',
        block('Codeforces is down'),
        block('AtCoder is down'),
        block('Something else'),
    ]


async def test_a_record_the_log_file_lacks_is_not_counted_on_a_full_queue(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_cog, 'QUEUE_LIMIT', 1)
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Codeforces is down'))
    # No room for either; but the log file lacks the first, whose arguments
    # don't fit its template, so the count would say something untrue.
    cog.emit(blocked_heartbeat(10))
    cog.emit(record('AtCoder is down'))

    await drain(cog)

    assert posted(channel) == [dropped(1), block('Codeforces is down')]


async def test_the_queue_holds_a_thousand_records(clock: FakeClock) -> None:
    cog = make_cog(make_bot(text_channel()), clock)

    assert cog.queue.maxsize == 1000


async def test_a_record_logged_in_the_bot_s_loop_is_queued_at_once(
    clock: FakeClock,
) -> None:
    cog = make_cog(make_bot(text_channel()), clock)

    cog.emit(record('In the loop'))

    assert cog.queue.qsize() == 1


async def test_a_record_from_another_thread_is_queued_by_the_bot_s_loop(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    # As discord.py's heartbeat thread logs it.
    heartbeat = record(HEARTBEAT_BLOCKED, 0, 10, level=logging.WARNING)
    thread = threading.Thread(target=cog.handle, args=(heartbeat,))
    thread.start()
    thread.join()

    # Handed over to the loop, which hasn't run since.
    assert cog.queue.empty()
    await eventually(lambda: cog.queue.qsize() == 1)
    await drain(cog)

    assert posted(channel) == [
        block('Shard ID 0 heartbeat blocked for more than 10 seconds.', ANSI_YELLOW)
    ]
    # The repeat filter ran in the loop's thread alone.
    assert clock.threads == {threading.get_ident()}


def test_a_record_after_the_loop_closed_is_dropped_quietly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def build() -> Logging:
        return Logging(make_bot(text_channel()), LOG_CHANNEL_ID)

    loop = asyncio.new_event_loop()
    try:
        cog = loop.run_until_complete(build())
    finally:
        loop.close()

    # As the bot stops, from its own thread and from another.
    cog.emit(record('After the loop closed'))
    thread = threading.Thread(target=cog.emit, args=(record('From a thread'),))
    thread.start()
    thread.join()

    assert capsys.readouterr().err == ''


async def test_the_cog_s_own_records_stay_out_of_the_log_channel(
    clock: FakeClock,
) -> None:
    cog = make_cog(make_bot(text_channel()), clock)

    # They are about the log channel, which may be what fails.
    cog.emit(record('The log channel is unreachable', name=COG_LOGGER))
    assert cog.queue.empty()
    # Its notice that the bot runs is posted.
    cog.emit(record('Bot running', name='Logging', level=100))
    assert cog.queue.qsize() == 1


async def test_a_failed_turn_leaves_the_task_running(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    cog.repeats.due = MagicMock(side_effect=[RuntimeError('Broken'), [], []])
    cog.emit(record('Lost'))
    # Held while the first waits in the queue: lost with it, and counted.
    cog.emit(record('Lost'))
    cog.emit(record('Posted'))
    await drain(cog)
    # Nobody saw the lost one, so it is posted in full when it comes again.
    cog.emit(record('Lost'))
    await drain(cog)

    assert posted(channel) == [dropped(2), block('Posted'), block('Lost')]
    [failure] = [found for found in caplog.records if found.name == COG_LOGGER]
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None


# The log channel


async def test_a_reconnect_loses_no_record(clock: FakeClock) -> None:
    channel = text_channel()
    bot = make_bot(channel)
    cog = make_cog(bot, clock)
    cog.emit(record('Before the reconnect'))
    await drain(cog)

    # A new session empties the bot's cache, which lacks the channel until its
    # server is back. A partial channel made from the id posts meanwhile.
    bot.get_channel.return_value = None
    cog.emit(record('During the reconnect'))
    await drain(cog)
    # The server is back, with a new channel object.
    back = text_channel()
    bot.get_channel.return_value = back
    cog.emit(record('After the reconnect'))
    await drain(cog)

    assert posted(channel) == [block('Before the reconnect')]
    assert posted(partial_channel(bot)) == [block('During the reconnect')]
    assert posted(back) == [block('After the reconnect')]
    bot.get_partial_messageable.assert_called_once_with(LOG_CHANNEL_ID)
    bot.fetch_channel.assert_not_awaited()


async def test_a_channel_missing_from_the_cache_is_posted_in_by_its_id(
    clock: FakeClock,
) -> None:
    bot = make_bot(None)
    cog = make_cog(bot, clock)
    cog.emit(record('First'))
    cog.emit(record('Second'))

    await drain(cog)

    # Without asking Discord for the channel.
    assert posted(partial_channel(bot)) == [block('First'), block('Second')]
    assert bot.get_partial_messageable.call_args_list == [call(LOG_CHANNEL_ID)] * 2
    bot.fetch_channel.assert_not_awaited()
    assert pings_nobody(partial_channel(bot))


async def test_a_deleted_log_channel_is_tried_every_ten_minutes_and_keeps_the_handler(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    bot = make_bot(None)
    deleted = partial_channel(bot)
    deleted.send.side_effect = http_error(discord.NotFound, 404, 'Unknown Channel')
    cog = make_cog(bot, clock)
    cog.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(cog)
    try:
        cog.emit(record('Posted in a deleted channel'))
        await drain(cog)
        # For ten minutes, the cog posts nothing, and counts the records.
        clock.advance(TEN_MINUTES - 1)
        cog.emit(record('Not tried'))
        await drain(cog)
        assert deleted.send.await_count == 1
        # Then it tries again, with the count.
        clock.advance(1)
        cog.emit(record('Tried again'))
        await drain(cog)

        assert root.handlers.count(cog) == 1
    finally:
        root.removeHandler(cog)

    assert posted(deleted) == [block('Posted in a deleted channel'), dropped(2)]
    notes = [found for found in caplog.records if found.name == COG_LOGGER]
    assert [found.levelno for found in notes] == [logging.INFO] * 2
    assert all('Unknown Channel' in found.getMessage() for found in notes)
    bot.fetch_channel.assert_not_awaited()


async def test_a_channel_the_bot_may_not_post_in_is_left_alone_for_ten_minutes(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    channel = text_channel()
    refused = http_error(discord.Forbidden, 403, 'Missing Permissions')
    channel.send.side_effect = [refused, None, None]
    bot = make_bot(channel)
    cog = make_cog(bot, clock)
    cog.emit(record('Refused'))
    await drain(cog)
    clock.advance(TEN_MINUTES - 1)
    cog.emit(record('Not tried'))
    await drain(cog)
    assert channel.send.await_count == 1

    clock.advance(1)
    cog.emit(record('Allowed again'))
    await drain(cog)

    assert posted(channel) == [block('Refused'), dropped(2), block('Allowed again')]
    [info] = [found for found in caplog.records if found.name == COG_LOGGER]
    assert info.levelno == logging.INFO
    assert 'Missing Permissions' in info.getMessage()


async def test_a_summary_lost_with_the_channel_counts_the_repeats_it_stood_for(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Codeforces is down'))
    await drain(cog)
    for _ in range(3):
        cog.emit(record('Codeforces is down'))
    refused = http_error(discord.Forbidden, 403, 'Missing Permissions')
    channel.send.side_effect = [refused, None, None]
    clock.advance(HOUR)
    cog.emit(record('Something else'))
    await drain(cog)

    clock.advance(TEN_MINUTES)
    cog.emit(record('Posted again'))
    await drain(cog)

    # The three repeats, and the record after them.
    assert posted(channel) == [
        block('Codeforces is down'),
        f'{repeated(3)}\n' + block('Codeforces is down'),
        dropped(4),
        block('Posted again'),
    ]


async def test_the_count_of_lost_records_is_posted_without_waiting_for_another(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging_cog, 'IDLE_SECONDS', 0.01)
    channel = text_channel()
    refused = http_error(discord.Forbidden, 403, 'Missing Permissions')
    channel.send.side_effect = [refused, None]
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Refused'))

    async with running(cog):
        await eventually(lambda: channel.send.await_count == 1)
        clock.advance(TEN_MINUTES)
        await eventually(lambda: channel.send.await_count == 2)

    assert posted(channel) == [block('Refused'), dropped(1)]


async def test_a_channel_that_takes_no_messages_pauses_posting(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=COG_LOGGER)
    category = MagicMock(spec=discord.CategoryChannel)
    bot = make_bot(category)
    cog = make_cog(bot, clock)
    cog.emit(record('Nowhere to post'))
    await drain(cog)
    clock.advance(TEN_MINUTES - 1)
    cog.emit(record('Not looked for'))
    await drain(cog)
    clock.advance(1)
    cog.emit(record('Still nowhere'))
    await drain(cog)

    # The cache is read once every ten minutes, and nothing else is tried.
    assert bot.get_channel.call_count == 2
    bot.get_partial_messageable.assert_not_called()
    notes = [found for found in caplog.records if found.name == COG_LOGGER]
    assert [found.levelno for found in notes] == [logging.INFO] * 2
    assert all('takes no messages' in found.getMessage() for found in notes)

    # Once the id is a text channel's, the count comes first.
    channel = text_channel()
    bot.get_channel.return_value = channel
    clock.advance(TEN_MINUTES)
    cog.emit(record('Posted at last'))
    await drain(cog)

    assert posted(channel) == [dropped(3), block('Posted at last')]


async def test_a_post_that_fails_otherwise_is_logged_and_counted(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    channel = text_channel()
    failed = http_error(discord.DiscordServerError, 500, 'Internal Server Error')
    channel.send.side_effect = [failed, None, None]
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Lost'))
    await drain(cog)

    # Not the channel's fault, so the next record is posted at once.
    cog.emit(record('Posted'))
    await drain(cog)

    assert posted(channel) == [block('Lost'), dropped(1), block('Posted')]
    [failure] = [found for found in caplog.records if found.name == COG_LOGGER]
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None


@pytest.mark.parametrize(
    'cls, status, text',
    [
        # Discord's 503, which discord.py doesn't try again.
        (discord.DiscordServerError, 503, 'Service Unavailable'),
        # The channel is gone, or closed to the bot: posting pauses.
        (discord.NotFound, 404, 'Unknown Channel'),
        (discord.Forbidden, 403, 'Missing Permissions'),
    ],
    ids=['server error', 'channel gone', 'not allowed'],
)
async def test_a_bug_whose_post_failed_is_posted_in_full_when_it_comes_again(
    cls: type[discord.HTTPException], status: int, text: str, clock: FakeClock
) -> None:
    channel = text_channel()
    channel.send.side_effect = [http_error(cls, status, text), None, None]
    cog = make_cog(make_bot(channel), clock)
    cog.emit(monitor_failed())
    await drain(cog)

    # Ten minutes later, once any pause is over, the bug again: nobody has
    # seen its traceback yet.
    clock.advance(TEN_MINUTES)
    cog.emit(monitor_failed())
    await drain(cog)

    lost, count, again = posted(channel)
    assert count == dropped(1)
    assert again == lost
    assert again.startswith(f'```ansi\n{ANSI_RED}{MONITOR_FAILED}\nTraceback')
    assert again.endswith(f'KeyError: 2271{RESET}```')


async def test_a_bug_first_logged_while_posting_paused_is_posted_in_full_after(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    deleted = http_error(discord.NotFound, 404, 'Unknown Channel')
    channel.send.side_effect = [deleted, None, None]
    cog = make_cog(make_bot(channel), clock)
    cog.emit(record('Posted in a deleted channel'))
    await drain(cog)
    # A new bug while posting pauses, and again five minutes later.
    cog.emit(monitor_failed())
    await drain(cog)
    clock.advance(5 * 60)
    cog.emit(monitor_failed())
    await drain(cog)

    # Once the pause is over, it comes again.
    clock.advance(5 * 60)
    cog.emit(monitor_failed())
    await drain(cog)

    _, count, bug = posted(channel)
    assert count == dropped(3)
    assert bug.startswith(f'```ansi\n{ANSI_RED}{MONITOR_FAILED}\nTraceback')


async def test_the_repeats_held_while_a_failed_post_waited_are_counted(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    unavailable = http_error(discord.DiscordServerError, 503, 'Service Unavailable')
    channel.send.side_effect = [unavailable, None, None]
    cog = make_cog(make_bot(channel), clock)
    # The repeat comes before the task has posted the first, and is held.
    cog.emit(monitor_failed())
    cog.emit(monitor_failed())
    assert cog.queue.qsize() == 1
    await drain(cog)
    cog.emit(monitor_failed())
    await drain(cog)

    lost, count, again = posted(channel)
    assert count == dropped(2)
    assert again == lost


async def test_a_record_the_log_file_lacks_is_not_counted_when_its_post_fails(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    unavailable = http_error(discord.DiscordServerError, 503, 'Service Unavailable')
    channel.send.side_effect = [unavailable, unavailable, None, None]
    cog = make_cog(make_bot(channel), clock)
    # The log file lacks the warning and its repeat, held while it waited:
    # their arguments don't fit their template. It has the error.
    cog.emit(blocked_heartbeat(10))
    cog.emit(blocked_heartbeat(20))
    cog.emit(record('Codeforces is down'))
    await drain(cog)
    cog.emit(record('Posted'))
    await drain(cog)

    tried, _, count, last = posted(channel)
    assert 'Arguments: (None, 10)' in tried
    assert [count, last] == [dropped(1), block('Posted')]


async def test_a_lost_summary_of_records_the_log_file_lacks_is_not_counted(
    clock: FakeClock,
) -> None:
    channel = text_channel()
    cog = make_cog(make_bot(channel), clock)
    cog.emit(blocked_heartbeat(10))
    await drain(cog)
    cog.emit(blocked_heartbeat(20))
    cog.emit(blocked_heartbeat(30))
    unavailable = http_error(discord.DiscordServerError, 503, 'Service Unavailable')
    channel.send.side_effect = [unavailable, None]
    clock.advance(HOUR)
    cog.emit(record('Posted'))
    await drain(cog)

    _, summary, last = posted(channel)
    assert summary.startswith(repeated(2))
    assert last == block('Posted')


# Installation


async def test_setup_puts_the_handler_on_the_root_logger_for_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('LOGGING_COG_CHANNEL_ID', str(LOG_CHANNEL_ID))
    bot = MagicMock(spec=commands.Bot)
    root = logging.getLogger()

    await setup(bot)

    [cog] = [handler for handler in root.handlers if isinstance(handler, Logging)]
    root.removeHandler(cog)
    assert cog.channel_id == LOG_CHANNEL_ID
    assert cog.level == logging.WARNING
    bot.add_cog.assert_awaited_once_with(cog)


async def test_unloading_the_cog_takes_its_handler_off_and_stops_its_task(
    clock: FakeClock,
) -> None:
    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    cog = Logging(bot, LOG_CHANNEL_ID, clock=clock)
    root = logging.getLogger()
    root.addHandler(cog)
    try:
        await bot.add_cog(cog)
        # As the bot's first ready event starts it.
        cog.task = asyncio.create_task(cog._log_task())

        # Closing, the bot removes its cogs, which unloads them.
        await bot.close()

        assert cog not in root.handlers
    finally:
        root.removeHandler(cog)
    await asyncio.wait([cog.task], timeout=TASK_TIMEOUT)
    assert cog.task.cancelled()
