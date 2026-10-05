"""Tests for the contests feature's reminders (features/contests/reminders.py).

The first tests check ContestReminders on its own: its policy, the contests it
lists from a real kcpc.db, and how it renders each notice. The rest are the
feature's end-to-end scenarios: fake sources, the real ContestSync and
ReminderEngine, and FakePublisher, on one database with a FakeClock, run as
the bot runs them. They follow what members of each server would see.
"""

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.kcpc.fakes import FakePublisher
from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import DeliveryLedger
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
from tle.kcpc.features.contests.reminders import (
    ContestOccurrence,
    ContestReminders,
    contest_details,
    contest_link,
    platform_name,
)
from tle.kcpc.features.contests.repo import ContestInfo, ContestRepo, ContestStatus
from tle.kcpc.features.contests.settings import (
    CONTESTS,
    PLATFORMS,
    SPEC,
    ContestSettings,
)
from tle.kcpc.features.contests.sync import ContestSync, SourceSnapshot

# Real snowflakes are 64-bit, so use big ones.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
REMINDERS_LOGGER = 'tle.kcpc.features.contests.reminders'
LONDON = ZoneInfo('Europe/London')

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
ROUND = 2 * HOUR + 15 * MINUTE  # how long a Codeforces round here runs
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # where the clock fixture starts
S = datetime(2026, 10, 3, 14, 35, tzinfo=UTC)  # a contest's start, for rendering
SYNC_INTERVAL = 5 * MINUTE  # how often the scenarios sync every source

CF_PAGE = 'https://codeforces.com/contests/2051'
PLATFORM = '**Platform:** Codeforces'
DURATION = '**Duration:** 2h 15m'
LINK = f'[Contest page]({CF_PAGE})'


def stamp(moment: datetime, style: str) -> str:
    """Discord's markup for ``moment``, as every post shows times."""
    return f'<t:{to_epoch(moment)}:{style}>'


def starts(moment: datetime = S) -> str:
    return f'**Starts:** {stamp(moment, "F")} ({stamp(moment, "R")})'


def occurrence(
    subject_id: str = 'codeforces:2051',
    *,
    title: str = 'Codeforces Round 1050 (Div. 1)',
    platform: str = 'codeforces',
    start: datetime = S,
    end: datetime | None = S + ROUND,
    url: str | None = CF_PAGE,
    revision: int = 0,
) -> ContestOccurrence:
    return ContestOccurrence(
        subject='contest',
        subject_id=subject_id,
        title=title,
        start=start,
        end=end,
        url=url,
        revision=revision,
        platform=platform,
    )


def post(
    title: str,
    description: str | None = None,
    *,
    url: str | None = None,
    fields: tuple[EmbedField, ...] = (),
) -> OutgoingMessage:
    return OutgoingMessage(
        title=title,
        description=description,
        url=url,
        fields=fields,
        footer='KCPC contests',
        mention_role=True,
    )


def reminder(offset: timedelta, *contests: Occurrence) -> Notice:
    return Notice(NoticeKind.REMINDER, contests, offset=offset)


def codeforces_round(
    contest_id: int, start: datetime, *, division: int = 1
) -> ContestInfo:
    return ContestInfo(
        platform='codeforces',
        external_id=str(contest_id),
        name=f'Codeforces Round 1050 (Div. {division})',
        start=start,
        start_date=None,
        end=start + ROUND,
        url=f'https://codeforces.com/contests/{contest_id}',
    )


def atcoder_contest(contest_id: str, start: datetime) -> ContestInfo:
    return ContestInfo(
        platform='atcoder',
        external_id=contest_id,
        name=f'AtCoder Beginner Contest {contest_id[3:]}',
        start=start,
        start_date=None,
        end=start + 100 * MINUTE,
        url=f'https://atcoder.jp/contests/{contest_id}',
    )


UKIEPC_NAME = 'The 2026 ICPC UK & Ireland Programming Contest'


