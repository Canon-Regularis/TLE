"""Members' linked accounts, the challenges that prove them, and rating snapshots.

A linked account is a member's handle on one platform, in one guild, once they
have proved that it is theirs. A member links one handle per platform, and a
handle is linked to one member of a guild at most. Codeforces handles stay in
TLE's own table (see ``tle.kcpc.bot.codeforces_links``), so for now the links
stored here are AtCoder ones. A challenge is the token that a member must show
on their profile to prove a link, kept until it is checked or expires.
Snapshots hold each handle's ratings as last fetched, on either platform, for
``/profile`` and ``/rank``: one per handle, however many guilds link it.

Handles compare case-insensitively, as SQLite's NOCASE does (ASCII letters
only), so that nobody can claim a member's handle by typing it in another case.
Guild and user IDs are stored as text, and times as whole seconds.

Each write runs in a transaction of its own, or joins the caller's, so a
service can group several in ``AccountRepo.transaction``.
"""

import string
from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from tle.kcpc.core.db import Database, Row
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import ensure_utc, from_epoch, to_epoch

# How a member proved a link: a token in their profile's Affiliation field.
AFFILIATION_TOKEN = 'affiliation-token'

# The most handles that ``snapshots`` puts in one query. SQLite before 3.32
# allows at most 999 parameters in a statement.
_SNAPSHOT_CHUNK = 500

# What SQLite's NOCASE folds: the 26 ASCII capitals, and nothing else.
_NOCASE = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

# Who can free a handle that someone else in the server has, on each platform:
# admins unlink AtCoder accounts, which are KCPC's, and TLE's moderators remove
# Codeforces handles, which are TLE's (TLE keeps those of members who left).
_FREEING_A_HANDLE = {
    'atcoder': 'If it is yours, ask an admin to unlink it with /kcpc accounts unlink.',
    'codeforces': (
        'If it is yours, ask an Admin or Moderator to remove it with /handle remove.'
    ),
}


class HandleTaken(KcpcUserError):
    """The handle is linked to someone else in the guild, who may have left it.

    The message names nobody, so it can be shown to whoever tried to link it,
    and says who can free the handle: ``freeing`` if given, else who can on
    the platform.
    """

    def __init__(
        self, platform: str, handle: str, *, freeing: str | None = None
    ) -> None:
        taken = f'The handle {handle} is already linked to someone else in this server.'
        if freeing is None:
            freeing = _FREEING_A_HANDLE.get(platform)
        super().__init__(taken if freeing is None else f'{taken} {freeing}')
        self.platform = platform
        self.handle = handle


@dataclass(frozen=True)
class LinkedAccount:
    """A row of ``linked_account``: a member's proved handle on one platform.

    ``verified_at`` is converted to UTC in whole seconds, as it is stored, so
    that a link built by hand equals the one read back.
    """

    guild_id: int
    user_id: int
    platform: str
    handle: str  # in the case the platform gives it
    method: str  # how the member proved it, e.g. AFFILIATION_TOKEN
    verified_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'verified_at', _whole_seconds(self.verified_at))


@dataclass(frozen=True)
class LinkChallenge:
    """A row of ``link_challenge``: what a member must do to prove a link.

    Its times are converted to UTC in whole seconds, as they are stored.
    """

    guild_id: int
    user_id: int
    platform: str
    handle: str
    token: str  # to be shown on the handle's profile
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'created_at', _whole_seconds(self.created_at))
        object.__setattr__(self, 'expires_at', _whole_seconds(self.expires_at))

    def expired(self, now: datetime) -> bool:
        """Whether it has expired at ``now``, as ``purge_expired`` decides."""
        return now >= self.expires_at


@dataclass(frozen=True)
class AccountSnapshot:
    """A row of ``account_snapshot``: a handle's ratings, as last fetched.

    ``fetched_at`` is converted to UTC in whole seconds, as it is stored.
    """

    platform: str
    handle: str  # in the case the platform gives it
    rating: int | None  # None if never rated
    max_rating: int | None
    rank: str | None  # the platform's name for its rating band, e.g. 'expert'
    rated_matches: int | None  # None if the platform does not say
    fetched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'fetched_at', _whole_seconds(self.fetched_at))


_SELECT_LINKS = """
    SELECT guild_id, user_id, platform, handle, method, verified_at
    FROM linked_account
"""
_OWNER = """
    SELECT user_id FROM linked_account
    WHERE guild_id = ? AND platform = ? AND handle = ?
"""
_LINK = """
    INSERT INTO linked_account (
        guild_id, user_id, platform, handle, method, verified_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (guild_id, user_id, platform) DO UPDATE SET
        handle = excluded.handle,
        method = excluded.method,
        verified_at = excluded.verified_at
"""
_UNLINK = """
    DELETE FROM linked_account WHERE guild_id = ? AND user_id = ? AND platform = ?
"""

