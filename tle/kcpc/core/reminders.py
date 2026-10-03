"""Reminders: posts about upcoming occurrences, each made at most once.

An occurrence is something that starts at a known time: a workshop now,
contests later. A feature lists its occurrences in each guild through a
``ReminderSource``, and the ``ReminderEngine`` posts about them: a reminder at
each of the policy's offsets before the start (and optionally one at the
start), and a notice when an occurrence that members have heard about moves,
is cancelled or comes back.

``plan`` decides what to post from the time, the occurrences and the ledger's
history of each one, and nothing else. Every post has one delivery key per
occurrence, which the ledger lets out once, and ``plan`` also counts a reminder
as done if the ledger has one of its kind for nearly the same start, even under
an earlier revision. So a tick can run any number of times, or stop anywhere,
without anything going out twice.
"""

import asyncio
import logging
from collections import Counter, defaultdict
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from operator import attrgetter
from typing import Protocol, cast

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.ledger import (
    Delivery,
    DeliveryLedger,
    DeliveryRecord,
    DeliveryStatus,
)
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, Publisher
from tle.kcpc.core.settings import FeatureSettings, GuildSettingsRepo

logger = logging.getLogger(__name__)

# The offset of the start announcement.
_AT_START = timedelta(0)
_ONE_MINUTE = timedelta(minutes=1)

# Why a due reminder is skipped: a shorter one is due already, or due soon.
_SUPERSEDED = 'superseded'
_LATE = 'late'

# A failure that keeps happening is logged at WARNING, which reaches the Discord
# log channel, at most this often; ticks run every minute.
_FAILURE_WARNING_INTERVAL = timedelta(hours=1)

# The most occurrences that one reminder post is about. Its source shows each
# one, say as an embed field, and Discord takes at most 25 fields and 6000
# characters in all: a post about more could leave some out.
MAX_GROUP = 10


class NoticeKind(str, Enum):
    """What a post about occurrences says."""

    REMINDER = 'reminder'  # it starts soon, or (offset 0) now
    MOVED = 'moved'  # the start changed after members heard about it
    CANCELLED = 'cancelled'  # cancelled after members heard about it
    REINSTATED = 'reinstated'  # back after members heard it was cancelled


@dataclass(frozen=True)
class Occurrence:
    """Something that starts at a known time, such as one workshop.

    Sources may subclass it, as a frozen dataclass whose new fields have
    defaults, to carry more for ``render``: notices hold the occurrences given
    to ``plan``. Sources bump ``revision`` when the start changes, on
    cancelling and on reinstating. Delivery keys include it, so each revision
    can get notices of its own.

    So a source must never reuse a (subject, subject_id) revision for another
    start or cancelled state: the ledger would take the posts about the new
    one for posts already made, and they would never go out. A source that
    merges several feeds, such as Luma calendars that list one event, must
    keep the revisions increasing across all of them.
    """

    subject: str  # what it is, e.g. 'event'
    subject_id: str  # stable across changes, e.g. a Luma event id
    title: str
    start: datetime  # aware, whole seconds
    end: datetime | None
    url: str | None
    revision: int
    cancelled: bool = False

    def __post_init__(self) -> None:
        # Fail where a bad occurrence is made, not in plan: a naive time fails
        # only when compared with an aware one, and the ledger keeps starts in
        # whole seconds, so a fraction of one would make a start look moved.
        for name, moment in (('start', self.start), ('end', self.end)):
            if moment is not None and moment.utcoffset() is None:
                raise ValueError(
                    f'Occurrence.{name} must be timezone-aware, got {moment!r}'
                )
        if self.start.microsecond:
            raise ValueError(
                f'Occurrence.start must be a whole second, got {self.start!r}'
            )


