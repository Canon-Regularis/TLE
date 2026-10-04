"""Picking a random problem by topic and difficulty, for /randproblem.

A difficulty is a band (easy, medium, hard or expert) or a rating from 800 to
3500, on Codeforces' scale for both platforms. A band takes the problems rated
inside it. A rating takes those rated within a window of it, and widens the
window in steps until some problem fits: Codeforces rates its problems in
steps of 100, so its first window is the rating itself, while AtCoder's
difficulties convert to any rating, so its first window is 50 either side.
Each problem that fits the first window with any is as likely as the others.
Problems without a rating are never picked.
"""

import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.features.problems.catalog import ATCODER, CODEFORCES, Problem
from tle.kcpc.features.problems.topics import Topic
from tle.kcpc.platforms.difficulty import (
    MAX_RATING,
    MIN_RATING,
    Band,
    band_bounds,
    parse_band,
)

# The windows that a rating is widened through, in turn: on each platform, the
# problems rated at most this far from it, on Codeforces' scale.
_WINDOWS: tuple[Mapping[str, int], ...] = (
    MappingProxyType({CODEFORCES: 0, ATCODER: 50}),
    MappingProxyType({CODEFORCES: 100, ATCODER: 100}),
    MappingProxyType({CODEFORCES: 200, ATCODER: 200}),
)
_RATING_STEP = 100
_RATING = re.compile(r'\d{1,5}', re.ASCII)
# The most suggestions that Discord's autocomplete shows.
_MAX_CHOICES = 25
_BAD_DIFFICULTY = (
    'Give a difficulty: easy, medium, hard or expert, or a rating from 800 to 3500.'
)


@dataclass(frozen=True)
class DifficultyChoice:
    """How hard a member asked a problem to be: a band or a rating, not both."""

    band: Band | None
    rating: int | None  # a multiple of 100 from 800 to 3500

    def __post_init__(self) -> None:
        if (self.band is None) == (self.rating is None):
            raise ValueError('A difficulty is a band or a rating, not both or neither')
        rating = self.rating
        if rating is not None and (
            rating % _RATING_STEP or not MIN_RATING <= rating <= MAX_RATING
        ):
            raise ValueError(f'{rating} is not a rating that problems have')


@dataclass(frozen=True)
class Pick:
    """A problem picked, and how far its rating is from the one asked for."""

    problem: Problem
    distance: int  # 0 when a band was asked for


def parse_difficulty(text: str) -> DifficultyChoice:
    """The difficulty that ``text`` names: a band, whatever its case, or a rating.

    A rating is rounded half up to a multiple of 100, so 1450 is 1500, and must
    then be from 800 to 3500. Raises ``KcpcUserError`` for anything else.
    """
    band = parse_band(text)
    if band is not None:
        return DifficultyChoice(band, None)
    digits = text.strip()
    if _RATING.fullmatch(digits) is None:
        raise KcpcUserError(_BAD_DIFFICULTY)
    # Rounded half up, not half to even as Python's round does.
    rating = (int(digits) + _RATING_STEP // 2) // _RATING_STEP * _RATING_STEP
    if not MIN_RATING <= rating <= MAX_RATING:
        raise KcpcUserError(_BAD_DIFFICULTY)
    return DifficultyChoice(None, rating)


def difficulty_choices(typed: str) -> list[tuple[str, str]]:
    """Autocomplete suggestions for a difficulty: (label, value) pairs, at most 25.

    The bands, each labelled with its ratings, then the ratings from 800 to
    3500; those whose label has ``typed`` in it, whatever its case.
    """
    needle = typed.strip().lower()
    choices = [(_band_label(band), band.value) for band in Band]
    choices += [
        (str(rating), str(rating))
        for rating in range(MIN_RATING, MAX_RATING + 1, _RATING_STEP)
    ]
    return [choice for choice in choices if needle in choice[0].lower()][:_MAX_CHOICES]


def pick(
    problems: Sequence[Problem],
    *,
    topic: Topic,
    difficulty: DifficultyChoice,
    exclude: Callable[[Problem], bool],
    rng: random.Random,
) -> Pick | None:
    """A random problem about ``topic`` as hard as ``difficulty``; None if none is.

    Problems that ``exclude`` is true of (those the member has solved, say)
    are left out. A rating is widened as the module docstring says.
    """
    rated = [
        (problem, problem.rating)
        for problem in problems
        if problem.rating is not None and topic.matches(problem.tags)
    ]
    asked = difficulty.rating
    if asked is None:  # a band, then
        candidates = [
            problem
            for problem, _ in rated
            if problem.band is difficulty.band and not exclude(problem)
        ]
        return Pick(rng.choice(candidates), 0) if candidates else None
    # Each problem in the widest window, with its distance from the rating.
    near = [
        (problem, abs(rating - asked))
        for problem, rating in rated
        if abs(rating - asked) <= _WINDOWS[-1][problem.platform]
    ]
    near = [(problem, distance) for problem, distance in near if not exclude(problem)]
    for window in _WINDOWS:
        fitting = [
            (problem, distance)
            for problem, distance in near
            if distance <= window[problem.platform]
        ]
        if fitting:
            problem, distance = rng.choice(fitting)
            return Pick(problem, distance)
    return None


def window_of(platform: str, distance: int) -> int:
    """The window that ``pick`` took a problem of ``platform`` from, if it is
    ``distance`` from the rating asked for: the narrowest that holds it.
    """
    return next(
        (window[platform] for window in _WINDOWS if distance <= window[platform]),
        distance,
    )


def _band_label(band: Band) -> str:
    """'easy (below 1200)', 'medium (1200 to 1599)', 'expert (2000 and up)'."""
    low, high = band_bounds(band)
    if low is None:
        return f'{band.value} (below {high})'
    if high is None:
        return f'{band.value} ({low} and up)'
    return f'{band.value} ({low} to {high - 1})'
