import asyncio
import functools
import logging
import math
import random
import time
from collections.abc import Callable
from typing import Any

import discord
from discord.ext import commands

from tle import constants
from tle.util import codeforces_api as cf, db

logger = logging.getLogger(__name__)

_CF_COLORS = (0xFFCA1F, 0x198BCC, 0xFF2020)
_SUCCESS_GREEN = 0x28A745
_ALERT_AMBER = 0xFFBF00

# Replies to command errors. They never name a role or an id.
NOT_ALLOWED_MESSAGE = "You can't use this command."
ALREADY_RUNNING_MESSAGE = 'That command is already running. Try again when it finishes.'
UNEXPECTED_ERROR_MESSAGE = 'Something went wrong. The error has been logged.'
# For a command used in private messages, slash or not.
PRIVATE_MESSAGES_MESSAGE = 'Commands work only in servers, not in private messages.'
# For a command that makes the channel it is used in the one the bot posts in,
# used in a thread: discord.py forgets a thread once it closes after a while
# without messages, so the bot would no longer find it.
NOT_IN_A_THREAD_MESSAGE = (
    'Use this command in a channel, not a thread: a thread closes after a '
    "while, and the bot then can't find it."
)

# How long the refusal of a prefix command stays in the channel, unless the
# refusal says otherwise, and how often one member gets one in a server.
REFUSAL_DELETE_AFTER = 20.0
REFUSAL_THROTTLE_SECONDS = 30.0


class AccessDenied(commands.CheckFailure):
    """The access rules refuse a command; ``text`` is the reply to the member.

    With ``silent`` there is no reply at all, as if the command did not exist;
    without it, a missing ``text`` gets ``NOT_ALLOWED_MESSAGE``.
    ``bot_error_handler`` sends the reply privately on slash. For a prefix
    command the channel sees it, so it is deleted after ``delete_after``
    seconds (by default ``REFUSAL_DELETE_AFTER``), and a member gets at most
    one in a server every ``REFUSAL_THROTTLE_SECONDS``.
    """

    def __init__(
        self,
        text: str | None,
        *,
        silent: bool = False,
        delete_after: float | None = None,
    ) -> None:
        super().__init__(text)
        self.text = text
        self.silent = silent
        self.delete_after = delete_after


