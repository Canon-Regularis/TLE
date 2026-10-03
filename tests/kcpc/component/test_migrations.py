"""Tests for the kcpc.db migrations and the schemas of migrations 1 to 3."""

import contextlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import MigrationError
from tle.kcpc.core.migrations import (
    ALL_MIGRATIONS,
    Migration,
    migrate,
    open_database,
    schema_version,
)

CORE = ALL_MIGRATIONS[0]
UP_TO_WORKSHOPS = ALL_MIGRATIONS[:2]


async def _create_extra_table(db: Database) -> None:
    await db.execute('CREATE TABLE extra (id INTEGER PRIMARY KEY NOT NULL)')


async def _fail_halfway(db: Database) -> None:
    await db.execute('CREATE TABLE partial (id INTEGER PRIMARY KEY NOT NULL)')
    raise RuntimeError('boom')


async def _apply_then_take_its_version(db: Database) -> None:
    """Apply, then write the migration's own schema_version row, so that the
    runner's insert of that row fails after the migration succeeded."""
    await _create_extra_table(db)
    await db.execute(
        "INSERT INTO schema_version (version, name, applied_at) VALUES (2, 'taken', 0)"
    )


EXTRA = Migration(2, 'extra', _create_extra_table)
BROKEN = Migration(2, 'broken', _fail_halfway)


async def table_names(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row['name'] for row in rows}


def tables_in_file(path: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


async def test_fresh_file_database_is_migrated_to_the_latest_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path)
    try:
        assert await schema_version(db) == 3
        assert await table_names(db) == {
            'schema_version',
            'guild_settings',
            'delivery_log',
            'job_state',
            'event',
            'calendar_state',
            'contest',
            'contest_override',
            'contest_source_state',
        }
        assert await migrate(db, ALL_MIGRATIONS, backup_dir=tmp_path) == []
    finally:
        await db.close()

    db = await open_database(path)  # reopening applies nothing
    await db.close()
    assert list(tmp_path.glob('*.bak')) == []  # nothing was upgraded


async def test_migrate_applies_only_pending_migrations_in_order() -> None:
    db = await Database.open(':memory:')
    try:
        assert await migrate(db, [CORE]) == [1]
        assert await migrate(db, [CORE, EXTRA]) == [2]
        assert 'extra' in await table_names(db)
        rows = await db.fetchall('SELECT version, name FROM schema_version ORDER BY 1')
        assert [tuple(row) for row in rows] == [(1, 'core'), (2, 'extra')]
    finally:
        await db.close()


async def test_failed_migration_leaves_the_previous_version_intact(
    tmp_path: Path,
) -> None:
    db = await open_database(tmp_path / 'kcpc.db', [CORE])
    try:
        with pytest.raises(
            MigrationError, match=r'^Migration 2 \(broken\) failed: boom$'
        ):
            await migrate(db, [CORE, BROKEN])
        assert await schema_version(db) == 1
        assert 'partial' not in await table_names(db)
        assert await db.fetchval('SELECT COUNT(*) FROM schema_version') == 1
    finally:
        await db.close()


async def test_a_failed_version_row_rolls_the_migration_back() -> None:
    # A migration and its schema_version row commit together. Were they
    # separate, a crash between them would leave the migration applied but
    # unrecorded, and every start would fail re-applying it.
    db = await Database.open(':memory:')
    try:
        await migrate(db, [CORE])
        late = Migration(2, 'late', _apply_then_take_its_version)
        with pytest.raises(
            MigrationError, match=r'^Migration 2 \(late\) failed: UNIQUE'
        ):
            await migrate(db, [CORE, late])
        assert await schema_version(db) == 1
        assert 'extra' not in await table_names(db)
        assert await db.fetchval('SELECT COUNT(*) FROM schema_version') == 1
    finally:
        await db.close()


async def test_failed_migration_error_chains_the_original() -> None:
    db = await Database.open(':memory:')
    try:
        with pytest.raises(MigrationError) as excinfo:
            await migrate(db, [CORE, BROKEN])
        assert isinstance(excinfo.value.__cause__, RuntimeError)
    finally:
        await db.close()


