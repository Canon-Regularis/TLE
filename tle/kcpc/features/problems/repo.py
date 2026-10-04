"""Each server's weekly problems, and the problems its admins have queued.

A server's weekly problem is posted each Friday at noon, club time, and its
solution the Friday after. ``weekly_problem`` holds one row per server and
week (the date of that Friday in club time), with its slot (the time of the
post): the problem, how it was picked, and its solution's link and whether
that has been posted. A row is stored before its problem is posted, so that a
retry posts the same problem, and stays, so that no server gets a problem
twice; it is deleted only once members can't see it any more, its post
having never gone out. Whether a row's problem was posted is the delivery
ledger's to say.

``weekly_queue`` holds the problems admins have queued for a server, which are
posted before the rotation picks any, oldest first. A problem is queued once
at most, and never once the server has had it.

Guild and user IDs are stored as text, and times as whole seconds. Each write
runs in a transaction of its own, or joins the caller's, so a service can
group several in ``WeeklyRepo.transaction``: storing a queued problem as the
week's and taking it out of the queue, say.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime

from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch
from tle.kcpc.features.problems.catalog import problem_title

# The platforms that problems come from, as stored.
SOURCES = ('codeforces', 'atcoder')
# How a weekly problem was picked: by the rotation, or from the queue.
AUTO = 'auto'
QUEUED = 'queued'


class ProblemAlreadyUsed(KcpcUserError):
    """The server had the problem as its weekly problem in ``week`` already."""

    def __init__(self, title: str, week: str) -> None:
        super().__init__(f"{title} was already this server's weekly problem on {week}.")
        self.week = week


class ProblemAlreadyQueued(KcpcUserError):
    """The problem is in the server's queue already."""

    def __init__(self, title: str) -> None:
        super().__init__(f"{title} is already in this server's queue.")


@dataclass(frozen=True)
class WeeklyProblem:
    """A row of ``weekly_problem``: a server's problem for one Friday.

    Its times are converted to UTC in whole seconds, as they are stored, so
    that a row built by hand equals the one read back.
    """

    guild_id: int
    slot: datetime  # when the problem is posted: Friday noon, club time
    week: str  # the slot's date in club time, 'YYYY-MM-DD'
    source: str  # one of SOURCES
    problem_id: str  # e.g. '1520D' or 'abc300_d'
    contest_id: str
    index: str  # the problem's letter in its contest, e.g. 'D', 'F2' or 'Ex'
    name: str
    url: str
    topic: str | None  # the rotation's topic key; None for any, or when queued
    difficulty: int | None  # its Codeforces-equivalent rating, if known
    band: str | None
    selection: str  # AUTO or QUEUED
    date_selected: datetime
    # The link an admin set, or the editorial the bot found. None leaves the
    # solution post to link the pages where its site lists editorials.
    solution_url: str | None
    solution_set_by: int | None  # the admin who set solution_url, if one did
    solution_posted: bool
    solution_posted_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'slot', _whole_seconds(self.slot))
        object.__setattr__(self, 'date_selected', _whole_seconds(self.date_selected))
        object.__setattr__(
            self, 'solution_posted_at', _whole_seconds_or_none(self.solution_posted_at)
        )


@dataclass(frozen=True)
class QueuedProblem:
    """A row of ``weekly_queue``: a problem an admin queued for a server.

    ``queued_at`` is converted to UTC in whole seconds, as it is stored.
    """

    guild_id: int
    source: str  # one of SOURCES
    problem_id: str
    contest_id: str
    index: str
    name: str
    url: str
    difficulty: int | None  # its Codeforces-equivalent rating, if known
    band: str | None
    solution_url: str | None  # as for WeeklyProblem
    queued_by: int  # the admin who queued it
    queued_at: datetime
    queue_id: int | None = None  # set once stored; the queue goes in its order

    def __post_init__(self) -> None:
        object.__setattr__(self, 'queued_at', _whole_seconds(self.queued_at))


