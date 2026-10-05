"""Tests for tle.kcpc.platforms.atcoder.editorials: reading the editorial
pages of AtCoder tasks.

fixtures/atcoder/editorial_*.html are AtCoder's pages of 2026-10-03, cut down
to the page's main column, each with a comment naming the page it came from.
The navigation bar, tabs, scripts and footer are gone, and members' names and
their own sites are made up (writerN, example.com). The column is otherwise
as AtCoder serves it: LF line endings, tab indents, and the task's heading,
then two editorial sections, the task's and the contest's. Most were fetched
with lang=en; editorial_task_abc470_d_ja_ui.html is the same page in Japanese,
and three files are pages of other kinds.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms.atcoder import editorials
from tle.kcpc.platforms.atcoder.editorials import (
    AtCoderEditorial,
    AtCoderEditorials,
    AtCoderEditorialsClient,
    parse_task_editorials,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'atcoder'
UNREADABLE = "AtCoder's editorial page could not be read."
INVALID_PROBLEM = "That isn't a valid AtCoder problem."
LOGGER = 'tle.kcpc.platforms.atcoder.editorials'
ABC470_D = 'editorial_task_abc470_d.html'


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def editorial(
    url: str,
    title: str = 'Editorial',
    *,
    official: bool = True,
    english: bool = True,
    video: bool = False,
    scope: str = 'task',
) -> AtCoderEditorial:
    return AtCoderEditorial(
        url=url,
        title=title,
        official=official,
        in_page_language=english,
        video=video,
        scope=scope,
    )


ABC470 = 'https://atcoder.jp/contests/abc470/editorial/'
EXPECTED_ABC470_D = (
    editorial(f'{ABC470}23875', '解説', english=False),
    editorial(f'{ABC470}23885'),
    editorial(
        'https://www.youtube.com/watch?v=cbUoPSwaqFo&t=4272s',
        '解説放送',
        english=False,
        video=True,
    ),
    editorial(f'{ABC470}23876', 'ユーザ解説', official=False, english=False),
    editorial(
        'https://youtube.com/live/cbUoPSwaqFo?feature=share',
        '解説放送',
        english=False,
        video=True,
        scope='overall',
    ),
)

# Each fixture's task, how many editorials it lists and how many are
# official, and the best of them.
READABLE = {
    ABC470_D: ('abc470', 'abc470_d', 5, 4, editorial(f'{ABC470}23885')),
    # Seventy minutes after the contest: no English editorial yet.
    'editorial_task_abc478_g_ja_only.html': (
        'abc478',
        'abc478_g',
        2,
        2,
        editorial(
            'https://atcoder.jp/contests/abc478/editorial/26504', '解説', english=False
        ),
    ),
    'editorial_task_arc230_b.html': (
        'arc230',
        'arc230_b',
        2,
        2,
        editorial('https://atcoder.jp/contests/arc230/editorial/25821'),
    ),
    'editorial_task_agc078_a.html': (
        'agc078',
        'agc078_a',
        2,
        2,
        editorial('https://atcoder.jp/contests/agc078/editorial/26421'),
    ),
    # 2016: only the contest's PDF, through /jump.
    'editorial_task_arc050_b_old_pdf.html': (
        'arc050',
        'arc050_b',
        1,
        1,
        editorial(
            'http://arc050.contest.atcoder.jp/data/arc/050/review.pdf', scope='overall'
        ),
    ),
    # 2020: members' editorials for the task, and the contest's PDF.
    'editorial_task_abc150_d_user_and_pdf.html': (
        'abc150',
        'abc150_d',
        5,
        1,
        editorial('https://img.atcoder.jp/abc150/editorial.pdf', scope='overall'),
    ),
    # Titled with the names of the models that wrote them.
    'editorial_task_awc0170_c_titles.html': (
        'awc0170',
        'awc0170_c',
        9,
        9,
        editorial(
            'https://atcoder.jp/contests/awc0170/editorial/25722',
            'Claude 4.6 Opus (Thinking)',
        ),
    ),
    # A Daily Training round's copy of ABC450 D links the same editorials
    # under its own name.
    'editorial_task_adt_abc450_d.html': (
        'adt_all_20260713_1',
        'abc450_d',
        3,
        3,
        editorial('https://atcoder.jp/contests/adt_all_20260713_1/editorial/17743'),
    ),
}
UNREADABLE_PAGES = {
    'japanese': ('editorial_task_abc470_d_ja_ui.html', 'abc470', 'abc470_d'),
    'contest-page': ('editorial_contest_arc050.html', 'arc050', 'arc050_b'),
    'not-found': ('editorial_404_task_not_found.html', 'abc042', 'abc042_c'),
    'jump': ('editorial_jump_interstitial.html', 'arc050', 'arc050_b'),
    'other-task': (ABC470_D, 'abc470', 'abc470_e'),
    'other-contest': (ABC470_D, 'abc471', 'abc470_d'),
}


def entry(
    href: str = f'{ABC470}23885',
    title: str = 'Editorial',
    *,
    official: bool = True,
    other_language: bool = False,
    video: bool = False,
) -> str:
    """An editorial's li, marked up as AtCoder's are."""
    li = '<li class="hidden lang-other">' if other_language else '<li>'
    label = '<span class="label label-default">Official</span>' if official else ''
    film = ' <span class="glyphicon glyphicon-film"></span>' if video else ''
    return (
        f'    {li}\n      \n      {label}\n'
        f'      <a href="{href}" target="_blank" rel="noopener">{title}{film}</a>'
        ' <span class="grey">by</span> <a href="/users/en_translator" '
        'class="username"><span class="user-unrated">en_translator</span></a>\n'
        '      \n    </li>\n'
    )


def section(*entries: str) -> str:
    """An editorial section of ``entries``, marked up as AtCoder's are."""
    hidden = ' hidden' if entries else ''
    return (
        '<div class="editorial-section">\n<ul>\n  \n' + ''.join(entries) + '  \n</ul>\n'
        f'<p class="no-editorial-msg{hidden}">There is no editorial yet.</p>\n'
        '</div>\n<br>\n'
    )