@dataclass(frozen=True)
class ReminderPolicy:
    """When a guild hears about a feature's occurrences (see ``plan``).

    - offsets: how long before the start each reminder goes out, in any order.
      They are distinct, positive whole minutes; there may be none.
    - announce_start: also post at the start, until ``start_window`` after it.
    - late_window: a reminder is skipped as late if a shorter one is due
      within this.
    - min_tolerance: how far the start may move without re-arming even a
      short reminder.
    - horizon: how far ahead occurrences are loaded.

    The windows are positive, except late_window, which may be zero.
    """

    offsets: tuple[timedelta, ...]
    announce_start: bool = False
    start_window: timedelta = timedelta(minutes=10)
    late_window: timedelta = timedelta(minutes=30)
    min_tolerance: timedelta = timedelta(minutes=10)
    horizon: timedelta = timedelta(days=400)

    def __post_init__(self) -> None:
        for offset in self.offsets:
            if offset <= _AT_START or not _is_whole_minutes(offset):
                raise ValueError(
                    f'Reminder offsets must be positive whole minutes, got {offset!r}'
                )
        if len(set(self.offsets)) != len(self.offsets):
            raise ValueError(f'Reminder offsets must be distinct, got {self.offsets!r}')
        if self.late_window < timedelta(0):
            raise ValueError(
                f'late_window must not be negative, got {self.late_window!r}'
            )
        # A zero min_tolerance would never count a start announcement as made,
        # not even for the same start.
        positive = (
            ('start_window', self.start_window),
            ('min_tolerance', self.min_tolerance),
            ('horizon', self.horizon),
        )
        for name, window in positive:
            if window <= timedelta(0):
                raise ValueError(f'{name} must be positive, got {window!r}')


@dataclass(frozen=True)
class Notice:
    """What one post says, for its source to render.

    ``occurrences`` holds more than one only for a reminder of several
    occurrences with the same start: at most ``MAX_GROUP``, sorted by title and
    then id. ``offset`` is
    a REMINDER's offset (0 for the start announcement). ``previous_start`` is a
    MOVED notice's start that members were last told. Both are None for other
    kinds.
    """

    kind: NoticeKind
    occurrences: tuple[Occurrence, ...]
    offset: timedelta | None = None
    previous_start: datetime | None = None


class ReminderSource(Protocol):
    """A feature's occurrences in each guild, and how to post about them."""

    @property
    def feature(self) -> str:
        """The feature's key: posts go to its channel in the guilds enabling it."""
        ...

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        """When the guild with ``settings`` hears about occurrences."""
        ...

    async def occurrences(
        self,
        guild_id: int,
        settings: FeatureSettings,
        start: datetime,
        end: datetime,
    ) -> Sequence[Occurrence]:
        """The guild's occurrences starting in [start, end), cancelled ones too.

        Cancelled occurrences are needed to post their cancellation notices.
        """
        ...

    def render(self, notice: Notice) -> OutgoingMessage:
        """The post that says ``notice``.

        It must show every occurrence of the notice, within Discord's limits:
        once the post is sent, each one counts as told, so one left out would
        never be posted.
        """
        ...


class ActionKind(str, Enum):
    SEND = 'send'
    SKIP = 'skip'


@dataclass(frozen=True)
class PlannedAction:
    """One decision of ``plan``: post ``notice``, or record it as skipped.

    ``deliveries`` holds one delivery per occurrence of the notice, sorted by
    key. Only a reminder is ever skipped, alone, and the skip has a reason:
    'superseded' if a shorter reminder is due already, 'late' if one is due
    soon.
    """

    kind: ActionKind
    notice: Notice
    deliveries: tuple[Delivery, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.kind is ActionKind.SKIP) != (self.reason is not None):
            raise ValueError('A skip needs a reason, and only a skip has one')


def reminder_kind(offset: timedelta) -> str:
    """The ledger kind of the reminder ``offset`` before the start.

    'start' for the start announcement (0), else the whole minutes, as in
    '1440m' or '60m'. ``ValueError`` for a negative offset or a fraction of a
    minute.
    """
    if offset < _AT_START or not _is_whole_minutes(offset):
        raise ValueError(
            f'A reminder offset must be zero or positive whole minutes, got {offset!r}'
        )
    if offset == _AT_START:
        return 'start'
    return f'{offset // _ONE_MINUTE}m'


