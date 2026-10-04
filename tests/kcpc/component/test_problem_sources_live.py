"""Live checks that AtCoder Problems' files and submissions, AtCoder's
editorial pages and Codeforces' problemset still read the way the parsers
expect.

They reach kenkoooo.com, atcoder.jp and codeforces.com, so they are marked
``network`` and skipped unless KCPC_NETWORK_TESTS=1. They make seven requests,
at most one every two seconds to each site, and check the shape of what comes
back and a few problems that have been there for years.
"""

import asyncio
import os
import re
from collections.abc import AsyncIterator

import aiohttp
import pytest

from tle.config import DEFAULT_HTTP_USER_AGENT
from tle.kcpc.core.clock import SystemClock
from tle.kcpc.core.http import DEFAULT_POLICIES, HostPolicy, HttpClient
from tle.kcpc.platforms.atcoder.editorials import AtCoderEditorialsClient
from tle.kcpc.platforms.atcoder.problems import AtCoderProblemsClient
from tle.kcpc.platforms.codeforces import fetch_problems
from tle.util import codeforces_api as cf

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason=(
            'reaches kenkoooo.com, atcoder.jp and codeforces.com; set '
            'KCPC_NETWORK_TESTS=1 to run'
        ),
    ),
]

# Seconds between requests to a site. The bot asks kenkoooo.com more often,
# and TLE's Codeforces client asks Codeforces' API every second.
PACE = 2.0
# ABC300 A, which AtCoder Daily Training rounds have reused since: AtCoder
# Problems' problems.json names one of them as its contest.
REUSED = 'abc300_a'
# Made up, and unknown to AtCoder Problems.
NOBODY = 'kcpc_nobody_0000'
# A problem of a Div. 3 round of 2021, rated since.
CODEFORCES_PROBLEM = '1520D'


@pytest.fixture
async def http() -> AsyncIterator[HttpClient]:
    policies = {**DEFAULT_POLICIES, 'kenkoooo.com': HostPolicy(min_interval=PACE)}
    client = HttpClient(
        user_agent=DEFAULT_HTTP_USER_AGENT, clock=SystemClock(), policies=policies
    )
    yield client
    await client.close()


@pytest.fixture
async def codeforces_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The session of TLE's Codeforces API client, which the bot opens at
    start, with the contest list asked for ``PACE`` seconds after the
    problemset, as Codeforces' API documentation asks.
    """
    session = aiohttp.ClientSession()
    monkeypatch.setattr(cf, '_session', session)
    to_list = cf.contest.to_list

    async def paced_to_list(*, gym: bool | None = None) -> list[cf.Contest]:
        await asyncio.sleep(PACE)
        return await to_list(gym=gym)

    monkeypatch.setattr(cf.contest, 'to_list', paced_to_list)
    yield
    await session.close()


async def test_atcoder_problems_files_and_submissions_fetch_and_parse(
    http: HttpClient,
) -> None:
    client = AtCoderProblemsClient(http)

    problems = await client.fetch_problem_set()
    submissions = await client.fetch_submissions(NOBODY, 0)

    problem = problems[REUSED]
    assert problem.contest_id == 'abc300', 'the contest that set it, not a reuse'
    assert problem.index == 'A'
    assert problem.url == 'https://atcoder.jp/contests/abc300/tasks/abc300_a'
    assert problem.name and problem.name == problem.name.strip()
    assert problem.in_pool, 'it has a difficulty'
    pool = [found for found in problems.values() if found.in_pool]
    assert len(pool) > 3500, 'ABC, ARC and AGC had 4,000 problems in October 2026'
    for found in pool:
        assert re.fullmatch(r'(abc|arc|agc)\d{3}', found.contest_id), found
        assert found.problem_id.startswith(f'{found.contest_id}_'), found
        assert found.difficulty is not None and found.difficulty >= 0, found
    assert submissions == [], 'an unknown user has none'


async def test_an_atcoder_editorial_page_fetches_and_parses(http: HttpClient) -> None:
    editorials = await AtCoderEditorialsClient(http).fetch('abc300', REUSED)

    assert editorials is not None
    best = editorials.best()
    assert best is not None, 'ABC300 has official editorials'
    assert best.official and best.in_page_language and not best.video, (
        'an official editorial in English'
    )
    assert re.fullmatch(r'https://atcoder\.jp/contests/abc300/editorial/\d+', best.url)
    assert editorials.page_url == (
        'https://atcoder.jp/contests/abc300/tasks/abc300_a/editorial?lang=en'
    )


async def test_codeforces_problems_fetch_and_parse(codeforces_api: None) -> None:
    catalog = await fetch_problems()

    by_id = {problem.problem_id: problem for problem in catalog}
    problem = by_id[CODEFORCES_PROBLEM]
    assert problem.rating is not None and problem.standard
    assert problem.url == 'https://codeforces.com/contest/1520/problem/D'
    assert problem.solved_count is not None and problem.solved_count > 0
    rated_standard = [p for p in catalog if p.rating is not None and p.standard]
    assert len(rated_standard) > 9000, 'Codeforces had 9,948 in October 2026'
    for found in rated_standard:
        assert found.rating is not None and 800 <= found.rating <= 3500, found
        assert found.rating % 100 == 0, found
        assert '*special' not in found.tags, found
