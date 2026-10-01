"""Embeds for KCPC posts and replies.

``to_embed`` renders a feature's ``OutgoingMessage``. For an automatic post the
footer ends with its batch marker, ``ref <batch>``, which the reconciler looks
for to tell whether a post reached its channel (see ``publisher``).
"""

import re

import discord

from tle.kcpc.core.messages import (
    DESCRIPTION_LIMIT,
    TITLE_LIMIT,
    OutgoingMessage,
    shorten,
)

KCPC_COLOR = 0x1E88E5
# The colours of TLE's discord_common.embed_success and embed_alert, so KCPC
# replies look like the rest of the bot's. They are copied rather than
# imported to keep this toolkit independent of tle.util.discord_common, which
# loads TLE's Codeforces API client and databases; tests check the copies
# against the originals.
SUCCESS_COLOR = 0x28A745
ALERT_COLOR = 0xFFBF00

MARKER_RE = re.compile(r'\bref ([0-9a-f]{8})\b')


def to_embed(message: OutgoingMessage, *, batch: str | None = None) -> discord.Embed:
    """Render ``message``, fitted to Discord's limits, with ``batch``'s marker.

    The marker ends the footer: ``'<footer> · ref <batch>'``, or just
    ``'ref <batch>'`` if the message has no footer. The colour defaults to
    ``KCPC_COLOR``, which also replaces a colour Discord would reject, and a
    link Discord would reject is left out (see
    ``OutgoingMessage.within_discord_limits``).
    """
    fitted = message.within_discord_limits()
    embed = discord.Embed(
        title=fitted.title,
        description=fitted.description,
        url=fitted.url,
        color=KCPC_COLOR if fitted.color is None else fitted.color,
    )
    for field in fitted.fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    footer = _footer_with_marker(fitted.footer, batch)
    if footer is not None:
        embed.set_footer(text=footer)
    return embed


def _footer_with_marker(footer: str | None, batch: str | None) -> str | None:
    if batch is None:
        return footer
    marker = f'ref {batch}'
    # A marker the reconciler could not find again would get the post resent.
    if not MARKER_RE.fullmatch(marker):
        raise ValueError(f'A batch is 8 lowercase hex digits, not {batch!r}')
    return marker if footer is None else f'{footer} · {marker}'


# The replies below cut over-long text to Discord's limits, ending it in '…'.
# They may repeat what a member or admin typed, and Discord would reject a
# reply over the limits, leaving the command unanswered.


def info_embed(
    title: str | None = None, description: str | None = None
) -> discord.Embed:
    return discord.Embed(
        title=shorten(title, TITLE_LIMIT),
        description=shorten(description, DESCRIPTION_LIMIT),
        color=KCPC_COLOR,
    )


def success_embed(text: str) -> discord.Embed:
    """Like TLE's ``discord_common.embed_success``."""
    return discord.Embed(
        description=shorten(text, DESCRIPTION_LIMIT), color=SUCCESS_COLOR
    )


def alert_embed(text: str) -> discord.Embed:
    """Like TLE's ``discord_common.embed_alert``."""
    return discord.Embed(
        description=shorten(text, DESCRIPTION_LIMIT), color=ALERT_COLOR
    )


def find_batch_marker(message: discord.Message) -> str | None:
    """The batch in the marker of the first embed footer that has one, if any."""
    for embed in message.embeds:
        markers: list[str] = MARKER_RE.findall(embed.footer.text or '')
        if markers:
            # The publisher appends the marker, so earlier matches are part of
            # the feature's own footer text.
            return markers[-1]
    return None
