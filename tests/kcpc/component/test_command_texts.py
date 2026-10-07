"""What members read about every command, and how every command answers.

A command's brief is its slash description and its line in /help's lists, its
help is the description and the examples that /help shows, and its options'
descriptions are what Discord shows as members type. These tests hold them to
the bot's style: short, plain and complete, with examples that run the
commands they document, typed as Discord takes them. The bot boots as
``booting.booted`` boots it, with every extension, so these are all of TLE's
and KCPC's commands, with /help and /access. Each test lists every command
that fails, rather than stopping at the first.

The last tests read the source: commands answer through their context, which
keeps a private answer private, no text calls TLE's roles by role names, and
no command runs another past its checks.
"""

import ast
import inspect
import itertools
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from discord import app_commands
from discord.ext import commands

from tests.kcpc.component.booting import booted
from tle.__main__ import TLEBot
from tle.access.help import split_help
from tle.access.rules import Where
from tle.access.table import RULES, canonical

REPO_ROOT = Path(__file__).resolve().parents[3]

# The longest brief, well within Discord's 100 characters for a slash
# command's description. And Discord's limit on an option's description, past
# which discord.py cuts it short.
BRIEF_LIMIT = 80
OPTION_LIMIT = 100
# What Discord shows for an option without a description.
UNDESCRIBED = ('', '…')
# Discord's types of the options that are commands in a group.
SUBCOMMAND, SUBCOMMAND_GROUP = 1, 2
# Words that start a noun phrase: a brief is a verb phrase, such as "Show the
# next KCPC workshop", never "The next KCPC workshop".
NOT_VERBS = frozenset(
    {
        'A',
        'All',
        'An',
        'Command',
        'Commands',
        'Each',
        'Every',
        'Its',
        'My',
        'Our',
        'Some',
        'That',
        'The',
        'Their',
        'These',
        'This',
        'Those',
        'Your',
    }
)
# The brief of a group whose slash fallback, show, shows the group's help.
# Only such a group says that it shows commands. /kcpc's and /access's show
# fallbacks show the server's settings instead.
SHOWS_COMMANDS = re.compile(r'Show the .+ commands')
SHOWS_SETTINGS = re.compile(r"Show this server's .*settings")
SETTINGS_GROUPS = frozenset({'kcpc', 'access'})
# What an optional option's description says: what happens without it.
IF_LEFT_OUT = 'if left out'
# Optional slash options that their commands need all the same: their
# descriptions say so instead.
NEEDED_THOUGH_OPTIONAL = frozenset({('duel vshistory', 'member1')})
# An option given by name in an example, as in /gitgud delta:200, but not a
# link's scheme, as in https://example.com.
NAMED_OPTION = re.compile(r'([a-z][a-z0-9_]*):(?!//)')
# An example line that starts with a label, as in "member: @alice", rather
# than with the command as typed.
LABEL = re.compile(r'\w+:')
# A mention as Discord writes it, which names a member, a channel or a role by
# id; examples type them as @alice and #general instead. And a number as long
# as a Discord id.
RAW_MENTION = re.compile(r'<[@#]')
DISCORD_ID = re.compile(r'\b\d{17,20}\b')
# TLE's roles written like role names, as in "ask an Admin": texts say "an
# admin", as /help's levels do, since the roles can have any name.
ROLE_NAME = re.compile(
    r'\b(?:[Aa]n?|[Tt]he) (?:Admin|Moderator|Trusted|Purgatory|Developer)s?\b'
)

# The commands that involve or ping other members. They work in bot channels
# alone, where members expect them.
OTHERS_INVOLVED = frozenset(
    {
        'duel challenge',
        'duel accept',
        'duel decline',
        'duel withdraw',
        'duel draw',
        'duel invalidate',
        'duel complete',
        'duel ranklist',
        'ratedvc',
        'ranklist',
        'gudgitters',
        'vcratings',
        'handle list',
        'handle refer',
        'rank',
    }
)

