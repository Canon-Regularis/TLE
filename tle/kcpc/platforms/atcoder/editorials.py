"""The editorials of AtCoder tasks, read from each task's editorial page.

AtCoder has no API for editorials. A task's editorial page, ``EDITORIALS_URL``,
lists the task's own editorials and the contest's overall ones, which is where
old contests keep their PDF; it is a 404 for an unknown contest or task, and
for one that isn't public yet. Under a heading, a span with class 'h2' that
links the task, come two ``div.editorial-section``: the task's, then, under
``<h3>Overall Editorial</h3>``, the contest's. Each has an ``<li>`` per
editorial:

- AtCoder's own have a label, ``<span class="label ...">Official</span>``;
  editorials by members have none.
- The first link that isn't the author's (``a.username``) is the editorial,
  and its text is the editorial's title ('Editorial', '解説' and so on). It
  links a page on AtCoder ('/contests/abc470/editorial/23885'), a PDF, or
  another site through '/jump?url=...'. A video's link holds a film icon,
  ``span.glyphicon-film``.
- Class 'lang-other' marks an editorial in another language than the page's.
  Requests send ``lang=en``: without it, the reader's Accept-Language decides
  the page's language, and so what 'lang-other' marks.

A section with no entry visible shows "There is no editorial yet."
(``p.no-editorial-msg``), and browsers hide entries in other languages, so an
English page shows it for a task whose only editorials are Japanese. The
entries are counted; the message is never read.
"""

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http import HTTPStatus
from urllib.parse import parse_qs, urljoin, urlsplit

from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient

logger = logging.getLogger(__name__)

PLATFORM = 'atcoder'
EDITORIALS_URL = 'https://atcoder.jp/contests/{contest_id}/tasks/{task_id}/editorial'
# What contest and task IDs can be. AtCoder Problems has contest IDs of up to
# 35 letters, digits, '_' and '-' ('ajo2024-final', 'DEGwer2023'), and task
# IDs of up to 31 letters, digits and '_'.
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,64}')

_SERVICE = 'AtCoder'
_ATCODER = 'https://atcoder.jp/'
_ATCODER_HOSTS = ('atcoder.jp', 'www.atcoder.jp')
_JUMP_PATH = '/jump'  # wraps links to other sites: /jump?url=<the link>
_TASK = 'task'
_OVERALL = 'overall'

# re.ASCII, so that \w and \d match no letters or digits from other scripts.
_TASK_PATH = re.compile(r'/contests/([\w-]+)/tasks/([\w-]+)/?', re.ASCII)
_EDITORIAL_PATH = re.compile(r'/contests/[\w-]+/editorial/\d+/?', re.ASCII)
# HTML's whitespace characters; a browser shows each run of them as one space.
_HTML_WHITESPACE = re.compile(r'[ \t\n\r\f]+')


@dataclass(frozen=True)
class AtCoderEditorial:
    """One editorial on a task's editorial page."""

    # Absolute http(s): '/jump' links are unwrapped, and editorial pages on
    # AtCoder are https://atcoder.jp/contests/<contest>/editorial/<n>.
    url: str
    title: str  # the link's text as shown: 'Editorial', '解説', a model's name
    official: bool  # it has AtCoder's 'Official' label
    # No 'lang-other': English, or an old entry in no language, such as a PDF.
    in_page_language: bool
    video: bool  # the link holds a film icon
    scope: str  # 'task' for the task's own, 'overall' for the contest's


@dataclass(frozen=True)
class AtCoderEditorials:
    """The editorials on a task's editorial page: the task's own, then the
    contest's, each in page order.
    """

    contest_id: str  # as asked for
    task_id: str  # as asked for
    editorials: tuple[AtCoderEditorial, ...]

    @property
    def page_url(self) -> str:
        """The task's editorial page in English, which lists them all."""
        page = EDITORIALS_URL.format(contest_id=self.contest_id, task_id=self.task_id)
        return f'{page}?lang=en'

    def best(self) -> AtCoderEditorial | None:
        """The official editorial to link; None if there is no official one.

        English before other languages, then text before video, then the
        task's own before the contest's, and otherwise the first on the page.
        Members' editorials never count: no one checks them, most are in
        Japanese and many are personal blogs. ``page_url`` lists them.
        """
        official = (editorial for editorial in self.editorials if editorial.official)
        return min(official, key=_rank, default=None)

    @property
    def has_editorial(self) -> bool:
        """Whether the task has an official editorial, its own or the contest's."""
        return self.best() is not None


