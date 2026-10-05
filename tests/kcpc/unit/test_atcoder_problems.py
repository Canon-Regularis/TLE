"""Tests for tle.kcpc.platforms.atcoder.problems: AtCoder Problems' problem
list and users' submissions.

fixtures/kenkoooo/*.json are entries copied from AtCoder Problems' files of
2026-10-03, written as compactly as kenkoooo.com writes them, one to a line.
They were picked for their quirks: problems that Daily Training rounds reused
(so problems.json names another contest and letter), names that end in a
space, the letter 'Ex', an ARC whose letters start at C, experimental and
missing models, a Heuristic contest's problem, and problems that no contest's
ID starts. The submissions are of a made-up user.
"""

import json
import logging
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HostPolicy, HttpClient
from tle.kcpc.platforms.atcoder import problems
from tle.kcpc.platforms.atcoder.problems import (
    SUBMISSIONS_PAGE,
    AtCoderProblem,
    AtCoderProblemsClient,
    AtCoderSubmission,
    parse_problem_set,
)

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'kenkoooo'
UNREADABLE = "AtCoder Problems' problem list could not be read."
UNREADABLE_SUBMISSIONS = "AtCoder Problems' list of submissions could not be read."
INVALID_NAME = "That isn't a valid AtCoder username."
LOGGER = 'tle.kcpc.platforms.atcoder.problems'
PROBLEMS = 'problems.json'
MODELS = 'problem-models.json'
PAIRS = 'contest-problem.json'


def problem(
    problem_id: str,
    contest_id: str,
    index: str,
    name: str,
    difficulty: int | None = None,
) -> AtCoderProblem:
    return AtCoderProblem(
        problem_id=problem_id,
        contest_id=contest_id,
        index=index,
        name=name,
        difficulty=difficulty,
    )


EXPECTED = {
    'abc477_a': problem('abc477_a', 'abc477', 'A', 'Traffic Light', 17),
    'abc477_g': problem('abc477_g', 'abc477', 'G', 'Frequency Query on Tree', 2226),
    # problems.json has it in a Daily Training round, as its B.
    'abc300_a': problem('abc300_a', 'abc300', 'A', 'N-choice question', 8),
    'abc212_d': problem('abc212_d', 'abc212', 'D', 'Querying Multiset', 775),
    # ARC058 shared its problems with ABC042, so its letters start at C.
    'arc058_a': problem('arc058_a', 'arc058', 'C', "Iroha's Obsession", 1174),
    # No contest's ID starts the problem's: problems.json's contest it is.
    'cf17_final_a': problem('cf17_final_a', 'cf17-final', 'A', 'AKIBA', 439),
    'agc010_a': problem('agc010_a', 'agc010', 'A', 'Addition', 0),
    'agc078_a': problem('agc078_a', 'agc078', 'A', 'Rearrange ABC', 3423),
    'abc233_h': problem('abc233_h', 'abc233', 'Ex', 'Manhattan Christmas Tree', 2530),
    # Experimental models.
    'abc001_1': problem('abc001_1', 'abc001', 'A', '積雪深差'),
    'abc007_3': problem('abc007_3', 'abc007', 'C', '幅優先探索'),
    'abc049_a': problem('abc049_a', 'abc049', 'A', 'UOIAUAI'),  # no difficulty
    'ahc055_a': problem('ahc055_a', 'ahc055', 'A', 'Weakpoint', 3884),
    'joisc2012_joi_flag': problem(
        'joisc2012_joi_flag', 'joisc2012', 'joi_flag', '日本情報オリンピック旗'
    ),
    '1202Contest_a': problem(
        '1202Contest_a', 'DEGwer2023', 'A', "DEGwer's Doctoral Dissertation"
    ),
    'cf16_exhibition_final_b': problem(
        'cf16_exhibition_final_b', 'cf16-exhibition-final', 'B', 'Inscribed Bicycle'
    ),
    'bitflyer2018_qual_a': problem(
        'bitflyer2018_qual_a', 'bitflyer2018-qual', 'A', '本選参加者数'
    ),
}
IN_POOL = [
    'abc477_a',
    'abc477_g',
    'abc300_a',
    'abc212_d',
    'arc058_a',
    'agc010_a',
    'agc078_a',
    'abc233_h',
]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture(name))


