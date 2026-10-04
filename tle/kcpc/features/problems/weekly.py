"""The weekly problem: each Friday at noon, last week's solution, then a new problem.

Every server with the weekly feature on gets a problem each Friday at noon,
club time (a slot), and a week later a post with its solution. The job runs
``run_slot`` at each slot, for every server the bot is in, and an admin's
/kcpc weekly post-now runs ``run_guild`` for the latest slot. Runs for one
server take turns, with each other and with its admins' changes to its queue
and solution links.

A run posts the solutions that are due first, so that they come before the new
problem: those of the server's earlier problems whose own post went out,
oldest first, going back four weeks. Then the week's problem, which the first
try of the week picks and stores before posting it, so that a retry posts the
same problem. The oldest problem in the server's queue comes first; else the
rotation's entry for the week says what to pick from (``rotation``).

Each post's delivery key holds the server and the problem's week, so running a
slot again posts nothing twice. A post that can't be delivered yet
(UNDELIVERABLE) is left for a retry: ``run_slot`` raises, so that the job tries
again within its grace.

A pick from the rotation comes from the entry's platform, else from the other
platform, in the same band on any topic: a problem that the server hasn't had
and hasn't queued, at random. On Codeforces, from round 1000 on, whose rounds
nearly all have an English editorial; its solution post links what an admin
set, else the contest's page, which lists the editorial. On AtCoder, only a
problem with an official editorial, which its solution post links.
"""

import asyncio
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, Publisher
from tle.kcpc.core.schedule import Weekly
from tle.kcpc.core.settings import GuildSettingsRepo
from tle.kcpc.core.timeutil import discord_timestamp
from tle.kcpc.features.problems import markdown
from tle.kcpc.features.problems.catalog import (
    ATCODER,
    CODEFORCES,
    Problem,
    ProblemCatalog,
    ProblemRef,
    describe_difficulty,
    platform_name,
    platform_possessive,
    problem_title,
)
from tle.kcpc.features.problems.editorials import (
    CONTEST_MATERIALS,
    EditorialFinder,
    solution_links,
)
from tle.kcpc.features.problems.repo import (
    AUTO,
    QUEUED,
    QueuedProblem,
    WeeklyProblem,
    WeeklyRepo,
)
from tle.kcpc.features.problems.rotation import (
    RotationEntry,
    decode_rotation,
    entry_for,
)
from tle.kcpc.features.problems.settings import WEEKLY, WeeklySettings, weekly_settings
from tle.kcpc.features.problems.topics import ANY, KNOWN_TAGS, Topic, resolve_topic

logger = logging.getLogger(__name__)

WEEKLY_JOB = 'weekly.post'
# Fridays (Monday is 0) at noon, club time.
FRIDAY = 4
POST_TIME = time(12, 0)
# Codeforces' rounds from mid-2018 on nearly all have an English editorial.
WEEKLY_MIN_CODEFORCES_CONTEST = 1000
FOOTER = 'KCPC weekly problem'
# The ledger's subject for a weekly problem, whose id is the problem's week,
# and the kinds of its two posts.
SUBJECT = 'weekly'
PROBLEM = 'problem'
SOLUTION = 'solution'
MAX_QUEUED = 25  # the most problems a server may queue at once
# Solutions of older problems that never went out are left alone: a week
# that has been and gone has no use for them.
SOLUTION_LOOKBACK = timedelta(weeks=4)

# The most AtCoder problems whose editorials one pick looks up: a request each.
_EDITORIAL_TRIES = 5
# The outcomes after which a solution counts as posted. A pending one is the
# reconciler's to settle, and one Discord refused is never retried.
_SOLUTION_DONE = frozenset(
    {
        PublishOutcome.SENT,
        PublishOutcome.PENDING,
        PublishOutcome.SKIPPED,
        PublishOutcome.ALREADY_HANDLED,
    }
)
# The ledger statuses of a post that went out, or may have.
_POSTED = frozenset({DeliveryStatus.SENT, DeliveryStatus.CLAIMED})
_NOTHING_PICKED = "Couldn't pick this week's problem: {reasons}."
_NOT_LOADED = "{platform} problem list isn't loaded yet"
_NO_CANDIDATE = (
    "no {band} {platform} problem{topic} is left that this server hasn't had"
)
_NO_EDITORIAL = (
    'none of the {count} {band} AtCoder problems tried has an official editorial'
)
_ATCODER_DOWN = "AtCoder's editorials couldn't be checked"
_QUEUE_FULL = (
    f'The queue is full: it holds at most {MAX_QUEUED} problems. Take one out '
    'with `/kcpc weekly unqueue` first.'
)

