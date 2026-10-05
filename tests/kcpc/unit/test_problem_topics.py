"""Tests for tle.kcpc.features.problems.topics: what problems are about."""

import pytest

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.features.problems.topics import (
    ANY,
    CURATED,
    KNOWN_TAGS,
    Topic,
    resolve_topic,
    topic_choices,
)

GRAPHS = CURATED['graphs']


class TestTags:
    def test_the_known_tags_are_codeforces_tags_but_special(self) -> None:
        assert len(KNOWN_TAGS) == 37
        assert '*special' not in KNOWN_TAGS
        assert {'dp', 'communication', 'graph matchings', '2-sat'} <= KNOWN_TAGS

    def test_curated_topics_list_known_tags_under_keys_of_their_own(self) -> None:
        assert list(CURATED) == [
            'graphs',
            'math',
            'number-theory',
            'strings',
            'data-structures',
            'searching',
            'brute-force',
            'constructive',
        ]
        for tags in CURATED.values():
            assert tags <= KNOWN_TAGS
        assert GRAPHS == {
            'graphs',
            'dfs and similar',
            'shortest paths',
            'dsu',
            'trees',
            'flows',
            'graph matchings',
            '2-sat',
        }


class TestMatches:
    def test_any_matches_every_problem_even_one_without_tags(self) -> None:
        assert Topic(ANY, None).matches(['dp'])
        assert Topic(ANY, None).matches(())

    def test_a_topic_matches_a_problem_with_one_of_its_tags(self) -> None:
        topic = Topic('graphs', GRAPHS)

        assert topic.matches(['greedy', 'trees'])
        assert not topic.matches(['greedy', 'dp'])
        assert not topic.matches(())

    def test_tags_match_exactly_unlike_tles_substrings(self) -> None:
        # TLE's ;gimme graph matches 'graphs' and 'graph matchings'.
        assert not Topic('graph', frozenset({'graph'})).matches(['graphs'])
        assert not Topic('graphs', frozenset({'graphs'})).matches(['graph matchings'])


class TestResolveTopic:
    @pytest.mark.parametrize('text', ['any', 'ANY', '  Any  '])
    def test_any_whatever_its_case(self, text: str) -> None:
        assert resolve_topic(text, KNOWN_TAGS) == Topic(ANY, None)

    def test_a_curated_key_wins_over_the_tag_of_its_name(self) -> None:
        assert resolve_topic('Graphs', KNOWN_TAGS) == Topic('graphs', GRAPHS)
        assert resolve_topic('math', KNOWN_TAGS) == Topic('math', CURATED['math'])

    def test_a_tag_is_a_topic_of_its_own(self) -> None:
        assert resolve_topic('Number   Theory', KNOWN_TAGS) == Topic(
            'number theory', frozenset({'number theory'})
        )
        assert resolve_topic('number-theory', KNOWN_TAGS) == Topic(
            'number-theory', frozenset({'number theory', 'chinese remainder theorem'})
        )
        assert resolve_topic(' DP ', KNOWN_TAGS) == Topic('dp', frozenset({'dp'}))

    def test_a_tag_that_codeforces_added_since_works_once_listed(self) -> None:
        with pytest.raises(KcpcUserError):
            resolve_topic('quantum', KNOWN_TAGS)

        topic = resolve_topic('Quantum', KNOWN_TAGS | {'quantum'})

        assert topic == Topic('quantum', frozenset({'quantum'}))

    def test_an_unknown_topic_suggests_the_closest(self) -> None:
        with pytest.raises(KcpcUserError) as raised:
            resolve_topic('Search', KNOWN_TAGS)

        assert str(raised.value) == (
            "There's no topic called 'search'. Did you mean searching, binary search "
            'or ternary search?'
        )

    @pytest.mark.parametrize(
        ('text', 'suggestion'),
        [('two-pointers', 'two pointers'), ('graph', 'graphs')],
    )
    def test_one_suggestion_is_named_alone(self, text: str, suggestion: str) -> None:
        # 'graphs' is a curated key and a tag, and suggested once.
        with pytest.raises(KcpcUserError) as raised:
            resolve_topic(text, KNOWN_TAGS)

        assert str(raised.value) == (
            f"There's no topic called '{text}'. Did you mean {suggestion}?"
        )

    def test_a_topic_like_none_lists_what_topics_are(self) -> None:
        with pytest.raises(KcpcUserError) as raised:
            resolve_topic('xyzzy', KNOWN_TAGS)

        assert str(raised.value) == (
            "There's no topic called 'xyzzy'. A topic is any, a Codeforces tag such "
            'as dp or greedy, or one of: graphs, math, number-theory, strings, '
            'data-structures, searching, brute-force, constructive.'
        )

    def test_the_special_tag_is_no_topic(self) -> None:
        with pytest.raises(KcpcUserError, match="no topic called '\\\\\\*special'"):
            resolve_topic('*special', KNOWN_TAGS)

    def test_an_unknown_topic_is_repeated_with_its_markdown_escaped(self) -> None:
        # Else it could be a masked link in a public reply.
        with pytest.raises(KcpcUserError) as raised:
            resolve_topic(
                '[Free  Nitro](https://example.com/gift) ||x|| _y_', KNOWN_TAGS
            )

        assert str(raised.value).startswith(
            "There's no topic called '\\[free nitro\\](https://example.com/gift) "
            "\\|\\|x\\|\\| \\_y\\_'."
        )

    def test_a_long_unknown_topic_is_shortened_in_the_error(self) -> None:
        with pytest.raises(KcpcUserError) as raised:
            resolve_topic('q' * 100, KNOWN_TAGS)

        assert f"'{'q' * 63}…'" in str(raised.value)


