"""Time helpers: UTC normalisation, epoch conversion and club-local time.

KCPC code works with aware UTC datetimes. Local wall-clock times (what members
type, and when club schedules fire) are turned into UTC by ``resolve_local``,
which also decides how to treat the wall times that daylight-saving changes
skip or repeat.
"""

import functools
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, available_timezones

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.errors import ConfigError, KcpcUserError

_DISCORD_TIMESTAMP_STYLES = frozenset('tTdDfFR')

# [0-9] rather than \d, which also matches digits from other scripts.
_LOCAL_DATETIME_RE = re.compile(
    r'([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2})'
)
_LOCAL_DATETIME_HINT = 'Use the format YYYY-MM-DD HH:MM, for example 2026-10-17 10:00.'

_DURATION_UNITS = (('d', 86_400), ('h', 3_600), ('m', 60), ('s', 1))

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` converted to UTC; naive datetimes are rejected."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f'Expected a timezone-aware datetime, got {dt!r}')
    return dt.astimezone(UTC)


def to_epoch(dt: datetime) -> int:
    """Whole seconds since the Unix epoch (floored), as stored in the database."""
    return int(ensure_utc(dt).timestamp() // 1)


def from_epoch(seconds: float) -> datetime:
    """Inverse of ``to_epoch``: an aware UTC datetime.

    Counting from the epoch works for any year on every platform, unlike
    ``datetime.fromtimestamp``: Windows can't convert timestamps before 1970 or
    after 3000, and a calendar feed may hold such times.
    """
    return _EPOCH + timedelta(seconds=seconds)


def zone(name: str) -> ZoneInfo:
    """Look up an IANA time zone such as ``'Europe/London'``.

    Anything but the exact name of a known zone raises ``ConfigError``.
    """
    # Check the list of zones rather than relying on ZoneInfo alone: on Windows
    # and macOS it also opens variants such as 'Europe/london' or
    # 'Europe/London ', which fail on Linux.
    if name not in _zone_names():
        raise ConfigError(f"Unknown time zone '{name}'")
    return ZoneInfo(name)


@functools.cache
def _zone_names() -> frozenset[str]:
    # Scans the time zone files, so only do it once.
    return frozenset(available_timezones())


def resolve_local(naive: datetime, tz: ZoneInfo, *, strict: bool = False) -> datetime:
    """Convert a naive wall-clock time in ``tz`` to an aware UTC datetime.

    Around a daylight-saving change a wall time can happen twice (the clocks go
    back) or not at all (they go forward). By default a repeated time means its
    first occurrence, and a skipped one lands as far after the gap as it was
    into it (01:30 on the London spring-forward day is 02:30 BST), so every
    wall time has exactly one answer. ``strict=True`` raises ``KcpcUserError``
    for both cases instead, which suits times typed by a person.

    ``naive.fold`` is ignored, and an aware ``naive`` raises ``ValueError``.
    """
    if naive.tzinfo is not None:
        raise ValueError(f'Expected a naive local datetime, got {naive!r}')
    resolved = naive.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if strict:
        _check_happens_once(naive, tz, resolved)
    return resolved


def _check_happens_once(naive: datetime, tz: ZoneInfo, resolved: datetime) -> None:
    """Raise ``KcpcUserError`` if a clock change skips or repeats ``naive``."""
    wall_time = f'{naive:%Y-%m-%d %H:%M}'
    # A wall time that exists survives the round trip through UTC unchanged; a
    # skipped one comes back moved past the gap.
    if resolved.astimezone(tz).replace(tzinfo=None) != naive:
        raise KcpcUserError(
            f"{wall_time} doesn't exist in {tz}, because the clocks go forward "
            'that day. Please choose another time.'
        )
    # It exists, so if its two folds give different offsets it happens twice.
    earlier_offset = naive.replace(tzinfo=tz, fold=0).utcoffset()
    later_offset = naive.replace(tzinfo=tz, fold=1).utcoffset()
    if earlier_offset != later_offset:
        raise KcpcUserError(
            f'{wall_time} happens twice in {tz}, because the clocks go back '
            'that day. Please choose another time.'
        )


def parse_local_datetime(text: str, tz: ZoneInfo) -> datetime:
    """Parse a local time a member typed, ``YYYY-MM-DD HH:MM`` in ``tz``, to UTC.

    ``YYYY-MM-DDTHH:MM`` and surrounding whitespace are accepted too. Anything
    else raises ``KcpcUserError``, as does a time that a clock change skips or
    repeats: it is safer to ask again than to guess which one was meant.
    """
    match = _LOCAL_DATETIME_RE.fullmatch(text.strip())
    if match is None:
        raise KcpcUserError(_LOCAL_DATETIME_HINT)
    year, month, day, hour, minute = map(int, match.groups())
    try:
        naive = datetime(year, month, day, hour, minute)
        return resolve_local(naive, tz, strict=True)
    # ValueError: the right shape but no such date or time (2026-02-30, 24:00).
    # OverflowError: in UTC it would fall outside the years 1 to 9999.
    except (ValueError, OverflowError):
        raise KcpcUserError(_LOCAL_DATETIME_HINT) from None


def local_week_bounds(when: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The local week containing ``when``, as UTC ``(start, end)``.

    The week runs from Monday 00:00 in ``tz`` (inclusive) to the next Monday
    00:00 (exclusive). A week with a daylight-saving change in it is an hour
    longer or shorter than seven days, and if a clock change skips Monday
    midnight the week starts at the first moment of Monday instead.
    """
    local_date = ensure_utc(when).astimezone(tz).date()
    monday = local_date - timedelta(days=local_date.weekday())
    return _start_of_day(monday, tz), _start_of_day(monday + timedelta(weeks=1), tz)


def _start_of_day(day: date, tz: ZoneInfo) -> datetime:
    return resolve_local(datetime.combine(day, time()), tz)


def discord_timestamp(dt: datetime, style: str = 'f') -> str:
    """Discord markup that shows ``dt`` in each reader's own time zone.

    ``style`` is ``t``/``T`` (short/long time), ``d``/``D`` (short/long date),
    ``f``/``F`` (short/long date and time) or ``R`` (relative, "in 2 hours").
    """
    if style not in _DISCORD_TIMESTAMP_STYLES:
        raise ValueError(
            f'Discord timestamp style must be one of tTdDfFR, got {style!r}'
        )
    return f'<t:{to_epoch(dt)}:{style}>'


def describe_duration(delta: timedelta, *, precise: bool = False) -> str:
    """Describe ``delta`` compactly, e.g. ``'2d 3h'``, ``'45m'`` or ``'-30s'``.

    Only the largest unit and the one below it are shown, and the rest is cut
    off: 2 days 3 hours 59 minutes is ``'2d 3h'``. ``precise=True`` shows every
    unit down to seconds instead (``'2d 3h 59m'``). Fractions of a second are
    always dropped, so anything under a second is ``'0s'``.
    """
    remaining = abs(delta) // timedelta(seconds=1)
    counts: list[tuple[int, str]] = []
    for unit, unit_seconds in _DURATION_UNITS:
        count, remaining = divmod(remaining, unit_seconds)
        if count or counts:  # start from the largest non-zero unit
            counts.append((count, unit))
    shown = counts if precise else counts[:2]
    text = ' '.join(f'{count}{unit}' for count, unit in shown if count)
    if not text:
        return '0s'
    return f'-{text}' if delta < timedelta(0) else text
