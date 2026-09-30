"""Runs recurring jobs at the slots of their schedules.

A slot is one instant at which a job is due (see ``schedule``). The handler is
given its slot, and must be safe to run again for the same slot: everything it
writes should be keyed by the slot.

A persistent job records its last completed slot in ``job_state``:

- On its first start it counts its latest slot as done without running it, so a
  fresh install never posts at once.
- On starting after downtime, only its latest missed slot is considered: that
  runs once if it is within the job's ``catch_up_grace``, and is otherwise
  skipped with a warning. A slot that the job wakes up to more than its grace
  late (after a suspend, say) is dealt with in the same way.
- A failed slot is retried after each of ``retry_delays`` in turn while the
  retry still falls within grace. Then it is given up: recorded as done, so that
  it is never retried, even after a restart.

A non-persistent job keeps that state in memory and never retries, and after a
long pause it runs only its next slot, not every slot it missed.

Each job runs in an asyncio task that ends only when cancelled: an unexpected
error is logged, and the job tries again a minute later. See
KCPC_ARCHITECTURE.md section 4.2.
"""

import asyncio
import itertools
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database
from tle.kcpc.core.schedule import Schedule
from tle.kcpc.core.timeutil import describe_duration, from_epoch, to_epoch

logger = logging.getLogger(__name__)

Handler = Callable[[datetime], Awaitable[None]]

# How long a job's task pauses after an unexpected error before trying again.
_ERROR_PAUSE = timedelta(minutes=1)
# A job logs unexpected errors at ERROR, which reaches the Discord log channel,
# at most this often; repeats in between are logged at INFO.
_LOUD_ERROR_INTERVAL = timedelta(hours=1)
# A slot run this late still counts as on time, whatever the job's grace: a task
# sleeping until a slot wakes a little after it, and a busy event loop can delay
# it further.
_ON_TIME_TOLERANCE = timedelta(minutes=1)
# The longest error text kept in job_state.last_error.
_MAX_ERROR_LENGTH = 500

_DEFAULT_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
)


@dataclass(frozen=True)
class ScheduledJob:
    """A job that runs ``handler(slot)`` at every slot of ``schedule``.

    ``name`` is unique within a scheduler, and a persistent job's state is stored
    under it. ``catch_up_grace`` is how late a persistent job may still run a
    slot: to catch up on one it missed (under a minute late always counts as on
    time), or to retry one that failed. After a failure, a persistent job retries
    after ``retry_delays[0]``, then ``retry_delays[1]`` and so on, repeating the
    last delay. ``run_on_start`` makes a non-persistent job run its latest slot
    as soon as it starts.
    """

    name: str
    schedule: Schedule
    handler: Handler
    persistent: bool = True
    catch_up_grace: timedelta = timedelta(0)
    run_on_start: bool = False
    retry_delays: tuple[timedelta, ...] = _DEFAULT_RETRY_DELAYS

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('A scheduled job needs a name')
        if self.catch_up_grace < timedelta(0):
            raise ValueError(
                f'catch_up_grace must not be negative, got {self.catch_up_grace!r}'
            )
        if any(delay <= timedelta(0) for delay in self.retry_delays):
            raise ValueError(
                f'retry_delays must all be positive, got {self.retry_delays!r}'
            )
        # Each option only means something for one kind of job, so setting it on
        # the other kind is a mistake worth catching.
        if self.persistent and self.run_on_start:
            raise ValueError(
                'run_on_start is for non-persistent jobs; '
                'a persistent job catches up on its latest slot instead'
            )
        if not self.persistent and self.catch_up_grace != timedelta(0):
            raise ValueError('catch_up_grace is for persistent jobs only')


@dataclass(frozen=True)
class JobStatus:
    """A snapshot of one job, e.g. for ``/kcpc status``."""

    name: str
    description: str  # schedule.describe()
    persistent: bool
    running: bool  # a handler call is in progress
    # The slot or retry the job is waiting for; None while it runs, pauses after
    # an error, waits to be ready or is stopped.
    next_run: datetime | None
    last_slot: datetime | None  # the last completed (or given-up) slot
    failures: int  # failed runs since the last success
    last_error: str | None