def page(
    task: Sequence[str] = (),
    overall: Sequence[str] = (),
    *,
    task_link: str = '/contests/abc470/tasks/abc470_d',
    language: str | None = 'en',
) -> str:
    """A task's editorial page, as AtCoder marks it up: the heading linking
    ``task_link``, then the task's section of ``task`` and the contest's of
    ``overall``.
    """
    meta = (
        ''
        if language is None
        else f'\t<meta http-equiv="Content-Language" content="{language}">\n'
    )
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n\t<title>Editorial - Example</title>\n'
        + meta
        + '</head>\n<body>\n<div class="col-sm-12">\n\t<div>\n'
        f'\t\t<span class="h2">\n\t\t\t<a href="{task_link}">D - Example</a>'
        ' Editorial\n\t\t\t\n\t\t</span>\n'
        '\t\t<div class="checkbox pull-right">\n\t\t\t<label><input '
        'type="checkbox" id="show-multilang-editorials"> Show editorials in other '
        'languages</label>\n\t\t</div>\n\t</div>\n\t<hr>\n\t\n'
        + section(*task)
        + '\t<hr>\n\t<h3>Overall Editorial</h3>\n\t\n'
        + section(*overall)
        + '</div>\n</body>\n</html>\n'
    )


def parse(
    html: str, contest_id: str = 'abc470', task_id: str = 'abc470_d'
) -> AtCoderEditorials:
    return parse_task_editorials(html, contest_id, task_id)


def urls(found: AtCoderEditorials) -> list[str]:
    return [editorial.url for editorial in found.editorials]


