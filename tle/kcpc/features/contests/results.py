"""Contest results: members' rating changes, posted after Codeforces and AtCoder
contests.

A server with results posts on (``ContestSettings.results_posts``) gets a post
in its contests channel after each Codeforces or AtCoder contest, of the
platforms it follows, in which some of its members' ratings changed: a line
for each of them, with their handle and rating change, the biggest gains
first. It mentions no role, and a contest in which no member was rated gets
no post. A member counts with the handle they linked: Codeforces handles are
TLE's, AtCoder handles the accounts feature's. The cog says which members a
server has, and whether the bot is in it.

Contest results start on an install at the bot's first run with them, and
kcpc.db keeps when (``ResultRepo.start``): the results of a contest that
ended before then are old news, and never posted. At that first run, the
Codeforces contests that TLE lists as finished in the last 48 hours are
stored as missed, and any other contest that ended before is when TLE
reports it.

Codeforces: TLE saves each rated contest's rating changes as Codeforces
publishes them, then tells its listeners, and the cog passes that on to
``report_codeforces``. TLE tells them only once, and not again after a
restart, so the results job also reads the rating changes that TLE saved for
the contests that ended in the last 48 hours and haven't been dealt with. It
never asks Codeforces itself.

AtCoder has no rating changes that a bot may read, only users' profiles,
with their rating and their number of rated matches. In the last 30 minutes
of an ABC, ARC or AGC, the job reads the profile of each handle that a member
of a server that wants the results has linked: its baseline. From 15 minutes
after the end, and then every 30 minutes for up to 6 hours, it reads them
again. A handle whose rated matches went up took part, and its rating change
is the difference. AtCoder rates a contest's participants all at once, so at
the first read that finds a change, the handles read before that one are read
again, in case they were read just before the ratings came out, and then the
results are posted. Without a change in 6 hours, nothing is; if the job isn't
running when those 6 hours run out, the contest is missed.

Contests watched at the same time watch the same handles, and a contest
posts only the rises in rated matches that it owns (see ``_owner_of``): a
rise goes to the one that ended first, which is read first, whatever its own
schedule, unless a later one's baseline of the handle, read over an hour
after the first ended (when AtCoder had rated it), had the same rated
matches. Once a contest claims a rise, the handle's baseline in the others
still watched is raised past it. Profiles can't tell apart contests that end
together, or that AtCoder rates between the same two reads, nor, when no
member took part in the first, the first and a later one whose baseline was
read within an hour of the first's end: such rises go to the first, by end
and then by ID. And a first contest that AtCoder rates over an hour after its
end can lose its rises to a later one. AtCoder's standings are out of bounds
for bots, so there are no ranks.

Each post's delivery key holds the server, the platform and the contest, so
nothing is posted twice, however the event and the job overlap. A post that
can't be delivered yet (UNDELIVERABLE) is tried again by later runs of the
job, until 48 hours after the contest ended.
"""

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import DESCRIPTION_LIMIT, OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, Publisher
from tle.kcpc.core.settings import GuildSettingsRepo
from tle.kcpc.core.timeutil import describe_duration, from_epoch
from tle.kcpc.features.contests.reminders import FOOTER, SUBJECT, platform_line
from tle.kcpc.features.contests.repo import ContestRepo, StoredContest
from tle.kcpc.features.contests.results_repo import (
    ATCODER,
    CODEFORCES,
    Claimant,
    ProfileReading,
    ResultContest,
    ResultEntry,
    ResultOutcome,
    ResultRecord,
    ResultRepo,
    ResultStatus,
)
from tle.kcpc.features.contests.settings import CONTESTS, contest_settings
from tle.kcpc.platforms.atcoder.profile import (
    PROFILE_URL as ATCODER_PROFILE_URL,
    AtCoderProfile,
)
from tle.kcpc.platforms.codeforces import (
    PROFILE_URL as CODEFORCES_PROFILE_URL,
    rank_name,
    rating_changes,
)
from tle.util import codeforces_api as cf

