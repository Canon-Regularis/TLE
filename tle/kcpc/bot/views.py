"""The base view of KCPC components, which replies to errors like KCPC commands."""

import logging
from typing import Any

import discord

from tle.kcpc.bot.cog import ErrorKind, classify_error, unwrap_error
from tle.kcpc.bot.embeds import alert_embed

logger = logging.getLogger(__name__)

UNEXPECTED_ERROR_MESSAGE = 'Something went wrong. The error has been logged.'
# For a discord.py error without a message, e.g. a bare CheckFailure.
_FALLBACK_MESSAGE = "You can't do that here."


async def reply_to_interaction_error(
    interaction: discord.Interaction, error: Exception, *, source: str
) -> None:
    """Reply ephemerally to an interaction whose handler raised ``error``.

    Errors are classified like command errors in ``KcpcCog``, except that
    discord.py's own errors show their message too, since no other handler
    would reply to them. ``source`` names the failing component in the log.
    ``KcpcView`` calls this from ``on_error``. ``DynamicItem`` callbacks should
    call it themselves, because their errors never reach ``View.on_error``.
    """
    err = unwrap_error(error)
    if classify_error(err) is ErrorKind.UNEXPECTED:
        logger.exception('Unexpected error in %s', source, exc_info=err)
        text = UNEXPECTED_ERROR_MESSAGE
    else:
        text = str(err) or _FALLBACK_MESSAGE
    await _send_alert(interaction, text)


async def _send_alert(interaction: discord.Interaction, text: str) -> None:
    embed = alert_embed(text)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException as exc:
        logger.warning('Could not reply to a failed interaction: %s', exc)


class KcpcView(discord.ui.View):
    """The base class of KCPC views: errors in their callbacks get a reply."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        source = f'{type(self).__name__} item {item!r}'
        await reply_to_interaction_error(interaction, error, source=source)
