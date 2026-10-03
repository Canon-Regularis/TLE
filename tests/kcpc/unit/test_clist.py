"""Tests for tle.kcpc.platforms.clist: reading clist.by's contest list.

fixtures/clist/codechef-1.json and codechef-2.json are laid out exactly as
clist.by's answers are (one line of JSON, the keys in the same order, times in
UTC with no zone), with made-up contests. The first page links to the second
although it isn't full, as clist.by's pages can. The resources in its list of
them have the keys clist.by's have, with made-up counts. The credentials are
made up.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms import clist
from tle.kcpc.platforms.clist import ClistClient, ClistContest, parse_contest

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'clist'
FIRST_PAGE = FIXTURES / 'codechef-1.json'
SECOND_PAGE = FIXTURES / 'codechef-2.json'
LOGGER = 'tle.kcpc.platforms.clist'
USERNAME = 'test-user'
API_KEY = 'test-key'
REFUSED = 'clist.by refused the API key. Check CLIST_USERNAME and CLIST_API_KEY.'
UNREADABLE = "clist.by's contest list could not be read."
NO_SUCH_SITE = 'clist.by has no site named codechef.com.'
FIELDS = [
    'duration',
    'end',
    'event',
    'host',
    'href',
    'id',
    'n_problems',
    'n_statistics',
    'parsed_at',
    'problems',
    'resource',
    'resource_id',
    'start',
]
META = ['estimated_count', 'limit', 'next', 'offset', 'previous', 'total_count']


def codechef(
    contest_id: int, name: str, code: str, day: int, hour: int
) -> ClistContest:
    start = datetime(2026, 10, day, hour, 30, tzinfo=UTC)
    return ClistContest(
        clist_id=contest_id,
        resource='codechef.com',
        name=name,
        start=start,
        end=start.replace(hour=16),
        url=f'https://www.codechef.com/{code}',
    )


STARTERS_210 = codechef(70900001, 'Starters 210 (Rated)', 'START210', 7, 14)
MONDAY_MUNCH = codechef(
    70900002, 'Monday Munch - DSA Challenge 024 (Rated)', 'DSAMONDAY024', 12, 13
)
STARTERS_211 = codechef(70900003, 'Starters 211 (Rated)', 'START211', 14, 14)


def fixture(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_bytes())
    assert isinstance(data, dict)
    return data


def first_object(**changes: Any) -> dict[str, Any]:
    """The first contest in the first page, with ``changes``.

    A change to ``...`` (Ellipsis) leaves that key out.
    """
    data: dict[str, Any] = fixture(FIRST_PAGE)['objects'][0]
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    return data


def resource_object(name: str, resource_id: int) -> dict[str, Any]:
    """A resource as clist.by's list of resources has it."""
    return {
        'icon': f'img/resources/{name}.png',
        'id': resource_id,
        'n_accounts': 1000,
        'n_contests': 100,
        'name': name,
        'short': name.split('.')[0],
    }


def page(*objects: object, next_page: str | None = None, offset: int = 0) -> bytes:
    """A page of one of clist.by's lists, as JSON."""
    meta = {
        'estimated_count': len(objects),
        'limit': 100,
        'next': next_page,
        'offset': offset,
        'previous': None,
        'total_count': None,
    }
    return json.dumps({'meta': meta, 'objects': list(objects)}).encode()


def link(offset: int) -> str:
    """meta.next for the page at ``offset``, as clist.by writes it."""
    return (
        '/api/v4/contest/?upcoming=true&resource=codechef.com&order_by=start'
        f'&limit=100&offset={offset}'
    )


class TestTheFixtures:
    @pytest.mark.parametrize('path', [FIRST_PAGE, SECOND_PAGE], ids=['1', '2'])
    def test_are_laid_out_like_clist_bys_answers(self, path: Path) -> None:
        # Guards the fixtures: an editor or a git setting could quietly change
        # them. One line, json.dumps' separators, no newline at the end.
        data = fixture(path)
        assert path.read_bytes() == json.dumps(data).encode()
        assert list(data) == ['meta', 'objects']
        assert list(data['meta']) == META
        assert [list(item) for item in data['objects']] == [FIELDS] * len(
            data['objects']
        )

    def test_the_first_page_links_to_the_second(self) -> None:
        assert fixture(FIRST_PAGE)['meta']['next'] == link(100)
        assert fixture(SECOND_PAGE)['meta']['next'] is None


