"""AtCoder's problems and users' submissions, from AtCoder Problems.

AtCoder Problems (kenkoooo.com) publishes AtCoder's problem data as JSON files
under ``RESOURCES_URL``, updated within hours of each contest. KCPC reads
three:

- ``problems.json``: a list of every problem,
  ``{"id": "abc300_a", "contest_id": ..., "problem_index": ..., "name": ...}``.
  Its contest and letter are those of the contest it was last seen in, often
  an AtCoder Daily Training round that reused it, so they count only when
  contest-problem.json can't tell which contest set it. Names can end in
  spaces.
- ``contest-problem.json``: a list of every contest each problem is in, with
  its letter there,
  ``{"contest_id": "abc300", "problem_id": "abc300_a", "problem_index": "A"}``.
  Letters aren't only A to H: 'Ex' and 'F2' occur.
- ``problem-models.json``: an object of models by problem ID. A model's
  ``difficulty`` is an integer on AtCoder's rating scale, and
  ``is_experimental`` marks a model of a contest from before AtCoder's ratings
  began. Fields without a value are left out, so not every model has a
  difficulty, and problems of unrated contests have no model.

Its API lists a user's submissions at ``SUBMISSIONS_URL``, oldest first, at
most ``SUBMISSIONS_PAGE`` from a given second on, that second included. It
finds a user whatever the case of the name, and lists none for an unknown
user. AtCoder Problems asks for more than a second between requests, which
the host's policy keeps to.
"""

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeGuard

from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.http import HttpClient
from tle.kcpc.platforms.atcoder.profile import HANDLE_RE
from tle.kcpc.platforms.difficulty import atcoder_to_codeforces, clip_atcoder_difficulty

logger = logging.getLogger(__name__)

PLATFORM = 'atcoder'
RESOURCES_URL = 'https://kenkoooo.com/atcoder/resources/'
SUBMISSIONS_URL = 'https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions'
TASK_URL = 'https://atcoder.jp/contests/{contest_id}/tasks/{problem_id}'
CONTEST_URL = 'https://atcoder.jp/contests/{contest_id}'
SUBMISSIONS_PAGE = 500  # the most submissions that one request lists

_SERVICE = 'AtCoder Problems'
_PROBLEMS = 'problems.json'
_MODELS = 'problem-models.json'
_PAIRS = 'contest-problem.json'
# The contests problems are picked from: AtCoder's Beginner, Regular and Grand
# contests. Not its Heuristic contests, whose problems have difficulties too.
# re.ASCII, so that \d matches no digits from other scripts.
_POOL_CONTEST = re.compile(r'(abc|arc|agc)\d{3}', re.ASCII)
# How the IDs of AtCoder Daily Training rounds start: 'adt_all_20261001_1'.
_DAILY_TRAINING = 'adt_'


@dataclass(frozen=True)
class AtCoderProblem:
    """An AtCoder problem, in the contest that set it.

    ``contest_id`` is the longest of the problem's contests whose ID, plus
    '_', starts the problem's ID ('abc300' for 'abc300_a', which Daily
    Training rounds reused). Else it is the first of its contests that isn't
    a Daily Training round ('cf17-final' for 'cf17_final_a', before its open
    mirror), else the one problems.json gives, which may be such a round.
    """

    problem_id: str  # AtCoder Problems' ID, in its case: 'abc300_a'
    contest_id: str  # e.g. 'abc300'
    index: str  # its letter in that contest, as stored: 'A', 'Ex', 'F2'
    name: str  # trimmed
    difficulty: int | None  # clipped; None without one, or if experimental

    @property
    def rating(self) -> int | None:
        """The difficulty on Codeforces' scale; None without a difficulty."""
        if self.difficulty is None:
            return None
        return atcoder_to_codeforces(self.difficulty)

    @property
    def url(self) -> str:
        """The problem's page on AtCoder."""
        return TASK_URL.format(contest_id=self.contest_id, problem_id=self.problem_id)

    @property
    def contest_url(self) -> str:
        """The page of the contest that set it."""
        return CONTEST_URL.format(contest_id=self.contest_id)

    @property
    def title(self) -> str:
        """The problem as posts name it: 'ABC300 A - N-choice question'."""
        return f'{self.contest_id.upper()} {self.index} - {self.name}'

    @property
    def in_pool(self) -> bool:
        """Whether problems are picked from it: it has a difficulty, and an
        ABC, ARC or AGC set it.
        """
        return (
            self.difficulty is not None
            and _POOL_CONTEST.fullmatch(self.contest_id) is not None
        )


