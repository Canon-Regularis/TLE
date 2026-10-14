import asyncio
import copy
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import discord
from discord.ext import commands

from tle.util import ansi, discord_common

root_logger = logging.getLogger()
logger = logging.getLogger(__name__)

_ANSI_BY_LEVEL = {
    logging.INFO: ansi.ANSI_GREEN,
    logging.WARNING: ansi.ANSI_YELLOW,
    logging.ERROR: ansi.ANSI_RED,
    logging.CRITICAL: ansi.ANSI_BOLD_RED,
}
# The log channel's messages ping nobody, although a logged command or error
# can name members, roles or @everyone.
_NO_PINGS = discord.AllowedMentions.none()
# Discord's limit on the length of a message.
MESSAGE_LIMIT = 2000
# How long the first window after a record is posted lasts, in seconds: the
# repeats it holds are then summed up in one post.
REPEAT_WINDOW = 3600
# How long a record is remembered after it was last seen, in seconds; also the
# longest a window grows.
REPEAT_MEMORY = 24 * 3600
# The most records waiting to be posted. Those past it are counted, and the
# log channel is told how many.
QUEUE_LIMIT = 1000
# How long the task waits for a record before it posts the summaries due
# anyway, in seconds.
IDLE_SECONDS = 60
# How long the cog stops posting after Discord refused a post because the log
# channel is gone or closed to the bot, in seconds.
PAUSE_SECONDS = 600
# What a record too long for one post shows between its first line and its
# end, and after them.
CUT_LINE = '…'
CUT_NOTE = '`Check logs for full stack trace`'

# An hour in seconds, which the rate of repeats and the summaries count in.
_HOUR = 3600
# What repeats share: the logger, the level, the first line of the message
# with its numbers masked, whether the arguments fit the template, and the
# root cause of the exception, if any: its type, and where it was raised.
_Key = tuple[str, int, str, bool, str | None, str]
# The ids, times and counts that differ between repeats.
_DIGITS = re.compile(r'\d+')
# The most causes followed to the root cause of an exception: a chain of
# causes can be long, or loop back on itself.
_MAX_CAUSES = 10


def _repeats_text(count: int, period: float) -> str:
    """The line above the summary of ``count`` repeats held over ``period``
    seconds.
    """
    hours = max(1, int(period / _HOUR + 0.5))
    times = 'time' if count == 1 else 'times'
    span = 'hour' if hours == 1 else f'{hours} hours'
    return f'Repeated {count} more {times} in the last {span}; the last:'


def _dropped_text(count: int) -> str:
    """The post that counts the records the cog couldn't post."""
    if count == 1:
        return '1 log record could not be posted here; the log file has it.'
    return f'{count} log records could not be posted here; the log file has them.'


