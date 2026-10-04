"""Tests for tle.kcpc.platforms.difficulty: the bands, and AtCoder's
difficulties clipped as AtCoder Problems shows them and converted to
Codeforces' scale as the rating converter does."""

import pytest

from tle.kcpc.platforms.difficulty import (
    MAX_RATING,
    MIN_RATING,
    Band,
    atcoder_to_codeforces,
    band_bounds,
    band_of,
    clip_atcoder_difficulty,
    parse_band,
)


class TestBands:
    def test_are_easy_medium_hard_and_expert_in_order(self) -> None:
        assert [band.value for band in Band] == ['easy', 'medium', 'hard', 'expert']
        assert Band.MEDIUM == 'medium'  # a str, to store and show as it is

    @pytest.mark.parametrize(
        ('band', 'bounds'),
        [
            (Band.EASY, (None, 1200)),
            (Band.MEDIUM, (1200, 1600)),
            (Band.HARD, (1600, 2000)),
            (Band.EXPERT, (2000, None)),
        ],
    )
    def test_bounds(self, band: Band, bounds: tuple[int | None, int | None]) -> None:
        assert band_bounds(band) == bounds

    @pytest.mark.parametrize(
        ('rating', 'band'),
        [
            (-801, Band.EASY),
            (0, Band.EASY),
            (800, Band.EASY),
            (1199, Band.EASY),
            (1200, Band.MEDIUM),
            (1599, Band.MEDIUM),
            (1600, Band.HARD),
            (1999, Band.HARD),
            (2000, Band.EXPERT),
            (3500, Band.EXPERT),
            (4027, Band.EXPERT),
        ],
    )
    def test_band_of_a_rating(self, rating: int, band: Band) -> None:
        assert band_of(rating) == band

    def test_every_rating_is_within_its_bands_bounds(self) -> None:
        for rating in range(MIN_RATING - 200, MAX_RATING + 201):
            low, high = band_bounds(band_of(rating))
            assert low is None or low <= rating, rating
            assert high is None or rating < high, rating

    def test_codeforces_rates_problems_from_800_to_3500(self) -> None:
        assert (MIN_RATING, MAX_RATING) == (800, 3500)


class TestParseBand:
    @pytest.mark.parametrize(
        ('text', 'band'),
        [
            ('easy', Band.EASY),
            ('Medium', Band.MEDIUM),
            ('HARD', Band.HARD),
            (' expert\n', Band.EXPERT),
        ],
    )
    def test_names_a_band_whatever_the_case(self, text: str, band: Band) -> None:
        assert parse_band(text) is band

    @pytest.mark.parametrize(
        'text',
        ['', ' ', 'easier', 'med', '1600', 'easy medium', 'Band.EASY', 'ｅａｓｙ'],
        ids=[
            'empty',
            'blank',
            'other-word',
            'prefix',
            'rating',
            'two',
            'enum-name',
            'full-width',
        ],
    )
    def test_anything_else_is_none(self, text: str) -> None:
        assert parse_band(text) is None


class TestClipAtCoderDifficulty:
    @pytest.mark.parametrize(
        ('raw', 'clipped'),
        [
            (-10000, 0),
            (-870, 17),
            (-249, 79),
            (0, 147),
            (200, 243),
            (399, 399),
            (400, 400),
            (946, 946),
            (1235, 1235),
            (4383, 4383),
        ],
    )
    def test_as_atcoder_problems_shows_it(self, raw: int, clipped: int) -> None:
        assert clip_atcoder_difficulty(raw) == clipped

    def test_below_400_rises_with_the_estimate_and_stays_in_0_to_399(self) -> None:
        clipped = [clip_atcoder_difficulty(raw) for raw in range(-10000, 400)]
        assert clipped == sorted(clipped)
        assert (clipped[0], clipped[-1]) == (0, 399)

    @pytest.mark.parametrize('raw', [-300_000, -(10**6), -(10**400)])
    def test_far_below_any_estimate_is_0(self, raw: int) -> None:
        # Where JavaScript's exp is Infinity, and Python's would overflow.
        assert clip_atcoder_difficulty(raw) == 0


class TestAtCoderToCodeforces:
    @pytest.mark.parametrize(
        ('raw', 'clipped', 'rating', 'band'),
        [
            (-835, 18, 724, Band.EASY),
            (-197, 90, 779, Band.EASY),
            (426, 426, 1033, Band.EASY),
            (729, 729, 1262, Band.MEDIUM),
            (1376, 1376, 1752, Band.HARD),
            (1588, 1588, 1912, Band.HARD),
            (2582, 2582, 2664, Band.EXPERT),
        ],
        ids=['A', 'B', 'C', 'D', 'E', 'F', 'G'],
    )
    def test_abc478_as_atcoder_problems_and_the_converter_have_it(
        self, raw: int, clipped: int, rating: int, band: Band
    ) -> None:
        assert clip_atcoder_difficulty(raw) == clipped
        assert atcoder_to_codeforces(clipped) == rating
        assert band_of(rating) == band

    @pytest.mark.parametrize(
        ('difficulty', 'rating', 'band'),
        [
            (646, 1199, Band.EASY),
            (647, 1200, Band.MEDIUM),
            (1174, 1599, Band.MEDIUM),
            (1175, 1600, Band.HARD),
            (1703, 1999, Band.HARD),
            (1704, 2000, Band.EXPERT),
        ],
    )
    def test_the_bands_split_at_these_clipped_difficulties(
        self, difficulty: int, rating: int, band: Band
    ) -> None:
        assert atcoder_to_codeforces(difficulty) == rating
        assert band_of(atcoder_to_codeforces(difficulty)) == band

    def test_the_lowest_clipped_difficulty_is_about_711(self) -> None:
        assert atcoder_to_codeforces(0) == 711

    @pytest.mark.parametrize(
        ('difficulty', 'rating'), [(-940, 0), (-941, 0), (-2000, -801)]
    )
    def test_truncates_toward_zero(self, difficulty: int, rating: int) -> None:
        # As the converter's '| 0' does: -0.76 is 0, not -1.
        assert atcoder_to_codeforces(difficulty) == rating
