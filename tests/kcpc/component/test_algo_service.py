"""Tests for tle.kcpc.features.algo.service: the algorithm of the month.

The service runs on a migrated in-memory kcpc.db with the real delivery ledger,
and posts through FakePublisher. Most tests pick from a small catalog of
made-up topics; a seeded ``random.Random`` makes picks repeatable, and
``FirstChoice`` and ``Recorded`` say which topic comes next.

The clock starts on Thursday 2026-10-01 at 12:00 UTC, an hour after
October's slot: noon in London, which is 11:00 UTC until the clocks go back on
2026-10-25, and 12:00 UTC after.
"""

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, TypeVar, cast

import pytest

from tests.kcpc.conftest import CLOCK_START
from tests.kcpc.fakes import FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome
from tle.kcpc.core.schedule import Monthly
from tle.kcpc.core.scheduler import JobStatus, ScheduledJob, Scheduler
from tle.kcpc.core.settings import GuildSettingsRepo
from tle.kcpc.core.timeutil import from_epoch, zone
from tle.kcpc.features.algo.catalog import AlgoTopic, Level, topic
from tle.kcpc.features.algo.repo import AlgoPick, AlgoRepo
from tle.kcpc.features.algo.service import (
    ALGO,
    ALGO_JOB,
    FOOTER,
    POST_DAY,
    POST_TIME,
    AlgoService,
    NoOtherTopic,
    PostResult,
    month_name,
    post_key,
)

T = TypeVar('T')

LOGGER = 'tle.kcpc.features.algo.service'
CLUB = zone('Europe/London')
SCHEDULE = Monthly(POST_DAY, POST_TIME, CLUB)
NOW = CLOCK_START
# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
STOP_TIMEOUT = 10  # real seconds


def first(month: int, year: int = 2026) -> datetime:
    """The slot of the 1st of ``month``: noon in London, in UTC."""
    return datetime(year, month, 1, 12, 0, tzinfo=CLUB).astimezone(UTC)


def made_up(number: int) -> AlgoTopic:
    """Topic ``number`` of a made-up catalog."""
    return AlgoTopic(
        slug=f'topic-{number}',
        name=f'Topic {number}',
        summary=f'What topic {number} is for.',
        level=Level.INTERMEDIATE,
        gfg_url=f'https://www.geeksforgeeks.org/dsa/topic-{number}/',
        cp_algorithms_url=f'https://cp-algorithms.com/topics/topic-{number}.html',
    )


TOPICS = tuple(made_up(number) for number in range(3))
EVERY_SLUG = ['topic-0', 'topic-1', 'topic-2']


def key(month: str, revision: int = 0, guild_id: int = GUILD) -> str:
    return f'algo:{guild_id}:{month}:r{revision}'


class FirstChoice(random.Random):
    """A ``random.Random`` that chooses the first of what it is offered."""

    def choice(self, seq: Sequence[T]) -> T:
        return seq[0]


class Recorded(FirstChoice):
    """A ``FirstChoice`` that lists the slugs of the topics it was offered."""

    def __init__(self) -> None:
        super().__init__()
        self.offered: list[list[str]] = []

    def choice(self, seq: Sequence[T]) -> T:
        self.offered.append([cast(AlgoTopic, found).slug for found in seq])
        return super().choice(seq)


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
def repo(db: Database) -> AlgoRepo:
    return AlgoRepo(db)


@pytest.fixture
def make_service(
    repo: AlgoRepo,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
    clock: FakeClock,
) -> Callable[..., AlgoService]:
    def make(
        *,
        topics: Sequence[AlgoTopic] = TOPICS,
        rng: random.Random | None = None,
        schedule: Monthly = SCHEDULE,
    ) -> AlgoService:
        return AlgoService(
            repo,
            guild_settings,
            ledger,
            publisher,
            clock,
            schedule,
            topics=topics,
            rng=random.Random(2026) if rng is None else rng,
        )

    return make


@pytest.fixture
def service(make_service: Callable[..., AlgoService]) -> AlgoService:
    return make_service()


@pytest.fixture(autouse=True)
def algo_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)


async def set_up(
    guild_settings: GuildSettingsRepo,
    guild_id: int = GUILD,
    *,
    enabled: bool = True,
    channel_id: int | None = CHANNEL,
) -> None:
    """Turn the algorithm of the month on in the guild."""
    await guild_settings.update(guild_id, ALGO, enabled=enabled, channel_id=channel_id)


def keys(publisher: FakePublisher) -> list[str]:
    return [key for post in publisher.posts for key in post.keys]


def lines(publisher: FakePublisher, number: int = -1) -> list[str]:
    """The lines of the description of post ``number``."""
    description = publisher.posts[number].message.description
    assert description is not None
    return description.splitlines()


def logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.INFO
    ]


async def pick_of(repo: AlgoRepo, month: str = '2026-10') -> AlgoPick:
    pick = await repo.get(GUILD, month)
    assert pick is not None
    return pick


