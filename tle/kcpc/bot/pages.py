"""Replies of several pages: one embed at a time, with Previous and Next buttons.

``send_pages`` sends the first page, and the buttons only if there are more.
Only the member who ran the command may turn the pages. The buttons stop
working after 5 minutes, and are then shown disabled.
"""

import logging
from collections.abc import Sequence
from typing import Any

import discord
from discord.ext import commands

from tle.kcpc.bot.embeds import alert_embed
from tle.kcpc.bot.views import KcpcView

logger = logging.getLogger(__name__)

PAGE_TIMEOUT = 5 * 60.0  # seconds
NOT_YOUR_PAGES = 'Only the member who ran the command can turn its pages.'


class PageView(KcpcView):
    """Previous and Next buttons that show ``pages`` one at a time.

    The buttons that would go past the first or the last page are disabled.
    Set ``message`` to the message the view is sent with, so that the buttons
    can be disabled there when they time out.
    """

    def __init__(
        self,
        pages: Sequence[discord.Embed],
        *,
        owner_id: int,
        timeout: float = PAGE_TIMEOUT,
    ) -> None:
        if len(pages) < 2:
            raise ValueError('A PageView needs at least two pages')
        super().__init__(timeout=timeout)
        self.pages = tuple(pages)
        self.owner_id = owner_id
        self.index = 0
        self.message: discord.Message | None = None
        self._update_buttons()

    @property
    def page(self) -> discord.Embed:
        """The page being shown."""
        return self.pages[self.index]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            embed=alert_embed(NOT_YOUR_PAGES), ephemeral=True
        )
        return False

    @discord.ui.button(label='Previous', style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button['PageView']
    ) -> None:
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label='Next', style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button['PageView']
    ) -> None:
        await self._show(interaction, self.index + 1)

    async def on_timeout(self) -> None:
        self.previous_page.disabled = True
        self.next_page.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException as exc:
            # The message may have been deleted; the buttons are dead anyway.
            logger.debug('Could not disable the buttons of a paged reply: %s', exc)

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = max(0, min(index, len(self.pages) - 1))
        self._update_buttons()
        await interaction.response.edit_message(embed=self.page, view=self)

    def _update_buttons(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.pages) - 1


async def send_pages(
    ctx: commands.Context[Any],
    pages: Sequence[discord.Embed],
    *,
    ephemeral: bool = False,
) -> None:
    """Reply with ``pages``: the first, and the buttons to turn them if there are more.

    ``ephemeral`` applies to slash commands; prefix commands ignore it.
    """
    if not pages:
        raise ValueError('A reply needs at least one page')
    if len(pages) == 1:
        await ctx.send(embed=pages[0], ephemeral=ephemeral)
        return
    view = PageView(pages, owner_id=ctx.author.id)
    view.message = await ctx.send(embed=view.page, view=view, ephemeral=ephemeral)