_SELECT_PROBLEMS = """
    SELECT guild_id, slot, week, source, problem_id, contest_id, problem_index,
        name, url, topic, difficulty, band, selection, date_selected,
        solution_url, solution_set_by, solution_posted, solution_posted_at
    FROM weekly_problem
"""
# A week keeps its first row.
_CREATE = """
    INSERT INTO weekly_problem (
        guild_id, slot, week, source, problem_id, contest_id, problem_index,
        name, url, topic, difficulty, band, selection, date_selected,
        solution_url, solution_set_by, solution_posted, solution_posted_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (guild_id, week) DO NOTHING
"""
_WEEK_USED = """
    SELECT week FROM weekly_problem
    WHERE guild_id = ? AND source = ? AND problem_id = ?
"""
# The first time a solution is marked posted is the time it was.
_MARK_SOLUTION_POSTED = """
    UPDATE weekly_problem SET solution_posted = 1, solution_posted_at = ?
    WHERE guild_id = ? AND slot = ? AND solution_posted = 0
"""
# A solution keeps the link it was posted with, and a link the bot found
# (no set_by) never replaces one an admin set.
_SET_SOLUTION = """
    UPDATE weekly_problem SET solution_url = ?, solution_set_by = ?
    WHERE guild_id = ? AND slot = ? AND solution_posted = 0
        AND (? IS NOT NULL OR solution_set_by IS NULL)
"""

_SELECT_QUEUE = """
    SELECT queue_id, guild_id, source, problem_id, contest_id, problem_index,
        name, url, difficulty, band, solution_url, queued_by, queued_at
    FROM weekly_queue
"""
_ENQUEUE = """
    INSERT INTO weekly_queue (
        guild_id, source, problem_id, contest_id, problem_index, name, url,
        difficulty, band, solution_url, queued_by, queued_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (guild_id, source, problem_id) DO NOTHING
"""


