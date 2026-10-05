"""Luma calendars, read from their public iCal feeds.

Every Luma calendar publishes its events at ``ICS_URL``, with no API key. The
feed lists each event once, as a VEVENT whose UID is the event's ID at
``events.lu.ma``: ``evt-…`` for an event hosted on Luma, ``calev-…`` for an
external event that the calendar lists. Times are in UTC, or are dates for
all-day events.

Luma deletes a cancelled event from the feed rather than marking it, so callers
notice cancellations by events going missing. Its STATUS, SEQUENCE and DTSTAMP
say nothing about changes (DTSTAMP is the time of the request), so they are
ignored, except that STATUS:CANCELLED drops an event in case Luma starts
sending it.
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from http import HTTPStatus
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo

from icalendar import Calendar, Component, vDDDTypes

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.timeutil import ensure_utc, resolve_local, to_epoch

logger = logging.getLogger(__name__)

ICS_URL = 'https://api.lu.ma/ics/get'

_SERVICE = 'Luma'
_EXTERNAL_EVENT_PREFIX = 'calev-'
_UNTITLED = '(untitled)'

_CALENDAR_ID = re.compile(r'cal-[A-Za-z0-9]{1,64}')
_CALENDAR_LINK_SCHEMES = ('http', 'https', 'webcal')  # webcal: "Add to calendar"
_LUMA_DOMAINS = ('lu.ma', 'luma.com')
_CALENDAR_HINT = (
    "That doesn't look like a Luma calendar. Give its calendar ID, which starts "
    'with "cal-", or its iCal link from "Add to calendar", which contains '
    '"id=cal-".'
)

# A link to a page on Luma: its path starts like a slug or 'event/…' does, so
# 'https://luma.com/.' at the end of a sentence is not one.
_LUMA_PAGE = re.compile(r'https://(?:luma\.com|lu\.ma)/[\w-]\S*', re.IGNORECASE)
# Prose that can follow a link in a description, which links don't end with.
_TRAILING_PUNCTUATION = '.,;:!?\'")]}>'


def calendar_id_from(text: str) -> str:
    """The Luma calendar ID that an admin gave: the ID itself or an iCal link.

    An ID looks like ``cal-…``. A link must be on lu.ma, luma.com or one of
    their subdomains (such as api.lu.ma or api2.luma.com), over http(s) or
    webcal, with ``id=cal-…`` in its query. Surrounding whitespace and the
    <angle brackets> that stop Discord embedding a link are ignored. Anything
    else raises ``KcpcUserError`` saying what to give instead.
    """
    candidate = text.strip().removeprefix('<').removesuffix('>').strip()
    if _CALENDAR_ID.fullmatch(candidate):
        return candidate
    calendar_id = _calendar_id_in_link(candidate)
    if calendar_id is None:
        raise KcpcUserError(_CALENDAR_HINT)
    return calendar_id


def _calendar_id_in_link(text: str) -> str | None:
    """The calendar ID in a Luma iCal link, or None if ``text`` isn't one."""
    try:
        parts = urlsplit(text)
        host = parts.hostname  # lowercased
    except ValueError:  # e.g. an unclosed [ in the host
        return None
    if parts.scheme.lower() not in _CALENDAR_LINK_SCHEMES or not host:
        return None
    if not any(host == d or host.endswith(f'.{d}') for d in _LUMA_DOMAINS):
        return None
    ids = parse_qs(parts.query).get('id', [])
    return next((value for value in ids if _CALENDAR_ID.fullmatch(value)), None)


@dataclass(frozen=True)
class LumaEvent:
    """One event of a Luma calendar.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted), and ``end``, when known, is after ``start``.
    """

    luma_id: str  # 'evt-…' or 'calev-…': the UID without its @domain
    name: str
    start: datetime
    end: datetime | None
    url: str
    location: str | None

    def __post_init__(self) -> None:
        # Converted here too, so that events built by hand (in tests, say)
        # compare and fingerprint exactly like parsed ones.
        start = _whole_seconds_utc(self.start)
        end = None if self.end is None else _whole_seconds_utc(self.end)
        if end is not None and end <= start:
            raise ValueError(f'Luma event {self.luma_id} must end after it starts')
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)

    def fingerprint(self) -> str:
        """A digest of what members are shown: name, times, link and location.

        Fields are encoded as a JSON list, so no two different events share an
        encoding (a name ending where a location begins, say, or None and '').
        """
        encoded = json.dumps(
            [
                self.name,
                to_epoch(self.start),
                None if self.end is None else to_epoch(self.end),
                self.url,
                self.location,
            ],
            separators=(',', ':'),
        )
        # ASCII: json.dumps escapes everything else.
        return hashlib.sha1(encoded.encode('ascii'), usedforsecurity=False).hexdigest()


