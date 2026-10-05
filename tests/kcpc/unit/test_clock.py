"""Tests for tle.kcpc.core.clock: the FakeClock that tests run on, and SystemClock."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC, FakeClock, SystemClock

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)  # the clock fixture's start

Spawn = Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]]
Move = Callable[[FakeClock], Awaitable[None]]
Woke = list[tuple[str, datetime]]


def after(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


@pytest.fixture
async def spawn() -> AsyncIterator[Spawn]:
    """Starts a test's tasks, and cancels any still running when it ends."""
    tasks: list[asyncio.Task[None]] = []

    def start(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    yield start
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def sleep_until_and_note(
    clock: FakeClock, when: datetime, woke: Woke, name: str
) -> None:
    """Sleep until ``when``, then note ``name`` and the time the clock shows."""
    await clock.sleep_until(when)
    woke.append((name, clock.now()))


async def tick(clock: FakeClock, every: float, ticks: list[datetime]) -> None:
    """Sleep for ``every`` seconds at a time, noting the time at each wake-up."""
    while True:
        await clock.sleep(every)
        ticks.append(clock.now())


async def tick_and_work_in_threads(
    clock: FakeClock, every: float, ticks: list[datetime]
) -> None:
    """Like ``tick``, but after each wake-up make a few slow calls to a thread.

    A job saving its state with aiosqlite does the same: each statement is a
    round trip to the database's worker thread. Together the calls take far
    longer than ``settle()``.
    """
    while True:
        await clock.sleep(every)
        ticks.append(clock.now())
        for _ in range(5):
            await asyncio.to_thread(time.sleep, 0.01)


async def work_after(
    clock: FakeClock, when: datetime, events: list[str], name: str
) -> None:
    """Wake at ``when``, then work for a few turns of the event loop."""
    await clock.sleep_until(when)
    events.append(f'{name} woke')
    for _ in range(3):
        await asyncio.sleep(0)
    events.append(f'{name} finished')


def test_a_fake_clock_tells_the_time_in_utc() -> None:
    clock = FakeClock(datetime(2026, 10, 1, 14, 0, tzinfo=timezone(timedelta(hours=2))))

    assert clock.now() == START
    assert clock.now().tzinfo is UTC
    assert clock.monotonic() == START.timestamp()


def test_a_fake_clock_needs_an_aware_start() -> None:
    with pytest.raises(ValueError, match='timezone-aware'):
        FakeClock(datetime(2026, 10, 1, 12, 0))


async def test_time_moves_only_when_the_test_moves_it(clock: FakeClock) -> None:
    await asyncio.sleep(0.01)
    assert clock.now() == START

    await clock.advance(timedelta(minutes=1))
    assert clock.now() == after(60)
    await clock.advance(1.5)  # seconds
    assert clock.now() == after(61.5)
    await clock.advance_to(after(3600))
    assert clock.now() == after(3600)
    assert clock.monotonic() == after(3600).timestamp()


@pytest.mark.parametrize(
    'move',
    [
        lambda clock: clock.advance(-1),
        lambda clock: clock.advance(timedelta(microseconds=-1)),
        lambda clock: clock.advance_to(START - timedelta(microseconds=1)),
        lambda clock: clock.jump(timedelta(seconds=-1)),
    ],
    ids=['advance seconds', 'advance timedelta', 'advance_to', 'jump'],
)
async def test_time_never_goes_backwards(clock: FakeClock, move: Move) -> None:
    with pytest.raises(ValueError, match='cannot go backwards'):
        await move(clock)

    assert clock.now() == START


async def test_a_sleeper_wakes_at_its_deadline_and_not_before(
    clock: FakeClock, spawn: Spawn
) -> None:
    woke: Woke = []
    sleeper = spawn(sleep_until_and_note(clock, after(60), woke, 'sleeper'))
    await clock.settle()

    await clock.advance(59)
    assert not sleeper.done()

    await clock.advance(1)
    assert sleeper.done()
    assert woke == [('sleeper', after(60))]


async def test_advance_wakes_sleepers_in_deadline_order_each_at_its_deadline(
    clock: FakeClock, spawn: Spawn
) -> None:
    woke: Woke = []
    for name, seconds in [('c', 30), ('a', 10), ('d', 30), ('b', 20)]:
        spawn(sleep_until_and_note(clock, after(seconds), woke, name))
    await clock.settle()

    await clock.advance(60)

    # Sleepers with the same deadline wake in the order they began sleeping.
    assert woke == [
        ('a', after(10)),
        ('b', after(20)),
        ('c', after(30)),
        ('d', after(30)),
    ]
    assert clock.now() == after(60)


async def test_advance_wakes_a_repeating_sleeper_at_each_deadline_on_the_way(
    clock: FakeClock, spawn: Spawn
) -> None:
    ticks: list[datetime] = []
    spawn(tick(clock, 10, ticks))
    await clock.settle()

    await clock.advance(35)

    assert ticks == [after(10), after(20), after(30)]
    assert clock.next_deadline == after(40)


@pytest.mark.parametrize(
    'move',
    [lambda clock: clock.advance(60), lambda clock: clock.jump(60)],
    ids=['advance', 'jump'],
)
async def test_each_woken_sleeper_runs_until_it_blocks_before_the_next_wakes(
    clock: FakeClock, spawn: Spawn, move: Move
) -> None:
    events: list[str] = []
    spawn(work_after(clock, after(10), events, 'a'))
    spawn(work_after(clock, after(20), events, 'b'))
    await clock.settle()

    await move(clock)

    assert events == ['a woke', 'a finished', 'b woke', 'b finished']


async def test_advance_waits_for_a_woken_task_to_finish_its_work_in_threads(
    clock: FakeClock, spawn: Spawn
) -> None:
    ticks: list[datetime] = []
    spawn(tick_and_work_in_threads(clock, 10, ticks))
    await clock.settle()

    await clock.advance(35)

    # Each tick's work was done, and the next sleep begun, before time moved on.
    assert ticks == [after(10), after(20), after(30)]
    assert clock.next_deadline == after(40)


async def test_jump_waits_for_a_woken_task_to_finish_its_work_in_threads(
    clock: FakeClock, spawn: Spawn
) -> None:
    ticks: list[datetime] = []
    spawn(tick_and_work_in_threads(clock, 10, ticks))
    await clock.settle()

    await clock.jump(35)

    assert ticks == [after(35)]
    assert clock.next_deadline == after(45)  # its work is done: it sleeps again


async def test_time_moves_on_as_soon_as_a_woken_task_sleeps_again_or_ends(
    spawn: Spawn,
) -> None:
    # Waiting this long for any task would make the advance below time out.
    clock = FakeClock(START, wake_timeout=60)
    ticks: list[datetime] = []
    woke: Woke = []
    spawn(tick_and_work_in_threads(clock, 10, ticks))
    spawn(sleep_until_and_note(clock, after(15), woke, 'once'))
    await clock.settle()

    await asyncio.wait_for(clock.advance(35), timeout=5)

    assert ticks == [after(10), after(20), after(30)]
    assert woke == [('once', after(15))]


async def test_a_woken_task_blocked_elsewhere_holds_time_up_only_for_the_wake_timeout(
    spawn: Spawn,
) -> None:
    clock = FakeClock(START, wake_timeout=0.05)
    gate = asyncio.Event()  # never opened
    woke: Woke = []

    async def wake_then_wait_for_the_gate() -> None:
        await sleep_until_and_note(clock, after(10), woke, 'blocked')
        await gate.wait()

    blocked = spawn(wake_then_wait_for_the_gate())
    spawn(sleep_until_and_note(clock, after(20), woke, 'next'))
    await clock.settle()

    # The timeout only stops a regression from hanging the test run.
    await asyncio.wait_for(clock.advance(60), timeout=5)

    assert woke == [('blocked', after(10)), ('next', after(20))]
    assert clock.now() == after(60)
    assert not blocked.done()


@pytest.mark.parametrize(
    'when', [START - timedelta(hours=1), START], ids=['in the past', 'now']
)
async def test_sleeping_until_a_time_already_reached_returns_at_once(
    clock: FakeClock, when: datetime
) -> None:
    # Deadlines are absolute: a sleep that begins late does not drift.
    await asyncio.wait_for(clock.sleep_until(when), timeout=1)

    assert clock.now() == START
    assert clock.pending_sleepers == 0


@pytest.mark.parametrize('seconds', [0, -5])
async def test_sleeping_for_no_time_returns_at_once(
    clock: FakeClock, seconds: float
) -> None:
    await asyncio.wait_for(clock.sleep(seconds), timeout=1)

    assert clock.pending_sleepers == 0


async def test_pending_sleepers_and_the_next_deadline(
    clock: FakeClock, spawn: Spawn
) -> None:
    assert (clock.pending_sleepers, clock.next_deadline) == (0, None)
    spawn(clock.sleep(30))
    spawn(clock.sleep(10))
    await clock.settle()

    assert (clock.pending_sleepers, clock.next_deadline) == (2, after(10))
    await clock.advance(10)
    assert (clock.pending_sleepers, clock.next_deadline) == (1, after(30))


async def test_a_cancelled_sleeper_is_forgotten(clock: FakeClock, spawn: Spawn) -> None:
    woke: Woke = []
    cancelled = spawn(sleep_until_and_note(clock, after(10), woke, 'cancelled'))
    spawn(sleep_until_and_note(clock, after(20), woke, 'kept'))
    await clock.settle()

    cancelled.cancel()
    await clock.settle()

    assert cancelled.cancelled()
    assert (clock.pending_sleepers, clock.next_deadline) == (1, after(20))
    await clock.advance(30)  # past the cancelled sleeper's deadline too
    assert woke == [('kept', after(20))]


async def test_jump_wakes_every_overdue_sleeper_at_the_new_time(
    clock: FakeClock, spawn: Spawn
) -> None:
    woke: Woke = []
    for name, seconds in [('b', 20), ('a', 10), ('later', 90)]:
        spawn(sleep_until_and_note(clock, after(seconds), woke, name))
    await clock.settle()

    await clock.jump(timedelta(minutes=1))

    assert clock.now() == after(60)
    # In deadline order, but each finds the clock already moved on.
    assert woke == [('a', after(60)), ('b', after(60))]
    assert (clock.pending_sleepers, clock.next_deadline) == (1, after(90))


async def test_jump_wakes_a_repeating_sleeper_only_once(
    clock: FakeClock, spawn: Spawn
) -> None:
    ticks: list[datetime] = []
    spawn(tick(clock, 10, ticks))
    await clock.settle()

    await clock.jump(35)  # seconds

    # Where advance would tick at 10, 20 and 30 seconds.
    assert ticks == [after(35)]
    assert clock.next_deadline == after(45)


class ScriptedWallClock(SystemClock):
    """SystemClock's own sleeping, on a wall clock that the test controls.

    See the ``wall_clock`` fixture.
    """

    def __init__(self, now: datetime) -> None:
        self.wall = now
        self.naps: list[float] = []  # the delays asyncio.sleep was called with
        self.suspended = timedelta(0)  # how long each nap overruns by

    def now(self) -> datetime:
        return self.wall


@pytest.fixture
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> ScriptedWallClock:
    """A ScriptedWallClock at START, whose naps take no real time.

    While the test runs, asyncio.sleep notes its delay and returns at once,
    moving the wall clock on by the delay, plus ``suspended`` as if the machine
    had been suspended meanwhile.
    """
    clock = ScriptedWallClock(START)
    real_sleep = asyncio.sleep

    async def nap(delay: float) -> None:
        clock.naps.append(delay)
        clock.wall += timedelta(seconds=delay) + clock.suspended
        await real_sleep(0)

    monkeypatch.setattr(asyncio, 'sleep', nap)
    return clock


def test_the_system_clock_tells_the_time_in_utc() -> None:
    clock = SystemClock()

    before = datetime.now(UTC)
    now = clock.now()

    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC)
    assert clock.monotonic() <= clock.monotonic()


async def test_the_system_clock_sleeps_until_a_time_in_chunks_of_five_minutes(
    wall_clock: ScriptedWallClock,
) -> None:
    await wall_clock.sleep_until(after(750))

    assert wall_clock.naps == [300, 300, 150]
    assert wall_clock.now() == after(750)


async def test_the_system_clock_notices_when_the_wall_clock_jumps(
    wall_clock: ScriptedWallClock,
) -> None:
    wall_clock.suspended = timedelta(hours=1)

    await wall_clock.sleep_until(after(1200))

    assert wall_clock.naps == [300]  # it woke past the deadline, so it stopped


async def test_the_system_clock_does_not_sleep_for_a_time_already_reached(
    wall_clock: ScriptedWallClock,
) -> None:
    await wall_clock.sleep(-5)
    await wall_clock.sleep_until(START - timedelta(seconds=1))

    # sleep only yields to the event loop; sleep_until does not even do that.
    assert wall_clock.naps == [0.0]