async def test_database_newer_than_the_code_is_refused(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path, [CORE, EXTRA])
    await db.close()
    with pytest.raises(MigrationError, match='newer than this code'):
        await open_database(path, [CORE])


async def test_open_database_closes_the_database_when_migrating_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[str] = []
    close = Database.close

    async def recording_close(self: Database) -> None:
        closed.append(self.path)
        await close(self)

    monkeypatch.setattr(Database, 'close', recording_close)
    path = tmp_path / 'kcpc.db'
    with pytest.raises(MigrationError):
        await open_database(path, [CORE, BROKEN])
    assert closed == [str(path)]


async def test_upgrade_backs_up_the_previous_version(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path, [CORE])
    await db.execute("INSERT INTO job_state (job, updated_at) VALUES ('probe', 0)")
    await db.close()

    db = await open_database(path, [CORE, EXTRA])
    try:
        assert await schema_version(db) == 2
    finally:
        await db.close()

    backup = tmp_path / 'kcpc.db.v1.bak'
    assert 'extra' not in tables_in_file(backup)
    with contextlib.closing(sqlite3.connect(backup)) as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone() == (
            1,
        )
        assert conn.execute('SELECT job FROM job_state').fetchall() == [('probe',)]


async def test_a_version_1_database_upgrades_to_version_2(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path, [CORE])
    await db.execute(
        'INSERT INTO guild_settings (guild_id, feature, data, updated_at) '
        "VALUES ('1', 'workshops', '{\"enabled\": true}', 0)"
    )
    await db.close()

    db = await open_database(path, UP_TO_WORKSHOPS)
    try:
        assert await schema_version(db) == 2
        rows = await db.fetchall('SELECT version, name FROM schema_version ORDER BY 1')
        assert [tuple(row) for row in rows] == [(1, 'core'), (2, 'workshops')]
        assert {'event', 'calendar_state'} <= await table_names(db)
        assert await db.fetchval('SELECT data FROM guild_settings') == (
            '{"enabled": true}'
        )
    finally:
        await db.close()

    backup = tmp_path / 'kcpc.db.v1.bak'
    assert tables_in_file(backup) == {
        'schema_version',
        'guild_settings',
        'delivery_log',
        'job_state',
    }
    with contextlib.closing(sqlite3.connect(backup)) as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone() == (
            1,
        )


async def test_a_version_2_database_upgrades_to_version_3(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path, UP_TO_WORKSHOPS)
    await db.execute(INSERT_EVENT, ('cal-a', 'evt-1'))
    await db.close()

    db = await open_database(path)
    try:
        assert await schema_version(db) == 3
        rows = await db.fetchall('SELECT version, name FROM schema_version ORDER BY 1')
        assert [tuple(row) for row in rows] == [
            (1, 'core'),
            (2, 'workshops'),
            (3, 'contests'),
        ]
        assert {
            'contest',
            'contest_override',
            'contest_source_state',
        } <= await table_names(db)
        assert await db.fetchval('SELECT luma_id FROM event') == 'evt-1'
    finally:
        await db.close()

    backup = tmp_path / 'kcpc.db.v2.bak'
    assert 'contest' not in tables_in_file(backup)
    with contextlib.closing(sqlite3.connect(backup)) as conn:
        assert conn.execute('SELECT MAX(version) FROM schema_version').fetchone() == (
            2,
        )
        assert conn.execute('SELECT luma_id FROM event').fetchall() == [('evt-1',)]


