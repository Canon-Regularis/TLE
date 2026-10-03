"""Accounts: members link their Codeforces and AtCoder accounts, and compare ratings.

- ``/link codeforces <handle>`` and ``/link atcoder <handle>`` give the member
  a token to put on the account's profile, with a Verify button (see
  ``views``); ``/link verify <platform>`` does what the button does. How the
  token proves the account is the member's is in ``service``.
- Codeforces handles are linked in TLE's own table, the rank role given as by
  ``;handle set``, through ``tle.kcpc.bot.codeforces_links``. TLE's commands
  share them, so moderators unlink them, with ``/handle remove``. AtCoder
  accounts are linked in kcpc.db, and ``/unlink atcoder`` unlinks them.
- Links stay when members leave the server, for if they come back, but
  whoever proves an AtCoder account that a member who left had linked takes
  the link over (see ``_is_member`` for who has left). A Codeforces handle
  stays theirs, as TLE keeps it, so /link refuses it before giving a token,
  as it does a handle TLE couldn't give the rank role for. Admins unlink
  anyone's AtCoder account with ``/kcpc accounts unlink <handle>`` (attached
  under /kcpc, see ``tle.kcpc.bot.admin``), e.g. one a member linked that
  isn't theirs. Without the kcpc.admin extension there is no /kcpc, so only
  the member who linked an AtCoder account can unlink it.
- ``/profile [member]`` shows a member's accounts and ratings, refreshing
  ratings more than an hour old first, and ``/rank [platform]`` ranks the
  server's members by rating.
- The job accounts.refresh refreshes the ratings of the accounts that members
  of the bot's servers linked every 6 hours, and accounts.purge-challenges
  deletes expired tokens every hour.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot import codeforces_links
from tle.kcpc.bot.admin import (
    attach_admin_group,
    detach_admin_group,
    withhold_admin_group,
)
from tle.kcpc.bot.checks import kcpc_admin_only
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import KCPC_COLOR, info_embed, success_embed
from tle.kcpc.bot.pages import send_pages
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob
from tle.kcpc.core.timeutil import discord_timestamp
from tle.kcpc.features.accounts.refresh import Account, RatingRefresher, RefreshReport
from tle.kcpc.features.accounts.repo import (
    AccountRepo,
    AccountSnapshot,
    HandleTaken,
    LinkChallenge,
)
from tle.kcpc.features.accounts.service import (
    ATCODER,
    CHALLENGE_LIFETIME,
    CODEFORCES,
    AccountService,
    IsMember,
    Profile,
    ProfileAccount,
    Standing,
    VerifiedProfile,
    link_platform,
)
from tle.kcpc.features.accounts.views import (
    ACCOUNTS_COG,
    VerifyLinkButton,
    verify_view,
)
from tle.kcpc.platforms.atcoder.profile import AtCoderProfileClient

logger = logging.getLogger(__name__)

REFRESH_JOB = 'accounts.refresh'
PURGE_JOB = 'accounts.purge-challenges'
_REFRESH_INTERVAL = timedelta(hours=6)
_PURGE_INTERVAL = timedelta(hours=1)

_PER_PAGE = 10  # members on each page of /rank
# The longest handle either platform allows: Codeforces' 24 characters.
_MAX_HANDLE_LENGTH = 24
# Seconds /profile waits for fresh ratings before it shows the stored ones. The
# sites' own timeouts, with retries, run to minutes.
_PROFILE_REFRESH_WAIT = 10.0

_UNLINK_CODEFORCES = (
    "Your Codeforces handle is shared with TLE's commands, such as gitgud, duels "
    'and rank roles, so an Admin or Moderator unlinks it, with /handle remove.'
)
# Who can free an AtCoder handle that someone else here linked, when there is
# no /kcpc accounts unlink for an admin to use: kcpc.admin is switched off.
_FREEING_WITHOUT_ADMINS = (
    'If it is yours, ask the member who linked it to unlink it with /unlink atcoder.'
)
_RATINGS_AS_OF = 'Ratings as of'
_NOT_REFRESHED = "Couldn't refresh the ratings just now."

# The colour of each Codeforces rank, as TLE's embeds show it
# (tle.util.codeforces_api.RATED_RANKS). Copied, since features can't import
# TLE's Codeforces client; tests check the copy against the original.
CODEFORCES_COLORS = {
    'newbie': 0x808080,
    'pupil': 0x008000,
    'specialist': 0x03A89E,
    'expert': 0x0000FF,
    'candidate master': 0xAA00AA,
    'master': 0xFF8C00,
    'international master': 0xF57500,
    'grandmaster': 0xFF3030,
    'international grandmaster': 0xFF0000,
    'legendary grandmaster': 0xCC0000,
}
# The colour AtCoder shows a name in, by its rating.
ATCODER_COLORS = {
    'gray': 0x808080,
    'brown': 0x804000,
    'green': 0x008000,
    'cyan': 0x00C0C0,
    'blue': 0x0000FF,
    'yellow': 0xC0C000,
    'orange': 0xFF8000,
    'red': 0xFF0000,
}
_RANK_COLORS = {CODEFORCES: CODEFORCES_COLORS, ATCODER: ATCODER_COLORS}


class KcpcAccounts(KcpcCog, name=ACCOUNTS_COG):
    """/link, /unlink, /profile, /rank and /kcpc accounts, and the rating jobs."""

    # Set by cog_load, which runs before any command or job of the cog can.
    _service: AccountService
    _refresher: RatingRefresher

    async def cog_load(self) -> None:
        """Make the service, add the Verify button and admin commands, then the jobs.

        If a step fails, the steps before it are undone before the error
        propagates: discord.py doesn't call ``cog_unload`` when ``cog_load``
        raises.
        """
        services = self.services
        repo = AccountRepo(services.db)
        atcoder = AtCoderProfileClient(services.http)
        self._service = AccountService(repo, atcoder, services.clock)
        self._refresher = RatingRefresher(repo, atcoder, services.clock)
        self.bot.add_dynamic_items(VerifyLinkButton)
        added: list[str] = []
        try:
            if not attach_admin_group(self.bot, self.accounts_admin):
                withhold_admin_group(self, self.accounts_admin)
            for job in self._jobs():
                services.scheduler.add(job)
                added.append(job.name)
        except BaseException:
            for name in added:
                await services.scheduler.remove(name)
            # Detaching a group that isn't attached does nothing.
            detach_admin_group(self.bot, self.accounts_admin)
            self.bot.remove_dynamic_items(VerifyLinkButton)
            raise

    async def cog_unload(self) -> None:
        """Stop the jobs, take the admin commands away, and stop answering Verify."""
        for name in (REFRESH_JOB, PURGE_JOB):
            await self.services.scheduler.remove(name)
        detach_admin_group(self.bot, self.accounts_admin)
        self.bot.remove_dynamic_items(VerifyLinkButton)

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(name='link', brief='Link your Codeforces or AtCoder account')  # type: ignore[arg-type]
    @commands.guild_only()
    async def link(self, ctx: commands.Context[Any]) -> None:
        """Link your Codeforces or AtCoder account, to show it on /profile and /rank.

        You get a token to put on your profile for a few minutes, which proves
        the account is yours.
        """
        # Only ;link gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @link.command(name='codeforces', brief='Link your Codeforces account')  # type: ignore[arg-type]
    @app_commands.describe(handle='Your Codeforces handle')
    async def link_codeforces(
        self,
        ctx: commands.Context[Any],
        handle: commands.Range[str, 1, _MAX_HANDLE_LENGTH],
    ) -> None:
        """Link your Codeforces account, with a token in its Organization.

        TLE's commands, such as gitgud, duels and rank roles, use it too.
        """
        await self._start_link(ctx, CODEFORCES, handle)

    @link.command(name='atcoder', brief='Link your AtCoder account')  # type: ignore[arg-type]
    @app_commands.describe(handle='Your AtCoder username')
    async def link_atcoder(
        self,
        ctx: commands.Context[Any],
        handle: commands.Range[str, 1, _MAX_HANDLE_LENGTH],
    ) -> None:
        """Link your AtCoder account, with a token in its Affiliation."""
        await self._start_link(ctx, ATCODER, handle)

    @link.command(name='verify', brief='Verify the account you are linking')  # type: ignore[arg-type]
    @app_commands.describe(platform='Where the account is')
    async def link_verify(
        self, ctx: commands.Context[Any], platform: Literal['codeforces', 'atcoder']
    ) -> None:
        """Check the token is on your profile, and link the account if it is.

        The same as pressing Verify.
        """
        await ctx.defer(ephemeral=True)
        embed = await self.verify_link(_guild(ctx), _author(ctx), platform)
        await _reply(ctx, embed)

    async def verify_link(
        self, guild: discord.Guild, member: discord.Member, platform: str
    ) -> discord.Embed:
        """Link the account the member is linking, if its profile shows the token.

        What /link verify and the Verify button do; returns the reply to show.
        """
        service = self._service
        if platform == CODEFORCES:
            current = await codeforces_links.linked_handle(
                self.bot, guild.id, member.id
            )
            profile = await service.check(
                guild.id, member.id, platform, current_handle=current
            )
            try:
                await codeforces_links.link(self.bot, guild, member, profile.handle)
            except codeforces_links.RankRoleRefused:
                # Discord refused only the rank roles: the handle is linked, so
                # the link is completed before the member is told what to fix.
                await service.complete_codeforces(guild.id, member.id, profile)
                raise
            await service.complete_codeforces(guild.id, member.id, profile)
        else:
            profile = await service.check(guild.id, member.id, platform)
            with self._naming_who_frees_handles():
                await service.complete_atcoder(
                    guild.id, member.id, profile, is_member=_is_member(guild)
                )
        logger.info(
            'Member %d of guild %d linked the %s account %s',
            member.id,
            guild.id,
            platform,
            profile.handle,
        )
        return success_embed(_linked_text(profile))

    @commands.hybrid_command(brief='Unlink your AtCoder account')  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(platform='Where the account is')
    async def unlink(
        self, ctx: commands.Context[Any], platform: Literal['codeforces', 'atcoder']
    ) -> None:
        """Unlink your AtCoder account.

        An Admin or Moderator unlinks Codeforces handles, with /handle remove,
        since TLE's commands use them too.
        """
        guild, member = _guild(ctx), _author(ctx)
        if platform == CODEFORCES:
            raise KcpcUserError(_UNLINK_CODEFORCES)
        removed = await self._service.unlink_atcoder(guild.id, member.id)
        handle = _escape(removed.handle)
        await _reply(ctx, success_embed(f'Unlinked your AtCoder account {handle}.'))

    @commands.hybrid_group(name='accounts', brief="Members' linked accounts")  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def accounts_admin(self, ctx: commands.Context[Any]) -> None:
        """Unlink a member's AtCoder account, e.g. one that isn't theirs."""
        # Only ;kcpc accounts gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @accounts_admin.command(name='unlink', brief="Unlink anyone's AtCoder account")  # type: ignore[arg-type]
    @app_commands.describe(handle='The AtCoder username to unlink')
    @kcpc_admin_only()
    async def admin_unlink(
        self,
        ctx: commands.Context[Any],
        handle: commands.Range[str, 1, _MAX_HANDLE_LENGTH],
    ) -> None:
        """Unlink an AtCoder account from whoever linked it in this server.

        It frees the handle of a member who linked an account that isn't
        theirs, so that its owner can link it, or removes the link of a member
        who left (which doesn't stop anyone who proves the account is theirs
        from linking it). Codeforces handles are removed with /handle remove.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        removed = await self._service.remove_atcoder_link(guild.id, handle)
        logger.info(
            'Admin %d of guild %d unlinked the AtCoder account %s from member %d',
            ctx.author.id,
            guild.id,
            removed.handle,
            removed.user_id,
        )
        # HandleTaken names nobody, but admins may see who had the handle. A
        # mention in an embed names the member without notifying them.
        await _reply(
            ctx,
            success_embed(
                f'Unlinked the AtCoder account {_escape(removed.handle)} '
                f'from <@{removed.user_id}>.'
            ),
        )

    @commands.hybrid_command(brief="A member's linked accounts and ratings")  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(member='Whose accounts to show; yours if left out')
    async def profile(
        self, ctx: commands.Context[Any], member: discord.Member | None = None
    ) -> None:
        """Show a member's linked Codeforces and AtCoder accounts, with ratings.

        Ratings more than an hour old are refreshed first.
        """
        await ctx.defer()
        guild = _guild(ctx)
        target = member or _author(ctx)
        codeforces = await codeforces_links.linked_handle(self.bot, guild.id, target.id)
        accounts = await self._service.profile_accounts(guild.id, target.id, codeforces)
        if not accounts:
            raise KcpcUserError(_nothing_linked(target, ctx.author.id))
        now = self.services.clock.now()
        notes = await self._refresh_stale(accounts, now)
        if notes is not None:
            accounts = await self._service.profile_accounts(
                guild.id, target.id, codeforces
            )
        # An account refreshed before the wait ran out is fresh now, though the
        # report counts it as failed.
        embeds = [
            _account_embed(target, account, notes if account.stale(now) else None)
            for account in accounts
        ]
        await ctx.send(embeds=embeds)

    @commands.hybrid_command(brief="This server's members by rating")  # type: ignore[arg-type]
    @commands.guild_only()
    @app_commands.describe(platform='Codeforces if left out')
    async def rank(
        self,
        ctx: commands.Context[Any],
        platform: Literal['codeforces', 'atcoder'] = 'codeforces',
    ) -> None:
        """Rank this server's members by their current Codeforces or AtCoder rating."""
        guild = _guild(ctx)
        members = await self._linked_members(guild, platform)
        standings = await self._service.leaderboard(platform, members)
        await send_pages(ctx, _leaderboard_pages(platform, standings, ctx.author.id))

    async def _start_link(
        self, ctx: commands.Context[Any], platform: str, handle: str
    ) -> None:
        await ctx.defer(ephemeral=True)
        guild, member = _guild(ctx), _author(ctx)
        current: str | None = None
        owner: int | None = None
        vet: Callable[[Profile], None] | None = None
        if platform == CODEFORCES:
            # Refused now, not at Verify once the member has edited their
            # profile: TLE won't link a handle that someone in its table has (a
            # member who left included), nor an account whose rank the server
            # has no role for.
            current = await codeforces_links.linked_handle(
                self.bot, guild.id, member.id
            )
            owner = await codeforces_links.handle_holder(
                self.bot, guild.id, handle.strip()
            )
            vet = _rank_role_check(guild)
        with self._naming_who_frees_handles():
            challenge = await self._service.start_link(
                guild.id,
                member.id,
                platform,
                handle,
                current_handle=current,
                owner_id=owner,
                is_member=_is_member(guild),
                vet=vet,
            )
        await ctx.send(
            embed=_instructions(challenge),
            view=verify_view(platform, member.id),
            ephemeral=True,
        )

    @contextmanager
    def _naming_who_frees_handles(self) -> Iterator[None]:
        """Have ``HandleTaken`` name only commands the server has.

        It sends whoever finds an AtCoder account taken to an admin, for
        /kcpc accounts unlink. Without the kcpc.admin extension that command
        isn't attached, and only the member who linked the account can unlink
        it.
        """
        try:
            yield
        except HandleTaken as taken:
            if taken.platform != ATCODER or self.accounts_admin.parent is not None:
                raise
            raise HandleTaken(
                taken.platform, taken.handle, freeing=_FREEING_WITHOUT_ADMINS
            ) from None

    async def _linked_members(
        self, guild: discord.Guild, platform: str
    ) -> list[tuple[int, str]]:
        """``(user_id, handle)`` of each member of the guild linked on ``platform``."""
        if platform == CODEFORCES:
            linked = await codeforces_links.guild_handles(self.bot, guild.id)
        else:
            linked = await self._service.linked_handles(guild.id, platform)
        # TLE marks the handles of members who leave inactive; KCPC keeps their
        # links, for if they come back.
        is_member = _is_member(guild)
        return [(user_id, handle) for user_id, handle in linked if is_member(user_id)]

    async def _refresh_stale(
        self, accounts: Sequence[ProfileAccount], now: datetime
    ) -> dict[Account, str] | None:
        """Refresh the accounts whose ratings are stale at ``now``; None if none were.

        Returns a note for each account that couldn't be refreshed, within
        ``_PROFILE_REFRESH_WAIT`` seconds.
        """
        stale = [
            (account.platform, account.handle)
            for account in accounts
            if account.stale(now)
        ]
        if not stale:
            return None
        try:
            report = await asyncio.wait_for(
                self._refresher.refresh_member(stale), _PROFILE_REFRESH_WAIT
            )
        except asyncio.TimeoutError:
            # The snapshots saved before the wait ran out are kept; the next
            # /profile or the refresh job fetches the rest.
            logger.info(
                'Gave up refreshing %d accounts for /profile after %g seconds',
                len(stale),
                _PROFILE_REFRESH_WAIT,
            )
            report = RefreshReport(failed=tuple(stale))
        notes = dict.fromkeys(report.failed, _NOT_REFRESHED)
        for platform, handle in report.missing:
            # Shown in a footer, which Discord doesn't format: no escaping.
            name = link_platform(platform).name
            notes[platform, handle] = f'{name} has no user called {handle} now.'
        return notes

    def _jobs(self) -> tuple[ScheduledJob, ...]:
        # Neither job runs as the bot starts: refreshing every account can
        # wait for its slot, and an expired token is never used anyway.
        return (
            ScheduledJob(
                REFRESH_JOB,
                Every(_REFRESH_INTERVAL),
                self._refresh_all,
                persistent=False,
            ),
            ScheduledJob(
                PURGE_JOB,
                Every(_PURGE_INTERVAL),
                self._purge_challenges,
                persistent=False,
            ),
        )

    async def _refresh_all(self, slot: datetime) -> None:
        """Refresh every account linked by a member of a guild the bot is in."""
        # Each run refreshes the accounts linked now, whichever slot it is for.
        # Only members' accounts, as /rank lists them: nothing shows the ratings
        # of members who left, and AtCoder profiles are fetched one at a time.
        codeforces_handles: list[str] = []
        atcoder_handles: list[str] = []
        for guild in self.bot.guilds:
            linked = await self._linked_members(guild, CODEFORCES)
            codeforces_handles += [handle for _, handle in linked]
            linked = await self._linked_members(guild, ATCODER)
            atcoder_handles += [handle for _, handle in linked]
        await self._refresher.refresh_all(codeforces_handles, atcoder_handles)

    async def _purge_challenges(self, slot: datetime) -> None:
        purged = await self._service.purge_expired_challenges()
        logger.debug('Deleted %d expired link challenges', purged)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcAccounts(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


def _author(ctx: commands.Context[Any]) -> discord.Member:
    if not isinstance(ctx.author, discord.Member):
        raise commands.NoPrivateMessage()
    return ctx.author


def _is_member(guild: discord.Guild) -> IsMember:
    """Whether a user is still a member of ``guild``, as far as the bot knows.

    The bot hears of members joining and leaving. But after each new connection
    to Discord, it starts with only a few of the guild's members, and asks
    Discord for the rest, while commands already run. Until it has them all
    (``Guild.chunked``), everyone counts as a member: no AtCoder link is taken
    over from a member the bot hasn't been sent yet, though /rank may list
    members who left.
    """
    return lambda user_id: not guild.chunked or guild.get_member(user_id) is not None


def _rank_role_check(guild: discord.Guild) -> Callable[[Profile], None]:
    """Refuses a Codeforces account whose rank ``guild`` has no role for."""

    def check(profile: Profile) -> None:
        codeforces_links.check_rank_role(guild, profile.rating)

    return check


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the member sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def _account_link(platform: str, handle: str) -> str:
    """``handle`` linked to its profile page on ``platform``."""
    return f'[{_escape(handle)}]({link_platform(platform).url_for(handle)})'


def _instructions(challenge: LinkChallenge) -> discord.Embed:
    """How to put the token on the profile, step by step."""
    platform = link_platform(challenge.platform)
    minutes = CHALLENGE_LIFETIME // timedelta(minutes=1)
    expiry = discord_timestamp(challenge.expires_at, 't')
    steps = '\n'.join(
        [
            f'1. Open <{platform.settings_url}> and add this token anywhere in '
            f'your **{platform.proof_field}**: `{challenge.token}`',
            '2. Save your settings.',
            f'3. Press **Verify** below, or use `/link verify {challenge.platform}`.',
            '',
            f'The token expires in {minutes} minutes, at {expiry}. Once your '
            'account is linked, remove it again.',
        ]
    )
    return info_embed(
        f'Link your {platform.name} account {_escape(challenge.handle)}', steps
    )


def _linked_text(profile: VerifiedProfile) -> str:
    """The reply once an account is linked: the account, and what comes next."""
    platform = link_platform(profile.platform)
    rated = 'unrated' if profile.rating is None else f'rated {profile.rating}'
    lines = [
        f'Linked your {platform.name} account '
        f'{_account_link(profile.platform, profile.handle)}, {rated}.',
        f'Please remove the token from your {platform.proof_field} now.',
    ]
    if profile.platform == CODEFORCES:
        lines.append(
            "TLE's commands, such as gitgud, duels and rank roles, use it too."
        )
    return '\n'.join(lines)


def _nothing_linked(member: discord.Member, viewer_id: int) -> str:
    if member.id == viewer_id:
        return (
            "You haven't linked any accounts yet. Link one with "
            '/link codeforces <handle> or /link atcoder <handle>.'
        )
    return f"{member.mention} hasn't linked any accounts yet."


def _rank_name(rank: str | None) -> str | None:
    """A rank or colour as members read it: 'expert' as 'Expert'."""
    return None if rank is None else rank.title()


def _color(platform: str, snapshot: AccountSnapshot | None) -> int:
    """The colour of the account's rank, else KCPC's."""
    rank = None if snapshot is None or snapshot.rating is None else snapshot.rank
    return _RANK_COLORS[platform].get(rank or '', KCPC_COLOR)


def _account_embed(
    member: discord.Member,
    account: ProfileAccount,
    notes: dict[Account, str] | None,
) -> discord.Embed:
    """One of ``member``'s accounts, with its ratings, in its rank's colour."""
    platform = link_platform(account.platform)
    snapshot = account.snapshot
    embed = discord.Embed(
        title=f'{platform.name}: {_escape(account.handle)}',
        url=account.url,
        color=_color(account.platform, snapshot),
    )
    embed.set_author(name=member.display_name)
    note = (notes or {}).get((account.platform, account.handle))
    if snapshot is None:
        embed.description = 'No ratings yet.'
        if note is not None:
            embed.set_footer(text=note)
        return embed
    if snapshot.rating is None:
        embed.description = 'Unrated.'
    else:
        embed.add_field(name='Rating', value=str(snapshot.rating))
        if snapshot.max_rating is not None:
            embed.add_field(name='Peak', value=str(snapshot.max_rating))
        rank = _rank_name(snapshot.rank)
        if rank is not None:
            label = 'Rank' if account.platform == CODEFORCES else 'Colour'
            embed.add_field(name=label, value=rank)
    if snapshot.rated_matches:
        embed.add_field(name='Rated matches', value=str(snapshot.rated_matches))
    # A footer can't show Discord's timestamp markup, so the embed's own
    # timestamp, shown just after the footer, says when they were fetched.
    footer = _RATINGS_AS_OF if note is None else f'{note} {_RATINGS_AS_OF}'
    embed.set_footer(text=footer)
    embed.timestamp = snapshot.fetched_at
    return embed


def _rating_summary(snapshot: AccountSnapshot | None) -> str:
    if snapshot is None:
        return 'no ratings yet'
    if snapshot.rating is None:
        return 'unrated'
    rank = _rank_name(snapshot.rank)
    return f'**{snapshot.rating}**' if rank is None else f'**{snapshot.rating}** {rank}'


def _standing_line(platform: str, standing: Standing) -> str:
    # A mention in an embed names the member without notifying them.
    return (
        f'**{standing.place}.** <@{standing.user_id}> · '
        f'{_account_link(platform, standing.handle)} · '
        f'{_rating_summary(standing.snapshot)}'
    )


def _leaderboard_pages(
    platform: str, standings: Sequence[Standing], viewer_id: int
) -> list[discord.Embed]:
    """The leaderboard, 10 members a page, each page saying where the viewer is."""
    name = link_platform(platform).name
    title = f'{name} leaderboard'
    if not standings:
        return [
            info_embed(
                title,
                f'Nobody here has linked their {name} account yet. Link yours with '
                f'/link {platform} <handle>.',
            )
        ]
    mine = next((s for s in standings if s.user_id == viewer_id), None)
    position = (
        f"You aren't on it: link your {name} account with /link {platform}."
        if mine is None
        else f'Your position: {mine.place} of {len(standings)}'
    )
    chunks = [
        standings[start : start + _PER_PAGE]
        for start in range(0, len(standings), _PER_PAGE)
    ]
    pages = []
    for number, chunk in enumerate(chunks, start=1):
        page = info_embed(title, '\n'.join(_standing_line(platform, s) for s in chunk))
        footer = (
            position
            if len(chunks) == 1
            else f'{position} · Page {number} of {len(chunks)}'
        )
        page.set_footer(text=footer)
        pages.append(page)
    return pages
