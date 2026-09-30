"""Tests for tle.kcpc.core.scheduler, driven by FakeClock on a real kcpc.db.

Jobs do their bookkeeping on aiosqlite's worker thread. FakeClock lets a job it
wakes finish that before time moves on, but a job that is starting, or that a
test has just released, finishes it in its own time. So a test waits (in real
time) until the job is parked again, waiting for a known next run, before it
looks at the results or moves the clock on.
"""

import asyncio
import itertools
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, Clock, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.schedule import Every, Schedule, Weekly
from tle.kcpc.core.scheduler import JobStatus, ScheduledJob, Scheduler
from tle.kcpc.core.timeutil import from_epoch, zone

SCHEDULER_LOGGER = 'tle.kcpc.core.scheduler'

# The clock fixture's start, a Thursday.
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOURLY = Every(timedelta(hours=1))
EVERY_2M = Every(timedelta(minutes=2))
DAILY = Every(timedelta(days=1))
FRIDAY_NOON = Weekly(4, time(12, 0), zone('Europe/London'))
LAST_FRIDAY = datetime(2026, 9, 25, 11, 0, tzinfo=UTC)  # noon in London (BST)
NEXT_FRIDAY = datetime(2026, 10, 2, 11, 0, tzinfo=UTC)
GRACE = timedelta(minutes=30)
ALWAYS = 1_000_000  # a Recorder with this many failures never succeeds
STOP_TIMEOUT = 10  # real seconds; a healthy stop takes a tiny fraction of that


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 1) -> datetime:
    """A time in October 2026, in UTC."""
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


@dataclass(frozen=True)
class Call:
    slot: datetime
    at: datetime  # the clock's time when the handler was called


class Recorder:
    """A job handler that records its calls, and can be made to fail or to block."""

    def __init__(self, clock: Clock, *, failures: int = 0) -> None:
        self._clock = clock
        self._failures = failures  # how many of the first calls raise
        self.gate: asyncio.Event | None = None  # when set, calls wait for it to open
        self.calls: list[Call] = []
        self.errors: list[Exception] = []
        self.cancelled = 0
        self.active = 0
        self.most_active = 0

    async def __call__(self, slot: datetime) -> None:
        self.calls.append(Call(slot, self._clock.now()))
        number = len(self.calls)
        self.active += 1
        self.most_active = max(self.most_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1
        if number <= self._failures:
            error = RuntimeError(f'call {number} failed')
            self.errors.append(error)
            raise error

    @property
    def slots(self) -> list[datetime]:
        return [call.slot for call in self.calls]

    @property
    def times(self) -> list[datetime]:
        return [call.at for call in self.calls]


async def noop(slot: datetime) -> None:
    pass


@dataclass(frozen=True)
class StartingAt:
    """Every ``interval`` from ``first`` on, with no slot before ``first``."""

    first: datetime
    interval: timedelta

    def next_after(self, t: datetime) -> datetime:
        return self.first if t < self.first else self._every.next_after(t)

    def prev_at_or_before(self, t: datetime) -> datetime | None:
        return None if t < self.first else self._every.prev_at_or_before(t)

    def describe(self) -> str:
        return f'every {self.interval} from {self.first}'

    @property
    def _every(self) -> Every:
        return Every(self.interval, self.first)


class FlakySchedule:
    """``inner``, except that ``next_after`` raises the first ``failures`` times."""

    def __init__(self, inner: Schedule, *, failures: int) -> None:
        self._inner = inner
        self._failures = failures

    def next_after(self, t: datetime) -> datetime:
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError('schedule bug')
        return self._inner.next_after(t)

    def prev_at_or_before(self, t: datetime) -> datetime | None:
        return self._inner.prev_at_or_before(t)

    def describe(self) -> str:
        return self._inner.describe()


@dataclass(frozen=True)
class Stored:
    """A job_state row."""

    last_slot: datetime | None
    failures: int
    last_error: str | None


async def stored(db: Database, job: str) -> Stored | None:
    row = await db.fetchone(
        'SELECT last_slot, failures, last_error FROM job_state WHERE job = ?', (job,)
    )
    if row is None:
        return None
    last_slot = None if row['last_slot'] is None else from_epoch(row['last_slot'])
    return Stored(last_slot, row['failures'], row['last_error'])


def status_of(scheduler: Scheduler, name: str) -> JobStatus:
    [status] = [status for status in scheduler.status() if status.name == name]
    return status


async def eventually(condition: Callable[[], bool], what: str) -> None:
    """Wait (up to 10 s of real time) until ``condition()`` holds."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f'Timed out waiting until {what}')


async def parked(scheduler: Scheduler, name: str, until: datetime) -> None:
    """Wait until the job is idle, waiting to run at ``until`` (a slot or a retry)."""
    await eventually(
        lambda: status_of(scheduler, name).next_run == until,
        f'job {name} waits for {until}',
    )


def logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    """The scheduler's messages logged at exactly ``level``."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == SCHEDULER_LOGGER and record.levelno == level
    ]


