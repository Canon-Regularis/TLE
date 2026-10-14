"""Tests for TLE's Contests cog (tle.cogs.contests): contest reminders,
ranklists and rated virtual contests.

Commands run by calling their callbacks, as discord.py does once it has parsed
the arguments, with a real in-memory user database (the ``user_db`` fixture)
and, where channels matter, a real access service. Discord itself (contexts,
the server, its channels and members) is mocked, but roles are real
``discord.Role`` objects, so that the checks of which roles members may give
themselves run as they do in a server. The rated virtual contest watcher runs
one tick at a time, on the test's clock, with Codeforces' submissions and the
ranklist cache's standings faked.
"""

import itertools
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
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
    RANKLIST_LISTED_NOTE,
    RANKLIST_OFFICIAL_NOTE,
    RANKLIST_PREDICTED_NOTE,
    RATED_VC_CHANNEL_SET_TEXT,
    RATED_VC_START_TEXT,
    UNASSIGNABLE_REMINDER_ROLE_TEXT,
    UNLISTED_NOTE,
    UNSCORABLE_RATED_VC_TEXT,
    UNSUITABLE_REMINDER_ROLE_TEXT,
    VC_RANKLIST_NOTE,
    ContestCogError,
    Contests,
)
from tle.config import Settings
from tle.util import codeforces_api as cf, codeforces_common as cf_common, tasks
from tle.util.cache import ContestNotFound, RanklistNotMonitored
from tle.util.codeforces_api import (
    Contest,
    Member,
    Party,
    Problem,
    ProblemResult,
    RanklistRow,
    RatingChange,
    Submission,
)
from tle.util.db.user_db_conn import UserDbConn
from tle.util.discord_common import NOT_IN_A_THREAD_MESSAGE
from tle.util.ranklist import Ranklist, virtual

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


def watermelon(contest: Contest) -> Problem:
    """The problem A of ``contest``."""
    return Problem(
        contestId=contest.id,
        problemsetName=None,
        index='A',
        name='Watermelon',
        type='PROGRAMMING',
        points=500.0,
        rating=800,
        tags=[],
    )


def make_ranklist(contest: Contest, handle: str) -> Ranklist:
    """The standings of ``contest``, which ``handle`` won with problem A."""
    problem = watermelon(contest)
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


def make_row(
    handle: str,
    points: float,
    penalty: int = 0,
    *,
    rank: int,
    team: Sequence[str] = (),
) -> RanklistRow:
    """A contestant's row, or with ``team`` a team's of those members: problem
    A solved if it has points.
    """
    party = Party(
        contestId=CONTEST_ID,
        members=[Member(handle=member) for member in team or [handle]],
        participantType='CONTESTANT',
        teamId=1 if team else None,
        teamName=handle if team else None,
        ghost=False,
        room=None,
        startTimeSeconds=None,
    )
    result = ProblemResult(
        points=points,
        penalty=None,
        rejectedAttemptCount=0,
        type='FINAL',
        bestSubmissionTimeSeconds=600 if points else None,
    )
    return RanklistRow(
        party=party, rank=rank, points=points, penalty=penalty, problemResults=[result]
    )


def make_change(handle: str, rank: int, delta: int) -> RatingChange:
    return RatingChange(
        contestId=CONTEST_ID,
        contestName=CONTEST_NAME,
        handle=handle,
        rank=rank,
        ratingUpdateTimeSeconds=0,
        oldRating=1500,
        newRating=1500 + delta,
    )


def official_ranklist(
    contest: Contest, rows: list[RanklistRow], *changes: RatingChange
) -> Ranklist:
    """``contest``'s standings with Codeforces' official ranks and rating
    changes.
    """
    ranklist = Ranklist(contest, [watermelon(contest)], rows, 0.0, is_rated=True)
    ranklist.set_official_results(changes)
    return ranklist


