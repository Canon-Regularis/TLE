"""Tests for tle.access.slash: which slash commands members see.

They run on a real ``commands.Bot`` with cogs of hybrid commands and groups of
every shape, and rules given by name, so they also pin what the module relies
on in discord.py: a group's slash fallback wraps the group itself, a slash
group can lose a subcommand while its hybrid group keeps the prefix one, and
syncing sends Discord what ``to_dict`` gives, with default permissions on
top-level commands only. The last tests use the table's rules, and check that
the access service then offers members only the slash forms that are left.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.ext.commands.view import StringView

from tle.access import table
from tle.access.rules import FAIL_CLOSED, Rule, Where, Who
from tle.access.service import AccessService
from tle.access.slash import Visibility, apply_visibility, default_rule

SLASH_LOGGER = 'tle.access.slash'

EVERYONE = Rule(Who.EVERYONE, Where.BOT)
TRUSTED = Rule(Who.TRUSTED, Where.BOT_ONLY)
MODERATOR = Rule(Who.MODERATOR, Where.STAFF)
DEVELOPER = Rule(Who.DEVELOPER, Where.STAFF_ONLY)
ADMIN = Rule(Who.ADMIN, Where.STAFF)
OWNER = Rule(Who.OWNER, Where.ANYWHERE)

# Discord's default_member_permissions values.
MANAGE_MESSAGES = discord.Permissions(manage_messages=True).value
MANAGE_GUILD = discord.Permissions(manage_guild=True).value
BAN_MEMBERS = discord.Permissions(ban_members=True).value
ADMINISTRATOR = discord.Permissions(administrator=True).value

# Discord's types of the options of a slash command.
SUBCOMMAND, SUBCOMMAND_GROUP = 1, 2


class Sample(commands.Cog):
    """Commands of every shape that the slash pass treats differently.

    ``ran`` notes each command as it runs, by its qualified name.
    """

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def cog_before_invoke(self, ctx: commands.Context[Any]) -> None:
        assert ctx.command is not None
        self.ran.append(ctx.command.qualified_name)

    # Top-level commands, one per level.
    @commands.hybrid_command()
    async def ping(self, ctx: commands.Context[Any]) -> None:
        """For everyone"""

    @commands.hybrid_command()
    async def vouch(self, ctx: commands.Context[Any]) -> None:
        """For trusted members"""

    @commands.hybrid_command()
    async def sweep(self, ctx: commands.Context[Any]) -> None:
        """For moderators"""

    @commands.hybrid_command()
    async def configure(self, ctx: commands.Context[Any]) -> None:
        """For admins"""

    @commands.hybrid_command()
    async def inspect(self, ctx: commands.Context[Any]) -> None:
        """For developers"""

    @commands.hybrid_command()
    async def shutdown(self, ctx: commands.Context[Any]) -> None:
        """For the bot owner"""

    # Default permissions set in the code.
    @commands.hybrid_command()
    @app_commands.default_permissions(administrator=True)
    async def restart(self, ctx: commands.Context[Any]) -> None:
        """For the bot owner, and for administrators to see"""

    @commands.hybrid_command()
    @app_commands.default_permissions(manage_guild=True)
    async def purge(self, ctx: commands.Context[Any]) -> None:
        """For moderators, and for Manage Server to see"""

    # A prefix command alone, which the slash pass never sees.
    @commands.command()
    async def notes(self, ctx: commands.Context[Any]) -> None:
        """For the bot owner, prefix only"""

    # A group of the bot owner's commands.
    @commands.hybrid_group(fallback='show')
    async def cache(self, ctx: commands.Context[Any]) -> None:
        """The caches"""

    @cache.command(name='reload')
    async def cache_reload(self, ctx: commands.Context[Any]) -> None:
        """Reload a cache"""

    @cache.command(name='stats')
    async def cache_stats(self, ctx: commands.Context[Any]) -> None:
        """Count what the caches hold"""

    # A group of moderators' commands.
    @commands.hybrid_group(fallback='show')
    async def roles(self, ctx: commands.Context[Any]) -> None:
        """Rank roles"""

    @roles.command(name='now')
    async def roles_now(self, ctx: commands.Context[Any]) -> None:
        """Update the rank roles now"""

    @roles.command(name='auto')
    async def roles_auto(self, ctx: commands.Context[Any]) -> None:
        """Update the rank roles by themselves"""

    @roles.command(name='publish')
    async def roles_publish(self, ctx: commands.Context[Any]) -> None:
        """Publish the rank changes"""

    # A group of every kind of staff's commands, with the bot owner's too.
    @commands.hybrid_group(fallback='show')
    async def board(self, ctx: commands.Context[Any]) -> None:
        """The board"""

    @board.command(name='add')
    async def board_add(self, ctx: commands.Context[Any]) -> None:
        """Add to the board"""

    @board.command(name='clear')
    async def board_clear(self, ctx: commands.Context[Any]) -> None:
        """Clear the board"""

    @board.command(name='debug')
    async def board_debug(self, ctx: commands.Context[Any]) -> None:
        """Debug the board"""

    @board.command(name='wipe')
    async def board_wipe(self, ctx: commands.Context[Any]) -> None:
        """Wipe every board"""

    @board.group(name='feeds')
    async def board_feeds(self, ctx: commands.Context[Any]) -> None:
        """The board's feeds"""

    @board_feeds.command(name='add')
    async def board_feeds_add(self, ctx: commands.Context[Any]) -> None:
        """Add a feed"""

    @board_feeds.command(name='sync')
    async def board_feeds_sync(self, ctx: commands.Context[Any]) -> None:
        """Sync every feed"""

    # A group of moderators' commands that the code hides behind Manage Server.
    @commands.hybrid_group(fallback='show')
    @app_commands.default_permissions(manage_guild=True)
    async def mods(self, ctx: commands.Context[Any]) -> None:
        """Moderation"""

    @mods.command(name='kick')
    async def mods_kick(self, ctx: commands.Context[Any]) -> None:
        """Kick a member"""

    # Like /kcpc: hidden by the code, with an admin group that another cog
    # attaches (``Events``).
    @commands.hybrid_group(fallback='show')
    @app_commands.default_permissions(manage_guild=True)
    async def club(self, ctx: commands.Context[Any]) -> None:
        """Club settings"""

    @club.command(name='status')
    async def club_status(self, ctx: commands.Context[Any]) -> None:
        """Club health"""

    # A group for members with staff commands in it, and subgroups.
    @commands.hybrid_group(fallback='show')
    async def members(self, ctx: commands.Context[Any]) -> None:
        """Members"""

    @members.command(name='list')
    async def members_list(self, ctx: commands.Context[Any]) -> None:
        """List the members"""

    @members.command(name='refer')
    async def members_refer(self, ctx: commands.Context[Any]) -> None:
        """Refer a member"""

    @members.command(name='register')
    async def members_register(self, ctx: commands.Context[Any]) -> None:
        """Register a member"""

    @members.command(name='grant')
    async def members_grant(self, ctx: commands.Context[Any]) -> None:
        """Grant a member a role"""

    @members.command(name='debug')
    async def members_debug(self, ctx: commands.Context[Any]) -> None:
        """Debug the members"""

    @members.command(name='kill')
    async def members_kill(self, ctx: commands.Context[Any]) -> None:
        """Forget every member"""

    @members.command(name='here', with_app_command=False)
    async def members_here(self, ctx: commands.Context[Any]) -> None:
        """Set this channel for members, prefix only"""

    # Discord ignores a subgroup's default permissions.
    @members.group(name='admin')
    @app_commands.default_permissions(manage_guild=True)
    async def members_admin(self, ctx: commands.Context[Any]) -> None:
        """Members admin"""

    @members_admin.command(name='reset')
    async def members_admin_reset(self, ctx: commands.Context[Any]) -> None:
        """Reset the members"""

    @members_admin.command(name='sync')
    async def members_admin_sync(self, ctx: commands.Context[Any]) -> None:
        """Sync the members everywhere"""

    # Named as /members list, which is for everyone.
    @members_admin.command(name='list')
    async def members_admin_list(self, ctx: commands.Context[Any]) -> None:
        """List the admins"""

    @members.group(name='stats', fallback='show')
    async def members_stats(self, ctx: commands.Context[Any]) -> None:
        """Member statistics"""

    @members_stats.command(name='reset')
    async def members_stats_reset(self, ctx: commands.Context[Any]) -> None:
        """Reset the statistics"""

    # A group for admins, through its fallback, with a command for members.
    @commands.hybrid_group(fallback='show')
    async def report(self, ctx: commands.Context[Any]) -> None:
        """Reports"""

    @report.command(name='daily')
    async def report_daily(self, ctx: commands.Context[Any]) -> None:
        """Today's report"""

    # A group without a fallback, whose own rule no slash command has.
    @commands.hybrid_group()
    async def lookup(self, ctx: commands.Context[Any]) -> None:
        """Look things up"""

    @lookup.command(name='user')
    async def lookup_user(self, ctx: commands.Context[Any]) -> None:
        """Look up a user"""

    @lookup.command(name='handle')
    async def lookup_handle(self, ctx: commands.Context[Any]) -> None:
        """Look up a handle"""

    # A group for members with a staff command, hidden by the code.
    @commands.hybrid_group(fallback='show')
    @app_commands.default_permissions(ban_members=True)
    async def legacy(self, ctx: commands.Context[Any]) -> None:
        """Old things"""

    @legacy.command(name='wipe')
    async def legacy_wipe(self, ctx: commands.Context[Any]) -> None:
        """Wipe the old things"""

    # A group with no slash command: its only subcommand is prefix only.
    @commands.hybrid_group()
    async def tools(self, ctx: commands.Context[Any]) -> None:
        """Tools"""

    @tools.command(name='calc', with_app_command=False)
    async def tools_calc(self, ctx: commands.Context[Any]) -> None:
        """Calculate, prefix only"""


