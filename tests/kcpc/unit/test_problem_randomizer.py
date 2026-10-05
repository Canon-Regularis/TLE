"""Tests for tle.kcpc.features.problems.randomizer: /randproblem's picks."""

import random
from collections.abc import Callable, Sequence
from dataclasses import replace

import pytest

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.features.problems.catalog import Problem
from tle.kcpc.features.problems.randomizer import (
    DifficultyChoice,
    Pick,
    difficulty_choices,
    parse_difficulty,
    pick,
    window_of,
)
from tle.kcpc.features.problems.topics import ANY, CURATED, Topic
from tle.kcpc.platforms.atcoder.problems import AtCoderProblem
from tle.kcpc.platforms.codeforces import CodeforcesProblem
from tle.kcpc.platforms.difficulty import Band

BAD_DIFFICULTY = (
    'Give a difficulty: easy, medium, hard or expert, or a rating from 800 to 3500.'
)
ANY_TOPIC = Topic(ANY, None)
GRAPHS = Topic('graphs', CURATED['graphs'])
SEEDS = range(60)


def codeforces(contest_id: int, index: str, rating: int | None, *tags: str) -> Problem:
    return Problem.from_codeforces(
        CodeforcesProblem(
            contest_id=contest_id,
            index=index,
            name=f'Problem {index} of round {contest_id}',
            rating=rating,
            tags=tags or ('implementation',),
            solved_count=1000,
            standard=True,
        )
    )


def atcoder(problem_id: str, rating: int | None) -> Problem:
    """An AtCoder problem whose difficulty is ``rating`` on Codeforces' scale."""
    contest_id, letter = problem_id.rsplit('_', 1)
    problem = Problem.from_atcoder(
        AtCoderProblem(problem_id, contest_id, letter.upper(), 'Task', 1000)
    )
    return replace(problem, rating=rating)


def ids(problems: Sequence[Problem]) -> set[str]:
    return {problem.problem_id for problem in problems}


def never(problem: Problem) -> bool:
    return False


def picked(
    problems: Sequence[Problem],
    difficulty: DifficultyChoice,
    *,
    topic: Topic = ANY_TOPIC,
    exclude: Callable[[Problem], bool] = never,
) -> set[tuple[str, int]]:
    """Each problem that ``pick`` chooses with any of many seeds, with its
    distance.
    """
    found: set[tuple[str, int]] = set()
    for seed in SEEDS:
        choice = pick(
            problems,
            topic=topic,
            difficulty=difficulty,
            exclude=exclude,
            rng=random.Random(seed),
        )
        assert choice is not None
        found.add((choice.problem.problem_id, choice.distance))
    return found


def band(value: Band) -> DifficultyChoice:
    return DifficultyChoice(value, None)


def rating(value: int) -> DifficultyChoice:
    return DifficultyChoice(None, value)


class TestParseDifficulty:
    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('easy', Band.EASY),
            ('Medium', Band.MEDIUM),
            (' HARD ', Band.HARD),
            ('expert', Band.EXPERT),
        ],
    )
    def test_a_band_whatever_its_case(self, text: str, expected: Band) -> None:
        assert parse_difficulty(text) == DifficultyChoice(expected, None)

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('1500', 1500),
            (' 1600 ', 1600),
            ('1450', 1500),  # half up, unlike Python's round
            ('1449', 1400),
            ('1550', 1600),
            ('750', 800),
            ('800', 800),
            ('3500', 3500),
            ('3549', 3500),
            ('01500', 1500),
        ],
    )
    def test_a_rating_rounded_half_up_to_a_hundred(
        self, text: str, expected: int
    ) -> None:
        assert parse_difficulty(text) == DifficultyChoice(None, expected)

    @pytest.mark.parametrize(
        'text',
        [
            '',
            'any',
            'hardest',
            '749',
            '3550',
            '0',
            '-800',
            '+1500',
            '1500.0',
            '1,500',
            '15000',
            '１５００',  # fullwidth digits
            '1500 easy',
        ],
    )
    def test_anything_else_is_refused(self, text: str) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_difficulty(text)

        assert str(raised.value) == BAD_DIFFICULTY

    @pytest.mark.parametrize(
        ('chosen_band', 'chosen_rating'),
        [(None, None), (Band.EASY, 800), (None, 1550), (None, 700), (None, 3600)],
    )
    def test_a_difficulty_is_one_band_or_one_rating_that_problems_have(
        self, chosen_band: Band | None, chosen_rating: int | None
    ) -> None:
        with pytest.raises(ValueError):
            DifficultyChoice(chosen_band, chosen_rating)


