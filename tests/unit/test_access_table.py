"""Tests for tle.access.table: every command's rule, the twins, limit keys and
the categories of /help.

The expected rules are listed here grouped by rule, as they were chosen,
while the table lists them by cog, so that a slip in either shows up. The
last tests read the cogs' source, without importing them, and check that the
table has a rule for exactly the commands they declare.
"""

import ast
import functools
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tle.access.rules import STAFF_LEVELS, Limit, Rule, Where, Who
from tle.access.settings import GuildAccess
from tle.access.table import (
    BY_DIRECT_MESSAGE,
    CATEGORIES,
    COG_CATEGORY,
    NAME_CATEGORY,
    OWNER_AND_ADMIN,
    PRIVATE_ON_SLASH,
    PROTECTED_ROOTS,
    RULES,
    TWINS,
    by_direct_message,
    canonical,
    category_of,
    default_who,
    is_protected,
    limit_keys,
    private_on_slash,
    rule_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TLE_COG_FILES = sorted((REPO_ROOT / 'tle' / 'cogs').glob('*.py'))
KCPC_COG_FILES = sorted((REPO_ROOT / 'tle' / 'kcpc' / 'features').glob('*/cog.py'))
KCPC_ADMIN_MODULE = REPO_ROOT / 'tle' / 'kcpc' / 'bot' / 'admin.py'

# The commands that the access package adds itself: /help and /access.
NEW_COMMANDS = frozenset(
    {
        'help',
        'access',
        'access bot-channels',
        'access bot-channels add',
        'access bot-channels remove',
        'access staff-channel',
        'access limit',
        'access reset',
    }
)
# The cogs that the access package adds.
NEW_COGS = frozenset({'Help', 'Access'})

EVERYONE, TRUSTED, MODERATOR = Who.EVERYONE, Who.TRUSTED, Who.MODERATOR
DEVELOPER, ADMIN, OWNER = Who.DEVELOPER, Who.ADMIN, Who.OWNER
ANYWHERE, BOT, BOT_ONLY = Where.ANYWHERE, Where.BOT, Where.BOT_ONLY
STAFF, STAFF_ONLY = Where.STAFF, Where.STAFF_ONLY

# Every canonical command, grouped by its rule.
EXPECTED: tuple[tuple[Who, Where, tuple[str, ...]], ...] = (
    # Members' commands, public in bot channels and private on slash elsewhere.
    (
        EVERYONE,
        BOT,
        (
            # practice
            'gitgud',
            'upsolve',
            'gotgud',
            'nogud',
            'gitlog',
            'gimme',
            'stalk',
            'mashup',
            'vc',
            'fullsolve',
            'teamrate',
            # contests
            'clist',
            'clist future',
            'clist active',
            'clist finished',
            'remind',
            'remind settings',
            'remind on',
            'remind off',
            'get_ratedvc_channel',
            'vcrating',
            # duels
            'duel',
            'duel selfregister',
            'duel profile',
            'duel vshistory',
            'duel history',
            'duel recent',
            'duel ongoing',
            'duel rating',
            # graphs
            'plot',
            'plot distrib',
            'plot cfdistrib',
            'plot rating',
            'plot extreme',
            'plot solved',
            'plot hist',
            'plot curve',
            'plot scatter',
            'plot centile',
            'plot howgud',
            'plot country',
            'plot visualrank',
            'plot speed',
            # handles
            'handle show',
            'handle identify',
            'handle get',
            'handle rget',
            'handle unmagic',
            'role',
            # the bot
            'meta',
            'meta ping',
            'meta uptime',
            'help',
            # KCPC
            'event',
            'event this-week',
            'contests',
            'contests live',
            'weekly',
            'weekly history',
            'algo',
            'algo history',
            'randproblem',
            'profile',
            'unlink',
            'notify',
            'link',
            'link codeforces',
            'link atcoder',
            'link verify',
        ),
    ),
    # Commands that involve or ping other members: bot channels only.
    (
        EVERYONE,
        BOT_ONLY,
        (
            'ranklist',
            'ratedvc',
            'vcratings',
            'duel challenge',
            'duel accept',
            'duel decline',
            'duel withdraw',
            'duel draw',
            'duel invalidate',
            'duel ranklist',
            'duel complete',
            'handle list',
            'gudgitters',
            'rank',
        ),
    ),
    (TRUSTED, BOT_ONLY, ('handle refer',)),
    (
        MODERATOR,
        BOT,
        (
            '_nogud',
            '_unregistervc',
            'duel register',
            'duel _invalidate',
            'handle set',
            'handle remove',
        ),
    ),
    (
        MODERATOR,
        STAFF,
        (
            'handle unmagic_all',
            'handle unmagic_debug',
            'roleupdate',
            'roleupdate now',
            'roleupdate auto',
        ),
    ),
    # Setup commands that act on the channel they are used in.
    (MODERATOR, ANYWHERE, ('roleupdate publish',)),
    (ADMIN, ANYWHERE, ('remind here', 'set_ratedvc_channel', 'starboard here')),
    (
        ADMIN,
        STAFF,
        (
            'remind clear',
            'handle grandfather',
            '_updatestatus',
            'starboard',
            'starboard add',
            'starboard delete',
            'starboard edit_threshold',
            'starboard edit_color',
            'starboard clear',
            'starboard remove',
            'kcpc',
            'kcpc channel',
            'kcpc role',
            'kcpc enable',
            'kcpc disable',
            'kcpc workshops',
            'kcpc workshops calendar',
            'kcpc workshops sync',
            'kcpc accounts',
            'kcpc accounts unlink',
            'kcpc weekly',
            'kcpc weekly queue',
            'kcpc weekly unqueue',
            'kcpc weekly solution',
            'kcpc weekly rotation',
            'kcpc weekly preview',
            'kcpc weekly post-now',
            'kcpc algo',
            'kcpc algo reroll',
            'kcpc algo post-now',
            'kcpc algo preview',
            'kcpc contests',
            'kcpc contests platforms',
            'kcpc contests start-posts',
            'kcpc contests results',
            'access',
            'access bot-channels',
            'access bot-channels add',
            'access bot-channels remove',
            'access staff-channel',
            'access limit',
            'access reset',
        ),
    ),
    (DEVELOPER, STAFF, ('meta git',)),
    (DEVELOPER, STAFF_ONLY, ('kcpc status',)),
    # What every server shares: the bot owner's alone.
    (
        OWNER,
        ANYWHERE,
        (
            'meta kill',
            'meta guilds',
            'cache',
            'cache contests',
            'cache problems',
            'cache ratingchanges',
            'cache problemsets',
            'kcpc contests add',
            'kcpc contests settime',
            'kcpc contests remove',
            'kcpc contests sync',
        ),
    ),
)


def expected_rules() -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    for who, where, names in EXPECTED:
        for name in names:
            assert name not in rules, f'{name} is listed twice'
            rules[name] = Rule(who, where)
    return rules


# --- The rules ---


def test_every_rule() -> None:
    expected = expected_rules()

    wrong = {
        name: (RULES.get(name), rule)
        for name, rule in expected.items()
        if RULES.get(name) != rule
    }
    assert wrong == {}
    assert sorted(set(RULES) - set(expected)) == []
    assert len(RULES) == len(expected) == 154


@pytest.mark.parametrize(
    ('names', 'rule'),
    [
        (['kcpc status'], Rule(DEVELOPER, STAFF_ONLY)),
        (
            [
                'kcpc contests add',
                'kcpc contests settime',
                'kcpc contests remove',
                'kcpc contests sync',
            ],
            Rule(OWNER, ANYWHERE),
        ),
        # The group's own callback only shows its help.
        (['kcpc contests'], Rule(ADMIN, STAFF)),
        (['meta kill', 'meta guilds'], Rule(OWNER, ANYWHERE)),
        (
            [
                'cache',
                'cache contests',
                'cache problems',
                'cache ratingchanges',
                'cache problemsets',
            ],
            Rule(OWNER, ANYWHERE),
        ),
        (['meta git'], Rule(DEVELOPER, STAFF)),
        (['meta', 'meta ping', 'meta uptime'], Rule(EVERYONE, BOT)),
        (['help'], Rule(EVERYONE, BOT)),
    ],
)
def test_rules_that_matter_most(names: list[str], rule: Rule) -> None:
    assert {name: RULES[name] for name in names} == dict.fromkeys(names, rule)


def test_every_access_command_is_for_admins_in_the_staff_channel() -> None:
    access = {name: rule for name, rule in RULES.items() if name.split()[0] == 'access'}

    assert set(access) == NEW_COMMANDS - {'help'}
    assert set(access.values()) == {Rule(ADMIN, STAFF)}


def test_every_kcpc_command_is_for_staff() -> None:
    kcpc = [rule for name, rule in RULES.items() if name.split()[0] == 'kcpc']

    assert len(kcpc) == 30
    assert all(rule.who in STAFF_LEVELS for rule in kcpc)


def test_no_rule_is_private_by_default() -> None:
    # Only a server's limits make answers private; /help and /access make
    # their own slash answers private.
    assert [name for name, rule in RULES.items() if rule.private] == []


def by_root() -> dict[str, list[Rule]]:
    """Each top-level command's rules, its subcommands' included."""
    trees: dict[str, list[Rule]] = {}
    for name, rule in RULES.items():
        trees.setdefault(name.split()[0], []).append(rule)
    return trees


def test_the_trees_kept_out_of_members_slash_lists() -> None:
    # Removed from the slash list when every command in the tree is the bot
    # owner's; hidden behind Manage Messages when every one is for moderators,
    # and behind Manage Server when every one is for some other staff.
    trees = by_root()
    owners = {
        root for root, rules in trees.items() if all(r.who is OWNER for r in rules)
    }
    staff = {
        root
        for root, rules in trees.items()
        if all(rule.who in STAFF_LEVELS for rule in rules)
    }
    moderators = {
        root for root in staff if all(rule.who is MODERATOR for rule in trees[root])
    }

    assert owners == {'cache'}
    assert staff - owners == {
        '_nogud',
        '_unregistervc',
        'set_ratedvc_channel',
        '_updatestatus',
        'roleupdate',
        'starboard',
        'kcpc',
        'access',
    }
    assert moderators == {'_nogud', '_unregistervc', 'roleupdate'}


def test_the_staff_commands_in_members_trees() -> None:
    # They leave members' slash lists, and keep working as prefix commands.
    trees = by_root()
    mixed = {
        root
        for root, rules in trees.items()
        if not all(rule.who in STAFF_LEVELS for rule in rules)
    }

    staff = {
        name
        for name, rule in RULES.items()
        if name.split()[0] in mixed and rule.who in STAFF_LEVELS
    }

    assert staff == {
        'remind here',
        'remind clear',
        'duel register',
        'duel _invalidate',
        'handle set',
        'handle remove',
        'handle unmagic_all',
        'handle unmagic_debug',
        'handle grandfather',
        'meta git',
        'meta kill',
        'meta guilds',
    }


# --- Twins, names and limit keys ---


def test_the_twins() -> None:
    assert dict(TWINS) == {
        'contests upcoming': 'contests',
        'weekly current': 'weekly',
        'algo current': 'algo',
        'handle': 'handle show',
    }
    assert set(TWINS).isdisjoint(RULES)
    assert set(TWINS.values()) <= set(RULES)


def test_a_twin_has_its_canonical_names_rule() -> None:
    for twin, name in TWINS.items():
        assert rule_for(twin) == RULES[name]


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('contests upcoming', 'contests'),
        ('weekly current', 'weekly'),
        ('algo current', 'algo'),
        ('handle', 'handle show'),
        ('contests', 'contests'),
        ('handle show', 'handle show'),
        ('duel register', 'duel register'),
        ('  handle  ', 'handle show'),
        ('contests\t upcoming', 'contests'),
        ('not a command', 'not a command'),
        ('', ''),
    ],
)
def test_canonical(name: str, expected: str) -> None:
    assert canonical(name) == expected


