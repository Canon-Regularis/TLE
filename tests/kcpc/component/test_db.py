"""Tests for tle.kcpc.core.db: transactions, serialization and cancellation."""

import asyncio
import contextlib
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tle.kcpc.core.db import Database, ExecResult

CREATE_ITEM = 'CREATE TABLE item (id INTEGER PRIMARY KEY, name TEXT NOT NULL)'


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    """A bare in-memory database with an ``item`` table and one ``counter`` row."""
    db = await Database.open(':memory:')
    await db.execute(CREATE_ITEM)
    await db.execute('CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER)')
    await db.execute('INSERT INTO counter (id, value) VALUES (1, 0)')
    yield db
    await db.close()


@pytest.fixture
async def file_database(tmp_path: Path) -> AsyncIterator[Database]:
    """A bare file database with an ``item`` table."""
    db = await Database.open(tmp_path / 'kcpc.db')
    await db.execute(CREATE_ITEM)
    yield db
    await db.close()


@pytest.fixture
def write_lock_holder(file_database: Database) -> Iterator[sqlite3.Connection]:
    """A second connection holding the file database's write lock.

    Until it rolls back, the database's BEGIN IMMEDIATE waits on aiosqlite's
    worker thread, so tests can cancel a task while a statement is in flight.
    """
    holder = sqlite3.connect(file_database.path, isolation_level=None)
    holder.execute('BEGIN IMMEDIATE')
    yield holder
    holder.rollback()
    holder.close()


@pytest.fixture
async def commit_blocker(file_database: Database) -> AsyncIterator[sqlite3.Connection]:
    """A second connection in a read transaction that makes COMMIT wait.

    In rollback-journal mode (unlike WAL) a COMMIT must wait for readers to
    finish, so tests can cancel a task while its COMMIT is in flight.
    """
    await file_database.fetchall('PRAGMA journal_mode = DELETE')
    blocker = sqlite3.connect(file_database.path, isolation_level=None)
    blocker.execute('BEGIN')
    blocker.execute('SELECT * FROM item').fetchall()
    yield blocker
    blocker.rollback()
    blocker.close()


@pytest.fixture
async def commit_pause(database: Database) -> AsyncIterator[threading.Event]:
    """Makes aiosqlite's worker thread wait before each COMMIT until it is set.

    This reaches into the connection because the race it opens up (the next
    task inspecting the connection before the worker has even started the
    previous owner's COMMIT) cannot be forced through the public API.
    """
    resume = threading.Event()

    def trace(sql: str) -> None:
        if sql == 'COMMIT':
            resume.wait(timeout=5)

    await database._conn.set_trace_callback(trace)
    yield resume
    resume.set()  # never leave the worker stuck, even if the test failed


async def insert(db: Database, name: str) -> None:
    await db.execute('INSERT INTO item (name) VALUES (?)', (name,))


async def names(db: Database) -> list[str]:
    rows = await db.fetchall('SELECT name FROM item ORDER BY name')
    return [row['name'] for row in rows]


def committed_names(path: str | Path) -> list[str]:
    """The names an independent connection sees, i.e. the committed ones."""
    with contextlib.closing(sqlite3.connect(path)) as reader:
        return [
            name for (name,) in reader.execute('SELECT name FROM item ORDER BY name')
        ]


async def committing_insert(db: Database, name: str) -> asyncio.Task[None]:
    """Start a task inserting ``name``; return it once its COMMIT is queued."""
    body_done = asyncio.Event()

    async def writer() -> None:
        async with db.transaction():
            await insert(db, name)
            body_done.set()

    task = asyncio.create_task(writer())
    await body_done.wait()
    # The writer left its block in the same step that set the event, so its
    # commit task is scheduled; one turn of the loop lets it queue the COMMIT.
    await asyncio.sleep(0)
    return task


async def let_tasks_run() -> None:
    """Give every ready task a turn, and worker threads a moment, to make progress."""
    await asyncio.sleep(0.02)
    for _ in range(10):
        await asyncio.sleep(0)


def db_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == 'tle.kcpc.core.db' and record.levelno >= logging.WARNING
    ]


async def wait_for_log(caplog: pytest.LogCaptureFixture, text: str) -> None:
    """Wait (up to 2 s) for a log message that a background task will write."""
    for _ in range(200):
        if any(text in message for message in db_warnings(caplog)):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f'No log message containing {text!r}')


