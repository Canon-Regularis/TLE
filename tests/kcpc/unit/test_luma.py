"""Tests for tle.kcpc.platforms.luma: parsing, calendar IDs and fetching.

fixtures/luma/calendar.ics is synthetic, but laid out exactly as Luma's feeds
are: LF line endings, lines folded at 75 octets (even inside an escape or a
URL) except long ORGANIZER lines, commas left unescaped, a constant SEQUENCE,
DTSTAMP the time of the request, and no newline after END:VCALENDAR.
fixtures/luma/truncated.ics is a feed cut off mid-event.
"""

import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms import luma
from tle.kcpc.platforms.luma import (
    CalendarNotFound,
    LumaCalendarClient,
    LumaEvent,
    calendar_id_from,
    parse_calendar,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'luma'
LONDON = ZoneInfo('Europe/London')
CALENDAR_ID = 'cal-ExampleClub001'
UNREADABLE = "Luma's calendar feed could not be read."
LUMA_LOGGER = 'tle.kcpc.platforms.luma'


def at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def feed(*events: str) -> bytes:
    """A feed with Luma's header and ``events``, each from ``vevent``."""
    return (
        'BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Luma//Example Coding Club//EN\n'
        + ''.join(events)
        + 'END:VCALENDAR'
    ).encode()


def vevent(*lines: str) -> str:
    return 'BEGIN:VEVENT\n' + ''.join(f'{line}\n' for line in lines) + 'END:VEVENT\n'


UID = 'UID:evt-Example0001@events.lu.ma'
START = 'DTSTART:20261008T170000Z'


def parse_one(*lines: str, tz: ZoneInfo = LONDON) -> LumaEvent:
    """The one event of a feed with a VEVENT of ``lines``."""
    [event] = parse_calendar(feed(vevent(*lines)), tz=tz)
    return event


def parse_lines(*lines: str) -> list[LumaEvent]:
    return parse_calendar(feed(vevent(*lines)), tz=LONDON)


EXPECTED_FIXTURE_EVENTS = [
    # An external event: its own page is its LOCATION, not the calendar's
    # page that its description links.
    LumaEvent(
        luma_id='calev-ExamplePartner1',
        name='Partner Hackathon Social',
        start=at(2026, 9, 24, 17),
        end=at(2026, 9, 24, 19),
        url='https://events.example.org/partner-hackathon-social',
        location='https://events.example.org/partner-hackathon-social',
    ),
    # Its link and its address were folded and escaped across three lines.
    LumaEvent(
        luma_id='evt-ExampleIntroDp1',
        name='Intro to Dynamic Programming',
        start=at(2026, 10, 8, 17),
        end=at(2026, 10, 8, 19),
        url='https://luma.com/intro-dp-example',
        location='Room 101, Example Building, 1 Example Street, London, UK',
    ),
    # A folded name with an emoji; the address is hidden, so LOCATION is the
    # event's own page, but the description's link comes first.
    LumaEvent(
        luma_id='evt-ExampleGraphs1',
        name=(
            'Graph Algorithms Workshop \U0001f9e9 shortest paths, network flows '
            'and matchings for contest problems'
        ),
        start=at(2026, 10, 15, 17),
        end=at(2026, 10, 15, 18, 30),
        url='https://luma.com/graphs-example',
        location='https://luma.com/event/evt-ExampleGraphs1',
    ),
    # All day on 24 and 25 October: from midnight BST to midnight GMT, as the
    # clocks go back in between.
    LumaEvent(
        luma_id='evt-ExampleRetreat',
        name='Contest Practice Weekend',
        start=at(2026, 10, 23, 23),
        end=at(2026, 10, 26, 0),
        url='https://luma.com/practice-weekend-example',
        location='Example Field Centre',
    ),
    LumaEvent(
        luma_id='evt-ExampleNoEnd01',
        name='Mock Contest',
        start=at(2026, 11, 5, 18),
        end=None,
        url='https://luma.com/mock-contest-example',
        location='Lab 2, Example Building',
    ),
    # evt-ExampleCancel1, marked STATUS:CANCELLED, is left out.
    LumaEvent(
        luma_id='calev-ExamplePartner2',
        name='Partner Talk: Careers in Competitive Programming',
        start=at(2026, 11, 19, 19),
        end=at(2026, 11, 19, 21),
        url='https://events.example.org/careers-talk',
        location='https://events.example.org/careers-talk',
    ),
]


class TestTheFixtureFeed:
    def test_parses_to_exactly_these_events(self) -> None:
        assert parse_calendar(fixture('calendar.ics'), tz=LONDON) == (
            EXPECTED_FIXTURE_EVENTS
        )

    def test_is_laid_out_like_lumas_feeds(self) -> None:
        # Guards the fixture: an editor or a git setting could quietly change
        # what the tests above read.
        data = fixture('calendar.ics')
        lines = data.split(b'\n')
        assert b'\r' not in data
        assert lines[-1] == b'END:VCALENDAR'  # no newline after it
        assert any(line.startswith(b' ') for line in lines)  # folded lines
        long_lines = [line for line in lines if len(line) > 75]
        assert long_lines and all(line.startswith(b'ORGANIZER') for line in long_lines)
        assert b'\\,' not in data  # Luma doesn't escape commas

    def test_crlf_line_endings_read_the_same(self) -> None:
        data = fixture('calendar.ics').replace(b'\n', b'\r\n')
        assert parse_calendar(data, tz=LONDON) == EXPECTED_FIXTURE_EVENTS

    def test_a_str_reads_the_same_as_bytes(self) -> None:
        data = fixture('calendar.ics')
        assert parse_calendar(data.decode(), tz=LONDON) == EXPECTED_FIXTURE_EVENTS

    def test_a_str_without_line_breaks_is_never_read_as_a_file_path(
        self, tmp_path: Path
    ) -> None:
        # icalendar would open the file a one-line str names and parse that.
        path = tmp_path / 'calendar.ics'
        path.write_bytes(fixture('calendar.ics'))
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_calendar(str(path), tz=LONDON)


class TestEventFields:
    @pytest.mark.parametrize(
        'uid',
        [
            UID,
            'UID:evt-Example0001@events.luma.com',
            'UID:evt-Example0001@EVENTS.LU.MA',
            'UID:evt-Example0001',
            'UID:  evt-Example0001 @events.lu.ma ',
        ],
        ids=['lu.ma', 'another-domain', 'upper-case', 'no-domain', 'spaces'],
    )
    def test_the_id_is_the_uid_without_its_domain(self, uid: str) -> None:
        # Whatever the domain, so that Luma moving its UIDs to another one
        # wouldn't make every event a new one, to be reminded of again.
        assert parse_one(uid, START).luma_id == 'evt-Example0001'

    def test_the_id_ends_at_the_last_at_sign(self) -> None:
        assert parse_one('UID:calev-Ext1@events.lu.ma', START).luma_id == 'calev-Ext1'
        assert parse_one('UID:a@b@events.lu.ma', START).luma_id == 'a@b'

    @pytest.mark.parametrize(
        ('summary', 'name'),
        [
            (['SUMMARY:  Intro to DP  '], 'Intro to DP'),
            (['SUMMARY:'], '(untitled)'),
            (['SUMMARY:   '], '(untitled)'),
            ([], '(untitled)'),
            (['SUMMARY:First', 'SUMMARY:Second'], 'First'),
        ],
        ids=['stripped', 'empty', 'blank', 'missing', 'repeated'],
    )
    def test_name(self, summary: list[str], name: str) -> None:
        assert parse_one(UID, START, *summary).name == name

    @pytest.mark.parametrize(
        ('lines', 'location'),
        [
            (['LOCATION:  Room 101, Example Building  '], 'Room 101, Example Building'),
            (['LOCATION:Room 101\\, Example Building'], 'Room 101, Example Building'),
            (['LOCATION:'], None),
            (['LOCATION:  '], None),
            ([], None),
        ],
        ids=['stripped', 'escaped-comma', 'empty', 'blank', 'missing'],
    )
    def test_location(self, lines: list[str], location: str | None) -> None:
        assert parse_one(UID, START, *lines).location == location

    @pytest.mark.parametrize(
        ('start_line', 'start'),
        [
            ('DTSTART:20261008T170000Z', at(2026, 10, 8, 17)),
            ('DTSTART;TZID=Europe/London:20261008T180000', at(2026, 10, 8, 17)),
            ('DTSTART;TZID=America/New_York:20261208T120000', at(2026, 12, 8, 17)),
            # Floating: no zone at all, or one icalendar doesn't know.
            ('DTSTART:20261008T170000', at(2026, 10, 8, 17)),
            ('DTSTART;TZID=Example/Nowhere:20261008T170000', at(2026, 10, 8, 17)),
            # A date is midnight in the club's time zone.
            ('DTSTART;VALUE=DATE:20261008', at(2026, 10, 7, 23)),
            ('DTSTART;VALUE=DATE:20261208', at(2026, 12, 8, 0)),
        ],
        ids=[
            'utc',
            'london-summer',
            'new-york-winter',
            'floating',
            'unknown-zone',
            'date-in-summer',
            'date-in-winter',
        ],
    )
    def test_start_in_utc(self, start_line: str, start: datetime) -> None:
        event = parse_one(UID, start_line)
        assert event.start == start
        assert event.start.tzinfo is UTC

    def test_a_date_is_midnight_in_the_given_zone(self) -> None:
        tokyo = ZoneInfo('Asia/Tokyo')
        event = parse_one(UID, 'DTSTART;VALUE=DATE:20261008', tz=tokyo)
        assert event.start == at(2026, 10, 7, 15)

    @pytest.mark.parametrize(
        ('end_line', 'end'),
        [
            ('DTEND:20261008T190000Z', at(2026, 10, 8, 19)),
            ('DTEND:20261008T170001Z', at(2026, 10, 8, 17, 0, 1)),
            ('DTEND:20261008T170000Z', None),  # at the start
            ('DTEND:20261008T160000Z', None),  # before the start
            ('DTEND:not-a-date', None),
            ('DTEND;VALUE=DATE:20261010', at(2026, 10, 9, 23)),
        ],
        ids=['later', 'a-second-later', 'equal', 'earlier', 'broken', 'date'],
    )
    def test_end(self, end_line: str, end: datetime | None) -> None:
        assert parse_one(UID, START, end_line).end == end

    def test_a_missing_end_is_none(self) -> None:
        assert parse_one(UID, START).end is None

    @pytest.mark.parametrize('status', ['TENTATIVE', 'CONFIRMED', 'Cancel'])
    def test_other_statuses_are_ignored(self, status: str) -> None:
        assert parse_lines(UID, START, f'STATUS:{status}') != []

    @pytest.mark.parametrize('status', ['CANCELLED', 'cancelled', ' Cancelled '])
    def test_a_cancelled_event_is_skipped(self, status: str) -> None:
        assert parse_lines(UID, START, f'STATUS:{status}') == []

    @pytest.mark.parametrize(
        'lines',
        [
            [START],
            ['UID:', START],
            ['UID:   ', START],
            ['UID:@events.lu.ma', START],
            ['UID:@events.luma.com', START],
            [UID],
            [UID, 'DTSTART:not-a-date'],
            [UID, 'DTSTART:20261008T170000.5Z'],
            [UID, 'DTSTART;VALUE=PERIOD:20261008T170000Z/PT1H'],
            [UID, 'DTSTART;VALUE=DURATION:PT1H'],
            [UID, 'DTSTART;VALUE=TIME:170000'],
            # Out of range once in UTC.
            [UID, 'DTSTART;TZID=America/New_York:99991231T230000'],
        ],
        ids=[
            'no-uid',
            'empty-uid',
            'blank-uid',
            'only-the-suffix',
            'only-another-domain',
            'no-dtstart',
            'broken-dtstart',
            'fractional-seconds',
            'period',
            'duration',
            'time-of-day',
            'out-of-range',
        ],
    )
    def test_an_event_without_a_uid_or_a_usable_start_is_skipped(
        self, lines: list[str]
    ) -> None:
        assert parse_lines(*lines, 'SUMMARY:Kept out') == []

    def test_a_date_out_of_range_in_utc_is_skipped(self) -> None:
        events = parse_calendar(
            feed(vevent(UID, 'DTSTART;VALUE=DATE:00010101')),
            tz=ZoneInfo('Asia/Tokyo'),
        )
        assert events == []

    def test_skipped_events_are_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LUMA_LOGGER)
        parse_calendar(
            feed(
                vevent(START),
                vevent(UID, 'DTSTART:not-a-date'),
                vevent(UID, START, 'STATUS:CANCELLED'),
                vevent(UID, START),
                vevent(UID, START),  # a second event with that UID
            ),
            tz=LONDON,
        )
        levels = [r.levelno for r in caplog.records if r.name == LUMA_LOGGER]
        assert levels == [logging.DEBUG] * 4

    def test_the_first_of_two_events_with_one_uid_is_kept(self) -> None:
        events = parse_calendar(
            feed(
                vevent(UID, START, 'SUMMARY:First'),
                vevent('UID:evt-Example0002@events.lu.ma', START),
                vevent(UID, 'DTSTART:20261009T170000Z', 'SUMMARY:Second'),
            ),
            tz=LONDON,
        )
        assert [(event.luma_id, event.name) for event in events] == [
            ('evt-Example0001', 'First'),
            ('evt-Example0002', '(untitled)'),
        ]

    def test_events_keep_the_feeds_order(self) -> None:
        events = parse_calendar(
            feed(
                vevent('UID:evt-B@events.lu.ma', 'DTSTART:20261009T170000Z'),
                vevent('UID:evt-A@events.lu.ma', 'DTSTART:20261008T170000Z'),
            ),
            tz=LONDON,
        )
        assert [event.luma_id for event in events] == ['evt-B', 'evt-A']

    def test_other_components_are_ignored(self) -> None:
        data = feed(
            'BEGIN:VTODO\nUID:todo-1\nDTSTART:20261008T170000Z\nEND:VTODO\n',
            vevent(UID, START),
        )
        [event] = parse_calendar(data, tz=LONDON)
        assert event.luma_id == 'evt-Example0001'

    def test_an_empty_calendar_has_no_events(self) -> None:
        assert parse_calendar(feed(), tz=LONDON) == []


