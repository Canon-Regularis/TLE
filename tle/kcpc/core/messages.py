"""Discord-free descriptions of automatic posts.

Features describe a post as an ``OutgoingMessage``; ``tle.kcpc.bot`` renders it
as an embed. The delivery ledger stores it as JSON, so that a post interrupted by
a crash can be re-sent exactly as it was meant to be.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from urllib.parse import urlsplit

# Discord's limits, in characters.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FIELD_COUNT_LIMIT = 25
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
FOOTER_LIMIT = 2048
CONTENT_LIMIT = 2000
EMBED_TOTAL_LIMIT = 6000  # title, description, field names and values, and footer
URL_LIMIT = 2048  # a conservative cap on an embed's link
COLOR_LIMIT = 0xFFFFFF  # embed colours are 24-bit RGB

# Room kept for what the publisher adds: ' · ref <batch>' after the footer, and
# a role mention ('<@&' + up to 20 digits + '>') and a space before the content.
REF_MARKER_RESERVE = 20
ROLE_MENTION_RESERVE = 25

ELLIPSIS = '…'
ZERO_WIDTH_SPACE = '\u200b'


@dataclass(frozen=True)
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True)
class OutgoingMessage:
    """A Discord-free description of an automatic post (rendered by tle.kcpc.bot).

    ``footer`` gets ' · ref <batch>' appended by the publisher, and ``content`` is
    plain text sent after the role mention, if the feature has a role and
    ``mention_role`` is set.
    """

    title: str | None = None
    description: str | None = None
    url: str | None = None
    color: int | None = None
    fields: tuple[EmbedField, ...] = ()
    footer: str | None = None
    content: str | None = None
    mention_role: bool = True

    def within_discord_limits(self) -> 'OutgoingMessage':
        """A copy that Discord will accept, shortening over-long text with '…'.

        Blank optional text becomes None, and blank field text a zero-width space
        (Discord rejects empty fields). Room is kept for the publisher's ref
        marker and role mention. If the embed is still over Discord's total
        limit, trailing fields are dropped and the description shortened.

        The url is stripped of surrounding whitespace, and dropped unless it is
        an absolute http(s) URL of at most ``URL_LIMIT`` characters with no
        whitespace or invisible characters inside: the post then goes out
        without a link. A colour outside 0 to ``COLOR_LIMIT`` is dropped, so the
        renderer's default applies.
        """
        content_limit = CONTENT_LIMIT
        if self.mention_role:
            content_limit -= ROLE_MENTION_RESERVE
        fitted = replace(
            self,
            title=_shorten(self.title, TITLE_LIMIT),
            description=_shorten(self.description, DESCRIPTION_LIMIT),
            url=_fit_url(self.url),
            color=_fit_color(self.color),
            fields=tuple(
                _fit_field(field) for field in self.fields[:FIELD_COUNT_LIMIT]
            ),
            footer=_shorten(self.footer, FOOTER_LIMIT - REF_MARKER_RESERVE),
            content=_shorten(self.content, content_limit),
        )
        return _fit_embed_total(fitted)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> 'OutgoingMessage':
        """Parse ``to_json`` output.

        Missing keys take their defaults and unknown keys are ignored, so stored
        payloads survive changes to this class. Anything else that is malformed
        raises ``ValueError``.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('An OutgoingMessage must be a JSON object')
        return cls(
            title=_optional_str(data, 'title'),
            description=_optional_str(data, 'description'),
            url=_optional_str(data, 'url'),
            color=_optional_int(data, 'color'),
            fields=_fields(data),
            footer=_optional_str(data, 'footer'),
            content=_optional_str(data, 'content'),
            mention_role=_bool(data, 'mention_role', default=True),
        )


def _shorten(text: str | None, limit: int) -> str | None:
    """``text`` cut to ``limit`` characters ending in '…'; None if it is blank."""
    if text is None or not text.strip():
        return None
    if len(text) <= limit:
        return text
    if limit < 1:
        return None
    return text[: limit - 1].rstrip() + ELLIPSIS


def _fit_url(url: str | None) -> str | None:
    """``url`` stripped, if Discord accepts it as an embed link; else None."""
    if url is None:
        return None
    url = url.strip()
    if not url or len(url) > URL_LIMIT:
        return None
    if any(char.isspace() or not char.isprintable() for char in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. an unclosed IPv6 bracket
        return None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return None
    return url


def _fit_color(color: int | None) -> int | None:
    """``color`` if it is a 24-bit RGB value, else None."""
    # bool is a subclass of int, but true/false is never a valid colour.
    if color is None or isinstance(color, bool) or not 0 <= color <= COLOR_LIMIT:
        return None
    return color


def _fit_field(field: EmbedField) -> EmbedField:
    return replace(
        field,
        name=_shorten(field.name, FIELD_NAME_LIMIT) or ZERO_WIDTH_SPACE,
        value=_shorten(field.value, FIELD_VALUE_LIMIT) or ZERO_WIDTH_SPACE,
    )


def _embed_length(message: OutgoingMessage) -> int:
    """The characters Discord counts towards ``EMBED_TOTAL_LIMIT``."""
    texts = (message.title, message.description, message.footer)
    fields = sum(len(field.name) + len(field.value) for field in message.fields)
    return sum(len(text) for text in texts if text is not None) + fields


def _fit_embed_total(message: OutgoingMessage) -> OutgoingMessage:
    """Fit the embed's total length by dropping fields, then shortening the text.

    Trailing fields are dropped only until shortening the description can make
    up the rest, so the description is never cut for nothing. The title and
    footer are left alone: within their own limits they fit the total anyway.
    """
    excess = _embed_length(message) - (EMBED_TOTAL_LIMIT - REF_MARKER_RESERVE)
    if excess <= 0:
        return message
    description = message.description or ''
    fields = list(message.fields)
    while fields and excess > len(description):
        dropped = fields.pop()
        excess -= len(dropped.name) + len(dropped.value)
    if excess > 0:
        return replace(
            message,
            description=_shorten(description, len(description) - excess),
            fields=tuple(fields),
        )
    return replace(message, fields=tuple(fields))


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _wrong_type(key, 'a string or null', value)


def _str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key, '')
    if isinstance(value, str):
        return value
    raise _wrong_type(key, 'a string', value)


def _optional_int(data: Mapping[str, object], key: str) -> int | None:
    value = data.get(key)
    # bool is a subclass of int, but true/false is never a valid colour.
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise _wrong_type(key, 'an integer or null', value)


def _bool(data: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    raise _wrong_type(key, 'true or false', value)


def _fields(data: Mapping[str, object]) -> tuple[EmbedField, ...]:
    items = data.get('fields', [])
    if not isinstance(items, list):
        raise _wrong_type('fields', 'a list', items)
    return tuple(_field(item) for item in items)


def _field(item: object) -> EmbedField:
    if not isinstance(item, dict):
        raise _wrong_type('fields[]', 'an object', item)
    return EmbedField(
        name=_str(item, 'name'),
        value=_str(item, 'value'),
        inline=_bool(item, 'inline', default=False),
    )


def _wrong_type(key: str, expected: str, value: object) -> ValueError:
    return ValueError(f'{key!r} must be {expected}, not {type(value).__name__}')