class Events(commands.Cog):
    """Attaches its admin group under /club, as KCPC's features do under /kcpc."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        club = self.bot.get_command('club')
        assert isinstance(club, commands.HybridGroup)
        club.add_command(self.events)

    @commands.hybrid_group(name='events')
    async def events(self, ctx: commands.Context[Any]) -> None:
        """Club events"""

    @events.command(name='add')
    async def events_add(self, ctx: commands.Context[Any]) -> None:
        """Add an event to every server's list"""

    @events.command(name='sync')
    async def events_sync(self, ctx: commands.Context[Any]) -> None:
        """Sync this server's events"""


async def report_message(
    interaction: discord.Interaction, message: discord.Message
) -> None:
    """A context menu command, which has no rule."""


RULES: dict[str, Rule] = {
    'ping': EVERYONE,
    'vouch': TRUSTED,
    'sweep': MODERATOR,
    'configure': ADMIN,
    'inspect': DEVELOPER,
    'shutdown': OWNER,
    'restart': OWNER,
    'purge': MODERATOR,
    'notes': OWNER,
    'cache': OWNER,
    'cache reload': OWNER,
    'cache stats': OWNER,
    'roles': MODERATOR,
    'roles now': MODERATOR,
    'roles auto': MODERATOR,
    'roles publish': MODERATOR,
    'board': ADMIN,
    'board add': ADMIN,
    'board clear': MODERATOR,
    'board debug': DEVELOPER,
    'board wipe': OWNER,
    'board feeds': ADMIN,
    'board feeds add': ADMIN,
    'board feeds sync': OWNER,
    'mods': MODERATOR,
    'mods kick': MODERATOR,
    'club': ADMIN,
    'club status': DEVELOPER,
    'club events': ADMIN,
    'club events add': OWNER,
    'club events sync': ADMIN,
    'members': EVERYONE,
    'members list': EVERYONE,
    'members refer': TRUSTED,
    'members register': MODERATOR,
    'members grant': ADMIN,
    'members debug': DEVELOPER,
    'members kill': OWNER,
    'members here': ADMIN,
    'members admin': ADMIN,
    'members admin reset': ADMIN,
    'members admin sync': OWNER,
    'members admin list': ADMIN,
    'members stats': EVERYONE,
    'members stats reset': MODERATOR,
    'report': ADMIN,
    'report daily': EVERYONE,
    'lookup': MODERATOR,
    'lookup user': EVERYONE,
    'lookup handle': EVERYONE,
    'legacy': EVERYONE,
    'legacy wipe': ADMIN,
    'tools': EVERYONE,
    'tools calc': EVERYONE,
}

