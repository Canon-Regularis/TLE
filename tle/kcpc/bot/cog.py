"""The base class of KCPC cogs, and how KCPC replies to command errors."""

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot.embeds import alert_embed
from tle.kcpc.core.errors import KcpcUserError

if TYPE_CHECKING:
    from tle.kcpc.services import KcpcServices

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR_MESSAGE = (
    'Something went wrong while running that command. The error has been logged.'
)

# discord.py wraps the exception a command raised, sometimes twice: a hybrid
# command run as a slash command reports HybridCommandError around
# app_commands.CommandInvokeError around the original.
_WRAPPERS = (
    commands.CommandInvokeError,
    commands.HybridCommandError,
    app_commands.CommandInvokeError,
)
_MAX_UNWRAP_DEPTH = 5


def unwrap_error(error: BaseException) -> BaseException:
    """The exception a command raised, without discord.py's wrappers around it."""
    for _ in range(_MAX_UNWRAP_DEPTH):
        if not isinstance(error, _WRAPPERS):
            break
        error = error.original
    return error


class ErrorKind(Enum):
    """How KCPC treats an (unwrapped) error from a command or a component."""

    USER = 'user'  # a KcpcUserError, whose message is meant for the user
    FRAMEWORK = 'framework'  # discord.py's own, e.g. a bad argument or failed check
    UNEXPECTED = 'unexpected'  # a bug: logged, and the user gets an apology


def classify_error(error: BaseException) -> ErrorKind:
    if isinstance(error, KcpcUserError):
        return ErrorKind.USER
    if isinstance(error, (commands.CommandError, app_commands.AppCommandError)):
        return ErrorKind.FRAMEWORK
    return ErrorKind.UNEXPECTED


class KcpcCog(commands.Cog):
    """The base class of KCPC cogs.

    ``services`` gives the running KCPC services. Errors from the cog's
    commands get one reply each: a ``KcpcUserError`` shows its message, a bug
    gets an apology and a logged traceback, and discord.py's own errors are
    left to TLE's ``bot_error_handler``.

    If a KCPC extension fails to load, the bot logs it and carries on without
    it. discord.py runs ``cog_load`` before it registers anything, and never
    calls ``cog_unload`` if ``cog_load`` raises. So ``cog_load`` adds scheduler
    jobs as its last step, or removes (``scheduler.remove``) any it added
    before re-raising; otherwise they would keep running without their cog.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def services(self) -> 'KcpcServices':
        """The KCPC services; ``KcpcDisabledError`` if they are not running."""
        # Imported on use: tle.kcpc.services is the composition root, which
        # builds this package's publisher.
        from tle.kcpc.services import get_services

        return get_services(self.bot)

    async def cog_command_error(
        self, ctx: commands.Context[Any], error: Exception
    ) -> None:
        err = unwrap_error(error)
        kind = classify_error(err)
        if kind is ErrorKind.FRAMEWORK:
            return
        if kind is ErrorKind.USER:
            text = str(err)
        else:
            logger.exception(
                'Unexpected error in command %s', ctx.command, exc_info=err
            )
            text = UNEXPECTED_ERROR_MESSAGE
        # TLE's bot_error_handler skips errors marked handled, as TLE's own
        # cogs mark them, so the user gets exactly one reply.
        error.handled = True  # type: ignore[attr-defined]
        await _send_alert(ctx, text)


async def _send_alert(ctx: commands.Context[Any], text: str) -> None:
    try:
        # Ephemeral for slash commands; prefix commands ignore it.
        await ctx.send(embed=alert_embed(text), ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning(
            'Could not reply to the error in command %s: %s', ctx.command, exc
        )