class TestTheFixturePages:
    @pytest.mark.parametrize(('name', 'expected'), READABLE.items(), ids=list(READABLE))
    def test_list_these_editorials(
        self, name: str, expected: tuple[str, str, int, int, AtCoderEditorial]
    ) -> None:
        contest_id, task_id, count, official, best = expected

        found = parse(fixture(name), contest_id, task_id)

        assert (found.contest_id, found.task_id) == (contest_id, task_id)
        assert len(found.editorials) == count
        assert sum(editorial.official for editorial in found.editorials) == official
        assert found.best() == best
        assert found.has_editorial

    def test_abc470_d_lists_exactly_these(self) -> None:
        found = parse(fixture(ABC470_D))
        assert found == AtCoderEditorials('abc470', 'abc470_d', EXPECTED_ABC470_D)

    @pytest.mark.parametrize(
        ('name', 'contest_id', 'task_id'),
        UNREADABLE_PAGES.values(),
        ids=list(UNREADABLE_PAGES),
    )
    def test_pages_that_are_not_the_tasks_english_page_are_unreadable(
        self, name: str, contest_id: str, task_id: str
    ) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse(fixture(name), contest_id, task_id)
        assert (excinfo.value.service, excinfo.value.status) == ('AtCoder', None)

    @pytest.mark.parametrize('task_id', ['arc050_a', 'arc050_b', 'arc050_d'])
    def test_the_contests_page_is_unreadable_for_any_of_its_tasks(
        self, task_id: str
    ) -> None:
        # It has a section for each task.
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(fixture('editorial_contest_arc050.html'), 'arc050', task_id)

    def test_are_marked_up_like_atcoders_pages(self) -> None:
        # Guards the fixtures: an editor or a git setting could quietly change
        # what the tests above read.
        names = sorted(path.name for path in FIXTURES.glob('editorial_*.html'))
        assert len(names) == 12
        for name in names:
            data = (FIXTURES / name).read_bytes()
            assert b'\r' not in data, name
            assert data.startswith(b'<!DOCTYPE html>\n<!-- KCPC research sample.')
        html = fixture(ABC470_D)
        assert (
            '\t\t\t<span class="h2">\n\t\t\t\t<a href="/contests/abc470/tasks/'
            'abc470_d">D - Inverse and Swap</a> Editorial\n'
        ) in html
        assert '\t\t<div class="editorial-section">\n<ul>\n  \n    <li class=' in html
        assert '<meta http-equiv="Content-Language" content="en">' in html
        assert (
            '<span class="label label-default">Official</span>\n      <a href="/'
        ) in html
        assert '解説放送 <span class="glyphicon glyphicon-film"></span></a>' in html
        # Hidden by the server, though a browser would show no entry there.
        message = '<p class="no-editorial-msg hidden">There is no editorial yet.</p>'
        assert message in fixture('editorial_task_abc478_g_ja_only.html')

    @pytest.mark.parametrize('name', READABLE)
    def test_crlf_line_endings_read_the_same(self, name: str) -> None:
        contest_id, task_id = READABLE[name][:2]
        crlf = fixture(name).replace('\n', '\r\n')
        assert parse(crlf, contest_id, task_id) == parse(
            fixture(name), contest_id, task_id
        )

    def test_the_navigation_bar_tabs_and_scripts_change_nothing(self) -> None:
        # The full page also links the task's editorial page itself, in other
        # languages, and the contest's editorials; and its scripts and share
        # widget name the page too.
        chrome = (
            '<nav class="navbar navbar-inverse navbar-fixed-top">\n'
            '<ul class="nav navbar-nav navbar-right">\n<li class="dropdown">\n'
            '<a class="dropdown-toggle" data-toggle="dropdown" href="#">'
            "<img src='//img.atcoder.jp/assets/top/img/flag-lang/en.png'> English"
            '</a>\n<ul class="dropdown-menu">\n'
            '<li><a href="/contests/abc470/tasks/abc470_d/editorial?lang=ja">'
            "<img src='//img.atcoder.jp/assets/top/img/flag-lang/ja.png'> 日本語"
            '</a></li>\n'
            '<li><a href="/contests/abc470/tasks/abc470_d/editorial?lang=en">'
            "<img src='//img.atcoder.jp/assets/top/img/flag-lang/en.png'> English"
            '</a></li>\n</ul>\n</li>\n</ul>\n</nav>\n'
            '<div id="contest-nav-tabs"><ul class="nav nav-tabs">\n'
            '<li><a href="/contests/abc470/tasks"> Tasks</a></li>\n'
            '<li class="active"><a href="/contests/abc470/editorial"> Editorial'
            '</a></li>\n'
            '<li class="pull-right"><a href="https://example.com/discuss" '
            'target="_blank"> Discuss</a></li>\n</ul></div>\n'
            '<div class="a2a_kit" data-a2a-url="https://atcoder.jp/contests/abc470/'
            'tasks/abc470_d/editorial?lang=en"></div>\n'
            '<script>var entry = "<div class=editorial-section><ul><li><span '
            'class=label>Official</span><a href=/x>x</a></li></ul></div>";\n'
            '$("li.lang-other").toggleClass("hidden", true);</script>\n'
        )
        html = fixture(ABC470_D)
        wrapped = html.replace('<body>\n', f'<body>\n{chrome}', 1)

        assert wrapped != html
        assert parse(wrapped) == parse(html)


