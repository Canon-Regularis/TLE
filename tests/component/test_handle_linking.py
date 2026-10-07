"""Tests for tle.util.handle_linking, and for TLE's callers: Handles and OAuth.

Handles are linked in a real in-memory user database (the ``user_db``
fixture). Discord's guilds, members and roles are mocks, and Codeforces is
patched, so no test reaches it.
"""

import datetime as dt
import html
import logging
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aiohttp.test_utils import make_mocked_request
from discord.ext import commands

from tle import constants
from tle.util import codeforces_api as cf, handle_linking, oauth
from tle.util.db.user_db_conn import UserDbConn
from tle.util.handle_linking import HandleLinkError, HandleTakenError

GUILD_ID = 1_100_000_000_000_000_001
MEMBER_ID = 1_200_000_000_000_000_001
OTHER_MEMBER_ID = 1_200_000_000_000_000_002
TRUSTED_ROLE_ID = 1_300_000_000_000_000_001
HANDLE = 'Fake_Coder'  # a made-up account, in Codeforces' case
EXPERT = 1700
LOGGER_NAME = 'test.handle_linking'
# Members rated 1900+ before this time (o1's release) become Trusted.
TRUSTED_CUTOFF = dt.datetime(2024, 9, 11, tzinfo=dt.timezone.utc)
TRUSTED_REASON = 'Historical rating >= 1900 before Aug 2024'


def codeforces_user(rating: int | None = EXPERT) -> cf.User:
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


def rating_change(new_rating: int, when: dt.datetime) -> cf.RatingChange:
    return cf.RatingChange(
        contestId=2000,
        contestName='Codeforces Round 2000',
        handle=HANDLE,
        rank=100,
        ratingUpdateTimeSeconds=int(when.timestamp()),
        oldRating=new_rating - 50,
        newRating=new_rating,
    )


def make_role(name: str) -> MagicMock:
    role = MagicMock(spec=discord.Role, name=name)  # the mock's name, for its repr
    role.name = name
    return role


def make_member(
    guild: MagicMock, *roles: MagicMock, member_id: int = MEMBER_ID
) -> MagicMock:
    """A member of ``guild`` with ``roles``; adding and removing roles updates them."""
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild)
    member.mention = f'<@{member_id}>'
    member.display_name = 'Test Member'
    member.roles = list(roles)

    async def add_roles(*added: MagicMock, reason: str | None = None) -> None:
        member.roles = [*member.roles, *added]

    async def remove_roles(*removed: MagicMock, reason: str | None = None) -> None:
        member.roles = [role for role in member.roles if role not in removed]

    member.add_roles = AsyncMock(side_effect=add_roles)
    member.remove_roles = AsyncMock(side_effect=remove_roles)
    return member


def remove_guild_role(guild: MagicMock, name: str) -> None:
    guild.roles = [role for role in guild.roles if role.name != name]