def with_official_ranks() -> Ranklist:
    contest = make_contest('FINISHED')
    return official_ranklist(
        contest, [make_row('tourist', 500, rank=1)], make_change('tourist', 1, 20)
    )


def with_predictions() -> Ranklist:
    contest = make_contest('FINISHED')
    rows = [make_row('tourist', 500, rank=1)]
    ranklist = Ranklist(contest, [watermelon(contest)], rows, 0.0, is_rated=True)
    ranklist.predict({'tourist': 3000})
    return ranklist


def unrated() -> Ranklist:
    return make_ranklist(make_contest('FINISHED'), 'tourist')


def of_teams() -> Ranklist:
    """A team contest's standings: final rating changes, with listed ranks."""
    contest = make_contest('FINISHED')
    return official_ranklist(
        contest,
        [make_row('tourist', 500, rank=1, team=['alice', 'bob'])],
        make_change('alice', 1, 20),
        make_change('bob', 1, 20),
    )


@pytest.mark.parametrize(
    ('make', 'footer'),
    [
        (with_official_ranks, RANKLIST_OFFICIAL_NOTE),
        (with_predictions, RANKLIST_PREDICTED_NOTE),
        (unrated, RANKLIST_LISTED_NOTE),
        (of_teams, RANKLIST_LISTED_NOTE),
    ],
    ids=['official', 'predicted', 'unrated', 'a team contest'],
)
async def test_a_ranklist_s_footer_says_what_its_ranks_count(
    cog: Contests,
    bot: MagicMock,
    guild: MagicMock,
    make: Callable[[], Ranklist],
    footer: str,
) -> None:
    ranklist = make()
    bot.cf_cache.contest_cache.get_contest.return_value = ranklist.contest
    bot.cf_cache.ranklist_cache.get_ranklist.return_value = ranklist
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    _, contest_embed, _ = ctx.send.await_args_list
    assert contest_embed.kwargs['embed'].footer.text == footer


async def test_ranklist_of_a_contest_no_longer_monitored_asks_for_its_rating_changes(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    # Once a contest's rating changes are saved, the ranklist cache stops
    # monitoring it, so its ranklist comes with Codeforces' official ranks.
    ranklist = with_official_ranks()
    bot.cf_cache.contest_cache.get_contest.return_value = ranklist.contest
    cache = bot.cf_cache.ranklist_cache
    cache.get_ranklist.side_effect = RanklistNotMonitored(ranklist.contest)
    cache.generate_ranklist = AsyncMock(return_value=ranklist)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist')

    cache.generate_ranklist.assert_awaited_once_with(
        CONTEST_ID, fetch_changes=True, rated_only=False
    )
    _, contest_embed, _ = ctx.send.await_args_list
    assert contest_embed.kwargs['embed'].footer.text == RANKLIST_OFFICIAL_NOTE


def data_lines(page: str) -> list[list[str]]:
    """The cells of each row of standings on ``page``, without blanks."""
    return [
        line.split()
        for line in page.splitlines()
        if line.split() and line.split()[0].isdigit()
    ]


async def test_a_member_left_out_of_the_public_standings_shows_their_official_rank(
    cog: Contests, bot: MagicMock, guild: MagicMock
) -> None:
    # A Div. 1 round can leave rated contestants out too.
    contest = make_contest('FINISHED')._replace(type='ICPC')
    ranklist = official_ranklist(
        contest,
        [make_row('tourist', 1, 30, rank=1)],
        make_change('hidden', 1, 50),
        make_change('tourist', 2, 20),
    )
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    bot.cf_cache.ranklist_cache.get_ranklist.return_value = ranklist
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).ranklist.callback(cog, ctx, CONTEST_ID, 'tourist', 'HIDDEN')

    *_, standings = ctx.send.await_args_list
    page = standings.args[0]
    # No results: just the official rank and rating change, marked.
    assert data_lines(page) == [
        ['1', 'hidden:*', '+50'],
        ['2', 'tourist:', '1', '30', '+', '+20'],
    ]
    assert UNLISTED_NOTE in page.splitlines()


