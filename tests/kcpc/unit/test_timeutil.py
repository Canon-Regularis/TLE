"""Tests for tle.kcpc.core.timeutil."""

import random
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.errors import ConfigError, KcpcUserError
from tle.kcpc.core.timeutil import (
    describe_duration,
    discord_timestamp,
    ensure_utc,
    from_epoch,
    local_week_bounds,
    parse_local_datetime,
    resolve_local,
    to_epoch,
    zone,
)

LONDON = ZoneInfo('Europe/London')
NEW_YORK = ZoneInfo('America/New_York')
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
MICROSECOND = timedelta(microseconds=1)
SEED = 20261025
FORMAT_HINT = 'Use the format YYYY-MM-DD HH:MM, for example 2026-10-17 10:00.'
# 2026-10-17 09:00 UTC (10:00 BST) as epoch seconds.
SAMPLE_EPOCH = 1_792_227_600


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


def random_instants(count: int, start: datetime, end: datetime) -> list[datetime]:
    """``count`` seeded random instants in [start, end), to the microsecond."""
    rng = random.Random(SEED)
    span = (end - start) // MICROSECOND
    return [start + rng.randrange(span) * MICROSECOND for _ in range(count)]


class WallClockSurvey(NamedTuple):
    """What a zone's wall clock showed, minute by minute, over a stretch of UTC."""

    # Wall-clock minute -> the UTC instants at which the clock showed it.
    occurrences: dict[datetime, list[datetime]]
    # Wall-clock minute the clock jumped over -> the UTC offset before the jump.
    skipped: dict[datetime, timedelta]


def survey_wall_clock(tz: ZoneInfo, start: datetime, end: datetime) -> WallClockSurvey:
    """Walk UTC from ``start`` to ``end`` a minute at a time, noting the wall clock."""
    occurrences: dict[datetime, list[datetime]] = defaultdict(list)
    skipped: dict[datetime, timedelta] = {}
    instant, wall = start, start.astimezone(tz).replace(tzinfo=None)
    while instant < end:
        occurrences[wall].append(instant)
        next_instant = instant + MINUTE
        next_wall = next_instant.astimezone(tz).replace(tzinfo=None)
        jumped_over = wall + MINUTE
        while jumped_over < next_wall:
            skipped[jumped_over] = wall - instant.replace(tzinfo=None)
            jumped_over += MINUTE
        instant, wall = next_instant, next_wall
    return WallClockSurvey(occurrences, skipped)


def expected_resolution(
    wall: datetime, survey: WallClockSurvey
) -> tuple[str, datetime]:
    """How often the clock showed ``wall``, and the instant the spec picks for it."""
    instants = survey.occurrences.get(wall, [])
    if not instants:
        # As far past the gap as it was into it: the offset from before the jump.
        return 'never', (wall - survey.skipped[wall]).replace(tzinfo=UTC)
    return ('once' if len(instants) == 1 else 'twice'), min(instants)


def actual_resolution(wall: datetime, tz: ZoneInfo) -> tuple[str, datetime]:
    """How strict mode classifies ``wall``, and where lenient mode puts it."""
    lenient = resolve_local(wall, tz)
    try:
        strict = resolve_local(wall, tz, strict=True)
    except KcpcUserError as exc:
        return ('twice' if 'happens twice' in str(exc) else 'never'), lenient
    assert strict == lenient
    return 'once', lenient


