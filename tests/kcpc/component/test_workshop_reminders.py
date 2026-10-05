"""Tests for the workshops feature's reminders (features/workshops/reminders.py).

The first tests check WorkshopReminders on its own: its policy, the workshops
it lists from a real kcpc.db, and how it renders each notice. The rest are
the feature's end-to-end scenarios: a fake Luma feed, the real EventSync and
ReminderEngine, and FakePublisher, on one database with a FakeClock, run as
the bot runs them. They follow what members of each server would see.
"""

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.kcpc.fakes import FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import ELLIPSIS, TITLE_LIMIT, EmbedField, OutgoingMessage
from tle.kcpc.core.reminders import (
    MAX_GROUP,
    Notice,
    NoticeKind,
    Occurrence,
    ReminderEngine,
    ReminderPolicy,
)
from tle.kcpc.core.settings import (
    FeatureRegistry,
    FeatureSettings,
    GuildSettingsRepo,
    default_registry,
)
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.features.workshops.reminders import (
    WorkshopOccurrence,
    WorkshopReminders,
    workshop_details,
)
from tle.kcpc.features.workshops.repo import EventRepo, EventStatus
from tle.kcpc.features.workshops.settings import SPEC, WORKSHOPS, WorkshopSettings
from tle.kcpc.features.workshops.sync import EventSync
from tle.kcpc.platforms.luma import CalendarNotFound, LumaEvent

CALENDAR = 'cal-ClubWorkshops01'
OTHER_CALENDAR = 'cal-OtherClub000002'
# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
REMINDERS_LOGGER = 'tle.kcpc.features.workshops.reminders'

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # where the clock fixture starts
S = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)  # a workshop's start, for rendering
SYNC_INTERVAL = 10 * MINUTE  # how often the workshops.sync job runs


def stamp(moment: datetime, style: str) -> str:
    """Discord's markup for ``moment``, as every post shows times."""
    return f'<t:{to_epoch(moment)}:{style}>'


def workshop(
    luma_id: str = 'evt-graphs',
    *,
    title: str = 'Graphs 101',
    start: datetime = S,
    end: datetime | None = S + 2 * HOUR,
    url: str | None = 'https://luma.com/graphs-101',
    location: str | None = 'Bush House 3.01',
    revision: int = 0,
) -> WorkshopOccurrence:
    return WorkshopOccurrence(
        subject='event',
        subject_id=luma_id,
        title=title,
        start=start,
        end=end,
        url=url,
        revision=revision,
        location=location,
    )


def luma_event(luma_id: str, start: datetime) -> LumaEvent:
    return LumaEvent(
        luma_id=luma_id,
        name=f'Workshop {luma_id}',
        start=start,
        end=start + 2 * HOUR,
        url=f'https://luma.com/{luma_id}',
        location='Bush House 3.01',
    )


class FakeFeed:
    """A Luma feed serving whatever events each calendar is given.

    A calendar that was never served is not found.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[LumaEvent]] = {}

    def serve(self, calendar_id: str, events: Sequence[LumaEvent]) -> None:
        self._events[calendar_id] = list(events)

    async def fetch(self, calendar_id: str) -> list[LumaEvent]:
        if calendar_id not in self._events:
            raise CalendarNotFound(calendar_id)
        return list(self._events[calendar_id])


@pytest.fixture
def clock() -> FakeClock:
    """The clock at NOW, moved without waiting in real time: nothing sleeps on it."""
    return FakeClock(NOW, io_grace=0)


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the workshops settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture
def repo(db: Database) -> EventRepo:
    return EventRepo(db)


@pytest.fixture
def source(repo: EventRepo) -> WorkshopReminders:
    return WorkshopReminders(repo, default_calendar=None)


def server_settings(
    *, calendar_id: str | None = None, reminder_minutes: tuple[int, ...] = (1440, 60)
) -> WorkshopSettings:
    """A server's workshop settings, turned on with a channel."""
    return WorkshopSettings(
        enabled=True,
        channel_id=CHANNEL,
        calendar_id=calendar_id,
        reminder_minutes=reminder_minutes,
    )


def test_the_feature_is_workshops(source: WorkshopReminders) -> None:
    assert source.feature == WORKSHOPS == 'workshops'


def test_the_default_policy_reminds_a_day_and_an_hour_before(
    source: WorkshopReminders,
) -> None:
    policy = source.policy(WorkshopSettings())

    assert policy == ReminderPolicy(offsets=(DAY, HOUR), horizon=400 * DAY)
    assert not policy.announce_start


