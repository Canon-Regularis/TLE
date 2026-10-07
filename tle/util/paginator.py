import logging
from collections.abc import Sequence
from typing import Any

import discord
from discord.ext import commands

from tle.util import discord_common

logger = logging.getLogger(__name__)

Page = tuple[str | None, discord.Embed]

# The private reply to anyone who presses the buttons of someone else's pages.
NOT_YOUR_PAGES = 'Only the person who asked can turn these pages.'


def chunkify(sequence: Sequence[Any], chunk_size: int) -> list[Sequence[Any]]:
    """Utility method to split a sequence into fixed size chunks."""
    return [sequence[i : i + chunk_size] for i in range(0, len(sequence), chunk_size)]


class PaginatorError(Exception):
    pass


class NoPagesError(PaginatorError):
    pass


class PaginatorView(discord.ui.View):
    """Buttons that show ``pages`` one at a time.

    With ``owner_id``, only that user can turn the pages: anyone else who presses
    a button gets a private refusal. With None, anyone can, as on the standings
    that the rated-VC watcher posts.
    """

    def __init__(
        self, pages: Sequence[Page], *, timeout: float, owner_id: int | None = None
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.owner_id = owner_id
        self.cur_page = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        try:
            await interaction.response.send_message(
                embed=discord_common.embed_alert(NOT_YOUR_PAGES), ephemeral=True
            )
        except discord.HTTPException as exc:
            # The pages stay as they are either way.
            logger.debug('Could not refuse a press on a paged reply: %s', exc)
        return False

    def _update_buttons(self) -> None:
        on_first = self.cur_page == 0
        on_last = self.cur_page == len(self.pages) - 1
        self.first_button.disabled = on_first
        self.prev_button.disabled = on_first
        self.next_button.disabled = on_last
        self.last_button.disabled = on_last

    async def _show_page(self, interaction: discord.Interaction) -> None:
        content, embed = self.pages[self.cur_page]
        self._update_buttons()
        await interaction.response.edit_message(content=content, embed=embed, view=self)

    @discord.ui.button(
        emoji='\N{BLACK LEFT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}',
        style=discord.ButtonStyle.secondary,
    )
    async def first_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self.cur_page = 0
        await self._show_page(interaction)

    @discord.ui.button(
        emoji='\N{BLACK LEFT-POINTING TRIANGLE}',
        style=discord.ButtonStyle.secondary,
    )
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self.cur_page = max(0, self.cur_page - 1)
        await self._show_page(interaction)

    @discord.ui.button(
        emoji='\N{BLACK RIGHT-POINTING TRIANGLE}',
        style=discord.ButtonStyle.secondary,
    )
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self.cur_page = min(len(self.pages) - 1, self.cur_page + 1)
        await self._show_page(interaction)

    @discord.ui.button(
        emoji='\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}',
        style=discord.ButtonStyle.secondary,
    )
    async def last_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        self.cur_page = len(self.pages) - 1
        await self._show_page(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException as exc:
                # The message may be gone, or be private and older than the 15
                # minutes in which an interaction's replies can be edited.
                logger.debug('Could not disable the buttons of a paged reply: %s', exc)


async def paginate(
    channel: discord.abc.Messageable,
    pages: Sequence[Page],
    *,
    wait_time: float,
    set_pagenum_footers: bool = False,
    delete_after: float | None = None,
    ctx: commands.Context | None = None,
    ephemeral: bool = False,
) -> None:
    """Send the first of ``pages``, with buttons to turn them if there are more.

    With ``ctx``, it replies to the command, and only its author can turn the
    pages; without, it posts in ``channel``, and anyone can. ``ephemeral`` makes
    the reply to a slash command private (prefix replies ignore it), so it needs
    ``ctx``.
    """
    if ephemeral and ctx is None:
        raise ValueError('A private reply needs ctx: a post in a channel is public')
    if not pages:
        raise NoPagesError()
    if len(pages) > 1 and set_pagenum_footers:
        for i, (_content, embed) in enumerate(pages):
            embed.set_footer(text=f'Page {i + 1} / {len(pages)}')

    # Passed only when set, so that public replies are sent exactly as before.
    private: dict[str, Any] = {'ephemeral': True} if ephemeral else {}
    content, embed = pages[0]
    if len(pages) == 1:
        if ctx is not None:
            await ctx.send(content, embed=embed, delete_after=delete_after, **private)
        else:
            await channel.send(content, embed=embed, delete_after=delete_after)
    else:
        owner_id = None if ctx is None else ctx.author.id
        view = PaginatorView(pages, timeout=wait_time, owner_id=owner_id)
        if ctx is not None:
            view.message = await ctx.send(
                content, embed=embed, view=view, delete_after=delete_after, **private
            )
        else:
            view.message = await channel.send(
                content, embed=embed, view=view, delete_after=delete_after
            )