async def went_out(repo: AlgoRepo, publisher: FakePublisher, pick: AlgoPick) -> None:
    """Store ``pick`` and record its post as sent, as a run would."""
    stored = await repo.create(pick)
    delivery = Delivery(
        post_key(stored),
        stored.guild_id,
        ALGO,
        subject='algo',
        subject_id=stored.month,
        kind='topic',
        occurrence_start=stored.slot,
    )
    result = await publisher.publish([delivery], OutgoingMessage(title='Earlier'))
    assert result.outcome is PublishOutcome.SENT


async def settle() -> None:
    """Give the other tasks a tenth of a second of real time to go on."""
    for _ in range(20):
        await asyncio.sleep(0.005)


def test_a_month_is_named_in_english() -> None:
    names = [month_name(f'2027-{number:02d}') for number in range(1, 13)]

    assert names == [
        'January',
        'February',
        'March',
        'April',
        'May',
        'June',
        'July',
        'August',
        'September',
        'October',
        'November',
        'December',
    ]


class TestRunGuild:
    async def test_post_now_posts_the_months_topic_once(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        slot = SCHEDULE.prev_at_or_before(clock.now())
        assert slot == first(10)

        result = await service.run_guild(GUILD, slot)

        pick = await pick_of(repo)
        assert result == PostResult(pick, PublishOutcome.SENT, None)
        assert keys(publisher) == ['algo:1100000000000000001:2026-10:r0']
        assert (pick.month, pick.slot, pick.revision, pick.picked_at) == (
            '2026-10',
            first(10),
            0,
            NOW,
        )
        assert pick.slug in EVERY_SLUG
        assert await service.posted(pick)

        again = await service.run_guild(GUILD, slot)

        assert again == PostResult(pick, PublishOutcome.ALREADY_HANDLED, None)
        assert len(publisher.posts) == 1
        assert await repo.history(GUILD) == [pick]

    async def test_the_post_names_the_topic_and_where_to_read_about_it(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        segment_tree = topic('segment-tree')
        assert segment_tree is not None
        service = make_service(topics=[segment_tree])
        await set_up(guild_settings)

        await service.run_guild(GUILD, first(10))

        [post] = publisher.posts
        assert post.deliveries == (
            Delivery(
                key='algo:1100000000000000001:2026-10:r0',
                guild_id=GUILD,
                feature='algo',
                subject='algo',
                subject_id='2026-10',
                kind='topic',
                occurrence_start=first(10),
                revision=0,
                expires_at=first(11),
            ),
        )
        assert post.message == OutgoingMessage(
            title='Algorithm of the month: Segment tree',
            description='\n'.join(
                [
                    segment_tree.summary,
                    '**Level:** intermediate',
                    '**Read:** [GeeksforGeeks](https://www.geeksforgeeks.org/dsa/'
                    'segment-tree-data-structure/) · [cp-algorithms]('
                    'https://cp-algorithms.com/data_structures/segment_tree.html)',
                ]
            ),
            url='https://www.geeksforgeeks.org/dsa/segment-tree-data-structure/',
            footer=FOOTER,
            mention_role=True,
        )

    async def test_a_topic_without_a_cp_algorithms_article_links_one_site(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        trie = topic('trie')
        assert trie is not None and trie.cp_algorithms_url is None
        await set_up(guild_settings)

        await make_service(topics=[trie]).run_guild(GUILD, first(10))

        assert lines(publisher)[1:] == [
            '**Level:** intermediate',
            '**Read:** [GeeksforGeeks](https://www.geeksforgeeks.org/dsa/'
            'trie-insert-and-search/)',
        ]

    async def test_a_summary_is_shown_as_written(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        starred = replace(made_up(0), summary='Uses *stars* and a_b.')
        await set_up(guild_settings)

        await make_service(topics=[starred]).run_guild(GUILD, first(10))

        assert lines(publisher)[0] == r'Uses \*stars\* and a\_b.'

    async def test_the_pick_is_stored_before_it_is_posted_and_kept_for_a_retry(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        ledger: DeliveryLedger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stored_when_posting: list[AlgoPick | None] = []
        publish = publisher.publish

        async def checking(deliveries: Any, message: OutgoingMessage) -> Any:
            stored_when_posting.append(await repo.get(GUILD, '2026-10'))
            return await publish(deliveries, message)

        monkeypatch.setattr(publisher, 'publish', checking)
        await set_up(guild_settings)
        publisher.undeliverable_next()

        failed = await make_service().run_guild(GUILD, first(10))

        picked = await pick_of(repo)
        assert failed == PostResult(
            picked, PublishOutcome.UNDELIVERABLE, 'guild-unavailable'
        )
        assert stored_when_posting == [picked]
        assert await ledger.get(post_key(picked)) is None

        # The retry picks nothing: it posts the topic the first try picked.
        recorded = Recorded()
        retried = await make_service(rng=recorded).run_guild(GUILD, first(10))

        assert recorded.offered == []
        assert retried == PostResult(picked, PublishOutcome.SENT, None)
        assert keys(publisher) == [key('2026-10')]
        name = TOPICS[EVERY_SLUG.index(picked.slug)].name
        assert publisher.posts[0].message.title == f'Algorithm of the month: {name}'

    @pytest.mark.parametrize(
        ('enabled', 'channel_id', 'reason'),
        [(False, CHANNEL, 'disabled'), (True, None, 'no-channel')],
        ids=['off', 'no channel'],
    )
    async def test_a_server_not_set_up_gets_nothing_written(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        ledger: DeliveryLedger,
        enabled: bool,
        channel_id: int | None,
        reason: str,
    ) -> None:
        await set_up(guild_settings, enabled=enabled, channel_id=channel_id)

        result = await service.run_guild(GUILD, first(10))

        assert result == PostResult(None, PublishOutcome.NOT_CONFIGURED, reason)
        assert publisher.posts == []
        assert await repo.history(GUILD) == []
        assert await ledger.status_counts(GUILD) == dict.fromkeys(DeliveryStatus, 0)

    async def test_an_instant_that_isnt_a_slot_runs_the_slot_before_it(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings)

        result = await service.run_guild(GUILD, first(10) + timedelta(days=12))
        again = await service.run_guild(GUILD, first(11) - SECOND)

        assert result.pick is not None
        assert (result.pick.month, result.pick.slot) == ('2026-10', first(10))
        assert again == replace(result, outcome=PublishOutcome.ALREADY_HANDLED)
        assert keys(publisher) == [key('2026-10')]

    async def test_a_topic_taken_out_of_the_catalog_before_its_post_is_replaced(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings)
        gone = AlgoPick(GUILD, '2026-10', first(10), 'gone', 0, first(10))
        await repo.create(gone)

        result = await make_service(rng=FirstChoice()).run_guild(GUILD, first(10))

        replacement = AlgoPick(GUILD, '2026-10', first(10), 'topic-0', 1, NOW)
        assert result == PostResult(replacement, PublishOutcome.SENT, None)
        assert await pick_of(repo) == replacement
        assert keys(publisher) == [key('2026-10', 1)]
        assert lines(publisher)[0] == 'What topic 0 is for.'  # replacing nothing
        assert (
            f"Picked topic-0 as guild {GUILD}'s algorithm of the month for 2026-10, "
            'in place of gone, which is no longer in the catalog'
        ) in logged(caplog)

    async def test_a_topic_taken_out_of_the_catalog_after_its_post_stays(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        await set_up(guild_settings)
        gone = await repo.create(
            AlgoPick(GUILD, '2026-10', first(10), 'gone', 0, first(10))
        )
        delivery = Delivery(post_key(gone), GUILD, ALGO)
        await publisher.publish([delivery], OutgoingMessage(title='Earlier'))

        result = await service.run_guild(GUILD, first(10))

        assert result == PostResult(gone, PublishOutcome.ALREADY_HANDLED, None)
        assert len(publisher.posts) == 1
        assert await pick_of(repo) == gone


class TestCycle:
    @pytest.mark.parametrize('seed', range(5))
    async def test_no_topic_repeats_until_the_catalog_cycles(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        seed: int,
    ) -> None:
        topics = tuple(made_up(number) for number in range(5))
        service = make_service(topics=topics, rng=random.Random(seed))
        await set_up(guild_settings)

        slugs = []
        for number in range(2 * len(topics)):  # October 2026 to July 2027
            year, month = divmod(9 + number, 12)
            result = await service.run_guild(GUILD, first(month + 1, 2026 + year))
            assert result.outcome is PublishOutcome.SENT
            assert result.pick is not None
            slugs.append(result.pick.slug)

        everything = sorted(found.slug for found in topics)
        assert sorted(slugs[:5]) == everything
        assert sorted(slugs[5:]) == everything
        assert keys(publisher)[-1] == key('2027-07')

    def test_the_cycle_rule(self, service: AlgoService) -> None:
        def remaining(*slugs: str) -> list[str]:
            return [found.slug for found in service.remaining(slugs)]

        assert remaining() == EVERY_SLUG
        assert remaining('topic-1') == ['topic-0', 'topic-2']
        assert remaining('topic-1', 'topic-0') == ['topic-2']
        # Every topic had: the cycle starts again, and is never empty.
        assert remaining('topic-1', 'topic-0', 'topic-2') == EVERY_SLUG
        assert remaining('topic-1', 'topic-0', 'topic-2', 'topic-2') == [
            'topic-0',
            'topic-1',
        ]
        # Had twice in a cycle, as when the catalog changed between, it counts once.
        assert remaining('topic-1', 'topic-1') == ['topic-0', 'topic-2']

    async def test_slugs_no_longer_in_the_catalog_are_ignored(
        self,
        service: AlgoService,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        def remaining(*slugs: str) -> list[str]:
            return [found.slug for found in service.remaining(slugs)]

        assert remaining('gone', 'topic-0', 'old', 'topic-1') == ['topic-2']
        assert remaining('topic-0', 'topic-1', 'gone', 'topic-2', 'gone') == EVERY_SLUG
        await set_up(guild_settings)
        august = AlgoPick(GUILD, '2026-08', first(8), 'gone', 0, first(8))
        await went_out(repo, publisher, august)
        september = AlgoPick(GUILD, '2026-09', first(9), 'topic-0', 0, first(9))
        await went_out(repo, publisher, september)
        recorded = Recorded()

        await make_service(rng=recorded).run_guild(GUILD, first(10))

        assert recorded.offered == [['topic-1', 'topic-2']]
        assert (await pick_of(repo)).slug == 'topic-1'

    async def test_the_rng_picks_among_the_topics_left(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        clock: FakeClock,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)

        await service.run_guild(GUILD, first(10))
        for month in (11, 12):
            await clock.advance_to(first(month))
            await service.run_guild(GUILD, first(month))
        await clock.advance_to(first(1, 2027))
        await service.run_guild(GUILD, first(1, 2027))

        assert recorded.offered == [
            EVERY_SLUG,
            ['topic-1', 'topic-2'],
            ['topic-2'],
            EVERY_SLUG,
        ]

    async def test_a_topic_counts_as_had_once_its_post_goes_out(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(6))  # topic 0, posted
        publisher.fail_next(PublishOutcome.SKIPPED)  # Discord refused it
        await service.run_guild(GUILD, first(7))  # topic 1
        publisher.undeliverable_next()
        await service.run_guild(GUILD, first(8))  # topic 1, never posted
        publisher.fail_next(PublishOutcome.PENDING)  # it may have gone out
        await service.run_guild(GUILD, first(9))  # topic 1

        await service.run_guild(GUILD, first(10))

        assert recorded.offered == [
            EVERY_SLUG,
            ['topic-1', 'topic-2'],
            ['topic-1', 'topic-2'],  # members never saw July's topic
            ['topic-1', 'topic-2'],  # nor August's
            ['topic-2'],  # September's may have gone out
        ]

    async def test_the_cycle_counts_the_topic_that_members_saw_in_a_month(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # topic 0, posted
        publisher.undeliverable_next()
        await service.reroll(GUILD)  # topic 1 in its place, never posted
        await clock.advance_to(first(11))

        await service.run_guild(GUILD, first(11))

        assert recorded.offered[-1] == ['topic-1', 'topic-2']

    def test_the_catalog_needs_topics_with_their_own_slugs(
        self, make_service: Callable[..., AlgoService]
    ) -> None:
        with pytest.raises(ValueError, match='at least one topic'):
            make_service(topics=())
        with pytest.raises(ValueError, match='unique'):
            make_service(topics=[made_up(0), replace(made_up(1), slug='topic-0')])

        service = make_service()

        assert service.topics == TOPICS
        assert service.topic('topic-1') is TOPICS[1]
        assert service.topic('gone') is None
        assert service.name('topic-1') == 'Topic 1'
        assert service.name('gone') == 'gone'


class TestRunSlot:
    async def test_every_server_with_the_feature_on_gets_its_topic_once(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await set_up(guild_settings, OTHER_GUILD)
        await set_up(guild_settings, 1_100_000_000_000_000_003, enabled=False)

        await service.run_slot(first(10))
        await service.run_slot(first(10))

        assert keys(publisher) == [
            key('2026-10'),
            key('2026-10', guild_id=OTHER_GUILD),
        ]

    async def test_an_undeliverable_post_raises_so_that_the_slot_is_retried(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await set_up(guild_settings, OTHER_GUILD)
        publisher.undeliverable_next()

        with pytest.raises(RuntimeError) as raised:
            await service.run_slot(first(10))

        assert str(raised.value) == (
            f'The algorithm of the month was not posted in guilds {GUILD}'
        )
        assert raised.value.__cause__ is None
        assert keys(publisher) == [key('2026-10', guild_id=OTHER_GUILD)]
        picked = await pick_of(repo)
        assert (
            f'The algorithm of the month of guild {GUILD} for its 2026-10-01 '
            '11:00:00+00:00 slot could not be delivered yet; the slot will be tried '
            'again'
        ) in logged(caplog)

        await service.run_slot(first(10))

        assert keys(publisher)[1:] == [key('2026-10')]
        assert await pick_of(repo) == picked

    async def test_a_bug_in_one_server_is_logged_with_its_traceback(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await set_up(guild_settings, GUILD)
        await set_up(guild_settings, OTHER_GUILD)
        run_guild = service.run_guild
        bug = ZeroDivisionError('a bug')

        async def failing(guild_id: int, slot: datetime) -> PostResult:
            if guild_id == GUILD:
                raise bug
            return await run_guild(guild_id, slot)

        monkeypatch.setattr(service, 'run_guild', failing)

        with pytest.raises(RuntimeError) as raised:
            await service.run_slot(first(10))

        assert raised.value.__cause__ is bug
        [record] = [r for r in caplog.records if r.name == LOGGER and r.exc_info]
        assert record.levelno == logging.INFO
        assert record.getMessage() == (
            f'Could not run the algorithm of the month of guild {GUILD} for its '
            '2026-10-01 11:00:00+00:00 slot'
        )
        assert keys(publisher) == [key('2026-10', guild_id=OTHER_GUILD)]

    async def test_a_server_the_bot_isnt_in_is_skipped(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The bot left OTHER_GUILD, whose settings stay: no admin there can
        # turn the feature off.
        await set_up(guild_settings, GUILD)
        await set_up(guild_settings, OTHER_GUILD)

        await service.run_slot(
            first(10), in_guild=lambda guild_id: guild_id != OTHER_GUILD
        )  # nothing to retry

        assert keys(publisher) == [key('2026-10')]
        assert await repo.history(OTHER_GUILD) == []
        assert (
            f'Skipping the algorithm of the month of guild {OTHER_GUILD} for its '
            '2026-10-01 11:00:00+00:00 slot: the bot is not in it'
        ) in logged(caplog)

    async def test_the_months_go_by_the_clubs_clock(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        # Noon in London on the 1st is 11:00 UTC in October, in summer time,
        # and 12:00 UTC in November.
        november = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
        assert (first(10), first(11)) == (
            datetime(2026, 10, 1, 11, 0, tzinfo=UTC),
            november,
        )
        await set_up(guild_settings)
        await service.run_slot(first(10))
        await clock.advance_to(november)

        await service.run_slot(november)

        october_post, november_post = publisher.posts
        assert october_post.deliveries[0].expires_at == november
        assert november_post.deliveries == (
            Delivery(
                key=key('2026-11'),
                guild_id=GUILD,
                feature=ALGO,
                subject='algo',
                subject_id='2026-11',
                kind='topic',
                occurrence_start=november,
                revision=0,
                expires_at=datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
            ),
        )
        # Half an hour before November's slot, the month is still October.
        before = await service.run_guild(GUILD, november - 30 * MINUTE)
        assert before.pick is not None and before.pick.month == '2026-10'
        assert before.outcome is PublishOutcome.ALREADY_HANDLED

    async def test_a_month_is_the_slots_month_in_club_time(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        # Noon on 1 November in Auckland is 23:00 UTC on 31 October.
        auckland = Monthly(POST_DAY, POST_TIME, zone('Pacific/Auckland'))
        slot = auckland.next_after(NOW)
        assert slot == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
        await set_up(guild_settings)

        result = await make_service(schedule=auckland).run_guild(GUILD, slot)

        assert result.pick is not None and result.pick.month == '2026-11'
        assert keys(publisher) == [key('2026-11')]


class TestReroll:
    async def test_a_reroll_changes_the_topic_and_posts_it_under_a_new_key(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))
        original = await pick_of(repo)
        await clock.advance(HOUR)

        result = await service.reroll(GUILD)

        rerolled = await pick_of(repo)
        assert rerolled == replace(
            original, slug='topic-1', revision=1, picked_at=NOW + HOUR
        )
        assert await repo.revisions(GUILD, '2026-10') == [rerolled, original]
        assert result == PostResult(
            rerolled, PublishOutcome.SENT, None, replaced=original
        )
        assert keys(publisher) == [key('2026-10'), key('2026-10', 1)]
        post = publisher.posts[1]
        assert post.deliveries[0] == Delivery(
            key=key('2026-10', 1),
            guild_id=GUILD,
            feature=ALGO,
            subject='algo',
            subject_id='2026-10',
            kind='topic',
            occurrence_start=first(10),
            revision=1,
            expires_at=first(11),
        )
        assert post.message.title == 'Algorithm of the month: Topic 1'
        assert post.message.mention_role
        assert lines(publisher) == [
            "This replaces October's earlier pick, **Topic 0**.",
            'What topic 1 is for.',
            '**Level:** intermediate',
            '**Read:** [GeeksforGeeks](https://www.geeksforgeeks.org/dsa/topic-1/)'
            ' · [cp-algorithms](https://cp-algorithms.com/topics/topic-1.html)',
        ]
        assert (
            f"Rerolled guild {GUILD}'s algorithm of the month for 2026-10: topic-1 "
            'in place of topic-0'
        ) in logged(caplog)

    async def test_a_second_reroll_works_and_may_bring_the_first_topic_back(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))

        await service.reroll(GUILD)
        result = await service.reroll(GUILD)

        # Each reroll leaves out the topic it replaces, and that topic only.
        assert recorded.offered == [
            EVERY_SLUG,
            ['topic-1', 'topic-2'],
            ['topic-0', 'topic-2'],
        ]
        assert result.outcome is PublishOutcome.SENT
        assert result.replaced is not None and result.replaced.slug == 'topic-1'
        assert (await pick_of(repo)).slug == 'topic-0'
        assert keys(publisher) == [
            key('2026-10'),
            key('2026-10', 1),
            key('2026-10', 2),
        ]
        assert lines(publisher)[0] == (
            "This replaces October's earlier pick, **Topic 1**."
        )

    async def test_a_topic_rerolled_away_counts_as_never_had(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        clock: FakeClock,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # topic-0
        await service.reroll(GUILD)  # topic-1 in its place
        await clock.advance_to(first(11))

        await service.run_guild(GUILD, first(11))

        assert recorded.offered[-1] == ['topic-0', 'topic-2']

    async def test_a_reroll_without_a_pick_posts_one_as_post_now_does(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        await set_up(guild_settings)

        result = await service.reroll(GUILD)

        pick = await pick_of(repo)
        assert result == PostResult(pick, PublishOutcome.SENT, None)
        assert pick.revision == 0
        assert keys(publisher) == [key('2026-10')]
        assert not lines(publisher)[0].startswith('This replaces')

    async def test_a_reroll_in_a_server_not_set_up_writes_nothing(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))
        picked = await pick_of(repo)
        await set_up(guild_settings, enabled=False)

        result = await service.reroll(GUILD)

        assert result == PostResult(None, PublishOutcome.NOT_CONFIGURED, 'disabled')
        assert await pick_of(repo) == picked
        assert len(publisher.posts) == 1

    async def test_a_reroll_with_no_other_topic_left_is_refused(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        service = make_service(topics=TOPICS[:2], rng=FirstChoice())
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # topic-0
        await clock.advance_to(first(11))
        await service.run_guild(GUILD, first(11))  # topic-1, the last one left
        november = await pick_of(repo, '2026-11')

        with pytest.raises(NoOtherTopic) as raised:
            await service.reroll(GUILD)

        assert isinstance(raised.value, KcpcUserError)
        assert str(raised.value) == (
            '**Topic 1** is the only topic this server has yet to have before the '
            'list starts over, so there is no other to reroll to.'
        )
        assert await pick_of(repo, '2026-11') == november
        assert len(publisher.posts) == 2

    @pytest.mark.parametrize('refused', [False, True], ids=['undelivered', 'refused'])
    async def test_a_reroll_of_a_topic_that_never_went_out_replaces_it_quietly(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        refused: bool,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        if refused:
            publisher.fail_next(PublishOutcome.SKIPPED)
        else:
            publisher.undeliverable_next()
        await service.run_guild(GUILD, first(10))
        original = await pick_of(repo)

        result = await service.reroll(GUILD)

        assert result.outcome is PublishOutcome.SENT
        assert result.replaced == original
        assert keys(publisher) == [key('2026-10', 1)]
        # Members never saw the topic it replaces.
        assert lines(publisher)[0] == 'What topic 1 is for.'

    async def test_a_reroll_waits_until_the_last_post_is_confirmed(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        ledger: DeliveryLedger,
    ) -> None:
        # Were it rerolled now, the reconciler could still post the topic it
        # replaced after the new one.
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        publisher.fail_next(PublishOutcome.PENDING)
        await service.run_guild(GUILD, first(10))
        unconfirmed = await pick_of(repo)

        with pytest.raises(KcpcUserError) as raised:
            await service.reroll(GUILD)

        assert str(raised.value) == (
            "The last post of October's topic, **Topic 0**, isn't confirmed yet. "
            'Please try again in a few minutes.'
        )
        assert await repo.revisions(GUILD, '2026-10') == [unconfirmed]
        assert publisher.posts == []

        # The reconciler found the post.
        record = await ledger.get(post_key(unconfirmed))
        assert record is not None
        await ledger.confirm(record.batch, 1)

        result = await service.reroll(GUILD)

        assert result.outcome is PublishOutcome.SENT
        assert keys(publisher) == [key('2026-10', 1)]

    @pytest.mark.parametrize(
        'post_later',
        [
            lambda service: service.run_guild(GUILD, first(10)),
            lambda service: service.run_slot(first(10)),
        ],
        ids=['post-now', 'the job'],
    )
    async def test_a_rerolled_topic_posted_later_says_which_topic_it_replaces(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        post_later: Callable[[AlgoService], Awaitable[object]],
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # members see topic 0
        publisher.undeliverable_next()
        failed = await service.reroll(GUILD)  # topic 1, not posted
        assert failed.outcome is PublishOutcome.UNDELIVERABLE

        await post_later(service)

        assert keys(publisher) == [key('2026-10'), key('2026-10', 1)]
        assert publisher.posts[-1].message.title == 'Algorithm of the month: Topic 1'
        assert lines(publisher)[0] == (
            "This replaces October's earlier pick, **Topic 0**."
        )

    async def test_a_post_names_the_last_topic_that_went_out_before_it(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # topic 0, posted
        await service.reroll(GUILD)  # topic 1, posted
        publisher.undeliverable_next()
        await service.reroll(GUILD)  # topic 0 again, not posted

        await service.run_guild(GUILD, first(10))

        assert keys(publisher)[-1] == key('2026-10', 2)
        assert lines(publisher)[0] == (
            "This replaces October's earlier pick, **Topic 1**."
        )

    async def test_a_reroll_after_one_that_failed_leaves_out_the_topic_shown(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))  # topic 0, posted
        publisher.undeliverable_next()
        await service.reroll(GUILD)  # topic 1, not posted

        result = await service.reroll(GUILD)

        # Neither the topic it replaces nor the one that members were shown.
        assert recorded.offered[-1] == ['topic-2']
        assert result.outcome is PublishOutcome.SENT
        assert result.replaced is not None and result.replaced.slug == 'topic-1'
        assert lines(publisher)[0] == (
            "This replaces October's earlier pick, **Topic 0**."
        )

    async def test_a_reroll_counts_only_the_earlier_topics_that_went_out(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        repo: AlgoRepo,
    ) -> None:
        recorded = Recorded()
        service = make_service(rng=recorded)
        await set_up(guild_settings)
        # September's topic, whose post never went out.
        await repo.create(AlgoPick(GUILD, '2026-09', first(9), 'topic-1', 0, first(9)))
        await service.run_guild(GUILD, first(10))  # topic 0

        await service.reroll(GUILD)

        assert recorded.offered == [EVERY_SLUG, ['topic-1', 'topic-2']]

    async def test_the_reroll_post_names_the_month_in_club_time(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        # Noon on 1 November in Auckland is 23:00 UTC on 31 October.
        auckland = Monthly(POST_DAY, POST_TIME, zone('Pacific/Auckland'))
        service = make_service(rng=FirstChoice(), schedule=auckland)
        await set_up(guild_settings)
        await clock.advance_to(datetime(2026, 10, 31, 23, 30, tzinfo=UTC))
        await service.run_guild(GUILD, clock.now())

        await service.reroll(GUILD)

        assert keys(publisher) == [key('2026-11'), key('2026-11', 1)]
        assert lines(publisher)[0] == (
            "This replaces November's earlier pick, **Topic 0**."
        )

    async def test_a_reroll_takes_turns_with_a_run(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        posting = asyncio.Event()
        go_on = asyncio.Event()
        publish = publisher.publish

        async def slow(deliveries: Sequence[Delivery], message: OutgoingMessage) -> Any:
            if deliveries[0].revision == 0:
                posting.set()
                await go_on.wait()
            return await publish(deliveries, message)

        monkeypatch.setattr(publisher, 'publish', slow)
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        run = asyncio.create_task(service.run_guild(GUILD, first(10)))
        await posting.wait()

        reroll = asyncio.create_task(service.reroll(GUILD))
        await settle()

        assert not reroll.done()
        assert (await pick_of(repo)).revision == 0
        go_on.set()
        await run
        result = await reroll
        assert keys(publisher) == [key('2026-10'), key('2026-10', 1)]
        assert result.replaced is not None and result.replaced.slug == 'topic-0'

    async def test_a_reroll_is_for_the_month_of_the_latest_slot(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))
        # Half an hour before November's slot.
        await clock.advance_to(datetime(2026, 11, 1, 11, 30, tzinfo=UTC))

        await service.reroll(GUILD)

        assert (await pick_of(repo)).revision == 1
        assert await repo.get(GUILD, '2026-11') is None


class TestReading:
    async def test_current_and_history_are_the_picks_that_went_out(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        assert await service.current(GUILD) is None
        assert await service.history(GUILD) == []
        await service.run_guild(GUILD, first(8))
        await service.run_guild(GUILD, first(9))
        publisher.fail_next(PublishOutcome.SKIPPED)  # Discord refused it
        await service.run_guild(GUILD, first(10))
        august, september, october = [
            await pick_of(repo, f'2026-{month:02d}') for month in (8, 9, 10)
        ]

        assert await service.history(GUILD) == [september, august]
        assert await service.current(GUILD) is None  # this month's isn't out
        assert not await service.posted(october)

        await clock.advance_to(first(11))
        publisher.fail_next(PublishOutcome.PENDING)  # it may have gone out
        await service.run_guild(GUILD, first(11))

        november = await pick_of(repo, '2026-11')
        assert await service.posted(november)
        assert await service.current(GUILD) == november
        assert await service.history(GUILD) == [november, september, august]
        assert await service.history(OTHER_GUILD) == []

    async def test_current_and_history_follow_a_reroll(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
    ) -> None:
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))

        await service.reroll(GUILD)

        rerolled = await pick_of(repo)
        assert await service.current(GUILD) == rerolled
        assert await service.history(GUILD) == [rerolled]

        # Until a new topic goes out, members are shown the last one that did.
        publisher.undeliverable_next()
        await service.reroll(GUILD)

        assert (await pick_of(repo)).revision == 2
        assert await service.current(GUILD) == rerolled
        assert await service.history(GUILD) == [rerolled]

        await service.run_guild(GUILD, first(10))

        newest = await pick_of(repo)
        assert await service.current(GUILD) == newest
        assert await service.history(GUILD) == [newest]

    async def test_a_month_that_members_saw_stays_in_the_history(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))
        october = await pick_of(repo)
        publisher.fail_next(PublishOutcome.SKIPPED)
        await service.reroll(GUILD)  # Discord refused the new topic
        await clock.advance_to(first(11))
        await service.run_guild(GUILD, first(11))
        november = await pick_of(repo, '2026-11')

        assert (await pick_of(repo)).revision == 1
        assert await service.history(GUILD) == [november, october]

    async def test_current_is_the_topic_of_the_latest_slot(
        self,
        service: AlgoService,
        guild_settings: GuildSettingsRepo,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        await service.run_guild(GUILD, first(10))
        october = await pick_of(repo)

        await clock.advance_to(datetime(2026, 11, 1, 11, 59, tzinfo=UTC))
        assert await service.current(GUILD) == october

        await clock.advance_to(first(11))  # November's isn't out yet
        assert await service.current(GUILD) is None

    async def test_upcoming_is_what_the_next_pick_chooses_from(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        assert await service.upcoming(GUILD) == list(TOPICS)

        await service.run_guild(GUILD, first(10))

        assert await service.upcoming(GUILD) == list(TOPICS[1:])
        assert await service.upcoming(OTHER_GUILD) == list(TOPICS)

    async def test_upcoming_counts_this_months_topic_before_it_goes_out(
        self,
        make_service: Callable[..., AlgoService],
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        clock: FakeClock,
    ) -> None:
        service = make_service(rng=FirstChoice())
        await set_up(guild_settings)
        publisher.undeliverable_next()
        await service.run_guild(GUILD, first(10))  # topic 0, still to post

        # A retry posts it, so the next pick is one of the others.
        assert await service.upcoming(GUILD) == list(TOPICS[1:])

        # October is over and its topic never went out, so it is not had.
        await clock.advance_to(first(11))

        assert await service.upcoming(GUILD) == list(TOPICS)


class TestTheJob:
    """The job as the cog adds it: persistent, with 24 hours' grace."""

    @pytest.fixture
    async def scheduler(
        self, db: Database, clock: FakeClock, service: AlgoService
    ) -> AsyncIterator[Scheduler]:
        scheduler = Scheduler(db, clock)
        scheduler.add(
            ScheduledJob(
                ALGO_JOB,
                SCHEDULE,
                service.run_slot,
                catch_up_grace=timedelta(hours=24),
            )
        )
        yield scheduler
        await asyncio.wait_for(scheduler.stop(), STOP_TIMEOUT)

    async def test_the_first_start_posts_nothing_until_the_next_slot(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        db: Database,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)

        scheduler.start()

        await parked(scheduler, first(11))
        assert publisher.posts == []
        assert await last_slot(db) == first(10)

        await clock.advance_to(first(11))
        await parked(scheduler, first(12))
        assert keys(publisher) == [key('2026-11')]

    async def test_a_slot_missed_by_less_than_a_day_is_caught_up(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        scheduler.start()
        await parked(scheduler, first(11))
        await scheduler.stop()

        late = first(11) + 24 * HOUR - SECOND
        await clock.advance_to(late)  # the bot was down at noon
        scheduler.start()

        await parked(scheduler, first(12))
        assert keys(publisher) == [key('2026-11')]
        assert (await pick_of(repo, '2026-11')).picked_at == late

    async def test_a_slot_missed_by_more_than_a_day_is_skipped(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        db: Database,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        scheduler.start()
        await parked(scheduler, first(11))
        await scheduler.stop()

        await clock.advance_to(first(11) + 24 * HOUR + SECOND)
        scheduler.start()

        await parked(scheduler, first(12))
        assert publisher.posts == []
        assert await repo.get(GUILD, '2026-11') is None
        assert await last_slot(db) == first(11)

    async def test_an_undeliverable_post_is_retried_within_the_grace(
        self,
        scheduler: Scheduler,
        guild_settings: GuildSettingsRepo,
        publisher: FakePublisher,
        repo: AlgoRepo,
        clock: FakeClock,
    ) -> None:
        await set_up(guild_settings)
        publisher.undeliverable_next()
        scheduler.start()
        await parked(scheduler, first(11))

        await clock.advance_to(first(11))

        await parked(scheduler, first(11) + 5 * MINUTE)
        assert publisher.posts == []
        assert job_status(scheduler).failures == 1
        picked = await pick_of(repo, '2026-11')

        await clock.advance_to(first(11) + 5 * MINUTE)

        await parked(scheduler, first(12))
        assert keys(publisher) == [key('2026-11')]
        assert await pick_of(repo, '2026-11') == picked
        assert job_status(scheduler).failures == 0


def job_status(scheduler: Scheduler) -> JobStatus:
    [status] = scheduler.status()
    return status


async def parked(scheduler: Scheduler, until: datetime) -> None:
    """Wait (up to 10 s of real time) until the job waits to run at ``until``."""
    for _ in range(2000):
        if job_status(scheduler).next_run == until:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until the job waits for {until}')


async def last_slot(db: Database) -> datetime | None:
    seconds = await db.fetchval(
        'SELECT last_slot FROM job_state WHERE job = ?', (ALGO_JOB,)
    )
    return None if seconds is None else from_epoch(seconds)