def encoded(data: object) -> bytes:
    return json.dumps(data).encode()


def parse(
    *,
    problem_rows: object = None,
    models: object = None,
    pairs: object = None,
) -> dict[str, AtCoderProblem]:
    """``parse_problem_set`` of the fixture files, any of them replaced."""
    return parse_problem_set(
        fixture(PROBLEMS) if problem_rows is None else encoded(problem_rows),
        fixture(MODELS) if models is None else encoded(models),
        fixture(PAIRS) if pairs is None else encoded(pairs),
    )


def problem_row(
    problem_id: str, contest_id: str, index: str, name: str
) -> dict[str, str]:
    """A row of problems.json."""
    return {
        'id': problem_id,
        'contest_id': contest_id,
        'problem_index': index,
        'name': name,
        'title': f'{index}. {name}',
    }


def pair(contest_id: str, problem_id: str, index: str) -> dict[str, str]:
    """A row of contest-problem.json."""
    return {'contest_id': contest_id, 'problem_id': problem_id, 'problem_index': index}


def parse_one(
    row: dict[str, str],
    pairs: list[dict[str, str]],
    model: dict[str, object] | None = None,
) -> AtCoderProblem:
    """The problem of ``row``, read with ``pairs`` and ``model``."""
    models = {} if model is None else {row['id']: model}
    [found] = parse(problem_rows=[row], models=models, pairs=pairs).values()
    return found


class TestTheFixtureFiles:
    def test_parse_to_exactly_these_problems(self) -> None:
        found = parse()
        assert found == EXPECTED
        assert list(found) == list(EXPECTED), 'in the order of problems.json'

    def test_are_written_like_atcoder_problems_files(self) -> None:
        # Guards the fixtures: an editor could quietly change what the tests
        # above read.
        assert fixture(PROBLEMS).startswith(b'[\n{"id":"abc477_a","contest_id":')
        assert (
            b'{"id":"abc300_a","contest_id":"adt_easy_20260826_2",'
            b'"problem_index":"B","name":"N-choice question",'
        ) in fixture(PROBLEMS)
        assert b'"name":"Querying Multiset ",' in fixture(PROBLEMS)
        assert b'{"contest_id":"abc233","problem_id":"abc233_h",' in fixture(PAIRS)
        assert b'"is_experimental":true}' in fixture(MODELS)
        assert b'"cf16_exhibition_final_b":{"is_experimental":false}' in fixture(MODELS)

    def test_problems_json_rows_have_five_text_fields(self) -> None:
        for row in fixture_json(PROBLEMS):
            assert sorted(row) == ['contest_id', 'id', 'name', 'problem_index', 'title']
            assert all(isinstance(value, str) for value in row.values())