class PrivateAnswerExpired(commands.CommandError):
    """A private answer came after the interaction expired.

    It is dropped, never posted publicly: discord.py would otherwise post it
    in the channel, for everyone to see.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or 'The interaction expired before its private answer was sent.'
        )


class RefusalThrottle:
    """Lets one refusal per key through in each ``window`` seconds.

    Keys are (guild id, member id). Keys whose window has passed are
    forgotten, so it holds only the refusals of the last ``window`` seconds.
    """

    def __init__(
        self, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.window = window
        self._clock = clock
        self._sent: dict[tuple[int | None, int], float] = {}

    def allow(self, key: tuple[int | None, int]) -> bool:
        """Whether a refusal for ``key`` may be sent now; if so, it counts."""
        now = self._clock()
        self._sent = {
            seen: at for seen, at in self._sent.items() if now - at < self.window
        }
        if key in self._sent:
            return False
        self._sent[key] = now
        return True

    def clear(self) -> None:
        self._sent.clear()

    def __len__(self) -> int:
        return len(self._sent)


# For the refusals of prefix commands, which the channel sees. Slash refusals
# are private, and each gets its reply.
refusal_throttle = RefusalThrottle(REFUSAL_THROTTLE_SECONDS)


def embed_neutral(desc: object, color: int | None = None) -> discord.Embed:
    return discord.Embed(description=str(desc), color=color)


def embed_success(desc: object) -> discord.Embed:
    return discord.Embed(description=str(desc), color=_SUCCESS_GREEN)


def embed_alert(desc: object) -> discord.Embed:
    return discord.Embed(description=str(desc), color=_ALERT_AMBER)


def random_cf_color() -> int:
    return random.choice(_CF_COLORS)


def cf_color_embed(**kwargs: Any) -> discord.Embed:
    return discord.Embed(**kwargs, color=random_cf_color())


def set_same_cf_color(embeds: list[discord.Embed]) -> None:
    color = random_cf_color()
    for embed in embeds:
        embed.color = color


def attach_image(embed: discord.Embed, img_file: discord.File) -> None:
    embed.set_image(url=f'attachment://{img_file.filename}')


def set_author_footer(
    embed: discord.Embed, user: discord.Member | discord.User
) -> None:
    embed.set_footer(text=f'Requested by {user}', icon_url=user.display_avatar.url)


def get_role(guild: discord.Guild, role_identifier: str | int) -> discord.Role | None:
    """Look up a role by name (str) or ID (int)."""
    if isinstance(role_identifier, int):
        return guild.get_role(role_identifier)
    return discord.utils.get(guild.roles, name=role_identifier)


def has_role(member: discord.Member, role_identifier: str | int) -> bool:
    """Check if member has a role identified by name (str) or ID (int).

    The server's default role never counts. Every member has @everyone, whose
    ID is the server's and whose name is '@everyone', so a setting naming it
    would give its role to everyone. Its ID is compared, rather than asking
    ``Role.is_default``, which stand-ins for roles answer truthily.
    """
    everyone = member.guild.id
    return any(
        role.id != everyone and _is_role(role, role_identifier) for role in member.roles
    )


def _is_role(role: discord.Role, role_identifier: str | int) -> bool:
    if isinstance(role_identifier, int):
        return role.id == role_identifier
    return role.name == role_identifier


def _tle_roles() -> list[str | int]:
    """TLE's own roles, by name or id, as configured right now."""
    roles: list[str | int | None] = [
        constants.TLE_ADMIN,
        constants.TLE_MODERATOR,
        constants.TLE_TRUSTED,
        constants.TLE_PURGATORY,
        # An id, or None when no developer role is configured.
        constants.TLE_DEVELOPER,
    ]
    return [role for role in roles if role is not None]


def self_assignable_problem(role: discord.Role, me: discord.Member) -> str | None:
    """Why self-service commands must not hand out ``role``, or None if they may.

    ``me`` is the bot in the role's server. The reason is a short clause,
    e.g. 'everyone has it already'. Refused are @everyone, roles managed by
    Discord or an integration, roles at or above the bot's highest role, TLE's
    admin, moderator, trusted, purgatory and developer roles, roles with
    permissions @everyone lacks, and roles that channels set permissions for.
    """
    problem = _unmanageable_problem(role, me)
    if problem is not None:
        return problem
    everyone = role.guild.default_role
    if role.permissions.value & ~everyone.permissions.value:
        return 'it grants permissions beyond what everyone has'
    if any(
        not channel.overwrites_for(role).is_empty() for channel in role.guild.channels
    ):
        return 'some channels set permissions for it'
    return None


def self_removable_problem(role: discord.Role, me: discord.Member) -> str | None:
    """Why self-service commands must not take ``role`` away, or None if they may.

    Losing a role lowers a member's rights, unless a channel denies the role
    a permission, or the role is one of TLE's, such as purgatory. So a role
    with permissions beyond @everyone's, or that channels only allow more, may
    be taken away though it may not be handed out (``self_assignable_problem``).
    Refused are @everyone, roles managed by Discord or an integration, roles
    at or above the bot's highest role, TLE's roles, and roles that a channel
    denies a permission.
    """
    problem = _unmanageable_problem(role, me)
    if problem is not None:
        return problem
    if any(
        channel.overwrites_for(role).pair()[1].value for channel in role.guild.channels
    ):
        return 'some channels deny it permissions'
    return None


def _unmanageable_problem(role: discord.Role, me: discord.Member) -> str | None:
    """Why self-service commands must not change who has ``role`` at all."""
    if role.is_default():
        return 'everyone has it already'
    if role.managed:
        return 'it is managed by Discord or an integration'
    if role >= me.top_role:
        return 'it is not below my highest role'
    if any(_is_role(role, tle_role) for tle_role in _tle_roles()):
        return 'the bot uses it to decide what members may do'
    return None


