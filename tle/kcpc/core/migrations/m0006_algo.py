"""Migration 6: each server's algorithm of the month.

``algo_pick`` holds a server's topic for each month it posts one: ``month`` is
the month of the post's slot in the club's time zone ('YYYY-MM'), ``slot`` the
time of that post (noon on the 1st, club time) and ``slug`` the topic's ID in
the catalog. A month is kept by its name, since the time of its slot changes
if the club's time zone does. Its first topic is revision 0, and each of the
admins' rerolls adds a row with the next ``revision`` and a topic of its own,
posted under a delivery key of its own: the newest revision is the month's
topic, and the ones before stay, since members may have been shown them.
``picked_at`` is when the revision's topic was picked.
"""

from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations.base import Migration

_STATEMENTS = (
    """
    CREATE TABLE algo_pick (
        guild_id TEXT NOT NULL,
        month TEXT NOT NULL,
        slot INTEGER NOT NULL,
        slug TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0,
        picked_at INTEGER NOT NULL,
        PRIMARY KEY (guild_id, month, revision)
    )
    """,
)


async def apply(db: Database) -> None:
    for statement in _STATEMENTS:
        await db.execute(statement)


MIGRATION = Migration(6, 'algo', apply)
