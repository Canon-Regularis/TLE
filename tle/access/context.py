"""The context of every command, which keeps private answers private.

When the access check decides that a slash command answers only the member
who used it, ``TLEContext`` makes everything the command sends private: each
message, and the deferral that ``defer`` and ``typing`` send first. It never
makes an answer public. A private answer that comes after the interaction has
expired raises ``PrivateAnswerExpired`` instead, since discord.py would post it
in the channel, for everyone to see.
"""

import importlib
from typing import Any

import discord
from discord.context_managers import Typing
from discord.ext import commands
from discord.ext.commands.context import DeferTyping

from tle.access.service import cached_decision
from tle.util.discord_common import PrivateAnswerExpired


class TLEContext(commands.Context[Any]):
    """The bot's command context.

    A prefix command's messages reply to the message that ran it, without
    pinging its author. A slash command's are private if the access check
    said so, or if no check decided on it although the bot has an access
    service.
    """

    async def send(self, *args: Any, **kwargs: Any) -> discord.Message:
        interaction = self.interaction
        if interaction is None:
            if 'reference' not in kwargs:
                kwargs['reference'] = self.message
                kwargs.setdefault('mention_author', False)
            return await super().send(*args, **kwargs)
        if kwargs.get('ephemeral', False) or self._forced_private():
            kwargs['ephemeral'] = True
            # Only a real True counts, as in the error handler.
            if interaction.is_expired() is True:
                raise PrivateAnswerExpired()
        return await super().send(*args, **kwargs)

    async def defer(self, *, ephemeral: bool = False) -> None:
        await super().defer(ephemeral=ephemeral or self._forced_private())

    # discord.py's own signature, which mypy finds incompatible with the
    # Messageable.typing that it overrides.
    def typing(  # type: ignore[override]
        self, *, ephemeral: bool = False
    ) -> Typing | DeferTyping[Any]:
        # A slash command's typing defers it, and the deferral decides whether
        # the answer that follows is private.
        return super().typing(ephemeral=ephemeral or self._forced_private())

    async def send_help(self, *args: Any) -> Any:
        """Show the help of ``args[0]``, a command, a group or a name, or the
        overview without it; always privately on slash.
        """
        # Imported when first needed: the help module builds on the access
        # service, and may import this one.
        help_module = importlib.import_module('tle.access.help')
        return await help_module.send_help(self, args[0] if args else None)

    def _forced_private(self) -> bool:
        """Whether this slash command must answer only the member who used it."""
        if self.interaction is None:
            return False
        decision = cached_decision(self)
        if decision is not None:
            return decision.private
        # No check decided on this command, so keep its answers private.
        return getattr(self.bot, 'access', None) is not None
