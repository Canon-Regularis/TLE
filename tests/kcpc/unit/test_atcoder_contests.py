"""Tests for tle.kcpc.platforms.atcoder.contests: reading AtCoder's contest list.

fixtures/atcoder/contests.html is synthetic, but marked up exactly as
https://atcoder.jp/contests/?lang=en is: LF line endings, tab indents,
attributes in single and double quotes, an icon and a mark before each name,
and five contest tables (ongoing, permanent, upcoming, daily and recent), each
in a div whose ID names it.
"""

import dataclasses
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms.atcoder import contests
from tle.kcpc.platforms.atcoder.contests import (
    AtCoderContest,
    AtCoderContestsClient,
    parse_upcoming,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'atcoder'
UNREADABLE = "AtCoder's contest list could not be read."
LOGGER = 'tle.kcpc.platforms.atcoder.contests'
UPCOMING = 'contest-table-upcoming'
ALGORITHM_ICON = '\u24b6'  # a circled A
MARK = '\u25c9'  # coloured by the rated range


def at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def fixture_page() -> str:
    return (FIXTURES / 'contests.html').read_text(encoding='utf-8')


def row(
    contest_id: str = 'abc478',
    name: str = 'AtCoder Beginner Contest 478',
    *,
    start: str = '2026-10-03 21:00:00+0900',
    duration: str = '01:40',
    rated_range: str = ' - 1999',
    kind: str = 'Algorithm',
) -> str:
    """A row of a contest table, marked up as AtCoder's are."""
    return (
        '<tr>\n'
        '<td class="text-center"><a href=\'http://www.timeanddate.com/worldclock/'
        "fixedtime.html?iso=20261003T2100&p1=248' target='blank'>"
        f"<time class='fixtime fixtime-full'>{start}</time></a></td>\n"
        "<td ><span aria-hidden='true' data-toggle='tooltip' data-placement='top' "
        f'title="{kind}">{ALGORITHM_ICON}</span><span class="user-blue">{MARK}</span>'
        f' <a href="/contests/{contest_id}">{name}</a></td>\n'
        f'<td class="text-center">{duration}</td>\n'
        f'<td class="text-center">{rated_range}</td>\n'
        '</tr>\n'
    )


def table(table_id: str, *rows: str) -> str:
    """A contest table's section: a heading, then the table with ``rows``."""
    return (
        f'<div id="{table_id}">\n<h3>Contests</h3>\n'
        '<div class="panel panel-default"><div class="table-responsive">\n'
        '<table class="table">\n<thead>\n<tr>\n'
        '<th>Start Time<small> (local time)</small></th><th>Contest Name</th>'
        '<th>Duration</th><th>Rated Range</th>\n'
        '</tr>\n</thead>\n<tbody>\n' + ''.join(rows) + '</tbody>\n</table>\n'
        '</div></div>\n</div>\n'
    )


def page(*tables: str) -> str:
    """A contest list page with ``tables``."""
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<title>Present Contests - AtCoder</title>\n'
        '</head>\n<body>\n<div class="col-lg-9 col-md-8">\n'
        + '<hr>\n'.join(tables)
        + '</div>\n</body>\n</html>\n'
    )


def upcoming_page(*rows: str) -> str:
    """A contest list whose upcoming table has ``rows``, then a recent contest."""
    recent = row(
        'abc477', 'AtCoder Beginner Contest 477', start='2026-09-26 21:00:00+0900'
    )
    return page(table(UPCOMING, *rows), table('contest-table-recent', recent))


def parse_one(contest_row: str) -> AtCoderContest:
    """The one contest of an upcoming table with ``contest_row``."""
    [contest] = parse_upcoming(upcoming_page(contest_row))
    return contest


def ids(found: list[AtCoderContest]) -> list[str]:
    return [contest.contest_id for contest in found]


def contest(
    contest_id: str,
    name: str,
    start: datetime,
    length: timedelta,
    *,
    kind: str = 'Algorithm',
    rated_range: str,
) -> AtCoderContest:
    return AtCoderContest(
        contest_id=contest_id,
        name=name,
        start=start,
        end=start + length,
        url=f'https://atcoder.jp/contests/{contest_id}',
        kind=kind,
        rated_range=rated_range,
    )


