import asyncio
import builtins
import contextlib
import datetime as dt
import html
import io
import logging
from collections.abc import Sequence
from typing import Any, Literal

import cairo
import discord
import gi
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.util import (
    ansi,
    codeforces_api as cf,
    codeforces_common as cf_common,
    discord_common,
    events,
    handle_linking,
    oauth,
    paginator,
    table,
    tasks,
)
from tle.util.cache import ContestNotFound

gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo

_HANDLES_PER_PAGE = 15
_NAME_MAX_LEN = 20
_PAGINATE_WAIT_TIME = 5 * 60  # 5 minutes
_TOP_DELTAS_COUNT = 10
_MAX_RATING_CHANGES_PER_EMBED = 15
_UPDATE_HANDLE_STATUS_INTERVAL = 6 * 60 * 60  # 6 hours
# Discord's limits on one message's embeds: how many, and how many characters
# in all.
_EMBEDS_PER_MESSAGE = 10
_EMBED_CHARACTERS_PER_MESSAGE = 6000

# What the options that take a choice accept, by command and option, for a
# value that is missing or isn't one of them. discord.py's own reply names the
# option instead, such as arg.
_CHOICES = {
    ('roleupdate auto', 'arg'): 'Choose `on` or `off`.',
    ('roleupdate publish', 'arg'): (
        'Choose `here`, `off` or a contest ID, such as `1950`.'
    ),
    ('role', 'action'): 'Choose `give` to take the role, or `remove` to drop it.',
    ('role', 'which'): 'Choose `duel` or `vc`.',
}
# The roles that /role hands out, by the choice that names them: the role's
# name, and what members get it for. Replies say what it is for, never its
# name.
_PING_ROLES = {
    'duel': ('Duelist', 'duel pings'),
    'vc': ('Virtual Contestant', 'virtual contest pings'),
}
# The trusted role, as replies name it: by what it is, never by its name or id.
_NO_TRUSTED_ROLE = 'This server has no trusted role, so nobody can be made trusted.'
# Why the trusted role can't be given, when Discord refuses it.
_TRUSTED_ROLE_REFUSED = (
    "I can't give the trusted role: it must be below my highest role, and I "
    'need the Manage Roles permission.'
)


class HandleCogError(commands.CommandError):
    pass


def rating_to_color(rating: int | str | None) -> tuple[int, int, int]:
    """returns (r, g, b) pixels values corresponding to rating"""
    # TODO: Integrate these colors with the ranks in codeforces_api.py
    BLACK = (10, 10, 10)
    RED = (255, 20, 20)
    BLUE = (0, 0, 200)
    GREEN = (0, 140, 0)
    ORANGE = (250, 140, 30)
    PURPLE = (160, 0, 120)
    CYAN = (0, 165, 170)
    GREY = (70, 70, 70)
    if rating is None or rating == 'N/A':
        return BLACK
    rating = int(rating)
    if rating < 1200:
        return GREY
    if rating < 1400:
        return GREEN
    if rating < 1600:
        return CYAN
    if rating < 1900:
        return BLUE
    if rating < 2100:
        return PURPLE
    if rating < 2400:
        return ORANGE
    return RED


FONTS = [
    'Noto Sans',
    'Noto Sans CJK JP',
    'Noto Sans CJK SC',
    'Noto Sans CJK TC',
    'Noto Sans CJK HK',
    'Noto Sans CJK KR',
]


def get_gudgitters_image(
    rankings: list[tuple[int, str, str, int | None, int]],
) -> discord.File:
    """return PIL image for rankings"""
    SMOKE_WHITE = (250, 250, 250)
    BLACK = (0, 0, 0)

    DISCORD_GRAY = (0.212, 0.244, 0.247)

    ROW_COLORS = ((0.95, 0.95, 0.95), (0.9, 0.9, 0.9))

    WIDTH = 900
    HEIGHT = 450
    BORDER_MARGIN = 20
    COLUMN_MARGIN = 10
    HEADER_SPACING = 1.25
    WIDTH_RANK = 0.08 * WIDTH
    WIDTH_NAME = 0.38 * WIDTH
    LINE_HEIGHT = (HEIGHT - 2 * BORDER_MARGIN) / (10 + HEADER_SPACING)

    # Cairo+Pango setup
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    context = cairo.Context(surface)
    context.set_line_width(1)
    context.set_source_rgb(*DISCORD_GRAY)
    context.rectangle(0, 0, WIDTH, HEIGHT)
    context.fill()
    layout = PangoCairo.create_layout(context)
    layout.set_font_description(
        Pango.font_description_from_string(','.join(FONTS) + ' 20')
    )
    layout.set_ellipsize(Pango.EllipsizeMode.END)

    def draw_bg(y: float, color_index: int) -> None:
        nxty = y + LINE_HEIGHT

        # Simple
        context.move_to(BORDER_MARGIN, y)
        context.line_to(WIDTH, y)
        context.line_to(WIDTH, nxty)
        context.line_to(0, nxty)
        context.set_source_rgb(*ROW_COLORS[color_index])
        context.fill()

    def draw_row(
        pos: str,
        username: str,
        handle: str,
        rating: str,
        color: tuple[int, int, int],
        y: float,
        bold: bool = False,
    ) -> None:
        context.set_source_rgb(*[x / 255.0 for x in color])

        context.move_to(BORDER_MARGIN, y)

        def draw(text: str, width: float = -1) -> None:
            text = html.escape(text)
            if bold:
                text = f'<b>{text}</b>'
            layout.set_width((width - COLUMN_MARGIN) * 1000)  # pixel = 1000 pango units
            layout.set_markup(text, -1)
            PangoCairo.show_layout(context, layout)
            context.rel_move_to(width, 0)

        draw(pos, WIDTH_RANK)
        draw(username, WIDTH_NAME)
        draw(handle, WIDTH_NAME)
        draw(rating)

    #

    y: float = BORDER_MARGIN

    # draw header
    draw_row('#', 'Name', 'Handle', 'Points', SMOKE_WHITE, y, bold=True)
    y += LINE_HEIGHT * HEADER_SPACING

    for i, (pos, name, handle, rating, score) in enumerate(rankings):
        color = rating_to_color(rating)
        draw_bg(y, i % 2)
        draw_row(
            str(pos),
            f'{name} ({rating if rating else "N/A"})',
            handle,
            str(score),
            color,
            y,
        )
        if rating and rating >= 3000:  # nutella
            draw_row('', name[0], handle[0], '', BLACK, y)
        y += LINE_HEIGHT

    image_data = io.BytesIO()
    surface.write_to_png(image_data)
    image_data.seek(0)
    discord_file = discord.File(image_data, filename='gudgitters.png')
    return discord_file


