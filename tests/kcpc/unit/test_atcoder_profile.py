"""Tests for tle.kcpc.platforms.atcoder.profile: reading AtCoder users' profiles.

fixtures/atcoder/profile_*.html are synthetic pages of made-up users, marked up
exactly as https://atcoder.jp/users/<name>?lang=en is: LF line endings, tab
indents, the name in a link with class 'username' around a span with its
colour class, and two tables of ``<tr><th>Label</th><td>value</td></tr>`` rows,
the user's details and their algorithm contests. A user never rated has no
contest rows; a user who left a detail empty has no row for it.
"""

import dataclasses
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms.atcoder import profile
from tle.kcpc.platforms.atcoder.profile import (
    AtCoderProfile,
    AtCoderProfileClient,
    parse_profile,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'atcoder'
UNREADABLE = "AtCoder's profile page could not be read."
INVALID_NAME = "That isn't a valid AtCoder username."
LOGGER = 'tle.kcpc.platforms.atcoder.profile'

RATED = AtCoderProfile(
    handle='Kcpc_Example',
    rating=1834,
    highest_rating=1912,
    rated_matches=27,
    affiliation='Example University',
    color='blue',
    url='https://atcoder.jp/users/Kcpc_Example',
)
UNRATED = AtCoderProfile(
    handle='kcpc_newcomer',
    rating=None,
    highest_rating=None,
    rated_matches=0,
    affiliation=None,
    color='unrated',
    url='https://atcoder.jp/users/kcpc_newcomer',
)
# Its Affiliation cell has character references, runs of spaces, a tab and a
# line break around a link token.
AFFILIATED = AtCoderProfile(
    handle='Kcpc_Linker',
    rating=1012,
    highest_rating=1105,
    rated_matches=9,
    affiliation='Example\'s College & "KCPC+" kcpc-5e1f0a',
    color='green',
    url='https://atcoder.jp/users/Kcpc_Linker',
)
EXPECTED = {
    'profile_rated.html': RATED,
    'profile_unrated.html': UNRATED,
    'profile_affiliation.html': AFFILIATED,
}
NUMBER_FIELDS = {
    'Rating': 'rating',
    'Highest Rating': 'highest_rating',
    'Rated Matches': 'rated_matches',
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


def link(name: str = 'Kcpc_Example', color: str = 'blue') -> str:
    """A username link, marked up as AtCoder's are."""
    return (
        f'<a href="/users/{name}" class="username">'
        f'<span class="user-{color}">{name}</span></a>'
    )


def row(label: str, value: str) -> str:
    """A table row of a label and a value, marked up as AtCoder's are."""
    return f'<tr><th class="no-break">{label}</th><td>{value}</td></tr>\n'


def page(*rows: str, username: str | None = None) -> str:
    """A profile page: ``username`` (by default ``link()``), then ``rows``."""
    return (
        '<!DOCTYPE html>\n<html>\n<head>\n<title>Kcpc_Example - AtCoder</title>\n'
        '</head>\n<body>\n<h3>\n<b>2 Dan</b><br>\n'
        + (link() if username is None else username)
        + '\n</h3>\n<table class="dl-table">\n'
        + ''.join(rows)
        + '</table>\n</body>\n</html>\n'
    )


class TestTheFixturePages:
    @pytest.mark.parametrize(('name', 'expected'), EXPECTED.items(), ids=list(EXPECTED))
    def test_parse_to_exactly_these_profiles(
        self, name: str, expected: AtCoderProfile
    ) -> None:
        assert parse_profile(fixture(name)) == expected

    @pytest.mark.parametrize('name', EXPECTED)
    def test_are_marked_up_like_atcoders_pages(self, name: str) -> None:
        # Guards the fixtures: an editor or a git setting could quietly change
        # what the tests above read.
        data = (FIXTURES / name).read_bytes()
        assert b'\r' not in data
        assert (
            b'\n\t\t\t<img src="//img.atcoder.jp/assets/flag32/GB.png"> <a href='
            in data
        )
        assert b'" class="username"><span class="user-' in data
        assert b'\n\t\t\t<tr><th class="no-break">Country/Region</th><td><img ' in data
        assert b'\n\t\t\t<h3>Contest Status</h3>\n' in data
        assert b'<script>' in data  # scripts are no part of the profile

    def test_the_rated_pages_contest_rows_are_marked_up_like_atcoders(self) -> None:
        data = (FIXTURES / 'profile_rated.html').read_bytes()
        assert (
            b'\n\t\t\t\t\t\t<tr><th class="no-break">Rating</th><td>'
            b"<span class='user-blue'>1834</span>\n\t\t\t\t\t\t\t</td></tr>\n"
        ) in data
        assert b'<span class="gray">(&#43;88 to promote)</span>' in data
        assert b'<tr><th class="no-break">Rated Matches <span class=' in data

    def test_the_unrated_page_has_no_contest_rows(self) -> None:
        data = (FIXTURES / 'profile_unrated.html').read_bytes()
        assert b'This user has not competed in a rated contest yet.' in data
        assert b'Rating</th>' not in data
        assert b'Affiliation' not in data

    def test_the_affiliation_has_references_and_odd_whitespace(self) -> None:
        data = (FIXTURES / 'profile_affiliation.html').read_bytes()
        assert (
            b'<td class="break-all">  Example&#39;s College  &amp;\t&#34;KCPC&#43;'
            b'&#34;\n\t\t\t\tkcpc-5e1f0a  </td>'
        ) in data

    @pytest.mark.parametrize('name', EXPECTED)
    def test_crlf_line_endings_read_the_same(self, name: str) -> None:
        crlf = fixture(name).replace('\n', '\r\n')
        assert parse_profile(crlf) == EXPECTED[name]


class TestTheUsernameLink:
    def test_the_handle_keeps_the_pages_case(self) -> None:
        found = parse_profile(page(username=link('KCPC_example')))
        assert found.handle == 'KCPC_example'
        assert found.url == 'https://atcoder.jp/users/KCPC_example'

    def test_the_handle_is_the_links_text_as_shown(self) -> None:
        spaced = (
            '<a href="/users/Kcpc_Example" class="username">\n\t'
            '<span class="user-blue"> Kcpc<b>_</b>Example </span>\n</a>'
        )
        assert parse_profile(page(username=spaced)).handle == 'Kcpc_Example'

    @pytest.mark.parametrize(
        'color',
        [
            'gray',
            'brown',
            'green',
            'cyan',
            'blue',
            'yellow',
            'orange',
            'red',
            'unrated',
        ],
    )
    def test_the_color_is_the_class_of_the_span(self, color: str) -> None:
        assert parse_profile(page(username=link(color=color))).color == color

    @pytest.mark.parametrize(
        ('username', 'color'),
        [
            ('<a class="username user-red">Kcpc_Example</a>', 'red'),
            (
                '<a class="user-orange username"><span class="user-red">x</span></a>',
                'orange',
            ),
            (
                '<a class="username"><i class="bold user-cyan user-red">x</i></a>',
                'cyan',
            ),
            (
                '<a class="username"><b><span class="user-yellow">x</span></b></a>',
                'yellow',
            ),
        ],
        ids=['on-the-link', 'link-first', 'first-class', 'nested'],
    )
    def test_the_color_is_the_first_color_class_in_the_link(
        self, username: str, color: str
    ) -> None:
        username = username.replace('>x<', '>Kcpc_Example<')
        assert parse_profile(page(username=username)).color == color

    @pytest.mark.parametrize(
        'username',
        [
            '<a class="username"><span class="bold">Kcpc_Example</span></a>',
            '<a class="username">Kcpc_Example</a>',
            '<a class="username"><span class="user">Kcpc_Example</span></a>',
            '<a class="username"><span class="user-">Kcpc_Example</span></a>',
            '<a class="username"><span class="user-Red">Kcpc_Example</span></a>',
            '<a class="username"><span class="user-red2">Kcpc_Example</span></a>',
            '<span class="user-red"><a class="username">Kcpc_Example</a></span>',
            '<a class="username">Kcpc_Example</a><span class="user-red">!</span>',
        ],
        ids=[
            'other-class',
            'no-class',
            'user',
            'user-dash',
            'capital',
            'digit',
            'around-the-link',
            'after-the-link',
        ],
    )
    def test_without_a_color_class_in_the_link_there_is_no_color(
        self, username: str
    ) -> None:
        found = parse_profile(page(username=username))
        assert (found.handle, found.color) == ('Kcpc_Example', None)

    def test_only_the_first_username_link_counts(self) -> None:
        html = page(username=link('Kcpc_Example', 'blue') + link('Kcpc_Other', 'red'))
        found = parse_profile(html)
        assert (found.handle, found.color) == ('Kcpc_Example', 'blue')

    def test_links_without_the_username_class_are_passed_over(self) -> None:
        other = '<a href="/users/Kcpc_Other" class="user-red bold">Kcpc_Other</a> '
        found = parse_profile(page(username=other + link()))
        assert (found.handle, found.color) == ('Kcpc_Example', 'blue')

    @pytest.mark.parametrize(
        'username',
        [
            link().replace('"', "'"),
            link().replace('class="username"', 'class=username'),
            link().replace('class=', 'CLASS='),
            link().replace('class="username"', 'class="  username\tbold "'),
        ],
        ids=['single-quotes', 'unquoted', 'upper-case', 'spaced'],
    )
    def test_any_attribute_markup_reads_the_same(self, username: str) -> None:
        found = parse_profile(page(username=username))
        assert (found.handle, found.color) == ('Kcpc_Example', 'blue')


class TestTheRows:
    def test_ratings_and_matches_as_atcoder_marks_them_up(self) -> None:
        html = page(
            row('Rating', "<span class='user-blue'>1834</span>\n\t\t\t\t\t\t\t"),
            row(
                'Highest Rating',
                "<span class='user-blue'>1912</span>\n\t\t\t\t\t\t\t"
                '<span class="gray">―</span>\n\t\t\t\t\t\t\t'
                '<span class="bold">2 Dan</span>\n\t\t\t\t\t\t\t\t'
                '<span class="gray">(&#43;88 to promote)</span>\n\t\t\t\t\t\t',
            ),
            row('Rated Matches', '27'),
        )
        found = parse_profile(html)
        assert (found.rating, found.highest_rating, found.rated_matches) == (
            1834,
            1912,
            27,
        )

    @pytest.mark.parametrize('label', NUMBER_FIELDS)
    @pytest.mark.parametrize(
        ('value', 'number'),
        [
            ('1834', 1834),
            (' \n\t1834\n', 1834),
            ('0', 0),
            ('0042', 42),
            ('999999', 999999),
            ('2062 (Provisional)', 2062),
            ('4229 ― King (&#43;171 to promote)', 4229),
            ('<span class="user-red">4229</span>―King', 4229),
        ],
    )
    def test_a_number_is_the_one_that_starts_the_value(
        self, label: str, value: str, number: int
    ) -> None:
        found = parse_profile(page(row(label, value)))
        assert getattr(found, NUMBER_FIELDS[label]) == number

    def test_rows_that_are_left_out_mean_never_rated_and_no_affiliation(
        self,
    ) -> None:
        found = parse_profile(page(row('Country/Region', 'United Kingdom')))
        assert found == AtCoderProfile(
            handle='Kcpc_Example',
            rating=None,
            highest_rating=None,
            rated_matches=0,
            affiliation=None,
            color='blue',
            url='https://atcoder.jp/users/Kcpc_Example',
        )

    @pytest.mark.parametrize('label', NUMBER_FIELDS)
    @pytest.mark.parametrize(
        'value',
        ['', '-', 'N/A', 'Rating 1834', '-5', '1234567', '１８３４'],
        ids=['empty', 'dash', 'text', 'text-first', 'negative', 'long', 'full-width'],
    )
    def test_a_number_row_that_does_not_start_with_one_is_unreadable(
        self, label: str, value: str
    ) -> None:
        # The layout must have changed: reading it as never rated would make a
        # member look unrated, or their matches none.
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_profile(page(row(label, value)))

    @pytest.mark.parametrize(
        ('value', 'shown'),
        [
            ('Example University', 'Example University'),
            ('  Example \n\t University  ', 'Example University'),
            (
                'Example &amp; Co. &lt;KCPC&gt; &#34;A&#34; &#39;B&#39; &#43;1 &quot;C',
                'Example & Co. <KCPC> "A" \'B\' +1 "C',
            ),
            ('<a href="https://example.com/">Example</a> Club', 'Example Club'),
            ('Example&nbsp;University', 'Example\xa0University'),
            ('サンプル　kcpc-5e1f0a', 'サンプル　kcpc-5e1f0a'),
            ('', None),
            (' \n\t ', None),
        ],
        ids=[
            'plain',
            'whitespace',
            'references',
            'markup',
            'no-break-space',
            'ideographic-space',
            'empty',
            'blank',
        ],
    )
    def test_the_affiliation_as_shown(self, value: str, shown: str | None) -> None:
        # Only HTML's whitespace collapses, as in a browser: a no-break space
        # or an ideographic space stays as it is.
        assert parse_profile(page(row('Affiliation', value))).affiliation == shown

    def test_labels_are_read_as_shown(self) -> None:
        label = (
            '\n\tRated  Matches <span class="glyphicon glyphicon-question-sign" '
            'data-toggle="tooltip" title="Counts only rated contests"></span>'
        )
        assert parse_profile(page(row(label, '27'))).rated_matches == 27

    def test_the_first_row_with_a_label_counts(self) -> None:
        found = parse_profile(page(row('Rating', '1834'), row('Rating', '2400')))
        assert found.rating == 1834

    @pytest.mark.parametrize(
        'markup',
        [
            '<tr><th>Rating</th></tr>',
            '<tr><td>Rating</td><td>1834</td></tr>',
            '<tr><th>Rating</th><th>1834</th></tr>',
        ],
        ids=['no-value', 'no-label', 'heading'],
    )
    def test_a_row_needs_a_th_and_a_td(self, markup: str) -> None:
        assert parse_profile(page(markup)).rating is None

    def test_only_the_first_th_and_td_of_a_row_count(self) -> None:
        html = page(
            '<tr><th>Rating</th><td>1834</td><td>2400</td></tr>',
            '<tr><th>Rated Matches</th><th>Highest Rating</th><td>27</td></tr>',
        )
        found = parse_profile(html)
        assert (found.rating, found.highest_rating, found.rated_matches) == (
            1834,
            None,
            27,
        )

    def test_end_tags_that_html_lets_pages_leave_out(self) -> None:
        html = page(
            '<tr><th>Rating<td>1834',
            '<tr><th>Rated Matches<td>27\n</table><table>',
            '<tr><th>Affiliation<td>Example University',
        )
        found = parse_profile(html)
        assert (found.rating, found.rated_matches, found.affiliation) == (
            1834,
            27,
            'Example University',
        )

    def test_rows_are_read_in_any_table(self) -> None:
        html = page(username=link()).replace(
            '</body>',
            '<div class="new-layout"><div><table><tbody>'
            + row('Rating', '1834')
            + '</tbody></table></div></div></body>',
        )
        assert parse_profile(html).rating == 1834

    def test_scripts_and_comments_hold_no_rows(self) -> None:
        html = page(
            "<script>document.write('<tr><th>Rating</th><td>2400</td></tr>');</script>",
            '<!-- <tr><th>Rated Matches</th><td>99</td></tr> -->',
            row('Rating', '1834'),
        )
        found = parse_profile(html)
        assert (found.rating, found.rated_matches) == (1834, 0)


class TestPagesThatAreNotProfiles:
    @pytest.mark.parametrize(
        'html',
        [
            '',
            '\n\n',
            'Not Found',
            '{"handle": "Kcpc_Example", "rating": 1834}',
            '<!DOCTYPE html>\n<html><head><title>404 Not Found - AtCoder</title></head>'
            '<body><h1>404 Not Found</h1></body></html>\n',
            '<!DOCTYPE html>\n<html><head><title>Maintenance - AtCoder</title></head>'
            '<body><p>AtCoder is under maintenance.</p></body></html>\n',
            fixture('contests.html'),
            page(row('Rating', '1834'), username=''),
            page(username='<a href="/users/Kcpc_Example">Kcpc_Example</a>'),
            page(username='<span class="username">Kcpc_Example</span>'),
            page(username='<!-- <a class="username">Kcpc_Example</a> -->'),
            page(username='<script>"<a class=username>Kcpc_Example</a>"</script>'),
            page(username='<a class="username"></a>'),
            page(username='<a class="username">Kcpc_Example'),
        ],
        ids=[
            'empty',
            'blank',
            'text',
            'json',
            'not-found',
            'maintenance',
            'contest-list',
            'no-link',
            'no-class',
            'not-a-link',
            'comment',
            'script',
            'empty-link',
            'unclosed-link',
        ],
    )
    def test_are_unreadable(self, html: str) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse_profile(html)
        assert (excinfo.value.service, excinfo.value.status) == ('AtCoder', None)

    @pytest.mark.parametrize(
        'name',
        ['ab', 'x' * 17, 'Kcpc Example', 'kcpc-example', 'Kcpc_Example!', 'naïve'],
        ids=['short', 'long', 'space', 'dash', 'punctuation', 'accent'],
    )
    def test_a_username_link_without_a_username_is_unreadable(self, name: str) -> None:
        with pytest.raises(ExternalServiceError, match=UNREADABLE):
            parse_profile(page(username=link(name)))

    def test_are_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        for html in ('Not Found', page(username=link('ab')), page(row('Rating', '-'))):
            with pytest.raises(ExternalServiceError):
                parse_profile(html)
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * 3


class FakeAtCoder:
    """A local stand-in for AtCoder's profile pages, at ``profile_url``.

    It answers every request with the reply set by ``reply``, a 404 until
    then, and records the path and query of each.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._status = 404
        self._body = b''
        self._content_type = 'text/html; charset=utf-8'
        app = web.Application()
        app.router.add_get('/users/{handle}', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def profile_url(self) -> str:
        """The site's ``PROFILE_URL``, with a {handle} field."""
        return str(self._server.make_url('/users/')) + '{handle}'

    def reply(
        self,
        body: bytes,
        *,
        content_type: str = 'text/html; charset=utf-8',
        status: int = 200,
    ) -> None:
        self._status, self._body, self._content_type = status, body, content_type

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        return web.Response(
            status=self._status,
            body=self._body,
            headers={'Content-Type': self._content_type},
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeAtCoder]:
    site = FakeAtCoder()
    await site.start()
    monkeypatch.setattr(profile, 'PROFILE_URL', site.profile_url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[AtCoderProfileClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield AtCoderProfileClient(http)
    await http.close()


class TestFetch:
    async def test_reads_the_profile_page_in_english(
        self, site: FakeAtCoder, client: AtCoderProfileClient
    ) -> None:
        site.reply((FIXTURES / 'profile_rated.html').read_bytes())

        found = await client.fetch('Kcpc_Example')

        # The profile links to the page it was read from.
        page_url = site.profile_url.format(handle='Kcpc_Example')
        assert found == dataclasses.replace(RATED, url=page_url)
        assert site.requests == [('/users/Kcpc_Example', {'lang': 'en'})]

    async def test_asks_by_the_name_as_given_and_keeps_the_pages_case(
        self, site: FakeAtCoder, client: AtCoderProfileClient
    ) -> None:
        site.reply((FIXTURES / 'profile_rated.html').read_bytes())

        found = await client.fetch('KCPC_EXAMPLE')

        assert found is not None
        assert found.handle == 'Kcpc_Example'
        assert found.url == site.profile_url.format(handle='Kcpc_Example')
        assert site.requests == [('/users/KCPC_EXAMPLE', {'lang': 'en'})]

    async def test_404_means_no_such_user(
        self, site: FakeAtCoder, client: AtCoderProfileClient
    ) -> None:
        site.reply(b'<html><body><h1>404 Not Found</h1></body></html>', status=404)

        assert await client.fetch('kcpc_nobody') is None
        assert site.requests == [('/users/kcpc_nobody', {'lang': 'en'})]

    @pytest.mark.parametrize(
        'name',
        [
            '',
            'ab',
            'x' * 17,
            'Kcpc Example',
            'kcpc-example',
            'kcpc.example',
            '../contests',
            'Kcpc_Example?lang=ja',
            ' Kcpc_Example',
            'Kcpc_Example\n',
            'naïve_user',
            'Ｋcpc_Example',  # a full-width K
        ],
        ids=[
            'empty',
            'short',
            'long',
            'space',
            'dash',
            'dot',
            'path',
            'query',
            'leading-space',
            'newline',
            'accent',
            'full-width',
        ],
    )
    async def test_a_name_that_cant_be_an_atcoder_username_is_refused_unasked(
        self, site: FakeAtCoder, client: AtCoderProfileClient, name: str
    ) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            await client.fetch(name)

        assert str(excinfo.value) == INVALID_NAME
        assert not isinstance(excinfo.value, ExternalServiceError)
        assert site.requests == []

    @pytest.mark.parametrize('name', ['abc', '_1_', 'x' * 16, 'Kcpc_Example_016'])
    async def test_names_of_3_to_16_letters_digits_and_underscores_are_asked_about(
        self, site: FakeAtCoder, client: AtCoderProfileClient, name: str
    ) -> None:
        assert await client.fetch(name) is None  # the fake site's 404
        assert site.requests == [(f'/users/{name}', {'lang': 'en'})]

    async def test_an_affiliation_in_japanese(
        self, site: FakeAtCoder, client: AtCoderProfileClient
    ) -> None:
        affiliation = 'サンプル大学 kcpc-5e1f0a'  # Sample Univ.
        site.reply(page(row('Affiliation', affiliation)).encode('utf-8'))

        found = await client.fetch('Kcpc_Example')

        assert found is not None and found.affiliation == affiliation

    async def test_a_page_that_is_not_a_profile_is_refused(
        self, site: FakeAtCoder, client: AtCoderProfileClient
    ) -> None:
        site.reply(b'<html><body>AtCoder is under maintenance.</body></html>')

        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            await client.fetch('Kcpc_Example')
        assert excinfo.value.service == 'AtCoder'

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
        client: AtCoderProfileClient,
        status: int,
        message: str,
    ) -> None:
        site.reply(b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch('Kcpc_Example')
        assert (excinfo.value.service, excinfo.value.status) == ('AtCoder', status)
