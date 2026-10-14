"""Live checks that Codeforces' API still answers TLE's client the way the bot
expects: standings to an anonymous GET with contestId alone, with official
contestants' rows only, contest.status to a POST, and "not found" in the
refusal of an unknown contest, which the client logs at INFO.

They reach codeforces.com, so they are marked ``network`` and skipped unless
KCPC_NETWORK_TESTS=1. They make three requests, two seconds apart, as
Codeforces' API documentation asks.
"""

import logging
import os
from collections.abc import AsyncIterator

import aiohttp
import pytest

from tle.util import codeforces_api as cf

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason='reaches codeforces.com; set KCPC_NETWORK_TESTS=1 to run',
    ),
]

# Round 1127 (Div. 1), which ended on 11 October 2026: a finished round in
# Codeforces format.
CONTEST_ID = 2273
# Far above any contest's id, below those of gyms
UNKNOWN_CONTEST_ID = 99999


@pytest.fixture
async def codeforces_api(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The session of TLE's Codeforces client, which the bot opens at start,
    with its calls two seconds apart.
    """
    session = aiohttp.ClientSession()
    monkeypatch.setattr(cf, '_session', session)
    monkeypatch.setattr(cf, '_CALL_SPACING_SECONDS', 2)
    yield
    await session.close()


async def test_standings_give_official_contestants_only(codeforces_api: None) -> None:
    contest, problems, rows = await cf.contest.standings(contest_id=CONTEST_ID)

    assert (contest.id, contest.type, contest.phase) == (CONTEST_ID, 'CF', 'FINISHED')
    assert [problem.index for problem in problems] == list('ABCDEF')
    assert all(problem.points for problem in problems), 'a Codeforces-format round'
    assert len(rows) > 300
    assert {row.party.participantType for row in rows} == {'CONTESTANT'}
    assert {row.penalty for row in rows} == {0}, 'Codeforces-format rows'
    ranks = [row.rank for row in rows]
    assert ranks[0] == 1 and ranks == sorted(ranks)


async def test_contest_status_gives_passed_test_counts(codeforces_api: None) -> None:
    submissions = await cf.contest.status(contest_id=CONTEST_ID, count=5)

    assert len(submissions) == 5
    for submission in submissions:
        assert submission.contestId == CONTEST_ID
        assert isinstance(submission.passedTestCount, int), submission
        assert submission.author.members, submission


async def test_an_unknown_contest_is_not_found(
    codeforces_api: None, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(cf.ContestNotFoundError):
        await cf.contest.standings(contest_id=UNKNOWN_CONTEST_ID)

    assert not [
        record
        for record in caplog.records
        if record.name == cf.logger.name and record.levelno >= logging.WARNING
    ], 'an expected refusal is logged at INFO'