def ukiepc(day: date = date(2026, 10, 3)) -> ContestInfo:
    """UKIEPC as icpc.global lists it: by its date only."""
    return ContestInfo(
        platform='icpc',
        external_id='9584',
        name=UKIEPC_NAME,
        start=None,
        start_date=day,
        end=None,
        url='https://ukiepc.info/',
    )


@pytest.fixture
def clock() -> FakeClock:
    """The clock at NOW, moved without waiting in real time: nothing sleeps on it."""
    return FakeClock(NOW, io_grace=0)


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    """The registry as bootstrap builds it, with the contest settings typed."""
    registry = default_registry()
    registry.register(SPEC, replace=True)
    return registry


@pytest.fixture
def repo(db: Database) -> ContestRepo:
    return ContestRepo(db, tz=LONDON)


@pytest.fixture
def source(repo: ContestRepo) -> ContestReminders:
    return ContestReminders(repo)


def server_settings(
    *,
    platforms: tuple[str, ...] = PLATFORMS,
    reminder_minutes: tuple[int, ...] = (60,),
    start_posts: bool = False,
) -> ContestSettings:
    """A server's contest settings, turned on with a channel."""
    return ContestSettings(
        enabled=True,
        channel_id=CHANNEL,
        platforms=platforms,
        reminder_minutes=reminder_minutes,
        start_posts=start_posts,
    )


def mark_all_synced(source: ContestReminders) -> None:
    for platform in ('codeforces', 'atcoder', 'icpc'):
        source.mark_synced(platform)


def test_the_feature_is_contests(source: ContestReminders) -> None:
    assert source.feature == CONTESTS == 'contests'


def test_the_default_policy_reminds_an_hour_before(source: ContestReminders) -> None:
    policy = source.policy(ContestSettings())

    assert policy == ReminderPolicy(offsets=(HOUR,), horizon=400 * DAY)
    assert not policy.announce_start


def test_the_policy_follows_the_servers_settings(source: ContestReminders) -> None:
    policy = source.policy(
        server_settings(reminder_minutes=(30, 1440), start_posts=True)
    )

    assert policy.offsets == (30 * MINUTE, DAY)
    assert policy.announce_start


def test_invalid_reminder_minutes_are_left_out_with_a_warning_once(
    source: ContestReminders, caplog: pytest.LogCaptureFixture
) -> None:
    # From 1 minute to 400 days (576000 minutes), each once.
    minutes = (60, 0, 1, -5, 60, 576_000, 576_001)

    with caplog.at_level(logging.WARNING, logger=REMINDERS_LOGGER):
        for _ in range(3):  # the engine asks for the policy every minute
            policy = source.policy(server_settings(reminder_minutes=minutes))

    assert policy.offsets == (HOUR, MINUTE, 576_000 * MINUTE)
    assert [record.getMessage() for record in caplog.records] == [
        'Ignoring 0, -5, 60, 576001 in the contest reminder_minutes '
        '(60, 0, 1, -5, 60, 576000, 576001): each reminder is from 1 to 576000 '
        'minutes before the start, and listed once'
    ]


def test_the_policy_needs_contest_settings(source: ContestReminders) -> None:
    with pytest.raises(
        TypeError, match='Expected ContestSettings, got FeatureSettings'
    ):
        source.policy(FeatureSettings(enabled=True))


async def test_occurrences_are_the_timed_contests_in_the_window(
    repo: ContestRepo, source: ContestReminders
) -> None:
    await repo.add(
        [
            codeforces_round(2040, NOW - HOUR),  # started before the window
            codeforces_round(2051, NOW + HOUR),
            codeforces_round(2060, NOW + 2 * DAY),  # after the window
            atcoder_contest('abc478', NOW + 2 * HOUR),
            ukiepc(date(2026, 10, 1)),  # no time yet
        ],
        now=NOW,
    )
    mark_all_synced(source)

    found = await source.occurrences(GUILD, server_settings(), NOW, NOW + DAY)

    assert [contest.subject_id for contest in found] == [
        'codeforces:2051',
        'atcoder:abc478',
    ]


