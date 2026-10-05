"""Migration 5: each server's weekly problems, and the problems admins queue.

``weekly_problem`` holds a server's problem for each Friday it posts one: the
``slot`` is the time of that post (noon, club time) and ``week`` its date in
the club's time zone ('YYYY-MM-DD'). A server has one problem a week, kept by
the week, since the time of its slot changes if the club's time zone does, and
gets a problem once at most.
``topic`` is the rotation's topic key, NULL for any topic or a queued problem,
``difficulty`` the problem's Codeforces-equivalent rating, if known, and
``selection`` says whether the rotation picked it or the queue. Its solution
is posted the Friday after. ``solution_url`` is the link an admin set
(``solution_set_by`` says who) or the editorial the bot found; while it is
NULL, the post links the pages where the problem's site lists its editorials.

``weekly_queue`` holds the problems admins have queued for a server, which go
before the rotation's, oldest first: in order of ``queue_id``.
"""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE weekly_problem (
        weekly_id INTEGER PRIMARY KEY,
        guild_id TEXT NOT NULL,
        slot INTEGER NOT NULL,
        week TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('codeforces', 'atcoder')),
        problem_id TEXT NOT NULL,
        contest_id TEXT NOT NULL,
        problem_index TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        topic TEXT,
        difficulty INTEGER,
        band TEXT,
        selection TEXT NOT NULL CHECK (selection IN ('auto', 'queued')),
        date_selected INTEGER NOT NULL,
        solution_url TEXT,
        solution_set_by TEXT,
        solution_posted INTEGER NOT NULL DEFAULT 0
            CHECK (solution_posted IN (0, 1)),
        solution_posted_at INTEGER,
        UNIQUE (guild_id, week),
        UNIQUE (guild_id, source, problem_id)
    )
    """,
    # For reading a server's problems in order of their slots.
    'CREATE INDEX ix_weekly_problem_guild_slot ON weekly_problem (guild_id, slot)',
    """
    CREATE TABLE weekly_queue (
        queue_id INTEGER PRIMARY KEY,
        guild_id TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('codeforces', 'atcoder')),
        problem_id TEXT NOT NULL,
        contest_id TEXT NOT NULL,
        problem_index TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        difficulty INTEGER,
        band TEXT,
        solution_url TEXT,
        queued_by TEXT NOT NULL,
        queued_at INTEGER NOT NULL,
        UNIQUE (guild_id, source, problem_id)
    )
    """,
    'CREATE INDEX ix_weekly_queue_guild ON weekly_queue (guild_id, queue_id)',
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(5, 'problems', apply)
