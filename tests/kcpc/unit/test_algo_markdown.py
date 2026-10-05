"""Tests for tle.kcpc.features.algo.markdown: text shown as written."""

from tle.kcpc.features.algo.markdown import escape, link


def test_the_characters_discord_reads_are_escaped() -> None:
    assert escape(r'[x](y) *b* _i_ ~s~ `c` ||h|| \ ') == (
        r'\[x\](y) \*b\* \_i\_ \~s\~ \`c\` \|\|h\|\| \\ '
    )
    assert escape('Sprague-Grundy theorem') == 'Sprague-Grundy theorem'


def test_a_link_shows_its_text_escaped() -> None:
    assert link('Segment *tree* [1]', 'https://example.com/a_(1)#b') == (
        r'[Segment \*tree\* \[1\]](https://example.com/a_%281%29#b)'
    )