def _make_profile_embed(
    member: discord.Member, user: cf.User, *, mode: str
) -> discord.Embed:
    assert mode in ('set', 'get')
    if mode == 'set':
        desc = (
            f'Handle for {member.mention} successfully set to'
            f' **[{user.handle}]({user.url})**'
        )
    else:
        desc = (
            f'Handle for {member.mention} is currently set to'
            f' **[{user.handle}]({user.url})**'
        )
    if user.rating is None:
        embed = discord.Embed(description=desc)
        embed.add_field(name='Rating', value='Unrated', inline=True)
    else:
        embed = discord.Embed(description=desc, color=user.rank.color_embed)
        embed.add_field(name='Rating', value=user.rating, inline=True)
        embed.add_field(name='Rank', value=user.rank.title, inline=True)
    embed.set_thumbnail(url=f'{user.titlePhoto}')
    return embed


def _make_pages(
    users: list[tuple[discord.Member, str, int | None]], title: str
) -> list[tuple[str, discord.Embed]]:
    chunks = paginator.chunkify(users, _HANDLES_PER_PAGE)
    pages = []
    done = 0

    style = table.Style('{:>}  {:<}  {:<}  {:<}')
    for chunk in chunks:
        t = table.Table(style)
        t += table.Header('#', 'Name', 'Handle', 'Rating')
        t += table.Line()
        for i, (member, handle, rating) in enumerate(chunk):
            name = member.display_name
            if len(name) > _NAME_MAX_LEN:
                name = name[: _NAME_MAX_LEN - 1] + '…'
            rank = cf.rating2rank(rating)
            rating_str = 'N/A' if rating is None else str(rating)
            colors = ansi.make_cell_colors(rank, ncols=4, handle_col=2)
            t += table.Data(
                i + done,
                name,
                handle,
                f'{rating_str} ({rank.title_abbr})',
                colors=colors,
            )
        table_str = '```ansi\n' + str(t) + '\n```'
        embed = discord_common.cf_color_embed(description=table_str)
        pages.append((title, embed))
        done += len(chunk)
    return pages


def _linked_handle_line(platform: str, handle: str, url: str) -> str:
    """'**Platform:** handle', the handle linked to its profile."""
    return f'**{platform}:** [{discord.utils.escape_markdown(handle)}]({url})'


def _embed_batches(embeds: Sequence[discord.Embed]) -> list[list[discord.Embed]]:
    """``embeds`` in order, in batches that each fit in one message.

    A batch grows only while it holds at most 10 embeds of at most 6000
    characters in all, Discord's limits on one message.
    """
    batches: list[list[discord.Embed]] = []
    size = 0
    for embed in embeds:
        if (
            batches
            and len(batches[-1]) < _EMBEDS_PER_MESSAGE
            and size + len(embed) <= _EMBED_CHARACTERS_PER_MESSAGE
        ):
            batches[-1].append(embed)
            size += len(embed)
        else:
            batches.append([embed])
            size = len(embed)
    return batches