class TestEventUrl:
    @pytest.mark.parametrize(
        ('lines', 'url'),
        [
            (
                [
                    'DESCRIPTION:Get up-to-date information at: '
                    'https://luma.com/intro-dp\\n\\nHosted by Example Host',
                    'LOCATION:https://luma.com/event/evt-Example0001',
                ],
                'https://luma.com/intro-dp',
            ),
            (
                ['DESCRIPTION:Get up-to-date information at: https://lu.ma/intro-dp'],
                'https://lu.ma/intro-dp',
            ),
            (
                ['DESCRIPTION:See https://example.com/x and https://luma.com/a-b.'],
                'https://luma.com/a-b',
            ),
            (
                ['DESCRIPTION:(details: https://luma.com/intro-dp?tk=Ab1)'],
                'https://luma.com/intro-dp?tk=Ab1',
            ),
            (
                [
                    'DESCRIPTION:Online at https://example.com/meet',
                    'LOCATION:  https://luma.com/event/evt-Example0001  ',
                ],
                'https://luma.com/event/evt-Example0001',
            ),
            (
                ['DESCRIPTION:Mentions https://luma.com/ only', 'LOCATION:Room 101'],
                'https://luma.com/event/evt-Example0001',
            ),
            (
                ['LOCATION:Zoom: https://example.com/meet'],
                'https://luma.com/event/evt-Example0001',
            ),
            (['LOCATION:www.example.com/x'], 'https://luma.com/event/evt-Example0001'),
            ([], 'https://luma.com/event/evt-Example0001'),
        ],
        ids=[
            'description-first',
            'old-domain',
            'trailing-punctuation',
            'with-query',
            'location-url',
            'bare-domain-is-no-page',
            'location-with-text',
            'location-without-scheme',
            'by-id',
        ],
    )
    def test_a_luma_event(self, lines: list[str], url: str) -> None:
        assert parse_one(UID, START, *lines).url == url

    @pytest.mark.parametrize(
        ('lines', 'url'),
        [
            (
                [
                    'DESCRIPTION:Find more information on '
                    'https://luma.com/example-coding-club',
                    'LOCATION:https://events.example.org/talk',
                ],
                'https://events.example.org/talk',
            ),
            (
                [
                    'DESCRIPTION:Find more information on '
                    'https://luma.com/example-coding-club',
                    'LOCATION:Example Hall',
                ],
                'https://luma.com/example-coding-club',
            ),
            ([], 'https://luma.com/event/calev-Ext1'),
        ],
        ids=['its-own-page', 'else-the-calendar', 'else-by-id'],
    )
    def test_an_external_event(self, lines: list[str], url: str) -> None:
        uid = 'UID:calev-Ext1@events.lu.ma'
        assert parse_one(uid, START, *lines).url == url

    def test_an_id_is_quoted_into_the_url(self) -> None:
        event = parse_one('UID:evt a/b?@events.lu.ma', START)
        assert event.url == 'https://luma.com/event/evt%20a%2Fb%3F'