def parse_task_editorials(
    html: str, contest_id: str, task_id: str
) -> AtCoderEditorials:
    """The editorials on the English editorial page of ``task_id`` in
    ``contest_id``.

    An entry without a link, or whose link isn't an absolute http(s) URL, is
    skipped (logged at DEBUG). Raises ``ExternalServiceError`` if ``html``
    isn't that page: if its Content-Language meta isn't 'en' (a page without
    one is read), if it hasn't exactly one section under a heading that links
    a task, or if that task isn't ``task_id`` in ``contest_id``. IDs are
    compared without case, as AtCoder's URLs are.
    """
    parser = _EditorialPageParser()
    parser.feed(html)
    parser.close()
    if parser.language not in (None, 'en'):
        logger.debug("AtCoder's editorial page is in %r", parser.language)
        raise _unreadable_page()
    tasks = [section for section in parser.sections if section.task is not None]
    linked = [section.task for section in tasks]
    if linked != [(contest_id.lower(), task_id.lower())]:
        logger.debug(
            "AtCoder's editorial page for %s in %s has sections for %r",
            task_id,
            contest_id,
            linked,
        )
        raise _unreadable_page()
    overall = [section for section in parser.sections if section.task is None]
    return AtCoderEditorials(
        contest_id=contest_id,
        task_id=task_id,
        editorials=tuple(
            editorial for section in tasks + overall for editorial in section.editorials
        ),
    )


class AtCoderEditorialsClient:
    """Fetches tasks' editorial pages through the shared ``HttpClient``."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch(self, contest_id: str, task_id: str) -> AtCoderEditorials | None:
        """The editorials of ``task_id`` in ``contest_id``, as
        ``parse_task_editorials`` reads them; None if AtCoder has no such task
        or doesn't show it yet.

        Give the contest that set the task, in AtCoder Problems' case: the
        page's links repeat the contest in the URL, and a contest that reused
        the task links the same editorials under its own name. Raises
        ``KcpcUserError`` without asking if an ID can't be one, and
        ``ExternalServiceError`` if AtCoder can't be reached or sends a page
        that can't be read.
        """
        if ID_RE.fullmatch(contest_id) is None or ID_RE.fullmatch(task_id) is None:
            raise KcpcUserError("That isn't a valid AtCoder problem.")
        response = await self._http.get(
            EDITORIALS_URL.format(contest_id=contest_id, task_id=task_id),
            params={'lang': 'en'},
            service=_SERVICE,
            allow_status={HTTPStatus.NOT_FOUND},
        )
        if response.status == HTTPStatus.NOT_FOUND:
            return None
        # On the event loop: a page takes a few milliseconds to parse, too
        # little to be worth a worker thread.
        return parse_task_editorials(response.text(), contest_id, task_id)


def _rank(editorial: AtCoderEditorial) -> tuple[bool, bool, bool]:
    """How ``best`` orders official editorials: the smallest first."""
    return (not editorial.in_page_language, editorial.video, editorial.scope != _TASK)


@dataclass
class _Section:
    """A div.editorial-section being read."""

    task: tuple[str, str] | None  # (contest, task) its heading links, lowercased
    editorials: list[AtCoderEditorial] = field(default_factory=list)

    @property
    def scope(self) -> str:
        return _OVERALL if self.task is None else _TASK


@dataclass
class _Entry:
    """An li being read."""

    other_language: bool
    official: bool = False
    href: str | None = None  # the editorial link's
    title: list[str] = field(default_factory=list)  # its text, in pieces
    video: bool = False


class _EditorialPageParser(HTMLParser):
    """Reads the page's language, and each div.editorial-section with the task
    that the heading before it links.

    A heading is a span with class 'h2', or an h3. Only a link inside one
    names a task, and only an li inside a section is an editorial, so links
    elsewhere, such as the language menu's to the page itself, are passed
    over.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.language: str | None = None  # the Content-Language meta's
        self.sections: list[_Section] = []
        # Inside a heading: its tag, and how many elements with that tag are
        # open (it included), so that its own end tag is the one that ends it.
        self._heading_tag: str | None = None
        self._heading_depth = 0
        # The task that the latest heading links, until a section takes it.
        self._task: tuple[str, str] | None = None
        self._section: _Section | None = None
        self._section_depth = 0  # the divs open in the section, it included
        self._entry: _Entry | None = None
        self._in_link = False  # inside the entry's editorial link

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (_attribute(attrs, 'class') or '').split()
        if tag == 'meta':
            self._read_meta(attrs)
        if self._section is not None:
            self._start_in_section(tag, attrs, classes)
        elif tag == 'div' and 'editorial-section' in classes:
            self._section = _Section(self._task)
            self._section_depth = 1
            self._heading_tag, self._task = None, None
        elif self._heading_tag is not None:
            if tag == self._heading_tag:
                self._heading_depth += 1
            elif tag == 'a' and self._task is None:
                self._task = _task_in(_attribute(attrs, 'href') or '')
        elif (tag == 'span' and 'h2' in classes) or tag == 'h3':
            self._heading_tag, self._heading_depth, self._task = tag, 1, None

    def handle_endtag(self, tag: str) -> None:
        if self._section is not None:
            if tag == 'a':
                self._in_link = False
            elif tag in ('li', 'ul'):
                self._end_entry()
            elif tag == 'div':
                self._section_depth -= 1
                if self._section_depth == 0:
                    self._end_section()
        elif tag == self._heading_tag:
            self._heading_depth -= 1
            if self._heading_depth == 0:
                self._heading_tag = None

    def handle_data(self, data: str) -> None:
        if self._in_link and self._entry is not None:
            self._entry.title.append(data)

    def close(self) -> None:
        super().close()
        self._end_section()  # in case the page ended inside a section

    def _read_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        """Note the page's language, from the first Content-Language meta."""
        equivalent = (_attribute(attrs, 'http-equiv') or '').strip().lower()
        if self.language is None and equivalent == 'content-language':
            self.language = (_attribute(attrs, 'content') or '').strip().lower() or None

    def _start_in_section(
        self, tag: str, attrs: list[tuple[str, str | None]], classes: list[str]
    ) -> None:
        entry = self._entry
        if tag == 'div':
            self._section_depth += 1
        elif tag == 'li':
            self._end_entry()  # in case the last one's end tag was left out
            self._entry = _Entry(other_language='lang-other' in classes)
        elif entry is None:
            return
        elif tag == 'span' and 'label' in classes:
            entry.official = True  # 'Official', '公式' on a Japanese page
        elif tag == 'a' and entry.href is None and 'username' not in classes:
            entry.href = _attribute(attrs, 'href') or ''
            self._in_link = True
        elif tag == 'span' and self._in_link and 'glyphicon-film' in classes:
            entry.video = True

    def _end_entry(self) -> None:
        entry, section = self._entry, self._section
        self._entry, self._in_link = None, False
        if entry is None or section is None:
            return
        url = _resolve(entry.href)
        if url is None:
            logger.debug('Skipping an AtCoder editorial that links %r', entry.href)
            return
        section.editorials.append(
            AtCoderEditorial(
                url=url,
                title=_visible(''.join(entry.title)),
                official=entry.official,
                in_page_language=not entry.other_language,
                video=entry.video,
                scope=section.scope,
            )
        )

    def _end_section(self) -> None:
        self._end_entry()
        if self._section is not None:
            self.sections.append(self._section)
        self._section = None


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    """The value of attribute ``name``: the first, as browsers read repeats."""
    return next((value for key, value in attrs if key == name), None)