_SELECT_CHALLENGE = """
    SELECT guild_id, user_id, platform, handle, token, created_at, expires_at
    FROM link_challenge WHERE guild_id = ? AND user_id = ? AND platform = ?
"""
_SAVE_CHALLENGE = """
    INSERT INTO link_challenge (
        guild_id, user_id, platform, handle, token, created_at, expires_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (guild_id, user_id, platform) DO UPDATE SET
        handle = excluded.handle,
        token = excluded.token,
        created_at = excluded.created_at,
        expires_at = excluded.expires_at
"""
_DELETE_CHALLENGE = """
    DELETE FROM link_challenge WHERE guild_id = ? AND user_id = ? AND platform = ?
"""

_SELECT_SNAPSHOTS = """
    SELECT platform, handle, rating, max_rating, rank, rated_matches, fetched_at
    FROM account_snapshot WHERE platform = ?
"""
# The handle takes the case of the latest snapshot, as the platform gave it.
_SAVE_SNAPSHOT = """
    INSERT INTO account_snapshot (
        platform, handle, rating, max_rating, rank, rated_matches, fetched_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (platform, handle) DO UPDATE SET
        handle = excluded.handle,
        rating = excluded.rating,
        max_rating = excluded.max_rating,
        rank = excluded.rank,
        rated_matches = excluded.rated_matches,
        fetched_at = excluded.fetched_at
    WHERE excluded.fetched_at >= account_snapshot.fetched_at
"""