# Whether the bot is in the server with this ID.
InGuild = Callable[[int], bool]


def _in_every_guild(guild_id: int) -> bool:
    """Counts the bot in every server, so that none is skipped."""
    return True


@dataclass(frozen=True)
class PostResult:
    """What became of one post of a run."""

    row: WeeklyProblem  # the problem it was about
    outcome: PublishOutcome
    reason: str | None  # the publisher's, e.g. 'guild-unavailable'


class _Unqueued(Exception):
    """A queued problem left the queue while it was being picked."""


class NoWeeklyProblem(KcpcUserError):
    """No problem could be picked for the week; the message says why.

    ``solutions`` are the solution posts that the run made before it picked,
    which went out all the same.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.solutions: tuple[PostResult, ...] = ()


@dataclass(frozen=True)
class WeeklyPlan:
    """What the next slot will post, as /kcpc weekly preview shows it."""

    slot: datetime  # the next slot
    queued: QueuedProblem | None  # the problem it will post, if one is queued
    entry: RotationEntry  # otherwise the rotation's entry it picks from
    queue: tuple[QueuedProblem, ...]
    rotation: tuple[RotationEntry, ...]


@dataclass(frozen=True)
class WeeklyReport:
    """What one server's run of a slot did."""

    configured: bool  # the feature is on and has a channel
    solutions: tuple[PostResult, ...]
    problem: PostResult | None  # None if the run stopped before the problem

    @property
    def retry_later(self) -> bool:
        """Whether a post couldn't be delivered yet, so the slot should run again."""
        results = [*self.solutions, *([self.problem] if self.problem else [])]
        return any(result.outcome is PublishOutcome.UNDELIVERABLE for result in results)


def problem_key(guild_id: int, week: str) -> str:
    """The delivery key of the post of the guild's problem for ``week``."""
    return f'weekly:{guild_id}:{PROBLEM}:{week}'


def solution_key(guild_id: int, week: str) -> str:
    """The delivery key of the post of the solution of the guild's problem for
    ``week`` (the problem's week, not the week it is posted in).
    """
    return f'weekly:{guild_id}:{SOLUTION}:{week}'


