"""Component tests for the accounts feature's AccountRepo, on kcpc.db."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import zone
from tle.kcpc.features.accounts.repo import (
    AFFILIATION_TOKEN,
    AccountRepo,
    AccountSnapshot,
    HandleTaken,
    LinkChallenge,
    LinkedAccount,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MICROSECOND = timedelta(microseconds=1)
TTL = timedelta(minutes=10)
EXPIRY = NOW + TTL
NAIVE = datetime(2026, 10, 1, 12, 0)

# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
ALICE = 1_200_000_000_000_000_001
BOB = 1_200_000_000_000_000_002
CAROL = 1_200_000_000_000_000_003


def at(**offset: float) -> datetime:
    """``NOW`` moved by ``offset``, e.g. ``at(hours=1)``."""
    return NOW + timedelta(**offset)


def link_of(handle: str = 'Amber_Owl', **changes: Any) -> LinkedAccount:
    """Alice's AtCoder link in the guild, with ``changes``."""
    account = LinkedAccount(
        guild_id=GUILD,
        user_id=ALICE,
        platform='atcoder',
        handle=handle,
        method=AFFILIATION_TOKEN,
        verified_at=NOW,
    )
    return replace(account, **changes)


def challenge_of(handle: str = 'Amber_Owl', **changes: Any) -> LinkChallenge:
    """Alice's AtCoder challenge in the guild, expiring at ``EXPIRY``."""
    challenge = LinkChallenge(
        guild_id=GUILD,
        user_id=ALICE,
        platform='atcoder',
        handle=handle,
        token='kcpc-0a1b2c',
        created_at=NOW,
        expires_at=EXPIRY,
    )
    return replace(challenge, **changes)


def snapshot_of(handle: str = 'Amber_Owl', **changes: Any) -> AccountSnapshot:
    """A rated AtCoder snapshot fetched at ``NOW``."""
    snapshot = AccountSnapshot(
        platform='atcoder',
        handle=handle,
        rating=1534,
        max_rating=1610,
        rank='cyan',
        rated_matches=12,
        fetched_at=NOW,
    )
    return replace(snapshot, **changes)


@pytest.fixture
def repo(db: Database) -> AccountRepo:
    return AccountRepo(db)


class TestRecords:
    def test_times_are_converted_to_whole_seconds_in_utc(self) -> None:
        # 12:00:00.999999 UTC.
        local = datetime(2026, 10, 1, 21, 0, 0, 999_999, tzinfo=zone('Asia/Tokyo'))

        link = link_of(verified_at=local)
        challenge = challenge_of(created_at=local, expires_at=local + TTL)
        snapshot = snapshot_of(fetched_at=local)

        assert link.verified_at == NOW
        assert link.verified_at.tzinfo is UTC
        assert (challenge.created_at, challenge.expires_at) == (NOW, EXPIRY)
        assert snapshot.fetched_at == NOW
        assert link == link_of()

    @pytest.mark.parametrize(
        'build',
        [
            lambda: link_of(verified_at=NAIVE),
            lambda: challenge_of(created_at=NAIVE),
            lambda: challenge_of(expires_at=NAIVE),
            lambda: snapshot_of(fetched_at=NAIVE),
        ],
        ids=['verified_at', 'created_at', 'expires_at', 'fetched_at'],
    )
    def test_naive_times_are_refused(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValueError, match='timezone-aware'):
            build()