class TestUnreadableFeeds:
    @pytest.mark.parametrize(
        'data',
        [
            b'',
            b'\n\n',
            fixture('truncated.ics'),
            b'<!DOCTYPE html>\n<html><body>Not here</body></html>\n',
            b'{"events": []}',
            b'\x00\xff\xfe garbage',
            feed() + b'\n' + feed(),
            vevent(UID, START).encode(),
            b'UID:evt-Example0001\n' + feed(),
            b'END:VEVENT\n' + feed(),
        ],
        ids=[
            'empty',
            'blank',
            'truncated',
            'html',
            'json',
            'binary',
            'two-calendars',
            'no-calendar',
            'property-outside',
            'end-without-begin',
        ],
    )
    def test_raise_external_service_error(self, data: bytes) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse_calendar(data, tz=LONDON)
        assert excinfo.value.service == 'Luma'

    def test_a_str_that_is_not_unicode_is_unreadable_too(self) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_calendar(feed().decode() + '\ud800', tz=LONDON)

    def test_the_parsers_error_is_kept_as_the_cause(self) -> None:
        with pytest.raises(ExternalServiceError) as excinfo:
            parse_calendar(b'<!DOCTYPE html>', tz=LONDON)
        assert isinstance(excinfo.value.__cause__, ValueError)