ABC_LENGTH = timedelta(hours=1, minutes=40)
EXPECTED_FIXTURE_CONTESTS = [
    contest(
        'abc478',
        'AtCoder Beginner Contest 478',
        at(2026, 10, 3, 12),
        ABC_LENGTH,
        rated_range='- 1999',
    ),
    contest(
        'arc231',
        'AtCoder Regular Contest 231',
        at(2026, 10, 4, 12),
        timedelta(hours=2),
        rated_range='1200 - 2799',
    ),
    contest(
        'ahc073',
        'AtCoder Heuristic Contest 073',
        at(2026, 10, 10, 6),
        timedelta(hours=4),
        kind='Heuristic',
        rated_range='All',
    ),
    # An &amp; in the markup, and full-width brackets.
    contest(
        'abc479',
        'Example & Co. Programming Contest 2026'
        '\uff08AtCoder Beginner Contest 479\uff09',
        at(2026, 10, 11, 12),
        ABC_LENGTH,
        rated_range='- 1999',
    ),
    contest(
        'agc075',
        'AtCoder Grand Contest 075',
        at(2026, 10, 18, 12),
        timedelta(hours=3),
        rated_range='1200 -',
    ),
    contest(
        'example-univ-2026',
        'Example University Programming Contest 2026 (unrated)',
        at(2026, 10, 24, 4),
        timedelta(hours=5),
        rated_range='-',
    ),
    # Ten days long: its duration reads 240:00.
    contest(
        'ahc074',
        'Example Heuristic Challenge 2027 (AtCoder Heuristic Contest 074)',
        at(2026, 10, 30, 10),
        timedelta(days=10),
        kind='Heuristic',
        rated_range='All',
    ),
]


class TestTheFixturePage:
    def test_parses_to_exactly_these_contests(self) -> None:
        # Not the ongoing, permanent, daily or recent contests.
        assert parse_upcoming(fixture_page()) == EXPECTED_FIXTURE_CONTESTS

    def test_is_marked_up_like_atcoders_page(self) -> None:
        # Guards the fixture: an editor or a git setting could quietly change
        # what the tests above read.
        data = (FIXTURES / 'contests.html').read_bytes()
        assert b'\r' not in data
        assert b'\n\t\t\t<tr>\n\t\t\t\t<td class="text-center"><a href=\'' in data
        assert b"data-toggle='tooltip' data-placement='top' title=\"Heuristic\"" in data
        assert b'<td class="text-center">240:00</td>' in data
        for table_id in ('action', 'permanent', 'upcoming', 'daily', 'recent'):
            assert f'<div id="contest-table-{table_id}">'.encode() in data

    def test_crlf_line_endings_read_the_same(self) -> None:
        crlf = fixture_page().replace('\n', '\r\n')
        assert parse_upcoming(crlf) == EXPECTED_FIXTURE_CONTESTS

    def test_without_its_upcoming_table_it_has_no_upcoming_contests(self) -> None:
        html = fixture_page()
        start = html.index(f'<div id="{UPCOMING}">')
        end = html.index('<div id="contest-table-daily">')
        assert parse_upcoming(html[:start] + html[end:]) == []


