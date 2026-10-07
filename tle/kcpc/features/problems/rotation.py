"""The rotation: which platform, band and topic each week's problem comes from.

A server's rotation is a cycle of entries, one a week, kept in its weekly
settings as 'platform:band:topic' strings, such as 'codeforces:medium:graphs'.
A week takes the entry its number gives, counting whole weeks from Friday
2026-01-02 in club time, modulo the rotation's length. So the entry for a week
doesn't depend on which weeks before it had a problem, and a server that posts
nothing for a while picks up the cycle where the calendar is.

The default rotation alternates the platforms and climbs from easy to hard.
AtCoder's problems have no topics, so its entries are always for any topic.
"""

import logging
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.messages import shorten
from tle.kcpc.features.problems import markdown
from tle.kcpc.features.problems.catalog import (
    ATCODER,
    CODEFORCES,
    PLATFORMS,
    platform_name,
)
from tle.kcpc.features.problems.topics import ANY, resolve_topic
from tle.kcpc.platforms.difficulty import Band, parse_band

logger = logging.getLogger(__name__)

# The Friday that week 0 starts on.
ROTATION_ANCHOR = date(2026, 1, 2)
# A year of weeks.
MAX_ROTATION = 52

# What admins may call each platform.
_PLATFORM_ALIASES = {
    'cf': CODEFORCES,
    'codeforces': CODEFORCES,
    'ac': ATCODER,
    'atcoder': ATCODER,
}
_ENTRY_SEPARATORS = re.compile(r'[,;]')
# How much of a bad entry its error repeats.
_SHOWN_LIMIT = 40
_NO_ENTRIES = (
    'Give at least one entry, such as: codeforces easy, atcoder medium, '
    'codeforces medium graphs, atcoder hard'
)
_TOO_MANY = f'A rotation has at most {MAX_ROTATION} entries, one for each week.'
_ENTRY = "Entry {number} ('{entry}')"
_NEEDS_BAND = ' needs a platform and a band, such as codeforces medium graphs.'
_UNKNOWN_PLATFORM = (
    ' has no platform I know: use codeforces or atcoder (cf or ac for short).'
)
_UNKNOWN_BAND = ' has no band I know: use easy, medium, hard or expert.'
_ATCODER_TOPIC = ' is for AtCoder, whose problems have no topics: leave the topic out.'

# Stored entries that couldn't be read and were warned about, each once:
# rotations are decoded every week and for every preview.
_reported: set[str] = set()


@dataclass(frozen=True)
class RotationEntry:
    """What one week's problem is picked from."""

    platform: str  # CODEFORCES or ATCODER
    band: Band
    topic: str  # ANY or a topic's key; always ANY on AtCoder

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(f'There are no problems of {self.platform!r}')
        if self.platform == ATCODER and self.topic != ANY:
            raise ValueError('AtCoder problems have no topics')

    def encode(self) -> str:
        """The entry as settings store it: 'codeforces:easy:any'."""
        return f'{self.platform}:{self.band.value}:{self.topic}'


DEFAULT_ROTATION = (
    RotationEntry(CODEFORCES, Band.EASY, ANY),
    RotationEntry(ATCODER, Band.MEDIUM, ANY),
    RotationEntry(CODEFORCES, Band.MEDIUM, ANY),
    RotationEntry(ATCODER, Band.HARD, ANY),
)


def parse_rotation(text: str, known_tags: Collection[str]) -> tuple[RotationEntry, ...]:
    """The rotation that an admin typed: entries separated by ',' or ';'.

    Each entry is 'platform band [topic]': cf or codeforces, ac or atcoder;
    easy, medium, hard or expert; then a topic, ``any`` if left out, as
    ``resolve_topic`` reads it against ``known_tags``. Blank entries are
    skipped. Raises ``KcpcUserError`` naming the first bad entry (counting
    from 1), or if there are none or more than ``MAX_ROTATION``.
    """
    entries = [entry.strip() for entry in _ENTRY_SEPARATORS.split(text)]
    entries = [entry for entry in entries if entry]
    if not entries:
        raise KcpcUserError(_NO_ENTRIES)
    if len(entries) > MAX_ROTATION:
        raise KcpcUserError(_TOO_MANY)
    return tuple(
        _parse_entry(number, entry, known_tags)
        for number, entry in enumerate(entries, start=1)
    )


def decode_rotation(stored: Sequence[str]) -> tuple[RotationEntry, ...]:
    """The rotation that a server's settings store; the default if they store none.

    An entry that can't be read is skipped, with a warning the first time;
    if none can be read, the default rotation is used.
    """
    entries: list[RotationEntry] = []
    for text in stored:
        entry = _decode_entry(text)
        if entry is not None:
            entries.append(entry)
        elif text not in _reported:
            _reported.add(text)
            logger.warning(
                "Skipping the stored weekly rotation entry %r: it isn't "
                "'platform:band:topic'; set the rotation again with "
                '/kcpc weekly rotation',
                text,
            )
    return tuple(entries) or DEFAULT_ROTATION


def entry_for(rotation: Sequence[RotationEntry], week: date) -> RotationEntry:
    """The rotation's entry for the week of the Friday ``week`` (club time).

    Weeks count from ``ROTATION_ANCHOR``, the first taking the first entry.
    """
    if not rotation:
        raise ValueError('A rotation has at least one entry')
    weeks = (week - ROTATION_ANCHOR).days // 7
    return rotation[weeks % len(rotation)]


def describe_entry(entry: RotationEntry) -> str:
    """The entry as replies show it: 'Codeforces · medium · graphs'."""
    topic = 'any topic' if entry.topic == ANY else entry.topic
    return f'{platform_name(entry.platform)} · {entry.band.value} · {topic}'


def _parse_entry(number: int, text: str, known_tags: Collection[str]) -> RotationEntry:
    """The entry ``text``, the ``number``th of a rotation an admin typed."""
    words = text.split()
    # Repeated on one line, its markdown escaped, as members' topics are.
    shown = shorten(' '.join(words), _SHOWN_LIMIT) or text
    name = _ENTRY.format(number=number, entry=markdown.escape(shown))
    if len(words) < 2:
        raise KcpcUserError(name + _NEEDS_BAND)
    platform = _PLATFORM_ALIASES.get(words[0].lower())
    if platform is None:
        raise KcpcUserError(name + _UNKNOWN_PLATFORM)
    band = parse_band(words[1])
    if band is None:
        raise KcpcUserError(name + _UNKNOWN_BAND)
    try:
        topic = resolve_topic(' '.join(words[2:]) or ANY, known_tags)
    except KcpcUserError as exc:
        raise KcpcUserError(f'{name}: {exc}') from None
    if platform == ATCODER and topic.key != ANY:
        raise KcpcUserError(name + _ATCODER_TOPIC)
    return RotationEntry(platform, band, topic.key)


def _decode_entry(text: str) -> RotationEntry | None:
    """The entry that settings store as ``text``; None if it can't be one."""
    parts = text.split(':')
    if len(parts) != 3:
        return None
    platform, band_value, topic = parts
    try:
        band = Band(band_value)
    except ValueError:
        return None
    if platform not in PLATFORMS or not topic or topic != topic.strip():
        return None
    if platform == ATCODER and topic != ANY:
        return None
    return RotationEntry(platform, band, topic)
