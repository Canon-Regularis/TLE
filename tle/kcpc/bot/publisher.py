"""Posts automatic KCPC messages to Discord, each delivery at most once.

``DiscordPublisher`` implements ``core.publishing.Publisher`` on the delivery
ledger (KCPC_ARCHITECTURE.md section 4.3). It claims the deliveries, sends one
message whose footer carries the batch marker, then confirms the batch, or marks
it skipped if Discord refused the message. When the outcome of a send is unknown
(a timeout, a Discord 5xx, or an unexpected error once the send has begun) the
batch stays claimed, and ``reconcile`` later looks for the marker in the
channel: a post that is there is confirmed, and one that is not is sent again
while it is still worth sending. Every send of a batch carries the same nonce,
so Discord also answers a repeat within a few minutes with the first post.

Call ``publish`` outside any database transaction (see ``Publisher.publish``).
"""

import asyncio
import hashlib
import logging
from collections import Counter
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import TypeAlias

import aiohttp
import discord
from discord.ext import commands

from tle.kcpc.bot.embeds import find_batch_marker, to_embed
from tle.kcpc.core.clock import Clock
from tle.kcpc.core.ledger import Delivery, DeliveryLedger, DeliveryRecord
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.settings import FeatureSettings, GuildSettingsRepo

logger = logging.getLogger(__name__)

# The guild channels posts can go to. Announcement channels are TextChannels.
PostChannel: TypeAlias = discord.TextChannel | discord.Thread | discord.VoiceChannel

# What the bot needs in a channel to post there, and to find its posts again
# when reconciling. Sending in a thread takes a permission of its own.
_POST_PERMISSIONS = (
    'view_channel',
    'send_messages',
    'embed_links',
    'read_message_history',
)
_THREAD_POST_PERMISSIONS = (
    'view_channel',
    'send_messages_in_threads',
    'embed_links',
    'read_message_history',
)

# Failures after which a request may or may not have reached Discord.
_NETWORK_ERRORS = (asyncio.TimeoutError, aiohttp.ClientError, OSError)

# reconcile reads every message sent in a post's channel since a few minutes
# before the claim (the margin allows for the bot's clock running ahead of
# Discord's), newest first, so the latest attempt is met first. The claim time
# never changes, so the search covers every attempt. A channel with more
# messages than the scan limit since then can't be searched in full, so a post
# missing from what was read may still be there: it is skipped, not resent.
_HISTORY_MARGIN = timedelta(minutes=5)
_HISTORY_SCAN_LIMIT = 1000

_NONCE_LENGTH = 25  # the longest nonce Discord accepts


@dataclass(frozen=True)
class ReconcileReport:
    """What one ``reconcile`` run did, in batches.

    - confirmed: the post was found in its channel, so it is marked sent.
    - resent: it was not found and still worth sending, so it was sent again.
    - lost: it was not found, and has expired or has no stored message.
    - skipped: its channel is gone, the channel's history can't be read or is
      too long to search in full, or Discord refused the resend.
    - pending: left for the next run, because Discord could not be reached, the
      guild is unavailable, the resend failed unexpectedly, or the batch is
      being sent right now.
    - failed: settling it raised an unexpected error, which was logged.
    """

    confirmed: int = 0
    resent: int = 0
    lost: int = 0
    skipped: int = 0
    pending: int = 0
    failed: int = 0


class _Settled(Enum):
    """How ``reconcile`` settled one batch: a field of ``ReconcileReport``."""

    CONFIRMED = 'confirmed'
    RESENT = 'resent'
    LOST = 'lost'
    SKIPPED = 'skipped'
    PENDING = 'pending'
    FAILED = 'failed'


_RESEND_SETTLED = {
    PublishOutcome.SENT: _Settled.RESENT,
    PublishOutcome.SKIPPED: _Settled.SKIPPED,
    PublishOutcome.PENDING: _Settled.PENDING,
}


class _HistoryTooLong(Exception):
    """A channel had too many messages since a claim to search them all."""


