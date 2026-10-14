import asyncio
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

from tle.util import codeforces_api as cf, tasks
from tle.util.cache._common import CacheError
from tle.util.cache.contest import ContestNotFound

if TYPE_CHECKING:
    from tle.util.cache.cache_system import CacheSystem


class ProblemsetCacheError(CacheError):
    pass


class ProblemsetNotCached(ProblemsetCacheError):
    def __init__(self, contest_id: int) -> None:
        super().__init__(f'Problemset for contest with id {contest_id} not cached.')


class ProblemsetUpdate(NamedTuple):
    """What an update of the problems of every finished contest did."""

    # How many problems it saved, new or changed.
    problems: int
    # The contests whose problems Codeforces didn't give, so the bot still
    # lacks them.
    failed: list[int]


class _ProblemList:
    """Codeforces' problem list (problemset.problems), read against the bot's
    contest list.

    The list leaves some contests out, and of contests held in parallel, such
    as the two divisions of a round, it lists the problems they share under
    one of them only. So it has all the problems of a contest only if it lists
    the contest and no other contest started at the same time.
    """

    def __init__(
        self, problems: Iterable[cf.Problem], contests: Iterable[cf.Contest]
    ) -> None:
        self._start_time: dict[int, int] = {
            contest.id: contest.startTimeSeconds
            for contest in contests
            if contest.startTimeSeconds is not None
        }
        starting = Counter(self._start_time.values())
        self._parallel = {
            contest_id
            for contest_id, start in self._start_time.items()
            if starting[start] > 1
        }
        self.by_contest: dict[int, list[cf.Problem]] = {}
        self._by_index: dict[tuple[int, str], cf.Problem] = {}
        # By start time and name; a rated problem wins over an unrated one.
        self._by_name: dict[tuple[int, str], cf.Problem] = {}
        for problem in problems:
            if problem.contestId is None:
                continue
            self.by_contest.setdefault(problem.contestId, []).append(problem)
            self._by_index[problem.contestId, problem.index] = problem
            start = self._start_time.get(problem.contestId)
            if start is not None:
                known = self._by_name.get((start, problem.name))
                if known is None or known.rating is None:
                    self._by_name[start, problem.name] = problem

    def complete(self, contest_id: int) -> bool:
        """Whether the list has every problem of contest ``contest_id``."""
        return contest_id in self.by_contest and contest_id not in self._parallel

    def refreshed(self, problem: cf.Problem) -> cf.Problem:
        """``problem`` with the rating and tags that the list gives it.

        They are those of the problem listed with its contest and index, or
        else of the problem listed with its name in a contest that started at
        the same time. Nothing else is taken, as points differ between
        divisions, and a rating or tags the list lacks never replace known ones.
        """
        contest_id = problem.contestId
        if contest_id is None:
            return problem
        listed = self._by_index.get((contest_id, problem.index))
        start = self._start_time.get(contest_id)
        if listed is None and start is not None:
            listed = self._by_name.get((start, problem.name))
        if listed is None:
            return problem
        return problem._replace(
            rating=problem.rating if listed.rating is None else listed.rating,
            tags=listed.tags or problem.tags,
        )