async def test_an_occurrence_carries_everything_a_post_shows(
    repo: ContestRepo, source: ContestReminders
) -> None:
    await repo.add([codeforces_round(2051, S)], now=NOW)
    mark_all_synced(source)

    (found,) = await source.occurrences(GUILD, server_settings(), NOW, NOW + 7 * DAY)

    assert found == occurrence()


async def test_cancelled_contests_are_listed_for_their_notices(
    repo: ContestRepo, source: ContestReminders
) -> None:
    added = await repo.add_manual('Club contest', S, S + HOUR, None, now=NOW)
    await repo.cancel_manual(added.contest_id, now=NOW)

    (found,) = await source.occurrences(GUILD, server_settings(), NOW, NOW + 7 * DAY)

    assert found.subject_id == f'manual:{added.contest_id}'
    assert found.cancelled
    assert found.revision == 1


async def test_only_the_servers_platforms_are_listed(
    repo: ContestRepo, source: ContestReminders
) -> None:
    await repo.add(
        [codeforces_round(2051, S), atcoder_contest('abc478', S + HOUR)], now=NOW
    )
    mark_all_synced(source)
    settings = server_settings(platforms=('atcoder',))

    found = await source.occurrences(GUILD, settings, NOW, NOW + 7 * DAY)

    assert [contest.subject_id for contest in found] == ['atcoder:abc478']
    assert await source.occurrences(GUILD, server_settings(platforms=()), NOW, S) == []


async def test_a_contest_is_listed_at_the_time_an_admin_set(
    repo: ContestRepo, source: ContestReminders
) -> None:
    await repo.add([ukiepc()], now=NOW)
    mark_all_synced(source)
    window = (NOW, NOW + 7 * DAY)
    assert await source.occurrences(GUILD, server_settings(), *window) == []
    (stored,) = await repo.upcoming(NOW, platforms=['icpc'], limit=1)
    start = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    await repo.set_time(stored.contest_id, start, start + 5 * HOUR, by='1', now=NOW)

    (found,) = await source.occurrences(GUILD, server_settings(), *window)
    assert (found.subject_id, found.start, found.revision) == ('icpc:9584', start, 1)


async def test_a_platform_has_no_contests_until_the_bot_has_tried_to_sync_it(
    repo: ContestRepo, source: ContestReminders
) -> None:
    # Stored before the bot restarted: they may have moved since.
    await repo.add([codeforces_round(2051, S)], now=NOW)
    club = await repo.add_manual('Club contest', S, S + HOUR, None, now=NOW)
    window = (NOW, NOW + 7 * DAY)

    found = await source.occurrences(GUILD, server_settings(), *window)

    # The club's contests have no source to wait for.
    assert [contest.subject_id for contest in found] == [f'manual:{club.contest_id}']
    assert not source.is_synced('codeforces')

    source.mark_synced('codeforces')

    found = await source.occurrences(GUILD, server_settings(), *window)
    assert {contest.subject_id for contest in found} == {
        'codeforces:2051',
        f'manual:{club.contest_id}',
    }


async def test_a_contest_known_only_by_its_date_is_no_occurrence(
    repo: ContestRepo,
) -> None:
    await repo.add([ukiepc()], now=NOW)
    (stored,) = await repo.upcoming(NOW, platforms=['icpc'], limit=1)

    with pytest.raises(ValueError, match='icpc:9584 has no start time yet'):
        ContestOccurrence.from_contest(stored)


def test_platforms_have_names_for_posts() -> None:
    assert [platform_name(platform) for platform in PLATFORMS] == [
        'Codeforces',
        'AtCoder',
        'CodeChef',
        'LeetCode',
        'TopCoder',
        'ICPC',
        'Club',
    ]
    assert platform_name('hackerrank') == 'hackerrank'


