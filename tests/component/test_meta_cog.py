"""Tests for the Meta cog (tle.cogs.meta): how the bot is doing, and the bot
owner's commands that concern every server.

The tests call each command's callback, as discord.py does once the access
check has let the command run, with a real context of a prefix or a slash
command whose replies are recorded. git and Discord are mocked.
"""

import subprocess
import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle.cogs.meta import (
    GIT_FAILED_TEXT,
    GIT_TIMEOUT,
    GUILDS_SENT_TEXT,
    MESSAGE_LIMIT,
    NO_DM_DELETE_AFTER,
    NO_DM_TEXT,
    Meta,
    git_history,
)

# Real snowflakes are 64-bit, so use big ones.
OWNER_ID = 1_400_000_000_000_000_009
COMMANDS = ('meta', 'meta kill', 'meta ping', 'meta git', 'meta uptime', 'meta guilds')
# What git answers to meta git's two commands.
HISTORY = {
    'rev-parse': b'main\n',
    'log': b'abc1234 Add /help\ndef5678 Fix the ping\n',
}

both_paths = pytest.mark.parametrize('slash', [False, True], ids=['prefix', 'slash'])


@pytest.fixture
def bot() -> MagicMock:
    return MagicMock(spec=commands.Bot)


@pytest.fixture
def cog(bot: MagicMock) -> Meta:
    return Meta(bot)


def make_context(bot: MagicMock, *, slash: bool) -> commands.Context[Any]:
    """A real context of a command used by the bot owner, prefix or slash.

    Replies are recorded by an ``AsyncMock`` in place of ``send``; posts in
    the channel by the channel's ``send``, and direct messages by the
    author's.
    """
    author = MagicMock(spec=discord.Member, id=OWNER_ID)
    channel = MagicMock(spec=discord.TextChannel)
    message = MagicMock(spec=discord.Message, author=author, channel=channel)
    interaction = MagicMock(spec=discord.Interaction, client=bot) if slash else None
    context: commands.Context[Any] = commands.Context(
        message=message,
        bot=bot,
        view=StringView(''),
        prefix='/' if slash else ';',
        interaction=interaction,
    )
    if interaction is not None:
        interaction._baton = context  # where discord.py keeps a slash command's context
    context.send = AsyncMock()  # type: ignore[method-assign]
    return context


