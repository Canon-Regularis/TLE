"""The problems that members have solved, so that /randproblem leaves them out.

Codeforces lists a user's whole history in one ``user.status`` request, which
is kept for ``SOLVED_TTL`` per handle. The request runs in a task of its own,
which a caller that stops waiting (/randproblem waits 10 seconds) leaves to
finish, so that the next call has its answer. After a request that fails,
Codeforces isn't asked about anyone for ``CODEFORCES_PAUSE``: TLE's client
logs every failed try as a warning, and members would otherwise repeat them
as fast as they run /randproblem. AtCoder Problems lists 500 submissions
a request, oldest first, from a given second on, so a long history takes many
requests, each a second or more apart. So what was read of each AtCoder
user's submissions is kept, and saved after every page: a caller that stops
waiting leaves what was read for the next call, which goes on from there, and
a call once ``SOLVED_TTL`` has passed reads only what is new since.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.features.problems.catalog import ATCODER, Problem
from tle.kcpc.platforms import codeforces
from tle.kcpc.platforms.atcoder.problems import (
    SUBMISSIONS_PAGE,
    AtCoderProblemsClient,
    AtCoderSubmission,
)

# How long what was read of a user's solved problems is used before asking
# again.
SOLVED_TTL = timedelta(minutes=15)
# How long Codeforces is left alone after a request there failed.
CODEFORCES_PAUSE = timedelta(minutes=2)

_ACCEPTED = 'AC'  # AtCoder's result for a submission that passed every test
_CODEFORCES = 'Codeforces'
_PAUSED = "Codeforces failed just now, so it isn't asked again for a while."

# Whether Codeforces' problemset lists a problem under this ID.
Listed = Callable[[str], bool]


def _nothing_listed(problem_id: str) -> bool:
    """Counts no problem as listed, so that every name solved is matched."""
    return False


@dataclass(frozen=True)
class SolvedSet:
    """The problems a member has solved on one platform."""

    ids: frozenset[str]  # the problems' IDs: '1520D', 'abc300_d'
    # On Codeforces, the names of those that its problemset doesn't list
    # under their own IDs: a problem that a Div. 1 and a Div. 2 round shared
    # is in the problemset once, under one of the two. The names of the rest
    # are left out, since problems of different rounds may share a name.
    names: frozenset[str]
    complete: bool  # False when not every submission could be read
    # The IDs lowercased, as AtCoder's are compared.
    _lowered_ids: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        lowered = frozenset(problem_id.lower() for problem_id in self.ids)
        object.__setattr__(self, '_lowered_ids', lowered)

    def contains(self, problem: Problem) -> bool:
        """Whether the member solved ``problem``.

        An AtCoder problem is found by its ID whatever the case; a Codeforces
        one by its ID, or by its name.
        """
        if problem.platform == ATCODER:
            return problem.problem_id.lower() in self._lowered_ids
        return problem.problem_id in self.ids or problem.name in self.names


@dataclass
class _AtCoderProgress:
    """What has been read of one AtCoder user's submissions."""

    accepted: set[str] = field(default_factory=set)  # their solved problems' IDs
    # Where the next page starts: the second of the last submission read, as
    # AtCoder Problems' bound includes it. The submissions of that second are
    # read again, which adds no problem twice.
    from_second: int = 0
    pages: int = 0  # how many pages have been read
    complete: bool = False  # the last page read was the last there was
    checked_at: datetime | None = None  # when it last was

    def solved_set(self) -> SolvedSet:
        return SolvedSet(frozenset(self.accepted), frozenset(), self.complete)


