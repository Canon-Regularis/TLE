"""Component tests for TLE's Codeforces client, against a local aiohttp server
that stands in for codeforces.com/api.

``API_BASE_URL`` points at the server and the spacing between calls is 0, so
no call waits. The outage tracker gets a fake clock. A request that must time
out fails at once, without reaching the server: a real timeout is a real wait.
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeAlias
from urllib.parse import parse_qs

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.util import codeforces_api as cf

FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'fixtures' / 'cf_api_responses'
LOCAL_HOST = '127.0.0.1'
CF_LOGGER = cf.logger.name
MINUTE = 60
# 12 October 2026, 23:00 UTC, when problemset.problems answered a 502 page
T0 = datetime(2026, 10, 12, 23, 0, tzinfo=timezone.utc).timestamp()
BAD_GATEWAY = cf.BadAnswer('HTTP 502 without JSON')
BAD_GATEWAY_LINE = 'Request to CF API problemset.problems failed: HTTP 502 without JSON'

# The refusal of a standings request that isn't an anonymous GET with contestId
# alone, as Codeforces sends it, escaping '=', '<' and '>' (12 October 2026).
LIVE_STANDINGS_REFUSAL = (
    '{"status":"FAILED","comment":"contestId: Non-gym contest standings for '
    'non-admin users are available only via anonymous GET requests with no '
    'extra parameters: https://codeforces.com/api/contest.standings?contestId'
    '\\u003d\\u003cid\\u003e"}'
)
LIVE_STANDINGS_COMMENT = (
    'contestId: Non-gym contest standings for non-admin users are available '
    'only via anonymous GET requests with no extra parameters: '
    'https://codeforces.com/api/contest.standings?contestId=<id>'
)


def fixture(name: str) -> Any:
    with open(FIXTURES_DIR / name, encoding='utf-8') as f:
        return json.load(f)


# --- The stand-in for codeforces.com ---

Handler: TypeAlias = Callable[[web.Request], Awaitable[web.StreamResponse]]
# A scripted reply: a ready-made response, or a handler for anything unusual.
Reply: TypeAlias = web.StreamResponse | Handler


@dataclass(frozen=True)
class SeenRequest:
    """A request as the server received it."""

    method: str
    api_method: str  # such as contest.standings
    query_string: str
    content_type: str
    body: bytes

    @property
    def form(self) -> dict[str, list[str]]:
        return parse_qs(self.body.decode())


class FakeCodeforces:
    """A local server that answers each /api/<method> call with the next
    scripted reply. Once the script runs out, it answers an empty result.
    """

    def __init__(self) -> None:
        self.requests: list[SeenRequest] = []
        self._replies: deque[Reply] = deque()
        app = web.Application()
        app.router.add_route('*', '/api/{method}', self._handle)
        self._server = TestServer(app, host=LOCAL_HOST)

    def script(self, *replies: Reply) -> None:
        self._replies.extend(replies)

    def base_url(self) -> str:
        return str(self._server.make_url('/api/'))

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            SeenRequest(
                method=request.method,
                api_method=request.match_info['method'],
                query_string=request.query_string,
                content_type=request.content_type,
                body=await request.read(),
            )
        )
        reply = self._replies.popleft() if self._replies else answer([])
        if isinstance(reply, web.StreamResponse):
            return reply
        return await reply(request)


def answer(result: Any) -> web.Response:
    """Codeforces' answer with ``result``."""
    return web.json_response({'status': 'OK', 'result': result})


def refusal(comment: str, status: int = 400) -> web.Response:
    """Codeforces' refusal, with ``comment``."""
    return web.json_response({'status': 'FAILED', 'comment': comment}, status=status)


def error_page(status: int = 502) -> web.Response:
    """An HTML error page, such as the 502 that problemset.problems got."""
    return web.Response(
        status=status,
        text=f'<html><body><h1>Error {status}</h1></body></html>',
        content_type='text/html',
    )


def error_pages(count: int, status: int = 502) -> list[web.Response]:
    return [error_page(status) for _ in range(count)]


async def disconnect(request: web.Request) -> web.StreamResponse:
    """Drop the connection without answering."""
    assert request.transport is not None
    request.transport.close()
    return web.Response()  # never sent: the connection is already gone