class TestContestsAndLetters:
    def test_a_problem_that_daily_training_reused_keeps_its_contest(self) -> None:
        found = parse()['abc300_a']

        assert (found.contest_id, found.index) == ('abc300', 'A')
        assert found.title == 'ABC300 A - N-choice question'
        assert found.url == 'https://atcoder.jp/contests/abc300/tasks/abc300_a'
        assert found.contest_url == 'https://atcoder.jp/contests/abc300'

    @pytest.mark.parametrize(
        ('problem_id', 'index'),
        [
            ('abc212_d', 'D'),  # G in the Daily Training rounds
            ('abc233_h', 'Ex'),
            ('arc058_a', 'C'),  # C in ABC042 as well
            ('abc007_3', 'C'),  # A in the contest problems.json gives
            ('joisc2012_joi_flag', 'joi_flag'),
        ],
    )
    def test_the_letter_is_the_one_in_its_contest_as_stored(
        self, problem_id: str, index: str
    ) -> None:
        assert parse()[problem_id].index == index

    def test_the_longest_contest_that_starts_the_id_wins(self) -> None:
        row = problem_row('jsc2026_final_a', 'adt_all_20261001_1', 'D', 'Example')
        pairs = [
            pair('adt_all_20261001_1', 'jsc2026_final_a', 'D'),
            pair('jsc2026', 'jsc2026_final_a', 'X'),
            pair('jsc2026_final', 'jsc2026_final_a', 'A'),
        ]

        found = parse_one(row, pairs)

        assert (found.contest_id, found.index) == ('jsc2026_final', 'A')

    def test_a_contest_starts_the_id_only_with_an_underscore_after_it(self) -> None:
        row = problem_row('abc3000_a', 'abc3000', 'A', 'Example')
        pairs = [pair('xyz3000', 'abc3000_a', 'A'), pair('abc300', 'abc3000_a', 'X')]

        found = parse_one(row, pairs)

        # Not abc300's, but the first of its contests, as none starts its ID.
        assert (found.contest_id, found.index) == ('xyz3000', 'A')

    @pytest.mark.parametrize(
        ('problem_id', 'contest_id'),
        [('cf17_final_a', 'cf17-final'), ('1202Contest_a', 'DEGwer2023')],
    )
    def test_else_the_contest_is_the_one_problems_json_gives(
        self, problem_id: str, contest_id: str
    ) -> None:
        found = parse()[problem_id]
        assert (found.contest_id, found.index) == (contest_id, 'A')

    def test_else_the_contest_is_the_first_that_isnt_a_daily_training_round(
        self,
    ) -> None:
        # problems.json gives one of the Daily Training rounds that reused it.
        row = problem_row(
            'codequeen2024_final_a', 'adt_easy_20241002_2', 'B', 'Example'
        )
        pairs = [
            pair('adt_all_20241002_2', 'codequeen2024_final_a', 'B'),
            pair('adt_easy_20241002_2', 'codequeen2024_final_a', 'B'),
            pair('codequeen2024-final-N9tn8QqD', 'codequeen2024_final_a', 'A'),
        ]

        found = parse_one(row, pairs)

        assert (found.contest_id, found.index) == ('codequeen2024-final-N9tn8QqD', 'A')
        assert found.title == 'CODEQUEEN2024-FINAL-N9TN8QQD A - Example'

    def test_a_contest_comes_before_its_open_mirror(self) -> None:
        row = problem_row('masters2025_final_a', 'masters2025-final-open', 'A', 'Ex')
        pairs = [
            pair('masters2025-final', 'masters2025_final_a', 'A'),
            pair('masters2025-final-open', 'masters2025_final_a', 'A'),
        ]

        assert parse_one(row, pairs).contest_id == 'masters2025-final'

    def test_with_only_daily_training_rounds_problems_jsons_contest_it_is(
        self,
    ) -> None:
        row = problem_row('xyz_a', 'adt_all_20241002_2', 'B', 'Example')
        pairs = [pair('adt_all_20241002_2', 'xyz_a', 'B')]

        found = parse_one(row, pairs)

        assert (found.contest_id, found.index) == ('adt_all_20241002_2', 'B')

    def test_without_a_pair_for_its_contest_the_letter_is_problems_jsons(
        self,
    ) -> None:
        row = problem_row('abc479_a', 'abc479', 'A', 'Example')
        assert parse_one(row, []) == problem('abc479_a', 'abc479', 'A', 'Example')

    def test_names_are_trimmed(self) -> None:
        found = parse()
        assert found['abc212_d'].name == 'Querying Multiset'
        assert found['abc233_h'].title == 'ABC233 Ex - Manhattan Christmas Tree'

    def test_titles_use_the_contest_id_in_capitals(self) -> None:
        found = parse()
        assert found['arc058_a'].title == "ARC058 C - Iroha's Obsession"
        assert found['cf17_final_a'].title == 'CF17-FINAL A - AKIBA'