def test_the_policy_follows_the_servers_reminder_minutes(
    source: WorkshopReminders,
) -> None:
    policy = source.policy(server_settings(reminder_minutes=(30, 2880)))

    assert policy.offsets == (30 * MINUTE, 2 * DAY)


def test_a_server_may_have_no_reminders(
    source: WorkshopReminders, caplog: pytest.LogCaptureFixture
) -> None:
    assert source.policy(server_settings(reminder_minutes=())).offsets == ()
    assert caplog.records == []


def test_invalid_reminder_minutes_are_left_out_with_a_warning(
    source: WorkshopReminders, caplog: pytest.LogCaptureFixture
) -> None:
    # From 1 minute to 400 days (576000 minutes), each once.
    minutes = (1440, 0, 1, -5, 60, 1440, 576_000, 576_001)

    with caplog.at_level(logging.WARNING, logger=REMINDERS_LOGGER):
        policy = source.policy(server_settings(reminder_minutes=minutes))

    assert policy.offsets == (DAY, MINUTE, HOUR, 400 * DAY)
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        'Ignoring 0, -5, 1440, 576001 in the workshop reminder_minutes '
        '(1440, 0, 1, -5, 60, 1440, 576000, 576001): each reminder is from 1 to '
        '576000 minutes before the start, and listed once'
    )


def test_each_invalid_setting_is_warned_about_once(
    source: WorkshopReminders, caplog: pytest.LogCaptureFixture
) -> None:
    # The engine asks for the policy every minute, for every server.
    with caplog.at_level(logging.WARNING, logger=REMINDERS_LOGGER):
        for _ in range(3):
            source.policy(server_settings(reminder_minutes=(60, 60)))
            source.policy(server_settings(reminder_minutes=(0,)))
            source.policy(server_settings(reminder_minutes=(1440, 60)))

    assert [record.getMessage().split(' in ')[0] for record in caplog.records] == [
        'Ignoring 60',
        'Ignoring 0',
    ]


def test_the_policy_needs_workshop_settings(source: WorkshopReminders) -> None:
    with pytest.raises(
        TypeError, match='Expected WorkshopSettings, got FeatureSettings'
    ):
        source.policy(FeatureSettings(enabled=True))


async def store(
    repo: EventRepo, calendar_id: str, *events: LumaEvent, cancel: Sequence[str] = ()
) -> None:
    """Store ``events`` as a sync would, then cancel those in ``cancel``."""
    await repo.add(calendar_id, events, now=NOW)
    for event in await repo.events(calendar_id):
        if event.luma_id in cancel:
            cancelled = replace(
                event, status=EventStatus.CANCELLED, revision=event.revision + 1
            )
            await repo.save([cancelled])


async def test_occurrences_are_the_calendars_workshops_in_the_window(
    repo: EventRepo, source: WorkshopReminders
) -> None:
    await store(
        repo,
        CALENDAR,
        luma_event('evt-before', NOW - MINUTE),
        luma_event('evt-first', NOW),
        luma_event('evt-cancelled', NOW + DAY),
        luma_event('evt-last', NOW + 2 * DAY - MINUTE),
        luma_event('evt-after', NOW + 2 * DAY),
        cancel=['evt-cancelled'],
    )
    await store(repo, OTHER_CALENDAR, luma_event('evt-elsewhere', NOW + HOUR))
    source.mark_synced(CALENDAR)
    source.mark_synced(OTHER_CALENDAR)

    found = await source.occurrences(
        GUILD, server_settings(calendar_id=CALENDAR), NOW, NOW + 2 * DAY
    )

    # Cancelled ones too, so that their cancellation can be announced.
    assert [(item.subject_id, item.cancelled, item.revision) for item in found] == [
        ('evt-first', False, 0),
        ('evt-cancelled', True, 1),
        ('evt-last', False, 0),
    ]


async def test_an_occurrence_carries_everything_a_post_shows(
    repo: EventRepo, source: WorkshopReminders
) -> None:
    event = luma_event('evt-graphs', S)
    await store(repo, CALENDAR, event)
    source.mark_synced(CALENDAR)

    (found,) = await source.occurrences(
        GUILD, server_settings(calendar_id=CALENDAR), NOW, NOW + 7 * DAY
    )

    assert found == WorkshopOccurrence(
        subject='event',
        subject_id='evt-graphs',
        title='Workshop evt-graphs',
        start=S,
        end=S + 2 * HOUR,
        url='https://luma.com/evt-graphs',
        revision=0,
        cancelled=False,
        location='Bush House 3.01',
    )