class SolvedProblems:
    """Members' solved problems on Codeforces and AtCoder, cached per handle.

    Lookups of one handle take turns on AtCoder, and share one request on
    Codeforces, so that a second waits for the first's requests instead of
    making them again. ``listed`` says which IDs Codeforces' problemset lists
    (none by default, so that every name solved is matched), and ``close``
    stops the Codeforces requests still running.
    """

    def __init__(
        self,
        atcoder: AtCoderProblemsClient,
        clock: Clock,
        *,
        listed: Listed = _nothing_listed,
    ) -> None:
        self._atcoder = atcoder
        self._clock = clock
        self._listed = listed
        self._codeforces: dict[str, tuple[datetime, SolvedSet | None]] = {}
        # Each handle's request that is running, by the handle in lower case.
        self._codeforces_fetches: dict[str, asyncio.Task[SolvedSet | None]] = {}
        # Until when Codeforces is left alone, after a request that failed.
        self._codeforces_paused_until: datetime | None = None
        self._atcoder_progress: dict[str, _AtCoderProgress] = {}
        self._atcoder_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def codeforces(self, handle: str) -> SolvedSet | None:
        """The problems the Codeforces user ``handle`` solved; None if there is
        no such user.

        Kept per handle, whatever its case, for ``SOLVED_TTL``. A caller that
        stops waiting leaves the request to finish, and its answer to be kept.
        Raises ``ExternalServiceError`` if Codeforces fails, and for
        ``CODEFORCES_PAUSE`` after a request that failed, without asking it.
        """
        key = handle.lower()
        now = self._clock.now()
        cached = self._codeforces.get(key)
        if cached is not None and now - cached[0] < SOLVED_TTL:
            return cached[1]
        fetch = self._codeforces_fetches.get(key)
        if fetch is None:
            paused_until = self._codeforces_paused_until
            if paused_until is not None and now < paused_until:
                raise ExternalServiceError(_CODEFORCES, _PAUSED)
            fetch = asyncio.create_task(self._fetch_codeforces(handle))
            self._codeforces_fetches[key] = fetch
            fetch.add_done_callback(self._forget_codeforces_fetch)
        # Shielded: a caller that is cancelled doesn't cancel the request.
        return await asyncio.shield(fetch)

    async def close(self) -> None:
        """Stop the Codeforces requests still running, and wait for them."""
        fetches = list(self._codeforces_fetches.values())
        for fetch in fetches:
            fetch.cancel()
        await asyncio.gather(*fetches, return_exceptions=True)

    async def _fetch_codeforces(self, handle: str) -> SolvedSet | None:
        """Ask Codeforces what ``handle`` solved, and keep the answer."""
        asked_at = self._clock.now()
        try:
            # Through the module, so that tests can stand in for TLE's client.
            solved = await codeforces.fetch_solved(handle)
        except ExternalServiceError:
            self._codeforces_paused_until = self._clock.now() + CODEFORCES_PAUSE
            raise
        found = None
        if solved is not None:
            names = frozenset(
                name
                for problem_id, name in solved.named
                if not self._listed(problem_id)
            )
            found = SolvedSet(solved.ids, names, complete=True)
        self._codeforces[handle.lower()] = (asked_at, found)
        return found

    def _forget_codeforces_fetch(self, fetch: asyncio.Task[SolvedSet | None]) -> None:
        """Drop a request that has finished, its answer kept if it had one.

        Its error is read here, since every caller may have stopped waiting:
        asyncio would log it as never retrieved otherwise.
        """
        for key, running in list(self._codeforces_fetches.items()):
            if running is fetch:
                del self._codeforces_fetches[key]
        if not fetch.cancelled():
            fetch.exception()

    async def atcoder(self, handle: str) -> SolvedSet:
        """The problems the AtCoder user ``handle`` solved, whatever its case.

        Reads the user's submissions from where the last call stopped, a page
        at a time, until a page has fewer than ``SUBMISSIONS_PAGE``; once that
        was more than ``SOLVED_TTL`` ago, it reads on from there again. Each
        page is saved as it is read, so a call that is cancelled keeps what it
        read (see ``cached_atcoder``). An unknown user has solved nothing.
        Raises ``ExternalServiceError`` if AtCoder Problems fails, and
        ``KcpcUserError`` if ``handle`` can't be an AtCoder username.
        """
        key = handle.lower()
        async with self._atcoder_locks[key]:
            progress = self._atcoder_progress.setdefault(key, _AtCoderProgress())
            checked_at = progress.checked_at
            fresh = checked_at is not None and (
                self._clock.now() - checked_at < SOLVED_TTL
            )
            if progress.complete and fresh:
                return progress.solved_set()
            # It is complete again only once the last page is read anew.
            progress.complete = False
            while not progress.complete:
                page = await self._atcoder.fetch_submissions(
                    handle, progress.from_second
                )
                _read_page(progress, page)
                if progress.complete:
                    progress.checked_at = self._clock.now()
            return progress.solved_set()

    def cached_atcoder(self, handle: str) -> SolvedSet | None:
        """What has been read so far of the AtCoder user's solved problems.

        For a caller that stopped waiting for ``atcoder``: ``complete`` says
        whether the last page was read. None if nothing has been read yet.
        """
        progress = self._atcoder_progress.get(handle.lower())
        if progress is None or progress.pages == 0:
            return None
        return progress.solved_set()


def _read_page(progress: _AtCoderProgress, page: list[AtCoderSubmission]) -> None:
    """Add a page of submissions, from ``progress.from_second`` on, to ``progress``."""
    progress.accepted.update(
        submission.problem_id for submission in page if submission.result == _ACCEPTED
    )
    if page:
        last = page[-1].epoch_second
        if page[0].epoch_second == last and len(page) == SUBMISSIONS_PAGE:
            # A whole page in one second: starting there again would read the
            # same page forever. More in that second are lost, but no one
            # submits 500 times a second.
            progress.from_second = last + 1
        else:
            progress.from_second = last
    progress.pages += 1
    progress.complete = len(page) < SUBMISSIONS_PAGE