logger = logging.getLogger(__name__)

RESULTS_JOB = 'contests.results'
RESULTS_INTERVAL = timedelta(minutes=5)
KIND = 'results'  # the ledger's kind of a results post

# How long after a Codeforces contest's end the job looks for its rating
# changes in TLE's cache: TLE stops looking for them after 36 hours.
_CODEFORCES_WINDOW = timedelta(hours=48)
_FINISHED = 'FINISHED'
# AtCoder's rated algorithm contests. A heuristic contest (AHC) never changes
# the rated matches that profiles show.
_ATCODER_SERIES = re.compile(r'(abc|arc|agc)\d+')
# Baselines are taken in a contest's last half hour, when its own ratings
# can't be out, and an earlier contest's have usually come out.
_BASELINE_LEAD = timedelta(minutes=30)
# Profiles are read from this long after the end (ABC ratings often come out
# 10 to 20 minutes after it), then at this interval, for this long.
_FIRST_CHECK = timedelta(minutes=15)
_CHECK_INTERVAL = timedelta(minutes=30)
_WATCH_FOR = timedelta(hours=6)
# AtCoder rates a contest within this long of its end, as a rule, so a
# profile read later has the contest's rated match if the user took part.
_RATED_WITHIN = timedelta(hours=1)
# Posts that can't be delivered are tried again until this long after the end.
_POST_FOR = timedelta(hours=48)
# How long a claimed post may still be sent again (see the publisher).
_DELIVERY_LIFETIME = timedelta(days=1)

# The most members a post lists; fewer if their lines would be too long.
_MAX_LINES = 30
# The longest description a post's lines may take, keeping room for the line
# that says how many more there are.
_DESCRIPTION_BUDGET = DESCRIPTION_LIMIT - 50
# The outcomes of a post that went out, or may have.
_WENT_OUT = frozenset(
    {PublishOutcome.SENT, PublishOutcome.PENDING, PublishOutcome.ALREADY_HANDLED}
)
_POSTED = frozenset({DeliveryStatus.SENT, DeliveryStatus.CLAIMED})
# The characters that Discord's markdown gives a meaning to within a line.
_MARKDOWN = re.compile(r'([\\\[\]*_~`|])')

# (user_id, handle) of each member of the server with the given ID who linked
# a handle on the given platform.
LinkedMembers = Callable[[int, str], Awaitable[list[tuple[int, str]]]]
# Whether the bot is in the server with this ID.
InGuild = Callable[[int], bool]
# The contests in TLE's Codeforces cache.
CachedContests = Callable[[], Iterable[cf.Contest]]
# The rating changes of the Codeforces contest with this ID, as TLE saved
# them; none if it has none.
SavedChanges = Callable[[int], Awaitable[Sequence[cf.RatingChange]]]


class Profiles(Protocol):
    """Where AtCoder users' profiles come from: ``AtCoderProfileClient``."""

    async def fetch(self, handle: str) -> AtCoderProfile | None: ...


def _in_every_guild(guild_id: int) -> bool:
    """Counts the bot in every server, so that none is skipped."""
    return True


def _no_contests() -> Iterable[cf.Contest]:
    """No Codeforces contests: a bot without TLE's cache."""
    return ()


async def _no_changes(contest_id: int) -> Sequence[cf.RatingChange]:
    """No saved rating changes: a bot without TLE's cache."""
    return ()


def results_key(guild_id: int, platform: str, external_id: str) -> str:
    """The delivery key of the post of a contest's results in the guild."""
    return f'results:{guild_id}:{platform}:{external_id}'