class TestParseContest:
    def test_reads_a_contest(self) -> None:
        assert parse_contest(first_object()) == STARTERS_210

    @pytest.mark.parametrize(
        ('written', 'expected'),
        [
            ('2026-10-03T12:00:00', datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
            ('2026-10-03T21:00:00+09:00', datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
            ('2026-10-03T12:00:00Z', datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
            (' 2026-10-03T12:00:00.750 ', datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
        ],
        ids=['no zone', 'offset', 'Z', 'fraction'],
    )
    def test_times_are_utc_in_whole_seconds(
        self, written: str, expected: datetime
    ) -> None:
        contest = parse_contest(first_object(start=written, end='2026-10-03T14:00:00'))

        assert contest is not None
        assert contest.start == expected
        assert contest.start.tzinfo is UTC

    @pytest.mark.parametrize('key', ['id', 'event', 'start', 'end', 'href'])
    @pytest.mark.parametrize('value', [..., None], ids=['missing', 'null'])
    def test_skips_a_contest_missing_a_field(
        self, caplog: pytest.LogCaptureFixture, key: str, value: object
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert parse_contest(first_object(**{key: value})) is None

        (record,) = caplog.records
        assert record.levelno == logging.DEBUG
        assert record.getMessage().endswith(f'which has no {key}')

    @pytest.mark.parametrize(
        'changes',
        [
            {'id': '70900001'},
            {'id': True},
            {'id': 70900001.5},
            {'event': '   '},
            {'event': 210},
            {'start': 'next Saturday'},
            {'start': '2026-02-30T14:30:00'},
            {'start': 1791642600},
            {'end': '2026-10-07T14:30:00'},  # when it starts
            {'end': '2026-10-07T14:00:00'},  # before it starts
            {'href': 'www.codechef.com/START210'},
            {'href': 'ftp://www.codechef.com/START210'},
            {'href': 'https://www.codechef.com/START 210'},
            {'href': ''},
        ],
    )
    def test_skips_a_contest_it_cannot_read(
        self, caplog: pytest.LogCaptureFixture, changes: dict[str, object]
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert parse_contest(first_object(**changes)) is None

        (record,) = caplog.records
        assert record.levelno == logging.DEBUG
        assert 'which can not be read' in record.getMessage()

    @pytest.mark.parametrize('data', [None, [], 'Starters 210', 70900001])
    def test_skips_anything_but_an_object(self, data: object) -> None:
        assert parse_contest(data) is None

    def test_a_contest_without_a_resource_has_a_blank_one(self) -> None:
        contest = parse_contest(first_object(resource=...))

        assert contest is not None and contest.resource == ''

    def test_a_contest_must_end_after_it_starts(self) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            ClistContest(
                1,
                'codechef.com',
                'x',
                STARTERS_210.end,
                STARTERS_210.start,
                'https://x',
            )


class FakeClist:
    """A local stand-in for clist.by's contest API.

    It answers a request for the page at ``offset`` of a contest list with
    ``pages[offset]``, a status and a body, or a 404 if there is none, and a
    request for its list of resources with ``sites``, whatever the query. It
    records each request: its path with the query, and its Authorization
    header.
    """

    def __init__(self) -> None:
        self.pages: dict[int, tuple[int, bytes]] = {}
        self.sites = page(
            resource_object('codechef.com', 2), resource_object('icpc.global', 86)
        )
        self.requests: list[tuple[str, str | None]] = []
        app = web.Application()
        app.router.add_get('/api/v4/resource/', self._handle_sites)
        app.router.add_get('/{tail:.*}', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def api_url(self) -> str:
        return str(self._server.make_url('/api/v4/contest/'))

    @property
    def queries(self) -> list[dict[str, list[str]]]:
        return [parse_qs(urlsplit(path).query) for path, _ in self.requests]

    def serve(self, *pages: bytes, status: int = 200) -> None:
        """Answer with ``pages``, each at its offset: 0, 100, 200 and so on."""
        self.pages = {100 * n: (status, body) for n, body in enumerate(pages)}

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.raw_path, request.headers.get('Authorization')))
        status, body = self.pages.get(int(request.query.get('offset', 0)), (404, b''))
        return web.Response(
            status=status, body=body, headers={'Content-Type': 'application/json'}
        )

    async def _handle_sites(self, request: web.Request) -> web.Response:
        self.requests.append((request.raw_path, request.headers.get('Authorization')))
        return web.Response(
            body=self.sites, headers={'Content-Type': 'application/json'}
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeClist]:
    site = FakeClist()
    await site.start()
    monkeypatch.setattr(clist, 'API_URL', site.api_url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[ClistClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield ClistClient(http, username=USERNAME, api_key=API_KEY)
    await http.close()


class TestUpcoming:
    async def test_reads_every_page_of_the_list(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(FIRST_PAGE.read_bytes(), SECOND_PAGE.read_bytes())

        contests = await client.upcoming('codechef.com')

        assert contests == [STARTERS_210, MONDAY_MUNCH, STARTERS_211]
        assert site.queries[0] == {
            'upcoming': ['true'],
            'resource': ['codechef.com'],
            'order_by': ['start'],
            'limit': ['100'],
        }
        # The second request follows meta.next as it is.
        assert [path for path, _ in site.requests][1:] == [link(100)]

    async def test_sends_the_key_only_in_the_authorization_header(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(FIRST_PAGE.read_bytes(), SECOND_PAGE.read_bytes())

        await client.upcoming('codechef.com')

        assert [header for _, header in site.requests] == [
            'ApiKey test-user:test-key'
        ] * 2
        for path, _ in site.requests:
            assert API_KEY not in path and USERNAME not in path

    def test_keeps_the_key_out_of_its_repr(self, client: ClistClient) -> None:
        assert API_KEY not in repr(client) and USERNAME not in repr(client)

    async def test_an_event_regex_narrows_the_contests_by_name(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(page())

        assert await client.upcoming('icpc.global', event_regex='world finals') == []
        query, sites_query = site.queries
        assert query['resource'] == ['icpc.global']
        assert query['event__iregex'] == ['world finals']
        # An empty list is checked against the site, not the regex.
        assert sites_query == {'name__in': ['icpc.global']}

    async def test_contests_come_by_start_then_id(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        end = '2026-10-10T10:00:00'
        later = first_object(id=3, start='2026-10-09T10:00:00', end=end)
        tied = first_object(id=2, start='2026-10-08T10:00:00', end=end)
        earlier = first_object(id=1, start='2026-10-08T10:00:00', end=end)
        site.serve(page(later, tied, earlier))

        contests = await client.upcoming('codechef.com')

        assert [contest.clist_id for contest in contests] == [1, 2, 3]

    async def test_skips_contests_it_cannot_read(
        self, site: FakeClist, client: ClistClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        site.serve(
            page(
                first_object(),
                first_object(id=7, href=...),
                'oops',
                next_page=link(100),
            ),
            page(fixture(SECOND_PAGE)['objects'][0], offset=100),
        )

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            contests = await client.upcoming('codechef.com')

        assert contests == [STARTERS_210, STARTERS_211]
        skipped = [record.getMessage() for record in caplog.records]
        assert 'Skipping clist.by contest 7, which has no href' in skipped
        assert 'Skipping a clist.by contest that is a str' in skipped

    async def test_a_contest_listed_twice_is_kept_once(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(
            page(first_object(), next_page=link(100)),
            page(first_object(event='Starters 210 again'), offset=100),
        )

        assert await client.upcoming('codechef.com') == [STARTERS_210]

    async def test_an_empty_page_ends_the_list(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(
            page(first_object(), next_page=link(100)),
            page(next_page=link(200), offset=100),
            page(fixture(SECOND_PAGE)['objects'][0], offset=200),
        )

        assert await client.upcoming('codechef.com') == [STARTERS_210]
        assert len(site.requests) == 2

    @pytest.mark.parametrize(
        'pages',
        [
            [page(first_object(href=...), 'oops')],
            [
                page(first_object(href=...), next_page=link(100)),
                page(first_object(id=8, start='nonsense'), offset=100),
            ],
        ],
        ids=['one page', 'two pages'],
    )
    async def test_a_list_with_no_contest_it_can_read_is_unreadable(
        self, site: FakeClist, client: ClistClient, pages: list[bytes]
    ) -> None:
        # clist.by's format has changed. No contests would make every contest
        # of a complete source look cancelled.
        site.serve(*pages)

        with pytest.raises(ExternalServiceError) as excinfo:
            await client.upcoming('codechef.com')

        assert str(excinfo.value) == UNREADABLE
        assert len(site.requests) == len(pages)

    async def test_an_empty_list_is_checked_against_clist_bys_sites(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(page())

        assert await client.upcoming('codechef.com') == []

        # One more request, for codechef.com in clist.by's list of resources,
        # with the key only in its Authorization header.
        _, (path, header) = site.requests
        assert urlsplit(path).path == '/api/v4/resource/'
        assert site.queries[1] == {'name__in': ['codechef.com']}
        assert header == 'ApiKey test-user:test-key'
        assert API_KEY not in path and USERNAME not in path

    @pytest.mark.parametrize(
        ('sites', 'message'),
        [
            (page(), NO_SUCH_SITE),
            (page(resource_object('topcoder.com', 12)), NO_SUCH_SITE),
            (b'[]', UNREADABLE),
        ],
        ids=['none', 'another', 'unreadable'],
    )
    async def test_an_empty_list_of_a_site_clist_by_does_not_have_fails(
        self,
        site: FakeClist,
        client: ClistClient,
        caplog: pytest.LogCaptureFixture,
        sites: bytes,
        message: str,
    ) -> None:
        # clist.by sends an empty list for a resource it doesn't have, as for
        # one with no contests coming: the site may have been renamed.
        site.serve(page())
        site.sites = sites

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ExternalServiceError) as excinfo:
                await client.upcoming('codechef.com')

        error = excinfo.value
        assert str(error) == message
        assert (error.service, error.status) == ('clist.by', None)
        for secret in (API_KEY, USERNAME):
            assert secret not in repr(error.args)
            assert secret not in caplog.text

    @pytest.mark.parametrize(
        'next_page',
        [
            'https://example.org/api/v4/contest/?offset=100',
            '//example.org/api/v4/contest/?offset=100',
            'http://[::1/api/v4/contest/?offset=100',
        ],
        ids=['absolute', 'scheme-relative', 'unparsable'],
    )
    async def test_never_follows_a_link_to_another_site(
        self, site: FakeClist, client: ClistClient, next_page: str
    ) -> None:
        # The key goes with every request.
        site.serve(page(first_object(), next_page=next_page))

        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            await client.upcoming('codechef.com')
        assert len(site.requests) == 1

    async def test_reads_at_most_5_pages(
        self, site: FakeClist, client: ClistClient
    ) -> None:
        site.serve(
            *(
                page(first_object(id=n), next_page=link(100 * (n + 1)), offset=100 * n)
                for n in range(6)
            )
        )

        with pytest.raises(ExternalServiceError) as excinfo:
            await client.upcoming('codechef.com')

        assert str(excinfo.value) == (
            'clist.by lists more than 500 contests for codechef.com, too many to read.'
        )
        assert excinfo.value.service == 'clist.by'
        assert len(site.requests) == 5

    @pytest.mark.parametrize('status', [401, 403])
    async def test_a_refused_key_says_so_without_giving_it_away(
        self,
        site: FakeClist,
        client: ClistClient,
        caplog: pytest.LogCaptureFixture,
        status: int,
    ) -> None:
        site.serve(b'{"detail": "Invalid API key"}', status=status)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ExternalServiceError) as excinfo:
                await client.upcoming('codechef.com')

        error = excinfo.value
        assert str(error) == REFUSED
        assert (error.service, error.status) == ('clist.by', status)
        for secret in (API_KEY, USERNAME):
            assert secret not in repr(error.args)
            assert secret not in caplog.text

    @pytest.mark.parametrize(
        'body',
        [b'', b'<!DOCTYPE html><html></html>', b'{"meta": {', b'\xff\xfe\x00'],
        ids=['empty', 'html', 'truncated', 'binary'],
    )
    async def test_a_body_that_is_not_json_is_unreadable(
        self, site: FakeClist, client: ClistClient, body: bytes
    ) -> None:
        site.serve(body)

        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            await client.upcoming('codechef.com')
        assert (excinfo.value.service, excinfo.value.status) == ('clist.by', 200)
        assert isinstance(excinfo.value.__cause__, ValueError)

    @pytest.mark.parametrize(
        'data',
        [
            [],
            {},
            {'objects': []},
            {'meta': {'next': None}},
            {'meta': [], 'objects': []},
            {'meta': {'next': None}, 'objects': {}},
            {'meta': {'next': 100}, 'objects': []},
        ],
    )
    async def test_json_that_is_not_a_contest_list_is_unreadable(
        self, site: FakeClist, client: ClistClient, data: object
    ) -> None:
        site.serve(json.dumps(data).encode())

        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            await client.upcoming('codechef.com')

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (404, r'^clist\.by returned an error \(HTTP 404\)\.$'),
            (500, r'^clist\.by is not responding right now'),
        ],
    )
    async def test_other_failures_name_clist_by(
        self, site: FakeClist, client: ClistClient, status: int, message: str
    ) -> None:
        site.serve(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.upcoming('codechef.com')
        assert (excinfo.value.service, excinfo.value.status) == ('clist.by', status)