class ProblemsetCache:
    """The problems of each contest, as its standings show them.

    They come from Codeforces' problem list where it has all of a contest's
    problems, and otherwise from the contest's standings, fetched once. Stored
    problems are never deleted unless a manual update replaces them.
    """

    _MONITOR_PERIOD_SINCE_CONTEST_END = 14 * 24 * 60 * 60
    _RELOAD_DELAY = 60 * 60
    # How long the hourly update leaves a contest alone after failing to get
    # its problems from its standings.
    _FAILED_FETCH_BACKOFF = 24 * 60 * 60

    def __init__(
        self, cache_master: 'CacheSystem', clock: Callable[[], float] = time.time
    ) -> None:
        self.problems: list[cf.Problem] = []
        self.problem_to_contests: defaultdict[tuple[str, int | None], list[int]] = (
            defaultdict(list)
        )
        self.cache_master = cache_master
        self.update_lock = asyncio.Lock()
        self.logger = logging.getLogger(self.__class__.__name__)
        self._clock = clock
        # When an update of every finished contest last failed to get each
        # contest's problems from its standings, by contest id.
        self._failed_fetches: dict[int, float] = {}

    async def run(self) -> None:
        if await self.cache_master.conn.problemset_empty():
            self.logger.warning(
                'Problemset cache on disk is empty. The hourly update fills in'
                " the contests that Codeforces' problem list has; run"
                ' ;cache problemsets all for the others.'
            )
        # The map is there even if the first update fails.
        await self._update_from_disk()
        assert isinstance(self._update_task, tasks.Task)
        self._update_task.start()

    async def update_for_contest(self, contest_id: int) -> int:
        """Fetch the problems of a contest again and replace the stored ones
        with them. Intended for manual trigger.

        The problems are always those of the contest's standings, with the
        ratings and tags that Codeforces' problem list gives them, so that a
        contest the list has wrong can be put right. If either request fails,
        nothing is replaced, and the error propagates.
        """
        async with self.update_lock:
            contest = self.cache_master.contest_cache.get_contest(contest_id)
            fetched = await self._fetch_standings(contest.id)
            if not fetched:
                # Every contest has problems: keep those stored.
                return 0
            problem_list = await self._fetch_problem_list()
            problems = [problem_list.refreshed(problem) for problem in fetched]
            count = await self.cache_master.conn.replace_problemset(
                contest.id, problems
            )
            await self._update_from_disk()
            return count

    async def update_for_all(self) -> ProblemsetUpdate:
        """Update the problems of every finished contest as the hourly update
        does, but fetch the standings of each contest that needs them however
        old it is, and without the backoff. Intended for manual trigger.

        Stored problems are never deleted, and a contest whose problems it
        fails to get is reported, not raised.
        """
        async with self.update_lock:
            return await self._update_finished(every_age=True)

    @tasks.task_spec(
        name='ProblemsetCacheUpdate', waiter=tasks.Waiter.fixed_delay(_RELOAD_DELAY)
    )
    async def _update_task(self, _: Any) -> None:
        async with self.update_lock:
            try:
                update = await self._update_finished(every_age=False)
            except cf.CodeforcesApiError as error:
                # The problem list failed, which the API client has logged.
                self.logger.info(f'Problems not updated. {error!r}')
                return
        # Each failure is logged as it happens; this says what comes next.
        if len(update.failed) == 1:
            self.logger.info(
                f'Problems of contest {update.failed[0]} still missing; the'
                ' hourly update tries again after 24 hours, until 14 days after'
                ' the contest ended.'
            )
        elif update.failed:
            contest_ids = ', '.join(map(str, update.failed))
            self.logger.info(
                f'Problems of {len(update.failed)} contests still missing:'
                f' {contest_ids}. The hourly update tries each again after 24'
                ' hours, until 14 days after it ended.'
            )

    async def _update_finished(self, *, every_age: bool) -> ProblemsetUpdate:
        """Bring the stored problems of every finished contest up to date.

        A contest whose problems are all in Codeforces' problem list gets them
        from it. The stored problems of other contests get their ratings and
        tags from it. Other contests get their problems from their standings,
        with one request each: with ``every_age``, all of them; otherwise
        those that ended in the last 14 days and whose problems it didn't fail
        to get in the last 24 hours. Only new or changed problems are saved.

        A contest whose problems it fails to get, for any reason, goes in
        ``failed``, and the update goes on. Raises ``CodeforcesApiError`` if
        the problem list fails, before anything changes.
        """
        contests = self.cache_master.contest_cache.contests_by_phase['FINISHED']
        if not contests:
            return ProblemsetUpdate(0, [])
        problem_list = await self._fetch_problem_list()
        stored = await self._stored_problems()
        now = self._clock()
        saved = 0
        unsaved: list[cf.Problem] = []
        failed: list[int] = []
        # The newest first, so that an update cut short has the contests that
        # members are likeliest to ask about.
        for contest in reversed(contests):
            rows = stored.get(contest.id, {})
            if problem_list.complete(contest.id):
                problems = problem_list.by_contest[contest.id]
            elif rows:
                problems = [
                    problem_list.refreshed(problem) for problem in rows.values()
                ]
            else:
                if every_age or self._due(contest, now):
                    # Saved at once, so that an update cut short keeps them.
                    count = await self._save_standings(contest.id, problem_list)
                    if count is None:
                        failed.append(contest.id)
                    else:
                        saved += count
                continue
            unsaved += [
                problem for problem in problems if rows.get(problem.index) != problem
            ]
        saved += await self._save(unsaved)
        await self._update_from_disk()
        self.logger.info(f'{saved} new or changed problems saved.')
        return ProblemsetUpdate(saved, sorted(failed))

    def _due(self, contest: cf.Contest, now: float) -> bool:
        """Whether the hourly update fetches the standings of ``contest``: it
        ended in the last 14 days, and getting its problems from them last
        failed more than 24 hours ago, if ever.
        """
        end = contest.end_time
        if end is None or now - end > self._MONITOR_PERIOD_SINCE_CONTEST_END:
            return False
        failed_at = self._failed_fetches.get(contest.id)
        return failed_at is None or now - failed_at > self._FAILED_FETCH_BACKOFF

    async def _fetch_problem_list(self) -> _ProblemList:
        problems, _ = await cf.problemset.problems()
        return _ProblemList(problems, self.cache_master.contest_cache.contests)

    async def _fetch_standings(self, contest_id: int) -> list[cf.Problem]:
        """The problems in the standings of contest ``contest_id``, from one
        request.
        """
        _, problems, _ = await cf.contest.standings(contest_id=contest_id)
        return problems

    async def _save_standings(
        self, contest_id: int, problem_list: _ProblemList
    ) -> int | None:
        """Save the problems in the standings of contest ``contest_id``, with
        the ratings and tags that ``problem_list`` gives them, and return how
        many there are.

        If the standings fail or have no problems, or saving them fails, it
        logs why and returns None, and the hourly update leaves the contest
        alone for 24 hours. A Codeforces error is logged as information, and
        anything else as a warning with its traceback. Cancelling still stops
        it.
        """
        try:
            found = await self._fetch_standings(contest_id)
            if found:
                count = await self._save(
                    [problem_list.refreshed(problem) for problem in found]
                )
                self._failed_fetches.pop(contest_id, None)
                return count
            self.logger.info(f'Standings of contest {contest_id} have no problems.')
        except cf.CodeforcesApiError as error:
            # Codeforces failed, which the API client has logged.
            self.logger.info(
                f'Standings of contest {contest_id} not fetched. {error!r}'
            )
        except Exception:
            # A bug, or an answer that the API client doesn't expect.
            self.logger.warning(
                f'Problems of contest {contest_id} not saved.', exc_info=True
            )
        self._failed_fetches[contest_id] = self._clock()
        return None

    async def _stored_problems(self) -> dict[int, dict[str, cf.Problem]]:
        """The stored problems, by contest id and index."""
        stored: dict[int, dict[str, cf.Problem]] = {}
        for problem in await self.cache_master.conn.fetch_problems2():
            if problem.contestId is not None:
                stored.setdefault(problem.contestId, {})[problem.index] = problem
        return stored

    async def _save(self, problems: list[cf.Problem]) -> int:
        if not problems:
            return 0
        return await self.cache_master.conn.cache_problemset(problems)

    async def get_problemset(self, contest_id: int) -> list[cf.Problem]:
        problemset = await self.cache_master.conn.fetch_problemset(contest_id)
        if not problemset:
            raise ProblemsetNotCached(contest_id)
        return problemset

    async def _update_from_disk(self) -> None:
        self.problems = await self.cache_master.conn.fetch_problems2()
        self.problem_to_contests = defaultdict(list)
        for problem in self.problems:
            try:
                if problem.contestId is None:
                    continue
                contest = self.cache_master.contest_cache.get_contest(problem.contestId)
                problem_id = (problem.name, contest.startTimeSeconds)
                self.problem_to_contests[problem_id].append(contest.id)
            except ContestNotFound:
                pass