class TestCalendarIdFrom:
    @pytest.mark.parametrize(
        'text',
        [
            'cal-ExampleClub001',
            '  cal-ExampleClub001\n',
            '<cal-ExampleClub001>',
            'https://api.lu.ma/ics/get?entity=calendar&id=cal-ExampleClub001',
            'https://api2.luma.com/ics/get?entity=calendar&id=cal-ExampleClub001',
            'webcal://api.lu.ma/ics/get?entity=calendar&id=cal-ExampleClub001',
            'http://lu.ma/ics/get?id=cal-ExampleClub001',
            '<https://api.lu.ma/ics/get?entity=calendar&id=cal-ExampleClub001>',
            'HTTPS://API.LU.MA/ics/get?entity=calendar&id=cal-ExampleClub001',
            'https://api.lu.ma/ics/get?id=evt-1&id=cal-ExampleClub001',
            'https://api.lu.ma/ics/get?entity=calendar&id=cal%2DExampleClub001',
        ],
        ids=[
            'id',
            'whitespace',
            'angle-brackets',
            'ical-link',
            'api2-luma-com',
            'webcal',
            'bare-domain-over-http',
            'link-in-angle-brackets',
            'upper-case-host',
            'second-id',
            'percent-encoded',
        ],
    )
    def test_finds_the_id(self, text: str) -> None:
        assert calendar_id_from(text) == CALENDAR_ID

    @pytest.mark.parametrize(
        'text',
        [
            '',
            'my club',
            'cal-',
            'cal-Example!',
            'cal-Example Club',
            'CAL-ExampleClub001',
            'cal-' + 'a' * 65,
            'evt-Example0001',
            'https://luma.com/example-coding-club',
            'https://api.lu.ma/ics/get?entity=event&id=evt-Example0001',
            'https://example.com/ics/get?entity=calendar&id=cal-ExampleClub001',
            'https://lu.ma.example.com/ics/get?id=cal-ExampleClub001',
            'https://notlu.ma/ics/get?id=cal-ExampleClub001',
            'ftp://api.lu.ma/ics/get?id=cal-ExampleClub001',
            'https://[api.lu.ma/ics/get?id=cal-ExampleClub001',
            'https://api.lu.ma/ics/get#id=cal-ExampleClub001',
        ],
        ids=[
            'empty',
            'words',
            'prefix-only',
            'punctuation',
            'space',
            'upper-case-prefix',
            'too-long',
            'event-id',
            'calendar-page',
            'event-feed',
            'other-site',
            'lookalike-host',
            'suffix-host',
            'other-scheme',
            'broken-host',
            'id-in-fragment',
        ],
    )
    def test_anything_else_says_what_to_give(self, text: str) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            calendar_id_from(text)
        message = str(excinfo.value)
        assert 'cal-' in message
        assert 'Add to calendar' in message