class TestBest:
    def test_english_before_other_languages(self) -> None:
        found = parse(
            page(
                [entry(f'{ABC470}1', '解説', other_language=True)],
                [entry(f'{ABC470}2')],
            )
        )
        best = found.best()
        assert best is not None and best.url == f'{ABC470}2'

    def test_text_before_video_in_the_same_language(self) -> None:
        found = parse(
            page(
                [
                    entry('https://www.youtube.com/watch?v=1', video=True),
                    entry(f'{ABC470}2'),
                ]
            )
        )
        best = found.best()
        assert best is not None and best.url == f'{ABC470}2'

    def test_language_comes_before_video(self) -> None:
        found = parse(
            page(
                [
                    entry(f'{ABC470}1', '解説', other_language=True),
                    entry('https://www.youtube.com/watch?v=2', video=True),
                ]
            )
        )
        best = found.best()
        assert best is not None and best.video and best.in_page_language

    def test_the_tasks_own_before_the_contests(self) -> None:
        found = parse(page([entry(f'{ABC470}2')], [entry(f'{ABC470}1')]))
        best = found.best()
        assert best is not None and (best.url, best.scope) == (f'{ABC470}2', 'task')

    def test_then_the_first_on_the_page(self) -> None:
        found = parse(page([entry(f'{ABC470}2', 'B'), entry(f'{ABC470}1', 'A')]))
        best = found.best()
        assert best is not None and best.title == 'B'

    def test_takes_a_japanese_one_when_there_is_nothing_else(self) -> None:
        found = parse(page([entry(f'{ABC470}1', '解説', other_language=True)]))
        best = found.best()
        assert best is not None and not best.in_page_language
        assert found.has_editorial

    @pytest.mark.parametrize(
        'html',
        [
            page(),
            page([entry(official=False)], [entry(f'{ABC470}2', official=False)]),
            page([entry('javascript:void(0)')]),
        ],
        ids=['no-entries', 'members-only', 'no-link'],
    )
    def test_none_without_an_official_editorial(self, html: str) -> None:
        found = parse(html)
        assert found.best() is None
        assert not found.has_editorial

    def test_the_page_url_lists_them_all_in_english(self) -> None:
        found = parse(fixture(ABC470_D))
        assert found.page_url == (
            'https://atcoder.jp/contests/abc470/tasks/abc470_d/editorial?lang=en'
        )