@dataclass(frozen=True)
class _JobState:
    """What a job remembers between runs: a persistent job's job_state row."""

    # The latest slot that needs no run: completed, skipped or given up.
    last_slot: datetime | None = None
    failures: int = 0
    last_error: str | None = None

    def covers(self, slot: datetime) -> bool:
        """Whether ``slot`` is at or before the last slot, so it needs no run."""
        return self.last_slot is not None and slot <= self.last_slot

    def advanced_to(self, slot: datetime | None) -> '_JobState':
        """This state with ``slot`` done too; the last slot never moves back."""
        if slot is None or self.covers(slot):
            return self
        return replace(self, last_slot=slot)

    def succeeded(self, slot: datetime) -> '_JobState':
        return replace(self.advanced_to(slot), failures=0, last_error=None)

    def failed(self, error: Exception) -> '_JobState':
        return replace(
            self, failures=self.failures + 1, last_error=_describe_error(error)
        )


_StateChange = Callable[[_JobState], _JobState]

_UPSERT_STATE = """
    INSERT INTO job_state (job, last_slot, failures, last_error, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT (job) DO UPDATE SET
        last_slot = excluded.last_slot,
        failures = excluded.failures,
        last_error = excluded.last_error,
        updated_at = excluded.updated_at
"""


class _JobStateTable:
    """The ``job_state`` table, where persistent jobs keep their state."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def load(self, job: str) -> _JobState | None:
        """The job's stored state, or None if it has never started."""
        row = await self._db.fetchone(
            'SELECT last_slot, failures, last_error FROM job_state WHERE job = ?',
            (job,),
        )
        if row is None:
            return None
        last_slot = row['last_slot']
        return _JobState(
            last_slot=None if last_slot is None else from_epoch(last_slot),
            failures=row['failures'],
            last_error=row['last_error'],
        )

    async def update(self, job: str, change: _StateChange) -> _JobState:
        """Apply ``change`` to the stored state (or the defaults) atomically.

        Returns the new state.
        """
        async with self._db.transaction():
            stored = await self.load(job)
            state = change(_JobState() if stored is None else stored)
            await self._db.execute(
                _UPSERT_STATE,
                (
                    job,
                    None if state.last_slot is None else to_epoch(state.last_slot),
                    state.failures,
                    state.last_error,
                    to_epoch(self._clock.now()),
                ),
            )
        return state


