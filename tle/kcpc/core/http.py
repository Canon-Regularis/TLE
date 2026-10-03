"""Paced, retrying HTTP client for the external sites KCPC reads.

Every KCPC request to another site goes through one ``HttpClient``. It keeps
to each host's ``HostPolicy`` (request spacing, a per-minute cap, extra
headers), retries 429s, 5xx responses, connection errors and timeouts with
backoff, and reports failure as ``ExternalServiceError``, whose message can be
shown to users as-is.
"""

import asyncio
import codecs
import email.message
import json
import logging
import random
import re
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import aiohttp

from tle.kcpc.core.clock import UTC, Clock
from tle.kcpc.core.errors import ExternalServiceError

logger = logging.getLogger(__name__)

# HostPolicy.max_per_minute counts request starts in a sliding window this long.
_RATE_WINDOW_SECONDS = 60.0

# Backoff waits are stretched by a random 0-25%, so that tasks which failed
# together don't all retry at the same moment.
_BACKOFF_JITTER = 0.25

# A request that fails for good is logged at WARNING, which reaches the Discord
# log channel, at most this often per host. Repeats during the same outage are
# logged at INFO.
_FAILURE_WARNING_INTERVAL = 3600.0

# Retry-After as delay-seconds. RFC 9110 allows only digits; a fraction is
# tolerated, but not a sign, 'nan' or 'inf', which float() would accept.
_RETRY_AFTER_SECONDS = re.compile(r'\d+(?:\.\d+)?', re.ASCII)

# Failures that say nothing about the request itself, so a retry may succeed.
# OSError covers any socket error that reaches us unwrapped by aiohttp; before
# Python 3.11, asyncio.TimeoutError is not an OSError.
_TRANSIENT_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


@dataclass(frozen=True)
class HostPolicy:
    """How to treat one host: request pacing, retries and extra headers."""

    min_interval: float = 0.0  # seconds between request starts to the host
    max_per_minute: int | None = None  # cap on request starts in any 60 s window
    max_attempts: int = 3
    backoff_base: float = 1.0  # retries wait 1, 2, 4 ... times this, plus jitter
    backoff_cap: float = 60.0  # longest wait between attempts, Retry-After too
    # Merged over the client's defaults. Left out of the hash because mappings
    # aren't hashable, which would make every policy unhashable.
    headers: Mapping[str, str] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if self.min_interval < 0:
            raise ValueError('min_interval must not be negative')
        if self.max_per_minute is not None and self.max_per_minute < 1:
            raise ValueError('max_per_minute must be at least 1')
        if self.max_attempts < 1:
            raise ValueError('max_attempts must be at least 1')
        if self.backoff_base < 0 or self.backoff_cap < 0:
            raise ValueError('backoff_base and backoff_cap must not be negative')
        # A read-only copy, so that changing the dict the caller passed in can't
        # change a frozen policy (or DEFAULT_POLICIES) behind our back.
        object.__setattr__(self, 'headers', MappingProxyType(dict(self.headers)))


DEFAULT_POLICY = HostPolicy(min_interval=0.5)

# Codeforces serves its HTML pages from behind Cloudflare, which challenges
# clients that don't look like a browser. Chrome's reduced User-Agent only
# reports the major version; bump it now and then.
_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,'
        'image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

DEFAULT_POLICIES: Mapping[str, HostPolicy] = MappingProxyType(
    {
        'atcoder.jp': HostPolicy(min_interval=2.0),
        'kenkoooo.com': HostPolicy(min_interval=1.1),
        'clist.by': HostPolicy(max_per_minute=10),
        'api.lu.ma': HostPolicy(min_interval=1.0),
        'icpc.global': HostPolicy(min_interval=1.0),
        'codeforces.com': HostPolicy(min_interval=2.0, headers=_BROWSER_HEADERS),
    }
)


class RateLimiter:
    """Paces the starts of requests to one host according to its policy.

    Callers go through one at a time, in arrival order, so a burst of
    concurrent requests is spread out instead of released all at once.
    """

    def __init__(self, policy: HostPolicy, clock: Clock) -> None:
        self._policy = policy
        self._clock = clock
        self._lock = asyncio.Lock()
        # Monotonic start times of the latest requests: min_interval needs only
        # the newest, the sliding window needs the last max_per_minute.
        self._starts: deque[float] = deque(maxlen=policy.max_per_minute or 1)

    async def acquire(self) -> None:
        """Wait until the policy allows another request, and record its start."""
        async with self._lock:
            delay = self._delay(self._clock.monotonic())
            if delay > 0:
                # One wait is enough: only the lock holder records starts, so
                # nothing can tighten the limits while we sleep.
                await self._clock.sleep(delay)
            self._starts.append(self._clock.monotonic())

    def _delay(self, now: float) -> float:
        """Seconds from ``now`` until a request may start (<= 0: straight away)."""
        if not self._starts:
            return 0.0
        delay = self._starts[-1] + self._policy.min_interval - now
        cap = self._policy.max_per_minute
        if cap is not None and len(self._starts) == cap:
            # The oldest of the last `cap` starts has to leave the window first.
            delay = max(delay, self._starts[0] + _RATE_WINDOW_SECONDS - now)
        return delay


