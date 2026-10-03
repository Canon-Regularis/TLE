"""Tests for tle.kcpc.bot.publisher: posting through the ledger, and reconciling.

Discord is faked: ``FakeDiscord.post`` creates messages that the channel's
history then returns, so a test can make a send fail before or after Discord
created the message and check what the reconciler makes of it. The last tests
crash the bot mid-send and restart it on the same kcpc.db file.
"""

import asyncio
import functools
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord
import pytest
from discord.ext import commands

from tle.kcpc.bot.embeds import find_batch_marker, to_embed
from tle.kcpc.bot.publisher import (
    DiscordPublisher,
    ReconcileReport,
    can_ping_role,
    missing_post_permissions,
)
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import (
    Delivery,
    DeliveryLedger,
    DeliveryRecord,
    DeliveryStatus,
    marker_for,
)
from tle.kcpc.core.messages import OutgoingMessage
from tle.kcpc.core.migrations import open_database
from tle.kcpc.core.publishing import PublishOutcome, PublishResult
from tle.kcpc.core.settings import GuildSettingsRepo, default_registry

E = TypeVar('E', bound=discord.HTTPException)

CLAIMED = DeliveryStatus.CLAIMED
SENT = DeliveryStatus.SENT
SKIPPED = DeliveryStatus.SKIPPED

# Real snowflakes are 64-bit, beyond SQLite's REAL precision, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
CHANNEL_ID = 1_200_000_000_000_000_001
OTHER_CHANNEL_ID = 1_200_000_000_000_000_002
ROLE_ID = 1_300_000_000_000_000_001
NEW_ROLE_ID = 1_300_000_000_000_000_002
BOT_USER_ID = 1_400_000_000_000_000_001
MEMBER_ID = 1_400_000_000_000_000_002  # someone chatting in the channel

FEATURE = 'workshops'
KEY = 'remind:1:event:graphs:60:r0'
OTHER_KEY = 'remind:1:event:trees:60:r0'
PUBLISHER_LOGGER = 'tle.kcpc.bot.publisher'

STALE_AFTER = timedelta(minutes=2)  # the publisher's default
STALE = STALE_AFTER + timedelta(minutes=1)

MESSAGE = OutgoingMessage(
    title='Graphs 101 starts in 1 hour',
    content='See you there!',
    footer='KCPC workshops',
)

POST_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True, read_message_history=True
)
THREAD_POST_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages_in_threads=True,
    embed_links=True,
    read_message_history=True,
)


def http_error(cls: type[E], status: int, message: str | dict[str, Any] = '') -> E:
    return cls(MagicMock(status=status, reason='Reason'), message)


class FakeDiscord:
    """A bot in one guild, starting with a text channel and a mentionable role.

    Sends create messages in their channel. A channel's history returns that
    channel's messages after the given snowflake, as discord.py does: oldest
    first unless ``oldest_first`` is False, and at most ``limit`` of them. It
    raises instead while ``history_errors`` holds an error for the next call.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.posts: list[MagicMock] = []
        self.history_errors: list[BaseException] = []
        self._authors: dict[int, MagicMock] = {}
        self.guild = MagicMock(spec=discord.Guild, id=GUILD_ID, unavailable=False)
        self.guild.me = MagicMock(spec=discord.Member)
        self.roles: dict[int, MagicMock] = {}
        self.guild.get_role.side_effect = self.roles.get
        self.role = self.add_role(ROLE_ID)
        self.bot = MagicMock(spec=commands.Bot)
        self.bot.user = MagicMock(spec=discord.ClientUser, id=BOT_USER_ID)
        self.guilds: dict[int, MagicMock] = {GUILD_ID: self.guild}
        self.bot.get_guild.side_effect = self.guilds.get
        self.channels: dict[int, MagicMock] = {}
        self.bot.get_channel.side_effect = self.channels.get
        self.channel = self.add_channel(discord.TextChannel, CHANNEL_ID)

    def add_role(self, role_id: int, *, mentionable: bool = True) -> MagicMock:
        role = MagicMock(
            spec=discord.Role,
            id=role_id,
            mention=f'<@&{role_id}>',
            mentionable=mentionable,
        )
        self.roles[role_id] = role
        return role

    def add_channel(
        self,
        kind: type[discord.abc.Messageable],
        channel_id: int,
        *,
        permissions: discord.Permissions = POST_PERMISSIONS,
    ) -> MagicMock:
        channel = MagicMock(spec=kind, id=channel_id, guild=self.guild)
        channel.permissions_for.return_value = permissions
        channel.send = AsyncMock(
            side_effect=functools.partial(self.post, channel_id=channel_id)
        )
        channel.history = MagicMock(
            side_effect=functools.partial(self.history, channel_id)
        )
        self.channels[channel_id] = channel
        return channel

    async def post(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
        nonce: str | int | None = None,
        channel_id: int = CHANNEL_ID,
        author_id: int = BOT_USER_ID,
    ) -> MagicMock:
        """Create a message, as Discord does for a send."""
        message = MagicMock(spec=discord.Message, content=content, nonce=nonce)
        # Snowflakes grow with time, which history(after=...) relies on.
        message.id = discord.utils.time_snowflake(self._clock.now()) + len(self.posts)
        message.channel_id = channel_id
        message.author = self._author(author_id)
        message.embeds = [] if embed is None else [embed]
        self.posts.append(message)
        return message

    async def chatter(self, count: int) -> None:
        """Have a member post ``count`` messages in the channel."""
        for _ in range(count):
            await self.post(content='chat', author_id=MEMBER_ID)

    def history(
        self,
        channel_id: int,
        *,
        limit: int | None,
        after: discord.abc.Snowflake,
        oldest_first: bool | None = None,
    ) -> AsyncIterator[MagicMock]:
        # discord.py's default order, since `after` is always given here.
        oldest_first = True if oldest_first is None else oldest_first
        return self._history(channel_id, limit, after.id, oldest_first)

    async def _history(
        self, channel_id: int, limit: int | None, after_id: int, oldest_first: bool
    ) -> AsyncIterator[MagicMock]:
        if self.history_errors:
            raise self.history_errors.pop(0)
        found = [
            post
            for post in self.posts
            if post.channel_id == channel_id and post.id > after_id
        ]
        found.sort(key=lambda post: int(post.id), reverse=not oldest_first)
        for message in found[:limit]:
            yield message

    def _author(self, author_id: int) -> MagicMock:
        if author_id not in self._authors:
            self._authors[author_id] = MagicMock(spec=discord.Member, id=author_id)
        return self._authors[author_id]


@pytest.fixture
def fake(clock: FakeClock) -> FakeDiscord:
    return FakeDiscord(clock)


@pytest.fixture
async def publisher(
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> DiscordPublisher:
    """A publisher for a guild that posts workshops in the channel, with a role."""
    await guild_settings.update(
        GUILD_ID, FEATURE, enabled=True, channel_id=CHANNEL_ID, role_id=ROLE_ID
    )
    return DiscordPublisher(fake.bot, guild_settings, ledger, clock)


def delivery(
    key: str = KEY,
    *,
    guild_id: int = GUILD_ID,
    feature: str = FEATURE,
    expires_at: datetime | None = None,
) -> Delivery:
    return Delivery(key=key, guild_id=guild_id, feature=feature, expires_at=expires_at)


async def record(ledger: DeliveryLedger, key: str = KEY) -> DeliveryRecord:
    found = await ledger.get(key)
    assert found is not None
    return found


async def ledger_is_empty(ledger: DeliveryLedger) -> bool:
    return await ledger.status_counts() == {CLAIMED: 0, SENT: 0, SKIPPED: 0}


def sent_kwargs(channel: MagicMock) -> dict[str, Any]:
    """The arguments of the channel's latest send."""
    assert channel.send.await_args is not None
    kwargs: dict[str, Any] = channel.send.await_args.kwargs
    return kwargs


def logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == PUBLISHER_LOGGER and record.levelno == level
    ]


def failing_send(
    fake: FakeDiscord, error: BaseException, *, created: bool
) -> Callable[..., Awaitable[None]]:
    """A send to the channel that raises ``error``, after Discord created the
    message if ``created``."""

    async def send(**kwargs: Any) -> None:
        if created:
            await fake.post(**kwargs)
        raise error

    return send


async def publish_pending(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    item: Delivery,
    *,
    created: bool,
    error: BaseException | None = None,
) -> None:
    """Publish ``item`` with a send that fails with ``error`` (a timeout by
    default), after Discord created the message or before; then Discord works
    again."""
    error = asyncio.TimeoutError() if error is None else error
    fake.channel.send.side_effect = failing_send(fake, error, created=created)
    result = await publisher.publish([item], MESSAGE)
    assert result.outcome is PublishOutcome.PENDING
    fake.channel.send.side_effect = fake.post


@pytest.mark.parametrize(
    ('changes', 'reason'),
    [({'enabled': False}, 'disabled'), ({'channel_id': None}, 'no-channel')],
)
async def test_nothing_is_posted_for_an_unconfigured_feature(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    changes: dict[str, Any],
    reason: str,
) -> None:
    await guild_settings.update(GUILD_ID, FEATURE, **changes)

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(PublishOutcome.NOT_CONFIGURED, reason=reason)
    fake.channel.send.assert_not_awaited()
    assert await ledger_is_empty(ledger)


@pytest.mark.parametrize('enabled', [True, False], ids=['configured', 'disabled'])
async def test_publishing_inside_a_transaction_is_refused(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    ledger: DeliveryLedger,
    db: Database,
    enabled: bool,
) -> None:
    # The claim would not be committed before the send, so a rollback (or a
    # crash mid-send) would forget a post that went out. Refused even while
    # the feature is off, so the mistake shows before the feature is set up.
    await guild_settings.update(GUILD_ID, FEATURE, enabled=enabled)

    with pytest.raises(RuntimeError, match='outside any database transaction'):
        async with db.transaction():
            await publisher.publish([delivery()], MESSAGE)

    fake.channel.send.assert_not_awaited()
    assert await ledger_is_empty(ledger)
    expected = PublishOutcome.SENT if enabled else PublishOutcome.NOT_CONFIGURED
    assert (await publisher.publish([delivery()], MESSAGE)).outcome is expected


def delete_channel(fake: FakeDiscord) -> None:
    del fake.channels[CHANNEL_ID]


def replace_with_a_category(fake: FakeDiscord) -> None:
    fake.channels[CHANNEL_ID] = MagicMock(
        spec=discord.CategoryChannel, id=CHANNEL_ID, guild=fake.guild
    )


