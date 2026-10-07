"""TLE's Codeforces commands at work in the booted bot: the gitgud commands,
gimme, ranklist and ratedvc, and how their cooldowns count uses.

The bot boots as ``booting.booted`` boots it, with every extension and TLE's
own rule table, in a server with one bot channel. Commands run as discord.py
runs them: a prefix command from its message, as ``process_commands`` runs
it, and a slash command from its interaction's data, through the bot's
command tree. Failures are answered by the cog's error handler, or by the
bot's, which discord.py schedules as an event. Discord and Codeforces are
stood in for: the server, its channels, the members, messages and
interactions are mocked, posts in a channel are recorded, and Codeforces
answers without a request.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from tests.kcpc.component.booting import booted
from tle import constants
from tle.__main__ import TLEBot
from tle.access.settings import GuildAccess
from tle.util import codeforces_api as cf, codeforces_common as cf_common
from tle.util.cache import ContestNotFound, RanklistNotMonitored
from tle.util.codeforces_api import (
    Contest,
    Member,
    Party,
    Problem,
    ProblemResult,
    RanklistRow,
    User,
)
from tle.util.ranklist import Ranklist

# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
BOT_CHANNEL_ID = 1_200_000_000_000_000_001
GENERAL_ID = 1_200_000_000_000_000_020  # not a bot channel
MEMBER_ID = 1_400_000_000_000_000_001
OTHER_ID = 1_400_000_000_000_000_002
BOT_USER_ID = 1_400_000_000_000_000_050
# Every member's linked handle, and its rating.
HANDLE = 'tourist'
RATING = 1500
# When a challenge was issued.
ISSUED = 1_000.0
# The reply to a gitgud command used while another of the member's is running.
RUNNING = 'You already have a gitgud command running. Try again when it finishes.'

CONTEST = Contest(
    id=1,
    name='Codeforces Round #1',
    startTimeSeconds=1_000_000,
    durationSeconds=7200,
    type='CF',
    phase='FINISHED',
    preparedBy=None,
)
# The only problem in the problem cache, at the handle's rating.
PROBLEM = Problem(
    contestId=1,
    problemsetName=None,
    index='A',
    name='Easy',
    type='PROGRAMMING',
    points=None,
    rating=RATING,
    tags=[],
)
# A contest in the bot's contest list that hasn't started.
UPCOMING = Contest(
    id=2,
    name='Codeforces Round #2',
    startTimeSeconds=4_000_000_000,
    durationSeconds=7200,
    type='CF',
    phase='BEFORE',
    preparedBy=None,
)
UNKNOWN_CONTEST_ID = 3  # not in the bot's contest list
# The commands of TLE's cogs that have a cooldown and take arguments.
COOLED_DOWN = [
    'upsolve',
    'gimme',
    'stalk',
    'mashup',
    'gitgud',
    'vc',
    'fullsolve',
    'teamrate',
    'ranklist',
    'ratedvc',
    'vcrating',
    'duel challenge',
    'duel rating',
    *(
        f'plot {name}'
        for name in (
            'rating',
            'extreme',
            'solved',
            'hist',
            'curve',
            'scatter',
            'cfdistrib',
            'centile',
            'howgud',
            'country',
            'visualrank',
            'speed',
        )
    ),
]
WAIT_TEXT = 'Generating ranklist, please wait...'


@pytest.fixture(autouse=True)
def tle_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """TLE's roles by name, and no developer role, as without a .env."""
    monkeypatch.setattr(constants, 'TLE_ADMIN', 'Admin')
    monkeypatch.setattr(constants, 'TLE_MODERATOR', 'Moderator')
    monkeypatch.setattr(constants, 'TLE_TRUSTED', 'Trusted')
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', None)


