"""Tests for TLE's Contests cog (tle.cogs.contests): contest reminders,
ranklists and rated virtual contests.

Commands run by calling their callbacks, as discord.py does once it has parsed
the arguments, with a real in-memory user database (the ``user_db`` fixture)
and, where channels matter, a real access service. Discord itself (contexts,
the server, its channels and members) is mocked, but roles are real
``discord.Role`` objects, so that the checks of which roles members may give
themselves run as they do in a server.
"""

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock, call

import discord
import pytest
from discord.ext import commands

from tle import constants
from tle.__main__ import TLEBot
from tle.access.cog import RATED_VC_STAFF_WARNING, RATED_VC_WARNING
from tle.access.help import describe_cooldown
from tle.access.service import AccessService
from tle.access.settings import GuildAccess
from tle.cogs import contests
from tle.cogs.contests import (
    RATED_VC_CHANNEL_SET_TEXT,
    UNASSIGNABLE_REMINDER_ROLE_TEXT,
    UNSUITABLE_REMINDER_ROLE_TEXT,
    ContestCogError,
    Contests,
)
from tle.config import Settings
from tle.util import codeforces_api as cf
from tle.util.cache import ContestNotFound, RanklistNotMonitored
from tle.util.codeforces_api import (
    Contest,
    Member,
    Party,
    Problem,
    ProblemResult,
    RanklistRow,
)
from tle.util.db.user_db_conn import UserDbConn
from tle.util.discord_common import NOT_IN_A_THREAD_MESSAGE
from tle.util.ranklist import Ranklist

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
GENERAL_ID = 1_200_000_000_000_000_020  # neither a bot channel nor the staff channel
THREAD_ID = 1_200_000_000_000_000_030
PINGS_ROLE_ID = 1_300_000_000_000_000_001
OTHER_ROLE_ID = 1_300_000_000_000_000_002
BOT_ROLE_ID = 1_300_000_000_000_000_009
MEMBER_ID = 1_400_000_000_000_000_001
CONTEST_ID = 1950
CONTEST_NAME = 'Codeforces Round 1950'
# The bot's highest role is at this position.
BOT_POSITION = 10
EVERYONE = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True
)
MORE_THAN_EVERYONE = EVERYONE | discord.Permissions(manage_messages=True)
# Why discord_common.self_assignable_problem refuses a role.
TLE_ROLE = 'the bot uses it to decide what members may do'
EXTRA_PERMISSIONS = 'it grants permissions beyond what everyone has'
TOO_HIGH = 'it is not below my highest role'
WAIT_TEXT = 'Generating ranklist, please wait...'


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role, whatever the environment says."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_PURGATORY', 'Purgatory')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


def make_role(
    guild: MagicMock,
    role_id: int,
    name: str,
    *,
    position: int = 3,
    permissions: discord.Permissions = EVERYONE,
) -> discord.Role:
    """A role that anyone may mention, in ``guild``."""
    return discord.Role(
        guild=guild,
        state=MagicMock(),
        data={
            'id': role_id,
            'name': name,
            'position': position,
            'permissions': str(permissions.value),
            'mentionable': True,
        },
    )


@pytest.fixture
def roles() -> dict[int, discord.Role]:
    """The server's roles by id, which ``Guild.get_role`` reads."""
    return {}


