"""Tests for tle.kcpc.core.messages: JSON round trips and Discord's limits."""

import json
from typing import Any

import pytest

from tle.kcpc.core.messages import (
    COLOR_LIMIT,
    CONTENT_LIMIT,
    DESCRIPTION_LIMIT,
    EMBED_TOTAL_LIMIT,
    FIELD_COUNT_LIMIT,
    FIELD_NAME_LIMIT,
    FIELD_VALUE_LIMIT,
    FOOTER_LIMIT,
    REF_MARKER_RESERVE,
    ROLE_MENTION_RESERVE,
    TITLE_LIMIT,
    URL_LIMIT,
    ZERO_WIDTH_SPACE,
    EmbedField,
    OutgoingMessage,
)

WORKSHOP = OutgoingMessage(
    title='Workshop: Graphs 101',
    description='Bring a laptop. Café au lait provided ☕',
    url='https://luma.com/graphs-101',
    color=0x1E88E5,
    fields=(
        EmbedField('When', '<t:1792000000:F>', inline=True),
        EmbedField('Where', 'Bush House'),
    ),
    footer='KCPC',
    content='See you there!',
    mention_role=False,
)

# What the embed may use once the publisher has appended its ref marker.
EMBED_BUDGET = EMBED_TOTAL_LIMIT - REF_MARKER_RESERVE


def embed_length(message: OutgoingMessage) -> int:
    """The characters Discord counts towards its total embed limit."""
    texts = [message.title, message.description, message.footer]
    texts += [text for field in message.fields for text in (field.name, field.value)]
    return sum(len(text) for text in texts if text is not None)


def test_json_round_trip() -> None:
    assert OutgoingMessage.from_json(WORKSHOP.to_json()) == WORKSHOP
    assert OutgoingMessage.from_json(OutgoingMessage().to_json()) == OutgoingMessage()


def test_to_json_is_stable_readable_json() -> None:
    raw = WORKSHOP.to_json()
    data = json.loads(raw)
    assert list(data) == sorted(data)
    assert data['fields'][0] == {
        'inline': True,
        'name': 'When',
        'value': '<t:1792000000:F>',
    }
    assert 'Café' in raw


def test_from_json_tolerates_missing_and_unknown_keys() -> None:
    assert OutgoingMessage.from_json('{}') == OutgoingMessage()
    raw = json.dumps(
        {'title': 'T', 'added_later': 1, 'fields': [{'name': 'N', 'extra': True}]}
    )
    assert OutgoingMessage.from_json(raw) == OutgoingMessage(
        title='T', fields=(EmbedField('N', ''),)
    )


@pytest.mark.parametrize(
    'raw',
    [
        'not json',
        '[]',
        '"text"',
        '{"title": 5}',
        '{"color": true}',
        '{"color": "blue"}',
        '{"mention_role": "yes"}',
        '{"fields": {}}',
        '{"fields": ["field"]}',
        '{"fields": [{"name": 1, "value": "v"}]}',
        '{"fields": [{"name": "n", "value": "v", "inline": 1}]}',
    ],
)
def test_from_json_rejects_malformed_payloads(raw: str) -> None:
    with pytest.raises(ValueError):
        OutgoingMessage.from_json(raw)


def test_message_within_the_limits_is_unchanged() -> None:
    assert WORKSHOP.within_discord_limits() == WORKSHOP


@pytest.mark.parametrize(
    ('attribute', 'limit'),
    [
        ('title', TITLE_LIMIT),
        ('description', DESCRIPTION_LIMIT),
        ('footer', FOOTER_LIMIT - REF_MARKER_RESERVE),
    ],
)
def test_text_is_cut_to_its_limit_with_an_ellipsis(attribute: str, limit: int) -> None:
    at_limit: dict[str, Any] = {attribute: 'x' * limit}
    assert OutgoingMessage(**at_limit).within_discord_limits() == OutgoingMessage(
        **at_limit
    )

    over_limit: dict[str, Any] = {attribute: 'x' * (limit + 1)}
    fitted = OutgoingMessage(**over_limit).within_discord_limits()
    assert getattr(fitted, attribute) == 'x' * (limit - 1) + '…'


def test_no_space_is_left_before_the_ellipsis() -> None:
    fitted = OutgoingMessage(title='word ' * 100).within_discord_limits()
    assert fitted.title is not None
    assert fitted.title.endswith('word…')


def test_content_leaves_room_for_a_role_mention() -> None:
    text = 'c' * (CONTENT_LIMIT + 10)
    mentioned = OutgoingMessage(content=text).within_discord_limits().content
    assert mentioned is not None
    assert len(mentioned) == CONTENT_LIMIT - ROLE_MENTION_RESERVE
    largest_mention = '<@&' + '9' * 20 + '>'
    assert len(f'{largest_mention} {mentioned}') <= CONTENT_LIMIT

    plain = OutgoingMessage(content=text, mention_role=False).within_discord_limits()
    assert plain.content is not None
    assert len(plain.content) == CONTENT_LIMIT


