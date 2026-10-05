"""Tests for tle.kcpc.core.reminders.ReminderEngine, on a real kcpc.db.

A fake source lists the test's occurrences, and ``FakePublisher`` posts by
recording, through the real ledger, so these tests follow what members would
see as time passes on a FakeClock. The last tests check FakePublisher itself,
since other tests rely on it behaving as DiscordPublisher does.
"""

import asyncio
import logging
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import pytest

from tests.kcpc.fakes import FIRST_MESSAGE_ID, FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import (
    Delivery,
    DeliveryLedger,
    DeliveryRecord,
    DeliveryStatus,
)
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.reminders import (
    Notice,
    NoticeKind,
    Occurrence,
    ReminderEngine,
    ReminderPolicy,
    TickReport,
)
from tle.kcpc.core.settings import FeatureSettings, GuildSettingsRepo

CLAIMED = DeliveryStatus.CLAIMED
SENT = DeliveryStatus.SENT
SKIPPED = DeliveryStatus.SKIPPED

# Real snowflakes are 64-bit, beyond SQLite's REAL precision, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
OTHER_CHANNEL = 1_200_000_000_000_000_002

WORKSHOPS = 'workshops'
CONTESTS = 'contests'
REMINDERS_LOGGER = 'tle.kcpc.core.reminders'

SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)

S = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)  # two days after the clock starts
DEFAULT = ReminderPolicy(offsets=(DAY, HOUR))


def workshop(subject_id: str = 'evt-1', *, start: datetime = S) -> Occurrence:
    return Occurrence(
        subject='event',
        subject_id=subject_id,
        title=f'Workshop {subject_id}',
        start=start,
        end=start + 2 * HOUR,
        url=f'https://luma.com/{subject_id}',
        revision=0,
    )


class Call(NamedTuple):
    """One call of ``FakeSource.occurrences``."""

    guild_id: int
    settings: FeatureSettings
    start: datetime
    end: datetime


class FakeSource:
    """A ReminderSource listing the test's occurrences, the same in every guild.

    ``change`` edits an occurrence as a real source would, bumping its
    revision. It renders each notice as a message titled after it, and keeps
    what it rendered. It notes the settings each guild's policy was asked for,
    and every call of ``occurrences``, which raises ``error`` for the guilds in
    ``failing_guilds``.
    """

    def __init__(
        self, feature: str = WORKSHOPS, policy: ReminderPolicy = DEFAULT
    ) -> None:
        self.feature = feature
        self.items: list[Occurrence] = []
        self.rendered: list[Notice] = []
        self.policies_for: list[FeatureSettings] = []
        self.calls: list[Call] = []
        self.failing_guilds: set[int] = set()
        self.error: type[Exception] = RuntimeError
        self._policy = policy

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        self.policies_for.append(settings)
        return self._policy

    async def occurrences(
        self, guild_id: int, settings: FeatureSettings, start: datetime, end: datetime
    ) -> list[Occurrence]:
        self.calls.append(Call(guild_id, settings, start, end))
        if guild_id in self.failing_guilds:
            raise self.error(f'no occurrences for guild {guild_id}')
        return [item for item in self.items if start <= item.start < end]

    def render(self, notice: Notice) -> OutgoingMessage:
        self.rendered.append(notice)
        titles = ', '.join(item.title for item in notice.occurrences)
        return OutgoingMessage(title=f'{notice.kind.value}: {titles}')

    def change(self, subject_id: str, **changes: Any) -> None:
        (old,) = [item for item in self.items if item.subject_id == subject_id]
        new = replace(old, revision=old.revision + 1, **changes)
        self.items[self.items.index(old)] = new


class OutcomePublisher:
    """A publisher whose every post has one outcome, recording nothing."""

    def __init__(self, outcome: PublishOutcome) -> None:
        self._outcome = outcome

    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        return PublishResult(self._outcome)