@pytest.mark.parametrize(
    ('name', 'rule'),
    [
        ('gitgud', Rule(EVERYONE, BOT)),
        ('handle', Rule(EVERYONE, BOT)),
        (' kcpc  status ', Rule(DEVELOPER, STAFF_ONLY)),
        ('not a command', None),
        ('', None),
        # A slash fallback's path is not a command's name.
        ('clist show', None),
    ],
)
def test_rule_for(name: str, rule: Rule | None) -> None:
    assert rule_for(name) == rule


def test_the_protected_roots() -> None:
    assert PROTECTED_ROOTS == {'help', 'access'}


@pytest.mark.parametrize(
    ('name', 'protected'),
    [
        ('help', True),
        ('access', True),
        ('access limit', True),
        ('access bot-channels add', True),
        (' access  reset ', True),
        ('helpful', False),
        ('accessible', False),
        ('kcpc', False),
        ('meta', False),
        ('handle', False),
        ('', False),
    ],
)
def test_is_protected(name: str, protected: bool) -> None:
    assert is_protected(name) is protected


# --- How commands answer, and who else the owner must be ---


def test_every_command_that_answers_privately_on_slash_has_a_rule() -> None:
    for name in PRIVATE_ON_SLASH | BY_DIRECT_MESSAGE:
        assert name in RULES, name