class TestLinks:
    async def test_a_link_reads_back_as_stored(
        self, repo: AccountRepo, db: Database
    ) -> None:
        link = link_of(verified_at=at(seconds=0.75))

        await repo.link(link)

        assert await repo.get_link(GUILD, ALICE, 'atcoder') == link
        assert await repo.get_link(GUILD, ALICE, 'codeforces') is None
        assert await repo.get_link(GUILD, BOB, 'atcoder') is None
        assert await repo.get_link(OTHER_GUILD, ALICE, 'atcoder') is None
        assert await db.fetchval('SELECT method FROM linked_account') == (
            'affiliation-token'
        )

    async def test_linking_again_replaces_the_members_handle(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('Amber_Owl'))
        relinked = link_of('Brisk_Heron', verified_at=at(hours=1))

        await repo.link(relinked)

        assert await repo.get_link(GUILD, ALICE, 'atcoder') == relinked
        assert await repo.links_for_guild(GUILD, 'atcoder') == [relinked]
        assert await repo.owner_of(GUILD, 'atcoder', 'Amber_Owl') is None

    async def test_a_member_may_relink_their_handle_in_another_case(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('amber_owl'))

        await repo.link(link_of('Amber_Owl'))

        assert await repo.links_for_guild(GUILD, 'atcoder') == [link_of('Amber_Owl')]

    async def test_a_handle_another_member_linked_is_refused_in_any_case(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('Amber_Owl'))
        bobs = link_of('Brisk_Heron', user_id=BOB)
        await repo.link(bobs)

        with pytest.raises(HandleTaken) as caught:
            await repo.link(link_of('aMBER_oWL', user_id=BOB, verified_at=at(hours=1)))

        # The message is shown to Bob as it is: it names nobody, and says who
        # can free the handle.
        error = caught.value
        assert str(error) == (
            'The handle aMBER_oWL is already linked to someone else in this server. '
            'If it is yours, ask an admin to unlink it with /kcpc accounts unlink.'
        )
        assert (error.platform, error.handle) == ('atcoder', 'aMBER_oWL')
        assert issubclass(HandleTaken, KcpcUserError)
        assert await repo.get_link(GUILD, BOB, 'atcoder') == bobs
        assert await repo.owner_of(GUILD, 'atcoder', 'Amber_Owl') == ALICE

    async def test_a_handle_may_be_linked_in_another_guild_or_on_another_platform(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('Amber_Owl'))

        await repo.link(link_of('amber_owl', guild_id=OTHER_GUILD, user_id=BOB))
        await repo.link(link_of('amber_owl', user_id=BOB, platform='codeforces'))

        assert await repo.owner_of(GUILD, 'atcoder', 'amber_owl') == ALICE
        assert await repo.owner_of(OTHER_GUILD, 'atcoder', 'Amber_Owl') == BOB
        assert await repo.owner_of(GUILD, 'codeforces', 'Amber_Owl') == BOB

    async def test_owner_of_a_handle_nobody_linked_is_none(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('Amber_Owl'))

        assert await repo.owner_of(GUILD, 'atcoder', 'AMBER_OWL') == ALICE
        assert await repo.owner_of(GUILD, 'atcoder', 'Brisk_Heron') is None
        assert await repo.owner_of(GUILD, 'codeforces', 'Amber_Owl') is None
        assert await repo.owner_of(OTHER_GUILD, 'atcoder', 'Amber_Owl') is None

    async def test_unlink_removes_the_link_and_returns_it(
        self, repo: AccountRepo
    ) -> None:
        link = link_of()
        kept = [
            link_of(platform='codeforces'),
            link_of(guild_id=OTHER_GUILD),
            link_of('Brisk_Heron', user_id=BOB),
        ]
        for account in [link, *kept]:
            await repo.link(account)

        assert await repo.unlink(GUILD, ALICE, 'atcoder') == link

        assert await repo.get_link(GUILD, ALICE, 'atcoder') is None
        assert await repo.owner_of(GUILD, 'atcoder', 'Amber_Owl') is None
        assert await repo.unlink(GUILD, ALICE, 'atcoder') is None
        for account in kept:
            stored = await repo.get_link(
                account.guild_id, account.user_id, account.platform
            )
            assert stored == account

    async def test_unlink_handle_removes_the_link_whoever_made_it(
        self, repo: AccountRepo
    ) -> None:
        bobs = link_of('Amber_Owl', user_id=BOB)
        kept = [
            link_of('Brisk_Heron'),
            link_of('Amber_Owl', guild_id=OTHER_GUILD),
            link_of('Amber_Owl', user_id=CAROL, platform='codeforces'),
        ]
        for account in [bobs, *kept]:
            await repo.link(account)

        assert await repo.unlink_handle(GUILD, 'atcoder', 'aMBER_oWL') == bobs

        assert await repo.owner_of(GUILD, 'atcoder', 'Amber_Owl') is None
        assert await repo.unlink_handle(GUILD, 'atcoder', 'Amber_Owl') is None
        for account in kept:
            stored = await repo.get_link(
                account.guild_id, account.user_id, account.platform
            )
            assert stored == account

    async def test_links_for_user_are_the_members_in_the_guild_by_platform(
        self, repo: AccountRepo
    ) -> None:
        codeforces = link_of(platform='codeforces')
        atcoder = link_of()
        for account in [
            codeforces,
            atcoder,
            link_of(guild_id=OTHER_GUILD),
            link_of('Brisk_Heron', user_id=BOB),
        ]:
            await repo.link(account)

        assert await repo.links_for_user(GUILD, ALICE) == [atcoder, codeforces]
        assert await repo.links_for_user(GUILD, CAROL) == []

    async def test_links_for_guild_are_by_handle_ignoring_case(
        self, repo: AccountRepo
    ) -> None:
        # Stored in reverse, and a case-sensitive sort would put Brisk_Heron
        # first.
        carols = link_of('cold_lynx', user_id=CAROL)
        bobs = link_of('Brisk_Heron', user_id=BOB)
        alices = link_of('amber_owl')
        for account in [
            carols,
            bobs,
            alices,
            link_of('Dusky_Wren', guild_id=OTHER_GUILD),
            link_of('Dusky_Wren', platform='codeforces'),
        ]:
            await repo.link(account)

        assert await repo.links_for_guild(GUILD, 'atcoder') == [alices, bobs, carols]
        assert await repo.links_for_guild(GUILD, 'kattis') == []