class TestEntries:
    def test_official_ones_have_the_label(self) -> None:
        found = parse(page([entry(official=True), entry(official=False)]))
        assert [e.official for e in found.editorials] == [True, False]

    def test_any_label_counts(self) -> None:
        # As on a Japanese page, where it reads 公式.
        html = page([entry()]).replace('>Official<', '>公式<')
        assert parse(html).editorials[0].official

    def test_lang_other_marks_another_language(self) -> None:
        found = parse(page([entry(other_language=True), entry()]))
        assert [e.in_page_language for e in found.editorials] == [False, True]

    def test_a_video_has_the_film_icon_inside_its_link(self) -> None:
        outside = entry().replace(
            '</a> <span class="grey">',
            '</a> <span class="glyphicon glyphicon-film"></span> <span class="grey">',
        )
        found = parse(page([entry(video=True), outside]))
        assert [e.video for e in found.editorials] == [True, False]

    def test_the_editorial_is_the_first_link_that_isnt_the_authors(self) -> None:
        author_first = entry().replace(
            '<a href="https://atcoder.jp',
            '<a href="/users/writer1" class="username">writer1</a> '
            '<a href="https://atcoder.jp',
        )
        found = parse(page([author_first]))
        assert urls(found) == [f'{ABC470}23885']
        assert found.editorials[0].title == 'Editorial'

    @pytest.mark.parametrize(
        ('title', 'shown'),
        [
            ('Editorial', 'Editorial'),
            ('\n\t解説放送  ', '解説放送'),
            ('Claude 4.6 Opus (Thinking)', 'Claude 4.6 Opus (Thinking)'),
            ('A &amp; B &lt;fast&gt;', 'A & B <fast>'),
            ('<b>Bold</b> editorial', 'Bold editorial'),
            ('', ''),
        ],
        ids=['plain', 'whitespace', 'model', 'entities', 'markup', 'empty'],
    )
    def test_the_title_is_the_links_text_as_shown(self, title: str, shown: str) -> None:
        assert parse(page([entry(title=title)])).editorials[0].title == shown

    def test_the_no_editorial_message_is_never_read(self) -> None:
        # A browser shows it whenever no entry is visible, Japanese ones being
        # hidden: entries are counted instead.
        shown = page([entry(other_language=True)]).replace(
            'no-editorial-msg hidden', 'no-editorial-msg'
        )
        hidden = page().replace('no-editorial-msg"', 'no-editorial-msg hidden"')
        assert len(parse(shown).editorials) == 1
        assert parse(hidden).editorials == ()

    def test_end_tags_that_html_lets_pages_leave_out(self) -> None:
        unclosed = [
            entry(f'{ABC470}1').replace('</li>', ''),
            entry(f'{ABC470}2').replace('</li>', ''),
        ]
        found = parse(page(unclosed, [entry(f'{ABC470}3').replace('</li>', '')]))
        assert urls(found) == [f'{ABC470}1', f'{ABC470}2', f'{ABC470}3']

    def test_entries_in_order_task_first(self) -> None:
        found = parse(
            page(
                [entry(f'{ABC470}3'), entry(f'{ABC470}1')],
                [entry(f'{ABC470}4'), entry(f'{ABC470}2')],
            )
        )
        assert urls(found) == [f'{ABC470}{n}' for n in (3, 1, 4, 2)]
        assert [e.scope for e in found.editorials] == ['task'] * 2 + ['overall'] * 2


class TestLinks:
    @pytest.mark.parametrize(
        ('href', 'url'),
        [
            ('/contests/abc470/editorial/23885', f'{ABC470}23885'),
            ('/contests/abc470/editorial/23885?lang=en', f'{ABC470}23885'),
            (
                'https://atcoder.jp/contests/abc470/editorial/23885#top',
                f'{ABC470}23885',
            ),
            (
                'https://www.atcoder.jp/contests/abc470/editorial/23885',
                f'{ABC470}23885',
            ),
            ('  /contests/abc470/editorial/23885\n', f'{ABC470}23885'),
            (
                'https://img.atcoder.jp/abc150/editorial.pdf',
                'https://img.atcoder.jp/abc150/editorial.pdf',
            ),
            (
                '/jump?url=http%3A%2F%2Farc050.contest.atcoder.jp%2Fdata%2Farc%2F050'
                '%2Freview.pdf',
                'http://arc050.contest.atcoder.jp/data/arc/050/review.pdf',
            ),
            (
                '/jump?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DcbUoPSwaqFo'
                '%26t%3D4272s',
                'https://www.youtube.com/watch?v=cbUoPSwaqFo&t=4272s',
            ),
            (
                'https://atcoder.jp/jump?url=https%3A%2F%2Fexample.com%2Feditorial',
                'https://example.com/editorial',
            ),
            (
                '/jump?url=https%3A%2F%2Fatcoder.jp%2Fcontests%2Fabc470%2Feditorial'
                '%2F23885%3Flang%3Dja',
                f'{ABC470}23885',
            ),
            (
                '/contests/abc470/editorial',
                'https://atcoder.jp/contests/abc470/editorial',
            ),
        ],
        ids=[
            'path',
            'query',
            'fragment',
            'www',
            'whitespace',
            'pdf',
            'jump-pdf',
            'jump-video',
            'jump-absolute',
            'jump-to-atcoder',
            'other-page',
        ],
    )
    def test_lead_to_absolute_urls(self, href: str, url: str) -> None:
        assert urls(parse(page([entry(href)]))) == [url]

    @pytest.mark.parametrize(
        'href',
        [
            '',
            '   ',
            'javascript:void(0)',
            'mailto:admin@example.com',
            'ftp://example.com/editorial.pdf',
            'data:text/html,editorial',
            'http:///editorial',
            'https://[atcoder.jp/contests/abc470/editorial/23885',
            '/jump',
            '/jump?url=',
            '/jump?url=javascript%3Aalert%281%29',
            '/jump?url=%2Fcontests%2Fabc470%2Feditorial%2F23885',
            '/jump?url=https%3A%2F%2Fexample.com%2Fmy%20editorial',
        ],
        ids=[
            'empty',
            'blank',
            'javascript',
            'mailto',
            'ftp',
            'data',
            'no-host',
            'bad-host',
            'jump-without-url',
            'jump-empty',
            'jump-javascript',
            'jump-relative',
            'jump-with-space',
        ],
    )
    def test_links_no_member_could_open_are_skipped(self, href: str) -> None:
        found = parse(page([entry(href), entry(f'{ABC470}1')]))
        assert urls(found) == [f'{ABC470}1']

    def test_an_entry_without_a_link_is_skipped(self) -> None:
        unlinked = (
            '<li><span class="label label-default">Official</span> Editorial</li>\n'
        )
        assert urls(parse(page([unlinked, entry(f'{ABC470}1')]))) == [f'{ABC470}1']

    def test_skipped_entries_are_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        parse(page([entry('javascript:void(0)'), entry('mailto:a@example.com')]))
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * 2