@pytest.mark.parametrize(
    ('name', 'private'),
    [
        ('help', True),
        ('access limit', True),
        ('kcpc', True),
        ('kcpc status', True),
        ('kcpc contests add', True),
        ('link', True),
        ('link codeforces', True),
        ('unlink', True),
        ('notify', True),
        ('handle identify', True),
        # Only handle identify of the handle commands.
        ('handle', False),
        ('handle show', False),
        ('handle set', False),
        ('gitgud', False),
        ('contests upcoming', False),
        ('linked', False),
        ('', False),
    ],
)
def test_private_on_slash(name: str, private: bool) -> None:
    assert private_on_slash(name) is private


def test_by_direct_message() -> None:
    assert BY_DIRECT_MESSAGE == {'meta guilds'}
    assert by_direct_message(' meta  guilds ')
    assert not by_direct_message('meta')
    assert not by_direct_message('meta kill')


def test_the_owners_commands_that_need_an_admin() -> None:
    # The club contest commands keep KCPC's admin check (see the next test).
    assert OWNER_AND_ADMIN == {
        'kcpc contests add',
        'kcpc contests settime',
        'kcpc contests remove',
        'kcpc contests sync',
    }
    for name in OWNER_AND_ADMIN:
        assert RULES[name] == Rule(OWNER, ANYWHERE), name
        assert default_who(name) == {OWNER, ADMIN}
    assert default_who('meta kill') == {OWNER}
    assert default_who('gitgud') == {EVERYONE}
    assert default_who('handle') == {EVERYONE}  # handle show's twin
    assert default_who('not a command') == {OWNER}  # FAIL_CLOSED's


