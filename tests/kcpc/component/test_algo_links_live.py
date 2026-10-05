"""A live check that every article the algorithm of the month links still answers.

It reaches www.geeksforgeeks.org and cp-algorithms.com, so it is marked
``network`` and skipped unless KCPC_NETWORK_TESTS=1. It makes a request for
each link, at most one every two seconds to each site, about 45 to each.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from urllib.parse import urldefrag

import pytest

from tle.config import DEFAULT_HTTP_USER_AGENT
from tle.kcpc.core.clock import SystemClock
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.features.algo.catalog import ALGO_TOPICS

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason=(
            'reaches www.geeksforgeeks.org and cp-algorithms.com; set '
            'KCPC_NETWORK_TESTS=1 to run'
        ),
    ),
]

PACE = 2.0  # seconds between requests to a site
NOT_RETRIED = frozenset(range(300, 500)) - {429}
LINKS = sorted(
    {
        url
        for found in ALGO_TOPICS
        for url in (found.gfg_url, found.cp_algorithms_url)
        if url is not None
    }
)


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    policies = {
        'www.geeksforgeeks.org': HostPolicy(min_interval=PACE),
        'cp-algorithms.com': HostPolicy(min_interval=PACE),
    }
    client = HttpClient(
        user_agent=DEFAULT_HTTP_USER_AGENT, clock=SystemClock(), policies=policies
    )
    yield client
    await client.close()


async def test_every_link_answers_where_the_catalog_says(http: HttpClient) -> None:
    async def check(url: str) -> str | None:
        """What is wrong with ``url``; None if nothing is."""
        page, anchor = urldefrag(url)
        # A 404, say, comes back rather than raising, so that each broken link
        # is named; 429s and 5xx responses are retried as usual.
        response = await http.get(page, allow_status=NOT_RETRIED)
        if response.status != 200:
            return f'{url}: HTTP {response.status}'
        # The catalog keeps each article where any redirect ended.
        if response.url != page:
            return f'{url}: moved to {response.url}'
        if anchor and f'id="{anchor}"' not in response.text():
            return f'{url}: the page has no section {anchor}'
        return None

    # Each site's requests are paced by its policy, the two sites side by side.
    problems = await asyncio.gather(*(check(url) for url in LINKS))

    assert len(LINKS) > 70
    assert [problem for problem in problems if problem is not None] == []