@pytest.mark.parametrize(
    ('offset', 'heading'),
    [(HOUR, 'Starting soon'), (DAY, 'Starting soon'), (timedelta(0), 'Starting now')],
)
def test_a_reminder_gives_the_platform_start_duration_and_link(
    source: ContestReminders, offset: timedelta, heading: str
) -> None:
    message = source.render(reminder(offset, occurrence()))

    assert message == post(
        f'{heading}: Codeforces Round 1050 (Div. 1)',
        '\n'.join([PLATFORM, starts(), DURATION, LINK]),
        url=CF_PAGE,
    )


def test_a_reminder_leaves_out_what_is_not_known(source: ContestReminders) -> None:
    bare = occurrence(
        'manual:7', title='Club contest', platform='manual', end=None, url=None
    )

    assert source.render(reminder(HOUR, bare)) == post(
        'Starting soon: Club contest', '\n'.join(['**Platform:** Club', starts()])
    )


def test_contests_at_the_same_time_share_one_reminder(
    source: ContestReminders,
) -> None:
    div1 = occurrence()
    div2 = occurrence(
        'codeforces:2052',
        title='Codeforces Round 1050 (Div. 2)',
        url='https://example.org/round-(div-2)',
    )

    message = source.render(reminder(HOUR, div1, div2))

    assert message == post(
        'Starting soon: 2 contests',
        fields=(
            EmbedField(
                'Codeforces Round 1050 (Div. 1)',
                '\n'.join([PLATFORM, starts(), DURATION, LINK]),
            ),
            EmbedField(
                'Codeforces Round 1050 (Div. 2)',
                # Parentheses would end the link early.
                '\n'.join(
                    [
                        PLATFORM,
                        starts(),
                        DURATION,
                        '[Contest page](https://example.org/round-%28div-2%29)',
                    ]
                ),
            ),
        ),
    )
    assert source.render(reminder(timedelta(0), div1, div2)).title == (
        'Starting now: 2 contests'
    )


def test_a_reminder_of_the_most_contests_a_post_holds_fits_them_all(
    source: ContestReminders,
) -> None:
    # Once posted, every contest of the reminder counts as reminded of, so
    # Discord's limits must not drop any, however long their details.
    page = 'https://codeforces.com/' + 'p' * 177  # the longest link a field keeps
    contests = [
        occurrence(
            f'codeforces:{n}',
            title=f'{n} ' + 'T' * (TITLE_LIMIT - 2),
            end=S + 7 * DAY - MINUTE,
            url=page,
        )
        for n in range(MAX_GROUP)
    ]

    message = source.render(reminder(HOUR, *contests))

    assert message.title == f'Starting soon: {MAX_GROUP} contests'
    assert message.within_discord_limits() == message
    assert len(message.fields) == MAX_GROUP
    for n, field in enumerate(message.fields):
        assert field.name == f'{n} ' + 'T' * 97 + ELLIPSIS
        assert field.value == '\n'.join(
            [PLATFORM, starts(), '**Duration:** 6d 23h 59m', f'[Contest page]({page})']
        )


def test_a_link_too_long_for_a_reminder_of_several_contests_is_left_out() -> None:
    page = 'https://codeforces.com/' + 'p' * 178

    assert contest_link(page) is None
    assert contest_link(None) is None
    # A post about one contest links its title to the page instead.
    assert contest_details(occurrence(url=page)) == '\n'.join(
        [PLATFORM, starts(), DURATION]
    )


def test_any_occurrence_can_be_described() -> None:
    plain = Occurrence('contest', 'codeforces:2051', 'Round', S, None, None, 0)

    assert contest_details(plain) == starts()