def _block(text: str, color: str, limit: int = MESSAGE_LIMIT) -> str:
    """``text`` in a code block in ``color``, within ``limit`` characters.

    A text too long keeps its first line and its end, which is where a
    traceback shows the last exception, with a ``CUT_LINE`` between them;
    ``CUT_NOTE`` follows the block.
    """
    whole = f'```ansi\n{color}{text}{ansi.RESET}```'
    if len(whole) <= limit:
        return whole
    room = limit - (len(whole) - len(text)) - len(f'\n{CUT_LINE}\n\n{CUT_NOTE}')
    # The first line takes at most half the room, and the end the rest.
    head = text.split('\n', 1)[0][: room // 2]
    start = len(text) - (room - len(head))
    # The end begins with a whole line, if one begins in it.
    line = text.find('\n', start - 1) + 1
    if 0 < line < len(text):
        start = line
    return (
        f'```ansi\n{color}{head}\n{CUT_LINE}\n{text[start:]}{ansi.RESET}```\n{CUT_NOTE}'
    )


@dataclass
class _Repeats:
    """What the filter remembers of a record: when it was last seen, the
    window open, and the repeats held in it.
    """

    seen: float
    window_start: float
    length: float
    held: int = 0
    last: logging.LogRecord | None = None  # the last repeat, without traceback


def _without_traceback(record: logging.LogRecord) -> logging.LogRecord:
    """A copy of ``record`` without the traceback and stack, which its first
    post showed.

    The record itself is left as it is for the other handlers, and the copy
    keeps no frames alive while it is held.
    """
    held = copy.copy(record)
    held.exc_info = held.exc_text = held.stack_info = None
    return held


def _message(record: logging.LogRecord) -> str | None:
    """The message of ``record``; None if its arguments don't fit its
    template, as when the stack in discord.py's warning of a blocked
    heartbeat holds a '%'.

    The console's and the log file's handlers fail on such a record too.
    """
    try:
        return record.getMessage()
    except Exception:
        return None


def _root_cause(error: BaseException) -> BaseException:
    """The end of the chain of exceptions ``error`` was raised from, or
    ``error`` itself: the bug, inside discord.py's wrappers of a command's
    error or KCPC's RuntimeError for a failed job.
    """
    for _ in range(_MAX_CAUSES):
        if error.__cause__ is None:
            break
        error = error.__cause__
    return error


def _raise_site(error: BaseException) -> str:
    """Where ``error`` was raised: the file and line of the innermost frame of
    its traceback, or '' without one.
    """
    tb = error.__traceback__
    if tb is None:
        return ''
    while tb.tb_next is not None:
        tb = tb.tb_next
    file = os.path.basename(tb.tb_frame.f_code.co_filename)
    return f'{file}:{tb.tb_lineno}'


class RepeatFilter:
    """Decides which records the log channel shows in full, and sums up the
    repeats of the others.

    Records repeat one another when they come from the same logger, at the
    same level, with the same first line once its numbers are masked, and
    with the same exception, if any. So "Standings of contest 2271 failed"
    repeats "Standings of contest 2273 failed", and discord.py's warning that
    the heartbeat is blocked repeats whatever the seconds and the stack below
    it; a template with other words in its arguments doesn't, nor does a
    record whose arguments don't fit its template repeat one whose arguments
    fit. Exceptions are the same when their root causes, at the end of the
    chain of exceptions each was raised from, have the same type and were
    raised from the same line: discord.py wraps every bug in a command in a
    CommandInvokeError, yet two bugs don't repeat each other.

    A record is remembered until ``memory`` seconds after it was last seen.
    The first is posted in full and opens a window of ``window`` seconds,
    which holds its repeats. Once a window that holds repeats has ended,
    ``due`` sums them up and opens the next window: twice as long, up to
    ``memory``, if they came less than twice an hour, or else ``window`` long
    again. A window that ends with nothing held stays open, so a late repeat
    is summed up at the next look, over the time it really took. A record
    that comes every hour is thus posted once, then summed up after about 1,
    3, 7, 15 and 31 hours, and then daily; a flood is summed up every hour.
    A record whose post failed is forgotten (``forget``), so that its next
    repeat is posted in full rather than summed up after a post nobody saw.
    """

    def __init__(
        self,
        window: float = REPEAT_WINDOW,
        memory: float = REPEAT_MEMORY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = window
        self.memory = memory
        self._clock = clock
        self._repeats: dict[_Key, _Repeats] = {}

    @staticmethod
    def key(record: logging.LogRecord) -> _Key:
        """What ``record`` shares with its repeats."""
        message = _message(record)
        text = str(record.msg) if message is None else message
        # (None, None, None) when logged with exc_info outside an except.
        error = record.exc_info[1] if record.exc_info else None
        root = None if error is None else _root_cause(error)
        return (
            record.name,
            record.levelno,
            _DIGITS.sub('#', text.split('\n', 1)[0]),
            message is not None,
            None if root is None else type(root).__name__,
            '' if root is None else _raise_site(root),
        )

    def admit(self, record: logging.LogRecord) -> bool:
        """Whether to post ``record`` in full; if not, it is held."""
        now = self._clock()
        key = self.key(record)
        repeats = self._repeats.get(key)
        if repeats is None or now - repeats.seen >= self.memory:
            self._repeats[key] = _Repeats(
                seen=now, window_start=now, length=self.window
            )
            return True
        # Held even after its window has ended: the next look sums it up.
        repeats.seen = now
        repeats.held += 1
        repeats.last = _without_traceback(record)
        return False

    def forget(self, record: logging.LogRecord) -> int:
        """Forget ``record``, whose post failed, so that its next repeat is
        posted in full; the number of its repeats held, which are lost.
        """
        repeats = self._repeats.pop(self.key(record), None)
        return 0 if repeats is None else repeats.held

    def due(self) -> list[tuple[int, float, logging.LogRecord]]:
        """The summaries of the windows that have ended holding repeats, each
        the number held, the seconds since the window opened, and the last
        repeat.

        Those windows open again, and the records unseen for ``memory``
        seconds are forgotten.
        """
        now = self._clock()
        summaries = []
        for key, repeats in list(self._repeats.items()):
            last = repeats.last
            if last is None:
                if now - repeats.seen >= self.memory:
                    del self._repeats[key]
                continue
            if now < repeats.window_start + repeats.length:
                continue
            period = now - repeats.window_start
            summaries.append((repeats.held, period, last))
            if repeats.held < 2 * period / _HOUR:
                repeats.length = min(2 * repeats.length, self.memory)
            else:
                repeats.length = self.window
            repeats.window_start = now
            repeats.held = 0
            repeats.last = None
        return summaries

    def __len__(self) -> int:
        """The number of records remembered."""
        return len(self._repeats)


class Logging(commands.Cog, logging.Handler):
    """Posts the bot's warnings and errors in the log channel.

    A record that repeats one posted lately is held and summed up later
    rather than posted again (see ``RepeatFilter``); the console and the log
    file still get every one. The next post counts the records that couldn't
    be posted and that the log file has, and a record whose own post failed
    is posted in full when it comes again. The cog posts through reconnects
    without asking Discord for the channel, and its handler stays in place
    whatever Discord answers, until the cog unloads.
    """

    def __init__(
        self,
        bot: commands.Bot,
        channel_id: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        logging.Handler.__init__(self)
        self.bot = bot
        self.channel_id = channel_id
        self.queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(
            maxsize=QUEUE_LIMIT
        )
        self.task: asyncio.Task[None] | None = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.repeats = RepeatFilter(clock=clock)
        self._clock = clock
        # Records come from other threads too, such as discord.py's heartbeat
        # thread, but only the bot's loop touches the queue and the filter.
        self._loop = asyncio.get_running_loop()
        # The records that weren't posted, which the next post counts as in
        # the log file.
        self._dropped = 0
        # Until when posting pauses, after Discord refused a post.
        self._paused_until: float | None = None

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self) -> None:
        self.task = asyncio.create_task(self._log_task())
        self.logger.log(level=100, msg='Bot running')

    async def cog_unload(self) -> None:
        """Take the handler off the root logger, and stop posting."""
        root_logger.removeHandler(self)
        if self.task is not None:
            self.task.cancel()

    async def _log_task(self) -> None:
        while True:
            record: logging.LogRecord | None
            try:
                record = await asyncio.wait_for(self.queue.get(), IDLE_SECONDS)
            except asyncio.TimeoutError:
                record = None
            try:
                await self._turn(record)
            except Exception:
                # The task outlives any failure; the log file tells of it.
                logger.exception('The log channel task failed')
                if record is not None:
                    # Lost, as if its post had failed.
                    self._drop(record, 1 + self.repeats.forget(record))
            finally:
                if record is not None:
                    self.queue.task_done()

    async def _turn(self, record: logging.LogRecord | None) -> None:
        """Post the summaries that are due, then ``record``; with no record,
        the count of the records lost, if any.
        """
        for count, period, last in self.repeats.due():
            await self._post(last, count, _repeats_text(count, period))
        if record is not None:
            if not await self._post(record, 1):
                # Its repeats would be summed up after a post nobody saw, so
                # the next one is posted in full, and those held are lost.
                self._drop(record, self.repeats.forget(record))
        elif self._dropped:
            await self._post(None, 0)

    async def _post(
        self, record: logging.LogRecord | None, weight: int, note: str | None = None
    ) -> bool:
        """Post the count of the records lost so far, if any, then ``record``
        with ``note``; whether all of it went through.

        ``weight`` is the number of records the post stands for: a summary
        stands for the repeats it counts. If the post fails, they count as
        lost.
        """
        channel = self._channel()
        if channel is None:
            self._drop(record, weight)
            return False
        try:
            dropped = self._dropped
            if dropped:
                await channel.send(_dropped_text(dropped), allowed_mentions=_NO_PINGS)
                # Not set to 0: more may have been lost meanwhile.
                self._dropped -= dropped
            if record is not None:
                await self._post_record(channel, record, note)
        except (discord.NotFound, discord.Forbidden) as exc:
            # The channel is gone, or the bot may no longer post in it.
            self._pause(repr(exc))
            self._drop(record, weight)
            return False
        except Exception:
            logger.exception('Failed to post in the log channel')
            self._drop(record, weight)
            return False
        return True

    def _drop(self, record: logging.LogRecord | None, count: int) -> None:
        """Count ``count`` records like ``record``, itself or its repeats, as
        not posted, for the next post to report.

        That post says the log file has them, so a record the log file lacks
        isn't counted: one whose arguments don't fit its template failed in
        the log file's handler too, and so did its repeats.
        """
        if record is None or _message(record) is not None:
            self._dropped += count

    async def _post_record(
        self,
        channel: discord.abc.Messageable,
        record: logging.LogRecord,
        note: str | None = None,
    ) -> None:
        """Post ``record`` as the log file has it, in colour, after the
        command that logged it, if any.

        With a ``note``, the post is a summary: one message, with the note
        and the command above the record.
        """
        text = self.format(record)
        color = _ANSI_BY_LEVEL.get(record.levelno, ansi.ANSI_WHITE)
        context: list[str] = []
        message_content = getattr(record, 'message_content', None)
        jump_url = getattr(record, 'jump_url', None)
        if message_content:
            context.append(f'Original Command: {message_content}')
        if jump_url:
            context.append(f'Jump Url: {jump_url}')
        if note is None:
            if context:
                await channel.send(
                    '\n'.join(context)[:MESSAGE_LIMIT], allowed_mentions=_NO_PINGS
                )
            await channel.send(_block(text, color), allowed_mentions=_NO_PINGS)
            return
        # The note and the command take at most half the message.
        header = '\n'.join([note, *context])[: MESSAGE_LIMIT // 2]
        body = _block(text, color, MESSAGE_LIMIT - len(header) - 1)
        await channel.send(f'{header}\n{body}', allowed_mentions=_NO_PINGS)

    def _channel(self) -> discord.abc.Messageable | None:
        """The log channel, or None while posting pauses.

        The bot's cache comes first. While it lacks the channel, as for a
        while after a reconnect, a partial channel made from the id posts all
        the same. Neither asks Discord anything.
        """
        if self._paused_until is not None and self._clock() < self._paused_until:
            return None
        channel = self.bot.get_channel(self.channel_id)
        if channel is None:
            return self.bot.get_partial_messageable(self.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            self._pause(f'{channel!r} takes no messages')
            return None
        return channel

    def _pause(self, reason: str) -> None:
        """Stop posting for ``PAUSE_SECONDS``, as Discord would refuse the
        posts.
        """
        self._paused_until = self._clock() + PAUSE_SECONDS
        logger.info(
            f'The log channel {self.channel_id} is unreachable ({reason}); '
            f'trying again in {PAUSE_SECONDS // 60} minutes'
        )

    def _enqueue(self, record: logging.LogRecord) -> None:
        """Queue ``record`` to be posted, unless it repeats one posted lately.

        It runs in the bot's loop.
        """
        # The cog's own records are about the log channel, which may well be
        # what fails.
        if record.name == logger.name:
            return
        try:
            # A record the queue has no room for is counted but not seen, so
            # its next repeat is posted in full.
            if self.queue.full():
                self._drop(record, 1)
            elif self.repeats.admit(record):
                self.queue.put_nowait(record)
        except Exception:
            self.handleError(record)

    # logging.Handler overrides below.

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except Exception:
            # Its arguments don't fit its template, as when the stack in
            # discord.py's warning of a blocked heartbeat holds a '%': the
            # post shows the template, then the arguments.
            raw = copy.copy(record)
            raw.msg = f'{record.msg}\nArguments: {record.args!r}'
            raw.args = ()
            return super().format(raw)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:  # no loop runs in this thread
            on_loop = False
        if on_loop:
            self._enqueue(record)
            return
        try:
            self._loop.call_soon_threadsafe(self._enqueue, record)
        except RuntimeError:
            # The loop is closed, so the bot has stopped; the log file has the
            # record.
            pass

    def close(self) -> None:
        if self.task:
            self.task.cancel()


async def setup(bot: commands.Bot) -> None:
    logging_cog_channel_id = os.environ.get('LOGGING_COG_CHANNEL_ID')
    if logging_cog_channel_id is None:
        logger.info(
            'Skipping installation of logging cog as logging channel is not provided.'
        )
        return

    logging_cog = Logging(bot, int(logging_cog_channel_id))
    logging_cog.setLevel(logging.WARNING)
    logging_cog.setFormatter(
        logging.Formatter(
            fmt='{asctime}:{levelname}:{name}:{message}',
            style='{',
            datefmt='%d-%m-%Y %H:%M:%S',
        )
    )
    root_logger.addHandler(logging_cog)
    await bot.add_cog(logging_cog)