class TestThePage:
    @pytest.mark.parametrize(
        ('contest_id', 'task_id'),
        [('ABC470', 'ABC470_D'), ('Abc470', 'abc470_D'), ('abc470', 'abc470_d')],
    )
    def test_ids_are_compared_without_case(self, contest_id: str, task_id: str) -> None:
        found = parse(fixture(ABC470_D), contest_id, task_id)
        assert (found.contest_id, found.task_id) == (contest_id, task_id)
        assert found.editorials == EXPECTED_ABC470_D

    def test_a_heading_that_links_the_task_in_capitals_reads(self) -> None:
        # AtCoder repeats the contest as the URL had it.
        html = page([entry()], task_link='/contests/ABC470/tasks/abc470_d')
        assert urls(parse(html)) == [f'{ABC470}23885']

    def test_a_heading_may_link_the_task_by_its_full_url(self) -> None:
        link = 'https://atcoder.jp/contests/abc470/tasks/abc470_d'
        assert urls(parse(page([entry()], task_link=link))) == [f'{ABC470}23885']

    @pytest.mark.parametrize(
        'task_link',
        [
            'https://example.com/contests/abc470/tasks/abc470_d',
            '/contests/abc470/tasks/abc470_d/editorial',
            '/contests/abc470/tasks',
            'https://[atcoder.jp/contests/abc470/tasks/abc470_d',
        ],
        ids=['other-site', 'editorial-page', 'task-list', 'bad-host'],
    )
    def test_a_heading_must_link_the_task(self, task_link: str) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(page([entry()], task_link=task_link))

    def test_only_a_link_inside_a_heading_names_the_task(self) -> None:
        html = page([entry()], task_link='#').replace(
            '\t<hr>\n\t\n<div class="editorial-section">',
            '\t<hr>\n<p><a href="/contests/abc470/tasks/abc470_d">D</a></p>\n'
            '<div class="editorial-section">',
            1,
        )
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(html)

    def test_a_heading_ends_with_its_own_end_tag(self) -> None:
        # Spans inside the heading don't end it; a link after it isn't in it.
        nested = page([entry()]).replace(
            '<a href="/contests/abc470/tasks/abc470_d">D - Example</a>',
            '<span class="small"><span>D</span></span>'
            '<a href="/contests/abc470/tasks/abc470_d">D - Example</a>',
        )
        after = page([entry()], task_link='#').replace(
            '\t\t</span>\n',
            '\t\t</span><a href="/contests/abc470/tasks/abc470_d">',
            1,
        )
        assert urls(parse(nested)) == [f'{ABC470}23885']
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(after)

    def test_divs_inside_a_section_dont_end_it(self) -> None:
        html = page([entry(f'{ABC470}1')]).replace(
            '<ul>\n  \n    <li>', '<div><div></div></div><ul>\n  \n    <li>', 1
        )
        found = parse(html)
        assert [(e.url, e.scope) for e in found.editorials] == [(f'{ABC470}1', 'task')]

    def test_a_page_that_ends_inside_a_section_keeps_what_it_read(self) -> None:
        html = page([entry(f'{ABC470}1')])
        cut = html[: html.index('<h3>Overall Editorial</h3>')] + (
            '<h3>Overall Editorial</h3>\n<div class="editorial-section">\n<ul>\n'
            + entry(f'{ABC470}2')
        )
        assert urls(parse(cut)) == [f'{ABC470}1', f'{ABC470}2']

    @pytest.mark.parametrize('language', ['en', 'EN', ' en '])
    def test_an_english_page_reads(self, language: str) -> None:
        assert urls(parse(page([entry()], language=language))) == [f'{ABC470}23885']

    def test_a_page_without_a_language_reads(self) -> None:
        assert urls(parse(page([entry()], language=None))) == [f'{ABC470}23885']

    @pytest.mark.parametrize('language', ['ja', 'en-US', 'zh'])
    def test_a_page_in_another_language_is_unreadable(self, language: str) -> None:
        # Its 'lang-other' marks editorials in another language than English.
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(page([entry()], language=language))

    def test_the_first_language_meta_counts(self) -> None:
        html = page([entry()]).replace(
            '</head>', '\t<meta http-equiv="content-language" content="ja">\n</head>'
        )
        assert urls(parse(html)) == [f'{ABC470}23885']

    def test_sections_for_two_tasks_are_unreadable(self) -> None:
        html = page([entry()]).replace(
            '<h3>Overall Editorial</h3>',
            '<h3>E - Other <a href="/contests/abc470/tasks/abc470_e">?</a></h3>',
        )
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(html)

    def test_any_number_of_the_contests_sections_reads(self) -> None:
        none = page([entry(f'{ABC470}1')])
        none = none[: none.index('\t<hr>\n\t<h3>')] + '</div>\n</body>\n</html>\n'
        two = page([entry(f'{ABC470}1')], [entry(f'{ABC470}2')]).replace(
            '</div>\n</body>',
            '<h3>More</h3>\n' + section(entry(f'{ABC470}3')) + '</div>\n</body>',
        )
        assert urls(parse(none)) == [f'{ABC470}1']
        assert urls(parse(two)) == [f'{ABC470}{n}' for n in (1, 2, 3)]

    @pytest.mark.parametrize(
        'html',
        ['', '\n', 'Not Found', '{"editorials": []}', '<html><body></body></html>'],
        ids=['empty', 'blank', 'text', 'json', 'empty-page'],
    )
    def test_anything_else_is_unreadable(self, html: str) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse(html)

    def test_unreadable_pages_are_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        for html in ('', page(language='ja')):
            with pytest.raises(ExternalServiceError):
                parse(html)
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * 2