class AccountRepo:
    """Reads and writes the accounts tables: links, challenges and snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run the block in one transaction, which the repo's methods join.

        The writes made in the block are committed together, or not at all.
        As with ``Database.transaction``, keep network calls out of it.
        """
        async with self._db.transaction():
            yield

    async def link(self, account: LinkedAccount) -> None:
        """Store the member's link on its platform, replacing any they had.

        Raises ``HandleTaken``, and stores nothing, if another member of the
        guild has linked the handle, in whatever case.
        """
        async with self._db.transaction():
            owner = await self.owner_of(
                account.guild_id, account.platform, account.handle
            )
            if owner is not None and owner != account.user_id:
                raise HandleTaken(account.platform, account.handle)
            await self._db.execute(
                _LINK,
                (
                    *_member_key(account.guild_id, account.user_id, account.platform),
                    account.handle,
                    account.method,
                    to_epoch(account.verified_at),
                ),
            )

    async def unlink(
        self, guild_id: int, user_id: int, platform: str
    ) -> LinkedAccount | None:
        """Remove the member's link on ``platform`` and return it; None if none."""
        async with self._db.transaction():
            account = await self.get_link(guild_id, user_id, platform)
            if account is not None:
                await self._db.execute(
                    _UNLINK, _member_key(guild_id, user_id, platform)
                )
        return account

    async def unlink_handle(
        self, guild_id: int, platform: str, handle: str
    ) -> LinkedAccount | None:
        """Remove the link of ``handle``, whoever made it, and return it; None if none.

        The handle matches in whatever case.
        """
        async with self._db.transaction():
            row = await self._db.fetchone(
                f'{_SELECT_LINKS} WHERE guild_id = ? AND platform = ? AND handle = ?',
                (str(guild_id), platform, handle),
            )
            account = None if row is None else _linked_account(row)
            if account is not None:
                await self._db.execute(
                    _UNLINK, _member_key(guild_id, account.user_id, platform)
                )
        return account

    async def get_link(
        self, guild_id: int, user_id: int, platform: str
    ) -> LinkedAccount | None:
        """The member's link on ``platform``, or None."""
        row = await self._db.fetchone(
            f'{_SELECT_LINKS} WHERE guild_id = ? AND user_id = ? AND platform = ?',
            _member_key(guild_id, user_id, platform),
        )
        return None if row is None else _linked_account(row)

    async def links_for_user(self, guild_id: int, user_id: int) -> list[LinkedAccount]:
        """The member's links in the guild, by platform."""
        rows = await self._db.fetchall(
            f'{_SELECT_LINKS} WHERE guild_id = ? AND user_id = ? ORDER BY platform',
            (str(guild_id), str(user_id)),
        )
        return [_linked_account(row) for row in rows]

    async def links_for_guild(
        self, guild_id: int, platform: str
    ) -> list[LinkedAccount]:
        """Every link on ``platform`` in the guild, by handle, ignoring case."""
        rows = await self._db.fetchall(
            f'{_SELECT_LINKS} WHERE guild_id = ? AND platform = ? ORDER BY handle',
            (str(guild_id), platform),
        )
        return [_linked_account(row) for row in rows]

    async def owner_of(self, guild_id: int, platform: str, handle: str) -> int | None:
        """The member of the guild who linked ``handle``, in whatever case, or None."""
        user_id = await self._db.fetchval(_OWNER, (str(guild_id), platform, handle))
        return None if user_id is None else int(user_id)

    async def save_challenge(self, challenge: LinkChallenge) -> None:
        """Store the challenge, replacing the member's last one on its platform."""
        await self._db.execute(
            _SAVE_CHALLENGE,
            (
                *_member_key(challenge.guild_id, challenge.user_id, challenge.platform),
                challenge.handle,
                challenge.token,
                to_epoch(challenge.created_at),
                to_epoch(challenge.expires_at),
            ),
        )

    async def get_challenge(
        self, guild_id: int, user_id: int, platform: str
    ) -> LinkChallenge | None:
        """The member's challenge on ``platform``, expired or not, or None.

        ``LinkChallenge.expired`` tells whether it still holds.
        """
        row = await self._db.fetchone(
            _SELECT_CHALLENGE, _member_key(guild_id, user_id, platform)
        )
        if row is None:
            return None
        return LinkChallenge(
            guild_id=int(row['guild_id']),
            user_id=int(row['user_id']),
            platform=row['platform'],
            handle=row['handle'],
            token=row['token'],
            created_at=from_epoch(row['created_at']),
            expires_at=from_epoch(row['expires_at']),
        )

    async def delete_challenge(
        self, guild_id: int, user_id: int, platform: str
    ) -> None:
        """Delete the member's challenge on ``platform``, if there is one."""
        await self._db.execute(
            _DELETE_CHALLENGE, _member_key(guild_id, user_id, platform)
        )

    async def purge_expired(self, now: datetime) -> int:
        """Delete every challenge expired at ``now``; returns how many there were."""
        deleted = await self._db.execute(
            'DELETE FROM link_challenge WHERE expires_at <= ?', (to_epoch(now),)
        )
        return deleted.rowcount

    async def save_snapshots(self, snapshots: Sequence[AccountSnapshot]) -> None:
        """Store the snapshots, each replacing its handle's on its platform.

        A snapshot fetched before the stored one is ignored, so that a fetch
        that finishes late never overwrites a newer one.
        """
        await self._db.executemany(
            _SAVE_SNAPSHOT,
            [
                (
                    snapshot.platform,
                    snapshot.handle,
                    snapshot.rating,
                    snapshot.max_rating,
                    snapshot.rank,
                    snapshot.rated_matches,
                    to_epoch(snapshot.fetched_at),
                )
                for snapshot in snapshots
            ],
        )

    async def snapshots(
        self, platform: str, handles: Collection[str]
    ) -> dict[str, AccountSnapshot]:
        """The snapshots of ``handles`` on ``platform``, by handle as given.

        Handles match in whatever case. Those without a snapshot are left out.
        """
        # A lone handle is a collection of strings too, its letters, which
        # would quietly look up the wrong handles.
        if isinstance(handles, str):
            raise TypeError(f'Expected a collection of handles, got {handles!r}')
        wanted = list(dict.fromkeys(handles))
        stored: dict[str, AccountSnapshot] = {}
        for first in range(0, len(wanted), _SNAPSHOT_CHUNK):
            chunk = wanted[first : first + _SNAPSHOT_CHUNK]
            marks = ', '.join('?' * len(chunk))
            rows = await self._db.fetchall(
                f'{_SELECT_SNAPSHOTS} AND handle IN ({marks})', (platform, *chunk)
            )
            stored.update((_folded(row['handle']), _snapshot(row)) for row in rows)
        found: dict[str, AccountSnapshot] = {}
        for handle in wanted:
            snapshot = stored.get(_folded(handle))
            if snapshot is not None:
                found[handle] = snapshot
        return found

    async def snapshot(self, platform: str, handle: str) -> AccountSnapshot | None:
        """The snapshot of ``handle`` on ``platform``, in whatever case, or None."""
        row = await self._db.fetchone(
            f'{_SELECT_SNAPSHOTS} AND handle = ?', (platform, handle)
        )
        return None if row is None else _snapshot(row)


def _member_key(guild_id: int, user_id: int, platform: str) -> tuple[str, str, str]:
    """The key of a member's link or challenge on a platform, as stored."""
    return str(guild_id), str(user_id), platform


def _linked_account(row: Row) -> LinkedAccount:
    return LinkedAccount(
        guild_id=int(row['guild_id']),
        user_id=int(row['user_id']),
        platform=row['platform'],
        handle=row['handle'],
        method=row['method'],
        verified_at=from_epoch(row['verified_at']),
    )


def _snapshot(row: Row) -> AccountSnapshot:
    return AccountSnapshot(
        platform=row['platform'],
        handle=row['handle'],
        rating=row['rating'],
        max_rating=row['max_rating'],
        rank=row['rank'],
        rated_matches=row['rated_matches'],
        fetched_at=from_epoch(row['fetched_at']),
    )


def _folded(handle: str) -> str:
    """``handle`` as NOCASE compares it."""
    return handle.translate(_NOCASE)


def _whole_seconds(moment: datetime) -> datetime:
    """``moment`` in UTC, truncated to whole seconds as ``to_epoch`` floors."""
    return ensure_utc(moment).replace(microsecond=0)