@pytest.fixture
def guild(roles: dict[int, discord.Role]) -> MagicMock:
    """The server: @everyone may read and talk, no channel sets permissions
    for a role, and the bot's highest role is at BOT_POSITION.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    guild.default_role = make_role(
        guild, GUILD_ID, '@everyone', position=0, permissions=EVERYONE
    )
    guild.channels = []
    guild.me = MagicMock(spec=discord.Member)
    guild.me.top_role = make_role(guild, BOT_ROLE_ID, 'TLE', position=BOT_POSITION)
    guild.get_role.side_effect = roles.get
    return guild


@pytest.fixture
def pings_role(guild: MagicMock, roles: dict[int, discord.Role]) -> discord.Role:
    """A role just for pings: below the bot, granting nothing more."""
    role = make_role(guild, PINGS_ROLE_ID, 'Contest pings')
    roles[role.id] = role
    return role


def make_member(*held: discord.Role) -> MagicMock:
    """A member of the server, holding ``held``."""
    member = MagicMock(spec=discord.Member, id=MEMBER_ID)
    member.display_name = 'Alice'
    member.mention = f'<@{MEMBER_ID}>'
    member.roles = list(held)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def make_channel(channel_id: int) -> MagicMock:
    """A text channel; posts in it are recorded."""
    channel = MagicMock(spec=discord.TextChannel, id=channel_id)
    channel.mention = f'<#{channel_id}>'
    channel.send = AsyncMock()
    return channel


def make_thread(parent_id: int) -> MagicMock:
    """A thread in the channel ``parent_id``."""
    thread = MagicMock(spec=discord.Thread, id=THREAD_ID, parent_id=parent_id)
    thread.mention = f'<#{THREAD_ID}>'
    return thread


def make_ctx(guild: MagicMock, channel: MagicMock, author: MagicMock) -> MagicMock:
    """The context of a command ``author`` uses in ``channel``; its replies are
    recorded, and each is a message that can be deleted. Its command records
    whether the use its cooldown counted is given back.
    """
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = guild
    ctx.channel = channel
    ctx.author = author
    ctx.command = MagicMock(spec=commands.Command)
    reply = MagicMock(spec=discord.Message)
    reply.delete = AsyncMock()
    ctx.send = AsyncMock(return_value=reply)
    return ctx


def sent_embed(ctx: MagicMock) -> discord.Embed:
    """The embed of the one reply to ``ctx``."""
    ctx.send.assert_awaited_once()
    embed = ctx.send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


@pytest.fixture
def bot(user_db: UserDbConn) -> MagicMock:
    """TLE's bot, without an access service until a test gives it one."""
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    bot.cf_cache = MagicMock()
    return bot


@pytest.fixture
def cog(bot: MagicMock) -> Contests:
    return Contests(bot)


@pytest.fixture
async def access(bot: MagicMock) -> AccessService:
    """The bot's access service, in a server with a bot channel and a staff
    channel; its settings are kept in memory.
    """
    service = AccessService(bot)
    settings = GuildAccess(frozenset({BOT_CHANNEL_ID}), STAFF_CHANNEL_ID)
    await service.change(GUILD_ID, lambda _: settings)
    bot.access = service
    return service


def make_contest(phase: str) -> Contest:
    return Contest(
        id=CONTEST_ID,
        name=CONTEST_NAME,
        startTimeSeconds=1_000_000,
        durationSeconds=7200,
        type='CF',
        phase=phase,
        preparedBy=None,
    )


def make_ranklist(contest: Contest, handle: str) -> Ranklist:
    """The standings of ``contest``, which ``handle`` won with problem A."""
    problem = Problem(
        contestId=contest.id,
        problemsetName=None,
        index='A',
        name='Watermelon',
        type='PROGRAMMING',
        points=500.0,
        rating=800,
        tags=[],
    )
    party = Party(
        contestId=contest.id,
        members=[Member(handle=handle)],
        participantType='CONTESTANT',
        teamId=None,
        teamName=None,
        ghost=False,
        room=None,
        startTimeSeconds=None,
    )
    result = ProblemResult(
        points=500.0,
        penalty=None,
        rejectedAttemptCount=0,
        type='FINAL',
        bestSubmissionTimeSeconds=600,
    )
    row = RanklistRow(
        party=party, rank=1, points=500.0, penalty=0, problemResults=[result]
    )
    return Ranklist(contest, [problem], [row], 0.0, is_rated=False)


# The access rules decide


def test_the_access_rules_alone_decide_who_may_use_each_command(
    cog: Contests,
) -> None:
    # A role check of a command's own would refuse admins by Manage Server,
    # whom the rules let in, and /help would offer them the command all the
    # same.
    checked = {
        command.qualified_name: command.checks
        for command in cog.walk_commands()
        if command.checks
    }

    assert checked == {}


def test_heavy_commands_have_cooldowns(cog: Contests) -> None:
    cooldowns = {
        command.qualified_name: describe_cooldown(command)
        for command in cog.walk_commands()
        if command.cooldown is not None
    }

    assert cooldowns == {
        'ranklist': 'Once every 30 seconds in this server',
        'ratedvc': 'Once every minute for each member',
        'vcrating': 'Once every 20 seconds for each member',
    }


# Contest reminders


