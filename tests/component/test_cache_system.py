"""Component tests for cache sub-systems — ContestCache, ProblemCache,
RatingChangesCache, ProblemsetCache.

Tests data management methods directly, NOT the periodic task infrastructure.
The timed tasks' bodies run once at a time, to show what they log when
Codeforces fails.

NOTE: The cache modules have a circular import chain
(tle.util.cache.__init__ <-> tle.util.codeforces_common). To avoid triggering
this at test collection time we import cache classes lazily inside fixtures,
and codeforces_common first, so that the file also runs on its own.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

import tle.util.codeforces_common  # noqa: F401
from tle.util import codeforces_api as cf
from tle.util.codeforces_api import Contest, Problem, RatingChange
from tle.util.events import (
    ContestListRefresh,
    EventSystem,
    Listener,
    RatingChangesUpdate,
)


def _make_contest(id=1, name='Round #1', start=1_000_000, dur=7200, phase='FINISHED'):
    return Contest(
        id=id,
        name=name,
        startTimeSeconds=start,
        durationSeconds=dur,
        type='CF',
        phase=phase,
        preparedBy=None,
    )


def _make_problem(contestId=1, index='A', name='Problem A', rating=1500):
    return Problem(
        contestId=contestId,
        problemsetName=None,
        index=index,
        name=name,
        type='PROGRAMMING',
        points=None,
        rating=rating,
        tags=[],
    )


def _make_rating_change(contestId=1, handle='alice', old=1500, new=1600):
    return RatingChange(
        contestId=contestId,
        contestName='Round',
        handle=handle,
        rank=1,
        ratingUpdateTimeSeconds=1_000_000,
        oldRating=old,
        newRating=new,
    )


@pytest.fixture
def cache_system(cache_db):
    """Build a CacheSystem with a real in-memory DB, using lazy import."""
    from tle.util.cache.cache_system import CacheSystem

    return CacheSystem(cache_db)


def logged(caplog, name=None):
    """The level and message of each captured record, or of those of the
    logger `name`.
    """
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if name is None or record.name == name
    ]


def warned(caplog):
    """The captured records at WARNING or above."""
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


# --- CacheSystem init ---


class TestCacheSystemInit:
    def test_creates_all_sub_caches(self, cache_system):
        from tle.util.cache.contest import ContestCache
        from tle.util.cache.problem import ProblemCache
        from tle.util.cache.problemset import ProblemsetCache
        from tle.util.cache.rating_changes import RatingChangesCache

        assert isinstance(cache_system.contest_cache, ContestCache)
        assert isinstance(cache_system.problem_cache, ProblemCache)
        assert isinstance(cache_system.rating_changes_cache, RatingChangesCache)
        assert isinstance(cache_system.problemset_cache, ProblemsetCache)

    def test_conn_reference(self, cache_system, cache_db):
        assert cache_system.conn is cache_db


# --- ContestCache ---


class TestContestCache:
    async def test_update_populates_contests(self, cache_system):
        contests = [
            _make_contest(id=1),
            _make_contest(id=2, name='Round #2', start=2_000_000),
        ]
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update(contests, from_api=False)
        assert len(cache_system.contest_cache.contests) == 2
        assert 1 in cache_system.contest_cache.contest_by_id
        assert 2 in cache_system.contest_cache.contest_by_id

    async def test_update_populates_contests_by_phase(self, cache_system):
        c_finished = _make_contest(id=1, phase='FINISHED')
        c_before = _make_contest(id=2, phase='BEFORE', start=99_999_999_999)
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update(
                [c_finished, c_before],
                from_api=False,
            )
        assert len(cache_system.contest_cache.contests_by_phase['FINISHED']) == 1
        assert len(cache_system.contest_cache.contests_by_phase['BEFORE']) == 1

    async def test_get_contest_found(self, cache_system):
        contests = [_make_contest(id=42)]
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update(contests, from_api=False)
        result = cache_system.contest_cache.get_contest(42)
        assert result.id == 42

    async def test_get_contest_not_found(self, cache_system):
        from tle.util.cache.contest import ContestNotFound

        with pytest.raises(ContestNotFound):
            cache_system.contest_cache.get_contest(999)

    async def test_running_phase_grouping(self, cache_system):
        c_coding = _make_contest(id=1, phase='CODING', start=1_000_000)
        c_system = _make_contest(id=2, phase='SYSTEM_TEST', start=2_000_000)
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update(
                [c_coding, c_system],
                from_api=False,
            )
        assert len(cache_system.contest_cache.contests_by_phase['_RUNNING']) == 2

    async def test_update_dispatches_event(self, cache_system):
        es = EventSystem()
        received = []

        async def on_refresh(event):
            received.append(event)

        lst = Listener('test', ContestListRefresh, on_refresh)
        es.add_listener(lst)

        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update([_make_contest()], from_api=False)

        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert isinstance(received[0], ContestListRefresh)

    async def test_update_from_api_stores_to_db(self, cache_system):
        contests = [_make_contest(id=1)]
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._update(contests, from_api=True)
        fetched = await cache_system.conn.fetch_contests()
        assert len(fetched) == 1

    async def test_try_disk_loads_from_db(self, cache_system):
        await cache_system.conn.cache_contests(
            [_make_contest(id=10, name='Disk Round')],
        )
        es = EventSystem()
        with patch('tle.util.cache.contest.cf_common') as mock_cc:
            mock_cc.event_sys = es
            await cache_system.contest_cache._try_disk()
        assert 10 in cache_system.contest_cache.contest_by_id

    async def test_codeforces_failing_is_information_and_retried_in_5_minutes(
        self, cache_system, caplog
    ):
        contest_cache = cache_system.contest_cache
        error = cf.ClientError()

        with patch.object(cf.contest, 'to_list', AsyncMock(side_effect=error)):
            with caplog.at_level(logging.INFO), pytest.raises(cf.ClientError):
                # As ;cache contests does: the task runs once, and its error
                # is raised to the command.
                await contest_cache.reload_now()

        assert logged(caplog, 'Task') == [
            (logging.INFO, 'Manually triggering task `ContestCacheUpdate`.'),
            (
                logging.INFO,
                'Task `ContestCacheUpdate` stopped on a Codeforces error,'
                f' ignoring: {error!r}',
            ),
        ]
        assert warned(caplog) == []
        assert all(record.exc_info is None for record in caplog.records)
        # The task's exception handler still had it, so the next try is sooner.
        assert contest_cache.reload_exception is error
        assert contest_cache.next_delay == contest_cache._EXCEPTION_CONTEST_RELOAD_DELAY

    async def test_a_bug_is_still_a_warning_with_its_traceback(
        self, cache_system, caplog
    ):
        contest_cache = cache_system.contest_cache
        error = KeyError('phase')

        with patch.object(cf.contest, 'to_list', AsyncMock(side_effect=error)):
            with caplog.at_level(logging.INFO), pytest.raises(KeyError):
                await contest_cache.reload_now()

        assert [(record.name, record.getMessage()) for record in warned(caplog)] == [
            ('Task', 'Exception in task `ContestCacheUpdate`, ignoring.')
        ]
        assert warned(caplog)[0].exc_info[1] is error
        assert contest_cache.next_delay == contest_cache._EXCEPTION_CONTEST_RELOAD_DELAY


# --- ProblemCache ---


class TestProblemCache:
    @pytest.fixture
    def cache_with_contests(self, cache_system):
        cache_system.contest_cache.contest_by_id = {1: _make_contest(id=1)}
        return cache_system

    async def test_update_filters_problems(self, cache_with_contests):
        p1 = _make_problem(contestId=1, name='Good', rating=1500)
        p2 = _make_problem(contestId=1, name='NoRating', rating=None)
        p3 = _make_problem(contestId=999, name='UnknownContest', rating=1500)

        await cache_with_contests.problem_cache._update([p1, p2, p3])
        assert len(cache_with_contests.problem_cache.problems) == 1
        assert cache_with_contests.problem_cache.problems[0].name == 'Good'

    async def test_problem_by_name_mapping(self, cache_with_contests):
        p = _make_problem(contestId=1, name='Unique', rating=1500)
        await cache_with_contests.problem_cache._update([p])
        assert 'Unique' in cache_with_contests.problem_cache.problem_by_name

    async def test_try_disk_loads(self, cache_with_contests):
        p = _make_problem(contestId=1, name='DiskProblem', rating=1500)
        await cache_with_contests.conn.cache_problems([p])
        await cache_with_contests.problem_cache._try_disk()
        assert len(cache_with_contests.problem_cache.problems) == 1
        prob = cache_with_contests.problem_cache.problem_by_name['DiskProblem']
        assert prob.rating == 1500

    async def test_codeforces_failing_is_information_and_keeps_the_problems(
        self, cache_with_contests, caplog
    ):
        problem_cache = cache_with_contests.problem_cache
        await problem_cache._update([_make_problem(contestId=1, name='Kept')])
        # Such as the error page Codeforces answered once instead of the list.
        error = cf.ClientError()

        with patch.object(cf.problemset, 'problems', AsyncMock(side_effect=error)):
            with caplog.at_level(logging.INFO), pytest.raises(cf.ClientError):
                # As ;cache problems does.
                await problem_cache.reload_now()

        assert logged(caplog, 'Task') == [
            (logging.INFO, 'Manually triggering task `ProblemCacheUpdate`.'),
            (
                logging.INFO,
                'Task `ProblemCacheUpdate` stopped on a Codeforces error,'
                f' ignoring: {error!r}',
            ),
        ]
        assert warned(caplog) == []
        assert all(record.exc_info is None for record in caplog.records)
        assert [problem.name for problem in problem_cache.problems] == ['Kept']


# --- RatingChangesCache ---


class TestRatingChangesCache:
    async def test_get_current_rating_found(self, cache_system):
        change = _make_rating_change(handle='alice', new=1700)
        await cache_system.conn.save_rating_changes([change])
        await cache_system.rating_changes_cache._refresh_handle_cache()
        assert cache_system.rating_changes_cache.get_current_rating('alice') == 1700

    async def test_get_current_rating_missing_returns_none(self, cache_system):
        assert cache_system.rating_changes_cache.get_current_rating('nobody') is None

    async def test_get_current_rating_default_if_absent(self, cache_system):
        result = cache_system.rating_changes_cache.get_current_rating(
            'nobody', default_if_absent=True
        )
        assert result == 1500

    async def test_get_all_ratings(self, cache_system):
        changes = [
            _make_rating_change(contestId=1, handle='alice', new=1700),
            _make_rating_change(contestId=2, handle='bob', new=1800),
        ]
        await cache_system.conn.save_rating_changes(changes)
        await cache_system.rating_changes_cache._refresh_handle_cache()
        ratings = cache_system.rating_changes_cache.get_all_ratings()
        assert sorted(ratings) == [1700, 1800]

    async def test_refresh_handle_cache_from_db(self, cache_system):
        changes = [_make_rating_change(handle='charlie', new=2000)]
        await cache_system.conn.save_rating_changes(changes)
        await cache_system.rating_changes_cache._refresh_handle_cache()
        assert cache_system.rating_changes_cache.get_current_rating('charlie') == 2000

    async def test_save_changes_stores_and_refreshes(self, cache_system):
        contest = _make_contest(id=1)
        change = _make_rating_change(contestId=1, handle='dave', new=1900)
        await cache_system.rating_changes_cache._save_changes([(contest, [change])])
        await cache_system.rating_changes_cache._refresh_handle_cache()
        assert cache_system.rating_changes_cache.get_current_rating('dave') == 1900


# --- RatingChangesCache, when Codeforces fails ---

# Codeforces Round 1127, whose Div. 1 and Div. 2 ran in parallel. The cache's
# clock is an hour after its end, while it waits for the rating changes.
ROUND_START = 1_791_743_700
ROUND_LENGTH = 2 * 60 * 60
NOW = ROUND_START + ROUND_LENGTH + 60 * 60
DIV1 = _make_contest(
    id=2273, name='Codeforces Round 1127 (Div. 1)', start=ROUND_START, dur=ROUND_LENGTH
)
DIV2 = _make_contest(
    id=2274, name='Codeforces Round 1127 (Div. 2)', start=ROUND_START, dur=ROUND_LENGTH
)


class FakeRatingChanges:
    """Codeforces' contest.ratingChanges for Round 1127, recording each request.
    A contest in `failing` gets a new error from its factory instead.
    """

    def __init__(self):
        self.changes = {
            DIV1.id: [_make_rating_change(DIV1.id, 'potato167', 2981, 3127)],
            DIV2.id: [
                _make_rating_change(DIV2.id, 'alice', 1500, 1650),
                _make_rating_change(DIV2.id, 'bob', 1400, 1380),
            ],
        }
        self.failing = {}
        self.requests = []

    async def __call__(self, *, contest_id):
        self.requests.append(contest_id)
        if contest_id in self.failing:
            raise self.failing[contest_id]()
        return self.changes[contest_id]


class TestRatingChangesCacheErrors:
    @pytest.fixture(autouse=True)
    def clock(self, monkeypatch):
        from tle.util.cache import rating_changes

        monkeypatch.setattr(rating_changes, 'time', SimpleNamespace(time=lambda: NOW))

    @pytest.fixture
    def codeforces(self, monkeypatch):
        fake = FakeRatingChanges()
        monkeypatch.setattr(cf.contest, 'ratingChanges', fake)
        return fake

    @pytest.fixture
    def dispatched(self):
        """The cache's dispatch of events, a mock."""
        with patch('tle.util.cache.rating_changes.cf_common') as common:
            yield common.event_sys.dispatch

    @pytest.mark.parametrize(
        'make_error',
        [
            # Codeforces down, after the client's tries.
            pytest.param(cf.ClientError, id='no-answer'),
            # A finished contest that isn't rated.
            pytest.param(
                lambda: cf.RatingChangesUnavailableError(
                    'contestId: Rating changes are unavailable for this contest',
                    DIV1.id,
                ),
                id='unrated',
            ),
            # A refusal the client doesn't expect, such as a new rule; the
            # client warns about it itself.
            pytest.param(
                lambda: cf.TrueApiError(
                    'contestId: Rating changes for non-admin users are available'
                    ' only via anonymous GET requests'
                ),
                id='refused',
            ),
        ],
    )
    async def test_fetch_logs_a_codeforces_error_as_information_and_goes_on(
        self, cache_system, codeforces, make_error, caplog
    ):
        cache = cache_system.rating_changes_cache
        codeforces.failing[DIV1.id] = make_error

        with caplog.at_level(logging.INFO, logger='RatingChangesCache'):
            fetched = await cache._fetch([DIV1, DIV2])

        assert codeforces.requests == [DIV1.id, DIV2.id]
        assert fetched == [(DIV2, codeforces.changes[DIV2.id])]
        assert logged(caplog) == [
            (
                logging.INFO,
                'Fetch rating changes failed for contest 2273, ignoring.'
                f' {make_error()!r}',
            ),
            (logging.INFO, '2 rating changes fetched for contest 2274'),
        ]
        assert all(record.exc_info is None for record in caplog.records)

    async def test_the_monitor_saves_the_others_and_retries_the_failed_one(
        self, cache_system, codeforces, dispatched, caplog
    ):
        cache = cache_system.rating_changes_cache
        monitor = cache._monitor_task
        cache.monitored_contests = [DIV1, DIV2]
        codeforces.failing[DIV1.id] = cf.ClientError

        with caplog.at_level(logging.INFO):
            await monitor.manual_trigger()

        # The Div. 2's changes are saved and announced, and its members'
        # ratings cached; Codeforces failing for the Div. 1 is information.
        assert codeforces.requests == [DIV1.id, DIV2.id]
        assert not await cache.has_rating_changes_saved(DIV1.id)
        assert cache.get_current_rating('alice') == 1650
        assert dispatched.call_args_list == [
            call(
                RatingChangesUpdate,
                contest=DIV2,
                rating_changes=codeforces.changes[DIV2.id],
            )
        ]
        assert (
            logging.INFO,
            'Fetch rating changes failed for contest 2273, ignoring.'
            f' {cf.ClientError()!r}',
        ) in logged(caplog)
        assert warned(caplog) == []

        # At the next tick Codeforces answers; only the Div. 1 is asked for.
        del codeforces.failing[DIV1.id]
        codeforces.requests.clear()
        dispatched.reset_mock()
        with caplog.at_level(logging.INFO):
            await monitor.manual_trigger()

        assert codeforces.requests == [DIV1.id]
        assert cache.get_current_rating('potato167') == 3127
        assert dispatched.call_args_list == [
            call(
                RatingChangesUpdate,
                contest=DIV1,
                rating_changes=codeforces.changes[DIV1.id],
            )
        ]

        # Then nothing is left to monitor.
        codeforces.requests.clear()
        with caplog.at_level(logging.INFO):
            await monitor.manual_trigger()

        assert codeforces.requests == []
        assert cache.monitored_contests == []
        assert warned(caplog) == []

    async def test_a_bug_is_still_a_warning_with_its_traceback(
        self, cache_system, codeforces, dispatched, caplog
    ):
        cache = cache_system.rating_changes_cache
        cache.monitored_contests = [DIV1, DIV2]
        error = KeyError('oldRating')
        codeforces.failing[DIV1.id] = lambda: error

        with caplog.at_level(logging.INFO):
            await cache._monitor_task.manual_trigger()

        assert len(warned(caplog)) == 1
        assert warned(caplog)[0].exc_info[1] is error


