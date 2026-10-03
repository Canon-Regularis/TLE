"""Tests for tle.kcpc.features.accounts.refresh: keeping rating snapshots fresh.

The refresher saves into a real AccountRepo in a migrated in-memory kcpc.db.
AtCoder's profile pages are faked by FakeAtCoder, and Codeforces by
FakeCodeforces in place of TLE's ``cf.user.info``. The users are made up.
"""

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import cast

import pytest

from tests.kcpc.conftest import CLOCK_START
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.features.accounts.refresh import (
    CODEFORCES_BATCH,
    RatingRefresher,
    RefreshReport,
)
from tle.kcpc.features.accounts.repo import AccountRepo, AccountSnapshot
from tle.kcpc.features.accounts.service import ATCODER, CODEFORCES
from tle.kcpc.platforms.atcoder.profile import (
    PROFILE_URL,
    AtCoderProfile,
    AtCoderProfileClient,
)
from tle.util import codeforces_api as cf

NOW = CLOCK_START
LOGGER = 'tle.kcpc.features.accounts.refresh'
DOWN = ExternalServiceError(
    'AtCoder', 'AtCoder is not responding right now. Please try again later.'
)


class FakeAtCoder:
    """AtCoder's profile pages: ``errors`` by name, and ``fetched`` in order.

    ``before_fetch`` runs as each fetch starts.
    """

    def __init__(self) -> None:
        self.profiles: dict[str, AtCoderProfile] = {}
        self.errors: dict[str, Exception] = {}
        self.fetched: list[str] = []
        self.before_fetch: Callable[[str], Awaitable[None]] | None = None

    def add(self, handle: str, rating: int | None = 1834) -> None:
        self.profiles[handle.lower()] = AtCoderProfile(
            handle=handle,
            rating=rating,
            highest_rating=None if rating is None else rating + 78,
            rated_matches=0 if rating is None else 27,
            affiliation=None,
            color='unrated' if rating is None else 'cyan',
            url=PROFILE_URL.format(handle=handle),
        )

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        if self.before_fetch is not None:
            await self.before_fetch(handle)
        self.fetched.append(handle)
        error = self.errors.get(handle)
        if error is not None:
            raise error
        return self.profiles.get(handle.lower())


class FakeCodeforces:
    """``cf.user.info``: fails a request for an unknown handle, as Codeforces does.

    ``errors`` fail the next requests, one each; ``asked`` lists each request.
    """

    def __init__(self) -> None:
        self.ratings: dict[str, tuple[str, int | None]] = {}
        self.errors: deque[Exception] = deque()
        self.asked: list[list[str]] = []

    def add(self, handle: str, rating: int | None = 1700) -> None:
        self.ratings[handle.lower()] = (handle, rating)

    async def info(self, *, handles: Sequence[str]) -> list[cf.User]:
        self.asked.append(list(handles))
        if self.errors:
            raise self.errors.popleft()
        users = []
        for asked in handles:
            if asked.lower() not in self.ratings:
                raise cf.HandleNotFoundError(
                    f'User with handle {asked} not found', asked
                )
            handle, rating = self.ratings[asked.lower()]
            users.append(
                cf.User(
                    handle=handle,
                    firstName=None,
                    lastName=None,
                    country=None,
                    city=None,
                    organization=None,
                    contribution=0,
                    rating=rating,
                    maxRating=None if rating is None else rating + 100,
                    lastOnlineTimeSeconds=1_790_000_000,
                    registrationTimeSeconds=1_600_000_000,
                    friendOfCount=0,
                    titlePhoto='https://userpic.codeforces.org/no-title.jpg',
                )
            )
        return users


@pytest.fixture
def atcoder() -> FakeAtCoder:
    return FakeAtCoder()


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.user, 'info', fake.info)
    return fake


@pytest.fixture
def repo(db: Database) -> AccountRepo:
    return AccountRepo(db)


@pytest.fixture
def refresher(
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> RatingRefresher:
    return RatingRefresher(repo, cast(AtCoderProfileClient, atcoder), clock)


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == LOGGER
    ]


