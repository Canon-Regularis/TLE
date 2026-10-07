"""Tests for tle.util.discord_common, apart from bot_error_handler.

They cover the errors the access rules raise, the throttle of refusals,
``send_error_if``, giving back a use that a cooldown counted, which roles
self-service commands may hand out, and whose name the bot's status shows.
bot_error_handler has tests of its own, in
tests/kcpc/component/test_error_handler.py.
"""

import logging
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle import constants
from tle.util import discord_common
from tle.util.discord_common import (
    REFUSAL_THROTTLE_SECONDS,
    AccessDenied,
    PrivateAnswerExpired,
    RefusalThrottle,
    embed_alert,
    presence_candidates,
    self_assignable_problem,
    self_removable_problem,
    send_error_if,
)

LOGGER = 'tle.util.discord_common'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
CHANNEL_ID = 1_200_000_000_000_000_001
ROLE_ID = 1_300_000_000_000_000_001
BOT_ROLE_ID = 1_300_000_000_000_000_009
PURGATORY_ROLE_ID = 1_300_000_000_000_000_002
MEMBER_ID = 1_400_000_000_000_000_001
# The bot's highest role is at this position.
BOT_POSITION = 10
EVERYONE = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True
)
TLE_ROLE = 'the bot uses it to decide what members may do'


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, as by default, whatever the environment says."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_PURGATORY', 'Purgatory')
    # No developer role, as when TLE_DEVELOPER is unset.
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


