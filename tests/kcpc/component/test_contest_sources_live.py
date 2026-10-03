"""Live checks that AtCoder's contest list, icpc.global's contest API and
clist.by's contest API still read the way the parsers expect.

They reach atcoder.jp, icpc.global and clist.by, so they are marked
``network`` and skipped unless KCPC_NETWORK_TESTS=1. The clist.by check also
needs CLIST_USERNAME and CLIST_API_KEY, from the environment or .env, and
never shows them. Codeforces needs no check here: its contests come from TLE's
own cache.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from tle.config import DEFAULT_HTTP_USER_AGENT
from tle.kcpc.core.clock import UTC, SystemClock
from tle.kcpc.core.http import HttpClient
from tle.kcpc.platforms.atcoder.contests import AtCoderContestsClient
from tle.kcpc.platforms.clist import ClistClient, ClistContest
from tle.kcpc.platforms.icpc import IcpcClient

DOTENV = Path(__file__).resolve().parents[3] / '.env'

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason=(
            'reaches atcoder.jp, icpc.global and clist.by; set KCPC_NETWORK_TESTS=1 '
            'to run'
        ),
    ),
]


def clist_credentials() -> tuple[str, str] | None:
    """clist.by's username and API key, from the environment or else .env.

    None unless both are set. Only the ``clist`` fixture holds them, to make a
    client, so that no test's variables do: a failure's report shows those
    with ``pytest -l``. For the same reason, an error from the client reaches
    a test only as its message (see ``clist_upcoming``).
    """
    dotenv = dotenv_values(DOTENV)
    username, api_key = (
        (os.environ.get(name) or dotenv.get(name) or '').strip()
        for name in ('CLIST_USERNAME', 'CLIST_API_KEY')
    )
    return (username, api_key) if username and api_key else None


async def clist_upcoming(
    client: ClistClient, resource: str, **kwargs: Any
) -> list[ClistContest]:
    """``client.upcoming(resource, **kwargs)``, any error of which fails the
    test with only its message, which never has the key in it.

    Its traceback would go through ``HttpClient.get``, whose variables hold
    the Authorization header.
    """
    try:
        return await client.upcoming(resource, **kwargs)
    except Exception as exc:
        raise AssertionError(f'{type(exc).__name__}: {exc}') from None


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    client = HttpClient(user_agent=DEFAULT_HTTP_USER_AGENT, clock=SystemClock())
    yield client
    await client.close()


@pytest.fixture
def clist(http: HttpClient) -> ClistClient:
    """A client with the bot's clist.by account, whose repr never shows its key.

    For tests that are skipped without the account.
    """
    credentials = clist_credentials()
    assert credentials is not None
    return ClistClient(http, username=credentials[0], api_key=credentials[1])


async def test_atcoders_upcoming_contests_fetch_and_parse(http: HttpClient) -> None:
    upcoming = await AtCoderContestsClient(http).fetch_upcoming()

    assert upcoming, 'AtCoder announces its contests weeks ahead'
    assert len({contest.contest_id for contest in upcoming}) == len(upcoming)
    for contest in upcoming:
        assert re.fullmatch(r'[a-z0-9_-]+', contest.contest_id), contest
        assert contest.name, contest
        assert contest.start.tzinfo is UTC, contest
        assert contest.end > contest.start, contest
        assert contest.url == f'https://atcoder.jp/contests/{contest.contest_id}'
        assert contest.kind in ('Algorithm', 'Heuristic'), contest
        assert contest.rated_range, contest


async def test_an_icpc_contest_fetches_and_parses(http: HttpClient) -> None:
    client = IcpcClient(http)

    contest = await client.fetch('UKIEPC')
    missing = await client.fetch('KCPC-Example-No-Such-Contest')

    assert contest is not None, 'UKIEPC is a contest code every season'
    assert contest.code == 'UKIEPC'
    assert contest.contest_id.isdigit(), contest
    assert contest.name, contest
    assert contest.end_date is None or contest.end_date >= contest.start_date
    assert contest.url.startswith(('https://', 'http://')), contest
    assert missing is None, 'an unknown code is a 404'


@pytest.mark.skipif(
    clist_credentials() is None,
    reason='needs CLIST_USERNAME and CLIST_API_KEY, in the environment or .env',
)
async def test_clist_bys_contest_lists_fetch_and_parse(clist: ClistClient) -> None:
    codechef = await clist_upcoming(clist, 'codechef.com')
    await asyncio.sleep(1)  # at most one request a second to a site
    finals = await clist_upcoming(clist, 'icpc.global', event_regex='world finals')

    assert codechef, 'CodeChef runs contests every week'
    assert {contest.resource for contest in codechef} == {'codechef.com'}
    # There may be no World Finals listed yet, but any listed are on icpc.global.
    for contest in finals:
        assert contest.resource == 'icpc.global', contest
        assert 'world finals' in contest.name.lower(), contest
    for contests in (codechef, finals):
        assert len({contest.clist_id for contest in contests}) == len(contests)
        for contest in contests:
            assert contest.name, contest
            assert contest.start.tzinfo is UTC, contest
            assert contest.end > contest.start, contest
            assert contest.url.startswith(('https://', 'http://')), contest
