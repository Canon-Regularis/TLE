"""Linking members' accounts, and reading what /profile and /rank show.

A member proves that an account is theirs by putting a token on its public
profile: Codeforces' Organization, or AtCoder's Affiliation.

1. ``start_link`` checks the account exists and is free, then gives the member
   a token that holds for 10 minutes.
2. ``check`` fetches the profile again and looks for the token.
3. The link is stored: AtCoder's by ``complete_atcoder``, Codeforces' in TLE's
   own table by the cog (only it can reach TLE, through
   ``tle.kcpc.bot.codeforces_links``) before ``complete_codeforces``.

Links stay when members leave the server, for if they come back. But an AtCoder
account linked by a member who has left is free to link: whoever proves it is
theirs takes the link over. Only the cog knows who is still a member, so it
says, with ``is_member``. A Codeforces handle stays with a member who left, as
TLE keeps it.

The reads, ``profile_accounts`` and ``leaderboard``, use only the database:
ratings come from the snapshots that ``refresh`` keeps up to date.
"""

import logging
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import NewType

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.features.accounts.repo import (
    AFFILIATION_TOKEN,
    AccountRepo,
    AccountSnapshot,
    HandleTaken,
    LinkChallenge,
    LinkedAccount,
)
from tle.kcpc.platforms import codeforces
from tle.kcpc.platforms.atcoder import profile as atcoder
from tle.kcpc.platforms.atcoder.profile import AtCoderProfile, AtCoderProfileClient
from tle.kcpc.platforms.codeforces import CodeforcesUser

logger = logging.getLogger(__name__)

CODEFORCES = codeforces.PLATFORM
ATCODER = atcoder.PLATFORM

# How long a member has to put their token on their profile.
CHALLENGE_LIFETIME = timedelta(minutes=10)
# 40 random bits per token. A token someone left in their profile can't then be
# matched by asking /link for new tokens until one is the same.
_TOKEN_BYTES = 5
# /profile refreshes ratings older than this before showing them.
SNAPSHOT_MAX_AGE = timedelta(hours=1)

# The characters Discord's markdown gives a meaning to, as a handle may hold.
_MARKDOWN = re.compile(r'([\\*_~`|>])')


@dataclass(frozen=True)
class LinkPlatform:
    """A platform that members link accounts on, and where its token goes."""

    key: str  # 'codeforces' or 'atcoder'
    name: str  # as replies name it
    proof_field: str  # the profile field that the token goes in
    settings_url: str  # where members edit that field
    profile_url: str  # an account's profile, with '{handle}' in it

    def url_for(self, handle: str) -> str:
        """The profile page of ``handle``."""
        return self.profile_url.format(handle=handle)


PLATFORMS: Mapping[str, LinkPlatform] = MappingProxyType(
    {
        CODEFORCES: LinkPlatform(
            CODEFORCES,
            'Codeforces',
            'Organization',
            'https://codeforces.com/settings/social',
            'https://codeforces.com/profile/{handle}',
        ),
        ATCODER: LinkPlatform(
            ATCODER,
            'AtCoder',
            'Affiliation',
            'https://atcoder.jp/settings',
            atcoder.PROFILE_URL,
        ),
    }
)


def link_platform(key: str) -> LinkPlatform:
    """The platform called ``key``; ``ValueError`` if members can't link one there."""
    platform = PLATFORMS.get(key)
    if platform is None:
        raise ValueError(f'Accounts cannot be linked on {key!r}')
    return platform


@dataclass(frozen=True)
class Profile:
    """An account's public profile, on either platform, as linking reads it."""

    platform: str
    handle: str  # in its canonical case
    rating: int | None  # None if never rated
    max_rating: int | None
    rank: str | None  # Codeforces' rank ('expert') or AtCoder's colour ('cyan')
    rated_matches: int | None  # None if the platform doesn't say
    proof: str | None  # the field the token goes in: Organization or Affiliation
    url: str

    @classmethod
    def from_codeforces(cls, user: CodeforcesUser) -> 'Profile':
        return cls(
            platform=CODEFORCES,
            handle=user.handle,
            rating=user.rating,
            max_rating=user.max_rating,
            rank=user.rank,
            rated_matches=None,
            proof=user.organization,
            url=user.url,
        )

    @classmethod
    def from_atcoder(cls, profile: AtCoderProfile) -> 'Profile':
        return cls(
            platform=ATCODER,
            handle=profile.handle,
            rating=profile.rating,
            max_rating=profile.highest_rating,
            rank=profile.color,
            rated_matches=profile.rated_matches,
            proof=profile.affiliation,
            url=profile.url,
        )

    def snapshot(self, fetched_at: datetime) -> AccountSnapshot:
        """The profile's ratings, to store as fetched at ``fetched_at``."""
        return AccountSnapshot(
            platform=self.platform,
            handle=self.handle,
            rating=self.rating,
            max_rating=self.max_rating,
            rank=self.rank,
            rated_matches=self.rated_matches,
            fetched_at=fetched_at,
        )


