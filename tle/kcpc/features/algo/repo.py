"""Each server's algorithm of the month: the topic it has for each month.

A server's topic is posted on the 1st of each month at noon, club time.
``algo_pick`` holds a row for each server, month (the month of that slot in
club time, 'YYYY-MM') and revision: the month's first topic is revision 0,
and each reroll adds the next revision, with another topic. The newest
revision is the month's topic, and the ones before it stay, since members may
have been shown them; whether a revision's post went out is the delivery
ledger's to say. A month's first row is stored before its topic is posted, so
that a retry posts the same topic, and rows stay, so that the server doesn't
get a topic again until it has had every other (see ``service``).

Guild IDs are stored as text, and times as whole seconds.
"""

from dataclasses import dataclass
from datetime import datetime

from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch


@dataclass(frozen=True)
class AlgoPick:
    """A row of ``algo_pick``: a revision of a server's topic for one month.

    Its times are converted to UTC in whole seconds, as they are stored, so
    that a pick built by hand equals the one read back.
    """

    guild_id: int
    month: str  # the slot's month in club time, 'YYYY-MM'
    slot: datetime  # when the topic is posted: the 1st at noon, club time
    slug: str  # the topic's ID in the catalog
    revision: int  # 0, then one more for each reroll
    picked_at: datetime  # when this revision's topic was picked

    def __post_init__(self) -> None:
        object.__setattr__(self, 'slot', _whole_seconds(self.slot))
        object.__setattr__(self, 'picked_at', _whole_seconds(self.picked_at))


_SELECT = 'SELECT guild_id, month, slot, slug, revision, picked_at FROM algo_pick'
# A month keeps the pick stored first.
_CREATE = """
    INSERT INTO algo_pick (guild_id, month, slot, slug, revision, picked_at)
    SELECT ?, ?, ?, ?, ?, ?
    WHERE NOT EXISTS (SELECT 1 FROM algo_pick WHERE guild_id = ? AND month = ?)
"""
# The month's next revision, at its slot; nothing if the month has no pick.
_REROLL = """
    INSERT INTO algo_pick (guild_id, month, slot, slug, revision, picked_at)
    SELECT guild_id, month, slot, ?, revision + 1, ?
    FROM algo_pick WHERE guild_id = ? AND month = ?
    ORDER BY revision DESC LIMIT 1
"""


class AlgoRepo:
    """Reads and writes ``algo_pick``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, guild_id: int, month: str) -> AlgoPick | None:
        """The guild's pick for ``month`` ('YYYY-MM'): its newest revision, or
        None.
        """
        row = await self._db.fetchone(
            f'{_SELECT} WHERE guild_id = ? AND month = ? '
            'ORDER BY revision DESC LIMIT 1',
            (str(guild_id), month),
        )
        return None if row is None else _pick(row)

    async def revisions(self, guild_id: int, month: str) -> list[AlgoPick]:
        """Every revision of the guild's pick for ``month``, newest first."""
        rows = await self._db.fetchall(
            f'{_SELECT} WHERE guild_id = ? AND month = ? ORDER BY revision DESC',
            (str(guild_id), month),
        )
        return [_pick(row) for row in rows]

    async def create(self, pick: AlgoPick) -> AlgoPick:
        """Store ``pick`` as the guild's pick for its month, and return the
        month's pick: its newest revision.

        A month keeps the pick stored first: if another run, or an earlier try
        of this one, stored the month's pick, ``pick`` is dropped.
        """
        async with self._db.transaction():
            await self._db.execute(
                _CREATE,
                (
                    str(pick.guild_id),
                    pick.month,
                    to_epoch(pick.slot),
                    pick.slug,
                    pick.revision,
                    to_epoch(pick.picked_at),
                    str(pick.guild_id),
                    pick.month,
                ),
            )
            stored = await self.get(pick.guild_id, pick.month)
        if stored is None:  # stored just now, in the same transaction
            raise RuntimeError('The pick stored was not found again')
        return stored

    async def history(self, guild_id: int, limit: int = 200) -> list[AlgoPick]:
        """Every revision of the guild's picks for its newest ``limit``
        months, newest first: by month, then revision.
        """
        if limit < 1:
            return []
        rows = await self._db.fetchall(
            f'{_SELECT} WHERE guild_id = ? AND month IN ('
            'SELECT DISTINCT month FROM algo_pick WHERE guild_id = ? '
            'ORDER BY month DESC LIMIT ?'
            ') ORDER BY month DESC, revision DESC',
            (str(guild_id), str(guild_id), limit),
        )
        return [_pick(row) for row in rows]

    async def picks_before(self, guild_id: int, month: str) -> list[AlgoPick]:
        """Every revision of the guild's picks for the months before ``month``
        ('YYYY-MM'), oldest first: by month, then revision.
        """
        rows = await self._db.fetchall(
            f'{_SELECT} WHERE guild_id = ? AND month < ? ORDER BY month, revision',
            (str(guild_id), month),
        )
        return [_pick(row) for row in rows]

    async def reroll(
        self, guild_id: int, month: str, slug: str, at: datetime
    ) -> AlgoPick | None:
        """Add the next revision of the guild's pick for ``month``: ``slug``,
        picked ``at``. Returns it; None if the guild has no pick for ``month``.
        """
        async with self._db.transaction():
            await self._db.execute(_REROLL, (slug, to_epoch(at), str(guild_id), month))
            return await self.get(guild_id, month)


def _pick(row: Row) -> AlgoPick:
    return AlgoPick(
        guild_id=int(row['guild_id']),
        month=row['month'],
        slot=from_epoch(row['slot']),
        slug=row['slug'],
        revision=row['revision'],
        picked_at=from_epoch(row['picked_at']),
    )


def _whole_seconds(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds as ``to_epoch`` floors."""
    return ensure_utc(moment).replace(microsecond=0)