class Schedulers:
    """Makes a test's schedulers on its database, and stops them all at the end."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock
        self._made: list[Scheduler] = []

    def new(
        self,
        *jobs: ScheduledJob,
        clock: Clock | None = None,
        ready: Callable[[], Awaitable[object]] | None = None,
        start: bool = True,
    ) -> Scheduler:
        scheduler = Scheduler(
            self._db, self._clock if clock is None else clock, ready=ready
        )
        for job in jobs:
            scheduler.add(job)
        if start:
            scheduler.start()
        self._made.append(scheduler)
        return scheduler

    async def stop_all(self) -> None:
        for scheduler in self._made:
            await scheduler.stop()


@pytest.fixture
async def schedulers(db: Database, clock: FakeClock) -> AsyncIterator[Schedulers]:
    assert clock.now() == START  # the times in these tests are spelled out from it
    made = Schedulers(db, clock)
    yield made
    # Bounded, so a stop that never ends fails the test instead of hanging the run.
    await asyncio.wait_for(made.stop_all(), STOP_TIMEOUT)


@pytest.fixture(autouse=True)
def scheduler_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)


@pytest.mark.parametrize(
    ('options', 'message'),
    [
        ({'name': ''}, 'needs a name'),
        ({'catch_up_grace': timedelta(seconds=-1)}, 'must not be negative'),
        (
            {'retry_delays': (timedelta(minutes=5), timedelta(0))},
            'must all be positive',
        ),
        ({'run_on_start': True}, 'run_on_start is for non-persistent jobs'),
        ({'persistent': False, 'catch_up_grace': GRACE}, 'for persistent jobs only'),
    ],
)
def test_a_job_rejects_options_that_cannot_work(
    options: dict[str, Any], message: str
) -> None:
    fields: dict[str, Any] = {'name': 'job', 'schedule': HOURLY, 'handler': noop}
    with pytest.raises(ValueError, match=message):
        ScheduledJob(**{**fields, **options})


async def test_add_rejects_a_name_already_taken(schedulers: Schedulers) -> None:
    scheduler = schedulers.new(ScheduledJob('job', HOURLY, noop), start=False)
    with pytest.raises(ValueError, match="'job' is already scheduled"):
        scheduler.add(ScheduledJob('job', EVERY_2M, noop, persistent=False))


def test_persistent_jobs_need_a_database(clock: FakeClock) -> None:
    scheduler = Scheduler(None, clock)
    scheduler.add(ScheduledJob('reconcile', EVERY_2M, noop, persistent=False))
    with pytest.raises(ValueError, match='no database'):
        scheduler.add(ScheduledJob('weekly', FRIDAY_NOON, noop))
    assert [status.name for status in scheduler.status()] == ['reconcile']


async def test_first_start_counts_the_latest_slot_as_done_without_running_it(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    scheduler = schedulers.new(ScheduledJob('weekly', FRIDAY_NOON, handler))

    await parked(scheduler, 'weekly', NEXT_FRIDAY)
    assert handler.calls == []
    assert await stored(db, 'weekly') == Stored(LAST_FRIDAY, 0, None)
    assert logged(caplog, logging.INFO) == [
        'Job weekly is starting for the first time; it will run from its next slot on'
    ]


async def test_a_job_whose_schedule_has_not_begun_first_runs_at_its_first_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    schedule = StartingAt(at(14), timedelta(hours=1))
    scheduler = schedulers.new(ScheduledJob('later', schedule, handler))

    await parked(scheduler, 'later', at(14))
    assert await stored(db, 'later') == Stored(None, 0, None)

    await clock.advance_to(at(14))
    await parked(scheduler, 'later', at(15))
    assert handler.calls == [Call(at(14), at(14))]
    assert await stored(db, 'later') == Stored(at(14), 0, None)


async def test_each_slot_runs_at_exactly_its_time_across_a_clock_change(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    scheduler = schedulers.new(ScheduledJob('weekly', FRIDAY_NOON, handler))
    # Noon in London is 11:00 UTC until the clocks go back on 25 October.
    fridays = [at(11, day=day) for day in (2, 9, 16, 23)] + [
        at(12, day=30),
        datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
    ]

    for runs, (slot, following) in enumerate(itertools.pairwise(fridays)):
        await parked(scheduler, 'weekly', slot)
        await clock.advance_to(slot - timedelta(seconds=1))
        assert len(handler.calls) == runs  # not a second early
        await clock.advance_to(slot)
        await parked(scheduler, 'weekly', following)
        assert handler.calls[-1] == Call(slot, slot)
        assert await stored(db, 'weekly') == Stored(slot, 0, None)
    assert handler.slots == fridays[:-1]


async def test_one_long_advance_runs_each_slot_on_the_way_at_its_time(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await clock.advance(timedelta(hours=3, minutes=10))

    # Each run was saved, and the next slot awaited, before time moved on: no
    # slot was passed over or run late, as after a jump.
    assert handler.calls == [
        Call(at(13), at(13)),
        Call(at(14), at(14)),
        Call(at(15), at(15)),
    ]
    assert status_of(scheduler, 'hourly').next_run == at(16)
    assert await stored(db, 'hourly') == Stored(at(15), 0, None)


async def test_after_downtime_only_the_latest_missed_slot_runs_if_within_grace(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    # A grace longer than the period, so each missed slot is itself within grace.
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(hours=6))
    first = schedulers.new(job)
    await parked(first, 'hourly', at(13))
    assert await stored(db, 'hourly') == Stored(at(12), 0, None)
    await first.stop()

    await clock.advance_to(at(15, 10))  # offline through 13:00, 14:00 and 15:00
    second = schedulers.new(job)

    await parked(second, 'hourly', at(16))
    assert handler.calls == [Call(at(15), at(15, 10))]
    assert await stored(db, 'hourly') == Stored(at(15), 0, None)
    assert (
        'Job hourly is catching up on its 2026-10-01 15:00:00+00:00 slot, 10m late'
        in logged(caplog, logging.INFO)
    )


async def test_after_downtime_a_slot_exactly_at_the_end_of_its_grace_still_runs(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    first = schedulers.new(job)
    await parked(first, 'hourly', at(13))
    await first.stop()

    await clock.advance_to(at(15, 30))
    second = schedulers.new(job)
    await parked(second, 'hourly', at(16))
    assert handler.calls == [Call(at(15), at(15, 30))]
    assert await stored(db, 'hourly') == Stored(at(15), 0, None)


async def test_after_downtime_a_slot_beyond_grace_is_skipped_but_the_next_one_runs(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    first = schedulers.new(job)
    await parked(first, 'hourly', at(13))
    await first.stop()

    await clock.advance_to(at(15, 30, 1))  # a second too late for 15:00
    second = schedulers.new(job)

    await parked(second, 'hourly', at(16))
    assert handler.calls == []
    assert await stored(db, 'hourly') == Stored(at(15), 0, None)
    assert logged(caplog, logging.WARNING) == [
        'Job hourly skipped its 2026-10-01 15:00:00+00:00 slot: it is 30m 1s late, '
        'and may run at most 30m late'
    ]

    await clock.advance_to(at(16))
    await parked(second, 'hourly', at(17))
    assert handler.calls == [Call(at(16), at(16))]


async def test_waking_late_runs_only_the_latest_missed_slot_and_only_within_grace(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await clock.jump(timedelta(hours=3, minutes=10))  # suspended until 15:10
    await parked(scheduler, 'hourly', at(16))
    assert handler.calls == [Call(at(15), at(15, 10))]
    # 13:00 and 14:00 were passed over for 15:00, not skipped one by one.
    assert logged(caplog, logging.WARNING) == []

    await clock.jump(timedelta(minutes=95))  # suspended until 16:45
    await parked(scheduler, 'hourly', at(17))
    assert len(handler.calls) == 1  # 16:00 was too late
    assert await stored(db, 'hourly') == Stored(at(16), 0, None)
    assert logged(caplog, logging.WARNING) == [
        'Job hourly skipped its 2026-10-01 16:00:00+00:00 slot: it is 45m late, '
        'and may run at most 30m late'
    ]


async def test_waking_up_to_a_minute_late_still_counts_as_on_time(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    scheduler = schedulers.new(ScheduledJob('hourly', HOURLY, handler))
    await parked(scheduler, 'hourly', at(13))

    await clock.advance_to(at(12, 59, 50))
    await clock.jump(timedelta(seconds=70))  # wakes at 13:01:00, a minute late
    await parked(scheduler, 'hourly', at(14))
    assert handler.calls == [Call(at(13), at(13, 1))]

    await clock.advance_to(at(13, 59, 50))
    await clock.jump(timedelta(seconds=71))  # wakes at 14:01:01
    await parked(scheduler, 'hourly', at(15))
    assert len(handler.calls) == 1
    assert await stored(db, 'hourly') == Stored(at(14), 0, None)
    assert logged(caplog, logging.WARNING) == [
        'Job hourly skipped its 2026-10-01 14:00:00+00:00 slot: it is 1m 1s late, '
        'and may run at most 1m late'
    ]


async def test_a_wake_up_exactly_a_minute_late_runs_the_slot_it_woke_for(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('minutely', Every(timedelta(minutes=1)), handler)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'minutely', at(12, 1))

    await clock.advance_to(at(12, 0, 30))
    await clock.jump(timedelta(seconds=90))  # wakes at 12:02, when 12:02 is due too
    await parked(scheduler, 'minutely', at(12, 3))
    assert handler.calls == [Call(at(12, 1), at(12, 2)), Call(at(12, 2), at(12, 2))]


async def test_after_a_run_overruns_later_slots_only_the_latest_within_grace_runs(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    handler.gate = asyncio.Event()
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await clock.advance_to(at(13))
    await eventually(lambda: status_of(scheduler, 'hourly').running, 'the run starts')
    await clock.advance_to(at(15, 10))  # the 13:00 run goes on until 15:10
    handler.gate.set()

    await parked(scheduler, 'hourly', at(16))
    assert handler.calls == [Call(at(13), at(13)), Call(at(15), at(15, 10))]
    assert await stored(db, 'hourly') == Stored(at(15), 0, None)


async def test_a_failed_slot_is_retried_after_5_then_15_minutes_until_it_succeeds(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock, failures=2)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(hours=6))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await clock.advance_to(at(13))
    await parked(scheduler, 'hourly', at(13, 5))
    assert await stored(db, 'hourly') == Stored(
        at(12), 1, 'RuntimeError: call 1 failed'
    )
    await clock.advance_to(at(13, 4, 59))
    assert len(handler.calls) == 1  # not a second early

    await clock.advance_to(at(13, 5))
    await parked(scheduler, 'hourly', at(13, 20))
    assert await stored(db, 'hourly') == Stored(
        at(12), 2, 'RuntimeError: call 2 failed'
    )

    await clock.advance_to(at(13, 20))
    await parked(scheduler, 'hourly', at(14))
    assert handler.calls == [
        Call(at(13), at(13)),
        Call(at(13), at(13, 5)),
        Call(at(13), at(13, 20)),
    ]
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)
    # Only the job starting to fail is a warning.
    slot = '2026-10-01 13:00:00+00:00'
    assert logged(caplog, logging.WARNING) == [f'Job hourly failed on its {slot} slot']
    assert logged(caplog, logging.INFO)[1:] == [
        f'Job hourly will retry its {slot} slot at 2026-10-01 13:05:00+00:00',
        f'Job hourly failed on its {slot} slot',
        f'Job hourly will retry its {slot} slot at 2026-10-01 13:20:00+00:00',
        'Job hourly succeeded after 2 failed runs',
    ]


async def test_retries_repeat_the_last_delay_until_grace_runs_out(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock, failures=ALWAYS)
    job = ScheduledJob('daily', DAILY, handler, catch_up_grace=timedelta(hours=2))
    scheduler = schedulers.new(job)
    # Delays of 5, 15, 30, 30 and 30 minutes; one more would end after 02:00.
    slot = at(0, day=2)
    attempts = [slot + timedelta(minutes=m) for m in (0, 5, 20, 50, 80, 110)]

    await parked(scheduler, 'daily', slot)
    for attempt, next_run in itertools.pairwise([*attempts, at(0, day=3)]):
        await clock.advance_to(attempt)
        await parked(scheduler, 'daily', next_run)
    assert handler.times == attempts
    assert await stored(db, 'daily') == Stored(slot, 6, 'RuntimeError: call 6 failed')


async def test_a_slot_given_up_beyond_grace_is_not_run_again_after_a_restart(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock, failures=ALWAYS)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(minutes=20))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    # The retry at 13:20 is just within grace; the next would be at 13:50.
    for attempt, next_run in [
        (at(13), at(13, 5)),
        (at(13, 5), at(13, 20)),
        (at(13, 20), at(14)),
    ]:
        await clock.advance_to(attempt)
        await parked(scheduler, 'hourly', next_run)
    assert handler.times == [at(13), at(13, 5), at(13, 20)]
    assert await stored(db, 'hourly') == Stored(
        at(13), 3, 'RuntimeError: call 3 failed'
    )
    [error] = [
        record
        for record in caplog.records
        if record.name == SCHEDULER_LOGGER and record.levelno == logging.ERROR
    ]
    assert error.getMessage() == (
        'Job hourly gave up on its 2026-10-01 13:00:00+00:00 slot after 3 failed '
        'attempts'
    )
    assert error.exc_info is not None and error.exc_info[1] is handler.errors[-1]

    # Restarting while 13:00 would still be within its grace does not bring it
    # back.
    await scheduler.stop()
    restarted = schedulers.new(job)
    await parked(restarted, 'hourly', at(14))
    assert len(handler.calls) == 3


async def test_a_retry_that_wakes_up_beyond_grace_gives_the_slot_up(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock, failures=2)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(hours=1))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await clock.advance_to(at(13))
    await parked(scheduler, 'hourly', at(13, 5))
    await clock.jump(timedelta(minutes=25))  # the retry wakes at 13:25, within grace
    await parked(scheduler, 'hourly', at(13, 40))
    await clock.jump(timedelta(minutes=65))  # the next wakes at 14:30, too late
    await parked(scheduler, 'hourly', at(15))

    # 13:00 was given up at 14:30, when 14:00 was only 30 minutes late.
    assert handler.calls == [
        Call(at(13), at(13)),
        Call(at(13), at(13, 25)),
        Call(at(14), at(14, 30)),
    ]
    assert logged(caplog, logging.ERROR) == [
        'Job hourly gave up on its 2026-10-01 13:00:00+00:00 slot after 2 failed '
        'attempts'
    ]
    assert await stored(db, 'hourly') == Stored(at(14), 0, None)


async def test_a_retry_woken_under_a_minute_late_still_runs_even_past_grace(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock, failures=2)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(minutes=20))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))
    for attempt, next_run in [(at(13), at(13, 5)), (at(13, 5), at(13, 20))]:
        await clock.advance_to(attempt)
        await parked(scheduler, 'hourly', next_run)

    # The second retry is due right at the end of grace, and wakes 30s after it.
    await clock.advance_to(at(13, 19, 50))
    await clock.jump(timedelta(seconds=40))
    await parked(scheduler, 'hourly', at(14))
    assert handler.times == [at(13), at(13, 5), at(13, 20, 30)]
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)


async def test_a_late_retry_of_a_slot_run_slot_did_meanwhile_is_quietly_dropped(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock, failures=1)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(hours=1))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))
    await clock.advance_to(at(13))
    await parked(scheduler, 'hourly', at(13, 5))

    assert await scheduler.run_slot('hourly', at(13)) == at(13)
    await clock.jump(timedelta(minutes=70))  # the retry wakes at 14:10, too late
    await parked(scheduler, 'hourly', at(15))
    assert handler.slots == [at(13), at(13), at(14)]
    assert logged(caplog, logging.ERROR) == []
    assert await stored(db, 'hourly') == Stored(at(14), 0, None)


@pytest.mark.parametrize(
    'options',
    [{}, {'catch_up_grace': timedelta(hours=6), 'retry_delays': ()}],
    ids=['no grace', 'no retry delays'],
)
async def test_a_failed_slot_with_no_room_for_a_retry_is_given_up_at_once(
    options: dict[str, Any],
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock, failures=ALWAYS)
    scheduler = schedulers.new(ScheduledJob('hourly', HOURLY, handler, **options))
    await parked(scheduler, 'hourly', at(13))

    await clock.advance_to(at(13))
    await parked(scheduler, 'hourly', at(14))
    assert handler.times == [at(13)]
    assert await stored(db, 'hourly') == Stored(
        at(13), 1, 'RuntimeError: call 1 failed'
    )
    assert len(logged(caplog, logging.ERROR)) == 1


async def test_a_new_scheduler_continues_from_the_stored_last_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    first = schedulers.new(job)
    await parked(first, 'hourly', at(13))
    await clock.advance_to(at(13))
    await parked(first, 'hourly', at(14))
    await first.stop()

    await clock.advance_to(at(13, 30))
    second = schedulers.new(job)
    await parked(second, 'hourly', at(14))
    assert handler.slots == [at(13)]  # 13:00 is not run again
    assert status_of(second, 'hourly').last_slot == at(13)

    await clock.advance_to(at(14))
    await parked(second, 'hourly', at(15))
    assert handler.calls[1:] == [Call(at(14), at(14))]
    assert await stored(db, 'hourly') == Stored(at(14), 0, None)


async def test_after_a_jump_a_non_persistent_job_runs_once_not_once_per_missed_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    await clock.advance(timedelta(minutes=1))
    handler = Recorder(clock)
    job = ScheduledJob(
        'reconcile', EVERY_2M, handler, persistent=False, run_on_start=True
    )
    scheduler = schedulers.new(job)
    await parked(scheduler, 'reconcile', at(12, 2))
    assert handler.calls == [Call(at(12), at(12, 1))]  # its latest slot, at once

    await clock.jump(timedelta(hours=3))  # 90 slots go by, e.g. while suspended
    await parked(scheduler, 'reconcile', at(15, 2))
    assert handler.calls[1:] == [Call(at(12, 2), at(15, 1))]

    await clock.advance_to(at(15, 2))
    await parked(scheduler, 'reconcile', at(15, 4))
    assert handler.slots[2:] == [at(15, 2)]
    assert await stored(db, 'reconcile') is None


async def test_a_non_persistent_job_keeps_its_state_in_memory_and_is_not_retried(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock, failures=1)
    job = ScheduledJob('reconcile', EVERY_2M, handler, persistent=False)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'reconcile', at(12, 2))
    assert handler.calls == []  # no run on start

    await clock.advance_to(at(12, 2))
    await parked(scheduler, 'reconcile', at(12, 4))  # no retry before then
    status = status_of(scheduler, 'reconcile')
    assert (status.last_slot, status.failures, status.last_error) == (
        None,
        1,
        'RuntimeError: call 1 failed',
    )

    await clock.advance_to(at(12, 4))
    await parked(scheduler, 'reconcile', at(12, 6))
    status = status_of(scheduler, 'reconcile')
    assert (status.last_slot, status.failures, status.last_error) == (
        at(12, 4),
        0,
        None,
    )
    assert handler.times == [at(12, 2), at(12, 4)]
    assert await stored(db, 'reconcile') is None


async def test_without_a_past_slot_the_latest_slot_means_now_in_whole_seconds(
    schedulers: Schedulers,
) -> None:
    clock = FakeClock(START + timedelta(microseconds=250_000))
    handler = Recorder(clock)
    schedule = StartingAt(at(14), timedelta(hours=1))
    scheduler = schedulers.new(
        ScheduledJob('later', schedule, handler, persistent=False, run_on_start=True),
        clock=clock,
    )
    await parked(scheduler, 'later', at(14))
    assert handler.slots == [START]
    assert await scheduler.run_slot('later') == START
    assert handler.slots == [START, START]


async def test_run_slot_defaults_to_the_latest_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    scheduler = schedulers.new(
        ScheduledJob('weekly', FRIDAY_NOON, handler), start=False
    )

    assert await scheduler.run_slot('weekly') == LAST_FRIDAY
    assert handler.calls == [Call(LAST_FRIDAY, START)]
    assert await stored(db, 'weekly') == Stored(LAST_FRIDAY, 0, None)


async def test_run_slot_runs_once_even_if_the_slot_is_done_and_reraises_its_error(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock, failures=1)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=timedelta(hours=6))
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))  # the first start counted 12:00 done

    with pytest.raises(RuntimeError) as raised:
        await scheduler.run_slot('hourly')
    assert raised.value is handler.errors[0]
    assert handler.calls == [Call(at(12), at(12))]
    assert await stored(db, 'hourly') == Stored(
        at(12), 1, 'RuntimeError: call 1 failed'
    )

    await clock.advance_to(at(12, 59, 59))  # past when a retry would have been
    assert len(handler.calls) == 1
    assert await scheduler.run_slot('hourly') == at(12)
    assert await stored(db, 'hourly') == Stored(at(12), 0, None)


async def test_run_slot_never_moves_the_last_slot_back(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    scheduler = schedulers.new(ScheduledJob('hourly', HOURLY, handler), start=False)

    assert await scheduler.run_slot('hourly', at(13)) == at(13)
    assert await scheduler.run_slot('hourly', at(11)) == at(11)
    assert handler.slots == [at(13), at(11)]
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)


async def test_run_slot_takes_any_aware_time_and_drops_fractions_of_a_second(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('reconcile', HOURLY, handler, persistent=False)
    scheduler = schedulers.new(job, start=False)
    paris = datetime(2026, 10, 1, 15, 0, 0, 750_000, tzinfo=zone('Europe/Paris'))

    assert await scheduler.run_slot('reconcile', paris) == at(13)
    assert handler.slots == [at(13)]
    assert handler.slots[0].tzinfo is UTC


async def test_run_slot_rejects_unknown_jobs_and_naive_times(
    schedulers: Schedulers,
) -> None:
    job = ScheduledJob('reconcile', HOURLY, noop, persistent=False)
    scheduler = schedulers.new(job, start=False)
    with pytest.raises(KeyError):
        await scheduler.run_slot('unknown')
    with pytest.raises(ValueError):
        await scheduler.run_slot('reconcile', datetime(2026, 10, 1, 12, 0))


async def test_a_scheduled_run_waits_for_a_run_slot_of_the_same_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock, failures=1)
    handler.gate = asyncio.Event()
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))
    await clock.advance_to(at(12, 59))
    manual = asyncio.create_task(scheduler.run_slot('hourly', at(13)))
    await eventually(lambda: len(handler.calls) == 1, 'run_slot calls the handler')

    await clock.advance_to(at(13))  # the scheduled run of 13:00 wakes up
    assert status_of(scheduler, 'hourly').next_run is None
    assert len(handler.calls) == 1  # it is waiting for the lock

    handler.gate.set()
    with pytest.raises(RuntimeError, match='call 1 failed'):
        await manual
    # run_slot failed, so the scheduled run still runs the slot, after it.
    await parked(scheduler, 'hourly', at(14))
    assert handler.calls == [Call(at(13), at(12, 59)), Call(at(13), at(13))]
    assert handler.most_active == 1
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)


async def test_a_scheduled_run_does_not_repeat_a_slot_run_slot_did_meanwhile(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    handler.gate = asyncio.Event()
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))
    await clock.advance_to(at(12, 59))
    manual = asyncio.create_task(scheduler.run_slot('hourly', at(13)))
    await eventually(lambda: len(handler.calls) == 1, 'run_slot calls the handler')
    await clock.advance_to(at(13))

    handler.gate.set()
    assert await manual == at(13)
    await parked(scheduler, 'hourly', at(14))
    assert handler.calls == [Call(at(13), at(12, 59))]
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)
    assert (
        'Not running job hourly for its 2026-10-01 13:00:00+00:00 slot: it is '
        'already done' in logged(caplog, logging.INFO)
    )


async def test_remove_cancels_a_job_and_frees_its_name(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    removed = Recorder(clock)
    kept = Recorder(clock)
    scheduler = schedulers.new(
        ScheduledJob('removed', HOURLY, removed, persistent=False),
        ScheduledJob('kept', HOURLY, kept, persistent=False),
    )
    await parked(scheduler, 'removed', at(13))
    await parked(scheduler, 'kept', at(13))

    await scheduler.remove('removed')
    assert [status.name for status in scheduler.status()] == ['kept']
    assert clock.pending_sleepers == 1
    await scheduler.remove('removed')  # now unknown: a no-op

    await clock.advance_to(at(13))
    await parked(scheduler, 'kept', at(14))
    assert removed.calls == []
    assert kept.slots == [at(13)]

    scheduler.add(ScheduledJob('removed', HOURLY, removed, persistent=False))
    await parked(scheduler, 'removed', at(14))


async def test_stop_cancels_every_job_and_start_resumes_them(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    hourly = Recorder(clock)
    reconcile = Recorder(clock)
    scheduler = schedulers.new(
        ScheduledJob('hourly', HOURLY, hourly, catch_up_grace=GRACE),
        ScheduledJob(
            'reconcile', HOURLY, reconcile, persistent=False, run_on_start=True
        ),
    )
    assert scheduler.running
    await parked(scheduler, 'hourly', at(13))
    await parked(scheduler, 'reconcile', at(13))

    await scheduler.stop()
    await scheduler.stop()  # idempotent
    assert not scheduler.running
    assert clock.pending_sleepers == 0
    assert [status.next_run for status in scheduler.status()] == [None, None]
    await clock.advance_to(at(13, 10))
    assert hourly.calls == []
    assert reconcile.calls == [Call(at(12), at(12))]  # only its run on start

    scheduler.start()
    assert scheduler.running
    await parked(scheduler, 'hourly', at(14))
    await parked(scheduler, 'reconcile', at(14))
    assert hourly.calls == [Call(at(13), at(13, 10))]  # caught up within grace
    assert reconcile.calls[1:] == [Call(at(13), at(13, 10))]  # run on start again
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)


async def test_start_is_idempotent_and_starts_jobs_added_later_at_once(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    first = Recorder(clock)
    later = Recorder(clock)
    scheduler = schedulers.new(ScheduledJob('first', HOURLY, first, persistent=False))
    scheduler.start()
    scheduler.add(ScheduledJob('later', HOURLY, later, persistent=False))
    await parked(scheduler, 'first', at(13))
    await parked(scheduler, 'later', at(13))
    assert clock.pending_sleepers == 2  # one task per job

    await clock.advance_to(at(13))
    await parked(scheduler, 'first', at(14))
    await parked(scheduler, 'later', at(14))
    assert first.slots == [at(13)]
    assert later.slots == [at(13)]


async def test_stop_cancels_a_running_handler_without_recording_its_slot(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    handler = Recorder(clock)
    handler.gate = asyncio.Event()
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))
    await clock.advance_to(at(13))
    await eventually(lambda: status_of(scheduler, 'hourly').running, 'the run starts')

    await scheduler.stop()
    assert handler.cancelled == 1
    status = status_of(scheduler, 'hourly')
    assert (status.running, status.failures) == (False, 0)
    assert await stored(db, 'hourly') == Stored(at(12), 0, None)


async def test_a_handler_can_remove_its_own_job(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    scheduler = schedulers.new(start=False)
    job_tasks: list[asyncio.Task[Any]] = []
    removed: list[datetime] = []

    async def remove_own_job(slot: datetime) -> None:
        task = asyncio.current_task()
        assert task is not None
        job_tasks.append(task)
        await scheduler.remove('once')
        removed.append(slot)  # remove() returns; the task ends at its next wait

    scheduler.add(
        ScheduledJob(
            'once', HOURLY, remove_own_job, persistent=False, run_on_start=True
        )
    )
    scheduler.start()
    await eventually(lambda: bool(job_tasks), 'the handler runs')
    [job_task] = job_tasks
    await asyncio.wait({job_task}, timeout=5)
    assert removed == [START]
    assert job_task.cancelled()
    assert scheduler.status() == []
    assert clock.pending_sleepers == 0


async def test_jobs_wait_until_ready_before_doing_anything(
    schedulers: Schedulers, db: Database, clock: FakeClock
) -> None:
    ready = asyncio.Event()
    weekly = Recorder(clock)
    reconcile = Recorder(clock)
    scheduler = schedulers.new(
        ScheduledJob('weekly', FRIDAY_NOON, weekly),
        ScheduledJob(
            'reconcile', EVERY_2M, reconcile, persistent=False, run_on_start=True
        ),
        ready=ready.wait,
    )

    await clock.advance(timedelta(hours=1))
    await asyncio.sleep(0.05)  # time for anything that was not waiting to happen
    assert weekly.calls == reconcile.calls == []
    assert await stored(db, 'weekly') is None
    assert [status.next_run for status in scheduler.status()] == [None, None]

    ready.set()
    await parked(scheduler, 'reconcile', at(13, 2))
    await parked(scheduler, 'weekly', NEXT_FRIDAY)
    assert reconcile.calls == [Call(at(13), at(13))]
    assert await stored(db, 'weekly') == Stored(LAST_FRIDAY, 0, None)


async def test_a_failing_ready_check_is_tried_again_a_minute_later(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    checks = 0

    async def ready() -> None:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError('not ready')

    handler = Recorder(clock)
    job = ScheduledJob(
        'reconcile', HOURLY, handler, persistent=False, run_on_start=True
    )
    scheduler = schedulers.new(job, ready=ready)
    await eventually(lambda: clock.next_deadline == at(12, 1), 'the job pauses')
    assert handler.calls == []

    await clock.advance_to(at(12, 1))
    await parked(scheduler, 'reconcile', at(13))
    assert handler.calls == [Call(at(12), at(12, 1))]


async def test_a_schedule_error_pauses_the_job_for_a_minute_then_it_carries_on(
    schedulers: Schedulers, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    handler = Recorder(clock)
    schedule = FlakySchedule(HOURLY, failures=1)
    scheduler = schedulers.new(ScheduledJob('hourly', schedule, handler))

    await eventually(lambda: clock.next_deadline == at(12, 1), 'the job pauses')
    assert status_of(scheduler, 'hourly').next_run is None
    [error] = [
        record
        for record in caplog.records
        if record.name == SCHEDULER_LOGGER and record.levelno == logging.ERROR
    ]
    assert (
        error.getMessage() == 'Job hourly hit an unexpected error; trying again in 1m'
    )
    assert error.exc_info is not None
    assert str(error.exc_info[1]) == 'schedule bug'

    await clock.advance_to(at(12, 1))
    await parked(scheduler, 'hourly', at(13))
    await clock.advance_to(at(13))
    await parked(scheduler, 'hourly', at(14))
    assert handler.calls == [Call(at(13), at(13))]


async def test_a_lasting_error_is_logged_as_an_error_once_an_hour(
    schedulers: Schedulers, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    schedule = FlakySchedule(HOURLY, failures=ALWAYS)
    schedulers.new(ScheduledJob('broken', schedule, noop, persistent=False))
    await eventually(lambda: clock.next_deadline == at(12, 1), 'the job pauses')

    await clock.advance_to(at(13))  # it tries again every minute
    await eventually(lambda: clock.next_deadline == at(13, 1), 'the job pauses')
    levels = [
        record.levelno
        for record in caplog.records
        if record.getMessage().startswith('Job broken hit an unexpected error')
    ]
    assert levels == [logging.ERROR] + [logging.INFO] * 59 + [logging.ERROR]


async def test_a_database_error_does_not_stop_the_job(
    schedulers: Schedulers,
    db: Database,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Recorder(clock)
    job = ScheduledJob('hourly', HOURLY, handler, catch_up_grace=GRACE)
    scheduler = schedulers.new(job)
    await parked(scheduler, 'hourly', at(13))

    await db.execute('ALTER TABLE job_state RENAME TO job_state_away')
    await clock.advance_to(at(13))
    await eventually(lambda: clock.next_deadline == at(13, 1), 'the job pauses')
    assert 'no such table: job_state' in caplog.text
    await db.execute('ALTER TABLE job_state_away RENAME TO job_state')

    await clock.advance_to(at(13, 1))
    await parked(scheduler, 'hourly', at(14))
    # The run whose success could not be saved is simply run again.
    assert handler.calls == [Call(at(13), at(13)), Call(at(13), at(13, 1))]
    assert await stored(db, 'hourly') == Stored(at(13), 0, None)


async def test_status_describes_every_job_in_name_order(
    schedulers: Schedulers, clock: FakeClock
) -> None:
    weekly = Recorder(clock, failures=1)
    weekly.gate = asyncio.Event()
    scheduler = schedulers.new(
        ScheduledJob('weekly.post', FRIDAY_NOON, weekly, catch_up_grace=GRACE),
        ScheduledJob('kcpc.reconcile', EVERY_2M, noop, persistent=False),
    )
    await parked(scheduler, 'weekly.post', NEXT_FRIDAY)
    await parked(scheduler, 'kcpc.reconcile', at(12, 2))
    reconcile_status = JobStatus(
        name='kcpc.reconcile',
        description='every 2m',
        persistent=False,
        running=False,
        next_run=at(12, 2),
        last_slot=None,
        failures=0,
        last_error=None,
    )
    weekly_status = JobStatus(
        name='weekly.post',
        description='every Friday at 12:00 (Europe/London)',
        persistent=True,
        running=False,
        next_run=NEXT_FRIDAY,
        last_slot=LAST_FRIDAY,
        failures=0,
        last_error=None,
    )
    assert scheduler.status() == [reconcile_status, weekly_status]

    manual = asyncio.create_task(scheduler.run_slot('weekly.post'))
    await eventually(lambda: len(weekly.calls) == 1, 'the handler runs')
    assert status_of(scheduler, 'weekly.post') == replace(weekly_status, running=True)

    weekly.gate.set()
    with pytest.raises(RuntimeError):
        await manual
    assert scheduler.status() == [
        reconcile_status,
        replace(weekly_status, failures=1, last_error='RuntimeError: call 1 failed'),
    ]


@pytest.mark.parametrize(
    ('error', 'text'),
    [
        (RuntimeError('boom'), 'RuntimeError: boom'),
        (RuntimeError(), 'RuntimeError'),
        (ValueError('x' * 1000), 'ValueError: ' + 'x' * 487 + '…'),
    ],
    ids=['message', 'no message', 'long message'],
)
async def test_the_last_error_names_the_error_in_at_most_500_characters(
    error: Exception, text: str, schedulers: Schedulers, db: Database
) -> None:
    async def fail(slot: datetime) -> None:
        raise error

    scheduler = schedulers.new(ScheduledJob('job', HOURLY, fail), start=False)
    with pytest.raises(type(error)):
        await scheduler.run_slot('job')
    assert len(text) <= 500
    assert status_of(scheduler, 'job').last_error == text
    assert await stored(db, 'job') == Stored(None, 1, text)