# RULES names each of the sample bot's prefix commands, and nothing else; the
# context menu has no rule.
PREFIX_NAMES = frozenset(RULES)

# What syncing sends Discord before the slash pass: each top-level command's
# default permissions and subcommands, a subgroup as (name, its subcommands).
BEFORE: dict[str, tuple[int | None, list[Any]]] = {
    'ping': (None, []),
    'vouch': (None, []),
    'sweep': (None, []),
    'configure': (None, []),
    'inspect': (None, []),
    'shutdown': (None, []),
    'restart': (ADMINISTRATOR, []),
    'purge': (MANAGE_GUILD, []),
    'cache': (None, ['show', 'reload', 'stats']),
    'roles': (None, ['show', 'now', 'auto', 'publish']),
    'board': (
        None,
        ['show', 'add', 'clear', 'debug', 'wipe', ('feeds', ['add', 'sync'])],
    ),
    'mods': (MANAGE_GUILD, ['show', 'kick']),
    'club': (MANAGE_GUILD, ['show', 'status', ('events', ['add', 'sync'])]),
    'members': (
        None,
        [
            'show',
            'list',
            'refer',
            'register',
            'grant',
            'debug',
            'kill',
            ('admin', ['reset', 'sync', 'list']),
            ('stats', ['show', 'reset']),
        ],
    ),
    'report': (None, ['show', 'daily']),
    'lookup': (None, ['user', 'handle']),
    'legacy': (BAN_MEMBERS, ['show', 'wipe']),
    'tools': (None, []),
    'Report message': (None, []),
}
# And after it.
AFTER: dict[str, tuple[int | None, list[Any]]] = {
    'ping': (None, []),
    'vouch': (None, []),
    'sweep': (MANAGE_MESSAGES, []),
    'configure': (MANAGE_GUILD, []),
    'inspect': (MANAGE_GUILD, []),
    'purge': (MANAGE_GUILD, []),
    'roles': (MANAGE_MESSAGES, ['show', 'now', 'auto', 'publish']),
    'board': (
        MANAGE_GUILD,
        ['show', 'add', 'clear', 'debug', 'wipe', ('feeds', ['add', 'sync'])],
    ),
    'mods': (MANAGE_GUILD, ['show', 'kick']),
    'club': (MANAGE_GUILD, ['show', 'status', ('events', ['add', 'sync'])]),
    'members': (None, ['show', 'list', 'refer', ('stats', ['show'])]),
    'report': (None, ['daily']),
    'lookup': (None, ['user', 'handle']),
    'legacy': (BAN_MEMBERS, ['show', 'wipe']),
    'Report message': (None, []),
}
REMOVED = [
    'shutdown',
    'restart',
    'cache',
    'members register',
    'members grant',
    'members debug',
    'members kill',
    'members admin reset',
    'members admin sync',
    'members admin list',
    'members admin',
    'members stats reset',
    'report show',
    'tools',
]
HIDDEN = {
    'sweep': 'manage_messages',
    'configure': 'manage_guild',
    'inspect': 'manage_guild',
    'purge': 'manage_guild',
    'roles': 'manage_messages',
    'board': 'manage_guild',
    'mods': 'manage_guild',
    'club': 'manage_guild',
    'legacy': 'ban_members',
}