@dataclass(frozen=True)
class HttpResponse:
    """A response whose body has been read in full."""

    url: str  # the final URL, after any redirects
    status: int
    headers: Mapping[str, str]  # case-insensitive: aiohttp's CIMultiDictProxy
    body: bytes = field(repr=False)

    def text(self, encoding: str | None = None) -> str:
        """Decode the body: ``encoding``, else the Content-Type charset, else UTF-8.

        Bytes that don't decode become U+FFFD instead of raising.
        """
        return self.body.decode(encoding or self._charset() or 'utf-8', 'replace')

    def json(self) -> Any:
        """Parse the body as JSON. Raises ``ValueError`` if it isn't JSON."""
        return json.loads(self.body)

    def _charset(self) -> str | None:
        """The Content-Type charset, if there is one and Python has a codec."""
        content_type = self.headers.get('Content-Type')
        if not content_type:
            return None
        # email.message is the standard library's parser for MIME parameters.
        message = email.message.Message()
        message['Content-Type'] = content_type
        charset = message.get_content_charset()
        if charset is None:
            return None
        try:
            return codecs.lookup(charset).name
        except LookupError:
            return None


@dataclass(frozen=True)
class _Failure:
    """An attempt that failed in a way that a later attempt might not."""

    problem: str  # for the logs: 'HTTP 503', 'ServerDisconnectedError: ...'
    status: int | None = None
    retry_after: float | None = None  # seconds, from a Retry-After header