class MonotonicClock:
    """Stands in for time.monotonic; it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# The errors of the access rules


def test_an_access_refusal_is_a_check_failure_with_its_reply() -> None:
    error = AccessDenied('Use this command in the staff channel.')

    # So discord.py, and handlers that know only CheckFailure, treat it as one.
    assert isinstance(error, commands.CheckFailure)
    assert (error.text, error.silent, error.delete_after) == (
        'Use this command in the staff channel.',
        False,
        None,
    )
    assert str(error) == 'Use this command in the staff channel.'


def test_a_silent_refusal_needs_no_text() -> None:
    error = AccessDenied(None, silent=True)

    assert (error.text, error.silent) == (None, True)
    assert str(error) == ''


def test_a_refusal_can_say_how_long_its_reply_stays() -> None:
    assert AccessDenied('Not here.', delete_after=5).delete_after == 5


def test_a_late_private_answer_is_a_command_error_but_not_a_check_failure() -> None:
    error = PrivateAnswerExpired()

    assert isinstance(error, commands.CommandError)
    assert not isinstance(error, commands.CheckFailure)
    assert str(error) == 'The interaction expired before its private answer was sent.'
    assert str(PrivateAnswerExpired('Too late.')) == 'Too late.'


# The throttle of refusals


def test_the_throttle_lets_one_refusal_per_key_through_in_its_window() -> None:
    clock = MonotonicClock()
    throttle = RefusalThrottle(30, clock)

    assert throttle.allow((GUILD_ID, MEMBER_ID))
    clock.now += 29.5
    assert not throttle.allow((GUILD_ID, MEMBER_ID))
    clock.now += 0.5
    assert throttle.allow((GUILD_ID, MEMBER_ID))


def test_a_refusal_held_back_does_not_extend_the_window() -> None:
    clock = MonotonicClock()
    throttle = RefusalThrottle(30, clock)
    throttle.allow((GUILD_ID, MEMBER_ID))

    clock.now += 20
    assert not throttle.allow((GUILD_ID, MEMBER_ID))
    clock.now += 10  # 30 s after the one let through, 10 s after the other

    assert throttle.allow((GUILD_ID, MEMBER_ID))


def test_the_throttle_counts_keys_apart() -> None:
    throttle = RefusalThrottle(30, MonotonicClock())
    throttle.allow((GUILD_ID, MEMBER_ID))

    assert throttle.allow((OTHER_GUILD_ID, MEMBER_ID))
    assert throttle.allow((GUILD_ID, MEMBER_ID + 1))
    assert throttle.allow((None, MEMBER_ID))


def test_the_throttle_forgets_keys_whose_window_has_passed() -> None:
    clock = MonotonicClock()
    throttle = RefusalThrottle(30, clock)
    throttle.allow((GUILD_ID, 1))
    clock.now += 10
    throttle.allow((GUILD_ID, 2))
    assert len(throttle) == 2

    clock.now += 20  # the first is 30 s old
    throttle.allow((GUILD_ID, 3))
    assert len(throttle) == 2

    clock.now += 30
    throttle.allow((GUILD_ID, 4))
    assert len(throttle) == 1


def test_clearing_the_throttle_forgets_every_refusal() -> None:
    throttle = RefusalThrottle(30, MonotonicClock())
    throttle.allow((GUILD_ID, MEMBER_ID))

    throttle.clear()

    assert len(throttle) == 0
    assert throttle.allow((GUILD_ID, MEMBER_ID))


def test_the_bots_throttle_has_a_30_second_window() -> None:
    assert discord_common.refusal_throttle.window == REFUSAL_THROTTLE_SECONDS == 30


def test_a_test_may_leave_a_refusal_in_the_bots_throttle() -> None:
    # The bot's own throttle, on the real clock.
    assert discord_common.refusal_throttle.allow((GUILD_ID, MEMBER_ID))
    assert not discord_common.refusal_throttle.allow((GUILD_ID, MEMBER_ID))


def test_the_next_test_starts_with_no_refusal_throttled() -> None:
    # tests/conftest.py clears the throttle before every test, so the refusal
    # left by the test above holds nothing back here.
    assert len(discord_common.refusal_throttle) == 0
    assert discord_common.refusal_throttle.allow((GUILD_ID, MEMBER_ID))


# send_error_if


class CogError(commands.CommandError):
    pass


class SubCogError(CogError):
    pass


class Cog(commands.Cog):
    """A cog that answers its own CogError and passes on anything else."""

    def __init__(self) -> None:
        self.passed_on: list[Exception] = []

    @send_error_if(CogError, commands.BadArgument)
    async def cog_command_error(
        self, ctx: commands.Context[Any], error: Exception
    ) -> None:
        self.passed_on.append(error)


def make_context(
    *, slash: bool = False, expired: bool = False
) -> commands.Context[commands.Bot]:
    """A real context of a command in a server; with ``slash``, of a slash one.

    The interaction of a slash command has expired with ``expired``. Replies
    are recorded by an ``AsyncMock`` in place of ``send``.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    author = MagicMock(spec=discord.Member, id=MEMBER_ID)
    message = MagicMock(spec=discord.Message, guild=guild, author=author)
    interaction = None
    if slash:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.is_expired.return_value = expired
    context: commands.Context[commands.Bot] = commands.Context(
        message=message,
        bot=MagicMock(spec=commands.Bot),
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def send_of(ctx: commands.Context[Any]) -> AsyncMock:
    return cast(AsyncMock, ctx.send)


def alert(text: str) -> dict[str, Any]:
    return embed_alert(text).to_dict()


@pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])
@pytest.mark.parametrize(
    'error_cls',
    [CogError, SubCogError, commands.BadArgument],
    ids=lambda error_cls: error_cls.__name__,
)
async def test_a_cog_error_is_answered_privately(
    error_cls: type[commands.CommandError], slash: bool
) -> None:
    cog, ctx = Cog(), make_context(slash=slash)
    error = error_cls("You haven't linked a handle.")

    await cog.cog_command_error(ctx, error)

    send_of(ctx).assert_awaited_once()
    assert send_of(ctx).await_args is not None
    options = dict(send_of(ctx).await_args.kwargs)
    assert options.pop('embed').to_dict() == alert("You haven't linked a handle.")
    assert options == {'ephemeral': True}
    assert getattr(error, 'handled', False)  # so bot_error_handler stays quiet
    assert cog.passed_on == []


async def test_any_other_error_is_passed_on() -> None:
    cog, ctx = Cog(), make_context()
    error = commands.CommandError('Something else.')

    await cog.cog_command_error(ctx, error)

    send_of(ctx).assert_not_awaited()
    assert cog.passed_on == [error]
    assert not getattr(error, 'handled', False)


async def test_a_cog_error_is_not_answered_once_the_interaction_has_expired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # discord.py would post the answer in the channel, for everyone to see.
    cog, ctx = Cog(), make_context(slash=True, expired=True)
    error = CogError("You haven't linked a handle.")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await cog.cog_command_error(ctx, error)

    send_of(ctx).assert_not_awaited()
    assert getattr(error, 'handled', False)
    assert [record.levelno for record in caplog.records] == [logging.INFO]