def plan(
    now: datetime,
    guild_id: int,
    feature: str,
    policy: ReminderPolicy,
    occurrences: Sequence[Occurrence],
    history: Mapping[str, Sequence[DeliveryRecord]],
) -> list[PlannedAction]:
    """What to post about ``occurrences`` in one guild at ``now``, and what to skip.

    ``history`` maps subject ids to their records in the guild's ledger, oldest
    first, as ``DeliveryLedger.history_for`` returns them. Records about another
    subject are ignored, so several subjects' histories can share the mapping.
    Each occurrence needs its own (subject, subject_id), else ``ValueError``.

    Notices correct what members were last told about an occurrence: the
    latest of its records that was sent, of any kind. A CANCELLED notice told
    them it was off; any other post, that it was on at the post's start. Only
    an occurrence that starts after ``now`` gets one, and only once per
    revision: never if the ledger has a notice of that kind for its revision,
    in any status. For each occurrence, starting at S:

    1. Cancelled, and members were last told it was on: CANCELLED.
    2. Not cancelled, and members were last told it was off, under an earlier
       revision: REINSTATED, which gives the start as well.
    3. Not cancelled, and members were last told another start, under an
       earlier revision (a record without one counts as older): MOVED, with
       ``previous_start`` the start they were told.
    4. Reminders, unless it is cancelled: one per offset o, and o = 0 if the
       start is announced. o > 0 is due from S - o until S, and o = 0 from S
       until S + start_window. A due reminder is handled already if the ledger
       has one of its kind, under any revision and in any status, for a start
       less than max(o, min_tolerance) away: a start postponed by 10 minutes
       doesn't re-arm its 1h reminder, while a move of a week does. Otherwise
       it gives way to the next shorter positive offset: it is skipped as
       'superseded' if that one is due, or as 'late' if it is due within
       late_window, and else sent.

    The reminders sent at one offset before one start go out together: by
    title and then id, in posts about up to ``MAX_GROUP`` occurrences each.
    Notices come first, then reminders, by start and then longest offset, with
    ties broken by delivery key: so the order of ``occurrences`` doesn't
    matter.
    """
    _require_distinct(occurrences)
    planner = _Planner(now, guild_id, feature, policy)
    actions: list[PlannedAction] = []
    # The reminders to send, by start and offset: each group goes out in posts
    # of up to MAX_GROUP.
    groups: dict[tuple[datetime, timedelta], list[Occurrence]] = {}
    for occurrence in occurrences:
        records = [
            record
            for record in history.get(occurrence.subject_id, ())
            if record.subject == occurrence.subject
        ]
        notice = _notice(now, occurrence, records)
        if notice is not None:
            actions.append(planner.notice_action(notice))
        if occurrence.cancelled:
            continue
        start = occurrence.start
        for offset in planner.offsets:
            if not planner.is_due(start, offset):
                continue
            if planner.is_handled(start, offset, records):
                continue
            reason = planner.skip_reason(start, offset)
            if reason is None:
                groups.setdefault((start, offset), []).append(occurrence)
            else:
                skip = planner.reminder_action(
                    ActionKind.SKIP, [occurrence], offset, reason=reason
                )
                actions.append(skip)
    for (_, offset), group in groups.items():
        group.sort(key=_by_title)
        for first in range(0, len(group), MAX_GROUP):
            chunk = group[first : first + MAX_GROUP]
            actions.append(planner.reminder_action(ActionKind.SEND, chunk, offset))
    return sorted(actions, key=_order)


