import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.util import discord_common

# A colour as admins type it: six hex digits, after an optional # or 0x.
_COLOUR = re.compile(r'(?:#|0[xX])?([0-9a-fA-F]{6})')
# Discord's limit on the value of an embed's field.
_FIELD_LIMIT = 1024

# The replies to admins. Embeds never ping, so they show emojis and channels
# as they are.
BAD_COLOUR_TEXT = 'Give the colour as six hex digits, such as `#ffd700`.'
NOT_ON_STARBOARD_TEXT = "{emoji} isn't a starboard emoji."
ADD_IT_TEXT = "{emoji} isn't a starboard emoji. Add it with `/starboard add`."
NO_CHANNEL_TEXT = '{emoji} has no starboard channel.'
NO_REPOST_TEXT = "That message hasn't been reposted with {emoji}."
ADDED_TEXT = (
    'Added {emoji}: a message that gets {reactions} is reposted in {channel}, '
    'in colour `{colour}`.'
)
ADDED_WITHOUT_CHANNEL_TEXT = (
    'Added {emoji}: once you use `/starboard here` in a channel, a message that '
    'gets {reactions} is reposted there, in colour `{colour}`.'
)
DELETED_TEXT = 'Deleted {emoji} and its settings. Its reposts stay.'
THRESHOLD_TEXT = 'A message now needs {reactions} to be reposted.'
COLOUR_TEXT = 'New reposts for {emoji} are in colour `{colour}`.'
HERE_TEXT = 'Reposts for {emoji} now go to {channel}.'
CLEARED_TEXT = (
    'Stopped posting the starred messages of {emoji}. Its threshold and colour '
    'stay, ready for `/starboard here`.'
)
FORGOTTEN_TEXT = "Forgot that message's repost for {emoji}: it can be reposted again."


class StarboardCogError(commands.CommandError):
    """A starboard command or repost that can't be done; its text says why."""


def parse_colour(text: str) -> int:
    """The colour that ``text`` gives as six hex digits, such as #ffd700,
    ffd700 or 0xffd700.

    ``StarboardCogError``, whose text says how to give one, if it gives none.
    """
    match = _COLOUR.fullmatch(text.strip())
    if match is None:
        raise StarboardCogError(BAD_COLOUR_TEXT)
    return int(match.group(1), 16)


def _colour_text(colour: int) -> str:
    return f'#{colour:06x}'


def _reactions(count: int, emoji: str) -> str:
    """Such as '5 ⭐ reactions'."""
    return f'{count} {emoji} reaction{"" if count == 1 else "s"}'


def _hidden_from_some_readers(
    guild: discord.Guild, source: Any, starboard: Any
) -> bool:
    """Whether a role of ``guild`` that can see the channel ``starboard`` can't
    see the channel ``source``. The roles include @everyone, so a channel
    hidden from it is hidden from someone, unless the starboard is too.
    """
    return any(
        starboard.permissions_for(role).view_channel
        and not source.permissions_for(role).view_channel
        for role in guild.roles
    )