class Rules:
    """Rules by name, like ``RULES``, noting every name asked for.

    An unknown name fails the test: the slash pass must ask by the names of
    prefix commands only.
    """

    def __init__(self, rules: Mapping[str, Rule]) -> None:
        self.rules = rules
        self.asked: list[str] = []

    def __call__(self, name: str) -> Rule:
        self.asked.append(name)
        assert name in self.rules, f'asked for the rule of {name!r}'
        return self.rules[name]


def new_bot() -> commands.Bot:
    """A bot that hasn't logged in, without discord.py's help command."""
    return commands.Bot(
        command_prefix=';', intents=discord.Intents.none(), help_command=None
    )


@asynccontextmanager
async def sample_bot() -> AsyncIterator[commands.Bot]:
    """A bot with ``Sample``'s and ``Events``' commands and a context menu."""
    bot = new_bot()
    try:
        await bot.add_cog(Sample())
        await bot.add_cog(Events(bot))
        bot.tree.add_command(
            app_commands.ContextMenu(name='Report message', callback=report_message)
        )
        yield bot
    finally:
        await bot.close()


@pytest.fixture
async def bot() -> AsyncIterator[commands.Bot]:
    async with sample_bot() as bot:
        yield bot


@pytest.fixture
def rules() -> Rules:
    return Rules(RULES)


def payload(bot: commands.Bot) -> dict[str, dict[str, Any]]:
    """What syncing would send Discord, by top-level command name."""
    return {
        command.name: command.to_dict(bot.tree) for command in bot.tree.get_commands()
    }


def subcommands(data: Mapping[str, Any]) -> list[Any]:
    """The names of a command's subcommands; a subgroup's as (name, theirs)."""
    return [
        (option['name'], subcommands(option))
        if option['type'] == SUBCOMMAND_GROUP
        else option['name']
        # A context menu has no options.
        for option in data.get('options', [])
        if option['type'] in (SUBCOMMAND, SUBCOMMAND_GROUP)
    ]


def menu(bot: commands.Bot) -> dict[str, tuple[int | None, list[Any]]]:
    """Each top-level command's default permissions and subcommands, as synced."""
    return {
        name: (data['default_member_permissions'], subcommands(data))
        for name, data in payload(bot).items()
    }


