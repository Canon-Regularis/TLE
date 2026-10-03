"""AtCoder users' profiles, read from their public profile pages.

AtCoder has no API for profiles, so ``AtCoderProfileClient`` reads the page at
``PROFILE_URL`` in English. An unknown user's page is a 404, and a user is
found whatever the case of the name in the URL. The page names the user in a
link with class 'username', whose text keeps the name's own case and whose
span is coloured by rating with a class such as 'user-blue' ('user-unrated'
for a user never rated). The rest is in table rows of a label and a value,
``<tr><th>Label</th><td>value</td></tr>``:

- about the user: Country/Region, Birth Year, X(Twitter) ID, TopCoder ID,
  Codeforces ID and Affiliation, each left out unless the user filled it in;
- their algorithm contests: Rank, Rating ('1834'), Highest Rating ('1912 ―
  2 Dan (+88 to promote)'), Rated Matches ('27') and Last Competed, all left
  out for a user never rated, whose page says "This user has not competed in a
  rated contest yet." instead.

Members prove that an account is theirs by putting a token in its Affiliation.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from http import HTTPStatus

from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient

logger = logging.getLogger(__name__)

PLATFORM = 'atcoder'
PROFILE_URL = 'https://atcoder.jp/users/{handle}'
# AtCoder usernames: 3 to 16 Latin letters, digits and underscores.
HANDLE_RE = re.compile(r'[A-Za-z0-9_]{3,16}')

_SERVICE = 'AtCoder'
_USERNAME_CLASS = 'username'
# The class that colours a name by rating: 'user-blue', or 'user-unrated'.
_COLOR_CLASS = re.compile(r'user-([a-z]+)')
# The number that starts a value: '1834', or '1912 ― 2 Dan (+88 to promote)'.
# Six digits are more than any rating or match count has, so a longer run is
# no number AtCoder would show. re.ASCII, so that \d matches no digits from
# other scripts.
_LEADING_NUMBER = re.compile(r'\d{1,6}(?!\d)', re.ASCII)
# HTML's whitespace characters; a browser shows each run of them as one space.
_HTML_WHITESPACE = re.compile(r'[ \t\n\r\f]+')

# The labels of the rows that are read, as the English page has them.
_RATING = 'Rating'
_HIGHEST_RATING = 'Highest Rating'
_RATED_MATCHES = 'Rated Matches'
_AFFILIATION = 'Affiliation'


@dataclass(frozen=True)
class AtCoderProfile:
    """What KCPC reads from an AtCoder user's profile page."""

    handle: str  # in its canonical case, from the page's username link
    rating: int | None  # None if never rated
    highest_rating: int | None  # None if never rated
    rated_matches: int  # 0 if never rated
    affiliation: str | None  # as shown: unescaped, whitespace runs as one space
    color: str | None  # by rating: 'gray' ... 'red', or 'unrated'; None if none
    url: str  # the profile page: https://atcoder.jp/users/<handle>


def parse_profile(html: str) -> AtCoderProfile:
    """The profile on an AtCoder user's profile page.

    Rows the page leaves out are fine: no Affiliation row means no affiliation,
    and no Rating, Highest Rating or Rated Matches row means never rated.
    Raises ``ExternalServiceError`` if ``html`` isn't a profile page: it has no
    username link with a valid username in it, or one of those three rows
    doesn't start with a number, which means the layout has changed.
    """
    parser = _ProfilePageParser()
    parser.feed(html)
    parser.close()
    handle = parser.username
    if handle is None:
        logger.debug("AtCoder's page has no username link")
        raise _unreadable_profile()
    if HANDLE_RE.fullmatch(handle) is None:
        logger.debug("AtCoder's page names the user %r", handle)
        raise _unreadable_profile()
    rows = parser.rows
    return AtCoderProfile(
        handle=handle,
        rating=_number(rows, _RATING),
        highest_rating=_number(rows, _HIGHEST_RATING),
        rated_matches=_number(rows, _RATED_MATCHES) or 0,
        affiliation=rows.get(_AFFILIATION) or None,
        color=parser.color,
        url=PROFILE_URL.format(handle=handle),
    )