def test_a_member_left_out_has_a_blank_cell_for_each_column(cog: Contests) -> None:
    # table.Table needs every cell.
    contest = make_contest('FINISHED')
    ranklist = official_ranklist(
        contest, [make_row('tourist', 500, rank=2)], make_change('hidden', 1, 50)
    )
    hidden = [('hidden', ranklist.get_standing_row('hidden'))]

    *_, cf_body = cog._get_cf_or_ioi_standings_table(
        ['A', 'B'], hidden, [50], mode='cf'
    )
    *_, icpc_body = cog._get_icpc_standings_table(['A', 'B'], hidden, [50])

    assert cf_body == [[1, 'hidden:*', '', '', '', '+50']]
    assert icpc_body == [[1, 'hidden:*', '', '', '', '', '+50']]


def test_only_a_page_with_a_member_left_out_explains_the_mark(cog: Contests) -> None:
    contest = make_contest('FINISHED')
    rows = [
        make_row(f'member{rank:02}', 600 - rank, rank=rank) for rank in range(2, 19)
    ]
    ranklist = official_ranklist(
        contest,
        rows,
        make_change('hidden', 1, 50),
        *(make_change(f'member{rank:02}', rank, 10) for rank in range(2, 19)),
    )
    standings = [('hidden', ranklist.get_standing_row('hidden'))] + [
        (Ranklist.get_ranklist_lookup_key(row), row) for row in ranklist.standings
    ]

    pages = cog._make_standings_pages(contest, ['A'], standings, [50] + [10] * 17)

    first, second = (content for content, _ in pages)
    assert UNLISTED_NOTE in first.splitlines()
    assert UNLISTED_NOTE not in second


async def test_standings_go_by_rank_then_by_score(
    cog: Contests, bot: MagicMock
) -> None:
    # An Educational round's unrated row takes the rank of the rated row below
    # it, and shows above it, with its better score.
    contest = make_contest('FINISHED')._replace(
        type='ICPC', name='Educational Codeforces Round 183 (Rated for Div. 2)'
    )
    ranklist = official_ranklist(
        contest,
        [
            make_row('cherry', 6, 100, rank=1),
            make_row('strong', 5, 50, rank=2),
            make_row('bob', 5, 80, rank=3),
        ],
        make_change('cherry', 1, 90),
        make_change('hidden', 2, 70),
        make_change('bob', 3, 10),
    )
    bot.cf_cache.contest_cache.get_contest.return_value = contest
    channel = make_channel(BOT_CHANNEL_ID)

    await cog._show_ranklist(
        channel, CONTEST_ID, ['bob', 'strong', 'hidden', 'cherry'], ranklist
    )

    page = channel.send.await_args.args[0]
    assert [cells[:2] for cells in data_lines(page)] == [
        ['1', 'cherry:'],
        ['2', 'hidden:*'],
        ['3', 'strong:'],
        ['3', 'bob:'],
    ]


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