def test_the_owners_commands_that_need_an_admin_are_those_kcpc_checks() -> None:
    # A command of the owner's that KCPC's admin check also guards refuses an
    # owner who isn't an admin, so its rule must say so for /help to.
    checked = admin_checked(KCPC_COG_FILES)
    owners = {name for name, rule in RULES.items() if rule.who is OWNER}

    guarded = {
        name
        for name, command in registered().items()
        if (command.cog, command.method) in checked and name in owners
    }

    assert guarded == OWNER_AND_ADMIN
    # And KCPC's admin check guards most of the admin commands too.
    assert len(checked) > len(OWNER_AND_ADMIN)


@pytest.mark.parametrize(
    ('name', 'keys'),
    [
        ('gitgud', ('gitgud', 'gitgud *')),
        ('duel', ('duel', 'duel *')),
        ('duel register', ('duel register', 'duel register *', 'duel *')),
        (
            'kcpc contests add',
            ('kcpc contests add', 'kcpc contests add *', 'kcpc contests *', 'kcpc *'),
        ),
        (
            'access bot-channels add',
            (
                'access bot-channels add',
                'access bot-channels add *',
                'access bot-channels *',
                'access *',
            ),
        ),
        # A twin's keys are its canonical name's.
        ('contests upcoming', ('contests', 'contests *')),
        ('weekly current', ('weekly', 'weekly *')),
        ('handle', ('handle show', 'handle show *', 'handle *')),
        ('  duel   register ', ('duel register', 'duel register *', 'duel *')),
        ('', ()),
        ('   ', ()),
    ],
)
def test_limit_keys(name: str, keys: tuple[str, ...]) -> None:
    assert limit_keys(name) == keys


def test_every_limit_key_can_be_stored() -> None:
    for name in [*RULES, *TWINS]:
        keys = limit_keys(name)
        GuildAccess(limits={key: Limit(off=True) for key in keys})
        assert keys[0] == canonical(name)


def test_a_group_key_covers_exactly_the_group_and_its_subcommands() -> None:
    names = [*RULES, *TWINS]

    covered = {name for name in names if 'duel *' in limit_keys(name)}
    assert covered == {name for name in RULES if name.split()[0] == 'duel'}

    # The member contests group and the admin one under kcpc are different.
    covered = {name for name in names if 'contests *' in limit_keys(name)}
    assert covered == {'contests', 'contests live', 'contests upcoming'}

    for name in names:
        for key in limit_keys(name)[1:]:
            group = key.removesuffix(' *')
            assert f'{canonical(name)} '.startswith(f'{group} '), (name, key)


# --- Categories ---