def parse_problem_set(
    problems: bytes, models: bytes, pairs: bytes
) -> dict[str, AtCoderProblem]:
    """Every problem in problems.json, by ID, read with its models and pairs.

    ``problems``, ``models`` and ``pairs`` are the bodies of problems.json,
    problem-models.json and contest-problem.json. A problem's letter is its
    pair's for its contest (see ``AtCoderProblem``), else the one problems.json
    gives. Its difficulty is its model's, clipped, unless the model has none
    or is experimental. Models and pairs of problems that problems.json
    doesn't list are ignored, and so is a second row for a problem. The full
    files take a quarter of a second, so callers on the event loop should run
    this in a worker thread. Raises ``ExternalServiceError`` if a file isn't
    JSON of the shape described above.
    """
    problem_rows = _list_in(_json_in(problems, _PROBLEMS), _PROBLEMS)
    model_map = _models_in(_json_in(models, _MODELS))
    pair_rows = _list_in(_json_in(pairs, _PAIRS), _PAIRS)
    letters: dict[str, dict[str, str]] = {}  # problem ID -> contest ID -> letter
    for item in pair_rows:
        contest_id, problem_id, index = _strings_in(
            item, ('contest_id', 'problem_id', 'problem_index'), _PAIRS
        )
        letters.setdefault(problem_id, {}).setdefault(contest_id, index)
    found: dict[str, AtCoderProblem] = {}
    for item in problem_rows:
        problem_id, listed_contest, listed_index, name = _strings_in(
            item, ('id', 'contest_id', 'problem_index', 'name'), _PROBLEMS
        )
        if problem_id in found:
            logger.debug('Skipping a second AtCoder problem %s', problem_id)
            continue
        contests = letters.get(problem_id, {})
        contest_id = (
            _owner(problem_id, contests) or _first_set(contests) or listed_contest
        )
        found[problem_id] = AtCoderProblem(
            problem_id=problem_id,
            contest_id=contest_id,
            index=contests.get(contest_id, listed_index),
            name=name.strip(),
            difficulty=_difficulty(problem_id, model_map.get(problem_id)),
        )
    return found


@dataclass(frozen=True)
class AtCoderSubmission:
    """A submission to an AtCoder problem, as AtCoder Problems lists it.

    It names the problem by its ID only: the contest it was submitted in may
    be a round that reused the problem.
    """

    submission_id: int
    epoch_second: int  # when it was submitted
    problem_id: str  # AtCoder Problems' ID: 'abc300_a'
    result: str  # 'AC' if accepted; else 'WA', 'TLE', 'CE' and so on


class AtCoderProblemsClient:
    """Fetches AtCoder Problems' files and submissions through the shared
    ``HttpClient``.
    """

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def fetch_problem_set(self) -> dict[str, AtCoderProblem]:
        """Every AtCoder problem, by ID, as ``parse_problem_set`` reads them.

        Fetches problems.json, problem-models.json and contest-problem.json in
        turn, as the host's policy paces them. Raises ``ExternalServiceError``
        if AtCoder Problems can't be reached or sends files that can't be read.
        """
        problems = await self._resource(_PROBLEMS)
        models = await self._resource(_MODELS)
        pairs = await self._resource(_PAIRS)
        # In a worker thread: the full files take a quarter of a second to
        # read, too long to hold up the event loop.
        return await asyncio.to_thread(parse_problem_set, problems, models, pairs)

    async def fetch_submissions(
        self, user: str, from_second: int
    ) -> list[AtCoderSubmission]:
        """One page of ``user``'s submissions, oldest first: at most
        ``SUBMISSIONS_PAGE``, from epoch second ``from_second`` on, that
        second included.

        A page with fewer is the last. A full page's last second may have more
        submissions, which the next page, from that second, lists again. An
        unknown user has none. Raises ``KcpcUserError`` without asking if
        ``user`` can't be an AtCoder username, and ``ExternalServiceError`` if
        AtCoder Problems can't be reached or sends a list that can't be read.
        """
        if HANDLE_RE.fullmatch(user) is None:
            raise KcpcUserError("That isn't a valid AtCoder username.")
        response = await self._http.get(
            SUBMISSIONS_URL,
            params={'user': user, 'from_second': str(from_second)},
            service=_SERVICE,
        )
        try:
            data: object = response.json()
        except ValueError as exc:
            logger.debug(
                'AtCoder Problems sent submissions of %s that are not JSON', user
            )
            raise _unreadable_submissions(status=response.status) from exc
        return _submissions_in(data, status=response.status)

    async def _resource(self, name: str) -> bytes:
        """The body of the file ``name`` under ``RESOURCES_URL``."""
        response = await self._http.get(RESOURCES_URL + name, service=_SERVICE)
        return response.body