def move_to_another_guild(fake: FakeDiscord) -> None:
    fake.channel.guild = MagicMock(spec=discord.Guild, id=OTHER_GUILD_ID)


@pytest.mark.parametrize(
    'lose_channel', [delete_channel, replace_with_a_category, move_to_another_guild]
)
async def test_a_missing_channel_is_undeliverable_and_alerted_once(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
    lose_channel: Callable[[FakeDiscord], None],
) -> None:
    lose_channel(fake)

    first = await publisher.publish([delivery()], MESSAGE)
    second = await publisher.publish([delivery()], MESSAGE)

    expected = PublishResult(PublishOutcome.UNDELIVERABLE, reason='channel-missing')
    assert first == second == expected
    fake.channel.send.assert_not_awaited()
    assert await ledger_is_empty(ledger)
    (warning,) = logged(caplog, logging.WARNING)
    assert f"can't post workshops messages in channel {CHANNEL_ID}" in warning
    assert '(channel-missing)' in warning


async def test_a_post_waits_while_its_guild_is_unavailable(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Just after the bot reconnects, a guild's channels are unknown until
    # Discord sends the guild: that is no reason to alert admins.
    delete_channel(fake)
    fake.guild.unavailable = True

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(
        PublishOutcome.UNDELIVERABLE, reason='guild-unavailable'
    )
    fake.channel.send.assert_not_awaited()
    assert await ledger_is_empty(ledger)
    assert logged(caplog, logging.WARNING) == []


async def test_missing_permissions_are_undeliverable_and_alerted_once(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.channel.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True
    )

    first = await publisher.publish([delivery()], MESSAGE)
    second = await publisher.publish([delivery()], MESSAGE)

    reason = 'missing-permissions: embed_links, read_message_history'
    assert first == second == PublishResult(PublishOutcome.UNDELIVERABLE, reason=reason)
    fake.channel.permissions_for.assert_called_with(fake.guild.me)
    fake.channel.send.assert_not_awaited()
    assert await ledger_is_empty(ledger)
    (warning,) = logged(caplog, logging.WARNING)
    assert reason in warning


async def test_an_alert_is_repeated_after_the_alert_interval(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=PUBLISHER_LOGGER)
    delete_channel(fake)

    await publisher.publish([delivery()], MESSAGE)
    await clock.advance(timedelta(hours=23, minutes=59))
    await publisher.publish([delivery()], MESSAGE)

    assert len(logged(caplog, logging.WARNING)) == 1
    assert len(logged(caplog, logging.INFO)) == 1  # the repeat stays in the log file

    await clock.advance(timedelta(minutes=1))
    await publisher.publish([delivery()], MESSAGE)

    assert len(logged(caplog, logging.WARNING)) == 2


async def test_a_post_is_sent_once(
    publisher: DiscordPublisher, fake: FakeDiscord, ledger: DeliveryLedger
) -> None:
    result = await publisher.publish([delivery()], MESSAGE)

    (post,) = fake.posts
    assert result == PublishResult(PublishOutcome.SENT, message_id=post.id, keys=(KEY,))
    (embed,) = post.embeds
    assert embed.title == 'Graphs 101 starts in 1 hour'
    assert embed.footer.text == f'KCPC workshops · ref {marker_for(KEY)}'
    row = await record(ledger)
    assert (row.status, row.channel_id, row.message_id) == (SENT, CHANNEL_ID, post.id)

    again = await publisher.publish([delivery()], MESSAGE)

    assert again == PublishResult(PublishOutcome.ALREADY_HANDLED)
    assert len(fake.posts) == 1


async def test_a_group_with_a_known_key_is_sent_for_the_new_keys_only(
    publisher: DiscordPublisher, fake: FakeDiscord, ledger: DeliveryLedger
) -> None:
    await publisher.publish([delivery(KEY)], MESSAGE)

    result = await publisher.publish([delivery(KEY), delivery(OTHER_KEY)], MESSAGE)

    first, second = fake.posts
    assert result.outcome is PublishOutcome.SENT
    assert result.keys == (OTHER_KEY,)
    assert find_batch_marker(second) == marker_for(OTHER_KEY)
    assert (await record(ledger, KEY)).message_id == first.id
    assert (await record(ledger, OTHER_KEY)).message_id == second.id
    assert (
        await publisher.publish([delivery(KEY), delivery(OTHER_KEY)], MESSAGE)
    ).outcome is PublishOutcome.ALREADY_HANDLED


async def test_the_post_mentions_the_features_role_and_nobody_else(
    publisher: DiscordPublisher, fake: FakeDiscord
) -> None:
    await publisher.publish([delivery()], MESSAGE)

    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] == f'<@&{ROLE_ID}> See you there!'
    mentions = kwargs['allowed_mentions']
    assert (mentions.everyone, mentions.users, mentions.roles) == (
        False,
        False,
        [fake.role],
    )


async def test_a_message_without_text_is_just_the_mention(
    publisher: DiscordPublisher, fake: FakeDiscord
) -> None:
    await publisher.publish([delivery()], OutgoingMessage(title='Graphs 101'))

    assert sent_kwargs(fake.channel)['content'] == f'<@&{ROLE_ID}>'


async def test_a_message_can_opt_out_of_the_mention(
    publisher: DiscordPublisher, fake: FakeDiscord
) -> None:
    message = OutgoingMessage(title='Results', content='Well done!', mention_role=False)

    await publisher.publish([delivery()], message)

    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] == 'Well done!'
    assert kwargs['allowed_mentions'].roles is False
    fake.guild.get_role.assert_not_called()