@pytest.fixture
def publisher(
    guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> FakePublisher:
    return FakePublisher(guild_settings, ledger)


@pytest.fixture
def engine(
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    publisher: FakePublisher,
    clock: FakeClock,
) -> ReminderEngine:
    return ReminderEngine(guild_settings, ledger, publisher, clock)


async def set_up(
    guild_settings: GuildSettingsRepo,
    guild_id: int = GUILD,
    feature: str = WORKSHOPS,
    *,
    channel_id: int | None = CHANNEL,
    enabled: bool = True,
) -> None:
    await guild_settings.update(
        guild_id, feature, enabled=enabled, channel_id=channel_id
    )


@pytest.fixture
async def source(
    engine: ReminderEngine, guild_settings: GuildSettingsRepo
) -> FakeSource:
    """A registered workshops source with evt-1 at S, for a guild set up for it."""
    await set_up(guild_settings)
    source = FakeSource()
    source.items.append(workshop())
    engine.register(source)
    return source


async def tick_at(
    clock: FakeClock, engine: ReminderEngine, when: datetime
) -> TickReport:
    await clock.advance_to(when)
    return await engine.tick()


def posted(publisher: FakePublisher) -> list[str]:
    """Each post as its deliveries, '<id> <kind> r<revision>', joined by '+'."""
    return [
        '+'.join(
            f'{item.subject_id} {item.kind} r{item.revision}'
            for item in post.deliveries
        )
        for post in publisher.posts
    ]


def delivery(key: str, *, guild_id: int = GUILD, feature: str = WORKSHOPS) -> Delivery:
    return Delivery(key, guild_id, feature)


def reminder_key(kind: str, revision: int = 0, *, guild_id: int = GUILD) -> str:
    return f'remind:{guild_id}:event:evt-1:{kind}:r{revision}'


async def get(ledger: DeliveryLedger, key: str) -> DeliveryRecord:
    found = await ledger.get(key)
    assert found is not None
    return found


async def ledger_is_empty(ledger: DeliveryLedger, guild_id: int) -> bool:
    return await ledger.status_counts(guild_id) == {CLAIMED: 0, SENT: 0, SKIPPED: 0}


async def test_concurrent_ticks_run_one_after_the_other_and_post_once(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await clock.advance_to(S - 23 * HOUR)
    entered, release = asyncio.Event(), asyncio.Event()
    occurrences = source.occurrences

    async def gated(*args: Any) -> list[Occurrence]:
        entered.set()
        await release.wait()
        return await occurrences(*args)

    monkeypatch.setattr(source, 'occurrences', gated)
    first = asyncio.create_task(engine.tick())
    await asyncio.wait_for(entered.wait(), timeout=5)
    entered.clear()
    second = asyncio.create_task(engine.tick())
    await clock.settle()

    assert not entered.is_set()  # the second waits for the first to finish
    release.set()
    reports = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    # The second tick found the post in the ledger, so it didn't even try it.
    assert reports == [TickReport(sent=1), TickReport()]
    assert posted(publisher) == ['evt-1 1440m r0']


async def test_a_tick_finds_nothing_new_to_post_after_one_that_posted(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=1)

    assert await engine.tick() == TickReport()
    assert await tick_at(clock, engine, S - 22 * HOUR) == TickReport()
    assert posted(publisher) == ['evt-1 1440m r0']


async def test_after_an_outage_across_the_24h_mark_only_the_1h_reminder_goes_out(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert await tick_at(clock, engine, S - 25 * HOUR) == TickReport()

    # The bot was down from then until 50 minutes before the start.
    with caplog.at_level(logging.INFO, logger=REMINDERS_LOGGER):
        report = await tick_at(clock, engine, S - 50 * MINUTE)

    assert report == TickReport(sent=1, skipped=1)
    assert posted(publisher) == ['evt-1 60m r0']
    skipped = await get(ledger, reminder_key('1440m'))
    assert (skipped.status, skipped.reason) == (SKIPPED, 'superseded')
    assert [r.getMessage() for r in caplog.records if r.name == REMINDERS_LOGGER] == [
        f'Skipped KCPC workshops {reminder_key("1440m")} in guild {GUILD}: superseded'
    ]
    for when in (S - 49 * MINUTE, S - MINUTE):
        assert await tick_at(clock, engine, when) == TickReport()


async def test_after_an_outage_ending_just_before_the_1h_reminder_only_it_goes_out(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
) -> None:
    assert await tick_at(clock, engine, S - 25 * HOUR) == TickReport()

    assert await tick_at(clock, engine, S - 75 * MINUTE) == TickReport(skipped=1)
    assert await tick_at(clock, engine, S - HOUR) == TickReport(sent=1)

    assert posted(publisher) == ['evt-1 60m r0']
    assert (await get(ledger, reminder_key('1440m'))).reason == 'late'


async def test_an_occurrence_found_80_minutes_before_it_starts_gets_only_the_1h_one(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
) -> None:
    source.items.clear()
    assert await tick_at(clock, engine, S - 2 * HOUR) == TickReport()

    source.items.append(workshop())
    assert await tick_at(clock, engine, S - 80 * MINUTE) == TickReport(skipped=1)
    assert posted(publisher) == []
    assert await tick_at(clock, engine, S - HOUR) == TickReport(sent=1)

    assert posted(publisher) == ['evt-1 60m r0']
    assert (await get(ledger, reminder_key('1440m'))).reason == 'late'


async def test_the_start_is_announced_once_within_its_window(
    clock: FakeClock,
    engine: ReminderEngine,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    source = FakeSource(policy=ReminderPolicy(offsets=(HOUR,), announce_start=True))
    source.items.append(workshop())
    engine.register(source)
    await set_up(guild_settings, channel_id=None)

    # Without a channel nothing is recorded, so the announcement goes out once
    # there is one, while it is still news: the occurrence is still listed.
    assert await tick_at(clock, engine, S) == TickReport(not_configured=1)
    await set_up(guild_settings)
    late = S + 10 * MINUTE - SECOND
    assert await tick_at(clock, engine, late) == TickReport(sent=1)
    assert await tick_at(clock, engine, S + 10 * MINUTE) == TickReport()

    assert posted(publisher) == ['evt-1 start r0']
    assert source.calls[-2].start == late - 10 * MINUTE


async def test_a_start_postponed_10_minutes_after_its_1h_reminder_gets_one_notice(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await tick_at(clock, engine, S - DAY)
    await tick_at(clock, engine, S - HOUR)

    source.change('evt-1', start=S + 10 * MINUTE)
    assert await tick_at(clock, engine, S - 50 * MINUTE) == TickReport(sent=1)
    for when in (S - 49 * MINUTE, S - MINUTE, S + 9 * MINUTE):
        assert await tick_at(clock, engine, when) == TickReport()

    assert posted(publisher) == ['evt-1 1440m r0', 'evt-1 60m r0', 'evt-1 moved r1']
    moved = source.rendered[-1]
    assert (moved.kind, moved.previous_start) == (NoticeKind.MOVED, S)
    assert moved.occurrences[0].start == S + 10 * MINUTE


async def test_a_move_by_a_week_after_its_24h_reminder_gets_a_notice_and_new_ones(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await tick_at(clock, engine, S - DAY)

    source.change('evt-1', start=S + 7 * DAY)
    assert await tick_at(clock, engine, S - 20 * HOUR) == TickReport(sent=1)
    assert await tick_at(clock, engine, S - HOUR) == TickReport()
    assert await tick_at(clock, engine, S + 6 * DAY) == TickReport(sent=1)
    assert await tick_at(clock, engine, S + 7 * DAY - HOUR) == TickReport(sent=1)

    assert posted(publisher) == [
        'evt-1 1440m r0',
        'evt-1 moved r1',
        'evt-1 1440m r1',
        'evt-1 60m r1',
    ]
    assert source.rendered[1].previous_start == S


async def test_each_move_is_announced_from_the_start_members_were_last_told(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await tick_at(clock, engine, S - DAY)

    source.change('evt-1', start=S + 2 * HOUR)
    assert await tick_at(clock, engine, S - 20 * HOUR) == TickReport(sent=1)
    # Back to the start of the 24h reminder: news after the first notice.
    source.change('evt-1', start=S)
    assert await tick_at(clock, engine, S - 19 * HOUR) == TickReport(sent=1)
    assert await tick_at(clock, engine, S - 18 * HOUR) == TickReport()
    assert await tick_at(clock, engine, S - HOUR) == TickReport(sent=1)

    assert posted(publisher) == [
        'evt-1 1440m r0',
        'evt-1 moved r1',
        'evt-1 moved r2',
        'evt-1 60m r2',
    ]
    assert [notice.previous_start for notice in source.rendered[1:3]] == [
        S,
        S + 2 * HOUR,
    ]


async def test_a_cancellation_and_reinstatement_after_a_reminder_get_a_notice_each(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await tick_at(clock, engine, S - DAY)

    source.change('evt-1', cancelled=True)
    assert await tick_at(clock, engine, S - 10 * HOUR) == TickReport(sent=1)
    assert await tick_at(clock, engine, S - 9 * HOUR) == TickReport()
    source.change('evt-1', cancelled=False)
    assert await tick_at(clock, engine, S - 5 * HOUR) == TickReport(sent=1)
    assert await tick_at(clock, engine, S - 4 * HOUR) == TickReport()
    assert await tick_at(clock, engine, S - HOUR) == TickReport(sent=1)

    assert posted(publisher) == [
        'evt-1 1440m r0',
        'evt-1 cancelled r1',
        'evt-1 reinstated r2',
        'evt-1 60m r2',
    ]


async def test_a_return_at_a_new_time_gets_one_notice_and_fresh_reminders(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    await tick_at(clock, engine, S - DAY)
    source.change('evt-1', cancelled=True)
    await tick_at(clock, engine, S - 20 * HOUR)

    # Back, two days later: the notice that it is back gives the new time, so
    # no notice of a time change follows it.
    source.change('evt-1', cancelled=False, start=S + 2 * DAY)
    assert await tick_at(clock, engine, S - 19 * HOUR) == TickReport(sent=1)
    for when in (S - 18 * HOUR, S - HOUR, S + DAY - MINUTE):
        assert await tick_at(clock, engine, when) == TickReport()
    assert await tick_at(clock, engine, S + DAY) == TickReport(sent=1)
    assert await tick_at(clock, engine, S + 2 * DAY - HOUR) == TickReport(sent=1)

    assert posted(publisher) == [
        'evt-1 1440m r0',
        'evt-1 cancelled r1',
        'evt-1 reinstated r2',
        'evt-1 1440m r2',
        'evt-1 60m r2',
    ]


async def test_a_guild_that_missed_a_return_hears_nothing_of_a_new_cancellation(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    await tick_at(clock, engine, S - DAY)
    source.change('evt-1', cancelled=True)
    await tick_at(clock, engine, S - 20 * HOUR)

    # The return can't be announced without a channel, and it is cancelled
    # again before there is one: members still know it as cancelled.
    await set_up(guild_settings, channel_id=None)
    source.change('evt-1', cancelled=False)
    assert await tick_at(clock, engine, S - 19 * HOUR) == TickReport(not_configured=1)
    source.change('evt-1', cancelled=True)
    await set_up(guild_settings)
    assert await tick_at(clock, engine, S - 18 * HOUR) == TickReport()

    assert posted(publisher) == ['evt-1 1440m r0', 'evt-1 cancelled r1']


async def test_a_cancellation_before_any_reminder_goes_unannounced(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
) -> None:
    source.change('evt-1', cancelled=True)
    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport()
    assert await ledger_is_empty(ledger, GUILD)

    source.change('evt-1', cancelled=False)
    assert await tick_at(clock, engine, S - 22 * HOUR) == TickReport(sent=1)
    assert posted(publisher) == ['evt-1 1440m r2']


async def test_disabled_guilds_get_nothing_and_leave_no_rows(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> None:
    await set_up(guild_settings, OTHER_GUILD, channel_id=OTHER_CHANNEL, enabled=False)

    assert await tick_at(clock, engine, S - HOUR) == TickReport(sent=1, skipped=1)

    assert [call.guild_id for call in source.calls] == [GUILD]
    assert await ledger_is_empty(ledger, OTHER_GUILD)


async def test_a_guild_without_a_channel_records_nothing_and_is_retried(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> None:
    await set_up(guild_settings, OTHER_GUILD, channel_id=None)

    report = await tick_at(clock, engine, S - 23 * HOUR)
    assert report == TickReport(sent=1, not_configured=1)
    assert await ledger_is_empty(ledger, OTHER_GUILD)
    report = await tick_at(clock, engine, S - 22 * HOUR)
    assert report == TickReport(not_configured=1)

    await set_up(guild_settings, OTHER_GUILD, channel_id=OTHER_CHANNEL)
    assert await tick_at(clock, engine, S - 21 * HOUR) == TickReport(sent=1)

    assert [post.keys for post in publisher.posts] == [
        (reminder_key('1440m'),),
        (reminder_key('1440m', guild_id=OTHER_GUILD),),
    ]


async def test_each_guild_gets_its_own_posts_planned_from_its_own_settings(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> None:
    await set_up(guild_settings, OTHER_GUILD, channel_id=OTHER_CHANNEL)

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=2)

    keys = [reminder_key('1440m'), reminder_key('1440m', guild_id=OTHER_GUILD)]
    assert [post.keys for post in publisher.posts] == [(key,) for key in keys]
    assert [(await get(ledger, key)).channel_id for key in keys] == [
        CHANNEL,
        OTHER_CHANNEL,
    ]
    settings = FeatureSettings(enabled=True, channel_id=CHANNEL)
    other_settings = FeatureSettings(enabled=True, channel_id=OTHER_CHANNEL)
    assert source.policies_for == [settings, other_settings]
    assert [(call.guild_id, call.settings) for call in source.calls] == [
        (GUILD, settings),
        (OTHER_GUILD, other_settings),
    ]


async def test_occurrences_with_the_same_start_get_one_reminder_together(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
) -> None:
    source.items.append(workshop('evt-2'))

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=1)

    assert posted(publisher) == ['evt-1 1440m r0+evt-2 1440m r0']
    (notice,) = source.rendered
    assert [item.subject_id for item in notice.occurrences] == ['evt-1', 'evt-2']
    (post,) = publisher.posts
    records = [await get(ledger, key) for key in post.keys]
    assert {(record.status, record.message_id) for record in records} == {
        (SENT, post.message_id)
    }


async def test_a_restarted_engine_does_not_repeat_what_was_posted(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> None:
    await tick_at(clock, engine, S - HOUR)
    restarted = ReminderEngine(guild_settings, ledger, publisher, clock)
    restarted.register(source)

    assert await restarted.tick() == TickReport()
    assert await tick_at(clock, restarted, S - 30 * MINUTE) == TickReport()
    assert posted(publisher) == ['evt-1 60m r0']


async def test_one_failing_guild_or_source_does_not_stop_the_others(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    contests = FakeSource(CONTESTS)
    contests.items.append(replace(workshop('cf-1'), subject='contest'))
    engine.register(contests)
    for guild_id, channel_id in ((GUILD, CHANNEL), (OTHER_GUILD, OTHER_CHANNEL)):
        for feature in (WORKSHOPS, CONTESTS):
            await set_up(guild_settings, guild_id, feature, channel_id=channel_id)
    source.failing_guilds.add(GUILD)

    report = await tick_at(clock, engine, S - 23 * HOUR)

    assert report == TickReport(sent=3, failed=1)
    # Features go in order, and each feature's guilds by id.
    assert [
        (post.deliveries[0].feature, post.deliveries[0].guild_id)
        for post in publisher.posts
    ] == [
        (CONTESTS, GUILD),
        (CONTESTS, OTHER_GUILD),
        (WORKSHOPS, OTHER_GUILD),
    ]

    source.failing_guilds.clear()
    assert await engine.tick() == TickReport(sent=1)


async def test_sources_tick_in_feature_order_and_their_guilds_in_id_order(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    # Registered after workshops: neither in feature order nor in reverse.
    for feature in ('algo', CONTESTS):
        extra = FakeSource(feature)
        # Each feature has subjects of its own, as delivery keys require.
        extra.items.append(replace(workshop(), subject=feature))
        engine.register(extra)
    for feature in (WORKSHOPS, 'algo', CONTESTS):
        await set_up(guild_settings, OTHER_GUILD, feature, channel_id=OTHER_CHANNEL)
        await set_up(guild_settings, GUILD, feature)

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=6)

    assert [
        (post.deliveries[0].feature, post.deliveries[0].guild_id)
        for post in publisher.posts
    ] == [
        (feature, guild_id)
        for feature in ('algo', CONTESTS, WORKSHOPS)
        for guild_id in (GUILD, OTHER_GUILD)
    ]


async def test_a_source_whose_guilds_cannot_be_listed_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
) -> None:
    engine.register(FakeSource(CONTESTS))  # ticked first
    enabled_guilds = guild_settings.enabled_guilds

    async def failing_for_contests(feature: str) -> list[tuple[int, FeatureSettings]]:
        if feature == CONTESTS:
            raise sqlite3.OperationalError('database is locked')
        return await enabled_guilds(feature)

    monkeypatch.setattr(guild_settings, 'enabled_guilds', failing_for_contests)

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=1, failed=1)
    assert posted(publisher) == ['evt-1 1440m r0']


def test_a_feature_takes_one_source(engine: ReminderEngine) -> None:
    first = FakeSource()
    engine.register(first)

    with pytest.raises(ValueError, match="for 'workshops' is already registered"):
        engine.register(FakeSource())
    with pytest.raises(ValueError, match='already registered'):
        engine.register(first)

    assert engine.features == [WORKSHOPS]


def test_a_source_for_an_unknown_feature_is_refused(engine: ReminderEngine) -> None:
    # No guild could ever enable it, so its reminders would never go out.
    with pytest.raises(ValueError, match="Unknown feature 'nonsense'"):
        engine.register(FakeSource('nonsense'))

    assert engine.features == []


def test_features_lists_the_registered_sources_in_order(engine: ReminderEngine) -> None:
    assert engine.features == []

    engine.register(FakeSource(WORKSHOPS))
    engine.register(FakeSource(CONTESTS))

    assert engine.features == [CONTESTS, WORKSHOPS]


async def test_an_unregistered_source_gets_no_more_posts(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
) -> None:
    engine.unregister(WORKSHOPS)
    engine.unregister('never-registered')  # ignored

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport()
    assert engine.features == []
    assert source.calls == []
    assert publisher.posts == []

    engine.register(source)  # and it can come back, as a reloaded cog's would
    assert await engine.tick() == TickReport(sent=1)


async def test_occurrences_are_asked_for_from_start_window_ago_to_the_horizon(
    clock: FakeClock, engine: ReminderEngine, guild_settings: GuildSettingsRepo
) -> None:
    await set_up(guild_settings)
    policy = ReminderPolicy(offsets=(HOUR,), start_window=5 * MINUTE, horizon=30 * DAY)
    source = FakeSource(policy=policy)
    engine.register(source)
    now = clock.now()

    await engine.tick()

    assert [(call.start, call.end) for call in source.calls] == [
        (now - 5 * MINUTE, now + 30 * DAY)
    ]


async def test_each_subjects_history_is_read_once_and_kept_apart(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
) -> None:
    # A contest with the same id as the workshop must not count as reminded.
    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=1)
    source.items.append(replace(workshop(), subject='contest', start=S + MINUTE))
    history_for = ledger.history_for
    reads: list[tuple[int, str, list[str]]] = []

    async def reading(
        guild_id: int, subject: str, subject_ids: Collection[str]
    ) -> dict[str, list[DeliveryRecord]]:
        reads.append((guild_id, subject, list(subject_ids)))
        return await history_for(guild_id, subject, subject_ids)

    monkeypatch.setattr(ledger, 'history_for', reading)

    assert await engine.tick() == TickReport(sent=1)

    assert sorted(reads) == [(GUILD, 'contest', ['evt-1']), (GUILD, 'event', ['evt-1'])]
    assert [post.keys for post in publisher.posts] == [
        (reminder_key('1440m'),),
        (f'remind:{GUILD}:contest:evt-1:1440m:r0',),
    ]


async def test_a_guild_without_occurrences_reads_no_history(
    monkeypatch: pytest.MonkeyPatch,
    engine: ReminderEngine,
    source: FakeSource,
    ledger: DeliveryLedger,
) -> None:
    source.items.clear()

    async def unexpected(*args: Any) -> dict[str, list[DeliveryRecord]]:
        raise AssertionError('history_for was called')

    monkeypatch.setattr(ledger, 'history_for', unexpected)

    assert await engine.tick() == TickReport()


OUTCOME_COUNTS = {
    PublishOutcome.SENT: TickReport(sent=1),
    PublishOutcome.ALREADY_HANDLED: TickReport(already_handled=1),
    PublishOutcome.NOT_CONFIGURED: TickReport(not_configured=1),
    PublishOutcome.UNDELIVERABLE: TickReport(undeliverable=1),
    PublishOutcome.SKIPPED: TickReport(rejected=1),
    PublishOutcome.PENDING: TickReport(pending=1),
}


def test_every_publish_outcome_is_counted() -> None:
    assert set(OUTCOME_COUNTS) == set(PublishOutcome)


@pytest.mark.parametrize(('outcome', 'report'), OUTCOME_COUNTS.items())
async def test_what_became_of_each_post_is_counted(
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    outcome: PublishOutcome,
    report: TickReport,
) -> None:
    await set_up(guild_settings)
    engine = ReminderEngine(guild_settings, ledger, OutcomePublisher(outcome), clock)
    source = FakeSource()
    source.items.append(workshop())
    engine.register(source)

    assert await tick_at(clock, engine, S - 23 * HOUR) == report


@pytest.mark.parametrize(
    ('failure', 'first'),
    [
        (PublishOutcome.PENDING, TickReport(pending=1)),
        (PublishOutcome.SKIPPED, TickReport(rejected=1)),
    ],
)
async def test_a_post_left_pending_or_refused_is_not_tried_again(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    failure: PublishOutcome,
    first: TickReport,
) -> None:
    # A pending post is the reconciler's to settle, and a refused one stays
    # refused.
    publisher.fail_next(failure)

    assert await tick_at(clock, engine, S - 23 * HOUR) == first
    assert await engine.tick() == TickReport()
    assert await tick_at(clock, engine, S - 22 * HOUR) == TickReport()
    assert publisher.posts == []


@pytest.mark.parametrize('stage', ['render', 'publish'])
async def test_a_post_that_fails_is_counted_and_the_tick_moves_on(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
    stage: str,
) -> None:
    source.items.append(workshop('evt-2', start=S + MINUTE))
    if stage == 'publish':
        publisher.fail_next(RuntimeError('boom'))
    else:
        render = source.render

        def render_failing_once(notice: Notice) -> OutgoingMessage:
            monkeypatch.setattr(source, 'render', render)
            raise RuntimeError('boom')

        monkeypatch.setattr(source, 'render', render_failing_once)

    assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(sent=1, failed=1)
    assert posted(publisher) == ['evt-2 1440m r0']
    assert await ledger.get(reminder_key('1440m')) is None  # so it is tried again

    assert await engine.tick() == TickReport(sent=1)
    assert posted(publisher) == ['evt-2 1440m r0', 'evt-1 1440m r0']


async def test_a_skip_already_in_the_ledger_counts_as_handled(
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    publisher: FakePublisher,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The 24h reminder's key was recorded for a start two days earlier, which
    # a source can cause by moving an occurrence without bumping its revision.
    stale = Delivery(
        reminder_key('1440m'),
        GUILD,
        WORKSHOPS,
        subject='event',
        subject_id='evt-1',
        kind='1440m',
        occurrence_start=S - 2 * DAY,
        revision=0,
    )
    await ledger.record_skip(stale, 'late')

    with caplog.at_level(logging.INFO, logger=REMINDERS_LOGGER):
        report = await tick_at(clock, engine, S - HOUR)
        # The source's mistake is reported, at WARNING at most hourly.
        await tick_at(clock, engine, S - HOUR + MINUTE)

    assert report == TickReport(sent=1, already_handled=1)
    assert (await get(ledger, reminder_key('1440m'))).occurrence_start == S - 2 * DAY
    reused = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if 'reused a revision' in record.getMessage()
    ]
    message = (
        f'Did not skip KCPC workshops {reminder_key("1440m")} in guild {GUILD}: '
        'the ledger has them for another start or state, so the source reused '
        'a revision'
    )
    assert reused == [(logging.WARNING, message), (logging.INFO, message)]


async def test_failures_are_warned_about_at_most_hourly_per_guild_and_kind(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    engine: ReminderEngine,
    source: FakeSource,
    guild_settings: GuildSettingsRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await set_up(guild_settings, OTHER_GUILD, channel_id=OTHER_CHANNEL)
    source.failing_guilds.update({GUILD, OTHER_GUILD})
    start = clock.now()

    def logged() -> list[tuple[int, bool]]:
        """The level of each failure logged since the last call, and whether
        it came with a traceback."""
        found = [
            (record.levelno, record.exc_info is not None)
            for record in caplog.records
            if record.name == REMINDERS_LOGGER
        ]
        caplog.clear()
        return found

    loud = [(logging.WARNING, True)] * 2  # once per guild
    quiet = [(logging.INFO, False)] * 2
    with caplog.at_level(logging.INFO, logger=REMINDERS_LOGGER):
        assert await engine.tick() == TickReport(failed=2)
        messages = [record.getMessage() for record in caplog.records]
        assert logged() == loud
        assert messages == [
            f'Could not plan KCPC workshops reminders for guild {guild_id}: '
            f"RuntimeError('no occurrences for guild {guild_id}')"
            for guild_id in (GUILD, OTHER_GUILD)
        ]
        await tick_at(clock, engine, start + MINUTE)
        assert logged() == quiet
        await tick_at(clock, engine, start + HOUR - SECOND)
        assert logged() == quiet
        await tick_at(clock, engine, start + HOUR)
        assert logged() == loud

        # Another kind of error in the same step is news.
        source.error = sqlite3.OperationalError
        await tick_at(clock, engine, start + HOUR + MINUTE)
        assert logged() == loud

        # So is a failure in another step in the same guilds.
        def render_failing(notice: Notice) -> OutgoingMessage:
            raise RuntimeError('cannot render')

        source.failing_guilds.clear()
        monkeypatch.setattr(source, 'render', render_failing)
        assert await tick_at(clock, engine, S - 23 * HOUR) == TickReport(failed=2)
        messages = [record.getMessage() for record in caplog.records]
        assert logged() == loud
        assert messages[0] == (
            f'Could not send KCPC workshops {reminder_key("1440m")} in guild '
            f"{GUILD}: RuntimeError('cannot render')"
        )


async def test_the_fake_publisher_refuses_to_post_inside_a_transaction(
    publisher: FakePublisher, guild_settings: GuildSettingsRepo, db: Database
) -> None:
    await set_up(guild_settings)

    with pytest.raises(RuntimeError, match='outside any database transaction'):
        async with db.transaction():
            await publisher.publish([delivery('k1')], OutgoingMessage(title='Hi'))

    assert publisher.posts == []


@pytest.mark.parametrize(
    ('enabled', 'channel_id', 'reason'),
    [(False, CHANNEL, 'disabled'), (True, None, 'no-channel')],
)
async def test_the_fake_publisher_posts_only_for_a_configured_feature(
    publisher: FakePublisher,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    enabled: bool,
    channel_id: int | None,
    reason: str,
) -> None:
    await set_up(guild_settings, enabled=enabled, channel_id=channel_id)

    result = await publisher.publish([delivery('k1')], OutgoingMessage(title='Hi'))

    assert result == PublishResult(PublishOutcome.NOT_CONFIGURED, reason=reason)
    assert await ledger_is_empty(ledger, GUILD)


async def test_the_fake_publisher_claims_posts_and_confirms_once(
    publisher: FakePublisher, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> None:
    await set_up(guild_settings)
    message = OutgoingMessage(title='Hi')

    first = await publisher.publish([delivery('k2'), delivery('k1')], message)
    again = await publisher.publish([delivery('k1')], message)
    more = await publisher.publish([delivery('k1'), delivery('k3')], message)

    assert first == PublishResult(
        PublishOutcome.SENT, message_id=FIRST_MESSAGE_ID, keys=('k1', 'k2')
    )
    assert again == PublishResult(PublishOutcome.ALREADY_HANDLED)
    assert more == PublishResult(
        PublishOutcome.SENT, message_id=FIRST_MESSAGE_ID + 1, keys=('k3',)
    )
    assert [(post.keys, post.message) for post in publisher.posts] == [
        (('k1', 'k2'), message),
        (('k3',), message),
    ]
    record = await get(ledger, 'k2')
    assert (record.status, record.message_id, record.channel_id, record.payload) == (
        SENT,
        FIRST_MESSAGE_ID,
        CHANNEL,
        message,
    )


async def test_the_fake_publisher_fails_on_cue(
    publisher: FakePublisher, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> None:
    await set_up(guild_settings)
    await publisher.publish([delivery('sent')], OutgoingMessage())
    message = OutgoingMessage(title='Hi')
    publisher.fail_next(
        PublishOutcome.PENDING, PublishOutcome.SKIPPED, RuntimeError('boom')
    )

    # A publish that finds every key handled leaves the cues alone.
    assert (await publisher.publish([delivery('sent')], message)).outcome is (
        PublishOutcome.ALREADY_HANDLED
    )
    assert await publisher.publish([delivery('pending')], message) == PublishResult(
        PublishOutcome.PENDING, reason='timeout', keys=('pending',)
    )
    assert await publisher.publish([delivery('refused')], message) == PublishResult(
        PublishOutcome.SKIPPED, reason='discord-403', keys=('refused',)
    )
    with pytest.raises(RuntimeError, match='boom'):
        await publisher.publish([delivery('broken')], message)

    assert (await get(ledger, 'pending')).status is CLAIMED
    refused = await get(ledger, 'refused')
    assert (refused.status, refused.reason) == (SKIPPED, 'discord-403')
    assert await ledger.get('broken') is None  # released, to be claimed again
    assert [post.keys for post in publisher.posts] == [('sent',)]
    # The cues are used up.
    assert (await publisher.publish([delivery('broken')], message)).outcome is (
        PublishOutcome.SENT
    )


@pytest.mark.parametrize(
    'outcome',
    [
        PublishOutcome.SENT,
        PublishOutcome.ALREADY_HANDLED,
        PublishOutcome.NOT_CONFIGURED,
        PublishOutcome.UNDELIVERABLE,
    ],
)
def test_the_fake_publisher_fakes_only_failures_after_a_claim(
    publisher: FakePublisher, outcome: PublishOutcome
) -> None:
    with pytest.raises(ValueError, match='cannot fail with'):
        publisher.fail_next(outcome)


@pytest.mark.parametrize(
    'deliveries',
    [[], [delivery('k1'), delivery('k2', guild_id=OTHER_GUILD)]],
    ids=['none', 'two guilds'],
)
async def test_the_fake_publisher_posts_for_one_guild_and_feature(
    publisher: FakePublisher, deliveries: Sequence[Delivery]
) -> None:
    with pytest.raises(ValueError, match='exactly one guild and feature'):
        await publisher.publish(deliveries, OutgoingMessage())
