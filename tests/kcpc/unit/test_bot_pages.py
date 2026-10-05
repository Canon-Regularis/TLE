"""Tests for tle.kcpc.bot.pages: replies of several pages, with buttons to turn them.

Presses go through discord.py's own handling of a view's items where it
matters (``View._scheduled_task``, which runs the view's interaction check
before an item's callback); Discord itself is mocked.
"""

import logging
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from tle.kcpc.bot.embeds import ALERT_COLOR
from tle.kcpc.bot.pages import NOT_YOUR_PAGES, PAGE_TIMEOUT, PageView, send_pages
from tle.kcpc.bot.views import KcpcView

OWNER = 1_300_000_000_000_000_001
STRANGER = 1_300_000_000_000_000_002


def make_pages(count: int) -> list[discord.Embed]:
    return [discord.Embed(title=f'Page {number}') for number in range(1, count + 1)]


def make_interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def disabled(view: PageView) -> tuple[bool, bool]:
    """Whether Previous and Next are disabled."""
    return view.previous_page.disabled, view.next_page.disabled


async def press(
    view: PageView, button: discord.ui.Button[Any], user_id: int
) -> MagicMock:
    """Press ``button`` as ``user_id``, as discord.py dispatches a press."""
    interaction = make_interaction(user_id)
    await view._scheduled_task(button, interaction)
    return interaction


def shown(interaction: MagicMock, view: PageView) -> str | None:
    """The title of the page the press showed; the view is sent again with it."""
    interaction.response.edit_message.assert_awaited_once_with(embed=ANY, view=view)
    embed = interaction.response.edit_message.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed.title


async def test_the_first_page_is_shown_with_only_next_enabled() -> None:
    view = PageView(make_pages(3), owner_id=OWNER)

    assert isinstance(view, KcpcView)  # so errors in its buttons get a reply
    assert view.page.title == 'Page 1'
    assert view.children == [view.previous_page, view.next_page]
    assert [view.previous_page.label, view.next_page.label] == ['Previous', 'Next']
    assert disabled(view) == (True, False)
    assert view.timeout == PAGE_TIMEOUT == 300


async def test_next_and_previous_turn_the_pages_and_stop_at_the_ends() -> None:
    view = PageView(make_pages(3), owner_id=OWNER)

    assert shown(await press(view, view.next_page, OWNER), view) == 'Page 2'
    assert disabled(view) == (False, False)
    assert shown(await press(view, view.next_page, OWNER), view) == 'Page 3'
    assert disabled(view) == (False, True)
    assert shown(await press(view, view.previous_page, OWNER), view) == 'Page 2'
    assert shown(await press(view, view.previous_page, OWNER), view) == 'Page 1'
    assert disabled(view) == (True, False)


async def test_a_press_past_the_end_shows_the_last_page_again() -> None:
    # Two members' clients may show a button enabled after it was disabled.
    view = PageView(make_pages(2), owner_id=OWNER)
    view.index = 1

    assert shown(await press(view, view.next_page, OWNER), view) == 'Page 2'
    assert disabled(view) == (False, True)


async def test_only_the_member_who_ran_the_command_can_turn_the_pages() -> None:
    view = PageView(make_pages(2), owner_id=OWNER)

    interaction = await press(view, view.next_page, STRANGER)

    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        embed=ANY, ephemeral=True
    )
    refusal = interaction.response.send_message.await_args.kwargs['embed']
    assert refusal.description == NOT_YOUR_PAGES
    assert refusal.colour == discord.Colour(ALERT_COLOR)
    assert view.page.title == 'Page 1'
    assert await view.interaction_check(make_interaction(OWNER)) is True


async def test_a_timeout_disables_both_buttons_on_the_message() -> None:
    view = PageView(make_pages(2), owner_id=OWNER)
    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()
    view.message = message

    await view.on_timeout()

    assert disabled(view) == (True, True)
    message.edit.assert_awaited_once_with(view=view)


async def test_a_timeout_after_the_message_is_gone_is_logged_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    view = PageView(make_pages(2), owner_id=OWNER)
    message = MagicMock(spec=discord.Message)
    response = MagicMock(status=404, reason='Not Found')
    message.edit = AsyncMock(side_effect=discord.NotFound(response, 'Unknown Message'))
    view.message = message

    with caplog.at_level(logging.DEBUG, logger='tle.kcpc.bot.pages'):
        await view.on_timeout()

    assert disabled(view) == (True, True)
    (record,) = caplog.records
    assert record.levelno == logging.DEBUG
    assert record.getMessage().startswith(
        'Could not disable the buttons of a paged reply: 404 Not Found'
    )


async def test_a_timeout_before_the_view_was_sent_only_disables_the_buttons() -> None:
    view = PageView(make_pages(2), owner_id=OWNER)

    await view.on_timeout()

    assert disabled(view) == (True, True)


@pytest.mark.parametrize('count', [0, 1])
async def test_a_page_view_needs_two_pages(count: int) -> None:
    with pytest.raises(ValueError, match='at least two pages'):
        PageView(make_pages(count), owner_id=OWNER)


def make_ctx() -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.author.id = OWNER
    ctx.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return ctx


@pytest.mark.parametrize('ephemeral', [False, True])
async def test_a_single_page_is_sent_without_buttons(ephemeral: bool) -> None:
    ctx = make_ctx()
    (page,) = make_pages(1)

    await send_pages(ctx, [page], ephemeral=ephemeral)

    ctx.send.assert_awaited_once_with(embed=page, ephemeral=ephemeral)


@pytest.mark.parametrize('ephemeral', [False, True])
async def test_several_pages_are_sent_from_the_first_with_buttons(
    ephemeral: bool,
) -> None:
    ctx = make_ctx()
    pages = make_pages(3)

    await send_pages(ctx, pages, ephemeral=ephemeral)

    ctx.send.assert_awaited_once_with(embed=pages[0], view=ANY, ephemeral=ephemeral)
    view = ctx.send.await_args.kwargs['view']
    assert isinstance(view, PageView)
    assert view.pages == tuple(pages)
    assert view.owner_id == OWNER
    # So that the buttons can be disabled on it when they time out.
    assert view.message is ctx.send.return_value


async def test_a_reply_needs_a_page() -> None:
    ctx = make_ctx()

    with pytest.raises(ValueError, match='at least one page'):
        await send_pages(ctx, [])

    ctx.send.assert_not_awaited()