class _JobRunner(ABC):
    """Runs one job: the loop of its task, and the one-off runs of ``run_slot``.

    Every handler call, and the bookkeeping of its outcome, happens under the
    job's lock, so a scheduled run and a ``run_slot`` never overlap.
    """

    def __init__(
        self,
        job: ScheduledJob,
        clock: Clock,
        ready: Callable[[], Awaitable[object]] | None,
    ) -> None:
        self.job = job
        self._clock = clock
        self._ready = ready
        self._lock = asyncio.Lock()
        self._state = _JobState()
        self._next_run: datetime | None = None
        self._running = False
        self._loud_error_at: float | None = None  # clock.monotonic() seconds

    def status(self) -> JobStatus:
        return JobStatus(
            name=self.job.name,
            description=self.job.schedule.describe(),
            persistent=self.job.persistent,
            running=self._running,
            next_run=self._next_run,
            last_slot=self._state.last_slot,
            failures=self._state.failures,
            last_error=self._state.last_error,
        )

    async def run_forever(self) -> None:
        """The job's task: wait until ready, start up, then run slot after slot.

        It ends only when cancelled. Any other error is logged, and the step that
        raised it is tried again after a pause.
        """
        if self._ready is not None:
            await self._until_done(self._ready)
        await self._until_done(self._start_up)
        while True:
            await self._until_done(self._run_next)

    async def run_manually(self, slot: datetime) -> None:
        """Run ``slot`` once, even if it is done, re-raising the handler's error."""
        error = await self._run_once(slot, unless_done=False)
        if error is not None:
            raise error

    @abstractmethod
    async def _start_up(self) -> None:
        """Whatever the job does once it is ready, before its first wait."""

    @abstractmethod
    async def _run_next(self) -> None:
        """Wait for the job's next slot and run it."""

    @abstractmethod
    async def _update_state(self, change: _StateChange) -> None:
        """Apply ``change`` to the job's state, wherever the job keeps it."""

    async def _until_done(self, step: Callable[[], Awaitable[object]]) -> None:
        """Run ``step`` until it completes, pausing after each failure.

        ``CancelledError`` is not an ``Exception``, so cancellation still ends
        the task.
        """
        while True:
            try:
                await step()
            except Exception:
                self._log_unexpected_error()
                await self._clock.sleep(_ERROR_PAUSE.total_seconds())
            else:
                return

    async def _wait_until(self, when: datetime) -> None:
        """Sleep until ``when``, showing it as the job's next run meanwhile."""
        self._next_run = when
        try:
            await self._clock.sleep_until(when)
        finally:
            self._next_run = None

    async def _run_once(self, slot: datetime, *, unless_done: bool) -> Exception | None:
        """Call the handler for ``slot`` and record the outcome, under the job lock.

        Returns the handler's exception, or None if it succeeded. With
        ``unless_done``, a slot that is already done (say by ``run_slot`` while
        this run waited for the lock) is not run again, and that counts as a
        success.
        """
        async with self._lock:
            if unless_done and self._state.covers(slot):
                logger.info(
                    'Not running job %s for its %s slot: it is already done',
                    self.job.name,
                    slot,
                )
                return None
            error = await self._call_handler(slot)
            if error is None:
                await self._record_success(slot)
            else:
                await self._record_failure(slot, error)
            return error

    async def _call_handler(self, slot: datetime) -> Exception | None:
        """Call the handler, returning its exception instead of raising it."""
        self._running = True
        try:
            await self.job.handler(slot)
        except Exception as error:
            return error
        finally:
            self._running = False
        return None

    async def _record_success(self, slot: datetime) -> None:
        failures = self._state.failures
        await self._update_state(lambda state: state.succeeded(slot))
        if failures:
            logger.info(
                'Job %s succeeded after %d failed runs', self.job.name, failures
            )

    async def _record_failure(self, slot: datetime, error: Exception) -> None:
        # Logged before it is saved, so the traceback is kept even if saving
        # fails. Only a healthy job starting to fail is a warning: a job that
        # keeps failing would otherwise flood the Discord log channel.
        level = logging.WARNING if self._state.failures == 0 else logging.INFO
        logger.log(
            level, 'Job %s failed on its %s slot', self.job.name, slot, exc_info=error
        )
        await self._update_state(lambda state: state.failed(error))

    def _log_unexpected_error(self) -> None:
        """Log the error being handled: at ERROR at most once an hour, else INFO."""
        now = self._clock.monotonic()
        loud = (
            self._loud_error_at is None
            or now - self._loud_error_at >= _LOUD_ERROR_INTERVAL.total_seconds()
        )
        if loud:
            self._loud_error_at = now
        logger.log(
            logging.ERROR if loud else logging.INFO,
            'Job %s hit an unexpected error; trying again in %s',
            self.job.name,
            describe_duration(_ERROR_PAUSE),
            exc_info=True,
        )


class _NonPersistentJobRunner(_JobRunner):
    """A non-persistent job: state in memory, no catch-up and no retries."""

    async def _start_up(self) -> None:
        if self.job.run_on_start:
            now = self._clock.now()
            latest = self.job.schedule.prev_at_or_before(now)
            await self._run_once(latest or _as_slot(now), unless_done=True)

    async def _run_next(self) -> None:
        # Counting from now rather than from the last slot means that after a
        # long suspend the job runs once, not once for every slot it missed.
        now = self._clock.now()
        last = self._state.last_slot
        target = self.job.schedule.next_after(now if last is None else max(last, now))
        await self._wait_until(target)
        await self._run_once(target, unless_done=True)

    async def _update_state(self, change: _StateChange) -> None:
        self._state = change(self._state)


