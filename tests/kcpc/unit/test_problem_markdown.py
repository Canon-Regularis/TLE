"""Tests for tle.kcpc.features.problems.markdown: text shown as written."""

from tle.kcpc.features.problems.markdown import LINK_LIMIT, escape, fits, link


def test_the_characters_discord_reads_are_escaped() -> None:
    assert escape(r'[x](y) *b* _i_ ~s~ `c` ||h|| \ ') == (
        r'\[x\](y) \*b\* \_i\_ \~s\~ \`c\` \|\|h\|\| \\ '
    )
    assert escape('1520D - Same Differences') == '1520D - Same Differences'


def test_a_link_shows_its_text_escaped() -> None:
    assert link('Sum *of* [Two]', 'https://example.com/a_(1)') == (
        r'[Sum \*of\* \[Two\]](https://example.com/a_%281%29)'
    )


def test_a_link_too_long_to_show_is_its_text_alone() -> None:
    assert LINK_LIMIT == 300
    longest = 'https://example.com/' + 'x' * 280
    # Each bracket takes three characters in the link.
    too_long = 'https://example.com/(' + 'x' * 278 + ')'

    assert (len(longest), len(too_long)) == (300, 300)
    assert fits(longest)
    assert link('Editorial', longest) == f'[Editorial]({longest})'
    assert not fits(too_long)
    assert link('Editorial_1', too_long) == r'Editorial\_1'