def test_fields_are_capped_and_shortened() -> None:
    fields = tuple(EmbedField(f'n{i}', f'v{i}') for i in range(FIELD_COUNT_LIMIT + 5))
    assert (
        OutgoingMessage(fields=fields).within_discord_limits().fields
        == (fields[:FIELD_COUNT_LIMIT])
    )

    long_field = EmbedField(
        'n' * (FIELD_NAME_LIMIT + 1), 'v' * (FIELD_VALUE_LIMIT + 1), inline=True
    )
    (fitted,) = OutgoingMessage(fields=(long_field,)).within_discord_limits().fields
    assert fitted == EmbedField(
        'n' * (FIELD_NAME_LIMIT - 1) + '…', 'v' * (FIELD_VALUE_LIMIT - 1) + '…', True
    )


def test_blank_text_is_dropped_or_replaced() -> None:
    message = OutgoingMessage(
        title='',
        description='   ',
        footer='\n',
        content='',
        fields=(EmbedField('', ' '),),
    )
    fitted = message.within_discord_limits()
    assert (fitted.title, fitted.description, fitted.footer, fitted.content) == (
        None,
        None,
        None,
        None,
    )
    assert fitted.fields == (EmbedField(ZERO_WIDTH_SPACE, ZERO_WIDTH_SPACE),)


def test_description_is_shortened_to_fit_the_embed_total() -> None:
    message = OutgoingMessage(
        title='t' * TITLE_LIMIT,
        description='d' * DESCRIPTION_LIMIT,
        fields=(EmbedField('n', 'v' * FIELD_VALUE_LIMIT),),
        footer='f' * 2000,
    )
    fitted = message.within_discord_limits()
    assert embed_length(fitted) == EMBED_BUDGET
    assert fitted.description is not None
    assert fitted.description.endswith('…')
    assert (fitted.title, fitted.fields, fitted.footer) == (
        message.title,
        message.fields,
        message.footer,
    )


def test_fields_are_dropped_when_the_description_cannot_make_up_the_excess() -> None:
    fields = tuple(EmbedField(f'field {i}', 'v' * FIELD_VALUE_LIMIT) for i in range(10))
    fitted = OutgoingMessage(description='short', fields=fields).within_discord_limits()
    assert fitted.description == 'short'  # not cut for nothing
    assert fitted.fields == fields[:5]
    assert embed_length(fitted) <= EMBED_BUDGET


@pytest.mark.parametrize(
    'url',
    [
        'https://luma.com/graphs-101',
        'http://example.com',
        'HTTPS://Luma.com/x',
        'https://codeforces.com/contest/1/problem/A?locale=en#x',
        'https://en.wikipedia.org/wiki/Café',
        'https://lu.ma/' + 'a' * (URL_LIMIT - len('https://lu.ma/')),
    ],
)
def test_a_url_discord_accepts_is_kept(url: str) -> None:
    assert OutgoingMessage(url=url).within_discord_limits().url == url


def test_a_url_is_stripped_of_surrounding_whitespace() -> None:
    message = OutgoingMessage(url=' https://luma.com/graphs-101\r\n')

    assert message.within_discord_limits().url == 'https://luma.com/graphs-101'


@pytest.mark.parametrize(
    'url',
    [
        '',
        '  ',
        'lu.ma/graphs-101',  # no scheme
        'ftp://example.com/x',
        'javascript:alert(1)',
        'https://',
        'https://lu.ma/graphs 101',
        f'https://lu.ma/{ZERO_WIDTH_SPACE}graphs',
        'https://[::1',  # unclosed IPv6 bracket
        'https://lu.ma/' + 'a' * URL_LIMIT,
    ],
)
def test_a_url_discord_would_reject_is_dropped(url: str) -> None:
    # Discord refuses the whole post over a bad link, and a refused post is
    # skipped for good; it is better sent without the link.
    assert OutgoingMessage(url=url).within_discord_limits().url is None


@pytest.mark.parametrize(
    ('color', 'fitted'),
    [
        (0, 0),
        (COLOR_LIMIT, COLOR_LIMIT),
        (-1, None),
        (COLOR_LIMIT + 1, None),
        (True, None),
    ],
)
def test_a_colour_outside_24_bits_is_dropped(color: int, fitted: int | None) -> None:
    assert OutgoingMessage(color=color).within_discord_limits().color == fitted


def test_fields_are_dropped_and_the_description_shortened_if_both_are_needed() -> None:
    fields = tuple(EmbedField(f'field {i}', 'v' * FIELD_VALUE_LIMIT) for i in range(6))
    fitted = OutgoingMessage(
        description='d' * 1000, fields=fields
    ).within_discord_limits()
    assert fitted.fields == fields[:5]
    assert fitted.description is not None
    assert fitted.description.endswith('…')
    assert embed_length(fitted) == EMBED_BUDGET