def test_the_categories() -> None:
    assert [(category.key, category.title) for category in CATEGORIES] == [
        ('practice', 'Practice'),
        ('contests', 'Contests'),
        ('club', 'Club'),
        ('accounts', 'Handles and accounts'),
        ('duels', 'Duels'),
        ('graphs', 'Graphs'),
        ('bot', 'Bot'),
        ('setup', 'Server setup'),
        ('owner', 'Bot owner'),
    ]
    for category in CATEGORIES:
        text = category.description
        assert text[0].isupper() and not text.endswith('.'), text
        assert len(text) <= 80, text


def test_the_cog_categories() -> None:
    assert dict(COG_CATEGORY) == {
        'Codeforces': 'practice',
        'KcpcProblems': 'practice',
        'Contests': 'contests',
        'KcpcContests': 'contests',
        'KcpcWorkshops': 'club',
        'KcpcAlgo': 'club',
        'KcpcNotify': 'club',
        'Handles': 'accounts',
        'KcpcAccounts': 'accounts',
        'Dueling': 'duels',
        'Graphs': 'graphs',
        'Help': 'bot',
        'Meta': 'bot',
        'Access': 'setup',
        'KcpcAdmin': 'setup',
        'Starboard': 'setup',
        'CacheControl': 'owner',
    }
    assert dict(NAME_CATEGORY) == {'gudgitters': 'practice'}
    keys = {category.key for category in CATEGORIES}
    assert set(COG_CATEGORY.values()) <= keys
    assert set(NAME_CATEGORY.values()) <= keys
    assert set(NAME_CATEGORY) <= set(RULES)


@pytest.mark.parametrize(
    ('name', 'cog', 'key'),
    [
        # The bot owner's commands first, whatever their cog.
        ('cache', 'CacheControl', 'owner'),
        ('meta kill', 'Meta', 'owner'),
        ('meta guilds', 'Meta', 'owner'),
        ('kcpc contests add', 'KcpcContests', 'owner'),
        ('kcpc contests sync', 'KcpcContests', 'owner'),
        # A command with no rule is the owner's alone.
        ('not a command', 'Codeforces', 'owner'),
        # Then everything under /kcpc and /access, whatever its cog.
        ('kcpc', 'KcpcAdmin', 'setup'),
        ('kcpc status', 'KcpcAdmin', 'setup'),
        ('kcpc contests platforms', 'KcpcContests', 'setup'),
        ('kcpc weekly queue', 'KcpcProblems', 'setup'),
        ('kcpc algo reroll', 'KcpcAlgo', 'setup'),
        ('kcpc accounts unlink', 'KcpcAccounts', 'setup'),
        ('access', None, 'setup'),
        ('access limit', 'Access', 'setup'),
        # Then the commands placed by name, and then each cog's.
        ('gudgitters', 'Handles', 'practice'),
        ('handle show', 'Handles', 'accounts'),
        ('handle', 'Handles', 'accounts'),
        ('meta ping', 'Meta', 'bot'),
        ('meta git', 'Meta', 'bot'),
        ('help', 'Help', 'bot'),
        ('gitgud', 'Codeforces', 'practice'),
        ('weekly current', 'KcpcProblems', 'practice'),
        ('contests upcoming', 'KcpcContests', 'contests'),
        ('remind clear', 'Contests', 'contests'),
        ('algo current', 'KcpcAlgo', 'club'),
        ('event', 'KcpcWorkshops', 'club'),
        ('notify', 'KcpcNotify', 'club'),
        ('link verify', 'KcpcAccounts', 'accounts'),
        ('duel challenge', 'Dueling', 'duels'),
        ('plot rating', 'Graphs', 'graphs'),
        ('starboard here', 'Starboard', 'setup'),
        # The rest are the bot's.
        ('gitgud', 'SomeNewCog', 'bot'),
        ('gitgud', None, 'bot'),
    ],
)
def test_category_of(name: str, cog: str | None, key: str) -> None:
    category = category_of(name, cog)

    assert category.key == key
    assert category in CATEGORIES


# --- The table matches the cogs' source ---

_DECORATORS = frozenset({'command', 'group', 'hybrid_command', 'hybrid_group'})


class UnreadableCog(Exception):
    """A cog declares a command in a way the reader below doesn't understand."""


@dataclass(frozen=True)
class Declared:
    """A command as a cog's source declares it."""

    cog: str  # the cog's name
    method: str  # the method it is declared on
    name: str
    group: bool
    fallback: str | None
    slash: bool  # with_app_command
    parent: str | None  # the method of its group in the same cog
    attached: bool  # added under the bot's /kcpc with attach_admin_group


