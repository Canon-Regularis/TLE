"""Workshop reminders: the workshops feature's ``ReminderSource``.

Each server follows one Luma calendar, its own or the bot's default (see
``effective_calendar``), and its workshops are that calendar's stored events.
The reminder engine (``tle.kcpc.core.reminders``) reminds members before each
one starts, at the server's ``reminder_minutes``, and tells them when one they
have heard about moves, is cancelled or is back on. This module says what each
of those posts looks like.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tle.kcpc.core.messages import EmbedField, OutgoingMessage, shorten
from tle.kcpc.core.reminders import Notice, NoticeKind, Occurrence, ReminderPolicy
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.core.timeutil import discord_timestamp
from tle.kcpc.features.workshops.repo import EventRepo, StoredEvent
from tle.kcpc.features.workshops.settings import (
    WORKSHOPS,
    WorkshopSettings,
    effective_calendar,
)

logger = logging.getLogger(__name__)

SUBJECT = 'event'  # the ledger's subject for a workshop, whose id is its Luma ID
FOOTER = 'KCPC workshops'

# How far ahead the engine looks for workshops. A reminder any earlier than
# this before the start could not go out on time.
_HORIZON = timedelta(days=400)
_LONGEST_REMINDER_MINUTES = _HORIZON // timedelta(minutes=1)
# A reminder this long before the start, or longer, says "Coming up"; a later
# one says "Starting soon".
_COMING_UP = timedelta(hours=12)
# A reminder of several workshops shows each as an embed field: its title, then
# workshop_details with a link. The post must show all of them, up to the
# engine's MAX_GROUP, and Discord takes at most 6000 characters per embed. With
# each title and location cut to these lengths, and a link to a page left out
# if longer than this, MAX_GROUP fields always fit.
_FIELD_TITLE_LIMIT = 100
_FIELD_LOCATION_LIMIT = 150
_FIELD_LINK_LIMIT = 200


@dataclass(frozen=True)
class WorkshopOccurrence(Occurrence):
    """A workshop as the reminder engine sees it, with its location for posts."""

    location: str | None = None

    @classmethod
    def from_event(cls, event: StoredEvent) -> 'WorkshopOccurrence':
        return cls(
            subject=SUBJECT,
            subject_id=event.luma_id,
            title=event.name,
            start=event.start_time,
            end=event.end_time,
            url=event.url,
            revision=event.revision,
            cancelled=event.cancelled,
            location=event.location,
        )


def workshop_settings(settings: FeatureSettings) -> WorkshopSettings:
    """``settings`` as the workshops feature's own, else ``TypeError``.

    They always are once its spec is registered, as ``tle.kcpc.bootstrap``
    does before anything reads them.
    """
    if not isinstance(settings, WorkshopSettings):
        raise TypeError(f'Expected WorkshopSettings, got {type(settings).__name__}')
    return settings


class WorkshopReminders:
    """The workshops of each server, and the posts about them.

    ``default_calendar`` is the calendar of servers that haven't chosen one
    (``LUMA_CALENDAR_ID``), if any.
    """

    def __init__(self, repo: EventRepo, default_calendar: str | None) -> None:
        self._repo = repo
        self._default_calendar = default_calendar
        # The calendars this process has tried to sync (see occurrences).
        self._synced: set[str] = set()
        # Settings already warned about: the engine asks for a policy every
        # minute, and warnings reach the Discord log channel.
        self._warned: set[tuple[int, ...]] = set()

    @property
    def feature(self) -> str:
        return WORKSHOPS

    def calendar_for(self, settings: FeatureSettings) -> str | None:
        """The calendar that a server with ``settings`` follows, if any."""
        return effective_calendar(workshop_settings(settings), self._default_calendar)

    def is_synced(self, calendar_id: str) -> bool:
        """Whether this process has tried to sync the calendar (``mark_synced``)."""
        return calendar_id in self._synced

    def mark_synced(self, calendar_id: str) -> None:
        """Note that this process has tried to sync the calendar.

        Called after every attempt, whether or not it worked: while Luma is
        down, reminders go by the events stored before rather than not at all.
        """
        self._synced.add(calendar_id)

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        """Reminders at the server's ``reminder_minutes``, with no start post.

        An entry that isn't from 1 minute to 400 days, or that repeats an
        earlier one, is left out, with a warning once per distinct setting.
        """
        minutes = workshop_settings(settings).reminder_minutes
        kept: list[int] = []
        ignored: list[int] = []
        for value in minutes:
            if 0 < value <= _LONGEST_REMINDER_MINUTES and value not in kept:
                kept.append(value)
            else:
                ignored.append(value)
        if ignored and minutes not in self._warned:
            self._warned.add(minutes)
            logger.warning(
                'Ignoring %s in the workshop reminder_minutes %s: each reminder is '
                'from 1 to %d minutes before the start, and listed once',
                ', '.join(map(str, ignored)),
                minutes,
                _LONGEST_REMINDER_MINUTES,
            )
        return ReminderPolicy(
            offsets=tuple(timedelta(minutes=value) for value in kept),
            horizon=_HORIZON,
        )

    async def occurrences(
        self,
        guild_id: int,
        settings: FeatureSettings,
        start: datetime,
        end: datetime,
    ) -> Sequence[WorkshopOccurrence]:
        """The workshops of the server's calendar in [start, end), cancelled too.

        A server that follows no calendar has none, and neither has one whose
        calendar this process hasn't tried to sync yet. Its stored events may be
        from before the bot restarted: a workshop that moved meanwhile would
        get a reminder at its old time, then a time change notice, and then
        perhaps the reminder again.
        """
        calendar_id = self.calendar_for(settings)
        if calendar_id is None or not self.is_synced(calendar_id):
            return []
        events = await self._repo.between(
            calendar_id, start, end, include_cancelled=True
        )
        return [WorkshopOccurrence.from_event(event) for event in events]

    def render(self, notice: Notice) -> OutgoingMessage:
        """The post for ``notice``. It mentions the workshops role."""
        if notice.kind is NoticeKind.REMINDER:
            return _reminder(notice)
        # Only reminders are ever about several workshops.
        (workshop,) = notice.occurrences
        if notice.kind is NoticeKind.MOVED:
            lines = [f'**New time:** {_start(workshop.start)}']
            if notice.previous_start is not None:
                previous = discord_timestamp(notice.previous_start, 'F')
                lines.append(f'**Previously:** {previous}')
            return _post(
                f'Time changed: {workshop.title}', '\n'.join(lines), url=workshop.url
            )
        if notice.kind is NoticeKind.CANCELLED:
            planned = discord_timestamp(workshop.start, 'F')
            return _post(
                f'Cancelled: {workshop.title}', f'**Was planned for:** {planned}'
            )
        return _post(
            f'Back on: {workshop.title}',
            f'**When:** {_start(workshop.start)}',
            url=workshop.url,
        )


def workshop_details(workshop: Occurrence, *, link: bool = False) -> str:
    """When and where ``workshop`` is, one fact per line.

    Times show in each reader's own time zone. With ``link``, for an embed
    field of a post about several workshops, whose title can't link to each,
    a link to the workshop's page ends the text. The location is then cut
    short, and a link too long left out, so that many such fields fit in one
    post.
    """
    lines = [f'**When:** {_start(workshop.start)}']
    if workshop.end is not None:
        lines.append(f'**Ends:** {discord_timestamp(workshop.end, "f")}')
    location = _location(workshop)
    # An external event's location is often its page, which is linked anyway.
    if location is not None and location != workshop.url:
        if link:
            location = shorten(location, _FIELD_LOCATION_LIMIT) or location
        lines.append(f'**Where:** {location}')
    if link and workshop.url is not None:
        target = _link_target(workshop.url)
        if len(target) <= _FIELD_LINK_LIMIT:
            lines.append(f'[Event page]({target})')
    return '\n'.join(lines)


def _reminder(notice: Notice) -> OutgoingMessage:
    """'Coming up' a while before the start; 'Starting soon' within 12 hours."""
    offset = notice.offset or timedelta(0)
    heading = 'Coming up' if offset >= _COMING_UP else 'Starting soon'
    workshops = notice.occurrences
    if len(workshops) == 1:
        (workshop,) = workshops
        return _post(
            f'{heading}: {workshop.title}',
            workshop_details(workshop),
            url=workshop.url,
        )
    return _post(
        f'{heading}: {len(workshops)} workshops',
        fields=tuple(
            EmbedField(
                shorten(workshop.title, _FIELD_TITLE_LIMIT) or workshop.title,
                workshop_details(workshop, link=True),
            )
            for workshop in workshops
        ),
    )


def _post(
    title: str,
    description: str | None = None,
    *,
    url: str | None = None,
    fields: tuple[EmbedField, ...] = (),
) -> OutgoingMessage:
    return OutgoingMessage(
        title=title,
        description=description,
        url=url,
        fields=fields,
        footer=FOOTER,
        mention_role=True,
    )


def _start(moment: datetime) -> str:
    """A start as a date and time, then how far off it is."""
    return f'{discord_timestamp(moment, "F")} ({discord_timestamp(moment, "R")})'


def _location(workshop: Occurrence) -> str | None:
    # The occurrences this source lists are all WorkshopOccurrences.
    return workshop.location if isinstance(workshop, WorkshopOccurrence) else None


def _link_target(url: str) -> str:
    """``url`` as the target of a Markdown link, which a ')' would end early."""
    return url.replace('(', '%28').replace(')', '%29')
