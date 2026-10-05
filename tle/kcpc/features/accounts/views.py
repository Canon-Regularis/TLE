"""The Verify button of /link, which keeps working after the bot restarts.

The button's custom ID says what it verifies, 'kcpc:link:<platform>:<user>',
and the challenge it checks is in the database. So the cog registers
``VerifyLinkButton`` with ``Bot.add_dynamic_items``, and discord.py makes a new
button from the custom ID whenever one is pressed, on a message sent before a
restart too. Pressing it does what ``/link verify <platform>`` does, through
the accounts cog.
"""

import re
from typing import Any, Protocol, runtime_checkable

import discord
from discord.ext import commands

from tle.kcpc.bot.views import reply_to_interaction_error
from tle.kcpc.core.errors import KcpcDisabledError, KcpcUserError

# The name of the accounts cog, which the button runs the verification through.
ACCOUNTS_COG = 'KcpcAccounts'
# A Discord ID has at most 20 digits.
VERIFY_TEMPLATE = r'kcpc:link:(?P<platform>codeforces|atcoder):(?P<user_id>[0-9]{1,20})'
NOT_YOUR_LINK = 'Only the member who is linking this account can verify it.'


@runtime_checkable
class LinkVerifier(Protocol):
    """What the Verify button needs of the accounts cog."""

    async def verify_link(
        self, guild: discord.Guild, member: discord.Member, platform: str
    ) -> discord.Embed:
        """Verify the account the member is linking; the reply to show them."""
        ...


class VerifyLinkButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]], template=VERIFY_TEMPLATE
):
    """Verifies the account that member ``user_id`` is linking on ``platform``."""

    def __init__(self, platform: str, user_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label='Verify',
                style=discord.ButtonStyle.success,
                custom_id=f'kcpc:link:{platform}:{user_id}',
            )
        )
        self.platform = platform
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> 'VerifyLinkButton':
        return cls(match['platform'], int(match['user_id']))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        # discord.py only logs what a dynamic item's callback raises, without
        # calling View.on_error, so the button replies to its own errors.
        try:
            # A button's deferral is ephemeral only if it's "thinking".
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.user.id != self.user_id:
                raise KcpcUserError(NOT_YOUR_LINK)
            guild, member = interaction.guild, interaction.user
            if guild is None or not isinstance(member, discord.Member):
                raise commands.NoPrivateMessage()
            verifier = _verifier(interaction.client)
            embed = await verifier.verify_link(guild, member, self.platform)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            source = f'the Verify button {self.custom_id}'
            await reply_to_interaction_error(interaction, exc, source=source)


def verify_view(platform: str, user_id: int) -> discord.ui.View:
    """A view of just the Verify button, for /link's reply."""
    # The button lasts as long as its challenge, which the view can't know.
    view = discord.ui.View(timeout=None)
    view.add_item(VerifyLinkButton(platform, user_id))
    return view


def _verifier(client: discord.Client) -> LinkVerifier:
    """The accounts cog; ``KcpcDisabledError`` if it isn't loaded."""
    cog = client.get_cog(ACCOUNTS_COG) if isinstance(client, commands.Bot) else None
    if not isinstance(cog, LinkVerifier):
        raise KcpcDisabledError()
    return cog