# (zone, first local date, number of days, repeated minutes, skipped minutes)
CLOCK_CHANGES = [
    pytest.param('Europe/London', date(2026, 10, 25), 1, 60, 0, id='london-autumn'),
    pytest.param('Europe/London', date(2027, 3, 28), 1, 0, 60, id='london-spring'),
    pytest.param('Europe/Dublin', date(2026, 10, 25), 1, 60, 0, id='negative-dst'),
    pytest.param('America/New_York', date(2026, 11, 1), 1, 60, 0, id='new-york-fall'),
    pytest.param('America/New_York', date(2027, 3, 14), 1, 0, 60, id='new-york-spring'),
    pytest.param('Australia/Sydney', date(2026, 10, 4), 1, 0, 60, id='sydney-spring'),
    pytest.param('Australia/Sydney', date(2027, 4, 4), 1, 60, 0, id='sydney-autumn'),
    pytest.param(
        'Australia/Lord_Howe', date(2026, 10, 4), 1, 0, 30, id='half-hour-gap'
    ),
    pytest.param(
        'Australia/Lord_Howe', date(2027, 4, 4), 1, 30, 0, id='half-hour-fold'
    ),
    pytest.param(
        'America/Sao_Paulo', date(2018, 11, 4), 1, 0, 60, id='midnight-skipped'
    ),
    pytest.param(
        'America/Sao_Paulo', date(2019, 2, 16), 2, 60, 0, id='hour-to-midnight-repeated'
    ),
    pytest.param(
        'America/St_Johns', date(2010, 11, 6), 2, 60, 0, id='repeat-across-midnight'
    ),
    pytest.param(
        'Antarctica/Casey', date(2010, 3, 4), 2, 180, 0, id='three-hours-repeated'
    ),
    pytest.param(
        'Pacific/Apia', date(2011, 12, 29), 3, 0, 1440, id='whole-day-skipped'
    ),
    pytest.param('Asia/Kolkata', date(2026, 10, 25), 1, 0, 0, id='no-daylight-saving'),
]