@pytest.mark.parametrize(
    ('failure', 'level'),
    [
        (
            discord.NotFound(
                MagicMock(status=404, reason='Not Found'), 'Unknown interaction'
            ),
            logging.WARNING,
        ),
        (PrivateAnswerExpired(), logging.INFO),
    ],
    ids=['discord refuses it', 'the context refuses it'],
)
async def test_an_answer_that_fails_is_only_logged(
    failure: Exception, level: int, caplog: pytest.LogCaptureFixture
) -> None:
    cog, ctx = Cog(), make_context(slash=True)
    send_of(ctx).side_effect = failure
    error = CogError("You haven't linked a handle.")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        await cog.cog_command_error(ctx, error)  # raises nothing

    assert getattr(error, 'handled', False)
    assert [record.levelno for record in caplog.records] == [level]


# Giving back a use that a cooldown counted


async def test_undo_cooldown_gives_back_the_use_the_cooldown_counted() -> None:
    @commands.command()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def heavy(ctx: commands.Context[Any]) -> None:
        pass

    ctx = make_context()
    ctx.message.created_at = discord.utils.utcnow()
    ctx.message.edited_at = None
    ctx.bot._before_invoke = None  # the bot has no hooks
    await heavy.prepare(ctx)
    assert heavy.is_on_cooldown(ctx)

    discord_common.undo_cooldown(ctx)

    assert not heavy.is_on_cooldown(ctx)
    await heavy.prepare(ctx)  # not CommandOnCooldown
    with pytest.raises(commands.CommandOnCooldown):
        await heavy.prepare(ctx)


def test_undo_cooldown_outside_a_command_does_nothing() -> None:
    ctx = make_context()
    assert ctx.command is None

    discord_common.undo_cooldown(ctx)  # raises nothing


# Self-service roles


def make_role(
    guild: MagicMock,
    role_id: int,
    name: str,
    *,
    position: int = 1,
    permissions: discord.Permissions | None = None,
    managed: bool = False,
) -> discord.Role:
    return discord.Role(
        guild=guild,
        state=MagicMock(),
        data={
            'id': role_id,
            'name': name,
            'position': position,
            'permissions': str((permissions or discord.Permissions.none()).value),
            'managed': managed,
        },
    )


def make_channel(
    guild: MagicMock, overwrites: dict[int, discord.PermissionOverwrite]
) -> discord.TextChannel:
    """A text channel in ``guild`` with ``overwrites`` for these role ids."""
    payload = []
    for role_id, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        payload.append(
            {
                'id': str(role_id),
                'type': 0,  # a role's
                'allow': str(allow.value),
                'deny': str(deny.value),
            }
        )
    return discord.TextChannel(
        state=MagicMock(),
        guild=guild,
        data={
            'id': CHANNEL_ID,
            'type': 0,
            'name': 'staff',
            'position': 0,
            'permission_overwrites': payload,
        },
    )


@pytest.fixture
def guild() -> MagicMock:
    """A server where @everyone may read and talk, with no channels yet."""
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.default_role = make_role(
        guild, GUILD_ID, '@everyone', position=0, permissions=EVERYONE
    )
    guild.channels = []
    return guild


def bot_member(guild: MagicMock, top_role: discord.Role | None = None) -> MagicMock:
    """The bot in ``guild``, whose highest role is ``top_role`` or its own."""
    me = MagicMock(spec=discord.Member)
    me.top_role = top_role or make_role(
        guild, BOT_ROLE_ID, 'TLE', position=BOT_POSITION, managed=True
    )
    return me


@pytest.fixture
def role(guild: MagicMock) -> discord.Role:
    """A role a member may take: below the bot, granting nothing more."""
    return make_role(guild, ROLE_ID, 'Contest pings', position=3, permissions=EVERYONE)


def test_an_ordinary_role_may_be_handed_out(
    guild: MagicMock, role: discord.Role
) -> None:
    # Channels may set permissions for other roles, and for @everyone.
    guild.channels = [
        make_channel(
            guild, {GUILD_ID: discord.PermissionOverwrite(view_channel=False)}
        ),
        make_channel(
            guild, {BOT_ROLE_ID: discord.PermissionOverwrite(view_channel=True)}
        ),
    ]

    assert self_assignable_problem(role, bot_member(guild)) is None


def test_everyone_is_refused(guild: MagicMock) -> None:
    problem = self_assignable_problem(guild.default_role, bot_member(guild))

    assert problem == 'everyone has it already'


