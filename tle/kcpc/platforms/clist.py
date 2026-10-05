"""Contests on other sites, read from clist.by's contest API.

clist.by gathers the contests of many sites, each a "resource" named by its
host ('codechef.com'), and serves them as JSON at ``API_URL`` to anyone with a
free account. Every request carries the account's username and API key in its
Authorization header: never in the URL, which logs and the API's own paging
links would show. A list comes in pages, ``{"meta": {...}, "objects": [...]}``,
where ``meta.next`` is the next page's URL relative to clist.by, or null on
the last; it can be there on a page that isn't full. Times are ISO 8601 in UTC
with no zone, '2026-10-03T12:00:00'. ``upcoming=true`` lists the contests that
haven't ended, so running ones too. A resource that clist.by doesn't have gets
an empty list, just as one with no contests coming does: clist.by's list of
resources, at ``resource/`` beside ``API_URL``, tells the two apart.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from urllib.parse import urljoin, urlsplit

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.timeutil import ensure_utc

logger = logging.getLogger(__name__)

API_URL = 'https://clist.by/api/v4/contest/'
SERVICE = 'clist.by'

_PAGE_SIZE = 100
# The most pages read for one list: far more contests than any site lists
# ahead, in few enough requests for clist.by's 10 a minute.
_MAX_PAGES = 5
_REFUSED_STATUSES = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})
_REFUSED = 'clist.by refused the API key. Check CLIST_USERNAME and CLIST_API_KEY.'
# What a contest can't do without.
_REQUIRED = ('id', 'event', 'start', 'end', 'href')


@dataclass(frozen=True)
class ClistContest:
    """A contest that clist.by lists.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted), and ``end`` is after ``start``.
    """

    clist_id: int  # clist.by's ID of the contest
    resource: str  # its site's host, e.g. 'codechef.com'; '' if not given
    name: str  # the API's 'event'
    start: datetime
    end: datetime
    url: str  # the API's 'href': the contest's page on its site

    def __post_init__(self) -> None:
        # Converted here too, so that contests built by hand (in tests, say)
        # compare exactly like parsed ones.
        start = ensure_utc(self.start).replace(microsecond=0)
        end = ensure_utc(self.end).replace(microsecond=0)
        if end <= start:
            raise ValueError(
                f'clist.by contest {self.clist_id} must end after it starts'
            )
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)


def parse_contest(data: object) -> ClistContest | None:
    """The contest in one object of clist.by's contest list, or None.

    None, logged at DEBUG, for an object missing any of id, event, start, end
    and href, or with one that can't be read: an ID that isn't an integer, a
    blank name, a time that isn't ISO 8601, an end that isn't after the start,
    or a link that isn't an absolute http(s) URL. A time with no zone is UTC.
    """
    if not isinstance(data, dict):
        logger.debug('Skipping a clist.by contest that is a %s', type(data).__name__)
        return None
    missing = [key for key in _REQUIRED if data.get(key) is None]
    if missing:
        logger.debug(
            'Skipping clist.by contest %r, which has no %s',
            data.get('id'),
            ', '.join(missing),
        )
        return None
    clist_id = data['id']
    name = _text(data['event'])
    start = _utc_time(data['start'])
    end = _utc_time(data['end'])
    url = _web_url(data['href'])
    if (
        # bool is a subclass of int, but true is no ID.
        not isinstance(clist_id, int)
        or isinstance(clist_id, bool)
        or not name
        or start is None
        or end is None
        or end <= start
        or url is None
    ):
        logger.debug(
            'Skipping clist.by contest %r, which can not be read: %r',
            clist_id,
            {key: data[key] for key in _REQUIRED},
        )
        return None
    return ClistContest(
        clist_id=clist_id,
        resource=_text(data.get('resource')),
        name=name,
        start=start,
        end=end,
        url=url,
    )


class ClistClient:
    """Fetches contests from clist.by through the shared ``HttpClient``.

    The username and API key go only in the Authorization header of its
    requests: never in a URL, a log line, an error or the client's repr.
    """

    def __init__(self, http: HttpClient, *, username: str, api_key: str) -> None:
        self._http = http
        self._authorization = f'ApiKey {username}:{api_key}'

    async def upcoming(
        self, resource: str, *, event_regex: str | None = None
    ) -> list[ClistContest]:
        """The contests of ``resource`` (a host, such as 'codechef.com') that
        haven't ended, by start.

        With ``event_regex``, only those whose name matches it, ignoring case.
        A contest that can't be read is left out (see ``parse_contest``), and
        so is a second listing of one. Raises ``ExternalServiceError`` if
        clist.by can't be reached, refuses the key, or sends a list that can't
        be read or that doesn't end within 5 pages. It raises as well, rather
        than return no contests, if the list has contests but none that can be
        read, or has none and clist.by has no resource named ``resource``:
        clist.by's format or the site's name has changed, and an empty list
        would make every contest of a complete source look cancelled.
        """
        params = {
            'upcoming': 'true',
            'resource': resource,
            'order_by': 'start',
            'limit': str(_PAGE_SIZE),
        }
        if event_regex is not None:
            params['event__iregex'] = event_regex
        contests: dict[int, ClistContest] = {}
        listed = 0  # the objects in the list, whether they can be read or not
        url = API_URL
        query: Mapping[str, str] | None = params
        for _ in range(_MAX_PAGES):
            objects, next_page = _read_page(await self._get(url, query))
            listed += len(objects)
            for item in objects:
                contest = parse_contest(item)
                if contest is None:
                    continue
                if contest.clist_id in contests:
                    logger.debug('clist.by listed contest %d twice', contest.clist_id)
                    continue
                contests[contest.clist_id] = contest
            # An empty page ends the list too: meta.next can be there anyway.
            if next_page is None or not objects:
                if not contests:
                    await self._check_no_contests(resource, listed)
                return sorted(contests.values(), key=lambda c: (c.start, c.clist_id))
            # The link has the query in it, with the offset of the next page.
            url, query = _next_page_url(next_page), None
        logger.debug('clist.by lists more than %d pages for %s', _MAX_PAGES, resource)
        raise ExternalServiceError(
            SERVICE,
            f'clist.by lists more than {_MAX_PAGES * _PAGE_SIZE} contests for '
            f'{resource}, too many to read.',
        )

    async def _check_no_contests(self, resource: str, listed: int) -> None:
        """Raises ``ExternalServiceError`` unless a list with no contest that
        can be read, of ``listed`` objects, means ``resource`` has none coming.

        It means so only if the list is empty and clist.by has a resource by
        that name.
        """
        if listed:
            logger.debug(
                'clist.by listed %d contests for %s but none could be read',
                listed,
                resource,
            )
            raise _unreadable_list()
        # Its first page is enough: no two resources have the same name.
        sites, _ = _read_page(
            await self._get(urljoin(API_URL, '../resource/'), {'name__in': resource})
        )
        if not any(
            isinstance(site, dict) and site.get('name') == resource for site in sites
        ):
            logger.debug('clist.by has no resource named %s', resource)
            raise ExternalServiceError(
                SERVICE, f'clist.by has no site named {resource}.'
            )

    async def _get(self, url: str, params: Mapping[str, str] | None) -> object:
        """One page of a list, parsed from JSON."""
        response = await self._http.get(
            url,
            params=params,
            headers={'Authorization': self._authorization},
            service=SERVICE,
            allow_status=_REFUSED_STATUSES,
        )
        if response.status in _REFUSED_STATUSES:
            raise ExternalServiceError(SERVICE, _REFUSED, status=response.status)
        try:
            data: object = response.json()
        except ValueError as exc:
            logger.debug('clist.by sent a body that is not JSON for %s', url)
            raise _unreadable_list(status=response.status) from exc
        return data


def _read_page(data: object) -> tuple[list[object], str | None]:
    """A page's objects (contests, or resources) and the link to the next page
    (None if none).

    Raises ``ExternalServiceError`` unless ``data`` is shaped like a page.
    """
    if isinstance(data, dict):
        meta, objects = data.get('meta'), data.get('objects')
        if isinstance(meta, dict) and isinstance(objects, list):
            next_page = meta.get('next')
            if next_page is None or isinstance(next_page, str):
                return objects, next_page or None
    logger.debug('clist.by sent a contest list shaped like this: %.200r', data)
    raise _unreadable_list()


def _next_page_url(link: str) -> str:
    """The URL of ``meta.next``, which must be on the same site as ``API_URL``.

    Every request carries the API key, so a link to another site is refused.
    """
    try:
        url = urljoin(API_URL, link)
        parts, api = urlsplit(url), urlsplit(API_URL)
        same_site = (parts.scheme, parts.hostname, parts.port) == (
            api.scheme,
            api.hostname,
            api.port,
        )
    except ValueError:  # e.g. a port that isn't a number
        same_site = False
    if not same_site:
        logger.debug('clist.by linked its next page elsewhere: %.200r', link)
        raise _unreadable_list()
    return url


def _utc_time(value: object) -> datetime | None:
    """An ISO 8601 time as an aware UTC datetime, UTC if it has no zone.

    None if ``value`` isn't one.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[-1:] in ('Z', 'z'):  # which fromisoformat reads only from 3.11
        text = f'{text[:-1]}+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _text(value: object) -> str:
    """``value`` trimmed if it is a string, else ''."""
    return value.strip() if isinstance(value, str) else ''


def _web_url(value: object) -> str | None:
    """``value`` if it is an absolute http(s) URL (trimmed), else None."""
    text = _text(value)
    if not text or any(character.isspace() for character in text):
        return None
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    return text if parts.scheme.lower() in ('http', 'https') and host else None


def _unreadable_list(*, status: int | None = None) -> ExternalServiceError:
    return ExternalServiceError(
        SERVICE, "clist.by's contest list could not be read.", status=status
    )
