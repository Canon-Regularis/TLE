"""ICPC contests, read from icpc.global's public contest API.

icpc.global serves the public details of a contest as JSON at ``API_URL``,
by the contest's code (such as 'UKIEPC'), with no API key; an unknown code is
a 404. The contest's numeric ``id`` stays the same for a season. Its
``startDate`` and ``endDate`` are the first and last days of its event, each
written as a UTC midnight, '2026-10-17T00:00:00.000Z', whether ``timezone`` is
null (as UKIEPC's is) or set ('UTC+02:00' for NWERC). No start time is
published, so only the dates mean anything, and each is taken as written
rather than moved to any time zone. An event can last several days (NWERC 2026
runs from 27 to 29 November), and which of them the contest is on isn't
published either.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from urllib.parse import quote, urlsplit

from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HttpClient

logger = logging.getLogger(__name__)

PLATFORM = 'icpc'
API_URL = 'https://icpc.global/api/contest/public/{code}'

_SERVICE = 'ICPC'
_ICPC_HOME = 'https://icpc.global/'

# The date that starts an ISO 8601 date or date-time. re.ASCII, so that \d
# matches no digits from other scripts.
_LEADING_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})(?:[T ]|$)', re.ASCII)


@dataclass(frozen=True)
class IcpcContest:
    """An ICPC contest's public details: its dates, as no times are published."""

    code: str  # the code it was fetched by, e.g. 'UKIEPC'
    contest_id: str  # icpc.global's numeric ID, as text, e.g. '9584'
    name: str
    start_date: date  # its event's first day
    end_date: date | None  # its event's last day, never before start_date
    url: str  # its homepage if it has one, else https://icpc.global/


def parse_contest(code: str, data: object) -> IcpcContest:
    """The contest in icpc.global's JSON for ``code``.

    Raises ``ExternalServiceError`` unless ``data`` is a contest with a numeric
    ID and a readable start date. Any other field that is missing or unreadable
    falls back: the name to ``code``, the end date to None and the link to
    icpc.global's home page.
    """
    if not isinstance(data, dict):
        logger.debug('icpc.global sent a %s for %s', type(data).__name__, code)
        raise _unreadable_contest(code)
    contest_id = data.get('id')
    # bool is a subclass of int, but true is no ID.
    if not isinstance(contest_id, int) or isinstance(contest_id, bool):
        logger.debug('icpc.global sent ID %r for %s', contest_id, code)
        raise _unreadable_contest(code)
    raw_start = data.get('startDate')
    start_date = _leading_date(raw_start)
    if start_date is None:
        logger.debug('icpc.global sent startDate %r for %s', raw_start, code)
        raise _unreadable_contest(code)
    end_date = _leading_date(data.get('endDate'))
    if end_date is not None and end_date < start_date:
        end_date = None
    return IcpcContest(
        code=code,
        contest_id=str(contest_id),
        name=_text(data.get('name')) or code,
        start_date=start_date,
        end_date=end_date,
        url=_web_url(data.get('homepage')) or _ICPC_HOME,
    )


class IcpcClient:
    """Fetches ICPC contests through the shared ``HttpClient``."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch(self, code: str) -> IcpcContest | None:
        """The contest with this code, as ``parse_contest`` reads it.

        Returns None if icpc.global has no contest with this code. Raises
        ``ExternalServiceError`` if icpc.global can't be reached or its answer
        can't be read.
        """
        response = await self._http.get(
            API_URL.format(code=quote(code, safe='')),
            service=_SERVICE,
            allow_status={HTTPStatus.NOT_FOUND},
        )
        if response.status == HTTPStatus.NOT_FOUND:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            logger.debug('icpc.global sent a body that is not JSON for %s', code)
            raise _unreadable_contest(code, status=response.status) from exc
        return parse_contest(code, data)


def _leading_date(value: object) -> date | None:
    """The date that starts an ISO 8601 date-time, as written; None if none."""
    if not isinstance(value, str):
        return None
    match = _LEADING_DATE.match(value.strip())
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:  # a date that doesn't exist, such as 2026-02-30
        return None


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


def _unreadable_contest(
    code: str, *, status: int | None = None
) -> ExternalServiceError:
    return ExternalServiceError(
        _SERVICE,
        f"ICPC's details of the contest {code} could not be read.",
        status=status,
    )