class ContestResults:
    """Finds and posts members' rating changes; see the module docstring.

    ``report_codeforces`` and the job's ``run`` take turns on each contest,
    and the ledger keeps any post from going out twice.
    """

    def __init__(
        self,
        contests: ContestRepo,
        results: ResultRepo,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        publisher: Publisher,
        atcoder: Profiles,
        clock: Clock,
        *,
        linked_members: LinkedMembers,
        in_guild: InGuild = _in_every_guild,
        codeforces_contests: CachedContests = _no_contests,
        codeforces_changes: SavedChanges = _no_changes,
    ) -> None:
        self._contests = contests
        self._results = results
        self._guild_settings = guild_settings
        self._ledger = ledger
        self._publisher = publisher
        self._atcoder = atcoder
        self._clock = clock
        self._linked_members = linked_members
        self._in_guild = in_guild
        self._codeforces_contests = codeforces_contests
        self._codeforces_changes = codeforces_changes
        # By contest key: held while a contest's results are worked on.
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._start_lock = asyncio.Lock()
        # When contest results started on this install, once read or noted.
        self._started: datetime | None = None

    async def report_codeforces(
        self, contest: cf.Contest, changes: Iterable[cf.RatingChange]
    ) -> None:
        """Post the rating changes of the members who took part in a finished
        Codeforces contest, in each server that wants them.

        ``changes`` are the contest's, as TLE saved them. Nothing is done if
        the contest was dealt with before, and a contest that ended before
        contest results started is stored as missed. Its linked handles'
        changes are stored before anything is posted, and a server whose post
        can't be delivered yet gets it from a later run of the job.
        """
        now = self._clock.now()
        started = await self._start(now)
        found = _codeforces_contest(contest)
        if found is None:
            return
        async with self._locks[found.key]:
            if await self._results.get(CODEFORCES, found.external_id) is not None:
                return
            if found.end < started:
                await self._results.add_done([found], ResultOutcome.MISSED, now=now)
                logger.info(
                    'Not posting the results of Codeforces contest %s: it ended '
                    'before contest results started',
                    found.external_id,
                )
                return
            linked: set[str] = set()
            for guild_id in await self._interested_guilds(CODEFORCES):
                members = await self._linked_members(guild_id, CODEFORCES)
                linked.update(handle.lower() for _, handle in members)
            entries = [
                ResultEntry(
                    handle=change.handle,
                    old_rating=change.old_rating,
                    new_rating=change.new_rating,
                    noted_at=now,
                    changed_at=now,
                    place=change.place,
                )
                for change in rating_changes(
                    change for change in changes if change.handle.lower() in linked
                )
            ]
            if not entries:
                await self._results.add_done([found], ResultOutcome.NOBODY, now=now)
                logger.info(
                    'No member of a server that wants the results took part in '
                    'Codeforces contest %s',
                    found.external_id,
                )
                return
            record = await self._results.start_codeforces(found, entries, now=now)
            logger.info(
                'Found the rating changes of %d linked handles in Codeforces '
                'contest %s',
                len(entries),
                found.external_id,
            )
            if record.status is ResultStatus.POSTING:
                await self._publish(record, now)

    async def run(self, now: datetime) -> None:
        """Do what is due at ``now``: the results job's handler.

        In turn: the Codeforces contests whose rating changes TLE saved
        without the bot hearing of them, the baselines of the AtCoder
        contests ending within half an hour, the reads of the AtCoder
        profiles that are due, and the posts that earlier runs couldn't
        deliver. Each contest is dealt with on its own, and one that fails is
        logged at INFO. Then, if any failed, raises ``RuntimeError`` (from the
        first failure), so that the scheduler reports it.
        """
        await self._start(now)
        failures: list[Exception] = []
        # Read first, so that a post this run can't deliver waits for the next.
        undelivered = await self._results.with_status(ResultStatus.POSTING)
        await self._catch_up_codeforces(now, failures)
        ending = 'the AtCoder contests ending soon'
        await self._guarded(ending, self._baselines(now), failures)
        # By handle in lower case: each profile is read once a run, for all
        # the contests that read it.
        readings: dict[str, ProfileReading | None] = {}
        for record in await self._checks_due(now):
            check = self._check(record, now, readings)
            await self._guarded(record.key, check, failures)
        for record in undelivered:
            await self._guarded(record.key, self._retry(record, now), failures)
        if failures:
            raise RuntimeError(
                f'Could not deal with contest results ({len(failures)} failed)'
            ) from failures[0]

    async def _guarded(
        self, what: str, action: Awaitable[None], failures: list[Exception]
    ) -> None:
        """Await ``action``; if it fails, log that at INFO and note the failure."""
        try:
            await action
        except Exception as exc:
            logger.info('Could not deal with the results of %s', what, exc_info=True)
            failures.append(exc)

    async def _start(self, now: datetime) -> datetime:
        """When contest results started on this install; ``now`` if they
        hadn't (see the module docstring), before anything else is done.
        """
        started = self._started
        if started is None:
            async with self._start_lock:
                started = self._started
                if started is None:
                    started = self._started = await self._begin(now)
        return started

    async def _begin(self, now: datetime) -> datetime:
        """Start contest results ``now``, unless they started before, as
        kcpc.db says after a restart; return when they did.
        """
        started = await self._results.started_at()
        if started is not None:
            return started
        since = now - _CODEFORCES_WINDOW
        missed = [
            found
            for contest in self._codeforces_contests()
            if contest.phase == _FINISHED
            and (found := _codeforces_contest(contest)) is not None
            and since <= found.end < now
        ]
        started, added = await self._results.start(missed, now=now)
        if added is not None:
            logger.info(
                'Contest results start now: not posting the results of contests '
                'that ended before now, such as the %d Codeforces contests that '
                'TLE lists as finished in the last 48 hours',
                added,
            )
        return started

    async def _catch_up_codeforces(
        self, now: datetime, failures: list[Exception]
    ) -> None:
        """Report each Codeforces contest that ended in the last 48 hours, isn't
        dealt with, and has rating changes saved in TLE's cache.
        """
        since = now - _CODEFORCES_WINDOW
        for contest in list(self._codeforces_contests()):
            found = _codeforces_contest(contest)
            if contest.phase != _FINISHED or found is None:
                continue
            if not since <= found.end <= now:
                continue
            if await self._results.get(CODEFORCES, found.external_id) is not None:
                continue
            await self._guarded(found.key, self._catch_up(contest), failures)

    async def _catch_up(self, contest: cf.Contest) -> None:
        changes = await self._codeforces_changes(contest.id)
        if changes:  # none until Codeforces publishes them, or ever if unrated
            await self.report_codeforces(contest, changes)

    async def _interested_guilds(self, platform: str) -> list[int]:
        """The servers that want the results of the platform's contests.

        That is those that the bot is in, with the contests feature on, a
        channel, results posts on and the platform among their platforms.
        """
        guilds: list[int] = []
        for guild_id, stored in await self._guild_settings.enabled_guilds(CONTESTS):
            settings = contest_settings(stored)
            if (
                settings.channel_id is None
                or not settings.results_posts
                or platform not in settings.platforms
            ):
                continue
            if not self._in_guild(guild_id):
                logger.debug(
                    'Leaving out guild %d from the contest results: the bot is '
                    'not in it',
                    guild_id,
                )
                continue
            guilds.append(guild_id)
        return guilds

    async def _baselines(self, now: datetime) -> None:
        """Take the missing baselines of the AtCoder contests ending within
        half an hour, for each handle that a member of an interested server
        has linked.

        Each handle's profile is read once for all of them. If AtCoder fails,
        the rest wait for the next run, still before the end.
        """
        ending = sorted(
            (
                contest
                for contest in await self._contests.live(now, platforms=[ATCODER])
                if _ATCODER_SERIES.fullmatch(contest.external_id)
                and contest.end is not None
                and contest.end - now <= _BASELINE_LEAD
            ),
            key=lambda contest: (contest.end, contest.external_id),
        )
        if not ending:
            return
        handles: dict[str, str] = {}
        for guild_id in await self._interested_guilds(ATCODER):
            for _, handle in await self._linked_members(guild_id, ATCODER):
                handles.setdefault(handle.lower(), handle)
        readings: dict[str, ProfileReading | None] = {}
        for contest in ending:
            record = await self._results.get(ATCODER, contest.external_id)
            if record is not None and record.status is not ResultStatus.WATCHING:
                continue
            entries = (
                []
                if record is None
                else await self._results.entries(ATCODER, contest.external_id)
            )
            taken = {entry.handle.lower() for entry in entries}
            baselines: list[ProfileReading] = []
            failed = False
            for key, handle in sorted(handles.items()):
                if key in taken:
                    continue
                if key not in readings:
                    try:
                        readings[key] = await self._read(handle)
                    except ExternalServiceError as exc:
                        logger.info(
                            'Could not take the baselines of AtCoder contest %s: %s',
                            contest.external_id,
                            exc,
                        )
                        failed = True
                        break
                reading = readings[key]
                if reading is not None:
                    baselines.append(reading)
            if baselines:
                end = _end_of(contest)
                added = await self._results.save_baselines(
                    _atcoder_contest(contest, end),
                    baselines,
                    next_check=end + _FIRST_CHECK,
                    now=now,
                )
                logger.info(
                    'Took the baselines of %d AtCoder handles for contest %s',
                    added,
                    contest.external_id,
                )
            if failed:
                return

    async def _checks_due(self, now: datetime) -> list[ResultRecord]:
        """The watched AtCoder contests to read at ``now``, by end: those due a
        read, and with them every other one that ended before one of them.

        So a contest that ended first is read first, whatever its own
        schedule, and posts the rises it owns before a later one reads them.
        """
        due = await self._results.due(now)
        if not due:
            return []
        last = max(record.end for record in due)
        return [
            record
            for record in await self._results.with_status(ResultStatus.WATCHING)
            if record.end <= last
        ]

    async def _check(
        self,
        record: ResultRecord,
        now: datetime,
        readings: dict[str, ProfileReading | None],
    ) -> None:
        """Read the profiles of a watched AtCoder contest's handles that haven't
        changed yet, and post its results once they have.

        ``readings`` are the profiles read earlier in the run, by handle in
        lower case, which gains the profiles read here.
        """
        async with self._locks[record.key]:
            current = await self._results.get(record.platform, record.external_id)
            if current is None or current.status is not ResultStatus.WATCHING:
                return
            if now > current.end + _WATCH_FOR:
                await self._close_watch(current, now)
                return
            entries = await self._results.entries(current.platform, current.external_id)
            if not entries:
                await self._results.finish(current, ResultOutcome.MISSED, now=now)
                return
            await self._read_changes(current, entries, now, readings)

    async def _read_changes(
        self,
        record: ResultRecord,
        entries: Sequence[ResultEntry],
        now: datetime,
        readings: dict[str, ProfileReading | None],
    ) -> None:
        """One read of the contest's profiles; see ``_check``.

        A rise in rated matches that another contest owns isn't a change
        here: that contest is read again from now on instead.
        """
        seen = any(entry.changed for entry in entries)
        changes: list[ProfileReading] = []
        dropped: list[str] = []
        # Read before any change was seen, so perhaps just before AtCoder
        # rated the contest: read again once a change is seen.
        early: list[ResultEntry] = []
        # By key, the other contests that own rises read here.
        owners: dict[str, Claimant] = {}
        complete = True
        try:
            for entry in entries:
                if entry.changed:
                    continue
                reading = await self._read_once(entry.handle, readings)
                if reading is None:
                    dropped.append(entry.handle)
                elif not _went_up(entry, reading):
                    if not (seen or changes):
                        early.append(entry)
                elif await self._owns(record, entry, reading, now, owners):
                    changes.append(reading)
            if changes and not seen:
                for entry in early:
                    reading = await self._read(entry.handle)
                    readings[entry.handle.lower()] = reading
                    if reading is None:
                        dropped.append(entry.handle)
                    elif _went_up(entry, reading) and await self._owns(
                        record, entry, reading, now, owners
                    ):
                        changes.append(reading)
        except ExternalServiceError as exc:
            # The handles not read are read at the next check.
            logger.info(
                "Could not read every profile for AtCoder contest %s's results: %s",
                record.external_id,
                exc,
            )
            complete = False
        for owner in owners.values():
            await self._results.check_now(owner.platform, owner.external_id, now=now)
        found = seen or bool(changes)
        if found and complete:
            record = await self._results.record_check(
                record, changes, dropped, now=now, found=True
            )
            logger.info(
                'Found the rating changes of AtCoder contest %s after %d checks',
                record.external_id,
                record.checks,
            )
            await self._publish(record, now)
            return
        if found:
            # The rest of the handles are read at the job's next run.
            await self._results.record_check(
                record, changes, dropped, now=now, next_check=now
            )
            return
        next_check = _next_check(record.end, now)
        if next_check is not None:
            await self._results.record_check(
                record, changes, dropped, now=now, next_check=next_check
            )
            return
        await self._results.record_check(
            record, changes, dropped, now=now, outcome=ResultOutcome.NOBODY
        )
        logger.info(
            'No rating changed in AtCoder contest %s within %s of its end%s',
            record.external_id,
            describe_duration(_WATCH_FOR),
            '' if complete else ' (AtCoder failed during the last read)',
        )

    async def _owns(
        self,
        record: ResultRecord,
        entry: ResultEntry,
        reading: ProfileReading,
        now: datetime,
        owners: dict[str, Claimant],
    ) -> bool:
        """Whether a rise in the rated matches of an entry's handle, read
        ``now``, is the contest's own; if another contest owns it, that one is
        added to ``owners``. A contest whose 6 hours of reads are over owns
        none.
        """
        claimants = [
            claimant
            for claimant in await self._results.claimants(
                record.platform, entry.handle, reading.matches, now=now
            )
            if now <= claimant.end + _WATCH_FOR
        ]
        owner = _owner_of(claimants)
        if owner is None or owner.key == record.key:
            return True
        owners[owner.key] = owner
        return False

    async def _close_watch(self, record: ResultRecord, now: datetime) -> None:
        """The contest's 6 hours of reads ran out while the job wasn't running,
        as when the bot was down: post what was found, if anything was. Else
        the contest is missed, as nothing read after then could tell.
        """
        entries = await self._results.entries(record.platform, record.external_id)
        if any(entry.changed for entry in entries):
            record = await self._results.start_posting(record, now=now)
            await self._publish(record, now)
            return
        await self._results.finish(record, ResultOutcome.MISSED, now=now)
        logger.info(
            'Stopped watching AtCoder contest %s after %d reads: the results job '
            'was not running when its %s of reads ran out',
            record.external_id,
            record.checks,
            describe_duration(_WATCH_FOR),
        )

    async def _retry(self, record: ResultRecord, now: datetime) -> None:
        """Post the contest's results where they couldn't be delivered before."""
        async with self._locks[record.key]:
            current = await self._results.get(record.platform, record.external_id)
            if current is not None and current.status is ResultStatus.POSTING:
                await self._publish(current, now)

    async def _publish(self, record: ResultRecord, now: datetime) -> None:
        """Post the contest's results in each interested server that hasn't had
        them, then mark the contest done, unless a post couldn't be delivered
        yet: then it is left for a later run, until 48 hours after the end.

        A server gets a post only if some of its members' ratings changed.
        """
        if now >= record.end + _POST_FOR:
            await self._results.finish(record, ResultOutcome.EXPIRED, now=now)
            logger.info(
                'Gave up posting the results of %s contest %s: it ended over %s ago',
                record.platform,
                record.external_id,
                describe_duration(_POST_FOR),
            )
            return
        changed = {
            entry.handle.lower(): entry
            for entry in await self._results.entries(
                record.platform, record.external_id
            )
            if entry.changed
        }
        posted = undelivered = False
        for guild_id in await self._interested_guilds(record.platform):
            key = results_key(guild_id, record.platform, record.external_id)
            earlier = await self._ledger.get(key)
            if earlier is not None:  # settled by an earlier run
                posted = posted or earlier.status in _POSTED
                continue
            members = [
                (user_id, changed[handle.lower()])
                for user_id, handle in await self._linked_members(
                    guild_id, record.platform
                )
                if handle.lower() in changed
            ]
            if not members:
                continue
            delivery = Delivery(
                key=key,
                guild_id=guild_id,
                feature=CONTESTS,
                subject=SUBJECT,
                subject_id=record.key,
                kind=KIND,
                expires_at=now + _DELIVERY_LIFETIME,
            )
            result = await self._publisher.publish(
                [delivery], results_post(record, members)
            )
            if result.outcome is PublishOutcome.UNDELIVERABLE:
                logger.info(
                    'The results of %s contest %s could not be posted in guild %d '
                    'yet (%s); trying again later',
                    record.platform,
                    record.external_id,
                    guild_id,
                    result.reason,
                )
                undelivered = True
            elif result.outcome in _WENT_OUT:
                posted = True
        if undelivered:
            return
        outcome = ResultOutcome.POSTED if posted else ResultOutcome.NOBODY
        await self._results.finish(record, outcome, now=now)

    async def _read_once(
        self, handle: str, readings: dict[str, ProfileReading | None]
    ) -> ProfileReading | None:
        """``_read``, unless ``readings`` have the profile from earlier in the
        run; else it is added to them.
        """
        key = handle.lower()
        if key not in readings:
            readings[key] = await self._read(handle)
        return readings[key]

    async def _read(self, handle: str) -> ProfileReading | None:
        """What the AtCoder profile of ``handle`` says; None if AtCoder has no
        such user, or the handle can't be one. ``ExternalServiceError`` if
        AtCoder fails.
        """
        try:
            profile = await self._atcoder.fetch(handle)
        except ExternalServiceError:
            raise
        except KcpcUserError:  # not a valid AtCoder username
            logger.debug('%r cannot be an AtCoder handle', handle)
            return None
        if profile is None:
            logger.info('AtCoder has no user %s now', handle)
            return None
        return ProfileReading(
            handle=profile.handle,
            rating=profile.rating,
            highest=profile.highest_rating,
            matches=profile.rated_matches,
        )