async def handle_of(user_db: UserDbConn, member_id: int = MEMBER_ID) -> str | None:
    return await user_db.get_handle(member_id, GUILD_ID)


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == LOGGER_NAME
    ]


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by their default names, whatever the environment says."""
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_PURGATORY', 'Purgatory')


@pytest.fixture(autouse=True)
def user_info(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """cf.user.info, answering with the made-up Expert."""
    info = AsyncMock(return_value=[codeforces_user()])
    monkeypatch.setattr(cf.user, 'info', info)
    return info


@pytest.fixture(autouse=True)
def rating_history(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """cf.user.rating, answering with no rating changes."""
    rating = AsyncMock(return_value=[])
    monkeypatch.setattr(cf.user, 'rating', rating)
    return rating


@pytest.fixture
def roles() -> dict[str, MagicMock]:
    """The guild's roles by name: one per rank, TLE's own, and an unrelated one."""
    names = [rank.title for rank in cf.RATED_RANKS]
    names += ['Trusted', 'Purgatory', 'Workshops']
    return {name: make_role(name) for name in names}


@pytest.fixture
def guild(roles: dict[str, MagicMock]) -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.name = 'Test Server'
    guild.roles = list(roles.values())
    return guild


@pytest.fixture
def member(guild: MagicMock) -> MagicMock:
    return make_member(guild)


@pytest.fixture
def log() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


class TestRankRoleFor:
    def test_an_unrated_user_has_no_role(self, guild: MagicMock) -> None:
        guild.roles = []
        assert handle_linking.rank_role_for(guild, codeforces_user(None)) is None

    @pytest.mark.parametrize(
        ('rating', 'title'),
        [(1199, 'Newbie'), (1700, 'Expert'), (3000, 'Legendary Grandmaster')],
    )
    def test_a_rated_user_has_the_role_named_after_the_rank(
        self, guild: MagicMock, roles: dict[str, MagicMock], rating: int, title: str
    ) -> None:
        role = handle_linking.rank_role_for(guild, codeforces_user(rating))
        assert role is roles[title]

    def test_a_missing_role_is_an_error(self, guild: MagicMock) -> None:
        remove_guild_role(guild, 'Expert')
        with pytest.raises(HandleLinkError) as excinfo:
            handle_linking.rank_role_for(guild, codeforces_user(EXPERT))
        # A sentence, as OAuth's reply quotes it within one of its own.
        assert str(excinfo.value) == (
            'Role for rank `Expert` not present in the server.'
        )


class TestLinkHandle:
    async def test_links_caches_and_gives_the_rank_role(
        self,
        user_db: UserDbConn,
        guild: MagicMock,
        roles: dict[str, MagicMock],
        member: MagicMock,
    ) -> None:
        user = codeforces_user(EXPERT)

        await handle_linking.link_handle(user_db, guild, member, user)

        assert await handle_of(user_db) == HANDLE
        assert await user_db.fetch_cf_user(HANDLE) == user
        assert member.roles == [roles['Expert']]
        member.add_roles.assert_awaited_once_with(
            roles['Expert'], reason='New handle set for user'
        )

    async def test_a_missing_rank_role_changes_nothing(
        self,
        user_db: UserDbConn,
        guild: MagicMock,
        roles: dict[str, MagicMock],
    ) -> None:
        # The handle used to be stored before the role was looked up, leaving
        # the member linked without a role.
        member = make_member(guild, roles['Pupil'])
        remove_guild_role(guild, 'Expert')

        with pytest.raises(HandleLinkError, match='Role for rank `Expert`'):
            await handle_linking.link_handle(
                user_db, guild, member, codeforces_user(EXPERT)
            )

        assert await handle_of(user_db) is None
        assert await user_db.fetch_cf_user(HANDLE) is None
        assert member.roles == [roles['Pupil']]
        member.remove_roles.assert_not_awaited()

    async def test_another_members_handle_is_refused(
        self, user_db: UserDbConn, guild: MagicMock, member: MagicMock
    ) -> None:
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        with pytest.raises(HandleTakenError) as excinfo:
            await handle_linking.link_handle(user_db, guild, member, codeforces_user())

        assert isinstance(excinfo.value, HandleLinkError)
        assert str(excinfo.value) == (
            f'The handle `{HANDLE}` is already associated with another user.'
        )
        assert await handle_of(user_db) is None
        assert await handle_of(user_db, OTHER_MEMBER_ID) == HANDLE
        assert await user_db.fetch_cf_user(HANDLE) is None
        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()

    async def test_an_unrated_user_loses_every_rank_role(
        self, user_db: UserDbConn, guild: MagicMock, roles: dict[str, MagicMock]
    ) -> None:
        member = make_member(guild, roles['Pupil'], roles['Workshops'])

        await handle_linking.link_handle(
            user_db, guild, member, codeforces_user(None), reason='Handle relinked'
        )

        assert await handle_of(user_db) == HANDLE
        assert member.roles == [roles['Workshops']]
        member.remove_roles.assert_awaited_once_with(
            roles['Pupil'], reason='Handle relinked'
        )
        member.add_roles.assert_not_awaited()

    async def test_the_members_old_handle_is_replaced(
        self, user_db: UserDbConn, guild: MagicMock, member: MagicMock
    ) -> None:
        await user_db.set_handle(MEMBER_ID, GUILD_ID, 'Old_Handle')

        await handle_linking.link_handle(user_db, guild, member, codeforces_user())

        assert await handle_of(user_db) == HANDLE


class TestUpdateMemberRankRole:
    async def test_the_new_rank_role_replaces_the_others(
        self, user_db: UserDbConn, guild: MagicMock, roles: dict[str, MagicMock]
    ) -> None:
        member = make_member(
            guild, roles['Pupil'], roles['Purgatory'], roles['Specialist']
        )

        await handle_linking.update_member_rank_role(
            member, roles['Expert'], reason='Rank update', user_db=user_db
        )

        # Below Candidate Master, Purgatory stays.
        assert member.roles == [roles['Purgatory'], roles['Expert']]
        member.remove_roles.assert_awaited_once_with(
            roles['Pupil'], roles['Specialist'], reason='Rank update'
        )
        member.add_roles.assert_awaited_once_with(roles['Expert'], reason='Rank update')

    async def test_candidate_master_and_above_leave_purgatory(
        self,
        user_db: UserDbConn,
        guild: MagicMock,
        roles: dict[str, MagicMock],
        rating_history: AsyncMock,
    ) -> None:
        await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)
        member = make_member(guild, roles['Expert'], roles['Purgatory'])

        await handle_linking.update_member_rank_role(
            member, roles['Candidate Master'], reason='Rank update', user_db=user_db
        )

        assert member.roles == [roles['Candidate Master']]
        rating_history.assert_awaited_once_with(handle=HANDLE)  # the Trusted check

    async def test_no_role_removes_every_rank_role(
        self,
        user_db: UserDbConn,
        guild: MagicMock,
        roles: dict[str, MagicMock],
        rating_history: AsyncMock,
    ) -> None:
        member = make_member(guild, roles['Master'], roles['Purgatory'])

        await handle_linking.update_member_rank_role(
            member, None, reason='Handle unlinked', user_db=user_db
        )

        assert member.roles == [roles['Purgatory']]
        member.add_roles.assert_not_awaited()
        rating_history.assert_not_awaited()

    async def test_a_rank_role_already_held_is_left_alone(
        self, user_db: UserDbConn, guild: MagicMock, roles: dict[str, MagicMock]
    ) -> None:
        member = make_member(guild, roles['Expert'])

        await handle_linking.update_member_rank_role(
            member, roles['Expert'], reason='Rank update', user_db=user_db
        )

        member.add_roles.assert_not_awaited()
        member.remove_roles.assert_not_awaited()


class TestMaybeAddTrustedRole:
    @pytest.fixture
    async def linked_member(self, user_db: UserDbConn, guild: MagicMock) -> MagicMock:
        await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)
        return make_member(guild)

    async def test_1900_before_the_cutoff_makes_a_member_trusted(
        self,
        user_db: UserDbConn,
        roles: dict[str, MagicMock],
        linked_member: MagicMock,
        rating_history: AsyncMock,
    ) -> None:
        rating_history.return_value = [
            rating_change(1850, TRUSTED_CUTOFF - dt.timedelta(days=30)),
            rating_change(1900, TRUSTED_CUTOFF - dt.timedelta(seconds=1)),
        ]

        await handle_linking.maybe_add_trusted_role(linked_member, user_db=user_db)

        rating_history.assert_awaited_once_with(handle=HANDLE)
        linked_member.add_roles.assert_awaited_once_with(
            roles['Trusted'], reason=TRUSTED_REASON
        )

    @pytest.mark.parametrize(
        'change',
        [
            rating_change(1899, TRUSTED_CUTOFF - dt.timedelta(days=1)),
            rating_change(2400, TRUSTED_CUTOFF),
        ],
        ids=['below-1900', 'at-the-cutoff'],
    )
    async def test_otherwise_a_member_is_not_trusted(
        self,
        user_db: UserDbConn,
        linked_member: MagicMock,
        rating_history: AsyncMock,
        change: cf.RatingChange,
    ) -> None:
        rating_history.return_value = [change]

        await handle_linking.maybe_add_trusted_role(linked_member, user_db=user_db)

        linked_member.add_roles.assert_not_awaited()

    async def test_the_trusted_role_may_be_set_by_its_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        user_db: UserDbConn,
        guild: MagicMock,
        linked_member: MagicMock,
        rating_history: AsyncMock,
    ) -> None:
        # TLE_TRUSTED holds the role's id, and the role has another name.
        veterans = make_role('Veterans')
        guild.roles = [*guild.roles, veterans]
        guild.get_role.side_effect = {TRUSTED_ROLE_ID: veterans}.get
        monkeypatch.setattr(constants, 'TLE_TRUSTED', TRUSTED_ROLE_ID)
        rating_history.return_value = [
            rating_change(1900, TRUSTED_CUTOFF - dt.timedelta(days=1))
        ]

        await handle_linking.maybe_add_trusted_role(linked_member, user_db=user_db)

        linked_member.add_roles.assert_awaited_once_with(
            veterans, reason=TRUSTED_REASON
        )

    async def test_a_trusted_member_is_not_checked_again(
        self,
        user_db: UserDbConn,
        roles: dict[str, MagicMock],
        linked_member: MagicMock,
        rating_history: AsyncMock,
    ) -> None:
        linked_member.roles = [roles['Trusted']]

        await handle_linking.maybe_add_trusted_role(linked_member, user_db=user_db)

        rating_history.assert_not_awaited()
        linked_member.add_roles.assert_not_awaited()

    async def test_a_member_without_a_handle_is_skipped_with_a_warning(
        self,
        user_db: UserDbConn,
        member: MagicMock,
        rating_history: AsyncMock,
        log: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await handle_linking.maybe_add_trusted_role(
                member, user_db=user_db, log=log
            )

        assert logged(caplog) == [
            (
                logging.WARNING,
                f'WARN: handle not found in guild Test Server ({GUILD_ID})',
            )
        ]
        rating_history.assert_not_awaited()

    async def test_a_guild_without_the_trusted_role_is_skipped_with_a_warning(
        self,
        user_db: UserDbConn,
        guild: MagicMock,
        linked_member: MagicMock,
        rating_history: AsyncMock,
        log: logging.Logger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        remove_guild_role(guild, 'Trusted')

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await handle_linking.maybe_add_trusted_role(
                linked_member, user_db=user_db, log=log
            )

        assert logged(caplog) == [
            (
                logging.WARNING,
                f"WARN: 'Trusted' role not found in guild Test Server ({GUILD_ID})",
            )
        ]
        rating_history.assert_not_awaited()

    @pytest.mark.parametrize(
        ('error', 'level', 'message'),
        [
            (
                cf.HandleNotFoundError('handles: User not found', HANDLE),
                logging.INFO,
                f'INFO: Rating history not found for handle {HANDLE}'
                ' during trusted check.',
            ),
            (
                cf.ClientError(),
                logging.WARNING,
                f'WARN: API Error fetching rating for {HANDLE} during trusted'
                ' check: Error connecting to Codeforces API',
            ),
        ],
        ids=['unknown-handle', 'codeforces-down'],
    )
    async def test_codeforces_errors_are_logged_not_raised(
        self,
        user_db: UserDbConn,
        linked_member: MagicMock,
        rating_history: AsyncMock,
        log: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        error: cf.CodeforcesApiError,
        level: int,
        message: str,
    ) -> None:
        rating_history.side_effect = error

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await handle_linking.maybe_add_trusted_role(
                linked_member, user_db=user_db, log=log
            )

        assert logged(caplog) == [(level, message)]
        linked_member.add_roles.assert_not_awaited()

    @pytest.mark.parametrize(
        ('error', 'message'),
        [
            (
                discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'no'),
                'WARN: Missing permissions to add Trusted role to Test Member'
                ' in Test Server',
            ),
            (
                discord.HTTPException(MagicMock(status=500, reason='Error'), 'oops'),
                'WARN: Failed to add Trusted role to Test Member in Test Server:'
                ' 500 Error (error code: 0): oops',
            ),
        ],
        ids=['forbidden', 'http-error'],
    )
    async def test_discord_refusing_the_role_is_logged_not_raised(
        self,
        user_db: UserDbConn,
        linked_member: MagicMock,
        rating_history: AsyncMock,
        log: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        error: discord.HTTPException,
        message: str,
    ) -> None:
        rating_history.return_value = [
            rating_change(2000, TRUSTED_CUTOFF - dt.timedelta(days=1))
        ]
        linked_member.add_roles.side_effect = error

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            await handle_linking.maybe_add_trusted_role(
                linked_member, user_db=user_db, log=log
            )

        assert logged(caplog) == [(logging.WARNING, message)]


@pytest.fixture
def handles() -> ModuleType:
    """tle.cogs.handles, which draws with cairo and Pango through gi.

    Docker and CI have them; a bare virtualenv may not, so these tests skip there.
    """
    pytest.importorskip('gi')
    from tle.cogs import handles

    return handles


@pytest.fixture
def cog(handles: ModuleType, user_db: UserDbConn) -> Any:
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    return handles.Handles(bot)


@pytest.fixture
def ctx(guild: MagicMock) -> MagicMock:
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.send = AsyncMock()
    return ctx


class TestHandlesCog:
    async def test_rank_role_updates_delegate_with_the_cogs_logger(
        self,
        cog: Any,
        member: MagicMock,
        roles: dict[str, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(handle_linking, 'update_member_rank_role', update)

        await cog.update_member_rank_role(
            member, roles['Expert'], reason='Codeforces rank update'
        )

        update.assert_awaited_once_with(
            member,
            roles['Expert'],
            reason='Codeforces rank update',
            user_db=cog.bot.user_db,
            log=cog.logger,
        )

    async def test_the_trusted_check_delegates_with_the_cogs_logger(
        self, cog: Any, member: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        check = AsyncMock()
        monkeypatch.setattr(handle_linking, 'maybe_add_trusted_role', check)

        await cog.maybe_add_trusted_role(member)

        check.assert_awaited_once_with(member, user_db=cog.bot.user_db, log=cog.logger)

    async def test_handle_set_links_the_member(
        self,
        cog: Any,
        ctx: MagicMock,
        member: MagicMock,
        roles: dict[str, MagicMock],
        user_db: UserDbConn,
        user_info: AsyncMock,
    ) -> None:
        await cog.set.callback(cog, ctx, member, 'fake_coder')

        user_info.assert_awaited_once_with(handles=['fake_coder'])
        assert await handle_of(user_db) == HANDLE
        assert member.roles == [roles['Expert']]
        embed = ctx.send.await_args.kwargs['embed']
        assert embed.description == (
            f'Handle for {member.mention} successfully set to'
            f' **[{HANDLE}](https://codeforces.com/profile/{HANDLE})**'
        )

    async def test_handle_set_with_a_missing_rank_role_links_nothing(
        self,
        handles: ModuleType,
        cog: Any,
        ctx: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        user_db: UserDbConn,
    ) -> None:
        remove_guild_role(guild, 'Expert')

        with pytest.raises(handles.HandleCogError) as excinfo:
            await cog.set.callback(cog, ctx, member, 'fake_coder')

        assert str(excinfo.value) == (
            'Role for rank `Expert` not present in the server.'
        )
        assert await handle_of(user_db) is None
        assert await user_db.fetch_cf_user(HANDLE) is None
        ctx.send.assert_not_awaited()

    async def test_handle_set_refuses_another_members_handle_as_before(
        self,
        handles: ModuleType,
        cog: Any,
        ctx: MagicMock,
        member: MagicMock,
        user_db: UserDbConn,
    ) -> None:
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        with pytest.raises(handles.HandleCogError) as excinfo:
            await cog.set.callback(cog, ctx, member, 'fake_coder')

        assert str(excinfo.value) == (
            f'When setting handle for {member}: '
            f'The handle `{HANDLE}` is already associated with another user.'
        )
        assert await handle_of(user_db) is None
        ctx.send.assert_not_awaited()

    async def test_handle_remove_unlinks_a_member_and_takes_their_rank_role(
        self,
        cog: Any,
        ctx: MagicMock,
        guild: MagicMock,
        roles: dict[str, MagicMock],
        user_db: UserDbConn,
    ) -> None:
        linked = make_member(guild, roles['Expert'], roles['Workshops'])
        guild.get_member.side_effect = {MEMBER_ID: linked}.get
        await user_db.set_handle(MEMBER_ID, GUILD_ID, HANDLE)

        await cog.remove.callback(cog, ctx, HANDLE)

        assert await handle_of(user_db) is None
        assert linked.roles == [roles['Workshops']]
        embed = ctx.send.await_args.kwargs['embed']
        assert embed.description == f'Unlinked `{HANDLE}`.'

    async def test_handle_remove_frees_the_handle_of_a_member_who_left(
        self, cog: Any, ctx: MagicMock, guild: MagicMock, user_db: UserDbConn
    ) -> None:
        # TLE keeps the handle of a member who leaves, marked inactive, and
        # /link codeforces sends whoever owns the account here to free it.
        await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)
        await user_db.set_inactive([(str(GUILD_ID), str(OTHER_MEMBER_ID))])
        guild.get_member.return_value = None  # they left

        await cog.remove.callback(cog, ctx, HANDLE)

        assert await user_db.get_user_id(HANDLE, GUILD_ID) is None
        guild.get_member.assert_called_once_with(OTHER_MEMBER_ID)
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs['embed']
        assert embed.description == f'Unlinked `{HANDLE}`.'


@pytest.fixture
def oauth_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth configured, with Codeforces' token endpoint answering for HANDLE."""
    monkeypatch.setattr(constants, 'OAUTH_CLIENT_ID', 'test-client')
    monkeypatch.setattr(constants, 'OAUTH_CLIENT_SECRET', 'test-secret')
    monkeypatch.setattr(constants, 'OAUTH_REDIRECT_URI', 'https://bot.test/callback')
    tokens = {'id_token': 'test-id-token'}
    monkeypatch.setattr(oauth, 'exchange_code', AsyncMock(return_value=tokens))
    claims = {'handle': HANDLE.lower()}
    monkeypatch.setattr(oauth, 'decode_id_token', MagicMock(return_value=claims))