EVENT = LumaEvent(
    luma_id='evt-Example0001',
    name='Intro to DP',
    start=at(2026, 10, 8, 17),
    end=at(2026, 10, 8, 19),
    url='https://luma.com/intro-dp',
    location='Room 101',
)
# SHA-1 of ["Intro to DP",1791478800,1791486000,"https://luma.com/intro-dp","Room 101"]
FINGERPRINT = '927e261e078f4b0e57e055bdc5b8d821f2864817'


class TestLumaEvent:
    def test_times_become_utc_in_whole_seconds(self) -> None:
        event = dataclasses.replace(
            EVENT,
            start=datetime(2026, 10, 8, 18, 0, 0, 999_999, tzinfo=LONDON),
            end=datetime(2026, 10, 8, 20, 0, 30, 1, tzinfo=LONDON),
        )
        assert (event.start, event.end) == (
            at(2026, 10, 8, 17),
            at(2026, 10, 8, 19, 0, 30),
        )
        assert event.start.tzinfo is UTC
        assert event == dataclasses.replace(
            EVENT, start=at(2026, 10, 8, 17), end=at(2026, 10, 8, 19, 0, 30)
        )

    def test_naive_times_are_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(EVENT, start=datetime(2026, 10, 8, 17))
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(EVENT, end=datetime(2026, 10, 8, 19))

    @pytest.mark.parametrize('end', [at(2026, 10, 8, 17), at(2026, 10, 8, 16)])
    def test_an_end_must_be_after_the_start(self, end: datetime) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            dataclasses.replace(EVENT, end=end)

    def test_the_fingerprint_is_stable(self) -> None:
        # Pinned: changing how it is computed makes every stored event look
        # changed once, so change it only on purpose.
        assert EVENT.fingerprint() == FINGERPRINT
        assert dataclasses.replace(EVENT).fingerprint() == FINGERPRINT
        [parsed] = parse_calendar(
            feed(
                vevent(
                    UID,
                    'DTSTART;TZID=Europe/London:20261008T180000',
                    'DTEND:20261008T190000Z',
                    'SUMMARY:Intro to DP',
                    'DESCRIPTION:Get up-to-date information at: '
                    'https://luma.com/intro-dp',
                    'LOCATION:Room 101',
                )
            ),
            tz=LONDON,
        )
        assert parsed.fingerprint() == FINGERPRINT

    @pytest.mark.parametrize(
        'changes',
        [
            {'name': 'Intro to DP!'},
            {'start': at(2026, 10, 8, 16, 59, 59)},
            {'end': at(2026, 10, 8, 19, 0, 1)},
            {'end': None},
            {'url': 'https://luma.com/intro-dp-2'},
            {'location': 'Room 102'},
            {'location': None},
            {'location': ''},
        ],
        ids=lambda changes: '-'.join(f'{k}={v}' for k, v in changes.items()),
    )
    def test_the_fingerprint_changes_with_what_members_see(
        self, changes: dict[str, Any]
    ) -> None:
        changed = dataclasses.replace(EVENT, **changes)
        assert changed.fingerprint() != EVENT.fingerprint()

    def test_the_fingerprint_does_not_depend_on_the_id(self) -> None:
        moved_to_another_id = dataclasses.replace(EVENT, luma_id='evt-Example0002')
        assert moved_to_another_id.fingerprint() == EVENT.fingerprint()

    def test_the_fingerprint_encoding_is_unambiguous(self) -> None:
        # Fields that would join into the same text must not collide.
        first = dataclasses.replace(EVENT, name='Intro', location='to DP')
        second = dataclasses.replace(EVENT, name='Intro to', location='DP')
        third = dataclasses.replace(EVENT, name='Intro","to DP', location=None)
        fingerprints = {e.fingerprint() for e in (first, second, third)}
        assert len(fingerprints) == 3