def results_post(
    record: ResultRecord, members: Sequence[tuple[int, ResultEntry]]
) -> OutgoingMessage:
    """The post of the contest's results in a server, for ``members``,
    ``(user_id, entry)``, which mentions no one.

    Its lines name the platform, then each member with their rating change,
    the biggest gain first; members in their first rated contest come last.
    At most ``_MAX_LINES`` members are listed, fewer if their lines would be
    too long, and a last line says how many more there are.
    """
    ordered = sorted(members, key=lambda member: _order(record.platform, member))
    member_lines = [
        _member_line(record.platform, user_id, entry) for user_id, entry in ordered
    ]
    lines = [platform_line(record.platform)]
    length = len(lines[0])
    for line in member_lines:
        if len(lines) > _MAX_LINES or length + 1 + len(line) > _DESCRIPTION_BUDGET:
            break
        lines.append(line)
        length += 1 + len(line)
    hidden = len(member_lines) - (len(lines) - 1)
    if hidden:
        lines.append(f'…and {hidden} more')
    return OutgoingMessage(
        title=f'Results: {record.name}',
        description='\n'.join(lines),
        url=record.url,
        footer=FOOTER,
        mention_role=False,
    )


def _member_line(platform: str, user_id: int, entry: ResultEntry) -> str:
    """'**12.** <@id> [handle](profile): 1500 → 1623 (**+123**), ...'."""
    url = CODEFORCES_PROFILE_URL if platform == CODEFORCES else ATCODER_PROFILE_URL
    handle = _MARKDOWN.sub(r'\\\1', entry.handle)
    line = f'<@{user_id}> [{handle}]({url.format(handle=entry.handle)}): '
    if entry.place is not None:
        line = f'**{entry.place}.** {line}'
    new, old = entry.new_rating, entry.old_rating
    if new is None:  # a rated profile always shows a rating, but just in case
        return f'{line}rated'
    if old is None or _first_rated(platform, entry):
        return f'{line}first rated contest: {new}'
    line += f'{old} → {new} (**{new - old:+d}**)'
    if platform == CODEFORCES and rank_name(old) != rank_name(new):
        line += f', {rank_name(old)} → {rank_name(new)}'
    if entry.old_highest is not None and new > entry.old_highest:
        line += ', new best'
    return line