class TestEpoch:
    @pytest.mark.parametrize('seconds', [0, 1, -1, SAMPLE_EPOCH, 2**31, 4_102_444_799])
    def test_integer_round_trip(self, seconds: int) -> None:
        dt = from_epoch(seconds)
        assert dt.tzinfo is UTC
        assert to_epoch(dt) == seconds

    def test_datetime_round_trip(self) -> None:
        rng = random.Random(SEED)
        for _ in range(500):
            dt = utc(2026, 1, 1) + timedelta(seconds=rng.randrange(-(10**9), 2 * 10**9))
            assert from_epoch(to_epoch(dt)) == dt

    @pytest.mark.parametrize(
        'dt',
        [utc(1, 1, 1), utc(1969, 7, 20, 20, 17), utc(3001, 1, 1), utc(9999, 12, 31)],
        ids=str,
    )
    def test_round_trip_in_any_year(self, dt: datetime) -> None:
        # A feed can hold any year; Windows' own conversion fails for these.
        assert from_epoch(to_epoch(dt)) == dt

    def test_to_epoch_floors_fractions_of_a_second(self) -> None:
        assert to_epoch(utc(2026, 10, 17, 9, 0, 0, 999_999)) == SAMPLE_EPOCH
        assert to_epoch(utc(1969, 12, 31, 23, 59, 59, 500_000)) == -1

    def test_to_epoch_accepts_any_zone(self) -> None:
        assert to_epoch(datetime(2026, 10, 17, 10, tzinfo=LONDON)) == SAMPLE_EPOCH

    def test_from_epoch_keeps_fractions_of_a_second(self) -> None:
        assert from_epoch(1.25) == utc(1970, 1, 1, 0, 0, 1, 250_000)

    def test_ensure_utc_converts_to_utc(self) -> None:
        converted = ensure_utc(datetime(2026, 7, 1, 12, tzinfo=LONDON))
        assert converted.tzinfo is UTC
        assert converted.replace(tzinfo=None) == datetime(2026, 7, 1, 11)

    def test_naive_datetimes_are_rejected(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            ensure_utc(datetime(2026, 7, 1, 12))
        with pytest.raises(ValueError, match='timezone-aware'):
            to_epoch(datetime(2026, 7, 1, 12))


class TestZone:
    @pytest.mark.parametrize(
        'name', ['Europe/London', 'UTC', 'America/New_York', 'GB', 'Etc/GMT+5']
    )
    def test_known_zones(self, name: str) -> None:
        assert zone(name).key == name

    def test_returns_a_working_zone(self) -> None:
        london = zone('Europe/London')
        assert datetime(2026, 7, 1, tzinfo=london).utcoffset() == timedelta(hours=1)
        assert datetime(2026, 12, 1, tzinfo=london).utcoffset() == timedelta(0)

    @pytest.mark.parametrize(
        'name',
        [
            'Mars/Olympus_Mons',
            '',
            ' ',
            'Europe',
            'Europe/',
            '/etc/localtime',
            '../Europe/London',
            'Europe/London\x00',
            'zone.tab',
            'posixrules',
            # ZoneInfo itself opens these on case-insensitive file systems.
            'Europe/london',
            'EUROPE/LONDON',
            'Europe/London ',
            ' Europe/London',
            'Europe/London.',
        ],
    )
    def test_unknown_or_invalid_names_raise_config_error(self, name: str) -> None:
        with pytest.raises(ConfigError) as excinfo:
            zone(name)
        assert str(excinfo.value) == f"Unknown time zone '{name}'"


class TestResolveLocal:
    def test_summer_and_winter_times(self) -> None:
        assert resolve_local(datetime(2026, 7, 1, 12), LONDON) == utc(2026, 7, 1, 11)
        assert resolve_local(datetime(2026, 12, 1, 12), LONDON) == utc(2026, 12, 1, 12)

    def test_ordinary_time_is_fine_when_strict(self) -> None:
        resolved = resolve_local(datetime(2026, 10, 17, 10), LONDON, strict=True)
        assert resolved == utc(2026, 10, 17, 9)
        assert resolved.tzinfo is UTC

    def test_repeated_time_means_the_first_occurrence(self) -> None:
        # On 2026-10-25 London goes from 02:00 BST back to 01:00 GMT, so 01:30
        # happens at 00:30 UTC and again at 01:30 UTC.
        resolved = resolve_local(datetime(2026, 10, 25, 1, 30), LONDON)
        assert resolved == utc(2026, 10, 25, 0, 30)

    def test_repeated_time_is_rejected_when_strict(self) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            resolve_local(datetime(2026, 10, 25, 1, 30), LONDON, strict=True)
        assert str(excinfo.value) == (
            '2026-10-25 01:30 happens twice in Europe/London, because the clocks '
            'go back that day. Please choose another time.'
        )

    def test_skipped_time_lands_as_far_after_the_gap(self) -> None:
        # On 2027-03-28 London goes from 01:00 GMT straight to 02:00 BST.
        resolved = resolve_local(datetime(2027, 3, 28, 1, 30), LONDON)
        assert resolved == utc(2027, 3, 28, 1, 30)
        wall = resolved.astimezone(LONDON).replace(tzinfo=None)
        assert wall == datetime(2027, 3, 28, 2, 30)

    def test_skipped_time_is_rejected_when_strict(self) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            resolve_local(datetime(2027, 3, 28, 1, 30), LONDON, strict=True)
        assert str(excinfo.value) == (
            "2027-03-28 01:30 doesn't exist in Europe/London, because the clocks "
            'go forward that day. Please choose another time.'
        )

    @pytest.mark.parametrize(
        ('naive', 'expected', 'problem'),
        [
            (datetime(2026, 10, 25, 0, 59, 59), utc(2026, 10, 24, 23, 59, 59), None),
            (datetime(2026, 10, 25, 1, 0), utc(2026, 10, 25, 0, 0), 'happens twice'),
            (
                datetime(2026, 10, 25, 1, 59, 59),
                utc(2026, 10, 25, 0, 59, 59),
                'happens twice',
            ),
            (datetime(2026, 10, 25, 2, 0), utc(2026, 10, 25, 2, 0), None),
            (datetime(2027, 3, 28, 0, 59, 59), utc(2027, 3, 28, 0, 59, 59), None),
            (datetime(2027, 3, 28, 1, 0), utc(2027, 3, 28, 1, 0), "doesn't exist"),
            (
                datetime(2027, 3, 28, 1, 59, 59),
                utc(2027, 3, 28, 1, 59, 59),
                "doesn't exist",
            ),
            (datetime(2027, 3, 28, 2, 0), utc(2027, 3, 28, 1, 0), None),
        ],
    )
    def test_edges_of_the_london_clock_changes(
        self, naive: datetime, expected: datetime, problem: str | None
    ) -> None:
        assert resolve_local(naive, LONDON) == expected
        if problem is None:
            assert resolve_local(naive, LONDON, strict=True) == expected
        else:
            with pytest.raises(KcpcUserError, match=problem):
                resolve_local(naive, LONDON, strict=True)

    def test_fold_of_the_input_is_ignored(self) -> None:
        second_occurrence = datetime(2026, 10, 25, 1, 30, fold=1)
        assert resolve_local(second_occurrence, LONDON) == utc(2026, 10, 25, 0, 30)
        with pytest.raises(KcpcUserError, match='happens twice'):
            resolve_local(second_occurrence, LONDON, strict=True)

    def test_aware_input_is_rejected(self) -> None:
        with pytest.raises(ValueError, match='naive'):
            resolve_local(utc(2026, 7, 1, 12), LONDON)

    @pytest.mark.parametrize(
        ('key', 'first_day', 'days', 'repeated', 'skipped'), CLOCK_CHANGES
    )
    def test_agrees_with_the_wall_clock_minute_by_minute(
        self, key: str, first_day: date, days: int, repeated: int, skipped: int
    ) -> None:
        # Brute force: walk UTC a minute at a time, note the wall-clock minute
        # shown at each step, and hold every local minute of the days to it.
        tz = ZoneInfo(key)
        start = datetime.combine(first_day, time(), tzinfo=UTC) - timedelta(days=1)
        survey = survey_wall_clock(tz, start, start + timedelta(days=days + 2))
        kinds: Counter[str] = Counter()
        for index in range(days * 24 * 60):
            wall = datetime.combine(first_day, time()) + index * MINUTE
            expected = expected_resolution(wall, survey)
            assert actual_resolution(wall, tz) == expected, wall
            kinds[expected[0]] += 1
        # The survey really did cross the clock change the case is about.
        assert (kinds['twice'], kinds['never']) == (repeated, skipped)


class TestParseLocalDatetime:
    @pytest.mark.parametrize(
        'text',
        [
            '2026-10-17 10:00',
            '2026-10-17T10:00',
            '  2026-10-17 10:00  ',
            '\t2026-10-17T10:00\n',
        ],
    )
    def test_accepted_formats(self, text: str) -> None:
        assert parse_local_datetime(text, LONDON) == utc(2026, 10, 17, 9)

    def test_reads_the_time_in_the_given_zone(self) -> None:
        text = '2026-12-01 10:00'
        assert parse_local_datetime(text, LONDON) == utc(2026, 12, 1, 10)
        assert parse_local_datetime(text, NEW_YORK) == utc(2026, 12, 1, 15)

    @pytest.mark.parametrize(
        'text',
        [
            '',
            '   ',
            '2026-10-17',
            '10:00',
            '2026-10-17 10',
            '2026-10-17 10:00:00',
            '2026-10-17 10:00Z',
            '2026-10-17 10:00+01:00',
            '2026/10/17 10:00',
            '17-10-2026 10:00',
            '26-10-17 10:00',
            '2026-1-17 10:00',
            '2026-10-17 9:00',
            '2026-10-17  10:00',
            '2026-10-17t10:00',
            '2026-10-17_10:00',
            '2026-10-17 10:00 BST',
            'tomorrow at 10:00',
            # 2026 in fullwidth digits, then in Arabic-Indic digits.
            '\uff12\uff10\uff12\uff16-10-17 10:00',
            '\u0662\u0660\u0662\u0666-10-17 10:00',
            # The right shape, but no such date or time.
            '2026-02-29 10:00',
            '2026-02-30 10:00',
            '2026-13-01 10:00',
            '2026-00-10 10:00',
            '2026-10-00 10:00',
            '2026-10-17 24:00',
            '2026-10-17 10:60',
            '0000-01-01 10:00',
        ],
    )
    def test_anything_else_is_rejected_with_the_format_hint(self, text: str) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            parse_local_datetime(text, LONDON)
        assert str(excinfo.value) == FORMAT_HINT

    def test_leap_day_is_accepted(self) -> None:
        assert parse_local_datetime('2028-02-29 10:00', LONDON) == utc(2028, 2, 29, 10)

    @pytest.mark.parametrize(
        ('text', 'key'),
        [('0001-01-01 00:00', 'Asia/Tokyo'), ('9999-12-31 23:00', 'America/New_York')],
    )
    def test_times_outside_the_utc_range_are_rejected(
        self, text: str, key: str
    ) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            parse_local_datetime(text, ZoneInfo(key))
        assert str(excinfo.value) == FORMAT_HINT

    def test_refuses_to_guess_around_clock_changes(self) -> None:
        with pytest.raises(KcpcUserError, match='happens twice'):
            parse_local_datetime('2026-10-25 01:30', LONDON)
        with pytest.raises(KcpcUserError, match="doesn't exist"):
            parse_local_datetime('2027-03-28T01:30', LONDON)


class TestLocalWeekBounds:
    def test_ordinary_week(self) -> None:
        # Wednesday 15 July 2026: Monday 13 to Monday 20 July, both in BST.
        bounds = local_week_bounds(utc(2026, 7, 15, 12), LONDON)
        assert bounds == (utc(2026, 7, 12, 23), utc(2026, 7, 19, 23))

    def test_week_in_which_the_clocks_go_back_is_an_hour_longer(self) -> None:
        start, end = local_week_bounds(utc(2026, 10, 21, 12), LONDON)
        assert (start, end) == (utc(2026, 10, 18, 23), utc(2026, 10, 26))
        assert end - start == timedelta(days=7, hours=1)

    def test_week_in_which_the_clocks_go_forward_is_an_hour_shorter(self) -> None:
        start, end = local_week_bounds(utc(2027, 3, 24, 12), LONDON)
        assert (start, end) == (utc(2027, 3, 22), utc(2027, 3, 28, 23))
        assert end - start == timedelta(days=6, hours=23)

    def test_start_is_inclusive_and_end_exclusive(self) -> None:
        start, end = local_week_bounds(utc(2026, 10, 21, 12), LONDON)
        assert local_week_bounds(start, LONDON) == (start, end)
        assert local_week_bounds(end - MICROSECOND, LONDON) == (start, end)
        assert local_week_bounds(end, LONDON)[0] == end

    def test_goes_by_the_local_date(self) -> None:
        monday_19th = utc(2026, 10, 18, 23)
        # 23:30 UTC on Sunday 18 October is already Monday 00:30 BST...
        assert local_week_bounds(utc(2026, 10, 18, 23, 30), LONDON)[0] == monday_19th
        # ...but 23:30 UTC on Sunday 25 October is still Sunday 23:30 GMT.
        assert local_week_bounds(utc(2026, 10, 25, 23, 30), LONDON)[0] == monday_19th

    def test_accepts_any_aware_datetime(self) -> None:
        when = datetime(2026, 10, 25, 20, tzinfo=NEW_YORK)  # Monday 00:00 GMT
        assert local_week_bounds(when, LONDON)[0] == utc(2026, 10, 26)
        second_0130 = datetime(2026, 10, 25, 1, 30, fold=1, tzinfo=LONDON)
        assert local_week_bounds(second_0130, LONDON)[1] == utc(2026, 10, 26)

    def test_starts_when_monday_begins_if_midnight_is_skipped(self) -> None:
        # Iran moved its clocks from 00:00 to 01:00 on Monday 22 March 2021.
        tehran = ZoneInfo('Asia/Tehran')
        start, end = local_week_bounds(utc(2021, 3, 24, 12), tehran)
        assert (start, end) == (utc(2021, 3, 21, 20, 30), utc(2021, 3, 28, 19, 30))
        local_start = start.astimezone(tehran).replace(tzinfo=None)
        assert local_start == datetime(2021, 3, 22, 1, 0)
        assert local_week_bounds(start - MICROSECOND, tehran)[1] == start

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            local_week_bounds(datetime(2026, 10, 21, 12), LONDON)

    @pytest.mark.parametrize(
        'key', ['Europe/London', 'America/New_York', 'Australia/Sydney', 'Asia/Kolkata']
    )
    def test_weeks_run_monday_to_monday_without_gaps(self, key: str) -> None:
        tz = ZoneInfo(key)
        week = timedelta(days=7)
        for when in random_instants(300, utc(2026, 1, 1), utc(2029, 1, 1)):
            start, end = local_week_bounds(when, tz)
            assert start <= when < end
            local_start = start.astimezone(tz)
            assert (local_start.weekday(), local_start.time()) == (0, time())
            assert end - start in (week - HOUR, week, week + HOUR)
            assert local_week_bounds(end, tz)[0] == end


class TestDiscordTimestamp:
    @pytest.mark.parametrize('style', list('tTdDfFR'))
    def test_styles(self, style: str) -> None:
        stamp = discord_timestamp(utc(2026, 10, 17, 9), style)
        assert stamp == f'<t:{SAMPLE_EPOCH}:{style}>'

    def test_default_style_is_short_date_and_time(self) -> None:
        assert discord_timestamp(utc(2026, 10, 17, 9)) == f'<t:{SAMPLE_EPOCH}:f>'

    def test_floors_to_whole_seconds_and_accepts_any_zone(self) -> None:
        late = utc(2026, 10, 17, 9, 0, 0, 999_999)
        assert discord_timestamp(late, 'R') == f'<t:{SAMPLE_EPOCH}:R>'
        local = datetime(2026, 10, 17, 10, tzinfo=LONDON)
        assert discord_timestamp(local, 'R') == f'<t:{SAMPLE_EPOCH}:R>'

    @pytest.mark.parametrize('style', ['', 'x', 'r', 'ff', 'tT', 'tTdDfFR', ' f'])
    def test_unknown_styles_are_rejected(self, style: str) -> None:
        with pytest.raises(ValueError, match='style'):
            discord_timestamp(utc(2026, 10, 17, 9), style)

    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            discord_timestamp(datetime(2026, 10, 17, 9))


class TestDescribeDuration:
    @pytest.mark.parametrize(
        ('delta', 'expected'),
        [
            (timedelta(days=2, hours=3), '2d 3h'),
            (timedelta(hours=3, minutes=5), '3h 5m'),
            (timedelta(minutes=45), '45m'),
            (timedelta(seconds=30), '30s'),
            (timedelta(0), '0s'),
            (timedelta(seconds=90), '1m 30s'),
            (timedelta(hours=24), '1d'),
            (timedelta(days=400), '400d'),
            # Everything below the second unit is cut off, not rounded.
            (timedelta(days=2, hours=3, minutes=59, seconds=59), '2d 3h'),
            (timedelta(days=2, minutes=5), '2d'),
            (timedelta(hours=1, seconds=59), '1h'),
            (timedelta(seconds=59, microseconds=999_999), '59s'),
            (timedelta(milliseconds=999), '0s'),
            (-timedelta(hours=3, minutes=5), '-3h 5m'),
            (-timedelta(seconds=30), '-30s'),
            (-timedelta(days=2, hours=3, minutes=59), '-2d 3h'),
            (-timedelta(milliseconds=500), '0s'),
        ],
    )
    def test_compact(self, delta: timedelta, expected: str) -> None:
        assert describe_duration(delta) == expected

    @pytest.mark.parametrize(
        ('delta', 'expected'),
        [
            (timedelta(days=1, seconds=1), '1d 1s'),
            (timedelta(days=2, hours=3, minutes=59, seconds=59), '2d 3h 59m 59s'),
            (timedelta(minutes=2), '2m'),
            (timedelta(0), '0s'),
            (timedelta(seconds=1, milliseconds=500), '1s'),
            (-timedelta(hours=1, seconds=5), '-1h 5s'),
        ],
    )
    def test_precise(self, delta: timedelta, expected: str) -> None:
        assert describe_duration(delta, precise=True) == expected