class TestDifficulties:
    @pytest.mark.parametrize(
        ('problem_id', 'difficulty', 'rating'),
        [
            ('abc477_a', 17, 724),  # -870 in the model
            ('agc010_a', 0, 711),  # -10000
            ('abc300_a', 8, 717),  # -1147
            ('arc058_a', 1174, 1599),
            ('abc477_g', 2226, 2395),
        ],
    )
    def test_are_the_models_clipped(
        self, problem_id: str, difficulty: int, rating: int
    ) -> None:
        found = parse()[problem_id]
        assert (found.difficulty, found.rating) == (difficulty, rating)

    @pytest.mark.parametrize('problem_id', ['abc001_1', 'abc007_3'])
    def test_experimental_models_give_none(self, problem_id: str) -> None:
        found = parse()[problem_id]
        assert (found.difficulty, found.rating) == (None, None)

    @pytest.mark.parametrize(
        'problem_id',
        [
            'abc049_a',
            'cf16_exhibition_final_b',
            'bitflyer2018_qual_a',
            'joisc2012_joi_flag',
            '1202Contest_a',
        ],
        ids=['no-difficulty', 'flag-only', 'legacy-model', 'no-model', 'no-model-2'],
    )
    def test_models_without_a_difficulty_and_no_model_give_none(
        self, problem_id: str
    ) -> None:
        assert parse()[problem_id].difficulty is None

    @pytest.mark.parametrize(
        ('model', 'difficulty'),
        [
            ({'difficulty': 1000}, 1000),
            ({'difficulty': 1000, 'is_experimental': False}, 1000),
            ({'difficulty': 1000, 'is_experimental': None}, 1000),
            ({'difficulty': 1000, 'is_experimental': True}, None),
            ({'difficulty': None, 'is_experimental': False}, None),
            ({'difficulty': -1147, 'slope': -0.00075}, 8),
            ({}, None),
        ],
        ids=[
            'no-flag',
            'not-experimental',
            'null-flag',
            'experimental',
            'null-difficulty',
            'clipped',
            'empty',
        ],
    )
    def test_the_models_fields(
        self, model: dict[str, object], difficulty: int | None
    ) -> None:
        row = problem_row('abc479_a', 'abc479', 'A', 'Example')
        assert parse_one(row, [], model).difficulty == difficulty


class TestThePool:
    @pytest.mark.parametrize('problem_id', IN_POOL)
    def test_has_abc_arc_and_agc_problems_with_a_difficulty(
        self, problem_id: str
    ) -> None:
        assert parse()[problem_id].in_pool

    def test_has_nothing_else_from_the_fixtures(self) -> None:
        pool = [found.problem_id for found in parse().values() if found.in_pool]
        assert pool == IN_POOL

    def test_leaves_out_heuristic_contests_whose_problems_have_difficulties(
        self,
    ) -> None:
        found = parse()['ahc055_a']
        assert found.difficulty == 3884
        assert not found.in_pool

    @pytest.mark.parametrize(
        ('contest_id', 'in_pool'),
        [
            ('abc300', True),
            ('arc058', True),
            ('agc078', True),
            ('ahc055', False),
            ('awc0170', False),
            ('abc30', False),
            ('abc3000', False),
            ('ABC300', False),
            ('xabc300', False),
            ('abc300x', False),
            ('adt_all_20231220_2', False),
            ('abc３００', False),  # full-width digits
        ],
    )
    def test_takes_contest_ids_of_abc_arc_or_agc_and_three_digits(
        self, contest_id: str, in_pool: bool
    ) -> None:
        found = problem(f'{contest_id}_a', contest_id, 'A', 'Example', 1000)
        assert found.in_pool is in_pool

    def test_needs_a_difficulty(self) -> None:
        assert not problem('abc300_a', 'abc300', 'A', 'Example').in_pool


class TestAtCoderProblem:
    def test_links_its_page_and_its_contests(self) -> None:
        found = problem('cf17_final_a', 'cf17-final', 'A', 'AKIBA', 439)
        assert found.url == 'https://atcoder.jp/contests/cf17-final/tasks/cf17_final_a'
        assert found.contest_url == 'https://atcoder.jp/contests/cf17-final'

    def test_has_no_rating_without_a_difficulty(self) -> None:
        assert problem('abc049_a', 'abc049', 'A', 'UOIAUAI').rating is None