async def test_a_server_without_a_calendar_follows_the_default(
    repo: EventRepo,
) -> None:
    await store(repo, CALENDAR, luma_event('evt-default', S))
    await store(repo, OTHER_CALENDAR, luma_event('evt-own', S))
    source = WorkshopReminders(repo, default_calendar=CALENDAR)
    source.mark_synced(CALENDAR)
    source.mark_synced(OTHER_CALENDAR)

    default = await source.occurrences(GUILD, server_settings(), NOW, NOW + 7 * DAY)
    own = await source.occurrences(
        GUILD, server_settings(calendar_id=OTHER_CALENDAR), NOW, NOW + 7 * DAY
    )

    assert [item.subject_id for item in default] == ['evt-default']
    assert [item.subject_id for item in own] == ['evt-own']
    assert source.calendar_for(server_settings()) == CALENDAR
    assert source.calendar_for(server_settings(calendar_id='')) == CALENDAR


async def test_a_server_without_any_calendar_has_no_workshops(
    repo: EventRepo, source: WorkshopReminders
) -> None:
    await store(repo, CALENDAR, luma_event('evt-graphs', S))
    source.mark_synced(CALENDAR)

    assert await source.occurrences(GUILD, server_settings(), NOW, NOW + 7 * DAY) == []
    assert source.calendar_for(server_settings()) is None


async def test_a_calendar_has_no_workshops_until_the_bot_has_tried_to_sync_it(
    repo: EventRepo, source: WorkshopReminders
) -> None:
    # Stored before the bot started: they may have changed since.
    await store(repo, CALENDAR, luma_event('evt-graphs', S))
    await store(repo, OTHER_CALENDAR, luma_event('evt-other', S))
    settings = server_settings(calendar_id=CALENDAR)
    assert not source.is_synced(CALENDAR)

    assert await source.occurrences(GUILD, settings, NOW, NOW + 7 * DAY) == []

    # Tried, whether or not it worked; another calendar doesn't count.
    source.mark_synced(OTHER_CALENDAR)
    assert await source.occurrences(GUILD, settings, NOW, NOW + 7 * DAY) == []
    source.mark_synced(CALENDAR)
    assert source.is_synced(CALENDAR)
    found = await source.occurrences(GUILD, settings, NOW, NOW + 7 * DAY)
    assert [item.subject_id for item in found] == ['evt-graphs']


def reminder(offset: timedelta, *workshops: Occurrence) -> Notice:
    return Notice(NoticeKind.REMINDER, workshops, offset=offset)


def post(
    title: str,
    description: str | None = None,
    *,
    url: str | None = None,
    fields: tuple[EmbedField, ...] = (),
) -> OutgoingMessage:
    """A post as the workshops feature makes them: footed, mentioning its role."""
    return OutgoingMessage(
        title=title,
        description=description,
        url=url,
        fields=fields,
        footer='KCPC workshops',
        mention_role=True,
    )


WHEN = f'**When:** {stamp(S, "F")} ({stamp(S, "R")})'
ENDS = f'**Ends:** {stamp(S + 2 * HOUR, "f")}'
WHERE = '**Where:** Bush House 3.01'


@pytest.mark.parametrize(
    ('offset', 'heading'),
    [
        (DAY, 'Coming up'),
        (12 * HOUR, 'Coming up'),
        (12 * HOUR - MINUTE, 'Starting soon'),
        (HOUR, 'Starting soon'),
        (timedelta(0), 'Starting soon'),
    ],
)
def test_a_reminder_says_when_and_where_the_workshop_is(
    source: WorkshopReminders, offset: timedelta, heading: str
) -> None:
    message = source.render(reminder(offset, workshop()))

    assert message == post(
        f'{heading}: Graphs 101',
        '\n'.join([WHEN, ENDS, WHERE]),
        url='https://luma.com/graphs-101',
    )


def test_a_reminder_leaves_out_what_is_not_known(source: WorkshopReminders) -> None:
    bare = workshop(end=None, url=None, location=None)

    assert source.render(reminder(HOUR, bare)) == post(
        'Starting soon: Graphs 101', WHEN
    )


def test_a_location_that_is_the_workshops_page_is_not_repeated(
    source: WorkshopReminders,
) -> None:
    # An external event's LOCATION is its page, which the title links to.
    external = workshop(
        'calev-graphs',
        url='https://example.org/graphs',
        location='https://example.org/graphs',
    )

    message = source.render(reminder(DAY, external))

    assert message.description == '\n'.join([WHEN, ENDS])