class FailedRequest:
    """A request that fails with ``error`` as soon as it is sent."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class Session:
    """Stands in for ``cf._session``.

    It passes each request on to a real session, noting its keyword arguments,
    unless ``fail`` holds an error for it: then the request fails with it at
    once, without reaching the server, as a request that timed out would. A
    None in ``fail`` lets one request through.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self.sent: list[dict[str, Any]] = []
        self.fail: deque[BaseException | None] = deque()

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._send(self._session.get, url, kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._send(self._session.post, url, kwargs)

    def _send(self, send: Callable[..., Any], url: str, kwargs: dict[str, Any]) -> Any:
        self.sent.append(kwargs)
        error = self.fail.popleft() if self.fail else None
        if error is not None:
            return FailedRequest(error)
        return send(url, **kwargs)


class FakeClock:
    """Epoch seconds that move only when a test moves them."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    def at(self, minutes: float) -> None:
        """Moves to ``minutes`` after T0."""
        self.now = T0 + minutes * MINUTE


class Outage(cf._Outage):
    """The outage tracker, noting what it is told."""

    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__(clock)
        self.told: list[tuple[str, ...]] = []

    def failed(self, path: str, error: BaseException) -> None:
        self.told.append(('failed', path, repr(error)))
        super().failed(path, error)

    def answered(self, path: str) -> None:
        self.told.append(('answered', path))
        super().answered(path)


def broken_clock() -> float:
    raise RuntimeError('the clock stopped')


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def local_time_9_hours_ahead() -> Iterator[None]:
    """The process's local time zone, 9 hours ahead of UTC like Tokyo's."""
    if not hasattr(time, 'tzset'):
        pytest.skip('only Unix can change the local time zone')
    before = os.environ.get('TZ')
    os.environ['TZ'] = 'JST-9'  # a POSIX rule, which needs no time zone files
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = before
        time.tzset()


@pytest.fixture
def outage(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> Outage:
    """A new outage tracker, in place of the client's own."""
    tracker = Outage(clock)
    monkeypatch.setattr(cf, '_outage', tracker)
    return tracker


@pytest.fixture
async def session() -> AsyncIterator[Session]:
    real = aiohttp.ClientSession()
    yield Session(real)
    await real.close()


@pytest.fixture
async def codeforces(
    monkeypatch: pytest.MonkeyPatch, session: Session, outage: Outage
) -> AsyncIterator[FakeCodeforces]:
    """The stand-in, which the client calls without spacing its calls."""
    site = FakeCodeforces()
    await site.start()
    monkeypatch.setattr(cf, 'API_BASE_URL', site.base_url())
    monkeypatch.setattr(cf, '_CALL_SPACING_SECONDS', 0)
    monkeypatch.setattr(cf, '_session', session)
    yield site
    await site.close()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO, logger=CF_LOGGER)
    return caplog


def records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == CF_LOGGER]