@pytest.fixture
def bot(user_db: UserDbConn, guild: MagicMock, member: MagicMock) -> MagicMock:
    """A bot without TLE's Handles cog, in whose guild ``member`` is.

    Direct messages to the member are recorded.
    """
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    bot.get_guild.return_value = guild
    guild.get_member.return_value = member
    bot.get_cog.return_value = None
    bot.get_user.return_value = member
    member.send = AsyncMock()
    return bot


@pytest.fixture
def interaction() -> MagicMock:
    """The interaction of a /handle identify, whose followups are recorded."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.followup = MagicMock(spec=discord.Webhook)
    interaction.followup.send = AsyncMock()
    return interaction


async def oauth_callback(
    bot: MagicMock, interaction: discord.Interaction | None = None
) -> Any:
    """Codeforces redirecting the member back, after they authorized the bot.

    The member signed in with /handle identify, whose ``interaction`` is
    kept, or without one with ;handle identify.
    """
    store = oauth.OAuthStateStore()
    state = store.create(MEMBER_ID, GUILD_ID, interaction=interaction)
    server = oauth.OAuthServer(bot, store, port=0)
    server._session = MagicMock()  # what start() opens, used only by exchange_code
    request = make_mocked_request('GET', f'/callback?state={state}&code=test-code')
    return await server._handle_callback(request)


def oauth_logs(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == oauth.__name__
    ]


LINKED = (
    f'Handle for <@{MEMBER_ID}> successfully set to'
    f' **[{HANDLE}](https://codeforces.com/profile/{HANDLE})**'
)
NOT_TOLD = f'Could not tell user {MEMBER_ID} how linking their Codeforces account went'


# The success page names the linked account with the profile embed of the
# Handles cog, which needs gi.
@pytest.mark.usefixtures('oauth_flow', 'handles')
class TestOAuthCallback:
    async def test_links_through_handle_linking(
        self,
        bot: MagicMock,
        member: MagicMock,
        roles: dict[str, MagicMock],
        user_db: UserDbConn,
        user_info: AsyncMock,
    ) -> None:
        response = await oauth_callback(bot)

        assert response.text == oauth._SUCCESS_HTML
        user_info.assert_awaited_once_with(handles=[HANDLE.lower()])
        assert await handle_of(user_db) == HANDLE
        assert member.roles == [roles['Expert']]
        bot.get_cog.assert_not_called()

    async def test_after_slash_identify_only_the_member_is_told_it_worked(
        self, bot: MagicMock, member: MagicMock, interaction: MagicMock
    ) -> None:
        await oauth_callback(bot, interaction)

        interaction.followup.send.assert_awaited_once()
        sent = interaction.followup.send.await_args.kwargs
        assert sent['ephemeral'] is True
        assert sent['embed'].description == LINKED
        member.send.assert_not_awaited()
        bot.get_channel.assert_not_called()

    async def test_after_prefix_identify_the_member_is_told_by_direct_message(
        self, bot: MagicMock, member: MagicMock
    ) -> None:
        await oauth_callback(bot)

        bot.get_user.assert_called_once_with(MEMBER_ID)
        member.send.assert_awaited_once()
        assert member.send.await_args.kwargs['embed'].description == LINKED
        bot.get_channel.assert_not_called()

    @pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
    @pytest.mark.parametrize(
        ('cause', 'reason'),
        [
            ('no rank role', 'Role for rank `Expert` not present in the server.'),
            (
                'handle taken',
                f'The handle `{HANDLE}` is already associated with another user.',
            ),
        ],
    )
    async def test_a_link_refused_for_a_reason_tells_the_member_why_privately(
        self,
        bot: MagicMock,
        guild: MagicMock,
        member: MagicMock,
        interaction: MagicMock,
        user_db: UserDbConn,
        caplog: pytest.LogCaptureFixture,
        slash: bool,
        cause: str,
        reason: str,
    ) -> None:
        # Trying again fails the same way, so the member used to be sent round
        # in circles: the reason is what a moderator needs to sort it out.
        if cause == 'no rank role':
            remove_guild_role(guild, 'Expert')
        else:
            # Such as a member who left: TLE keeps their handle.
            await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, HANDLE)

        with caplog.at_level(logging.INFO, logger=oauth.__name__):
            response = await oauth_callback(bot, interaction if slash else None)

        assert await handle_of(user_db) is None
        send = interaction.followup.send if slash else member.send
        send.assert_awaited_once()
        told = (
            f"I couldn't link your Codeforces account: {reason} Ask a moderator "
            'to sort it out.'
        )
        assert send.await_args.kwargs['embed'].description == told
        if slash:
            assert send.await_args.kwargs['ephemeral'] is True
            member.send.assert_not_awaited()
        bot.get_channel.assert_not_called()
        # The page says it too, as plain text.
        assert html.escape(told.replace('`', '')) in response.text
        assert 'try the command again' not in response.text
        # Not an error of the bot's.
        assert oauth_logs(caplog) == [
            (
                logging.INFO,
                f'Could not link the Codeforces account of user {MEMBER_ID}: {reason}',
            )
        ]

    @pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
    async def test_an_unexpected_failure_says_to_try_again_privately(
        self,
        bot: MagicMock,
        member: MagicMock,
        interaction: MagicMock,
        user_db: UserDbConn,
        caplog: pytest.LogCaptureFixture,
        slash: bool,
    ) -> None:
        # The bot has left the server since the member signed in, say.
        bot.get_guild.return_value = None

        with caplog.at_level(logging.ERROR, logger=oauth.__name__):
            response = await oauth_callback(bot, interaction if slash else None)

        assert 'An error occurred. Please try the command again in Discord.' in (
            response.text
        )
        assert await handle_of(user_db) is None
        send = interaction.followup.send if slash else member.send
        send.assert_awaited_once()
        assert send.await_args.kwargs['embed'].description == (
            "I couldn't link your Codeforces account. Try `/handle identify` again, "
            'or ask a moderator if it keeps failing.'
        )
        if slash:
            assert send.await_args.kwargs['ephemeral'] is True
            member.send.assert_not_awaited()
        bot.get_channel.assert_not_called()
        [record] = [r for r in caplog.records if r.name == oauth.__name__]
        assert record.getMessage() == 'OAuth callback error'
        assert record.exc_info is not None
        assert str(record.exc_info[1]) == 'Guild not found'

    @pytest.mark.parametrize(
        'slash', [False, True], ids=['direct messages closed', 'interaction expired']
    )
    async def test_a_message_that_cannot_be_sent_is_only_logged(
        self,
        bot: MagicMock,
        member: MagicMock,
        interaction: MagicMock,
        user_db: UserDbConn,
        caplog: pytest.LogCaptureFixture,
        slash: bool,
    ) -> None:
        # The message is sent once the link is made, outside the linking's try:
        # failing to send it neither undoes the link nor tells the member that
        # linking failed, and nothing is posted anywhere else.
        refused = discord.Forbidden(MagicMock(status=403, reason='Forbidden'), 'no')
        expired = discord.NotFound(MagicMock(status=404, reason='Not Found'), 'gone')
        member.send.side_effect = refused
        interaction.followup.send.side_effect = expired

        with caplog.at_level(logging.INFO, logger=oauth.__name__):
            response = await oauth_callback(bot, interaction if slash else None)

        assert response.text == oauth._SUCCESS_HTML
        assert await handle_of(user_db) == HANDLE
        send = interaction.followup.send if slash else member.send
        send.assert_awaited_once()
        error = expired if slash else refused
        assert oauth_logs(caplog) == [(logging.INFO, f'{NOT_TOLD}: {error}')]
        bot.get_channel.assert_not_called()

    async def test_a_member_the_bot_cannot_find_is_not_told(
        self,
        bot: MagicMock,
        user_db: UserDbConn,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bot.get_user.return_value = None

        with caplog.at_level(logging.INFO, logger=oauth.__name__):
            response = await oauth_callback(bot)

        assert response.text == oauth._SUCCESS_HTML
        assert await handle_of(user_db) == HANDLE
        assert oauth_logs(caplog) == [
            (logging.INFO, f'{NOT_TOLD}: they are not in any server of the bot')
        ]

    async def test_an_unexpected_failure_to_tell_is_logged_and_the_link_stays(
        self,
        bot: MagicMock,
        interaction: MagicMock,
        user_db: UserDbConn,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        bug = RuntimeError('broken')
        interaction.followup.send.side_effect = bug

        with caplog.at_level(logging.INFO, logger=oauth.__name__):
            response = await oauth_callback(bot, interaction)

        assert response.text == oauth._SUCCESS_HTML
        assert await handle_of(user_db) == HANDLE
        [record] = [r for r in caplog.records if r.name == oauth.__name__]
        assert (record.levelno, record.getMessage()) == (logging.ERROR, NOT_TOLD)
        assert record.exc_info is not None and record.exc_info[1] is bug

    async def test_the_error_in_the_link_is_shown_escaped(self, bot: MagicMock) -> None:
        # Anyone can open the callback, with any error.
        server = oauth.OAuthServer(bot, oauth.OAuthStateStore(), port=0)
        request = make_mocked_request(
            'GET', '/callback?error=%3Cscript%3Ealert(1)%3C/script%3E'
        )

        response = await server._handle_callback(request)

        assert '<script>' not in response.text
        assert 'Authorization denied: &lt;script&gt;alert(1)&lt;/script&gt;' in (
            response.text
        )