def test_workshops_at_the_same_time_share_one_reminder(
    source: WorkshopReminders,
) -> None:
    graphs = workshop()
    dp = workshop(
        'evt-dp',
        title='Dynamic programming',
        url='https://luma.com/dp-(part-1)',
        end=None,
        location=None,
    )

    message = source.render(reminder(HOUR, graphs, dp))

    assert message == post(
        'Starting soon: 2 workshops',
        fields=(
            EmbedField(
                'Graphs 101',
                '\n'.join(
                    [WHEN, ENDS, WHERE, '[Event page](https://luma.com/graphs-101)']
                ),
            ),
            EmbedField(
                'Dynamic programming',
                # Parentheses would end the link early.
                '\n'.join([WHEN, '[Event page](https://luma.com/dp-%28part-1%29)']),
            ),
        ),
    )


def test_a_reminder_of_the_most_workshops_a_post_holds_fits_them_all(
    source: WorkshopReminders,
) -> None:
    # Once posted, every workshop of the reminder counts as reminded of, so
    # Discord's limits must not drop any, however long their details.
    page = 'https://luma.com/' + 'p' * 183  # the longest link a field keeps
    workshops = [
        workshop(
            f'evt-{n}',
            title=f'{n} ' + 'T' * (TITLE_LIMIT - 2),
            url=page,
            location='L' * 300,
        )
        for n in range(MAX_GROUP)
    ]

    message = source.render(reminder(HOUR, *workshops))

    assert message.title == f'Starting soon: {MAX_GROUP} workshops'
    assert len(message.fields) == MAX_GROUP
    assert message.within_discord_limits() == message
    for n, field in enumerate(message.fields):
        assert field.name == f'{n} ' + 'T' * 97 + ELLIPSIS
        assert field.value == '\n'.join(
            [WHEN, ENDS, '**Where:** ' + 'L' * 149 + ELLIPSIS, f'[Event page]({page})']
        )


def test_a_link_too_long_for_a_reminder_of_several_workshops_is_left_out() -> None:
    long_page = workshop(url='https://luma.com/' + 'p' * 184)

    assert workshop_details(long_page, link=True) == '\n'.join([WHEN, ENDS, WHERE])
    # A post about one workshop links its title to the page instead.
    assert workshop_details(long_page) == '\n'.join([WHEN, ENDS, WHERE])


def test_a_time_change_gives_the_new_and_the_previous_time(
    source: WorkshopReminders,
) -> None:
    moved = workshop(start=S + 10 * MINUTE, revision=1)
    notice = Notice(NoticeKind.MOVED, (moved,), previous_start=S)

    assert source.render(notice) == post(
        'Time changed: Graphs 101',
        '\n'.join(
            [
                f'**New time:** {stamp(S + 10 * MINUTE, "F")} '
                f'({stamp(S + 10 * MINUTE, "R")})',
                f'**Previously:** {stamp(S, "F")}',
            ]
        ),
        url='https://luma.com/graphs-101',
    )


def test_a_time_change_without_the_previous_time_gives_the_new_one(
    source: WorkshopReminders,
) -> None:
    notice = Notice(NoticeKind.MOVED, (workshop(),))

    assert source.render(notice).description == (
        f'**New time:** {stamp(S, "F")} ({stamp(S, "R")})'
    )


def test_a_cancellation_gives_the_time_it_was_planned_for(
    source: WorkshopReminders,
) -> None:
    notice = Notice(NoticeKind.CANCELLED, (workshop(revision=1),))

    # No link: the workshop's page is gone.
    assert source.render(notice) == post(
        'Cancelled: Graphs 101', f'**Was planned for:** {stamp(S, "F")}'
    )


def test_a_workshop_back_on_gives_its_time(source: WorkshopReminders) -> None:
    notice = Notice(NoticeKind.REINSTATED, (workshop(revision=2),))

    assert source.render(notice) == post(
        'Back on: Graphs 101', WHEN, url='https://luma.com/graphs-101'
    )


def test_workshop_details_can_end_with_a_link() -> None:
    assert workshop_details(workshop()) == '\n'.join([WHEN, ENDS, WHERE])
    assert workshop_details(workshop(), link=True) == '\n'.join(
        [WHEN, ENDS, WHERE, '[Event page](https://luma.com/graphs-101)']
    )
    assert workshop_details(workshop(url=None), link=True) == '\n'.join(
        [WHEN, ENDS, WHERE]
    )