def _task_in(href: str) -> tuple[str, str] | None:
    """The contest and task IDs, lowercased, in a link to a task on AtCoder."""
    try:
        parts = urlsplit(href.strip())
    except ValueError:  # e.g. an unclosed [ in the host
        return None
    if parts.netloc and parts.hostname not in _ATCODER_HOSTS:
        return None
    match = _TASK_PATH.fullmatch(parts.path)
    if match is None:
        return None
    return match.group(1).lower(), match.group(2).lower()


def _resolve(href: str | None) -> str | None:
    """Where an editorial's link leads, as an absolute http(s) URL; None if it
    leads nowhere a member could open.

    Links to other sites are unwrapped from '/jump?url=...', and editorial
    pages on AtCoder lose any query, so that each has one URL.
    """
    href = (href or '').strip()
    if not href:  # a blank link leads back to the page itself
        return None
    try:
        url = urljoin(_ATCODER, href)
        parts = urlsplit(url)
        if parts.hostname in _ATCODER_HOSTS and parts.path == _JUMP_PATH:
            # parse_qs decodes the link's percent-escapes.
            url = parse_qs(parts.query).get('url', [''])[0].strip()
            parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. an unclosed [ in the host
        return None
    if parts.scheme not in ('http', 'https') or not host:
        return None
    if host in _ATCODER_HOSTS and _EDITORIAL_PATH.fullmatch(parts.path):
        return f'https://atcoder.jp{parts.path}'
    # A link can't have whitespace in it: Discord would end it there.
    if any(character.isspace() for character in url):
        return None
    return url


def _visible(text: str) -> str:
    """``text`` as a browser shows it: whitespace runs as one space, trimmed."""
    return _HTML_WHITESPACE.sub(' ', text).strip(' ')


def _unreadable_page() -> ExternalServiceError:
    return ExternalServiceError(_SERVICE, "AtCoder's editorial page could not be read.")
