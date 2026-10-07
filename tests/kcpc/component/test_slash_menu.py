"""The slash list of the booted bot: the payload its sync sends Discord.

Once every cog has loaded, the slash pass (``tle.access.slash``) removes the
trees of the bot owner's commands, hides the trees of staff commands behind a
permission, and removes the staff commands from the groups that members use.
Every prefix form stays. The bot boots as ``booting.booted`` boots it, with
TLE's own rule table, and the tests read what ``CommandTree.sync`` would send:
each top-level command's ``to_dict``.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tests.kcpc.component.booting import booted
from tle.__main__ import TLEBot
from tle.access.rules import STAFF_LEVELS, Rule
from tle.access.table import rule_for

MANAGE_MESSAGES = discord.Permissions(manage_messages=True).value
MANAGE_SERVER = discord.Permissions(manage_guild=True).value
# Discord's types of the options that are commands in a group.
SUBCOMMAND, SUBCOMMAND_GROUP = 1, 2
# Discord's interaction context type for servers, and its installation type
# for an app installed in a server.
GUILD = 0

# Every top-level slash command: the permission members need to see it (None
# if every member sees it), and the slash commands in it.
SLASH_LIST: dict[str, tuple[int | None, set[str]]] = {
    # Codeforces
    'gitgud': (None, {'gitgud'}),
    'upsolve': (None, {'upsolve'}),
    'gotgud': (None, {'gotgud'}),
    'nogud': (None, {'nogud'}),
    'gitlog': (None, {'gitlog'}),
    '_nogud': (MANAGE_MESSAGES, {'_nogud'}),
    # Contests
    'clist': (
        None,
        {'clist show', 'clist future', 'clist active', 'clist finished'},
    ),
    'remind': (None, {'remind show', 'remind settings', 'remind on', 'remind off'}),
    '_unregistervc': (MANAGE_MESSAGES, {'_unregistervc'}),
    'set_ratedvc_channel': (MANAGE_SERVER, {'set_ratedvc_channel'}),
    'get_ratedvc_channel': (None, {'get_ratedvc_channel'}),
    'vcratings': (None, {'vcratings'}),
    # Dueling
    'duel': (
        None,
        {
            'duel show',
            'duel selfregister',
            'duel accept',
            'duel decline',
            'duel withdraw',
            'duel complete',
            'duel draw',
            'duel profile',
            'duel vshistory',
            'duel history',
            'duel recent',
            'duel ongoing',
            'duel ranklist',
            'duel invalidate',
        },
    ),
    # Graphs
    'plot': (None, {'plot show', 'plot distrib', 'plot cfdistrib'}),
    # Handles
    'handle': (
        None,
        {
            'handle show',
            'handle identify',
            'handle get',
            'handle rget',
            'handle unmagic',
            'handle refer',
        },
    ),
    'gudgitters': (None, {'gudgitters'}),
    '_updatestatus': (MANAGE_SERVER, {'_updatestatus'}),
    'roleupdate': (
        MANAGE_MESSAGES,
        {'roleupdate show', 'roleupdate now', 'roleupdate auto', 'roleupdate publish'},
    ),
    'role': (None, {'role'}),
    # Meta
    'meta': (None, {'meta show', 'meta ping', 'meta uptime'}),
    # Starboard
    'starboard': (
        MANAGE_SERVER,
        {
            'starboard show',
            'starboard add',
            'starboard delete',
            'starboard edit_threshold',
            'starboard edit_color',
            'starboard here',
            'starboard clear',
            'starboard remove',
        },
    ),
    # KCPC. /kcpc keeps the permission its code gives it, and all its
    # commands, the bot owner's included.
    'kcpc': (
        MANAGE_SERVER,
        {
            'kcpc show',
            'kcpc status',
            'kcpc channel',
            'kcpc role',
            'kcpc enable',
            'kcpc disable',
            'kcpc workshops calendar',
            'kcpc workshops sync',
            'kcpc contests add',
            'kcpc contests settime',
            'kcpc contests remove',
            'kcpc contests platforms',
            'kcpc contests start-posts',
            'kcpc contests results',
            'kcpc contests sync',
            'kcpc accounts unlink',
            'kcpc weekly queue',
            'kcpc weekly unqueue',
            'kcpc weekly solution',
            'kcpc weekly rotation',
            'kcpc weekly preview',
            'kcpc weekly post-now',
            'kcpc algo reroll',
            'kcpc algo post-now',
            'kcpc algo preview',
        },
    ),
    'event': (None, {'event next', 'event this-week'}),
    'contests': (None, {'contests upcoming', 'contests live'}),
    'link': (None, {'link codeforces', 'link atcoder', 'link verify'}),
    'unlink': (None, {'unlink'}),
    'profile': (None, {'profile'}),
    'rank': (None, {'rank'}),
    'randproblem': (None, {'randproblem'}),
    'weekly': (None, {'weekly current', 'weekly history'}),
    'algo': (None, {'algo current', 'algo history'}),
    'notify': (None, {'notify'}),
    # /help and /access
    'help': (None, {'help'}),
    'access': (
        MANAGE_SERVER,
        {
            'access show',
            'access bot-channels add',
            'access bot-channels remove',
            'access staff-channel',
            'access limit',
            'access reset',
        },
    ),
}
# The slash commands removed, which keep their prefix forms: the whole of
# /cache, the bot owner's, and the staff commands in members' groups.
REMOVED = frozenset(
    {
        'cache',
        'cache contests',
        'cache problems',
        'cache ratingchanges',
        'cache problemsets',
        'remind clear',
        'duel register',
        'duel _invalidate',
        'handle set',
        'handle remove',
        'handle unmagic_all',
        'handle grandfather',
        'meta git',
        'meta kill',
        'meta guilds',
    }
)
# What /help and /access look like whatever else is switched off.
ACCESS_SLASH_LIST = {name: SLASH_LIST[name] for name in ('help', 'access')}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'db' / 'kcpc.db'


@pytest.fixture
async def bot(db_path: Path) -> AsyncIterator[TLEBot]:
    """The bot with every extension."""
    async with booted(db_path) as bot:
        yield bot


def payload(bot: commands.Bot) -> list[dict[str, Any]]:
    """What the bot's sync would send Discord, as CommandTree.sync makes it."""
    return [command.to_dict(bot.tree) for command in bot.tree.get_commands()]