def test_a_managed_role_is_refused(guild: MagicMock) -> None:
    booster = make_role(guild, ROLE_ID, 'Server Booster', position=2, managed=True)

    problem = self_assignable_problem(booster, bot_member(guild))

    assert problem == 'it is managed by Discord or an integration'


@pytest.mark.parametrize('higher', [False, True], ids=['the same role', 'higher'])
def test_a_role_at_or_above_the_bots_highest_role_is_refused(
    guild: MagicMock, higher: bool
) -> None:
    # Admins gave the bot an ordinary role as its highest.
    bots = make_role(guild, BOT_ROLE_ID, 'Bots', position=BOT_POSITION)
    role = make_role(guild, ROLE_ID, 'Veterans', position=BOT_POSITION + 1)

    problem = self_assignable_problem(role if higher else bots, bot_member(guild, bots))

    assert problem == 'it is not below my highest role'


@pytest.mark.parametrize('by_id', [False, True], ids=['by name', 'by id'])
@pytest.mark.parametrize(
    'setting', ['TLE_ADMIN', 'TLE_MODERATOR', 'TLE_TRUSTED', 'TLE_PURGATORY']
)
def test_tles_own_roles_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    guild: MagicMock,
    role: discord.Role,
    setting: str,
    by_id: bool,
) -> None:
    monkeypatch.setattr(constants, setting, ROLE_ID if by_id else role.name)

    assert self_assignable_problem(role, bot_member(guild)) == TLE_ROLE


def test_the_developer_role_is_refused(
    monkeypatch: pytest.MonkeyPatch, guild: MagicMock, role: discord.Role
) -> None:
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', ROLE_ID)

    assert self_assignable_problem(role, bot_member(guild)) == TLE_ROLE


def test_without_a_developer_role_no_more_roles_are_refused(
    monkeypatch: pytest.MonkeyPatch, guild: MagicMock, role: discord.Role
) -> None:
    # None, as tle.constants gives when TLE_DEVELOPER is unset.
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)

    assert self_assignable_problem(role, bot_member(guild)) is None


def test_tle_role_settings_match_ids_to_ids_and_names_to_names(
    monkeypatch: pytest.MonkeyPatch, guild: MagicMock, role: discord.Role
) -> None:
    # A name that looks like the role's id is still a name.
    monkeypatch.setattr(constants, 'TLE_ADMIN', str(ROLE_ID))
    monkeypatch.setattr(constants, 'TLE_MODERATOR', ROLE_ID + 1)

    assert self_assignable_problem(role, bot_member(guild)) is None


@pytest.mark.parametrize(
    'permissions',
    [
        discord.Permissions(manage_messages=True),
        discord.Permissions(view_channel=True, mention_everyone=True),
        discord.Permissions(administrator=True),
    ],
    ids=['manage messages', 'mention everyone', 'administrator'],
)
def test_a_role_granting_more_than_everyone_has_is_refused(
    guild: MagicMock, permissions: discord.Permissions
) -> None:
    role = make_role(guild, ROLE_ID, 'Helpers', position=3, permissions=permissions)

    problem = self_assignable_problem(role, bot_member(guild))

    assert problem == 'it grants permissions beyond what everyone has'


def test_a_role_granting_less_than_everyone_has_may_be_handed_out(
    guild: MagicMock,
) -> None:
    role = make_role(
        guild,
        ROLE_ID,
        'Readers',
        position=3,
        permissions=discord.Permissions(view_channel=True),
    )

    assert self_assignable_problem(role, bot_member(guild)) is None


@pytest.mark.parametrize(
    'overwrite',
    [
        discord.PermissionOverwrite(view_channel=True),
        discord.PermissionOverwrite(send_messages=False),
    ],
    ids=['allows', 'denies'],
)
def test_a_role_that_a_channel_sets_permissions_for_is_refused(
    guild: MagicMock, role: discord.Role, overwrite: discord.PermissionOverwrite
) -> None:
    guild.channels = [
        make_channel(guild, {}),
        make_channel(guild, {ROLE_ID: overwrite}),
    ]

    problem = self_assignable_problem(role, bot_member(guild))

    assert problem == 'some channels set permissions for it'


def test_a_channel_overwrite_that_changes_nothing_does_not_count(
    guild: MagicMock, role: discord.Role
) -> None:
    guild.channels = [make_channel(guild, {ROLE_ID: discord.PermissionOverwrite()})]

    assert self_assignable_problem(role, bot_member(guild)) is None