class WeeklyService:
    """Picks and posts each server's weekly problems; see the module docstring."""

    def __init__(
        self,
        repo: WeeklyRepo,
        catalog: ProblemCatalog,
        editorials: EditorialFinder,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        publisher: Publisher,
        clock: Clock,
        schedule: Weekly,
        *,
        rng: random.Random | None = None,
        in_guild: InGuild = _in_every_guild,
    ) -> None:
        self._repo = repo
        self._catalog = catalog
        self._editorials = editorials
        self._guild_settings = guild_settings
        self._ledger = ledger
        self._publisher = publisher
        self._clock = clock
        self._schedule = schedule
        self._rng = random.Random() if rng is None else rng
        self._in_guild = in_guild
        # Held for a whole run, so that the job and post-now never pick or
        # post a server's problem at once, and for each change an admin makes
        # to the server's queue or solution links, so that none lands mid-run.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run_slot(self, slot: datetime) -> None:
        """Run ``slot`` for every server with the feature on: the job's handler.

        A server the bot is no longer in is skipped: nothing can be posted
        there, and no admin there can turn the feature off. Each server's run
        is apart from the others', and one that fails is logged at INFO. Then,
        if any failed or has a post to deliver later, raises ``RuntimeError``
        (from the first failure), so that the job retries the slot; a retry
        posts nothing that went out already.
        """
        failures: list[tuple[int, Exception]] = []
        undelivered: list[int] = []
        for guild_id, _ in await self._guild_settings.enabled_guilds(WEEKLY):
            if not self._in_guild(guild_id):
                logger.info(
                    'Skipping the weekly problem of guild %d for its %s slot: '
                    'the bot is not in it',
                    guild_id,
                    slot,
                )
                continue
            try:
                report = await self.run_guild(guild_id, slot)
            except KcpcUserError as exc:
                logger.info(
                    'Could not run the weekly problem of guild %d for its %s slot: %s',
                    guild_id,
                    slot,
                    exc,
                )
                failures.append((guild_id, exc))
                continue
            except Exception as exc:
                logger.info(
                    'Could not run the weekly problem of guild %d for its %s slot',
                    guild_id,
                    slot,
                    exc_info=True,
                )
                failures.append((guild_id, exc))
                continue
            if report.retry_later:
                logger.info(
                    'A weekly post of guild %d for its %s slot could not be '
                    'delivered yet; the slot will be tried again',
                    guild_id,
                    slot,
                )
                undelivered.append(guild_id)
        if failures or undelivered:
            guilds = sorted({guild_id for guild_id, _ in failures} | set(undelivered))
            error = RuntimeError(
                'The weekly problem was not posted in guilds '
                f'{", ".join(map(str, guilds))}'
            )
            raise error from (failures[0][1] if failures else None)

    async def run_guild(self, guild_id: int, slot: datetime) -> WeeklyReport:
        """Post the guild's solutions that are due, then its problem for ``slot``.

        Writes nothing while the feature is off or has no channel. An instant
        that isn't a slot runs the slot before it. Raises ``NoWeeklyProblem``
        if the slot's problem has to be picked and none can be, with the
        solutions posted before then.
        """
        slot = self._schedule.prev_at_or_before(slot)
        week = self._week(slot)
        async with self._locks[guild_id]:
            settings = weekly_settings(await self._guild_settings.get(guild_id, WEEKLY))
            if not settings.enabled or settings.channel_id is None:
                return WeeklyReport(configured=False, solutions=(), problem=None)
            solutions: list[PostResult] = []
            due = await self._repo.unposted_solutions(
                guild_id, before=slot, since=slot - SOLUTION_LOOKBACK
            )
            for row in due:
                if row.week == week:
                    # The week's own problem, stored before its slot moved
                    # (the club's time zone changed): its solution comes later.
                    continue
                if not await self.problem_posted(row):
                    continue  # members never saw the problem, so no solution
                result = await self._post_solution(row, slot)
                solutions.append(result)
                if result.outcome is PublishOutcome.NOT_CONFIGURED:
                    return WeeklyReport(False, tuple(solutions), None)
                if result.outcome is PublishOutcome.UNDELIVERABLE:
                    # The problem waits too: on the retry it goes out after
                    # the solution, as it should.
                    return WeeklyReport(True, tuple(solutions), None)
            # A retry of the week posts the problem that its first try picked,
            # even if the slot has moved since.
            problem = await self._repo.get_week(guild_id, week)
            if problem is None:
                try:
                    problem = await self._pick(guild_id, slot, settings)
                except NoWeeklyProblem as exc:
                    exc.solutions = tuple(solutions)
                    raise
            result = await self._post_problem(problem)
            configured = result.outcome is not PublishOutcome.NOT_CONFIGURED
            return WeeklyReport(configured, tuple(solutions), result)

    async def enqueue(self, item: QueuedProblem) -> QueuedProblem:
        """Add the problem to the end of the guild's queue, and return it as stored.

        Raises ``KcpcUserError`` if the queue holds ``MAX_QUEUED`` problems
        already, and as ``WeeklyRepo.enqueue`` does if the guild has had the
        problem or has queued it. A problem whose post never went out, in a
        week that is over, isn't one the guild has had: its row is deleted
        with the same transaction that queues it again.
        """
        async with self._locks[item.guild_id]:
            if len(await self._repo.queue(item.guild_id)) >= MAX_QUEUED:
                raise KcpcUserError(_QUEUE_FULL)
            unposted = await self._unposted_row(
                item.guild_id, item.source, item.problem_id
            )
            async with self._repo.transaction():
                if unposted is not None:
                    await self._repo.delete(unposted.guild_id, unposted.week)
                return await self._repo.enqueue(item)

    async def unqueue(
        self, guild_id: int, source: str, problem_id: str
    ) -> QueuedProblem | None:
        """Take the problem out of the guild's queue and return it; None if it
        isn't in it, as when a run has just picked it.
        """
        async with self._locks[guild_id]:
            return await self._repo.dequeue(guild_id, source, problem_id)

    async def set_solution(
        self, guild_id: int, slot: datetime, url: str, *, set_by: int
    ) -> WeeklyProblem | None:
        """Set the link an admin gave for the solution of the guild's problem for
        ``slot``; return the row as stored afterwards, as
        ``WeeklyRepo.set_solution`` does. A solution that a run is posting
        meanwhile is posted first, and keeps its link.
        """
        async with self._locks[guild_id]:
            return await self._repo.set_solution(guild_id, slot, url, set_by=set_by)

    def in_lookback(self, row: WeeklyProblem) -> bool:
        """Whether the next slot's run looks back as far as ``row``: if its
        solution isn't posted by then, that run posts it, as long as its
        problem's post went out.
        """
        next_slot = self._schedule.next_after(self._clock.now())
        return row.slot >= next_slot - SOLUTION_LOOKBACK

    async def plan_next(self, guild_id: int) -> WeeklyPlan:
        """What the guild's next slot will post: its queued problem, or the
        rotation's entry it will pick from. Nothing is written.
        """
        slot = self._schedule.next_after(self._clock.now())
        settings = weekly_settings(await self._guild_settings.get(guild_id, WEEKLY))
        rotation = decode_rotation(settings.rotation)
        queue = tuple(await self._repo.queue(guild_id))
        used = await self._repo.used_problems(guild_id)
        queued = next(
            (item for item in queue if (item.source, item.problem_id) not in used),
            None,
        )
        return WeeklyPlan(
            slot=slot,
            queued=queued,
            entry=entry_for(rotation, self._club_date(slot)),
            queue=queue,
            rotation=rotation,
        )

    async def current(self, guild_id: int) -> WeeklyProblem | None:
        """The guild's latest problem, at or before now, whose post went out."""
        posted = await self.history(guild_id)
        return posted[0] if posted else None

    async def history(self, guild_id: int) -> list[WeeklyProblem]:
        """The guild's problems, at or before now, whose posts went out, newest
        first.
        """
        rows = await self._repo.history(guild_id, at_or_before=self._clock.now())
        records = await self._ledger.history_for(
            guild_id, SUBJECT, [row.week for row in rows]
        )
        return [
            row
            for row in rows
            if any(
                record.key == problem_key(guild_id, row.week)
                and record.status in _POSTED
                for record in records[row.week]
            )
        ]

    async def problem_posted(self, row: WeeklyProblem) -> bool:
        """Whether the problem's post went out, or was claimed and may have."""
        record = await self._ledger.get(problem_key(row.guild_id, row.week))
        return record is not None and record.status in _POSTED

    async def _unposted_row(
        self, guild_id: int, source: str, problem_id: str
    ) -> WeeklyProblem | None:
        """The guild's row of the problem if members never saw it: neither its
        post nor its solution's went out, and its week is over, so no retry or
        post-now can post it any more. None if there is no such row.
        """
        week = (await self._repo.used_problems(guild_id)).get((source, problem_id))
        row = None if week is None else await self._repo.get_week(guild_id, week)
        if row is None or row.solution_posted or await self.problem_posted(row):
            return None
        latest = self._schedule.prev_at_or_before(self._clock.now())
        if date.fromisoformat(row.week) >= self._club_date(latest):
            return None  # the week's own problem, which may still go out
        return row

    def difficulty(self, row: WeeklyProblem) -> str:
        """How hard ``row``'s problem is, as its post says it.

        An AtCoder problem's own difficulty isn't stored, so it is read from
        AtCoder's list while that is loaded, as is its rating then.
        """
        if row.source == ATCODER and self._catalog.loaded(ATCODER):
            ref = ProblemRef(ATCODER, row.problem_id, row.contest_id, None)
            problem = self._catalog.find(ref)
            if problem is not None:
                return describe_difficulty(ATCODER, problem.rating, problem.difficulty)
        return describe_difficulty(row.source, row.difficulty, None)

    async def _post_solution(self, row: WeeklyProblem, slot: datetime) -> PostResult:
        """Post the solution of ``row``'s problem at ``slot``, and record it."""
        if row.source == ATCODER and row.solution_set_by is None:
            row = await self._refresh_editorial(row)
        # As stored now, should a link have been set since the run read it.
        row = await self._repo.get(row.guild_id, row.slot) or row
        delivery = Delivery(
            key=solution_key(row.guild_id, row.week),
            guild_id=row.guild_id,
            feature=WEEKLY,
            subject=SUBJECT,
            subject_id=row.week,
            kind=SOLUTION,
            occurrence_start=row.slot,
            expires_at=self._schedule.next_after(slot),
        )
        result = await self._publisher.publish([delivery], _solution_post(row))
        if result.outcome in _SOLUTION_DONE:
            await self._repo.mark_solution_posted(
                row.guild_id, row.slot, self._clock.now()
            )
        return PostResult(row, result.outcome, result.reason)

    async def _refresh_editorial(self, row: WeeklyProblem) -> WeeklyProblem:
        """``row`` with its AtCoder task's best official editorial now, if it
        has one; an editorial added since the pick may be a better one.

        Best effort: a failure keeps the link there was.
        """
        best = await self._best_editorial(row.guild_id, row.contest_id, row.problem_id)
        if best is None or best == row.solution_url:
            return row
        stored = await self._repo.set_solution(
            row.guild_id, row.slot, best, set_by=None
        )
        return row if stored is None else stored

    async def _post_problem(self, row: WeeklyProblem) -> PostResult:
        delivery = Delivery(
            key=problem_key(row.guild_id, row.week),
            guild_id=row.guild_id,
            feature=WEEKLY,
            subject=SUBJECT,
            subject_id=row.week,
            kind=PROBLEM,
            occurrence_start=row.slot,
            expires_at=self._schedule.next_after(row.slot),
        )
        message = self._problem_post(row)
        result = await self._publisher.publish([delivery], message)
        return PostResult(row, result.outcome, result.reason)

    def _problem_post(self, row: WeeklyProblem) -> OutgoingMessage:
        """The post of ``row``'s problem, which mentions the weekly role."""
        solution_at = self._schedule.next_after(row.slot)
        lines = [
            f'**Platform:** {platform_name(row.source)}',
            f'**Difficulty:** {self.difficulty(row)}',
        ]
        if row.topic is not None:
            lines.append(f'**Topic:** {row.topic}')
        lines.append(
            f'**Solution:** {discord_timestamp(solution_at, "F")} '
            f'({discord_timestamp(solution_at, "R")})'
        )
        return OutgoingMessage(
            title=f'Weekly problem: {_title(row)}',
            description='\n'.join(lines),
            url=row.url,
            footer=FOOTER,
            mention_role=True,
        )

    async def _pick(
        self, guild_id: int, slot: datetime, settings: WeeklySettings
    ) -> WeeklyProblem:
        """Pick the guild's problem for ``slot`` and store it: the oldest
        queued problem, else one from the rotation.
        """
        used = await self._repo.used_problems(guild_id)
        queue = await self._repo.queue(guild_id)
        for item in queue:
            week = used.get((item.source, item.problem_id))
            if week is None:
                taken = await self._take_queued(item, slot)
                if taken is not None:
                    return taken
                continue  # taken out of the queue meanwhile
            # Queued while it was being picked: it can't be the weekly
            # problem again.
            await self._repo.dequeue(guild_id, item.source, item.problem_id)
            logger.info(
                "Took %s %s out of guild %d's weekly queue: it was the weekly "
                'problem of %s',
                item.source,
                item.problem_id,
                guild_id,
                week,
            )
        queued = {(item.source, item.problem_id) for item in queue}
        return await self._take_from_rotation(
            guild_id, slot, settings, used.keys() | queued
        )

    async def _take_queued(
        self, item: QueuedProblem, slot: datetime
    ) -> WeeklyProblem | None:
        """Store the queued problem as the slot's, and take it out of the queue.

        None, storing nothing, if it was taken out of the queue while its
        editorial was looked up.
        """
        solution_url = item.solution_url
        # A queued problem's link is the admin's who queued it.
        set_by = None if solution_url is None else item.queued_by
        if item.source == ATCODER and solution_url is None:
            solution_url = await self._best_editorial(
                item.guild_id, item.contest_id, item.problem_id
            )
        row = WeeklyProblem(
            guild_id=item.guild_id,
            slot=slot,
            week=self._week(slot),
            source=item.source,
            problem_id=item.problem_id,
            contest_id=item.contest_id,
            index=item.index,
            name=item.name,
            url=item.url,
            topic=None,
            difficulty=item.difficulty,
            band=item.band,
            selection=QUEUED,
            date_selected=self._clock.now(),
            solution_url=solution_url,
            solution_set_by=set_by,
            solution_posted=False,
            solution_posted_at=None,
        )
        try:
            async with self._repo.transaction():
                stored = await self._repo.create(row)
                # Another run may have stored the week's problem first.
                if (stored.source, stored.problem_id) == (item.source, item.problem_id):
                    taken = await self._repo.dequeue(
                        item.guild_id, item.source, item.problem_id
                    )
                    if taken is None:
                        raise _Unqueued  # rolls the row back
        except _Unqueued:
            logger.info(
                "%s %s left guild %d's weekly queue while it was being picked",
                item.source,
                item.problem_id,
                item.guild_id,
            )
            return None
        _log_pick(stored)
        return stored

    async def _take_from_rotation(
        self,
        guild_id: int,
        slot: datetime,
        settings: WeeklySettings,
        excluded: Collection[tuple[str, str]],
    ) -> WeeklyProblem:
        """Pick a problem by the rotation's entry for the slot's week, and store
        it. Raises ``NoWeeklyProblem`` if none can be picked.
        """
        entry = entry_for(decode_rotation(settings.rotation), self._club_date(slot))
        other = ATCODER if entry.platform == CODEFORCES else CODEFORCES
        if not (self._catalog.loaded(entry.platform) and self._catalog.loaded(other)):
            # Just after a restart, the refresh job may still be fetching the
            # lists: this waits for it, sharing its lock, and fetches only
            # what is still missing. A list that fails is the job's to report.
            await self._catalog.refresh()
        reasons: list[str] = []
        for platform, topic in ((entry.platform, entry.topic), (other, ANY)):
            if not self._catalog.loaded(platform):
                owner = platform_possessive(platform)
                reasons.append(_NOT_LOADED.format(platform=owner))
                continue
            candidates = self._candidates(platform, entry, topic, excluded)
            if platform == CODEFORCES:
                if candidates:
                    return await self._store_pick(guild_id, slot, candidates[0], topic)
                reasons.append(_no_candidate(platform, entry, topic))
                continue
            try:
                found = await self._with_editorial(guild_id, candidates)
            except ExternalServiceError:
                reasons.append(_ATCODER_DOWN)
                continue
            if found is not None:
                problem, solution_url = found
                return await self._store_pick(
                    guild_id, slot, problem, topic, solution_url=solution_url
                )
            if candidates:
                tried = min(len(candidates), _EDITORIAL_TRIES)
                reasons.append(_NO_EDITORIAL.format(count=tried, band=entry.band.value))
            else:
                reasons.append(_no_candidate(platform, entry, topic))
        raise NoWeeklyProblem(_NOTHING_PICKED.format(reasons='; '.join(reasons)))

    def _candidates(
        self,
        platform: str,
        entry: RotationEntry,
        topic_key: str,
        excluded: Collection[tuple[str, str]],
    ) -> list[Problem]:
        """The platform's problems that the entry may pick, shuffled."""
        topic = self._topic(topic_key)
        if topic is None:
            return []
        candidates = [
            problem
            for problem in self._catalog.pool(platform)
            if problem.band is entry.band
            and topic.matches(problem.tags)
            and (platform, problem.problem_id) not in excluded
            and (
                platform != CODEFORCES
                or int(problem.contest_id) >= WEEKLY_MIN_CODEFORCES_CONTEST
            )
        ]
        self._rng.shuffle(candidates)
        return candidates

    def _topic(self, key: str) -> Topic | None:
        """The topic a rotation's entry names; None if it names none now."""
        try:
            return resolve_topic(key, KNOWN_TAGS | self._catalog.tags())
        except KcpcUserError:
            logger.info("The weekly rotation's topic %r is no longer known", key)
            return None

    async def _with_editorial(
        self, guild_id: int, candidates: Sequence[Problem]
    ) -> tuple[Problem, str] | None:
        """The first of the first ``_EDITORIAL_TRIES`` AtCoder candidates with an
        official editorial, and that editorial's link; None if none has one.

        Raises ``ExternalServiceError`` if AtCoder fails: no more of its
        problems are tried in that run.
        """
        for problem in candidates[:_EDITORIAL_TRIES]:
            try:
                editorials = await self._editorials.atcoder(
                    problem.contest_id, problem.problem_id
                )
            except ExternalServiceError as exc:
                logger.info(
                    'Could not look up the editorials of AtCoder %s for guild %d, '
                    'so no AtCoder problem is picked: %s',
                    problem.problem_id,
                    guild_id,
                    exc,
                )
                raise
            except KcpcUserError:  # an ID that AtCoder can't have
                continue
            best = None if editorials is None else editorials.best()
            if best is not None:
                return problem, best.url
            logger.debug('AtCoder %s has no official editorial', problem.problem_id)
        return None

    async def _best_editorial(
        self, guild_id: int, contest_id: str, problem_id: str
    ) -> str | None:
        """The link of the AtCoder task's best official editorial, if it has one
        and AtCoder answers.
        """
        try:
            editorials = await self._editorials.atcoder(contest_id, problem_id)
        except KcpcUserError as exc:  # ExternalServiceError is one
            logger.info(
                'Could not look up the editorials of AtCoder %s for guild %d: %s',
                problem_id,
                guild_id,
                exc,
            )
            return None
        best = None if editorials is None else editorials.best()
        return None if best is None else best.url

    async def _store_pick(
        self,
        guild_id: int,
        slot: datetime,
        problem: Problem,
        topic: str,
        *,
        solution_url: str | None = None,
    ) -> WeeklyProblem:
        band = problem.band
        stored = await self._repo.create(
            WeeklyProblem(
                guild_id=guild_id,
                slot=slot,
                week=self._week(slot),
                source=problem.platform,
                problem_id=problem.problem_id,
                contest_id=problem.contest_id,
                index=problem.index,
                name=problem.name,
                url=problem.url,
                topic=None if topic == ANY else topic,
                difficulty=problem.rating,
                band=None if band is None else band.value,
                selection=AUTO,
                date_selected=self._clock.now(),
                solution_url=solution_url,
                solution_set_by=None,
                solution_posted=False,
                solution_posted_at=None,
            )
        )
        _log_pick(stored)
        return stored

    def _week(self, slot: datetime) -> str:
        """The slot's week, as stored and in delivery keys: its date in club time."""
        return self._club_date(slot).isoformat()

    def _club_date(self, slot: datetime) -> date:
        return slot.astimezone(self._schedule.tz).date()