async def test_a_contest_reminder_pings_its_role_and_nobody_else(
    monkeypatch: pytest.MonkeyPatch, pings_role: discord.Role
) -> None:
    # The reminder is due a moment from now.
    monkeypatch.setattr(contests, 'time', SimpleNamespace(time=lambda: 1_000_000.0))
    channel = make_channel(BOT_CHANNEL_ID)

    await contests._send_reminder_at(
        channel, pings_role, [make_contest('BEFORE')], 3600, 1_000_000.001
    )

    channel.send.assert_awaited_once_with(
        pings_role.mention, embed=ANY, allowed_mentions=ANY
    )
    # Discord gets the bot's own allowed mentions, which ping no role, with
    # the message's laid over them.
    bot = TLEBot(
        nodb=True,
        settings=Settings(),
        command_prefix=';',
        intents=discord.Intents.none(),
    )
    assert bot.allowed_mentions is not None
    mentions = channel.send.await_args.kwargs['allowed_mentions']
    payload = bot.allowed_mentions.merge(mentions).to_dict()
    assert payload == {'parse': [], 'roles': [PINGS_ROLE_ID]}


async def test_remind_here_sets_reminders_up_in_this_channel(
    cog: Contests, user_db: UserDbConn, guild: MagicMock, pings_role: discord.Role
) -> None:
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).here.callback(cog, ctx, pings_role, 10, 60)

    settings = await user_db.get_reminder_settings(GUILD_ID)
    assert settings is not None
    channel_id, role_id, before = settings
    assert (int(channel_id), int(role_id)) == (BOT_CHANNEL_ID, PINGS_ROLE_ID)
    assert json.loads(before) == [60, 10]
    assert sent_embed(ctx).description == (
        'Reminder settings saved. `/remind settings` shows them.'
    )


async def test_remind_here_refuses_a_thread(
    cog: Contests, user_db: UserDbConn, guild: MagicMock, pings_role: discord.Role
) -> None:
    # Once the thread closed, /remind settings said the channel no longer
    # exists, and every reminder failed.
    ctx = make_ctx(guild, make_thread(BOT_CHANNEL_ID), make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).here.callback(cog, ctx, pings_role, 10, 60)

    assert str(refused.value) == NOT_IN_A_THREAD_MESSAGE
    assert await user_db.get_reminder_settings(GUILD_ID) is None
    ctx.send.assert_not_awaited()


def tle_role(guild: MagicMock) -> discord.Role:
    return make_role(guild, OTHER_ROLE_ID, 'Admin')


def role_with_permissions(guild: MagicMock) -> discord.Role:
    return make_role(guild, OTHER_ROLE_ID, 'Helpers', permissions=MORE_THAN_EVERYONE)


def role_above_the_bot(guild: MagicMock) -> discord.Role:
    return make_role(guild, OTHER_ROLE_ID, 'Seniors', position=BOT_POSITION + 1)


def everyone_role(guild: MagicMock) -> discord.Role:
    return guild.default_role  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ('make', 'problem'),
    [
        (tle_role, TLE_ROLE),
        (role_with_permissions, EXTRA_PERMISSIONS),
        (role_above_the_bot, TOO_HIGH),
        (everyone_role, 'everyone has it already'),
    ],
    ids=['a TLE role', 'a role with permissions', 'a role above the bot', 'everyone'],
)
async def test_remind_here_refuses_a_role_the_bot_must_not_hand_out(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    make: Callable[[MagicMock], discord.Role],
    problem: str,
) -> None:
    # Members give themselves the reminder role with /remind on.
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).here.callback(cog, ctx, make(guild), 60)

    assert str(refused.value) == UNSUITABLE_REMINDER_ROLE_TEXT.format(problem=problem)
    assert await user_db.get_reminder_settings(GUILD_ID) is None
    ctx.send.assert_not_awaited()


async def test_remind_on_gives_you_the_reminder_role(
    cog: Contests, user_db: UserDbConn, guild: MagicMock, pings_role: discord.Role
) -> None:
    await user_db.set_reminder_settings(GUILD_ID, BOT_CHANNEL_ID, PINGS_ROLE_ID, '[60]')
    member = make_member()
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)

    await type(cog).on.callback(cog, ctx)

    member.add_roles.assert_awaited_once_with(pings_role, reason=ANY)


