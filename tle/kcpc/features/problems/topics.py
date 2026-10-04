"""Topics that members pick problems by: Codeforces' tags, and groups of them.

Codeforces tags each problem with what it is about, such as 'dp' or 'trees'. A
topic is ``any``, one of those tags, matched exactly, or a curated group of tags
under a key of its own: 'graphs' takes in 'trees', 'dsu' and the rest. A
curated key wins over a tag of the same name. AtCoder's problems have no tags,
so only ``any`` matches them.

The tags members can use are ``KNOWN_TAGS`` plus those of the problems in the
loaded list (``ProblemCatalog.tags``), so a tag that Codeforces adds works once
the list is refreshed.
"""

import difflib
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.messages import shorten
from tle.kcpc.features.problems import markdown

ANY = 'any'

# Codeforces' tags in 2026-10, but '*special', which marks the problems of
# contests that aren't standard rounds rather than what a problem is about.
KNOWN_TAGS = frozenset(
    {
        '2-sat',
        'binary search',
        'bitmasks',
        'brute force',
        'chinese remainder theorem',
        'combinatorics',
        'communication',
        'constructive algorithms',
        'data structures',
        'dfs and similar',
        'divide and conquer',
        'dp',
        'dsu',
        'expression parsing',
        'fft',
        'flows',
        'games',
        'geometry',
        'graph matchings',
        'graphs',
        'greedy',
        'hashing',
        'implementation',
        'interactive',
        'math',
        'matrices',
        'meet-in-the-middle',
        'number theory',
        'probabilities',
        'schedules',
        'shortest paths',
        'sortings',
        'string suffix structures',
        'strings',
        'ternary search',
        'trees',
        'two pointers',
    }
)

# Groups of tags that members are likelier to ask for than one tag alone, by
# key. Codeforces tags a problem with few of its ideas, so a tree problem is
# often tagged 'trees' or 'dfs and similar' but not 'graphs'.
CURATED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        'graphs': frozenset(
            {
                'graphs',
                'dfs and similar',
                'shortest paths',
                'dsu',
                'trees',
                'flows',
                'graph matchings',
                '2-sat',
            }
        ),
        'math': frozenset(
            {
                'math',
                'number theory',
                'combinatorics',
                'probabilities',
                'matrices',
                'chinese remainder theorem',
                'fft',
            }
        ),
        'number-theory': frozenset({'number theory', 'chinese remainder theorem'}),
        'strings': frozenset({'strings', 'string suffix structures', 'hashing'}),
        'data-structures': frozenset({'data structures', 'dsu'}),
        'searching': frozenset(
            {'binary search', 'ternary search', 'two pointers', 'meet-in-the-middle'}
        ),
        'brute-force': frozenset({'brute force', 'bitmasks', 'meet-in-the-middle'}),
        'constructive': frozenset({'constructive algorithms'}),
    }
)

# The most suggestions that Discord's autocomplete shows, and the longest name
# or value that one can have.
_MAX_CHOICES = 25
_CHOICE_NAME_LIMIT = 100
# How many topics close to an unknown one its error suggests.
_SUGGESTIONS = 3
# How much of an unknown topic its error repeats.
_SHOWN_LIMIT = 64
_ANY_LABEL = 'any topic'
_UNKNOWN_TOPIC = "There's no topic called '{topic}'."
_DID_YOU_MEAN = ' Did you mean {suggestions}?'
_TOPICS_HINT = (
    ' A topic is any, a Codeforces tag such as dp or greedy, or one of: {curated}.'
)


@dataclass(frozen=True)
class Topic:
    """What a member asked problems to be about: any, a curated group or a tag."""

    key: str  # ANY, a key of CURATED, or one exact tag
    tags: frozenset[str] | None  # the tags that match it; None for any

    def matches(self, tags: Iterable[str]) -> bool:
        """Whether a problem tagged ``tags`` is about this topic.

        Every problem is about ``any``; otherwise the problem needs one of the
        topic's tags, exactly.
        """
        if self.tags is None:
            return True
        return not self.tags.isdisjoint(tags)


def resolve_topic(text: str, known_tags: Collection[str]) -> Topic:
    """The topic that ``text`` names, whatever its case and spacing.

    That is ``any``, a curated key, or one of ``known_tags``. Raises
    ``KcpcUserError`` for anything else, suggesting the closest topics.
    """
    key = _normalized(text)
    if key == ANY:
        return Topic(ANY, None)
    curated = CURATED.get(key)
    if curated is not None:
        return Topic(key, curated)
    tags = _tags_by_key(known_tags)
    if key in tags:
        return Topic(tags[key], frozenset({tags[key]}))
    shown = shorten(key, _SHOWN_LIMIT) or text
    # A member's own text, which mustn't make a link, say; spaces are
    # collapsed, so nothing of it starts a line.
    message = _UNKNOWN_TOPIC.format(topic=markdown.escape(shown))
    keys = list(dict.fromkeys([ANY, *CURATED, *sorted(tags)]))
    close = difflib.get_close_matches(key, keys, n=_SUGGESTIONS)
    if close:
        message += _DID_YOU_MEAN.format(suggestions=_either(close))
    else:
        message += _TOPICS_HINT.format(curated=', '.join(CURATED))
    raise KcpcUserError(message)


def topic_choices(typed: str, known_tags: Collection[str]) -> list[tuple[str, str]]:
    """Autocomplete suggestions for a topic: (label, value) pairs, at most 25.

    ``any`` first, then the curated keys, each labelled with its tags, then
    the tags in alphabetical order; those whose label has ``typed`` in it,
    whatever its case. A tag too long to be a suggestion's value is left out.
    """
    needle = _normalized(typed)
    choices = [(_ANY_LABEL, ANY)]
    for key, tags in CURATED.items():
        label = f'{key} ({", ".join(sorted(tags))})'
        choices.append((shorten(label, _CHOICE_NAME_LIMIT) or key, key))
    for tag in sorted(_tags_by_key(known_tags).values()):
        # A curated key of the tag's name stands for it.
        if tag not in CURATED and len(tag) <= _CHOICE_NAME_LIMIT:
            choices.append((tag, tag))
    return [choice for choice in choices if needle in choice[0].lower()][:_MAX_CHOICES]


def _normalized(text: str) -> str:
    """``text`` as topics are compared: lower case, one space between words."""
    return ' '.join(text.split()).lower()


def _tags_by_key(known_tags: Collection[str]) -> dict[str, str]:
    """``known_tags`` by the key members type for each."""
    return {_normalized(tag): tag for tag in known_tags}


def _either(options: list[str]) -> str:
    """'a', 'a or b', or 'a, b or c'."""
    if len(options) == 1:
        return options[0]
    return f'{", ".join(options[:-1])} or {options[-1]}'
