"""Tests for tle.kcpc.platforms.codeforces: upcoming rounds from TLE's cache."""

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.timeutil import to_epoch
from tle.kcpc.platforms.codeforces import CodeforcesContest, upcoming_contests
from tle.util import codeforces_api as cf

LONDON = ZoneInfo('Europe/London')
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
TWO_HOURS = timedelta(hours=2)


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


ROUND_START = at(2026, 10, 3, 14, 35)


def contest(
    contest_id: int,
    *,
    start: datetime | None = ROUND_START,
    duration: timedelta | None = TWO_HOURS,
    phase: str = 'BEFORE',
) -> cf.Contest:
    """A contest as TLE caches it from Codeforces' contest list."""
    return cf.Contest(
        id=contest_id,
        name=f'Codeforces Round {contest_id}',
        startTimeSeconds=None if start is None else to_epoch(start),
        durationSeconds=None if duration is None else int(duration.total_seconds()),
        type='CF',
        phase=phase,
        preparedBy=None,
    )


def ids(contests: list[CodeforcesContest]) -> list[int]:
    return [c.contest_id for c in contests]


class TestUpcomingContests:
    def test_a_contest_becomes_a_codeforces_contest(self) -> None:
        cached = contest(
            2150, start=at(2026, 10, 3, 14, 35), duration=timedelta(hours=2, minutes=15)
        )

        [upcoming] = upcoming_contests([cached], now=NOW)

        assert upcoming == CodeforcesContest(
            contest_id=2150,
            name='Codeforces Round 2150',
            start=at(2026, 10, 3, 14, 35),
            end=at(2026, 10, 3, 16, 50),
            url='https://codeforces.com/contests/2150',
        )
        assert upcoming.start.tzinfo is UTC
        assert upcoming.end.tzinfo is UTC

    @pytest.mark.parametrize('phase', ['BEFORE', 'CODING'])
    def test_contests_yet_to_start_or_running_are_kept(self, phase: str) -> None:
        running = contest(2150, start=NOW - timedelta(hours=1), phase=phase)
        assert ids(upcoming_contests([running], now=NOW)) == [2150]

    @pytest.mark.parametrize(
        'phase', ['PENDING_SYSTEM_TEST', 'SYSTEM_TEST', 'FINISHED', 'before']
    )
    def test_contests_in_other_phases_are_left_out(self, phase: str) -> None:
        cached = contest(2150, start=NOW + timedelta(days=1), phase=phase)
        assert upcoming_contests([cached], now=NOW) == []

    @pytest.mark.parametrize(
        ('start', 'duration'),
        [
            (None, TWO_HOURS),
            (at(2026, 10, 3, 14, 35), None),
            (None, None),
            (at(2026, 10, 3, 14, 35), timedelta(0)),
            (at(2026, 10, 3, 14, 35), timedelta(seconds=-1)),
        ],
        ids=['no-start', 'no-duration', 'neither', 'zero-duration', 'negative'],
    )
    def test_contests_without_a_start_and_a_duration_are_left_out(
        self, start: datetime | None, duration: timedelta | None
    ) -> None:
        cached = contest(2150, start=start, duration=duration)
        assert upcoming_contests([cached], now=NOW) == []

    @pytest.mark.parametrize(
        ('end', 'kept'),
        [
            (NOW - timedelta(seconds=1), False),
            (NOW, False),
            (NOW + timedelta(seconds=1), True),
        ],
        ids=['ended-before', 'ends-now', 'ends-after'],
    )
    @pytest.mark.parametrize('phase', ['BEFORE', 'CODING'])
    def test_only_contests_ending_after_now_are_kept(
        self, end: datetime, kept: bool, phase: str
    ) -> None:
        # The cache can lag: a contest it has as CODING, or even BEFORE, may
        # already be over.
        cached = contest(2150, start=end - TWO_HOURS, phase=phase)
        assert ids(upcoming_contests([cached], now=NOW)) == ([2150] if kept else [])

    def test_contests_are_sorted_by_start_then_id(self) -> None:
        div1_and_div2 = at(2026, 10, 3, 14, 35)
        cached = [
            contest(2152, start=at(2026, 10, 9, 14, 35)),
            contest(2151, start=div1_and_div2),
            contest(2160, start=at(2026, 10, 2, 9)),
            contest(2150, start=div1_and_div2),
        ]
        assert ids(upcoming_contests(cached, now=NOW)) == [2160, 2150, 2151, 2152]

    def test_reads_any_iterable(self) -> None:
        cached = (contest(contest_id) for contest_id in (2151, 2150))
        assert ids(upcoming_contests(cached, now=NOW)) == [2150, 2151]

    def test_no_contests_is_an_empty_list(self) -> None:
        assert upcoming_contests([], now=NOW) == []

    def test_now_may_be_in_any_zone(self) -> None:
        cached = contest(2150, start=NOW - TWO_HOURS + timedelta(seconds=1))
        london_now = NOW.astimezone(LONDON)
        assert ids(upcoming_contests([cached], now=london_now)) == [2150]

    def test_a_naive_now_is_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            upcoming_contests([contest(2150)], now=datetime(2026, 10, 1, 12))


CONTEST = CodeforcesContest(
    contest_id=2150,
    name='Codeforces Round 2150',
    start=at(2026, 10, 3, 14, 35),
    end=at(2026, 10, 3, 16, 35),
    url='https://codeforces.com/contests/2150',
)


class TestCodeforcesContest:
    def test_times_become_utc_in_whole_seconds(self) -> None:
        contest = dataclasses.replace(
            CONTEST,
            start=datetime(2026, 10, 3, 15, 35, 0, 999_999, tzinfo=LONDON),
            end=datetime(2026, 10, 3, 17, 35, 30, 1, tzinfo=LONDON),
        )
        assert (contest.start, contest.end) == (
            at(2026, 10, 3, 14, 35),
            datetime(2026, 10, 3, 16, 35, 30, tzinfo=UTC),
        )
        assert contest.start.tzinfo is UTC
        assert contest.end.tzinfo is UTC

    def test_naive_times_are_refused(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, start=datetime(2026, 10, 3, 14, 35))
        with pytest.raises(ValueError, match='timezone-aware'):
            dataclasses.replace(CONTEST, end=datetime(2026, 10, 3, 16, 35))

    @pytest.mark.parametrize(
        'end', [at(2026, 10, 3, 14, 35), at(2026, 10, 3, 14, 34)], ids=['at', 'before']
    )
    def test_the_end_must_be_after_the_start(self, end: datetime) -> None:
        with pytest.raises(ValueError, match='must end after it starts'):
            dataclasses.replace(CONTEST, end=end)