def test_a_time_change_gives_the_new_and_the_previous_time(
    source: ContestReminders,
) -> None:
    moved = occurrence(start=S + 15 * MINUTE, end=S + 15 * MINUTE + ROUND, revision=1)
    notice = Notice(NoticeKind.MOVED, (moved,), previous_start=S)

    assert source.render(notice) == post(
        'Time changed: Codeforces Round 1050 (Div. 1)',
        '\n'.join(
            [
                PLATFORM,
                f'**New time:** {stamp(S + 15 * MINUTE, "F")} '
                f'({stamp(S + 15 * MINUTE, "R")})',
                f'**Previously:** {stamp(S, "F")}',
            ]
        ),
        url=CF_PAGE,
    )


def test_a_time_change_without_the_previous_time_gives_the_new_one(
    source: ContestReminders,
) -> None:
    notice = Notice(NoticeKind.MOVED, (occurrence(),))

    assert source.render(notice).description == (
        f'{PLATFORM}\n**New time:** {stamp(S, "F")} ({stamp(S, "R")})'
    )


def test_a_cancellation_gives_the_time_it_was_planned_for(
    source: ContestReminders,
) -> None:
    notice = Notice(NoticeKind.CANCELLED, (occurrence(revision=1),))

    # No link: the contest's page may be gone.
    assert source.render(notice) == post(
        'Cancelled: Codeforces Round 1050 (Div. 1)',
        f'{PLATFORM}\n**Was planned for:** {stamp(S, "F")}',
    )


def test_a_contest_back_on_gives_its_details(source: ContestReminders) -> None:
    notice = Notice(NoticeKind.REINSTATED, (occurrence(revision=2),))

    assert source.render(notice) == post(
        'Back on: Codeforces Round 1050 (Div. 1)',
        '\n'.join([PLATFORM, starts(), DURATION, LINK]),
        url=CF_PAGE,
    )


class FakeSource:
    """A ContestSource that lists whatever contests it is given."""

    def __init__(self, platform: str, *, complete: bool = True) -> None:
        self.name = platform
        self.platform = platform
        self._complete = complete
        self._contests: list[ContestInfo] = []

    def serve(self, *contests: ContestInfo) -> None:
        self._contests = list(contests)

    async def fetch(self) -> SourceSnapshot:
        return SourceSnapshot(list(self._contests), complete=self._complete)


class League:
    """The contests feature as the bot runs it, on the test's kcpc.db and clock.

    Fake Codeforces, AtCoder and ICPC sources, ContestSync, and the reminder
    engine with ContestReminders posting through FakePublisher. ``run_until``
    runs the bot's jobs minute by minute: every 5 minutes each source is
    synced (the fakes answer at once), with a tick at once if contests changed
    or a platform was synced for the first time; every minute the reminders
    job ticks. Admins act through ``repo``, after which the cog ticks.
    """

    def __init__(
        self,
        db: Database,
        clock: FakeClock,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
    ) -> None:
        self.clock = clock
        self.repo = ContestRepo(db, tz=LONDON)
        self.sync = ContestSync(db, self.repo, clock)
        self.codeforces = FakeSource('codeforces')
        self.atcoder = FakeSource('atcoder')
        self.icpc = FakeSource('icpc', complete=False)
        self.reminders = ContestReminders(self.repo)
        self.publisher = FakePublisher(guild_settings, ledger)
        self.engine = ReminderEngine(guild_settings, ledger, self.publisher, clock)
        self.engine.register(self.reminders)

    async def start(self) -> None:
        """What the bot does at startup: the reminders job ticks, then the
        sync jobs run, as they wait for the sites.
        """
        await self.engine.tick()
        await self.sync_all()

    async def sync_all(self) -> None:
        """What the sync jobs do (``KcpcContests._sync_sources``)."""
        remind = False
        for source in (self.codeforces, self.atcoder, self.icpc):
            remind = remind or not self.reminders.is_synced(source.platform)
            report = await self.sync.sync(source)
            self.reminders.mark_synced(source.platform)
            remind = remind or report.changed
        if remind:
            await self.engine.tick()

    async def run_until(self, when: datetime) -> None:
        while self.clock.now() < when:
            await self.clock.advance(MINUTE)
            if (self.clock.now() - NOW) % SYNC_INTERVAL == timedelta(0):
                await self.sync_all()
            await self.engine.tick()

    def posts(self, guild_id: int = GUILD) -> list[str]:
        """Each post in the guild, as '<kind> <contest> r<revision>: <title>'."""
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
def league(
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
) -> League:
    return League(db, clock, guild_settings, ledger)