# Where the code of commands and their replies lives.
COMMAND_SOURCES = ('tle/cogs', 'tle/kcpc', 'tle/util', 'tle/access')
# Sends that go around a command's context: in its channel, or straight to the
# member. Only the context keeps a private answer private.
AROUND_CONTEXT = frozenset(
    {
        ('ctx', 'channel'),
        ('ctx', 'author'),
        ('interaction', 'channel'),
        ('interaction', 'user'),
    }
)
# The code that may still send so, by file and the class or function it is
# in, and why.
ALLOWED_SENDS = {
    ('tle/cogs/duel.py', 'DuelChallengeView'): (
        "the challenge's buttons tell the channel how the duel goes"
    ),
    ('tle/cogs/handles.py', 'Handles.identify'): (
        ';handle identify sends the sign-in link by direct message'
    ),
    ('tle/cogs/meta.py', 'Meta.guilds'): (
        'the list of servers goes to the bot owner by direct message alone'
    ),
}

Command = commands.Command[Any, ..., Any]
SlashCommand = app_commands.Command[Any, ..., Any]


@pytest.fixture
async def bot(tmp_path: Path) -> AsyncIterator[TLEBot]:
    """The bot with every extension."""
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        yield bot


def every_command(bot: commands.Bot) -> list[Command]:
    """Every prefix command, groups, subcommands and twins included."""
    return sorted(bot.walk_commands(), key=lambda command: command.qualified_name)


def slash_entries(bot: commands.Bot) -> Iterator[tuple[str, dict[str, Any]]]:
    """Each slash command in the payload that the sync sends, by its path."""
    for command in bot.tree.get_commands():
        yield from _entries(command.to_dict(bot.tree))


def _entries(
    entry: dict[str, Any], prefix: str = ''
) -> Iterator[tuple[str, dict[str, Any]]]:
    path = f'{prefix}{entry["name"]}'
    inner = [
        option
        for option in entry.get('options', [])
        if option['type'] in (SUBCOMMAND, SUBCOMMAND_GROUP)
    ]
    if not inner:
        yield path, entry
    for option in inner:
        yield from _entries(option, f'{path} ')


def written_descriptions(command: Command) -> dict[str, str]:
    """The descriptions of ``command``'s options as written, before discord.py
    cuts any short: those of ``app_commands.describe``, and of flags.
    """
    written = getattr(
        command.callback, '__discord_app_commands_param_description__', {}
    )
    return {name: str(text) for name, text in written.items()}


def slash_target(
    bot: commands.Bot, words: list[str]
) -> tuple[SlashCommand | None, int]:
    """The slash command that ``words`` start with, through the tree that the
    sync sends, a group's fallback included, and how many words name it.
    """
    found: object = bot.tree.get_command(words[0]) if words[0] else None
    used = 1
    while isinstance(found, app_commands.Group) and used < len(words):
        child = found.get_command(words[used])
        if child is None:
            break
        found, used = child, used + 1
    if isinstance(found, app_commands.Command):
        return found, used
    return None, used


def prefix_target(bot: commands.Bot, words: list[str]) -> tuple[Command | None, int]:
    """The prefix command named by the longest run of ``words`` from the
    start, and how many words name it.
    """
    for used in range(len(words), 0, -1):
        found = bot.get_command(' '.join(words[:used]))
        # get_command ignores the words after a command that isn't a group.
        if found is not None and len(found.qualified_name.split()) == used:
            return found, used
    return None, 0


def flag_names(command: Command) -> frozenset[str]:
    """The names of the flags that ``command`` takes, as in off:yes."""
    names: set[str] = set()
    for parameter in command.clean_params.values():
        converter = parameter.converter
        if isinstance(converter, type) and issubclass(
            converter, commands.FlagConverter
        ):
            for flag in converter.get_flags().values():
                names.add(flag.name)
                names.update(flag.aliases)
    return frozenset(names)


def documents(command: Command, target: Command) -> bool:
    """Whether an example that runs ``target`` shows how to use ``command``:
    it runs the command itself, its twin, or one of its subcommands.
    """
    if canonical(target.qualified_name) == canonical(command.qualified_name):
        return True
    parent = target.parent
    while parent is not None:
        if parent.qualified_name == command.qualified_name:
            return True
        parent = parent.parent
    return False