def test_the_first_problem_found_is_the_one_given(
    monkeypatch: pytest.MonkeyPatch, guild: MagicMock
) -> None:
    # A moderator role with permissions of its own, everywhere.
    role = make_role(
        guild,
        ROLE_ID,
        'Moderator',
        position=3,
        permissions=discord.Permissions(manage_messages=True),
    )
    guild.channels = [
        make_channel(guild, {ROLE_ID: discord.PermissionOverwrite(view_channel=True)})
    ]

    assert self_assignable_problem(role, bot_member(guild)) == TLE_ROLE


# Self-service roles taken away


def test_a_role_that_only_adds_may_be_taken_away(guild: MagicMock) -> None:
    # Losing permissions beyond @everyone's, or what channels allow it, only
    # lowers a member's rights.
    role = make_role(
        guild,
        ROLE_ID,
        'Helpers',
        position=3,
        permissions=discord.Permissions(manage_messages=True),
    )
    guild.channels = [
        make_channel(guild, {ROLE_ID: discord.PermissionOverwrite(view_channel=True)})
    ]

    assert self_removable_problem(role, bot_member(guild)) is None
    assert self_assignable_problem(role, bot_member(guild)) is not None


def test_a_role_that_a_channel_denies_permissions_is_not_taken_away(
    guild: MagicMock, role: discord.Role
) -> None:
    # Losing it would let the member do what the channel denies the role.
    overwrite = discord.PermissionOverwrite(view_channel=True, send_messages=False)
    guild.channels = [make_channel(guild, {ROLE_ID: overwrite})]

    problem = self_removable_problem(role, bot_member(guild))

    assert problem == 'some channels deny it permissions'


@pytest.mark.parametrize(
    'setting', ['TLE_ADMIN', 'TLE_MODERATOR', 'TLE_TRUSTED', 'TLE_PURGATORY']
)
def test_tles_own_roles_are_not_taken_away(
    monkeypatch: pytest.MonkeyPatch,
    guild: MagicMock,
    role: discord.Role,
    setting: str,
) -> None:
    # Above all purgatory, whose loss would raise the member's rights.
    monkeypatch.setattr(constants, setting, role.name)

    assert self_removable_problem(role, bot_member(guild)) == TLE_ROLE


def test_roles_the_bot_cannot_manage_are_not_taken_away(guild: MagicMock) -> None:
    booster = make_role(guild, ROLE_ID, 'Server Booster', position=2, managed=True)
    seniors = make_role(guild, ROLE_ID + 1, 'Seniors', position=BOT_POSITION + 1)
    me = bot_member(guild)

    assert self_removable_problem(guild.default_role, me) == 'everyone has it already'
    assert self_removable_problem(booster, me) == (
        'it is managed by Discord or an integration'
    )
    assert self_removable_problem(seniors, me) == 'it is not below my highest role'


# Whether a member has one of TLE's roles


@pytest.mark.parametrize('by_id', [False, True], ids=['by name', 'by id'])
def test_a_role_is_found_by_its_id_or_its_name(
    guild: MagicMock, role: discord.Role, by_id: bool
) -> None:
    member = make_member('Alice', GUILD_ID, guild.default_role, role)

    assert discord_common.has_role(member, ROLE_ID if by_id else 'Contest pings')
    assert not discord_common.has_role(member, BOT_ROLE_ID if by_id else 'TLE')


@pytest.mark.parametrize('setting', [GUILD_ID, '@everyone'], ids=['its id', 'its name'])
def test_the_default_role_never_counts(guild: MagicMock, setting: str | int) -> None:
    # Every member has @everyone, whose id is the server's: a setting that
    # names it would give its role to every member.
    member = make_member('Alice', GUILD_ID, guild.default_role)

    assert not discord_common.has_role(member, setting)


# Presence


class FakeAccess:
    """Stands in for the access service: only these servers are allowed."""

    def __init__(self, *allowed: int) -> None:
        self.allowed = set(allowed)
        self.asked: list[int | None] = []

    def guild_allowed(self, guild_id: int | None) -> bool:
        self.asked.append(guild_id)
        return guild_id in self.allowed


class FakeBot:
    """A bot with these members and, if given, an access service."""

    def __init__(self, *members: MagicMock, access: FakeAccess | None = None) -> None:
        self.members = list(members)
        if access is not None:
            self.access = access
        self.change_presence = AsyncMock()

    def get_all_members(self) -> Iterator[MagicMock]:
        return iter(self.members)


