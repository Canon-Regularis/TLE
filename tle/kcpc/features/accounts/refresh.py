"""Keeps the rating snapshots of linked accounts up to date.

Codeforces users are fetched through TLE's ``user.info``, up to 300 a request,
and AtCoder profiles one page at a time, as the shared HTTP client paces them.
Each snapshot is saved as soon as it is fetched, so a refresh that fails part
way keeps what it got, and an account that can't be fetched keeps its last
snapshot without holding up the others.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.features.accounts.repo import AccountRepo
from tle.kcpc.features.accounts.service import ATCODER, CODEFORCES, Profile
from tle.kcpc.platforms import codeforces
from tle.kcpc.platforms.atcoder.profile import AtCoderProfileClient

logger = logging.getLogger(__name__)

# The most Codeforces handles that one user.info request asks about.
CODEFORCES_BATCH = 300
# A refresh of every account that couldn't fetch some logs at WARNING at most
# this often, and at INFO in between: a site that is down fails every refresh.
_FAILURE_WARNING_INTERVAL = timedelta(days=1)

# An account: (platform, handle).
Account = tuple[str, str]


@dataclass(frozen=True)
class RefreshReport:
    """What a refresh did with each account, handles as they were asked for."""

    saved: tuple[Account, ...] = ()  # a new snapshot was saved
    missing: tuple[Account, ...] = ()  # the platform has no such user now
    failed: tuple[Account, ...] = ()  # the platform couldn't be asked


@dataclass
class _Tally:
    saved: list[Account] = field(default_factory=list)
    missing: list[Account] = field(default_factory=list)
    failed: list[Account] = field(default_factory=list)

    def report(self) -> RefreshReport:
        return RefreshReport(tuple(self.saved), tuple(self.missing), tuple(self.failed))


class RatingRefresher:
    """Fetches linked accounts' ratings and saves them as snapshots."""

    def __init__(
        self, repo: AccountRepo, atcoder: AtCoderProfileClient, clock: Clock
    ) -> None:
        self._repo = repo
        self._atcoder = atcoder
        self._clock = clock
        self._last_warning: datetime | None = None

    async def refresh_all(
        self, codeforces_handles: Iterable[str], atcoder_handles: Iterable[str]
    ) -> RefreshReport:
        """Refresh every account given, each handle once whatever its case."""
        report = await self._refresh(codeforces_handles, atcoder_handles)
        self._log_refresh(report)
        return report

    async def refresh_member(self, accounts: Iterable[Account]) -> RefreshReport:
        """Refresh one member's accounts now, as /profile does when they are stale.

        Failures are left to the caller to show, so they are logged quietly.
        """
        codeforces_handles: list[str] = []
        atcoder_handles: list[str] = []
        for platform, handle in accounts:
            if platform == CODEFORCES:
                codeforces_handles.append(handle)
            elif platform == ATCODER:
                atcoder_handles.append(handle)
            else:
                raise ValueError(f'Accounts cannot be linked on {platform!r}')
        return await self._refresh(codeforces_handles, atcoder_handles)

    async def _refresh(
        self, codeforces_handles: Iterable[str], atcoder_handles: Iterable[str]
    ) -> RefreshReport:
        tally = _Tally()
        await self._refresh_codeforces(_distinct(codeforces_handles), tally)
        await self._refresh_atcoder(_distinct(atcoder_handles), tally)
        return tally.report()

    async def _refresh_codeforces(self, handles: list[str], tally: _Tally) -> None:
        for start in range(0, len(handles), CODEFORCES_BATCH):
            batch = handles[start : start + CODEFORCES_BATCH]
            try:
                users = await codeforces.fetch_users(batch)
            except ExternalServiceError as exc:
                logger.info(
                    'Could not refresh %d Codeforces users: %s', len(batch), exc
                )
                tally.failed += [(CODEFORCES, handle) for handle in batch]
                continue
            now = self._clock.now()
            await self._repo.save_snapshots(
                [Profile.from_codeforces(user).snapshot(now) for user in users]
            )
            # Codeforces gives each handle in its own case, which may not be
            # the case it was asked in.
            found = {user.handle.lower() for user in users}
            for handle in batch:
                fetched = handle.lower() in found
                (tally.saved if fetched else tally.missing).append((CODEFORCES, handle))

    async def _refresh_atcoder(self, handles: list[str], tally: _Tally) -> None:
        for handle in handles:
            account = (ATCODER, handle)
            try:
                profile = await self._atcoder.fetch(handle)
            except ExternalServiceError as exc:
                logger.info('Could not refresh AtCoder user %s: %s', handle, exc)
                tally.failed.append(account)
                continue
            except KcpcUserError:  # it can't be an AtCoder username
                profile = None
            if profile is None:
                tally.missing.append(account)
                continue
            snapshot = Profile.from_atcoder(profile).snapshot(self._clock.now())
            await self._repo.save_snapshots([snapshot])
            tally.saved.append(account)

    def _log_refresh(self, report: RefreshReport) -> None:
        level = logging.INFO
        if report.failed:
            now = self._clock.now()
            last = self._last_warning
            if last is None or now - last >= _FAILURE_WARNING_INTERVAL:
                level = logging.WARNING
                self._last_warning = now
        logger.log(
            level,
            'Refreshed the ratings of linked accounts: %d saved, %d not found, '
            '%d could not be fetched',
            len(report.saved),
            len(report.missing),
            len(report.failed),
        )


def _distinct(handles: Iterable[str]) -> list[str]:
    """``handles`` in order, each once whatever its case."""
    distinct: dict[str, str] = {}
    for handle in handles:
        distinct.setdefault(handle.lower(), handle)
    return list(distinct.values())