def _order(
    platform: str, member: tuple[int, ResultEntry]
) -> tuple[bool, int, int, str, int]:
    """Where a member comes in a post: by gain, then by new rating."""
    user_id, entry = member
    new, old = entry.new_rating or 0, entry.old_rating
    first = old is None or _first_rated(platform, entry)
    gain = 0 if old is None or first else new - old
    return first, -gain, -new, entry.handle.lower(), user_id


def _first_rated(platform: str, entry: ResultEntry) -> bool:
    """Whether the contest was the handle's first rated one.

    Codeforces gives the rating before it as 0, AtCoder's baseline none.
    """
    if platform == CODEFORCES:
        return entry.old_rating == 0
    return entry.old_rating is None


def _went_up(entry: ResultEntry, reading: ProfileReading) -> bool:
    """Whether the handle has rated matches that its baseline hadn't."""
    return reading.matches > (entry.old_matches or 0)


def _owner_of(claimants: Sequence[Claimant]) -> Claimant | None:
    """Which of the contests that could own a rise in a handle's rated
    matches, ``claimants`` (by end, then by ID), does: the first, unless a
    later one's baseline of the handle has the same rated matches although it
    was read over an hour after the first ended. AtCoder had rated the first
    by then, so the handle didn't take part in it.
    """
    owner: Claimant | None = None
    for claimant in claimants:
        if owner is None or (
            claimant.old_matches == owner.old_matches
            and claimant.noted_at >= owner.end + _RATED_WITHIN
        ):
            owner = claimant
    return owner


def _next_check(end: datetime, now: datetime) -> datetime | None:
    """When to read a contest's profiles after ``now``: 15 minutes after its
    end, then every 30 minutes; None once that would be over 6 hours after it.
    """
    first = end + _FIRST_CHECK
    if now < first:
        return first
    upcoming = first + ((now - first) // _CHECK_INTERVAL + 1) * _CHECK_INTERVAL
    return upcoming if upcoming <= end + _WATCH_FOR else None


def _codeforces_contest(contest: cf.Contest) -> ResultContest | None:
    """A Codeforces contest from TLE's cache; None if it has no end time."""
    end = contest.end_time
    if end is None:
        return None
    return ResultContest(
        CODEFORCES, str(contest.id), contest.name, contest.url, from_epoch(end)
    )


def _atcoder_contest(contest: StoredContest, end: datetime) -> ResultContest:
    return ResultContest(ATCODER, contest.external_id, contest.name, contest.url, end)


def _end_of(contest: StoredContest) -> datetime:
    """The end of a contest running now, which ``live`` gives only with one."""
    if contest.end is None:
        raise ValueError(f'Contest {contest.key} has no end')
    return contest.end