@pytest.fixture
async def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TLEBot]:
    """The bot with every extension, in a server with one bot channel.

    Its user database knows the handle's rating, and its problem cache holds
    PROBLEM.
    """
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        # Who the bot is, which discord.py learns as it logs in.
        me = MagicMock(spec=discord.ClientUser, id=BOT_USER_ID)
        monkeypatch.setattr(TLEBot, 'user', property(lambda _: me))
        settings = GuildAccess(frozenset({BOT_CHANNEL_ID}))
        await bot.access.change(GUILD_ID, lambda _: settings)
        await bot.user_db.cache_cf_user(
            User(
                handle=HANDLE,
                firstName=None,
                lastName=None,
                country=None,
                city=None,
                organization=None,
                contribution=0,
                rating=RATING,
                maxRating=RATING,
                lastOnlineTimeSeconds=0,
                registrationTimeSeconds=0,
                friendOfCount=0,
                titlePhoto='https://example.com/photo.jpg',
            )
        )
        cf_cache = MagicMock()
        cf_cache.problem_cache.problems = [PROBLEM]
        cf_cache.contest_cache.get_contest.return_value = CONTEST
        cf_cache.conn.close = AsyncMock()  # the bot closes it as it stops
        bot.cf_cache = cf_cache
        yield bot


@pytest.fixture
def steps() -> list[str]:
    """What happened, in order: the answers to a slash command, and the
    requests to Codeforces.
    """
    return []


@pytest.fixture
def codeforces(
    bot: TLEBot, monkeypatch: pytest.MonkeyPatch, steps: list[str]
) -> AsyncMock:
    """Codeforces' list of a handle's submissions, answered without a
    request: there are none. Each request is logged in ``steps``.

    Every member's handle is HANDLE.
    """

    async def status(**kwargs: Any) -> list[Any]:
        steps.append('asked Codeforces')
        return []

    request = AsyncMock(side_effect=status)
    monkeypatch.setattr(cf.user, 'status', request)
    monkeypatch.setattr(cf_common, 'resolve_handles', AsyncMock(return_value=[HANDLE]))
    # For is_nonstandard_problem, which reads the cache from cf_common.
    monkeypatch.setattr(cf_common, 'cf_cache', bot.cf_cache)
    return request


@pytest.fixture
def guild() -> MagicMock:
    """The server, whose channels every member can see, and where the bot's
    typing indicator shows without a request.
    """
    guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    channels = {
        channel_id: MagicMock(
            spec=discord.TextChannel,
            id=channel_id,
            guild=guild,
            mention=f'<#{channel_id}>',
        )
        for channel_id in (BOT_CHANNEL_ID, GENERAL_ID)
    }
    for channel in channels.values():
        channel.permissions_for.return_value = discord.Permissions(view_channel=True)
        channel._state = SimpleNamespace(http=SimpleNamespace(send_typing=AsyncMock()))
    guild.get_channel.side_effect = channels.get
    return guild


