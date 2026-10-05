"""Tests for tle.kcpc.features.algo.catalog: the algorithm of the month's topics."""

import re
from collections import Counter
from urllib.parse import urlsplit

import pytest

from tle.kcpc.features.algo.catalog import ALGO_TOPICS, AlgoTopic, Level, topic

SLUG_RE = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
# GeeksforGeeks moved its articles under /dsa/, and redirects the old links.
GFG_PATH_RE = re.compile(r'/dsa/[a-z0-9]+(?:-[a-z0-9]+)*/')
CP_ALGORITHMS_PATH_RE = re.compile(r'/[a-z_]+/[a-z0-9_-]+\.html')
# What a post or a reply shows of a summary, comfortably within an embed.
MAX_SUMMARY_LENGTH = 250

ids = [found.slug for found in ALGO_TOPICS]


def test_there_are_enough_topics_for_three_years() -> None:
    assert len(ALGO_TOPICS) >= 36


def test_slugs_are_unique_and_never_need_escaping() -> None:
    assert [slug for slug, count in Counter(ids).items() if count > 1] == []
    for slug in ids:
        assert SLUG_RE.fullmatch(slug), slug


def test_names_are_unique() -> None:
    names = [found.name for found in ALGO_TOPICS]

    assert [name for name, count in Counter(names).items() if count > 1] == []


@pytest.mark.parametrize('found', ALGO_TOPICS, ids=ids)
def test_each_topic_has_a_name_and_a_summary(found: AlgoTopic) -> None:
    assert found.name and found.name == found.name.strip()
    assert found.summary and found.summary == found.summary.strip()
    assert found.summary.endswith('.')
    assert len(found.summary) <= MAX_SUMMARY_LENGTH
    assert '  ' not in found.summary and '\n' not in found.summary


@pytest.mark.parametrize('found', ALGO_TOPICS, ids=ids)
def test_each_topic_links_geeksforgeeks_and_maybe_cp_algorithms(
    found: AlgoTopic,
) -> None:
    gfg = urlsplit(found.gfg_url)
    assert (gfg.scheme, gfg.netloc) == ('https', 'www.geeksforgeeks.org')
    assert GFG_PATH_RE.fullmatch(gfg.path), found.gfg_url
    assert not (gfg.query or gfg.fragment)
    if found.cp_algorithms_url is not None:
        cp = urlsplit(found.cp_algorithms_url)
        assert (cp.scheme, cp.netloc) == ('https', 'cp-algorithms.com')
        assert CP_ALGORITHMS_PATH_RE.fullmatch(cp.path), found.cp_algorithms_url
        assert not cp.query


def test_each_topic_has_its_own_geeksforgeeks_article() -> None:
    urls = Counter(found.gfg_url for found in ALGO_TOPICS)

    assert [url for url, count in urls.items() if count > 1] == []


def test_every_level_has_topics() -> None:
    levels = Counter(found.level for found in ALGO_TOPICS)

    assert all(isinstance(found.level, Level) for found in ALGO_TOPICS)
    assert set(levels) == set(Level)
    assert [level.value for level in Level] == ['beginner', 'intermediate', 'advanced']
    assert min(levels.values()) >= 5


def test_a_topic_is_found_by_its_slug() -> None:
    segment_tree = topic('segment-tree')

    assert segment_tree is not None
    assert segment_tree.name == 'Segment tree'
    assert segment_tree.level is Level.INTERMEDIATE
    assert segment_tree.gfg_url == (
        'https://www.geeksforgeeks.org/dsa/segment-tree-data-structure/'
    )
    assert segment_tree.cp_algorithms_url == (
        'https://cp-algorithms.com/data_structures/segment_tree.html'
    )
    assert all(topic(found.slug) is found for found in ALGO_TOPICS)
    assert topic('Segment-Tree') is None
    assert topic('no-such-topic') is None