class FakeLuma:
    """A local stand-in for Luma's feed endpoint, at ``url``.

    It answers every request with the reply set by ``reply``, a 404 until
    then, and records each query.
    """

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []
        self._status = 404
        self._body = b''
        self._content_type = 'text/plain'
        app = web.Application()
        app.router.add_get('/ics/get', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def url(self) -> str:
        return str(self._server.make_url('/ics/get'))

    def reply(
        self,
        body: bytes,
        *,
        content_type: str = 'text/calendar; charset=utf-8',
        status: int = 200,
    ) -> None:
        self._status, self._body, self._content_type = status, body, content_type

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.queries.append(dict(request.query))
        return web.Response(
            status=self._status,
            body=self._body,
            headers={'Content-Type': self._content_type},
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeLuma]:
    site = FakeLuma()
    await site.start()
    monkeypatch.setattr(luma, 'ICS_URL', site.url)
    yield site
    await site.close()


def local_http(clock: FakeClock) -> HttpClient:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    return HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[LumaCalendarClient]:
    http = local_http(clock)
    yield LumaCalendarClient(http, tz=LONDON)
    await http.close()


class TestFetch:
    async def test_returns_the_calendars_events(
        self, site: FakeLuma, client: LumaCalendarClient
    ) -> None:
        site.reply(fixture('calendar.ics'))

        assert await client.fetch(CALENDAR_ID) == EXPECTED_FIXTURE_EVENTS
        assert site.queries == [{'entity': 'calendar', 'id': CALENDAR_ID}]

    async def test_uses_the_clients_time_zone_for_dates(
        self, site: FakeLuma, clock: FakeClock
    ) -> None:
        site.reply(feed(vevent(UID, 'DTSTART;VALUE=DATE:20261008')))
        http = local_http(clock)
        try:
            tokyo = LumaCalendarClient(http, tz=ZoneInfo('Asia/Tokyo'))
            [event] = await tokyo.fetch(CALENDAR_ID)
        finally:
            await http.close()
        assert event.start == at(2026, 10, 7, 15)

    @pytest.mark.parametrize(
        'content_type', ['text/calendar', 'Text/Calendar ; charset=UTF-8']
    )
    async def test_accepts_any_spelling_of_the_calendar_type(
        self, site: FakeLuma, client: LumaCalendarClient, content_type: str
    ) -> None:
        site.reply(feed(vevent(UID, START)), content_type=content_type)
        [event] = await client.fetch(CALENDAR_ID)
        assert event.luma_id == 'evt-Example0001'

    async def test_404_means_no_such_calendar(
        self, site: FakeLuma, client: LumaCalendarClient
    ) -> None:
        site.reply(b'Not found', content_type='text/html', status=404)

        with pytest.raises(CalendarNotFound) as excinfo:
            await client.fetch(CALENDAR_ID)
        assert CALENDAR_ID in str(excinfo.value)
        assert excinfo.value.calendar_id == CALENDAR_ID
        assert isinstance(excinfo.value, KcpcUserError)

    @pytest.mark.parametrize(
        'content_type',
        [
            'text/html; charset=utf-8',
            'application/json',
            'application/octet-stream',
            'text/calendarx',
        ],
    )
    async def test_something_other_than_a_calendar_is_refused(
        self, site: FakeLuma, client: LumaCalendarClient, content_type: str
    ) -> None:
        site.reply(fixture('calendar.ics'), content_type=content_type)

        with pytest.raises(ExternalServiceError) as excinfo:
            await client.fetch(CALENDAR_ID)
        assert str(excinfo.value) == 'Luma sent something other than a calendar feed.'
        assert (excinfo.value.service, excinfo.value.status) == ('Luma', 200)

    async def test_a_calendar_that_cannot_be_read_is_refused(
        self, site: FakeLuma, client: LumaCalendarClient
    ) -> None:
        site.reply(fixture('truncated.ics'))

        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            await client.fetch(CALENDAR_ID)

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (403, r'^Luma returned an error \(HTTP 403\)\.$'),
            (503, '^Luma is not responding right now'),
        ],
    )
    async def test_other_failures_name_luma(
        self, site: FakeLuma, client: LumaCalendarClient, status: int, message: str
    ) -> None:
        site.reply(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch(CALENDAR_ID)
        assert excinfo.value.service == 'Luma'
        assert not isinstance(excinfo.value, CalendarNotFound)


def test_whole_seconds_hold_far_from_1970() -> None:
    # Converting through epoch seconds fails on Windows before 1970 and after
    # 3000; parsed events must not depend on that.
    for year in (1, 1969, 3001, 9998):
        start = datetime(year, 6, 1, 12, 0, 0, 500, tzinfo=UTC)
        event = dataclasses.replace(EVENT, start=start, end=start + timedelta(hours=1))
        assert event.start == start.replace(microsecond=0)