class TestEntriesOfOtherProblems:
    def test_models_and_pairs_of_problems_not_listed_are_ignored(self) -> None:
        models = {
            **fixture_json(MODELS),
            'ahc013_b': {'difficulty': 1500, 'is_experimental': False},
            'ahc014_b': ['not', 'a', 'model'],
        }
        pairs = [*fixture_json(PAIRS), pair('ahc013', 'ahc013_b', 'B')]

        assert parse(models=models, pairs=pairs) == EXPECTED

    def test_a_second_row_for_a_problem_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        rows = [
            problem_row('abc479_a', 'abc479', 'A', 'First'),
            problem_row('abc479_a', 'abc479', 'A', 'Second'),
        ]

        found = parse(problem_rows=rows, models={}, pairs=[])

        assert [p.name for p in found.values()] == ['First']
        assert [r.levelno for r in caplog.records if r.name == LOGGER] == [
            logging.DEBUG
        ]

    def test_a_second_pair_for_a_contest_is_ignored(self) -> None:
        row = problem_row('abc479_a', 'abc479', 'A', 'Example')
        pairs = [pair('abc479', 'abc479_a', 'A'), pair('abc479', 'abc479_a', 'Z')]
        assert parse_one(row, pairs).index == 'A'


ROW = problem_row('abc479_a', 'abc479', 'A', 'Example')
UNREADABLE_FILES: dict[str, dict[str, object]] = {
    # problems.json
    'problems-empty': {'problem_rows': b''},
    'problems-html': {'problem_rows': b'<html><body>Not Found</body></html>'},
    'problems-cut-off': {'problem_rows': b'[{"id":"abc479_a",'},
    'problems-not-utf-8': {'problem_rows': b'["\xff\xfe"]'},
    'problems-object': {'problem_rows': {'abc479_a': ROW}},
    'problems-null': {'problem_rows': None},
    'row-list': {'problem_rows': [list(ROW.values())]},
    'row-text': {'problem_rows': ['abc479_a']},
    'row-null': {'problem_rows': [None]},
    'id-number': {'problem_rows': [{**ROW, 'id': 479}]},
    'name-null': {'problem_rows': [{**ROW, 'name': None}]},
    'no-name': {'problem_rows': [{k: v for k, v in ROW.items() if k != 'name'}]},
    'no-contest': {
        'problem_rows': [{k: v for k, v in ROW.items() if k != 'contest_id'}]
    },
    'no-index': {
        'problem_rows': [{k: v for k, v in ROW.items() if k != 'problem_index'}]
    },
    # problem-models.json
    'models-html': {'models': b'<html></html>'},
    'models-list': {'models': [{'abc479_a': {'difficulty': 1000}}]},
    'models-null': {'models': None},
    'model-list': {'models': {'abc479_a': [1000]}},
    'model-number': {'models': {'abc479_a': 1000}},
    'difficulty-text': {'models': {'abc479_a': {'difficulty': '1000'}}},
    'difficulty-float': {'models': {'abc479_a': {'difficulty': 1000.5}}},
    'difficulty-bool': {'models': {'abc479_a': {'difficulty': True}}},
    'experimental-text': {
        'models': {'abc479_a': {'difficulty': 1000, 'is_experimental': 'false'}}
    },
    'experimental-number': {
        'models': {'abc479_a': {'difficulty': 1000, 'is_experimental': 0}}
    },
    # contest-problem.json
    'pairs-html': {'pairs': b'<html></html>'},
    'pairs-object': {'pairs': {'abc479': ['abc479_a']}},
    'pair-list': {'pairs': [['abc479', 'abc479_a', 'A']]},
    'pair-index-number': {
        'pairs': [{**pair('abc479', 'abc479_a', 'A'), 'problem_index': 1}]
    },
    'pair-no-problem': {'pairs': [{'contest_id': 'abc479', 'problem_index': 'A'}]},
    'pair-of-other-problem': {
        'pairs': [{**pair('ahc013', 'ahc013_b', 'B'), 'contest_id': 13}]
    },
}


