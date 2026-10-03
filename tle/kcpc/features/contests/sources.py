"""Where the contests feature gets each platform's contests: its sources.

Each source is a ``ContestSource`` over a platform adapter
(``tle.kcpc.platforms``), giving ``ContestSync`` the contests as
``ContestInfo``:

- Codeforces: TLE's cache of Codeforces' contest list, which TLE keeps up to
  date. It has every upcoming round, so its snapshots are complete.
- AtCoder: the upcoming table of AtCoder's contest list, complete too.
- ICPC: the contests of the configured icpc.global codes, fetched one by one.
  icpc.global publishes only their dates, so they are known by date until an
  admin sets a time. A snapshot says nothing of the contests it wasn't asked
  for, so it is incomplete.
- CodeChef, LeetCode, TopCoder and the ICPC World Finals: clist.by's lists of
  those sites' contests, read with the bot's clist.by account (see
  ``clist_sources``). The first three are complete. The World Finals are ICPC
  contests, which the ICPC source lists too, so theirs are not.
"""

import logging
from collections.abc import Callable, Sequence

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError
from tle.kcpc.features.contests.repo import ContestInfo
from tle.kcpc.features.contests.sync import SourceSnapshot
from tle.kcpc.platforms import clist, codeforces, icpc
from tle.kcpc.platforms.atcoder import contests as atcoder
from tle.util import codeforces_api as cf

logger = logging.getLogger(__name__)

# The contests in TLE's Codeforces cache: all of them, finished ones too.
CachedContests = Sequence[cf.Contest]

_CODEFORCES_NOT_LOADED = "TLE hasn't loaded Codeforces' contest list yet."


class CodeforcesSource:
    """Codeforces' contests that haven't ended, from TLE's cache of its list.

    ``contests_provider`` gives the contests in the cache, which TLE fills as
    the bot starts. Codeforces lists thousands of past contests, so an empty
    cache is one that hasn't been filled yet: a snapshot of it would miss
    every stored round, so fetching raises ``ExternalServiceError`` instead,
    and the sync counts as failed.
    """

    def __init__(
        self, contests_provider: Callable[[], CachedContests], clock: Clock
    ) -> None:
        self._contests = contests_provider
        self._clock = clock

    @property
    def name(self) -> str:
        return codeforces.PLATFORM

    @property
    def platform(self) -> str:
        return codeforces.PLATFORM

    async def fetch(self) -> SourceSnapshot:
        cached = self._contests()
        if not cached:
            raise ExternalServiceError('Codeforces', _CODEFORCES_NOT_LOADED)
        upcoming = codeforces.upcoming_contests(cached, now=self._clock.now())
        return SourceSnapshot(
            [
                ContestInfo(
                    platform=codeforces.PLATFORM,
                    external_id=str(contest.contest_id),
                    name=contest.name,
                    start=contest.start,
                    start_date=None,
                    end=contest.end,
                    url=contest.url,
                )
                for contest in upcoming
            ],
            complete=True,
        )


