"""AtCoder's upcoming contests, read from its contest list.

AtCoder publishes its schedule only as a web page, ``CONTESTS_URL``, with no
API or feed. The page's "Upcoming Contests" table, ``#contest-table-upcoming``,
has a row per contest with four cells:

- the start, in Japan time: '2026-10-03 21:00:00+0900';
- an icon whose tooltip names the kind of contest ('Algorithm' or
  'Heuristic'), then the name, linking to the contest's page
  ('/contests/abc478', which ends in the contest's ID);
- the duration in hours and minutes: '01:40', or '240:00' for ten days;
- the rated range: ' - 1999', '1200 - 2799', 'All' or '-'.

The page's other contest tables share the ID prefix 'contest-table-'. A contest
leaves the upcoming table for the ongoing one when it starts, so callers must
not take a contest that has started and gone missing for a cancelled one.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.timeutil import ensure_utc

logger = logging.getLogger(__name__)

PLATFORM = 'atcoder'
CONTESTS_URL = 'https://atcoder.jp/contests/'

_SERVICE = 'AtCoder'
_CONTEST_PAGE = 'https://atcoder.jp/contests/{}'
_ATCODER_HOSTS = ('atcoder.jp', 'www.atcoder.jp')

_TABLE_ID_PREFIX = 'contest-table-'
_UPCOMING_TABLE_ID = 'contest-table-upcoming'
_COLUMNS = 4  # start, name, duration, rated range

# re.ASCII, so that \d and \w match no digits or letters from other scripts.
_START_TIME = re.compile(
    r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:?\d{2}', re.ASCII
)
_START_TIME_FORMAT = '%Y-%m-%d %H:%M:%S%z'
_DURATION = re.compile(r'(\d{1,4}):([0-5]\d)', re.ASCII)  # hours:minutes
_CONTEST_PATH = re.compile(r'/contests/([\w-]+)/?', re.ASCII)
# HTML's whitespace characters; a browser shows each run of them as one space.
_HTML_WHITESPACE = re.compile(r'[ \t\n\r\f]+')


@dataclass(frozen=True)
class AtCoderContest:
    """A contest in AtCoder's upcoming table.

    ``start`` and ``end`` are aware UTC datetimes in whole seconds (others are
    converted), and ``end`` is after ``start``.
    """

    contest_id: str  # e.g. 'abc478', from the link to its page
    name: str
    start: datetime
    end: datetime
    url: str  # https://atcoder.jp/contests/<contest_id>
    kind: str  # 'Algorithm' or 'Heuristic', from the icon's tooltip; '' if none
    rated_range: str  # as shown, trimmed: '- 1999', '1200 - 2799', 'All', '-'

    def __post_init__(self) -> None:
        # Converted here too, so that contests built by hand (in tests, say)
        # compare exactly like parsed ones.
        start = ensure_utc(self.start).replace(microsecond=0)
        end = ensure_utc(self.end).replace(microsecond=0)
        if end <= start:
            raise ValueError(
                f'AtCoder contest {self.contest_id} must end after it starts'
            )
        object.__setattr__(self, 'start', start)
        object.__setattr__(self, 'end', end)


def parse_upcoming(html: str) -> list[AtCoderContest]:
    """The contests in the upcoming table of AtCoder's contest list, in order.

    A row that can't be read is skipped, and so is a second row for a contest,
    both logged at DEBUG. A page with AtCoder's other contest tables but no
    upcoming table lists no upcoming contests. Raises ``ExternalServiceError``
    if ``html`` isn't AtCoder's contest list, or if its upcoming table has rows
    but none can be read: the layout has changed then, and an empty list would
    make every AtCoder contest look cancelled.
    """
    parser = _ContestListParser()
    parser.feed(html)
    parser.close()
    if _UPCOMING_TABLE_ID not in parser.table_ids:
        if parser.table_ids:
            return []
        raise _unreadable_list()
    contests: list[AtCoderContest] = []
    seen: set[str] = set()
    for cells in parser.rows:
        contest = _contest_from(cells)
        if contest is None:
            continue
        if contest.contest_id in seen:
            logger.debug(
                'Skipping a second row for AtCoder contest %s', contest.contest_id
            )
            continue
        seen.add(contest.contest_id)
        contests.append(contest)
    if parser.rows and not contests:
        raise _unreadable_list()
    return contests


class AtCoderContestsClient:
    """Fetches AtCoder's contest list through the shared ``HttpClient``."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch_upcoming(self) -> list[AtCoderContest]:
        """The contests in AtCoder's upcoming table, as ``parse_upcoming`` reads it.

        Raises ``ExternalServiceError`` if AtCoder can't be reached or sends a
        page that can't be read as its contest list.
        """
        html = await self._http.get_text(
            CONTESTS_URL, params={'lang': 'en'}, service=_SERVICE
        )
        # On the event loop: the page takes about 20 ms to parse, too little
        # to be worth a worker thread.
        return parse_upcoming(html)


@dataclass
class _Link:
    href: str
    parts: list[str] = field(default_factory=list)  # its text, in pieces

    @property
    def text(self) -> str:
        return _visible(''.join(self.parts))


@dataclass
class _Cell:
    """What the parser keeps of a cell in the upcoming table."""

    parts: list[str] = field(default_factory=list)  # its text, in pieces
    links: list[_Link] = field(default_factory=list)
    title: str | None = None  # the first title attribute inside it

    @property
    def text(self) -> str:
        return _visible(''.join(self.parts))