def make_member(guild: MagicMock, member_id: int, **permissions: bool) -> MagicMock:
    """A member of ``guild`` with no role, and only the server permissions
    given.
    """
    member = MagicMock(spec=discord.Member, id=member_id, guild=guild, bot=False)
    member.mention = f'<@{member_id}>'
    member.guild_permissions = discord.Permissions(**permissions)
    member.roles = []
    return member


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Records what would be posted in a channel, for everyone to see."""
    post = AsyncMock(return_value=MagicMock(spec=discord.Message))
    monkeypatch.setattr(discord.abc.Messageable, 'send', post)
    return post


async def handled() -> None:
    """Let the events that discord.py has scheduled, such as the bot's error
    handler, run to the end.
    """
    while True:
        pending = [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith('discord.py: ') and not task.done()
        ]
        if not pending:
            return
        await asyncio.wait(pending)


async def use_prefix(
    bot: TLEBot, member: MagicMock, channel_id: int, content: str
) -> None:
    """Send ``content`` as ``member`` in a channel, and wait until everything
    that it set off has run.
    """
    channel = member.guild.get_channel(channel_id)
    message = MagicMock(
        spec=discord.Message,
        content=content,
        author=member,
        channel=channel,
        guild=member.guild,
        jump_url='https://discord.com/channels/1/2/3',
        # Cooldowns count from when the message was sent.
        created_at=discord.utils.utcnow(),
        edited_at=None,
    )
    message.mentions = []
    # The typing indicator keeps itself going in a task on the bot's loop.
    message._state = SimpleNamespace(loop=asyncio.get_running_loop())
    ctx = await bot.get_context(message)
    await bot.invoke(ctx)
    await handled()


def _whom(ephemeral: bool) -> str:
    return 'privately' if ephemeral else 'publicly'


def _logged(steps: list[str], what: str) -> Callable[..., Awaitable[Any]]:
    """An answer to an interaction that logs ``what`` was done, and for whom."""

    async def answer(*args: Any, ephemeral: bool = False, **kwargs: Any) -> Any:
        steps.append(f'{what} {_whom(ephemeral)}')
        return MagicMock(resource=MagicMock(spec=discord.InteractionMessage))

    return answer


class Response:
    """Stands in for an interaction's response, which is done once anything
    answers it.
    """

    def __init__(self, steps: list[str]) -> None:
        self.done = False
        self.send_message = AsyncMock(side_effect=self._done(steps, 'answered'))
        self.defer = AsyncMock(side_effect=self._done(steps, 'deferred'))

    def is_done(self) -> bool:
        return self.done

    def _done(self, steps: list[str], what: str) -> Callable[..., Awaitable[Any]]:
        answer = _logged(steps, what)

        async def respond(*args: Any, **kwargs: Any) -> Any:
            self.done = True
            return await answer(*args, **kwargs)

        return respond


async def use_slash(
    bot: TLEBot, member: MagicMock, channel_id: int, name: str, steps: list[str]
) -> MagicMock:
    """Use the top-level slash command ``name``, without options, as ``member``
    in a channel, through the bot's command tree. Its answers are logged in
    ``steps``. Returns its interaction, once everything that it set off has
    run.
    """
    channel = member.guild.get_channel(channel_id)
    data: dict[str, Any] = {'type': 1, 'name': name, 'options': []}
    interaction = MagicMock(spec=discord.Interaction, client=bot)
    interaction.type = discord.InteractionType.application_command
    interaction.guild_id = GUILD_ID
    interaction.user = member
    interaction.channel = channel
    interaction.data = data
    interaction.command_failed = False
    # What a real interaction works out from its data.
    command, options = bot.tree._get_app_command_options(data)
    interaction.command = command
    interaction.namespace = app_commands.Namespace(interaction, {}, options)
    # discord.py makes the context's message up from the interaction, and
    # cooldowns count from when it was sent.
    interaction.message = MagicMock(
        spec=discord.Message,
        guild=member.guild,
        author=member,
        channel=channel,
        created_at=discord.utils.utcnow(),
        edited_at=None,
    )
    interaction.is_expired.return_value = False
    interaction.response = Response(steps)
    interaction.followup.send = AsyncMock(side_effect=_logged(steps, 'followed up'))

    await bot.tree._call(interaction)
    await handled()
    return interaction


# The 3-second limit: gitgud defers its answer before asking Codeforces.


async def test_outside_the_bot_channels_gitgud_answers_only_the_member(
    bot: TLEBot,
    guild: MagicMock,
    codeforces: AsyncMock,
    steps: list[str],
    posted: AsyncMock,
) -> None:
    member = make_member(guild, MEMBER_ID)

    interaction = await use_slash(bot, member, GENERAL_ID, 'gitgud', steps)

    assert steps == ['deferred privately', 'asked Codeforces', 'followed up privately']
    followup = interaction.followup.send.await_args.kwargs
    assert followup['content'] == f'Challenge problem for `{HANDLE}`'
    posted.assert_not_awaited()


async def test_in_a_bot_channel_gitgud_answers_everyone(
    bot: TLEBot, guild: MagicMock, codeforces: AsyncMock, steps: list[str]
) -> None:
    member = make_member(guild, MEMBER_ID)

    await use_slash(bot, member, BOT_CHANNEL_ID, 'gitgud', steps)

    assert steps == ['deferred publicly', 'asked Codeforces', 'followed up publicly']


# One gitgud command at a time.


async def test_gotgud_while_gitgud_runs_tells_the_member_why_it_did_nothing(
    bot: TLEBot,
    guild: MagicMock,
    codeforces: AsyncMock,
    steps: list[str],
    posted: AsyncMock,
) -> None:
    member = make_member(guild, MEMBER_ID)
    asked, release = asyncio.Event(), asyncio.Event()

    async def slow_status(**kwargs: Any) -> list[Any]:
        asked.set()
        await release.wait()
        return []

    codeforces.side_effect = slow_status
    running = asyncio.create_task(use_slash(bot, member, BOT_CHANNEL_ID, 'gitgud', []))
    await asked.wait()
    try:
        interaction = await use_slash(bot, member, BOT_CHANNEL_ID, 'gotgud', steps)
    finally:
        release.set()
        await running

    # Privately, even in a bot channel: only the member needs to know.
    assert steps == ['answered privately']
    embed = interaction.response.send_message.await_args.kwargs['embed']
    assert embed.description == RUNNING
    posted.assert_not_awaited()


# Cooldowns.


async def test_gimme_again_within_10_seconds_says_when_it_works_again(
    bot: TLEBot, guild: MagicMock, codeforces: AsyncMock, posted: AsyncMock
) -> None:
    member = make_member(guild, MEMBER_ID)

    await use_prefix(bot, member, BOT_CHANNEL_ID, ';gimme')
    await use_prefix(bot, member, BOT_CHANNEL_ID, ';gimme')
    # Each member has a cooldown of their own.
    await use_prefix(bot, make_member(guild, OTHER_ID), BOT_CHANNEL_ID, ';gimme')

    first, again, other = (call.kwargs for call in posted.await_args_list)
    assert first['content'] == other['content'] == f'Recommended problem for `{HANDLE}`'
    assert re.fullmatch(
        r'You can use `;gimme` again in \d+ seconds?\.', again['embed'].description
    )
    # Gone once gimme works again.
    assert 0 < again['delete_after'] <= 10
    assert codeforces.await_count == 2


# A cooldown counts a use once its arguments are parsed, and a refusal that
# comes before any request to Codeforces gives the use back: a mistake costs
# no wait.


def texts(posted: AsyncMock) -> list[str | None]:
    """What each post said: its text, or else its embed's description."""
    found: list[str | None] = []
    for call in posted.await_args_list:
        text = call.kwargs.get('content')
        embed = call.kwargs.get('embed')
        if text is None and embed is not None:
            text = embed.description
        found.append(text)
    return found