def lines(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The client's messages at ``level``."""
    return [r.getMessage() for r in records(caplog) if r.levelno == level]


def posted(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The client's messages at WARNING or above: those the log channel gets."""
    return [r.getMessage() for r in records(caplog) if r.levelno >= logging.WARNING]


# --- Requests ---


class TestRequests:
    async def test_standings_is_an_anonymous_get_with_the_contest_id_alone(
        self, codeforces: FakeCodeforces
    ) -> None:
        codeforces.script(answer(fixture('contest_standings.json')))

        contest, problems, rows = await cf.contest.standings(contest_id=2273)

        (seen,) = codeforces.requests
        assert seen.method == 'GET'
        assert seen.api_method == 'contest.standings'
        assert seen.query_string == 'contestId=2273'
        assert seen.body == b''
        assert contest.id == 2273
        assert [problem.index for problem in problems] == list('ABCDEF')
        assert [row.rank for row in rows] == [1, 2, 3, 3, 5, 5]

    @pytest.mark.parametrize(
        ('call', 'api_method', 'form'),
        [
            pytest.param(
                lambda: cf.contest.status(
                    contest_id=2273, handle='Alice', from_=1, count=3
                ),
                'contest.status',
                {
                    'contestId': ['2273'],
                    'handle': ['Alice'],
                    'from': ['1'],
                    'count': ['3'],
                },
                id='contest.status',
            ),
            pytest.param(
                lambda: cf.contest.ratingChanges(contest_id=2273),
                'contest.ratingChanges',
                {'contestId': ['2273']},
                id='contest.ratingChanges',
            ),
            pytest.param(
                lambda: cf.contest.to_list(gym=False),
                'contest.list',
                {'gym': ['false']},
                id='contest.list',
            ),
            pytest.param(
                lambda: cf.user.info(handles=['Alice', 'Bob']),
                'user.info',
                {'handles': ['Alice;Bob']},
                id='user.info',
            ),
            pytest.param(
                lambda: cf.user.status(handle='Alice', count=5),
                'user.status',
                {'handle': ['Alice'], 'count': ['5']},
                id='user.status',
            ),
        ],
    )
    async def test_other_methods_post_a_form(
        self,
        codeforces: FakeCodeforces,
        call: Callable[[], Awaitable[Any]],
        api_method: str,
        form: dict[str, list[str]],
    ) -> None:
        await call()

        (seen,) = codeforces.requests
        assert (seen.method, seen.api_method) == ('POST', api_method)
        assert seen.query_string == ''
        assert seen.content_type == 'application/x-www-form-urlencoded'
        assert seen.form == form

    async def test_every_request_has_the_timeout(
        self, codeforces: FakeCodeforces, session: Session
    ) -> None:
        codeforces.script(answer(fixture('contest_standings.json')), answer([]))

        await cf.contest.standings(contest_id=2273)
        await cf.contest.ratingChanges(contest_id=2273)

        assert cf._REQUEST_TIMEOUT == aiohttp.ClientTimeout(
            total=5 * 60, sock_connect=30, sock_read=60
        )
        assert [sent['timeout'] for sent in session.sent] == [cf._REQUEST_TIMEOUT] * 2

    async def test_contest_status_gives_submissions_with_passed_test_counts(
        self, codeforces: FakeCodeforces
    ) -> None:
        codeforces.script(answer(fixture('contest_status_virtual.json')))

        submissions = await cf.contest.status(contest_id=2267, handle='Alice_VC')

        by_id = {submission.id: submission for submission in submissions}
        assert by_id[340000004].passedTestCount == 0
        assert by_id[340000005].passedTestCount == 7
        assert by_id[340000006].author.participantType == 'VIRTUAL'

    async def test_user_status_gives_submissions_with_passed_test_counts(
        self, codeforces: FakeCodeforces
    ) -> None:
        codeforces.script(answer(fixture('user_status.json')))

        (submission,) = await cf.user.status(handle='tourist')

        assert submission.passedTestCount == 10
        assert submission.author.members[0].handle == 'tourist'


# --- Refusals: a JSON answer under 500 ---


class TestRefusals:
    async def test_the_live_standings_refusal_is_one_warning_naming_the_method(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture, outage: Outage
    ) -> None:
        codeforces.script(
            web.Response(
                status=400, text=LIVE_STANDINGS_REFUSAL, content_type='application/json'
            )
        )

        with pytest.raises(cf.TrueApiError) as caught:
            await cf.contest.standings(contest_id=2273)

        error = caught.value
        assert type(error) is cf.TrueApiError, 'no "not found" in it'
        assert error.comment == f'HTTP Error 400, {LIVE_STANDINGS_COMMENT}'
        assert str(error) == 'Codeforces API error'
        assert len(codeforces.requests) == 1, 'a refusal is not retried'
        assert posted(logs) == [
            f'Query to CF API contest.standings failed: {error.comment}'
        ]
        assert outage.told == [('answered', 'contest.standings')]

    @pytest.mark.parametrize(
        'comment',
        [
            # What Codeforces said to standings requests in April 2026
            'contestId: You have to be authenticated to use this method',
            'contestId: Only gym and mashup contests are available to non-admin '
            'users in this method',
        ],
    )
    async def test_any_other_refusal_is_a_warning(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture, comment: str
    ) -> None:
        codeforces.script(refusal(comment))

        with pytest.raises(cf.TrueApiError):
            await cf.contest.standings(contest_id=2273)

        assert posted(logs) == [
            f'Query to CF API contest.standings failed: HTTP Error 400, {comment}'
        ]

    @pytest.mark.parametrize(
        ('call', 'api_method', 'comment', 'error_type', 'message'),
        [
            pytest.param(
                lambda: cf.contest.standings(contest_id=999999),
                'contest.standings',
                'contestId: Contest with id 999999 not found',
                cf.ContestNotFoundError,
                'Contest with ID `999999` not found on Codeforces',
                id='standings of an unknown contest',
            ),
            pytest.param(
                lambda: cf.contest.status(contest_id=2273, handle='nobody_0000'),
                'contest.status',
                'handle: User with handle nobody_0000 not found',
                cf.HandleNotFoundError,
                'Handle `nobody_0000` not found on Codeforces',
                id='status of an unknown handle',
            ),
            pytest.param(
                lambda: cf.contest.status(contest_id=2273, handle='no body'),
                'contest.status',
                'handle: Field should contain only Latin letters, digits, '
                'underscore or dash characters',
                cf.HandleInvalidError,
                '`no body` is not a valid Codeforces handle',
                id='status of an invalid handle',
            ),
            pytest.param(
                lambda: cf.contest.status(contest_id=999999, handle='Alice'),
                'contest.status',
                'contestId: Contest with id 999999 not found',
                cf.ContestNotFoundError,
                'Contest with ID `999999` not found on Codeforces',
                id='status of an unknown contest',
            ),
            pytest.param(
                lambda: cf.contest.ratingChanges(contest_id=2276),
                'contest.ratingChanges',
                'contestId: Rating changes are unavailable for this contest',
                cf.RatingChangesUnavailableError,
                'Rating changes unavailable for contest with ID `2276`',
                id='rating changes not out yet',
            ),
            pytest.param(
                lambda: cf.user.info(handles=['Alice', 'nobody_0000']),
                'user.info',
                'handles: User with handle nobody_0000 not found',
                cf.HandleNotFoundError,
                'Handle `nobody_0000` not found on Codeforces',
                id='info of an unknown handle',
            ),
            pytest.param(
                lambda: cf.user.rating(handle='nobody_0000'),
                'user.rating',
                'handle: User with handle nobody_0000 not found',
                cf.HandleNotFoundError,
                'Handle `nobody_0000` not found on Codeforces',
                id='rating of an unknown handle',
            ),
            pytest.param(
                lambda: cf.user.status(handle='ab'),
                'user.status',
                'handle: Field should contain between 3 and 24 characters, inclusive',
                cf.HandleInvalidError,
                '`ab` is not a valid Codeforces handle',
                id='status of a short handle',
            ),
        ],
    )
    async def test_refusals_the_callers_expect_are_info(
        self,
        codeforces: FakeCodeforces,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
        call: Callable[[], Awaitable[Any]],
        api_method: str,
        comment: str,
        error_type: type[cf.TrueApiError],
        message: str,
    ) -> None:
        codeforces.script(refusal(comment))

        with pytest.raises(error_type) as caught:
            await call()

        assert str(caught.value) == message
        assert len(codeforces.requests) == 1
        assert posted(logs) == []
        assert f'Query to CF API {api_method} failed: HTTP Error 400, {comment}' in (
            lines(logs, logging.INFO)
        )
        assert outage.told == [('answered', api_method)]

    async def test_a_handle_gone_from_codeforces_is_dropped_at_info(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture
    ) -> None:
        # ;handle unmagic_all, with one member's old handle and a deleted account
        codeforces.script(
            refusal('handles: User with handle gone_0000 not found'),
            answer(fixture('user_info.json')),
        )

        renamed = await cf.resolve_redirects(['tourist_2025', 'gone_0000'])

        assert {old: user.handle for old, user in renamed.items()} == {
            'tourist_2025': 'tourist'
        }
        assert [seen.form['handles'] for seen in codeforces.requests] == [
            ['tourist_2025;gone_0000'],
            ['tourist_2025'],
        ]
        assert posted(logs) == []
        assert 'Handle gone_0000 not found, dropping it.' in lines(logs, logging.INFO)


# --- Answers to retry ---


class TestTransientAnswers:
    async def test_a_502_page_then_an_answer_gives_the_result_without_a_warning(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture, outage: Outage
    ) -> None:
        codeforces.script(
            error_page(502), answer({'problems': [], 'problemStatistics': []})
        )

        assert await cf.problemset.problems() == ([], [])

        assert len(codeforces.requests) == 2
        assert lines(logs, logging.INFO).count(BAD_GATEWAY_LINE) == 1
        assert posted(logs) == []
        assert outage.told == [('answered', 'problemset.problems')]

    async def test_three_502_pages_give_up_with_info_lines_only(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture, outage: Outage
    ) -> None:
        codeforces.script(*error_pages(3))

        with pytest.raises(cf.ClientError) as caught:
            await cf.problemset.problems()

        assert repr(caught.value.__cause__) == repr(BAD_GATEWAY)
        assert str(caught.value) == 'Error connecting to Codeforces API'
        assert len(codeforces.requests) == 3
        assert {record.levelno for record in records(logs)} == {logging.INFO}
        info = lines(logs, logging.INFO)
        assert info.count(BAD_GATEWAY_LINE) == 3
        assert info[-1] == (
            'CF API problemset.problems failed 3 times, giving up: '
            "BadAnswer('HTTP 502 without JSON')"
        )
        assert outage.told == [('failed', 'problemset.problems', repr(BAD_GATEWAY))]

    @pytest.mark.parametrize(
        ('reply', 'detail'),
        [
            pytest.param(
                lambda: web.Response(body=b'', content_type='application/json'),
                'HTTP 200 without JSON',
                id='empty body',
            ),
            pytest.param(
                lambda: web.Response(), 'HTTP 200 without JSON', id='empty, no type'
            ),
            pytest.param(
                lambda: web.Response(body=b'null', content_type='application/json'),
                'HTTP 200 without JSON',
                id='null',
            ),
            pytest.param(
                lambda: web.Response(
                    body=b'{"status": "OK", "resu', content_type='application/json'
                ),
                'HTTP 200 without JSON',
                id='cut-off JSON',
            ),
            pytest.param(
                lambda: web.Response(
                    body=b'\xff\xfe{}', content_type='application/json'
                ),
                'HTTP 200 without JSON',
                id='not UTF-8',
            ),
            pytest.param(
                lambda: web.json_response([1, 2]),
                'HTTP 200 without a JSON object',
                id='a list',
            ),
            pytest.param(
                lambda: web.json_response({'status': 'OK'}),
                'HTTP 200 without a result',
                id='no result',
            ),
            pytest.param(
                lambda: error_page(503), 'HTTP 503 without JSON', id='503 page'
            ),
            pytest.param(
                lambda: error_page(403), 'HTTP 403 without JSON', id='403 page'
            ),
            pytest.param(
                lambda: refusal('Internal Server Error', status=500),
                'HTTP 500: Internal Server Error',
                id='500 with JSON',
            ),
        ],
    )
    async def test_an_answer_that_is_no_json_object_or_a_server_error_is_retried(
        self,
        codeforces: FakeCodeforces,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
        reply: Callable[[], web.Response],
        detail: str,
    ) -> None:
        codeforces.script(reply(), answer([]))

        assert await cf.contest.ratingChanges(contest_id=2273) == []

        assert len(codeforces.requests) == 2
        assert f'Request to CF API contest.ratingChanges failed: {detail}' in lines(
            logs, logging.INFO
        )
        assert posted(logs) == []
        assert outage.told == [('answered', 'contest.ratingChanges')]


# --- Requests that fail ---


class TestFailedRequests:
    async def test_a_timeout_is_retried(
        self,
        codeforces: FakeCodeforces,
        session: Session,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
    ) -> None:
        session.fail.append(asyncio.TimeoutError())
        codeforces.script(answer([]))

        assert await cf.contest.to_list(gym=False) == []

        assert len(session.sent) == 2
        assert len(codeforces.requests) == 1
        assert 'Request to CF API contest.list failed: TimeoutError()' in lines(
            logs, logging.INFO
        )
        assert posted(logs) == []
        assert outage.told == [('answered', 'contest.list')]

    async def test_three_timeouts_give_up_with_a_client_error(
        self,
        codeforces: FakeCodeforces,
        session: Session,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
    ) -> None:
        session.fail.extend(asyncio.TimeoutError() for _ in range(3))

        with pytest.raises(cf.ClientError) as caught:
            await cf.contest.to_list(gym=False)

        assert isinstance(caught.value.__cause__, asyncio.TimeoutError)
        assert codeforces.requests == []
        assert {record.levelno for record in records(logs)} == {logging.INFO}
        assert lines(logs, logging.INFO)[-1] == (
            'CF API contest.list failed 3 times, giving up: TimeoutError()'
        )
        assert outage.told == [('failed', 'contest.list', 'TimeoutError()')]

    async def test_a_dropped_connection_is_retried(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture
    ) -> None:
        codeforces.script(disconnect, answer([]))

        assert await cf.contest.to_list(gym=False) == []

        assert len(codeforces.requests) == 2
        assert posted(logs) == []

    async def test_three_dropped_connections_give_up_with_a_client_error(
        self,
        codeforces: FakeCodeforces,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
    ) -> None:
        codeforces.script(disconnect, disconnect, disconnect)

        with pytest.raises(cf.ClientError) as caught:
            await cf.contest.to_list(gym=False)

        assert isinstance(caught.value.__cause__, aiohttp.ClientError)
        assert len(codeforces.requests) == 3
        assert posted(logs) == []
        assert [told[:2] for told in outage.told] == [('failed', 'contest.list')]


# --- The call limit ---


class TestCallLimit:
    # Codeforces may give it any status: the comment decides.
    @pytest.mark.parametrize('status', [400, 503])
    async def test_the_call_limit_is_retried_at_info(
        self,
        codeforces: FakeCodeforces,
        logs: pytest.LogCaptureFixture,
        outage: Outage,
        status: int,
    ) -> None:
        codeforces.script(refusal('Call limit exceeded', status=status), answer([]))

        assert await cf.user.info(handles=['Alice']) == []

        assert len(codeforces.requests) == 2
        assert (
            f'Query to CF API user.info failed: HTTP Error {status}, '
            'Call limit exceeded'
        ) in lines(logs, logging.INFO)
        assert posted(logs) == []
        assert outage.told == [('answered', 'user.info')] * 2

    async def test_three_call_limits_in_a_row_are_one_warning(
        self, codeforces: FakeCodeforces, logs: pytest.LogCaptureFixture, outage: Outage
    ) -> None:
        codeforces.script(
            *(refusal('Call limit exceeded', status=503) for _ in range(3))
        )

        with pytest.raises(cf.CallLimitExceededError) as caught:
            await cf.user.info(handles=['Alice'])

        assert str(caught.value) == 'Codeforces API call limit exceeded'
        assert len(codeforces.requests) == 3
        assert posted(logs) == ['CF API user.info hit the call limit 3 times in a row']
        # Codeforces answered, so it is no outage.
        assert outage.told == [('answered', 'user.info')] * 3

    async def test_a_call_limit_after_a_timeout_gives_up_at_info(
        self,
        codeforces: FakeCodeforces,
        session: Session,
        logs: pytest.LogCaptureFixture,
    ) -> None:
        session.fail.append(asyncio.TimeoutError())
        codeforces.script(
            *(refusal('Call limit exceeded', status=503) for _ in range(2))
        )

        with pytest.raises(cf.CallLimitExceededError):
            await cf.user.info(handles=['Alice'])

        assert posted(logs) == []
        assert lines(logs, logging.INFO)[-1] == (
            'CF API user.info failed 3 times, giving up: '
            "CallLimitExceededError('Codeforces API call limit exceeded')"
        )


# --- Spacing and tries ---


class FakeTime:
    """Epoch seconds that move only when cf_ratelimit sleeps, at once."""

    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class TestCallSpacing:
    """``cf_ratelimit`` around a stand-in for a query."""

    @pytest.fixture
    def fake_time(self, monkeypatch: pytest.MonkeyPatch) -> FakeTime:
        fake = FakeTime()
        monkeypatch.setattr(cf, 'time', SimpleNamespace(time=fake.time))
        monkeypatch.setattr(cf, 'asyncio', SimpleNamespace(sleep=fake.sleep))
        return fake

    async def test_calls_are_spaced_by_the_spacing_of_the_moment(
        self, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def query(path: str) -> str:
            return path

        limited = cf.cf_ratelimit(query)

        assert await limited('contest.list') == 'contest.list'
        assert await limited('user.info') == 'user.info'
        monkeypatch.setattr(cf, '_CALL_SPACING_SECONDS', 0)
        await limited('user.info')
        monkeypatch.setattr(cf, '_CALL_SPACING_SECONDS', 2)
        await limited('user.info')

        assert fake_time.sleeps == [1, 2]

    async def test_a_failing_call_is_tried_three_times_a_second_apart(
        self, fake_time: FakeTime, outage: Outage, logs: pytest.LogCaptureFixture
    ) -> None:
        tries = []

        async def query(path: str) -> None:
            tries.append(fake_time.now - T0)
            raise cf.ClientError() from asyncio.TimeoutError()

        with pytest.raises(cf.ClientError):
            await cf.cf_ratelimit(query)('contest.list')

        assert tries == [0, 1, 2]
        assert lines(logs, logging.INFO) == [
            'CF API contest.list failed 3 times, giving up: TimeoutError()'
        ]
        assert outage.told == [('failed', 'contest.list', 'TimeoutError()')]


# --- The outage tracker ---


def fail(
    tracker: cf._Outage,
    clock: FakeClock,
    minutes: float,
    path: str = 'problemset.problems',
    error: BaseException = BAD_GATEWAY,
) -> None:
    clock.at(minutes)
    tracker.failed(path, error)


def answered(
    tracker: cf._Outage, clock: FakeClock, minutes: float, path: str = 'contest.list'
) -> None:
    clock.at(minutes)
    tracker.answered(path)


def ends(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The tracker's INFO lines, on the ends of outages."""
    return [
        line
        for line in lines(caplog, logging.INFO)
        if line.startswith('Codeforces answers')
    ]


# Minutes from a streak's latest failure to the next one, and the WARNINGs: under
# 3 hours the streak goes on and warns, and at 3 hours a new one starts.
STREAK_GAPS = [
    pytest.param(3 * 60 - 0.01, 1, id='under 3 hours: one streak'),
    pytest.param(3 * 60, 0, id='3 hours: a new streak'),
]


class TestOutageTracker:
    def test_everything_down_warns_once_at_15_minutes(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'problemset.problems')
        fail(outage, clock, 5, 'contest.list')
        fail(outage, clock, 10, 'user.info')
        fail(outage, clock, 14.99, 'contest.list')
        assert posted(logs) == []

        fail(outage, clock, 15, 'user.info')
        assert posted(logs) == [
            'Codeforces has failed every call since 23:00 UTC (5 calls); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]

        # A day more of it, with contest.list tried every 5 minutes while it
        # fails: still one streak, as no failure comes 3 hours after the last.
        for minutes in range(20, 24 * 60 + 1, 5):
            fail(outage, clock, minutes, 'contest.list')
        assert len(posted(logs)) == 1

    def test_one_method_down_while_others_work_warns_once(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, 5, 'contest.standings')
        fail(outage, clock, 10, 'contest.standings')
        answered(outage, clock, 12, 'contest.list')
        # 15 minutes after its first failure, but not yet since others worked
        fail(outage, clock, 15.99, 'contest.standings')
        assert posted(logs) == []

        fail(outage, clock, 16, 'contest.standings')
        assert posted(logs) == [
            'Codeforces has failed every contest.standings call since 23:00 UTC '
            "(5 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

        answered(outage, clock, 17, 'user.info')
        fail(outage, clock, 20, 'contest.standings')
        answered(outage, clock, 30, 'user.info')
        fail(outage, clock, 60, 'contest.standings')
        assert len(posted(logs)) == 1
        assert ends(logs) == [], 'the other calls never failed for long'

    def test_a_method_failing_again_as_codeforces_recovers_is_not_warned_about(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.list')
        fail(outage, clock, 2, 'contest.standings')
        fail(outage, clock, 15, 'contest.list')
        answered(outage, clock, 30, 'contest.list')
        # Its first call since the outage ended fails once more.
        fail(outage, clock, 32, 'contest.standings')
        assert posted(logs) == [
            'Codeforces has failed every call since 23:00 UTC (3 calls); the last: '
            "contest.list: BadAnswer('HTTP 502 without JSON')"
        ]

        # It still fails 15 minutes after the other calls work again.
        fail(outage, clock, 44.99, 'contest.standings')
        assert len(posted(logs)) == 1
        fail(outage, clock, 45, 'contest.standings')
        assert posted(logs)[1:] == [
            'Codeforces has failed every contest.standings call since 23:02 UTC '
            "(4 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

    def test_a_failure_after_a_short_outage_of_every_call_logs_nothing(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.list')
        fail(outage, clock, 1, 'contest.standings')
        answered(outage, clock, 10, 'contest.list')
        fail(outage, clock, 16, 'contest.standings')

        assert posted(logs) == []

    def test_a_method_failing_within_a_longer_outage_is_not_warned_about(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, 10, 'user.info')
        fail(outage, clock, 25, 'user.info')  # everything down since 10
        fail(outage, clock, 30, 'contest.standings')

        assert posted(logs) == [
            'Codeforces has failed every call since 23:10 UTC (2 calls); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]

    def test_the_end_of_an_outage_is_info(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0)
        fail(outage, clock, 15)

        answered(outage, clock, 25)

        assert ends(logs) == ['Codeforces answers again after 25 minutes of failures']
        assert len(posted(logs)) == 1

    def test_the_end_of_one_method_s_outage_is_info(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, 16, 'contest.standings')
        answered(outage, clock, 30, 'user.info')
        assert len(posted(logs)) == 1
        assert ends(logs) == []

        answered(outage, clock, 65, 'contest.standings')

        assert ends(logs) == [
            'Codeforces answers contest.standings again after 1 hour 5 minutes'
        ]

    def test_a_short_outage_logs_nothing(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'problemset.problems')
        fail(outage, clock, 7, 'contest.list')
        fail(outage, clock, 14.99, 'problemset.problems')
        answered(outage, clock, 14.99, 'contest.list')

        assert records(logs) == []

    def test_a_new_outage_after_an_answer_warns_again(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0)
        fail(outage, clock, 15)
        answered(outage, clock, 20)

        fail(outage, clock, 60, 'contest.list')
        fail(outage, clock, 70, 'user.info')
        assert len(posted(logs)) == 1
        fail(outage, clock, 75, 'user.info')
        answered(outage, clock, 80)

        assert posted(logs)[1:] == [
            'Codeforces has failed every call since 00:00 UTC (3 calls); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]
        assert ends(logs) == [
            'Codeforces answers again after 20 minutes of failures',
            'Codeforces answers again after 20 minutes of failures',
        ]

    def test_a_method_failing_again_days_later_starts_a_new_streak(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        # The ranklist monitor's last call for one contest, then its first for
        # the next, on 15 October at 14:35 UTC
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        later = (2 * 24 + 15) * 60 + 35
        fail(outage, clock, later, 'contest.standings')
        assert posted(logs) == []

        answered(outage, clock, later + 1, 'user.info')
        fail(outage, clock, later + 16, 'contest.standings')
        assert posted(logs) == [
            'Codeforces has failed every contest.standings call since 14:35 UTC '
            "(2 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

    def test_a_short_outage_days_after_a_method_failed_logs_nothing(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.status')
        answered(outage, clock, 0.1, 'user.info')
        # 2 days later every call fails for 5 minutes, that method first.
        start = 2 * 24 * 60
        fail(outage, clock, start, 'contest.status')
        fail(outage, clock, start + 2, 'user.info')
        fail(outage, clock, start + 4, 'contest.status')
        answered(outage, clock, start + 5, 'contest.list')

        assert records(logs) == []

    def test_a_long_outage_a_day_after_a_method_failed_warns_once(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'user.rating')
        answered(outage, clock, 0.1, 'user.info')
        # From 09:00 UTC on 14 October every call fails for 35 minutes, that
        # method first.
        start = (24 + 10) * 60
        fail(outage, clock, start, 'user.rating')
        for minutes in range(5, 35, 5):
            fail(outage, clock, start + minutes, 'contest.list')
        answered(outage, clock, start + 35, 'contest.list')

        assert posted(logs) == [
            'Codeforces has failed every call since 09:00 UTC (4 calls); the last: '
            "contest.list: BadAnswer('HTTP 502 without JSON')"
        ]
        assert ends(logs) == ['Codeforces answers again after 35 minutes of failures']

    def test_a_method_down_alone_and_called_hourly_warns_once(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        # The hourly problem-list run, while contest.list is answered every 30
        # minutes
        for run in range(6):
            fail(outage, clock, run * 61, 'problemset.problems')
            answered(outage, clock, run * 61 + 1)
            answered(outage, clock, run * 61 + 31)

        assert posted(logs) == [
            'Codeforces has failed every problemset.problems call since 23:00 UTC '
            "(2 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

    @pytest.mark.parametrize(('minutes', 'warnings'), STREAK_GAPS)
    def test_a_method_s_streak_ends_when_its_latest_failure_is_3_hours_old(
        self,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
        minutes: float,
        warnings: int,
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, minutes, 'contest.standings')

        assert len(posted(logs)) == warnings

    @pytest.mark.parametrize(('minutes', 'warnings'), STREAK_GAPS)
    def test_the_streak_of_every_call_ends_when_its_latest_failure_is_3_hours_old(
        self,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
        minutes: float,
        warnings: int,
    ) -> None:
        fail(outage, clock, 0, 'contest.list')
        fail(outage, clock, minutes, 'user.info')

        assert len(posted(logs)) == warnings

    def test_failures_3_hours_apart_with_no_call_between_are_no_outage(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.list')
        # No call for 3 hours, then failures every 5 minutes from 02:00 UTC
        for minutes in (180, 185, 190):
            fail(outage, clock, minutes, 'user.info')
        assert posted(logs) == []

        fail(outage, clock, 195, 'contest.list')
        assert posted(logs) == [
            'Codeforces has failed every call since 02:00 UTC (4 calls); the last: '
            "contest.list: BadAnswer('HTTP 502 without JSON')"
        ]

    def test_a_method_failing_as_every_call_begins_to_fail_is_not_warned_about(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 0.1, 'user.info')
        # Every call fails from 23:01; the method's own 15 minutes come first.
        fail(outage, clock, 1, 'contest.list')
        fail(outage, clock, 5, 'user.info')
        fail(outage, clock, 10, 'user.info')
        fail(outage, clock, 15.5, 'contest.standings')
        assert posted(logs) == []

        fail(outage, clock, 16, 'contest.list')
        assert posted(logs) == [
            'Codeforces has failed every call since 23:01 UTC (5 calls); the last: '
            "contest.list: BadAnswer('HTTP 502 without JSON')"
        ]

    def test_a_method_warned_about_warns_again_in_a_new_streak(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, 16, 'contest.standings')
        assert len(posted(logs)) == 1

        # Never answered itself, it fails again 3 hours after its last failure.
        fail(outage, clock, 16 + 180, 'contest.standings')
        answered(outage, clock, 16 + 181, 'user.info')
        fail(outage, clock, 16 + 196, 'contest.standings')

        assert posted(logs)[1:] == [
            'Codeforces has failed every contest.standings call since 02:16 UTC '
            "(2 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

    def test_a_method_due_both_warnings_at_once_gets_its_own(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        # Its 15 minutes since the answer, and every call's since 23:01:30, end
        # together: only this method failed, so that is what it says.
        fail(outage, clock, 1.5, 'contest.standings')
        fail(outage, clock, 10, 'contest.standings')
        fail(outage, clock, 16.5, 'contest.standings')

        assert posted(logs) == [
            'Codeforces has failed every contest.standings call since 23:00 UTC '
            "(4 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

    def test_a_method_reported_alone_is_not_reported_again_as_every_call(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        # No other call is made while it keeps failing.
        for minutes in (2, 5, 10, 16, 17, 30):
            fail(outage, clock, minutes, 'contest.standings')

        assert posted(logs) == [
            'Codeforces has failed every contest.standings call since 23:00 UTC '
            "(5 calls), while other calls work; the last: BadAnswer('HTTP 502 "
            "without JSON')"
        ]

        # Then the other calls fail too: that is news.
        fail(outage, clock, 31, 'user.info')
        assert posted(logs)[1:] == [
            'Codeforces has failed every call since 23:02 UTC (7 calls); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]

    def test_an_answer_logs_no_end_for_a_streak_3_hours_old(
        self, outage: Outage, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        fail(outage, clock, 0, 'contest.standings')
        answered(outage, clock, 1, 'user.info')
        fail(outage, clock, 16, 'contest.standings')
        fail(outage, clock, 20)
        fail(outage, clock, 35)
        assert len(posted(logs)) == 2

        answered(outage, clock, 35 + 180, 'contest.standings')

        assert ends(logs) == []

    def test_one_call_is_singular(
        self,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(cf, 'OUTAGE_WARNING_AFTER', 0)

        fail(outage, clock, 0, 'user.info')

        assert posted(logs) == [
            'Codeforces has failed every call since 23:00 UTC (1 call); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]

    def test_its_clock_gives_epoch_seconds_by_default(self) -> None:
        assert cf._Outage()._clock is time.time

    def test_times_are_in_utc_whatever_the_local_time_zone(
        self,
        local_time_9_hours_ahead: None,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
    ) -> None:
        assert time.localtime(T0).tm_hour == 8, 'the local time zone took effect'

        fail(outage, clock, 0)
        fail(outage, clock, 15)

        assert posted(logs) == [
            'Codeforces has failed every call since 23:00 UTC (2 calls); the last: '
            "problemset.problems: BadAnswer('HTTP 502 without JSON')"
        ]

    @pytest.mark.parametrize(
        ('seconds', 'text'),
        [
            (0, '0 minutes'),
            (59, '0 minutes'),
            (60, '1 minute'),
            (15 * MINUTE, '15 minutes'),
            (60 * MINUTE, '1 hour'),
            (61 * MINUTE, '1 hour 1 minute'),
            (125 * MINUTE, '2 hours 5 minutes'),
            (26 * 60 * MINUTE, '26 hours'),
            (-30, '0 minutes'),  # the clock went back
        ],
    )
    def test_durations(self, seconds: float, text: str) -> None:
        assert cf._duration(seconds) == text

    def test_a_fault_of_its_own_never_raises(
        self,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fail(outage, clock, 0)
        fail(outage, clock, 15)
        monkeypatch.setattr(outage, '_clock', broken_clock)

        outage.answered('contest.list')
        outage.failed('contest.list', BAD_GATEWAY)

        fault = 'Outage tracker failed on CF API contest.list, ignoring.'
        faults = [r for r in records(logs) if r.getMessage() == fault]
        assert [(record.levelno, record.exc_info[0]) for record in faults] == [
            (logging.WARNING, RuntimeError),
            (logging.WARNING, RuntimeError),
        ]

    def test_an_error_it_cannot_show_never_raises(
        self, clock: FakeClock, logs: pytest.LogCaptureFixture
    ) -> None:
        class Unshowable(Exception):
            def __repr__(self) -> str:
                raise ValueError('no repr')

        tracker = cf._Outage(clock)
        fail(tracker, clock, 0, error=Unshowable())
        fail(tracker, clock, 15, error=Unshowable())

        assert posted(logs) == [
            'Outage tracker failed on CF API problemset.problems, ignoring.'
        ]


class TestOutagesThroughTheClient:
    async def test_a_long_outage_warns_once_and_its_end_is_info(
        self,
        codeforces: FakeCodeforces,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[tuple[float, Callable[[], Awaitable[Any]]]] = [
            (0, cf.problemset.problems),
            (10, lambda: cf.contest.to_list(gym=False)),
            (15, lambda: cf.user.info(handles=['Alice'])),
            (30, lambda: cf.contest.to_list(gym=False)),
        ]
        for minutes, call in calls:
            clock.at(minutes)
            codeforces.script(*error_pages(3))
            with pytest.raises(cf.ClientError):
                await call()

        assert posted(logs) == [
            'Codeforces has failed every call since 23:00 UTC (3 calls); the last: '
            "user.info: BadAnswer('HTTP 502 without JSON')"
        ]
        (warning,) = [r for r in records(logs) if r.levelno >= logging.WARNING]
        assert warning.exc_info is None, 'one line, without a traceback'

        clock.at(40)
        codeforces.script(answer([]))
        assert await cf.contest.to_list(gym=False) == []

        assert ends(logs) == ['Codeforces answers again after 40 minutes of failures']
        assert len(posted(logs)) == 1

    async def test_a_tracker_fault_never_fails_a_call(
        self,
        codeforces: FakeCodeforces,
        outage: Outage,
        clock: FakeClock,
        logs: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fail(outage, clock, 0)
        fail(outage, clock, 15)  # warned, so the next answer reads the clock
        monkeypatch.setattr(outage, '_clock', broken_clock)

        codeforces.script(answer([]))
        assert await cf.contest.to_list(gym=False) == []

        codeforces.script(*error_pages(3))
        with pytest.raises(cf.ClientError):
            await cf.contest.to_list(gym=False)

        assert posted(logs)[1:] == [
            'Outage tracker failed on CF API contest.list, ignoring.',
            'Outage tracker failed on CF API contest.list, ignoring.',
        ]
