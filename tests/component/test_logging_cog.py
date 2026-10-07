"""Tests for the Logging cog (tle.cogs.logging), which posts the bot's
warnings and errors in the log channel.

The log channel and the bot are mocked; records go through the cog's queue
and its task, as when the bot runs.
"""

import asyncio
import logging
from unittest.mock import MagicMock

import discord
from discord.ext import commands

from tle.cogs.logging import Logging
from tle.util.ansi import ANSI_RED

# Real snowflakes are 64-bit, so use big ones.
LOG_CHANNEL_ID = 1_200_000_000_000_000_001
# Seconds the cog's task may take, many times over.
TASK_TIMEOUT = 5


def record(message: str, **extra: str) -> logging.LogRecord:
    """An error record, with the ``extra`` that the error handler adds."""
    found = logging.LogRecord('tle', logging.ERROR, __file__, 1, message, None, None)
    for name, value in extra.items():
        setattr(found, name, value)
    return found


async def test_the_log_channel_pings_nobody() -> None:
    channel = MagicMock(spec=discord.TextChannel)
    bot = MagicMock(spec=commands.Bot)
    # The second record finds the log channel gone, which ends the task.
    bot.get_channel.side_effect = [channel, None]
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

    await asyncio.wait_for(cog._log_task(), TASK_TIMEOUT)

    # The command, the message cut short, and the note that it was.
    posts = channel.send.await_args_list
    texts = [post.args[0] for post in posts]
    assert texts[0] == (
        'Original Command: ;duel challenge <@1234> <@&5678> @everyone\n'
        'Jump Url: https://discord.com/channels/1/2/3'
    )
    assert texts[1].startswith(f'```ansi\n{ANSI_RED}@everyone xxx')
    assert len(texts[1]) == 2000
    assert texts[2] == '`Check logs for full stack trace`'
    assert len(posts) == 3
    for post in posts:
        mentions = post.kwargs['allowed_mentions']
        assert mentions.to_dict() == discord.AllowedMentions.none().to_dict()
