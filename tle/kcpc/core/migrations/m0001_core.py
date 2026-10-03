"""Migration 1: per-guild feature settings, the delivery ledger and job state."""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE guild_settings (
        guild_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        data TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, feature)
    )
    """,
    """
    CREATE TABLE delivery_log (
        key TEXT PRIMARY KEY NOT NULL,
        batch TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        subject TEXT,
        subject_id TEXT,
        kind TEXT,
        occurrence_start INTEGER,
        revision INTEGER,
        status TEXT NOT NULL CHECK (status IN ('claimed', 'sent', 'skipped')),
        channel_id TEXT,
        message_id TEXT,
        reason TEXT,
        payload TEXT,
        claimed_at INTEGER NOT NULL,
        sent_at INTEGER,
        expires_at INTEGER
    )
    """,
    """
    CREATE INDEX ix_delivery_log_subject
    ON delivery_log (guild_id, subject, subject_id, kind)
    """,
    'CREATE INDEX ix_delivery_log_status ON delivery_log (status, claimed_at)',
    'CREATE INDEX ix_delivery_log_batch ON delivery_log (batch)',
    """
    CREATE TABLE job_state (
        job TEXT PRIMARY KEY NOT NULL,
        last_slot INTEGER,
        failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        updated_at INTEGER NOT NULL
    )
    """,
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(1, 'core', apply)