async def follow(
    guild_settings: GuildSettingsRepo, guild_id: int = GUILD, **settings: object
) -> None:
    """Set the guild up for contests: on, with a channel, and ``settings``."""
    await guild_settings.update(
        guild_id, CONTESTS, enabled=True, channel_id=CHANNEL, **settings
    )


async def test_a_div_1_and_div_2_round_at_one_time_get_one_reminder(
    league: League, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)
    start = NOW + 90 * MINUTE
    league.codeforces.serve(
        codeforces_round(2052, start, division=2), codeforces_round(2051, start)
    )

    await league.start()
    await league.run_until(start + 10 * MINUTE)

    assert league.posts() == [
        '60m codeforces:2051 r0 + 60m codeforces:2052 r0: Starting soon: 2 contests'
    ]
    (sent,) = league.publisher.posts
    assert [field.name for field in sent.message.fields] == [
        'Codeforces Round 1050 (Div. 1)',
        'Codeforces Round 1050 (Div. 2)',
    ]
    assert sent.message.mention_role


async def test_ukiepc_is_reminded_of_once_an_admin_sets_its_time(
    league: League, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)
    league.icpc.serve(ukiepc(date(2026, 10, 3)))
    await league.start()
    await league.run_until(NOW + 30 * MINUTE)
    assert league.posts() == []

    # The day before, an admin sets it for 10:00 in London: 09:00 UTC.
    (stored,) = await league.repo.upcoming(clock.now(), platforms=['icpc'], limit=1)
    await clock.advance_to(datetime(2026, 10, 2, 18, 0, tzinfo=UTC))
    start = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
    await league.repo.set_time(
        stored.contest_id, start, start + 5 * HOUR, by='1', now=clock.now()
    )
    await league.engine.tick()
    await clock.advance_to(start - 90 * MINUTE)  # nothing is due overnight
    await league.run_until(start + 10 * MINUTE)

    assert league.posts() == [f'60m icpc:9584 r1: Starting soon: {UKIEPC_NAME}']


async def test_an_admin_moving_a_contest_after_its_reminder_gives_one_notice(
    league: League, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)
    start = NOW + 70 * MINUTE
    league.codeforces.serve(codeforces_round(2051, start))
    await league.start()
    await league.run_until(NOW + 15 * MINUTE)
    (stored,) = await league.repo.upcoming(
        clock.now(), platforms=['codeforces'], limit=1
    )

    # Codeforces still lists the old time; the admin's wins.
    new_start = start + 30 * MINUTE
    await league.repo.set_time(stored.contest_id, new_start, None, by='1', now=NOW)
    await league.engine.tick()
    await league.run_until(new_start + 10 * MINUTE)

    assert league.posts() == [
        '60m codeforces:2051 r0: Starting soon: Codeforces Round 1050 (Div. 1)',
        'moved codeforces:2051 r1: Time changed: Codeforces Round 1050 (Div. 1)',
    ]
    moved = league.publisher.posts[1].message
    assert moved.description is not None
    assert f'**Previously:** {stamp(start, "F")}' in moved.description


