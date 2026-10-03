"""Tests for tle.kcpc.platforms.icpc: reading and fetching ICPC contests.

fixtures/icpc/ukiepc.json is laid out exactly as icpc.global's answer for
UKIEPC is (compact JSON, its keys in the same order, dates as UTC midnights,
``timezone`` null), with made-up sites and addresses.
"""

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms import icpc
from tle.kcpc.platforms.icpc import IcpcClient, IcpcContest, parse_contest

FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures' / 'icpc' / 'ukiepc.json'
CODE = 'UKIEPC'
UNREADABLE = "ICPC's details of the contest UKIEPC could not be read."

EXPECTED_FIXTURE_CONTEST = IcpcContest(
    code=CODE,
    contest_id='9584',
    name='The 2026 ICPC UK & Ireland Programming Contest',
    start_date=date(2026, 10, 17),
    end_date=date(2026, 10, 17),
    url='https://ukiepc.info',
)


def fixture_data() -> dict[str, Any]:
    data = json.loads(FIXTURE.read_bytes())
    assert isinstance(data, dict)
    return data


def parse_with(**changes: Any) -> IcpcContest:
    """The fixture's contest, parsed with ``changes`` to its JSON.

    A change to ``...`` (Ellipsis) leaves that key out.
    """
    data = fixture_data()
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    return parse_contest(CODE, data)


class TestTheFixture:
    def test_parses_to_this_contest(self) -> None:
        assert parse_contest(CODE, fixture_data()) == EXPECTED_FIXTURE_CONTEST

    def test_is_laid_out_like_icpc_globals_answers(self) -> None:
        # Guards the fixture: an editor or a git setting could quietly change
        # what the tests above read.
        raw = FIXTURE.read_bytes()
        assert b'\n' not in raw and b'": ' not in raw  # compact, on one line
        assert raw.startswith(b'{"abbr":"UKIEPC","activeSites":[{"id":')
        assert b'"startDate":"2026-10-17T00:00:00.000Z"' in raw
        assert b'"timezone":null' in raw
        assert list(fixture_data()) == sorted(fixture_data())  # keys in order


class TestParseContest:
    @pytest.mark.parametrize(
        ('start_date', 'expected'),
        [
            ('2026-10-17T00:00:00.000Z', date(2026, 10, 17)),
            # As written: not moved to UTC or to any other zone.
            ('2026-10-17T23:30:00.000-05:00', date(2026, 10, 17)),
            ('2026-10-17T00:30:00.000+09:00', date(2026, 10, 17)),
            ('2026-10-17', date(2026, 10, 17)),
            ('2026-10-17 09:00', date(2026, 10, 17)),
            ('  2026-10-17T00:00:00Z\n', date(2026, 10, 17)),
        ],
    )
    def test_the_start_date_is_the_date_as_written(
        self, start_date: str, expected: date
    ) -> None:
        # The end date too stays on or after it.
        contest = parse_with(startDate=start_date, endDate=start_date)
        assert (contest.start_date, contest.end_date) == (expected, expected)

    @pytest.mark.parametrize(
        'start_date',
        [
            ...,
            None,
            '',
            '17/10/2026',
            '20261017',
            '2026-10-17X00:00:00Z',
            '2026-10-1',
            '2026-02-30T00:00:00.000Z',
            '\uff12\uff10\uff12\uff16-10-17',  # full-width digits
            1792195200,
            ['2026-10-17'],
        ],
        ids=[
            'missing',
            'null',
            'empty',
            'day-first',
            'basic-format',
            'bad-separator',
            'short-day',
            'no-such-day',
            'full-width',
            'number',
            'list',
        ],
    )
    def test_a_missing_or_unreadable_start_date_is_unreadable(
        self, start_date: object
    ) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse_with(startDate=start_date)
        assert excinfo.value.service == 'ICPC'

    @pytest.mark.parametrize(
        ('end_date', 'expected'),
        [
            ('2026-10-17T00:00:00.000Z', date(2026, 10, 17)),
            ('2026-10-18T00:00:00.000Z', date(2026, 10, 18)),
            ('2026-10-16T00:00:00.000Z', None),  # before the start
            (..., None),
            (None, None),
            ('', None),
            ('2026-02-30T00:00:00.000Z', None),
            (20261017, None),
        ],
        ids=[
            'same-day',
            'next-day',
            'before',
            'missing',
            'null',
            'empty',
            'bad',
            'int',
        ],
    )
    def test_the_end_date(self, end_date: object, expected: date | None) -> None:
        assert parse_with(endDate=end_date).end_date == expected

    @pytest.mark.parametrize(
        ('name', 'expected'),
        [
            (
                '  The 2026 ICPC UK & Ireland Programming Contest \n',
                EXPECTED_FIXTURE_CONTEST.name,
            ),
            ('', CODE),
            ('   ', CODE),
            (None, CODE),
            (..., CODE),
            (42, CODE),
        ],
        ids=['trimmed', 'empty', 'blank', 'null', 'missing', 'number'],
    )
    def test_the_name_or_else_the_code(self, name: object, expected: str) -> None:
        assert parse_with(name=name).name == expected

    @pytest.mark.parametrize(
        ('homepage', 'url'),
        [
            ('https://ukiepc.info', 'https://ukiepc.info'),
            ('  https://ukiepc.info/ \n', 'https://ukiepc.info/'),
            ('http://example.org/contest', 'http://example.org/contest'),
            ('HTTPS://EXAMPLE.ORG', 'HTTPS://EXAMPLE.ORG'),
            ('', 'https://icpc.global/'),
            (None, 'https://icpc.global/'),
            (..., 'https://icpc.global/'),
            ('ukiepc.info', 'https://icpc.global/'),
            ('ftp://example.org/contest', 'https://icpc.global/'),
            ('javascript:alert(1)', 'https://icpc.global/'),
            ('https://', 'https://icpc.global/'),
            ('https://example.org/a contest', 'https://icpc.global/'),
            ('https://[example.org', 'https://icpc.global/'),
            (['https://ukiepc.info'], 'https://icpc.global/'),
        ],
        ids=[
            'homepage',
            'trimmed',
            'http',
            'upper-case',
            'empty',
            'null',
            'missing',
            'no-scheme',
            'ftp',
            'javascript',
            'no-host',
            'space',
            'broken-host',
            'list',
        ],
    )
    def test_the_link_is_the_homepage_or_else_icpc_global(
        self, homepage: object, url: str
    ) -> None:
        assert parse_with(homepage=homepage).url == url

    def test_the_id_becomes_text(self) -> None:
        assert parse_with(id=12345).contest_id == '12345'

    @pytest.mark.parametrize(
        'contest_id',
        [..., None, '9584', 9584.0, True, [9584]],
        ids=['missing', 'null', 'text', 'float', 'bool', 'list'],
    )
    def test_a_contest_without_a_numeric_id_is_unreadable(
        self, contest_id: object
    ) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_with(id=contest_id)

    @pytest.mark.parametrize(
        'data',
        [None, [], [fixture_data()], 'UKIEPC', 9584, True],
        ids=['null', 'empty-list', 'list', 'text', 'number', 'bool'],
    )
    def test_anything_but_an_object_is_unreadable(self, data: object) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_contest(CODE, data)

    def test_the_code_is_kept_as_given(self) -> None:
        contest = parse_contest('Northwestern-Europe-2027', fixture_data())
        assert contest.code == 'Northwestern-Europe-2027'

    def test_the_error_names_the_code(self) -> None:
        with pytest.raises(ExternalServiceError) as excinfo:
            parse_contest('Northwestern-Europe-2027', [])
        assert 'Northwestern-Europe-2027' in str(excinfo.value)


