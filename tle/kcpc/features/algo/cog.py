"""The algorithm of the month: /algo, /kcpc algo, and the job that posts it.

- Members: ``/algo`` (also plain ``;algo``, or ``;algo current``) shows this
  month's topic in the server, and ``/algo history`` the earlier ones.
- Admins: ``/kcpc algo`` to reroll this month's topic, post it now and preview
  the next post, attached under /kcpc (see ``tle.kcpc.bot.admin``).
- The job algo.post runs on the 1st of each month at noon, club time, posting
  a topic in every server with the feature on that the bot is in (see
  ``service``). A fresh install posts from the next 1st on, and a 1st the bot
  missed is posted if it is back within 24 hours.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import discord
from discord.ext import commands

from tle.kcpc.bot.admin import (
    attach_admin_group,
    detach_admin_group,
    withhold_admin_group,
)
from tle.kcpc.bot.checks import kcpc_admin_only
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import alert_embed, info_embed, success_embed, to_embed
from tle.kcpc.bot.pages import send_pages
from tle.kcpc.core.ledger import DeliveryStatus
from tle.kcpc.core.messages import EmbedField, OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome
from tle.kcpc.core.schedule import Monthly
from tle.kcpc.core.scheduler import ScheduledJob
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.core.timeutil import discord_timestamp
from tle.kcpc.features.algo import markdown
from tle.kcpc.features.algo.repo import AlgoPick, AlgoRepo
from tle.kcpc.features.algo.service import (
    ALGO,
    ALGO_JOB,
    FOOTER,
    POST_DAY,
    POST_TIME,
    AlgoService,
    PostResult,
    month_name,
    post_key,
    read_links,
)

logger = logging.getLogger(__name__)

# How late the job may still post a month's topic that it missed, while the
# bot was down, say: the topic is for the whole month, so the next day is
# still worth it.
_CATCH_UP_GRACE = timedelta(hours=24)
_PER_PAGE = 12  # months on each page of /algo history: a year
# DiscordPublisher's reason while Discord hasn't sent a guild's channels, as
# just after the bot reconnects: the post goes out if tried again then.
_GUILD_UNAVAILABLE = 'guild-unavailable'
# The outcomes of a post that went out, or was already out.
_WENT_OUT = frozenset({PublishOutcome.SENT, PublishOutcome.ALREADY_HANDLED})

_TITLE = 'Algorithm of the month'
_HISTORY_TITLE = 'Algorithms of the month'
_PREVIEW_TITLE = 'Algorithm of the month preview'
_NOTHING_POSTED = 'No algorithm of the month has been posted here yet.'
_NOTHING_YET = 'Nothing has been posted yet.'
_NOT_SET_UP = (
    "The algorithm of the month isn't set up here. Turn it on with "
    '`/kcpc enable algo` and set its channel with `/kcpc channel algo #channel`, '
    'then try again.'
)
_REFUSED_EARLIER = (
    "Discord refused to post {post}, earlier: {reason}. It can't be posted "
    'again, but `/kcpc algo reroll` posts another topic.'
)
_TRY_AGAIN = (
    "Discord hasn't sent this server's channels yet. Please try again in a minute"
)
_CHECK_CHANNEL = (
    '{reason}. Check the channel and my permissions there, e.g. with '
    '`/kcpc channel algo #channel`'
)
# A reroll stores its topic before posting it, so another reroll would replace
# it again.
_STILL_TO_POST = (
    " It is {month}'s topic now: post it with `/kcpc algo post-now` once that is fixed."
)
_LEFT = '{left} of {total} topics; the next pick is one of them.'
# What became of the post of this month's topic, as /kcpc algo preview tells
# it: by the status of its delivery, or _NOT_POSTED_YET without one.
_NOT_POSTED_YET = 'Not posted yet.'
_POST_STATES = {
    DeliveryStatus.SENT: 'Posted.',
    DeliveryStatus.CLAIMED: "Posted, but Discord hasn't confirmed it yet.",
    DeliveryStatus.SKIPPED: 'Refused by Discord: {reason}.',
}

# How each outcome of a post is told. {post} names the post with its month, as
# in "October's topic, **X**", and {subject} names it to begin a sentence:
# "October's topic, **X**,". The month is the topic's, which before noon on
# the 1st is the month before.
_OUTCOMES = {
    PublishOutcome.SENT: 'Posted {post}.',
    PublishOutcome.ALREADY_HANDLED: '{subject} is already posted.',
    PublishOutcome.PENDING: (
        "Posted {post}, but Discord didn't confirm it; I'll check within a few minutes."
    ),
    PublishOutcome.SKIPPED: 'Discord refused to post {post}: {reason}.',
    PublishOutcome.UNDELIVERABLE: "Couldn't post {post}: {reason}.",
}


# A subcommand of the cog, as discord.py types it.
_Subcommand = commands.HybridCommand[Any, ..., Any]


class KcpcAlgo(KcpcCog):
    """/algo for members and /kcpc algo for admins, and the job.

    Its subcommands are declared on their own and put in their groups by
    ``cog_load``. discord.py links a cog's subcommands to their groups by
    qualified name as it makes the cog, and /algo and /kcpc algo share one
    until the latter is attached under /kcpc: declared in their groups, the
    subcommands of one would end up in the other.
    """

    # Set by cog_load, which runs before any command or job of the cog can.
    _schedule: Monthly
    _service: AlgoService

    async def cog_load(self) -> None:
        """Make the service, add the admin commands, then the job.

        If a step fails, the steps before it are undone before the error
        propagates: discord.py doesn't call ``cog_unload`` when ``cog_load``
        raises.
        """
        services = self.services
        self._schedule = Monthly(POST_DAY, POST_TIME, services.settings.tz)
        self._service = AlgoService(
            AlgoRepo(services.db),
            services.guild_settings,
            services.ledger,
            services.publisher,
            services.clock,
            self._schedule,
        )
        self._nest_subcommands()
        try:
            if not attach_admin_group(self.bot, self.algo_admin):
                withhold_admin_group(self, self.algo_admin)
            # Persistent, so that a fresh install posts nothing at once, and a
            # 1st missed while the bot was down is posted within the grace.
            services.scheduler.add(
                ScheduledJob(
                    ALGO_JOB,
                    self._schedule,
                    self._post_topics,
                    catch_up_grace=_CATCH_UP_GRACE,
                )
            )
        except BaseException:
            # Detaching a group that isn't attached does nothing.
            detach_admin_group(self.bot, self.algo_admin)
            raise

    async def cog_unload(self) -> None:
        """Stop the job and take the admin commands away."""
        await self.services.scheduler.remove(ALGO_JOB)
        detach_admin_group(self.bot, self.algo_admin)

    def _nest_subcommands(self) -> None:
        """Put each subcommand in its group (see the class docstring)."""
        member_commands: tuple[_Subcommand, ...] = (self.current, self.history)
        for command in member_commands:
            self.algo.add_command(command)
        admin_commands: tuple[_Subcommand, ...] = (
            self.reroll,
            self.post_now,
            self.preview,
        )
        for command in admin_commands:
            self.algo_admin.add_command(command)

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(fallback='current', brief="This month's algorithm")  # type: ignore[arg-type]
    @commands.guild_only()
    async def algo(self, ctx: commands.Context[Any]) -> None:
        """Show this server's algorithm of the month, and where to read about it."""
        await self._show_current(ctx)

    # The slash command is the group's fallback, which prefix commands lack.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name='current', with_app_command=False, brief="This month's algorithm"
    )
    async def current(self, ctx: commands.Context[Any]) -> None:
        """Show this server's algorithm of the month, and where to read about it."""
        await self._show_current(ctx)

    @commands.hybrid_command(name='history', brief='Earlier algorithms of the month')  # type: ignore[arg-type]
    async def history(self, ctx: commands.Context[Any]) -> None:
        """List this server's algorithms of the month, newest first."""
        guild = _guild(ctx)
        picks = await self._service.history(guild.id)
        await send_pages(ctx, self._history_pages(picks))

    @commands.hybrid_group(  # type: ignore[arg-type]
        name='algo', brief='The algorithm of the month: reroll, post, preview'
    )
    @kcpc_admin_only()
    async def algo_admin(self, ctx: commands.Context[Any]) -> None:
        """Reroll this month's topic, post it now, or preview the next post."""
        # Only ;kcpc algo gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @commands.hybrid_command(name='reroll', brief='Pick another topic for this month')  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def reroll(self, ctx: commands.Context[Any]) -> None:
        """Replace this month's topic with another, and post it.

        The topic replaced counts as one the server hasn't had, so it can come
        up again. Without a topic this month yet, this posts one, as post-now
        does.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        result = await self._service.reroll(guild.id)
        logger.info(
            'Admin %d of guild %d rerolled the algorithm of the month',
            ctx.author.id,
            guild.id,
        )
        await _reply(ctx, await self._post_reply(result))

    @commands.hybrid_command(name='post-now', brief="Post this month's topic now")  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def post_now(self, ctx: commands.Context[Any]) -> None:
        """Post this month's topic now, if it hasn't gone out.

        For a server that has just turned the feature on: the job posts on the
        next 1st, and this posts the month's topic before then. Running it
        again posts nothing twice.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        slot = self._schedule.prev_at_or_before(self.services.clock.now())
        result = await self._service.run_guild(guild.id, slot)
        logger.info(
            'Admin %d of guild %d ran the algorithm of the month of %s now',
            ctx.author.id,
            guild.id,
            slot,
        )
        await _reply(ctx, await self._post_reply(result))

    @commands.hybrid_command(name='preview', brief='When the next topic goes out')  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def preview(self, ctx: commands.Context[Any]) -> None:
        """Show when and where the next topic goes, this month's topic and
        whether it went out, and how many topics are left before the list
        starts over.
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        settings = await self.services.guild_settings.get(guild.id, ALGO)
        slot = self._schedule.next_after(self.services.clock.now())
        pick = await self._service.this_months_pick(guild.id)
        upcoming = await self._service.upcoming(guild.id)
        left = _LEFT.format(left=len(upcoming), total=len(self._service.topics))
        fields = (
            EmbedField('Next post', _next_post(slot, settings)),
            EmbedField('This month', await self._this_month(pick)),
            EmbedField('Left in this cycle', left),
        )
        message = OutgoingMessage(title=_PREVIEW_TITLE, fields=fields)
        await _reply(ctx, to_embed(message))

    async def _post_topics(self, slot: datetime) -> None:
        """Post every server's topic for ``slot``; see ``AlgoService.run_slot``."""
        await self._service.run_slot(slot, in_guild=self._in_guild)

    def _in_guild(self, guild_id: int) -> bool:
        """Whether the bot is in the server, even if Discord hasn't sent it
        yet, as just after the bot reconnects.
        """
        return self.bot.get_guild(guild_id) is not None

    async def _post_reply(self, result: PostResult) -> discord.Embed:
        """What a post-now or a reroll did: green if its post is out."""
        pick = result.pick
        if pick is None or result.outcome is PublishOutcome.NOT_CONFIGURED:
            return alert_embed(_NOT_SET_UP)
        month = month_name(pick.month)
        name = _bold(self._service.name(pick.slug))
        if result.replaced is None:
            post = f"{month}'s topic, {name}"
            subject = f"{month}'s topic, {name},"
        else:
            replaced = _bold(self._service.name(result.replaced.slug))
            post = f"{month}'s new topic, {name}, in place of {replaced}"
            subject = f"{month}'s new topic, {name},"
        refused = await self._refused_earlier(result)
        if refused is not None:  # handled already, but never out
            return alert_embed(
                _REFUSED_EARLIER.format(post=post, reason=_code(refused))
            )
        text = _describe_post(result, post, subject)
        if (
            result.replaced is not None
            and result.outcome is PublishOutcome.UNDELIVERABLE
        ):
            text += _STILL_TO_POST.format(month=month)
        if result.outcome in _WENT_OUT:
            return success_embed(text)
        return alert_embed(text)

    async def _refused_earlier(self, result: PostResult) -> str | None:
        """Why Discord refused the post of this month's topic, if it did so
        before this command: its delivery is spent, so it can't go out again.
        """
        if result.pick is None or result.outcome is not PublishOutcome.ALREADY_HANDLED:
            return None
        record = await self.services.ledger.get(post_key(result.pick))
        if record is None or record.status is not DeliveryStatus.SKIPPED:
            return None
        return record.reason or 'no reason given'

    async def _show_current(self, ctx: commands.Context[Any]) -> None:
        guild = _guild(ctx)
        pick = await self._service.current(guild.id)
        if pick is None:
            await ctx.send(embed=info_embed(_TITLE, _NOTHING_POSTED))
            return
        posted = f'**Posted:** {_when(await self._posted_at(pick))}'
        found = self._service.topic(pick.slug)
        if found is None:  # taken out of the catalog since
            message = OutgoingMessage(
                title=f'{_TITLE}: {pick.slug}', description=posted, footer=FOOTER
            )
        else:
            lines = [
                markdown.escape(found.summary),
                f'**Level:** {found.level.value}',
                f'**Read:** {read_links(found)}',
                posted,
            ]
            message = OutgoingMessage(
                title=f'{_TITLE}: {found.name}',
                description='\n'.join(lines),
                url=found.gfg_url,
                footer=FOOTER,
            )
        await ctx.send(embed=to_embed(message))

    def _history_pages(self, picks: Sequence[AlgoPick]) -> list[discord.Embed]:
        """/algo history: twelve months a page, newest first."""
        if not picks:
            return [info_embed(_HISTORY_TITLE, _NOTHING_POSTED)]
        chunks = [
            picks[start : start + _PER_PAGE]
            for start in range(0, len(picks), _PER_PAGE)
        ]
        pages = []
        for number, chunk in enumerate(chunks, start=1):
            lines = (self._history_line(pick) for pick in chunk)
            page = info_embed(_HISTORY_TITLE, '\n'.join(lines))
            page.set_footer(text=f'Page {number} of {len(chunks)}')
            pages.append(page)
        return pages

    def _history_line(self, pick: AlgoPick) -> str:
        """A month of /algo history: the month, its topic and the topic's level."""
        found = self._service.topic(pick.slug)
        if found is None:  # taken out of the catalog since
            return f'`{pick.month}` {markdown.escape(pick.slug)}'
        topic = markdown.link(found.name, found.gfg_url)
        return f'`{pick.month}` {topic} · {found.level.value}'

    async def _this_month(self, pick: AlgoPick | None) -> str:
        """This month's topic and what became of its post, as /kcpc algo
        preview shows them.
        """
        if pick is None:
            return _NOTHING_YET
        found = self._service.topic(pick.slug)
        if found is None:  # taken out of the catalog since
            topic = markdown.escape(pick.slug)
        else:
            topic = f'{markdown.link(found.name, found.gfg_url)} ({found.level.value})'
        record = await self.services.ledger.get(post_key(pick))
        if record is None:
            return f'{topic}\n{_NOT_POSTED_YET}'
        reason = _code(record.reason or 'no reason given')
        return f'{topic}\n{_POST_STATES[record.status].format(reason=reason)}'

    async def _posted_at(self, pick: AlgoPick) -> datetime:
        """When the post of ``pick`` went out, as the ledger has it.

        A post-now or a reroll posts after the slot, and a retry or a catch-up
        later still.
        """
        record = await self.services.ledger.get(post_key(pick))
        if record is None:  # never, for a pick that was posted
            return pick.slot
        return record.sent_at or record.claimed_at


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcAlgo(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _describe_post(result: PostResult, post: str, subject: str) -> str:
    """What became of a post, which ``post`` and ``subject`` name (see
    ``_OUTCOMES``).
    """
    template = _OUTCOMES.get(result.outcome)
    if template is None:  # NOT_CONFIGURED: turned off during the run
        return _NOT_SET_UP
    reason = result.reason or 'no reason given'
    if result.outcome is PublishOutcome.UNDELIVERABLE:
        if reason == _GUILD_UNAVAILABLE:
            reason = _TRY_AGAIN
        else:
            reason = _CHECK_CHANNEL.format(reason=_code(reason))
    elif result.outcome is PublishOutcome.SKIPPED:
        reason = _code(reason)
    return template.format(post=post, subject=subject, reason=reason)


def _next_post(slot: datetime, settings: FeatureSettings) -> str:
    """When the next topic goes out, and where, or what stops it."""
    fixes = []
    if not settings.enabled:
        fixes.append('turn it on with `/kcpc enable algo`')
    if settings.channel_id is None:
        fixes.append('set its channel with `/kcpc channel algo #channel`')
    if not fixes:
        return f'{_when(slot)} in <#{settings.channel_id}>'
    return (
        f'{_when(slot)}, once you {" and ".join(fixes)}. Until then nothing is posted.'
    )


def _when(moment: datetime) -> str:
    return f'{discord_timestamp(moment, "F")} ({discord_timestamp(moment, "R")})'


def _bold(text: str) -> str:
    return f'**{markdown.escape(text)}**'


def _code(text: str) -> str:
    """``text`` as inline code; backticks would end it early, so they go."""
    return '`' + text.replace('`', "'") + '`'