class TestRows:
    def test_a_row_becomes_a_contest(self) -> None:
        assert parse_one(row()) == AtCoderContest(
            contest_id='abc478',
            name='AtCoder Beginner Contest 478',
            start=at(2026, 10, 3, 12),
            end=at(2026, 10, 3, 13, 40),
            url='https://atcoder.jp/contests/abc478',
            kind='Algorithm',
            rated_range='- 1999',
        )

    @pytest.mark.parametrize(
        ('duration', 'length'),
        [
            ('01:40', timedelta(hours=1, minutes=40)),
            ('1:40', timedelta(hours=1, minutes=40)),
            ('00:01', timedelta(minutes=1)),
            ('24:00', timedelta(days=1)),
            ('239:50', timedelta(days=9, hours=23, minutes=50)),
            ('240:00', timedelta(days=10)),
            (' 240:00\n', timedelta(days=10)),
        ],
    )
    def test_durations_may_pass_a_day(self, duration: str, length: timedelta) -> None:
        found = parse_one(row(duration=duration))
        assert found.end - found.start == length

    def test_a_heuristic_contest(self) -> None:
        found = parse_one(
            row('ahc074', 'AtCoder Heuristic Contest 074', kind='Heuristic')
        )
        assert (found.contest_id, found.kind) == ('ahc074', 'Heuristic')

    def test_the_kind_is_the_first_tooltip_in_the_name_cell(self) -> None:
        untitled = row().replace('title="Algorithm"', '')
        assert parse_one(untitled).kind == ''
        spaced = row(kind='  Heuristic \n')
        assert parse_one(spaced).kind == 'Heuristic'
        second = row().replace('class="user-blue"', 'class="user-blue" title="Rated"')
        assert parse_one(second).kind == 'Algorithm'

    @pytest.mark.parametrize(
        ('rated_range', 'shown'),
        [
            (' - 1999', '- 1999'),
            ('1200 - 2799', '1200 - 2799'),
            ('1200 - ', '1200 -'),
            ('All', 'All'),
            ('-', '-'),
            ('', ''),
            ('\n\t\t - 1999\n', '- 1999'),
        ],
    )
    def test_the_rated_range_as_shown(self, rated_range: str, shown: str) -> None:
        assert parse_one(row(rated_range=rated_range)).rated_range == shown

    @pytest.mark.parametrize(
        ('start', 'utc'),
        [
            ('2026-10-03 21:00:00+0900', at(2026, 10, 3, 12)),
            ('2026-10-03 21:00:00+09:00', at(2026, 10, 3, 12)),
            ('2026-10-03 21:00:00+0000', at(2026, 10, 3, 21)),
            ('2026-10-03 21:00:00-0530', at(2026, 10, 4, 2, 30)),
            ('2026-10-03 21:00:59+0900', at(2026, 10, 3, 12, 0, 59)),
            ('2026-10-04 06:00:00+0900', at(2026, 10, 3, 21)),
            ('Sat 2026-10-03 21:00:00+0900 (JST)', at(2026, 10, 3, 12)),
        ],
    )
    def test_the_start_in_utc(self, start: str, utc: datetime) -> None:
        found = parse_one(row(start=start))
        assert found.start == utc
        assert found.start.tzinfo is UTC

    @pytest.mark.parametrize(
        ('name', 'shown'),
        [
            ('AtCoder Beginner Contest 478', 'AtCoder Beginner Contest 478'),
            ('  AtCoder\n\tBeginner  Contest 478 ', 'AtCoder Beginner Contest 478'),
            ('Example &amp; Co. &lt;Final&gt;', 'Example & Co. <Final>'),
            ('Example <b>Bold</b> Contest', 'Example Bold Contest'),
            ('', 'abc478'),
            (' \n ', 'abc478'),
        ],
        ids=['plain', 'whitespace', 'entities', 'markup', 'empty', 'blank'],
    )
    def test_the_name_is_the_links_text(self, name: str, shown: str) -> None:
        assert parse_one(row(name=name)).name == shown

    @pytest.mark.parametrize(
        ('href', 'contest_id'),
        [
            ('/contests/abc478', 'abc478'),
            ('/contests/abc478/', 'abc478'),
            ('https://atcoder.jp/contests/abc478', 'abc478'),
            ('https://www.atcoder.jp/contests/abc478?lang=en', 'abc478'),
            ('/contests/jsc2026-final', 'jsc2026-final'),
            ('/contests/past_202610_open', 'past_202610_open'),
        ],
    )
    def test_the_id_is_in_the_contests_link(self, href: str, contest_id: str) -> None:
        found = parse_one(row().replace('href="/contests/abc478"', f'href="{href}"'))
        assert found.contest_id == contest_id
        assert found.url == f'https://atcoder.jp/contests/{contest_id}'

    def test_rows_keep_the_pages_order(self) -> None:
        found = parse_upcoming(
            upcoming_page(
                row('abc479', start='2026-10-11 21:00:00+0900'),
                row('abc478', start='2026-10-03 21:00:00+0900'),
            )
        )
        assert ids(found) == ['abc479', 'abc478']

    def test_the_first_row_for_a_contest_is_kept(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        found = parse_upcoming(
            upcoming_page(
                row('abc478', 'First'),
                row('arc231', 'AtCoder Regular Contest 231'),
                row('abc478', 'Second'),
            )
        )
        assert [(c.contest_id, c.name) for c in found] == [
            ('abc478', 'First'),
            ('arc231', 'AtCoder Regular Contest 231'),
        ]
        assert [r.levelno for r in caplog.records if r.name == LOGGER] == [
            logging.DEBUG
        ]

    def test_only_the_upcoming_table_is_read(self) -> None:
        html = page(
            table(
                'contest-table-action', row('ahc072', start='2026-09-25 19:10:00+0900')
            ),
            table('contest-table-permanent', row('practice')),
            table(UPCOMING, row('abc478')),
            table('contest-table-daily', row('awc0172')),
            table('contest-table-recent', row('abc477')),
        )
        assert ids(parse_upcoming(html)) == ['abc478']

    @pytest.mark.parametrize(
        'html',
        [
            upcoming_page(row()).replace(f'id="{UPCOMING}"', f"id='{UPCOMING}'"),
            upcoming_page(row()).replace(f'id="{UPCOMING}"', f'id={UPCOMING}'),
            upcoming_page(row().replace('"', "'")),
            upcoming_page(
                row()
                .replace('href="/contests/abc478"', 'href=/contests/abc478')
                .replace('title="Algorithm"', 'title=Algorithm')
            ),
            upcoming_page(row().replace('title=', 'TITLE=').replace('href=', 'HREF=')),
        ],
        ids=['single', 'unquoted-id', 'all-single', 'unquoted', 'upper-case'],
    )
    def test_any_attribute_quoting_reads_the_same(self, html: str) -> None:
        assert parse_upcoming(html) == [parse_one(row())]

    def test_end_tags_that_html_lets_pages_leave_out(self) -> None:
        unclosed = row().replace('</td>', '').replace('</tr>', '')
        found = parse_upcoming(upcoming_page(unclosed, unclosed.replace('478', '479')))
        assert ids(found) == ['abc478', 'abc479']

    def test_a_section_ends_with_its_own_end_tag(self) -> None:
        # The upcoming section holds divs of its own; the rows after it, in
        # the next section, are not upcoming contests.
        html = page(
            '<div id="contest-table-upcoming"><div><div>\n<table>'
            + row('abc478')
            + '</table></div></div></div>\n<div><table>'
            + row('abc477')
            + '</table></div>'
        )
        assert ids(parse_upcoming(html)) == ['abc478']


UNREADABLE_ROWS = {
    'three-cells': row().replace('<td class="text-center"> - 1999</td>\n', ''),
    'no-link': row().replace('<a href="/contests/abc478">', '<a>'),
    'no-contest-link': row().replace('/contests/abc478', '/contests/abc478/tasks'),
    'other-site': row().replace(
        '/contests/abc478', 'https://example.com/contests/abc478'
    ),
    'bad-link': row().replace(
        '/contests/abc478', 'https://[atcoder.jp/contests/abc478'
    ),
    'no-start': row(start=''),
    'no-offset': row(start='2026-10-03 21:00:00'),
    'no-seconds': row(start='2026-10-03 21:00+0900'),
    'no-such-day': row(start='2026-02-30 21:00:00+0900'),
    'no-such-hour': row(start='2026-10-03 24:00:00+0900'),
    'before-year-1': row(start='0001-01-01 08:00:00+0900'),
    'after-9999': row(start='9999-12-31 23:00:00-0900'),
    'no-duration': row(duration=''),
    'dash': row(duration='-'),
    'zero': row(duration='00:00'),
    'minutes-only': row(duration='100'),
    '60-minutes': row(duration='01:60'),
    'one-digit-minutes': row(duration='01:4'),
    'with-seconds': row(duration='01:40:00'),
    'days': row(duration='1d 01:40'),
    'ends-after-9999': row(start='9999-12-31 21:00:00+0900', duration='240:00'),
}


class TestUnreadableRows:
    @pytest.mark.parametrize('bad_row', UNREADABLE_ROWS.values(), ids=UNREADABLE_ROWS)
    def test_are_skipped(self, bad_row: str) -> None:
        found = parse_upcoming(upcoming_page(bad_row, row('arc231')))
        assert ids(found) == ['arc231']

    def test_are_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        parse_upcoming(upcoming_page(*UNREADABLE_ROWS.values(), row('arc231')))
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * len(UNREADABLE_ROWS)

    def test_heading_rows_are_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        assert ids(parse_upcoming(upcoming_page(row()))) == ['abc478']
        assert [r for r in caplog.records if r.name == LOGGER] == []

    def test_a_table_of_only_unreadable_rows_is_an_unreadable_list(self) -> None:
        # Its layout must have changed: taking it for an empty table would
        # make every AtCoder contest look cancelled.
        html = upcoming_page(UNREADABLE_ROWS['no-offset'], UNREADABLE_ROWS['dash'])
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_upcoming(html)


class TestPagesWithoutUpcomingContests:
    def test_other_contest_tables_but_no_upcoming_table(self) -> None:
        html = page(
            table('contest-table-permanent', row('practice')),
            table('contest-table-recent', row('abc477')),
        )
        assert parse_upcoming(html) == []

    def test_an_upcoming_table_without_rows(self) -> None:
        assert parse_upcoming(upcoming_page()) == []

    @pytest.mark.parametrize(
        'html',
        [
            '',
            '\n\n',
            'Not Found',
            '{"contests": []}',
            '<!DOCTYPE html>\n<html><head><title>Maintenance - AtCoder</title></head>'
            '<body><p>AtCoder is under maintenance.</p></body></html>\n',
            # The archive of past contests: a table without the sections.
            page('<div class="table-responsive"><table>' + row() + '</table></div>'),
            page(table('contest-table', row())),
            page(table('upcoming', row())),
            '<!-- <div id="contest-table-upcoming"> -->',
            '<script>$("#contest-table-upcoming").show();</script>',
        ],
        ids=[
            'empty',
            'blank',
            'text',
            'json',
            'maintenance',
            'archive',
            'no-suffix',
            'no-prefix',
            'comment',
            'script',
        ],
    )
    def test_a_page_that_is_not_the_contest_list_is_unreadable(self, html: str) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse_upcoming(html)
        assert excinfo.value.service == 'AtCoder'


CONTEST = EXPECTED_FIXTURE_CONTESTS[0]
TOKYO = ZoneInfo('Asia/Tokyo')


class TestAtCoderContest:
    def test_times_become_utc_in_whole_seconds(self) -> None:
        found = dataclasses.replace(
            CONTEST,
            start=datetime(2026, 10, 3, 21, 0, 0, 999_999, tzinfo=TOKYO),
            end=datetime(2026, 10, 3, 22, 40, 30, 1, tzinfo=TOKYO),
        )
        assert (found.start, found.end) == (
            at(2026, 10, 3, 12),
            at(2026, 10, 3, 13, 40, 30),
        )
        assert found.start.tzinfo is UTC
        assert found.end.tzinfo is UTC

    def test_naive_times_are_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, start=datetime(2026, 10, 3, 12))
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, end=datetime(2026, 10, 3, 13, 40))

    @pytest.mark.parametrize(
        'end', [at(2026, 10, 3, 12), at(2026, 10, 3, 11)], ids=['at', 'before']
    )
    def test_the_end_must_be_after_the_start(self, end: datetime) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            dataclasses.replace(CONTEST, end=end)


