import asyncio
import logging
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from typing import TYPE_CHECKING, Any, NamedTuple

from tle.util import codeforces_api as cf, codeforces_common as cf_common, events, tasks
from tle.util.cache._common import CacheError, _is_blacklisted, getUsersEffectiveRating
from tle.util.ranklist import Ranklist, virtual

if TYPE_CHECKING:
    from tle.util.cache.cache_system import CacheSystem

ContestDetails = tuple[cf.Contest, list[cf.Problem], list[cf.RanklistRow]]


class RanklistCacheError(CacheError):
    pass


class RanklistNotMonitored(RanklistCacheError):
    def __init__(self, contest: cf.Contest) -> None:
        super().__init__(f'The ranklist for `{contest.name}` is not being monitored')
        self.contest = contest


class _Kept(NamedTuple):
    """A finished contest's details, as the cache keeps them."""

    fetched: float  # on the cache's clock
    details: ContestDetails
    # Its rating changes, if they were out when its standings were fetched;
    # None if they weren't, or nobody asked.
    changes: list[cf.RatingChange] | None


def _score_and_rank(
    contest: cf.Contest,
    problems: Sequence[cf.Problem],
    official: Sequence[cf.RanklistRow],
    participations: Sequence[virtual.VirtualParticipation],
    decay: Fraction | None,
) -> list[cf.RanklistRow]:
    """The rows that ``participations`` earn, ranked among the ``official``
    rows and each other, in the same order. Pure, to run in a thread.
    """
    rows = [
        virtual.score(contest, problems, participation, decay=decay)
        for participation in participations
    ]
    return virtual.rank_among(official, rows)


def _predict_deltas(
    row_by_handle: Mapping[str, cf.RanklistRow],
    rating_by_handle: Mapping[str, int],
    official: Sequence[cf.RanklistRow],
    changes: Sequence[cf.RatingChange],
) -> dict[str, int]:
    """``virtual.predict_delta`` of each handle's row. Pure, to run in a
    thread.
    """
    return {
        handle: virtual.predict_delta(
            handle, row, rating_by_handle[handle], official, changes
        )
        for handle, row in row_by_handle.items()
    }