def nested(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every subcommand and subgroup in a command's payload, at any depth."""
    found: list[Mapping[str, Any]] = []
    for option in data.get('options', []):
        if option['type'] in (SUBCOMMAND, SUBCOMMAND_GROUP):
            found += [option, *nested(option)]
    return found


def slash_group(bot: commands.Bot, name: str) -> app_commands.Group:
    group = bot.tree.get_command(name)
    assert isinstance(group, app_commands.Group), name
    return group


def prefix_commands(bot: commands.Bot) -> dict[str, commands.Command[Any, ..., Any]]:
    return {command.qualified_name: command for command in bot.walk_commands()}


async def run_prefix(bot: commands.Bot, name: str) -> None:
    """Run the prefix command ``name``, with no arguments, as a member would."""
    command = bot.get_command(name)
    assert command is not None, name
    ctx: commands.Context[commands.Bot] = commands.Context(
        message=MagicMock(spec=discord.Message),
        bot=bot,
        view=StringView(''),
        prefix=';',
        command=command,
        invoked_with=command.name,
    )
    await bot.invoke(ctx)


# --- The sample bot as it starts ---


async def test_the_sample_bot_starts_with_every_command_in_the_slash_list(
    bot: commands.Bot,
) -> None:
    assert menu(bot) == BEFORE
    assert set(prefix_commands(bot)) == PREFIX_NAMES


async def test_a_fallback_wraps_its_group(bot: commands.Bot) -> None:
    # What gives a fallback, such as /members show, its group's rule.
    members = slash_group(bot, 'members')
    stats = members.get_command('stats')
    assert isinstance(stats, app_commands.Group)
    members_show = members.get_command('show')
    stats_show = stats.get_command('show')

    assert getattr(members_show, 'wrapped', None) is bot.get_command('members')
    assert getattr(stats_show, 'wrapped', None) is bot.get_command('members stats')


# --- Each kind of tree ---


async def test_a_tree_of_the_bot_owners_commands_leaves_the_slash_list(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    for name in ('shutdown', 'cache'):
        assert bot.tree.get_command(name) is None
        assert name in visibility.removed
    # The commands in a removed group go with it.
    assert not [name for name in visibility.removed if name.startswith('cache ')]


async def test_the_bot_owners_commands_leave_even_if_the_code_hides_them(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    assert bot.tree.get_command('restart') is None
    assert 'restart' in visibility.removed
    assert 'restart' not in visibility.hidden


async def test_a_tree_of_moderators_commands_needs_manage_messages(
    bot: commands.Bot, rules: Rules
) -> None:
    apply_visibility(bot, rules)

    assert menu(bot)['sweep'] == (MANAGE_MESSAGES, [])
    assert menu(bot)['roles'] == (MANAGE_MESSAGES, ['show', 'now', 'auto', 'publish'])


@pytest.mark.parametrize('name', ['configure', 'inspect', 'board'])
async def test_a_tree_of_other_staff_commands_needs_manage_server(
    bot: commands.Bot, rules: Rules, name: str
) -> None:
    apply_visibility(bot, rules)

    permissions, _subcommands = menu(bot)[name]
    assert permissions == MANAGE_GUILD


async def test_nothing_is_removed_from_a_tree_it_hides(
    bot: commands.Bot, rules: Rules
) -> None:
    apply_visibility(bot, rules)

    # Not even the bot owner's: staff who see /board see /board wipe too, and
    # the access rules refuse it to them.
    assert menu(bot)['board'] == AFTER['board']
    board = slash_group(bot, 'board')
    assert board.get_command('wipe') is not None
    assert board.get_command('debug') is not None


async def test_default_permissions_set_in_the_code_are_kept(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    # Moderators' commands that the code hides behind Manage Server stay so,
    # where the slash pass alone would choose Manage Messages.
    assert menu(bot)['purge'] == (MANAGE_GUILD, [])
    assert menu(bot)['mods'] == (MANAGE_GUILD, ['show', 'kick'])
    assert visibility.hidden['purge'] == visibility.hidden['mods'] == 'manage_guild'


async def test_nothing_is_removed_from_a_tree_the_code_hides(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    # Like /kcpc and its owner-only /kcpc contests add, which another cog
    # attaches.
    assert menu(bot)['club'] == (
        MANAGE_GUILD,
        ['show', 'status', ('events', ['add', 'sync'])],
    )
    # Even where some commands are for members.
    assert menu(bot)['legacy'] == (BAN_MEMBERS, ['show', 'wipe'])
    assert visibility.hidden['legacy'] == 'ban_members'
    assert not [
        name for name in visibility.removed if name.startswith(('club', 'legacy'))
    ]


async def test_commands_for_staff_leave_a_group_for_members(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    members = slash_group(bot, 'members')
    for name in ('register', 'grant', 'debug', 'kill'):
        assert members.get_command(name) is None, name
        assert f'members {name}' in visibility.removed
    # Trusted members aren't staff.
    assert members.get_command('refer') is not None
    assert members.get_command('list') is not None
    assert members.get_command('show') is not None
    # Members still see it.
    assert members.default_permissions is None
    assert 'members' not in visibility.hidden


async def test_a_subgroup_left_empty_leaves_too(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    members = slash_group(bot, 'members')
    # Its default permissions don't count: Discord ignores a subgroup's.
    assert members.get_command('admin') is None
    start = visibility.removed.index('members admin reset')
    assert visibility.removed[start : start + 4] == [
        'members admin reset',
        'members admin sync',
        'members admin list',
        'members admin',
    ]
    # A subgroup with a command left stays, with its fallback.
    stats = members.get_command('stats')
    assert isinstance(stats, app_commands.Group)
    assert [command.name for command in stats.commands] == ['show']


async def test_a_groups_own_rule_counts_through_its_fallback(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    # /report show runs ;report, which is for admins.
    report = slash_group(bot, 'report')
    assert [command.name for command in report.commands] == ['daily']
    assert 'report show' in visibility.removed
    # /lookup runs no command of its own, so its rule doesn't count.
    assert menu(bot)['lookup'] == (None, ['user', 'handle'])
    assert 'lookup' not in rules.asked


async def test_a_group_without_slash_commands_leaves_the_slash_list(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    # Discord couldn't run it: its only subcommand is prefix only.
    assert bot.tree.get_command('tools') is None
    assert 'tools' in visibility.removed
    assert bot.get_command('tools calc') is not None


async def test_commands_for_everyone_and_trusted_members_stay_visible(
    bot: commands.Bot, rules: Rules
) -> None:
    visibility = apply_visibility(bot, rules)

    assert menu(bot)['ping'] == (None, [])
    assert menu(bot)['vouch'] == (None, [])
    assert {'ping', 'vouch'}.isdisjoint(visibility.hidden)
    assert {'ping', 'vouch'}.isdisjoint(visibility.removed)


# --- The rules asked for ---


async def test_rules_are_asked_for_by_the_names_of_prefix_commands(
    bot: commands.Bot, rules: Rules
) -> None:
    apply_visibility(bot, rules)

    # A fallback by its group's name, and nothing without a slash form:
    # prefix-only commands, groups without a fallback, the context menu.
    assert set(rules.asked) == (
        PREFIX_NAMES
        - {
            'notes',
            'members here',
            'tools calc',
            'board feeds',
            'club events',
            'members admin',
            'lookup',
            'tools',
        }
    )


# --- The whole slash list ---


async def test_what_syncing_sends_discord(bot: commands.Bot, rules: Rules) -> None:
    apply_visibility(bot, rules)

    assert menu(bot) == AFTER
    # Discord takes default permissions on top-level commands only.
    for data in payload(bot).values():
        for option in nested(data):
            assert 'default_member_permissions' not in option, option['name']


async def test_the_visibility_says_what_changed(
    bot: commands.Bot, rules: Rules
) -> None:
    assert apply_visibility(bot, rules) == Visibility(REMOVED, HIDDEN)


async def test_a_second_run_changes_nothing_more(
    bot: commands.Bot, rules: Rules
) -> None:
    apply_visibility(bot, rules)

    assert apply_visibility(bot, rules) == Visibility([], HIDDEN)
    assert menu(bot) == AFTER


async def test_context_menus_are_left_alone(bot: commands.Bot, rules: Rules) -> None:
    apply_visibility(bot, rules)

    assert (
        bot.tree.get_command('Report message', type=discord.AppCommandType.message)
        is not None
    )
    assert 'Report message' not in rules.asked


async def test_another_bot_with_the_same_cogs_is_untouched(rules: Rules) -> None:
    async with sample_bot() as first:
        apply_visibility(first, rules)

    async with sample_bot() as second:
        assert menu(second) == BEFORE


# --- Prefix commands ---


async def test_every_prefix_command_stays(bot: commands.Bot, rules: Rules) -> None:
    before = prefix_commands(bot)
    groups = {
        name: dict(command.all_commands)
        for name, command in before.items()
        if isinstance(command, commands.Group)
    }

    apply_visibility(bot, rules)

    after = prefix_commands(bot)
    assert set(after) == PREFIX_NAMES
    for name, command in before.items():
        assert after[name] is command, name
    for name, children in groups.items():
        group = after[name]
        assert isinstance(group, commands.Group)
        assert group.all_commands == children, name


@pytest.mark.parametrize(
    'name', ['shutdown', 'cache', 'cache reload', 'members register', 'report']
)
async def test_a_prefix_command_runs_after_its_slash_form_is_removed(
    bot: commands.Bot, rules: Rules, name: str
) -> None:
    apply_visibility(bot, rules)
    sample = bot.get_cog('Sample')
    assert isinstance(sample, Sample)

    await run_prefix(bot, name)

    assert sample.ran == [name]


async def test_only_the_tree_tells_that_a_slash_form_is_gone(
    bot: commands.Bot, rules: Rules
) -> None:
    apply_visibility(bot, rules)

    # discord.py unlinks neither a hybrid command from its slash form nor a
    # slash command from its group: code that asks whether a command has a
    # slash form must look it up in the tree.
    members = slash_group(bot, 'members')
    register = bot.get_command('members register')
    assert isinstance(register, commands.HybridCommand)
    assert register.app_command is not None
    assert register.app_command.parent is members
    assert members.get_command('register') is None
    cache = bot.get_command('cache')
    assert isinstance(cache, commands.HybridGroup)
    assert isinstance(cache.app_command, app_commands.Group)
    assert bot.tree.get_command('cache') is None


# --- Logging ---


async def test_the_changes_are_logged(
    bot: commands.Bot, rules: Rules, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=SLASH_LOGGER)

    apply_visibility(bot, rules)

    messages = [
        record.getMessage() for record in caplog.records if record.name == SLASH_LOGGER
    ]
    assert len(messages) == 2
    hidden, removed = messages
    assert '/sweep (manage_messages)' in hidden
    assert '/legacy (ban_members)' in hidden
    assert '/members admin, /members stats reset' in removed
    assert '/cache' in removed


class Plain(commands.Cog):
    """A command for everyone, which the slash pass leaves as it is."""

    @commands.hybrid_command()
    async def ping(self, ctx: commands.Context[Any]) -> None:
        """For everyone"""


async def test_nothing_is_logged_when_nothing_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=SLASH_LOGGER)
    bot = new_bot()
    try:
        await bot.add_cog(Plain())

        assert apply_visibility(bot, Rules({'ping': EVERYONE})) == Visibility()
        assert menu(bot) == {'ping': (None, [])}
    finally:
        await bot.close()

    assert not [record for record in caplog.records if record.name == SLASH_LOGGER]


# --- The table's rules ---


class Lookalike(commands.Cog):
    """Commands named as some of TLE's and KCPC's, whose rules the table has."""

    @commands.hybrid_command(name='gitgud')
    async def gitgud(self, ctx: commands.Context[Any]) -> None:
        """Get a problem"""

    @commands.hybrid_command(name='_nogud')
    async def force_nogud(self, ctx: commands.Context[Any]) -> None:
        """Skip someone's problem"""

    @commands.hybrid_command(name='_updatestatus')
    async def update_status(self, ctx: commands.Context[Any]) -> None:
        """Mark the members as active"""

    # A command missing from the table.
    @commands.hybrid_command(name='mystery')
    async def mystery(self, ctx: commands.Context[Any]) -> None:
        """Unknown"""

    @commands.hybrid_group(fallback='show')
    async def cache(self, ctx: commands.Context[Any]) -> None:
        """The caches"""

    @cache.command(name='contests')
    async def cache_contests(self, ctx: commands.Context[Any]) -> None:
        """Reload the contests"""

    @commands.hybrid_group(fallback='show')
    async def meta(self, ctx: commands.Context[Any]) -> None:
        """The bot"""

    @meta.command(name='kill')
    async def meta_kill(self, ctx: commands.Context[Any]) -> None:
        """Stop the bot"""

    @meta.command(name='ping')
    async def meta_ping(self, ctx: commands.Context[Any]) -> None:
        """Check the bot"""

    @meta.command(name='git')
    async def meta_git(self, ctx: commands.Context[Any]) -> None:
        """Show the bot's version"""

    @meta.command(name='guilds')
    async def meta_guilds(self, ctx: commands.Context[Any]) -> None:
        """List the bot's servers"""

    @meta.command(name='mystery')
    async def meta_mystery(self, ctx: commands.Context[Any]) -> None:
        """Unknown"""

    @commands.hybrid_group(fallback='show')
    async def roleupdate(self, ctx: commands.Context[Any]) -> None:
        """Rank roles"""

    @roleupdate.command(name='now')
    async def roleupdate_now(self, ctx: commands.Context[Any]) -> None:
        """Update the rank roles"""

    @roleupdate.command(name='publish')
    async def roleupdate_publish(self, ctx: commands.Context[Any]) -> None:
        """Publish the rank changes"""

    @commands.hybrid_group(fallback='upcoming')
    async def contests(self, ctx: commands.Context[Any]) -> None:
        """Upcoming contests"""

    @contests.command(name='live')
    async def contests_live(self, ctx: commands.Context[Any]) -> None:
        """Contests running now"""

    @commands.hybrid_group()
    async def handle(self, ctx: commands.Context[Any]) -> None:
        """Handles"""

    @handle.command(name='show')
    async def handle_show(self, ctx: commands.Context[Any]) -> None:
        """Show handles"""

    @handle.command(name='set')
    async def handle_set(self, ctx: commands.Context[Any]) -> None:
        """Set a handle"""

    @handle.command(name='refer')
    async def handle_refer(self, ctx: commands.Context[Any]) -> None:
        """Refer a member"""

    @handle.command(name='grandfather')
    async def handle_grandfather(self, ctx: commands.Context[Any]) -> None:
        """Trust old members"""

    @handle.command(name='list', with_app_command=False)
    async def handle_list(self, ctx: commands.Context[Any]) -> None:
        """List handles"""

    @commands.hybrid_group(fallback='show')
    @app_commands.default_permissions(manage_guild=True)
    async def kcpc(self, ctx: commands.Context[Any]) -> None:
        """Club settings"""

    @kcpc.command(name='status')
    async def kcpc_status(self, ctx: commands.Context[Any]) -> None:
        """Club health"""

    @kcpc.group(name='contests')
    async def kcpc_contests(self, ctx: commands.Context[Any]) -> None:
        """Club contests"""

    @kcpc_contests.command(name='add')
    async def kcpc_contests_add(self, ctx: commands.Context[Any]) -> None:
        """Add a club contest"""

    @kcpc_contests.command(name='platforms')
    async def kcpc_contests_platforms(self, ctx: commands.Context[Any]) -> None:
        """Choose the platforms"""

    @commands.hybrid_command(name='help')
    async def help_(self, ctx: commands.Context[Any]) -> None:
        """Show the commands"""

    @commands.hybrid_group(fallback='show')
    async def access(self, ctx: commands.Context[Any]) -> None:
        """Access settings"""

    @access.group(name='bot-channels')
    async def access_bot_channels(self, ctx: commands.Context[Any]) -> None:
        """Bot channels"""

    @access_bot_channels.command(name='add')
    async def access_bot_channels_add(self, ctx: commands.Context[Any]) -> None:
        """Add a bot channel"""

    @access.command(name='limit')
    async def access_limit(self, ctx: commands.Context[Any]) -> None:
        """Limit a command"""


async def test_the_table_gives_the_rules_by_default() -> None:
    bot = new_bot()
    try:
        await bot.add_cog(Lookalike())

        visibility = apply_visibility(bot)

        assert menu(bot) == {
            'gitgud': (None, []),
            '_nogud': (MANAGE_MESSAGES, []),
            '_updatestatus': (MANAGE_GUILD, []),
            'meta': (None, ['show', 'ping']),
            'roleupdate': (MANAGE_MESSAGES, ['show', 'now', 'publish']),
            # Its fallback, /contests upcoming, runs ;contests.
            'contests': (None, ['upcoming', 'live']),
            'handle': (None, ['show', 'refer']),
            # The bot owner's /kcpc contests add stays under hidden /kcpc.
            'kcpc': (
                MANAGE_GUILD,
                ['show', 'status', ('contests', ['add', 'platforms'])],
            ),
            'help': (None, []),
            'access': (MANAGE_GUILD, ['show', ('bot-channels', ['add']), 'limit']),
        }
        # Commands missing from the table are the bot owner's.
        assert visibility.removed == [
            'mystery',
            'cache',
            'meta kill',
            'meta git',
            'meta guilds',
            'meta mystery',
            'handle set',
            'handle grandfather',
        ]
    finally:
        await bot.close()


async def test_the_access_service_offers_only_the_slash_forms_left() -> None:
    # Refusals and /help name a command's slash form: never one the pass has
    # removed, and to a member only one their slash list shows. A slash form
    # that was removed still looks attached, so the service must ask the tree.
    bot = new_bot()
    try:
        await bot.add_cog(Lookalike())
        apply_visibility(bot)
        access = AccessService(bot)
        member = MagicMock(spec=discord.Member)
        member.guild_permissions = discord.Permissions.none()
        moderator = MagicMock(spec=discord.Member)
        moderator.guild_permissions = discord.Permissions(manage_messages=True)

        def paths(name: str) -> tuple[str | None, ...]:
            command = bot.get_command(name)
            assert command is not None, name
            return (
                access.slash_path(command),
                access.listed_slash_path(command, member),
                access.listed_slash_path(command, moderator),
            )

        assert {
            name: paths(name)
            for name in (
                'gitgud',
                'meta',
                'meta ping',
                'meta git',
                'meta kill',
                'cache',
                'cache contests',
                'contests',
                'handle',
                'handle set',
                'handle list',
                '_nogud',
                'roleupdate now',
                '_updatestatus',
                'kcpc contests add',
            )
        } == {
            'gitgud': ('/gitgud', '/gitgud', '/gitgud'),
            'meta': ('/meta show', '/meta show', '/meta show'),
            'meta ping': ('/meta ping', '/meta ping', '/meta ping'),
            # Removed from the slash list: only their prefix forms are left.
            'meta git': (None, None, None),
            'meta kill': (None, None, None),
            'cache': (None, None, None),
            'cache contests': (None, None, None),
            # A group's own command, and a twin, through their slash forms.
            'contests': ('/contests upcoming',) * 3,
            'handle': ('/handle show',) * 3,
            'handle set': (None, None, None),
            'handle list': (None, None, None),  # prefix only
            # Hidden from members who lack the permission.
            '_nogud': ('/_nogud', None, '/_nogud'),
            'roleupdate now': ('/roleupdate now', None, '/roleupdate now'),
            '_updatestatus': ('/_updatestatus', None, None),
            'kcpc contests add': ('/kcpc contests add', None, None),
        }
    finally:
        await bot.close()


def test_the_default_rule_is_the_tables() -> None:
    assert default_rule('duel register') == table.RULES['duel register']
    assert default_rule('kcpc contests add') == Rule(Who.OWNER, Where.ANYWHERE)


def test_a_twin_has_its_canonical_commands_rule() -> None:
    assert default_rule('contests upcoming') == table.RULES['contests']
    assert default_rule('handle') == table.RULES['handle show']


@pytest.mark.parametrize('name', ['mystery', 'meta mystery', 'clist show', ''])
def test_a_command_missing_from_the_table_fails_closed(name: str) -> None:
    assert default_rule(name) == FAIL_CLOSED


def test_the_default_rule_reads_the_table_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(table, 'RULES', {'gitgud': OWNER})

    assert default_rule('gitgud') == OWNER
    assert default_rule('upsolve') == FAIL_CLOSED