# A profile that showed its member's token: proof that the account is theirs.
# Only ``AccountService.check`` makes one.
VerifiedProfile = NewType('VerifiedProfile', Profile)

# Whether the user with this ID is still a member of the guild.
IsMember = Callable[[int], bool]


def _everyone(user_id: int) -> bool:
    """Counts every user as a member, so that no link is ever taken over."""
    return True


@dataclass(frozen=True)
class ProfileAccount:
    """One of a member's linked accounts, with its ratings as last fetched."""

    platform: str
    handle: str
    snapshot: AccountSnapshot | None  # None until its ratings are first fetched

    @property
    def url(self) -> str:
        return link_platform(self.platform).url_for(self.handle)

    def stale(self, now: datetime) -> bool:
        """Whether its ratings are missing or more than an hour old at ``now``."""
        return (
            self.snapshot is None or now - self.snapshot.fetched_at > SNAPSHOT_MAX_AGE
        )


@dataclass(frozen=True)
class Standing:
    """A member's place on a leaderboard."""

    place: int  # members with the same rating share a place
    user_id: int
    handle: str
    snapshot: AccountSnapshot | None  # None until its ratings are first fetched

    @property
    def rating(self) -> int | None:
        return None if self.snapshot is None else self.snapshot.rating


class AccountService:
    """Links members' accounts, and reads their ratings for /profile and /rank."""

    def __init__(
        self, repo: AccountRepo, atcoder: AtCoderProfileClient, clock: Clock
    ) -> None:
        self._repo = repo
        self._atcoder = atcoder
        self._clock = clock

    async def start_link(
        self,
        guild_id: int,
        user_id: int,
        platform: str,
        handle: str,
        *,
        current_handle: str | None = None,
        owner_id: int | None = None,
        is_member: IsMember = _everyone,
        vet: Callable[[Profile], None] | None = None,
    ) -> LinkChallenge:
        """Give the member a token to prove that ``handle`` on ``platform`` is theirs.

        The account must exist, and nobody else in the guild may have it,
        except, on AtCoder, a user who has left the guild (``is_member`` says
        who is still in it). Codeforces handles are in TLE's table, which only
        the cog can read, so for Codeforces the cog passes ``current_handle``,
        the member's handle there, and ``owner_id``, whoever has ``handle``
        there, members who left included. A member who has a Codeforces
        handle can't link another: as TLE has it, an Admin or Moderator
        changes it. ``vet``, if given, checks the profile once it is fetched,
        and raises ``KcpcUserError`` for an account that couldn't be linked
        anyway, so that the member isn't sent to edit their profile for
        nothing. The challenge, with the handle in its canonical case, replaces
        any the member had on the platform.
        """
        details = link_platform(platform)
        handle = handle.strip()
        if not handle:
            raise KcpcUserError(f'Give your {details.name} handle.')
        if platform == CODEFORCES and current_handle is not None:
            raise KcpcUserError(_codeforces_handle_set(current_handle))
        if platform == ATCODER:
            owner_id = await self._repo.owner_of(guild_id, platform, handle)
            if owner_id is not None and not is_member(owner_id):
                owner_id = None  # they left: the account is free to prove
        if owner_id is not None and owner_id != user_id:
            raise HandleTaken(platform, handle)
        profile = await self._fetch(platform, handle)
        if profile is None:
            raise KcpcUserError(f'No {details.name} user called {_escape(handle)}.')
        if vet is not None:
            vet(profile)
        now = self._clock.now()
        challenge = LinkChallenge(
            guild_id=guild_id,
            user_id=user_id,
            platform=platform,
            handle=profile.handle,
            token=f'kcpc-{secrets.token_hex(_TOKEN_BYTES)}',
            created_at=now,
            expires_at=now + CHALLENGE_LIFETIME,
        )
        await self._repo.save_challenge(challenge)
        return challenge

    async def check(
        self,
        guild_id: int,
        user_id: int,
        platform: str,
        *,
        current_handle: str | None = None,
    ) -> VerifiedProfile:
        """The profile of the account the member is linking, if it shows their token.

        The token may be anywhere in the profile's Organization (Codeforces) or
        Affiliation (AtCoder), in any case. Nothing is stored: the caller links
        the account. ``current_handle`` is as for ``start_link``, since a
        moderator may have set the member's Codeforces handle meanwhile.
        """
        details = link_platform(platform)
        if platform == CODEFORCES and current_handle is not None:
            raise KcpcUserError(_codeforces_handle_set(current_handle))
        challenge = await self._repo.get_challenge(guild_id, user_id, platform)
        if challenge is None:
            raise KcpcUserError(
                f'You have no {details.name} account waiting to be verified. '
                f'Start with /link {platform} <handle>.'
            )
        handle = _escape(challenge.handle)
        if challenge.expired(self._clock.now()):
            raise KcpcUserError(
                f'Your token for {handle} has expired. Get a new one with '
                f'/link {platform} {handle}.'
            )
        profile = await self._fetch(platform, challenge.handle)
        if profile is None:
            raise KcpcUserError(f'No {details.name} user called {handle}.')
        if profile.handle.lower() != challenge.handle.lower():
            # The token only proves the account whose page was asked for, so
            # never link whatever other account the parser found on it.
            raise ExternalServiceError(
                details.name,
                f'{details.name} showed a different account for {handle}. '
                'Please try again later.',
            )
        if challenge.token.lower() not in (profile.proof or '').lower():
            raise KcpcUserError(
                f"The token {challenge.token} isn't in the {details.proof_field} "
                f'of {handle} yet. Put it there at <{details.settings_url}>, '
                'save, then verify again.'
            )
        return VerifiedProfile(profile)

    async def complete_atcoder(
        self,
        guild_id: int,
        user_id: int,
        profile: VerifiedProfile,
        *,
        is_member: IsMember = _everyone,
    ) -> LinkedAccount:
        """Link the member to the verified AtCoder account, and return the link.

        The link replaces any the member had, the challenge is deleted and the
        ratings saved, all together. If a user who has left the guild
        (``is_member`` says who is still in it) had linked the account, their
        link goes: the member has just proved the account is theirs. Raises
        ``HandleTaken``, changing nothing, if another member of the guild has
        linked the account meanwhile.
        """
        if profile.platform != ATCODER:
            raise ValueError(f'Not an AtCoder profile: {profile.platform}')
        now = self._clock.now()
        account = LinkedAccount(
            guild_id=guild_id,
            user_id=user_id,
            platform=ATCODER,
            handle=profile.handle,
            method=AFFILIATION_TOKEN,
            verified_at=now,
        )
        async with self._repo.transaction():
            # Whoever has the account linked, if they have left the guild.
            departed = await self._repo.owner_of(guild_id, ATCODER, profile.handle)
            if departed is not None and (departed == user_id or is_member(departed)):
                departed = None  # the member's own link, or a member's still here
            if departed is not None:
                await self._repo.unlink(guild_id, departed, ATCODER)
            await self._repo.link(account)
            await self._repo.delete_challenge(guild_id, user_id, ATCODER)
            await self._repo.save_snapshots([profile.snapshot(now)])
        if departed is not None:
            logger.info(
                'Member %d of guild %d proved the AtCoder account %s, so it is '
                'no longer linked to user %d, who left',
                user_id,
                guild_id,
                profile.handle,
                departed,
            )
        return account

    async def complete_codeforces(
        self, guild_id: int, user_id: int, profile: VerifiedProfile
    ) -> None:
        """Delete the member's challenge and save the ratings, together.

        For after the cog has linked the verified Codeforces account in TLE's
        table.
        """
        if profile.platform != CODEFORCES:
            raise ValueError(f'Not a Codeforces profile: {profile.platform}')
        async with self._repo.transaction():
            await self._repo.delete_challenge(guild_id, user_id, CODEFORCES)
            await self._repo.save_snapshots([profile.snapshot(self._clock.now())])

    async def unlink_atcoder(self, guild_id: int, user_id: int) -> LinkedAccount:
        """Unlink the member's AtCoder account and return the link that was removed."""
        removed = await self._repo.unlink(guild_id, user_id, ATCODER)
        if removed is None:
            raise KcpcUserError("You haven't linked an AtCoder account.")
        return removed

    async def remove_atcoder_link(self, guild_id: int, handle: str) -> LinkedAccount:
        """Unlink the AtCoder account ``handle`` from whoever linked it in the guild.

        For admins: it frees the handle of a member who linked an account that
        isn't theirs, which its owner can then link, or removes the link of a
        member who left. Returns the link that was removed.
        """
        handle = handle.strip()
        removed = await self._repo.unlink_handle(guild_id, ATCODER, handle)
        if removed is None:
            raise KcpcUserError(
                'Nobody in this server has linked the AtCoder account '
                f'{_escape(handle)}.'
            )
        return removed

    async def purge_expired_challenges(self) -> int:
        """Delete the challenges that have expired; returns how many there were."""
        return await self._repo.purge_expired(self._clock.now())

    async def profile_accounts(
        self, guild_id: int, user_id: int, codeforces_handle: str | None
    ) -> list[ProfileAccount]:
        """The member's linked accounts with their ratings: Codeforces first.

        ``codeforces_handle`` is the member's handle in TLE's table, if any;
        the others are linked in kcpc.db.
        """
        accounts = [(CODEFORCES, codeforces_handle)] if codeforces_handle else []
        links = await self._repo.links_for_user(guild_id, user_id)
        accounts += [(link.platform, link.handle) for link in links]
        return [
            ProfileAccount(
                platform, handle, await self._repo.snapshot(platform, handle)
            )
            for platform, handle in accounts
        ]

    async def linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        """The member's handle on ``platform`` as linked in kcpc.db, or None.

        The cog registers the service as the ``HandleSource`` of AtCoder
        handles (see ``tle.kcpc.core.handles``), which other features read
        through it. Codeforces handles are in TLE's table instead.
        """
        link = await self._repo.get_link(guild_id, user_id, platform)
        return None if link is None else link.handle

    async def linked_handles(
        self, guild_id: int, platform: str
    ) -> list[tuple[int, str]]:
        """``(user_id, handle)`` of each link on ``platform`` stored in kcpc.db."""
        links = await self._repo.links_for_guild(guild_id, platform)
        return [(link.user_id, link.handle) for link in links]

    async def leaderboard(
        self, platform: str, members: Iterable[tuple[int, str]]
    ) -> list[Standing]:
        """``members``, ``(user_id, handle)``, by current rating on ``platform``.

        The best rating comes first. Members who are unrated, or whose ratings
        haven't been fetched yet, come last, sharing a place, as do members
        with the same rating; ties are in order of handle.
        """
        pairs = list(members)
        snapshots = await self._repo.snapshots(
            platform, [handle for _, handle in pairs]
        )
        entries = sorted(
            ((user_id, handle, snapshots.get(handle)) for user_id, handle in pairs),
            key=lambda entry: _ranking_key(entry[1], entry[2]),
        )
        standings: list[Standing] = []
        for index, (user_id, handle, snapshot) in enumerate(entries):
            standing = Standing(index + 1, user_id, handle, snapshot)
            if standings and standings[-1].rating == standing.rating:
                standing = Standing(standings[-1].place, user_id, handle, snapshot)
            standings.append(standing)
        return standings

    async def _fetch(self, platform: str, handle: str) -> Profile | None:
        """The account's profile, whatever the case of ``handle``; None if none."""
        if platform == CODEFORCES:
            user = await codeforces.fetch_user(handle)
            return None if user is None else Profile.from_codeforces(user)
        found = await self._atcoder.fetch(handle)
        return None if found is None else Profile.from_atcoder(found)


def _ranking_key(
    handle: str, snapshot: AccountSnapshot | None
) -> tuple[bool, int, str]:
    rating = None if snapshot is None else snapshot.rating
    return rating is None, -(rating or 0), handle.lower()


def _codeforces_handle_set(handle: str) -> str:
    return (
        f'Your Codeforces handle is already set to {_escape(handle)}. '
        'Ask an Admin or Moderator if you wish to change it.'
    )


def _escape(text: str) -> str:
    """``text`` to show as it is in a Discord message, whatever its characters."""
    return _MARKDOWN.sub(r'\\\1', text)