def standings() -> Ranklist:
    """CONTEST's standings, in which HANDLE solved PROBLEM."""
    party = Party(
        contestId=CONTEST.id,
        members=[Member(handle=HANDLE)],
        participantType='CONTESTANT',
        teamId=None,
        teamName=None,
        ghost=False,
        room=None,
        startTimeSeconds=None,
    )
    result = ProblemResult(
        points=1.0,
        penalty=None,
        rejectedAttemptCount=0,
        type='FINAL',
        bestSubmissionTimeSeconds=600,
    )
    row = RanklistRow(
        party=party, rank=1, points=1.0, penalty=0, problemResults=[result]
    )
    return Ranklist(CONTEST, [PROBLEM], [row], 0.0, is_rated=False)


@pytest.fixture
def contests(bot: TLEBot) -> None:
    """The bot's Codeforces contest list: CONTEST, whose standings the cache
    keeps, and UPCOMING.
    """
    known = {contest.id: contest for contest in (CONTEST, UPCOMING)}

    def get_contest(contest_id: int) -> Contest:
        if contest_id not in known:
            raise ContestNotFound(contest_id)
        return known[contest_id]

    def get_ranklist(contest: Contest, show_official: bool) -> Ranklist:
        if contest != CONTEST:
            raise RanklistNotMonitored(contest)
        return standings()

    bot.cf_cache.contest_cache.get_contest.side_effect = get_contest
    bot.cf_cache.ranklist_cache.get_ranklist.side_effect = get_ranklist


async def test_every_cooldown_of_tle_s_commands_counts_parsed_uses_only(
    bot: TLEBot,
) -> None:
    # A command without parameters has nothing to parse.
    cooled_down = {
        command.qualified_name: command.cooldown_after_parsing
        for command in bot.walk_commands()
        if command.cooldown is not None
        and command.clean_params
        and type(command.cog).__module__.startswith('tle.cogs.')
    }

    assert cooled_down == dict.fromkeys(COOLED_DOWN, True)


