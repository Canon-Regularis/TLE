"""Tests for tle.kcpc.bot.codeforces_links: KCPC's way to TLE's Codeforces handles.

TLE's user database is a real in-memory one (the ``user_db`` fixture). Discord
is mocked, and Codeforces patched.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from tle.kcpc.bot.codeforces_links import (
    RankRoleRefused,
    check_rank_role,
    guild_handles,
    handle_holder,
    link,
    linked_handle,
)
from tle.kcpc.core.errors import ExternalServiceError, KcpcDisabledError, KcpcUserError
from tle.util import codeforces_api as cf
from tle.util.db.user_db_conn import UniqueConstraintFailed, UserDbConn

GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
MEMBER_ID = 1_200_000_000_000_000_001
OTHER_MEMBER_ID = 1_200_000_000_000_000_002
HANDLE = 'Fake_Coder'  # a made-up account, in Codeforces' case
LINK_REASON = 'Codeforces handle verified with /link'


def codeforces_user(rating: int | None) -> cf.User:
    """The made-up account, as cf.user.info returns it."""
    return cf.User(
        handle=HANDLE,
        firstName=None,
        lastName=None,
        country=None,
        city=None,
        organization=None,
        contribution=0,
        rating=rating,
        maxRating=rating,
        lastOnlineTimeSeconds=1_790_000_000,
        registrationTimeSeconds=1_600_000_000,
        friendOfCount=0,
        titlePhoto='https://userpic.codeforces.org/no-title.jpg',
    )


def make_role(name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role, name=name)  # the mock's name, for its repr
    role.name = name
    return role


@pytest.fixture(autouse=True)
def user_info(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """cf.user.info, answering with the made-up account, rated Expert."""
    info = AsyncMock(return_value=[codeforces_user(1700)])
    monkeypatch.setattr(cf.user, 'info', info)
    return info


@pytest.fixture
def bot(user_db: UserDbConn) -> MagicMock:
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db  # as TLE attaches it at startup
    return bot


@pytest.fixture
def roles() -> dict[str, MagicMock]:
    """The guild's rank roles, by name."""
    return {rank.title: make_role(rank.title) for rank in cf.RATED_RANKS}


@pytest.fixture
def guild(roles: dict[str, MagicMock]) -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.roles = list(roles.values())
    return guild


@pytest.fixture
def member(guild: MagicMock) -> MagicMock:
    """A member without roles; adding and removing roles updates ``member.roles``."""
    member = MagicMock(spec=discord.Member, id=MEMBER_ID, guild=guild)
    member.roles = []

    async def add_roles(*added: MagicMock, reason: str | None = None) -> None:
        member.roles = [*member.roles, *added]

    async def remove_roles(*removed: MagicMock, reason: str | None = None) -> None:
        member.roles = [role for role in member.roles if role not in removed]

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    return member


class TestLinkedHandle:
    async def test_is_the_members_handle_in_that_guild(
        self, bot: MagicMock, user_db: UserDbConn
    ) -> None:
        await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)

        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) == HANDLE
        assert await linked_handle(bot, GUILD_ID, OTHER_MEMBER_ID) is None
        assert await linked_handle(bot, OTHER_GUILD_ID, MEMBER_ID) is None


class TestGuildHandles:
    async def test_are_the_handles_of_the_guilds_active_members(
        self, bot: MagicMock, user_db: UserDbConn
    ) -> None:
        await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, 'Other_Coder')
        await user_db.set_handle(MEMBER_ID, OTHER_GUILD_ID, 'Elsewhere_Coder')

        assert sorted(await guild_handles(bot, GUILD_ID)) == [
            (MEMBER_ID, HANDLE),
            (OTHER_MEMBER_ID, 'Other_Coder'),
        ]

        # TLE marks a member who left inactive.
        await user_db.set_inactive([(str(GUILD_ID), str(OTHER_MEMBER_ID))])
        assert await guild_handles(bot, GUILD_ID) == [(MEMBER_ID, HANDLE)]

    async def test_a_guild_without_handles_has_none(self, bot: MagicMock) -> None:
        assert await guild_handles(bot, GUILD_ID) == []


class TestHandleHolder:
    async def test_is_whoever_has_the_handle_in_any_case_even_after_leaving(
        self, bot: MagicMock, user_db: UserDbConn
    ) -> None:
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        assert await handle_holder(bot, GUILD_ID, ' fake_coder ') == OTHER_MEMBER_ID

        # TLE marks a member who left inactive, but keeps their handle and won't
        # link it to anyone else.
        await user_db.set_inactive([(str(GUILD_ID), str(OTHER_MEMBER_ID))])
        assert await handle_holder(bot, GUILD_ID, 'FAKE_CODER') == OTHER_MEMBER_ID
        with pytest.raises(UniqueConstraintFailed):
            await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)

    async def test_is_none_for_a_free_handle_or_in_another_guild(
        self, bot: MagicMock, user_db: UserDbConn
    ) -> None:
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        assert await handle_holder(bot, GUILD_ID, 'Other_Coder') is None
        assert await handle_holder(bot, OTHER_GUILD_ID, HANDLE) is None


class TestCheckRankRole:
    def test_passes_if_the_guild_has_the_rank_role(self, guild: MagicMock) -> None:
        check_rank_role(guild, 1700)

    def test_an_unrated_account_needs_no_role(self, guild: MagicMock) -> None:
        guild.roles = []

        check_rank_role(guild, None)

    def test_a_missing_rank_role_is_the_user_error_link_raises(
        self, guild: MagicMock
    ) -> None:
        guild.roles = [role for role in guild.roles if role.name != 'Expert']

        with pytest.raises(KcpcUserError) as excinfo:
            check_rank_role(guild, 1700)

        assert type(excinfo.value) is KcpcUserError
        assert str(excinfo.value) == (
            'Role for rank `Expert` not present in the server.'
        )
        check_rank_role(guild, 1900)  # Candidate Master's role is still there


