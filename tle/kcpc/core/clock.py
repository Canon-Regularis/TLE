"""Injectable time source.

All KCPC code reads the time and sleeps through a ``Clock`` so that tests can
drive time with ``FakeClock`` instead of waiting for real time to pass.
"""

import asyncio
import heapq
import itertools
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

UTC = timezone.utc

# SystemClock.sleep_until never sleeps longer than this in one go, so that a
# suspended laptop or a wall-clock change is noticed within a few minutes.
_MAX_SLEEP_CHUNK_SECONDS = 300.0
# How often, in real seconds, FakeClock checks whether a task it woke has gone
# back to sleep.
_WAKE_POLL_SECONDS = 0.001


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as an aware ``datetime`` in UTC."""
        ...

    def monotonic(self) -> float:
        """Return seconds from an arbitrary fixed point, never going backwards."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of this clock's time (no-op if <= 0)."""
        ...

    async def sleep_until(self, when: datetime) -> None:
        """Sleep until ``now() >= when``; return at once if already there."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def sleep_until(self, when: datetime) -> None:
        # Re-check the wall clock after each chunk instead of trusting one long
        # sleep, so drift, suspend and clock adjustments cannot delay us much.
        while (remaining := (when - self.now()).total_seconds()) > 0:
            await asyncio.sleep(min(remaining, _MAX_SLEEP_CHUNK_SECONDS))


class FakeClock:
    """A manually advanced clock for tests.

    ``sleep`` and ``sleep_until`` park the calling task until ``advance`` or
    ``advance_to`` moves time past its deadline. Sleepers wake in deadline
    order, each with the clock at its own deadline. Before the next one wakes,
    or time moves on, the woken task runs until it sleeps on this clock again,
    ends, or ``wake_timeout`` real seconds pass. So whatever it awaits is done
    by then, even calls to helper threads such as aiosqlite's, and a task that
    keeps sleeping wakes at each of its deadlines on the way.

    Only the woken task itself is waited for: other tasks, including any it
    starts, get just ``settle()``. And a woken task that blocks on anything
    else, such as a lock or an event, holds time up for the whole
    ``wake_timeout``.

    Deadlines are absolute, so a task that registers a sleep late (after time
    already moved past it) wakes immediately instead of drifting. ``jump``
    moves time the way a machine resuming from suspend sees it instead: all at
    once, before any overdue sleeper wakes.
    """

    def __init__(
        self, start: datetime, *, io_grace: float = 0.002, wake_timeout: float = 1.0
    ):
        if start.tzinfo is None:
            raise ValueError('FakeClock needs a timezone-aware start time')
        self._now = start.astimezone(UTC)
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        # The task parked on each sleeper's future, until it wakes or is
        # cancelled.
        self._sleeping_tasks: dict[asyncio.Future[None], asyncio.Task[Any]] = {}
        # Real seconds that settle() waits, so that work running in helper
        # threads (e.g. aiosqlite) has a chance to finish.
        self._io_grace = io_grace
        # The longest a wake-up waits, in real seconds, for the woken task to
        # sleep again or end.
        self._wake_timeout = wake_timeout

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    async def sleep(self, seconds: float) -> None:
        await self.sleep_until(self._now + timedelta(seconds=seconds))

    async def sleep_until(self, when: datetime) -> None:
        if when <= self._now:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (when, next(self._seq), future))
        task = asyncio.current_task()
        if task is not None:
            self._sleeping_tasks[future] = task
        try:
            await future
        finally:
            self._sleeping_tasks.pop(future, None)

    @property
    def pending_sleepers(self) -> int:
        """Number of tasks currently parked in ``sleep``/``sleep_until``."""
        return sum(1 for _, _, future in self._sleepers if not future.done())

    @property
    def next_deadline(self) -> datetime | None:
        """Earliest pending wake-up time, if any task is sleeping."""
        live = [deadline for deadline, _, fut in self._sleepers if not fut.done()]
        return min(live) if live else None

    async def advance(self, delta: timedelta | float) -> None:
        """Move time forward by ``delta`` (a float is seconds); see ``advance_to``."""
        await self.advance_to(self._now + _forward(delta))

    async def advance_to(self, when: datetime) -> None:
        """Move time forward to ``when``, waking due sleepers in deadline order.

        Each sleeper wakes with the clock at its own deadline, as if the time
        had passed normally, and runs until it sleeps again, ends or times out
        (see the class docstring) before time moves on. So a woken task that
        sleeps again until a time up to ``when`` wakes again during this call.
        """
        if when < self._now:
            raise ValueError('FakeClock cannot go backwards')
        await self._wake_sleepers_due_by(when)
        self._now = when
        await self.settle()

    async def jump(self, delta: timedelta | float) -> None:
        """Move time forward by ``delta`` at once, as after a suspend or a stall.

        Unlike ``advance``, the clock shows the new time before anyone wakes:
        every sleeper whose deadline has passed wakes to find it, in deadline
        order, each running as it would for ``advance`` before the next wakes.
        So a task that sleeps again wakes once here, where ``advance`` would
        wake it at every deadline on the way.
        """
        self._now += _forward(delta)
        await self._wake_sleepers_due_by(self._now)
        await self.settle()

    async def settle(self) -> None:
        """Let ready tasks run for a moment, without moving time.

        That is ten turns of the event loop, ``io_grace`` real seconds, then ten
        more turns: enough for tasks to reach their next wait, but not always
        for a chain of calls to helper threads, such as a database transaction.
        """
        for _ in range(10):
            await asyncio.sleep(0)
        if self._io_grace > 0:
            await asyncio.sleep(self._io_grace)
        for _ in range(10):
            await asyncio.sleep(0)

    async def _wake_sleepers_due_by(self, when: datetime) -> None:
        """Wake each sleeper whose deadline is at or before ``when``, in order.

        The clock first moves up to the sleeper's deadline if it is behind it.
        Each woken task runs until it sleeps again (possibly until a time that
        is due by ``when`` too), ends or times out, before the next sleeper
        wakes.
        """
        await self.settle()
        while self._sleepers and self._sleepers[0][0] <= when:
            deadline, _, future = heapq.heappop(self._sleepers)
            if future.done():  # the sleeping task was cancelled
                continue
            self._now = max(self._now, deadline)
            task = self._sleeping_tasks.get(future)
            future.set_result(None)
            await self.settle()
            if task is not None:
                await self._until_asleep_or_done(task)

    async def _until_asleep_or_done(self, task: asyncio.Task[Any]) -> None:
        """Wait until ``task`` sleeps on this clock again, or ends.

        ``settle()`` alone can return while the task still waits for a helper
        thread, and then time would move on under it. This gives up after
        ``wake_timeout`` real seconds, as the task may instead be blocked on
        something that only the test can release, such as an event.
        """
        give_up_at = time.monotonic() + self._wake_timeout
        while not (task.done() or self._is_asleep(task)):
            if time.monotonic() >= give_up_at:
                return
            await asyncio.sleep(_WAKE_POLL_SECONDS)

    def _is_asleep(self, task: asyncio.Task[Any]) -> bool:
        """Whether ``task`` is parked in ``sleep``/``sleep_until`` on this clock."""
        return any(
            sleeper is task and not future.done()
            for future, sleeper in self._sleeping_tasks.items()
        )


def _forward(delta: timedelta | float) -> timedelta:
    """``delta`` as a timedelta (a float is seconds), refusing to go backwards."""
    step = delta if isinstance(delta, timedelta) else timedelta(seconds=delta)
    if step < timedelta(0):
        raise ValueError('FakeClock cannot go backwards')
    return step
