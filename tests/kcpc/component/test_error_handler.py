"""Tests for TLE's bot_error_handler, which replies to discord.py's own errors.

KCPC cogs leave those errors to it (see ``KcpcCog.cog_command_error``), so a
failed permission check gets its reply here.
"""

import logging
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from tle import constants
from tle.kcpc.bot.checks import NotKcpcAdmin
from tle.kcpc.bot.cog import KcpcCog
from tle.util import db
from tle.util.discord_common import bot_error_handler, embed_alert

LOGGER = 'tle.util.discord_common'


def make_ctx() -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.send = AsyncMock()
    ctx.command = 'kcpc status'
    ctx.invoked_with = 'nosuchcommand'
    ctx.message = MagicMock(spec=discord.Message)
    ctx.message.content = ';nosuchcommand'
    ctx.message.jump_url = 'https://discord.com/channels/1/2/3'
    return ctx


def reply(ctx: MagicMock) -> discord.Embed:
    """The embed of the one reply sent."""
    ctx.send.assert_awaited_once()
    embed = ctx.send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


def records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == LOGGER]


@pytest.mark.parametrize(
    'error',
    [
        commands.MissingRole('Admin'),
        commands.MissingRole(1_300_000_000_000_000_001),
        commands.MissingAnyRole(['Admin', 'Moderator']),
        commands.MissingPermissions(['manage_guild']),
        commands.CheckAnyFailure([], []),
        commands.CheckFailure('The check functions for command kcpc failed.'),
        NotKcpcAdmin(),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_failed_check_gets_its_message_only_to_the_user(
    error: commands.CheckFailure,
) -> None:
    ctx = make_ctx()

    await bot_error_handler(ctx, error)

    ctx.send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    assert str(error)  # each of these explains what is missing
    assert reply(ctx).to_dict() == embed_alert(str(error)).to_dict()


async def test_a_kcpc_admin_check_says_who_may_run_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    ctx = make_ctx()

    await bot_error_handler(ctx, NotKcpcAdmin())

    assert reply(ctx).description == (
        'You need the Manage Server permission or the Admin role to do that.'
    )


@pytest.mark.parametrize(
    'error',
    [commands.CheckFailure(), commands.NotOwner()],
    ids=lambda error: type(error).__name__,
)
async def test_a_failed_check_without_a_message_gets_a_general_one(
    error: commands.CheckFailure,
) -> None:
    ctx = make_ctx()

    await bot_error_handler(ctx, error)

    ctx.send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    assert reply(ctx).description == "You can't use this command here."


async def test_no_private_message_keeps_its_own_reply() -> None:
    ctx = make_ctx()

    await bot_error_handler(ctx, commands.NoPrivateMessage())

    ctx.send.assert_awaited_once_with(embed=ANY)
    assert reply(ctx).description == 'Commands are disabled in private channels'


async def test_an_unknown_command_is_ignored_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_ctx()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(
            ctx, commands.CommandNotFound('Command "nosuchcommand" is not found')
        )

    ctx.send.assert_not_awaited()
    assert [(r.levelno, r.getMessage()) for r in records(caplog)] == [
        (logging.DEBUG, "Ignoring unknown command 'nosuchcommand'")
    ]


@pytest.mark.parametrize(
    ('error', 'text'),
    [
        (
            db.DatabaseDisabledError(),
            'Sorry, the database is not available. Some features are disabled.',
        ),
        (commands.DisabledCommand(), 'Sorry, this command is temporarily disabled'),
        (
            commands.BadArgument('Channel "#nope" not found.'),
            'Channel "#nope" not found.',
        ),
    ],
    ids=lambda value: type(value).__name__,
)
async def test_the_earlier_replies_are_unchanged(error: Exception, text: str) -> None:
    ctx = make_ctx()

    await bot_error_handler(ctx, error)

    ctx.send.assert_awaited_once_with(embed=ANY)
    assert reply(ctx).description == text


async def test_an_error_a_cog_handled_gets_no_second_reply() -> None:
    ctx = make_ctx()
    error = commands.CheckFailure('Handled already.')
    error.handled = True  # type: ignore[attr-defined]  # TLE's convention

    await bot_error_handler(ctx, error)

    ctx.send.assert_not_awaited()


async def test_any_other_error_is_logged_with_the_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_ctx()
    error = commands.CommandInvokeError(RuntimeError('boom'))

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await bot_error_handler(ctx, error)

    ctx.send.assert_not_awaited()
    (record,) = records(caplog)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == 'Ignoring exception in command kcpc status:'
    assert record.exc_info is not None and record.exc_info[1] is error
    assert record.__dict__['message_content'] == ';nosuchcommand'


async def test_a_kcpc_command_denied_by_its_check_gets_exactly_one_reply() -> None:
    # discord.py calls the cog's handler first, then on_command_error.
    ctx = make_ctx()
    error = NotKcpcAdmin()

    await KcpcCog(MagicMock(spec=commands.Bot)).cog_command_error(ctx, error)
    await bot_error_handler(ctx, error)

    ctx.send.assert_awaited_once_with(embed=ANY, ephemeral=True)
    assert reply(ctx).description == str(error)