class AtCoderSource:
    """AtCoder's upcoming contests, from the upcoming table of its contest list.

    Like Codeforces' snapshots, its snapshots leave out contests that have
    ended, should the page still list one by the time it is read.
    """

    def __init__(self, client: atcoder.AtCoderContestsClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @property
    def name(self) -> str:
        return atcoder.PLATFORM

    @property
    def platform(self) -> str:
        return atcoder.PLATFORM

    async def fetch(self) -> SourceSnapshot:
        listed = await self._client.fetch_upcoming()
        now = self._clock.now()
        return SourceSnapshot(
            [
                ContestInfo(
                    platform=atcoder.PLATFORM,
                    external_id=contest.contest_id,
                    name=contest.name,
                    start=contest.start,
                    start_date=None,
                    end=contest.end,
                    url=contest.url,
                )
                for contest in listed
                if contest.end > now
            ],
            complete=True,
        )


class IcpcSource:
    """The ICPC contests of the configured codes, such as 'UKIEPC', by date.

    icpc.global publishes no start times, so each contest is listed by its
    event's first day, as 'time TBA', until an admin sets the contest's own
    time with /kcpc contests settime (``ContestRepo.set_time``). Until then, a
    contest whose event lasts several days, such as NWERC (27-29 November
    2026), drops out of /contests upcoming from that first day. A code that
    icpc.global doesn't know is skipped, with a warning the first time;
    another goes out if it is found and then lost again. If any code can't
    be fetched or read, the fetch fails as a whole.
    """

    def __init__(self, client: icpc.IcpcClient, codes: Sequence[str]) -> None:
        self._client = client
        self._codes = tuple(codes)
        # The unknown codes already warned about: the source is synced every
        # few hours for as long as the bot runs.
        self._unknown: set[str] = set()

    @property
    def name(self) -> str:
        return icpc.PLATFORM

    @property
    def platform(self) -> str:
        return icpc.PLATFORM

    async def fetch(self) -> SourceSnapshot:
        contests: list[ContestInfo] = []
        for code in self._codes:
            contest = await self._client.fetch(code)
            if contest is None:
                self._report_unknown(code)
                continue
            self._unknown.discard(code)
            contests.append(
                ContestInfo(
                    platform=icpc.PLATFORM,
                    external_id=contest.contest_id,
                    name=contest.name,
                    start=None,
                    start_date=contest.start_date,
                    end=None,
                    url=contest.url,
                )
            )
        return SourceSnapshot(contests, complete=False)

    def _report_unknown(self, code: str) -> None:
        if code in self._unknown:
            return
        self._unknown.add(code)
        logger.warning(
            'icpc.global has no contest with the code %s: check ICPC_CONTEST_CODES',
            code,
        )


class ClistSource:
    """A site's contests that haven't ended, as clist.by lists them.

    ``resource`` is the site on clist.by, by its host ('codechef.com'), and
    ``event_regex`` keeps only its contests whose name matches, ignoring case.
    ``name`` is the source's own name, which its sync job and state go by, and
    ``platform`` the platform of its contests. Its snapshots are ``complete``
    unless another source lists contests of that platform too: a contest that
    this one doesn't list may be one of those.
    """

    def __init__(
        self,
        client: clist.ClistClient,
        *,
        name: str,
        platform: str,
        resource: str,
        complete: bool,
        event_regex: str | None = None,
    ) -> None:
        self._client = client
        self._name = name
        self._platform = platform
        self._resource = resource
        self._complete = complete
        self._event_regex = event_regex

    @property
    def name(self) -> str:
        return self._name

    @property
    def platform(self) -> str:
        return self._platform

    async def fetch(self) -> SourceSnapshot:
        listed = await self._client.upcoming(
            self._resource, event_regex=self._event_regex
        )
        return SourceSnapshot(
            [
                ContestInfo(
                    platform=self._platform,
                    external_id=f'clist-{contest.clist_id}',
                    name=contest.name,
                    start=contest.start,
                    start_date=None,
                    end=contest.end,
                    url=contest.url,
                )
                for contest in listed
            ],
            complete=self._complete,
        )


def clist_sources(client: clist.ClistClient) -> tuple[ClistSource, ...]:
    """The sources read through clist.by: CodeChef, LeetCode, TopCoder and the
    ICPC World Finals.

    The World Finals are ICPC contests, which ``IcpcSource`` lists too, so
    their snapshots are incomplete: a complete one would count the contests of
    ``IcpcSource`` as missing. The regionals, such as UKIEPC and NWERC, stay
    with ``IcpcSource``.
    """
    return (
        ClistSource(
            client,
            name='codechef',
            platform='codechef',
            resource='codechef.com',
            complete=True,
        ),
        ClistSource(
            client,
            name='leetcode',
            platform='leetcode',
            resource='leetcode.com',
            complete=True,
        ),
        ClistSource(
            client,
            name='topcoder',
            platform='topcoder',
            resource='topcoder.com',
            complete=True,
        ),
        ClistSource(
            client,
            name='icpc-world-finals',
            platform=icpc.PLATFORM,
            resource='icpc.global',
            complete=False,
            event_regex='world finals',
        ),
    )