def example_problem(bot: commands.Bot, command: Command, line: str) -> str | None:
    """What is wrong with ``line``, an example in ``command``'s help, if
    anything: it must be typed as a member types it, run the command or one of
    its subcommands, and name only options that the command has.
    """
    if LABEL.match(line):
        return 'it starts with a label'
    words = line.split()
    kind, words[0] = words[0][:1], words[0][1:]
    target: Command | None
    if kind == '/':
        app, used = slash_target(bot, words)
        if app is None:
            return 'there is no such slash command'
        wrapped = getattr(app, 'wrapped', None)
        target = wrapped if isinstance(wrapped, commands.Command) else None
        options = frozenset(parameter.display_name for parameter in app.parameters)
    elif kind == ';':
        target, used = prefix_target(bot, words)
        options = frozenset() if target is None else flag_names(target)
    else:
        return 'it starts with neither / nor ;'
    if target is None:
        return 'there is no such command'
    if not documents(command, target):
        return f'it runs {target.qualified_name}'
    for word in words[used:]:
        named = NAMED_OPTION.match(word)
        if named is not None and named.group(1) not in options:
            return f'{named.group(1)} is none of its options'
    return None


def unnamed_value(app: SlashCommand, words: list[str]) -> str | None:
    """The first of ``words``, which follow the slash command ``app`` in an
    example, that gives an option's value without the name Discord needs.

    As a member types, Discord fills the command's required options in order,
    so their values may come first, a word each, and the last one's may run
    on if the command takes the rest of the line for it. Any other value
    follows its option's name, as in /gitgud delta:200: Discord takes an
    optional option's value only once the option is picked by name.
    """
    named = {match.group(1) for word in words if (match := NAMED_OPTION.match(word))}
    unnamed = list(
        itertools.takewhile(lambda word: NAMED_OPTION.match(word) is None, words)
    )
    required = [
        parameter
        for parameter in app.parameters
        if parameter.required and parameter.display_name not in named
    ]
    if len(unnamed) <= len(required):
        return None
    if required and takes_the_rest(app, required[-1]):
        return None
    return unnamed[len(required)]


def takes_the_rest(app: SlashCommand, parameter: app_commands.Parameter) -> bool:
    """Whether ``app`` takes the rest of the line for ``parameter``, as
    ;kcpc contests platforms takes its list of platforms: the prefix command
    has it as a keyword-only parameter.
    """
    wrapped = getattr(app, 'wrapped', None)
    if not isinstance(wrapped, commands.Command):
        return False
    found = wrapped.clean_params.get(parameter.name)
    return found is not None and found.kind is inspect.Parameter.KEYWORD_ONLY


def texts_of(command: Command) -> Iterator[str]:
    """What /help and the slash list show of ``command``, but its examples."""
    yield command.brief or ''
    yield split_help(command.help)[0]
    yield from written_descriptions(command).values()


def source_files(folders: tuple[str, ...]) -> Iterator[tuple[str, ast.Module]]:
    """Each Python file in ``folders``, by its path in the repository, parsed."""
    for folder in folders:
        for path in sorted((REPO_ROOT / folder).rglob('*.py')):
            source = path.read_text(encoding='utf-8')
            yield path.relative_to(REPO_ROOT).as_posix(), ast.parse(source)


class _Calls(ast.NodeVisitor):
    """The calls in a module, each with the classes and functions it is in."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def _enter(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append(('.'.join(self.scope), node))
        self.generic_visit(node)


def calls_in(module: ast.Module) -> list[tuple[str, ast.Call]]:
    visitor = _Calls()
    visitor.visit(module)
    return visitor.calls


def send_around_context(call: ast.Call) -> str | None:
    """``call`` as typed, if it sends around a command's context."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == 'send'
        and isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Name)
    ):
        return None
    owner, attribute = func.value.value.id, func.value.attr
    if (owner, attribute) not in AROUND_CONTEXT:
        return None
    return f'{owner}.{attribute}.send'


def allowed_send(path: str, scope: str) -> tuple[str, str] | None:
    """The entry of ``ALLOWED_SENDS`` that lets code in ``scope`` of ``path``
    send around a command's context, if any.
    """
    for allowed in ALLOWED_SENDS:
        file, name = allowed
        if file == path and (scope == name or scope.startswith(f'{name}.')):
            return allowed
    return None


