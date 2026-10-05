"""Migration 7: contest results, members' rating changes after a contest.

``contest_result`` holds one row per Codeforces or AtCoder contest whose
results the bot has worked on, by its platform and its ID there, as
``contest.external_id`` and the delivery keys have them: a Codeforces contest
from TLE's cache may be missing from ``contest``. Its ``status`` is
'watching' while the bot reads AtCoder profiles for the contest's rating
changes, 'posting' once it has them and a server's post is still to go out,
and 'done' with an ``outcome`` at the end. ``next_check`` is when the AtCoder
profiles are read next, and ``checks`` how many times they have been read
since the contest ended.

``contest_result_entry`` holds one row per contest and handle: on AtCoder, the
handle's rating and rated matches just before the contest ended (the
baseline), and the new ones once they change; on Codeforces, the change of a
handle that a member linked, with their place. Handles compare
case-insensitively. The bot never deletes a contest's row, but drops an
AtCoder handle's entry if AtCoder no longer has the user.

``contest_result_start`` holds one row, written once: when contest results
started on this install, at the bot's first run with them. The results of a
contest that ended before then are never posted.
"""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE contest_result (
        platform TEXT NOT NULL CHECK (platform IN ('codeforces', 'atcoder')),
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT,
        end_time INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('watching', 'posting', 'done')),
        checks INTEGER NOT NULL DEFAULT 0,
        next_check INTEGER,
        found_at INTEGER,
        outcome TEXT CHECK (
            outcome IS NULL OR outcome IN ('posted', 'nobody', 'missed', 'expired')
        ),
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (platform, external_id)
    )
    """,
    'CREATE INDEX ix_contest_result_due ON contest_result (status, next_check)',
    """
    CREATE TABLE contest_result_entry (
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        handle TEXT NOT NULL COLLATE NOCASE,
        old_rating INTEGER,
        new_rating INTEGER,
        place INTEGER,
        old_matches INTEGER,
        new_matches INTEGER,
        old_highest INTEGER,
        noted_at INTEGER NOT NULL,
        changed_at INTEGER,
        PRIMARY KEY (platform, external_id, handle),
        FOREIGN KEY (platform, external_id)
            REFERENCES contest_result (platform, external_id)
    )
    """,
    """
    CREATE TABLE contest_result_start (
        id INTEGER PRIMARY KEY NOT NULL CHECK (id = 1),
        started_at INTEGER NOT NULL
    )
    """,
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(7, 'contest_results', apply)