def summary(saved: int, missing: int, failed: int) -> str:
    return (
        f'Refreshed the ratings of linked accounts: {saved} saved, {missing} not '
        f'found, {failed} could not be fetched'
    )


async def test_refresh_all_saves_each_accounts_ratings(
    refresher: RatingRefresher,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    codeforces.add('FakeCoder')
    atcoder.add('FakeAtCoder')
    atcoder.add('NewAtCoder', rating=None)
    await clock.advance(timedelta(minutes=5))

    report = await refresher.refresh_all(['fakecoder'], ['FakeAtCoder', 'newatcoder'])

    assert report == RefreshReport(
        saved=(
            (CODEFORCES, 'fakecoder'),
            (ATCODER, 'FakeAtCoder'),
            (ATCODER, 'newatcoder'),
        )
    )
    later = NOW + timedelta(minutes=5)
    assert await repo.snapshot(CODEFORCES, 'FakeCoder') == AccountSnapshot(
        CODEFORCES, 'FakeCoder', 1700, 1800, 'expert', None, later
    )
    assert await repo.snapshot(ATCODER, 'FakeAtCoder') == AccountSnapshot(
        ATCODER, 'FakeAtCoder', 1834, 1912, 'cyan', 27, later
    )
    assert await repo.snapshot(ATCODER, 'NewAtCoder') == AccountSnapshot(
        ATCODER, 'NewAtCoder', None, None, 'unrated', 0, later
    )


async def test_codeforces_users_are_asked_about_300_at_a_time(
    refresher: RatingRefresher, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    handles = [f'coder{n}' for n in range(CODEFORCES_BATCH + 1)]
    for handle in handles:
        codeforces.add(handle)

    report = await refresher.refresh_all(handles, [])

    assert CODEFORCES_BATCH == 300
    assert [len(asked) for asked in codeforces.asked] == [300, 1]
    assert len(report.saved) == 301
    assert len(await repo.snapshots(CODEFORCES, handles)) == 301


async def test_a_codeforces_request_that_fails_keeps_the_others(
    refresher: RatingRefresher, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    handles = [f'coder{n}' for n in range(CODEFORCES_BATCH + 1)]
    for handle in handles:
        codeforces.add(handle)
    codeforces.errors.append(cf.ClientError())

    report = await refresher.refresh_all(handles, [])

    assert report.failed == tuple((CODEFORCES, handle) for handle in handles[:300])
    assert report.saved == ((CODEFORCES, 'coder300'),)
    assert list(await repo.snapshots(CODEFORCES, handles)) == ['coder300']


async def test_codeforces_users_that_are_gone_are_reported_missing(
    refresher: RatingRefresher, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')

    report = await refresher.refresh_all(['FakeCoder', 'renamed'], [])

    assert report == RefreshReport(
        saved=((CODEFORCES, 'FakeCoder'),), missing=((CODEFORCES, 'renamed'),)
    )
    assert await repo.snapshot(CODEFORCES, 'FakeCoder') is not None


async def test_atcoder_profiles_are_saved_one_by_one_as_they_arrive(
    refresher: RatingRefresher, repo: AccountRepo, atcoder: FakeAtCoder
) -> None:
    handles = ['First', 'Second', 'Third']
    for handle in handles:
        atcoder.add(handle)
    saved_before: list[list[str]] = []

    async def note_saved(handle: str) -> None:
        saved_before.append(sorted(await repo.snapshots(ATCODER, handles)))

    atcoder.before_fetch = note_saved

    await refresher.refresh_all([], handles)

    assert atcoder.fetched == handles
    assert saved_before == [[], ['First'], ['First', 'Second']]


async def test_an_atcoder_profile_that_cant_be_fetched_doesnt_stop_the_others(
    refresher: RatingRefresher, repo: AccountRepo, atcoder: FakeAtCoder
) -> None:
    for handle in ('Before', 'Down', 'After'):
        atcoder.add(handle)
    atcoder.errors['Down'] = DOWN
    atcoder.errors['bad name'] = KcpcUserError("That isn't a valid AtCoder username.")
    stale = AccountSnapshot(ATCODER, 'Down', 1200, 1300, 'green', 9, NOW)
    await repo.save_snapshots([stale])

    report = await refresher.refresh_all(
        [], ['Before', 'Down', 'Gone', 'bad name', 'After']
    )

    assert report == RefreshReport(
        saved=((ATCODER, 'Before'), (ATCODER, 'After')),
        missing=((ATCODER, 'Gone'), (ATCODER, 'bad name')),
        failed=((ATCODER, 'Down'),),
    )
    assert await repo.snapshot(ATCODER, 'Down') == stale


async def test_each_account_is_refreshed_once_whatever_its_case(
    refresher: RatingRefresher, atcoder: FakeAtCoder, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')
    atcoder.add('FakeAtCoder')

    report = await refresher.refresh_all(
        ['FakeCoder', 'fakecoder'], ['FakeAtCoder', 'FAKEATCODER']
    )

    assert codeforces.asked == [['FakeCoder']]
    assert atcoder.fetched == ['FakeAtCoder']
    assert report.saved == ((CODEFORCES, 'FakeCoder'), (ATCODER, 'FakeAtCoder'))


async def test_refreshing_everything_warns_of_failures_at_most_once_a_day(
    refresher: RatingRefresher,
    atcoder: FakeAtCoder,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    atcoder.add('Fine')
    atcoder.errors['Down'] = DOWN
    caplog.set_level(logging.INFO, logger=LOGGER)

    await refresher.refresh_all([], ['Fine'])
    await refresher.refresh_all([], ['Fine', 'Down'])
    await clock.advance(timedelta(hours=23, minutes=59))
    await refresher.refresh_all([], ['Down'])
    await clock.advance(timedelta(minutes=1))
    await refresher.refresh_all([], ['Down'])

    could_not = f'Could not refresh AtCoder user Down: {DOWN}'
    assert logged(caplog) == [
        (logging.INFO, summary(1, 0, 0)),
        (logging.INFO, could_not),
        (logging.WARNING, summary(1, 0, 1)),
        (logging.INFO, could_not),
        (logging.INFO, summary(0, 0, 1)),
        (logging.INFO, could_not),
        (logging.WARNING, summary(0, 0, 1)),
    ]


async def test_refresh_member_refreshes_the_accounts_given_and_logs_quietly(
    refresher: RatingRefresher,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codeforces.add('FakeCoder')
    atcoder.errors['FakeAtCoder'] = DOWN
    caplog.set_level(logging.DEBUG, logger=LOGGER)

    report = await refresher.refresh_member(
        [(CODEFORCES, 'FakeCoder'), (ATCODER, 'FakeAtCoder')]
    )

    assert report == RefreshReport(
        saved=((CODEFORCES, 'FakeCoder'),), failed=((ATCODER, 'FakeAtCoder'),)
    )
    assert await repo.snapshot(CODEFORCES, 'FakeCoder') is not None
    # /profile shows the failure, so it is no news for the logs.
    assert [level for level, _ in logged(caplog)] == [logging.INFO]


async def test_refresh_member_takes_only_codeforces_and_atcoder_accounts(
    refresher: RatingRefresher, codeforces: FakeCodeforces
) -> None:
    with pytest.raises(ValueError, match="cannot be linked on 'leetcode'"):
        await refresher.refresh_member([(CODEFORCES, 'FakeCoder'), ('leetcode', 'x')])

    assert codeforces.asked == []


async def test_refreshing_nothing_asks_nothing(
    refresher: RatingRefresher, atcoder: FakeAtCoder, codeforces: FakeCodeforces
) -> None:
    assert await refresher.refresh_all([], []) == RefreshReport()
    assert await refresher.refresh_member([]) == RefreshReport()
    assert codeforces.asked == []
    assert atcoder.fetched == []