class TestTopicChoices:
    def test_any_then_curated_topics_then_tags_at_most_25(self) -> None:
        choices = topic_choices('', KNOWN_TAGS)

        assert len(choices) == 25
        assert choices[0] == ('any topic', ANY)
        assert [value for _, value in choices[1:9]] == list(CURATED)
        assert choices[1] == (
            'graphs (2-sat, dfs and similar, dsu, flows, graph matchings, graphs, '
            'shortest paths, trees)',
            'graphs',
        )
        assert [value for _, value in choices[9:12]] == [
            '2-sat',
            'binary search',
            'bitmasks',
        ]

    def test_suggestions_have_the_typed_text_in_their_labels(self) -> None:
        choices = topic_choices('DSU', KNOWN_TAGS)

        assert choices == [
            (
                'graphs (2-sat, dfs and similar, dsu, flows, graph matchings, graphs, '
                'shortest paths, trees)',
                'graphs',
            ),
            ('data-structures (data structures, dsu)', 'data-structures'),
            ('dsu', 'dsu'),
        ]

    def test_a_tag_with_a_curated_key_of_its_name_is_listed_once(self) -> None:
        assert topic_choices('graph', KNOWN_TAGS) == [
            (
                'graphs (2-sat, dfs and similar, dsu, flows, graph matchings, graphs, '
                'shortest paths, trees)',
                'graphs',
            ),
            ('graph matchings', 'graph matchings'),
        ]

    def test_new_tags_are_suggested_too(self) -> None:
        assert topic_choices('quant', KNOWN_TAGS | {'quantum'}) == [
            ('quantum', 'quantum')
        ]

    def test_suggestions_fit_discord(self) -> None:
        # Discord takes names and values of up to 100 characters.
        too_long = 'a' * 101

        choices = topic_choices('', {too_long, 'a' * 100})

        assert [value for _, value in choices[9:]] == ['a' * 100]
        assert all(len(label) <= 100 for label, _ in topic_choices('', KNOWN_TAGS))

    def test_nothing_matches_nothing(self) -> None:
        assert topic_choices('xyzzy', KNOWN_TAGS) == []