class TestDifficultyChoices:
    def test_the_bands_then_the_ratings_at_most_25(self) -> None:
        choices = difficulty_choices('')

        assert choices[:4] == [
            ('easy (below 1200)', 'easy'),
            ('medium (1200 to 1599)', 'medium'),
            ('hard (1600 to 1999)', 'hard'),
            ('expert (2000 and up)', 'expert'),
        ]
        assert [value for _, value in choices[4:]] == [
            str(value) for value in range(800, 2900, 100)
        ]

    def test_suggestions_have_the_typed_text_in_their_labels(self) -> None:
        assert difficulty_choices(' 15') == [
            ('medium (1200 to 1599)', 'medium'),
            ('1500', '1500'),
        ]
        assert difficulty_choices('EX') == [('expert (2000 and up)', 'expert')]
        assert difficulty_choices('35') == [('3500', '3500')]
        assert difficulty_choices('xyz') == []

    def test_ratings_beyond_the_first_25_are_found_by_typing(self) -> None:
        assert [value for _, value in difficulty_choices('3')] == [
            '1300',
            '2300',
            '3000',
            '3100',
            '3200',
            '3300',
            '3400',
            '3500',
        ]


class TestPickByBand:
    def test_a_band_takes_the_ratings_inside_its_bounds(self) -> None:
        problems = [
            atcoder('abc001_a', 1199),
            atcoder('abc001_b', 1200),
            atcoder('abc001_c', 1599),
            atcoder('abc001_d', 1600),
        ]

        assert picked(problems, band(Band.MEDIUM)) == {('abc001_b', 0), ('abc001_c', 0)}
        assert picked(problems, band(Band.EASY)) == {('abc001_a', 0)}
        assert picked(problems, band(Band.HARD)) == {('abc001_d', 0)}

    def test_easy_and_expert_have_no_far_bound(self) -> None:
        problems = [codeforces(1, 'A', 800), codeforces(2, 'A', 3500)]

        assert picked(problems, band(Band.EASY)) == {('1A', 0)}
        assert picked(problems, band(Band.EXPERT)) == {('2A', 0)}

    def test_problems_without_a_rating_are_never_picked(self) -> None:
        problems = [codeforces(1, 'A', None), atcoder('abc001_a', None)]

        for value in Band:
            assert (
                pick(
                    problems,
                    topic=ANY_TOPIC,
                    difficulty=band(value),
                    exclude=never,
                    rng=random.Random(1),
                )
                is None
            )
        assert (
            pick(
                problems,
                topic=ANY_TOPIC,
                difficulty=rating(1500),
                exclude=never,
                rng=random.Random(1),
            )
            is None
        )

    def test_a_topic_takes_the_problems_with_one_of_its_tags(self) -> None:
        problems = [
            codeforces(1, 'A', 1300, 'trees', 'dp'),
            codeforces(2, 'A', 1300, 'dp'),
            codeforces(3, 'A', 1300, 'graphs'),
            atcoder('abc001_a', 1300),  # AtCoder's have no tags
        ]

        assert picked(problems, band(Band.MEDIUM), topic=GRAPHS) == {
            ('1A', 0),
            ('3A', 0),
        }
        assert len(picked(problems, band(Band.MEDIUM))) == 4

    def test_excluded_problems_are_left_out(self) -> None:
        problems = [codeforces(1, 'A', 1300), codeforces(2, 'A', 1300)]

        def solved(problem: Problem) -> bool:
            return problem.problem_id == '1A'

        assert picked(problems, band(Band.MEDIUM), exclude=solved) == {('2A', 0)}

    def test_nothing_found_is_none(self) -> None:
        problems = [codeforces(1, 'A', 1300, 'dp')]

        def solved(problem: Problem) -> bool:
            return True

        for topic, exclude in ((GRAPHS, never), (ANY_TOPIC, solved)):
            assert (
                pick(
                    problems,
                    topic=topic,
                    difficulty=band(Band.MEDIUM),
                    exclude=exclude,
                    rng=random.Random(1),
                )
                is None
            )
        assert (
            pick(
                problems,
                topic=ANY_TOPIC,
                difficulty=band(Band.EASY),
                exclude=never,
                rng=random.Random(1),
            )
            is None
        )

    def test_each_candidate_is_as_likely_and_a_seed_repeats_its_pick(self) -> None:
        problems = [codeforces(n, 'A', 1300) for n in range(1, 5)]

        choices = [
            pick(
                problems,
                topic=ANY_TOPIC,
                difficulty=band(Band.MEDIUM),
                exclude=never,
                rng=random.Random(seed),
            )
            for seed in range(400)
        ]

        counts = {problem.problem_id: 0 for problem in problems}
        for choice in choices:
            assert choice is not None
            counts[choice.problem.problem_id] += 1
        assert all(70 <= count <= 130 for count in counts.values()), counts
        again = pick(
            problems,
            topic=ANY_TOPIC,
            difficulty=band(Band.MEDIUM),
            exclude=never,
            rng=random.Random(7),
        )
        assert again == choices[7]
        assert again == Pick(random.Random(7).choice(problems), 0)