class DiscordPublisher:
    """Posts features' messages in each guild's feature channel, via the ledger."""

    def __init__(
        self,
        bot: commands.Bot,
        settings: GuildSettingsRepo,
        ledger: DeliveryLedger,
        clock: Clock,
        *,
        stale_after: timedelta = timedelta(minutes=2),
        alert_interval: timedelta = timedelta(days=1),
    ) -> None:
        self._bot = bot
        self._settings = settings
        self._ledger = ledger
        self._clock = clock
        self._stale_after = stale_after
        self._alerts = _Throttle(clock, alert_interval)
        # Batches being sent right now. discord.py retries 5xx responses itself,
        # so a send can outlast stale_after, and reconciling its batch meanwhile
        # could post it twice. This guard only works if the bot publishes and
        # reconciles through one instance, KcpcServices.publisher, so features
        # must never build a DiscordPublisher of their own.
        self._sending: set[str] = set()

    async def publish(
        self, deliveries: Sequence[Delivery], message: OutgoingMessage
    ) -> PublishResult:
        """Post ``message`` once for the deliveries that are not in the ledger yet.

        The deliveries must share a guild and a feature, else ``ValueError``.
        The post goes to the feature's channel, mentioning the feature's role if
        ``message.mention_role`` is set. ``PublishOutcome`` lists the outcomes.

        Call it outside any database transaction, else ``RuntimeError``: the
        claim must be committed before the post is sent (``Publisher.publish``
        explains why). The check comes first, so misuse fails even while the
        feature is off.
        """
        self._ledger.require_outside_transaction()
        guild_id, feature = _scope(deliveries)
        config = await self._settings.get(guild_id, feature)
        if not config.enabled:
            return PublishResult(PublishOutcome.NOT_CONFIGURED, reason='disabled')
        if config.channel_id is None:
            return PublishResult(PublishOutcome.NOT_CONFIGURED, reason='no-channel')
        channel = self._post_channel(guild_id, config.channel_id)
        if channel is None:
            if self._guild_unavailable(guild_id):
                # Not for admins to fix: the channel is back once Discord sends
                # the guild, so the caller can simply try again later.
                logger.info(
                    'Guild %d is unavailable, so a KCPC %s post was not sent',
                    guild_id,
                    feature,
                )
                return PublishResult(
                    PublishOutcome.UNDELIVERABLE, reason='guild-unavailable'
                )
            return self._undeliverable(
                guild_id, config.channel_id, feature, 'channel-missing'
            )
        missing = missing_post_permissions(channel)
        if missing:
            reason = 'missing-permissions: ' + ', '.join(missing)
            return self._undeliverable(guild_id, channel.id, feature, reason)
        role = self._mention_role(channel, feature, config, message)
        claimed, batch = await self._ledger.claim(
            deliveries, channel_id=channel.id, message=message
        )
        if batch is None:
            return PublishResult(PublishOutcome.ALREADY_HANDLED)
        result = await self._send(channel, feature, batch, message, role)
        return replace(result, keys=tuple(delivery.key for delivery in claimed))

    async def reconcile(self) -> ReconcileReport:
        """Settle the batches claimed over ``stale_after`` ago and still unconfirmed.

        Each post is looked for in its channel. A post that is there is
        confirmed. One that is not is sent again under the same batch if every
        delivery is unexpired and the message was stored, and otherwise given
        up as lost. An error in one batch is logged, and the others still run.
        """
        bot_user = self._bot.user
        if bot_user is None:  # not logged in, so our own posts can't be recognised
            return ReconcileReport()
        stale = await self._ledger.stale_batches(self._clock.now() - self._stale_after)
        counts: Counter[_Settled] = Counter()
        for records in stale:
            counts[await self._settle_safely(records, bot_user.id)] += 1
        report = ReconcileReport(
            confirmed=counts[_Settled.CONFIRMED],
            resent=counts[_Settled.RESENT],
            lost=counts[_Settled.LOST],
            skipped=counts[_Settled.SKIPPED],
            pending=counts[_Settled.PENDING],
            failed=counts[_Settled.FAILED],
        )
        if stale:
            logger.info('Reconciled %d unconfirmed KCPC posts: %s', len(stale), report)
        return report

    def _post_channel(
        self, guild_id: int, channel_id: int | None
    ) -> PostChannel | None:
        """The guild's channel ``channel_id``, if it exists and can take posts."""
        channel = None if channel_id is None else self._bot.get_channel(channel_id)
        if isinstance(channel, PostChannel) and channel.guild.id == guild_id:
            return channel
        return None

    def _guild_unavailable(self, guild_id: int) -> bool:
        """Whether the bot is in the guild, but Discord hasn't sent its channels.

        After the bot (re)connects, discord.py knows none of a guild's channels
        until Discord sends the guild, which takes as long as any outage there.
        Its channels are unknown then, not gone. A guild the bot has left is not
        unavailable, but unknown.
        """
        guild = self._bot.get_guild(guild_id)
        return guild is not None and guild.unavailable

    def _undeliverable(
        self, guild_id: int, channel_id: int, feature: str, reason: str
    ) -> PublishResult:
        self._alert(
            (guild_id, channel_id, reason),
            "KCPC can't post %s messages in channel %d of guild %d (%s). Nothing "
            'is posted there until an admin fixes it, e.g. with /kcpc channel.',
            feature,
            channel_id,
            guild_id,
            reason,
        )
        return PublishResult(PublishOutcome.UNDELIVERABLE, reason=reason)

    def _mention_role(
        self,
        channel: PostChannel,
        feature: str,
        config: FeatureSettings,
        message: OutgoingMessage,
    ) -> discord.Role | None:
        """The feature's role, if it has one and the message wants it mentioned.

        A role that no longer exists is reported, and the post goes out without
        a mention. A role that Discord would not notify in ``channel`` (see
        ``can_ping_role``) is reported too, but still mentioned: that is
        harmless, and the mention notifies the role once an admin fixes it.
        """
        if not message.mention_role or config.role_id is None:
            return None
        role = channel.guild.get_role(config.role_id)
        if role is None:
            self._alert(
                (channel.guild.id, channel.id, 'role-missing'),
                'The KCPC %s role %d no longer exists in guild %d, so posts go '
                'out without a mention. Set a new one with /kcpc role.',
                feature,
                config.role_id,
                channel.guild.id,
            )
        elif not can_ping_role(channel, role):
            self._alert(
                (channel.guild.id, channel.id, 'role-unpingable'),
                'KCPC %s posts in channel %d of guild %d mention role %d, but '
                "Discord notifies nobody: the role isn't mentionable, and the bot "
                'lacks Mention @everyone, @here and All Roles there. Allow anyone '
                'to mention the role, or give the bot that permission in the '
                'channel.',
                feature,
                channel.id,
                channel.guild.id,
                role.id,
            )
        return role

    async def _send(
        self,
        channel: PostChannel,
        feature: str,
        batch: str,
        message: OutgoingMessage,
        role: discord.Role | None,
    ) -> PublishResult:
        """Send a claimed batch, and settle it in the ledger if the outcome is known."""
        self._sending.add(batch)
        try:
            return await self._send_and_record(channel, feature, batch, message, role)
        finally:
            self._sending.discard(batch)

    async def _send_and_record(
        self,
        channel: PostChannel,
        feature: str,
        batch: str,
        message: OutgoingMessage,
        role: discord.Role | None,
    ) -> PublishResult:
        try:
            fitted = message.within_discord_limits()
            content = _content(fitted.content, role)
            embed = to_embed(fitted, batch=batch)
            allowed_mentions = _allowed_mentions(role)
        except Exception:
            # A bug stopped the post before anything reached Discord, so free
            # the deliveries to be claimed again.
            await self._ledger.release(batch)
            raise
        # Once the send has begun the claim is never released: whatever fails,
        # the message may exist.
        try:
            sent = await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions,
                nonce=_nonce(channel.id, batch),
            )
        except discord.HTTPException as exc:
            return await self._after_http_error(channel, feature, batch, exc)
        except _NETWORK_ERRORS as exc:
            reason = 'timeout' if isinstance(exc, asyncio.TimeoutError) else 'network'
            return self._leave_claimed(channel, feature, batch, reason, exc)
        except Exception as exc:
            # discord.py reads Discord's answer after the message was created
            # (decoding it, the rate limit headers, building the Message), so
            # this can come after a successful post.
            return self._leave_claimed(
                channel,
                feature,
                batch,
                'unexpected-error',
                exc,
                level=logging.ERROR,
                exc_info=exc,
            )
        # A cancellation leaves the batch claimed for the reconciler, since the
        # message may be on its way; so does a failed confirm, since the
        # reconciler will find the message.
        await self._ledger.confirm(batch, sent.id)
        logger.info(
            'Posted KCPC %s batch %s in channel %d as message %d',
            feature,
            batch,
            channel.id,
            sent.id,
        )
        return PublishResult(PublishOutcome.SENT, message_id=sent.id)

    async def _after_http_error(
        self,
        channel: PostChannel,
        feature: str,
        batch: str,
        exc: discord.HTTPException,
    ) -> PublishResult:
        """Settle a batch whose send failed with an HTTP error from Discord."""
        if exc.status >= 500 or exc.status == 429:
            # A 5xx may have created the message anyway, and a 429 (after
            # discord.py's own retries) means try later: the reconciler checks
            # the channel, then resends if the post is still due.
            return self._leave_claimed(
                channel, feature, batch, f'discord-{exc.status}', exc
            )
        if isinstance(exc, (discord.Forbidden, discord.NotFound)):
            # Missing access or a deleted channel: for admins to fix.
            reason, level = f'discord-{exc.status}', logging.WARNING
        else:
            # Discord rejected the message itself, which is a bug here.
            reason, level = f'rejected-{exc.status}', logging.ERROR
        await self._ledger.mark_skipped(batch, reason)
        self._alert(
            (channel.guild.id, channel.id, reason),
            'Discord refused a KCPC %s post (batch %s) in channel %d of guild %d, '
            'so it was skipped: %s',
            feature,
            batch,
            channel.id,
            channel.guild.id,
            exc,
            level=level,
        )
        return PublishResult(PublishOutcome.SKIPPED, reason=reason)

    def _leave_claimed(
        self,
        channel: PostChannel,
        feature: str,
        batch: str,
        reason: str,
        exc: Exception,
        *,
        level: int = logging.WARNING,
        exc_info: BaseException | None = None,
    ) -> PublishResult:
        self._alert(
            (channel.guild.id, channel.id, reason),
            'Sending a KCPC %s post (batch %s) to channel %d failed (%r). The '
            'reconciler will check whether it arrived.',
            feature,
            batch,
            channel.id,
            exc,
            level=level,
            exc_info=exc_info,
        )
        return PublishResult(PublishOutcome.PENDING, reason=reason)

    async def _settle_safely(
        self, records: Sequence[DeliveryRecord], bot_user_id: int
    ) -> _Settled:
        """``_settle`` one batch, unless it is being sent; errors are logged."""
        batch = records[0].batch
        if batch in self._sending:
            return _Settled.PENDING
        try:
            return await self._settle(records, bot_user_id)
        except Exception as exc:
            self._alert(
                ('reconcile', batch),
                'Could not reconcile KCPC batch %s: %s',
                batch,
                exc,
                level=logging.ERROR,
                exc_info=exc,
            )
            return _Settled.FAILED

    async def _settle(
        self, records: Sequence[DeliveryRecord], bot_user_id: int
    ) -> _Settled:
        first = records[0]
        channel = self._post_channel(first.guild_id, first.channel_id)
        if channel is None:
            if self._guild_unavailable(first.guild_id):
                logger.info(
                    'Guild %d is unavailable, so KCPC batch %s waits for it',
                    first.guild_id,
                    first.batch,
                )
                return _Settled.PENDING
            await self._skip(
                first,
                'channel-missing',
                'KCPC gave up on a %s post (batch %s): channel %s of guild %d is gone.',
                first.feature,
                first.batch,
                first.channel_id,
                first.guild_id,
            )
            return _Settled.SKIPPED
        claimed_at = min(record.claimed_at for record in records)
        try:
            post = await self._find_post(channel, first.batch, claimed_at, bot_user_id)
        except discord.Forbidden:
            await self._skip(
                first,
                'unverifiable',
                "KCPC can't read the history of channel %d in guild %d, so it "
                "can't tell whether a %s post (batch %s) arrived, and skipped it. "
                'Give the bot Read Message History there.',
                channel.id,
                first.guild_id,
                first.feature,
                first.batch,
            )
            return _Settled.SKIPPED
        except _HistoryTooLong:
            await self._skip(
                first,
                'history-too-long',
                '%d or more messages were sent in channel %d of guild %d since a '
                'KCPC %s post (batch %s) was claimed, more than KCPC reads, so it '
                "can't tell whether the post arrived. It was skipped rather than "
                'risk posting it twice.',
                _HISTORY_SCAN_LIMIT,
                channel.id,
                first.guild_id,
                first.feature,
                first.batch,
            )
            return _Settled.SKIPPED
        except (discord.HTTPException, *_NETWORK_ERRORS) as exc:
            logger.info(
                'Could not search channel %d for KCPC batch %s (%r); will retry',
                channel.id,
                first.batch,
                exc,
            )
            return _Settled.PENDING
        if post is not None:
            await self._ledger.confirm(first.batch, post.id)
            return _Settled.CONFIRMED
        return await self._resend_or_give_up(channel, records)

    async def _find_post(
        self,
        channel: PostChannel,
        batch: str,
        claimed_at: datetime,
        bot_user_id: int,
    ) -> discord.Message | None:
        """The bot's post with ``batch``'s marker, if one was sent since the claim.

        Every message from ``_HISTORY_MARGIN`` before the claim until now is
        read, newest first. ``_HistoryTooLong`` if there are
        ``_HISTORY_SCAN_LIMIT`` or more, since the post could be among the
        messages not read.
        """
        after = discord.utils.time_snowflake(claimed_at - _HISTORY_MARGIN)
        history = channel.history(
            limit=_HISTORY_SCAN_LIMIT,
            after=discord.Object(id=after),
            oldest_first=False,
        )
        read = 0
        async for message in history:
            if message.author.id == bot_user_id and find_batch_marker(message) == batch:
                return message
            read += 1
        if read >= _HISTORY_SCAN_LIMIT:
            raise _HistoryTooLong
        return None

    def _any_expired(self, records: Sequence[DeliveryRecord]) -> bool:
        now = self._clock.now()
        return any(
            record.expires_at is not None and record.expires_at <= now
            for record in records
        )

    async def _resend_or_give_up(
        self, channel: PostChannel, records: Sequence[DeliveryRecord]
    ) -> _Settled:
        """Send a post that never arrived again, unless it is no longer possible
        or worth it: its message was not stored, or a delivery has expired."""
        first = records[0]
        if first.payload is not None and not self._any_expired(records):
            return await self._resend(channel, first, first.payload)
        await self._skip(
            first,
            'lost',
            'A KCPC %s post (batch %s) never arrived in channel %d of guild %d, '
            'and %s, so it was given up.',
            first.feature,
            first.batch,
            channel.id,
            first.guild_id,
            'its message was not stored' if first.payload is None else 'it expired',
        )
        return _Settled.LOST

    async def _resend(
        self, channel: PostChannel, record: DeliveryRecord, message: OutgoingMessage
    ) -> _Settled:
        """Send the post again under its batch, mentioning the current role."""
        config = await self._settings.get(record.guild_id, record.feature)
        role = self._mention_role(channel, record.feature, config, message)
        result = await self._send(channel, record.feature, record.batch, message, role)
        return _RESEND_SETTLED[result.outcome]

    async def _skip(
        self, record: DeliveryRecord, reason: str, msg: str, *args: object
    ) -> None:
        """Mark ``record``'s batch skipped for ``reason``, and alert admins."""
        await self._ledger.mark_skipped(record.batch, reason)
        self._alert((record.guild_id, record.channel_id, reason), msg, *args)

    def _alert(
        self,
        key: Hashable,
        msg: str,
        *args: object,
        level: int = logging.WARNING,
        exc_info: BaseException | None = None,
    ) -> None:
        """Log for admins, at most once per ``key`` per alert interval.

        WARNING and above reach the Discord log channel, so repeats within the
        interval are logged at INFO instead, without a traceback.
        """
        if self._alerts.allow(key):
            logger.log(level, msg, *args, exc_info=exc_info)
        else:
            logger.info(msg, *args)


