"""The algorithm of the month: a topic from the catalog on the 1st at noon.

Every server with the feature on gets a topic on the 1st of each month at
noon, club time (a slot). The job runs ``run_slot`` at each slot, for every
server the bot is in, and an admin's /kcpc algo post-now runs ``run_guild``
for the latest slot. Runs for one server take turns, with each other and
with its admins' rerolls.

The first try of the month picks the topic, at random, and stores it before
posting it, so that a retry posts the same topic. A server gets no topic
twice until it has had every topic in the catalog; then the cycle starts
again (``remaining``). A topic counts as had once its post went out, or may
have: its delivery is sent, or claimed and not yet confirmed.

A reroll stores another topic that the cycle allows as the month's next
revision, and posts it; the topic it replaced counts as never had. The
revisions before it stay, and members are shown, for each month, its newest
revision whose post went out, so that a month they saw never disappears.

Each post's delivery key holds the server, the month and the pick's revision,
so running a slot again posts nothing twice, while a reroll, a new revision,
posts again. A post that can't be delivered yet (UNDELIVERABLE) is left for a
retry: ``run_slot`` raises, so that the job tries again within its grace.
"""

import asyncio
import logging
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryStatus
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, Publisher
from tle.kcpc.core.schedule import Monthly
from tle.kcpc.core.settings import GuildSettingsRepo
from tle.kcpc.features.algo import markdown
from tle.kcpc.features.algo.catalog import ALGO_TOPICS, AlgoTopic
from tle.kcpc.features.algo.repo import AlgoPick, AlgoRepo

logger = logging.getLogger(__name__)

# The feature's key: its settings, /kcpc algo and /notify algo go by it.
ALGO = 'algo'
ALGO_JOB = 'algo.post'
# The 1st of each month at noon, club time.
POST_DAY = 1
POST_TIME = time(12, 0)
FOOTER = 'KCPC algorithm of the month'
# The ledger's subject for a server's topic, whose id is the month, and the
# kind of its post.
SUBJECT = 'algo'
TOPIC = 'topic'

# The ledger statuses of a post that went out, or may have.
_POSTED = frozenset({DeliveryStatus.SENT, DeliveryStatus.CLAIMED})
_NO_OTHER_TOPIC = (
    '**{name}** is the only topic this server has yet to have before the list '
    'starts over, so there is no other to reroll to.'
)
_UNCONFIRMED = (
    "The last post of {month}'s topic, **{name}**, isn't confirmed yet. "
    'Please try again in a few minutes.'
)
# In English, as everything else the bot says, whatever the locale.
_MONTH_NAMES = (
    'January',
    'February',
    'March',
    'April',
    'May',
    'June',
    'July',
    'August',
    'September',
    'October',
    'November',
    'December',
)

# Whether the bot is in the server with this ID.
InGuild = Callable[[int], bool]


def _in_every_guild(guild_id: int) -> bool:
    """Counts the bot in every server, so that none is skipped."""
    return True


@dataclass(frozen=True)
class PostResult:
    """What became of the post of a server's topic for the month."""

    pick: AlgoPick | None  # None if nothing was picked: the feature isn't set up
    outcome: PublishOutcome
    reason: str | None  # the publisher's, e.g. 'guild-unavailable'
    replaced: AlgoPick | None = None  # the pick a reroll replaced


class NoOtherTopic(KcpcUserError):
    """A reroll found no other topic that the cycle allows this month."""

    def __init__(self, name: str) -> None:
        super().__init__(_NO_OTHER_TOPIC.format(name=markdown.escape(name)))


class UnconfirmedPost(KcpcUserError):
    """A reroll while Discord hasn't confirmed the post of the month's topic.

    The reconciler may still send that post, which would then come after the
    new topic's.
    """

    def __init__(self, month: str, name: str) -> None:
        super().__init__(
            _UNCONFIRMED.format(month=month_name(month), name=markdown.escape(name))
        )


def post_key(pick: AlgoPick) -> str:
    """The delivery key of the post of ``pick``'s revision."""
    return f'algo:{pick.guild_id}:{pick.month}:r{pick.revision}'


def month_name(month: str) -> str:
    """The name of ``month`` ('YYYY-MM'), as posts and replies give it:
    'October'.
    """
    _, number = month.split('-')
    return _MONTH_NAMES[int(number) - 1]


def read_links(topic: AlgoTopic) -> str:
    """Where to read about ``topic``: its articles' links, as posts show them."""
    links = [markdown.link('GeeksforGeeks', topic.gfg_url)]
    if topic.cp_algorithms_url is not None:
        links.append(markdown.link('cp-algorithms', topic.cp_algorithms_url))
    return ' · '.join(links)