# Briefs and help


async def test_every_brief_is_a_short_verb_phrase(bot: TLEBot) -> None:
    wrong = []
    for command in every_command(bot):
        brief = command.brief or ''
        if not (
            brief
            and len(brief) <= BRIEF_LIMIT
            and brief[0].isupper()
            and not brief.endswith('.')
            and brief.split()[0] not in NOT_VERBS
        ):
            wrong.append(f'{command.qualified_name}: {brief!r}')

    assert wrong == []


async def test_only_a_group_whose_slash_fallback_is_show_shows_commands(
    bot: TLEBot,
) -> None:
    wrong = []
    for command in every_command(bot):
        name, brief = command.qualified_name, command.brief or ''
        show = isinstance(command, commands.HybridGroup) and command.fallback == 'show'
        if not show:
            fits = SHOWS_COMMANDS.fullmatch(brief) is None
        elif name in SETTINGS_GROUPS:
            fits = SHOWS_SETTINGS.match(brief) is not None
        else:
            fits = SHOWS_COMMANDS.fullmatch(brief) is not None
        if not fits:
            wrong.append(f'{name}: {brief!r}')

    assert wrong == []
    # The groups whose show fallbacks show settings are such groups.
    for name in SETTINGS_GROUPS:
        group = bot.get_command(name)
        assert isinstance(group, commands.HybridGroup) and group.fallback == 'show'


async def test_every_command_says_what_it_does(bot: TLEBot) -> None:
    # /help shows the help before its Examples: as the command's description.
    wrong = [
        command.qualified_name
        for command in every_command(bot)
        if not split_help(command.help)[0]
    ]

    assert wrong == []


# Options


async def test_every_slash_option_is_described(bot: TLEBot) -> None:
    wrong = []
    options = 0
    for path, entry in slash_entries(bot):
        for option in entry.get('options', []):
            options += 1
            if option['description'] in UNDESCRIBED:
                wrong.append(f'/{path} {option["name"]}')

    assert wrong == []
    assert options > 50


async def test_every_optional_slash_option_says_what_happens_without_it(
    bot: TLEBot,
) -> None:
    wrong = []
    for path, entry in slash_entries(bot):
        for option in entry.get('options', []):
            optional = not option.get('required', False)
            if optional and (path, option['name']) not in NEEDED_THOUGH_OPTIONAL:
                if IF_LEFT_OUT not in option['description']:
                    wrong.append(f'/{path} {option["name"]}: {option["description"]!r}')

    assert wrong == []
    # The options that say otherwise are optional in the slash list.
    shown = {
        (path, option['name']): option.get('required', False)
        for path, entry in slash_entries(bot)
        for option in entry.get('options', [])
    }
    assert {shown.get(each) for each in NEEDED_THOUGH_OPTIONAL} == {False}


async def test_no_option_description_is_cut_short(bot: TLEBot) -> None:
    # discord.py cuts a description past Discord's limit and ends it with …;
    # these are the descriptions as written.
    wrong = []
    written = 0
    for command in every_command(bot):
        for name, text in written_descriptions(command).items():
            written += 1
            if len(text) > OPTION_LIMIT:
                wrong.append(f'{command.qualified_name} {name}: {len(text)} characters')

    assert wrong == []
    assert written > 50


# Examples


async def test_every_example_runs_the_command_it_documents(bot: TLEBot) -> None:
    # /x examples run through the slash list, where /clist show runs the clist
    # group; ;x examples by the longest run of words that names a command.
    wrong = []
    examples = 0
    for command in every_command(bot):
        for line in split_help(command.help)[1]:
            examples += 1
            problem = example_problem(bot, command, line)
            if problem is not None:
                wrong.append(f'{command.qualified_name}: {line!r}: {problem}')

    assert wrong == []
    assert examples > 200


async def test_every_slash_example_names_the_options_discord_needs_named(
    bot: TLEBot,
) -> None:
    # Discord takes an optional option's value only after its name, so
    # /help command:clist future, never /help clist future.
    wrong = []
    examples = 0
    for command in every_command(bot):
        for line in split_help(command.help)[1]:
            words = line.split()
            if not words[0].startswith('/'):
                continue
            words[0] = words[0][1:]
            app, used = slash_target(bot, words)
            if app is None:
                continue  # the test above says so
            examples += 1
            value = unnamed_value(app, words[used:])
            if value is not None:
                wrong.append(f'{command.qualified_name}: {line!r}: {value} has no name')

    assert wrong == []
    assert examples > 100