@pytest.mark.parametrize(
    ('make', 'problem'),
    [(role_with_permissions, EXTRA_PERMISSIONS), (tle_role, TLE_ROLE)],
    ids=['it gained permissions', 'a TLE role'],
)
async def test_remind_on_checks_the_reminder_role_every_time(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    roles: dict[int, discord.Role],
    make: Callable[[MagicMock], discord.Role],
    problem: str,
) -> None:
    # The role an admin chose has changed since, or was chosen before the
    # bot checked roles.
    role = make(guild)
    roles[role.id] = role
    await user_db.set_reminder_settings(GUILD_ID, BOT_CHANNEL_ID, role.id, '[60]')
    member = make_member()
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)

    with pytest.raises(ContestCogError) as refused:
        await type(cog).on.callback(cog, ctx)

    assert str(refused.value) == UNASSIGNABLE_REMINDER_ROLE_TEXT.format(problem=problem)
    member.add_roles.assert_not_awaited()


def purgatory_role(guild: MagicMock) -> discord.Role:
    return make_role(guild, OTHER_ROLE_ID, 'Purgatory')


def role_with_overwrite(
    guild: MagicMock, overwrite: discord.PermissionOverwrite
) -> discord.Role:
    """A role that a channel of ``guild`` sets ``overwrite`` for."""
    role = make_role(guild, OTHER_ROLE_ID, 'Muted')
    channel = MagicMock(spec=discord.TextChannel)
    channel.overwrites_for.side_effect = lambda target: (
        overwrite if target is role else discord.PermissionOverwrite()
    )
    guild.channels.append(channel)
    return role


def role_a_channel_denies(guild: MagicMock) -> discord.Role:
    return role_with_overwrite(guild, discord.PermissionOverwrite(send_messages=False))


def role_a_channel_allows(guild: MagicMock) -> discord.Role:
    return role_with_overwrite(guild, discord.PermissionOverwrite(view_channel=True))


@pytest.mark.parametrize(
    ('make', 'problem'),
    [
        (purgatory_role, TLE_ROLE),
        (tle_role, TLE_ROLE),
        (role_a_channel_denies, 'some channels deny it permissions'),
        (role_above_the_bot, TOO_HIGH),
    ],
    ids=['purgatory', 'a TLE role', 'a channel denies it', 'a role above the bot'],
)
async def test_remind_off_refuses_a_role_whose_loss_could_raise_your_rights(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    roles: dict[int, discord.Role],
    make: Callable[[MagicMock], discord.Role],
    problem: str,
) -> None:
    role = make(guild)
    roles[role.id] = role
    await user_db.set_reminder_settings(GUILD_ID, BOT_CHANNEL_ID, role.id, '[60]')
    member = make_member(role)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)

    with pytest.raises(ContestCogError) as refused:
        await type(cog).off.callback(cog, ctx)

    assert str(refused.value) == UNASSIGNABLE_REMINDER_ROLE_TEXT.format(problem=problem)
    member.remove_roles.assert_not_awaited()


@pytest.mark.parametrize(
    'make',
    [role_with_permissions, role_a_channel_allows],
    ids=['it gained permissions', 'a channel allows it more'],
)
async def test_remind_off_takes_away_a_role_whose_loss_only_lowers_your_rights(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    roles: dict[int, discord.Role],
    make: Callable[[MagicMock], discord.Role],
) -> None:
    # It can't be handed out any more, but the pings must still stop.
    role = make(guild)
    roles[role.id] = role
    await user_db.set_reminder_settings(GUILD_ID, BOT_CHANNEL_ID, role.id, '[60]')
    member = make_member(role)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)

    await type(cog).off.callback(cog, ctx)

    member.remove_roles.assert_awaited_once_with(role, reason=ANY)
    assert sent_embed(ctx).description == (
        'Successfully unsubscribed from contest reminders'
    )


async def test_remind_off_without_the_role_says_so_whatever_the_role(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    roles: dict[int, discord.Role],
) -> None:
    role = tle_role(guild)
    roles[role.id] = role
    await user_db.set_reminder_settings(GUILD_ID, BOT_CHANNEL_ID, role.id, '[60]')
    member = make_member()
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)

    await type(cog).off.callback(cog, ctx)

    assert sent_embed(ctx).description == 'You are not subscribed to contest reminders'
    member.remove_roles.assert_not_awaited()