def test_any_occurrence_can_be_described() -> None:
    plain = Occurrence('event', 'evt-graphs', 'Graphs 101', S, None, None, 0)

    assert workshop_details(plain) == WHEN


class Club:
    """The workshops feature as the bot runs it, on the test's kcpc.db and clock.

    A fake Luma feed, EventSync, and the reminder engine with WorkshopReminders
    posting through FakePublisher. ``run_until`` runs the bot's jobs minute by
    minute: every 10 minutes the sync job syncs each calendar that a server
    with workshops on follows, and ticks at once if events changed or a
    calendar was synced for the first time; every minute the reminders job
    ticks.
    """

    def __init__(
        self,
        db: Database,
        clock: FakeClock,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        feed: FakeFeed,
        *,
        default_calendar: str | None = None,
    ) -> None:
        self.clock = clock
        self.guild_settings = guild_settings
        self.feed = feed
        repo = EventRepo(db)
        self.sync = EventSync(db, repo, feed, clock)
        self.source = WorkshopReminders(repo, default_calendar)
        self.publisher = FakePublisher(guild_settings, ledger)
        self.engine = ReminderEngine(guild_settings, ledger, self.publisher, clock)
        self.engine.register(self.source)

    async def start(self) -> None:
        """What the bot does at startup: both jobs run once it is ready.

        The reminders job ticks first: it started first, and reads only the
        database, while the sync job waits for Luma.
        """
        await self.engine.tick()
        await self.sync_calendars()

    async def sync_calendars(self) -> None:
        """What the sync job does (``KcpcWorkshops._sync_all``)."""
        guilds = await self.guild_settings.enabled_guilds(WORKSHOPS)
        calendars = {self.source.calendar_for(settings) for _, settings in guilds}
        remind = False
        for calendar_id in sorted(c for c in calendars if c is not None):
            remind = remind or not self.source.is_synced(calendar_id)
            report = await self.sync.sync(calendar_id)
            self.source.mark_synced(calendar_id)
            remind = remind or report.changed
        if remind:
            await self.engine.tick()

    async def run_until(self, when: datetime) -> None:
        while self.clock.now() < when:
            await self.clock.advance(MINUTE)
            if (self.clock.now() - NOW) % SYNC_INTERVAL == timedelta(0):
                await self.sync_calendars()
            await self.engine.tick()

    def posts(self, guild_id: int = GUILD) -> list[str]:
        """Each post in the guild, as '<kind> <workshop> r<revision>: <title>'."""
        return [
            ' + '.join(
                f'{delivery.kind} {delivery.subject_id} r{delivery.revision}'
                for delivery in item.deliveries
            )
            + f': {item.message.title}'
            for item in self.publisher.posts
            if item.deliveries[0].guild_id == guild_id
        ]


@pytest.fixture
def feed() -> FakeFeed:
    return FakeFeed()


@pytest.fixture
def club(
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    feed: FakeFeed,
) -> Club:
    return Club(db, clock, guild_settings, ledger, feed)


async def follow(
    guild_settings: GuildSettingsRepo, guild_id: int, calendar_id: str | None
) -> None:
    """Set the guild up for workshops: on, with a channel and a calendar."""
    await guild_settings.update(
        guild_id, WORKSHOPS, enabled=True, channel_id=CHANNEL, calendar_id=calendar_id
    )


async def kinds_in_ledger(
    ledger: DeliveryLedger, luma_id: str, guild_id: int = GUILD
) -> list[tuple[str | None, DeliveryStatus, str | None]]:
    history = await ledger.history_for(guild_id, 'event', [luma_id])
    return [(record.kind, record.status, record.reason) for record in history[luma_id]]