class TestChallenges:
    async def test_a_challenge_reads_back_as_stored(self, repo: AccountRepo) -> None:
        challenge = challenge_of(created_at=at(seconds=0.5))

        await repo.save_challenge(challenge)

        assert await repo.get_challenge(GUILD, ALICE, 'atcoder') == challenge
        assert await repo.get_challenge(GUILD, ALICE, 'codeforces') is None
        assert await repo.get_challenge(GUILD, BOB, 'atcoder') is None
        assert await repo.get_challenge(OTHER_GUILD, ALICE, 'atcoder') is None

    async def test_saving_again_replaces_the_members_challenge(
        self, repo: AccountRepo
    ) -> None:
        await repo.save_challenge(challenge_of('Amber_Owl'))
        again = challenge_of(
            'Brisk_Heron',
            token='kcpc-3d4e5f',
            created_at=at(minutes=5),
            expires_at=at(minutes=15),
        )

        await repo.save_challenge(again)

        assert await repo.get_challenge(GUILD, ALICE, 'atcoder') == again

    async def test_delete_challenge_deletes_only_the_members_on_the_platform(
        self, repo: AccountRepo
    ) -> None:
        kept = [
            challenge_of(platform='codeforces'),
            challenge_of(guild_id=OTHER_GUILD),
            challenge_of(user_id=BOB),
        ]
        for challenge in [challenge_of(), *kept]:
            await repo.save_challenge(challenge)

        await repo.delete_challenge(GUILD, ALICE, 'atcoder')
        await repo.delete_challenge(GUILD, CAROL, 'atcoder')  # has none

        assert await repo.get_challenge(GUILD, ALICE, 'atcoder') is None
        for challenge in kept:
            stored = await repo.get_challenge(
                challenge.guild_id, challenge.user_id, challenge.platform
            )
            assert stored == challenge

    @pytest.mark.parametrize(
        ('now', 'expired'),
        [
            (EXPIRY - SECOND, False),
            (EXPIRY - MICROSECOND, False),
            (EXPIRY, True),
            (EXPIRY + MICROSECOND, True),
        ],
        ids=['second-before', 'microsecond-before', 'at-expiry', 'microsecond-after'],
    )
    async def test_a_challenge_expires_at_its_expiry_time(
        self, repo: AccountRepo, now: datetime, expired: bool
    ) -> None:
        challenge = challenge_of()
        await repo.save_challenge(challenge)

        assert challenge.expired(now) is expired
        assert await repo.purge_expired(now) == int(expired)
        remaining = await repo.get_challenge(GUILD, ALICE, 'atcoder')
        assert remaining == (None if expired else challenge)

    async def test_purge_expired_deletes_every_expired_challenge(
        self, repo: AccountRepo
    ) -> None:
        live = challenge_of(user_id=CAROL, expires_at=EXPIRY + SECOND)
        for challenge in [
            challenge_of(created_at=at(minutes=-11), expires_at=at(minutes=-1)),
            challenge_of(guild_id=OTHER_GUILD, user_id=BOB, platform='codeforces'),
            live,
        ]:
            await repo.save_challenge(challenge)

        assert await repo.purge_expired(EXPIRY) == 2

        assert await repo.get_challenge(GUILD, ALICE, 'atcoder') is None
        assert await repo.get_challenge(OTHER_GUILD, BOB, 'codeforces') is None
        assert await repo.get_challenge(GUILD, CAROL, 'atcoder') == live
        assert await repo.purge_expired(EXPIRY) == 0