class _ContestListParser(HTMLParser):
    """Collects the cells of the upcoming table and the IDs of all contest tables.

    The IDs tell AtCoder's contest list without upcoming contests from a page
    that isn't the contest list at all.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_ids: set[str] = set()
        self.rows: list[list[_Cell]] = []
        # Inside the element with the upcoming table's ID: its tag, and how
        # many elements with that tag are open (it included), so that its own
        # end tag is the one that ends it.
        self._section_tag: str | None = None
        self._section_depth = 0
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._link: _Link | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = _attribute(attrs, 'id')
        if element_id is not None and element_id.startswith(_TABLE_ID_PREFIX):
            self.table_ids.add(element_id)
            if element_id == _UPCOMING_TABLE_ID and self._section_tag is None:
                self._section_tag = tag
        if self._section_tag is None:
            return
        if tag == self._section_tag:
            self._section_depth += 1
        if tag == 'tr':
            self._end_row()
            self._row = []
        elif tag == 'td' and self._row is not None:
            self._cell = _Cell()
            self._link = None
            self._row.append(self._cell)
        elif tag == 'a' and self._cell is not None:
            self._link = _Link(_attribute(attrs, 'href') or '')
            self._cell.links.append(self._link)
        if self._cell is not None and self._cell.title is None:
            self._cell.title = _attribute(attrs, 'title')

    def handle_endtag(self, tag: str) -> None:
        if self._section_tag is None:
            return
        if tag == 'a':
            self._link = None
        elif tag == 'td':
            self._cell = None
            self._link = None
        elif tag == 'tr':
            self._end_row()
        if tag == self._section_tag:
            self._section_depth -= 1
            if self._section_depth == 0:
                self._end_row()
                self._section_tag = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.parts.append(data)
        if self._link is not None:
            self._link.parts.append(data)

    def close(self) -> None:
        super().close()
        self._end_row()  # in case the page ended inside the table

    def _end_row(self) -> None:
        if self._row:  # rows without data cells are headings
            self.rows.append(self._row)
        self._row = None
        self._cell = None
        self._link = None


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    """The value of attribute ``name``: the first, as browsers read repeats."""
    return next((value for key, value in attrs if key == name), None)


def _contest_from(cells: list[_Cell]) -> AtCoderContest | None:
    """The contest in a row of the upcoming table, or None (logged) if unreadable."""
    if len(cells) < _COLUMNS:
        logger.debug(
            "Skipping a row of AtCoder's upcoming contests with %d cell(s)",
            len(cells),
        )
        return None
    start_cell, name_cell, duration_cell, range_cell = cells[:_COLUMNS]
    link = _contest_link(name_cell)
    if link is None:
        logger.debug(
            "Skipping a row of AtCoder's upcoming contests without a contest link: %r",
            name_cell.text,
        )
        return None
    contest_id, name = link
    start = _start_time(start_cell.text)
    if start is None:
        logger.debug(
            'Skipping AtCoder contest %s: its start time %r could not be read',
            contest_id,
            start_cell.text,
        )
        return None
    end = _end_time(start, duration_cell.text)
    if end is None:
        logger.debug(
            'Skipping AtCoder contest %s: its duration %r could not be read',
            contest_id,
            duration_cell.text,
        )
        return None
    return AtCoderContest(
        contest_id=contest_id,
        name=name or contest_id,
        start=start,
        end=end,
        url=_CONTEST_PAGE.format(contest_id),
        kind=_visible(name_cell.title or ''),
        rated_range=range_cell.text,
    )


def _contest_link(cell: _Cell) -> tuple[str, str] | None:
    """The contest ID and text of the first link in ``cell`` to a contest."""
    for link in cell.links:
        contest_id = _contest_id_in(link.href)
        if contest_id is not None:
            return contest_id, link.text
    return None


def _contest_id_in(href: str) -> str | None:
    """The contest ID in a link to a contest's page on AtCoder, else None.

    The page links contests by path ('/contests/abc478'); a full URL on
    atcoder.jp reads the same.
    """
    try:
        parts = urlsplit(href.strip())
    except ValueError:  # e.g. an unclosed [ in the host
        return None
    if parts.netloc and parts.hostname not in _ATCODER_HOSTS:
        return None
    match = _CONTEST_PATH.fullmatch(parts.path)
    return None if match is None else match.group(1)


def _start_time(text: str) -> datetime | None:
    """The start time in a cell's text, in UTC; None if it has none to read."""
    match = _START_TIME.search(text)
    if match is None:
        return None
    try:
        return ensure_utc(datetime.strptime(match.group(), _START_TIME_FORMAT))
    # A date that doesn't exist, or one that leaves datetime's range in UTC.
    except (OverflowError, ValueError):
        return None


def _end_time(start: datetime, duration: str) -> datetime | None:
    """``start`` plus a duration of 'hours:minutes', or None if it isn't one.

    The hours go past 24 for long contests: '240:00' is ten days.
    """
    match = _DURATION.fullmatch(duration)
    if match is None:
        return None
    length = timedelta(hours=int(match[1]), minutes=int(match[2]))
    if length == timedelta(0):
        return None
    try:
        return start + length
    except OverflowError:  # past the year 9999
        return None


def _visible(text: str) -> str:
    """``text`` as a browser shows it: whitespace runs as one space, trimmed."""
    return _HTML_WHITESPACE.sub(' ', text).strip(' ')


def _unreadable_list() -> ExternalServiceError:
    return ExternalServiceError(_SERVICE, "AtCoder's contest list could not be read.")