def declared_commands(source: str) -> list[Declared]:
    """Every command that the cogs in ``source`` declare.

    Reads command decorators, a group's ``@group.command`` subcommands, and
    what a cog nests itself: ``self.group.add_command(self.command)``, also in
    a loop over a tuple or list of commands, and ``attach_admin_group``.
    """
    found: list[Declared] = []
    tree = ast.parse(source)
    for cog in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        commands: dict[str, dict[str, object]] = {}
        for method in cog.body:
            if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                declared = _declaration(cog, method, commands)
                if declared is not None:
                    commands[method.name] = declared
        for group, command in _nested(cog):
            _check_command(cog, commands, group, group=True)
            _check_command(cog, commands, command)
            commands[command]['parent'] = group
        for node in ast.walk(cog):
            if isinstance(node, ast.Call) and _calls(node, 'attach_admin_group'):
                group = _self_attribute(cog, node.args[1])
                _check_command(cog, commands, group, group=True)
                commands[group]['attached'] = True
        name = _cog_name(cog, tree)
        found += [
            Declared(cog=name, method=method, **fields)  # type: ignore[arg-type]
            for method, fields in commands.items()
        ]
    return found


def _declaration(
    cog: ast.ClassDef,
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    commands: dict[str, dict[str, object]],
) -> dict[str, object] | None:
    for decorator in method.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        function = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not (isinstance(function, ast.Attribute) and function.attr in _DECORATORS):
            continue
        owner = function.value
        if isinstance(owner, ast.Name) and owner.id == 'commands':
            parent = None
        elif isinstance(owner, ast.Name) and owner.id in commands:
            parent = owner.id
            _check_command(cog, commands, parent, group=True)
        else:
            raise UnreadableCog(f'{cog.name}.{method.name}: {ast.unparse(decorator)}')
        return {
            'name': _keyword(call, 'name', method.name),
            'group': function.attr in ('group', 'hybrid_group'),
            'fallback': _keyword(call, 'fallback', None),
            'slash': _keyword(call, 'with_app_command', True),
            'parent': parent,
            'attached': False,
        }
    return None


def _keyword(call: ast.Call | None, name: str, default: object) -> object:
    for keyword in call.keywords if call is not None else []:
        if keyword.arg == name:
            if not isinstance(keyword.value, ast.Constant):
                raise UnreadableCog(f'{name}={ast.unparse(keyword.value)}')
            return keyword.value.value
    return default


def _nested(cog: ast.ClassDef) -> Iterator[tuple[str, str]]:
    """(group, command) for each ``add_command`` call in the cog's methods.

    A call in a loop adds each command the loop goes through, so a loop
    variable is read from the loop around the call that uses it.
    """
    for method in cog.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        in_loops: set[int] = set()
        for loop in ast.walk(method):
            if not (isinstance(loop, ast.For) and isinstance(loop.target, ast.Name)):
                continue
            variable = loop.target.id
            calls = [
                call
                for call in _add_command_calls(loop)
                if isinstance(call.args[0], ast.Name) and call.args[0].id == variable
            ]
            if not calls:
                continue
            commands = _commands_in(cog, method, loop.iter)
            for call in calls:
                in_loops.add(id(call))
                group = _self_attribute(cog, call.func.value)  # type: ignore[attr-defined]
                yield from ((group, command) for command in commands)
        for call in _add_command_calls(method):
            if id(call) not in in_loops:
                group = _self_attribute(cog, call.func.value)  # type: ignore[attr-defined]
                yield group, _self_attribute(cog, call.args[0])


def _add_command_calls(node: ast.AST) -> list[ast.Call]:
    """The ``<group>.add_command(<command>)`` calls in ``node``."""
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == 'add_command'
        and len(call.args) == 1
    ]


def _commands_in(
    cog: ast.ClassDef, method: ast.FunctionDef | ast.AsyncFunctionDef, items: ast.expr
) -> list[str]:
    """The commands that a loop goes through: a tuple or list of them, or the
    variable of the method that one is assigned to.
    """
    if isinstance(items, ast.Name):
        values = [
            node.value
            for node in ast.walk(method)
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == items.id
                    for target in node.targets
                )
            )
            or (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == items.id
            )
        ]
        if len(values) != 1 or values[0] is None:
            raise UnreadableCog(f'{cog.name}.{method.name}: what {items.id} holds')
        items = values[0]
    if not isinstance(items, ast.Tuple | ast.List):
        raise UnreadableCog(f'{cog.name}.{method.name}: {ast.unparse(items)}')
    return [_self_attribute(cog, item) for item in items.elts]


