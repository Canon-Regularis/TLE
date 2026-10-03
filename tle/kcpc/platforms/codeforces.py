"""Upcoming Codeforces rounds, read from TLE's contest cache.

TLE already polls Codeforces' contest list and caches it, so this module asks
Codeforces nothing itself: the caller passes the cached ``Contest`` objects in.
Codeforces gives a contest's start and duration in seconds, and its phase:
BEFORE until it starts, CODING while it runs, then on to FINISHED.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from tle.kcpc.core.timeutil import ensure_utc, from_epoch
from tle.util import codeforces_api as cf

PLATFORM = 'codeforces'

# The phases of a contest that is yet to start or still running.
_UPCOMING_PHASES = frozenset({'BEFORE', 'CODING'})


@dataclass(frozen=True)
class CodeforcesContest:
    """A Codeforces contest that is yet to start or still running.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted), and ``end`` is after ``start``.
    """

    contest_id: int
    name: str
    start: datetime
    end: datetime
    url: str  # https://codeforces.com/contests/<id>, which also lets you register

    def __post_init__(self) -> None:
        # Converted here too, so that contests built by hand (in tests, say)
        # compare exactly like those read from the cache.
        start = ensure_utc(self.start).replace(microsecond=0)
        end = ensure_utc(self.end).replace(microsecond=0)
        if end <= start:
            raise ValueError(
                f'Codeforces contest {self.contest_id} must end after it starts'
            )
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)


def upcoming_contests(
    contests: Iterable[cf.Contest], *, now: datetime
) -> list[CodeforcesContest]:
    """The contests that are yet to start or still running, by start then ID.

    That is the contests in phase BEFORE or CODING that have a start time and
    a positive duration, and end after ``now``: a contest the cache still has
    as CODING may have ended since it was cached.
    """
    now = ensure_utc(now)
    upcoming: list[CodeforcesContest] = []
    for contest in contests:
        start_seconds = contest.startTimeSeconds
        duration_seconds = contest.durationSeconds
        if contest.phase not in _UPCOMING_PHASES:
            continue
        if start_seconds is None or duration_seconds is None or duration_seconds <= 0:
            continue
        end = from_epoch(start_seconds + duration_seconds)
        if end <= now:
            continue
        upcoming.append(
            CodeforcesContest(
                contest_id=contest.id,
                name=contest.name,
                start=from_epoch(start_seconds),
                end=end,
                url=contest.register_url,
            )
        )
    upcoming.sort(key=lambda contest: (contest.start, contest.contest_id))
    return upcoming
