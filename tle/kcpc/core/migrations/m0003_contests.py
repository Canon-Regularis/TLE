"""Migration 3: contests, the times admins set for them, and each source's sync state.

A contest whose source knows only its date (an ICPC regional, say) has a
``start_date`` ('YYYY-MM-DD') but no ``start_time`` until it gets one, so every
contest has at least one of the two. ``contest_override`` holds the times that
admins set, apart from what sources report, so that syncing never overwrites
them.
"""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE contest (
        contest_id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        start_time INTEGER,
        start_date TEXT,
        end_time INTEGER,
        url TEXT,
        status TEXT NOT NULL DEFAULT 'scheduled'
            CHECK (status IN ('scheduled', 'cancelled')),
        revision INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT NOT NULL,
        miss_count INTEGER NOT NULL DEFAULT 0,
        first_seen INTEGER NOT NULL,
        last_synced INTEGER NOT NULL,
        UNIQUE (platform, external_id),
        CHECK (start_time IS NOT NULL OR start_date IS NOT NULL)
    )
    """,
    'CREATE INDEX ix_contest_start ON contest (start_time)',
    """
    CREATE TABLE contest_override (
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        start_time INTEGER,
        end_time INTEGER,
        set_by TEXT NOT NULL,
        set_at INTEGER NOT NULL,
        PRIMARY KEY (platform, external_id)
    )
    """,
    """
    CREATE TABLE contest_source_state (
        source TEXT PRIMARY KEY NOT NULL,
        last_attempt INTEGER,
        last_ok INTEGER,
        last_future_count INTEGER,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT
    )
    """,
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(3, 'contests', apply)