@pytest.mark.parametrize(
    ('typed', 'refusal'),
    [
        (';gitgud soon', 'Converting to "int" failed for parameter "delta".'),
        (';gitgud 50', 'Delta must be a multiple of 100.'),
        (';gitgud 400', 'Delta must range from -300 to 300.'),
    ],
    ids=['not a number', 'not a multiple of 100', 'out of range'],
)
async def test_a_mistyped_gitgud_costs_no_wait(
    bot: TLEBot,
    guild: MagicMock,
    codeforces: AsyncMock,
    posted: AsyncMock,
    typed: str,
    refusal: str,
) -> None:
    member = make_member(guild, MEMBER_ID)

    await use_prefix(bot, member, BOT_CHANNEL_ID, typed)
    await use_prefix(bot, member, BOT_CHANNEL_ID, ';gitgud')

    assert texts(posted) == [refusal, f'Challenge problem for `{HANDLE}`']
    codeforces.assert_awaited_once()


async def test_gitgud_while_a_challenge_is_active_costs_no_wait(
    bot: TLEBot, guild: MagicMock, codeforces: AsyncMock, posted: AsyncMock
) -> None:
    member = make_member(guild, MEMBER_ID)
    await bot.user_db.new_challenge(MEMBER_ID, ISSUED, PROBLEM, 0)

    await use_prefix(bot, member, BOT_CHANNEL_ID, ';gitgud')
    await use_prefix(bot, member, BOT_CHANNEL_ID, ';gitgud')

    # The second used to get "You can use `;gitgud` again in 10 seconds."
    active = f'You have an active challenge Easy at {cf.CONTEST_BASE_URL}1/problem/A'
    assert texts(posted) == [active, active]
    codeforces.assert_not_awaited()


async def test_slash_gitgud_while_a_challenge_is_active_costs_no_wait(
    bot: TLEBot, guild: MagicMock, codeforces: AsyncMock, steps: list[str]
) -> None:
    member = make_member(guild, MEMBER_ID)
    await bot.user_db.new_challenge(MEMBER_ID, ISSUED, PROBLEM, 0)

    first = await use_slash(bot, member, BOT_CHANNEL_ID, 'gitgud', steps)
    again = await use_slash(bot, member, BOT_CHANNEL_ID, 'gitgud', steps)

    active = f'You have an active challenge Easy at {cf.CONTEST_BASE_URL}1/problem/A'
    for interaction in (first, again):
        embed = interaction.response.send_message.await_args.kwargs['embed']
        assert embed.description == active
    assert steps == ['answered privately', 'answered privately']
    codeforces.assert_not_awaited()


@pytest.mark.usefixtures('codeforces', 'contests')
@pytest.mark.parametrize(
    ('typed', 'refusal'),
    [
        (';ranklist', 'contest_id is a required argument that is missing.'),
        (';ranklist soon', 'Converting to "int" failed for parameter "contest_id".'),
        (
            f';ranklist {UNKNOWN_CONTEST_ID}',
            f'Contest with ID `{UNKNOWN_CONTEST_ID}` not found',
        ),
        (f';ranklist {UPCOMING.id}', f"`{UPCOMING.name}` hasn't started yet."),
    ],
    ids=['no contest', 'not a number', 'an unknown contest', 'not started'],
)
async def test_a_mistaken_ranklist_leaves_the_server_s_turn_to_the_others(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock, typed: str, refusal: str
) -> None:
    # Its cooldown is the whole server's: one member's mistake used to hold
    # everyone back for 30 seconds.
    member, other = make_member(guild, MEMBER_ID), make_member(guild, OTHER_ID)

    await use_prefix(bot, member, BOT_CHANNEL_ID, typed)
    assert texts(posted)[-1] == refusal
    posted.reset_mock()
    await use_prefix(bot, other, BOT_CHANNEL_ID, f';ranklist {CONTEST.id}')
    # A ranklist shown counts, for everyone.
    await use_prefix(bot, member, BOT_CHANNEL_ID, f';ranklist {CONTEST.id}')

    shown = texts(posted)
    assert shown[0] == WAIT_TEXT
    assert [text for text in shown if text and HANDLE in text]  # the standings
    assert re.fullmatch(
        r'You can use `;ranklist` again in \d+ seconds?\.', shown[-1] or ''
    )


