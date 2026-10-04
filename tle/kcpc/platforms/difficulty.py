"""How hard a problem is, on one scale for Codeforces and AtCoder.

Codeforces rates its problems from 800 to 3500, in steps of 100. AtCoder rates
none, but AtCoder Problems (kenkoooo.com) estimates a difficulty for most
problems of rated contests, on AtCoder's rating scale, where the easiest go
far below zero. AtCoder Problems shows them clipped, and so does KCPC
(``clip_atcoder_difficulty``). A clipped difficulty converts to a rating on
Codeforces' scale (``atcoder_to_codeforces``), so that the problems of both
sites fall into the same bands.
"""

import math
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType


class Band(str, Enum):
    """A range of Codeforces-equivalent ratings that members ask for."""

    EASY = 'easy'
    MEDIUM = 'medium'
    HARD = 'hard'
    EXPERT = 'expert'


# The lowest and highest ratings Codeforces gives a problem.
MIN_RATING = 800
MAX_RATING = 3500

# The Codeforces-equivalent ratings of each band, from the first (included) to
# the second (not included); None is no bound.
_BOUNDS: Mapping[Band, tuple[int | None, int | None]] = MappingProxyType(
    {
        Band.EASY: (None, 1200),
        Band.MEDIUM: (1200, 1600),
        Band.HARD: (1600, 2000),
        Band.EXPERT: (2000, None),
    }
)

# AtCoder Problems shows difficulties from 400 up as they are, and squeezes
# lower ones into 0-399.
_CLIPPED_BELOW = 400

# A line fitted over users rated on both sites, as the rating converter at
# https://silverfoxxxy.github.io/converter.js has it:
# Codeforces = 3900 * (AtCoder + 940) / 5155.
_CODEFORCES_SPAN = 3900
_ATCODER_OFFSET = 940
_ATCODER_SPAN = 5155


def band_bounds(band: Band) -> tuple[int | None, int | None]:
    """The Codeforces-equivalent ratings in ``band``: from the first, included,
    to the second, not included. None is no bound: easy is (None, 1200) and
    expert (2000, None).
    """
    return _BOUNDS[band]


def band_of(rating: int) -> Band:
    """The band that a Codeforces-equivalent rating is in."""
    # The bands cover every rating, so one always matches.
    return next(
        band
        for band, (low, high) in _BOUNDS.items()
        if (low is None or rating >= low) and (high is None or rating < high)
    )


def parse_band(text: str) -> Band | None:
    """The band that ``text`` names, whatever its case and surrounding
    whitespace; None if it names none.
    """
    try:
        return Band(text.strip().lower())
    except ValueError:
        return None


def clip_atcoder_difficulty(raw: int) -> int:
    """An AtCoder Problems difficulty as its site shows it.

    Difficulties from 400 up are kept; lower ones become
    ``400 / exp(1 - raw / 400)``, rounded half up, so 0 becomes 147 and none
    goes below 0. The site rounds with JavaScript's Math.round, half up,
    unlike Python's round, which rounds half to even.
    """
    if raw >= _CLIPPED_BELOW:
        return raw
    try:
        return math.floor(_CLIPPED_BELOW / math.exp(1 - raw / _CLIPPED_BELOW) + 0.5)
    except OverflowError:
        # Far below anything estimated (the lowest is -10000), where
        # JavaScript's exp is Infinity and the site shows 0.
        return 0


def atcoder_to_codeforces(difficulty: int) -> int:
    """A clipped AtCoder difficulty as a rating on Codeforces' scale.

    Truncated toward zero, as the converter does: 646 is 1199 and 647 is 1200.
    The line was fitted to users, so for problems it is only a guide.
    """
    # In integers, so that no float rounding moves a rating across a band.
    scaled = _CODEFORCES_SPAN * (difficulty + _ATCODER_OFFSET)
    rating = abs(scaled) // _ATCODER_SPAN
    return rating if scaled >= 0 else -rating