class FakeAtCoder:
    """A local stand-in for AtCoder's editorial pages, at ``editorials_url``.

    It answers every request with the reply set by ``reply``, a 404 until
    then, and records the path and query of each.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._status = 404
        self._body = b''
        app = web.Application()
        app.router.add_get('/contests/{contest}/tasks/{task}/editorial', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def editorials_url(self) -> str:
        """The site's ``EDITORIALS_URL``, with {contest_id} and {task_id} fields."""
        root = str(self._server.make_url('/contests/'))
        return root + '{contest_id}/tasks/{task_id}/editorial'

    def reply(self, body: bytes, *, status: int = 200) -> None:
        self._status, self._body = status, body

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        return web.Response(
            status=self._status,
            body=self._body,
            headers={'Content-Type': 'text/html; charset=utf-8'},
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeAtCoder]:
    site = FakeAtCoder()
    await site.start()
    monkeypatch.setattr(editorials, 'EDITORIALS_URL', site.editorials_url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[AtCoderEditorialsClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield AtCoderEditorialsClient(http)
    await http.close()


class TestFetch:
    async def test_reads_the_tasks_page_in_english(
        self, site: FakeAtCoder, client: AtCoderEditorialsClient
    ) -> None:
        site.reply((FIXTURES / ABC470_D).read_bytes())

        found = await client.fetch('abc470', 'abc470_d')

        assert found == AtCoderEditorials('abc470', 'abc470_d', EXPECTED_ABC470_D)
        assert site.requests == [
            ('/contests/abc470/tasks/abc470_d/editorial', {'lang': 'en'})
        ]
        page_url = site.editorials_url.format(contest_id='abc470', task_id='abc470_d')
        assert found.page_url == f'{page_url}?lang=en'

    async def test_asks_by_the_ids_as_given(
        self, site: FakeAtCoder, client: AtCoderEditorialsClient
    ) -> None:
        site.reply((FIXTURES / ABC470_D).read_bytes())

        found = await client.fetch('ABC470', 'abc470_D')

        assert found is not None and found.editorials == EXPECTED_ABC470_D
        assert site.requests == [
            ('/contests/ABC470/tasks/abc470_D/editorial', {'lang': 'en'})
        ]

    async def test_404_means_no_such_task(
        self, site: FakeAtCoder, client: AtCoderEditorialsClient
    ) -> None:
        site.reply(
            (FIXTURES / 'editorial_404_task_not_found.html').read_bytes(), status=404
        )

        assert await client.fetch('abc042', 'abc042_c') is None
        assert site.requests == [
            ('/contests/abc042/tasks/abc042_c/editorial', {'lang': 'en'})
        ]

    async def test_a_page_in_japanese_is_refused(
        self, site: FakeAtCoder, client: AtCoderEditorialsClient
    ) -> None:
        site.reply((FIXTURES / 'editorial_task_abc470_d_ja_ui.html').read_bytes())

        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            await client.fetch('abc470', 'abc470_d')

    @pytest.mark.parametrize(
        ('contest_id', 'task_id'),
        [
            ('', 'abc470_d'),
            ('abc470', ''),
            ('abc 470', 'abc470_d'),
            ('abc470', 'abc470/editorial'),
            ('..', 'abc470_d'),
            ('abc470', 'abc470_d?lang=ja'),
            ('abc.470', 'abc470_d'),
            ('abc470', 'abc470_d\n'),
            ('x' * 65, 'abc470_d'),
            ('ａｂｃ470', 'abc470_d'),
        ],
        ids=[
            'no-contest',
            'no-task',
            'space',
            'slash',
            'dots',
            'query',
            'dot',
            'newline',
            'long',
            'full-width',
        ],
    )
    async def test_ids_that_cant_be_atcoders_are_refused_unasked(
        self,
        site: FakeAtCoder,
        client: AtCoderEditorialsClient,
        contest_id: str,
        task_id: str,
    ) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            await client.fetch(contest_id, task_id)

        assert str(excinfo.value) == INVALID_PROBLEM
        assert not isinstance(excinfo.value, ExternalServiceError)
        assert site.requests == []

    @pytest.mark.parametrize(
        ('contest_id', 'task_id'),
        [
            ('cf17-final', 'cf17_final_a'),
            ('DEGwer2023', '1202Contest_a'),
            ('x' * 64, 'a'),
        ],
    )
    async def test_ids_of_up_to_64_letters_digits_underscores_and_dashes_are_asked(
        self,
        site: FakeAtCoder,
        client: AtCoderEditorialsClient,
        contest_id: str,
        task_id: str,
    ) -> None:
        assert await client.fetch(contest_id, task_id) is None  # the fake's 404
        assert site.requests == [
            (f'/contests/{contest_id}/tasks/{task_id}/editorial', {'lang': 'en'})
        ]

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (403, r'^AtCoder returned an error \(HTTP 403\)\.$'),
            (503, '^AtCoder is not responding right now'),
        ],
    )
    async def test_failures_name_atcoder(
        self,
        site: FakeAtCoder,
        client: AtCoderEditorialsClient,
        status: int,
        message: str,
    ) -> None:
        site.reply(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch('abc470', 'abc470_d')
        assert (excinfo.value.service, excinfo.value.status) == ('AtCoder', status)