def _solution_post(row: WeeklyProblem) -> OutgoingMessage:
    """The post of ``row``'s solution, which mentions no one."""
    links = solution_links(row)
    title = _title(row)
    lines = [f"Last week's problem: {markdown.link(title, row.url)}"]
    for link in links:
        if link.label == CONTEST_MATERIALS:
            page = markdown.link('the contest page', link.url)
            lines.append(
                f'Codeforces lists the editorial under **Contest materials** on {page}.'
            )
        else:
            lines.append(markdown.link(link.label, link.url))
    return OutgoingMessage(
        title=f'Solution: {title}',
        description='\n'.join(lines),
        url=links[0].url,
        footer=FOOTER,
        mention_role=False,
    )


def _title(row: WeeklyProblem) -> str:
    return problem_title(row.source, row.contest_id, row.index, row.name)


def _no_candidate(platform: str, entry: RotationEntry, topic: str) -> str:
    about = '' if topic == ANY else f' about {topic}'
    return _NO_CANDIDATE.format(
        band=entry.band.value, platform=platform_name(platform), topic=about
    )


def _log_pick(row: WeeklyProblem) -> None:
    logger.info(
        "Picked %s %s (%s) as guild %d's weekly problem of %s",
        row.source,
        row.problem_id,
        row.selection,
        row.guild_id,
        row.week,
    )