class CalendarNotFound(KcpcUserError):
    """Luma has no calendar with this ID."""

    def __init__(self, calendar_id: str) -> None:
        super().__init__(f'Luma has no calendar with the ID {calendar_id}.')
        self.calendar_id = calendar_id


def parse_calendar(ics: bytes | str, *, tz: ZoneInfo) -> list[LumaEvent]:
    """The events of a Luma iCal feed, in feed order.

    ``tz`` is the club's time zone: an all-day event starts at midnight there.
    Events without a UID or a usable DTSTART are skipped, and so are events
    marked STATUS:CANCELLED, so that they count as missing. If two events share
    a UID, the first is kept. Raises ``ExternalServiceError`` if ``ics`` is not
    a readable iCalendar document.
    """
    calendar = _read_calendar(ics)
    events: list[LumaEvent] = []
    seen: set[str] = set()
    for component in calendar.subcomponents:
        if component.name != 'VEVENT':
            continue
        event = _event_from(component, tz)
        if event is None:
            continue
        if event.luma_id in seen:
            logger.debug(
                'Skipping a second Luma event with the UID of %s', event.luma_id
            )
            continue
        seen.add(event.luma_id)
        events.append(event)
    return events


class LumaCalendarClient:
    """Fetches Luma calendars through the shared ``HttpClient``."""

    def __init__(self, http: HttpClient, *, tz: ZoneInfo) -> None:
        self._http = http
        self._tz = tz

    async def fetch(self, calendar_id: str) -> list[LumaEvent]:
        """Every event in the calendar's feed, as ``parse_calendar`` reads it.

        Raises ``CalendarNotFound`` if Luma has no such calendar, and
        ``ExternalServiceError`` if Luma can't be reached or sends something
        other than a readable calendar feed.
        """
        response = await self._http.get(
            ICS_URL,
            params={'entity': 'calendar', 'id': calendar_id},
            service=_SERVICE,
            allow_status={HTTPStatus.NOT_FOUND},
        )
        if response.status == HTTPStatus.NOT_FOUND:
            raise CalendarNotFound(calendar_id)
        content_type = response.headers.get('Content-Type', '')
        if content_type.partition(';')[0].strip().lower() != 'text/calendar':
            logger.debug(
                'Luma sent Content-Type %r for calendar %s', content_type, calendar_id
            )
            raise ExternalServiceError(
                _SERVICE,
                'Luma sent something other than a calendar feed.',
                status=response.status,
            )
        # In a worker thread: a big calendar takes icalendar a few hundred
        # milliseconds, too long to hold up the event loop.
        return await asyncio.to_thread(parse_calendar, response.body, tz=self._tz)


def _read_calendar(ics: bytes | str) -> Component:
    """The VCALENDAR in ``ics``; ``ExternalServiceError`` if there isn't one."""
    try:
        # Always bytes: icalendar treats a str without line breaks as the path
        # of a file to read.
        data = ics.encode() if isinstance(ics, str) else ics
        calendar = Calendar.from_ical(data)
    # The feed is untrusted input, and icalendar's errors are not limited to
    # ValueError (a bad calendar-level property can raise TypeError, say).
    # Whatever it raises, the feed can't be read.
    except Exception as exc:
        raise _unreadable_feed() from exc
    if calendar.name != 'VCALENDAR':
        raise _unreadable_feed()
    return calendar


def _unreadable_feed() -> ExternalServiceError:
    return ExternalServiceError(_SERVICE, "Luma's calendar feed could not be read.")