class RanklistCache:
    _RELOAD_DELAY = 2 * 60
    # The standings of a finished contest no longer change, so they are kept a
    # while for the next ;ranklist, rated virtual contest tick or refresh of a
    # monitored contest, with its rating changes once they are out. Each takes
    # megabytes, so only the few used last are kept.
    _FINISHED_TTL = 30 * 60
    _FINISHED_KEPT = 3

    def __init__(
        self,
        cache_master: 'CacheSystem',
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_master = cache_master
        self.monitored_contests: list[cf.Contest] = []
        self.ranklist_by_contest: dict[int, Ranklist] = {}
        self.logger = logging.getLogger(self.__class__.__name__)
        self._clock = clock
        # The details of finished contests by id, the least recently used
        # first.
        self._finished: OrderedDict[int, _Kept] = OrderedDict()
        # A lock for each contest being fetched, so that callers that come
        # together share one fetch. A lock goes once nobody holds or awaits it.
        self._fetching: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # The decay constants of finished contests in Codeforces format.
        self._decay_by_contest: dict[int, Fraction] = {}

    async def run(self) -> None:
        assert isinstance(self._update_task, tasks.Task)
        self._update_task.start()

    def get_ranklist(self, contest: cf.Contest, show_official: bool) -> Ranklist:
        if show_official or contest.id not in self.ranklist_by_contest:
            raise RanklistNotMonitored(contest)
        return self.ranklist_by_contest[contest.id]

    def _forget_unmonitored(self) -> None:
        """Drop the ranklists of the contests no longer monitored, such as one
        whose rating changes are saved. Otherwise ``get_ranklist`` would hand
        out its last predicted ranklist for as long as any contest is
        monitored, and ``;ranklist`` would never fetch its official ranks.
        """
        ids = {contest.id for contest in self.monitored_contests}
        self.ranklist_by_contest = {
            contest_id: ranklist
            for contest_id, ranklist in self.ranklist_by_contest.items()
            if contest_id in ids
        }

    @tasks.task_spec(
        name='RanklistCacheUpdate',
        waiter=tasks.Waiter.for_event(events.ContestListRefresh),
    )
    async def _update_task(self, _: Any) -> None:
        contests_by_phase = self.cache_master.contest_cache.contests_by_phase
        running_contests = contests_by_phase['_RUNNING']

        rating_cache = self.cache_master.rating_changes_cache
        finished_contests = [
            contest
            for contest in contests_by_phase['FINISHED']
            if not _is_blacklisted(contest)
            and await rating_cache.is_newly_finished_without_rating_changes(contest)
        ]

        to_monitor = running_contests + finished_contests
        cur_ids = {contest.id for contest in self.monitored_contests}
        new_ids = {contest.id for contest in to_monitor}
        if new_ids != cur_ids:
            assert isinstance(self._monitor_task, tasks.Task)
            await self._monitor_task.stop()
            # The monitor is stopped, so none of its runs can add a ranklist
            # back.
            self.monitored_contests = to_monitor
            self._forget_unmonitored()
            if to_monitor:
                self._monitor_task.start()

    @tasks.task_spec(
        name='RanklistCacheUpdate.MonitorActiveContests',
        waiter=tasks.Waiter.fixed_delay(_RELOAD_DELAY),
    )
    async def _monitor_task(self, _: Any) -> None:
        cache = self.cache_master.rating_changes_cache
        self.monitored_contests = [
            contest
            for contest in self.monitored_contests
            if not _is_blacklisted(contest)
            and (
                contest.phase != 'FINISHED'
                or await cache.is_newly_finished_without_rating_changes(contest)
            )
        ]
        self._forget_unmonitored()

        if not self.monitored_contests:
            self.logger.info('No more active contests for which to monitor ranklists.')
            assert isinstance(self._monitor_task, tasks.Task)
            await self._monitor_task.stop()
            return

        ranklist_by_contest = await self._fetch(self.monitored_contests)
        for contest_id, ranklist in ranklist_by_contest.items():
            self.ranklist_by_contest[contest_id] = ranklist

    async def _get_contest_details(self, contest_id: int) -> ContestDetails:
        """The contest, its problems and its standings (see ``_get_contest``)."""
        details, _ = await self._get_contest(contest_id, with_changes=False)
        return details

    async def _get_contest(
        self, contest_id: int, *, with_changes: bool
    ) -> tuple[ContestDetails, list[cf.RatingChange]]:
        """The contest, its problems and its standings, from one request, and
        with ``with_changes`` its rating changes from another: none until they
        are out. Raises RatingChangesUnavailableError as Codeforces does.

        Codeforces' standings have official contestants alone, often only those
        it trusts (see ``cf.contest.standings``). Out-of-competition and
        virtual rows are let through all the same, should they come back.

        A finished contest's details are kept for ``_FINISHED_TTL`` seconds,
        with its rating changes once they are out. Standings kept from before
        the rating changes came out are fetched again, as they can hold rows
        that Codeforces has since removed, such as skipped cheaters'. Callers
        that come together for a contest share its fetch.
        """
        lock = self._fetching.get(contest_id)
        if lock is None:
            lock = self._fetching[contest_id] = asyncio.Lock()
        async with lock:
            # A caller that waited for another's fetch finds what it kept.
            kept = self._kept(contest_id)
            changes: list[cf.RatingChange] = []
            if with_changes:
                if kept is not None and kept.changes is not None:
                    changes = kept.changes
                else:
                    changes = await cf.contest.ratingChanges(contest_id=contest_id)
                    if changes:
                        # Standings kept from before them are fetched again.
                        # Other contests' reads may have dropped them
                        # meanwhile, as expired or as the least recently used.
                        self._finished.pop(contest_id, None)
                        kept = None
            if kept is None:
                details = await self._fetch_details(contest_id)
                if details[0].phase == 'FINISHED':
                    self._keep(
                        contest_id, _Kept(self._clock(), details, changes or None)
                    )
            else:
                details = kept.details
        contest, problems, standings = details
        return (contest, list(problems), list(standings)), list(changes)

    @staticmethod
    async def _fetch_details(contest_id: int) -> ContestDetails:
        contest, problems, standings = await cf.contest.standings(contest_id=contest_id)
        standings = [
            row
            for row in standings
            if row.party.participantType
            in ('CONTESTANT', 'OUT_OF_COMPETITION', 'VIRTUAL')
        ]
        return contest, problems, standings

    def _kept(self, contest_id: int) -> _Kept | None:
        """The kept details of ``contest_id``, which become the most recently
        used, or None. Expired details are dropped first.
        """
        now = self._clock()
        for kept_id, kept in list(self._finished.items()):
            if now - kept.fetched >= self._FINISHED_TTL:
                del self._finished[kept_id]
        found = self._finished.get(contest_id)
        if found is not None:
            self._finished.move_to_end(contest_id)
        return found

    def _keep(self, contest_id: int, kept: _Kept) -> None:
        """Keep a finished contest's details as the most recently used, and
        drop the least recently used beyond ``_FINISHED_KEPT``.
        """
        self._finished[contest_id] = kept
        self._finished.move_to_end(contest_id)
        while len(self._finished) > self._FINISHED_KEPT:
            self._finished.popitem(last=False)

    async def _get_ranklist_with_predicted_changes(
        self,
        contest: cf.Contest,
        problems: list[cf.Problem],
        standings: list[cf.RanklistRow],
        now: float,
    ) -> Ranklist:
        official = [
            row for row in standings if row.party.participantType == 'CONTESTANT'
        ]
        has_teams = any(row.party.teamId is not None for row in official)
        if cf_common.is_nonstandard_contest(contest) or has_teams:
            ranklist = Ranklist(contest, problems, standings, now, is_rated=False)
        else:
            current_rating = await getUsersEffectiveRating(activeOnly=False)
            current_rating = {
                row.party.members[0].handle: current_rating.get(
                    row.party.members[0].handle, 1500
                )
                for row in official
            }
            if 'Educational' in contest.name:
                current_rating = {
                    handle: rating
                    for handle, rating in current_rating.items()
                    if rating < 2100
                }
            ranklist = Ranklist(contest, problems, standings, now, is_rated=True)
            # The rating calculation takes a while for a big contest.
            await asyncio.to_thread(ranklist.predict, current_rating)
        return ranklist

    async def generate_ranklist(
        self,
        contest_id: int,
        *,
        fetch_changes: bool = False,
        predict_changes: bool = False,
        rated_only: bool = False,
    ) -> Ranklist:
        """The ranklist of a contest, from one request for its standings.

        With ``fetch_changes``, it has Codeforces' rating changes and official
        ranks once they are out, and predicted changes until then; with
        ``predict_changes``, predicted changes. ``rated_only`` leaves out the
        contestants that an Educational round doesn't rate.
        """
        assert fetch_changes ^ predict_changes

        changes: list[cf.RatingChange] = []
        if fetch_changes:
            try:
                details, changes = await self._get_contest(
                    contest_id, with_changes=True
                )
            except cf.RatingChangesUnavailableError:
                details = await self._get_contest_details(contest_id)
        else:
            details = await self._get_contest_details(contest_id)
        contest, problems, standings = details
        now = time.time()
        if changes:
            ranklist = Ranklist(contest, problems, standings, now, is_rated=True)
            ranklist.set_official_results(changes)
        else:
            ranklist = await self._get_ranklist_with_predicted_changes(
                contest, problems, standings, now
            )

        if rated_only and 'Educational' in ranklist.contest.name:
            ranklist.remove_unofficial_contestants()

        return ranklist

    async def generate_vc_ranklist(
        self,
        contest_id: int,
        handle_to_member_id: Mapping[str, Any],
        participations: Mapping[str, virtual.VirtualParticipation],
    ) -> Ranklist:
        """The standings of a rated virtual contest: the rows that the members'
        ``participations``, by handle, earn, ranked among the official
        contestants that Codeforces lists, with predicted rating changes.

        A member's change is reckoned among everyone Codeforces rated in the
        contest (``virtual.predict_delta``), from their rated virtual contest
        rating. Until a member takes part, or in a contest whose format the
        bot can't score, there are no rows, and Codeforces isn't asked.
        """
        contest = self.cache_master.contest_cache.get_contest(contest_id)
        if not participations or contest.type not in virtual.SCORABLE_TYPES:
            ranklist = Ranklist(contest, [], [], time.time(), is_rated=True)
            ranklist.delta_by_handle = {}
            ranklist.deltas_status = 'Predicted'
            return ranklist

        details, changes = await self._get_contest(contest_id, with_changes=True)
        contest, problems, standings = details
        official = [
            row for row in standings if row.party.participantType == 'CONTESTANT'
        ]
        decay = None
        if contest.type == 'CF':
            decay = await self._decay_constant(contest, problems, official)
        rows = await asyncio.to_thread(
            _score_and_rank,
            contest,
            problems,
            official,
            list(participations.values()),
            decay,
        )
        row_by_handle = dict(zip(participations, rows, strict=True))
        now = time.time()

        rating_by_handle = {
            handle: await cf_common.user_db.get_vc_rating(
                handle_to_member_id.get(handle)
            )
            for handle in row_by_handle
        }
        # Codeforces' rating calculation over everyone rated takes a while.
        delta_by_handle = await asyncio.to_thread(
            _predict_deltas, row_by_handle, rating_by_handle, official, changes
        )
        ranklist = Ranklist(
            contest, problems, list(row_by_handle.values()), now, is_rated=True
        )
        ranklist.delta_by_handle = {
            Ranklist.get_ranklist_lookup_key(row): delta_by_handle[handle]
            for handle, row in row_by_handle.items()
        }
        ranklist.deltas_status = 'Predicted'
        return ranklist

    async def _decay_constant(
        self,
        contest: cf.Contest,
        problems: list[cf.Problem],
        official: list[cf.RanklistRow],
    ) -> Fraction:
        """``virtual.decay_constant``, worked out in a thread, and only once
        for a finished contest.
        """
        decay = self._decay_by_contest.get(contest.id)
        if decay is None:
            decay = await asyncio.to_thread(
                virtual.decay_constant, contest, problems, official
            )
            if contest.phase == 'FINISHED':
                self._decay_by_contest[contest.id] = decay
        return decay

    async def _fetch(self, contests: list[cf.Contest]) -> dict[int, Ranklist]:
        ranklist_by_contest = {}
        for contest in contests:
            # One contest's failure mustn't hold the others back. Codeforces
            # fails now and then, and the next refresh tries again.
            try:
                ranklist = await self.generate_ranklist(
                    contest.id, predict_changes=True
                )
            except cf.CodeforcesApiError as er:
                self.logger.info(
                    f'Ranklist fetch failed for contest {contest.id}. {er!r}'
                )
                continue
            except Exception as er:
                self.logger.warning(
                    f'Ranklist fetch failed for contest {contest.id}. {er!r}',
                    exc_info=True,
                )
                continue
            ranklist_by_contest[contest.id] = ranklist
            self.logger.info(f'Ranklist fetched for contest {contest.id}')

        return ranklist_by_contest