class TestLink:
    async def test_links_the_canonical_handle_for_tle_with_the_rank_role(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        roles: dict[str, MagicMock],
        user_db: UserDbConn,
        user_info: AsyncMock,
    ) -> None:
        await link(bot, guild, member, 'fake_coder')

        user_info.assert_awaited_once_with(handles=['fake_coder'])
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) == HANDLE
        assert await user_db.fetch_cf_user(HANDLE) == codeforces_user(1700)
        assert member.roles == [roles['Expert']]
        member.add_roles.assert_awaited_once_with(roles['Expert'], reason=LINK_REASON)

    async def test_another_members_handle_is_a_user_error(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_db: UserDbConn,
    ) -> None:
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        with pytest.raises(KcpcUserError) as excinfo:
            await link(bot, guild, member, HANDLE)

        assert str(excinfo.value) == (
            f'The handle `{HANDLE}` is already associated with another user.'
        )
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) is None
        member.add_roles.assert_not_awaited()

    async def test_a_missing_rank_role_is_a_user_error_and_links_nothing(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_db: UserDbConn,
    ) -> None:
        guild.roles = [role for role in guild.roles if role.name != 'Expert']

        with pytest.raises(KcpcUserError) as excinfo:
            await link(bot, guild, member, HANDLE)

        assert str(excinfo.value) == (
            'Role for rank `Expert` not present in the server.'
        )
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) is None
        assert await user_db.fetch_cf_user(HANDLE) is None
        member.add_roles.assert_not_awaited()

    async def test_a_refused_role_change_is_a_user_error_and_keeps_the_link(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        refusal = discord.Forbidden(
            MagicMock(status=403, reason='Forbidden'), 'Missing Permissions'
        )
        member.add_roles.side_effect = refusal
        caplog.set_level(logging.INFO, logger='tle.kcpc.bot.codeforces_links')

        with pytest.raises(RankRoleRefused) as excinfo:
            await link(bot, guild, member, HANDLE)

        # A user error, so KCPC shows the member its message.
        assert isinstance(excinfo.value, KcpcUserError)
        assert str(excinfo.value) == (
            r'Your Codeforces account Fake\_Coder is linked, but Discord '
            "didn't let me change your rank roles. Ask an admin to check that I "
            'have the Manage Roles permission and that my highest role is above '
            'the rank roles.'
        )
        assert excinfo.value.__cause__ is refusal
        # Discord refused after the handle was stored, and it stays linked.
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) == HANDLE
        (record,) = [
            record
            for record in caplog.records
            if record.name == 'tle.kcpc.bot.codeforces_links'
        ]
        assert record.levelno == logging.INFO
        assert record.getMessage() == (
            f'Discord refused to change the rank roles of member {MEMBER_ID} '
            f'in guild {GUILD_ID}: Missing Permissions'
        )

    async def test_an_unrated_account_links_without_a_rank_role(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_info: AsyncMock,
    ) -> None:
        user_info.return_value = [codeforces_user(None)]
        guild.roles = []

        await link(bot, guild, member, HANDLE)

        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) == HANDLE
        member.add_roles.assert_not_awaited()

    async def test_an_unknown_handle_is_a_user_error(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_info: AsyncMock,
    ) -> None:
        user_info.side_effect = cf.HandleNotFoundError(
            'handles: User with handle no_such_coder not found', 'no_such_coder'
        )

        with pytest.raises(KcpcUserError) as excinfo:
            await link(bot, guild, member, 'no_such_coder')

        assert type(excinfo.value) is KcpcUserError
        assert str(excinfo.value) == 'No Codeforces user called no_such_coder.'
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) is None

    @pytest.mark.parametrize(
        'error',
        [
            cf.ClientError(),
            cf.CallLimitExceededError('Call limit exceeded'),
            cf.TrueApiError('Internal Server Error'),
            # TLE's client doesn't catch its session's timeout.
            asyncio.TimeoutError(),
        ],
        ids=['unreachable', 'call-limit', 'api-error', 'timeout'],
    )
    async def test_codeforces_failing_is_an_external_service_error(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_info: AsyncMock,
        error: Exception,
    ) -> None:
        user_info.side_effect = error

        with pytest.raises(ExternalServiceError) as excinfo:
            await link(bot, guild, member, HANDLE)

        assert excinfo.value.service == 'Codeforces'
        assert str(excinfo.value) == (
            'Codeforces is not responding right now. Please try again later.'
        )
        assert excinfo.value.__cause__ is error
        assert await linked_handle(bot, GUILD_ID, MEMBER_ID) is None


class TestWithoutTlesDatabase:
    @pytest.fixture
    def bot(self) -> MagicMock:
        return MagicMock(spec=commands.Bot)  # TLE attached no user_db

    async def test_kcpc_is_unavailable_and_codeforces_is_not_asked(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_info: AsyncMock,
    ) -> None:
        with pytest.raises(KcpcDisabledError):
            await linked_handle(bot, GUILD_ID, MEMBER_ID)
        with pytest.raises(KcpcDisabledError):
            await guild_handles(bot, GUILD_ID)
        with pytest.raises(KcpcDisabledError):
            await handle_holder(bot, GUILD_ID, HANDLE)
        with pytest.raises(KcpcDisabledError):
            await link(bot, guild, member, HANDLE)

        user_info.assert_not_awaited()
