"""Workshops: reminders of the club's Luma workshops, and commands about them.

- Members: ``/event next`` (also plain ``;event``) and ``/event this-week``.
  They read the database only, which syncing keeps up to date.
- Admins: ``/kcpc workshops calendar`` and ``/kcpc workshops sync``, attached
  under /kcpc (see ``tle.kcpc.bot.admin``).
- The ``workshops.sync`` job syncs every 10 minutes each calendar that a
  server with workshops on follows. The reminder engine's own job posts the
  reminders, which ``reminders`` describes, for each calendar once it has
  been synced since the bot started.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from tle.kcpc.bot.admin import (
    attach_admin_group,
    detach_admin_group,
    withhold_admin_group,
)
from tle.kcpc.bot.checks import kcpc_admin_only
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.bot.embeds import success_embed, to_embed
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.messages import EmbedField, OutgoingMessage
from tle.kcpc.core.schedule import Every
from tle.kcpc.core.scheduler import ScheduledJob
from tle.kcpc.core.timeutil import discord_timestamp, local_week_bounds
from tle.kcpc.features.workshops.reminders import (
    WorkshopOccurrence,
    WorkshopReminders,
    workshop_details,
)
from tle.kcpc.features.workshops.repo import EventRepo, StoredEvent
from tle.kcpc.features.workshops.settings import WORKSHOPS
from tle.kcpc.features.workshops.sync import EventSync, SyncReport
from tle.kcpc.platforms.luma import (
    CalendarNotFound,
    LumaCalendarClient,
    calendar_id_from,
)

logger = logging.getLogger(__name__)

SYNC_JOB = 'workshops.sync'
_SYNC_INTERVAL = timedelta(minutes=10)

NO_CALENDAR = (
    'No Luma calendar is set up yet. An admin can set one with '
    '/kcpc workshops calendar.'
)
NEVER_SYNCED = "Workshops haven't been synced yet."
_NOTHING_UPCOMING = 'There are no workshops coming up. Check back soon!'
_NONE_THIS_WEEK = 'There are no workshops this week. `/event next` shows the next one.'
# A footer can't show Discord's timestamp markup, so the embed's own timestamp,
# shown just after the footer, says when.
_LAST_SYNCED = 'Last synced'


class KcpcWorkshops(KcpcCog):
    """Workshop reminders, /event for members and /kcpc workshops for admins."""

    # Set by cog_load, which runs before any command or job of the cog can.
    _repo: EventRepo
    _client: LumaCalendarClient
    _sync: EventSync
    _source: WorkshopReminders

    async def cog_load(self) -> None:
        """Start reminding, add the admin commands, then start syncing.

        If a step fails, the steps before it are undone before the error
        propagates: discord.py doesn't call ``cog_unload`` when ``cog_load``
        raises.
        """
        services = self.services
        self._repo = EventRepo(services.db)
        self._client = LumaCalendarClient(services.http, tz=services.settings.tz)
        # One EventSync for the job and the commands, so that syncs of the same
        # calendar take turns.
        self._sync = EventSync(services.db, self._repo, self._client, services.clock)
        self._source = WorkshopReminders(self._repo, services.settings.luma_calendar_id)
        services.reminders.register(self._source)
        try:
            if not attach_admin_group(self.bot, self.workshops_admin):
                withhold_admin_group(self, self.workshops_admin)
            services.scheduler.add(
                ScheduledJob(
                    SYNC_JOB,
                    Every(_SYNC_INTERVAL),
                    self._sync_all,
                    persistent=False,
                    run_on_start=True,
                )
            )
        except BaseException:
            # Detaching a group that isn't attached does nothing.
            detach_admin_group(self.bot, self.workshops_admin)
            services.reminders.unregister(WORKSHOPS)
            raise

    async def cog_unload(self) -> None:
        """Stop syncing and reminding, and take the admin commands away."""
        services = self.services
        await services.scheduler.remove(SYNC_JOB)
        services.reminders.unregister(WORKSHOPS)
        detach_admin_group(self.bot, self.workshops_admin)

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignores on them.
    @commands.hybrid_group(fallback='next', brief='Show the next KCPC workshop')  # type: ignore[arg-type]
    @commands.guild_only()
    async def event(self, ctx: commands.Context[Any]) -> None:
        """Show the next KCPC workshop: when and where it is.

        Workshops come from this server's Luma calendar, which the bot checks
        every 10 minutes.

        Examples:
            /event next
            ;event
        """
        calendar_id = await self._calendar(_guild(ctx).id)
        last_synced = await self._last_synced(calendar_id)
        upcoming = await self._repo.next_from(calendar_id, self.services.clock.now())
        if upcoming is None:
            message = OutgoingMessage(
                title='Next workshop',
                description=_NOTHING_UPCOMING,
                footer=_LAST_SYNCED,
            )
        else:
            workshop = WorkshopOccurrence.from_event(upcoming)
            message = OutgoingMessage(
                title=f'Next workshop: {workshop.title}',
                url=workshop.url,
                description=workshop_details(workshop),
                footer=_LAST_SYNCED,
            )
        await ctx.send(embed=_synced_embed(message, last_synced))

    @event.command(name='this-week', brief="Show this week's KCPC workshops")  # type: ignore[arg-type]
    async def this_week(self, ctx: commands.Context[Any]) -> None:
        """Show this week's KCPC workshops, Monday to Sunday in the club's time zone.

        Those that are over are marked as finished.

        Examples:
            /event this-week
            ;event this-week
        """
        calendar_id = await self._calendar(_guild(ctx).id)
        last_synced = await self._last_synced(calendar_id)
        services = self.services
        now = services.clock.now()
        week_start, week_end = local_week_bounds(now, services.settings.tz)
        events = await self._repo.between(calendar_id, week_start, week_end)
        fields = tuple(_week_field(event, now) for event in events)
        message = OutgoingMessage(
            title='Workshops this week',
            description=None if fields else _NONE_THIS_WEEK,
            fields=fields,
            footer=_LAST_SYNCED,
        )
        await ctx.send(embed=_synced_embed(message, last_synced))

    @commands.hybrid_group(  # type: ignore[arg-type]
        name='workshops', brief='Set or sync the Luma calendar that workshops come from'
    )
    @kcpc_admin_only()
    async def workshops_admin(self, ctx: commands.Context[Any]) -> None:
        """Set the Luma calendar whose workshops this server follows, or sync it
        now.
        """
        # Only ;kcpc workshops gets here: Discord can't run a slash group.
        await ctx.send_help(ctx.command)

    @workshops_admin.command(name='calendar', brief="Set this server's Luma calendar")  # type: ignore[arg-type]
    @app_commands.describe(
        calendar='The calendar ID (cal-…), or its iCal link from "Add to calendar"'
    )
    @kcpc_admin_only()
    async def set_calendar(self, ctx: commands.Context[Any], calendar: str) -> None:
        """Follow a Luma calendar: this server gets reminders of its workshops
        from now on.

        The bot first checks with Luma that the calendar exists, then saves it
        and syncs it at once.

        Examples:
            /kcpc workshops calendar cal-ExampleClub001
            ;kcpc workshops calendar https://api.lu.ma/ics/get?entity=calendar&id=cal-ExampleClub001
        """
        await ctx.defer(ephemeral=True)
        guild = _guild(ctx)
        calendar_id = calendar_id_from(calendar)
        # CalendarNotFound is a KcpcUserError: the admin is told there's no
        # such calendar, and nothing is saved.
        await self._client.fetch(calendar_id)
        services = self.services
        await services.guild_settings.update(
            guild.id, WORKSHOPS, calendar_id=calendar_id
        )
        report = await self._sync_calendar(calendar_id)
        await services.reminders.tick()
        upcoming = await self._repo.next_from(calendar_id, services.clock.now())
        followed = f'This server now follows Luma calendar `{calendar_id}`.'
        await _reply(
            ctx, success_embed(f'{followed}\n{_describe_upcoming(report, upcoming)}')
        )

    @workshops_admin.command(name='sync', brief="Sync this server's Luma calendar now")  # type: ignore[arg-type]
    @kcpc_admin_only()
    async def sync_now(self, ctx: commands.Context[Any]) -> None:
        """Sync this server's Luma calendar now, rather than within 10 minutes,
        and say what changed.

        Examples:
            /kcpc workshops sync
            ;kcpc workshops sync
        """
        await ctx.defer(ephemeral=True)
        calendar_id = await self._calendar(_guild(ctx).id)
        report = await self._sync_calendar(calendar_id)
        # Even a feed that failed the health check is applied: members hear of
        # what it changed at once.
        if report.changed:
            await self.services.reminders.tick()
        if not report.applied:
            raise KcpcUserError(
                f"Couldn't sync Luma calendar `{calendar_id}`: {report.error}"
            )
        if not report.ok:
            raise KcpcUserError(_describe_shrunk(report))
        await _reply(ctx, success_embed(_describe_sync(report)))

    async def _sync_all(self, slot: datetime) -> None:
        """Sync every calendar that a server with workshops on follows.

        A sync that fails doesn't stop the others. ``EventSync`` logs and
        records a feed it can't fetch or read, and a calendar that doesn't
        exist. Anything else is a bug: logged here, it fails the job once the
        other calendars are synced, so that the scheduler reports it. If events
        changed, or a calendar was synced for the first time since the bot
        started, reminders tick at once, so that notices about the changes, and
        reminders that waited for the sync, don't wait for the next tick.
        """
        # Each run syncs the feeds as they are now, whichever slot it is for.
        remind = False
        errors: list[tuple[str, Exception]] = []
        for calendar_id in await self._followed_calendars():
            # A calendar's reminders wait for its first sync, so tick after
            # it, whether or not it works (see _sync_calendar).
            remind = remind or not self._source.is_synced(calendar_id)
            try:
                report = await self._sync_calendar(calendar_id)
            except CalendarNotFound:
                continue
            except Exception as exc:
                logger.info(
                    'Syncing Luma calendar %s failed', calendar_id, exc_info=True
                )
                errors.append((calendar_id, exc))
                continue
            remind = remind or report.changed
        if remind:
            await self.services.reminders.tick()
        if errors:
            failed = ', '.join(calendar_id for calendar_id, _ in errors)
            message = f'Could not sync Luma calendars: {failed}'
            raise RuntimeError(message) from errors[0][1]

    async def _sync_calendar(self, calendar_id: str) -> SyncReport:
        """Sync the calendar, after which its workshops get reminders.

        They do even if syncing failed (see ``WorkshopReminders.occurrences``).
        """
        try:
            return await self._sync.sync(calendar_id)
        finally:
            self._source.mark_synced(calendar_id)

    async def _followed_calendars(self) -> list[str]:
        """The calendars that servers with workshops on follow, once each, sorted."""
        guilds = await self.services.guild_settings.enabled_guilds(WORKSHOPS)
        calendars = {self._source.calendar_for(settings) for _, settings in guilds}
        return sorted(calendar for calendar in calendars if calendar is not None)

    async def _calendar(self, guild_id: int) -> str:
        """The calendar the server follows; ``KcpcUserError`` if there is none."""
        settings = await self.services.guild_settings.get(guild_id, WORKSHOPS)
        calendar_id = self._source.calendar_for(settings)
        if calendar_id is None:
            raise KcpcUserError(NO_CALENDAR)
        return calendar_id

    async def _last_synced(self, calendar_id: str) -> datetime:
        """When the calendar last synced; ``KcpcUserError`` if it never has."""
        state = await self._repo.calendar_state(calendar_id)
        if state is None or state.last_ok is None:
            raise KcpcUserError(NEVER_SYNCED)
        return state.last_ok


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KcpcWorkshops(bot))


def _guild(ctx: commands.Context[Any]) -> discord.Guild:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()
    return ctx.guild


async def _reply(ctx: commands.Context[Any], embed: discord.Embed) -> None:
    # Only the admin sees a slash command's reply; prefix commands ignore it.
    await ctx.send(embed=embed, ephemeral=True)


def _synced_embed(message: OutgoingMessage, last_synced: datetime) -> discord.Embed:
    embed = to_embed(message)
    embed.timestamp = last_synced
    return embed


def _week_field(event: StoredEvent, now: datetime) -> EmbedField:
    """A workshop of this week, marked once it has finished.

    It has finished once it has ended or, if its end isn't known, started.
    """
    workshop = WorkshopOccurrence.from_event(event)
    finished = (workshop.end or workshop.start) <= now
    name = f'{workshop.title} (finished)' if finished else workshop.title
    return EmbedField(name, workshop_details(workshop, link=True))


def _describe_upcoming(report: SyncReport, upcoming: StoredEvent | None) -> str:
    """What syncing a newly chosen calendar found: what's coming up, and when."""
    if not report.applied:
        return f'Syncing it failed: {report.error}'
    if not report.ok:
        return _describe_shrunk(report)
    if upcoming is None:
        return 'It has no upcoming workshops.'
    count = _workshops(report.future_count)
    when = discord_timestamp(upcoming.start_time, 'F')
    return f'It has {count} coming up. The next is **{upcoming.name}**, {when}.'


def _describe_sync(report: SyncReport) -> str:
    return (
        f'Synced Luma calendar `{report.calendar_id}`: {_describe_changes(report)}. '
        f'Upcoming workshops: {report.future_count}.'
    )


def _describe_shrunk(report: SyncReport) -> str:
    """What a sync did with a feed that failed the health check (``EventSync``)."""
    return (
        f'Luma calendar `{report.calendar_id}` lists far fewer upcoming workshops '
        f'than before ({report.error}). Applied: {_describe_changes(report)}; '
        'workshops missing from it count as cancelled after about an hour.'
    )


def _describe_changes(report: SyncReport) -> str:
    return (
        f'{report.added} added, {report.updated} updated, {report.moved} moved, '
        f'{report.cancelled} cancelled, {report.reinstated} reinstated'
    )


def _workshops(count: int) -> str:
    return f'{count} workshop' if count == 1 else f'{count} workshops'
