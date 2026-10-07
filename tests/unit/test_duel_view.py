"""Tests for DuelChallengeView in tle.cogs.duel."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from tle.cogs.duel import DuelChallengeView
from tle.util.db.user_db_conn import Duel

# Each button, the command whose access rule it follows, the duelist it is
# for, and what anyone else who presses it is told.
BUTTONS = [
    ('accept_button', 'duel accept', 2002, 'Only the challenged user can accept.'),
    ('decline_button', 'duel decline', 2002, 'Only the challenged user can decline.'),
    ('withdraw_button', 'duel withdraw', 1001, 'Only the challenger can withdraw.'),
]


def _make_view(timeout=300):
    bot = MagicMock()
    bot.user_db = AsyncMock()
    view = DuelChallengeView(
        bot=bot,
        duelid=42,
        challenger_id=1001,
        challengee_id=2002,
        problem_name='A. Test Problem',
        timeout=timeout,
    )
    return view


def _make_interaction(user_id, guild=None, channel=None):
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = AsyncMock()
    interaction.guild = guild or MagicMock()
    interaction.channel = channel or AsyncMock()
    # A bot without an access service, which lets every press through.
    interaction.client = MagicMock(spec=commands.Bot)
    return interaction


def _with_access(interaction, *, allowed):
    """Give the bot of ``interaction`` an access service that allows the press,
    or refuses it, as if it had told the member why.
    """
    access = MagicMock()
    access.component_allowed = AsyncMock(return_value=allowed)
    interaction.client = SimpleNamespace(access=access)
    return access


class TestDuelChallengeViewInit:
    async def test_initialization_stores_state(self):
        view = _make_view()
        assert view.duelid == 42
        assert view.challenger_id == 1001
        assert view.challengee_id == 2002
        assert view.problem_name == 'A. Test Problem'
        assert view.message is None

    async def test_has_three_buttons(self):
        view = _make_view()
        assert len(view.children) == 3

    async def test_all_buttons_enabled_on_init(self):
        view = _make_view()
        for item in view.children:
            assert item.disabled is False

    async def test_button_labels(self):
        view = _make_view()
        labels = {item.label for item in view.children}
        assert labels == {'Accept', 'Decline', 'Withdraw'}

    async def test_button_styles(self):
        view = _make_view()
        styles = {item.label: item.style for item in view.children}
        assert styles['Accept'] == discord.ButtonStyle.success
        assert styles['Decline'] == discord.ButtonStyle.danger
        assert styles['Withdraw'] == discord.ButtonStyle.secondary


class TestAcceptButton:
    async def test_rejects_wrong_user(self):
        view = _make_view()
        interaction = _make_interaction(user_id=9999)

        await view.accept_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenged user can accept.',
            ephemeral=True,
        )

    async def test_rejects_challenger(self):
        view = _make_view()
        interaction = _make_interaction(user_id=1001)

        await view.accept_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenged user can accept.',
            ephemeral=True,
        )

    async def test_accepts_and_disables_buttons(self):
        view = _make_view()
        view.bot.user_db.start_duel.return_value = 1
        problem = MagicMock()
        problem.index = 'A'
        problem.name = 'Test Problem'
        problem.url = 'https://example.com'
        problem.rating = 1500
        problem.contestId = 100
        contest = MagicMock()
        contest.name = 'Test Contest'
        view.bot.cf_cache.problem_cache.problem_by_name = {
            'A. Test Problem': problem,
        }
        view.bot.cf_cache.contest_cache.get_contest.return_value = contest

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=2002,
            guild=guild,
            channel=channel,
        )

        with patch('tle.cogs.duel.asyncio.sleep', new_callable=AsyncMock):
            await view.accept_button.callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        for item in view.children:
            assert item.disabled is True

    async def test_handles_start_duel_failure(self):
        view = _make_view()
        view.bot.user_db.start_duel.return_value = 0

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=2002,
            guild=guild,
            channel=channel,
        )

        with patch('tle.cogs.duel.asyncio.sleep', new_callable=AsyncMock):
            await view.accept_button.callback(interaction)

        # Should send error about unable to start
        calls = channel.send.call_args_list
        assert any('Unable to start' in str(call) for call in calls)


class TestDeclineButton:
    async def test_rejects_wrong_user(self):
        view = _make_view()
        interaction = _make_interaction(user_id=9999)

        await view.decline_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenged user can decline.',
            ephemeral=True,
        )

    async def test_rejects_challenger(self):
        view = _make_view()
        interaction = _make_interaction(user_id=1001)

        await view.decline_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenged user can decline.',
            ephemeral=True,
        )

    async def test_decline_cancels_duel(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 1

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=2002,
            guild=guild,
            channel=channel,
        )

        await view.decline_button.callback(interaction)

        view.bot.user_db.cancel_duel.assert_awaited_once_with(42, Duel.DECLINED)
        interaction.response.edit_message.assert_awaited_once()
        for item in view.children:
            assert item.disabled is True

    async def test_decline_already_resolved(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 0

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=2002,
            guild=guild,
            channel=channel,
        )

        await view.decline_button.callback(interaction)

        channel.send.assert_awaited_with('This duel has already been resolved.')


class TestWithdrawButton:
    async def test_rejects_wrong_user(self):
        view = _make_view()
        interaction = _make_interaction(user_id=9999)

        await view.withdraw_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenger can withdraw.',
            ephemeral=True,
        )

    async def test_rejects_challengee(self):
        view = _make_view()
        interaction = _make_interaction(user_id=2002)

        await view.withdraw_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            'Only the challenger can withdraw.',
            ephemeral=True,
        )

    async def test_withdraw_cancels_duel(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 1

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=1001,
            guild=guild,
            channel=channel,
        )

        await view.withdraw_button.callback(interaction)

        view.bot.user_db.cancel_duel.assert_awaited_once_with(42, Duel.WITHDRAWN)
        interaction.response.edit_message.assert_awaited_once()
        for item in view.children:
            assert item.disabled is True

    async def test_withdraw_already_resolved(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 0

        guild = MagicMock()
        guild.get_member.return_value = MagicMock(mention='@user')
        channel = AsyncMock()
        interaction = _make_interaction(
            user_id=1001,
            guild=guild,
            channel=channel,
        )

        await view.withdraw_button.callback(interaction)

        channel.send.assert_awaited_with('This duel has already been resolved.')


class TestOnTimeout:
    async def test_disables_buttons_and_expires_duel(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 1
        message = AsyncMock()
        message.guild = MagicMock()
        message.guild.get_member.return_value = MagicMock(mention='@user')
        message.channel = AsyncMock()
        view.message = message

        await view.on_timeout()

        for item in view.children:
            assert item.disabled is True
        message.edit.assert_awaited_once_with(view=view)
        view.bot.user_db.cancel_duel.assert_awaited_once_with(42, Duel.EXPIRED)
        message.channel.send.assert_awaited_once()

    async def test_timeout_no_message(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 0

        # Should not raise when message is None
        await view.on_timeout()

        for item in view.children:
            assert item.disabled is True

    async def test_timeout_handles_not_found(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 0
        message = AsyncMock()
        message.edit.side_effect = discord.NotFound(
            MagicMock(status=404),
            'Not found',
        )
        view.message = message

        # Should not raise
        await view.on_timeout()

    async def test_timeout_already_resolved_no_alert(self):
        view = _make_view()
        view.bot.user_db.cancel_duel.return_value = 0
        message = AsyncMock()
        message.guild = MagicMock()
        message.channel = AsyncMock()
        view.message = message

        await view.on_timeout()

        # cancel_duel returned 0, so no expiry alert
        message.channel.send.assert_not_awaited()


class TestButtonsAskTheAccessRules:
    """Each button first asks the bot's access service whether the member may
    use its command, and does nothing more if not.
    """

    @pytest.mark.parametrize(('button', 'command', 'user_id', 'other'), BUTTONS)
    async def test_a_refused_press_changes_nothing(
        self, button, command, user_id, other
    ):
        view = _make_view()
        # The duelist the button is for, whom nothing else would stop.
        interaction = _make_interaction(user_id=user_id)
        access = _with_access(interaction, allowed=False)

        with patch('tle.cogs.duel.asyncio.sleep', new_callable=AsyncMock):
            await getattr(view, button).callback(interaction)

        access.component_allowed.assert_awaited_once_with(interaction, command)
        # The service has answered the press; the button adds nothing.
        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_not_awaited()
        interaction.channel.send.assert_not_awaited()
        view.bot.user_db.start_duel.assert_not_awaited()
        view.bot.user_db.cancel_duel.assert_not_awaited()
        assert not any(item.disabled for item in view.children)

    @pytest.mark.parametrize(('button', 'command', 'user_id', 'other'), BUTTONS)
    async def test_an_allowed_press_goes_on_as_before(
        self, button, command, user_id, other
    ):
        view = _make_view()
        interaction = _make_interaction(user_id=9999)  # neither duelist
        access = _with_access(interaction, allowed=True)

        await getattr(view, button).callback(interaction)

        access.component_allowed.assert_awaited_once_with(interaction, command)
        interaction.response.send_message.assert_awaited_once_with(
            other, ephemeral=True
        )

    @pytest.mark.parametrize(('button', 'command', 'user_id', 'other'), BUTTONS)
    async def test_without_an_access_service_every_press_goes_on(
        self, button, command, user_id, other
    ):
        view = _make_view()
        interaction = _make_interaction(user_id=9999)

        await getattr(view, button).callback(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            other, ephemeral=True
        )