class Handles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot
        self.logger: logging.Logger = logging.getLogger(self.__class__.__name__)
        self.converter: commands.MemberConverter = commands.MemberConverter()

    @commands.Cog.listener()
    @discord_common.once
    async def on_ready(self) -> None:
        self.bot.event_sys.add_listener(self._on_rating_changes)
        assert isinstance(self._set_ex_users_inactive_task, tasks.Task)
        self._set_ex_users_inactive_task.start()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bot.user_db.set_inactive([(member.guild.id, member.id)])

    @commands.hybrid_command(brief="Mark this server's current members as active")
    async def _updatestatus(self, ctx: commands.Context) -> None:
        """Mark the linked handles of this server's current members as active,
        and those of members who have left as inactive.

        Rank updates and ;handle list leave out inactive handles. The bot keeps
        this up to date as members join and leave, so you need this only if it
        missed some, for example while it was offline.

        Examples:
            /_updatestatus
            ;_updatestatus
        """
        gid = ctx.guild.id
        active_ids = [m.id for m in ctx.guild.members]
        await self.bot.user_db.reset_status(gid)
        rc = 0
        for chunk in paginator.chunkify(active_ids, 100):
            rc += await self.bot.user_db.update_status(gid, chunk)
        members = 'member' if rc == 1 else 'members'
        await ctx.send(f'Marked {rc} {members} with a linked handle as active.')

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        rc = await self.bot.user_db.update_status(member.guild.id, [member.id])
        if rc == 1:
            handle = await self.bot.user_db.get_handle(member.id, member.guild.id)
            await self._update_ranks(member.guild, [(int(member.id), handle)])

    @tasks.task_spec(
        name='SetExUsersInactive',
        waiter=tasks.Waiter.fixed_delay(_UPDATE_HANDLE_STATUS_INTERVAL),
    )
    async def _set_ex_users_inactive_task(self, _: Any) -> None:
        # To set users inactive in case the bot was dead when they left.
        to_set_inactive = []
        for guild in self.bot.guilds:
            user_id_handle_pairs = await self.bot.user_db.get_handles_for_guild(
                guild.id
            )
            to_set_inactive += [
                (guild.id, user_id)
                for user_id, _ in user_id_handle_pairs
                if guild.get_member(user_id) is None
            ]
        await self.bot.user_db.set_inactive(to_set_inactive)

    @events.listener_spec(
        name='RatingChangesListener',
        event_cls=events.RatingChangesUpdate,
        with_lock=True,
    )
    async def _on_rating_changes(self, event: events.RatingChangesUpdate) -> None:
        contest, changes = event.contest, event.rating_changes
        change_by_handle = {change.handle: change for change in changes}

        async def update_for_guild(guild: discord.Guild) -> None:
            if await self.bot.user_db.has_auto_role_update_enabled(guild.id):
                with contextlib.suppress(HandleCogError):
                    await self._update_ranks_all(guild)
            channel_id = await self.bot.user_db.get_rankup_channel(guild.id)
            channel = guild.get_channel(channel_id)
            if channel is not None:
                with contextlib.suppress(HandleCogError):
                    embeds = await self._make_rankup_embeds(
                        guild, contest, change_by_handle
                    )
                    for embed in embeds:
                        await channel.send(embed=embed)

        await asyncio.gather(
            *(update_for_guild(guild) for guild in self.bot.guilds),
            return_exceptions=True,
        )
        self.logger.info(f'All guilds updated for contest {contest.id}.')

    @commands.hybrid_group(brief='Link, show and look up Codeforces handles')
    async def handle(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show the handles a member has linked: yours, if you name no one.

        The commands in this group link Codeforces handles, look them up, and
        keep them up to date when members change them.

        Examples:
            ;handle
            ;handle @alice
        """
        await self._show_handles(ctx, member)

    # /handle show and ;handle show are one hybrid subcommand, which Discord's
    # picker describes with its own brief. A group fallback would get the
    # group's brief instead, and couldn't sit beside a prefix-only show: as
    # discord.py makes the cog, it re-adds each subcommand to its group, first
    # removing the slash command of that name, which would drop the fallback.
    @handle.command(name='show', brief="Show a member's linked handles")
    @app_commands.describe(member='Whose handles to show; yours if left out')
    async def show(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show the handles a member has linked: yours, if you name no one.

        That is their Codeforces handle, and the accounts they linked with
        /link, such as AtCoder.

        Examples:
            /handle show
            /handle show member:@alice
            ;handle show @alice
        """
        await self._show_handles(ctx, member)

    async def _show_handles(
        self, ctx: commands.Context, member: discord.Member | None
    ) -> None:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        member = member or ctx.author
        lines = []
        handle = await self.bot.user_db.get_handle(member.id, ctx.guild.id)
        if handle:
            url = f'{cf.PROFILE_BASE_URL}{handle}'
            lines.append(_linked_handle_line('Codeforces', handle, url))
        lines += await self._kcpc_handle_lines(ctx.guild.id, member)
        if not lines:
            raise HandleCogError(f'{member.mention} has not linked any handles.')
        embed = discord_common.embed_neutral('\n'.join(lines))
        name = discord.utils.escape_markdown(member.display_name)
        embed.title = f'Handles of {name}'
        await ctx.send(embed=embed)

    async def _kcpc_handle_lines(
        self, guild_id: int, member: discord.Member
    ) -> list[str]:
        """The accounts the member linked with KCPC's /link, if KCPC is running."""
        kcpc = getattr(self.bot, 'kcpc', None)
        if kcpc is None:
            return []
        try:
            # Imported here, as KCPC is optional: a KCPC module that fails to
            # import must not stop this cog from loading.
            from tle.kcpc.features.accounts import directory

            accounts = await directory.linked_accounts(kcpc, guild_id, member.id)
            return [
                _linked_handle_line(
                    directory.platform_name(platform),
                    linked,
                    directory.profile_url(platform, linked),
                )
                for platform, linked in accounts
            ]
        except Exception:
            # The member's Codeforces handle is still worth showing.
            self.logger.exception(f'Could not list the KCPC accounts of {member}')
            return []

    async def maybe_add_trusted_role(self, member: discord.Member) -> None:
        """Add trusted role for eligible users.

        See `handle_linking.maybe_add_trusted_role`.
        """
        await handle_linking.maybe_add_trusted_role(
            member, user_db=self.bot.user_db, log=self.logger
        )

    async def update_member_rank_role(
        self,
        member: discord.Member,
        role_to_assign: discord.Role | None,
        *,
        reason: str,
    ) -> None:
        """Sets the `member` to only have the rank role of `role_to_assign`.

        See `handle_linking.update_member_rank_role`.
        """
        await handle_linking.update_member_rank_role(
            member,
            role_to_assign,
            reason=reason,
            user_db=self.bot.user_db,
            log=self.logger,
        )

    @handle.command(brief='Link a member to a Codeforces handle', aliases=['link'])
    @app_commands.describe(
        member='The member to link', handle='Their Codeforces handle, such as tourist'
    )
    async def set(
        self, ctx: commands.Context, member: discord.Member, handle: str
    ) -> None:
        """Link a member to a Codeforces handle, in place of any they had.

        They get the role for the handle's Codeforces rank instead of their
        old rank role.

        Examples:
            ;handle set @alice tourist
        """
        async with ctx.typing():
            # CF API returns correct handle ignoring case, update to it
            (user,) = await cf.user.info(handles=[handle])
            await self._set(ctx, member, user)
        embed = _make_profile_embed(member, user, mode='set')
        await ctx.send(embed=embed)

    async def _set_from_oauth(
        self, guild: discord.Guild, member: discord.Member, user: cf.User
    ) -> None:
        """Link `member` to `user`; see `handle_linking.link_handle`."""
        try:
            await handle_linking.link_handle(
                self.bot.user_db, guild, member, user, log=self.logger
            )
        except handle_linking.HandleTakenError as e:
            raise HandleCogError(f'When setting handle for {member}: {e}')
        except handle_linking.HandleLinkError as e:
            raise HandleCogError(str(e))

    async def _set(
        self, ctx: commands.Context, member: discord.Member, user: cf.User
    ) -> None:
        await self._set_from_oauth(ctx.guild, member, user)

    @handle.command(brief='Sign in to Codeforces to link your account')
    async def identify(self, ctx: commands.Context) -> None:
        """Link your Codeforces account by signing in to Codeforces.

        You get a sign-in link that works for 5 minutes, then a message saying
        whether your account was linked. Only you see them: with ;handle
        identify, both come by direct message.

        Examples:
            /handle identify
            ;handle identify
        """
        if not constants.OAUTH_CONFIGURED:
            raise HandleCogError(
                "Signing in to Codeforces isn't set up for this bot. Ask a "
                'moderator to link your handle.'
            )

        if await self.bot.user_db.get_handle(ctx.author.id, ctx.guild.id):
            raise HandleCogError(
                'You have already linked a Codeforces handle. To change it, ask a '
                'moderator.'
            )

        self.bot.oauth_state_store.revoke(ctx.author.id)

        # The slash command's interaction tells the member how it went, as
        # privately as this; without one, they hear by direct message.
        state = self.bot.oauth_state_store.create(
            ctx.author.id, ctx.guild.id, interaction=ctx.interaction
        )
        assert constants.OAUTH_CLIENT_ID is not None
        assert constants.OAUTH_REDIRECT_URI is not None
        auth_url = oauth.build_auth_url(
            constants.OAUTH_CLIENT_ID, constants.OAUTH_REDIRECT_URI, state
        )

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label='Sign in to Codeforces',
                url=auth_url,
            )
        )
        msg = (
            'Press the button to sign in to Codeforces and link your account.'
            " The link works for 5 minutes, and I'll tell you here how it went."
        )
        if ctx.interaction:
            await ctx.send(msg, view=view, ephemeral=True)
        else:
            try:
                await ctx.author.send(msg, view=view)
                await ctx.send("I've sent you the sign-in link in a direct message.")
            except discord.Forbidden:
                self.bot.oauth_state_store.revoke(ctx.author.id)
                await ctx.send(
                    "I couldn't send you a direct message. Allow direct messages"
                    " from this server's members and try again, or use"
                    ' `/handle identify`.'
                )

    @handle.command(brief="Show a member's Codeforces handle, rating and rank")
    @app_commands.describe(member='The member whose handle to show')
    async def get(self, ctx: commands.Context, member: discord.Member) -> None:
        """Show the Codeforces handle a member has linked, with its rating and
        rank.

        Examples:
            /handle get member:@alice
            ;handle get @alice
        """
        handle = await self.bot.user_db.get_handle(member.id, ctx.guild.id)
        if not handle:
            raise HandleCogError(f"{member.mention} hasn't linked a Codeforces handle.")
        user = await self.bot.user_db.fetch_cf_user(handle)
        embed = _make_profile_embed(member, user, mode='get')
        await ctx.send(embed=embed)

    @handle.command(brief='Find who linked a Codeforces handle')
    @app_commands.describe(handle='A Codeforces handle, such as tourist')
    async def rget(self, ctx: commands.Context, handle: str) -> None:
        """Find the member of this server who linked a Codeforces handle.

        Examples:
            /handle rget handle:tourist
            ;handle rget tourist
        """
        user_id = await self.bot.user_db.get_user_id(handle, ctx.guild.id)
        if not user_id:
            raise HandleCogError(f'No member of this server has linked `{handle}`.')
        member = ctx.guild.get_member(user_id)
        if member is None:
            # TLE keeps the handles of members who leave, for if they come back.
            raise HandleCogError(
                f'`{handle}` was linked by someone who has left this server.'
            )
        user = await self.bot.user_db.fetch_cf_user(handle)
        embed = _make_profile_embed(member, user, mode='get')
        await ctx.send(embed=embed)

    @handle.command(
        brief='Unlink a Codeforces handle from the member who linked it',
        aliases=['unlink'],
    )
    @app_commands.describe(
        handle='The Codeforces handle, or ! and a member, such as !alice'
    )
    async def remove(self, ctx: commands.Context, handle: str) -> None:
        """Unlink a Codeforces handle from the member who linked it, who also
        loses their rank role.

        Name the handle, or the member with ! in front.

        Examples:
            ;handle remove tourist
            ;handle remove !alice
        """
        (handle,) = await cf_common.resolve_handles(ctx, self.converter, [handle])
        user_id = await self.bot.user_db.get_user_id(handle, ctx.guild.id)
        if user_id is None:
            raise HandleCogError(f'No member of this server has linked `{handle}`.')

        await self.bot.user_db.remove_handle(handle, ctx.guild.id)
        member = ctx.guild.get_member(user_id)
        # A member who left the server (TLE keeps their handle) has no roles here.
        if member is not None:
            await self.update_member_rank_role(
                member, role_to_assign=None, reason='Handle unlinked'
            )
        embed = discord_common.embed_success(f'Unlinked `{handle}`.')
        await ctx.send(embed=embed)

    @handle.command(brief='Update your handle after you change it on Codeforces')
    async def unmagic(self, ctx: commands.Context) -> None:
        """Update your linked handle after you change it on Codeforces.

        Codeforces lets you change your handle around each New Year. This finds
        your new handle through your old one, and links it instead.

        Examples:
            /handle unmagic
            ;handle unmagic
        """
        member = ctx.author
        handle = await self.bot.user_db.get_handle(member.id, ctx.guild.id)
        if handle is None:
            raise HandleCogError(
                "You haven't linked a Codeforces handle, so there is none to update."
            )
        async with ctx.typing():
            await self._unmagic_handles(ctx, [handle], {handle: member})

    @handle.command(brief='Update every linked handle changed on Codeforces')
    async def unmagic_all(self, ctx: commands.Context) -> None:
        """Update the linked handles of all members who have changed their
        handle on Codeforces, as many do around each New Year.

        Examples:
            ;handle unmagic_all
        """
        user_id_and_handles = await self.bot.user_db.get_handles_for_guild(ctx.guild.id)

        handles = []
        rev_lookup = {}
        for user_id, handle in user_id_and_handles:
            member = ctx.guild.get_member(user_id)
            handles.append(handle)
            rev_lookup[handle] = member
        async with ctx.typing():
            await self._unmagic_handles(ctx, handles, rev_lookup)

    @handle.command(
        brief='Preview what unmagic would do for the handles you give',
        usage='<handles...> [+skip_filter]',
        with_app_command=False,
    )
    async def unmagic_debug(self, ctx: commands.Context, *args: str) -> None:
        """Show the handle that each handle you give now leads to on
        Codeforces, as ;handle unmagic would see it, without linking anything.

        It lists only the handles that have changed, unless you add
        +skip_filter.

        Examples:
            ;handle unmagic_debug tourist
            ;handle unmagic_debug tourist Petr +skip_filter
        """
        handles = list(args)
        skip_filter = False
        if '+skip_filter' in handles:
            handles.remove('+skip_filter')
            skip_filter = True
        handle_cf_user_mapping = await cf.resolve_redirects(handles, skip_filter)

        lines = ['Resolved handles:']
        for handle, cf_user in handle_cf_user_mapping.items():
            if cf_user:
                lines.append(f'{handle} -> {cf_user.handle}')
            else:
                lines.append(f'{handle} -> None')
        await ctx.send(embed=discord_common.embed_success('\n'.join(lines)))

    async def _unmagic_handles(
        self,
        ctx: commands.Context,
        handles: list[str],
        rev_lookup: dict[str, discord.Member | None],
    ) -> None:
        handle_cf_user_mapping = await cf.resolve_redirects(handles)
        mapping: dict[tuple[discord.Member | None, str], cf.User | None] = {
            (rev_lookup[handle], handle): cf_user
            for handle, cf_user in handle_cf_user_mapping.items()
        }
        summary_embed = await self._fix_and_report(ctx, mapping)
        await ctx.send(embed=summary_embed)

    async def _fix_and_report(
        self,
        ctx: commands.Context,
        redirections: dict[tuple[discord.Member | None, str], cf.User | None],
    ) -> discord.Embed:
        fixed = []
        failed = []
        for (member, handle), cf_user in redirections.items():
            if not cf_user:
                failed.append(handle)
            else:
                await self._set(ctx, member, cf_user)
                fixed.append((handle, cf_user.handle))

        # Return summary embed
        lines = []
        if not fixed and not failed:
            return discord_common.embed_success(
                'No linked handle has changed on Codeforces.'
            )
        if fixed:
            lines.append('**Fixed**')
            lines += (f'{old} -> {new}' for old, new in fixed)
        if failed:
            lines.append('**Failed**')
            lines += failed
        return discord_common.embed_success('\n'.join(lines))

    @commands.hybrid_command(
        brief='Show the members with the most gitgud points', aliases=['gitgudders']
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def gudgitters(self, ctx: commands.Context) -> None:
        """Show the 10 members of this server with the most gitgud points.

        You earn points by solving the problems that /gitgud gives you.

        Examples:
            /gudgitters
            ;gudgitters
        """
        async with ctx.typing():
            res = await self.bot.user_db.get_gudgitters()
            res.sort(key=lambda r: r[1], reverse=True)

            rankings = []
            index = 0
            for user_id, score in res:
                member = ctx.guild.get_member(int(user_id))
                if member is None:
                    continue
                if score > 0:
                    handle = await self.bot.user_db.get_handle(user_id, ctx.guild.id)
                    user = await self.bot.user_db.fetch_cf_user(handle)
                    if user is None:
                        continue
                    discord_handle = member.display_name
                    rating = user.rating
                    rankings.append((index, discord_handle, handle, rating, score))
                    index += 1
                if index == 10:
                    break

            if not rankings:
                raise HandleCogError(
                    'Nobody here has solved a gitgud problem yet. Get one with '
                    '`/gitgud`, and say you solved it with `/gotgud`.'
                )
            discord_file = get_gudgitters_image(rankings)
        await ctx.send(file=discord_file)

    @handle.command(
        brief="List the server's members with their Codeforces handles",
        with_app_command=False,
    )
    async def list(self, ctx: commands.Context, *countries: str) -> None:
        """List the members of this server who have linked a Codeforces handle,
        highest rated first.

        Name countries to list only the members whose Codeforces profiles give
        one of them. Put a name of several words in quotes.

        Examples:
            ;handle list
            ;handle list Croatia Slovenia
            ;handle list "United Kingdom"
        """
        country_list = [country.title() for country in countries]
        res = await self.bot.user_db.get_cf_users_for_guild(ctx.guild.id)
        users = [
            (ctx.guild.get_member(user_id), cf_user.handle, cf_user.rating)
            for user_id, cf_user in res
            if not country_list or cf_user.country in country_list
        ]
        users = [
            (member, handle, rating)
            for member, handle, rating in users
            if member is not None
        ]
        if not users:
            if country_list:
                raise HandleCogError(
                    'No member of this server from those countries has linked a '
                    'Codeforces handle.'
                )
            raise HandleCogError(
                'No member of this server has linked a Codeforces handle.'
            )

        users.sort(
            key=lambda x: (1 if x[2] is None else -x[2], x[1])
        )  # Sorting by (-rating, handle)
        title = 'Handles of server members'
        if country_list:
            title += ' from ' + ', '.join(f'`{country}`' for country in country_list)
        pages = _make_pages(users, title)
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=_PAGINATE_WAIT_TIME,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    async def _update_ranks_all(self, guild: discord.Guild) -> None:
        """For each member in the guild, fetches their current ratings and
        updates their role if required.
        """
        res = await self.bot.user_db.get_handles_for_guild(guild.id)
        await self._update_ranks(guild, res)

    async def _update_ranks(
        self, guild: discord.Guild, res: builtins.list[tuple[int, str]]
    ) -> None:
        member_handles = [
            (guild.get_member(user_id), handle) for user_id, handle in res
        ]
        member_handles = [
            (member, handle) for member, handle in member_handles if member is not None
        ]
        if not member_handles:
            raise HandleCogError(
                'No member of this server has linked a Codeforces handle.'
            )
        members, handles = zip(*member_handles, strict=False)
        users = await cf.user.info(handles=handles)
        for user in users:
            await self.bot.user_db.cache_cf_user(user)

        required_roles = {
            user.rank.title for user in users if user.rank != cf.UNRATED_RANK
        }
        rank2role = {
            role.name: role for role in guild.roles if role.name in required_roles
        }
        missing_roles = required_roles - rank2role.keys()
        if missing_roles:
            roles_str = ', '.join(f'`{role}`' for role in sorted(missing_roles))
            plural = 's' if len(missing_roles) > 1 else ''
            raise HandleCogError(
                f'This server has no role for the rank{plural} {roles_str}. Add '
                'a role named after each rank, then try again.'
            )

        for member, user in zip(members, users, strict=False):
            role_to_assign = (
                None if user.rank == cf.UNRATED_RANK else rank2role[user.rank.title]
            )
            await self.update_member_rank_role(
                member, role_to_assign, reason='Codeforces rank update'
            )

    async def _make_rankup_embeds(
        self,
        guild: discord.Guild,
        contest: cf.Contest,
        change_by_handle: dict[str, cf.RatingChange],
    ) -> builtins.list[discord.Embed]:
        """Make an embed containing a list of rank changes and top rating
        increases for the members of this guild.
        """
        user_id_handle_pairs = await self.bot.user_db.get_handles_for_guild(guild.id)
        member_handle_pairs = [
            (guild.get_member(user_id), handle)
            for user_id, handle in user_id_handle_pairs
        ]

        def ispurg(member: discord.Member) -> bool:
            return discord_common.has_role(member, constants.TLE_PURGATORY)

        member_change_pairs = [
            (member, change_by_handle[handle])
            for member, handle in member_handle_pairs
            if member is not None and handle in change_by_handle and not ispurg(member)
        ]
        if not member_change_pairs:
            raise HandleCogError(
                f'Contest `{contest.id} | {contest.name}`'
                ' was not rated for any member of this server.'
            )

        member_change_pairs.sort(key=lambda pair: pair[1].newRating, reverse=True)
        rank_to_role = {role.name: role for role in guild.roles}

        def rating_to_displayable_rank(rating: int) -> str:
            rank = cf.rating2rank(rating).title
            role = rank_to_role.get(rank)
            return role.mention if role else rank

        rank_changes_str = []
        for member, change in member_change_pairs:
            cache = self.bot.cf_cache.rating_changes_cache
            if (
                change.oldRating == 1500
                and len(await cache.get_rating_changes_for_handle(change.handle)) == 1
            ):
                # If this is the user's first rated contest.
                old_role = 'Unrated'
            else:
                old_role = rating_to_displayable_rank(change.oldRating)
            new_role = rating_to_displayable_rank(change.newRating)
            if new_role != old_role:
                rank_change_str = (
                    f'{member.mention}'
                    f' [{change.handle}]({cf.PROFILE_BASE_URL}{change.handle}):'
                    f' {old_role} \N{LONG RIGHTWARDS ARROW} {new_role}'
                )
                rank_changes_str.append(rank_change_str)

        member_change_pairs.sort(
            key=lambda pair: pair[1].newRating - pair[1].oldRating, reverse=True
        )
        top_increases_str = []
        for member, change in member_change_pairs[:_TOP_DELTAS_COUNT]:
            delta = change.newRating - change.oldRating
            if delta <= 0:
                break
            increase_str = (
                f'{member.mention}'
                f' [{change.handle}]({cf.PROFILE_BASE_URL}{change.handle}):'
                f' {change.oldRating} \N{HORIZONTAL BAR} **{delta:+}**'
                f' \N{LONG RIGHTWARDS ARROW} {change.newRating}'
            )
            top_increases_str.append(increase_str)

        rank_changes_str = rank_changes_str or ['No rank changes']

        embed_heading = discord.Embed(
            title=contest.name, url=contest.url, description=''
        )
        embed_heading.set_author(name='Rank updates')
        embeds = [embed_heading]

        for rank_changes_chunk in paginator.chunkify(
            rank_changes_str, _MAX_RATING_CHANGES_PER_EMBED
        ):
            desc = '\n'.join(rank_changes_chunk)
            embed = discord.Embed(description=desc)
            embeds.append(embed)

        top_rating_increases_embed = discord.Embed(
            description='\n'.join(top_increases_str) or 'Nobody got a positive delta :('
        )
        top_rating_increases_embed.set_author(name='Top rating increases')

        embeds.append(top_rating_increases_embed)
        discord_common.set_same_cf_color(embeds)

        return embeds

    @commands.hybrid_group(brief='Show the rank role commands', fallback='show')
    async def roleupdate(self, ctx: commands.Context) -> None:
        """Show the commands that update rank roles, the roles named after
        Codeforces ranks such as Expert, and post members' rank changes after
        contests.

        Examples:
            /roleupdate show
            ;roleupdate
        """
        await ctx.send_help(ctx.command)

    @roleupdate.command(brief="Update every member's rank role from Codeforces now")
    async def now(self, ctx: commands.Context) -> None:
        """Give every member with a linked handle the rank role for their
        current Codeforces rating, in place of any other rank role.

        Examples:
            /roleupdate now
            ;roleupdate now
        """
        async with ctx.typing():
            await self._update_ranks_all(ctx.guild)
        await ctx.send(
            embed=discord_common.embed_success(
                'Updated the rank role of every member with a linked handle.'
            )
        )

    @roleupdate.command(brief='Turn automatic rank role updates on or off')
    @app_commands.describe(
        arg='on to update rank roles after each rated contest; off to stop'
    )
    async def auto(self, ctx: commands.Context, arg: Literal['on', 'off']) -> None:
        """Turn automatic rank role updates on or off. While they are on, every
        member's rank role is updated whenever Codeforces publishes the rating
        changes of a contest.

        Examples:
            /roleupdate auto arg:on
            ;roleupdate auto off
        """
        if arg == 'on':
            rc = await self.bot.user_db.enable_auto_role_update(ctx.guild.id)
            if not rc:
                raise HandleCogError('Automatic rank role updates are already on.')
            await ctx.send(
                embed=discord_common.embed_success(
                    'Automatic rank role updates are on.'
                )
            )
        elif arg == 'off':
            rc = await self.bot.user_db.disable_auto_role_update(ctx.guild.id)
            if not rc:
                raise HandleCogError('Automatic rank role updates are already off.')
            await ctx.send(
                embed=discord_common.embed_success(
                    'Automatic rank role updates are off.'
                )
            )
        else:
            # Discord offers only on and off, and ;roleupdate auto takes no
            # other value, so only a stray value gets here.
            raise HandleCogError(_CHOICES['roleupdate auto', 'arg'])

    @roleupdate.command(
        brief='Post rank changes here after each contest, or for one contest now'
    )
    @app_commands.describe(
        arg='here to post after each contest, off to stop, or a contest ID to post '
        'its changes now'
    )
    async def publish(self, ctx: commands.Context, arg: str) -> None:
        """Post this server's rank changes and biggest rating gains in this
        channel. With here, they come after each rated contest, until you use
        off. With a contest ID, that contest's come now.

        Examples:
            /roleupdate publish arg:here
            /roleupdate publish arg:1950
            ;roleupdate publish off
        """
        if arg == 'here':
            if isinstance(ctx.channel, discord.Thread):
                raise HandleCogError(discord_common.NOT_IN_A_THREAD_MESSAGE)
            await self.bot.user_db.set_rankup_channel(ctx.guild.id, ctx.channel.id)
            await ctx.send(
                embed=discord_common.embed_success(
                    'Rank changes will be posted in this channel after each rated '
                    'contest.'
                )
            )
        elif arg == 'off':
            rc = await self.bot.user_db.clear_rankup_channel(ctx.guild.id)
            if not rc:
                raise HandleCogError(
                    "Rank changes aren't posted after contests, so there is nothing "
                    'to stop.'
                )
            await ctx.send(
                embed=discord_common.embed_success(
                    'Rank changes will no longer be posted after contests.'
                )
            )
        else:
            try:
                contest_id = int(arg)
            except ValueError:
                raise HandleCogError(_CHOICES['roleupdate publish', 'arg'])
            await self._publish_now(ctx, contest_id)

    async def _publish_now(self, ctx: commands.Context, contest_id: int) -> None:
        try:
            contest = self.bot.cf_cache.contest_cache.get_contest(contest_id)
        except ContestNotFound as e:
            raise HandleCogError(f'Contest with ID `{e.contest_id}` not found.')
        if contest.phase != 'FINISHED':
            raise HandleCogError(
                f'Contest `{contest_id} | {contest.name}` has not finished.'
            )
        # Codeforces may take longer to answer than Discord waits for a slash
        # command's first answer.
        await ctx.defer()
        try:
            changes = await cf.contest.ratingChanges(contest_id=contest_id)
        except cf.RatingChangesUnavailableError:
            changes = None
        if not changes:
            raise HandleCogError(
                'Rating changes are not available for contest'
                f' `{contest_id} | {contest.name}`.'
            )

        change_by_handle = {change.handle: change for change in changes}
        rankup_embeds = await self._make_rankup_embeds(
            ctx.guild, contest, change_by_handle
        )
        # As answers to the command, in as few messages as Discord allows.
        for batch in _embed_batches(rankup_embeds):
            await ctx.send(embeds=batch)

    @commands.hybrid_command(
        brief='Take or drop the ping role for duels or virtual contests'
    )
    @app_commands.describe(
        action='give to take the role, or remove to drop it',
        which='duel for duel pings, or vc for virtual contest pings',
    )
    async def role(self, ctx: commands.Context, action: str, which: str) -> None:
        """Take or drop the role that is pinged about duels, or the one pinged
        about virtual contests.

        Examples:
            /role action:give which:duel
            ;role give duel
            ;role remove vc
        """
        if action not in ('give', 'remove'):
            raise HandleCogError(_CHOICES['role', 'action'])
        if which not in _PING_ROLES:
            raise HandleCogError(_CHOICES['role', 'which'])
        role_name, pings = _PING_ROLES[which]
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if role is None:
            raise HandleCogError(f'This server has no role for {pings}.')
        has_it = role in ctx.author.roles
        if action == 'give':
            if has_it:
                text = f'You already have the role for {pings}.'
                await ctx.send(embed=discord_common.embed_neutral(text))
                return
            # The role is whichever has that name, so it may be one that must
            # not be handed out to whoever asks, such as a staff role.
            problem = discord_common.self_assignable_problem(role, ctx.guild.me)
            if problem is not None:
                raise HandleCogError(
                    f"I can't give you the role for {pings}: {problem}. Ask an "
                    'admin to fix the role.'
                )
            await ctx.author.add_roles(role, reason=f'Member asked for {pings}')
            text = f'You now have the role for {pings}.'
        else:
            if not has_it:
                text = f"You don't have the role for {pings}."
                await ctx.send(embed=discord_common.embed_neutral(text))
                return
            await ctx.author.remove_roles(role, reason=f'Member asked to stop {pings}')
            text = f'You no longer have the role for {pings}.'
        await ctx.send(embed=discord_common.embed_success(text))

    @discord_common.send_error_if(HandleCogError, cf_common.ResolveHandleError)
    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        # An option that takes a choice, left out or given something else on
        # prefix, gets the choices: discord.py's own reply names the option.
        if ctx.command is not None and isinstance(
            error, (commands.MissingRequiredArgument, commands.BadLiteralArgument)
        ):
            choices = _CHOICES.get((ctx.command.qualified_name, error.param.name))
            if choices is not None:
                error.handled = True
                await self.cog_command_error(ctx, HandleCogError(choices))

    @handle.command(brief='Make another member trusted')
    @app_commands.describe(target_user='The member to make trusted')
    async def refer(self, ctx: commands.Context, target_user: discord.Member) -> None:
        """Make another member trusted, unless they are in purgatory. Trusted
        members can then refer others too.

        Examples:
            /handle refer target_user:@alice
            ;handle refer @alice
        """
        guild = ctx.guild
        trusted_role_name = constants.TLE_TRUSTED
        purgatory_role_name = constants.TLE_PURGATORY

        if target_user == ctx.author:
            raise HandleCogError("You can't refer yourself.")

        # Find the Purgatory role
        purgatory_role = discord_common.get_role(guild, purgatory_role_name)
        if purgatory_role is None:
            # This case might indicate a server setup issue, but we proceed as
            # if the user is not in purgatory
            self.logger.warning(
                f"Role '{purgatory_role_name}'"
                f' not found in guild {guild.name} ({guild.id}).'
            )
        elif purgatory_role in target_user.roles:
            await ctx.send(
                embed=discord_common.embed_alert(
                    f"{target_user.mention} is in purgatory, so they can't be made "
                    'trusted.'
                )
            )
            return

        # Find the Trusted role
        trusted_role = discord_common.get_role(guild, trusted_role_name)
        if trusted_role is None:
            raise HandleCogError(_NO_TRUSTED_ROLE)

        # Check if target user already has the role
        if trusted_role in target_user.roles:
            await ctx.send(
                embed=discord_common.embed_neutral(
                    f'{target_user.mention} is already trusted.'
                )
            )
            return

        # Grant the Trusted role
        try:
            await target_user.add_roles(
                trusted_role, reason=f'Referred by {ctx.author.name} ({ctx.author.id})'
            )
        except discord.Forbidden:
            raise HandleCogError(_TRUSTED_ROLE_REFUSED)
        except discord.HTTPException as e:
            self.logger.warning(
                f'Could not make {target_user.id} trusted in guild {guild.id}: {e}'
            )
            raise HandleCogError(
                'Something went wrong while giving the trusted role. Try again later.'
            )
        await ctx.send(
            f'{target_user.mention} is now trusted, referred by {ctx.author.mention}.'
        )

    @handle.command(brief='Make members who joined before 21 April 2025 trusted')
    async def grandfather(self, ctx: commands.Context) -> None:
        """Make every member who joined before 21 April 2025 trusted, unless
        they are in purgatory.

        Examples:
            ;handle grandfather
        """
        guild = ctx.guild
        trusted_role_name = constants.TLE_TRUSTED
        purgatory_role_name = constants.TLE_PURGATORY

        trusted_role = discord_common.get_role(guild, trusted_role_name)
        if trusted_role is None:
            raise HandleCogError(_NO_TRUSTED_ROLE)

        purgatory_role = discord_common.get_role(guild, purgatory_role_name)
        # If Purgatory role doesn't exist, we assume no one has it.
        if purgatory_role is None:
            self.logger.warning(
                f"Role '{purgatory_role_name}'"
                f' not found in guild {guild.name} ({guild.id}).'
                f' Proceeding without Purgatory check.'
            )

        # The date when this code was added.
        # April 20 was o3's first contest.
        cutoff_date = dt.datetime(2025, 4, 21, 0, 0, 0, tzinfo=dt.timezone.utc)

        added_count = 0
        skipped_purgatory = 0
        skipped_already_trusted = 0
        skipped_join_date = 0
        processed_count = 0
        http_failure_count = 0

        # Create a list to avoid issues if members leave/join during processing
        members_to_process = list(guild.members)

        status_message = await ctx.send(
            f'Checking {len(members_to_process)} members, to make those who joined '
            'before 21 April 2025 trusted…'
        )

        for i, member in enumerate(members_to_process):
            processed_count += 1
            if i % 100 == 0 and i > 0:
                await status_message.edit(
                    content=f'Checked {i} of {len(members_to_process)} members…'
                )

            if purgatory_role is not None and purgatory_role in member.roles:
                # User has purgatory role so is not eligible, skip
                skipped_purgatory += 1
                continue

            if member.joined_at is None:
                # Cannot determine join date, skip
                skipped_join_date += 1
                continue

            # Make member.joined_at timezone-aware
            # (assuming it's UTC, which discord.py uses)
            member_joined_at_aware = member.joined_at.replace(tzinfo=dt.timezone.utc)

            if member_joined_at_aware >= cutoff_date:
                # User joined too late to be eligible, skip
                skipped_join_date += 1
                continue

            if trusted_role in member.roles:
                # User already trusted, skip
                skipped_already_trusted += 1
                continue

            # Eligible for Trusted role, try to grant it
            try:
                await member.add_roles(
                    trusted_role,
                    reason='Grandfather clause: Joined before 2025-04-21 and not in Purgatory',  # noqa: E501
                )
                added_count += 1
                # Short delay to avoid hitting rate limits on large servers
                await asyncio.sleep(0.1)
            except discord.Forbidden:
                await ctx.send(
                    embed=discord_common.embed_alert(
                        f'{_TRUSTED_ROLE_REFUSED} I stopped there. Made trusted so '
                        f'far: {added_count}.'
                    )
                )
                return  # Stop processing if permissions are missing
            except discord.HTTPException as e:
                self.logger.warning(
                    f'Failed to assign {trusted_role_name} role to'
                    f' {member.display_name} ({member.id}): {e}'
                )
                http_failure_count += 1

        summary_message = (
            f'Done: I checked {processed_count} members.\n'
            f'- Made trusted: {added_count}\n'
            f'- Already trusted: {skipped_already_trusted}\n'
            f'- Joined on or after 21 April 2025, or unknown: {skipped_join_date}\n'
            f'- Could not be made trusted: {http_failure_count}\n'
        )
        if purgatory_role:
            summary_message += f'- In purgatory: {skipped_purgatory}\n'

        await status_message.edit(content=summary_message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Handles(bot))