def _interaction(ctx: commands.Context[Any]) -> discord.Interaction | None:
    """The interaction of a slash command's context; None for a prefix one."""
    # getattr: not every stand-in for a context has the attribute.
    interaction: discord.Interaction | None = getattr(ctx, 'interaction', None)
    return interaction


def _expired(ctx: commands.Context[Any]) -> bool:
    """Whether ``ctx`` belongs to an interaction that has expired.

    discord.py then sends a reply as a message in the channel, for everyone to
    see, so error replies are dropped instead. Only a real True from
    ``is_expired`` counts.
    """
    interaction = _interaction(ctx)
    return interaction is not None and interaction.is_expired() is True


async def _reply(
    ctx: commands.Context[Any], message: object, *, delete_after: float | None = None
) -> None:
    """Answer a command error in an alert embed, privately on slash.

    Nothing is sent once the interaction has expired, and a reply that fails
    is only logged: it must not raise a second error from an error handler.
    """
    if _expired(ctx):
        logger.info(
            'Dropped the reply to the error in command %s: the interaction expired',
            ctx.command,
        )
        return
    embed = embed_alert(message)
    try:
        if delete_after is None:
            await ctx.send(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed, ephemeral=True, delete_after=delete_after)
    except PrivateAnswerExpired:
        logger.info(
            'Dropped the reply to the error in command %s: the interaction expired',
            ctx.command,
        )
    except discord.HTTPException as exc:
        logger.warning(
            'Could not reply to the error in command %s: %s', ctx.command, exc
        )


