"""Migration 4: linked accounts, link challenges and rating snapshots.

``linked_account`` holds the handles that members have proved are theirs, one
per platform for each member of a guild. Codeforces handles stay in TLE's own
``user_handle`` table, so for now it holds AtCoder handles. ``link_challenge``
holds the token that a member must show on their profile while a link is
pending. ``account_snapshot`` holds each handle's ratings as last fetched, on
either platform, however many guilds link it.

Handles compare case-insensitively, so that a handle is linked to one member of
a guild at most, whatever case it is typed in.
"""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE linked_account (
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        handle TEXT NOT NULL COLLATE NOCASE,
        method TEXT NOT NULL,
        verified_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id, platform),
        UNIQUE (guild_id, platform, handle)
    )
    """,
    """
    CREATE TABLE link_challenge (
        guild_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        handle TEXT NOT NULL,
        token TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, user_id, platform)
    )
    """,
    """
    CREATE TABLE account_snapshot (
        platform TEXT NOT NULL,
        handle TEXT NOT NULL COLLATE NOCASE,
        rating INTEGER,
        max_rating INTEGER,
        rank TEXT,
        rated_matches INTEGER,
        fetched_at INTEGER NOT NULL,
        PRIMARY KEY (platform, handle)
    )
    """,
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(4, 'accounts', apply)