async def test_ratedvc_refuses_a_contest_it_cannot_score_before_asking_codeforces(
    cog: Contests,
    bot: MagicMock,
    user_db: UserDbConn,
    guild: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot.cf_cache.contest_cache.get_contest.return_value = make_contest(
        'FINISHED'
    )._replace(type='IOI')
    rating_changes = AsyncMock(return_value=[])
    monkeypatch.setattr(cf.contest, 'ratingChanges', rating_changes)
    await user_db.set_rated_vc_channel(GUILD_ID, BOT_CHANNEL_ID)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    with pytest.raises(ContestCogError) as refused:
        await type(cog).ratedvc.callback(cog, ctx, CONTEST_ID, make_member())

    assert str(refused.value) == UNSCORABLE_RATED_VC_TEXT.format(name=CONTEST_NAME)
    rating_changes.assert_not_awaited()
    ctx.command.reset_cooldown.assert_called_once_with(ctx)


async def test_ratedvc_asks_its_members_to_start_on_their_own_within_10_minutes(
    cog: Contests,
    bot: MagicMock,
    user_db: UserDbConn,
    guild: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    clock: 'Clock',
) -> None:
    bot.cf_cache.contest_cache.get_contest.return_value = make_contest('FINISHED')
    changes = [make_change(f'rated{rank}', rank, 0) for rank in range(1, 51)]
    monkeypatch.setattr(cf.contest, 'ratingChanges', AsyncMock(return_value=changes))
    monkeypatch.setattr(cf_common, 'user_db', user_db)
    monkeypatch.setattr(
        cf_common, 'get_visited_contests', AsyncMock(return_value=set())
    )
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'alice')
    await user_db.set_rated_vc_channel(GUILD_ID, BOT_CHANNEL_ID)
    ctx = make_ctx(guild, make_channel(BOT_CHANNEL_ID), make_member())

    await type(cog).ratedvc.callback(cog, ctx, CONTEST_ID, make_member())

    _, alert = ctx.send.await_args_list
    # The contest's 2 hours, and the 10 minutes to start it.
    assert alert.kwargs['embed'].description == (
        'You have 130 minutes to finish the contest!\n'
        + RATED_VC_START_TEXT.format(minutes=10)
    )
    [vc_id] = await user_db.get_ongoing_rated_vc_ids()
    vc = await user_db.get_rated_vc(vc_id)
    assert (vc.start_time, vc.finish_time) == (clock.now, clock.now + 130 * 60)


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


# The rated virtual contest watcher

OTHER_ID = 1_400_000_000_000_000_002
THIRD_ID = 1_400_000_000_000_000_003
# A rated virtual contest of the 2-hour contest, which ends 10 minutes later,
# as its members have 10 minutes to start it.
VC_START = 1_700_000_000
VC_FINISH = VC_START + 7200 + 10 * 60
SUBMISSION_IDS = itertools.count(1)


class Clock:
    """The time, which the tests move on."""

    def __init__(self) -> None:
        self.now: float = VC_START

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The cog's clock."""
    clock = Clock()
    monkeypatch.setattr(contests, 'time', SimpleNamespace(time=clock))
    return clock


def virtual_submission(
    handle: str,
    minutes: float,
    verdict: str | None = 'OK',
    *,
    started: int = VC_START + 60,
) -> Submission:
    """``handle``'s virtual submission for problem A, ``minutes`` after they
    started the contest on Codeforces, at ``started``.
    """
    created = started + round(60 * minutes)
    return Submission(
        id=next(SUBMISSION_IDS),
        contestId=CONTEST_ID,
        problem=watermelon(make_contest('FINISHED')),
        author=Party(
            contestId=CONTEST_ID,
            members=[Member(handle=handle)],
            participantType='VIRTUAL',
            teamId=None,
            teamName=None,
            ghost=False,
            room=None,
            startTimeSeconds=started,
        ),
        programmingLanguage='C++',
        verdict=verdict,
        creationTimeSeconds=created,
        relativeTimeSeconds=created - started,
        passedTestCount=3,
    )


class Submissions:
    """Codeforces' answers to contest.status: each handle's submissions to the
    contest, newest first. Each request is recorded.
    """

    def __init__(self) -> None:
        self.by_handle: dict[str, list[Submission]] = {}
        self.errors: dict[str, Exception] = {}
        self.requests: list[str] = []
        # Runs as each request is answered: time passes meanwhile.
        self.answering: Callable[[], None] = lambda: None

    async def status(
        self, *, contest_id: int, handle: str | None = None
    ) -> list[Submission]:
        assert contest_id == CONTEST_ID
        assert handle is not None
        self.requests.append(handle)
        self.answering()
        if handle in self.errors:
            raise self.errors[handle]
        return sorted(
            self.by_handle.get(handle, []),
            key=lambda submission: -submission.creationTimeSeconds,
        )


