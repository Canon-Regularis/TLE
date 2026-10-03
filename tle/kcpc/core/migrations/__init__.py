"""Schema migrations for kcpc.db.

Each migration is a module exposing ``MIGRATION``, a ``Migration``, listed in
order in ``ALL_MIGRATIONS``. Its ``apply`` runs inside a transaction, one
``db.execute()`` per statement (``executescript`` would commit on its own), and
the matching ``schema_version`` row is written in that same transaction, so a
migration either applies completely or leaves no trace. Never edit a released
migration: change the schema by adding a new one.
"""

import logging
import time
from collections.abc import Sequence
from pathlib import Path

from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import MigrationError
from tle.kcpc.core.migrations import m0001_core, m0002_workshops, m0003_contests
from tle.kcpc.core.migrations.base import Migration

__all__ = [
    'ALL_MIGRATIONS',
    'Migration',
    'migrate',
    'open_database',
    'schema_version',
]

logger = logging.getLogger(__name__)

ALL_MIGRATIONS: tuple[Migration, ...] = (
    m0001_core.MIGRATION,
    m0002_workshops.MIGRATION,
    m0003_contests.MIGRATION,
)

_CREATE_SCHEMA_VERSION = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        applied_at INTEGER NOT NULL
    )
"""


async def schema_version(db: Database) -> int:
    """The highest applied migration version, or 0 if none has been applied."""
    has_table = await db.fetchval(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    )
    if not has_table:
        return 0
    return int(
        await db.fetchval('SELECT COALESCE(MAX(version), 0) FROM schema_version')
    )


async def migrate(
    db: Database, migrations: Sequence[Migration], *, backup_dir: Path | None = None
) -> list[int]:
    """Apply the pending ``migrations`` in order and return their versions.

    Before upgrading an existing file database, it is backed up into
    ``backup_dir`` (if given) as ``kcpc.db.v<current>.bak``.

    Raises ``MigrationError`` if the database is newer than ``migrations``, or if
    the backup or a migration fails; each failed migration is rolled back.
    Raises ``ValueError`` if ``migrations`` is not numbered 1, 2, 3, ... in order.
    """
    _check_numbering(migrations)
    await db.execute(_CREATE_SCHEMA_VERSION)
    current = await schema_version(db)
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise MigrationError(
            f'The kcpc.db schema (version {current}) is newer than this code '
            f'(version {latest}); refusing to start.'
        )
    pending = [migration for migration in migrations if migration.version > current]
    if pending and backup_dir is not None and current > 0 and not db.is_memory:
        await _back_up(db, backup_dir / f'kcpc.db.v{current}.bak', current)
    for migration in pending:
        await _apply(db, migration)
    return [migration.version for migration in pending]


async def open_database(
    path: str | Path,
    migrations: Sequence[Migration] = ALL_MIGRATIONS,
    *,
    backup: bool = True,
) -> Database:
    """Open the database at ``path`` and bring its schema up to date.

    With ``backup``, a file database is backed up next to itself before an
    upgrade. If anything fails, the database is closed before the error
    propagates.
    """
    db = await Database.open(path)
    try:
        backup_dir = Path(db.path).parent if backup and not db.is_memory else None
        await migrate(db, migrations, backup_dir=backup_dir)
    except BaseException:
        await db.close()
        raise
    return db


def _check_numbering(migrations: Sequence[Migration]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(
            f'Migration versions must be 1, 2, 3, ... in order, not {versions}'
        )


async def _back_up(db: Database, dest: Path, version: int) -> None:
    try:
        await db.backup_to(dest)
    except Exception as exc:
        raise MigrationError(
            f'Could not back up kcpc.db to {dest} before migrating: {exc}'
        ) from exc
    logger.info('Backed up kcpc.db (schema version %d) to %s', version, dest)


async def _apply(db: Database, migration: Migration) -> None:
    try:
        async with db.transaction():
            await migration.apply(db)
            await db.execute(
                'INSERT INTO schema_version (version, name, applied_at) '
                'VALUES (?, ?, ?)',
                (migration.version, migration.name, int(time.time())),
            )
    except Exception as exc:
        raise MigrationError(
            f'Migration {migration.version} ({migration.name}) failed: {exc}'
        ) from exc
    logger.info('Applied kcpc.db migration %d (%s)', migration.version, migration.name)