class _PersistentJobRunner(_JobRunner):
    """A persistent job: state in job_state, catch-up and retries within grace."""

    def __init__(
        self,
        job: ScheduledJob,
        clock: Clock,
        ready: Callable[[], Awaitable[object]] | None,
        table: _JobStateTable,
    ) -> None:
        super().__init__(job, clock, ready)
        self._table = table

    async def _start_up(self) -> None:
        now = self._clock.now()
        latest = self.job.schedule.prev_at_or_before(now)
        stored = await self._table.load(self.job.name)
        if stored is None:
            await self._update_state(lambda state: state.advanced_to(latest))
            logger.info(
                'Job %s is starting for the first time; it will run from its next '
                'slot on',
                self.job.name,
            )
            return
        self._state = stored
        if latest is not None and not stored.covers(latest):
            await self._catch_up(latest)

    async def _run_next(self) -> None:
        # Start-up leaves the last slot unset only if the schedule had no slot
        # before then, and then the first slot to run is the first one after now.
        target = self.job.schedule.next_after(
            self._state.last_slot or self._clock.now()
        )
        await self._wait_until(target)
        now = self._clock.now()
        if now - target <= self._allowed_lateness:
            await self._run_with_retries(target)
        else:
            # Woken late (the bot was suspended, or a run overran): of the slots
            # missed meanwhile, only the latest can still be worth running.
            await self._catch_up(self.job.schedule.prev_at_or_before(now) or target)

    async def _update_state(self, change: _StateChange) -> None:
        self._state = await self._table.update(self.job.name, change)

    @property
    def _allowed_lateness(self) -> timedelta:
        return max(self.job.catch_up_grace, _ON_TIME_TOLERANCE)

    async def _catch_up(self, slot: datetime) -> None:
        """Run a slot that is due late, or skip it if it is later than grace allows."""
        lateness = self._clock.now() - slot
        if lateness > self._allowed_lateness:
            await self._update_state(lambda state: state.advanced_to(slot))
            logger.warning(
                'Job %s skipped its %s slot: it is %s late, and may run at most %s '
                'late',
                self.job.name,
                slot,
                describe_duration(lateness),
                describe_duration(self._allowed_lateness),
            )
            return
        logger.info(
            'Job %s is catching up on its %s slot, %s late',
            self.job.name,
            slot,
            describe_duration(lateness),
        )
        await self._run_with_retries(slot)

    async def _run_with_retries(self, slot: datetime) -> None:
        """Run ``slot``, retrying failures while grace allows, then give it up."""
        deadline = slot + self.job.catch_up_grace
        for attempts in itertools.count(1):
            error = await self._run_once(slot, unless_done=True)
            if error is None:
                return
            retry_at = self._retry_time(attempts)
            if retry_at is None or retry_at > deadline:
                await self._give_up(slot, attempts, error)
                return
            logger.info(
                'Job %s will retry its %s slot at %s', self.job.name, slot, retry_at
            )
            await self._wait_until(retry_at)
            woken_too_late = self._clock.now() > deadline + _ON_TIME_TOLERANCE
            if woken_too_late and not self._state.covers(slot):
                # The bot was suspended, say. (A slot done meanwhile, by run_slot,
                # is left to _run_once, which knows not to run it again.)
                await self._give_up(slot, attempts, error)
                return

    def _retry_time(self, failed_attempts: int) -> datetime | None:
        """When to retry after ``failed_attempts`` failures; None if never."""
        delays = self.job.retry_delays
        if not delays:
            return None
        return self._clock.now() + delays[min(failed_attempts, len(delays)) - 1]

    async def _give_up(self, slot: datetime, attempts: int, error: Exception) -> None:
        # Recording the slot as done means it is never retried, even after a
        # restart.
        await self._update_state(lambda state: state.advanced_to(slot))
        logger.error(
            'Job %s gave up on its %s slot after %d failed attempts',
            self.job.name,
            slot,
            attempts,
            exc_info=error,
        )