def _calls(call: ast.Call, name: str) -> bool:
    """Whether ``call`` calls a function or method called ``name``."""
    function = call.func
    return (isinstance(function, ast.Name) and function.id == name) or (
        isinstance(function, ast.Attribute) and function.attr == name
    )


def _self_attribute(cog: ast.ClassDef, node: ast.expr) -> str:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == 'self'
    ):
        return node.attr
    raise UnreadableCog(f'{cog.name}: {ast.unparse(node)} is not self.<command>')


def _check_command(
    cog: ast.ClassDef,
    commands: dict[str, dict[str, object]],
    method: str,
    *,
    group: bool = False,
) -> None:
    if method not in commands or (group and not commands[method]['group']):
        kind = 'group' if group else 'command'
        raise UnreadableCog(f'{cog.name}.{method} is not a {kind}')


def _cog_name(cog: ast.ClassDef, tree: ast.Module) -> str:
    """The cog's name: its class name unless the class says ``name=``."""
    for keyword in cog.keywords:
        if keyword.arg != 'name':
            continue
        if isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
        if isinstance(keyword.value, ast.Name):
            return _constant(keyword.value.id, tree)
        raise UnreadableCog(f'{cog.name}: name={ast.unparse(keyword.value)}')
    return cog.name


def _constant(name: str, tree: ast.Module) -> str:
    """The string that a module-level ``name = '...'`` assigns, in the module
    or in the module of the repository that it imports ``name`` from.
    """
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and any(alias.name == name and not alias.asname for alias in node.names)
        ):
            source = REPO_ROOT.joinpath(*node.module.split('.')).with_suffix('.py')
            if source.is_file():
                return _constant(name, ast.parse(source.read_text(encoding='utf-8')))
    raise UnreadableCog(f'{name} is not a string constant')


def admin_group_name() -> str:
    """The name of the group that KCPC's admin groups are attached under."""
    return _constant(
        'ADMIN_GROUP_NAME', ast.parse(KCPC_ADMIN_MODULE.read_text(encoding='utf-8'))
    )


def qualified_names(declared: list[Declared], admin_group: str) -> dict[str, Declared]:
    by_method = {(command.cog, command.method): command for command in declared}

    def qualified(command: Declared) -> str:
        if command.parent is not None:
            parent = by_method[command.cog, command.parent]
            return f'{qualified(parent)} {command.name}'
        if command.attached:
            return f'{admin_group} {command.name}'
        return command.name

    names: dict[str, Declared] = {}
    for command in declared:
        name = qualified(command)
        assert name not in names, f'{name} is declared twice'
        names[name] = command
    return names


def declared_in(paths: list[Path]) -> list[Declared]:
    return [
        command
        for path in paths
        for command in declared_commands(path.read_text(encoding='utf-8'))
    ]


@functools.cache
def registered() -> dict[str, Declared]:
    """Every command the cogs declare, by qualified name."""
    return qualified_names(
        declared_in(TLE_COG_FILES + KCPC_COG_FILES), admin_group_name()
    )


def admin_checked(paths: list[Path]) -> set[tuple[str, str]]:
    """The cog and method of every command in ``paths`` that KCPC's admin
    check guards: those decorated with ``kcpc_admin_only()``.
    """
    checked: set[tuple[str, str]] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for cog in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for method in cog.body:
                if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                    isinstance(decorator, ast.Call)
                    and _calls(decorator, 'kcpc_admin_only')
                    for decorator in method.decorator_list
                ):
                    checked.add((_cog_name(cog, tree), method.name))
    return checked