class WeeklyRepo:
    """Reads and writes ``weekly_problem`` and ``weekly_queue``.

    Slots are stored in whole seconds but compared exactly: a slot at 12:00:00
    is before 12:00:00.5, and not at or after it.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run the block in one transaction, which the repo's methods join.

        The writes made in the block are committed together, or not at all.
        As with ``Database.transaction``, keep network calls out of it, and
        never publish in it.
        """
        async with self._db.transaction():
            yield

    async def get(self, guild_id: int, slot: datetime) -> WeeklyProblem | None:
        """The guild's problem for ``slot``, or None."""
        row = await self._db.fetchone(
            f'{_SELECT_PROBLEMS} WHERE guild_id = ? AND slot = ?',
            (str(guild_id), to_epoch(slot)),
        )
        return None if row is None else _weekly_problem(row)

    async def get_week(self, guild_id: int, week: str) -> WeeklyProblem | None:
        """The guild's problem for ``week`` ('YYYY-MM-DD'), whatever its slot,
        or None.
        """
        row = await self._db.fetchone(
            f'{_SELECT_PROBLEMS} WHERE guild_id = ? AND week = ?',
            (str(guild_id), week),
        )
        return None if row is None else _weekly_problem(row)

    async def create(self, problem: WeeklyProblem) -> WeeklyProblem:
        """Store the guild's problem for its week, and return the week's row.

        A week keeps the row stored first: if another run, or an earlier try
        of this one, stored the week's problem, that row is returned and
        ``problem`` is dropped, even if its slot differs (the club's time zone
        changed in between). Otherwise a problem that the guild has had
        before raises ``ProblemAlreadyUsed``, and nothing is stored.
        """
        async with self._db.transaction():
            stored = await self.get_week(problem.guild_id, problem.week)
            if stored is not None:
                return stored
            week = await self._week_used(
                problem.guild_id, problem.source, problem.problem_id
            )
            if week is not None:
                raise ProblemAlreadyUsed(_problem_title(problem), week)
            await self._db.execute(_CREATE, _problem_params(problem))
            created = await self.get_week(problem.guild_id, problem.week)
        if created is None:  # stored just now, in the same transaction
            raise RuntimeError('The weekly problem stored was not found again')
        return created

    async def delete(self, guild_id: int, week: str) -> None:
        """Delete the guild's problem for ``week``, which it then counts as never
        having had. Only for a problem whose post never went out, once its
        week is over.
        """
        await self._db.execute(
            'DELETE FROM weekly_problem WHERE guild_id = ? AND week = ?',
            (str(guild_id), week),
        )

    async def latest(
        self, guild_id: int, *, at_or_before: datetime
    ) -> WeeklyProblem | None:
        """The guild's problem with the latest slot at or before ``at_or_before``.

        None if the guild had none by then.
        """
        problems = await self.history(guild_id, at_or_before=at_or_before, limit=1)
        return problems[0] if problems else None

    async def history(
        self, guild_id: int, *, at_or_before: datetime, limit: int = 200
    ) -> list[WeeklyProblem]:
        """The guild's problems with slots at or before ``at_or_before``, newest first.

        At most ``limit`` of them: the newest.
        """
        if limit < 1:
            return []
        rows = await self._db.fetchall(
            f'{_SELECT_PROBLEMS} WHERE guild_id = ? AND slot <= ? '
            'ORDER BY slot DESC LIMIT ?',
            (str(guild_id), to_epoch(at_or_before), limit),
        )
        return [_weekly_problem(row) for row in rows]

    async def unposted_solutions(
        self, guild_id: int, *, before: datetime, since: datetime
    ) -> list[WeeklyProblem]:
        """The guild's problems whose solutions aren't posted yet, oldest first.

        Those whose slots are in ``[since, before)``.
        """
        rows = await self._db.fetchall(
            f'{_SELECT_PROBLEMS} WHERE guild_id = ? AND solution_posted = 0 '
            'AND slot >= ? AND slot < ? ORDER BY slot',
            (str(guild_id), _ceil_epoch(since), _ceil_epoch(before)),
        )
        return [_weekly_problem(row) for row in rows]

    async def mark_solution_posted(
        self, guild_id: int, slot: datetime, at: datetime
    ) -> None:
        """Record that the solution of the guild's problem for ``slot`` went out ``at``.

        A solution marked posted already keeps the time it was marked first.
        """
        await self._db.execute(
            _MARK_SOLUTION_POSTED, (to_epoch(at), str(guild_id), to_epoch(slot))
        )

    async def set_solution(
        self, guild_id: int, slot: datetime, url: str, *, set_by: int | None
    ) -> WeeklyProblem | None:
        """Set the solution link of the guild's problem for ``slot``; return the row.

        ``set_by`` is the admin who set the link, or None for a link the bot
        found, which never replaces one an admin set. A solution posted
        already keeps its link. So the row returned, as stored afterwards,
        tells whether the link changed. None if the guild has no problem for
        ``slot``.
        """
        by = _text_or_none(set_by)
        async with self._db.transaction():
            await self._db.execute(
                _SET_SOLUTION, (url, by, str(guild_id), to_epoch(slot), by)
            )
            return await self.get(guild_id, slot)

    async def used_problems(self, guild_id: int) -> dict[tuple[str, str], str]:
        """Each problem the guild has had, ``(source, problem_id)``, with its week."""
        rows = await self._db.fetchall(
            'SELECT source, problem_id, week FROM weekly_problem '
            'WHERE guild_id = ? ORDER BY slot',
            (str(guild_id),),
        )
        return {(row['source'], row['problem_id']): row['week'] for row in rows}

    async def enqueue(self, item: QueuedProblem) -> QueuedProblem:
        """Add the problem to the end of the guild's queue, and return it as stored.

        Raises ``ProblemAlreadyUsed`` if the guild has had it as a weekly
        problem, and ``ProblemAlreadyQueued`` if it is in the queue already;
        nothing is stored then. Its ``queue_id`` is ignored: storing it gives
        it a new one.
        """
        title = problem_title(item.source, item.contest_id, item.index, item.name)
        async with self._db.transaction():
            week = await self._week_used(item.guild_id, item.source, item.problem_id)
            if week is not None:
                raise ProblemAlreadyUsed(title, week)
            inserted = await self._db.execute(_ENQUEUE, _queue_params(item))
            if inserted.rowcount == 0:
                raise ProblemAlreadyQueued(title)
            queue_id = inserted.lastrowid
            if queue_id is None:  # an INSERT that adds a row always has one
                raise RuntimeError('SQLite gave the queued problem no ID')
        return replace(item, queue_id=queue_id)

    async def queue(self, guild_id: int) -> list[QueuedProblem]:
        """The guild's queued problems, oldest first: the order they are posted in."""
        rows = await self._db.fetchall(
            f'{_SELECT_QUEUE} WHERE guild_id = ? ORDER BY queue_id', (str(guild_id),)
        )
        return [_queued_problem(row) for row in rows]

    async def all_queues(self) -> dict[int, list[QueuedProblem]]:
        """Every guild's queue, oldest first, by guild ID; empty ones left out."""
        rows = await self._db.fetchall(f'{_SELECT_QUEUE} ORDER BY queue_id')
        queues: dict[int, list[QueuedProblem]] = {}
        for row in rows:
            item = _queued_problem(row)
            queues.setdefault(item.guild_id, []).append(item)
        return dict(sorted(queues.items()))

    async def dequeue(
        self, guild_id: int, source: str, problem_id: str
    ) -> QueuedProblem | None:
        """Take the problem out of the guild's queue and return it; None if not in it.

        ``problem_id`` matches as stored, in its case.
        """
        async with self._db.transaction():
            row = await self._db.fetchone(
                f'{_SELECT_QUEUE} WHERE guild_id = ? AND source = ? AND problem_id = ?',
                (str(guild_id), source, problem_id),
            )
            if row is None:
                return None
            await self._db.execute(
                'DELETE FROM weekly_queue WHERE queue_id = ?', (row['queue_id'],)
            )
        return _queued_problem(row)

    async def _week_used(
        self, guild_id: int, source: str, problem_id: str
    ) -> str | None:
        """The week the guild had the problem in, or None if it never did."""
        week: str | None = await self._db.fetchval(
            _WEEK_USED, (str(guild_id), source, problem_id)
        )
        return week