# Ranklists


async def test_ranklist_answers_the_command_rather_than_posting_in_the_channel(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    contest = make_contest('FINISHED')
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    bot.cf_cache.ranklist_cache.get_ranklist.return_value = make_ranklist(
        contest, 'tourist'
    )
    channel = make_channel(BOT_CHANNEL_ID)
    ctx = make_ctx(guild, channel, make_member())

    await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    wait, contest_embed, standings = ctx.send.await_args_list
    assert wait == call(WAIT_TEXT)
    assert contest_embed.kwargs['embed'].title == CONTEST_NAME
    assert 'tourist' in standings.args[0]
    ctx.send.return_value.delete.assert_awaited_once_with()
    channel.send.assert_not_awaited()


async def test_ranklist_takes_its_wait_message_back_when_it_fails(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    contest = make_contest('BEFORE')
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    bot.cf_cache.ranklist_cache.get_ranklist.side_effect = RanklistNotMonitored(contest)
    channel = make_channel(BOT_CHANNEL_ID)
    ctx = make_ctx(guild, channel, make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    assert str(refused.value) == f"`{CONTEST_NAME}` hasn't started yet."
    ctx.send.assert_awaited_once_with(WAIT_TEXT)
    ctx.send.return_value.delete.assert_awaited_once_with()
    channel.send.assert_not_awaited()
    # Codeforces wasn't asked anything, so the server's cooldown isn't spent.
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


async def test_ranklist_of_a_contest_the_bot_does_not_know_spends_no_cooldown(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    bot.cf_cache.contest_cache.get_contest.side_effect = ContestNotFound(CONTEST_ID)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    with pytest.raises(ContestNotFound) as refused:
        await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    assert str(refused.value) == f'Contest with ID `{CONTEST_ID}` not found'
    ctx.send.assert_not_awaited()
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


async def test_a_ranklist_shown_spends_the_server_s_cooldown(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    contest = make_contest('FINISHED')
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    bot.cf_cache.ranklist_cache.get_ranklist.return_value = make_ranklist(
        contest, 'tourist'
    )
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    ctx.command.reset_cooldown.assert_not_called()


# Rated virtual contests


@pytest.mark.parametrize(
    ('place', 'warning'),
    [
        (BOT_CHANNEL_ID, None),
        (STAFF_CHANNEL_ID, RATED_VC_STAFF_WARNING),
        (GENERAL_ID, RATED_VC_WARNING),
    ],
    ids=['a bot channel', 'the staff channel', 'another channel'],
)
async def test_set_ratedvc_channel_warns_where_members_cannot_use_ratedvc(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    access: AccessService,
    place: int,
    warning: str | None,
) -> None:
    channel = make_channel(place)
    ctx = make_ctx(guild, channel, make_member())

    await type(cog).set_ratedvc_channel.callback(cog, ctx)

    assert await user_db.get_rated_vc_channel(GUILD_ID) == place
    expected = RATED_VC_CHANNEL_SET_TEXT
    if warning is not None:
        # ;ratedvc works in bot channels only, and in its own channel only;
        # the staff channel counts as a bot channel, but members can't read it.
        expected = f'{expected}\n\n{warning.format(channel=channel.mention)}'
    assert sent_embed(ctx).description == expected


@pytest.mark.parametrize(
    'parent',
    [BOT_CHANNEL_ID, STAFF_CHANNEL_ID, GENERAL_ID],
    ids=['in a bot channel', 'in the staff channel', 'elsewhere'],
)
async def test_set_ratedvc_channel_refuses_a_thread(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    access: AccessService,
    parent: int,
) -> None:
    # A thread closes after a while, and discord.py then forgets it: the bot
    # would say there is no rated virtual contest channel, and post nowhere.
    ctx = make_ctx(guild, make_thread(parent), make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).set_ratedvc_channel.callback(cog, ctx)

    assert str(refused.value) == NOT_IN_A_THREAD_MESSAGE
    assert NOT_IN_A_THREAD_MESSAGE == (
        'Use this command in a channel, not a thread: a thread closes after a '
        "while, and the bot then can't find it."
    )
    assert await user_db.get_rated_vc_channel(GUILD_ID) is None
    ctx.send.assert_not_awaited()


async def test_without_bot_channels_set_ratedvc_channel_warns(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    bot.access = AccessService(bot)  # a server whose access settings are unset
    channel = make_channel(GENERAL_ID)
    ctx = make_ctx(guild, channel, make_member())

    await type(cog).set_ratedvc_channel.callback(cog, ctx)

    warning = RATED_VC_WARNING.format(channel=channel.mention)
    assert sent_embed(ctx).description == f'{RATED_VC_CHANNEL_SET_TEXT}\n\n{warning}'


async def test_without_an_access_service_set_ratedvc_channel_never_warns(
    cog: Contests, user_db: UserDbConn, guild: MagicMock
) -> None:
    ctx = make_ctx(guild, make_channel(GENERAL_ID), make_member())

    await type(cog).set_ratedvc_channel.callback(cog, ctx)

    assert await user_db.get_rated_vc_channel(GUILD_ID) == GENERAL_ID
    assert sent_embed(ctx).description == RATED_VC_CHANNEL_SET_TEXT


@pytest.mark.parametrize(
    ('rated_vc_channel', 'text'),
    [
        (
            None,
            'There is no rated virtual contest channel yet. Ask an admin to set one.',
        ),
        (
            BOT_CHANNEL_ID,
            f'Use this command in <#{BOT_CHANNEL_ID}>, the rated virtual contest '
            'channel.',
        ),
    ],
    ids=['none set', 'another channel'],
)
async def test_ratedvc_says_where_it_works(
    cog: Contests,
    user_db: UserDbConn,
    guild: MagicMock,
    rated_vc_channel: int | None,
    text: str,
) -> None:
    if rated_vc_channel is not None:
        await user_db.set_rated_vc_channel(GUILD_ID, rated_vc_channel)
    ctx = make_ctx(guild, make_channel(STAFF_CHANNEL_ID), make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).ratedvc.callback(cog, ctx, CONTEST_ID, make_member())

    assert str(refused.value) == text
    # Codeforces wasn't asked anything, so the member's cooldown isn't spent.
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


async def test_ratedvc_refused_after_asking_codeforces_spends_its_cooldown(
    cog: Contests,
    bot: MagicMock,
    user_db: UserDbConn,
    guild: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot.cf_cache.contest_cache.get_contest.return_value = make_contest('FINISHED')
    # Too few contestants were rated.
    monkeypatch.setattr(cf.contest, 'ratingChanges', AsyncMock(return_value=[]))
    await user_db.set_rated_vc_channel(GUILD_ID, BOT_CHANNEL_ID)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    with pytest.raises(ContestCogError, match="can't be a rated virtual contest"):
        await type(cog).ratedvc.callback(cog, ctx, CONTEST_ID, make_member())

    ctx.command.reset_cooldown.assert_not_called()


@pytest.fixture
async def rated(user_db: UserDbConn) -> None:
    """Alice, whose handle is tourist, finished a rated virtual contest with
    a rating of 1600.
    """
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'tourist')
    vc_id = await user_db.create_rated_vc(CONTEST_ID, 0.0, 1.0, GUILD_ID, [MEMBER_ID])
    await user_db.update_vc_rating(vc_id, MEMBER_ID, 1600)


class Typing:
    """Stands in for ``ctx.typing()``, noting when it starts and ends."""

    def __init__(self, steps: list[str]) -> None:
        self.steps = steps

    async def __aenter__(self) -> None:
        self.steps.append('typing')

    async def __aexit__(self, *exc_info: Any) -> None:
        self.steps.append('typed')


@pytest.mark.usefixtures('rated')
async def test_vcratings_defers_its_answer_before_finding_the_members(
    cog: Contests, guild: MagicMock
) -> None:
    # On slash, typing defers the answer, which must come within 3 seconds.
    steps: list[str] = []
    member = make_member()
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), member)
    ctx.typing.return_value = Typing(steps)
    ctx.send.side_effect = lambda *args, **kwargs: steps.append('answered')

    async def find(ctx: Any, argument: str) -> MagicMock:
        steps.append('found a member')
        return member

    cog.member_converter = MagicMock(convert=AsyncMock(side_effect=find))

    await type(cog).vcratings.callback(cog, ctx)

    assert steps == ['typing', 'found a member', 'typed', 'answered']