async def test_no_backup_when_disabled(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    await (await open_database(path, [CORE])).close()
    db = await open_database(path, [CORE, EXTRA], backup=False)
    try:
        assert await schema_version(db) == 2  # upgraded, without a backup
    finally:
        await db.close()
    assert list(tmp_path.glob('*.bak')) == []


async def test_failed_backup_stops_the_upgrade(tmp_path: Path) -> None:
    path = tmp_path / 'kcpc.db'
    db = await open_database(path, [CORE])
    not_a_directory = tmp_path / 'occupied'
    not_a_directory.write_text('a file where the backup directory should be')
    try:
        with pytest.raises(MigrationError, match='Could not back up'):
            await migrate(db, [CORE, EXTRA], backup_dir=not_a_directory)
        assert await schema_version(db) == 1
    finally:
        await db.close()


@pytest.mark.parametrize(
    'versions',
    [(2,), (1, 1), (1, 3), (2, 1), (0, 1)],
    ids=lambda versions: ','.join(map(str, versions)),
)
async def test_badly_numbered_migrations_are_a_programming_error(
    versions: Sequence[int],
) -> None:
    migrations = [Migration(v, f'm{v}', _create_extra_table) for v in versions]
    db = await Database.open(':memory:')
    try:
        with pytest.raises(ValueError, match='1, 2, 3'):
            await migrate(db, migrations)
        assert await table_names(db) == set()  # checked before touching anything
    finally:
        await db.close()


async def test_schema_version() -> None:
    db = await Database.open(':memory:')
    try:
        assert await schema_version(db) == 0  # no schema_version table
        assert await migrate(db, []) == []
        assert await schema_version(db) == 0  # the table exists but is empty
        await migrate(db, [CORE, EXTRA])
        assert await schema_version(db) == 2
    finally:
        await db.close()


EXPECTED_COLUMNS = {
    'schema_version': ['version', 'name', 'applied_at'],
    'guild_settings': ['guild_id', 'feature', 'data', 'updated_at'],
    'delivery_log': [
        'key',
        'batch',
        'guild_id',
        'feature',
        'subject',
        'subject_id',
        'kind',
        'occurrence_start',
        'revision',
        'status',
        'channel_id',
        'message_id',
        'reason',
        'payload',
        'claimed_at',
        'sent_at',
        'expires_at',
    ],
    'job_state': ['job', 'last_slot', 'failures', 'last_error', 'updated_at'],
}


async def test_core_schema(db: Database) -> None:
    for table, expected in EXPECTED_COLUMNS.items():
        columns = await db.fetchall(f'PRAGMA table_info({table})')
        assert [column['name'] for column in columns] == expected, table
        for column in columns:
            if column['pk']:
                assert column['notnull'], f'{table}.{column["name"]} allows NULL'

    indexes = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'index'")
    assert {row['name'] for row in indexes} >= {
        'ix_delivery_log_subject',
        'ix_delivery_log_status',
        'ix_delivery_log_batch',
    }

    await db.execute("INSERT INTO job_state (job, updated_at) VALUES ('j', 0)")
    assert await db.fetchval("SELECT failures FROM job_state WHERE job = 'j'") == 0
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        await db.execute(
            'INSERT INTO delivery_log (key, batch, guild_id, feature, status, '
            "claimed_at) VALUES ('k', 'b', '1', 'f', 'lost', 0)"
        )


WORKSHOP_COLUMNS = {
    'event': [
        'event_id',
        'calendar_id',
        'luma_id',
        'name',
        'start_time',
        'end_time',
        'url',
        'location',
        'status',
        'revision',
        'fingerprint',
        'miss_count',
        'first_seen',
        'last_synced',
    ],
    'calendar_state': [
        'calendar_id',
        'last_attempt',
        'last_ok',
        'last_future_count',
        'consecutive_failures',
        'last_error',
    ],
}
WORKSHOP_NOT_NULL = {
    'event': {
        'calendar_id',
        'luma_id',
        'name',
        'start_time',
        'status',
        'revision',
        'fingerprint',
        'miss_count',
        'first_seen',
        'last_synced',
    },
    'calendar_state': {'calendar_id', 'consecutive_failures'},
}
INSERT_EVENT = (
    'INSERT INTO event (event_id, calendar_id, luma_id, name, start_time, '
    "fingerprint, first_seen, last_synced) VALUES (NULL, ?, ?, 'DP', 0, 'f', 0, 0)"
)


async def test_workshops_schema(db: Database) -> None:
    for table, expected in WORKSHOP_COLUMNS.items():
        columns = await db.fetchall(f'PRAGMA table_info({table})')
        assert [column['name'] for column in columns] == expected, table
        not_null = {column['name'] for column in columns if column['notnull']}
        assert not_null == WORKSHOP_NOT_NULL[table], table
    index = await db.fetchall('PRAGMA index_info(ix_event_calendar_start)')
    assert [row['name'] for row in index] == ['calendar_id', 'start_time']

    # event_id is the rowid, so even an explicit NULL gets an id.
    await db.execute(INSERT_EVENT, ('cal-a', 'evt-1'))
    row = await db.fetchone(
        'SELECT event_id, status, revision, miss_count, end_time, url, location '
        'FROM event'
    )
    assert tuple(row or ()) == (1, 'scheduled', 0, 0, None, None, None)

    # Each calendar stores a Luma event once.
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        await db.execute(INSERT_EVENT, ('cal-a', 'evt-1'))
    await db.execute(INSERT_EVENT, ('cal-b', 'evt-1'))
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        await db.execute("UPDATE event SET status = 'deleted'")

    await db.execute("INSERT INTO calendar_state (calendar_id) VALUES ('cal-a')")
    row = await db.fetchone('SELECT * FROM calendar_state')
    assert tuple(row or ()) == ('cal-a', None, None, None, 0, None)


CONTEST_COLUMNS = {
    'contest': [
        'contest_id',
        'platform',
        'external_id',
        'name',
        'start_time',
        'start_date',
        'end_time',
        'url',
        'status',
        'revision',
        'fingerprint',
        'miss_count',
        'first_seen',
        'last_synced',
    ],
    'contest_override': [
        'platform',
        'external_id',
        'start_time',
        'end_time',
        'set_by',
        'set_at',
    ],
    'contest_source_state': [
        'source',
        'last_attempt',
        'last_ok',
        'last_future_count',
        'consecutive_failures',
        'last_error',
    ],
}
CONTEST_NOT_NULL = {
    'contest': {
        'platform',
        'external_id',
        'name',
        'status',
        'revision',
        'fingerprint',
        'miss_count',
        'first_seen',
        'last_synced',
    },
    'contest_override': {'platform', 'external_id', 'set_by', 'set_at'},
    'contest_source_state': {'source', 'consecutive_failures'},
}
INSERT_CONTEST = (
    'INSERT INTO contest (contest_id, platform, external_id, name, start_time, '
    'start_date, fingerprint, first_seen, last_synced) '
    "VALUES (NULL, ?, ?, 'ABC 478', ?, ?, 'f', 0, 0)"
)
INSERT_OVERRIDE = (
    'INSERT INTO contest_override (platform, external_id, start_time, set_by, '
    "set_at) VALUES ('icpc', '9584', 0, 'admin', 0)"
)


async def test_contests_schema(db: Database) -> None:
    for table, expected in CONTEST_COLUMNS.items():
        columns = await db.fetchall(f'PRAGMA table_info({table})')
        assert [column['name'] for column in columns] == expected, table
        not_null = {column['name'] for column in columns if column['notnull']}
        assert not_null == CONTEST_NOT_NULL[table], table
    index = await db.fetchall('PRAGMA index_info(ix_contest_start)')
    assert [row['name'] for row in index] == ['start_time']

    # contest_id is the rowid, so even an explicit NULL gets an id.
    await db.execute(INSERT_CONTEST, ('atcoder', 'abc478', 0, None))
    row = await db.fetchone(
        'SELECT contest_id, status, revision, miss_count, start_date, end_time, url '
        'FROM contest'
    )
    assert tuple(row or ()) == (1, 'scheduled', 0, 0, None, None, None)

    # A platform stores a contest once, and every contest has a start or a date.
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        await db.execute(INSERT_CONTEST, ('atcoder', 'abc478', 0, None))
    await db.execute(INSERT_CONTEST, ('codeforces', 'abc478', 0, None))
    await db.execute(INSERT_CONTEST, ('icpc', '9584', None, '2026-10-17'))
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        await db.execute(INSERT_CONTEST, ('icpc', '9585', None, None))
    with pytest.raises(sqlite3.IntegrityError, match='CHECK'):
        await db.execute("UPDATE contest SET status = 'deleted'")

    # A contest has one override at most.
    await db.execute(INSERT_OVERRIDE)
    with pytest.raises(sqlite3.IntegrityError, match='UNIQUE'):
        await db.execute(INSERT_OVERRIDE)

    await db.execute("INSERT INTO contest_source_state (source) VALUES ('atcoder')")
    row = await db.fetchone('SELECT * FROM contest_source_state')
    assert tuple(row or ()) == ('atcoder', None, None, None, 0, None)
