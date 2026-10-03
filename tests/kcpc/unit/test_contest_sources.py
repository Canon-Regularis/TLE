"""Tests for tle.kcpc.features.contests.sources.is_contest, with the patterns
of CodeChef's events that aren't contests, ``CODECHEF_NOT_CONTESTS``.

The names in CODECHEF_CONTESTS, CODECHEF_EVENTS and LEETCODE_CONTESTS are
clist.by's, as it listed those sites' events from February to October 2026.
The other tests make up names like them.
"""

import pytest

from tle.kcpc.features.contests.sources import CODECHEF_NOT_CONTESTS, is_contest

# CodeChef's contests, which last 2 or 3 hours.
CODECHEF_CONTESTS = [
    'Starters 258 (Rated till 5 Star)',
    'Starters 257 (Rated till 6 Star)',
    'Starters 255 (Rated still 6 stars)',
    'Starters 248 (Unrated)',
    'Starters 234 (Rated for all)',
    'Monday Munch - DSA Challenge 023 (Rated)',
    'Monday Munch - DSA Challenge 35',
]
# CodeChef's events that aren't contests, which take a weekend.
CODECHEF_EVENTS = [
    'Placement Prep Weekends - 10',
    'Placement Prep Weekends - 01',
    'Weekend Dev Challenge 60: LLD Projects',
    'Weekend Dev Challenge 56: .NET projects',
    'Weekend Dev Challenge 47: Cybersecurity',
    'Weekend Dev Challenge 40: Linux & Shell Scripting Challenges',
]
LEETCODE_CONTESTS = ['Weekly Contest 522', 'Biweekly Contest 193']


@pytest.mark.parametrize('name', CODECHEF_CONTESTS + LEETCODE_CONTESTS)
def test_contests_are_contests(name: str) -> None:
    assert is_contest(name, CODECHEF_NOT_CONTESTS)


@pytest.mark.parametrize(
    'name',
    [
        'October Challenge 2021 Division 1',  # a Long Challenge, 10 days long
        'September Cook-Off 2021 Division 2',
        'September Lunchtime 2021 Division 3',
        'SnackDown 2021 Online Qualifier Round',
    ],
)
def test_contests_of_codechefs_earlier_series_are_contests(name: str) -> None:
    # Names in the style of the series it ran before Starters, as it could
    # run them again.
    assert is_contest(name, CODECHEF_NOT_CONTESTS)


@pytest.mark.parametrize('name', CODECHEF_EVENTS)
def test_codechefs_weekend_events_are_not_contests(name: str) -> None:
    assert not is_contest(name, CODECHEF_NOT_CONTESTS)


@pytest.mark.parametrize(
    'name',
    [
        'PLACEMENT PREP WEEKENDS - 11',
        'placement prep weekends - 11',
        'Weekend DEV challenge 61: Rust Projects',
        'Placement  Prep Weekends - 11',
        'Weekend Dev  Challenge 61: Rust Projects',
        'Placement\xa0Prep Weekends - 11',  # a no-break space
    ],
)
def test_a_series_is_found_whatever_its_case_and_spacing(name: str) -> None:
    assert not is_contest(name, CODECHEF_NOT_CONTESTS)


@pytest.mark.parametrize(
    'name',
    [
        'CodeChef Placement Prep Weekends - 11',
        'Placement Preparation Weekend 1',
        'Dev Challenge 62',
    ],
)
def test_a_series_is_found_anywhere_in_a_name(name: str) -> None:
    assert not is_contest(name, CODECHEF_NOT_CONTESTS)


@pytest.mark.parametrize(
    'name',
    [
        'Starters 260 (Placement Special)',
        'Starters 261: Prep for placements',
        'Monday Munch - DSA Challenge 024 (Rated)',
        'Starters 262: Displacement Prep',
    ],
)
def test_a_name_that_only_shares_words_with_a_series_is_a_contest(name: str) -> None:
    # An event counts as a contest unless its name names a series that isn't.
    assert is_contest(name, CODECHEF_NOT_CONTESTS)


def test_without_patterns_every_event_is_a_contest() -> None:
    assert is_contest('Placement Prep Weekends - 10', ())