def send_error_if(*error_cls: type[Exception]) -> Callable[..., Any]:
    """Decorator for `cog_command_error` methods.

    Decorated methods answer the error privately in an alert embed when the
    error is an instance of one of the specified errors, otherwise the wrapped
    function is invoked.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(cog: Any, ctx: commands.Context, error: Exception) -> None:
            if isinstance(error, error_cls):
                # First, so that bot_error_handler never adds a second reply,
                # even if this one fails.
                error.handled = True
                await _reply(ctx, error)
            else:
                await func(cog, ctx, error)

        return wrapper

    return decorator


def undo_cooldown(ctx: commands.Context[Any]) -> None:
    """Give back the use of ``ctx``'s command that its cooldown counted.

    For a refusal that cost the bot nothing, such as one that comes before any
    request to Codeforces: a cooldown keeps heavy commands from asking too
    often, so a mistake shouldn't hold the member back, nor, for a cooldown
    shared by the server, everyone else.
    """
    if ctx.command is not None:
        ctx.command.reset_cooldown(ctx)


def _called_as(ctx: commands.Context[Any]) -> str:
    """How the member calls the command: ``/a b`` on slash, else ``;a b``.

    On slash it is the command the member picked, e.g. ``/clist show`` for the
    group ``clist``.
    """
    interaction = _interaction(ctx)
    if interaction is None:
        return f';{ctx.command.qualified_name}'
    command = interaction.command or ctx.command
    return f'/{command.qualified_name}'


async def _refuse(ctx: commands.Context[Any], denied: AccessDenied) -> None:
    """Send an access refusal: private on slash, short-lived and rare on prefix."""
    guild_id = ctx.guild.id if ctx.guild is not None else None
    if denied.silent:
        logger.debug(
            'Refused command %s to member %s in guild %s silently',
            ctx.command,
            ctx.author.id,
            guild_id,
        )
        return
    text = denied.text or NOT_ALLOWED_MESSAGE
    if _interaction(ctx) is not None:
        await _reply(ctx, text)
    elif refusal_throttle.allow((guild_id, ctx.author.id)):
        delete_after = denied.delete_after
        if delete_after is None:
            delete_after = REFUSAL_DELETE_AFTER
        await _reply(ctx, text, delete_after=delete_after)
    else:
        logger.debug(
            'Refused command %s to member %s in guild %s again, without a reply',
            ctx.command,
            ctx.author.id,
            guild_id,
        )


async def bot_error_handler(ctx: commands.Context, exception: Exception) -> None:
    if getattr(exception, 'handled', False):
        # Errors already handled in cogs should have .handled = True
        return

    if isinstance(exception, db.DatabaseDisabledError):
        await _reply(
            ctx, 'Sorry, the database is not available. Some features are disabled.'
        )
    elif isinstance(exception, commands.NoPrivateMessage):
        await _reply(ctx, PRIVATE_MESSAGES_MESSAGE)
    elif isinstance(exception, commands.DisabledCommand):
        await _reply(ctx, 'Sorry, this command is temporarily disabled')
    elif isinstance(exception, AccessDenied):
        await _refuse(ctx, exception)
    elif isinstance(exception, PrivateAnswerExpired):
        logger.info(
            'Dropped the private answer to command %s: the interaction expired',
            ctx.command,
        )
    elif isinstance(exception, commands.CommandOnCooldown):
        seconds = max(1, math.ceil(exception.retry_after))
        unit = 'second' if seconds == 1 else 'seconds'
        text = f'You can use `{_called_as(ctx)}` again in {seconds} {unit}.'
        if _interaction(ctx) is None:
            # Gone when the command can be used again.
            await _reply(ctx, text, delete_after=exception.retry_after)
        else:
            await _reply(ctx, text)
    elif isinstance(exception, commands.MaxConcurrencyReached):
        if _interaction(ctx) is None:
            # The channel sees it, so it goes as a refusal does.
            await _reply(
                ctx, ALREADY_RUNNING_MESSAGE, delete_after=REFUSAL_DELETE_AFTER
            )
        else:
            await _reply(ctx, ALREADY_RUNNING_MESSAGE)
    elif isinstance(exception, (cf.CodeforcesApiError, commands.UserInputError)):
        await _reply(ctx, exception)
    elif isinstance(exception, commands.CommandNotFound):
        logger.debug('Ignoring unknown command %r', ctx.invoked_with)
    elif isinstance(exception, commands.CheckFailure):
        # Their own messages, e.g. MissingRole's, can name roles and ids.
        await _reply(ctx, NOT_ALLOWED_MESSAGE)
    else:
        msg = 'Ignoring exception in command {}:'.format(ctx.command)
        exc_info = type(exception), exception, exception.__traceback__
        extra = {
            'message_content': ctx.message.content,
            'jump_url': ctx.message.jump_url,
        }
        logger.exception(msg, exc_info=exc_info, extra=extra)
        await _reply(ctx, UNEXPECTED_ERROR_MESSAGE)


def once(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator that wraps a coroutine such that it is executed only once."""
    first = True

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> None:
        nonlocal first
        if first:
            first = False
            await func(*args, **kwargs)

    return wrapper


def presence_candidates(bot: Any) -> list[discord.Member]:
    """The members whose names the bot's status may show.

    Members in purgatory are left out, and so are the members of servers the
    bot's access service doesn't allow (``guild_allowed``). Without an access
    service, every server counts.
    """
    access = getattr(bot, 'access', None)
    return [
        member
        for member in bot.get_all_members()
        if (access is None or access.guild_allowed(member.guild.id))
        and not has_role(member, constants.TLE_PURGATORY)
    ]


async def presence(bot: Any) -> None:
    # Imported here: at module level, importing this module before
    # codeforces_common fails on the import cycle tasks -> codeforces_common
    # -> cache -> tasks.
    from tle.util import tasks

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening, name='your commands'
        )
    )
    await asyncio.sleep(60)

    @tasks.task(name='OrzUpdate', waiter=tasks.Waiter.fixed_delay(10 * 60))
    async def presence_task(_: Any) -> None:
        candidates = presence_candidates(bot)
        if not candidates:
            return
        target = random.choice(candidates)
        await bot.change_presence(
            activity=discord.Game(name=f'{target.display_name} orz')
        )

    presence_task.start()