def unreadable(case: dict[str, object]) -> tuple[bytes, bytes, bytes]:
    """The three files of one problem, with one replaced by ``case``'s: bytes
    as they are, anything else as JSON.
    """
    files: dict[str, object] = {
        'problem_rows': [ROW],
        'models': {'abc479_a': {'difficulty': 1000, 'is_experimental': False}},
        'pairs': [pair('abc479', 'abc479_a', 'A')],
        **case,
    }

    def body(name: str) -> bytes:
        value = files[name]
        return value if isinstance(value, bytes) else encoded(value)

    return body('problem_rows'), body('models'), body('pairs')


class TestUnreadableFiles:
    def test_the_one_problem_reads_when_nothing_is_replaced(self) -> None:
        assert parse_problem_set(*unreadable({})) == {
            'abc479_a': problem('abc479_a', 'abc479', 'A', 'Example', 1000)
        }

    @pytest.mark.parametrize(
        'case', UNREADABLE_FILES.values(), ids=list(UNREADABLE_FILES)
    )
    def test_are_refused(self, case: dict[str, object]) -> None:
        # Kept from changing a good problem list: a list that can't be read
        # all through isn't read at all.
        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            parse_problem_set(*unreadable(case))
        assert (excinfo.value.service, excinfo.value.status) == (
            'AtCoder Problems',
            None,
        )

    def test_are_logged_at_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG, logger=LOGGER)
        for case in UNREADABLE_FILES.values():
            with pytest.raises(ExternalServiceError):
                parse_problem_set(*unreadable(case))
        levels = [r.levelno for r in caplog.records if r.name == LOGGER]
        assert levels == [logging.DEBUG] * len(UNREADABLE_FILES)


def submission_row(
    submission_id: int,
    epoch_second: int,
    problem_id: str = 'abc300_a',
    result: str = 'AC',
) -> dict[str, object]:
    """An entry of the submissions API's list."""
    return {
        'id': submission_id,
        'epoch_second': epoch_second,
        'problem_id': problem_id,
        'contest_id': problem_id.rpartition('_')[0],
        'user_id': 'fake_user',
        'language': 'C++ 23 (gcc 12.2)',
        'point': 100.0,
        'length': 1024,
        'result': result,
        'execution_time': 1,
    }


EXPECTED_SUBMISSIONS = [
    AtCoderSubmission(37190, 1344004808, 'tenka1_2012_qualA_4', 'AC'),
    AtCoderSubmission(37709, 1344512948, 'arc001_2', 'WA'),
    AtCoderSubmission(57571, 1354424215, 'utpc2012_05', 'TLE'),
    AtCoderSubmission(119935, 1386245020, 'tricky_1', 'CE'),
    AtCoderSubmission(70292381, 1760873378, 'ahc055_a', 'AC'),
    # Submitted in a Daily Training round, to ABC351's problem.
    AtCoderSubmission(70902531, 1763026818, 'abc351_a', 'AC'),
    AtCoderSubmission(74534717, 1774800218, 'agc077_a', 'AC'),
]