@pytest.fixture
def submissions(monkeypatch: pytest.MonkeyPatch) -> Submissions:
    fake = Submissions()
    monkeypatch.setattr(cf.contest, 'status', fake.status)
    return fake


class VcStandings:
    """The ranklist cache's standings of a rated virtual contest: a point for
    each accepted submission, and the rating change that the test sets for
    each handle. Records the participations it scores.
    """

    def __init__(self) -> None:
        self.deltas: dict[str, int] = {}
        self.scored: list[dict[str, virtual.VirtualParticipation]] = []

    async def generate_vc_ranklist(
        self,
        contest_id: int,
        handle_to_member_id: Mapping[str, Any],
        participations: Mapping[str, virtual.VirtualParticipation],
    ) -> Ranklist:
        assert contest_id == CONTEST_ID
        self.scored.append(dict(participations))
        contest = make_contest('FINISHED')
        rows = [
            RanklistRow(
                party=participation.party,
                rank=1,
                points=float(
                    sum(
                        submission.verdict == 'OK'
                        for submission in participation.submissions
                    )
                ),
                penalty=0,
                problemResults=[],
            )
            for participation in participations.values()
        ]
        ranklist = Ranklist(contest, [watermelon(contest)], rows, 0.0, is_rated=True)
        ranklist.delta_by_handle = {
            handle: self.deltas.get(handle, 0) for handle in participations
        }
        ranklist.deltas_status = 'Predicted'
        return ranklist


@pytest.fixture
def vc_standings(bot: MagicMock) -> VcStandings:
    standings = VcStandings()
    bot.cf_cache.ranklist_cache.generate_vc_ranklist = standings.generate_vc_ranklist
    bot.cf_cache.contest_cache.get_contest.return_value = make_contest('FINISHED')
    return standings


def vc_member(member_id: int) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=member_id)
    member.mention = f'<@{member_id}>'
    return member


@pytest.fixture
def vc_channel(bot: MagicMock, guild: MagicMock) -> MagicMock:
    """The rated virtual contest channel, which the bot finds; posts in it are
    recorded.
    """
    channel = make_channel(BOT_CHANNEL_ID)
    channel.guild = guild
    members = {
        member_id: vc_member(member_id) for member_id in (MEMBER_ID, OTHER_ID, THIRD_ID)
    }
    guild.get_member.side_effect = members.get
    bot.get_channel.side_effect = {BOT_CHANNEL_ID: channel}.get
    return channel


async def start_vc(user_db: UserDbConn, handles: Mapping[int, str | None]) -> int:
    """A rated virtual contest for the members with ``handles``, by id; a
    member with None has no handle.
    """
    await user_db.set_rated_vc_channel(GUILD_ID, BOT_CHANNEL_ID)
    for member_id, handle in handles.items():
        if handle is not None:
            await user_db.set_handle(member_id, GUILD_ID, handle)
    vc_id = await user_db.create_rated_vc(
        CONTEST_ID, VC_START, VC_FINISH, GUILD_ID, list(handles)
    )
    assert vc_id is not None
    return vc_id


async def tick(cog: Contests) -> None:
    """Run the rated virtual contest watcher once, as its task does every 5
    minutes.
    """
    task = cog._watch_rated_vcs_task
    assert isinstance(task, tasks.Task)
    await task.func(cog, None)


async def vc_rating(user_db: UserDbConn, member_id: int) -> int | None:
    return await user_db.get_vc_rating(str(member_id), default_if_not_exist=False)


def logged(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str, bool]]:
    """What the cog logged: each record's level, text and whether it has a
    traceback.
    """
    return [
        (record.levelno, record.getMessage(), record.exc_info is not None)
        for record in caplog.records
        if record.name == 'Contests'
    ]