# --- RatingChangesCache, fetched again by ;cache ratingchanges ---


async def stored(cache_system, contest):
    """The handle and new rating of each stored rating change of `contest`."""
    changes = await cache_system.conn.get_rating_changes_for_contest(contest.id)
    return sorted((change.handle, change.newRating) for change in changes)


class TestRatingChangesCacheFetchedAgain:
    @pytest.fixture
    def codeforces(self, monkeypatch):
        fake = FakeRatingChanges()
        monkeypatch.setattr(cf.contest, 'ratingChanges', fake)
        return fake

    @pytest.fixture
    async def cache(self, cache_system):
        """The cache, with Round 1127 in the contest list and its rating
        changes as they were first stored: since then, Codeforces changed the
        Div. 2's and removed carol from it.
        """
        contest_cache = cache_system.contest_cache
        contest_cache.contest_by_id = {DIV1.id: DIV1, DIV2.id: DIV2}
        contest_cache.contests_by_phase['FINISHED'] = [DIV1, DIV2]
        await cache_system.conn.save_rating_changes(
            [
                _make_rating_change(DIV1.id, 'potato167', 2981, 3127),
                _make_rating_change(DIV2.id, 'alice', 1500, 1600),
                _make_rating_change(DIV2.id, 'carol', 1500, 1450),
            ]
        )
        return cache_system.rating_changes_cache

    @pytest.mark.parametrize(
        'make_error',
        [
            # Codeforces down, after the client's tries.
            pytest.param(cf.ClientError, id='no-answer'),
            # A refusal the client doesn't expect; the client warns about it.
            pytest.param(
                lambda: cf.TrueApiError(
                    'contestId: Rating changes for non-admin users are available'
                    ' only via anonymous GET requests'
                ),
                id='refused',
            ),
        ],
    )
    async def test_a_contest_that_codeforces_fails_for_keeps_its_changes(
        self, cache_system, cache, codeforces, make_error, caplog
    ):
        codeforces.failing[DIV2.id] = make_error

        with caplog.at_level(logging.INFO), pytest.raises(type(make_error())):
            # Raised to ;cache ratingchanges 2274, which replies with it. It
            # used to delete the changes, and reply that it fetched none.
            await cache.fetch_contest(DIV2.id)

        assert await stored(cache_system, DIV2) == [('alice', 1600), ('carol', 1450)]
        assert warned(caplog) == []

    async def test_a_contest_fetched_again_has_its_changes_replaced(
        self, cache_system, cache, codeforces
    ):
        fetched = await cache.fetch_contest(DIV2.id)

        # The reply counts changes, not contests.
        assert fetched == 2
        assert codeforces.requests == [DIV2.id]
        assert await stored(cache_system, DIV2) == [('alice', 1650), ('bob', 1380)]
        assert await stored(cache_system, DIV1) == [('potato167', 3127)]
        assert cache.get_current_rating('alice') == 1650

    async def test_a_contest_without_changes_yet_keeps_those_stored(
        self, cache_system, cache, codeforces
    ):
        # An answer without changes brings none to store in their place.
        codeforces.changes[DIV2.id] = []

        assert await cache.fetch_contest(DIV2.id) == 0

        assert await stored(cache_system, DIV2) == [('alice', 1600), ('carol', 1450)]

    async def test_all_contests_fetched_again_keep_the_changes_of_those_that_fail(
        self, cache_system, cache, codeforces, caplog
    ):
        # Each contest's changes used to be deleted before any was fetched,
        # so those that Codeforces failed for were left without any.
        codeforces.failing[DIV1.id] = cf.ClientError
        codeforces.changes[DIV2.id] = [
            _make_rating_change(DIV2.id, 'alice', 1500, 1650),
            _make_rating_change(DIV2.id, 'dave', 1200, 1350),
        ]

        with caplog.at_level(logging.INFO):
            fetched = await cache.fetch_all_contests()

        assert fetched == 2
        assert codeforces.requests == [DIV1.id, DIV2.id]
        assert await stored(cache_system, DIV1) == [('potato167', 3127)]
        assert await stored(cache_system, DIV2) == [('alice', 1650), ('dave', 1350)]
        assert cache.get_current_rating('dave') == 1350
        assert warned(caplog) == []

    async def test_missing_contests_are_fetched_and_counted_by_their_changes(
        self, cache_system, codeforces
    ):
        cache = cache_system.rating_changes_cache
        cache_system.contest_cache.contests_by_phase['FINISHED'] = [DIV1, DIV2]
        await cache_system.conn.save_rating_changes(codeforces.changes[DIV1.id])

        fetched = await cache.fetch_missing_contests()

        # The Div. 2's 2 changes; the Div. 1's were stored already.
        assert fetched == 2
        assert codeforces.requests == [DIV2.id]
        assert await stored(cache_system, DIV2) == [('alice', 1650), ('bob', 1380)]


# --- ProblemsetCache ---


class TestProblemsetCache:
    async def test_get_problemset_not_cached_raises(self, cache_system):
        from tle.util.cache.problemset import ProblemsetNotCached

        with pytest.raises(ProblemsetNotCached):
            await cache_system.problemset_cache.get_problemset(999)

    async def test_get_problemset_returns_data(self, cache_system):
        p = _make_problem(contestId=1, index='A', name='P1', rating=1500)
        await cache_system.conn.cache_problemset([p])
        result = await cache_system.problemset_cache.get_problemset(1)
        assert len(result) == 1
        assert result[0].name == 'P1'