def make_member(name: str, guild_id: int, *roles: discord.Role) -> MagicMock:
    member = MagicMock(spec=discord.Member, display_name=name)
    member.guild = MagicMock(spec=discord.Guild, id=guild_id)
    member.roles = list(roles)
    return member


@pytest.fixture
def purgatory(guild: MagicMock) -> discord.Role:
    return make_role(guild, PURGATORY_ROLE_ID, 'Purgatory', position=2)


def test_without_an_access_service_members_of_every_server_count() -> None:
    alice = make_member('Alice', GUILD_ID)
    bob = make_member('Bob', OTHER_GUILD_ID)

    assert presence_candidates(FakeBot(alice, bob)) == [alice, bob]


def test_only_members_of_allowed_servers_count() -> None:
    alice = make_member('Alice', GUILD_ID)
    mallory = make_member('Mallory', OTHER_GUILD_ID)
    access = FakeAccess(GUILD_ID)

    assert presence_candidates(FakeBot(mallory, alice, access=access)) == [alice]
    assert access.asked == [OTHER_GUILD_ID, GUILD_ID]


@pytest.mark.parametrize('by_id', [False, True], ids=['by name', 'by id'])
def test_members_in_purgatory_never_count(
    monkeypatch: pytest.MonkeyPatch, purgatory: discord.Role, by_id: bool
) -> None:
    monkeypatch.setattr(
        constants, 'TLE_PURGATORY', PURGATORY_ROLE_ID if by_id else 'Purgatory'
    )
    alice = make_member('Alice', GUILD_ID)
    eve = make_member('Eve', GUILD_ID, purgatory)

    assert presence_candidates(FakeBot(eve, alice)) == [alice]
    assert presence_candidates(FakeBot(eve, alice, access=FakeAccess(GUILD_ID))) == [
        alice
    ]


class FakeTask:
    """What presence() makes with tasks.task, recorded instead of run."""

    def __init__(self, name: str, func: Callable[[Any], Awaitable[None]]) -> None:
        self.name = name
        self.func = func
        self.started = False

    def start(self) -> None:
        self.started = True


@pytest.fixture
def made_tasks(monkeypatch: pytest.MonkeyPatch) -> list[FakeTask]:
    """The tasks presence() makes; none runs on its own, and it never waits."""
    # Imported before tasks, which can't be imported first (see presence).
    import tle.util.codeforces_common  # noqa: F401
    from tle.util import tasks

    made: list[FakeTask] = []

    def task(
        *, name: str, waiter: Any = None, exception_handler: Any = None
    ) -> Callable[[Callable[[Any], Awaitable[None]]], FakeTask]:
        def decorator(func: Callable[[Any], Awaitable[None]]) -> FakeTask:
            made.append(FakeTask(name, func))
            return made[-1]

        return decorator

    monkeypatch.setattr(tasks, 'task', task)
    monkeypatch.setattr(discord_common, 'asyncio', SimpleNamespace(sleep=AsyncMock()))
    return made


async def test_presence_shows_a_member_of_an_allowed_server(
    made_tasks: list[FakeTask],
) -> None:
    alice = make_member('Alice', GUILD_ID)
    bot = FakeBot(
        make_member('Mallory', OTHER_GUILD_ID), alice, access=FakeAccess(GUILD_ID)
    )

    await discord_common.presence(bot)

    activity = bot.change_presence.await_args.kwargs['activity']
    assert (activity.type, activity.name) == (
        discord.ActivityType.listening,
        'your commands',
    )
    cast(AsyncMock, discord_common.asyncio.sleep).assert_awaited_once_with(60)
    (task,) = made_tasks
    assert (task.name, task.started) == ('OrzUpdate', True)

    await task.func(None)

    assert bot.change_presence.await_count == 2
    assert bot.change_presence.await_args.kwargs == {
        'activity': discord.Game(name='Alice orz')
    }


async def test_presence_changes_nothing_when_no_member_can_be_shown(
    made_tasks: list[FakeTask],
) -> None:
    bot = FakeBot(make_member('Mallory', OTHER_GUILD_ID), access=FakeAccess(GUILD_ID))
    await discord_common.presence(bot)
    (task,) = made_tasks

    await task.func(None)  # raises nothing

    bot.change_presence.assert_awaited_once()  # only the first, 'your commands'
