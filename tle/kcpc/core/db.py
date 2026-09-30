"""The KCPC database: one shared aiosqlite connection with serialized access.

Every statement goes through one ``asyncio.Lock``. A transaction belongs to the
task that opened it, compared by task identity rather than a ContextVar, so a
child task never piggy-backs on its parent's transaction: it waits for the lock
like any other task. Inside the owning task, nested ``transaction()`` blocks join
the outer one. Writes made outside a transaction get one of their own; reads
made outside one see only committed data.

Commits and rollbacks are shielded from cancellation, and the lock is not handed
to another task until they have finished, so a cancelled task cannot leave the
connection mid-transaction for the next one.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Coroutine, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

Row = aiosqlite.Row

MEMORY_PATH = ':memory:'


@dataclass(frozen=True)
class ExecResult:
    """What a write statement did.

    ``lastrowid`` is only meaningful after an INSERT that added a row: for any
    other statement SQLite reports the connection's most recent insert.
    """

    rowcount: int
    lastrowid: int | None


class Database:
    """A serialized async SQLite connection. Create one with ``Database.open``."""

    def __init__(self, conn: aiosqlite.Connection, path: str) -> None:
        self._conn = conn
        self._path = path
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[Any] | None = None
        # The latest commit or rollback. It can outlive its task if that task is
        # cancelled while waiting for it, so the next lock holder waits for it.
        self._finishing: asyncio.Future[None] | None = None
        self._closed = False

    @classmethod
    async def open(cls, path: str | Path) -> 'Database':
        """Connect to ``path`` and configure the connection.

        This does not run migrations; see ``migrations.open_database``.
        """
        location = str(path)
        is_memory = location == MEMORY_PATH
        if not is_memory:
            Path(location).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit, so sqlite3 never opens a transaction
        # implicitly. Transactions are begun explicitly by transaction().
        conn = await aiosqlite.connect(location, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            await _configure(conn, is_memory=is_memory)
        except BaseException:
            await conn.close()
            raise
        return cls(conn, location)

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_memory(self) -> bool:
        return self._path == MEMORY_PATH

    async def close(self) -> None:
        """Wait for work in progress, then close the connection. Idempotent."""
        self._refuse_inside_own_transaction('close the database')
        async with self._lock:
            if self._closed:
                return
            await self._wait_for_finishing()
            self._closed = True
            await self._conn.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator['Database']:
        """Run the block in one transaction: commit on success, roll back on error.

        Inside the owning task a nested ``transaction()`` joins the outer one, and
        the outermost block alone commits or rolls back: an error that a nested
        block raises but the outer block handles does not undo the nested writes.

        Any other task, including one started inside the block, waits for the
        lock. So never await such a task from inside a transaction, and keep
        network calls (HTTP, Discord) out of it: every other task waits
        meanwhile. Never publish a post inside one either: its claim must be
        committed before it is sent, so the ledger refuses (see
        ``Publisher.publish``).
        """
        if self.in_transaction():
            yield self
            return
        async with self._exclusive():
            self._owner = asyncio.current_task()
            try:
                await self._run('BEGIN IMMEDIATE')
                yield self
            except BaseException:
                await self._finish(self._rollback())
                raise
            else:
                await self._finish(self._commit())
            finally:
                self._owner = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        """Run one statement, in a transaction of its own unless inside one."""
        async with self.transaction():
            return await self._run(sql, params)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        """Run ``sql`` once per parameter set, atomically; return the total rowcount."""
        # Materialize here: aiosqlite would otherwise iterate ``seq`` on its
        # worker thread.
        param_sets = list(seq)
        async with self.transaction():
            async with self._conn.executemany(sql, param_sets) as cursor:
                return cursor.rowcount

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        async with self._reading():
            async with self._conn.execute(sql, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self._reading():
            return list(await self._conn.execute_fetchall(sql, params))

    async def fetchval(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """The first column of the first row, or None if there are no rows."""
        row = await self.fetchone(sql, params)
        return None if row is None else row[0]

    async def backup_to(self, dest: str | Path) -> None:
        """Copy the committed database to ``dest``, replacing what is there."""
        self._refuse_inside_own_transaction('back up the database')
        # aiosqlite runs the backup on its worker thread, so the target must be a
        # plain sqlite3 connection that may be used from any thread.
        target = sqlite3.connect(str(dest), check_same_thread=False)
        try:
            async with self._exclusive():
                await self._conn.backup(target)
        finally:
            target.close()

    def in_transaction(self) -> bool:
        """Whether the calling task is inside one of its ``transaction()`` blocks.

        A task started inside the block is not: it waits for the lock instead.
        """
        return self._owner is not None and self._owner is asyncio.current_task()

    def _refuse_inside_own_transaction(self, action: str) -> None:
        # Waiting for the lock would deadlock: this task already holds it.
        if self.in_transaction():
            raise RuntimeError(f'Cannot {action} inside one of its transactions')

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[None]:
        """Read access: direct for the owner, else the lock without a transaction."""
        if self.in_transaction():
            yield
        else:
            async with self._exclusive():
                yield

    @asynccontextmanager
    async def _exclusive(self) -> AsyncIterator[None]:
        """Hold the lock, with the connection open and outside any transaction."""
        async with self._lock:
            if self._closed:
                raise sqlite3.ProgrammingError('Cannot operate on a closed database.')
            await self._wait_for_finishing()
            await self._discard_stale_transaction()
            yield

    async def _wait_for_finishing(self) -> None:
        # A commit or rollback abandoned by a cancelled task may still be queued
        # on aiosqlite's worker thread. Until it has run, the connection is still
        # in the previous owner's transaction.
        if self._finishing is not None and not self._finishing.done():
            await asyncio.wait({self._finishing})

    async def _discard_stale_transaction(self) -> None:
        # Owners always finish their transactions, so this only happens if a
        # rollback failed or SQL such as BEGIN was run outside transaction().
        if self._conn.in_transaction:
            logger.warning('Rolling back a KCPC database transaction left open')
            await self._finish(self._rollback())

    async def _run(self, sql: str, params: Sequence[Any] = ()) -> ExecResult:
        async with self._conn.execute(sql, params) as cursor:
            return ExecResult(rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

    async def _finish(self, operation: Coroutine[Any, Any, None]) -> None:
        """Run a commit or rollback to completion, even if the caller is cancelled.

        The operation runs as a task of its own under ``asyncio.shield``. If the
        caller is cancelled while waiting for it, the operation carries on and
        the next lock holder waits for it (see ``_exclusive``).
        """
        task = asyncio.ensure_future(operation)
        self._finishing = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                task.add_done_callback(_log_abandoned_failure)
            raise

    async def _commit(self) -> None:
        try:
            await self._conn.commit()
        except BaseException:
            # A failed COMMIT (e.g. a deferred foreign key violation) leaves the
            # transaction open.
            await self._rollback()
            raise

    async def _rollback(self) -> None:
        """Roll back, logging rather than raising any failure.

        Rollbacks run while another error is already on its way to the caller,
        and that error is the one worth reporting.
        """
        try:
            await self._conn.rollback()
        except Exception:
            logger.exception('Rolling back a KCPC database transaction failed')


async def _configure(conn: aiosqlite.Connection, *, is_memory: bool) -> None:
    """Set the connection's pragmas, outside any transaction."""
    # First, so that switching to WAL waits for other connections, not fails.
    await _pragma(conn, 'busy_timeout = 5000')
    if not is_memory:
        mode = await _pragma(conn, 'journal_mode = WAL')
        if str(mode).lower() != 'wal':
            logger.warning('Could not enable WAL for the KCPC database (mode %s)', mode)
    # FULL, not NORMAL: in WAL mode NORMAL syncs the log only at checkpoints, so
    # a commit can be undone by a power loss or an OS crash. A delivery's claim
    # must be on disk before its post is sent, or an unclean reboot could post
    # it again. FULL costs one sync of the log per commit.
    await _pragma(conn, 'synchronous = FULL')
    await _pragma(conn, 'foreign_keys = ON')


async def _pragma(conn: aiosqlite.Connection, pragma: str) -> Any:
    """Run ``PRAGMA <pragma>`` and return its first value, if it has one."""
    async with conn.execute(f'PRAGMA {pragma}') as cursor:
        row = await cursor.fetchone()
    return None if row is None else row[0]


def _log_abandoned_failure(task: asyncio.Future[None]) -> None:
    """Report a commit that failed after the task waiting for it was cancelled."""
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error(
            'A KCPC database commit failed after its task was cancelled',
            exc_info=error,
        )
