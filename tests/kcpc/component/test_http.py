"""Component tests for tle.kcpc.core.http, against a real local aiohttp server.

Time is virtual: ``InstantClock`` records every sleep and returns at once, so
backoff, Retry-After and pacing waits are asserted exactly, without waiting.
"""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import format_datetime
from typing import Any, Protocol, TypeAlias

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict, CIMultiDictProxy

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import (
    DEFAULT_POLICIES,
    DEFAULT_POLICY,
    HostPolicy,
    HttpClient,
    HttpResponse,
    RateLimiter,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
USER_AGENT = 'KCPC-bot-tests (+https://example.com)'
LOCAL_HOST = '127.0.0.1'
HTTP_LOGGER = 'tle.kcpc.core.http'

# No pacing, so the only sleeps a test sees are the waits between attempts.
UNPACED = HostPolicy()


def elapsed(clock: FakeClock) -> float:
    """Virtual seconds since T0."""
    return (clock.now() - T0).total_seconds()


def http_date(when: datetime) -> str:
    return format_datetime(when, usegmt=True)


class InstantClock(FakeClock):
    """A FakeClock whose ``sleep`` returns at once, moving virtual time on.

    Every requested sleep is recorded in ``sleeps``.
    """

    def __init__(self) -> None:
        super().__init__(T0, io_grace=0)
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self.advance(max(0.0, seconds))


Handler: TypeAlias = Callable[[web.Request], Awaitable[web.StreamResponse]]
# A scripted reply: a bare status, a text body (sent as a 200), a ready-made
# response, or a handler for anything more unusual.
Reply: TypeAlias = int | str | web.StreamResponse | Handler


@dataclass(frozen=True)
class SeenRequest:
    """A request as the site received it."""

    path: str
    query: dict[str, str]
    headers: CIMultiDictProxy[str]
    at: float  # virtual seconds since T0


class FakeSite:
    """A local HTTP server that answers each GET with the next scripted reply.

    Once the script runs out it answers 200 with an empty body.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.requests: list[SeenRequest] = []
        self._clock = clock
        self._replies: deque[Reply] = deque()
        app = web.Application()
        app.router.add_get('/{path:.*}', self._handle)
        self._server = TestServer(app, host=LOCAL_HOST)

    def script(self, *replies: Reply) -> None:
        self._replies.extend(replies)

    def url(self, path: str = '/') -> str:
        return str(self._server.make_url(path))

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            SeenRequest(
                path=request.path,
                query=dict(request.query),
                headers=request.headers,
                at=elapsed(self._clock),
            )
        )
        reply = self._replies.popleft() if self._replies else 200
        if isinstance(reply, int):
            return web.Response(status=reply)
        if isinstance(reply, str):
            return web.Response(text=reply)
        if isinstance(reply, web.StreamResponse):
            return reply
        return await reply(request)


async def stall(request: web.Request) -> web.StreamResponse:
    """Never answer. The client times out, and TestServer then cancels this."""
    await asyncio.Event().wait()
    raise AssertionError('unreachable')


async def disconnect(request: web.Request) -> web.StreamResponse:
    """Drop the connection without answering."""
    assert request.transport is not None
    request.transport.close()
    return web.Response()  # never sent: the connection is already gone


class ClientFactory(Protocol):
    def __call__(
        self, policy: HostPolicy = ..., *, timeout: float = ...
    ) -> HttpClient: ...


@pytest.fixture
def instant_clock() -> InstantClock:
    return InstantClock()


@pytest.fixture
async def site(instant_clock: InstantClock) -> AsyncIterator[FakeSite]:
    site = FakeSite(instant_clock)
    await site.start()
    yield site
    await site.close()


@pytest.fixture
async def make_client(instant_clock: InstantClock) -> AsyncIterator[ClientFactory]:
    """Builds clients whose policy for the local site is ``policy``."""
    clients: list[HttpClient] = []

    def make(policy: HostPolicy = UNPACED, *, timeout: float = 30.0) -> HttpClient:
        client = HttpClient(
            user_agent=USER_AGENT,
            clock=instant_clock,
            policies={LOCAL_HOST: policy},
            timeout=timeout,
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.close()


def http_log_levels(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [record.levelno for record in caplog.records if record.name == HTTP_LOGGER]


class TestGet:
    async def test_sends_the_user_agent(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script('hello')
        response = await make_client().get(site.url('/page'))
        assert (response.status, response.body) == (200, b'hello')
        assert site.requests[0].headers['User-Agent'] == USER_AGENT

    async def test_get_json(self, site: FakeSite, make_client: ClientFactory) -> None:
        site.script(web.json_response({'contests': [1, 2]}))
        assert await make_client().get_json(site.url()) == {'contests': [1, 2]}

    async def test_get_text_decodes_with_the_declared_charset(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(
            web.Response(
                body='Café'.encode('latin-1'),
                content_type='text/plain',
                charset='latin-1',
            )
        )
        assert await make_client().get_text(site.url()) == 'Café'

    async def test_get_json_rejects_a_body_that_is_not_json(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script('<html>Just a moment...</html>')
        with pytest.raises(ExternalServiceError) as caught:
            await make_client().get_json(site.url(), service='Codeforces')
        assert str(caught.value) == 'Codeforces returned an unexpected response.'
        assert caught.value.status == 200

    async def test_sends_query_params(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        params = {'q': 'dp on trees', 'page': '2'}
        await make_client().get(site.url('/search'), params=params)
        assert site.requests[0].query == params

    async def test_response_has_the_final_url_and_case_insensitive_headers(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(
            web.Response(status=302, headers={'Location': '/final'}),
            web.Response(text='done', headers={'X-Served-By': 'final'}),
        )
        response = await make_client().get(site.url('/start'))
        assert response.url == site.url('/final')
        assert response.headers['x-served-by'] == 'final'
        assert [request.path for request in site.requests] == ['/start', '/final']

    async def test_allow_status_returns_the_response(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        site.script(web.Response(status=404, text='no such user'))
        response = await make_client().get(site.url(), allow_status={404})
        assert (response.status, response.text()) == (404, 'no such user')
        assert instant_clock.sleeps == []

    async def test_allow_status_wins_over_retrying(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(503)
        response = await make_client().get(site.url(), allow_status=[503])
        assert response.status == 503
        assert len(site.requests) == 1

    @pytest.mark.parametrize(
        'url',
        [
            'ftp://example.com/file',
            'example.com/page',
            '/relative/path',
            'https:///no-host',
        ],
    )
    async def test_rejects_urls_that_are_not_absolute_http(
        self, make_client: ClientFactory, url: str
    ) -> None:
        with pytest.raises(ValueError, match='absolute http'):
            await make_client().get(url)


class TestRetries:
    async def test_server_errors_are_retried_with_backoff(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        site.script(503, 503, web.json_response({'ok': True}))
        assert await make_client().get_json(site.url()) == {'ok': True}
        assert len(site.requests) == 3
        first, second = instant_clock.sleeps
        assert 1.0 <= first <= 1.25  # backoff_base, plus up to 25% jitter
        assert 2.0 <= second <= 2.5

    async def test_backoff_doubles_up_to_the_cap(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        site.script(500, 500, 500, 500, 'ok')
        policy = HostPolicy(max_attempts=5, backoff_base=10.0, backoff_cap=35.0)
        await make_client(policy).get(site.url())
        first, second, third, fourth = instant_clock.sleeps
        assert 10.0 <= first <= 12.5
        assert 20.0 <= second <= 25.0
        assert third == fourth == 35.0  # 40 s and 80 s before the cap

    @pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
    async def test_retryable_statuses(
        self, site: FakeSite, make_client: ClientFactory, status: int
    ) -> None:
        site.script(status, 'ok')
        assert await make_client().get_text(site.url()) == 'ok'
        assert len(site.requests) == 2

    @pytest.mark.parametrize(
        ('status', 'retry_after', 'expected'),
        [
            pytest.param(429, '7', 7.0, id='seconds'),
            pytest.param(503, '2.5', 2.5, id='fractional-seconds'),
            pytest.param(503, http_date(T0 + timedelta(seconds=30)), 30.0, id='date'),
            pytest.param(429, http_date(T0 - timedelta(seconds=30)), 0.0, id='past'),
            pytest.param(429, '3600', 60.0, id='capped'),
        ],
    )
    async def test_retry_after_is_honoured(
        self,
        site: FakeSite,
        make_client: ClientFactory,
        instant_clock: InstantClock,
        status: int,
        retry_after: str,
        expected: float,
    ) -> None:
        site.script(
            web.Response(status=status, headers={'Retry-After': retry_after}), 'ok'
        )
        assert await make_client().get_text(site.url()) == 'ok'
        assert instant_clock.sleeps == [expected]
        assert site.requests[1].at == expected

    @pytest.mark.parametrize(
        'retry_after',
        ['soon', '-5', 'nan', 'inf', '1_000', 'Wed, 21 Oct 99999999999999 07:28 GMT'],
    )
    async def test_unusable_retry_after_falls_back_to_backoff(
        self,
        site: FakeSite,
        make_client: ClientFactory,
        instant_clock: InstantClock,
        retry_after: str,
    ) -> None:
        site.script(
            web.Response(status=429, headers={'Retry-After': retry_after}), 'ok'
        )
        assert await make_client().get_text(site.url()) == 'ok'
        [wait] = instant_clock.sleeps
        assert 1.0 <= wait <= 1.25

    @pytest.mark.parametrize('status', [304, 400, 401, 403, 404, 410])
    async def test_other_statuses_fail_at_once(
        self,
        site: FakeSite,
        make_client: ClientFactory,
        instant_clock: InstantClock,
        status: int,
    ) -> None:
        site.script(status)
        with pytest.raises(ExternalServiceError) as caught:
            await make_client().get(site.url('/users/ghost'), service='AtCoder')
        assert caught.value.service == 'AtCoder'
        assert caught.value.status == status
        assert str(caught.value) == f'AtCoder returned an error (HTTP {status}).'
        assert len(site.requests) == 1
        assert instant_clock.sleeps == []

    async def test_service_defaults_to_the_host(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(404)
        with pytest.raises(ExternalServiceError) as caught:
            await make_client().get(site.url())
        assert caught.value.service == LOCAL_HOST
        assert str(caught.value) == f'{LOCAL_HOST} returned an error (HTTP 404).'

    async def test_gives_up_after_max_attempts(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        site.script(500, 502, 503)
        with pytest.raises(ExternalServiceError) as caught:
            await make_client().get(site.url(), service='Luma')
        assert caught.value.service == 'Luma'
        assert caught.value.status == 503
        assert (
            str(caught.value)
            == 'Luma is not responding right now. Please try again later.'
        )
        assert len(site.requests) == 3
        assert len(instant_clock.sleeps) == 2  # no wait after the final attempt

    async def test_timeout_is_retried(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        site.script(stall, 'ok')
        assert await make_client(timeout=0.1).get_text(site.url()) == 'ok'
        assert len(site.requests) == 2
        assert len(instant_clock.sleeps) == 1

    async def test_dropped_connection_is_retried(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(disconnect, 'ok')
        assert await make_client().get_text(site.url()) == 'ok'
        assert len(site.requests) == 2

    async def test_status_is_none_when_the_final_attempt_got_no_response(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        # A stall rather than a dropped connection: aiohttp itself retries a
        # request once when a reused keep-alive connection is dropped.
        site.script(503, stall)
        client = make_client(HostPolicy(max_attempts=2), timeout=0.1)
        with pytest.raises(ExternalServiceError) as caught:
            await client.get(site.url())
        assert caught.value.status is None
        assert len(site.requests) == 2


class TestPacing:
    async def test_min_interval_spaces_consecutive_requests(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        client = make_client(HostPolicy(min_interval=2.0))
        for _ in range(3):
            await client.get(site.url())
        assert [request.at for request in site.requests] == [0.0, 2.0, 4.0]
        assert instant_clock.sleeps == pytest.approx([2.0, 2.0])

    async def test_time_already_passed_counts_towards_min_interval(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        client = make_client(HostPolicy(min_interval=2.0))
        await client.get(site.url())
        await instant_clock.advance(1.5)
        await client.get(site.url())
        assert instant_clock.sleeps == pytest.approx([0.5])

    async def test_max_per_minute_paces_a_sliding_window(
        self, site: FakeSite, make_client: ClientFactory, instant_clock: InstantClock
    ) -> None:
        client = make_client(HostPolicy(max_per_minute=3))
        for _ in range(7):
            await client.get(site.url())
        arrivals = [request.at for request in site.requests]
        assert arrivals == [0.0, 0.0, 0.0, 60.0, 60.0, 60.0, 120.0]
        assert instant_clock.sleeps == pytest.approx([60.0, 60.0])

    async def test_retries_are_paced_too(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        site.script(503, 'ok')
        await make_client(HostPolicy(min_interval=5.0)).get(site.url())
        # About 1 s of backoff, then the limiter waits out the rest of the 5 s.
        assert site.requests[1].at == pytest.approx(5.0)


class TestRateLimiter:
    async def test_combines_min_interval_with_the_minute_window(self) -> None:
        clock = InstantClock()
        limiter = RateLimiter(HostPolicy(min_interval=10.0, max_per_minute=3), clock)
        starts = []
        for _ in range(7):
            await limiter.acquire()
            starts.append(elapsed(clock))
        assert starts == [0.0, 10.0, 20.0, 60.0, 70.0, 80.0, 120.0]

    async def test_concurrent_callers_take_turns(self) -> None:
        clock = FakeClock(T0, io_grace=0)
        limiter = RateLimiter(HostPolicy(min_interval=2.0), clock)
        starts: list[float] = []

        async def request() -> None:
            await limiter.acquire()
            starts.append(elapsed(clock))

        tasks = [asyncio.create_task(request()) for _ in range(3)]
        await clock.settle()
        assert starts == [0.0]
        await clock.advance(2)
        assert starts == [0.0, 2.0]
        await clock.advance(2)
        assert starts == [0.0, 2.0, 4.0]
        await asyncio.gather(*tasks)

    async def test_a_cancelled_wait_is_not_counted(self) -> None:
        clock = FakeClock(T0, io_grace=0)
        limiter = RateLimiter(HostPolicy(min_interval=2.0), clock)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await clock.settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await clock.advance(2)
        # The lock was released and no start recorded, so there is no wait.
        # (The timeout only stops a regression from hanging the test run.)
        await asyncio.wait_for(limiter.acquire(), timeout=1)
        assert elapsed(clock) == 2.0


class TestHeaders:
    async def test_policy_then_call_headers_win_whatever_the_case(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        policy = HostPolicy(headers={'user-agent': 'Policy/1.0', 'Accept': 'text/html'})
        client = make_client(policy)
        await client.get(site.url())
        await client.get(site.url(), headers={'User-Agent': 'Call/1.0', 'X-Call': '1'})
        by_policy, by_call = (request.headers for request in site.requests)
        assert by_policy.getall('User-Agent') == ['Policy/1.0']
        assert by_call.getall('User-Agent') == ['Call/1.0']
        assert by_policy['Accept'] == by_call['Accept'] == 'text/html'
        assert by_call['X-Call'] == '1'


class TestPolicies:
    def test_lookup_matches_the_host_exactly_ignoring_www(self) -> None:
        client = HttpClient(user_agent=USER_AGENT, clock=FakeClock(T0))
        atcoder = DEFAULT_POLICIES['atcoder.jp']
        assert client.policy_for('https://atcoder.jp/contests/') is atcoder
        assert client.policy_for('https://WWW.AtCoder.jp/users/x?lang=en') is atcoder
        assert client.policy_for('https://img.atcoder.jp/logo.png') is DEFAULT_POLICY
        assert client.policy_for('https://example.com/') is DEFAULT_POLICY

    def test_given_policies_replace_the_defaults(self) -> None:
        mine, fallback = HostPolicy(min_interval=9.0), HostPolicy(min_interval=3.0)
        client = HttpClient(
            user_agent=USER_AGENT,
            clock=FakeClock(T0),
            policies={'www.Example.com': mine},
            default_policy=fallback,
        )
        assert client.policy_for('http://example.com/feed') is mine
        assert client.policy_for('https://atcoder.jp/') is fallback

    def test_default_policies(self) -> None:
        assert DEFAULT_POLICY == HostPolicy(min_interval=0.5)
        pacing = {
            host: (policy.min_interval, policy.max_per_minute)
            for host, policy in DEFAULT_POLICIES.items()
        }
        assert pacing == {
            'atcoder.jp': (2.0, None),
            'kenkoooo.com': (1.1, None),
            'clist.by': (0.0, 10),
            'api.lu.ma': (1.0, None),
            'icpc.global': (1.0, None),
            'codeforces.com': (2.0, None),
        }
        browser = DEFAULT_POLICIES['codeforces.com'].headers
        assert browser['User-Agent'].startswith('Mozilla/5.0 (Windows NT 10.0;')
        assert ' Chrome/' in browser['User-Agent']
        assert browser['Accept'].startswith('text/html,')
        assert browser['Accept-Language'] == 'en-GB,en;q=0.9'

    @pytest.mark.parametrize(
        'invalid',
        [
            {'min_interval': -1.0},
            {'max_per_minute': 0},
            {'max_attempts': 0},
            {'backoff_base': -1.0},
            {'backoff_cap': -1.0},
        ],
    )
    def test_invalid_settings_are_rejected(self, invalid: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            HostPolicy(**invalid)

    def test_headers_are_copied(self) -> None:
        headers = {'Accept': 'text/html'}
        policy = HostPolicy(headers=headers)
        headers['Accept'] = 'application/json'
        assert policy.headers == {'Accept': 'text/html'}


def response_with(body: bytes, content_type: str | None = None) -> HttpResponse:
    headers: CIMultiDict[str] = CIMultiDict()
    if content_type is not None:
        headers['Content-Type'] = content_type
    return HttpResponse(
        url='https://example.com/',
        status=200,
        headers=CIMultiDictProxy(headers),
        body=body,
    )


class TestHttpResponse:
    @pytest.mark.parametrize(
        ('content_type', 'body', 'expected'),
        [
            ('text/html; charset=ISO-8859-1', 'Café'.encode('latin-1'), 'Café'),
            ('text/html; charset="utf-8"', 'Café'.encode(), 'Café'),
            ('text/html', 'Café'.encode(), 'Café'),
            (None, 'Café'.encode(), 'Café'),
            ('text/plain; charset=klingon', 'Café'.encode(), 'Café'),
            (None, b'Caf\xe9', 'Caf\ufffd'),
        ],
        ids=['charset', 'quoted', 'no-charset', 'no-type', 'unknown', 'bad-bytes'],
    )
    def test_text(self, content_type: str | None, body: bytes, expected: str) -> None:
        assert response_with(body, content_type).text() == expected

    def test_text_with_an_explicit_encoding(self) -> None:
        response = response_with('Café'.encode('cp1252'), 'text/plain; charset=utf-8')
        assert response.text('cp1252') == 'Café'

    def test_json(self) -> None:
        assert response_with(b'{"ok": true}').json() == {'ok': True}

    def test_repr_leaves_out_the_body(self) -> None:
        assert 'secret' not in repr(response_with(b'secret'))


class TestLifecycle:
    def test_building_a_client_needs_no_event_loop(self) -> None:
        # The aiohttp session only appears on first use, inside the loop.
        HttpClient(user_agent=USER_AGENT, clock=FakeClock(T0))

    async def test_close_is_idempotent(
        self, site: FakeSite, make_client: ClientFactory
    ) -> None:
        client = make_client()
        await client.get(site.url())
        await client.close()
        await client.close()
        with pytest.raises(RuntimeError, match='closed'):
            await client.get(site.url())

    async def test_close_before_first_use(self) -> None:
        client = HttpClient(user_agent=USER_AGENT, clock=FakeClock(T0))
        await client.close()
        await client.close()


class TestLogging:
    async def test_retries_log_at_debug_and_giving_up_at_warning(
        self,
        site: FakeSite,
        make_client: ClientFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=HTTP_LOGGER)
        site.script(503, 503, 503)
        with pytest.raises(ExternalServiceError):
            await make_client().get(site.url())
        assert http_log_levels(caplog) == [
            logging.DEBUG,
            logging.DEBUG,
            logging.WARNING,
        ]

    async def test_warns_at_most_hourly_per_host(
        self,
        site: FakeSite,
        make_client: ClientFactory,
        instant_clock: InstantClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.INFO, logger=HTTP_LOGGER)
        site.script(404, 404, 404)
        client = make_client()
        for wait in (0, 3599, 1):
            await instant_clock.advance(wait)
            with pytest.raises(ExternalServiceError):
                await client.get(site.url())
        assert http_log_levels(caplog) == [
            logging.WARNING,
            logging.INFO,
            logging.WARNING,
        ]