async def test_transaction_commits_when_the_block_succeeds(
    file_database: Database,
) -> None:
    async with file_database.transaction() as tx:
        assert tx is file_database
        await insert(file_database, 'a')
        await insert(file_database, 'b')
        assert await names(file_database) == ['a', 'b']  # the owner sees its writes
        assert committed_names(file_database.path) == []  # nobody else does yet
    assert committed_names(file_database.path) == ['a', 'b']


async def test_transaction_rolls_back_when_the_block_raises(
    file_database: Database,
) -> None:
    with pytest.raises(RuntimeError, match='boom'):
        async with file_database.transaction():
            await insert(file_database, 'a')
            raise RuntimeError('boom')
    assert await names(file_database) == []
    assert committed_names(file_database.path) == []


async def test_nested_transaction_joins_the_outer_one(file_database: Database) -> None:
    async with file_database.transaction():
        async with file_database.transaction():
            await insert(file_database, 'inner')
        # Leaving the nested block did not commit: the outermost block decides.
        assert committed_names(file_database.path) == []
    assert committed_names(file_database.path) == ['inner']


async def test_failure_in_a_nested_block_rolls_back_everything(
    database: Database,
) -> None:
    with pytest.raises(RuntimeError):
        async with database.transaction():
            await insert(database, 'outer')
            async with database.transaction():
                await insert(database, 'inner')
                raise RuntimeError('nested failure')
    assert await names(database) == []


async def test_outer_block_that_handles_a_nested_failure_still_commits(
    database: Database,
) -> None:
    async with database.transaction():
        await insert(database, 'outer')
        with contextlib.suppress(RuntimeError):
            async with database.transaction():
                await insert(database, 'inner')
                raise RuntimeError('handled by the outer block')
    # No savepoints: the nested writes stand unless the outermost block fails.
    assert await names(database) == ['inner', 'outer']


async def test_concurrent_read_modify_write_loses_no_updates(
    database: Database,
) -> None:
    async def increment() -> None:
        async with database.transaction():
            value = await database.fetchval('SELECT value FROM counter WHERE id = 1')
            await asyncio.sleep(0)  # invite the other tasks to interleave
            await database.execute(
                'UPDATE counter SET value = ? WHERE id = 1', (value + 1,)
            )

    await asyncio.gather(*(increment() for _ in range(50)))
    assert await database.fetchval('SELECT value FROM counter WHERE id = 1') == 50


async def test_writes_outside_a_transaction_are_serialized(database: Database) -> None:
    expected = [f'item {i:02}' for i in range(20)]
    await asyncio.gather(*(insert(database, name) for name in expected))
    assert await names(database) == expected