def _event_from(component: Component, tz: ZoneInfo) -> LumaEvent | None:
    """The event in a VEVENT, or None (logged) if it is to be skipped."""
    uid = (_text(component, 'UID') or '').strip()
    # The ID is the UID up to its last @, whatever the domain: were Luma to
    # move its UIDs off events.lu.ma, as it moved its links to luma.com, every
    # event would otherwise get a new ID, and be reminded of again.
    head, at, _ = uid.rpartition('@')
    luma_id = (head if at else uid).strip()
    if not luma_id:
        logger.debug('Skipping a Luma event without a UID')
        return None
    if (_text(component, 'STATUS') or '').strip().upper() == 'CANCELLED':
        logger.debug('Skipping Luma event %s: it is cancelled', luma_id)
        return None
    start = _instant(component, 'DTSTART', tz)
    if start is None:
        logger.debug('Skipping Luma event %s: it has no usable DTSTART', luma_id)
        return None
    end = _instant(component, 'DTEND', tz)
    location = (_text(component, 'LOCATION') or '').strip() or None
    return LumaEvent(
        luma_id=luma_id,
        name=(_text(component, 'SUMMARY') or '').strip() or _UNTITLED,
        start=start,
        end=end if end is not None and end > start else None,
        url=_event_url(luma_id, _text(component, 'DESCRIPTION') or '', location),
        location=location,
    )


def _first(component: Component, name: str) -> object:
    """The value of property ``name``, the first one if it is repeated."""
    value = component.get(name)
    if isinstance(value, list):  # repeated, though RFC 5545 allows it only once
        return value[0] if value else None
    return value


def _text(component: Component, name: str) -> str | None:
    """A text property, unescaped, or None if absent."""
    value = _first(component, name)
    return None if value is None else str(value)


def _instant(component: Component, name: str, tz: ZoneInfo) -> datetime | None:
    """A DTSTART or DTEND as an aware UTC datetime in whole seconds.

    A date-time without a zone (floating, or in a zone icalendar doesn't know)
    is read as UTC, and a date as midnight in ``tz``. None if the property is
    absent, didn't parse, or isn't a date or date-time.
    """
    prop = _first(component, name)
    # Anything else is absent, or a value icalendar couldn't parse (vBroken).
    if not isinstance(prop, vDDDTypes):
        return None
    value = prop.dt
    try:
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                return _whole_seconds_utc(value.replace(tzinfo=UTC))
            return _whole_seconds_utc(value)
        if isinstance(value, date):
            return resolve_local(datetime.combine(value, time()), tz)
    # Near year 1 or 9999, moving to UTC can leave datetime's range.
    except (OverflowError, ValueError):
        return None
    return None  # a time of day or a duration, which pins down no instant


def _whole_seconds_utc(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds; naive raises ValueError.

    Truncating floors, as ``to_epoch`` does, so the time reads back from the
    database unchanged.
    """
    return ensure_utc(moment).replace(microsecond=0)


def _event_url(luma_id: str, description: str, location: str | None) -> str:
    """The page to link for an event.

    Luma's own events link their page first in the description ("Get
    up-to-date information at: https://luma.com/…"), and their LOCATION is an
    address or that same page. An external event's description links only the
    calendar ("Find more information on https://luma.com/<calendar>"), while
    its LOCATION is the event's own page elsewhere, so that comes first. Failing
    both, the event's page on Luma by its ID.
    """
    on_luma = _first_luma_page(description)
    elsewhere = location if location is not None and _is_web_url(location) else None
    if luma_id.startswith(_EXTERNAL_EVENT_PREFIX):
        candidates = (elsewhere, on_luma)
    else:
        candidates = (on_luma, elsewhere)
    fallback = f'https://luma.com/event/{quote(luma_id, safe="")}'
    return next((url for url in candidates if url is not None), fallback)


def _first_luma_page(text: str) -> str | None:
    match = _LUMA_PAGE.search(text)
    return None if match is None else match.group().rstrip(_TRAILING_PUNCTUATION)


def _is_web_url(text: str) -> bool:
    """Whether ``text`` is an absolute http(s) URL and nothing else."""
    if any(character.isspace() for character in text):
        return False
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme.lower() in ('http', 'https') and bool(host)