class TestPickByRating:
    def test_codeforces_takes_the_rating_itself_first(self) -> None:
        problems = [
            codeforces(1, 'A', 1500),
            codeforces(2, 'A', 1500),
            codeforces(3, 'A', 1400),
            codeforces(4, 'A', 1600),
        ]

        assert picked(problems, rating(1500)) == {('1A', 0), ('2A', 0)}

    def test_codeforces_widens_by_100_then_200(self) -> None:
        problems = [
            codeforces(3, 'A', 1400),
            codeforces(4, 'A', 1600),
            codeforces(5, 'A', 1300),
            codeforces(6, 'A', 1800),
        ]

        assert picked(problems, rating(1500)) == {('3A', 100), ('4A', 100)}
        assert picked(problems[2:], rating(1500)) == {('5A', 200)}
        assert (
            pick(
                [codeforces(7, 'A', 1200), codeforces(8, 'A', 1800)],
                topic=ANY_TOPIC,
                difficulty=rating(1500),
                exclude=never,
                rng=random.Random(1),
            )
            is None
        )

    def test_atcoder_takes_50_either_side_first(self) -> None:
        problems = [
            atcoder('abc001_a', 1450),
            atcoder('abc001_b', 1550),
            atcoder('abc001_c', 1449),
            atcoder('abc001_d', 1551),
            atcoder('abc001_e', 1700),
        ]

        assert picked(problems, rating(1500)) == {('abc001_a', 50), ('abc001_b', 50)}
        assert picked(problems[2:], rating(1500)) == {
            ('abc001_c', 51),
            ('abc001_d', 51),
        }
        assert picked(problems[4:], rating(1500)) == {('abc001_e', 200)}
        assert (
            pick(
                [atcoder('abc001_f', 1701)],
                topic=ANY_TOPIC,
                difficulty=rating(1500),
                exclude=never,
                rng=random.Random(1),
            )
            is None
        )

    def test_each_platform_keeps_its_own_windows_in_one_list(self) -> None:
        problems = [
            codeforces(1, 'A', 1500),
            atcoder('abc001_a', 1530),
            codeforces(2, 'A', 1600),
        ]

        assert picked(problems, rating(1500)) == {('1A', 0), ('abc001_a', 30)}

    def test_a_solved_problem_widens_the_search(self) -> None:
        problems = [codeforces(1, 'A', 1500), codeforces(2, 'A', 1600)]

        def solved(problem: Problem) -> bool:
            return problem.problem_id == '1A'

        assert picked(problems, rating(1500), exclude=solved) == {('2A', 100)}

    def test_a_topic_narrows_the_search_before_it_widens(self) -> None:
        problems = [
            codeforces(1, 'A', 1500, 'dp'),
            codeforces(2, 'A', 1700, 'graphs'),
        ]

        assert picked(problems, rating(1500), topic=GRAPHS) == {('2A', 200)}


class TestWindowOf:
    @pytest.mark.parametrize(
        ('platform', 'distance', 'window'),
        [
            ('codeforces', 0, 0),
            ('codeforces', 100, 100),
            ('codeforces', 200, 200),
            ('atcoder', 0, 50),
            ('atcoder', 50, 50),
            ('atcoder', 51, 100),
            ('atcoder', 100, 100),
            ('atcoder', 101, 200),
            ('atcoder', 200, 200),
        ],
    )
    def test_a_pick_comes_from_the_narrowest_window_that_holds_it(
        self, platform: str, distance: int, window: int
    ) -> None:
        assert window_of(platform, distance) == window