@pytest.mark.usefixtures('codeforces', 'contests')
async def test_ratedvc_refused_before_it_asks_codeforces_costs_no_wait(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Too few contestants were rated for a rated virtual contest.
    rating_changes = AsyncMock(return_value=[])
    monkeypatch.setattr(cf.contest, 'ratingChanges', rating_changes)
    member = make_member(guild, MEMBER_ID)
    guild.get_member.side_effect = {OTHER_ID: make_member(guild, OTHER_ID)}.get
    ratedvc = f';ratedvc {CONTEST.id} <@{OTHER_ID}>'

    await use_prefix(bot, member, BOT_CHANNEL_ID, ratedvc)
    await bot.user_db.set_rated_vc_channel(GUILD_ID, GENERAL_ID)
    await use_prefix(bot, member, BOT_CHANNEL_ID, ratedvc)
    await bot.user_db.set_rated_vc_channel(GUILD_ID, BOT_CHANNEL_ID)
    await use_prefix(bot, member, BOT_CHANNEL_ID, f';ratedvc {CONTEST.id}')
    await use_prefix(
        bot, member, BOT_CHANNEL_ID, f';ratedvc {UNKNOWN_CONTEST_ID} <@{OTHER_ID}>'
    )
    rating_changes.assert_not_awaited()
    # This one asks Codeforces, so it counts.
    await use_prefix(bot, member, BOT_CHANNEL_ID, ratedvc)
    await use_prefix(bot, member, BOT_CHANNEL_ID, ratedvc)

    *refusals, cooldown = texts(posted)
    assert refusals == [
        'There is no rated virtual contest channel yet. Ask an admin to set one.',
        f'Use this command in <#{GENERAL_ID}>, the rated virtual contest channel.',
        'Name the members who take part, yourself included if you do.',
        f'Contest with ID `{UNKNOWN_CONTEST_ID}` not found',
        f"`{CONTEST.name}` can't be a rated virtual contest: fewer than 50 "
        "contestants were rated in it, or its rating changes aren't out yet.",
    ]
    assert re.fullmatch(
        r'You can use `;ratedvc` again in \d+ seconds?\.', cooldown or ''
    )
    rating_changes.assert_awaited_once_with(contest_id=CONTEST.id)


# _nogud: the access rules decide who may use it, and a member without a
# challenge gets a reason.


async def test_an_admin_without_tle_s_roles_can_skip_a_member_s_challenge(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock
) -> None:
    admin = make_member(guild, MEMBER_ID, manage_guild=True)
    guild.get_member.side_effect = {OTHER_ID: make_member(guild, OTHER_ID)}.get
    await bot.user_db.new_challenge(OTHER_ID, ISSUED, PROBLEM, 0)

    await use_prefix(bot, admin, BOT_CHANNEL_ID, f';_nogud <@{OTHER_ID}>')

    posted.assert_awaited_once()
    assert posted.await_args.kwargs['content'] == 'Challenge skip forced.'
    assert await bot.user_db.check_challenge(OTHER_ID) is None


async def test_skipping_the_challenge_of_a_member_without_one_says_so(
    bot: TLEBot, guild: MagicMock, posted: AsyncMock
) -> None:
    admin = make_member(guild, MEMBER_ID, manage_guild=True)
    guild.get_member.side_effect = {OTHER_ID: make_member(guild, OTHER_ID)}.get

    await use_prefix(bot, admin, BOT_CHANNEL_ID, f';_nogud <@{OTHER_ID}>')

    posted.assert_awaited_once()
    embed = posted.await_args.kwargs['embed']
    assert embed.description == f'<@{OTHER_ID}> has no gitgud challenge to skip.'