async def test_while_a_rated_virtual_contest_runs_its_standings_are_posted(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    vc_id = await start_vc(
        user_db, {MEMBER_ID: 'alice', OTHER_ID: 'bob', THIRD_ID: None}
    )
    submissions.by_handle['alice'] = [virtual_submission('alice', 10)]
    clock.now = VC_START + 30 * 60

    await tick(cog)

    # One request for each member with a handle.
    assert sorted(submissions.requests) == ['alice', 'bob']
    [scored] = vc_standings.scored
    assert list(scored) == ['alice']
    embed_post, standings_post = vc_channel.send.await_args_list
    assert embed_post.kwargs['embed'].footer.text == VC_RANKLIST_NOTE
    assert data_lines(standings_post.args[0]) == [['1', 'alice:#', '1', '+0']]
    assert await user_db.get_ongoing_rated_vc_ids() == [vc_id]


async def test_at_its_end_a_rated_virtual_contest_saves_and_posts_the_ratings(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    vc_id = await start_vc(
        user_db, {MEMBER_ID: 'alice', OTHER_ID: 'bob', THIRD_ID: None}
    )
    submissions.by_handle['alice'] = [virtual_submission('alice', 10)]
    vc_standings.deltas['alice'] = 40
    clock.now = VC_FINISH + 60

    await tick(cog)

    assert await user_db.get_ongoing_rated_vc_ids() == []
    assert await vc_rating(user_db, MEMBER_ID) == 1540
    # Those who took no part leave it, so that it doesn't rate them.
    assert await user_db.get_rated_vc_user_ids(vc_id) == [str(MEMBER_ID)]
    results, standings_post = vc_channel.send.await_args_list
    embed = results.kwargs['embed']
    assert embed.author.name == 'Virtual contest results'
    assert '1500 \N{HORIZONTAL BAR} **+40** \N{LONG RIGHTWARDS ARROW} 1540' in (
        embed.fields[0].value
    )
    assert data_lines(standings_post.args[0]) == [['1', 'alice:#', '1', '+40']]


@pytest.mark.parametrize(
    'error',
    [
        cf.HandleNotFoundError('handle: User with handle renamed not found', 'renamed'),
        cf.HandleInvalidError(
            'handle: Field should contain only Latin letters, digits, underscore '
            'or dash characters',
            'renamed',
        ),
    ],
    ids=['not found', 'invalid'],
)
async def test_a_handle_codeforces_does_not_know_takes_no_part(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
    caplog: pytest.LogCaptureFixture,
    error: cf.TrueApiError,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice', OTHER_ID: 'renamed'})
    submissions.by_handle['alice'] = [virtual_submission('alice', 10)]
    submissions.errors['renamed'] = error
    vc_standings.deltas['alice'] = 40
    clock.now = VC_FINISH + 60
    caplog.set_level(logging.INFO, logger='Contests')

    await tick(cog)

    assert sorted(submissions.requests) == ['alice', 'renamed']
    [(level, text, traceback)] = logged(caplog)
    assert (level, traceback) == (logging.INFO, False)
    assert text.startswith(f'Rated virtual contest {vc_id}: renamed took no part.')
    assert await user_db.get_rated_vc_user_ids(vc_id) == [str(MEMBER_ID)]
    assert await vc_rating(user_db, MEMBER_ID) == 1540


async def test_a_submission_still_being_judged_keeps_the_contest_open(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice'})
    submissions.by_handle['alice'] = [
        virtual_submission('alice', 10),
        virtual_submission('alice', 125, 'TESTING'),
    ]
    clock.now = VC_FINISH + 60

    await tick(cog)

    alert, _, standings_post = vc_channel.send.await_args_list
    assert alert.kwargs['embed'].description == (
        'Some submissions are still being judged'
    )
    assert 'alice:#' in standings_post.args[0]
    assert await user_db.get_ongoing_rated_vc_ids() == [vc_id]
    assert await vc_rating(user_db, MEMBER_ID) is None


async def test_submissions_after_its_end_count_for_nothing(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    # Alice started 40 minutes late: her virtual contest on Codeforces goes on
    # after the rated one has ended.
    await start_vc(user_db, {MEMBER_ID: 'alice'})
    started = VC_START + 40 * 60
    in_time = virtual_submission('alice', 10, started=started)
    submissions.by_handle['alice'] = [
        in_time,
        virtual_submission('alice', 115, started=started),
        virtual_submission('alice', 118, 'TESTING', started=started),
    ]
    vc_standings.deltas['alice'] = 25
    clock.now = VC_FINISH + 60

    await tick(cog)

    # Scored on a shorter contest, and not held open by the late submission
    # still being judged.
    [scored] = vc_standings.scored
    assert scored['alice'].submissions == (in_time,)
    assert await user_db.get_ongoing_rated_vc_ids() == []
    assert await vc_rating(user_db, MEMBER_ID) == 1525


async def test_a_member_may_start_up_to_10_minutes_before_it(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    # Alice started on Codeforces 10 minutes before the bot's command, and Bob
    # a second earlier still; both submitted after it.
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice', OTHER_ID: 'bob'})
    submissions.by_handle['alice'] = [
        virtual_submission('alice', 15, started=VC_START - 10 * 60)
    ]
    submissions.by_handle['bob'] = [
        virtual_submission('bob', 15, started=VC_START - 10 * 60 - 1)
    ]
    vc_standings.deltas.update(alice=40, bob=-30)
    clock.now = VC_FINISH + 60

    await tick(cog)

    [scored] = vc_standings.scored
    assert list(scored) == ['alice']
    assert await vc_rating(user_db, MEMBER_ID) == 1540
    assert await vc_rating(user_db, OTHER_ID) is None
    assert await user_db.get_rated_vc_user_ids(vc_id) == [str(MEMBER_ID)]


async def test_a_contest_that_ends_while_its_submissions_come_waits_a_tick(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice'})
    submissions.by_handle['alice'] = [virtual_submission('alice', 10)]
    vc_standings.deltas['alice'] = 40
    clock.now = VC_FINISH - 1

    def time_passes() -> None:
        clock.now += 5

    submissions.answering = time_passes

    # Its submissions came from before its end, so they don't finish it.
    await tick(cog)
    assert await user_db.get_ongoing_rated_vc_ids() == [vc_id]

    await tick(cog)
    assert await user_db.get_ongoing_rated_vc_ids() == []
    assert await vc_rating(user_db, MEMBER_ID) == 1540


async def test_a_finish_tried_again_after_a_failure_changes_each_rating_once(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice', OTHER_ID: 'bob'})
    for handle in ('alice', 'bob'):
        submissions.by_handle[handle] = [virtual_submission(handle, 10)]
    vc_standings.deltas.update(alice=40, bob=-30)
    clock.now = VC_FINISH + 60
    # The database fails as the second rating is saved.
    save = user_db.update_vc_rating
    saves = itertools.count(1)

    async def failing_once(vc_id: int, user_id: str, rating: int) -> None:
        if next(saves) == 2:
            raise sqlite3.OperationalError('disk I/O error')
        await save(vc_id, user_id, rating)

    monkeypatch.setattr(user_db, 'update_vc_rating', failing_once)

    await tick(cog)
    assert await user_db.get_ongoing_rated_vc_ids() == [vc_id]
    await tick(cog)

    assert await user_db.get_ongoing_rated_vc_ids() == []
    assert await vc_rating(user_db, MEMBER_ID) == 1540
    assert await vc_rating(user_db, OTHER_ID) == 1470


async def test_a_contest_whose_channel_is_gone_finishes_an_hour_late_unposted(
    cog: Contests,
    bot: MagicMock,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot.get_channel.return_value = None
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice', OTHER_ID: 'bob'})
    submissions.by_handle['alice'] = [virtual_submission('alice', 10)]
    vc_standings.deltas['alice'] = 40
    caplog.set_level(logging.INFO, logger='Contests')

    # It waits for its channel, without asking Codeforces anything.
    clock.now = VC_FINISH + 59 * 60
    await tick(cog)
    assert submissions.requests == []
    assert await user_db.get_ongoing_rated_vc_ids() == [vc_id]

    clock.now = VC_FINISH + 61 * 60
    await tick(cog)

    # Its members' handles come from its server.
    assert sorted(submissions.requests) == ['alice', 'bob']
    assert await user_db.get_ongoing_rated_vc_ids() == []
    assert await vc_rating(user_db, MEMBER_ID) == 1540
    assert await user_db.get_rated_vc_user_ids(vc_id) == [str(MEMBER_ID)]
    assert logged(caplog) == [
        (logging.INFO, f'Rated virtual contest {vc_id} has no channel yet.', False),
        (
            logging.WARNING,
            f'Rated virtual contest {vc_id} finished without a post: its channel '
            'is gone.',
            False,
        ),
    ]


async def test_a_contest_unfinished_an_hour_after_its_end_is_warned_of_once(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice'})
    submissions.errors['alice'] = cf.ClientError()
    caplog.set_level(logging.INFO, logger='Contests')

    for minutes in (30, 55, 60, 65):
        clock.now = VC_FINISH + minutes * 60
        await tick(cog)

    error = "ClientError('Error connecting to Codeforces API')"
    not_updated = (
        logging.INFO,
        f'Rated virtual contest {vc_id} not updated. {error}',
        False,
    )
    assert logged(caplog) == [
        not_updated,
        not_updated,
        not_updated,
        (
            logging.WARNING,
            f'Rated virtual contest {vc_id} has not finished an hour after its '
            f'end; the last error: {error}',
            False,
        ),
        not_updated,
    ]


async def test_an_overdue_contest_that_did_not_fail_is_warned_of_without_an_error(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    vc_id = await start_vc(user_db, {MEMBER_ID: 'alice'})
    submissions.errors['alice'] = cf.ClientError()
    clock.now = VC_FINISH + 30 * 60
    await tick(cog)
    # Codeforces answers again, but a submission is still being judged.
    del submissions.errors['alice']
    submissions.by_handle['alice'] = [virtual_submission('alice', 10, 'TESTING')]
    caplog.set_level(logging.WARNING, logger='Contests')

    clock.now = VC_FINISH + 60 * 60
    await tick(cog)

    assert logged(caplog) == [
        (
            logging.WARNING,
            f'Rated virtual contest {vc_id} has not finished an hour after its end',
            False,
        )
    ]


async def test_one_contest_s_failure_does_not_hold_the_others_back(
    cog: Contests,
    user_db: UserDbConn,
    clock: Clock,
    submissions: Submissions,
    vc_standings: VcStandings,
    vc_channel: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    down = await start_vc(user_db, {MEMBER_ID: 'alice'})
    broken = await start_vc(user_db, {OTHER_ID: 'bob'})
    await start_vc(user_db, {THIRD_ID: 'carol'})
    submissions.errors['alice'] = cf.ClientError()
    submissions.errors['bob'] = RuntimeError('broken')
    submissions.by_handle['carol'] = [virtual_submission('carol', 10)]
    clock.now = VC_START + 30 * 60
    caplog.set_level(logging.INFO, logger='Contests')

    await tick(cog)

    # A Codeforces failure is no bug: the next tick tries again.
    assert logged(caplog) == [
        (
            logging.INFO,
            f'Rated virtual contest {down} not updated. '
            "ClientError('Error connecting to Codeforces API')",
            False,
        ),
        (
            logging.WARNING,
            f"Rated virtual contest {broken} not updated. RuntimeError('broken')",
            True,
        ),
    ]
    assert 'carol:#' in vc_channel.send.await_args.args[0]
