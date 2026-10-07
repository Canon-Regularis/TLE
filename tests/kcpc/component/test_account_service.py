"""Tests for tle.kcpc.features.accounts.service: linking accounts, and the reads.

The service runs on a real AccountRepo in a migrated in-memory kcpc.db. The
sites are faked: AtCoder's profile pages by FakeAtCoder, and Codeforces by
FakeCodeforces in place of TLE's ``cf.user.info``. The users are made up.
"""

import itertools
import logging
import re
import secrets
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import cast

import pytest

from tests.kcpc.conftest import CLOCK_START
from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import ExternalServiceError, KcpcUserError
from tle.kcpc.features.accounts import service as service_module
from tle.kcpc.features.accounts.repo import (
    AccountRepo,
    AccountSnapshot,
    HandleTaken,
    LinkChallenge,
    LinkedAccount,
)
from tle.kcpc.features.accounts.service import (
    ATCODER,
    CODEFORCES,
    AccountService,
    Profile,
    ProfileAccount,
    VerifiedProfile,
    link_platform,
)
from tle.kcpc.platforms.atcoder.profile import (
    HANDLE_RE,
    PROFILE_URL,
    AtCoderProfile,
    AtCoderProfileClient,
)
from tle.util import codeforces_api as cf

GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
MEMBER = 1_300_000_000_000_000_001
OTHER_MEMBER = 1_300_000_000_000_000_002
NOW = CLOCK_START
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
TOKENS = ('a1b2c3', 'd4e5f6', '0a0b0c')
# Taken before the ``tokens`` fixture replaces it.
TOKEN_HEX = secrets.token_hex


class FakeAtCoder:
    """AtCoder's profile pages, as AtCoderProfileClient reads them.

    A user is found whatever the case of the name asked for. ``error`` makes
    every fetch raise, and ``fetched`` lists the names asked for.
    """

    def __init__(self) -> None:
        self.profiles: dict[str, AtCoderProfile] = {}
        self.fetched: list[str] = []
        self.error: Exception | None = None

    def add(
        self, handle: str, *, rating: int | None = 1834, affiliation: str | None = None
    ) -> AtCoderProfile:
        profile = AtCoderProfile(
            handle=handle,
            rating=rating,
            highest_rating=None if rating is None else rating + 78,
            rated_matches=0 if rating is None else 27,
            affiliation=affiliation,
            color='unrated' if rating is None else 'cyan',
            url=PROFILE_URL.format(handle=handle),
        )
        self.profiles[handle.lower()] = profile
        return profile

    async def fetch(self, handle: str) -> AtCoderProfile | None:
        self.fetched.append(handle)
        if self.error is not None:
            raise self.error
        if HANDLE_RE.fullmatch(handle) is None:
            raise KcpcUserError("That isn't a valid AtCoder username.")
        return self.profiles.get(handle.lower())


class FakeCodeforces:
    """Codeforces as TLE's ``cf.user.info`` asks it, whatever the case asked.

    Like the real API, it fails a whole request for one unknown handle.
    ``asked`` lists each request's handles.
    """

    def __init__(self) -> None:
        self.users: dict[str, cf.User] = {}
        self.asked: list[list[str]] = []
        self.error: Exception | None = None

    def add(
        self, handle: str, *, rating: int | None = 1700, organization: str | None = None
    ) -> cf.User:
        user = cf.User(
            handle=handle,
            firstName=None,
            lastName=None,
            country=None,
            city=None,
            organization=organization,
            contribution=0,
            rating=rating,
            maxRating=None if rating is None else rating + 100,
            lastOnlineTimeSeconds=1_790_000_000,
            registrationTimeSeconds=1_600_000_000,
            friendOfCount=0,
            titlePhoto='https://userpic.codeforces.org/no-title.jpg',
        )
        self.users[handle.lower()] = user
        return user

    async def info(self, *, handles: Sequence[str]) -> list[cf.User]:
        self.asked.append(list(handles))
        if self.error is not None:
            raise self.error
        found = []
        for handle in handles:
            user = self.users.get(handle.lower())
            if user is None:
                comment = f'handles: User with handle {handle} not found'
                raise cf.HandleNotFoundError(comment, handle)
            found.append(user)
        return found


