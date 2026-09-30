"""Tests for tle.kcpc.bot's embeds, checks, cog error replies and view errors.

The toolkit copies a few helpers from TLE's discord_common rather than import
it; the tests compare each copy with the original.
"""

import logging
import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.kcpc.bot.checks import (
    NotKcpcAdmin,
    ensure_kcpc_admin,
    is_kcpc_admin,
    kcpc_admin_only,
)
from tle.kcpc.bot.cog import (
    UNEXPECTED_ERROR_MESSAGE,
    ErrorKind,
    KcpcCog,
    classify_error,
    unwrap_error,
)
from tle.kcpc.bot.embeds import (
    ALERT_COLOR,
    KCPC_COLOR,
    MARKER_RE,
    SUCCESS_COLOR,
    alert_embed,
    find_batch_marker,
    info_embed,
    success_embed,
    to_embed,
)
from tle.kcpc.bot.views import KcpcView, reply_to_interaction_error
from tle.kcpc.core.errors import (
    ConfigError,
    ExternalServiceError,
    KcpcDisabledError,
    KcpcUserError,
)
from tle.kcpc.core.messages import (
    FIELD_COUNT_LIMIT,
    FOOTER_LIMIT,
    TITLE_LIMIT,
    EmbedField,
    OutgoingMessage,
)
from tle.util import discord_common

E = TypeVar('E', bound=discord.HTTPException)

BATCH = '0123abcd'
ADMIN_ROLE_ID = 1_300_000_000_000_000_001

MESSAGE = OutgoingMessage(
    title='Graphs 101',
    description='Shortest paths, from BFS to Dijkstra.',
    url='https://lu.ma/kcpc-graphs',
    fields=(
        EmbedField('When', 'Thursday 18:00', inline=True),
        EmbedField('Where', 'Bush House'),
    ),
    footer='KCPC workshops',
)


def message_with(*embeds: discord.Embed) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.embeds = list(embeds)
    return message


def footer_embed(text: str) -> discord.Embed:
    return discord.Embed().set_footer(text=text)


def test_to_embed_renders_the_message() -> None:
    embed = to_embed(MESSAGE)

    assert embed.title == 'Graphs 101'
    assert embed.description == 'Shortest paths, from BFS to Dijkstra.'
    assert embed.url == 'https://lu.ma/kcpc-graphs'
    assert embed.color == discord.Colour(KCPC_COLOR)
    assert [(f.name, f.value, f.inline) for f in embed.fields] == [
        ('When', 'Thursday 18:00', True),
        ('Where', 'Bush House', False),
    ]
    assert embed.footer.text == 'KCPC workshops'


@pytest.mark.parametrize('color', [0x000000, 0xFF0000])
def test_to_embed_keeps_the_messages_colour(color: int) -> None:
    embed = to_embed(OutgoingMessage(title='x', color=color))

    assert embed.color == discord.Colour(color)


def test_to_embed_leaves_out_what_discord_would_reject() -> None:
    # Discord would refuse the whole post over either of these.
    embed = to_embed(OutgoingMessage(title='x', url='lu.ma/x', color=-1))

    assert embed.url is None
    assert embed.color == discord.Colour(KCPC_COLOR)
    assert 'url' not in embed.to_dict()


@pytest.mark.parametrize(
    ('footer', 'expected'),
    [('KCPC workshops', 'KCPC workshops · ref 0123abcd'), (None, 'ref 0123abcd')],
)
def test_to_embed_ends_the_footer_with_the_batch_marker(
    footer: str | None, expected: str
) -> None:
    embed = to_embed(OutgoingMessage(title='x', footer=footer), batch=BATCH)

    assert embed.footer.text == expected


def test_to_embed_without_a_footer_or_batch_has_no_footer() -> None:
    assert to_embed(OutgoingMessage(title='x')).footer.text is None


@pytest.mark.parametrize('batch', ['0123ABCD', '0123abc', '0123abcde', 'ref 0123', ''])
def test_to_embed_rejects_a_batch_the_reconciler_could_not_find(batch: str) -> None:
    with pytest.raises(ValueError, match='8 lowercase hex digits'):
        to_embed(MESSAGE, batch=batch)


def test_to_embed_fits_the_message_to_discords_limits() -> None:
    message = OutgoingMessage(
        title='t' * (TITLE_LIMIT + 10),
        fields=tuple(EmbedField(f'n{i}', 'v') for i in range(FIELD_COUNT_LIMIT + 5)),
        footer='f' * FOOTER_LIMIT,
    )

    embed = to_embed(message, batch=BATCH)

    assert embed.title is not None and len(embed.title) == TITLE_LIMIT
    assert embed.title.endswith('…')
    assert len(embed.fields) == FIELD_COUNT_LIMIT
    # The shortened footer leaves room for the marker, which survives whole.
    assert embed.footer.text is not None
    assert len(embed.footer.text) <= FOOTER_LIMIT
    assert embed.footer.text.endswith(' · ref 0123abcd')


