"""Live checks that AtCoder's profile pages and Codeforces' user.info still
read the way the parsers expect.

They reach atcoder.jp and codeforces.com, so they are marked ``network`` and
skipped unless KCPC_NETWORK_TESTS=1. They read the public profiles of one
long-standing competitor, whose name is in examples everywhere, and check only
their shape.
"""

import os
from collections.abc import AsyncIterator

import aiohttp
import pytest

from tle.config import DEFAULT_HTTP_USER_AGENT
from tle.kcpc.core.clock import SystemClock
from tle.kcpc.core.http import HttpClient
from tle.kcpc.platforms.atcoder.profile import AtCoderProfileClient
from tle.kcpc.platforms.codeforces import fetch_users
from tle.util import codeforces_api as cf

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason='reaches atcoder.jp and codeforces.com; set KCPC_NETWORK_TESTS=1 to run',
    ),
]

# Rated on both sites for over a decade, and asked for in the wrong case.
RATED = 'TOURIST'
CANONICAL = 'tourist'
# Made up, and unknown on both sites.
NOBODY = 'kcpc_nobody_0000'

ATCODER_COLORS = {'gray', 'brown', 'green', 'cyan', 'blue', 'yellow', 'orange', 'red'}
CODEFORCES_RANKS = {rank.title.lower() for rank in cf.RATED_RANKS}


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    client = HttpClient(user_agent=DEFAULT_HTTP_USER_AGENT, clock=SystemClock())
    yield client
    await client.close()


@pytest.fixture
async def codeforces_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The session of TLE's Codeforces API client, which the bot opens at start."""
    session = aiohttp.ClientSession()
    monkeypatch.setattr(cf, '_session', session)
    yield
    await session.close()


async def test_an_atcoder_profile_fetches_and_parses(http: HttpClient) -> None:
    client = AtCoderProfileClient(http)

    # The client keeps to atcoder.jp's pace, two seconds between requests.
    profile = await client.fetch(RATED)
    missing = await client.fetch(NOBODY)

    assert profile is not None
    assert profile.handle == CANONICAL, 'the page gives the canonical case'
    assert profile.url == f'https://atcoder.jp/users/{CANONICAL}'
    assert profile.rating is not None and profile.highest_rating is not None
    assert 0 < profile.rating <= profile.highest_rating
    assert profile.rated_matches > 0
    assert profile.color in ATCODER_COLORS
    assert profile.affiliation != '', 'an empty affiliation is None'
    assert missing is None, 'an unknown user is a 404'


async def test_codeforces_users_fetch_and_unknown_handles_are_left_out(
    codeforces_api: None,
) -> None:
    # The unknown handle fails the first request, so each handle is then asked
    # about on its own: three requests, at TLE's pace of one a second.
    users = await fetch_users([RATED, NOBODY])

    assert [user.handle for user in users] == [CANONICAL]
    [user] = users
    assert user.url == f'https://codeforces.com/profile/{CANONICAL}'
    assert user.rating is not None and user.max_rating is not None
    assert 0 < user.rating <= user.max_rating
    assert user.rank in CODEFORCES_RANKS
    assert user.organization != '', 'an empty organization is None'