class FakeAtCoder:
    """A local stand-in for AtCoder's contest list, at ``url``.

    It answers every request with the reply set by ``reply``, a 404 until
    then, and records each query.
    """

    def __init__(self) -> None:
        self.queries: list[dict[str, str]] = []
        self._status = 404
        self._body = b''
        self._content_type = 'text/plain'
        app = web.Application()
        app.router.add_get('/contests/', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def url(self) -> str:
        return str(self._server.make_url('/contests/'))

    def reply(
        self,
        body: bytes,
        *,
        content_type: str = 'text/html; charset=utf-8',
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
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeAtCoder]:
    site = FakeAtCoder()
    await site.start()
    monkeypatch.setattr(contests, 'CONTESTS_URL', site.url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[AtCoderContestsClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield AtCoderContestsClient(http)
    await http.close()


class TestFetchUpcoming:
    async def test_reads_the_contest_list_in_english(
        self, site: FakeAtCoder, client: AtCoderContestsClient
    ) -> None:
        site.reply((FIXTURES / 'contests.html').read_bytes())

        assert await client.fetch_upcoming() == EXPECTED_FIXTURE_CONTESTS
        assert site.queries == [{'lang': 'en'}]

    async def test_decodes_the_page_by_its_charset(
        self, site: FakeAtCoder, client: AtCoderContestsClient
    ) -> None:
        name = 'Example \u30b3\u30f3\u30c6\u30b9\u30c8 2026'  # Japanese for "contest"
        # Shift_JIS has no circled A or mark, so the rows go without them.
        html = upcoming_page(row(name=name))
        html = html.replace(ALGORITHM_ICON, 'A').replace(MARK, '*')
        site.reply(
            html.encode('shift_jis'), content_type='text/html; charset=Shift_JIS'
        )

        [found] = await client.fetch_upcoming()
        assert found.name == name

    async def test_a_page_that_is_not_the_contest_list_is_refused(
        self, site: FakeAtCoder, client: AtCoderContestsClient
    ) -> None:
        site.reply(b'<html><body>AtCoder is under maintenance.</body></html>')

        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            await client.fetch_upcoming()
        assert excinfo.value.service == 'AtCoder'

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (403, r'^AtCoder returned an error \(HTTP 403\)\.$'),
            (404, r'^AtCoder returned an error \(HTTP 404\)\.$'),
            (503, '^AtCoder is not responding right now'),
        ],
    )
    async def test_failures_name_atcoder(
        self,
        site: FakeAtCoder,
        client: AtCoderContestsClient,
        status: int,
        message: str,
    ) -> None:
        site.reply(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch_upcoming()
        assert (excinfo.value.service, excinfo.value.status) == ('AtCoder', status)