def test_marker_pattern() -> None:
    assert MARKER_RE.pattern == r'\bref ([0-9a-f]{8})\b'


def test_find_batch_marker_reads_back_what_to_embed_wrote() -> None:
    assert find_batch_marker(message_with(to_embed(MESSAGE, batch=BATCH))) == BATCH


def test_find_batch_marker_searches_every_embed() -> None:
    message = message_with(
        discord.Embed(title='no footer'), to_embed(MESSAGE, batch=BATCH)
    )

    assert find_batch_marker(message) == BATCH


def test_find_batch_marker_prefers_the_marker_at_the_end_of_the_footer() -> None:
    embed = to_embed(
        OutgoingMessage(title='x', footer='Replaces ref 99999999'), batch=BATCH
    )

    assert find_batch_marker(message_with(embed)) == BATCH


@pytest.mark.parametrize(
    'footer',
    [
        'KCPC workshops',
        'ref 0123ABCD',  # upper case
        'ref 0123abcd9',  # nine digits
        'xref 0123abcd',  # not a word on its own
        'ref: 0123abcd',
    ],
)
def test_find_batch_marker_ignores_footers_without_a_marker(footer: str) -> None:
    assert find_batch_marker(message_with(footer_embed(footer))) is None


def test_find_batch_marker_without_embeds() -> None:
    assert find_batch_marker(message_with()) is None
    assert find_batch_marker(message_with(discord.Embed(title='plain'))) is None


def test_reply_embeds() -> None:
    info = info_embed('KCPC settings', 'All features are off.')
    assert (info.title, info.description) == ('KCPC settings', 'All features are off.')
    assert info.color == discord.Colour(KCPC_COLOR)
    empty = info_embed()
    assert (empty.title, empty.description) == (None, None)

    success = success_embed('Saved.')
    assert (success.description, success.color) == (
        'Saved.',
        discord.Colour(SUCCESS_COLOR),
    )

    alert = alert_embed('Nope.')
    assert (alert.description, alert.color) == ('Nope.', discord.Colour(ALERT_COLOR))


def test_success_and_alert_embeds_match_tles() -> None:
    assert success_embed('Saved.').to_dict() == (
        discord_common.embed_success('Saved.').to_dict()
    )
    assert (
        alert_embed('Nope.').to_dict() == discord_common.embed_alert('Nope.').to_dict()
    )