async def test_task_cancelled_mid_transaction_leaves_no_trace(
    file_database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    wrote = asyncio.Event()

    async def writer() -> None:
        async with file_database.transaction():
            await insert(file_database, 'doomed')
            wrote.set()
            await asyncio.Event().wait()  # until cancelled

    task = asyncio.create_task(writer())
    await wrote.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert committed_names(file_database.path) == []
    await insert(file_database, 'after')
    assert committed_names(file_database.path) == ['after']
    assert db_warnings(caplog) == []


async def test_cancel_while_a_statement_is_in_flight(
    file_database: Database,
    write_lock_holder: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = asyncio.create_task(insert(file_database, 'doomed'))
    await let_tasks_run()  # its BEGIN IMMEDIATE now waits for the write lock
    task.cancel()
    await let_tasks_run()
    # The rollback is queued behind the waiting BEGIN, and the task (holding the
    # lock) waits for it rather than hand over a connection mid-transaction.
    assert not task.done()
    write_lock_holder.rollback()
    with pytest.raises(asyncio.CancelledError):
        await task
    await insert(file_database, 'after')
    assert committed_names(file_database.path) == ['after']
    assert db_warnings(caplog) == []


async def test_second_cancel_does_not_hand_over_an_unfinished_rollback(
    file_database: Database,
    write_lock_holder: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = asyncio.create_task(insert(file_database, 'doomed'))
    await let_tasks_run()
    task.cancel()
    await let_tasks_run()
    task.cancel()  # stop waiting: the rollback carries on in the background
    with pytest.raises(asyncio.CancelledError):
        await task

    follower = asyncio.create_task(insert(file_database, 'follower'))
    await let_tasks_run()
    assert not follower.done()
    write_lock_holder.rollback()
    await follower
    assert committed_names(file_database.path) == ['follower']
    assert db_warnings(caplog) == []


async def test_cancel_during_a_blocked_commit_still_commits(
    file_database: Database,
    commit_blocker: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = await committing_insert(file_database, 'a')  # the reader blocks it
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # returns at once; the shielded COMMIT carries on

    follower = asyncio.create_task(insert(file_database, 'b'))
    await let_tasks_run()
    assert not follower.done()
    commit_blocker.rollback()
    await follower
    assert committed_names(file_database.path) == ['a', 'b']
    assert db_warnings(caplog) == []


async def test_next_task_waits_for_a_commit_the_worker_has_not_started(
    database: Database,
    commit_pause: threading.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = await committing_insert(database, 'a')  # queued, but not yet begun
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    follower = asyncio.create_task(insert(database, 'b'))
    await let_tasks_run()
    # The connection is still mid-transaction, so the follower must wait for the
    # commit rather than mistake it for a transaction left open (and roll back).
    assert not follower.done()
    commit_pause.set()
    await follower
    assert await names(database) == ['a', 'b']
    assert db_warnings(caplog) == []


async def test_commit_failing_after_its_task_was_cancelled_is_logged(
    file_database: Database,
    commit_blocker: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await file_database.fetchall('PRAGMA busy_timeout = 100')
    task = await committing_insert(file_database, 'a')
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await wait_for_log(caplog, 'commit failed after its task was cancelled')
    commit_blocker.rollback()
    await insert(file_database, 'b')
    assert committed_names(file_database.path) == ['b']
    assert len(db_warnings(caplog)) == 1  # no transaction was left open


async def test_failed_commit_is_rolled_back(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await database.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
    await database.execute(
        'CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER '
        'REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)'
    )
    # The foreign key is only checked by COMMIT, which then fails and leaves
    # the transaction open.
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        async with database.transaction():
            await database.execute('INSERT INTO child (id, parent_id) VALUES (1, 99)')
    assert await database.fetchval('SELECT COUNT(*) FROM child') == 0
    await insert(database, 'after')  # a new transaction can begin
    assert db_warnings(caplog) == []


async def test_reader_in_a_child_task_waits_for_the_transaction(
    file_database: Database,
) -> None:
    async with file_database.transaction():
        await insert(file_database, 'a')
        reader = asyncio.create_task(names(file_database))
        await let_tasks_run()
        assert not reader.done()  # it may not see the open transaction
    assert await reader == ['a']


async def test_writer_in_a_child_task_is_not_part_of_the_transaction(
    database: Database,
) -> None:
    with pytest.raises(RuntimeError):
        async with database.transaction():
            await insert(database, 'parent')
            child = asyncio.create_task(insert(database, 'child'))
            await let_tasks_run()
            assert not child.done()
            raise RuntimeError('roll back the parent')
    await child
    assert await names(database) == ['child']


async def test_in_transaction_is_true_only_in_the_owning_tasks_block(
    database: Database,
) -> None:
    child_saw: list[bool] = []

    async def child() -> None:
        # Only a check, so it needs no lock and may be awaited in the block.
        child_saw.append(database.in_transaction())

    assert not database.in_transaction()
    async with database.transaction():
        assert database.in_transaction()
        async with database.transaction():
            assert database.in_transaction()
        await asyncio.create_task(child())
    assert child_saw == [False]
    assert not database.in_transaction()  # after a commit
    with pytest.raises(RuntimeError):
        async with database.transaction():
            raise RuntimeError('roll back')
    assert not database.in_transaction()  # after a rollback


async def test_transaction_left_open_outside_the_api_is_rolled_back(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await database.fetchall('BEGIN')  # SQL that bypasses transaction()
    await insert(database, 'after')
    assert await names(database) == ['after']
    assert db_warnings(caplog) == ['Rolling back a KCPC database transaction left open']


async def test_fetch_helpers(database: Database) -> None:
    await insert(database, 'a')
    await insert(database, 'b')

    row = await database.fetchone('SELECT id, name FROM item ORDER BY name')
    assert row is not None
    assert (row['id'], row['name']) == (1, 'a')
    assert await database.fetchone('SELECT * FROM item WHERE name = ?', ('z',)) is None

    rows = await database.fetchall('SELECT name FROM item ORDER BY name DESC')
    assert [tuple(row) for row in rows] == [('b',), ('a',)]
    assert await database.fetchall('SELECT * FROM item WHERE id < 0') == []

    assert await database.fetchval('SELECT COUNT(*) FROM item') == 2
    assert await database.fetchval('SELECT name FROM item WHERE id = ?', (2,)) == 'b'
    assert await database.fetchval('SELECT name FROM item WHERE id < 0') is None


async def test_execute_reports_rowcount_and_lastrowid(database: Database) -> None:
    assert await database.execute("INSERT INTO item (name) VALUES ('a')") == ExecResult(
        rowcount=1, lastrowid=1
    )
    assert (
        await database.execute("INSERT INTO item (name) VALUES ('b')")
    ).lastrowid == 2
    assert (await database.execute("UPDATE item SET name = 'c'")).rowcount == 2
    assert (await database.execute('DELETE FROM item WHERE id < 0')).rowcount == 0


async def test_executemany_returns_the_total_rowcount(database: Database) -> None:
    count = await database.executemany(
        'INSERT INTO item (name) VALUES (?)', ((name,) for name in 'abc')
    )
    assert count == 3
    assert await names(database) == ['a', 'b', 'c']


async def test_executemany_is_atomic(database: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await database.executemany(
            'INSERT INTO item (name) VALUES (?)', [('a',), (None,), ('c',)]
        )
    assert await names(database) == []


async def test_file_database_configuration(tmp_path: Path) -> None:
    path = tmp_path / 'nested' / 'dir' / 'kcpc.db'
    db = await Database.open(path)  # creates the missing directories
    try:
        assert db.path == str(path)
        assert not db.is_memory
        assert await db.fetchval('PRAGMA journal_mode') == 'wal'
        assert await db.fetchval('PRAGMA foreign_keys') == 1
        assert await db.fetchval('PRAGMA busy_timeout') == 5000
        # FULL: under WAL, NORMAL lets a power loss undo a committed claim,
        # whose post would then be sent again. A power cut can't be staged
        # here, so this pin is the regression test.
        assert await db.fetchval('PRAGMA synchronous') == 2  # FULL
    finally:
        await db.close()
    assert path.is_file()


async def test_memory_database_configuration(database: Database) -> None:
    assert database.path == ':memory:'
    assert database.is_memory
    assert await database.fetchval('PRAGMA journal_mode') == 'memory'
    assert await database.fetchval('PRAGMA foreign_keys') == 1


async def test_backup_to_produces_a_readable_copy(
    file_database: Database, database: Database, tmp_path: Path
) -> None:
    dest = tmp_path / 'backup.db'
    await insert(file_database, 'a')
    await file_database.backup_to(dest)
    await insert(file_database, 'b')
    await file_database.backup_to(dest)  # replaces the earlier copy
    assert committed_names(dest) == ['a', 'b']

    await insert(database, 'memory')
    await database.backup_to(tmp_path / 'memory.db')
    assert committed_names(tmp_path / 'memory.db') == ['memory']


async def test_close_is_idempotent_and_later_use_fails(database: Database) -> None:
    await database.close()
    await database.close()
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        await database.fetchval('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        await insert(database, 'a')


async def test_close_waits_for_a_running_transaction(file_database: Database) -> None:
    wrote = asyncio.Event()
    finish = asyncio.Event()

    async def writer() -> None:
        async with file_database.transaction():
            await insert(file_database, 'a')
            wrote.set()
            await finish.wait()

    task = asyncio.create_task(writer())
    await wrote.wait()
    closing = asyncio.create_task(file_database.close())
    await let_tasks_run()
    assert not closing.done()
    finish.set()
    await task
    await closing
    assert committed_names(file_database.path) == ['a']


async def test_closing_or_backing_up_inside_a_transaction_is_refused(
    database: Database, tmp_path: Path
) -> None:
    async with database.transaction():
        with pytest.raises(RuntimeError, match='inside'):
            await database.close()
        with pytest.raises(RuntimeError, match='inside'):
            await database.backup_to(tmp_path / 'backup.db')
    assert await database.fetchval('SELECT 1') == 1  # still open and usable