async def test_start_posts_go_out_only_where_they_are_on(
    league: League, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD, start_posts=True)
    await follow(guild_settings, OTHER_GUILD)
    start = NOW + 30 * MINUTE
    league.atcoder.serve(atcoder_contest('abc478', start))

    await league.start()
    await league.run_until(start + 15 * MINUTE)

    assert league.posts(GUILD) == [
        '60m atcoder:abc478 r0: Starting soon: AtCoder Beginner Contest 478',
        'start atcoder:abc478 r0: Starting now: AtCoder Beginner Contest 478',
    ]
    assert league.posts(OTHER_GUILD) == [
        '60m atcoder:abc478 r0: Starting soon: AtCoder Beginner Contest 478'
    ]


async def test_an_atcoder_contest_leaving_the_list_as_it_starts_is_not_cancelled(
    league: League, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)
    start = NOW + 70 * MINUTE
    league.atcoder.serve(atcoder_contest('abc478', start))
    await league.start()
    await league.run_until(start)

    # AtCoder lists only contests yet to start.
    league.atcoder.serve()
    await league.run_until(start + 2 * HOUR)

    assert league.posts() == [
        '60m atcoder:abc478 r0: Starting soon: AtCoder Beginner Contest 478'
    ]
    (record,) = await league.repo.source_records('atcoder')
    assert (record.status, record.revision, record.miss_count) == (
        ContestStatus.SCHEDULED,
        0,
        0,
    )


async def test_a_club_contest_removed_after_its_reminder_gets_one_notice(
    league: League, clock: FakeClock, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings)
    start = NOW + 70 * MINUTE
    await league.start()
    club = await league.repo.add_manual(
        'KCPC Autumn Contest', start, start + 2 * HOUR, None, now=clock.now()
    )
    await league.engine.tick()
    await league.run_until(NOW + 15 * MINUTE)

    await league.repo.cancel_manual(club.contest_id, now=clock.now())
    await league.engine.tick()
    await league.run_until(start + 10 * MINUTE)

    key = f'manual:{club.contest_id}'
    assert league.posts() == [
        f'60m {key} r0: Starting soon: KCPC Autumn Contest',
        f'cancelled {key} r1: Cancelled: KCPC Autumn Contest',
    ]


async def test_a_server_gets_nothing_for_a_platform_it_does_not_follow(
    league: League, guild_settings: GuildSettingsRepo
) -> None:
    await follow(guild_settings, GUILD)
    await follow(guild_settings, OTHER_GUILD, platforms=('atcoder', 'manual'))
    league.codeforces.serve(codeforces_round(2051, NOW + 30 * MINUTE))
    league.atcoder.serve(atcoder_contest('abc478', NOW + 40 * MINUTE))

    await league.start()
    await league.run_until(NOW + HOUR)

    assert league.posts(GUILD) == [
        '60m codeforces:2051 r0: Starting soon: Codeforces Round 1050 (Div. 1)',
        '60m atcoder:abc478 r0: Starting soon: AtCoder Beginner Contest 478',
    ]
    assert league.posts(OTHER_GUILD) == [
        '60m atcoder:abc478 r0: Starting soon: AtCoder Beginner Contest 478'
    ]


async def test_reminders_wait_for_the_first_sync_since_the_bot_started(
    league: League,
    db: Database,
    clock: FakeClock,
    guild_settings: GuildSettingsRepo,
) -> None:
    await follow(guild_settings)
    # Synced before the bot started, so the round may have moved since.
    before = FakeSource('codeforces')
    before.serve(codeforces_round(2051, NOW + 30 * MINUTE))
    await ContestSync(db, ContestRepo(db, tz=LONDON), clock).sync(before)
    league.codeforces.serve(codeforces_round(2051, NOW + 30 * MINUTE))

    await league.engine.tick()  # the reminders job, which starts first

    assert league.posts() == []

    # Nothing changed, but the reminder that waited goes out at once.
    await league.sync_all()

    assert league.posts() == [
        '60m codeforces:2051 r0: Starting soon: Codeforces Round 1050 (Div. 1)'
    ]