class AtCoderProfileClient:
    """Fetches AtCoder users' profile pages through the shared ``HttpClient``."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        """``handle``'s profile, whatever its case; None if AtCoder has no such user.

        Raises ``KcpcUserError`` without asking AtCoder if ``handle`` can't be
        an AtCoder username, and ``ExternalServiceError`` if AtCoder can't be
        reached or sends a page that can't be read as a profile.
        """
        if HANDLE_RE.fullmatch(handle) is None:
            raise KcpcUserError("That isn't a valid AtCoder username.")
        response = await self._http.get(
            PROFILE_URL.format(handle=handle),
            params={'lang': 'en'},
            service=_SERVICE,
            allow_status={HTTPStatus.NOT_FOUND},
        )
        if response.status == HTTPStatus.NOT_FOUND:
            return None
        # On the event loop: a profile page takes about 3 ms to parse, too
        # little to be worth a worker thread.
        return parse_profile(response.text())


@dataclass
class _Row:
    """A table row being read: the text of its first th and first td, in pieces."""

    label: list[str] | None = None
    value: list[str] | None = None


class _ProfilePageParser(HTMLParser):
    """Reads the first username link, and the label and value of every table row.

    Rows are read wherever they are rather than in particular tables, so that
    new classes or containers for the tables leave them readable.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.username: str | None = None  # the first username link's text
        self.color: str | None = None  # its colour class, without 'user-'
        self.rows: dict[str, str] = {}  # label -> value, of the first such row
        self._link: list[str] | None = None  # inside the username link: its text
        self._row: _Row | None = None
        self._cell: list[str] | None = None  # the text of the th or td being read

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (_attribute(attrs, 'class') or '').split()
        if tag == 'a' and self.username is None and _USERNAME_CLASS in classes:
            self._link = []
        if self._link is not None and self.color is None:
            # On the link itself or on an element inside it (today a span).
            self.color = _color_in(classes)
        if tag == 'tr':
            self._end_row()
            self._row = _Row()
        elif tag in ('th', 'td'):
            self._cell = self._start_cell(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == 'a' and self._link is not None:
            self.username = _visible(''.join(self._link))
            self._link = None
        if tag in ('th', 'td'):
            self._cell = None
        elif tag in ('tr', 'table'):
            self._end_row()

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            self._link.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def close(self) -> None:
        super().close()
        self._end_row()  # in case the page ended inside a row

    def _start_cell(self, tag: str) -> list[str] | None:
        """Start reading the row's first th or first td; None for any other."""
        row = self._row
        if row is None:
            return None
        if tag == 'th' and row.label is None:
            row.label = []
            return row.label
        if tag == 'td' and row.value is None:
            row.value = []
            return row.value
        return None

    def _end_row(self) -> None:
        row, self._row, self._cell = self._row, None, None
        # Rows without both a th and a td (headings, say) aren't label and value.
        if row is not None and row.label is not None and row.value is not None:
            label = _visible(''.join(row.label))
            self.rows.setdefault(label, _visible(''.join(row.value)))


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    """The value of attribute ``name``: the first, as browsers read repeats."""
    return next((value for key, value in attrs if key == name), None)


def _color_in(classes: list[str]) -> str | None:
    """The colour that the first 'user-<colour>' class in ``classes`` names."""
    for name in classes:
        match = _COLOR_CLASS.fullmatch(name)
        if match is not None:
            return match.group(1)
    return None


def _number(rows: Mapping[str, str], label: str) -> int | None:
    """The number that starts the value of row ``label``; None without the row.

    Raises ``ExternalServiceError`` if the value doesn't start with a number.
    """
    value = rows.get(label)
    if value is None:
        return None
    match = _LEADING_NUMBER.match(value)
    if match is None:
        logger.debug("AtCoder's profile page shows %s as %r", label, value)
        raise _unreadable_profile()
    return int(match.group())


def _visible(text: str) -> str:
    """``text`` as a browser shows it: whitespace runs as one space, trimmed."""
    return _HTML_WHITESPACE.sub(' ', text).strip(' ')


def _unreadable_profile() -> ExternalServiceError:
    return ExternalServiceError(_SERVICE, "AtCoder's profile page could not be read.")