async def test_a_feature_without_a_role_posts_without_a_mention(
    publisher: DiscordPublisher, fake: FakeDiscord, guild_settings: GuildSettingsRepo
) -> None:
    await guild_settings.update(GUILD_ID, FEATURE, role_id=None)

    await publisher.publish([delivery()], OutgoingMessage(title='Graphs 101'))

    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] is None
    assert kwargs['allowed_mentions'].roles is False


async def test_a_missing_role_is_alerted_and_the_post_goes_out_without_it(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await guild_settings.update(GUILD_ID, FEATURE, role_id=ROLE_ID + 1)

    result = await publisher.publish([delivery()], MESSAGE)

    assert result.outcome is PublishOutcome.SENT
    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] == 'See you there!'
    assert kwargs['allowed_mentions'].roles is False
    (warning,) = logged(caplog, logging.WARNING)
    assert f'role {ROLE_ID + 1} no longer exists in guild {GUILD_ID}' in warning


async def test_a_role_discord_would_not_notify_is_alerted_and_still_mentioned(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Not mentionable, and the bot lacks Mention Everyone in the channel (a
    # channel overwrite can deny it even where the server grants it).
    fake.role.mentionable = False

    result = await publisher.publish([delivery()], MESSAGE)
    await publisher.publish([delivery(OTHER_KEY)], MESSAGE)

    assert result.outcome is PublishOutcome.SENT
    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] == f'<@&{ROLE_ID}> See you there!'
    assert kwargs['allowed_mentions'].roles == [fake.role]
    (warning,) = logged(caplog, logging.WARNING)  # the second post is not alerted
    assert f'mention role {ROLE_ID}, but Discord notifies nobody' in warning


async def test_a_role_the_bot_may_ping_in_the_channel_is_not_alerted(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.role.mentionable = False
    fake.channel.permissions_for.return_value = POST_PERMISSIONS | discord.Permissions(
        mention_everyone=True
    )

    assert (await publisher.publish([delivery()], MESSAGE)).outcome is (
        PublishOutcome.SENT
    )

    assert logged(caplog, logging.WARNING) == []
    fake.channel.permissions_for.assert_called_with(fake.guild.me)


@pytest.mark.parametrize(
    ('mentionable', 'mention_everyone', 'pingable'),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_can_ping_role(
    fake: FakeDiscord, mentionable: bool, mention_everyone: bool, pingable: bool
) -> None:
    role = fake.add_role(NEW_ROLE_ID, mentionable=mentionable)
    permissions = discord.Permissions(mention_everyone=mention_everyone)
    channel = fake.add_channel(
        discord.TextChannel, OTHER_CHANNEL_ID, permissions=permissions
    )

    assert can_ping_role(channel, role) is pingable


@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        (http_error(discord.Forbidden, 403, 'Missing Permissions'), 'discord-403'),
        (http_error(discord.NotFound, 404, 'Unknown Channel'), 'discord-404'),
    ],
)
async def test_a_post_discord_refuses_is_skipped_and_alerted(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
    error: discord.HTTPException,
    reason: str,
) -> None:
    fake.channel.send.side_effect = error

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(PublishOutcome.SKIPPED, reason=reason, keys=(KEY,))
    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, reason)
    (warning,) = logged(caplog, logging.WARNING)
    assert str(error) in warning


