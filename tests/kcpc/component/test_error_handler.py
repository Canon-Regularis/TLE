"""Tests for TLE's bot_error_handler, which replies to discord.py's own errors.

KCPC cogs leave those errors to it (see ``KcpcCog.cog_command_error``), so a
failed permission check gets its reply here. Every reply is an alert only the
member sees on slash, and it names no role or id. Once a slash command's
interaction has expired there is no reply at all: discord.py would post it in
the channel instead, for everyone to see. KCPC's own replies follow that rule
too.

The contexts are real, for prefix and slash invocations of a real hybrid group.
"""

import logging
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.core.errors import KcpcUserError
from tle.util import codeforces_api as cf, db, discord_common
from tle.util.discord_common import (
    ALREADY_RUNNING_MESSAGE,
    NOT_ALLOWED_MESSAGE,
    REFUSAL_DELETE_AFTER,
    REFUSAL_THROTTLE_SECONDS,
    UNEXPECTED_ERROR_MESSAGE,
    AccessDenied,
    PrivateAnswerExpired,
    RefusalThrottle,
    bot_error_handler,
    embed_alert,
)

LOGGER = 'tle.util.discord_common'
KCPC_LOGGER = 'tle.kcpc.bot.cog'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
ROLE_ID = 1_300_000_000_000_000_001
MEMBER_ID = 1_400_000_000_000_000_001
OTHER_MEMBER_ID = 1_400_000_000_000_000_002
DROPPED = 'Dropped the reply to the error in command clist: the interaction expired'
REFUSAL = 'Use this command in a bot channel: <#1>.'

Hybrid = commands.HybridCommand[Any, ..., Any] | commands.HybridGroup[Any, ..., Any]


@commands.hybrid_group(fallback='show')
async def clist(ctx: commands.Context[commands.Bot]) -> None:
    """A group whose slash form is its fallback, /clist show."""


@clist.command()
async def future(ctx: commands.Context[commands.Bot]) -> None:
    """A subcommand, /clist future."""