class _Planner:
    """The reminder rules and delivery details of one ``plan`` call."""

    def __init__(
        self, now: datetime, guild_id: int, feature: str, policy: ReminderPolicy
    ) -> None:
        self._now = now
        self._guild_id = guild_id
        self._feature = feature
        self._policy = policy
        # Every offset to remind at, longest first; 0 is the start announcement.
        at_start = (_AT_START,) if policy.announce_start else ()
        self.offsets = (*sorted(policy.offsets, reverse=True), *at_start)

    def is_due(self, start: datetime, offset: timedelta) -> bool:
        if offset == _AT_START:
            return start <= self._now < start + self._policy.start_window
        return start - offset <= self._now < start

    def is_handled(
        self, start: datetime, offset: timedelta, records: Sequence[DeliveryRecord]
    ) -> bool:
        """Whether the ledger has the reminder at ``offset`` for nearly ``start``."""
        kind = reminder_kind(offset)
        tolerance = max(offset, self._policy.min_tolerance)
        return any(
            record.kind == kind
            and record.occurrence_start is not None
            and abs(start - record.occurrence_start) < tolerance
            for record in records
        )

    def skip_reason(self, start: datetime, offset: timedelta) -> str | None:
        """Why the due reminder at ``offset`` gives way to a shorter one, if it does.

        Only the next shorter positive offset matters, since it is due first.
        The start announcement neither gives way nor makes others give way.
        """
        shorter = [other for other in self.offsets if _AT_START < other < offset]
        if not shorter:
            return None
        wait = start - max(shorter) - self._now  # until it is due
        if wait <= timedelta(0):
            return _SUPERSEDED
        if wait <= self._policy.late_window:
            return _LATE
        return None

    def reminder_action(
        self,
        kind: ActionKind,
        occurrences: Sequence[Occurrence],
        offset: timedelta,
        *,
        reason: str | None = None,
    ) -> PlannedAction:
        """Post, or skip, the reminder at ``offset`` of ``occurrences``.

        The start announcement may still go out until the end of its window;
        any other reminder only until the start.
        """
        expiry = self._policy.start_window if offset == _AT_START else timedelta(0)
        deliveries = [
            self._delivery(
                'remind',
                reminder_kind(offset),
                occurrence,
                expires_at=occurrence.start + expiry,
            )
            for occurrence in occurrences
        ]
        notice = Notice(
            NoticeKind.REMINDER,
            tuple(sorted(occurrences, key=_by_title)),
            offset=offset,
        )
        return PlannedAction(kind, notice, _by_key(deliveries), reason)

    def notice_action(self, notice: Notice) -> PlannedAction:
        """Post ``notice`` about its occurrence, until that starts."""
        deliveries = [
            self._delivery(
                'notice', notice.kind.value, occurrence, expires_at=occurrence.start
            )
            for occurrence in notice.occurrences
        ]
        return PlannedAction(ActionKind.SEND, notice, _by_key(deliveries))

    def _delivery(
        self, prefix: str, kind: str, occurrence: Occurrence, *, expires_at: datetime
    ) -> Delivery:
        subject, subject_id = occurrence.subject, occurrence.subject_id
        return Delivery(
            key=(
                f'{prefix}:{self._guild_id}:{subject}:{subject_id}:{kind}'
                f':r{occurrence.revision}'
            ),
            guild_id=self._guild_id,
            feature=self._feature,
            subject=subject,
            subject_id=subject_id,
            kind=kind,
            occurrence_start=occurrence.start,
            revision=occurrence.revision,
            expires_at=expires_at,
        )


def _notice(
    now: datetime, occurrence: Occurrence, records: Sequence[DeliveryRecord]
) -> Notice | None:
    """The notice that corrects what members were last told, if one is due.

    Rules 1 to 3 of ``plan``. What members were told is the latest record that
    was sent: one still claimed may not have arrived, and a skipped one never
    did. Every post gives its start, so a notice counts as well as a reminder:
    a second move is reported from the start that the first MOVED notice gave,
    and a move back to a start that members were reminded of is reported too.
    """
    told = _last_sent(records)
    if told is None or now >= occurrence.start:
        return None
    revision = occurrence.revision
    told_cancelled = told.kind == NoticeKind.CANCELLED.value
    # A notice answers a change made after members were told: a newer revision.
    # (CANCELLED needs none, as telling members of a cancellation is never
    # wrong while they think it is on.)
    newer = _revision_of(told) < revision
    if occurrence.cancelled:
        needed = NoticeKind.CANCELLED if not told_cancelled else None
    elif told_cancelled:
        needed = NoticeKind.REINSTATED if newer else None
    else:
        moved = (
            told.occurrence_start is not None
            and told.occurrence_start != occurrence.start
        )
        needed = NoticeKind.MOVED if moved and newer else None
    if needed is None or _has_record(records, needed, revision):
        return None
    if needed is NoticeKind.MOVED:
        return Notice(needed, (occurrence,), previous_start=told.occurrence_start)
    return Notice(needed, (occurrence,))


