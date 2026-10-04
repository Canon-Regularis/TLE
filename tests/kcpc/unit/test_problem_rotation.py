"""Tests for tle.kcpc.features.problems.rotation: what each week is picked from."""

import logging
from datetime import date, timedelta

import pytest

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.features.problems import rotation
from tle.kcpc.features.problems.rotation import (
    DEFAULT_ROTATION,
    MAX_ROTATION,
    ROTATION_ANCHOR,
    RotationEntry,
    decode_rotation,
    describe_entry,
    entry_for,
    parse_rotation,
)
from tle.kcpc.features.problems.topics import KNOWN_TAGS
from tle.kcpc.platforms.difficulty import Band

LOGGER = 'tle.kcpc.features.problems.rotation'
CF_EASY = RotationEntry('codeforces', Band.EASY, 'any')
AC_MEDIUM = RotationEntry('atcoder', Band.MEDIUM, 'any')
CF_MEDIUM = RotationEntry('codeforces', Band.MEDIUM, 'any')
AC_HARD = RotationEntry('atcoder', Band.HARD, 'any')
WEEK = timedelta(weeks=1)


@pytest.fixture(autouse=True)
def nothing_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget the stored entries warned about, which the module remembers."""
    monkeypatch.setattr(rotation, '_reported', set())


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.levelno == logging.WARNING
    ]


class TestEntries:
    def test_an_entry_is_stored_as_platform_band_and_topic(self) -> None:
        assert CF_EASY.encode() == 'codeforces:easy:any'
        entry = RotationEntry('codeforces', Band.HARD, 'dfs and similar')
        assert entry.encode() == 'codeforces:hard:dfs and similar'

    def test_an_entry_is_for_a_platform_with_problems(self) -> None:
        with pytest.raises(ValueError, match="no problems of 'topcoder'"):
            RotationEntry('topcoder', Band.EASY, 'any')

    def test_atcoder_entries_are_for_any_topic(self) -> None:
        with pytest.raises(ValueError, match='AtCoder problems have no topics'):
            RotationEntry('atcoder', Band.EASY, 'graphs')

    def test_the_default_alternates_platforms_and_climbs(self) -> None:
        assert [entry.encode() for entry in DEFAULT_ROTATION] == [
            'codeforces:easy:any',
            'atcoder:medium:any',
            'codeforces:medium:any',
            'atcoder:hard:any',
        ]

    def test_entries_are_described_for_replies(self) -> None:
        graphs = RotationEntry('codeforces', Band.MEDIUM, 'graphs')

        assert describe_entry(graphs) == 'Codeforces · medium · graphs'
        assert describe_entry(AC_HARD) == 'AtCoder · hard · any topic'


class TestParseRotation:
    def test_entries_are_separated_by_commas_or_semicolons(self) -> None:
        rotation = parse_rotation(
            'cf easy, ac medium; codeforces medium graphs ;atcoder hard', KNOWN_TAGS
        )

        assert rotation == (
            CF_EASY,
            AC_MEDIUM,
            RotationEntry('codeforces', Band.MEDIUM, 'graphs'),
            AC_HARD,
        )

    def test_words_are_read_whatever_their_case(self) -> None:
        rotation = parse_rotation('CF Expert DP, AtCoder EASY Any', KNOWN_TAGS)

        assert rotation == (
            RotationEntry('codeforces', Band.EXPERT, 'dp'),
            RotationEntry('atcoder', Band.EASY, 'any'),
        )

    def test_a_topic_may_have_several_words(self) -> None:
        rotation = parse_rotation(
            'cf hard dfs  and similar, cf easy number-theory', KNOWN_TAGS
        )

        assert rotation == (
            RotationEntry('codeforces', Band.HARD, 'dfs and similar'),
            RotationEntry('codeforces', Band.EASY, 'number-theory'),
        )

    def test_blank_entries_are_skipped(self) -> None:
        assert parse_rotation(' ,cf easy,, ;ac hard, ', KNOWN_TAGS) == (
            CF_EASY,
            AC_HARD,
        )

    def test_a_topic_codeforces_added_since_is_taken(self) -> None:
        rotation = parse_rotation('cf easy quantum', KNOWN_TAGS | {'quantum'})

        assert rotation == (RotationEntry('codeforces', Band.EASY, 'quantum'),)

    @pytest.mark.parametrize('text', ['', '   ', ', ;, '])
    def test_a_rotation_needs_an_entry(self, text: str) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_rotation(text, KNOWN_TAGS)

        assert str(raised.value) == (
            'Give at least one entry, such as: cf easy, ac medium, cf medium '
            'graphs, ac hard'
        )

    def test_a_rotation_has_at_most_a_year_of_entries(self) -> None:
        year = ', '.join(['cf easy'] * MAX_ROTATION)
        assert len(parse_rotation(year, KNOWN_TAGS)) == 52

        with pytest.raises(KcpcUserError) as raised:
            parse_rotation(year + ', ac hard', KNOWN_TAGS)

        assert str(raised.value) == (
            'A rotation has at most 52 entries, one for each week.'
        )

    @pytest.mark.parametrize(
        ('text', 'message'),
        [
            (
                'cf easy, cf',
                "Entry 2 ('cf') needs a platform and a band, such as cf medium graphs.",
            ),
            (
                'topcoder easy',
                "Entry 1 ('topcoder easy') has no platform I know: use cf "
                '(Codeforces) or ac (AtCoder).',
            ),
            (
                'cf easy, ac medium, cf hardest',
                "Entry 3 ('cf hardest') has no band I know: use easy, medium, hard "
                'or expert.',
            ),
            (
                'cf easy, ac medium graphs',
                "Entry 2 ('ac medium graphs') is for AtCoder, whose problems have no "
                'topics: leave the topic out.',
            ),
            (
                'cf easy grph',
                "Entry 1 ('cf easy grph'): There's no topic called 'grph'. Did you "
                'mean graphs?',
            ),
        ],
        ids=['no band', 'platform', 'band', 'atcoder topic', 'topic'],
    )
    def test_a_bad_entry_is_named_by_its_number(self, text: str, message: str) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_rotation(text, KNOWN_TAGS)

        assert str(raised.value) == message

    def test_a_bad_entry_is_repeated_on_one_line_with_its_markdown_escaped(
        self,
    ) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_rotation('cf easy\n# [x](https://example.com)', KNOWN_TAGS)

        assert str(raised.value).startswith(
            "Entry 1 ('cf easy # \\[x\\](https://example.com)'): There's no topic "
            "called '# \\[x\\](https://example.com)'."
        )

    def test_a_long_bad_entry_is_shortened_in_the_error(self) -> None:
        with pytest.raises(KcpcUserError) as raised:
            parse_rotation('cf ' + 'x' * 100, KNOWN_TAGS)

        assert str(raised.value).startswith(f"Entry 1 ('cf {'x' * 36}…') has no band")

    def test_entries_parsed_are_stored_and_read_back(self) -> None:
        rotation = parse_rotation(
            'cf easy, ac medium, cf hard dfs and similar, cf expert data-structures',
            KNOWN_TAGS,
        )

        stored = tuple(entry.encode() for entry in rotation)

        assert stored == (
            'codeforces:easy:any',
            'atcoder:medium:any',
            'codeforces:hard:dfs and similar',
            'codeforces:expert:data-structures',
        )
        assert decode_rotation(stored) == rotation


class TestDecodeRotation:
    def test_no_stored_entries_is_the_default_rotation(self) -> None:
        assert decode_rotation(()) == DEFAULT_ROTATION

    def test_bad_stored_entries_are_skipped_with_a_warning_each_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored = (
            'codeforces:easy',
            'codeforces:easy:any',
            'topcoder:easy:any',
            'codeforces:EASY:any',
            'codeforces:easy:',
            'codeforces:easy: any',
            'atcoder:hard:graphs',
            'atcoder:hard:any:extra',
            'atcoder:hard:any',
        )

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert decode_rotation(stored) == (CF_EASY, AC_HARD)
            assert decode_rotation(stored) == (CF_EASY, AC_HARD)

        assert warnings(caplog) == [
            f"Skipping the stored weekly rotation entry {text!r}: it isn't "
            "'platform:band:topic'; set the rotation again with /kcpc weekly rotation"
            for text in stored
            if text not in ('codeforces:easy:any', 'atcoder:hard:any')
        ]

    def test_with_no_entry_left_the_default_rotation_is_used(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert decode_rotation(('nonsense: unused',)) == DEFAULT_ROTATION

        assert len(warnings(caplog)) == 1


class TestEntryFor:
    def test_weeks_count_from_the_anchor_friday(self) -> None:
        assert ROTATION_ANCHOR == date(2026, 1, 2)
        assert ROTATION_ANCHOR.weekday() == 4  # a Friday

        weeks = [
            entry_for(DEFAULT_ROTATION, ROTATION_ANCHOR + n * WEEK) for n in range(6)
        ]

        assert weeks == [CF_EASY, AC_MEDIUM, CF_MEDIUM, AC_HARD, CF_EASY, AC_MEDIUM]

    def test_october_2026(self) -> None:
        # 39 weeks after the anchor.
        assert [
            entry_for(DEFAULT_ROTATION, date(2026, 10, day))
            for day in (2, 9, 16, 23, 30)
        ] == [AC_HARD, CF_EASY, AC_MEDIUM, CF_MEDIUM, AC_HARD]

    def test_any_day_of_a_week_is_that_week(self) -> None:
        assert entry_for(DEFAULT_ROTATION, date(2026, 1, 8)) == CF_EASY  # Thursday
        assert entry_for(DEFAULT_ROTATION, date(2026, 1, 9)) == AC_MEDIUM

    def test_weeks_before_the_anchor_count_back(self) -> None:
        assert entry_for(DEFAULT_ROTATION, date(2025, 12, 26)) == AC_HARD

    def test_a_rotation_of_one_entry_is_every_week(self) -> None:
        assert entry_for((AC_HARD,), date(2026, 10, 9)) == AC_HARD

    def test_a_rotation_has_an_entry(self) -> None:
        with pytest.raises(ValueError, match='at least one entry'):
            entry_for((), ROTATION_ANCHOR)