class HttpClient:
    """The HTTP client for every request KCPC makes to an external site.

    ``policies`` maps hosts to their ``HostPolicy`` and replaces
    ``DEFAULT_POLICIES`` when given; every other host gets ``default_policy``.
    ``timeout`` bounds each attempt, from connecting to reading the whole body.
    """

    def __init__(
        self,
        *,
        user_agent: str,
        clock: Clock,
        policies: Mapping[str, HostPolicy] | None = None,
        default_policy: HostPolicy = DEFAULT_POLICY,
        timeout: float = 30.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        chosen = DEFAULT_POLICIES if policies is None else policies
        self._policies = {_normalize_host(h): p for h, p in chosen.items()}
        self._default_policy = default_policy
        self._user_agent = user_agent
        self._clock = clock
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._limiters: dict[str, RateLimiter] = {}
        self._last_warning: dict[str, float] = {}  # host -> clock.monotonic()
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    def policy_for(self, url: str) -> HostPolicy:
        """The policy for ``url``: an exact host match, ignoring a leading 'www.'."""
        return self._policies.get(_host_of(url), self._default_policy)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        service: str | None = None,
        allow_status: Collection[int] = (),
    ) -> HttpResponse:
        """GET ``url``, paced and retried according to its host's policy.

        Returns the response if its status is 2xx or in ``allow_status``. Any
        other status apart from 429 and 5xx raises ``ExternalServiceError`` at
        once. 429s, 5xx responses, connection errors and timeouts are retried
        up to the policy's ``max_attempts``, then raise ``ExternalServiceError``
        with the final attempt's status (None if it got no response).
        ``service`` names the site in error messages; it defaults to the host.
        """
        host = _host_of(url)
        policy = self._policies.get(host, self._default_policy)
        limiter = self._limiter_for(host, policy)
        service = service or host
        request_headers = _merge_headers(
            {'User-Agent': self._user_agent}, policy.headers, headers or {}
        )
        for attempt in range(1, policy.max_attempts + 1):
            await limiter.acquire()
            try:
                response = await self._send(url, params, request_headers)
            except _TRANSIENT_ERRORS as exc:
                failure = _Failure(_describe_error(exc))
            else:
                status = response.status
                if 200 <= status < 300 or status in allow_status:
                    return response
                if not _is_retryable(status):
                    self._log_failure(host, 'GET %s failed: HTTP %d', url, status)
                    raise ExternalServiceError(
                        service,
                        f'{service} returned an error (HTTP {status}).',
                        status=status,
                    )
                retry_after = _retry_after_seconds(
                    response.headers.get('Retry-After'), self._clock.now()
                )
                failure = _Failure(
                    f'HTTP {status}', status=status, retry_after=retry_after
                )
            if attempt < policy.max_attempts:
                await self._wait_to_retry(url, policy, attempt, failure)
        # max_attempts >= 1, so the loop ran, and every pass that got to the end
        # recorded a failure.
        self._log_failure(
            host,
            'GET %s failed after %d attempt(s): %s',
            url,
            policy.max_attempts,
            failure.problem,
        )
        raise ExternalServiceError(
            service,
            f'{service} is not responding right now. Please try again later.',
            status=failure.status,
        )

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        """``get`` the URL and parse the body as JSON.

        A body that isn't JSON raises ``ExternalServiceError`` as well: to the
        user, a garbled answer is no better than none.
        """
        response = await self.get(url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            host = _host_of(url)
            self._log_failure(host, 'GET %s returned a body that is not JSON', url)
            service = kwargs.get('service') or host
            raise ExternalServiceError(
                service,
                f'{service} returned an unexpected response.',
                status=response.status,
            ) from exc

    async def get_text(self, url: str, **kwargs: Any) -> str:
        """``get`` the URL and decode the body, as ``HttpResponse.text`` does."""
        return (await self.get(url, **kwargs)).text()

    async def close(self) -> None:
        """Close the connection pool. Idempotent; the client can't be used after."""
        self._closed = True
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    def _limiter_for(self, host: str, policy: HostPolicy) -> RateLimiter:
        limiter = self._limiters.get(host)
        if limiter is None:
            limiter = self._limiters[host] = RateLimiter(policy, self._clock)
        return limiter

    def _open_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError('HttpClient is closed')
        if self._session is None:
            # Created on first use, so that it belongs to the running loop.
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def _send(
        self,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
    ) -> HttpResponse:
        """Make one request and read the whole body."""
        session = self._open_session()
        async with session.get(url, params=params, headers=headers) as response:
            body = await response.read()
        return HttpResponse(
            url=str(response.url),
            status=response.status,
            headers=response.headers,
            body=body,
        )

    async def _wait_to_retry(
        self, url: str, policy: HostPolicy, attempt: int, failure: _Failure
    ) -> None:
        """Sleep after failed attempt number ``attempt`` before the next one."""
        if failure.retry_after is not None:
            delay = min(policy.backoff_cap, failure.retry_after)
        else:
            delay = _backoff_seconds(policy, attempt)
        logger.debug(
            'GET %s failed (%s); retrying in %.1fs', url, failure.problem, delay
        )
        await self._clock.sleep(delay)

    def _log_failure(self, host: str, message: str, *args: object) -> None:
        """Log a request that failed for good, rate-limiting WARNINGs per host."""
        now = self._clock.monotonic()
        last = self._last_warning.get(host)
        if last is None or now - last >= _FAILURE_WARNING_INTERVAL:
            self._last_warning[host] = now
            logger.warning(message, *args)
        else:
            logger.info(message, *args)


def _normalize_host(host: str) -> str:
    """Lowercase ``host`` and drop a leading 'www.', so both share a policy."""
    return host.lower().removeprefix('www.')


def _host_of(url: str) -> str:
    """The normalized host of an absolute http(s) URL."""
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError(f'Expected an absolute http(s) URL, got {url!r}')
    return _normalize_host(parts.hostname)


def _merge_headers(*layers: Mapping[str, str]) -> dict[str, str]:
    """Merge header mappings; later layers win, matching names case-insensitively."""
    merged: dict[str, tuple[str, str]] = {}
    for layer in layers:
        for name, value in layer.items():
            merged[name.lower()] = (name, value)
    return dict(merged.values())


def _is_retryable(status: int) -> bool:
    """429 and 5xx may succeed later; other error statuses won't."""
    return status == 429 or 500 <= status <= 599


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """Seconds to wait according to a Retry-After value, or None if unusable.

    The value is either a number of seconds or an HTTP date.
    """
    if value is None:
        return None
    value = value.strip()
    if _RETRY_AFTER_SECONDS.fullmatch(value):
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (ValueError, OverflowError):  # a hostile date can overflow a C int
        return None
    if when.tzinfo is None:  # a '-0000' zone parses as naive; HTTP dates are UTC
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def _backoff_seconds(policy: HostPolicy, failed_attempts: int) -> float:
    """Seconds to back off after ``failed_attempts`` failed attempts.

    That is base, 2 x base, 4 x base and so on, stretched by up to 25% jitter
    and never longer than the policy's cap.
    """
    # Bounding the exponent only keeps the arithmetic finite; the cap applies
    # long before it matters.
    delay = policy.backoff_base * 2.0 ** min(failed_attempts - 1, 32)
    return min(policy.backoff_cap, delay * (1 + random.uniform(0, _BACKOFF_JITTER)))


def _describe_error(exc: BaseException) -> str:
    """'TypeName: message' for the logs; timeouts often have no message."""
    detail = str(exc)
    return f'{type(exc).__name__}: {detail}' if detail else type(exc).__name__