class TestSnapshots:
    async def test_snapshots_read_back_as_stored(self, repo: AccountRepo) -> None:
        rated = snapshot_of(fetched_at=at(seconds=0.25))
        unrated = AccountSnapshot(
            'codeforces', 'Brisk_Heron', None, None, None, None, NOW
        )

        await repo.save_snapshots([rated, unrated])

        assert await repo.snapshot('atcoder', 'Amber_Owl') == rated
        assert await repo.snapshot('codeforces', 'Brisk_Heron') == unrated
        assert await repo.snapshot('atcoder', 'Brisk_Heron') is None
        assert await repo.snapshot('codeforces', 'Amber_Owl') is None

    async def test_a_snapshot_is_found_in_any_case(self, repo: AccountRepo) -> None:
        await repo.save_snapshots([snapshot_of('Amber_Owl')])

        assert await repo.snapshot('atcoder', 'aMBER_oWL') == snapshot_of('Amber_Owl')

    @pytest.mark.parametrize(
        ('fetched_at', 'replaced'),
        [(at(hours=1), True), (NOW, True), (at(seconds=-1), False)],
        ids=['newer', 'same-second', 'older'],
    )
    async def test_a_snapshot_replaces_the_stored_one_unless_older(
        self, repo: AccountRepo, fetched_at: datetime, replaced: bool
    ) -> None:
        stored = snapshot_of('Amber_Owl')
        await repo.save_snapshots([stored])
        # The handle takes the case that the platform gives it now.
        fetched = snapshot_of('amber_owl', rating=1601, fetched_at=fetched_at)

        await repo.save_snapshots([fetched])

        assert await repo.snapshot('atcoder', 'Amber_Owl') == (
            fetched if replaced else stored
        )

    async def test_snapshots_are_by_handle_as_given(self, repo: AccountRepo) -> None:
        amber = snapshot_of('Amber_Owl')
        heron = snapshot_of('Brisk_Heron', rating=None, max_rating=None, rank=None)
        await repo.save_snapshots(
            [amber, heron, snapshot_of('Cold_Lynx', platform='codeforces')]
        )

        found = await repo.snapshots(
            'atcoder', ['BRISK_HERON', 'Cold_Lynx', 'amber_owl', 'Amber_Owl']
        )

        assert list(found.items()) == [
            ('BRISK_HERON', heron),
            ('amber_owl', amber),
            ('Amber_Owl', amber),
        ]
        assert await repo.snapshots('atcoder', []) == {}

    async def test_snapshots_of_more_handles_than_one_query_holds(
        self, repo: AccountRepo
    ) -> None:
        stored = [snapshot_of(f'user_{n:04d}', rating=n) for n in range(1200)]
        await repo.save_snapshots(stored)
        handles = [snapshot.handle.upper() for snapshot in stored]

        found = await repo.snapshots('atcoder', handles)

        assert list(found) == handles
        assert list(found.values()) == stored

    async def test_snapshots_refuses_a_lone_handle(self, repo: AccountRepo) -> None:
        with pytest.raises(TypeError, match='collection of handles'):
            await repo.snapshots('atcoder', 'Amber_Owl')


class TestTransaction:
    async def test_writes_in_the_block_are_committed_together(
        self, repo: AccountRepo, db: Database
    ) -> None:
        await repo.save_challenge(challenge_of())

        async with repo.transaction():
            await repo.link(link_of())
            await repo.delete_challenge(GUILD, ALICE, 'atcoder')
            await repo.save_snapshots([snapshot_of()])
            assert db.in_transaction()

        assert not db.in_transaction()
        assert await repo.get_link(GUILD, ALICE, 'atcoder') == link_of()
        assert await repo.get_challenge(GUILD, ALICE, 'atcoder') is None
        assert await repo.snapshot('atcoder', 'Amber_Owl') == snapshot_of()

    async def test_an_error_rolls_back_every_write_in_the_block(
        self, repo: AccountRepo
    ) -> None:
        await repo.link(link_of('Amber_Owl'))

        with pytest.raises(HandleTaken):
            async with repo.transaction():
                await repo.save_challenge(challenge_of(user_id=BOB))
                await repo.save_snapshots([snapshot_of()])
                await repo.link(link_of('amber_owl', user_id=BOB))

        assert await repo.get_challenge(GUILD, BOB, 'atcoder') is None
        assert await repo.snapshot('atcoder', 'Amber_Owl') is None
        assert await repo.get_link(GUILD, BOB, 'atcoder') is None