def _weekly_problem(row: Row) -> WeeklyProblem:
    return WeeklyProblem(
        guild_id=int(row['guild_id']),
        slot=from_epoch(row['slot']),
        week=row['week'],
        source=row['source'],
        problem_id=row['problem_id'],
        contest_id=row['contest_id'],
        index=row['problem_index'],
        name=row['name'],
        url=row['url'],
        topic=row['topic'],
        difficulty=row['difficulty'],
        band=row['band'],
        selection=row['selection'],
        date_selected=from_epoch(row['date_selected']),
        solution_url=row['solution_url'],
        solution_set_by=_int_or_none(row['solution_set_by']),
        solution_posted=bool(row['solution_posted']),
        solution_posted_at=_datetime_or_none(row['solution_posted_at']),
    )


def _problem_params(problem: WeeklyProblem) -> tuple[object, ...]:
    return (
        str(problem.guild_id),
        to_epoch(problem.slot),
        problem.week,
        problem.source,
        problem.problem_id,
        problem.contest_id,
        problem.index,
        problem.name,
        problem.url,
        problem.topic,
        problem.difficulty,
        problem.band,
        problem.selection,
        to_epoch(problem.date_selected),
        problem.solution_url,
        _text_or_none(problem.solution_set_by),
        int(problem.solution_posted),
        _epoch_or_none(problem.solution_posted_at),
    )


def _queued_problem(row: Row) -> QueuedProblem:
    return QueuedProblem(
        guild_id=int(row['guild_id']),
        source=row['source'],
        problem_id=row['problem_id'],
        contest_id=row['contest_id'],
        index=row['problem_index'],
        name=row['name'],
        url=row['url'],
        difficulty=row['difficulty'],
        band=row['band'],
        solution_url=row['solution_url'],
        queued_by=int(row['queued_by']),
        queued_at=from_epoch(row['queued_at']),
        queue_id=row['queue_id'],
    )


def _queue_params(item: QueuedProblem) -> tuple[object, ...]:
    return (
        str(item.guild_id),
        item.source,
        item.problem_id,
        item.contest_id,
        item.index,
        item.name,
        item.url,
        item.difficulty,
        item.band,
        item.solution_url,
        str(item.queued_by),
        to_epoch(item.queued_at),
    )


def _problem_title(problem: WeeklyProblem) -> str:
    return problem_title(
        problem.source, problem.contest_id, problem.index, problem.name
    )


def _ceil_epoch(moment: datetime) -> int:
    """The first whole second at or after ``moment``, as stored.

    A slot in whole seconds is at or after ``moment`` exactly when it is at or
    after this, and before ``moment`` exactly when it is before this.
    """
    floor = to_epoch(moment)
    return floor if ensure_utc(moment).microsecond == 0 else floor + 1


def _whole_seconds(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds as ``to_epoch`` floors."""
    return ensure_utc(moment).replace(microsecond=0)


def _whole_seconds_or_none(moment: datetime | None) -> datetime | None:
    return None if moment is None else _whole_seconds(moment)


def _epoch_or_none(moment: datetime | None) -> int | None:
    return None if moment is None else to_epoch(moment)


def _datetime_or_none(seconds: int | None) -> datetime | None:
    return None if seconds is None else from_epoch(seconds)


def _text_or_none(user_id: int | None) -> str | None:
    """A Discord ID as stored, or None."""
    return None if user_id is None else str(user_id)


def _int_or_none(text: str | None) -> int | None:
    """A Discord ID as read back, or None."""
    return None if text is None else int(text)