def _json_in(body: bytes, file: str) -> object:
    """The JSON in ``body``, the body of ``file``."""
    try:
        data: object = json.loads(body)
    except ValueError as exc:  # UnicodeDecodeError, for bytes, is one too
        logger.debug("AtCoder Problems' %s is not JSON", file)
        raise _unreadable_problem_set() from exc
    return data


def _list_in(data: object, file: str) -> list[object]:
    """``data``, the JSON of ``file``, which must be a list."""
    if not isinstance(data, list):
        logger.debug(
            "AtCoder Problems' %s is a %s, not a list", file, type(data).__name__
        )
        raise _unreadable_problem_set()
    return data


def _models_in(data: object) -> dict[str, object]:
    """``data``, the JSON of problem-models.json, which must be an object."""
    if not isinstance(data, dict):
        logger.debug(
            "AtCoder Problems' %s is a %s, not an object",
            _MODELS,
            type(data).__name__,
        )
        raise _unreadable_problem_set()
    return data


def _strings_in(item: object, keys: tuple[str, ...], file: str) -> list[str]:
    """The fields ``keys`` of an entry in ``file``, which must all be strings."""
    if isinstance(item, dict):
        values = [item.get(key) for key in keys]
        strings = [value for value in values if isinstance(value, str)]
        if len(strings) == len(keys):
            return strings
    logger.debug(
        "AtCoder Problems' %s has an entry shaped like this: %.200r", file, item
    )
    raise _unreadable_problem_set()


def _owner(problem_id: str, contests: Iterable[str]) -> str | None:
    """The longest of ``contests`` whose ID, plus '_', starts ``problem_id``."""
    owners = [contest for contest in contests if problem_id.startswith(f'{contest}_')]
    return max(owners, key=len, default=None)


def _first_set(contests: Iterable[str]) -> str | None:
    """The first of a problem's ``contests``, in contest-problem.json's order,
    that isn't a Daily Training round, which only reuses problems: in that
    file, a contest that set a problem comes before its open mirror.
    """
    return next(
        (contest for contest in contests if not contest.startswith(_DAILY_TRAINING)),
        None,
    )


def _difficulty(problem_id: str, model: object) -> int | None:
    """The clipped difficulty in a problem's model, if it has one.

    None without a model, or for a model without a difficulty or marked
    experimental. Raises ``ExternalServiceError`` unless the model is an
    object whose difficulty, if any, is an integer and whose is_experimental,
    if any, is a boolean.
    """
    if model is None:
        return None
    if isinstance(model, dict):
        difficulty = model.get('difficulty')
        experimental = model.get('is_experimental')
        if (difficulty is None or _is_int(difficulty)) and (
            experimental is None or isinstance(experimental, bool)
        ):
            if difficulty is None or experimental:
                return None
            return clip_atcoder_difficulty(difficulty)
    logger.debug(
        "AtCoder Problems' model of %s is shaped like this: %.200r", problem_id, model
    )
    raise _unreadable_problem_set()


def _submissions_in(data: object, *, status: int) -> list[AtCoderSubmission]:
    """The submissions in a page of AtCoder Problems' API, oldest first.

    Raises ``ExternalServiceError`` unless ``data`` is a list of submissions
    that can all be read.
    """
    if isinstance(data, list):
        found = [_submission(item) for item in data]
        submissions = [submission for submission in found if submission is not None]
        if len(submissions) == len(found):
            # The API lists them oldest first already. Sorted all the same,
            # since callers page on from the last one's second; the sort is
            # stable, so those of one second keep the API's order.
            return sorted(submissions, key=lambda submission: submission.epoch_second)
    logger.debug('AtCoder Problems sent submissions shaped like this: %.200r', data)
    raise _unreadable_submissions(status=status)


def _submission(item: object) -> AtCoderSubmission | None:
    """The submission in an entry of the API's list; None if it isn't one."""
    if not isinstance(item, dict):
        return None
    submission_id = item.get('id')
    epoch_second = item.get('epoch_second')
    problem_id = item.get('problem_id')
    result = item.get('result')
    if (
        _is_int(submission_id)
        and _is_int(epoch_second)
        and isinstance(problem_id, str)
        and isinstance(result, str)
    ):
        return AtCoderSubmission(
            submission_id=submission_id,
            epoch_second=epoch_second,
            problem_id=problem_id,
            result=result,
        )
    return None


def _is_int(value: object) -> TypeGuard[int]:
    # bool is a subclass of int, but true is no number.
    return isinstance(value, int) and not isinstance(value, bool)


def _unreadable_problem_set() -> ExternalServiceError:
    return ExternalServiceError(
        _SERVICE, "AtCoder Problems' problem list could not be read."
    )


def _unreadable_submissions(*, status: int | None = None) -> ExternalServiceError:
    return ExternalServiceError(
        _SERVICE,
        "AtCoder Problems' list of submissions could not be read.",
        status=status,
    )