def fake_git(
    outputs: dict[str, bytes], threads: list[int]
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Stands in for ``subprocess.run``: answers each git command with its
    output in ``outputs``, by its first argument, and records the thread
    that ran it in ``threads``.
    """

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        threads.append(threading.get_ident())
        assert cmd[0] == 'git'
        # git is stopped if it hangs, and an exit status other than 0 counts
        # as a failure.
        assert kwargs['timeout'] == GIT_TIMEOUT
        assert kwargs['check'] is True
        return subprocess.CompletedProcess(cmd, 0, stdout=outputs[cmd[1]])

    return run


def make_guild(guild_id: int, name: str, owner_id: int) -> MagicMock:
    """A server whose owner isn't cached, as can happen."""
    guild = MagicMock(spec=discord.Guild, id=guild_id, owner_id=owner_id, icon=None)
    guild.name = name  # not MagicMock(name=...), which names the mock itself
    guild.owner = None
    return guild


def dm_refused() -> discord.Forbidden:
    """What Discord answers when a member doesn't take direct messages."""
    response = MagicMock(status=403, reason='Forbidden')
    return discord.Forbidden(response, 'Cannot send messages to this user')


def test_only_the_access_rules_decide_who_uses_the_commands(cog: Meta) -> None:
    # No role check of their own: meta kill and meta guilds are for the bot
    # owner, whatever their roles, and TLE's admins no longer pass.
    checks = {command.qualified_name: command.checks for command in cog.walk_commands()}
    assert checks == {name: [] for name in COMMANDS}


# meta git


async def test_git_reads_the_history_off_the_event_loop(
    cog: Meta, bot: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: list[int] = []
    monkeypatch.setattr(subprocess, 'run', fake_git(HISTORY, threads))
    ctx = make_context(bot, slash=False)

    await Meta.git.callback(cog, ctx)

    # In a worker thread: this test runs on the event loop's.
    assert len(threads) == 2
    assert threading.get_ident() not in threads
    ctx.send.assert_awaited_once_with(
        '```yaml\n'
        'Branch:\n'
        '  main\n'
        'Commits:\n'
        '  abc1234 Add /help\n'
        '  def5678 Fix the ping```'
    )


@pytest.mark.parametrize(
    'error',
    [
        FileNotFoundError(2, 'No such file or directory', 'git'),
        subprocess.CalledProcessError(128, ['git', 'rev-parse']),
        subprocess.TimeoutExpired(['git', 'log'], GIT_TIMEOUT),
    ],
    ids=['no git', 'no repository', 'too slow'],
)
async def test_git_says_so_when_git_cannot_tell(
    cog: Meta, bot: MagicMock, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(subprocess, 'run', MagicMock(side_effect=error))
    ctx = make_context(bot, slash=False)

    await Meta.git.callback(cog, ctx)

    ctx.send.assert_awaited_once_with(GIT_FAILED_TEXT)
    assert GIT_FAILED_TEXT == "Couldn't read the bot's git history."


@pytest.mark.parametrize(
    ('log', 'shown'),
    [
        ('abc1234 Corrige la clé ✓\n'.encode(), 'abc1234 Corrige la clé ✓'),
        (b'abc1234 Not UTF-8: \xff\n', 'abc1234 Not UTF-8: \ufffd'),
    ],
    ids=['any language', 'not utf-8'],
)
def test_git_history_reads_commit_messages_in_any_characters(
    monkeypatch: pytest.MonkeyPatch, log: bytes, shown: str
) -> None:
    monkeypatch.setattr(subprocess, 'run', fake_git({**HISTORY, 'log': log}, []))

    assert git_history() == f'Branch:\n  main\nCommits:\n  {shown}'


# meta guilds


@both_paths
async def test_guilds_sends_the_list_by_direct_message(
    cog: Meta, bot: MagicMock, slash: bool
) -> None:
    bot.guilds = [make_guild(11, 'KCPC', 101), make_guild(12, 'Other', 102)]
    bot.guilds[0].icon = MagicMock(url='https://cdn.discordapp.com/icons/11/a.png')
    ctx = make_context(bot, slash=slash)

    await Meta.guilds.callback(cog, ctx)

    ctx.author.send.assert_awaited_once_with(
        '```\n'
        'Guild ID: 11 | Name: KCPC | Owner: 101 | '
        'Icon: https://cdn.discordapp.com/icons/11/a.png\n'
        'Guild ID: 12 | Name: Other | Owner: 102 | Icon: None\n'
        '```'
    )
    ctx.channel.send.assert_not_awaited()
    if slash:
        # A slash command must be answered, and only its user sees this.
        ctx.send.assert_awaited_once_with(GUILDS_SENT_TEXT, ephemeral=True)
    else:
        ctx.send.assert_not_awaited()


async def test_a_long_list_of_servers_takes_several_messages(
    cog: Meta, bot: MagicMock
) -> None:
    bot.guilds = [make_guild(n, f'Server {n} ' + 'x' * 80, 1000 + n) for n in range(40)]
    # Longer than a message can be, and cut short.
    bot.guilds.append(make_guild(99, 'y' * 2500, 1099))
    ctx = make_context(bot, slash=False)

    await Meta.guilds.callback(cog, ctx)

    sent = [call.args[0] for call in ctx.author.send.await_args_list]
    assert len(sent) > 2
    for text in sent:
        assert len(text) <= MESSAGE_LIMIT
        assert text.startswith('```\n') and text.endswith('\n```')
    lines = [line for text in sent for line in text[4:-4].split('\n')]
    assert [line.split(' | ')[0] for line in lines] == [
        f'Guild ID: {n}' for n in [*range(40), 99]
    ]
    assert lines[-1].startswith('Guild ID: 99 | Name: yyy')


@both_paths
async def test_guilds_never_shows_the_list_when_the_dm_is_refused(
    cog: Meta, bot: MagicMock, slash: bool
) -> None:
    bot.guilds = [make_guild(11, 'KCPC', 101)]
    ctx = make_context(bot, slash=slash)
    ctx.author.send.side_effect = dm_refused()

    await Meta.guilds.callback(cog, ctx)

    # Only a note, private on slash, and gone after 20 seconds.
    ctx.send.assert_awaited_once_with(
        NO_DM_TEXT, ephemeral=True, delete_after=NO_DM_DELETE_AFTER
    )
    assert (NO_DM_TEXT, NO_DM_DELETE_AFTER) == ("I couldn't DM you.", 20)
    ctx.channel.send.assert_not_awaited()


# meta uptime


async def test_uptime_says_how_long_the_bot_has_been_running(
    cog: Meta, bot: MagicMock
) -> None:
    cog.start_time -= 2 * 24 * 3600 + 3 * 3600 + 4 * 60 + 5
    ctx = make_context(bot, slash=True)

    await Meta.uptime.callback(cog, ctx)

    ctx.send.assert_awaited_once_with(
        'The bot has been running for 2 days 3 hours 4 minutes.'
    )


# meta kill


async def test_kill_says_so_and_stops_the_bot(cog: Meta, bot: MagicMock) -> None:
    ctx = make_context(bot, slash=False)

    with pytest.raises(SystemExit) as stopped:
        await Meta.kill.callback(cog, ctx)

    assert stopped.value.code == 0
    ctx.send.assert_awaited_once_with('Shutting down...')
    bot.close.assert_awaited_once_with()