def _last_sent(records: Sequence[DeliveryRecord]) -> DeliveryRecord | None:
    """The latest of ``records`` (oldest first) that was sent, if any."""
    return next(
        (
            record
            for record in reversed(records)
            if record.status is DeliveryStatus.SENT
        ),
        None,
    )


def _revision_of(record: DeliveryRecord) -> int:
    # A record without a revision counts as older than every revision.
    return -1 if record.revision is None else record.revision


def _has_record(
    records: Sequence[DeliveryRecord], kind: NoticeKind, revision: int
) -> bool:
    """Whether ``records`` has a ``kind`` notice for ``revision``, in any status."""
    return any(
        record.kind == kind.value and record.revision == revision for record in records
    )


def _require_distinct(occurrences: Sequence[Occurrence]) -> None:
    seen: set[tuple[str, str]] = set()
    for occurrence in occurrences:
        identity = (occurrence.subject, occurrence.subject_id)
        if identity in seen:
            raise ValueError(f'Occurrence {identity} is listed more than once')
        seen.add(identity)


def _is_whole_minutes(delta: timedelta) -> bool:
    return delta % _ONE_MINUTE == timedelta(0)


def _by_key(deliveries: Sequence[Delivery]) -> tuple[Delivery, ...]:
    return tuple(sorted(deliveries, key=attrgetter('key')))


def _by_title(occurrence: Occurrence) -> tuple[str, str, str]:
    """How a reminder orders its occurrences: by title, then id.

    The subject breaks a tie between two subjects' occurrences with one id, so
    which post each occurrence lands in never depends on the order given.
    """
    return (occurrence.title, occurrence.subject_id, occurrence.subject)


def _order(action: PlannedAction) -> tuple[bool, datetime, timedelta, str]:
    """Notices first, then reminders; each by start, longest offset and key."""
    notice = action.notice
    offset = _AT_START if notice.offset is None else notice.offset
    return (
        notice.kind is NoticeKind.REMINDER,
        notice.occurrences[0].start,
        -offset,
        action.deliveries[0].key,
    )


@dataclass(frozen=True)
class TickReport:
    """What one ``tick`` did, counted in posts (a grouped reminder is one).

    - sent: posted.
    - skipped: recorded as skipped without posting (see ``plan``).
    - already_handled: already in the ledger, so nothing was done.
    - not_configured: the feature has no channel, or was just disabled;
      nothing was recorded, so later ticks try again.
    - undeliverable: the channel is missing, the bot can't post there or the
      guild is unavailable; nothing was recorded, so later ticks try again.
    - rejected: Discord refused the post, which is recorded as skipped.
    - pending: the outcome is unknown, and the reconciler settles it.
    - failed: listing a source's guilds, planning a guild's posts, or making
      one post or skip raised an error, which was logged.
    """

    sent: int = 0
    skipped: int = 0
    already_handled: int = 0
    not_configured: int = 0
    undeliverable: int = 0
    rejected: int = 0
    pending: int = 0
    failed: int = 0


class _Tally(Enum):
    """One thing a tick did: a field of ``TickReport``."""

    SENT = 'sent'
    SKIPPED = 'skipped'
    ALREADY_HANDLED = 'already_handled'
    NOT_CONFIGURED = 'not_configured'
    UNDELIVERABLE = 'undeliverable'
    REJECTED = 'rejected'
    PENDING = 'pending'
    FAILED = 'failed'


_PUBLISH_TALLIES = {
    PublishOutcome.SENT: _Tally.SENT,
    PublishOutcome.ALREADY_HANDLED: _Tally.ALREADY_HANDLED,
    PublishOutcome.NOT_CONFIGURED: _Tally.NOT_CONFIGURED,
    PublishOutcome.UNDELIVERABLE: _Tally.UNDELIVERABLE,
    PublishOutcome.SKIPPED: _Tally.REJECTED,
    PublishOutcome.PENDING: _Tally.PENDING,
}