class FakeIcpc:
    """A local stand-in for icpc.global's public contest API.

    It answers every request with the reply set by ``reply``, a 404 until
    then, and records the raw path of each request.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []
        self._status = 404
        self._body = b''
        self._content_type = 'application/json'
        app = web.Application()
        app.router.add_get('/{tail:.*}', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def api_url(self) -> str:
        return str(self._server.make_url('/api/contest/public/')) + '{code}'

    def reply(
        self,
        body: bytes,
        *,
        content_type: str = 'application/json',
        status: int = 200,
    ) -> None:
        self._status, self._body, self._content_type = status, body, content_type

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.paths.append(request.raw_path)
        return web.Response(
            status=self._status,
            body=self._body,
            headers={'Content-Type': self._content_type},
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeIcpc]:
    site = FakeIcpc()
    await site.start()
    monkeypatch.setattr(icpc, 'API_URL', site.api_url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[IcpcClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield IcpcClient(http)
    await http.close()


class TestFetch:
    async def test_returns_the_contest(
        self, site: FakeIcpc, client: IcpcClient
    ) -> None:
        site.reply(FIXTURE.read_bytes())

        assert await client.fetch(CODE) == EXPECTED_FIXTURE_CONTEST
        assert site.paths == ['/api/contest/public/UKIEPC']

    async def test_the_code_is_quoted_into_the_path(
        self, site: FakeIcpc, client: IcpcClient
    ) -> None:
        site.reply(FIXTURE.read_bytes())

        contest = await client.fetch('UK/IE PC?')
        assert contest is not None and contest.code == 'UK/IE PC?'
        assert site.paths == ['/api/contest/public/UK%2FIE%20PC%3F']

    async def test_404_means_no_such_contest(
        self, site: FakeIcpc, client: IcpcClient
    ) -> None:
        site.reply(b'{"status":404,"error":"Not Found"}', status=404)
        assert await client.fetch('NO-SUCH-CONTEST') is None

    @pytest.mark.parametrize(
        'body',
        [b'', b'<!DOCTYPE html><html></html>', b'{"id": 9584', b'\xff\xfe\x00'],
        ids=['empty', 'html', 'truncated', 'binary'],
    )
    async def test_a_body_that_is_not_json_is_unreadable(
        self, site: FakeIcpc, client: IcpcClient, body: bytes
    ) -> None:
        site.reply(body)

        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            await client.fetch(CODE)
        assert (excinfo.value.service, excinfo.value.status) == ('ICPC', 200)
        assert isinstance(excinfo.value.__cause__, ValueError)

    async def test_json_that_is_not_a_contest_is_unreadable(
        self, site: FakeIcpc, client: IcpcClient
    ) -> None:
        site.reply(b'{"abbr":"UKIEPC"}')

        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            await client.fetch(CODE)

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (403, r'^ICPC returned an error \(HTTP 403\)\.$'),
            (500, '^ICPC is not responding right now'),
        ],
    )
    async def test_other_failures_name_icpc(
        self, site: FakeIcpc, client: IcpcClient, status: int, message: str
    ) -> None:
        site.reply(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch(CODE)
        assert (excinfo.value.service, excinfo.value.status) == ('ICPC', status)