class Starboard(commands.Cog):
    """Reposts messages that get enough reactions with a starboard emoji.

    Each server's admins choose its starboard emojis, and for each one the
    channel its reposts go to, how many reactions a message needs and the
    colour of the reposts. Messages in the server's staff channel, or in a
    thread in it, are never reposted, since they are for staff alone. Nor are
    those that some readers of the starboard channel couldn't read where they
    were posted: in a private thread, or in a channel hidden from them.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.logger = logging.getLogger(self.__class__.__name__)

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        guild_id = payload.guild_id
        if guild_id is None:
            return
        emoji = str(payload.emoji)
        entry = await self.bot.user_db.get_starboard_entry(guild_id, emoji)
        if entry is None:
            return
        channel_id, threshold, color = entry
        try:
            await self.check_and_add_to_starboard(
                channel_id, threshold, color, emoji, payload
            )
        except StarboardCogError as e:
            self.logger.info(f'Failed to starboard: {e!r}')

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        removed = await self.bot.user_db.remove_starboard_message(
            starboard_msg_id=payload.message_id
        )
        if removed:
            self.logger.info(
                f'Removed starboard record for deleted message {payload.message_id}'
            )

    @staticmethod
    def prepare_embed(message: discord.Message, color: int) -> discord.Embed:
        embed = discord.Embed(color=color, timestamp=message.created_at)
        embed.add_field(name='Channel', value=message.channel.mention)
        embed.add_field(name='Jump to', value=f'[Original]({message.jump_url})')

        if message.content:
            content = message.content
            if len(content) > _FIELD_LIMIT:
                # The repost links to the whole message.
                content = f'{content[: _FIELD_LIMIT - 1]}…'
            embed.add_field(name='Content', value=content, inline=False)

        if message.embeds:
            data = message.embeds[0]
            if data.type == 'image':
                embed.set_image(url=data.url)

        if message.attachments:
            file = message.attachments[0]
            if file.filename.lower().endswith(('png', 'jpeg', 'jpg', 'gif', 'webp')):
                embed.set_image(url=file.url)
            else:
                embed.add_field(
                    name='Attachment',
                    value=f'[{file.filename}]({file.url})',
                    inline=False,
                )

        embed.set_footer(
            text=str(message.author),
            icon_url=message.author.display_avatar.url,
        )
        return embed

    async def check_and_add_to_starboard(
        self,
        channel_id: int,
        threshold: int,
        color: int | None,
        emoji: str,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        guild = self.bot.get_guild(payload.guild_id)
        starboard_channel = guild.get_channel(channel_id)
        if starboard_channel is None:
            raise StarboardCogError('Starboard channel not found')

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            raise StarboardCogError('Channel of the message not found')
        kept = self._kept_from_starboard(guild, channel, starboard_channel)
        if kept is not None:
            self.logger.debug(f'Not reposting message {payload.message_id}: {kept}')
            return
        message = await channel.fetch_message(payload.message_id)
        if message.type != discord.MessageType.default or (
            not message.content and not message.attachments
        ):
            raise StarboardCogError('Cannot starboard this message')

        count = sum(r.count for r in message.reactions if str(r) == emoji)
        if count < threshold:
            return

        lock = self.locks.setdefault(payload.guild_id, asyncio.Lock())
        async with lock:
            if await self.bot.user_db.check_exists_starboard_message(message.id, emoji):
                return
            # Not `color or ...`: 0, which is #000000, is a colour too.
            if color is None:
                color = constants._DEFAULT_COLOR
            embed = self.prepare_embed(message, color)
            star_msg = await starboard_channel.send(embed=embed)
            await self.bot.user_db.add_starboard_message(
                message.id, star_msg.id, payload.guild_id, emoji
            )
            self.logger.info(f'Added message {message.id} to starboard under {emoji}')

    def _kept_from_starboard(
        self, guild: discord.Guild, channel: Any, starboard: Any
    ) -> str | None:
        """Why messages in ``channel`` must not be reposted in ``starboard``,
        or None if they may be.

        Messages in the server's staff channel, or in a thread in it, are for
        staff alone. And as a repost shows a message to everyone who can read
        the starboard, messages that some of them couldn't read where they
        were posted stay there too: those in a private thread, or in a channel
        that a role able to see the starboard can't see (for a thread, the
        channel it is in).

        The bot's access service knows the staff channel; without one, no
        channel is the staff channel. While the server's access settings need
        repair, the staff channel is unknown, so nothing may be reposted.
        """
        access = getattr(self.bot, 'access', None)
        if access is not None:
            settings = access.guild_access(guild.id)
            if settings.broken:
                return "the server's access settings need repair"
            if isinstance(channel, discord.Thread):
                place = channel.parent_id
            else:
                place = getattr(channel, 'id', None)
            if settings.staff_channel is not None and place == settings.staff_channel:
                return 'it is in the staff channel'
        if isinstance(channel, discord.Thread):
            if channel.is_private():
                return 'it is in a private thread'
            channel = channel.parent
            if channel is None:
                return "its thread's channel is unknown"
        if _hidden_from_some_readers(guild, channel, starboard):
            return 'not every reader of the starboard can see its channel'
        return None

    @commands.hybrid_group(brief='Show the starboard commands', fallback='show')
    async def starboard(self, ctx: commands.Context) -> None:
        """Show the starboard commands.

        Once a message gets enough reactions with a starboard emoji, the bot
        reposts it in that emoji's channel. Messages in the staff channel and
        its threads, in private threads, and in channels that some readers of
        the starboard channel can't see are never reposted.

        Examples:
            /starboard show
            ;starboard
        """
        await ctx.send_help(ctx.command)

    @starboard.command(
        brief='Add a starboard emoji and set how many of its reactions a message needs'
    )
    @app_commands.describe(
        emoji='The emoji members react with, such as ⭐',
        threshold='How many reactions with the emoji a message needs to be reposted',
        color=(
            "The colour of the emoji's reposts, as six hex digits such as #ffd700; "
            f'{_colour_text(constants._DEFAULT_COLOR)} if left out'
        ),
    )
    async def add(
        self,
        ctx: commands.Context,
        emoji: str,
        threshold: int,
        color: str | None = None,
    ) -> None:
        """Add a starboard emoji, and set how many reactions with it a message
        needs to be reposted. Adding an emoji again replaces its threshold and
        colour.

        Its reposts go to the channel where you use /starboard here.

        Examples:
            /starboard add ⭐ 5
            ;starboard add ⭐ 5 #ffd700
        """
        colour = constants._DEFAULT_COLOR if color is None else parse_colour(color)
        await self.bot.user_db.add_starboard_emoji(
            ctx.guild.id, emoji, threshold, colour
        )
        reactions = _reactions(threshold, emoji)
        entry = await self.bot.user_db.get_starboard_entry(ctx.guild.id, emoji)
        if entry is None:
            text = ADDED_WITHOUT_CHANNEL_TEXT.format(
                emoji=emoji, reactions=reactions, colour=_colour_text(colour)
            )
        else:
            text = ADDED_TEXT.format(
                emoji=emoji,
                reactions=reactions,
                channel=f'<#{entry[0]}>',
                colour=_colour_text(colour),
            )
        await ctx.send(embed=discord_common.embed_success(text))

    @starboard.command(brief='Delete a starboard emoji and its settings')
    @app_commands.describe(emoji='The starboard emoji to delete')
    async def delete(self, ctx: commands.Context, emoji: str) -> None:
        """Delete a starboard emoji, with its channel, threshold and colour. Its
        reposts stay.

        Examples:
            /starboard delete ⭐
        """
        removed = await self.bot.user_db.remove_starboard_emoji(ctx.guild.id, emoji)
        cleared = await self.bot.user_db.clear_starboard_channel(ctx.guild.id, emoji)
        if not removed and not cleared:
            raise StarboardCogError(NOT_ON_STARBOARD_TEXT.format(emoji=emoji))
        await ctx.send(
            embed=discord_common.embed_success(DELETED_TEXT.format(emoji=emoji))
        )

    @starboard.command(
        brief="Change how many of a starboard emoji's reactions a message needs"
    )
    @app_commands.describe(
        emoji='The starboard emoji to change',
        threshold='How many reactions with the emoji a message needs to be reposted',
    )
    async def edit_threshold(
        self, ctx: commands.Context, emoji: str, threshold: int
    ) -> None:
        """Change how many reactions with a starboard emoji a message needs to
        be reposted.

        Examples:
            /starboard edit_threshold ⭐ 10
        """
        changed = await self.bot.user_db.update_starboard_threshold(
            ctx.guild.id, emoji, threshold
        )
        if not changed:
            raise StarboardCogError(ADD_IT_TEXT.format(emoji=emoji))
        text = THRESHOLD_TEXT.format(reactions=_reactions(threshold, emoji))
        await ctx.send(embed=discord_common.embed_success(text))

    @starboard.command(brief="Change the colour of a starboard emoji's reposts")
    @app_commands.describe(
        emoji='The starboard emoji to change',
        color='The new colour of its reposts, as six hex digits such as #ffd700',
    )
    async def edit_color(self, ctx: commands.Context, emoji: str, color: str) -> None:
        """Change the colour of a starboard emoji's reposts. Reposts made
        already keep their colour.

        Examples:
            /starboard edit_color ⭐ #ffd700
            ;starboard edit_color ⭐ ffd700
        """
        colour = parse_colour(color)
        changed = await self.bot.user_db.update_starboard_color(
            ctx.guild.id, emoji, colour
        )
        if not changed:
            raise StarboardCogError(ADD_IT_TEXT.format(emoji=emoji))
        text = COLOUR_TEXT.format(emoji=emoji, colour=_colour_text(colour))
        await ctx.send(embed=discord_common.embed_success(text))

    @starboard.command(brief="Post an emoji's starred messages in this channel")
    @app_commands.describe(
        emoji='The emoji whose starred messages are posted in this channel'
    )
    async def here(self, ctx: commands.Context, emoji: str) -> None:
        """Post an emoji's starred messages in this channel: a message that gets
        enough reactions with the emoji is reposted here. This replaces the
        emoji's earlier channel, if it had one.

        Nothing is reposted until the emoji is added with /starboard add too.

        Examples:
            /starboard here ⭐
            ;starboard here ⭐
        """
        if isinstance(ctx.channel, discord.Thread):
            raise StarboardCogError(discord_common.NOT_IN_A_THREAD_MESSAGE)
        await self.bot.user_db.set_starboard_channel(
            ctx.guild.id, emoji, ctx.channel.id
        )
        text = HERE_TEXT.format(emoji=emoji, channel=ctx.channel.mention)
        await ctx.send(embed=discord_common.embed_success(text))

    @starboard.command(brief="Stop posting an emoji's starred messages")
    @app_commands.describe(emoji='The emoji whose starred messages to stop posting')
    async def clear(self, ctx: commands.Context, emoji: str) -> None:
        """Stop posting an emoji's starred messages, by clearing its channel. The
        emoji keeps its threshold and colour, so /starboard here starts posting
        them again.

        Examples:
            /starboard clear ⭐
        """
        cleared = await self.bot.user_db.clear_starboard_channel(ctx.guild.id, emoji)
        if not cleared:
            raise StarboardCogError(NO_CHANNEL_TEXT.format(emoji=emoji))
        await ctx.send(
            embed=discord_common.embed_success(CLEARED_TEXT.format(emoji=emoji))
        )

    @starboard.command(
        brief="Forget a message's repost, so that it can be reposted again"
    )
    @app_commands.describe(
        emoji='The starboard emoji the message was reposted with',
        original_message_id=(
            "The original message's ID, not the repost's: the last number of the "
            "repost's Original link"
        ),
    )
    async def remove(
        self, ctx: commands.Context, emoji: str, original_message_id: int
    ) -> None:
        """Forget that a message was reposted with a starboard emoji, so that it
        can be reposted again. The repost stays, but deleting it has the same
        effect. Use ;starboard remove, as Discord's slash options can't take a
        number as big as a message ID.

        Examples:
            ;starboard remove ⭐ 1234567890123456789
        """
        removed = await self.bot.user_db.remove_starboard_message(
            original_msg_id=original_message_id, emoji=emoji
        )
        if not removed:
            raise StarboardCogError(NO_REPOST_TEXT.format(emoji=emoji))
        await ctx.send(
            embed=discord_common.embed_success(FORGOTTEN_TEXT.format(emoji=emoji))
        )

    @discord_common.send_error_if(StarboardCogError)
    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Starboard(bot))
