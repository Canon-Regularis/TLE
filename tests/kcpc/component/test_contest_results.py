"""Tests for tle.kcpc.features.contests.results: contest results posts.

The service runs on a migrated in-memory kcpc.db with the real delivery ledger
and settings, and posts through FakePublisher. What it reads from elsewhere
is faked: the members each server has linked (``Linked``), TLE's Codeforces
cache (``FakeTle``) and AtCoder's profiles (``FakeAtCoder``). The clock starts
on 2026-10-01 at 12:00 UTC; the AtCoder contests run on 2026-10-03.
"""

import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timedelta

import pytest

from tests.kcpc.conftest import CLOCK_START
from tests.kcpc.fakes import FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger
from tle.kcpc.core.messages import DESCRIPTION_LIMIT, OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo, default_registry
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.contests.repo import ContestInfo, ContestRepo
from tle.kcpc.features.contests.results import (
    KIND,
    RESULTS_INTERVAL,
    RESULTS_JOB,
    ContestResults,
    results_key,
    results_post,
)
from tle.kcpc.features.contests.results_repo import (
    ATCODER,
    CODEFORCES,
    ResultContest,
    ResultEntry,
    ResultOutcome,
    ResultRecord,
    ResultRepo,
    ResultStatus,
)
from tle.kcpc.features.contests.settings import CONTESTS, SPEC
from tle.kcpc.platforms.atcoder.profile import AtCoderProfile
from tle.util import codeforces_api as cf

LOGGER = 'tle.kcpc.features.contests.results'
NOW = CLOCK_START
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
AMBER = 1_300_000_000_000_000_001
BEE = 1_300_000_000_000_000_002
CAT = 1_300_000_000_000_000_003
DOG = 1_300_000_000_000_000_004
UNREACHABLE = 'AtCoder is not responding right now. Please try again later.'

ROUND_LENGTH = 2 * HOUR
ABC_START = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ABC_END = ABC_START + 100 * MINUTE  # 13:40
ARC_END = ABC_START + 2 * HOUR  # 14:00


def cf_round(contest_id: int, end: datetime, *, phase: str = 'FINISHED') -> cf.Contest:
    """A Codeforces round as TLE caches it, ending at ``end``."""
    return cf.Contest(
        contest_id,
        f'Codeforces Round {contest_id} (Div. 2)',
        to_epoch(end - ROUND_LENGTH),
        int(ROUND_LENGTH.total_seconds()),
        'CF',
        phase,
        None,
    )


ROUND = cf_round(2051, NOW - HOUR)
# A contest dealt with long ago: KCPC isn't new.
PAST = ResultContest(
    CODEFORCES, '2000', 'Codeforces Round 2000', None, NOW - 30 * 24 * HOUR
)


def rated(
    handle: str, place: int, old: int, new: int, contest_id: int = 2051
) -> cf.RatingChange:
    """A rating change in a Codeforces contest, as TLE saves it."""
    return cf.RatingChange(
        contestId=contest_id,
        contestName=f'Codeforces Round {contest_id} (Div. 2)',
        handle=handle,
        rank=place,
        ratingUpdateTimeSeconds=to_epoch(NOW),
        oldRating=old,
        newRating=new,
    )


class Linked:
    """The members each server has linked on each platform, as the cog finds
    them: its members only. ``asked`` lists each lookup."""

    def __init__(self) -> None:
        self.members: dict[tuple[int, str], list[tuple[int, str]]] = {}
        self.asked: list[tuple[int, str]] = []
        # Makes a lookup on the platform raise.
        self.errors: dict[str, Exception] = {}

    def link(self, guild_id: int, platform: str, user_id: int, handle: str) -> None:
        self.members.setdefault((guild_id, platform), []).append((user_id, handle))

    async def __call__(self, guild_id: int, platform: str) -> list[tuple[int, str]]:
        self.asked.append((guild_id, platform))
        error = self.errors.get(platform)
        if error is not None:
            raise error
        return list(self.members.get((guild_id, platform), []))


class FakeTle:
    """TLE's Codeforces cache: its contests, and the rating changes it saved."""

    def __init__(self) -> None:
        self.contests: list[cf.Contest] = []
        self.changes: dict[int, list[cf.RatingChange]] = {}
        self.asked: list[int] = []

    def cached(self) -> list[cf.Contest]:
        return self.contests

    async def saved(self, contest_id: int) -> list[cf.RatingChange]:
        self.asked.append(contest_id)
        return self.changes.get(contest_id, [])


class FakeAtCoder:
    """AtCoder's profiles, by handle in any case, as the test sets them.

    ``reads`` lists each read: when, and the handle asked for. ``errors``
    makes reading a handle raise, and ``errors_once`` only the next read of
    it. ``after_read`` runs once, after the next read: AtCoder rating a
    contest just then.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.profiles: dict[str, AtCoderProfile] = {}
        self.errors: dict[str, Exception] = {}
        self.errors_once: dict[str, Exception] = {}
        self.reads: list[tuple[datetime, str]] = []
        self.after_read: Callable[[], None] | None = None

    def set(
        self, handle: str, rating: int | None, matches: int, highest: int | None = None
    ) -> None:
        self.profiles[handle.lower()] = AtCoderProfile(
            handle=handle,
            rating=rating,
            highest_rating=rating if highest is None else highest,
            rated_matches=matches,
            affiliation=None,
            color=None if rating is None else 'green',
            url=f'https://atcoder.jp/users/{handle}',
        )

    def remove(self, handle: str) -> None:
        del self.profiles[handle.lower()]

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        self.reads.append((self._clock.now(), handle))
        error = self.errors.get(handle.lower()) or self.errors_once.pop(
            handle.lower(), None
        )
        if error is not None:
            raise error
        found = self.profiles.get(handle.lower())
        if self.after_read is not None:
            after, self.after_read = self.after_read, None
            after()
        return found

    def read_handles(self, since: datetime | None = None) -> list[str]:
        return [handle for at, handle in self.reads if since is None or at >= since]


class ChannelGone:
    """FakePublisher, except in the servers in ``gone``, which have lost their
    channel: a post there is UNDELIVERABLE, before anything is claimed, as
    DiscordPublisher has it. ``asked`` lists the server of each publish.
    """

    def __init__(self, publisher: FakePublisher) -> None:
        self._publisher = publisher
        self.gone: set[int] = set()
        self.asked: list[int] = []

    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        guild_id = deliveries[0].guild_id
        self.asked.append(guild_id)
        if guild_id in self.gone:
            return PublishResult(PublishOutcome.UNDELIVERABLE, reason='channel-missing')
        return await self._publisher.publish(deliveries, message)


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the contest settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
def contests(db: Database) -> ContestRepo:
    return ContestRepo(db)


@pytest.fixture
def results(db: Database) -> ResultRepo:
    return ResultRepo(db)


@pytest.fixture
def linked() -> Linked:
    return Linked()


@pytest.fixture
def tle() -> FakeTle:
    return FakeTle()


@pytest.fixture
def atcoder(clock: FakeClock) -> FakeAtCoder:
    return FakeAtCoder(clock)


@pytest.fixture
def joined() -> set[int]:
    """The servers the bot is in."""
    return {GUILD, OTHER_GUILD}


@pytest.fixture
def make_service(
    contests: ContestRepo,
    results: ResultRepo,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
    atcoder: FakeAtCoder,
    clock: FakeClock,
    linked: Linked,
    tle: FakeTle,
    joined: set[int],
) -> Callable[[], ContestResults]:
    def make() -> ContestResults:
        return ContestResults(
            contests,
            results,
            guild_settings,
            ledger,
            publisher,
            atcoder,
            clock,
            linked_members=linked,
            in_guild=lambda guild_id: guild_id in joined,
            codeforces_contests=tle.cached,
            codeforces_changes=tle.saved,
        )

    return make


@pytest.fixture
async def service(
    make_service: Callable[[], ContestResults], results: ResultRepo
) -> AsyncIterator[ContestResults]:
    """The service of a bot that has posted results before."""
    await results.add_done([PAST], ResultOutcome.POSTED, now=PAST.end)
    yield make_service()


@pytest.fixture(autouse=True)
def results_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)


async def follow(
    guild_settings: GuildSettingsRepo, guild_id: int = GUILD, **settings: object
) -> None:
    """Set the guild up for contests: on, with a channel, and ``settings``."""
    await guild_settings.update(
        guild_id, CONTESTS, enabled=True, channel_id=CHANNEL, **settings
    )


async def record_of(
    results: ResultRepo, platform: str, external_id: str
) -> ResultRecord:
    record = await results.get(platform, external_id)
    assert record is not None
    return record


def lines(publisher: FakePublisher, number: int = -1) -> list[str]:
    """The lines of the description of post ``number``."""
    description = publisher.posts[number].message.description
    assert description is not None
    return description.splitlines()


def keys(publisher: FakePublisher) -> list[str]:
    return [key for post in publisher.posts for key in post.keys]


def by_contest(publisher: FakePublisher) -> list[tuple[str, list[int]]]:
    """Each post's contest, by its ID on its platform, and the members it lists."""
    return [
        (
            post.keys[0].rsplit(':', 1)[1],
            [
                int(user_id)
                for user_id in re.findall(r'<@(\d+)>', post.message.description or '')
            ],
        )
        for post in publisher.posts
    ]


def logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.INFO
    ]


def cf_line(user_id: int, handle: str, place: int, rest: str) -> str:
    escaped = handle.replace('_', '\\_')
    return (
        f'**{place}.** <@{user_id}> [{escaped}]'
        f'(https://codeforces.com/profile/{handle}): {rest}'
    )


def ac_line(user_id: int, handle: str, rest: str) -> str:
    escaped = handle.replace('_', '\\_')
    return f'<@{user_id}> [{escaped}](https://atcoder.jp/users/{handle}): {rest}'


def test_the_job_runs_every_5_minutes() -> None:
    assert (RESULTS_JOB, RESULTS_INTERVAL, KIND) == (
        'contests.results',
        5 * MINUTE,
        'results',
    )


class TestCodeforces:
    async def test_each_server_gets_a_post_of_its_members_changes(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
    ) -> None:
        await follow(guild_settings, GUILD)
        await follow(guild_settings, OTHER_GUILD)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        # TLE's table may have a handle in another case than Codeforces.
        linked.link(GUILD, CODEFORCES, BEE, 'bee_bot')
        linked.link(OTHER_GUILD, CODEFORCES, CAT, 'Cat_Nap')
        linked.link(OTHER_GUILD, CODEFORCES, DOG, 'Dog_Day')  # not rated
        changes = [
            rated('tourist', 1, 3700, 3750),
            rated('Amber_Owl', 30, 1500, 1623),
            rated('Bee_Bot', 250, 1400, 1380),
            rated('Cat_Nap', 1200, 0, 380),
        ]

        await service.report_codeforces(ROUND, changes)

        assert keys(publisher) == [
            f'results:{GUILD}:codeforces:2051',
            f'results:{OTHER_GUILD}:codeforces:2051',
        ]
        assert lines(publisher, 0) == [
            '**Platform:** Codeforces',
            cf_line(
                AMBER, 'Amber_Owl', 30, '1500 → 1623 (**+123**), Specialist → Expert'
            ),
            cf_line(BEE, 'Bee_Bot', 250, '1400 → 1380 (**-20**), Specialist → Pupil'),
        ]
        assert lines(publisher, 1) == [
            '**Platform:** Codeforces',
            cf_line(CAT, 'Cat_Nap', 1200, 'first rated contest: 380'),
        ]
        first = publisher.posts[0]
        message = first.message
        assert message.title == 'Results: Codeforces Round 2051 (Div. 2)'
        assert message.url == 'https://codeforces.com/contest/2051'
        assert message.footer == 'KCPC contests'
        assert not message.mention_role
        assert first.deliveries == (
            Delivery(
                key=f'results:{GUILD}:codeforces:2051',
                guild_id=GUILD,
                feature=CONTESTS,
                subject='contest',
                subject_id='codeforces:2051',
                kind='results',
                expires_at=NOW + 24 * HOUR,
            ),
        )
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome, record.found_at) == (
            ResultStatus.DONE,
            ResultOutcome.POSTED,
            NOW,
        )
        entries = await results.entries(CODEFORCES, '2051')
        assert [entry.handle for entry in entries] == [
            'Amber_Owl',
            'Bee_Bot',
            'Cat_Nap',
        ]
        assert results_key(GUILD, CODEFORCES, '2051') == first.keys[0]

    async def test_a_contest_is_posted_once(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        linked: Linked,
        tle: FakeTle,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        changes = [rated('Amber_Owl', 30, 1500, 1623)]
        tle.contests = [ROUND]
        tle.changes = {2051: changes}

        await service.report_codeforces(ROUND, changes)
        await service.report_codeforces(ROUND, changes)
        await service.run(NOW)

        assert len(publisher.posts) == 1
        # The second report did nothing, and the job knows the contest is
        # done, so doesn't read its changes.
        assert logged(caplog) == [
            'Found the rating changes of 1 linked handles in Codeforces contest 2051'
        ]
        assert tle.asked == []

    async def test_only_servers_that_want_the_results_get_them(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        linked: Linked,
        joined: set[int],
    ) -> None:
        guilds = [GUILD + number for number in range(6)]
        joined.update(guilds[:-1])
        await follow(guild_settings, guilds[0])
        await guild_settings.update(guilds[1], CONTESTS, channel_id=CHANNEL)
        await guild_settings.update(guilds[2], CONTESTS, enabled=True)
        await follow(guild_settings, guilds[3], results_posts=False)
        await follow(guild_settings, guilds[4], platforms=('atcoder', 'manual'))
        await follow(guild_settings, guilds[5])  # the bot isn't in it
        for guild_id in guilds:
            linked.link(guild_id, CODEFORCES, AMBER, 'Amber_Owl')

        await service.report_codeforces(ROUND, [rated('Amber_Owl', 30, 1500, 1623)])

        assert [post.deliveries[0].guild_id for post in publisher.posts] == [guilds[0]]
        # The others' members weren't even looked up.
        assert {guild_id for guild_id, _ in linked.asked} == {guilds[0]}

    async def test_no_post_when_no_member_took_part(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')

        await service.report_codeforces(ROUND, [rated('tourist', 1, 3700, 3750)])

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.NOBODY,
        )
        assert await results.entries(CODEFORCES, '2051') == []
        assert logged(caplog) == [
            'No member of a server that wants the results took part in Codeforces '
            'contest 2051'
        ]

    async def test_a_server_without_rated_members_gets_no_post(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
    ) -> None:
        await follow(guild_settings, GUILD)
        await follow(guild_settings, OTHER_GUILD)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        linked.link(OTHER_GUILD, CODEFORCES, DOG, 'Dog_Day')

        await service.report_codeforces(ROUND, [rated('Amber_Owl', 30, 1500, 1623)])

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']
        record = await record_of(results, CODEFORCES, '2051')
        assert record.outcome is ResultOutcome.POSTED

    async def test_the_job_catches_up_on_the_changes_tle_saved(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        tle.contests = [
            cf_round(2049, NOW - 49 * HOUR),  # too long ago
            cf_round(2050, NOW + HOUR, phase='CODING'),
            cf_round(2051, NOW - 3 * HOUR),
            cf_round(2052, NOW - 2 * HOUR),  # no changes yet
        ]
        tle.changes = {
            2049: [rated('Amber_Owl', 3, 1400, 1500, 2049)],
            2051: [rated('Amber_Owl', 30, 1500, 1623)],
        }

        await service.run(NOW)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']
        assert tle.asked == [2051, 2052]
        assert await results.get(CODEFORCES, '2049') is None
        assert await results.get(CODEFORCES, '2052') is None

        await service.run(NOW + 5 * MINUTE)

        assert tle.asked == [2051, 2052, 2052]

        tle.changes[2052] = [rated('Amber_Owl', 12, 1623, 1700, 2052)]
        await service.run(NOW + 10 * MINUTE)

        assert keys(publisher) == [
            f'results:{GUILD}:codeforces:2051',
            f'results:{GUILD}:codeforces:2052',
        ]

    async def test_an_undeliverable_post_is_tried_again_by_the_job(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings, GUILD)
        await follow(guild_settings, OTHER_GUILD)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        linked.link(OTHER_GUILD, CODEFORCES, CAT, 'Amber_Owl')
        publisher.undeliverable_next()  # the first server's

        await service.report_codeforces(ROUND, [rated('Amber_Owl', 30, 1500, 1623)])

        assert keys(publisher) == [f'results:{OTHER_GUILD}:codeforces:2051']
        record = await record_of(results, CODEFORCES, '2051')
        assert record.status is ResultStatus.POSTING
        assert (
            f'The results of codeforces contest 2051 could not be posted in guild '
            f'{GUILD} yet (guild-unavailable); trying again later'
        ) in logged(caplog)

        await service.run(NOW + 5 * MINUTE)

        # The other server's post isn't sent again.
        assert keys(publisher) == [
            f'results:{OTHER_GUILD}:codeforces:2051',
            f'results:{GUILD}:codeforces:2051',
        ]
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.POSTED,
        )

    async def test_a_server_posted_in_before_is_not_posted_in_again(
        self,
        contests: ContestRepo,
        results: ResultRepo,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        publisher: FakePublisher,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        linked: Linked,
    ) -> None:
        await results.add_done([PAST], ResultOutcome.POSTED, now=PAST.end)
        channels = ChannelGone(publisher)
        service = ContestResults(
            contests,
            results,
            guild_settings,
            ledger,
            channels,
            atcoder,
            clock,
            linked_members=linked,
        )
        await follow(guild_settings, GUILD)
        await follow(guild_settings, OTHER_GUILD)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        linked.link(OTHER_GUILD, CODEFORCES, CAT, 'Amber_Owl')
        channels.gone = {OTHER_GUILD}
        await service.report_codeforces(ROUND, [rated('Amber_Owl', 30, 1500, 1623)])
        # GUILD's channel is deleted after its post, and OTHER_GUILD's is back.
        channels.gone = {GUILD}
        channels.asked.clear()

        await service.run(NOW + 5 * MINUTE)

        # A post in GUILD again would be UNDELIVERABLE, as DiscordPublisher
        # checks the channel first, and the contest would never be done.
        assert channels.asked == [OTHER_GUILD]
        assert keys(publisher) == [
            f'results:{GUILD}:codeforces:2051',
            f'results:{OTHER_GUILD}:codeforces:2051',
        ]
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.POSTED,
        )

    async def test_a_post_the_job_cannot_deliver_waits_for_its_next_run(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        tle.contests = [ROUND]
        tle.changes = {2051: [rated('Amber_Owl', 30, 1500, 1623)]}
        publisher.undeliverable_next(2)

        await service.run(NOW)  # the first try
        await service.run(NOW + 5 * MINUTE)  # the second, not in the same run

        record = await record_of(results, CODEFORCES, '2051')
        assert record.status is ResultStatus.POSTING

        await service.run(NOW + 10 * MINUTE)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']

    async def test_undeliverable_posts_are_given_up_48_hours_after_the_end(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        publisher.undeliverable_next(3, reason='channel-missing')
        await service.report_codeforces(ROUND, [rated('Amber_Owl', 30, 1500, 1623)])

        await service.run(NOW + 5 * MINUTE)
        await service.run(after_round(47 * HOUR))

        record = await record_of(results, CODEFORCES, '2051')
        assert record.status is ResultStatus.POSTING

        await service.run(after_round(48 * HOUR))

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.EXPIRED,
        )

    async def test_a_contest_that_fails_leaves_the_others_to_go_on(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        tle.contests = [cf_round(2060, ABC_END - 2 * HOUR)]
        tle.changes = {2060: [rated('Amber_Owl', 30, 1500, 1623, 2060)]}
        await add_abc(contests)
        linked.link(GUILD, ATCODER, AMBER, 'Fake_AtCoder')
        atcoder.set('Fake_AtCoder', 1200, 5)
        failure = RuntimeError('boom')
        linked.errors[CODEFORCES] = failure
        at = ABC_END - 30 * MINUTE
        await clock.advance_to(at)

        with pytest.raises(RuntimeError, match=r'\(1 failed\)') as raised:
            await service.run(at)

        assert raised.value.__cause__ is failure
        # The AtCoder baselines were taken all the same.
        assert len(await results.entries(ATCODER, 'abc478')) == 1
        assert 'Could not deal with the results of codeforces:2060' in logged(caplog)


def after_round(delta: timedelta) -> datetime:
    """``delta`` after ``ROUND`` ended."""
    return NOW - HOUR + delta


def started(missed: int) -> str:
    """What is logged as contest results start, with ``missed`` stored."""
    return (
        'Contest results start now: not posting the results of contests that '
        f'ended before now, such as the {missed} Codeforces contests that TLE '
        'lists as finished in the last 48 hours'
    )


NOT_POSTED = (
    'Not posting the results of Codeforces contest 2051: it ended before contest '
    'results started'
)


class TestNewInstall:
    async def test_contests_that_ended_before_are_never_posted(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        tle.contests = [
            cf_round(2049, NOW - 50 * HOUR),
            cf_round(2051, NOW - 3 * HOUR),
            cf_round(2052, NOW + 2 * HOUR, phase='BEFORE'),
        ]
        tle.changes = {
            2049: [rated('Amber_Owl', 3, 1400, 1500, 2049)],
            2051: [rated('Amber_Owl', 30, 1500, 1623)],
        }
        service = make_service()

        await service.run(NOW)

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.MISSED,
        )
        assert await results.get(CODEFORCES, '2049') is None
        assert tle.asked == []
        assert logged(caplog) == [started(1)]
        assert await results.started_at() == NOW

        # A contest that ends afterwards is posted.
        tle.contests[2] = cf_round(2052, NOW + 2 * HOUR)
        tle.changes[2052] = [rated('Amber_Owl', 12, 1623, 1700, 2052)]
        await clock.advance_to(NOW + 4 * HOUR)
        await service.run(NOW + 4 * HOUR)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2052']

    async def test_tles_report_of_a_contest_that_ended_before_is_not_posted(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        tle.contests = [ROUND]

        await make_service().report_codeforces(
            ROUND, [rated('Amber_Owl', 30, 1500, 1623)]
        )

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert record.outcome is ResultOutcome.MISSED

    async def test_a_contest_tle_lists_only_later_that_ended_before_isnt_posted(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        service = make_service()

        await service.run(NOW)  # TLE hasn't loaded its list yet

        assert await results.is_empty()
        assert await results.started_at() == NOW

        tle.contests = [ROUND]
        tle.changes = {2051: [rated('Amber_Owl', 30, 1500, 1623)]}
        await service.run(NOW + 5 * MINUTE)

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert record.outcome is ResultOutcome.MISSED
        assert logged(caplog) == [started(0), NOT_POSTED]

    @pytest.mark.parametrize(
        'phase',
        ['PENDING_SYSTEM_TEST', 'SYSTEM_TEST', 'CODING'],
        ids=['hacking', 'system tests', 'running as TLE last saw it'],
    )
    @pytest.mark.parametrize('restart', [False, True], ids=['', 'after a restart'])
    @pytest.mark.parametrize('by', ['tle', 'the job'])
    async def test_a_round_that_ended_before_isnt_posted_whatever_its_phase_then(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
        phase: str,
        restart: bool,
        by: str,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        # The round ended 3 hours before the install. TLE's list has it in its
        # 12-hour hacking phase or its system tests, or, as loaded from TLE's
        # disk after a long stop, still running.
        tle.contests = [cf_round(2051, NOW - 3 * HOUR, phase=phase)]
        service = make_service()
        await service.run(NOW)
        assert await results.is_empty()

        # It is finished and rated the next morning.
        finished = cf_round(2051, NOW - 3 * HOUR)
        tle.contests = [finished]
        changes = [rated('Amber_Owl', 30, 1500, 1623)]
        tle.changes = {2051: changes}
        later = NOW + 12 * HOUR
        await clock.advance_to(later)
        if restart:
            service = make_service()
        if by == 'tle':
            await service.report_codeforces(finished, changes)
        else:
            await service.run(later)

        assert publisher.posts == []
        record = await record_of(results, CODEFORCES, '2051')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.MISSED,
        )
        assert NOT_POSTED in logged(caplog)

    async def test_a_round_moved_to_after_the_install_is_posted(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        # TLE's list, as loaded from its disk, still has the round at a time
        # that has passed: Codeforces has moved it to the next day.
        tle.contests = [cf_round(2051, NOW - HOUR, phase='BEFORE')]
        service = make_service()
        await service.run(NOW)

        moved = cf_round(2051, NOW + 20 * HOUR)
        tle.contests = [moved]
        changes = [rated('Amber_Owl', 30, 1500, 1623)]
        tle.changes = {2051: changes}
        await clock.advance_to(NOW + 22 * HOUR)
        await service.report_codeforces(moved, changes)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']

    async def test_a_contest_that_ends_after_the_install_is_posted(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        # No contest ended in the last 48 hours, so nothing is stored.
        tle.contests = [cf_round(2049, NOW - 72 * HOUR)]
        service = make_service()
        await service.run(NOW)
        assert await results.is_empty()

        tle.contests.append(cf_round(2051, NOW + HOUR))
        tle.changes = {2051: [rated('Amber_Owl', 30, 1500, 1623)]}
        await clock.advance_to(NOW + 3 * HOUR)
        await service.run(NOW + 3 * HOUR)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']

    async def test_a_restart_does_not_start_them_again(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        # Installed in a quiet week: no contest ended in the 48 hours before,
        # so no contest is stored.
        tle.contests = [cf_round(2049, NOW - 72 * HOUR)]
        await make_service().run(NOW)
        assert await results.is_empty()
        # A round ends an hour later. Its system tests are over when the bot
        # restarts, but Codeforces hasn't published its ratings yet.
        after = cf_round(2051, NOW + HOUR)
        tle.contests.append(after)
        await clock.advance_to(NOW + 2 * HOUR)
        restarted = make_service()

        await restarted.run(NOW + 2 * HOUR)

        assert await results.get(CODEFORCES, '2051') is None

        # TLE saves its ratings, and says so.
        changes = [rated('Amber_Owl', 30, 1500, 1623)]
        tle.changes = {2051: changes}
        await clock.advance_to(NOW + 3 * HOUR)
        await restarted.report_codeforces(after, changes)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']
        assert await results.started_at() == NOW
        starts = [line for line in logged(caplog) if line.startswith('Contest results')]
        assert starts == [started(0)]

    async def test_a_restart_catches_up_on_what_was_rated_while_the_bot_was_down(
        self,
        make_service: Callable[[], ContestResults],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        linked: Linked,
        tle: FakeTle,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        linked.link(GUILD, CODEFORCES, AMBER, 'Amber_Owl')
        tle.contests = [cf_round(2049, NOW - 72 * HOUR)]
        await make_service().run(NOW)
        # The bot is down from just after the install until 5 hours on. A
        # round ends meanwhile, and TLE saves its ratings as the bot starts
        # again, before KCPC listens.
        tle.contests.append(cf_round(2051, NOW + HOUR))
        tle.changes = {2051: [rated('Amber_Owl', 30, 1500, 1623)]}
        await clock.advance_to(NOW + 5 * HOUR)

        await make_service().run(NOW + 5 * HOUR)

        assert keys(publisher) == [f'results:{GUILD}:codeforces:2051']

    async def test_they_start_once_per_install(
        self,
        make_service: Callable[[], ContestResults],
        results: ResultRepo,
        tle: FakeTle,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        tle.contests = [ROUND]
        await make_service().run(NOW)
        await clock.advance_to(NOW + HOUR)

        await make_service().run(NOW + HOUR)

        assert await results.started_at() == NOW
        assert logged(caplog) == [started(1)]


async def add_abc(contests: ContestRepo, *others: ContestInfo) -> None:
    """Store ABC 478 (12:00 to 13:40 on 2026-10-03), and ``others``."""
    abc = ContestInfo(
        ATCODER,
        'abc478',
        'AtCoder Beginner Contest 478',
        ABC_START,
        None,
        ABC_END,
        'https://atcoder.jp/contests/abc478',
    )
    await contests.add([abc, *others], now=NOW)


def atcoder_contest(contest_id: str, end: datetime) -> ContestInfo:
    return ContestInfo(
        ATCODER,
        contest_id,
        f'AtCoder contest {contest_id}',
        ABC_START,
        None,
        end,
        f'https://atcoder.jp/contests/{contest_id}',
    )


async def run_at(service: ContestResults, clock: FakeClock, at: datetime) -> None:
    await clock.advance_to(at)
    await service.run(at)


async def run_every_5_minutes(
    service: ContestResults, clock: FakeClock, start: datetime, end: datetime
) -> None:
    """Run the job every 5 minutes from ``start`` to ``end``, both included."""
    at = start
    while at <= end:
        await run_at(service, clock, at)
        at += 5 * MINUTE


class TestAtCoderBaselines:
    async def test_they_are_taken_in_the_contests_last_half_hour(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings, GUILD)
        await follow(guild_settings, OTHER_GUILD)
        await add_abc(contests)
        linked.link(GUILD, ATCODER, AMBER, 'amber_owl')
        # The same account, linked in another server: read once.
        linked.link(OTHER_GUILD, ATCODER, CAT, 'Amber_Owl')
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')
        atcoder.set('Amber_Owl', 1200, 5, highest=1300)
        atcoder.set('Bee_Bot', None, 0)

        await run_at(service, clock, ABC_END - 31 * MINUTE)

        assert atcoder.reads == []

        await run_at(service, clock, ABC_END - 30 * MINUTE)

        assert atcoder.read_handles() == ['amber_owl', 'Bee_Bot']
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.next_check, record.name, record.url) == (
            ResultStatus.WATCHING,
            ABC_END + 15 * MINUTE,
            'AtCoder Beginner Contest 478',
            'https://atcoder.jp/contests/abc478',
        )
        taken = ABC_END - 30 * MINUTE
        # In AtCoder's case.
        assert await results.entries(ATCODER, 'abc478') == [
            ResultEntry(
                'Amber_Owl', 1200, None, taken, old_matches=5, old_highest=1300
            ),
            ResultEntry('Bee_Bot', None, None, taken, old_matches=0),
        ]

        # A handle linked meanwhile gets its baseline at the next run.
        linked.link(GUILD, ATCODER, DOG, 'Dog_Day')
        atcoder.set('Dog_Day', 800, 3)
        await run_at(service, clock, ABC_END - 25 * MINUTE)

        assert atcoder.read_handles() == ['amber_owl', 'Bee_Bot', 'Dog_Day']

        # None once the contest has ended.
        linked.link(GUILD, ATCODER, BEE + 10, 'Late_Owl')
        atcoder.set('Late_Owl', 1000, 1)
        await run_at(service, clock, ABC_END)

        assert atcoder.read_handles(since=ABC_END) == []

    async def test_only_abc_arc_and_agc_contests_are_watched(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(
            contests,
            atcoder_contest('arc231', ABC_END),
            atcoder_contest('agc070', ABC_END),
            atcoder_contest('ahc050', ABC_END),
            atcoder_contest('awc0167', ABC_END),
            atcoder_contest('abc478x', ABC_END),
        )
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')
        atcoder.set('Amber_Owl', 1200, 5)

        await run_at(service, clock, ABC_END - 10 * MINUTE)

        watching = await results.with_status(ResultStatus.WATCHING)
        assert [record.external_id for record in watching] == [
            'abc478',
            'agc070',
            'arc231',
        ]
        # One read for all of them.
        assert atcoder.read_handles() == ['Amber_Owl']

    async def test_none_without_a_server_that_wants_them(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings, GUILD, platforms=('codeforces',))
        await follow(guild_settings, OTHER_GUILD, results_posts=False)
        await add_abc(contests)
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')
        linked.link(OTHER_GUILD, ATCODER, BEE, 'Bee_Bot')
        atcoder.set('Amber_Owl', 1200, 5)

        await run_at(service, clock, ABC_END - 10 * MINUTE)

        assert atcoder.reads == []
        assert await results.get(ATCODER, 'abc478') is None

    async def test_unknown_and_invalid_handles_get_none(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests)
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')
        linked.link(GUILD, ATCODER, BEE, 'Gone_Owl')
        linked.link(GUILD, ATCODER, CAT, 'not valid')
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.errors['not valid'] = KcpcUserError(
            "That isn't a valid AtCoder username."
        )

        await run_at(service, clock, ABC_END - 10 * MINUTE)

        entries = await results.entries(ATCODER, 'abc478')
        assert [entry.handle for entry in entries] == ['Amber_Owl']

    async def test_atcoder_failing_leaves_the_rest_to_the_next_run(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests)
        for user_id, handle in (
            (AMBER, 'Amber_Owl'),
            (BEE, 'Bee_Bot'),
            (CAT, 'Cat_Nap'),
        ):
            linked.link(GUILD, ATCODER, user_id, handle)
            atcoder.set(handle, 1200, 5)
        atcoder.errors['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)

        await run_at(service, clock, ABC_END - 30 * MINUTE)

        assert atcoder.read_handles() == ['Amber_Owl', 'Bee_Bot']
        entries = await results.entries(ATCODER, 'abc478')
        assert [entry.handle for entry in entries] == ['Amber_Owl']
        assert (
            f'Could not take the baselines of AtCoder contest abc478: {UNREACHABLE}'
            in logged(caplog)
        )

        del atcoder.errors['bee_bot']
        await run_at(service, clock, ABC_END - 25 * MINUTE)

        assert atcoder.read_handles(since=ABC_END - 25 * MINUTE) == [
            'Bee_Bot',
            'Cat_Nap',
        ]


async def watch_abc(
    service: ContestResults,
    guild_settings: GuildSettingsRepo,
    contests: ContestRepo,
    linked: Linked,
    atcoder: FakeAtCoder,
    clock: FakeClock,
    *members: tuple[int, str, int | None, int],
) -> None:
    """Follow ABC 478 in GUILD with ``members`` (user ID, handle, rating, rated
    matches), whose baselines are taken half an hour before its end."""
    await follow(guild_settings)
    await add_abc(contests)
    for user_id, handle, rating, matches in members:
        linked.link(GUILD, ATCODER, user_id, handle)
        atcoder.set(handle, rating, matches)
    await run_at(service, clock, ABC_END - 30 * MINUTE)


ABC_MEMBERS = (
    (AMBER, 'Amber_Owl', 1200, 5),
    (BEE, 'Bee_Bot', 1450, 7),
    (CAT, 'Cat_Nap', None, 0),
)


class TestAtCoderChecks:
    async def test_profiles_are_read_15_minutes_after_the_end_then_every_30(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await watch_abc(
            service,
            guild_settings,
            contests,
            linked,
            atcoder,
            clock,
            (AMBER, 'Amber_Owl', 1200, 5),
        )

        await run_every_5_minutes(
            service, clock, ABC_END - 25 * MINUTE, ABC_END + 7 * HOUR
        )

        checks = [at - ABC_END for at, _ in atcoder.reads[1:]]
        assert checks == [(15 + 30 * number) * MINUTE for number in range(12)]
        assert checks[-1] == 5 * HOUR + 45 * MINUTE
        assert publisher.posts == []
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.outcome, record.checks) == (
            ResultStatus.DONE,
            ResultOutcome.NOBODY,
            12,
        )
        assert 'No rating changed in AtCoder contest abc478 within 6h of its end' in (
            logged(caplog)
        )

    async def test_the_first_change_found_posts_the_results(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        await run_at(service, clock, ABC_END + 15 * MINUTE)
        # AtCoder rates the contest.
        atcoder.set('Amber_Owl', 1290, 6, highest=1290)
        atcoder.set('Bee_Bot', 1430, 8, highest=1500)
        atcoder.set('Cat_Nap', 456, 1)

        await run_every_5_minutes(
            service, clock, ABC_END + 20 * MINUTE, ABC_END + 2 * HOUR
        )

        assert [at - ABC_END for at, _ in atcoder.reads[3:]] == [15 * MINUTE] * 3 + [
            45 * MINUTE
        ] * 3
        assert keys(publisher) == [f'results:{GUILD}:atcoder:abc478']
        message = publisher.posts[0].message
        assert message.title == 'Results: AtCoder Beginner Contest 478'
        assert message.url == 'https://atcoder.jp/contests/abc478'
        assert not message.mention_role
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
            ac_line(BEE, 'Bee_Bot', '1450 → 1430 (**-20**)'),
            ac_line(CAT, 'Cat_Nap', 'first rated contest: 456'),
        ]
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.outcome, record.checks) == (
            ResultStatus.DONE,
            ResultOutcome.POSTED,
            2,
        )
        assert record.found_at == ABC_END + 45 * MINUTE

    async def test_handles_read_before_the_first_change_are_read_again(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )

        def rate() -> None:
            atcoder.set('Amber_Owl', 1290, 6)
            atcoder.set('Bee_Bot', 1430, 8, highest=1500)

        # AtCoder rates the contest just after Amber_Owl's profile is read.
        atcoder.after_read = rate
        check = ABC_END + 15 * MINUTE
        await run_at(service, clock, check)

        assert atcoder.read_handles(since=check) == [
            'Amber_Owl',
            'Bee_Bot',
            'Cat_Nap',
            'Amber_Owl',
        ]
        # Cat_Nap, read after the first change, wasn't rated.
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
            ac_line(BEE, 'Bee_Bot', '1450 → 1430 (**-20**)'),
        ]

    async def test_a_handle_read_again_without_a_change_is_left_out(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        atcoder.set('Bee_Bot', 1430, 8, highest=1500)
        check = ABC_END + 15 * MINUTE

        await run_at(service, clock, check)

        assert atcoder.read_handles(since=check) == [
            'Amber_Owl',
            'Bee_Bot',
            'Cat_Nap',
            'Amber_Owl',
        ]
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(BEE, 'Bee_Bot', '1450 → 1430 (**-20**)'),
        ]

    async def test_atcoder_failing_after_a_change_finishes_the_read_next_run(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        atcoder.set('Amber_Owl', 1290, 6)
        atcoder.set('Bee_Bot', 1430, 8)
        atcoder.errors['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)
        check = ABC_END + 15 * MINUTE

        await run_at(service, clock, check)

        assert atcoder.read_handles(since=check) == ['Amber_Owl', 'Bee_Bot']
        assert publisher.posts == []
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.next_check) == (ResultStatus.WATCHING, check)
        assert (
            "Could not read every profile for AtCoder contest abc478's results: "
            f'{UNREACHABLE}'
        ) in logged(caplog)

        del atcoder.errors['bee_bot']
        await run_at(service, clock, check + 5 * MINUTE)

        # A change was seen before these reads, so none is read again.
        assert atcoder.read_handles(since=check + 5 * MINUTE) == ['Bee_Bot', 'Cat_Nap']
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
            ac_line(BEE, 'Bee_Bot', '1450 → 1430 (**-20**)'),
        ]

    async def test_atcoder_failing_before_any_change_waits_for_the_next_check(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        atcoder.errors['amber_owl'] = ExternalServiceError('AtCoder', UNREACHABLE)

        await run_every_5_minutes(
            service, clock, ABC_END + 15 * MINUTE, ABC_END + 40 * MINUTE
        )

        assert atcoder.read_handles(since=ABC_END) == ['Amber_Owl']
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.checks, record.next_check) == (1, ABC_END + 45 * MINUTE)

    async def test_a_handle_atcoder_no_longer_has_is_dropped(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        atcoder.remove('Bee_Bot')

        await run_at(service, clock, ABC_END + 15 * MINUTE)
        await run_at(service, clock, ABC_END + 45 * MINUTE)

        assert atcoder.read_handles(since=ABC_END + 45 * MINUTE) == [
            'Amber_Owl',
            'Cat_Nap',
        ]
        entries = await results.entries(ATCODER, 'abc478')
        assert [entry.handle for entry in entries] == ['Amber_Owl', 'Cat_Nap']

    async def test_a_contest_left_without_handles_is_missed(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service,
            guild_settings,
            contests,
            linked,
            atcoder,
            clock,
            (AMBER, 'Amber_Owl', 1200, 5),
        )
        atcoder.remove('Amber_Owl')

        await run_at(service, clock, ABC_END + 15 * MINUTE)
        await run_at(service, clock, ABC_END + 45 * MINUTE)

        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.outcome) == (
            ResultStatus.DONE,
            ResultOutcome.MISSED,
        )

    @pytest.mark.parametrize('reads', [0, 2])
    async def test_after_its_6_hours_a_contest_is_read_no_more(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
        reads: int,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        for number in range(reads):
            await run_at(service, clock, ABC_END + (15 + 30 * number) * MINUTE)
        atcoder.set('Amber_Owl', 1290, 6)
        down = ABC_END + (15 + 30 * reads) * MINUTE

        # The bot was down from that check until 7 hours on.
        await run_at(service, clock, ABC_END + 7 * HOUR)

        assert atcoder.read_handles(since=down) == []
        assert publisher.posts == []
        record = await record_of(results, ATCODER, 'abc478')
        # Not 'nobody': no read could tell whether anyone took part.
        assert (record.status, record.outcome, record.checks) == (
            ResultStatus.DONE,
            ResultOutcome.MISSED,
            reads,
        )
        assert logged(caplog)[-1] == (
            f'Stopped watching AtCoder contest abc478 after {reads} reads: the '
            'results job was not running when its 6h of reads ran out'
        )

    async def test_a_last_read_that_atcoder_cut_short_says_so(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        last = ABC_END + 5 * HOUR + 45 * MINUTE
        await run_every_5_minutes(
            service, clock, ABC_END + 15 * MINUTE, last - 5 * MINUTE
        )
        atcoder.errors['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)

        await run_at(service, clock, last)

        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.outcome, record.checks) == (
            ResultStatus.DONE,
            ResultOutcome.NOBODY,
            12,
        )
        assert logged(caplog)[-1] == (
            'No rating changed in AtCoder contest abc478 within 6h of its end '
            '(AtCoder failed during the last read)'
        )

    async def test_a_change_read_before_atcoder_failed_is_posted_after_the_rest(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        # AtCoder rates the contest; Bee_Bot and Cat_Nap didn't take part.
        atcoder.set('Amber_Owl', 1290, 6)
        atcoder.errors_once['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)
        check = ABC_END + 15 * MINUTE
        await run_at(service, clock, check)
        assert publisher.posts == []

        await run_at(service, clock, check + 5 * MINUTE)

        assert atcoder.read_handles(since=check + 5 * MINUTE) == ['Bee_Bot', 'Cat_Nap']
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
        ]
        record = await record_of(results, ATCODER, 'abc478')
        assert (record.status, record.outcome, record.found_at) == (
            ResultStatus.DONE,
            ResultOutcome.POSTED,
            check + 5 * MINUTE,
        )

    async def test_a_read_after_its_time_keeps_the_next_on_the_schedule(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service,
            guild_settings,
            contests,
            linked,
            atcoder,
            clock,
            (AMBER, 'Amber_Owl', 1200, 5),
        )

        # The bot was down 15 minutes after the end, and is back 5 minutes on.
        await run_at(service, clock, ABC_END + 20 * MINUTE)

        record = await record_of(results, ATCODER, 'abc478')
        assert (record.checks, record.next_check) == (1, ABC_END + 45 * MINUTE)

    async def test_changes_found_before_the_6_hours_ran_out_are_posted(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await watch_abc(
            service, guild_settings, contests, linked, atcoder, clock, *ABC_MEMBERS
        )
        atcoder.set('Amber_Owl', 1290, 6)
        atcoder.errors['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)
        await run_at(service, clock, ABC_END + 15 * MINUTE)

        # The bot was down until 7 hours on, and AtCoder still fails.
        await run_at(service, clock, ABC_END + 7 * HOUR)

        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
        ]

    async def test_a_match_one_contest_claims_isnt_claimed_by_the_next(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        # ABC 478 ends at 13:40 and ARC 231 at 14:00.
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ABC
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')  # and in the ARC
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.set('Bee_Bot', 1450, 7)
        await run_at(service, clock, ABC_END - 30 * MINUTE)  # the ABC's baselines
        await run_at(service, clock, ARC_END - 30 * MINUTE)  # the ARC's
        atcoder.set('Amber_Owl', 1290, 6)  # the ABC is rated

        await run_at(service, clock, ABC_END + 15 * MINUTE)

        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(AMBER, 'Amber_Owl', '1200 → 1290 (**+90**), new best'),
        ]
        arc = {
            entry.handle: entry for entry in await results.entries(ATCODER, 'arc231')
        }
        assert (arc['Amber_Owl'].old_rating, arc['Amber_Owl'].old_matches) == (1290, 6)

        atcoder.set('Bee_Bot', 1500, 8)  # the ARC is rated
        await run_at(service, clock, ARC_END + 15 * MINUTE)

        assert keys(publisher) == [
            f'results:{GUILD}:atcoder:abc478',
            f'results:{GUILD}:atcoder:arc231',
        ]
        assert lines(publisher) == [
            '**Platform:** AtCoder',
            ac_line(BEE, 'Bee_Bot', '1450 → 1500 (**+50**), new best'),
        ]
        record = await record_of(results, ATCODER, 'arc231')
        assert record.outcome is ResultOutcome.POSTED


# When AtCoder rates a handle in a contest: then its rating and rated matches.
Rating = tuple[datetime, str, int, int]


async def watch_until(
    service: ContestResults,
    clock: FakeClock,
    atcoder: FakeAtCoder,
    start: datetime,
    end: datetime,
    ratings: Sequence[Rating],
) -> None:
    """Run the job every 5 minutes from ``start`` to ``end``, both included;
    each rating shows on its profile from its time on."""
    pending = sorted(ratings)
    at = start
    while at <= end:
        while pending and pending[0][0] <= at:
            _, handle, rating, matches = pending.pop(0)
            atcoder.set(handle, rating, matches)
        await run_at(service, clock, at)
        at += 5 * MINUTE


# A contest from 14:30 to 16:30, after ABC 478 (12:00 to 13:40).
LATER_START = ABC_START + 150 * MINUTE
LATER_END = LATER_START + 2 * HOUR


def later_arc() -> ContestInfo:
    return ContestInfo(
        ATCODER,
        'arc231',
        'AtCoder contest arc231',
        LATER_START,
        None,
        LATER_END,
        'https://atcoder.jp/contests/arc231',
    )


class TestAtCoderOverlap:
    """Contests read at the same time: ABC 478 ends at 13:40, and the other
    at 14:00 (ARC_END) unless said otherwise."""

    @pytest.mark.parametrize(
        ('rated', 'found'),
        [
            (10 * MINUTE, ABC_END + 15 * MINUTE),  # before its own first read
            (30 * MINUTE, ARC_END + 15 * MINUTE),  # after the ARC ended
        ],
        ids=['before its first read', 'after the other ended'],
    )
    async def test_the_contest_that_ended_first_gets_its_rise_whatever_reads_first(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        rated: timedelta,
        found: datetime,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ABC
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')  # and in the ARC
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.set('Bee_Bot', 1450, 7)

        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            ABC_END + 7 * HOUR,
            [
                (ABC_END + rated, 'Amber_Owl', 1290, 6),
                (ARC_END + HOUR, 'Bee_Bot', 1500, 8),
            ],
        )

        assert by_contest(publisher) == [('abc478', [AMBER]), ('arc231', [BEE])]
        abc = await record_of(results, ATCODER, 'abc478')
        assert abc.found_at == found
        # Not before AtCoder rated it, at 15:00.
        arc = await record_of(results, ATCODER, 'arc231')
        assert arc.found_at == ARC_END + 75 * MINUTE
        # Each profile is read once a run, whichever contests read it.
        at_arcs_first_read = ARC_END + 15 * MINUTE
        assert [handle for at, handle in atcoder.reads if at == at_arcs_first_read] == [
            'Amber_Owl',
            'Bee_Bot',
        ]

    async def test_a_rise_goes_to_the_first_if_the_later_baseline_was_read_too_soon(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        # The ARC ends an hour after the ABC, so its baselines are read from
        # half an hour after the ABC's end: perhaps before AtCoder rated it.
        await add_abc(contests, atcoder_contest('arc231', ABC_END + HOUR))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ABC
        atcoder.set('Amber_Owl', 1200, 5)

        # AtCoder rates the ABC late, after the ARC ended.
        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            ABC_END + 7 * HOUR,
            [(ABC_END + 70 * MINUTE, 'Amber_Owl', 1290, 6)],
        )

        assert by_contest(publisher) == [('abc478', [AMBER])]

    @pytest.mark.parametrize(
        ('rated', 'found'),
        [
            (10 * MINUTE, LATER_END + 15 * MINUTE),  # read first for the ARC
            (20 * MINUTE, LATER_END + 30 * MINUTE),  # read first for the ABC
        ],
        ids=['read for itself first', 'read for the earlier first'],
    )
    async def test_a_contest_well_after_one_no_member_took_part_in_gets_its_rise(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        rated: timedelta,
        found: datetime,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, later_arc())
        # Amber_Owl takes part in the ARC only. Her baseline for it, taken
        # from 16:00, over an hour after the ABC ended, when AtCoder had
        # rated the ABC, has the rated matches of her ABC one.
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')
        atcoder.set('Amber_Owl', 1200, 5)

        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            LATER_END + 7 * HOUR,
            [(LATER_END + rated, 'Amber_Owl', 1290, 6)],
        )

        assert by_contest(publisher) == [('arc231', [AMBER])]
        arc = await record_of(results, ATCODER, 'arc231')
        assert arc.found_at == found
        abc = await record_of(results, ATCODER, 'abc478')
        assert (abc.status, abc.outcome) == (ResultStatus.DONE, ResultOutcome.NOBODY)

    async def test_a_contest_two_hours_after_one_no_member_took_part_in_gets_its_rise(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        # The ARC ends at 15:40, two hours after the ABC: its baselines are
        # read from 15:10, an hour and a half after the ABC's end.
        arc_end = ABC_END + 2 * HOUR
        await add_abc(contests, atcoder_contest('arc231', arc_end))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ARC
        atcoder.set('Amber_Owl', 1200, 5)

        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            arc_end + 7 * HOUR,
            [(arc_end + 10 * MINUTE, 'Amber_Owl', 1290, 6)],
        )

        assert by_contest(publisher) == [('arc231', [AMBER])]
        arc = await record_of(results, ATCODER, 'arc231')
        assert arc.found_at == arc_end + 15 * MINUTE

    async def test_a_rise_the_earlier_contest_could_not_read_waits_for_it(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')  # takes part in the ABC
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.set('Bee_Bot', 1450, 7)
        await run_at(service, clock, ABC_END - 30 * MINUTE)  # the ABC's baselines
        await run_at(service, clock, ARC_END - 30 * MINUTE)  # the ARC's
        await run_at(service, clock, ABC_END + 15 * MINUTE)  # nothing yet
        atcoder.set('Bee_Bot', 1500, 8)  # AtCoder rates the ABC
        # At the ARC's first read, AtCoder fails once, as Bee_Bot is read for
        # the ABC.
        atcoder.errors_once['bee_bot'] = ExternalServiceError('AtCoder', UNREACHABLE)

        await run_at(service, clock, ARC_END + 15 * MINUTE)

        # The ARC read the rise, but leaves it to the ABC, read again at once.
        assert publisher.posts == []
        abc = await record_of(results, ATCODER, 'abc478')
        assert abc.next_check == ARC_END + 15 * MINUTE

        await run_at(service, clock, ARC_END + 20 * MINUTE)

        assert by_contest(publisher) == [('abc478', [BEE])]
        assert lines(publisher)[1] == ac_line(
            BEE, 'Bee_Bot', '1450 → 1500 (**+50**), new best'
        )

    async def test_a_member_in_both_contests_is_posted_in_both(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, later_arc())
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in both
        atcoder.set('Amber_Owl', 1200, 5)
        # AtCoder rates the ABC late, at 15:58, just before the ARC's
        # baselines, and the bot is down from 16:05 until 16:45, while
        # AtCoder rates the ARC.
        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            LATER_START + 95 * MINUTE,
            [(LATER_START + 88 * MINUTE, 'Amber_Owl', 1250, 6)],
        )
        atcoder.set('Amber_Owl', 1300, 7)

        await run_at(service, clock, LATER_END + 15 * MINUTE)

        assert by_contest(publisher) == [('abc478', [AMBER]), ('arc231', [AMBER])]
        # Her change in the ARC is the ARC's own: from the baseline that had
        # the ABC's.
        assert lines(publisher)[1] == ac_line(
            AMBER, 'Amber_Owl', '1250 → 1300 (**+50**), new best'
        )

    async def test_contests_that_end_together_go_by_their_ids(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await contests.add(
            [atcoder_contest('arc231', ARC_END), atcoder_contest('arc232', ARC_END)],
            now=NOW,
        )
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in arc231
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')  # and in arc232
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.set('Bee_Bot', 1450, 7)

        await watch_until(
            service,
            clock,
            atcoder,
            ARC_END - 30 * MINUTE,
            ARC_END + 7 * HOUR,
            [
                (ARC_END + 10 * MINUTE, 'Bee_Bot', 1500, 8),  # arc232 first
                (ARC_END + HOUR, 'Amber_Owl', 1290, 6),
            ],
        )

        # Nothing tells the contests apart: a rise goes to the first by ID.
        assert by_contest(publisher) == [('arc231', [BEE]), ('arc232', [AMBER])]

    async def test_rises_read_together_go_to_the_contest_that_ended_first(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ABC
        linked.link(GUILD, ATCODER, BEE, 'Bee_Bot')  # and in the ARC
        atcoder.set('Amber_Owl', 1200, 5)
        atcoder.set('Bee_Bot', 1450, 7)

        # AtCoder rates both between the reads at 13:55 and 14:15.
        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            ABC_END + 7 * HOUR,
            [
                (ABC_END + 28 * MINUTE, 'Amber_Owl', 1290, 6),
                (ARC_END + 13 * MINUTE, 'Bee_Bot', 1500, 8),
            ],
        )

        assert by_contest(publisher) == [('abc478', [AMBER, BEE])]
        arc = await record_of(results, ATCODER, 'arc231')
        assert arc.outcome is ResultOutcome.NOBODY

    async def test_an_earlier_contest_no_member_took_part_in_takes_the_rise(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ARC
        atcoder.set('Amber_Owl', 1200, 5)

        await watch_until(
            service,
            clock,
            atcoder,
            ABC_END - 30 * MINUTE,
            ABC_END + 7 * HOUR,
            [(ARC_END + 20 * MINUTE, 'Amber_Owl', 1290, 6)],
        )

        # Her baselines for both were taken before AtCoder rated either, so
        # her rise could be the ABC's: it goes to the contest that ended first.
        assert by_contest(publisher) == [('abc478', [AMBER])]
        arc = await record_of(results, ATCODER, 'arc231')
        assert arc.outcome is ResultOutcome.NOBODY

    async def test_a_contest_whose_reads_are_over_owns_no_rise(
        self,
        service: ContestResults,
        guild_settings: GuildSettingsRepo,
        contests: ContestRepo,
        publisher: FakePublisher,
        results: ResultRepo,
        linked: Linked,
        atcoder: FakeAtCoder,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await follow(guild_settings)
        await add_abc(contests, atcoder_contest('arc231', ARC_END))
        linked.link(GUILD, ATCODER, AMBER, 'Amber_Owl')  # takes part in the ARC
        atcoder.set('Amber_Owl', 1200, 5)
        await run_at(service, clock, ABC_END - 30 * MINUTE)  # the ABC's baselines
        await run_at(service, clock, ARC_END - 30 * MINUTE)  # the ARC's
        await run_at(service, clock, ABC_END + 15 * MINUTE)
        # The bot is down until after the ABC's 6 hours, at the ARC's last
        # read; AtCoder rated the ARC meanwhile. Closing the ABC fails.
        atcoder.set('Amber_Owl', 1290, 6)
        finish = results.finish

        async def fails_once(
            record: ResultRecord, outcome: ResultOutcome, *, now: datetime
        ) -> ResultRecord:
            monkeypatch.setattr(results, 'finish', finish)
            raise RuntimeError('boom')

        monkeypatch.setattr(results, 'finish', fails_once)

        with pytest.raises(RuntimeError, match=r'\(1 failed\)'):
            await run_at(service, clock, ARC_END + 5 * HOUR + 45 * MINUTE)

        # Still watched, the ABC can't take the ARC's rise.
        assert by_contest(publisher) == [('arc231', [AMBER])]


def entry(
    handle: str,
    old: int | None,
    new: int,
    *,
    place: int | None = None,
    highest: int | None = None,
) -> ResultEntry:
    return ResultEntry(
        handle,
        old,
        new,
        NOW,
        changed_at=NOW,
        place=place,
        old_highest=highest,
    )


def record(platform: str, name: str = 'Contest') -> ResultRecord:
    return ResultRecord(
        platform=platform,
        external_id='1',
        name=name,
        url=None,
        end=NOW,
        status=ResultStatus.POSTING,
        checks=0,
        next_check=None,
        found_at=NOW,
        outcome=None,
        updated_at=NOW,
    )


class TestPosts:
    def test_members_come_by_gain_then_rating_and_first_rated_last(self) -> None:
        members = [
            (1, entry('a_first', 0, 500, place=900)),
            (2, entry('loser', 1600, 1590, place=800)),
            (3, entry('gainer', 1550, 1600, place=300)),
            (4, entry('top', 1650, 1700, place=200)),
            (5, entry('b_first', 0, 900, place=400)),
            (6, entry('same', 1650, 1700, place=201)),
        ]

        post = results_post(record(CODEFORCES, 'Round'), members)

        assert post.description is not None
        assert post.description.splitlines() == [
            '**Platform:** Codeforces',
            cf_line(6, 'same', 201, '1650 → 1700 (**+50**)'),
            cf_line(4, 'top', 200, '1650 → 1700 (**+50**)'),
            cf_line(3, 'gainer', 300, '1550 → 1600 (**+50**), Specialist → Expert'),
            cf_line(2, 'loser', 800, '1600 → 1590 (**-10**), Expert → Specialist'),
            cf_line(5, 'b_first', 400, 'first rated contest: 900'),
            cf_line(1, 'a_first', 900, 'first rated contest: 500'),
        ]
        assert (post.title, post.url, post.footer) == (
            'Results: Round',
            None,
            'KCPC contests',
        )
        assert not post.mention_role

    def test_atcoder_lines_have_no_place_and_say_when_a_rating_is_a_new_best(
        self,
    ) -> None:
        members = [
            (1, entry('Peak', 1200, 1300, highest=1250)),
            (2, entry('Below', 1200, 1240, highest=1250)),
            (3, entry('New', None, 300)),
        ]

        post = results_post(record(ATCODER), members)

        assert post.description is not None
        assert post.description.splitlines() == [
            '**Platform:** AtCoder',
            ac_line(1, 'Peak', '1200 → 1300 (**+100**), new best'),
            ac_line(2, 'Below', '1200 → 1240 (**+40**)'),
            ac_line(3, 'New', 'first rated contest: 300'),
        ]

    def test_markdown_in_handles_is_escaped(self) -> None:
        post = results_post(record(ATCODER), [(1, entry('a_b_c', 1200, 1300))])

        assert post.description is not None
        assert post.description.splitlines()[1] == (
            '<@1> [a\\_b\\_c](https://atcoder.jp/users/a_b_c): 1200 → 1300 (**+100**)'
        )

    def test_at_most_30_members_are_listed(self) -> None:
        members = [
            (user_id, entry(f'user{user_id:02d}', 1500, 1500 + user_id))
            for user_id in range(40)
        ]

        post = results_post(record(ATCODER), members)

        assert post.description is not None
        shown = post.description.splitlines()
        assert len(shown) == 32
        assert shown[1] == ac_line(39, 'user39', '1500 → 1539 (**+39**)')
        assert shown[30] == ac_line(10, 'user10', '1500 → 1510 (**+10**)')
        assert shown[-1] == '…and 10 more'

    def test_fewer_are_listed_if_their_lines_would_be_too_long(self) -> None:
        handle = 'x' * 24
        members = [
            (
                10**18 + user_id,
                entry(f'{handle}{user_id:02d}', 3000, 1100, place=user_id),
            )
            for user_id in range(30)
        ]

        post = results_post(record(CODEFORCES, 'Round'), members)

        description = post.description
        assert description is not None
        assert len(description) <= DESCRIPTION_LIMIT
        shown = description.splitlines()
        hidden = 30 - (len(shown) - 2)
        assert 0 < hidden < 30
        assert shown[-1] == f'…and {hidden} more'
        assert post.within_discord_limits().description == description

    def test_the_line_that_says_how_many_more_always_fits(self) -> None:
        longest = 0
        for length in range(3, 25):
            members = [
                (
                    10**18 + user_id,
                    entry(f'{"x" * length}{user_id:02d}', 3000, 1100, place=user_id),
                )
                for user_id in range(30)
            ]

            post = results_post(record(CODEFORCES, 'Round'), members)

            description = post.description
            assert description is not None
            assert len(description) <= DESCRIPTION_LIMIT, length
            assert post.within_discord_limits().description == description
            shown = description.splitlines()
            listed = sum(1 for line in shown if '<@' in line)
            if listed < 30:
                assert shown[-1] == f'…and {30 - listed} more', length
            longest = max(longest, len(description))
        # Some of those lengths fill the room kept for that line.
        assert longest > DESCRIPTION_LIMIT - 50

    def test_a_rating_that_equals_the_best_before_is_no_new_best(self) -> None:
        post = results_post(
            record(ATCODER), [(1, entry('Tie', 1200, 1250, highest=1250))]
        )

        assert post.description is not None
        assert post.description.splitlines()[1] == ac_line(
            1, 'Tie', '1200 → 1250 (**+50**)'
        )