def make_role(role_id: int, name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name  # not MagicMock(name=...), which names the mock itself
    return role


def make_member(*roles: MagicMock, manage_guild: bool = False) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    member.guild_permissions = discord.Permissions(manage_guild=manage_guild)
    member.roles = list(roles)
    return member


@pytest.fixture
def admin_role_name(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    return 'Admin'


def test_a_member_who_can_manage_the_server_is_an_admin(admin_role_name: str) -> None:
    assert is_kcpc_admin(make_member(manage_guild=True))


def test_a_member_with_the_admin_role_is_an_admin(admin_role_name: str) -> None:
    assert is_kcpc_admin(make_member(make_role(1, 'Member'), make_role(2, 'Admin')))


def test_an_ordinary_member_is_not_an_admin(admin_role_name: str) -> None:
    assert not is_kcpc_admin(make_member(make_role(1, 'Member')))
    assert not is_kcpc_admin(make_member())


def test_a_user_outside_a_server_is_not_an_admin(admin_role_name: str) -> None:
    assert not is_kcpc_admin(MagicMock(spec=discord.User))


def test_an_admin_role_id_matches_by_id_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', ADMIN_ROLE_ID)

    assert is_kcpc_admin(make_member(make_role(ADMIN_ROLE_ID, 'Committee')))
    assert not is_kcpc_admin(make_member(make_role(5, str(ADMIN_ROLE_ID))))


def test_the_admin_role_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    member = make_member(make_role(1, 'Committee'))
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    assert not is_kcpc_admin(member)

    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Committee')
    assert is_kcpc_admin(member)


@pytest.mark.parametrize(
    'admin_role', ['Admin', 'Committee', 7, ADMIN_ROLE_ID, str(ADMIN_ROLE_ID)]
)
def test_the_role_rule_matches_tles(
    monkeypatch: pytest.MonkeyPatch, admin_role: str | int
) -> None:
    member = make_member(make_role(7, 'Committee'), make_role(ADMIN_ROLE_ID, 'Admin'))
    monkeypatch.setattr(constants, 'TLE_ADMIN', admin_role)

    assert is_kcpc_admin(member) == discord_common.has_role(member, admin_role)


def make_ctx(author: object) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.author = author
    ctx.command = 'kcpc show'
    ctx.send = AsyncMock()
    return ctx


async def test_ensure_kcpc_admin_passes_an_admin(admin_role_name: str) -> None:
    assert await ensure_kcpc_admin(make_ctx(make_member(manage_guild=True)))


async def test_ensure_kcpc_admin_names_the_admin_role(admin_role_name: str) -> None:
    with pytest.raises(NotKcpcAdmin) as caught:
        await ensure_kcpc_admin(make_ctx(make_member()))

    assert str(caught.value) == (
        'You need the Manage Server permission or the Admin role to do that.'
    )
    # A CheckFailure, so TLE's bot_error_handler shows the message.
    assert isinstance(caught.value, commands.CheckFailure)


async def test_ensure_kcpc_admin_mentions_an_admin_role_given_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(constants, 'TLE_ADMIN', ADMIN_ROLE_ID)

    with pytest.raises(NotKcpcAdmin, match=f'or the <@&{ADMIN_ROLE_ID}> role to'):
        await ensure_kcpc_admin(make_ctx(make_member()))


def test_kcpc_admin_only_adds_the_admin_check() -> None:
    @kcpc_admin_only()
    @commands.command()
    async def settings(ctx: commands.Context[Any]) -> None:
        pass

    assert settings.checks == [ensure_kcpc_admin]


async def kcpc_command(interaction: discord.Interaction) -> None:
    """An app command callback, for building app_commands.CommandInvokeError."""


APP_COMMAND: app_commands.Command[Any, ..., None] = app_commands.Command(
    name='kcpc', description='KCPC', callback=kcpc_command
)


# How discord.py reports an exception from a command's callback.
Wrapper = Callable[[Exception], commands.CommandError]


def slash_error(original: Exception) -> commands.HybridCommandError:
    """What a hybrid command run as a slash command reports for ``original``."""
    return commands.HybridCommandError(
        app_commands.CommandInvokeError(APP_COMMAND, original)
    )


def test_unwrap_error_leaves_other_errors_alone() -> None:
    error = ValueError('boom')

    assert unwrap_error(error) is error


def test_unwrap_error_removes_a_prefix_commands_wrapper() -> None:
    error = KcpcUserError('Unknown feature')

    assert unwrap_error(commands.CommandInvokeError(error)) is error


def test_unwrap_error_removes_a_slash_commands_two_wrappers() -> None:
    error = KcpcUserError('Unknown feature')

    assert unwrap_error(slash_error(error)) is error


def test_unwrap_error_keeps_an_app_command_error_that_is_not_a_wrapper() -> None:
    error = app_commands.CheckFailure('No.')

    assert unwrap_error(commands.HybridCommandError(error)) is error


def test_unwrap_error_stops_after_five_wrappers() -> None:
    innermost = commands.CommandInvokeError(ValueError('deep'))
    error: Exception = innermost
    for _ in range(5):
        error = commands.CommandInvokeError(error)

    assert unwrap_error(error) is innermost


@pytest.mark.parametrize(
    ('error', 'kind'),
    [
        (KcpcUserError('x'), ErrorKind.USER),
        (ExternalServiceError('AtCoder', 'AtCoder is down.'), ErrorKind.USER),
        (KcpcDisabledError(), ErrorKind.USER),
        (commands.BadArgument('x'), ErrorKind.FRAMEWORK),
        (NotKcpcAdmin(), ErrorKind.FRAMEWORK),
        (app_commands.CheckFailure(), ErrorKind.FRAMEWORK),
        (ConfigError('x'), ErrorKind.UNEXPECTED),
        (ValueError('x'), ErrorKind.UNEXPECTED),
    ],
)
def test_classify_error(error: Exception, kind: ErrorKind) -> None:
    assert classify_error(error) is kind


@pytest.fixture
def cog() -> KcpcCog:
    return KcpcCog(MagicMock(spec=commands.Bot))


def ephemeral_alert(send: AsyncMock) -> str | None:
    """The text of the one alert embed sent with ``send``, checked to be ephemeral."""
    send.assert_awaited_once()
    assert send.await_args is not None
    assert send.await_args.kwargs['ephemeral'] is True
    embed = send.await_args.kwargs['embed']
    assert embed.color == discord.Colour(ALERT_COLOR)
    description: str | None = embed.description
    return description


@pytest.mark.parametrize(
    'wrap', [commands.CommandInvokeError, slash_error], ids=['prefix', 'slash']
)
async def test_a_user_error_is_shown_to_the_user(
    cog: KcpcCog, wrap: Wrapper, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_ctx(make_member())
    error = wrap(ExternalServiceError('AtCoder', 'AtCoder is not responding.'))

    await cog.cog_command_error(ctx, error)

    assert ephemeral_alert(ctx.send) == 'AtCoder is not responding.'
    assert getattr(error, 'handled', False) is True
    assert not caplog.records


@pytest.mark.parametrize(
    'error',
    [
        commands.BadArgument('Channel "x" not found.'),
        NotKcpcAdmin(),
        commands.HybridCommandError(app_commands.CheckFailure('No.')),
    ],
)
async def test_discord_errors_are_left_to_tles_handler(
    cog: KcpcCog, error: commands.CommandError
) -> None:
    ctx = make_ctx(make_member())

    await cog.cog_command_error(ctx, error)

    ctx.send.assert_not_awaited()
    assert not getattr(error, 'handled', False)


@pytest.mark.parametrize(
    'wrap', [commands.CommandInvokeError, slash_error], ids=['prefix', 'slash']
)
async def test_a_bug_is_logged_and_the_user_gets_an_apology(
    cog: KcpcCog, wrap: Wrapper, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_ctx(make_member())
    bug = ZeroDivisionError('division by zero')
    error = wrap(bug)

    await cog.cog_command_error(ctx, error)

    assert ephemeral_alert(ctx.send) == UNEXPECTED_ERROR_MESSAGE
    assert getattr(error, 'handled', False) is True
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == 'Unexpected error in command kcpc show'
    assert record.exc_info is not None and record.exc_info[1] is bug


def discord_error(cls: type[E], status: int) -> E:
    return cls(MagicMock(status=status, reason='Error'), 'Unknown interaction')


async def test_a_failed_reply_is_logged_as_a_warning(
    cog: KcpcCog, caplog: pytest.LogCaptureFixture
) -> None:
    ctx = make_ctx(make_member())
    ctx.send.side_effect = discord_error(discord.NotFound, 404)

    await cog.cog_command_error(ctx, commands.CommandInvokeError(KcpcUserError('x')))

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert 'Could not reply to the error in command kcpc show' in record.getMessage()


async def test_services_come_from_the_composition_root(
    cog: KcpcCog, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stand-in module, so the test does not depend on the real one.
    services = object()
    fake = ModuleType('tle.kcpc.services')
    monkeypatch.setattr(
        fake,
        'get_services',
        lambda bot: services if bot is cog.bot else None,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, 'tle.kcpc.services', fake)

    assert cog.services is services


def make_interaction(*, responded: bool = False) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.is_done.return_value = responded
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def response_alert(interaction: MagicMock) -> str | None:
    """The alert sent as the interaction's response (not as a followup)."""
    interaction.followup.send.assert_not_awaited()
    return ephemeral_alert(interaction.response.send_message)


async def test_view_shows_a_user_error() -> None:
    interaction = make_interaction()

    await KcpcView().on_error(
        interaction, KcpcUserError('Not verified yet.'), discord.ui.Button()
    )

    assert response_alert(interaction) == 'Not verified yet.'


async def test_view_follows_up_when_the_interaction_was_answered() -> None:
    interaction = make_interaction(responded=True)

    await KcpcView().on_error(
        interaction, KcpcUserError('Not verified yet.'), discord.ui.Button()
    )

    assert ephemeral_alert(interaction.followup.send) == 'Not verified yet.'


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (NotKcpcAdmin(), str(NotKcpcAdmin())),
        (commands.CheckFailure(), "You can't do that here."),
    ],
)
async def test_view_shows_discord_errors_too(error: Exception, expected: str) -> None:
    # Unlike a command's, no other handler would reply to them.
    interaction = make_interaction()

    await KcpcView().on_error(interaction, error, discord.ui.Button())

    assert response_alert(interaction) == expected


async def test_view_logs_a_bug_and_apologises(caplog: pytest.LogCaptureFixture) -> None:
    interaction = make_interaction()
    bug = KeyError('member')

    await KcpcView().on_error(interaction, bug, discord.ui.Button(label='Verify'))

    assert response_alert(interaction) == (
        'Something went wrong. The error has been logged.'
    )
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith('Unexpected error in KcpcView item <Button')
    assert "label='Verify'" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[1] is bug


async def test_a_failed_interaction_reply_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = make_interaction()
    interaction.response.send_message.side_effect = discord_error(
        discord.HTTPException, 400
    )

    await reply_to_interaction_error(interaction, KcpcUserError('x'), source='test')

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith('Could not reply to a failed interaction')