async def test_a_workshop_70_minutes_ahead_gets_exactly_one_1h_reminder(
    club: Club, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', NOW + 70 * MINUTE)])

    await club.start()
    await club.run_until(NOW + 90 * MINUTE)

    assert club.posts() == ['60m evt-1 r0: Starting soon: Workshop evt-1']
    # The day-before reminder was due when the workshop was found, but the 1h
    # one was due within half an hour: it was recorded as skipped instead.
    assert await kinds_in_ledger(ledger, 'evt-1') == [
        ('1440m', DeliveryStatus.SKIPPED, 'late'),
        ('60m', DeliveryStatus.SENT, None),
    ]
    sent = await ledger.get(club.publisher.posts[0].keys[0])
    assert sent is not None and sent.sent_at == NOW + 10 * MINUTE


async def test_a_restart_does_not_repeat_a_reminder(
    club: Club,
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    feed: FakeFeed,
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [luma_event('evt-1', NOW + 70 * MINUTE)])
    await club.start()
    await club.run_until(NOW + 15 * MINUTE)
    assert len(club.posts()) == 1

    # The bot restarts: everything is new but the database.
    restarted = Club(db, clock, guild_settings, ledger, feed)
    await restarted.start()
    await restarted.run_until(NOW + 90 * MINUTE)

    assert restarted.posts() == []


async def test_a_workshop_moved_while_the_bot_was_down_is_reminded_of_once(
    club: Club,
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    feed: FakeFeed,
) -> None:
    start, new_start = NOW + 7 * HOUR, NOW + 8 * HOUR  # 19:00, then 20:00
    await follow(guild_settings, GUILD, CALENDAR)
    feed.serve(CALENDAR, [luma_event('evt-1', start)])
    await club.start()
    await club.run_until(NOW + 5 * HOUR)
    assert club.posts() == ['1440m evt-1 r0: Coming up: Workshop evt-1']

    # The bot is down from 17:00 to 18:30, through 18:00 when the 1h reminder
    # was due, and meanwhile the workshop moves to 20:00.
    feed.serve(CALENDAR, [luma_event('evt-1', new_start)])
    await clock.advance_to(NOW + 6 * HOUR + 30 * MINUTE)
    restarted = Club(db, clock, guild_settings, ledger, feed)
    # The reminders job ticks before the sync job has synced anything. The
    # database still has the workshop at 19:00, so it must not remind of it.
    await restarted.engine.tick()
    assert restarted.posts() == []

    await restarted.sync_calendars()
    await restarted.run_until(NOW + 7 * HOUR + 30 * MINUTE)

    assert restarted.posts() == [
        'moved evt-1 r1: Time changed: Workshop evt-1',
        '60m evt-1 r1: Starting soon: Workshop evt-1',
    ]
    notice, reminder = restarted.publisher.posts
    assert notice.message.description == time_change(new_start, start)
    assert reminder.message.description is not None
    assert stamp(new_start, 'F') in reminder.message.description
    sent = await ledger.get(reminder.keys[0])
    assert sent is not None and sent.sent_at == new_start - HOUR


async def test_a_workshop_postponed_10_minutes_gets_only_a_time_change_notice(
    club: Club, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', NOW + 70 * MINUTE)])
    await club.start()
    await club.run_until(NOW + 15 * MINUTE)

    club.feed.serve(CALENDAR, [luma_event('evt-1', NOW + 80 * MINUTE)])
    await club.run_until(NOW + 100 * MINUTE)

    # The 1h reminder isn't sent again: the start moved by less than an hour.
    assert club.posts() == [
        '60m evt-1 r0: Starting soon: Workshop evt-1',
        'moved evt-1 r1: Time changed: Workshop evt-1',
    ]
    notice = club.publisher.posts[1].message
    assert notice.description == time_change(NOW + 80 * MINUTE, NOW + 70 * MINUTE)


def time_change(new: datetime, previous: datetime) -> str:
    """What a time change notice says."""
    return (
        f'**New time:** {stamp(new, "F")} ({stamp(new, "R")})\n'
        f'**Previously:** {stamp(previous, "F")}'
    )


async def test_a_second_time_change_gives_the_time_members_were_last_told(
    club: Club, guild_settings: GuildSettingsRepo
) -> None:
    first, second, third = (NOW + minutes * MINUTE for minutes in (70, 80, 90))
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', first)])
    await club.start()
    await club.run_until(NOW + 15 * MINUTE)

    club.feed.serve(CALENDAR, [luma_event('evt-1', second)])
    await club.run_until(NOW + 25 * MINUTE)
    club.feed.serve(CALENDAR, [luma_event('evt-1', third)])
    await club.run_until(NOW + 35 * MINUTE)

    assert club.posts() == [
        '60m evt-1 r0: Starting soon: Workshop evt-1',
        'moved evt-1 r1: Time changed: Workshop evt-1',
        'moved evt-1 r2: Time changed: Workshop evt-1',
    ]
    # Not the time the 1h reminder gave, but the one the first notice gave.
    assert [post.message.description for post in club.publisher.posts[1:]] == [
        time_change(second, first),
        time_change(third, second),
    ]


async def test_a_vanished_workshop_is_cancelled_after_3_syncs_with_one_notice(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    start = NOW + 30 * HOUR
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', start)])  # its only workshop
    await club.start()
    await clock.advance_to(start - DAY - MINUTE)
    await club.run_until(start - DAY)
    assert club.posts() == ['1440m evt-1 r0: Coming up: Workshop evt-1']

    # Luma drops a cancelled event from the feed. Two syncs could be a glitch.
    club.feed.serve(CALENDAR, [])
    await club.run_until(start - DAY + 29 * MINUTE)
    assert len(club.posts()) == 1

    await club.run_until(start - DAY + 30 * MINUTE)  # the third sync
    assert club.posts()[1:] == ['cancelled evt-1 r1: Cancelled: Workshop evt-1']

    await club.run_until(start - DAY + 60 * MINUTE)
    assert len(club.posts()) == 2


async def test_a_reinstated_workshop_gets_one_notice(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    start = NOW + 30 * HOUR
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', start)])
    await club.start()
    await clock.advance_to(start - DAY - MINUTE)
    await club.run_until(start - DAY)
    club.feed.serve(CALENDAR, [])
    await club.run_until(start - DAY + 30 * MINUTE)

    club.feed.serve(CALENDAR, [luma_event('evt-1', start)])
    await club.run_until(start - DAY + 90 * MINUTE)

    assert club.posts() == [
        '1440m evt-1 r0: Coming up: Workshop evt-1',
        'cancelled evt-1 r1: Cancelled: Workshop evt-1',
        'reinstated evt-1 r2: Back on: Workshop evt-1',
    ]
    # The day-before reminder isn't repeated, and the 1h one comes as usual.
    await clock.advance_to(start - HOUR - MINUTE)
    await club.run_until(start)
    assert club.posts()[3:] == ['60m evt-1 r2: Starting soon: Workshop evt-1']


async def test_quick_syncs_by_hand_do_not_cancel_a_missing_workshop(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    start = NOW + 30 * HOUR
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', start)])
    await club.start()
    await clock.advance_to(start - DAY - MINUTE)
    await club.run_until(start - DAY)

    # Luma drops the workshop for a minute, while an admin runs
    # /kcpc workshops sync three times (it ticks if events changed).
    club.feed.serve(CALENDAR, [])
    for _ in range(3):
        await clock.advance(timedelta(seconds=20))
        if (await club.sync.sync(CALENDAR)).changed:
            await club.engine.tick()
    await clock.advance(MINUTE)
    club.feed.serve(CALENDAR, [luma_event('evt-1', start)])
    await club.run_until(start - DAY + 30 * MINUTE)

    # Three misses within a minute cancel nothing: no notice either way.
    assert club.posts() == ['1440m evt-1 r0: Coming up: Workshop evt-1']


async def test_a_move_in_a_feed_that_lost_most_workshops_is_applied_at_once(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    first = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    weekly = [luma_event(f'evt-W{n + 1}', first + n * 7 * DAY) for n in range(6)]
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, weekly)
    await club.start()

    # Four of the six workshops vanish, as a glitch at Luma would look, while
    # W2 moves from 12:00 to 14:00. The move is applied at once; the missing
    # ones are cancelled only after about an hour.
    w2_moved = luma_event('evt-W2', weekly[1].start + 2 * HOUR)
    club.feed.serve(CALENDAR, [weekly[0], w2_moved])
    await club.run_until(NOW + 2 * HOUR)
    starts = (weekly[1].start, w2_moved.start, weekly[2].start)
    for due in sorted(start - offset for start in starts for offset in (DAY, HOUR)):
        if clock.now() < due - 3 * MINUTE:
            await clock.advance_to(due - 3 * MINUTE)
        await club.run_until(due + 3 * MINUTE)

    # Members never heard of W3 to W6, so their cancellation isn't announced.
    assert club.posts() == [
        '1440m evt-W1 r0: Coming up: Workshop evt-W1',
        '1440m evt-W2 r1: Coming up: Workshop evt-W2',
        '60m evt-W2 r1: Starting soon: Workshop evt-W2',
    ]
    w2_reminders = club.publisher.posts[1:]
    assert {post.deliveries[0].occurrence_start for post in w2_reminders} == {
        w2_moved.start
    }


async def test_switching_a_servers_calendar_is_quiet(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    old_start = NOW + 30 * HOUR
    new_start = old_start + DAY
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-old', old_start)])
    club.feed.serve(OTHER_CALENDAR, [luma_event('evt-new', new_start)])
    await club.start()
    await clock.advance_to(old_start - DAY - MINUTE)
    await club.run_until(old_start - DAY)
    assert club.posts() == ['1440m evt-old r0: Coming up: Workshop evt-old']

    await follow(guild_settings, GUILD, OTHER_CALENDAR)
    await club.run_until(old_start - DAY + 30 * MINUTE)
    await clock.advance_to(old_start - HOUR - MINUTE)
    await club.run_until(old_start + 10 * MINUTE)

    # Nothing more about the old calendar's workshop, not even that it's gone;
    # the new calendar's workshop is reminded of as usual.
    assert club.posts() == [
        '1440m evt-old r0: Coming up: Workshop evt-old',
        '1440m evt-new r0: Coming up: Workshop evt-new',
    ]


async def test_switching_to_a_calendar_that_lists_a_moved_workshop_too(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    first = NOW + 30 * HOUR
    postponed = first + DAY
    await follow(guild_settings, GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', first)])
    await club.start()
    await clock.advance_to(first - DAY - MINUTE)
    await club.run_until(first - DAY)
    await clock.advance_to(first - HOUR - MINUTE)
    await club.run_until(first - HOUR + 5 * MINUTE)
    club.feed.serve(CALENDAR, [luma_event('evt-1', postponed)])
    await club.run_until(first - HOUR + 20 * MINUTE)

    # Another calendar lists the same Luma event, first after the move, and the
    # server switches to it. Its copy's revision goes on from the first
    # calendar's: the ledger knows posts by Luma ID and revision, and would
    # take a reused one for reminders already made.
    club.feed.serve(OTHER_CALENDAR, [luma_event('evt-1', postponed)])
    await follow(guild_settings, GUILD, OTHER_CALENDAR)
    await club.run_until(first - HOUR + 40 * MINUTE)
    await clock.advance_to(postponed - DAY - MINUTE)
    await club.run_until(postponed - DAY + MINUTE)
    await clock.advance_to(postponed - HOUR - MINUTE)
    await club.run_until(postponed)

    assert club.posts() == [
        '1440m evt-1 r0: Coming up: Workshop evt-1',
        '60m evt-1 r0: Starting soon: Workshop evt-1',
        'moved evt-1 r1: Time changed: Workshop evt-1',
        '1440m evt-1 r2: Coming up: Workshop evt-1',
        '60m evt-1 r2: Starting soon: Workshop evt-1',
    ]


async def test_a_server_without_a_calendar_gets_nothing(
    club: Club, guild_settings: GuildSettingsRepo, ledger: DeliveryLedger
) -> None:
    await follow(guild_settings, GUILD, None)
    await follow(guild_settings, OTHER_GUILD, CALENDAR)
    club.feed.serve(CALENDAR, [luma_event('evt-1', NOW + 70 * MINUTE)])

    await club.start()
    await club.run_until(NOW + 90 * MINUTE)

    assert club.posts(GUILD) == []
    assert set((await ledger.status_counts(GUILD)).values()) == {0}
    # The calendar was synced and reminded of, for the server that follows it.
    assert club.posts(OTHER_GUILD) == ['60m evt-1 r0: Starting soon: Workshop evt-1']


async def test_a_server_without_a_calendar_follows_the_bots_default(
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    feed: FakeFeed,
) -> None:
    club = Club(db, clock, guild_settings, ledger, feed, default_calendar=CALENDAR)
    await follow(guild_settings, GUILD, None)
    feed.serve(CALENDAR, [luma_event('evt-1', NOW + 70 * MINUTE)])

    await club.start()
    await club.run_until(NOW + 90 * MINUTE)

    assert club.posts() == ['60m evt-1 r0: Starting soon: Workshop evt-1']


async def test_servers_on_one_calendar_each_get_their_reminders(
    club: Club, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, CALENDAR)
    await follow(guild_settings, OTHER_GUILD, CALENDAR)
    club.feed.serve(
        CALENDAR,
        [
            luma_event('evt-a', NOW + 70 * MINUTE),
            luma_event('evt-b', NOW + 70 * MINUTE),
        ],
    )

    await club.start()
    await club.run_until(NOW + 90 * MINUTE)

    # Workshops at the same time share a post.
    expected = ['60m evt-a r0 + 60m evt-b r0: Starting soon: 2 workshops']
    assert club.posts(GUILD) == expected
    assert club.posts(OTHER_GUILD) == expected