def missing_post_permissions(channel: PostChannel) -> list[str]:
    """The permissions the bot lacks in ``channel`` to post there.

    Names are ``discord.Permissions`` attributes. Read Message History is
    needed too, for the reconciler to find the bot's posts.
    """
    permissions = channel.permissions_for(channel.guild.me)
    required = (
        _THREAD_POST_PERMISSIONS
        if isinstance(channel, discord.Thread)
        else _POST_PERMISSIONS
    )
    return [name for name in required if not getattr(permissions, name)]


def can_ping_role(channel: PostChannel, role: discord.Role) -> bool:
    """Whether the bot mentioning ``role`` in ``channel`` notifies its members.

    Discord only notifies a role that is mentionable, or when the sender has
    Mention @everyone, @here and All Roles in that channel, after the
    channel's permission overwrites. Otherwise the mention is shown, silently.
    """
    return (
        role.mentionable or channel.permissions_for(channel.guild.me).mention_everyone
    )


def _nonce(channel_id: int, batch: str) -> str:
    """The nonce of every send of ``batch`` to the channel.

    discord.py asks Discord to enforce a nonce: a send that repeats one Discord
    created in the last few minutes gets that message back instead of a new
    post. discord.py's own retries of a 5xx rely on this, and one nonce per
    batch extends it to the reconciler's resends. The channel is part of it,
    so batches with the same marker in different channels never share one.
    """
    text = f'{channel_id}:{batch}'.encode()
    return hashlib.sha1(text, usedforsecurity=False).hexdigest()[:_NONCE_LENGTH]


def _scope(deliveries: Sequence[Delivery]) -> tuple[int, str]:
    """The one guild and feature that all ``deliveries`` are for."""
    scopes = {(delivery.guild_id, delivery.feature) for delivery in deliveries}
    if len(scopes) != 1:
        raise ValueError(
            'A post needs deliveries for exactly one guild and feature, '
            f'not {sorted(scopes)}'
        )
    (scope,) = scopes
    return scope


def _content(text: str | None, role: discord.Role | None) -> str | None:
    """The role mention and ``text``, joined by a space; None if both are absent."""
    parts = [part for part in (role.mention if role else None, text) if part]
    return ' '.join(parts) or None


def _allowed_mentions(role: discord.Role | None) -> discord.AllowedMentions:
    # Only the feature's role may be pinged: never @everyone, nor a user or
    # another role that the text happens to mention.
    return discord.AllowedMentions(
        everyone=False, users=False, roles=[role] if role else False
    )


class _Throttle:
    """Lets each key through at most once per interval of clock time."""

    def __init__(self, clock: Clock, interval: timedelta) -> None:
        self._clock = clock
        self._interval = interval
        self._last: dict[Hashable, datetime] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock.now()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        return True