class ReminderEngine:
    """Posts the reminders and notices of every registered source.

    A job calls ``tick`` every minute (see ``tle.kcpc.bootstrap``), and a
    feature may call it as soon as its occurrences change. Each tick plans
    every guild afresh (see ``plan``), so ticks may run as often as wanted.
    """

    def __init__(
        self,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        publisher: Publisher,
        clock: Clock,
    ) -> None:
        self._guild_settings = guild_settings
        self._ledger = ledger
        self._publisher = publisher
        self._clock = clock
        self._sources: dict[str, ReminderSource] = {}
        # Ticks run one at a time. Overlapping ones would plan from the same
        # history and try the same posts twice: the ledger would refuse the
        # repeats, but only after both ticks had done the work.
        self._lock = asyncio.Lock()
        self._warnings = _Throttle(clock, _FAILURE_WARNING_INTERVAL)

    def register(self, source: ReminderSource) -> None:
        """Post about ``source``'s occurrences, from the next tick on.

        ``ValueError`` if its feature is not in the settings registry (no guild
        could enable it), or already has a source.
        """
        feature = source.feature
        if feature not in self._guild_settings.registry:
            raise ValueError(f'Unknown feature {feature!r}: register its spec first')
        if feature in self._sources:
            raise ValueError(f'A reminder source for {feature!r} is already registered')
        self._sources[feature] = source

    def unregister(self, feature: str) -> None:
        """Stop posting about the feature's occurrences. An unknown one is ignored."""
        self._sources.pop(feature, None)

    @property
    def features(self) -> list[str]:
        """The features with a registered source, sorted."""
        return sorted(self._sources)

    async def tick(self) -> TickReport:
        """Post, and record as skipped, whatever ``plan`` says for every guild.

        Sources go in feature order, and each one's guilds in id order: those
        that have its feature enabled. An error in one guild, or in one post,
        is logged and counted as failed, and the rest still run. Ticks never
        overlap: one called during another waits for it to finish. A tick opens
        no database transaction, so that each post's claim is committed before
        it is sent (see ``Publisher.publish``).
        """
        async with self._lock:
            now = self._clock.now()
            counts: Counter[_Tally] = Counter()
            # A snapshot, as a source may be unregistered meanwhile.
            sources = [self._sources[feature] for feature in self.features]
            for source in sources:
                await self._tick_source(source, now, counts)
        return TickReport(
            sent=counts[_Tally.SENT],
            skipped=counts[_Tally.SKIPPED],
            already_handled=counts[_Tally.ALREADY_HANDLED],
            not_configured=counts[_Tally.NOT_CONFIGURED],
            undeliverable=counts[_Tally.UNDELIVERABLE],
            rejected=counts[_Tally.REJECTED],
            pending=counts[_Tally.PENDING],
            failed=counts[_Tally.FAILED],
        )

    async def _tick_source(
        self, source: ReminderSource, now: datetime, counts: Counter[_Tally]
    ) -> None:
        feature = source.feature
        try:
            guilds = await self._guild_settings.enabled_guilds(feature)
        except Exception as exc:
            counts[_Tally.FAILED] += 1
            self._report_failure(
                (feature, None, 'guilds', type(exc)),
                exc,
                'Could not list the guilds with KCPC %s enabled, so none got '
                'reminders: %r',
                feature,
                exc,
            )
            return
        for guild_id, settings in guilds:
            await self._tick_guild(source, guild_id, settings, now, counts)

    async def _tick_guild(
        self,
        source: ReminderSource,
        guild_id: int,
        settings: FeatureSettings,
        now: datetime,
        counts: Counter[_Tally],
    ) -> None:
        feature = source.feature
        try:
            actions = await self._plan(source, guild_id, settings, now)
        except Exception as exc:
            counts[_Tally.FAILED] += 1
            self._report_failure(
                (feature, guild_id, 'plan', type(exc)),
                exc,
                'Could not plan KCPC %s reminders for guild %d: %r',
                feature,
                guild_id,
                exc,
            )
            return
        for action in actions:
            keys = ', '.join(delivery.key for delivery in action.deliveries)
            try:
                tally = await self._carry_out(source, guild_id, action)
            except Exception as exc:
                counts[_Tally.FAILED] += 1
                self._report_failure(
                    (feature, guild_id, action.kind, type(exc)),
                    exc,
                    'Could not %s KCPC %s %s in guild %d: %r',
                    action.kind.value,
                    feature,
                    keys,
                    guild_id,
                    exc,
                )
                continue
            counts[tally] += 1
            if tally is _Tally.ALREADY_HANDLED:
                # plan leaves out what the ledger has for the start, so the
                # ledger has these keys for another start or state: the source
                # reused a revision (see Occurrence).
                self._report_failure(
                    (feature, guild_id, tally),
                    None,
                    'Did not %s KCPC %s %s in guild %d: the ledger has them for '
                    'another start or state, so the source reused a revision',
                    action.kind.value,
                    feature,
                    keys,
                    guild_id,
                )

    async def _plan(
        self,
        source: ReminderSource,
        guild_id: int,
        settings: FeatureSettings,
        now: datetime,
    ) -> list[PlannedAction]:
        policy = source.policy(settings)
        # From start_window ago, for start announcements still due.
        occurrences = await source.occurrences(
            guild_id, settings, now - policy.start_window, now + policy.horizon
        )
        history = await self._history(guild_id, occurrences)
        return plan(now, guild_id, source.feature, policy, occurrences, history)

    async def _history(
        self, guild_id: int, occurrences: Sequence[Occurrence]
    ) -> dict[str, list[DeliveryRecord]]:
        """The guild's ledger records about ``occurrences``, one query per subject."""
        ids_by_subject: defaultdict[str, list[str]] = defaultdict(list)
        for occurrence in occurrences:
            ids_by_subject[occurrence.subject].append(occurrence.subject_id)
        history: defaultdict[str, list[DeliveryRecord]] = defaultdict(list)
        for subject, subject_ids in ids_by_subject.items():
            found = await self._ledger.history_for(guild_id, subject, subject_ids)
            # An id may recur under another subject; plan tells their records
            # apart.
            for subject_id, records in found.items():
                history[subject_id].extend(records)
        return dict(history)

    async def _carry_out(
        self, source: ReminderSource, guild_id: int, action: PlannedAction
    ) -> _Tally:
        """Record a skip, or render and publish a post; say what happened."""
        if action.kind is ActionKind.SKIP:
            # PlannedAction guarantees that a skip has a reason.
            return await self._record_skip(
                source.feature, guild_id, action.deliveries, cast(str, action.reason)
            )
        message = source.render(action.notice)
        result = await self._publisher.publish(action.deliveries, message)
        return _PUBLISH_TALLIES[result.outcome]

    async def _record_skip(
        self,
        feature: str,
        guild_id: int,
        deliveries: Sequence[Delivery],
        reason: str,
    ) -> _Tally:
        recorded = False
        for delivery in deliveries:
            if await self._ledger.record_skip(delivery, reason):
                recorded = True
                logger.info(
                    'Skipped KCPC %s %s in guild %d: %s',
                    feature,
                    delivery.key,
                    guild_id,
                    reason,
                )
        return _Tally.SKIPPED if recorded else _Tally.ALREADY_HANDLED

    def _report_failure(
        self, key: Hashable, error: Exception | None, msg: str, *args: object
    ) -> None:
        """Log a failure at WARNING with its traceback, once an hour per ``key``.

        The key is the feature, the guild, the step and the type of error, so a
        new kind of failure is reported at once. Repeats in between are logged
        at INFO, without the traceback. A failure without an ``error`` has no
        traceback.
        """
        if self._warnings.allow(key):
            logger.warning(msg, *args, exc_info=error)
        else:
            logger.info(msg, *args)


class _Throttle:
    """Lets each key through at most once per interval of clock time."""

    def __init__(self, clock: Clock, interval: timedelta) -> None:
        self._clock = clock
        self._interval = interval.total_seconds()
        self._last: dict[Hashable, float] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        return True