def leaves(entry: dict[str, Any], prefix: str = '') -> Iterator[str]:
    """The paths of the slash commands in a payload's ``entry``."""
    path = f'{prefix}{entry["name"]}'
    inner = [
        option
        for option in entry.get('options', [])
        if option['type'] in (SUBCOMMAND, SUBCOMMAND_GROUP)
    ]
    if not inner:
        yield path
    for option in inner:
        yield from leaves(option, f'{path} ')


def slash_list(bot: commands.Bot) -> dict[str, tuple[int | None, set[str]]]:
    """Each top-level command in the payload: the permission members need to
    see it, and the slash commands in it.
    """
    return {
        entry['name']: (entry['default_member_permissions'], set(leaves(entry)))
        for entry in payload(bot)
    }


def slash_commands(
    command: app_commands.Command[Any, ..., Any] | app_commands.Group,
) -> list[app_commands.Command[Any, ..., Any]]:
    """The slash commands in a top-level ``command``, or ``command`` itself."""
    if isinstance(command, app_commands.Group):
        return [
            child
            for child in command.walk_commands()
            if isinstance(child, app_commands.Command)
        ]
    return [command]


def rule_of(command: app_commands.Command[Any, ..., Any]) -> Rule:
    """The rule of the prefix command that a slash command runs."""
    wrapped = getattr(command, 'wrapped', None)
    assert isinstance(wrapped, commands.Command), command.qualified_name
    rule = rule_for(wrapped.qualified_name)
    assert rule is not None, wrapped.qualified_name
    return rule


async def test_members_see_the_slash_commands_that_are_for_them(bot: TLEBot) -> None:
    listed = slash_list(bot)

    assert listed == SLASH_LIST
    assert len(listed) == 34


async def test_the_trees_of_staff_commands_need_a_permission_to_be_seen(
    bot: TLEBot,
) -> None:
    hidden = {
        name: permission
        for name, (permission, _) in slash_list(bot).items()
        if permission is not None
    }

    # Manage Messages for trees of moderators' commands, Manage Server for
    # the rest.
    assert hidden == {
        '_nogud': MANAGE_MESSAGES,
        '_unregistervc': MANAGE_MESSAGES,
        'roleupdate': MANAGE_MESSAGES,
        'set_ratedvc_channel': MANAGE_SERVER,
        '_updatestatus': MANAGE_SERVER,
        'starboard': MANAGE_SERVER,
        'kcpc': MANAGE_SERVER,
        'access': MANAGE_SERVER,
    }
    for name in hidden:
        command = bot.tree.get_command(name)
        assert command is not None
        assert all(
            rule_of(slash).who in STAFF_LEVELS for slash in slash_commands(command)
        ), name


async def test_every_visible_tree_holds_member_commands_alone(bot: TLEBot) -> None:
    for command in bot.tree.get_commands():
        if command.default_permissions is not None:
            continue
        levels = {rule_of(slash).who for slash in slash_commands(command)}
        assert levels.isdisjoint(STAFF_LEVELS), command.name


async def test_removed_slash_commands_keep_their_prefix_forms(bot: TLEBot) -> None:
    shown = {path for _, paths in slash_list(bot).values() for path in paths}
    for name in REMOVED:
        command = bot.get_command(name)
        # A hybrid command that had a slash form.
        assert command is not None, name
        assert getattr(command, 'app_command', None) is not None, name
        assert bot.access.slash_path(command) is None, name
        assert not [
            path for path in shown if path == name or path.startswith(f'{name} ')
        ], name
        rule = rule_for(name)
        assert rule is not None and rule.who in STAFF_LEVELS, name
    # Nothing of /cache is left, not even the group.
    assert bot.tree.get_command('cache') is None


async def test_slash_commands_work_in_servers_and_install_in_servers_alone(
    bot: TLEBot,
) -> None:
    for entry in payload(bot):
        assert entry['contexts'] == [GUILD], entry['name']
        assert entry['integration_types'] == [GUILD], entry['name']


async def test_help_and_access_are_there_without_tle_s_extensions(
    db_path: Path,
) -> None:
    async with booted(db_path, disabled='tle') as bot:
        listed = slash_list(bot)
        (access,) = [entry for entry in payload(bot) if entry['name'] == 'access']

    # The rest comes from KCPC.
    assert set(listed) == {
        'help',
        'access',
        'kcpc',
        'event',
        'contests',
        'link',
        'unlink',
        'profile',
        'rank',
        'randproblem',
        'weekly',
        'algo',
        'notify',
    }
    assert {name: listed[name] for name in ('help', 'access')} == ACCESS_SLASH_LIST
    # /access in the order its cog declares its commands.
    assert list(leaves(access)) == [
        'access show',
        'access bot-channels add',
        'access bot-channels remove',
        'access staff-channel',
        'access limit',
        'access reset',
    ]