class AlgoService:
    """Picks and posts each server's topic of the month; see the module docstring.

    ``topics`` is the catalog it picks from, and ``rng`` what it picks with.
    """

    def __init__(
        self,
        repo: AlgoRepo,
        guild_settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        publisher: Publisher,
        clock: Clock,
        schedule: Monthly,
        *,
        topics: Sequence[AlgoTopic] = ALGO_TOPICS,
        rng: random.Random | None = None,
    ) -> None:
        if not topics:
            raise ValueError('The catalog needs at least one topic')
        self._by_slug = {found.slug: found for found in topics}
        if len(self._by_slug) != len(topics):
            raise ValueError("The catalog's slugs must be unique")
        self._topics = tuple(topics)
        self._repo = repo
        self._guild_settings = guild_settings
        self._ledger = ledger
        self._publisher = publisher
        self._clock = clock
        self._schedule = schedule
        self._rng = random.Random() if rng is None else rng
        # Held for a whole run, so that the job, post-now and a reroll never
        # pick or post a server's topic at once.
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def topics(self) -> tuple[AlgoTopic, ...]:
        """The catalog that the service picks from."""
        return self._topics

    def topic(self, slug: str) -> AlgoTopic | None:
        """The catalog's topic with ``slug``; None if it has none, or no longer."""
        return self._by_slug.get(slug)

    def name(self, slug: str) -> str:
        """The name of the topic with ``slug``, or the slug if it has left the
        catalog.
        """
        found = self.topic(slug)
        return slug if found is None else found.name

    async def run_slot(
        self, slot: datetime, *, in_guild: InGuild = _in_every_guild
    ) -> None:
        """Run ``slot`` for every server with the feature on: the job's handler.

        A server the bot isn't in (``in_guild``) is skipped: nothing can be
        posted there, and no admin there can turn the feature off. Each
        server's run is apart from the others', and one that fails is logged
        at INFO. Then, if any failed or has a post to deliver later, raises
        ``RuntimeError`` (from the first failure), so that the job retries the
        slot; a retry posts nothing that went out already.
        """
        failures: list[tuple[int, Exception]] = []
        undelivered: list[int] = []
        for guild_id, _ in await self._guild_settings.enabled_guilds(ALGO):
            if not in_guild(guild_id):
                logger.info(
                    'Skipping the algorithm of the month of guild %d for its %s '
                    'slot: the bot is not in it',
                    guild_id,
                    slot,
                )
                continue
            try:
                result = await self.run_guild(guild_id, slot)
            except Exception as exc:
                logger.info(
                    'Could not run the algorithm of the month of guild %d for its '
                    '%s slot',
                    guild_id,
                    slot,
                    exc_info=True,
                )
                failures.append((guild_id, exc))
                continue
            if result.outcome is PublishOutcome.UNDELIVERABLE:
                logger.info(
                    'The algorithm of the month of guild %d for its %s slot could '
                    'not be delivered yet; the slot will be tried again',
                    guild_id,
                    slot,
                )
                undelivered.append(guild_id)
        if failures or undelivered:
            guilds = sorted({guild_id for guild_id, _ in failures} | set(undelivered))
            error = RuntimeError(
                'The algorithm of the month was not posted in guilds '
                f'{", ".join(map(str, guilds))}'
            )
            raise error from (failures[0][1] if failures else None)

    async def run_guild(self, guild_id: int, slot: datetime) -> PostResult:
        """Post the guild's topic for ``slot``, picking it first if need be.

        Writes nothing while the feature is off or has no channel. An instant
        that isn't a slot runs the slot before it.
        """
        slot = self._schedule.prev_at_or_before(slot)
        month = self._month(slot)
        async with self._locks[guild_id]:
            not_set_up = await self._not_set_up(guild_id)
            if not_set_up is not None:
                return not_set_up
            # A retry of the month posts the topic that its first try picked.
            pick = await self._repo.get(guild_id, month)
            if pick is None:
                pick = await self._pick(guild_id, month, slot)
            elif (
                self.topic(pick.slug) is None
                and await self._ledger.get(post_key(pick)) is None
            ):
                # Taken out of the catalog before its post went out: it can't
                # be posted, so another topic takes its place.
                pick = await self._repick(pick)
            return await self._post(pick)

    async def reroll(self, guild_id: int) -> PostResult:
        """Replace the guild's topic for this month with another, and post it.

        This month is that of the latest slot. Without a pick for it yet, one
        is picked and posted, as post-now does. The new topic is one the cycle
        allows, other than the month's topic and the one members were shown
        this month, and is stored as the month's next revision. The topic
        replaced counts as one the guild never had. Raises ``UnconfirmedPost``
        while the post of the month's topic is claimed but unconfirmed, and
        ``NoOtherTopic`` if the cycle allows no other topic. Writes nothing
        while the feature is off or has no channel.
        """
        slot = self._schedule.prev_at_or_before(self._clock.now())
        month = self._month(slot)
        async with self._locks[guild_id]:
            not_set_up = await self._not_set_up(guild_id)
            if not_set_up is not None:
                return not_set_up
            revisions = await self._repo.revisions(guild_id, month)
            if not revisions:
                return await self._post(await self._pick(guild_id, month, slot))
            pick = revisions[0]
            record = await self._ledger.get(post_key(pick))
            if record is not None and record.status is DeliveryStatus.CLAIMED:
                raise UnconfirmedPost(month, self.name(pick.slug))
            shown = await self._shown(guild_id, revisions)
            left_out = {pick.slug, *(found.slug for found in shown)}
            had = await self._had(guild_id, month)
            candidates = [
                found for found in self.remaining(had) if found.slug not in left_out
            ]
            if not candidates:
                raise NoOtherTopic(self.name(pick.slug))
            chosen = self._rng.choice(candidates)
            stored = await self._repo.reroll(
                guild_id, month, chosen.slug, self._clock.now()
            )
            if stored is None:  # read under the same lock just now
                raise RuntimeError('The pick to reroll was not found again')
            logger.info(
                "Rerolled guild %d's algorithm of the month for %s: %s in place of %s",
                guild_id,
                month,
                stored.slug,
                pick.slug,
            )
            return await self._post(stored, replaced=pick)

    async def current(self, guild_id: int) -> AlgoPick | None:
        """The guild's topic for this month (the latest slot's), as members
        were shown it: the month's newest revision whose post went out, or was
        claimed and may have. None if none did.
        """
        slot = self._schedule.prev_at_or_before(self._clock.now())
        revisions = await self._repo.revisions(guild_id, self._month(slot))
        shown = await self._shown(guild_id, revisions)
        return shown[0] if shown else None

    async def this_months_pick(self, guild_id: int) -> AlgoPick | None:
        """The guild's pick for this month (the latest slot's), whether or
        not its post went out: the month's newest revision. None if it has
        none.
        """
        slot = self._schedule.prev_at_or_before(self._clock.now())
        return await self._repo.get(guild_id, self._month(slot))

    async def history(self, guild_id: int) -> list[AlgoPick]:
        """The guild's topic for each month, as members were shown it, newest
        first: the month's newest revision whose post went out, or was claimed
        and may have. A month with no such revision is left out.
        """
        return await self._shown(guild_id, await self._repo.history(guild_id))

    async def posted(self, pick: AlgoPick) -> bool:
        """Whether the post of ``pick``'s revision went out, or was claimed and
        may have.
        """
        record = await self._ledger.get(post_key(pick))
        return record is not None and record.status in _POSTED

    async def upcoming(self, guild_id: int) -> list[AlgoTopic]:
        """The topics that the guild's next pick chooses from (``remaining``).

        This month's own topic (the latest slot's) counts as had even before
        its post goes out, since a retry posts it.
        """
        slot = self._schedule.prev_at_or_before(self._clock.now())
        month = self._month(slot)
        had = await self._had(guild_id, month)
        pick = await self._repo.get(guild_id, month)
        if pick is not None:
            had.append(pick.slug)
        return self.remaining(had)

    def remaining(self, picks_in_order: Sequence[str]) -> list[AlgoTopic]:
        """The topics a server may get next, after the picks ``picks_in_order``
        (slugs, oldest first), in the catalog's order. Never empty.

        Those it hasn't had since it last had every topic. Slugs no longer in
        the catalog don't count.
        """
        had: set[str] = set()
        for slug in picks_in_order:
            if slug not in self._by_slug:
                continue
            had.add(slug)
            if len(had) == len(self._by_slug):
                had.clear()  # it has had every topic: the cycle starts again
        return [found for found in self._topics if found.slug not in had]

    async def _not_set_up(self, guild_id: int) -> PostResult | None:
        """The result of a run while the feature is off or has no channel;
        None if it is set up.
        """
        settings = await self._guild_settings.get(guild_id, ALGO)
        if not settings.enabled:
            return PostResult(None, PublishOutcome.NOT_CONFIGURED, 'disabled')
        if settings.channel_id is None:
            return PostResult(None, PublishOutcome.NOT_CONFIGURED, 'no-channel')
        return None

    async def _had(self, guild_id: int, month: str) -> list[str]:
        """The slugs of the topics that the guild had before ``month``, oldest
        first, as the cycle counts them: of each earlier month, its newest
        revision whose post went out, or was claimed and may have.
        """
        picks = await self._repo.picks_before(guild_id, month)
        return [pick.slug for pick in await self._shown(guild_id, picks)]

    async def _shown(self, guild_id: int, picks: Sequence[AlgoPick]) -> list[AlgoPick]:
        """What members were shown of each month of ``picks``, which holds
        every revision of its months: the month's newest revision whose post
        went out, or was claimed and may have. The months keep their order in
        ``picks``, and a month with no such revision is left out.
        """
        records = await self._ledger.history_for(
            guild_id, SUBJECT, {pick.month for pick in picks}
        )
        went_out = {
            record.key
            for month_records in records.values()
            for record in month_records
            if record.status in _POSTED
        }
        newest: dict[str, AlgoPick] = {}
        for pick in picks:
            kept = newest.get(pick.month)
            if post_key(pick) in went_out and (
                kept is None or pick.revision > kept.revision
            ):
                newest[pick.month] = pick
        return list(newest.values())

    async def _pick(self, guild_id: int, month: str, slot: datetime) -> AlgoPick:
        """Pick the guild's topic for ``month`` by the cycle rule, and store it."""
        had = await self._had(guild_id, month)
        chosen = self._rng.choice(self.remaining(had))
        stored = await self._repo.create(
            AlgoPick(
                guild_id=guild_id,
                month=month,
                slot=slot,
                slug=chosen.slug,
                revision=0,
                picked_at=self._clock.now(),
            )
        )
        logger.info(
            "Picked %s as guild %d's algorithm of the month for %s",
            stored.slug,
            guild_id,
            month,
        )
        return stored

    async def _repick(self, pick: AlgoPick) -> AlgoPick:
        """Replace ``pick``, whose topic left the catalog, with another."""
        had = await self._had(pick.guild_id, pick.month)
        chosen = self._rng.choice(self.remaining(had))
        stored = await self._repo.reroll(
            pick.guild_id, pick.month, chosen.slug, self._clock.now()
        )
        if stored is None:  # read under the same lock just now
            raise RuntimeError('The pick to replace was not found again')
        logger.info(
            "Picked %s as guild %d's algorithm of the month for %s, in place of "
            '%s, which is no longer in the catalog',
            stored.slug,
            pick.guild_id,
            pick.month,
            pick.slug,
        )
        return stored

    async def _post(
        self, pick: AlgoPick, *, replaced: AlgoPick | None = None
    ) -> PostResult:
        """Post ``pick``'s topic, which mentions the feature's role.

        The post of a later revision, whether a reroll's or one posted after
        it, says which topic it replaces if members were shown one this
        month: the newest earlier revision whose post went out, or may have.
        ``replaced`` is the pick a reroll replaced, for the result.
        """
        delivery = Delivery(
            key=post_key(pick),
            guild_id=pick.guild_id,
            feature=ALGO,
            subject=SUBJECT,
            subject_id=pick.month,
            kind=TOPIC,
            occurrence_start=pick.slot,
            revision=pick.revision,
            expires_at=self._schedule.next_after(pick.slot),
        )
        earlier = [
            found
            for found in await self._repo.revisions(pick.guild_id, pick.month)
            if found.revision < pick.revision
        ]
        shown = await self._shown(pick.guild_id, earlier)
        replaces = self.name(shown[0].slug) if shown else None
        result = await self._publisher.publish(
            [delivery], self._message(pick, replaces)
        )
        return PostResult(pick, result.outcome, result.reason, replaced)

    def _message(self, pick: AlgoPick, replaces: str | None) -> OutgoingMessage:
        """The post of ``pick``'s topic; ``replaces`` names the topic it replaces."""
        found = self.topic(pick.slug)
        if found is None:
            # Only for a post in the ledger already, which goes out no more.
            return OutgoingMessage(
                title=f'Algorithm of the month: {pick.slug}', footer=FOOTER
            )
        lines = []
        if replaces is not None:
            lines.append(
                f"This replaces {month_name(pick.month)}'s earlier pick, "
                f'**{markdown.escape(replaces)}**.'
            )
        lines += [
            markdown.escape(found.summary),
            f'**Level:** {found.level.value}',
            f'**Read:** {read_links(found)}',
        ]
        return OutgoingMessage(
            title=f'Algorithm of the month: {found.name}',
            description='\n'.join(lines),
            url=found.gfg_url,
            footer=FOOTER,
            mention_role=True,
        )

    def _month(self, slot: datetime) -> str:
        """The slot's month, as stored and in delivery keys: 'YYYY-MM' in club time."""
        return slot.astimezone(self._schedule.tz).strftime('%Y-%m')