class Scheduler:
    """Runs scheduled jobs, each in an asyncio task of its own.

    ``db`` holds persistent jobs' state, so it may be None only if every job is
    non-persistent. With ``ready``, each job's task awaits ``ready()`` before
    doing anything else (e.g. ``bot.wait_until_ready``).
    """

    def __init__(
        self,
        db: Database | None,
        clock: Clock,
        *,
        ready: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._table = None if db is None else _JobStateTable(db, clock)
        self._clock = clock
        self._ready = ready
        self._runners: dict[str, _JobRunner] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False

    @property
    def running(self) -> bool:
        """Whether the scheduler has been started, and not stopped since."""
        return self._started

    def add(self, job: ScheduledJob) -> None:
        """Register ``job``; if the scheduler is running, the job starts at once.

        Raises ``ValueError`` if the name is taken, or if the job is persistent
        and the scheduler has no database.
        """
        if job.name in self._runners:
            raise ValueError(f'A job named {job.name!r} is already scheduled')
        self._runners[job.name] = self._runner_for(job)
        if self._started:
            self._launch(job.name)

    async def remove(self, name: str) -> None:
        """Unregister a job, cancelling its task. An unknown name is ignored."""
        self._runners.pop(name, None)
        task = self._tasks.pop(name, None)
        if task is not None:
            await _cancel_and_wait([task])

    def start(self) -> None:
        """Start a task for every job, and for jobs added later. Idempotent."""
        for name in self._runners:
            task = self._tasks.get(name)
            if task is None or task.done():
                self._launch(name)
        self._started = True

    async def stop(self) -> None:
        """Cancel every job's task and wait for them to end. Idempotent."""
        self._started = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        await _cancel_and_wait(tasks)

    async def run_slot(self, name: str, slot: datetime | None = None) -> datetime:
        """Run one slot of a job now, once, and return the slot.

        ``slot`` defaults to the job's latest slot, or now if it has none;
        fractions of a second are dropped. The run shares the job's lock and
        bookkeeping with scheduled runs, but it runs even if the slot is done and
        is never retried: the handler's exception is re-raised. Raises
        ``KeyError`` for an unknown job.
        """
        runner = self._runners[name]
        if slot is None:
            now = self._clock.now()
            slot = runner.job.schedule.prev_at_or_before(now) or now
        slot = _as_slot(slot)
        await runner.run_manually(slot)
        return slot

    def status(self) -> list[JobStatus]:
        """Every job's status, sorted by name.

        A persistent job's stored state shows once its task has started up (or
        a ``run_slot`` has run it).
        """
        return [self._runners[name].status() for name in sorted(self._runners)]

    def _runner_for(self, job: ScheduledJob) -> _JobRunner:
        if not job.persistent:
            return _NonPersistentJobRunner(job, self._clock, self._ready)
        if self._table is None:
            raise ValueError(
                f'Job {job.name!r} is persistent, but the scheduler has no database'
            )
        return _PersistentJobRunner(job, self._clock, self._ready, self._table)

    def _launch(self, name: str) -> None:
        self._tasks[name] = asyncio.create_task(
            self._runners[name].run_forever(), name=f'kcpc-job:{name}'
        )


async def _cancel_and_wait(tasks: Iterable[asyncio.Task[None]]) -> None:
    """Cancel ``tasks`` and wait for them to end.

    A job's handler may stop its own task (by removing its job, say). That task
    is only cancelled: waiting for it from inside it would never end.
    """
    current = asyncio.current_task()
    others: list[asyncio.Task[None]] = []
    for task in tasks:
        task.cancel()
        if task is not current:
            others.append(task)
    if others:
        await asyncio.wait(others)


def _as_slot(moment: datetime) -> datetime:
    """``moment`` as a slot: in UTC, whole seconds (as job_state stores it)."""
    return from_epoch(to_epoch(moment))


def _describe_error(error: Exception) -> str:
    """``'Type: message'`` for job_state.last_error, cut to fit."""
    message = str(error)
    text = f'{type(error).__name__}: {message}' if message else type(error).__name__
    if len(text) <= _MAX_ERROR_LENGTH:
        return text
    return text[: _MAX_ERROR_LENGTH - 1] + '…'