class FakeKenkoooo:
    """A local stand-in for AtCoder Problems: its files at ``resources_url``
    and its submissions API at ``submissions_url``.

    It answers each file, and 'submissions', with the reply set by ``reply``,
    a 404 until then, and records the path and query of each request.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._replies: dict[str, tuple[int, bytes]] = {}
        app = web.Application()
        app.router.add_get('/atcoder/resources/{name}', self._handle)
        app.router.add_get('/atcoder/atcoder-api/v3/user/submissions', self._handle)
        self._server = TestServer(app, host='127.0.0.1')

    @property
    def resources_url(self) -> str:
        return str(self._server.make_url('/atcoder/resources/'))

    @property
    def submissions_url(self) -> str:
        return str(self._server.make_url('/atcoder/atcoder-api/v3/user/submissions'))

    def reply(self, name: str, body: bytes, *, status: int = 200) -> None:
        self._replies[name] = (status, body)

    def reply_with_fixtures(self) -> None:
        for name in (PROBLEMS, MODELS, PAIRS):
            self.reply(name, fixture(name))

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.Response:
        self.requests.append((request.path, dict(request.query)))
        name = request.match_info.get('name', 'submissions')
        status, body = self._replies.get(name, (404, b''))
        return web.Response(
            status=status,
            body=body,
            headers={'Content-Type': 'application/json;charset=utf-8'},
        )


@pytest.fixture
async def site(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeKenkoooo]:
    site = FakeKenkoooo()
    await site.start()
    monkeypatch.setattr(problems, 'RESOURCES_URL', site.resources_url)
    monkeypatch.setattr(problems, 'SUBMISSIONS_URL', site.submissions_url)
    yield site
    await site.close()


@pytest.fixture
async def client(clock: FakeClock) -> AsyncIterator[AtCoderProblemsClient]:
    # One attempt and no pacing, so that nothing waits on the fake clock.
    http = HttpClient(
        user_agent='KCPC-bot-tests',
        clock=clock,
        policies={'127.0.0.1': HostPolicy(max_attempts=1)},
    )
    yield AtCoderProblemsClient(http)
    await http.close()


RESOURCES = '/atcoder/resources/'
SUBMISSIONS = '/atcoder/atcoder-api/v3/user/submissions'


class TestFetchProblemSet:
    async def test_fetches_the_three_files_in_turn(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        site.reply_with_fixtures()

        assert await client.fetch_problem_set() == EXPECTED
        assert site.requests == [
            (f'{RESOURCES}problems.json', {}),
            (f'{RESOURCES}problem-models.json', {}),
            (f'{RESOURCES}contest-problem.json', {}),
        ]

    async def test_reads_them_in_a_worker_thread(
        self,
        site: FakeKenkoooo,
        client: AtCoderProblemsClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site.reply_with_fixtures()
        threads: list[threading.Thread] = []

        def recording_parse(*files: bytes) -> dict[str, AtCoderProblem]:
            threads.append(threading.current_thread())
            return parse_problem_set(*files)

        monkeypatch.setattr(problems, 'parse_problem_set', recording_parse)

        assert await client.fetch_problem_set() == EXPECTED
        assert len(threads) == 1
        assert threads[0] is not threading.current_thread()

    async def test_a_file_that_cannot_be_read_is_refused(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        site.reply_with_fixtures()
        site.reply(MODELS, b'<html><body>Maintenance</body></html>')

        with pytest.raises(ExternalServiceError, match=UNREADABLE) as excinfo:
            await client.fetch_problem_set()
        assert excinfo.value.service == 'AtCoder Problems'

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (403, r'^AtCoder Problems returned an error \(HTTP 403\)\.$'),
            (404, r'^AtCoder Problems returned an error \(HTTP 404\)\.$'),
            (503, '^AtCoder Problems is not responding right now'),
        ],
    )
    async def test_failures_name_atcoder_problems_and_stop_the_fetch(
        self,
        site: FakeKenkoooo,
        client: AtCoderProblemsClient,
        status: int,
        message: str,
    ) -> None:
        site.reply_with_fixtures()
        site.reply(MODELS, b'', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch_problem_set()
        assert (excinfo.value.service, excinfo.value.status) == (
            'AtCoder Problems',
            status,
        )
        assert [path for path, _ in site.requests] == [
            f'{RESOURCES}problems.json',
            f'{RESOURCES}problem-models.json',
        ]


class TestFetchSubmissions:
    async def test_reads_a_page_of_submissions(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        site.reply('submissions', fixture('user_submissions.json'))

        assert await client.fetch_submissions('fake_user', 0) == EXPECTED_SUBMISSIONS
        assert site.requests == [
            (SUBMISSIONS, {'user': 'fake_user', 'from_second': '0'})
        ]

    async def test_asks_from_the_second_given_for_the_user_as_given(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        # AtCoder Problems finds users whatever the case, and lists the
        # submissions of that second too.
        site.reply('submissions', b'[]')

        await client.fetch_submissions('Fake_User', 1471003949)

        assert site.requests == [
            (SUBMISSIONS, {'user': 'Fake_User', 'from_second': '1471003949'})
        ]

    async def test_reads_a_full_page(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        rows = [submission_row(1000 + n, 1700000000 + n // 3) for n in range(500)]
        site.reply('submissions', encoded(rows))

        found = await client.fetch_submissions('fake_user', 1700000000)

        assert SUBMISSIONS_PAGE == 500
        assert len(found) == SUBMISSIONS_PAGE
        assert found[-1] == AtCoderSubmission(1499, 1700000166, 'abc300_a', 'AC')

    async def test_an_unknown_user_has_none(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        site.reply('submissions', b'[]')
        assert await client.fetch_submissions('kcpc_nobody_0000', 0) == []

    async def test_come_oldest_first(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient
    ) -> None:
        # Those of one second keep the API's order, which isn't by ID.
        rows = [
            submission_row(30, 1700000002),
            submission_row(12, 1700000001),
            submission_row(11, 1700000001),
            submission_row(20, 1700000000, result='WA'),
        ]
        site.reply('submissions', encoded(rows))

        found = await client.fetch_submissions('fake_user', 0)

        assert [s.submission_id for s in found] == [20, 12, 11, 30]

    @pytest.mark.parametrize(
        'name',
        [
            '',
            'ab',
            'x' * 17,
            'fake user',
            'fake-user',
            'fake.user',
            '../resources',
            'fake_user&from_second=0',
            'fake_user\n',
            'Ｆake_user',  # a full-width F
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
            'newline',
            'full-width',
        ],
    )
    async def test_a_name_that_cant_be_an_atcoder_username_is_refused_unasked(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient, name: str
    ) -> None:
        with pytest.raises(KcpcUserError) as excinfo:
            await client.fetch_submissions(name, 0)

        assert str(excinfo.value) == INVALID_NAME
        assert not isinstance(excinfo.value, ExternalServiceError)
        assert site.requests == []

    @pytest.mark.parametrize(
        'body',
        [
            b'',
            b'<html><body>Bad Gateway</body></html>',
            b'{"error":"from_second required"}',
            b'[1, 2]',
            encoded([{**submission_row(1, 1700000000), 'id': '1'}]),
            encoded([{**submission_row(1, 1700000000), 'id': True}]),
            encoded([{**submission_row(1, 1700000000), 'epoch_second': 1.7e9}]),
            encoded([{**submission_row(1, 1700000000), 'problem_id': None}]),
            encoded([{**submission_row(1, 1700000000), 'result': None}]),
            encoded([{k: v for k, v in submission_row(1, 0).items() if k != 'id'}]),
            encoded([submission_row(1, 1700000000), None]),
        ],
        ids=[
            'empty',
            'html',
            'object',
            'numbers',
            'id-text',
            'id-bool',
            'time-float',
            'no-problem',
            'no-result',
            'no-id',
            'one-bad',
        ],
    )
    async def test_a_list_that_cannot_be_read_is_refused(
        self, site: FakeKenkoooo, client: AtCoderProblemsClient, body: bytes
    ) -> None:
        site.reply('submissions', body)

        with pytest.raises(ExternalServiceError) as excinfo:
            await client.fetch_submissions('fake_user', 0)

        assert str(excinfo.value) == UNREADABLE_SUBMISSIONS
        assert (excinfo.value.service, excinfo.value.status) == (
            'AtCoder Problems',
            200,
        )

    @pytest.mark.parametrize(
        ('status', 'message'),
        [
            (400, r'^AtCoder Problems returned an error \(HTTP 400\)\.$'),
            (503, '^AtCoder Problems is not responding right now'),
        ],
    )
    async def test_failures_name_atcoder_problems(
        self,
        site: FakeKenkoooo,
        client: AtCoderProblemsClient,
        status: int,
        message: str,
    ) -> None:
        site.reply('submissions', b'{"error":"from_second required"}', status=status)

        with pytest.raises(ExternalServiceError, match=message) as excinfo:
            await client.fetch_submissions('fake_user', 0)
        assert (excinfo.value.service, excinfo.value.status) == (
            'AtCoder Problems',
            status,
        )
