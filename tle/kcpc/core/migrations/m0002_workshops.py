"""Migration 2: Luma events and each calendar's sync state, for workshops."""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE event (
        event_id INTEGER PRIMARY KEY,
        calendar_id TEXT NOT NULL,
        luma_id TEXT NOT NULL,
        name TEXT NOT NULL,
        start_time INTEGER NOT NULL,
        end_time INTEGER,
        url TEXT,
        location TEXT,
        status TEXT NOT NULL DEFAULT 'scheduled'
            CHECK (status IN ('scheduled', 'cancelled')),
        revision INTEGER NOT NULL DEFAULT 0,
        fingerprint TEXT NOT NULL,
        miss_count INTEGER NOT NULL DEFAULT 0,
        first_seen INTEGER NOT NULL,
        last_synced INTEGER NOT NULL,
        UNIQUE (calendar_id, luma_id)
    )
    """,
    'CREATE INDEX ix_event_calendar_start ON event (calendar_id, start_time)',
    """
    CREATE TABLE calendar_state (
        calendar_id TEXT PRIMARY KEY NOT NULL,
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


MIGRATION = Migration(2, 'workshops', apply)