async def test_a_post_discord_rejects_as_invalid_is_skipped_and_logged_as_a_bug(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake.channel.send.side_effect = http_error(
        discord.HTTPException, 400, {'code': 50035, 'message': 'Invalid Form Body'}
    )

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(
        PublishOutcome.SKIPPED, reason='rejected-400', keys=(KEY,)
    )
    assert (await record(ledger)).reason == 'rejected-400'
    (error,) = logged(caplog, logging.ERROR)
    assert 'Invalid Form Body' in error


@pytest.mark.parametrize(
    ('error', 'reason'),
    [
        (http_error(discord.DiscordServerError, 500), 'discord-500'),
        (http_error(discord.DiscordServerError, 503), 'discord-503'),
        # Discord only asked us to slow down, so the post can go out later.
        (http_error(discord.HTTPException, 429), 'discord-429'),
        (asyncio.TimeoutError(), 'timeout'),
        (aiohttp.ServerTimeoutError(), 'timeout'),
        (aiohttp.ServerDisconnectedError(), 'network'),
        (ConnectionResetError(10054, 'Connection reset by peer'), 'network'),
    ],
)
async def test_a_post_with_an_unknown_outcome_stays_claimed(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    reason: str,
) -> None:
    fake.channel.send.side_effect = error

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(PublishOutcome.PENDING, reason=reason, keys=(KEY,))
    assert (await record(ledger)).status is CLAIMED
    (warning,) = logged(caplog, logging.WARNING)
    assert 'The reconciler will check whether it arrived' in warning


async def test_a_post_that_cannot_be_built_frees_the_claim_and_propagates(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bug stopped the post before anything reached Discord, so nothing is
    # lost by letting the deliveries be claimed again.
    with monkeypatch.context() as patched:
        patched.setattr(
            'tle.kcpc.bot.publisher.to_embed',
            MagicMock(side_effect=RuntimeError('bug')),
        )
        with pytest.raises(RuntimeError, match='bug'):
            await publisher.publish([delivery()], MESSAGE)

    assert await ledger.get(KEY) is None
    fake.channel.send.assert_not_awaited()
    assert (await publisher.publish([delivery()], MESSAGE)).outcome is (
        PublishOutcome.SENT
    )


@pytest.mark.parametrize(
    'error',
    [
        # What discord.py raises when it can't read the answer to a send that
        # Discord accepted: a body without 'type', one decoded as text, one
        # that isn't JSON; and any other bug inside the send.
        KeyError('type'),
        TypeError('string indices must be integers'),
        ValueError('Expecting value: line 1 column 1 (char 0)'),
        RuntimeError('bug'),
    ],
)
@pytest.mark.parametrize(
    ('created', 'report'),
    [(True, ReconcileReport(confirmed=1)), (False, ReconcileReport(resent=1))],
    ids=['created', 'not created'],
)
async def test_an_unexpected_error_from_the_send_is_left_to_the_reconciler(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    created: bool,
    report: ReconcileReport,
) -> None:
    fake.channel.send.side_effect = failing_send(fake, error, created=created)

    result = await publisher.publish([delivery()], MESSAGE)

    assert result == PublishResult(
        PublishOutcome.PENDING, reason='unexpected-error', keys=(KEY,)
    )
    assert (await record(ledger)).status is CLAIMED
    (logged_error,) = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert 'The reconciler will check whether it arrived' in logged_error.getMessage()
    assert logged_error.exc_info is not None and logged_error.exc_info[1] is error
    # The post may well be out, so it is never sent again by the next publish,
    posts = len(fake.posts)
    again = await publisher.publish([delivery()], MESSAGE)
    assert again == PublishResult(PublishOutcome.ALREADY_HANDLED)
    assert len(fake.posts) == posts
    # and the reconciler confirms it, or sends it if it never arrived.
    fake.channel.send.side_effect = fake.post
    await clock.advance(STALE)
    assert await publisher.reconcile() == report
    (post,) = fake.posts
    assert (await record(ledger)).message_id == post.id


async def test_an_unexpected_error_from_a_resend_is_left_to_the_next_run(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    fake.channel.send.side_effect = failing_send(fake, KeyError('type'), created=True)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(pending=1)

    assert (await record(ledger)).status is CLAIMED
    fake.channel.send.side_effect = fake.post
    assert await publisher.reconcile() == ReconcileReport(confirmed=1)
    (post,) = fake.posts
    assert (await record(ledger)).message_id == post.id


async def test_a_failed_confirm_leaves_a_sent_post_to_the_reconciler(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confirm = ledger.confirm
    failures = [sqlite3.OperationalError('database is locked')]

    async def confirm_failing_once(batch: str, message_id: int) -> int:
        if failures:
            raise failures.pop()
        return await confirm(batch, message_id)

    monkeypatch.setattr(ledger, 'confirm', confirm_failing_once)

    with pytest.raises(sqlite3.OperationalError):
        await publisher.publish([delivery()], MESSAGE)

    # The post went out, so its claim must stand: freeing it would let the
    # next publish send it again.
    (post,) = fake.posts
    assert (await record(ledger)).status is CLAIMED
    again = await publisher.publish([delivery()], MESSAGE)
    assert again.outcome is PublishOutcome.ALREADY_HANDLED
    fake.channel.send.assert_awaited_once()
    await clock.advance(STALE)
    assert await publisher.reconcile() == ReconcileReport(confirmed=1)
    assert (await record(ledger)).message_id == post.id
    assert len(fake.posts) == 1


async def test_a_cancelled_send_is_left_to_the_reconciler(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    started = asyncio.Event()

    async def hang(**kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    fake.channel.send.side_effect = hang
    task = asyncio.create_task(publisher.publish([delivery()], MESSAGE))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await record(ledger)).status is CLAIMED
    fake.channel.send.side_effect = fake.post
    await clock.advance(STALE)
    assert await publisher.reconcile() == ReconcileReport(resent=1)


@pytest.mark.parametrize(
    'deliveries',
    [
        [],
        [delivery(KEY), delivery(OTHER_KEY, guild_id=OTHER_GUILD_ID)],
        [delivery(KEY), delivery(OTHER_KEY, feature='contests')],
    ],
    ids=['none', 'two guilds', 'two features'],
)
async def test_a_post_is_for_one_guild_and_feature(
    publisher: DiscordPublisher, fake: FakeDiscord, deliveries: list[Delivery]
) -> None:
    with pytest.raises(ValueError, match='exactly one guild and feature'):
        await publisher.publish(deliveries, MESSAGE)

    fake.channel.send.assert_not_awaited()


@pytest.mark.parametrize(
    ('kind', 'permissions'),
    [
        (discord.Thread, THREAD_POST_PERMISSIONS),
        (discord.VoiceChannel, POST_PERMISSIONS),
    ],
)
async def test_posts_can_go_to_threads_and_voice_channel_chats(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    kind: type[discord.abc.Messageable],
    permissions: discord.Permissions,
) -> None:
    channel = fake.add_channel(kind, OTHER_CHANNEL_ID, permissions=permissions)
    await guild_settings.update(GUILD_ID, FEATURE, channel_id=OTHER_CHANNEL_ID)

    result = await publisher.publish([delivery()], MESSAGE)

    assert result.outcome is PublishOutcome.SENT
    channel.send.assert_awaited_once()


@pytest.mark.parametrize(
    ('kind', 'permissions', 'missing'),
    [
        (discord.TextChannel, POST_PERMISSIONS, []),
        (discord.TextChannel, THREAD_POST_PERMISSIONS, ['send_messages']),
        (discord.Thread, THREAD_POST_PERMISSIONS, []),
        (discord.Thread, POST_PERMISSIONS, ['send_messages_in_threads']),
        (
            discord.VoiceChannel,
            discord.Permissions.none(),
            ['view_channel', 'send_messages', 'embed_links', 'read_message_history'],
        ),
    ],
)
def test_missing_post_permissions(
    fake: FakeDiscord,
    kind: type[discord.abc.Messageable],
    permissions: discord.Permissions,
    missing: list[str],
) -> None:
    channel = fake.add_channel(kind, OTHER_CHANNEL_ID, permissions=permissions)

    assert missing_post_permissions(channel) == missing


async def test_reconcile_confirms_a_post_that_arrived(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=True)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(confirmed=1)

    (post,) = fake.posts
    row = await record(ledger)
    assert (row.status, row.message_id) == (SENT, post.id)
    assert fake.channel.send.await_count == 1


async def test_reconcile_searches_everything_since_shortly_before_the_claim(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    claimed_at = clock.now()
    await publish_pending(publisher, fake, delivery(), created=True)
    await clock.advance(STALE)

    await publisher.reconcile()

    kwargs = fake.channel.history.call_args.kwargs
    # Newest first, from 5 minutes before the claim (in case the bot's clock
    # runs ahead of Discord's), up to 1000 messages.
    assert kwargs['oldest_first'] is False
    assert kwargs['limit'] == 1000
    expected_after = discord.utils.time_snowflake(claimed_at - timedelta(minutes=5))
    assert kwargs['after'].id == expected_after


async def test_reconcile_finds_a_resend_long_after_the_claim(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    await fake.chatter(60)
    await clock.advance(timedelta(hours=3))  # the bot was away meanwhile
    # The resend reaches Discord, but its answer doesn't reach the bot.
    fake.channel.send.side_effect = failing_send(
        fake, asyncio.TimeoutError(), created=True
    )
    assert await publisher.reconcile() == ReconcileReport(pending=1)
    fake.channel.send.side_effect = fake.post
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(confirmed=1)

    (post,) = [post for post in fake.posts if post.author.id == BOT_USER_ID]
    assert (await record(ledger)).message_id == post.id


async def test_reconcile_finds_a_post_after_a_burst_of_chat_before_the_claim(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await fake.chatter(60)
    await clock.advance(timedelta(seconds=55))  # the burst came just before
    await publish_pending(
        publisher,
        fake,
        delivery(),
        created=True,
        error=http_error(discord.DiscordServerError, 503),
    )
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(confirmed=1)

    (post,) = [post for post in fake.posts if post.author.id == BOT_USER_ID]
    assert (await record(ledger)).message_id == post.id


async def test_reconcile_skips_a_post_it_cannot_search_for_in_full(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Too many messages since the claim to read them all, so the post could be
    # among those not read: sending it again could post it twice.
    await publish_pending(publisher, fake, delivery(), created=False)
    await fake.chatter(1000)
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(skipped=1)

    fake.channel.send.assert_awaited_once()  # the first, failed, send
    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, 'history-too-long')
    (warning,) = logged(caplog, logging.WARNING)
    assert 'rather than risk posting it twice' in warning


async def test_reconcile_resends_once_it_has_read_every_message_since_the_claim(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    await fake.chatter(999)  # one fewer than the most it reads
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(resent=1)


async def test_every_send_of_a_batch_carries_its_own_nonce(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    # Discord answers a repeated nonce with the message it already created, so
    # a resend that repeats a post Discord did create can't post it twice.
    await publish_pending(publisher, fake, delivery(), created=False)
    nonce = sent_kwargs(fake.channel)['nonce']
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(resent=1)
    assert sent_kwargs(fake.channel)['nonce'] == nonce

    await publisher.publish([delivery(OTHER_KEY)], MESSAGE)
    assert sent_kwargs(fake.channel)['nonce'] != nonce
    assert isinstance(nonce, str) and len(nonce) <= 25  # Discord's limit


async def test_reconcile_resends_a_post_that_never_arrived_once(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    reminder = delivery(expires_at=clock.now() + timedelta(hours=1))
    await publish_pending(publisher, fake, reminder, created=False)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(resent=1)

    (post,) = fake.posts
    assert find_batch_marker(post) == marker_for(KEY)
    row = await record(ledger)
    assert (row.status, row.message_id) == (SENT, post.id)
    # The reminder is never sent twice: nothing is left to reconcile, and
    # publishing it again is a no-op.
    await clock.advance(STALE)
    assert await publisher.reconcile() == ReconcileReport()
    assert (await publisher.publish([reminder], MESSAGE)).outcome is (
        PublishOutcome.ALREADY_HANDLED
    )
    assert len(fake.posts) == 1


async def test_reconcile_resends_with_the_features_current_role(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    new_role = fake.add_role(NEW_ROLE_ID)
    await guild_settings.update(GUILD_ID, FEATURE, role_id=NEW_ROLE_ID)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(resent=1)

    kwargs = sent_kwargs(fake.channel)
    assert kwargs['content'] == f'<@&{NEW_ROLE_ID}> See you there!'
    assert kwargs['allowed_mentions'].roles == [new_role]


async def test_reconcile_alerts_a_resend_whose_role_discord_would_not_notify(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await publish_pending(
        publisher,
        fake,
        delivery(),
        created=False,
        error=http_error(discord.DiscordServerError, 500),
    )
    fake.role.mentionable = False  # a moderator turned it off meanwhile
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(resent=1)

    (warning,) = logged(caplog, logging.WARNING)
    assert f'mention role {ROLE_ID}, but Discord notifies nobody' in warning


async def test_reconcile_ignores_the_marker_in_someone_elses_message(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    # Someone else's message quoting the post's embed, marker and all.
    await fake.post(
        embed=to_embed(MESSAGE, batch=marker_for(KEY)), author_id=BOT_USER_ID + 1
    )
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(resent=1)


@pytest.mark.parametrize(
    'expires_in', [timedelta(minutes=1), STALE], ids=['earlier', 'at reconcile']
)
async def test_reconcile_gives_up_an_expired_post_as_lost(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    expires_in: timedelta,
) -> None:
    reminder = delivery(expires_at=clock.now() + expires_in)
    await publish_pending(publisher, fake, reminder, created=False)
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(lost=1)

    assert fake.posts == []
    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, 'lost')
    (warning,) = logged(caplog, logging.WARNING)
    assert 'never arrived' in warning and 'it expired' in warning


async def test_reconcile_gives_up_a_post_whose_message_was_not_stored(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    db: Database,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    await db.execute('UPDATE delivery_log SET payload = NULL')
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(lost=1)

    assert fake.posts == []
    assert (await record(ledger)).reason == 'lost'


async def test_reconcile_skips_a_post_whose_channel_is_gone(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    delete_channel(fake)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(skipped=1)

    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, 'channel-missing')


async def test_reconcile_skips_a_post_in_a_guild_the_bot_left(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    delete_channel(fake)
    fake.guilds.clear()
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(skipped=1)

    assert (await record(ledger)).reason == 'channel-missing'


async def test_reconcile_waits_while_the_guild_is_unavailable(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # After the bot reconnects, discord.py knows none of a guild's channels
    # until Discord sends the guild: they are unknown, not gone.
    await publish_pending(publisher, fake, delivery(), created=False)
    delete_channel(fake)
    fake.guild.unavailable = True
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(pending=1)

    assert (await record(ledger)).status is CLAIMED
    assert logged(caplog, logging.WARNING) == []
    fake.channels[CHANNEL_ID] = fake.channel
    fake.guild.unavailable = False
    assert await publisher.reconcile() == ReconcileReport(resent=1)
    assert len(fake.posts) == 1


@pytest.mark.parametrize(
    ('created', 'report'),
    [(True, ReconcileReport(confirmed=1)), (False, ReconcileReport(resent=1))],
    ids=['created', 'not created'],
)
async def test_reconcile_settles_a_post_in_the_channel_it_was_claimed_for(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    guild_settings: GuildSettingsRepo,
    clock: FakeClock,
    created: bool,
    report: ReconcileReport,
) -> None:
    # An admin moves the feature to another channel after a send timed out.
    other = fake.add_channel(discord.TextChannel, OTHER_CHANNEL_ID)
    await publish_pending(publisher, fake, delivery(), created=created)
    await guild_settings.update(GUILD_ID, FEATURE, channel_id=OTHER_CHANNEL_ID)
    await clock.advance(STALE)

    assert await publisher.reconcile() == report

    (post,) = fake.posts
    assert post.channel_id == CHANNEL_ID
    other.history.assert_not_called()
    other.send.assert_not_awaited()


async def test_reconcile_skips_a_post_it_cannot_verify(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    fake.history_errors.append(http_error(discord.Forbidden, 403, 'Missing Access'))
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(skipped=1)

    assert fake.posts == []
    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, 'unverifiable')
    (warning,) = logged(caplog, logging.WARNING)
    assert 'Read Message History' in warning


@pytest.mark.parametrize(
    'error',
    [
        http_error(discord.DiscordServerError, 503),
        asyncio.TimeoutError(),
        aiohttp.ClientConnectionError(),
    ],
)
async def test_reconcile_retries_a_search_that_failed(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
    error: Exception,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    fake.history_errors.append(error)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(pending=1)
    assert (await record(ledger)).status is CLAIMED
    assert await publisher.reconcile() == ReconcileReport(resent=1)


async def test_reconcile_skips_a_resend_that_discord_refuses(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    fake.channel.send.side_effect = http_error(discord.Forbidden, 403)
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport(skipped=1)

    row = await record(ledger)
    assert (row.status, row.reason) == (SKIPPED, 'discord-403')


async def test_reconcile_leaves_a_recent_claim_alone(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    ledger: DeliveryLedger,
    clock: FakeClock,
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    await clock.advance(STALE_AFTER)

    assert await publisher.reconcile() == ReconcileReport()

    fake.channel.history.assert_not_called()
    assert (await record(ledger)).status is CLAIMED
    await clock.advance(timedelta(seconds=1))
    assert await publisher.reconcile() == ReconcileReport(resent=1)


async def test_reconcile_leaves_a_batch_that_is_being_sent_alone(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    # A send can outlast stale_after while discord.py retries a 5xx; resending
    # the batch meanwhile would post it twice.
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_send(**kwargs: Any) -> MagicMock:
        started.set()
        await finish.wait()
        return await fake.post(**kwargs)

    fake.channel.send.side_effect = slow_send
    task = asyncio.create_task(publisher.publish([delivery()], MESSAGE))
    await asyncio.wait_for(started.wait(), timeout=5)
    await clock.advance(STALE)

    try:
        # Bounded, because a resend would wait for this same slow send.
        report = await asyncio.wait_for(publisher.reconcile(), timeout=5)
    finally:
        finish.set()

    assert report == ReconcileReport(pending=1)
    fake.channel.history.assert_not_called()
    assert (await task).outcome is PublishOutcome.SENT
    assert len(fake.posts) == 1


async def test_reconcile_carries_on_after_an_error_in_one_batch(
    publisher: DiscordPublisher,
    fake: FakeDiscord,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await publish_pending(publisher, fake, delivery(KEY), created=False)
    await publish_pending(publisher, fake, delivery(OTHER_KEY), created=False)
    fake.history_errors.append(RuntimeError('bug'))
    await clock.advance(STALE)
    caplog.clear()

    assert await publisher.reconcile() == ReconcileReport(resent=1, failed=1)

    (error,) = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert error.getMessage().startswith('Could not reconcile KCPC batch')
    assert error.exc_info is not None
    # The failed batch is still claimed, so the next run settles it.
    assert await publisher.reconcile() == ReconcileReport(resent=1)
    assert len(fake.posts) == 2


async def test_reconcile_waits_until_the_bot_is_logged_in(
    publisher: DiscordPublisher, fake: FakeDiscord, clock: FakeClock
) -> None:
    await publish_pending(publisher, fake, delivery(), created=False)
    fake.bot.user = None
    await clock.advance(STALE)

    assert await publisher.reconcile() == ReconcileReport()

    fake.channel.history.assert_not_called()


@dataclass(frozen=True)
class Run:
    """One run of the bot's publishing side, on a kcpc.db file."""

    db: Database
    settings: GuildSettingsRepo
    ledger: DeliveryLedger
    publisher: DiscordPublisher

    @classmethod
    async def start(cls, path: Path, fake: FakeDiscord, clock: FakeClock) -> 'Run':
        db = await open_database(path)
        settings = GuildSettingsRepo(db, clock, default_registry())
        ledger = DeliveryLedger(db, clock)
        publisher = DiscordPublisher(fake.bot, settings, ledger, clock)
        return cls(db, settings, ledger, publisher)

    async def crash_while_sending(
        self, fake: FakeDiscord, item: Delivery, *, reached_discord: bool
    ) -> None:
        """Publish ``item``, and crash while Discord has the send in hand.

        With ``reached_discord``, Discord has created the message by then. The
        publish stops dead and the database connection goes, so only what the
        ledger committed, and what Discord received, outlive the run.
        """
        in_flight = asyncio.Event()

        async def send_then_crash(**kwargs: Any) -> None:
            if reached_discord:
                await fake.post(**kwargs)
            in_flight.set()
            await asyncio.Event().wait()  # the crash comes before any answer

        fake.channel.send.side_effect = send_then_crash
        publishing = asyncio.create_task(self.publisher.publish([item], MESSAGE))
        await asyncio.wait_for(in_flight.wait(), timeout=5)
        publishing.cancel()
        await asyncio.wait({publishing})
        await self.db.close()
        fake.channel.send.side_effect = fake.post


@pytest.mark.parametrize(
    ('reached_discord', 'expires_in', 'report', 'settled', 'posts'),
    [
        (True, timedelta(hours=1), ReconcileReport(confirmed=1), (SENT, None), 1),
        (False, timedelta(hours=1), ReconcileReport(resent=1), (SENT, None), 1),
        (False, timedelta(minutes=5), ReconcileReport(lost=1), (SKIPPED, 'lost'), 0),
    ],
    ids=['it reached Discord', 'it did not', 'it did not, and it is too late now'],
)
async def test_a_post_cut_off_by_a_crash_is_settled_once_the_bot_restarts(
    tmp_path: Path,
    fake: FakeDiscord,
    clock: FakeClock,
    reached_discord: bool,
    expires_in: timedelta,
    report: ReconcileReport,
    settled: tuple[DeliveryStatus, str | None],
    posts: int,
) -> None:
    path = tmp_path / 'kcpc.db'
    reminder = delivery(expires_at=clock.now() + expires_in)
    crashed = await Run.start(path, fake, clock)
    await crashed.settings.update(
        GUILD_ID, FEATURE, enabled=True, channel_id=CHANNEL_ID, role_id=ROLE_ID
    )
    await crashed.crash_while_sending(fake, reminder, reached_discord=reached_discord)

    await clock.advance(timedelta(minutes=10))  # the bot is down for a while
    restarted = await Run.start(path, fake, clock)

    assert await restarted.publisher.reconcile() == report
    row = await record(restarted.ledger)
    assert (row.status, row.reason) == settled
    assert len(fake.posts) == posts
    if posts:
        assert row.message_id == fake.posts[0].id
    # Nothing more is posted: the claim is settled for good.
    await clock.advance(STALE)
    assert await restarted.publisher.reconcile() == ReconcileReport()
    assert (await restarted.publisher.publish([reminder], MESSAGE)).outcome is (
        PublishOutcome.ALREADY_HANDLED
    )
    assert len(fake.posts) == posts
