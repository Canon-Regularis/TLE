"""Tests for tle.util.paginator — chunkify, errors, PaginatorView, paginate.

Presses go through discord.py's own handling of a view's items
(``View._scheduled_task``, which runs the view's interaction check before a
button's callback); Discord itself is mocked.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle.util import discord_common
from tle.util.paginator import (
    NOT_YOUR_PAGES,
    NoPagesError,
    PaginatorError,
    PaginatorView,
    chunkify,
    paginate,
)

OWNER = 1_300_000_000_000_000_001
STRANGER = 1_300_000_000_000_000_002
BUTTONS = ('first_button', 'prev_button', 'next_button', 'last_button')

# --- Helpers ---


def make_pages(count: int) -> list[tuple[str, discord.Embed]]:
    """``count`` pages; page n has the content and the embed title 'Page n'."""
    return [
        (f'Page {number}', discord.Embed(title=f'Page {number}'))
        for number in range(1, count + 1)
    ]


def make_interaction(user_id: int) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def _reraise(interaction: Any, error: Exception, item: Any) -> None:
    raise error


async def dispatch(
    view: PaginatorView, button: discord.ui.Button[Any], interaction: MagicMock
) -> None:
    """Press ``button`` with ``interaction``, as discord.py dispatches a press.

    An error fails the test, instead of only being logged by the view.
    """
    view.on_error = AsyncMock(side_effect=_reraise)
    await view._scheduled_task(button, interaction)


async def press(
    view: PaginatorView, button: discord.ui.Button[Any], user_id: int
) -> MagicMock:
    """Press ``button`` as ``user_id``; the interaction records the replies."""
    interaction = make_interaction(user_id)
    await dispatch(view, button, interaction)
    return interaction


def shown(interaction: MagicMock, view: PaginatorView) -> str:
    """The page a press showed: the message is edited to it, with the view."""
    interaction.response.edit_message.assert_awaited_once_with(
        content=ANY, embed=ANY, view=view
    )
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs['embed'].title == kwargs['content']  # both from the same page
    return kwargs['content']


def disabled(view: PaginatorView) -> tuple[bool, ...]:
    """Whether the first, previous, next and last buttons are disabled."""
    return tuple(getattr(view, name).disabled for name in BUTTONS)


def refusal(interaction: MagicMock) -> discord.Embed:
    """The one private reply that refused a press."""
    interaction.response.edit_message.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        embed=ANY, ephemeral=True
    )
    return interaction.response.send_message.await_args.kwargs['embed']


@pytest.fixture
async def bot() -> AsyncIterator[commands.Bot]:
    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    yield bot
    await bot.close()


def make_context(
    bot: commands.Bot, author_id: int, *, slash: bool
) -> commands.Context[commands.Bot]:
    """A real context for a command run by ``author_id``.

    With ``slash``, it belongs to an interaction, as for a slash command.
    Replies are recorded by an ``AsyncMock`` in place of ``send``.
    """
    author = MagicMock(spec=discord.Member, id=author_id)
    message = MagicMock(spec=discord.Message, author=author)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    context.send = AsyncMock(return_value=MagicMock(spec=discord.Message))
    return context


# --- chunkify ---


class TestChunkify:
    def test_exact_division(self):
        assert chunkify([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_remainder(self):
        assert chunkify([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_single_chunk(self):
        assert chunkify([1, 2, 3], 5) == [[1, 2, 3]]

    def test_empty(self):
        assert chunkify([], 3) == []

    def test_chunk_size_one(self):
        assert chunkify([1, 2, 3], 1) == [[1], [2], [3]]

    def test_string_sequence(self):
        assert chunkify('abcde', 2) == ['ab', 'cd', 'e']


# --- Errors ---


class TestErrors:
    def test_no_pages_is_paginator_error(self):
        assert issubclass(NoPagesError, PaginatorError)


# --- PaginatorView ---


class TestPaginatorView:
    async def test_initialization(self):
        pages = [('c1', 'e1'), ('c2', 'e2')]
        view = PaginatorView(pages, timeout=60)
        assert view.cur_page == 0
        assert view.message is None
        assert len(view.children) == 4

    async def test_buttons_disabled_on_first_page(self):
        pages = [('c1', 'e1'), ('c2', 'e2'), ('c3', 'e3')]
        view = PaginatorView(pages, timeout=60)
        # On first page: first/prev disabled, next/last enabled
        assert view.first_button.disabled is True
        assert view.prev_button.disabled is True
        assert view.next_button.disabled is False
        assert view.last_button.disabled is False

    async def test_buttons_disabled_on_last_page(self):
        pages = [('c1', 'e1'), ('c2', 'e2'), ('c3', 'e3')]
        view = PaginatorView(pages, timeout=60)
        view.cur_page = 2
        view._update_buttons()
        # On last page: first/prev enabled, next/last disabled
        assert view.first_button.disabled is False
        assert view.prev_button.disabled is False
        assert view.next_button.disabled is True
        assert view.last_button.disabled is True

    async def test_buttons_middle_page(self):
        pages = [('c1', 'e1'), ('c2', 'e2'), ('c3', 'e3')]
        view = PaginatorView(pages, timeout=60)
        view.cur_page = 1
        view._update_buttons()
        # On middle page: all enabled
        assert view.first_button.disabled is False
        assert view.prev_button.disabled is False
        assert view.next_button.disabled is False
        assert view.last_button.disabled is False

    async def test_single_page_all_disabled(self):
        pages = [('c1', 'e1')]
        view = PaginatorView(pages, timeout=60)
        assert view.first_button.disabled is True
        assert view.prev_button.disabled is True
        assert view.next_button.disabled is True
        assert view.last_button.disabled is True

    async def test_on_timeout_disables_buttons(self):
        pages = [('c1', 'e1'), ('c2', 'e2')]
        view = PaginatorView(pages, timeout=60)
        view.message = AsyncMock()

        await view.on_timeout()

        for item in view.children:
            assert item.disabled is True
        view.message.edit.assert_awaited_once_with(view=view)

    async def test_on_timeout_handles_not_found(self):
        pages = [('c1', 'e1'), ('c2', 'e2')]
        view = PaginatorView(pages, timeout=60)
        view.message = AsyncMock()
        view.message.edit.side_effect = discord.NotFound(
            MagicMock(status=404), 'Not found'
        )

        # Should not raise
        await view.on_timeout()

    async def test_on_timeout_no_message(self):
        pages = [('c1', 'e1'), ('c2', 'e2')]
        view = PaginatorView(pages, timeout=60)
        # message is None by default, should not raise
        await view.on_timeout()

    async def test_on_timeout_logs_other_http_errors_quietly(self, caplog):
        # A private reply can only be edited in the 15 minutes its interaction
        # lasts; after that Discord refuses the token.
        view = PaginatorView(make_pages(2), timeout=60)
        view.message = AsyncMock()
        view.message.edit.side_effect = discord.HTTPException(
            MagicMock(status=401, reason='Unauthorized'), 'Invalid Webhook Token'
        )

        with caplog.at_level(logging.DEBUG, logger='tle.util.paginator'):
            await view.on_timeout()

        assert disabled(view) == (True, True, True, True)
        view.message.edit.assert_awaited_once_with(view=view)
        (record,) = [r for r in caplog.records if r.name == 'tle.util.paginator']
        assert record.levelno == logging.DEBUG
        assert record.getMessage().startswith(
            'Could not disable the buttons of a paged reply: 401 Unauthorized'
        )

    async def test_on_timeout_lets_other_errors_through(self):
        view = PaginatorView(make_pages(2), timeout=60)
        view.message = AsyncMock()
        view.message.edit.side_effect = RuntimeError('a bug')

        with pytest.raises(RuntimeError, match='a bug'):
            await view.on_timeout()


class TestPageTurning:
    async def test_the_buttons_turn_the_pages(self):
        view = PaginatorView(make_pages(3), timeout=60)

        assert shown(await press(view, view.next_button, OWNER), view) == 'Page 2'
        assert view.cur_page == 1
        assert disabled(view) == (False, False, False, False)
        assert shown(await press(view, view.last_button, OWNER), view) == 'Page 3'
        assert disabled(view) == (False, False, True, True)
        assert shown(await press(view, view.prev_button, OWNER), view) == 'Page 2'
        assert shown(await press(view, view.next_button, OWNER), view) == 'Page 3'
        assert shown(await press(view, view.first_button, OWNER), view) == 'Page 1'
        assert view.cur_page == 0
        assert disabled(view) == (True, True, False, False)

    async def test_presses_past_either_end_show_the_end_page_again(self):
        # Another client may still show a button enabled after it was disabled.
        view = PaginatorView(make_pages(2), timeout=60)

        assert shown(await press(view, view.prev_button, OWNER), view) == 'Page 1'
        assert shown(await press(view, view.first_button, OWNER), view) == 'Page 1'
        view.cur_page = 1
        assert shown(await press(view, view.next_button, OWNER), view) == 'Page 2'
        assert shown(await press(view, view.last_button, OWNER), view) == 'Page 2'
        assert disabled(view) == (False, False, True, True)


class TestPaginatorViewOwner:
    async def test_anyone_by_default(self):
        assert PaginatorView(make_pages(2), timeout=60).owner_id is None

    async def test_the_owner_can_turn_the_pages(self):
        view = PaginatorView(make_pages(3), timeout=60, owner_id=OWNER)

        interaction = await press(view, view.next_button, OWNER)

        assert shown(interaction, view) == 'Page 2'
        interaction.response.send_message.assert_not_awaited()

    @pytest.mark.parametrize('button', BUTTONS)
    async def test_nobody_else_can_turn_the_pages(self, button):
        view = PaginatorView(make_pages(3), timeout=60, owner_id=OWNER)
        view.cur_page = 1  # where every button would turn the page
        view._update_buttons()

        interaction = await press(view, getattr(view, button), STRANGER)

        embed = refusal(interaction)
        assert embed.description == NOT_YOUR_PAGES
        assert NOT_YOUR_PAGES == 'Only the person who asked can turn these pages.'
        assert embed.colour == discord_common.embed_alert('').colour
        assert view.cur_page == 1
        assert disabled(view) == (False, False, False, False)

    async def test_the_owner_still_can_after_someone_else_tried(self):
        view = PaginatorView(make_pages(2), timeout=60, owner_id=OWNER)
        refusal(await press(view, view.next_button, STRANGER))

        assert shown(await press(view, view.next_button, OWNER), view) == 'Page 2'

    async def test_without_an_owner_anyone_can_turn_the_pages(self):
        view = PaginatorView(make_pages(2), timeout=60, owner_id=None)

        interaction = await press(view, view.next_button, STRANGER)

        assert shown(interaction, view) == 'Page 2'
        interaction.response.send_message.assert_not_awaited()

    async def test_interaction_check(self):
        owned = PaginatorView(make_pages(2), timeout=60, owner_id=OWNER)
        anyones = PaginatorView(make_pages(2), timeout=60)
        owner, stranger = make_interaction(OWNER), make_interaction(STRANGER)

        assert await owned.interaction_check(owner) is True
        owner.response.send_message.assert_not_awaited()
        assert await owned.interaction_check(stranger) is False
        assert await anyones.interaction_check(make_interaction(STRANGER)) is True

    async def test_a_refusal_that_cannot_be_sent_still_refuses(self, caplog):
        # For instance when the press reached the bot too late to be answered.
        view = PaginatorView(make_pages(2), timeout=60, owner_id=OWNER)
        interaction = make_interaction(STRANGER)
        interaction.response.send_message.side_effect = discord.NotFound(
            MagicMock(status=404, reason='Not Found'), 'Unknown interaction'
        )

        with caplog.at_level(logging.DEBUG, logger='tle.util.paginator'):
            await dispatch(view, view.next_button, interaction)

        interaction.response.edit_message.assert_not_awaited()
        assert view.cur_page == 0
        (record,) = [r for r in caplog.records if r.name == 'tle.util.paginator']
        assert record.levelno == logging.DEBUG
        assert record.getMessage().startswith(
            'Could not refuse a press on a paged reply: 404 Not Found'
        )


# --- paginate function ---


class TestPaginateFunction:
    async def test_empty_pages_raises_no_pages_error(self):
        with pytest.raises(NoPagesError):
            await paginate(MagicMock(), [], wait_time=60)

    async def test_sets_page_footers(self):
        channel = AsyncMock()
        embed1 = MagicMock()
        embed2 = MagicMock()
        pages = [('c1', embed1), ('c2', embed2)]

        await paginate(channel, pages, wait_time=60, set_pagenum_footers=True)

        embed1.set_footer.assert_called_once_with(text='Page 1 / 2')
        embed2.set_footer.assert_called_once_with(text='Page 2 / 2')

    async def test_single_page_no_footers(self):
        channel = AsyncMock()
        embed = MagicMock()
        pages = [('c1', embed)]

        await paginate(channel, pages, wait_time=60, set_pagenum_footers=True)

        # Single page, set_pagenum_footers condition (len > 1) is false
        embed.set_footer.assert_not_called()

    async def test_single_page_sends_without_view(self):
        channel = AsyncMock()
        embed = MagicMock()
        pages = [('content', embed)]

        await paginate(channel, pages, wait_time=60)

        channel.send.assert_awaited_once_with('content', embed=embed, delete_after=None)

    async def test_single_page_with_ctx(self):
        channel = AsyncMock()
        ctx = AsyncMock()
        embed = MagicMock()
        pages = [('content', embed)]

        await paginate(channel, pages, wait_time=60, ctx=ctx)

        ctx.send.assert_awaited_once_with('content', embed=embed, delete_after=None)
        channel.send.assert_not_awaited()

    async def test_multi_page_sends_with_view(self):
        channel = AsyncMock()
        embed1 = MagicMock()
        embed2 = MagicMock()
        pages = [('c1', embed1), ('c2', embed2)]

        await paginate(channel, pages, wait_time=60)

        # Should send with view kwarg
        channel.send.assert_awaited_once()
        call_kwargs = channel.send.call_args.kwargs
        assert 'view' in call_kwargs
        assert isinstance(call_kwargs['view'], PaginatorView)

    async def test_multi_page_with_ctx(self):
        ctx = AsyncMock()
        channel = AsyncMock()
        embed1 = MagicMock()
        embed2 = MagicMock()
        pages = [('c1', embed1), ('c2', embed2)]

        await paginate(channel, pages, wait_time=60, ctx=ctx)

        ctx.send.assert_awaited_once()
        call_kwargs = ctx.send.call_args.kwargs
        assert 'view' in call_kwargs
        assert isinstance(call_kwargs['view'], PaginatorView)
        channel.send.assert_not_awaited()

    async def test_multi_page_with_ctx_is_public_by_default(self, mock_ctx):
        pages = make_pages(2)

        await paginate(MagicMock(), pages, wait_time=60, ctx=mock_ctx)

        view = mock_ctx.send.await_args.kwargs['view']
        # Exactly as before: no ephemeral argument at all.
        mock_ctx.send.assert_awaited_once_with(
            'Page 1', embed=pages[0][1], view=view, delete_after=None
        )

    async def test_delete_after_is_passed_on(self):
        channel = AsyncMock()
        pages = make_pages(2)

        await paginate(channel, pages, wait_time=60, delete_after=30)

        view = channel.send.await_args.kwargs['view']
        channel.send.assert_awaited_once_with(
            'Page 1', embed=pages[0][1], view=view, delete_after=30
        )


class TestPaginateOwner:
    async def test_with_ctx_only_its_author_can_turn_the_pages(self, mock_ctx):
        await paginate(MagicMock(), make_pages(2), wait_time=90, ctx=mock_ctx)

        view = mock_ctx.send.await_args.kwargs['view']
        assert view.owner_id == mock_ctx.author.id == 12345
        assert view.timeout == 90
        # So that the buttons can be disabled on it when they time out.
        assert view.message is mock_ctx.send.return_value

    async def test_without_ctx_anyone_can_turn_the_pages(self):
        channel = AsyncMock()

        await paginate(channel, make_pages(2), wait_time=60)

        view = channel.send.await_args.kwargs['view']
        assert view.owner_id is None
        assert view.message is channel.send.return_value
        interaction = await press(view, view.next_button, STRANGER)
        assert shown(interaction, view) == 'Page 2'


class TestPaginatePrivate:
    @pytest.mark.parametrize('delete_after', [None, 30])
    async def test_a_private_single_page(self, mock_ctx, delete_after):
        (page,) = make_pages(1)

        await paginate(
            MagicMock(),
            [page],
            wait_time=60,
            delete_after=delete_after,
            ctx=mock_ctx,
            ephemeral=True,
        )

        mock_ctx.send.assert_awaited_once_with(
            'Page 1', embed=page[1], delete_after=delete_after, ephemeral=True
        )

    async def test_private_pages(self, mock_ctx):
        channel = AsyncMock()
        pages = make_pages(3)

        await paginate(
            channel,
            pages,
            wait_time=60,
            set_pagenum_footers=True,
            ctx=mock_ctx,
            ephemeral=True,
        )

        mock_ctx.send.assert_awaited_once_with(
            'Page 1', embed=pages[0][1], view=ANY, delete_after=None, ephemeral=True
        )
        view = mock_ctx.send.await_args.kwargs['view']
        assert isinstance(view, PaginatorView)
        assert view.owner_id == mock_ctx.author.id
        assert view.message is mock_ctx.send.return_value
        assert [embed.footer.text for _, embed in pages] == [
            'Page 1 / 3',
            'Page 2 / 3',
            'Page 3 / 3',
        ]
        channel.send.assert_not_awaited()

    @pytest.mark.parametrize('count', [0, 1, 2])
    async def test_a_private_reply_needs_ctx(self, count):
        # A post in a channel is public, so it is refused before anything is sent.
        channel = AsyncMock()
        pages = make_pages(count)

        with pytest.raises(ValueError, match='private reply needs ctx'):
            await paginate(
                channel, pages, wait_time=60, set_pagenum_footers=True, ephemeral=True
            )

        channel.send.assert_not_awaited()
        assert [embed.footer.text for _, embed in pages] == [None] * count


class TestPaginateOnBothPaths:
    """With real contexts: a prefix command's, and a slash command's."""

    @pytest.mark.parametrize('ephemeral', [False, True])
    @pytest.mark.parametrize('slash', [False, True])
    async def test_only_the_author_can_turn_the_pages(self, bot, slash, ephemeral):
        ctx = make_context(bot, OWNER, slash=slash)
        pages = make_pages(3)

        await paginate(
            ctx.channel,
            pages,
            wait_time=60,
            set_pagenum_footers=True,
            ctx=ctx,
            ephemeral=ephemeral,
        )

        private = {'ephemeral': True} if ephemeral else {}
        ctx.send.assert_awaited_once_with(
            'Page 1', embed=pages[0][1], view=ANY, delete_after=None, **private
        )
        view = ctx.send.await_args.kwargs['view']
        assert view.owner_id == OWNER
        assert refusal(await press(view, view.next_button, STRANGER)).description == (
            NOT_YOUR_PAGES
        )
        assert shown(await press(view, view.next_button, OWNER), view) == 'Page 2'
        assert pages[1][1].footer.text == 'Page 2 / 3'

    @pytest.mark.parametrize('deferred', [False, True])
    async def test_private_pages_answer_the_interaction_privately(self, bot, deferred):
        # Through discord.py's own Context.send: the answer to the interaction,
        # or its followup once deferred, is private and carries the buttons.
        author = MagicMock(spec=discord.Member, id=OWNER)
        message = MagicMock(spec=discord.Message, author=author)
        interaction = MagicMock(spec=discord.Interaction, client=bot)
        interaction.is_expired.return_value = False
        interaction.response.is_done.return_value = deferred
        sent = MagicMock(spec=discord.InteractionMessage)
        sent.edit = AsyncMock()
        interaction.response.send_message = AsyncMock(
            return_value=MagicMock(resource=sent)
        )
        interaction.followup.send = AsyncMock(return_value=sent)
        ctx: commands.Context[commands.Bot] = commands.Context(
            message=message,
            bot=bot,
            view=StringView(''),
            prefix='/',
            interaction=interaction,
        )
        pages = make_pages(2)

        await paginate(ctx.channel, pages, wait_time=60, ctx=ctx, ephemeral=True)

        answer, unused = (
            (interaction.followup.send, interaction.response.send_message)
            if deferred
            else (interaction.response.send_message, interaction.followup.send)
        )
        answer.assert_awaited_once()
        unused.assert_not_awaited()
        kwargs = answer.await_args.kwargs
        view = kwargs['view']
        assert isinstance(view, PaginatorView)
        assert (kwargs['content'], kwargs['embed'], kwargs['ephemeral']) == (
            'Page 1',
            pages[0][1],
            True,
        )
        assert view.owner_id == OWNER
        assert view.message is sent
        await view.on_timeout()
        sent.edit.assert_awaited_once_with(view=view)