class MonotonicClock:
    """Stands in for time.monotonic; it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def refusal_clock(monkeypatch: pytest.MonkeyPatch) -> MonotonicClock:
    """A fresh throttle of prefix refusals for each test, on a clock it moves."""
    clock = MonotonicClock()
    throttle = RefusalThrottle(REFUSAL_THROTTLE_SECONDS, clock)
    monkeypatch.setattr(discord_common, 'refusal_throttle', throttle)
    return clock


def picked(command: Hybrid) -> app_commands.Command[Any, ..., Any]:
    """The slash command a member picks to run ``command``."""
    if isinstance(command, commands.HybridGroup):
        assert command.app_command is not None and command.fallback is not None
        fallback = command.app_command.get_command(command.fallback)
        assert isinstance(fallback, app_commands.Command)
        return fallback
    assert command.app_command is not None
    return command.app_command


def make_context(
    *,
    slash: bool = False,
    expired: bool = False,
    command: Hybrid = clist,
    guild_id: int = GUILD_ID,
    member_id: int = MEMBER_ID,
) -> commands.Context[commands.Bot]:
    """A real context of ``command``, run by a member of a server.

    With ``slash``, it belongs to an interaction, as for a slash command, which
    has expired with ``expired``. Replies are recorded by an ``AsyncMock`` in
    place of ``send``.
    """
    guild = MagicMock(spec=discord.Guild, id=guild_id)
    author = MagicMock(spec=discord.Member, id=member_id)
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    message.content = f';{command.qualified_name}'
    message.jump_url = 'https://discord.com/channels/1/2/3'
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.is_expired.return_value = expired
        interaction.command = picked(command)
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=MagicMock(spec=commands.Bot),
        view=StringView(''),
        prefix='/' if slash else ';',
        command=command,
        invoked_with=command.name,
        interaction=interaction,
    )
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def sent(ctx: commands.Context[Any]) -> tuple[str | None, dict[str, Any]]:
    """The text of the one alert sent, and what else it was sent with."""
    send = cast(AsyncMock, ctx.send)
    send.assert_awaited_once()
    assert send.await_args is not None and not send.await_args.args
    options = dict(send.await_args.kwargs)
    embed = options.pop('embed')
    assert isinstance(embed, discord.Embed)
    assert embed.to_dict() == embed_alert(embed.description).to_dict()
    return embed.description, options


def records(
    caplog: pytest.LogCaptureFixture, logger: str = LOGGER
) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == logger]


def logged(
    caplog: pytest.LogCaptureFixture, logger: str = LOGGER
) -> list[tuple[int, str]]:
    return [(record.levelno, record.getMessage()) for record in records(caplog, logger)]


def not_sent(ctx: commands.Context[Any]) -> None:
    cast(AsyncMock, ctx.send).assert_not_awaited()


def cooldown(retry_after: float) -> commands.CommandOnCooldown:
    return commands.CommandOnCooldown(
        commands.Cooldown(1, 60), retry_after, commands.BucketType.user
    )


def http_error() -> discord.NotFound:
    return discord.NotFound(
        MagicMock(status=404, reason='Not Found'), 'Unknown interaction'
    )


# Errors of every branch that replies, one each.
REPLIED = [
    db.DatabaseDisabledError(),
    commands.NoPrivateMessage(),
    commands.DisabledCommand(),
    AccessDenied(REFUSAL),
    cooldown(3),
    commands.MaxConcurrencyReached(1, commands.BucketType.user),
    cf.CodeforcesApiError('Codeforces is down.'),
    commands.BadArgument('Channel "#nope" not found.'),
    commands.CheckFailure(),
    commands.CommandInvokeError(RuntimeError('boom')),
]


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize(
    'error',
    [
        commands.MissingRole('Admin'),
        commands.MissingRole(ROLE_ID),
        commands.MissingAnyRole(['Admin', 'Moderator']),
        commands.MissingPermissions(['manage_guild']),
        commands.CheckAnyFailure([], []),
        commands.CheckFailure('The check functions for command kcpc failed.'),
        commands.CheckFailure(),
        commands.NotOwner(),
        NotKcpcAdmin(),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_failed_check_gets_a_general_reply_only_the_member_sees(
    error: commands.CheckFailure, slash: bool
) -> None:
    ctx = make_context(slash=slash)

    await bot_error_handler(ctx, error)

    assert sent(ctx) == (NOT_ALLOWED_MESSAGE, {'ephemeral': True})
    assert NOT_ALLOWED_MESSAGE == "You can't use this command."


@pytest.mark.parametrize('admin_role', ['Committee', ROLE_ID], ids=['name', 'id'])
async def test_a_failed_check_never_names_the_role_it_wanted(
    monkeypatch: pytest.MonkeyPatch, admin_role: str | int
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', admin_role)

    for error in (commands.MissingRole(admin_role), NotKcpcAdmin()):
        ctx = make_context()

        await bot_error_handler(ctx, error)

        text, _ = sent(ctx)
        assert text is not None and str(admin_role) not in text
    # discord.py's own message names the role, so the reply can't be that.
    assert str(admin_role) in str(commands.MissingRole(admin_role))


async def test_an_unknown_command_is_ignored_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_context()
    ctx.command = None
    ctx.invoked_with = 'nosuchcommand'

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(
            ctx, commands.CommandNotFound('Command "nosuchcommand" is not found')
        )

    not_sent(ctx)
    assert logged(caplog) == [
        (logging.DEBUG, "Ignoring unknown command 'nosuchcommand'")
    ]


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize(
    ('error', 'text'),
    [
        (
            db.DatabaseDisabledError(),
            'Sorry, the database is not available. Some features are disabled.',
        ),
        # A CheckFailure, but with a reply of its own: the slash commands'.
        (
            commands.NoPrivateMessage(),
            'Commands work only in servers, not in private messages.',
        ),
        (commands.DisabledCommand(), 'Sorry, this command is temporarily disabled'),
        (cf.CodeforcesApiError('Codeforces is down.'), 'Codeforces is down.'),
        (
            commands.BadArgument('Channel "#nope" not found.'),
            'Channel "#nope" not found.',
        ),
        (
            commands.MissingRequiredArgument(commands.Parameter('handle', 1)),
            'handle is a required argument that is missing.',
        ),
    ],
    ids=lambda value: type(value).__name__,
)
async def test_the_earlier_replies_keep_their_texts_but_are_private(
    error: Exception, text: str, slash: bool
) -> None:
    ctx = make_context(slash=slash)

    await bot_error_handler(ctx, error)

    assert sent(ctx) == (text, {'ephemeral': True})


@pytest.mark.parametrize(
    'error',
    [
        commands.CheckFailure('Handled already.'),
        AccessDenied(REFUSAL),
        cooldown(3),
        commands.CommandInvokeError(RuntimeError('boom')),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_an_error_a_cog_handled_gets_no_second_reply(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context()
    error.handled = True  # type: ignore[attr-defined]  # TLE's convention

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(ctx, error)

    not_sent(ctx)
    assert logged(caplog) == []


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_any_other_error_is_logged_and_the_member_told_privately(
    slash: bool, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=slash)
    error = commands.CommandInvokeError(RuntimeError('boom'))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(ctx, error)

    assert sent(ctx) == (UNEXPECTED_ERROR_MESSAGE, {'ephemeral': True})
    assert UNEXPECTED_ERROR_MESSAGE == (
        'Something went wrong. The error has been logged.'
    )
    (record,) = records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == 'Ignoring exception in command clist:'
    assert record.exc_info is not None and record.exc_info[1] is error
    assert record.__dict__['message_content'] == ';clist'


async def test_a_kcpc_command_denied_by_its_check_gets_exactly_one_reply() -> None:
    # discord.py calls the cog's handler first, then on_command_error.
    ctx = make_context()
    error = NotKcpcAdmin()

    await KcpcCog(MagicMock(spec=commands.Bot)).cog_command_error(ctx, error)
    await bot_error_handler(ctx, error)

    assert sent(ctx) == (NOT_ALLOWED_MESSAGE, {'ephemeral': True})


# Access refusals


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('text', [None, REFUSAL], ids=['no text', 'text'])
async def test_a_silent_refusal_gets_no_reply_and_only_a_debug_log(
    text: str | None, slash: bool, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=slash)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(ctx, AccessDenied(text, silent=True))

    not_sent(ctx)
    assert logged(caplog) == [
        (
            logging.DEBUG,
            f'Refused command clist to member {MEMBER_ID} in guild {GUILD_ID} silently',
        )
    ]


async def test_a_slash_refusal_is_private_and_stays() -> None:
    ctx = make_context(slash=True)

    await bot_error_handler(ctx, AccessDenied(REFUSAL, delete_after=5))

    assert sent(ctx) == (REFUSAL, {'ephemeral': True})


async def test_a_prefix_refusal_is_deleted_after_20_seconds() -> None:
    ctx = make_context()

    await bot_error_handler(ctx, AccessDenied(REFUSAL))

    assert sent(ctx) == (REFUSAL, {'ephemeral': True, 'delete_after': 20})
    assert REFUSAL_DELETE_AFTER == 20


async def test_a_prefix_refusal_can_say_how_long_it_stays() -> None:
    ctx = make_context()

    await bot_error_handler(ctx, AccessDenied(REFUSAL, delete_after=5))

    assert sent(ctx) == (REFUSAL, {'ephemeral': True, 'delete_after': 5})


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
async def test_a_refusal_without_a_text_gets_the_general_one(slash: bool) -> None:
    ctx = make_context(slash=slash)

    await bot_error_handler(ctx, AccessDenied(None))

    text, _ = sent(ctx)
    assert text == NOT_ALLOWED_MESSAGE


async def test_a_member_gets_one_prefix_refusal_every_30_seconds(
    refusal_clock: MonotonicClock, caplog: pytest.LogCaptureFixture
) -> None:
    first, again, later = make_context(), make_context(), make_context()

    await bot_error_handler(first, AccessDenied(REFUSAL))
    refusal_clock.now += 29.5
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(
            again, AccessDenied('This command is switched off in this server.')
        )
    refusal_clock.now += 0.5
    await bot_error_handler(later, AccessDenied(REFUSAL))

    assert sent(first) == (REFUSAL, {'ephemeral': True, 'delete_after': 20})
    not_sent(again)
    assert logged(caplog) == [
        (
            logging.DEBUG,
            f'Refused command clist to member {MEMBER_ID} in guild {GUILD_ID} '
            'again, without a reply',
        )
    ]
    assert sent(later) == (REFUSAL, {'ephemeral': True, 'delete_after': 20})
    assert REFUSAL_THROTTLE_SECONDS == 30


@pytest.mark.parametrize(
    ('guild_id', 'member_id'),
    [(OTHER_GUILD_ID, MEMBER_ID), (GUILD_ID, OTHER_MEMBER_ID)],
    ids=['same member, other server', 'other member, same server'],
)
async def test_the_throttle_counts_each_member_in_each_server(
    guild_id: int, member_id: int
) -> None:
    await bot_error_handler(make_context(), AccessDenied(REFUSAL))
    other = make_context(guild_id=guild_id, member_id=member_id)

    await bot_error_handler(other, AccessDenied(REFUSAL))

    assert sent(other) == (REFUSAL, {'ephemeral': True, 'delete_after': 20})


async def test_slash_refusals_are_never_throttled() -> None:
    await bot_error_handler(make_context(), AccessDenied(REFUSAL))
    slash = [make_context(slash=True) for _ in range(3)]

    for ctx in slash:
        await bot_error_handler(ctx, AccessDenied(REFUSAL))

    assert [sent(ctx) for ctx in slash] == [(REFUSAL, {'ephemeral': True})] * 3


@pytest.mark.parametrize(
    'earlier',
    [
        lambda: (make_context(slash=True), AccessDenied(REFUSAL)),
        lambda: (make_context(), AccessDenied(None, silent=True)),
    ],
    ids=['after a slash refusal', 'after a silent refusal'],
)
async def test_only_prefix_replies_count_towards_the_throttle(earlier: Any) -> None:
    await bot_error_handler(*earlier())
    ctx = make_context()

    await bot_error_handler(ctx, AccessDenied(REFUSAL))

    assert sent(ctx) == (REFUSAL, {'ephemeral': True, 'delete_after': 20})


# Answers that came too late, cooldowns and commands already running


@pytest.mark.parametrize(
    ('slash', 'expired'),
    [(True, True), (True, False), (False, False)],
    ids=['expired', 'live', 'prefix'],
)
async def test_a_private_answer_that_came_too_late_is_only_logged(
    slash: bool, expired: bool, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=slash, expired=expired)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(ctx, PrivateAnswerExpired())

    not_sent(ctx)
    assert logged(caplog) == [
        (
            logging.INFO,
            'Dropped the private answer to command clist: the interaction expired',
        )
    ]


@pytest.mark.parametrize(
    ('command', 'slash', 'called_as'),
    [
        (clist, False, ';clist'),
        (future, False, ';clist future'),
        (clist, True, '/clist show'),
        (future, True, '/clist future'),
    ],
    ids=['prefix group', 'prefix subcommand', 'slash fallback', 'slash subcommand'],
)
async def test_a_cooldown_says_how_and_when_to_use_the_command_again(
    command: Hybrid, slash: bool, called_as: str
) -> None:
    ctx = make_context(command=command, slash=slash)

    await bot_error_handler(ctx, cooldown(4.2))

    text, _ = sent(ctx)
    assert text == f'You can use `{called_as}` again in 5 seconds.'


async def test_a_prefix_cooldown_reply_goes_when_the_cooldown_ends() -> None:
    ctx = make_context()

    await bot_error_handler(ctx, cooldown(4.2))

    _, options = sent(ctx)
    assert options == {'ephemeral': True, 'delete_after': 4.2}


async def test_a_slash_cooldown_reply_is_private_and_stays() -> None:
    ctx = make_context(slash=True)

    await bot_error_handler(ctx, cooldown(4.2))

    _, options = sent(ctx)
    assert options == {'ephemeral': True}


@pytest.mark.parametrize(
    ('retry_after', 'wait'),
    [(0.2, '1 second'), (1.0, '1 second'), (1.01, '2 seconds'), (59.5, '60 seconds')],
)
async def test_a_cooldown_rounds_the_wait_up(retry_after: float, wait: str) -> None:
    ctx = make_context()

    await bot_error_handler(ctx, cooldown(retry_after))

    text, _ = sent(ctx)
    assert text == f'You can use `;clist` again in {wait}.'


@pytest.mark.parametrize(
    ('slash', 'options'),
    [
        # The channel sees a prefix reply, so it goes after 20 seconds, as a
        # refusal does.
        (False, {'ephemeral': True, 'delete_after': REFUSAL_DELETE_AFTER}),
        (True, {'ephemeral': True}),
    ],
    ids=['prefix', 'slash'],
)
async def test_a_command_already_running_says_so_privately(
    slash: bool, options: dict[str, Any]
) -> None:
    ctx = make_context(slash=slash)
    error = commands.MaxConcurrencyReached(1, commands.BucketType.user)

    await bot_error_handler(ctx, error)

    assert sent(ctx) == (ALREADY_RUNNING_MESSAGE, options)
    assert ALREADY_RUNNING_MESSAGE == (
        'That command is already running. Try again when it finishes.'
    )
    assert REFUSAL_DELETE_AFTER == 20


# Every reply


@pytest.mark.parametrize('error', REPLIED, ids=lambda error: type(error).__name__)
async def test_there_is_no_reply_once_the_interaction_has_expired(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=True, expired=True)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot_error_handler(ctx, error)

    not_sent(ctx)
    assert (logging.INFO, DROPPED) in logged(caplog)


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize('error', REPLIED, ids=lambda error: type(error).__name__)
async def test_a_reply_discord_refuses_is_only_logged(
    error: Exception, slash: bool, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=slash)
    cast(AsyncMock, ctx.send).side_effect = http_error()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot_error_handler(ctx, error)  # raises nothing

    cast(AsyncMock, ctx.send).assert_awaited_once()
    assert [
        message for level, message in logged(caplog) if level == logging.WARNING
    ] == [
        'Could not reply to the error in command clist: '
        '404 Not Found (error code: 0): Unknown interaction'
    ]


@pytest.mark.parametrize('error', REPLIED, ids=lambda error: type(error).__name__)
async def test_a_reply_the_context_wont_send_publicly_is_only_logged(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    # TLE's context raises PrivateAnswerExpired rather than post a private
    # answer publicly, e.g. when the interaction expires during the reply.
    ctx = make_context(slash=True)
    cast(AsyncMock, ctx.send).side_effect = PrivateAnswerExpired()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await bot_error_handler(ctx, error)  # raises nothing

    cast(AsyncMock, ctx.send).assert_awaited_once()
    assert (logging.INFO, DROPPED) in logged(caplog)
    assert not [level for level, _ in logged(caplog) if level == logging.WARNING]


# KCPC's own replies


def kcpc_user_error() -> commands.CommandInvokeError:
    return commands.CommandInvokeError(KcpcUserError("I can't post there."))


async def test_kcpc_sends_no_alert_once_the_interaction_has_expired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_context(slash=True, expired=True)
    error = kcpc_user_error()

    with caplog.at_level(logging.INFO, logger=KCPC_LOGGER):
        await KcpcCog(MagicMock(spec=commands.Bot)).cog_command_error(ctx, error)
    await bot_error_handler(ctx, error)

    not_sent(ctx)
    assert getattr(error, 'handled', False)  # so TLE's handler adds nothing
    assert logged(caplog, KCPC_LOGGER) == [(logging.INFO, DROPPED)]


async def test_kcpc_alerts_a_live_slash_command_privately() -> None:
    ctx = make_context(slash=True)

    await KcpcCog(MagicMock(spec=commands.Bot)).cog_command_error(
        ctx, kcpc_user_error()
    )

    cast(AsyncMock, ctx.send).assert_awaited_once_with(embed=ANY, ephemeral=True)
    assert sent(ctx)[0] == "I can't post there."


@pytest.mark.parametrize(
    'failure',
    [PrivateAnswerExpired(), commands.CommandError('No.'), http_error()],
    ids=lambda failure: type(failure).__name__,
)
async def test_kcpc_logs_an_alert_it_could_not_send(
    failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_context(slash=True)
    cast(AsyncMock, ctx.send).side_effect = failure

    await KcpcCog(MagicMock(spec=commands.Bot)).cog_command_error(
        ctx, kcpc_user_error()
    )  # raises nothing

    assert logged(caplog, KCPC_LOGGER) == [
        (logging.WARNING, f'Could not reply to the error in command clist: {failure}')
    ]