@pytest.mark.parametrize(
    ('line', 'unnamed'),
    [
        # Optional options, which Discord fills only by name.
        ('/help command:clist future', None),
        ('/help clist future', 'clist'),
        ('/duel vshistory member1:@alice member2:@bob', None),
        ('/duel vshistory @alice @bob', '@alice'),
        # Required options, a word each, in order or by name.
        ('/kcpc channel workshops #workshops', None),
        ('/kcpc channel channel:#workshops feature:workshops', None),
        ('/kcpc role workshops role:@Workshops', None),
        ('/kcpc role workshops @Workshops', '@Workshops'),
        (
            '/kcpc weekly queue 1520D https://example.com/1520d',
            'https://example.com/1520d',
        ),
        # The rest of the line, for an option the command takes it for.
        ('/kcpc contests platforms codeforces atcoder manual', None),
    ],
)
async def test_an_example_names_the_options_discord_fills_only_by_name(
    bot: TLEBot, line: str, unnamed: str | None
) -> None:
    words = line[1:].split()
    app, used = slash_target(bot, words)
    assert app is not None

    assert unnamed_value(app, words[used:]) == unnamed


# What texts never do


async def test_no_text_names_a_member_a_channel_or_a_role_by_id(bot: TLEBot) -> None:
    wrong = []
    for command in every_command(bot):
        name = command.qualified_name
        for text in texts_of(command):
            if RAW_MENTION.search(text) or DISCORD_ID.search(text):
                wrong.append(f'{name}: {text!r}')
        # An example may give a message's id, but types mentions as @alice.
        for line in split_help(command.help)[1]:
            if RAW_MENTION.search(line):
                wrong.append(f'{name}: {line!r}')

    assert wrong == []


def test_no_text_calls_tle_s_roles_by_role_names() -> None:
    # The roles can have any name in a server, so texts say "a moderator", not
    # "a Moderator". The constants the code reads are no texts.
    wrong = []
    for path, module in source_files(COMMAND_SOURCES):
        for node in ast.walk(module):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ROLE_NAME.search(node.value):
                    wrong.append(f'{path}:{node.lineno}: {node.value!r}')

    assert wrong == []


# Where commands work, and how they answer


async def test_commands_that_involve_other_members_work_in_bot_channels_alone(
    bot: TLEBot,
) -> None:
    names = {command.qualified_name for command in bot.walk_commands()}
    bot_only = {name for name, rule in RULES.items() if rule.where is Where.BOT_ONLY}

    assert bot_only == OTHERS_INVOLVED
    assert OTHERS_INVOLVED <= names


def test_commands_answer_through_their_context() -> None:
    # A send in the channel is public, and a direct message isn't where the
    # command was used: only the context keeps a private answer private.
    found = [
        (path, scope, call.lineno, typed)
        for path, module in source_files(COMMAND_SOURCES)
        for scope, call in calls_in(module)
        if (typed := send_around_context(call)) is not None
    ]
    wrong = [
        f'{path}:{line} in {scope or "the module"}: {typed}'
        for path, scope, line, typed in found
        if allowed_send(path, scope) is None
    ]

    assert wrong == []
    # Each place on the list still sends so; one that no longer does comes off.
    assert {allowed_send(path, scope) for path, scope, _, _ in found} == set(
        ALLOWED_SENDS
    )


def test_no_command_runs_another_past_its_checks() -> None:
    # ctx.invoke runs a command's callback without its checks, the access
    # check included, and so can reinvoke.
    wrong = []
    for path, module in source_files(('tle',)):
        for scope, call in calls_in(module):
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            on_context = isinstance(func.value, ast.Name) and func.value.id in (
                'ctx',
                'context',
            )
            if func.attr == 'reinvoke' or (func.attr == 'invoke' and on_context):
                wrong.append(f'{path}:{call.lineno} in {scope}')

    assert wrong == []