@pytest.fixture
def atcoder() -> FakeAtCoder:
    return FakeAtCoder()


@pytest.fixture
def codeforces(monkeypatch: pytest.MonkeyPatch) -> FakeCodeforces:
    fake = FakeCodeforces()
    monkeypatch.setattr(cf.user, 'info', fake.info)
    return fake


@pytest.fixture(autouse=True)
def tokens(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The random part of each token, in turn: TOKENS, then more."""
    hexes = itertools.chain(TOKENS, (f'{n:06x}' for n in itertools.count()))
    monkeypatch.setattr(service_module.secrets, 'token_hex', lambda size: next(hexes))
    return hexes


@pytest.fixture
def repo(db: Database) -> AccountRepo:
    return AccountRepo(db)


@pytest.fixture
def service(
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> AccountService:
    return AccountService(repo, cast(AtCoderProfileClient, atcoder), clock)


def challenge(
    platform: str, handle: str, token: str, *, at: datetime = NOW
) -> LinkChallenge:
    return LinkChallenge(
        guild_id=GUILD,
        user_id=MEMBER,
        platform=platform,
        handle=handle,
        token=f'kcpc-{token}',
        created_at=at,
        expires_at=at + 10 * MINUTE,
    )


def atcoder_link(handle: str, user_id: int = OTHER_MEMBER) -> LinkedAccount:
    return LinkedAccount(GUILD, user_id, ATCODER, handle, 'affiliation-token', NOW)


def other_member_left(user_id: int) -> bool:
    """``is_member`` for the guild once OTHER_MEMBER has left it."""
    return user_id != OTHER_MEMBER


def snapshot(
    platform: str, handle: str, rating: int | None, *, at: datetime = NOW
) -> AccountSnapshot:
    return AccountSnapshot(platform, handle, rating, rating, None, None, at)


@pytest.mark.parametrize('platform', [ATCODER, CODEFORCES])
async def test_start_link_saves_a_challenge_for_the_canonical_handle(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    platform: str,
) -> None:
    atcoder.add('FakeAtCoder')
    codeforces.add('FakeAtCoder')

    started = await service.start_link(GUILD, MEMBER, platform, ' fakeatcoder ')

    # 10 minutes to put the token on the profile.
    assert started == challenge(platform, 'FakeAtCoder', 'a1b2c3')
    assert await repo.get_challenge(GUILD, MEMBER, platform) == started


async def test_linking_again_replaces_the_challenge(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    clock: FakeClock,
) -> None:
    atcoder.add('FakeAtCoder')
    atcoder.add('OtherAtCoder')
    await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')
    await clock.advance(MINUTE)

    again = await service.start_link(GUILD, MEMBER, ATCODER, 'OtherAtCoder')

    assert again == challenge(ATCODER, 'OtherAtCoder', 'd4e5f6', at=NOW + MINUTE)
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) == again


async def test_a_token_from_before_linking_again_is_refused(
    service: AccountService, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder', affiliation='kcpc-a1b2c3')
    await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')
    await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')  # kcpc-d4e5f6

    with pytest.raises(KcpcUserError) as raised:
        await service.check(GUILD, MEMBER, ATCODER)

    assert str(raised.value) == (
        "The token kcpc-d4e5f6 isn't in the Affiliation of FakeAtCoder yet. "
        'Put it there at <https://atcoder.jp/settings>, save, then verify again.'
    )


async def test_a_token_is_kcpc_then_ten_random_hex_digits(
    service: AccountService, atcoder: FakeAtCoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other tests fix the random part; this one keeps secrets' own.
    monkeypatch.setattr(service_module.secrets, 'token_hex', TOKEN_HEX)
    atcoder.add('FakeAtCoder')

    started = await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')

    assert re.fullmatch(r'kcpc-[0-9a-f]{10}', started.token)


@pytest.mark.parametrize(
    ('platform', 'handle', 'message'),
    [
        (ATCODER, 'nobody', 'No AtCoder user called nobody.'),
        (CODEFORCES, 'nobody', 'No Codeforces user called nobody.'),
        (CODEFORCES, 'no_body', r'No Codeforces user called no\_body.'),
        (ATCODER, '  ', 'Give your AtCoder handle.'),
        (CODEFORCES, '', 'Give your Codeforces handle.'),
        (ATCODER, 'not valid', "That isn't a valid AtCoder username."),
    ],
)
async def test_start_link_refuses_an_account_that_isnt_there(
    service: AccountService,
    repo: AccountRepo,
    platform: str,
    handle: str,
    message: str,
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await service.start_link(GUILD, MEMBER, platform, handle)

    assert str(raised.value) == message
    assert await repo.get_challenge(GUILD, MEMBER, platform) is None


async def test_start_link_passes_on_a_site_that_is_down(
    service: AccountService, codeforces: FakeCodeforces
) -> None:
    codeforces.error = cf.ClientError()

    with pytest.raises(ExternalServiceError, match='Codeforces is not responding'):
        await service.start_link(GUILD, MEMBER, CODEFORCES, 'FakeCoder')


async def test_an_atcoder_account_another_member_linked_is_refused_unasked(
    service: AccountService, repo: AccountRepo, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder')
    await repo.link(atcoder_link('FakeAtCoder'))

    with pytest.raises(HandleTaken) as raised:
        await service.start_link(GUILD, MEMBER, ATCODER, 'FAKEATCODER')

    assert str(raised.value) == (
        'The handle FAKEATCODER is already linked to someone else in this server. '
        'If it is yours, ask an admin to unlink it with /kcpc accounts unlink.'
    )
    assert atcoder.fetched == []
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is None
    # Members of another server, and the member who linked it, may link it.
    await service.start_link(OTHER_GUILD, MEMBER, ATCODER, 'FakeAtCoder')
    await service.start_link(GUILD, OTHER_MEMBER, ATCODER, 'FakeAtCoder')


async def test_an_atcoder_account_linked_by_someone_who_left_is_free_to_prove(
    service: AccountService, repo: AccountRepo, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder')
    await repo.link(atcoder_link('FakeAtCoder'))

    started = await service.start_link(
        GUILD, MEMBER, ATCODER, 'fakeatcoder', is_member=other_member_left
    )

    assert started == challenge(ATCODER, 'FakeAtCoder', 'a1b2c3')
    # Their link stays until the account is proved.
    assert await repo.owner_of(GUILD, ATCODER, 'FakeAtCoder') == OTHER_MEMBER


async def test_a_codeforces_handle_another_member_has_is_refused_unasked(
    service: AccountService, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')

    with pytest.raises(HandleTaken) as raised:
        await service.start_link(
            GUILD,
            MEMBER,
            CODEFORCES,
            'fakecoder',
            owner_id=OTHER_MEMBER,
            # TLE keeps the handle of a member who left, and won't link it to
            # anyone else, so it stays taken.
            is_member=other_member_left,
        )

    # Moderators free a Codeforces handle, as it is TLE's.
    assert str(raised.value) == (
        'The handle fakecoder is already linked to someone else in this server. '
        'If it is yours, ask a moderator or admin to remove it with `;handle remove`.'
    )
    assert codeforces.asked == []
    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) is None


async def test_a_member_with_a_codeforces_handle_cant_link_another(
    service: AccountService, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')

    with pytest.raises(KcpcUserError) as raised:
        await service.start_link(
            GUILD, MEMBER, CODEFORCES, 'FakeCoder', current_handle='Old_Coder'
        )

    # TLE's rule, as /handle identify has it: staff change the handle.
    assert str(raised.value) == (
        r'Your Codeforces handle is already set to Old\_Coder. '
        'To change it, ask a moderator or admin.'
    )
    assert codeforces.asked == []
    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) is None


async def test_vet_checks_the_fetched_profile_before_a_token_is_given(
    service: AccountService, repo: AccountRepo, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')
    seen: list[Profile] = []

    def refuse(profile: Profile) -> None:
        seen.append(profile)
        raise KcpcUserError('Role for rank `Expert` not present in the server')

    with pytest.raises(KcpcUserError, match='Role for rank `Expert`'):
        await service.start_link(GUILD, MEMBER, CODEFORCES, ' fakecoder ', vet=refuse)

    assert [(p.platform, p.handle, p.rating) for p in seen] == [
        (CODEFORCES, 'FakeCoder', 1700)
    ]
    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) is None
    started = await service.start_link(
        GUILD, MEMBER, CODEFORCES, 'FakeCoder', vet=seen.append
    )
    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) == started


async def test_check_finds_the_token_in_the_affiliation_in_any_case(
    service: AccountService, atcoder: FakeAtCoder, clock: FakeClock
) -> None:
    atcoder.add('FakeAtCoder')
    await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')
    atcoder.add('FakeAtCoder', affiliation="King's College London KCPC-A1B2C3")
    await clock.advance(10 * MINUTE - timedelta(seconds=1))

    verified = await service.check(GUILD, MEMBER, ATCODER)

    assert verified == Profile(
        platform=ATCODER,
        handle='FakeAtCoder',
        rating=1834,
        max_rating=1912,
        rank='cyan',
        rated_matches=27,
        proof="King's College London KCPC-A1B2C3",
        url='https://atcoder.jp/users/FakeAtCoder',
    )


async def test_check_finds_the_token_in_the_codeforces_organization(
    service: AccountService, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder')
    await service.start_link(GUILD, MEMBER, CODEFORCES, 'fakecoder')
    codeforces.add('FakeCoder', organization='kcpc-a1b2c3')

    verified = await service.check(GUILD, MEMBER, CODEFORCES)

    assert verified == Profile(
        platform=CODEFORCES,
        handle='FakeCoder',
        rating=1700,
        max_rating=1800,
        rank='expert',
        rated_matches=None,
        proof='kcpc-a1b2c3',
        url='https://codeforces.com/profile/FakeCoder',
    )


@pytest.mark.parametrize(
    ('platform', 'message'),
    [
        (
            ATCODER,
            'You have no AtCoder account waiting to be verified. '
            'Start with /link atcoder <handle>.',
        ),
        (
            CODEFORCES,
            'You have no Codeforces account waiting to be verified. '
            'Start with /link codeforces <handle>.',
        ),
    ],
)
async def test_check_without_a_challenge_says_how_to_start(
    service: AccountService, platform: str, message: str
) -> None:
    with pytest.raises(KcpcUserError) as raised:
        await service.check(GUILD, MEMBER, platform)

    assert str(raised.value) == message


async def test_an_expired_token_says_how_to_get_a_new_one(
    service: AccountService, repo: AccountRepo, atcoder: FakeAtCoder, clock: FakeClock
) -> None:
    atcoder.add('Fake_AtCoder', affiliation='kcpc-a1b2c3')
    await service.start_link(GUILD, MEMBER, ATCODER, 'Fake_AtCoder')
    await clock.advance(10 * MINUTE)

    with pytest.raises(KcpcUserError) as raised:
        await service.check(GUILD, MEMBER, ATCODER)

    assert str(raised.value) == (
        r'Your token for Fake\_AtCoder has expired. '
        r'Get a new one with /link atcoder Fake\_AtCoder.'
    )
    assert atcoder.fetched == ['Fake_AtCoder']  # not fetched again


@pytest.mark.parametrize(
    ('platform', 'proof', 'message'),
    [
        (
            ATCODER,
            'kcpc-0b0b0b',  # a token this member wasn't given
            "The token kcpc-a1b2c3 isn't in the Affiliation of FakeUser yet. "
            'Put it there at <https://atcoder.jp/settings>, save, then verify '
            'again.',
        ),
        (
            CODEFORCES,
            None,
            "The token kcpc-a1b2c3 isn't in the Organization of FakeUser yet. "
            'Put it there at <https://codeforces.com/settings/social>, save, then '
            'verify again.',
        ),
    ],
)
async def test_a_profile_without_the_token_is_refused(
    service: AccountService,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    platform: str,
    proof: str | None,
    message: str,
) -> None:
    atcoder.add('FakeUser', affiliation=proof)
    codeforces.add('FakeUser', organization=proof)
    await service.start_link(GUILD, MEMBER, platform, 'FakeUser')

    with pytest.raises(KcpcUserError) as raised:
        await service.check(GUILD, MEMBER, platform)

    assert str(raised.value) == message


async def test_an_account_gone_since_the_start_cant_be_verified(
    service: AccountService, atcoder: FakeAtCoder
) -> None:
    atcoder.add('FakeAtCoder')
    await service.start_link(GUILD, MEMBER, ATCODER, 'FakeAtCoder')
    atcoder.profiles.clear()

    with pytest.raises(KcpcUserError) as raised:
        await service.check(GUILD, MEMBER, ATCODER)

    assert str(raised.value) == 'No AtCoder user called FakeAtCoder.'


@pytest.mark.parametrize(
    ('platform', 'name'), [(ATCODER, 'AtCoder'), (CODEFORCES, 'Codeforces')]
)
async def test_check_refuses_a_page_that_shows_another_account(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    platform: str,
    name: str,
) -> None:
    atcoder.add('FakeUser')
    codeforces.add('FakeUser')
    started = await service.start_link(GUILD, MEMBER, platform, 'FakeUser')
    # Asked for FakeUser, the site answers with another account, which shows
    # the token: it proves nothing about FakeUser.
    atcoder.profiles['fakeuser'] = atcoder.add('OtherUser', affiliation=started.token)
    codeforces.users['fakeuser'] = codeforces.add(
        'OtherUser', organization=started.token
    )

    with pytest.raises(ExternalServiceError) as raised:
        await service.check(GUILD, MEMBER, platform)

    assert str(raised.value) == (
        f'{name} showed a different account for FakeUser. Please try again later.'
    )
    assert await repo.get_challenge(GUILD, MEMBER, platform) == started


async def test_check_refuses_once_a_moderator_set_a_codeforces_handle(
    service: AccountService, codeforces: FakeCodeforces
) -> None:
    codeforces.add('FakeCoder', organization='kcpc-a1b2c3')
    await service.start_link(GUILD, MEMBER, CODEFORCES, 'FakeCoder')

    with pytest.raises(KcpcUserError, match='already set to FakeCoder'):
        await service.check(GUILD, MEMBER, CODEFORCES, current_handle='FakeCoder')


async def verified(
    service: AccountService,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    platform: str,
) -> VerifiedProfile:
    """Start linking FakeUser on ``platform``, show the token, and verify it."""
    atcoder.add('FakeUser')
    codeforces.add('FakeUser')
    started = await service.start_link(GUILD, MEMBER, platform, 'FakeUser')
    atcoder.add('FakeUser', affiliation=started.token)
    codeforces.add('FakeUser', organization=started.token)
    return await service.check(GUILD, MEMBER, platform)


async def test_complete_atcoder_links_and_saves_the_ratings_together(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    clock: FakeClock,
) -> None:
    profile = await verified(service, atcoder, codeforces, ATCODER)
    await clock.advance(MINUTE)

    account = await service.complete_atcoder(GUILD, MEMBER, profile)

    done = NOW + MINUTE
    assert account == LinkedAccount(
        GUILD, MEMBER, ATCODER, 'FakeUser', 'affiliation-token', done
    )
    assert await repo.get_link(GUILD, MEMBER, ATCODER) == account
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is None
    assert await repo.snapshot(ATCODER, 'fakeuser') == AccountSnapshot(
        ATCODER, 'FakeUser', 1834, 1912, 'cyan', 27, done
    )


async def test_complete_atcoder_changes_nothing_if_another_member_won_the_race(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> None:
    profile = await verified(service, atcoder, codeforces, ATCODER)
    await repo.link(atcoder_link('FakeUser'))

    with pytest.raises(HandleTaken):
        await service.complete_atcoder(GUILD, MEMBER, profile)

    assert await repo.get_link(GUILD, MEMBER, ATCODER) is None
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is not None
    assert await repo.snapshot(ATCODER, 'FakeUser') is None


async def test_complete_atcoder_takes_the_link_over_from_someone_who_left(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = await verified(service, atcoder, codeforces, ATCODER)
    await repo.link(atcoder_link('FakeUser'))  # OTHER_MEMBER's, who then left

    with caplog.at_level(logging.INFO, logger=service_module.__name__):
        account = await service.complete_atcoder(
            GUILD, MEMBER, profile, is_member=other_member_left
        )

    assert account.user_id == MEMBER
    assert await repo.links_for_guild(GUILD, ATCODER) == [account]
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is None
    assert await repo.snapshot(ATCODER, 'FakeUser') is not None
    assert caplog.messages == [
        f'Member {MEMBER} of guild {GUILD} proved the AtCoder account FakeUser, '
        f'so it is no longer linked to user {OTHER_MEMBER}, who left'
    ]


async def test_a_takeover_that_fails_midway_leaves_the_link_where_it_was(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = await verified(service, atcoder, codeforces, ATCODER)
    theirs = atcoder_link('FakeUser')
    await repo.link(theirs)

    async def fail(snapshots: Sequence[AccountSnapshot]) -> None:
        raise RuntimeError('disk full')

    monkeypatch.setattr(repo, 'save_snapshots', fail)

    with pytest.raises(RuntimeError, match='disk full'):
        await service.complete_atcoder(
            GUILD, MEMBER, profile, is_member=other_member_left
        )

    assert await repo.links_for_guild(GUILD, ATCODER) == [theirs]
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is not None


async def test_complete_codeforces_forgets_the_challenge_and_saves_the_ratings(
    service: AccountService,
    repo: AccountRepo,
    atcoder: FakeAtCoder,
    codeforces: FakeCodeforces,
) -> None:
    profile = await verified(service, atcoder, codeforces, CODEFORCES)

    await service.complete_codeforces(GUILD, MEMBER, profile)

    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) is None
    assert await repo.snapshot(CODEFORCES, 'FakeUser') == AccountSnapshot(
        CODEFORCES, 'FakeUser', 1700, 1800, 'expert', None, NOW
    )
    # Codeforces handles are linked in TLE's table, by the cog.
    assert await repo.links_for_user(GUILD, MEMBER) == []


async def test_completing_takes_only_its_own_platforms_profile(
    service: AccountService, atcoder: FakeAtCoder, codeforces: FakeCodeforces
) -> None:
    codeforces_profile = await verified(service, atcoder, codeforces, CODEFORCES)
    atcoder_profile = await verified(service, atcoder, codeforces, ATCODER)

    with pytest.raises(ValueError, match='Not an AtCoder profile'):
        await service.complete_atcoder(GUILD, MEMBER, codeforces_profile)
    with pytest.raises(ValueError, match='Not a Codeforces profile'):
        await service.complete_codeforces(GUILD, MEMBER, atcoder_profile)


async def test_unlink_atcoder_removes_the_link(
    service: AccountService, repo: AccountRepo
) -> None:
    link = atcoder_link('FakeAtCoder', MEMBER)
    await repo.link(link)

    assert await service.unlink_atcoder(GUILD, MEMBER) == link
    assert await repo.get_link(GUILD, MEMBER, ATCODER) is None
    with pytest.raises(KcpcUserError) as raised:
        await service.unlink_atcoder(GUILD, MEMBER)
    assert str(raised.value) == "You haven't linked an AtCoder account."


async def test_an_admin_unlinks_an_atcoder_account_whoever_linked_it(
    service: AccountService, repo: AccountRepo
) -> None:
    link = atcoder_link('Fake_AtCoder')  # another member's, who may have left
    await repo.link(link)

    assert await service.remove_atcoder_link(GUILD, ' fake_atcoder ') == link
    assert await repo.links_for_guild(GUILD, ATCODER) == []
    with pytest.raises(KcpcUserError) as raised:
        await service.remove_atcoder_link(GUILD, 'Fake_AtCoder')
    assert str(raised.value) == (
        r'Nobody in this server has linked the AtCoder account Fake\_AtCoder.'
    )


async def test_purging_deletes_only_expired_challenges(
    service: AccountService, repo: AccountRepo, clock: FakeClock
) -> None:
    await repo.save_challenge(challenge(ATCODER, 'Old', 'a1b2c3', at=NOW - HOUR))
    await repo.save_challenge(challenge(CODEFORCES, 'New', 'd4e5f6'))

    assert await service.purge_expired_challenges() == 1
    assert await repo.get_challenge(GUILD, MEMBER, ATCODER) is None
    assert await repo.get_challenge(GUILD, MEMBER, CODEFORCES) is not None


async def test_profile_accounts_lists_codeforces_then_atcoder_with_ratings(
    service: AccountService, repo: AccountRepo
) -> None:
    await repo.link(atcoder_link('FakeAtCoder', MEMBER))
    rated = snapshot(CODEFORCES, 'FakeCoder', 1700)
    await repo.save_snapshots([rated])

    accounts = await service.profile_accounts(GUILD, MEMBER, 'fakecoder')

    assert accounts == [
        ProfileAccount(CODEFORCES, 'fakecoder', rated),
        ProfileAccount(ATCODER, 'FakeAtCoder', None),
    ]
    assert [account.url for account in accounts] == [
        'https://codeforces.com/profile/fakecoder',
        'https://atcoder.jp/users/FakeAtCoder',
    ]
    assert await service.profile_accounts(GUILD, OTHER_MEMBER, None) == []


def test_ratings_are_stale_once_over_an_hour_old_or_missing() -> None:
    account = ProfileAccount(CODEFORCES, 'FakeCoder', snapshot(CODEFORCES, 'x', 1))

    assert not account.stale(NOW + HOUR)
    assert account.stale(NOW + HOUR + timedelta(seconds=1))
    assert ProfileAccount(CODEFORCES, 'FakeCoder', None).stale(NOW)


async def test_the_leaderboard_ranks_by_rating_with_unrated_members_last(
    service: AccountService, repo: AccountRepo
) -> None:
    await repo.save_snapshots(
        [
            snapshot(CODEFORCES, 'Middle', 1500),
            snapshot(CODEFORCES, 'TopB', 2000),
            snapshot(CODEFORCES, 'topa', 2000),
            snapshot(CODEFORCES, 'Unrated', None),
        ]
    )
    members = [
        (1, 'middle'),
        (2, 'Unfetched'),
        (3, 'Unrated'),
        (4, 'TopB'),
        (5, 'TopA'),
    ]

    standings = await service.leaderboard(CODEFORCES, members)

    # Equal ratings share a place, ties in order of handle.
    assert [(s.place, s.user_id, s.handle, s.rating) for s in standings] == [
        (1, 5, 'TopA', 2000),
        (1, 4, 'TopB', 2000),
        (3, 1, 'middle', 1500),
        (4, 2, 'Unfetched', None),
        (4, 3, 'Unrated', None),
    ]
    assert standings[3].snapshot is None
    assert await service.leaderboard(ATCODER, []) == []


async def test_linked_handles_are_the_guilds_links_on_the_platform(
    service: AccountService, repo: AccountRepo
) -> None:
    await repo.link(atcoder_link('FakeAtCoder', MEMBER))
    await repo.link(atcoder_link('anotherone', OTHER_MEMBER))

    assert await service.linked_handles(GUILD, ATCODER) == [
        (OTHER_MEMBER, 'anotherone'),
        (MEMBER, 'FakeAtCoder'),
    ]
    assert await service.linked_handles(OTHER_GUILD, ATCODER) == []


async def test_linked_handle_is_the_members_link_on_the_platform(
    service: AccountService, repo: AccountRepo
) -> None:
    # What other features read through KcpcServices.handles.
    await repo.link(atcoder_link('FakeAtCoder', MEMBER))
    await repo.link(atcoder_link('anotherone', OTHER_MEMBER))

    assert await service.linked_handle(GUILD, MEMBER, ATCODER) == 'FakeAtCoder'
    assert await service.linked_handle(GUILD, OTHER_MEMBER, ATCODER) == 'anotherone'
    assert await service.linked_handle(OTHER_GUILD, MEMBER, ATCODER) is None
    # Codeforces handles are in TLE's table, not kcpc.db.
    assert await service.linked_handle(GUILD, MEMBER, CODEFORCES) is None


def test_only_codeforces_and_atcoder_can_be_linked() -> None:
    assert link_platform(ATCODER).url_for('x_y') == 'https://atcoder.jp/users/x_y'
    with pytest.raises(ValueError, match="cannot be linked on 'leetcode'"):
        link_platform('leetcode')