SAMPLE = """
from tle.kcpc.features.accounts.views import ACCOUNTS_COG

class Sample(commands.Cog, name='Renamed'):
    @commands.hybrid_group(fallback='show', brief='Top')
    async def top(self, ctx): ...

    @top.command(name='sub-one', with_app_command=False)
    @commands.cooldown(1, 10)
    async def sub_one(self, ctx): ...

    @top.group()
    async def inner(self, ctx): ...

    @inner.command
    async def leaf(self, ctx): ...

    @commands.command()
    async def plain(self, ctx): ...

    @commands.hybrid_group(name='top')
    async def admin(self, ctx): ...

    @commands.hybrid_command(name='direct')
    async def direct(self, ctx): ...

    @commands.hybrid_command(name='looped')
    async def looped(self, ctx): ...

    @commands.hybrid_command(name='listed')
    async def listed(self, ctx): ...

    async def cog_load(self):
        attach_admin_group(self.bot, self.admin)
        self.admin.add_command(self.direct)
        members: tuple[X, ...] = (self.looped,)
        for command in members:
            self.top.add_command(command)
        for command in [self.listed]:
            self.inner.add_command(command)

    @commands.Cog.listener()
    async def on_ready(self): ...

    async def cog_unload(self):
        # Loops and tuples of other things are no commands.
        for name in (JOB, OTHER_JOB):
            await self.scheduler.remove(name)
        fields = (EmbedField('a', 'b'),)
        for command in self.bot.walk_commands():
            print(command, fields)


class Imported(commands.Cog, name=ACCOUNTS_COG):
    @commands.command()
    async def other(self, ctx): ...
"""


def test_the_reader_finds_every_kind_of_declaration() -> None:
    declared = declared_commands(SAMPLE)

    names = qualified_names(declared, 'kcpc')

    assert set(names) == {
        'top',
        'top sub-one',
        'top inner',
        'top inner leaf',
        'top looped',
        'top inner listed',
        'plain',
        'kcpc top',
        'kcpc top direct',
        'other',
    }
    assert {command.cog for command in declared} == {'Renamed', 'KcpcAccounts'}
    assert names['top'].fallback == 'show' and names['top'].group
    assert not names['top sub-one'].slash and names['top'].slash
    assert names['top inner'].group and not names['top inner leaf'].group
    assert names['kcpc top'].attached and not names['top'].attached


@pytest.mark.parametrize(
    'source',
    [
        'class C:\n    @app_commands.command()\n    async def f(self): ...',
        'class C:\n    @missing.command()\n    async def f(self): ...',
        'class C:\n    @commands.command(name=NAME)\n    async def f(self): ...',
        'class C:\n    async def g(self):\n        self.a.add_command(self.b)',
        'class C:\n    @commands.command()\n    async def f(self): ...\n'
        '    @f.command()\n    async def g(self): ...',
        'class C:\n    @commands.group()\n    async def g(self): ...\n'
        '    async def h(self):\n        for c in self.found():\n'
        '            self.g.add_command(c)',
        'class C(commands.Cog, name=UNKNOWN):\n    pass',
    ],
)
def test_the_reader_refuses_what_it_cannot_read(source: str) -> None:
    with pytest.raises(UnreadableCog):
        declared_commands(source)


def test_the_cogs_declare_100_tle_and_50_kcpc_commands() -> None:
    assert len(declared_in(TLE_COG_FILES)) == 100
    assert len(declared_in(KCPC_COG_FILES)) == 50


def test_the_table_has_a_rule_for_exactly_the_declared_commands() -> None:
    names = set(registered()) | NEW_COMMANDS
    ruled = set(RULES) | set(TWINS)

    assert sorted(names - ruled) == [], 'commands without a rule'
    assert sorted(ruled - names) == [], 'rules for no command'
    assert len(names) == 158
    assert len(RULES) == 158 - len(TWINS) == 154


def test_the_kcpc_admin_groups_are_attached_under_kcpc() -> None:
    names = registered()

    assert admin_group_name() == 'kcpc'
    assert names['kcpc'].group and names['kcpc'].parent is None
    assert {name for name, command in names.items() if command.attached} == {
        'kcpc workshops',
        'kcpc contests',
        'kcpc accounts',
        'kcpc weekly',
        'kcpc algo',
    }


def test_twins_are_prefix_fallbacks_or_a_group_shown_by_a_subcommand() -> None:
    names = registered()
    fallbacks = set()
    for name, command in names.items():
        group = names.get(name.rsplit(' ', 1)[0]) if command.parent else None
        if group is not None and not command.slash and group.fallback == command.name:
            fallbacks.add(name)

    # A prefix subcommand that does what its group's slash fallback does.
    assert fallbacks == {'contests upcoming', 'weekly current', 'algo current'}
    for twin in fallbacks:
        assert TWINS[twin] == twin.rsplit(' ', 1)[0]
    # The handle group's callback shows handles, as handle show does.
    assert names['handle'].group
    assert names['handle'].fallback is None
    assert names['handle show'].parent == names['handle'].method
    assert set(TWINS) == fallbacks | {'handle'}


def test_every_cog_with_commands_has_a_category() -> None:
    cogs = {command.cog for command in registered().values()}

    assert cogs == set(COG_CATEGORY) - NEW_COGS
