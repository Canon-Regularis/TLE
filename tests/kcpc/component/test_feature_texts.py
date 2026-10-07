"""What members read about KCPC's feature commands: in Discord's slash list,
and in /help, which shows each command's brief, help and options.

The features are workshops, contests, accounts, problems, the algorithm of
the month and /notify; the admin cog's texts have tests of their own. The bot
boots as ``booting.booted`` boots it, with every extension, so the commands
that the texts name, TLE's included, are there to be found. Each test checks
every feature command and lists all that fail, rather than stopping at the
first.
"""

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
from tle.kcpc.features.contests.settings import PLATFORMS

# The cogs of the features, by name.
FEATURE_COGS = frozenset(
    {
        'KcpcWorkshops',
        'KcpcContests',
        'KcpcAccounts',
        'KcpcProblems',
        'KcpcAlgo',
        'KcpcNotify',
    }
)
# The longest brief, which is also the slash command's description: the
# bot's style keeps it well within Discord's 100 characters. And Discord's
# limit on an option's description.
BRIEF_LIMIT = 80
OPTION_LIMIT = 100
# What Discord shows for an option that has no description.
UNDESCRIBED = ('', '…')
# Each group whose own command is the slash group's fallback, and the prefix
# twin of that fallback: the same command, so the same texts.
TWINS = {
    'contests': 'contests upcoming',
    'weekly': 'weekly current',
    'algo': 'algo current',
}
# A command that a text names: a / or a ; and then words, at the start of the
# text or after a space or an opening bracket.
NAMED = re.compile(r'(?:^|(?<=[\s(]))([/;])([a-z_][\w-]*(?: [a-z_][\w-]*)*)')

Command = commands.Command[Any, ..., Any]
SlashCommand = app_commands.Command[Any, ..., Any]


@pytest.fixture
async def bot(tmp_path: Path) -> AsyncIterator[TLEBot]:
    """The bot with every extension."""
    async with booted(tmp_path / 'db' / 'kcpc.db') as bot:
        yield bot


def feature_commands(bot: commands.Bot) -> list[Command]:
    """Every prefix command of the features, groups and twins included."""
    found = [
        command for command in bot.walk_commands() if command.cog_name in FEATURE_COGS
    ]
    assert len(found) == 44
    return found


def feature_slash_commands(bot: commands.Bot) -> list[SlashCommand]:
    """Every slash command of the features, in the tree that the sync sends."""
    found = []
    for app in bot.tree.walk_commands():
        wrapped = getattr(app, 'wrapped', None)
        if (
            isinstance(app, app_commands.Command)
            and isinstance(wrapped, commands.Command)
            and wrapped.cog_name in FEATURE_COGS
        ):
            found.append(app)
    assert len(found) == 35
    return found


def option_descriptions(bot: commands.Bot, app: SlashCommand) -> dict[str, str]:
    """The description of each option of ``app``, as the sync sends it."""
    payload = app.to_dict(bot.tree)
    return {option['name']: option['description'] for option in payload['options']}


def described(bot: commands.Bot, path: str) -> dict[str, str]:
    """The option descriptions of the slash command at ``path``."""
    (app,) = [app for app in feature_slash_commands(bot) if app.qualified_name == path]
    return option_descriptions(bot, app)


def prefix_command(bot: commands.Bot, words: list[str]) -> Command | None:
    """The prefix command whose qualified name is ``words``, exactly."""
    found: Command | None = None
    container: object = bot
    for word in words:
        if not isinstance(container, commands.GroupMixin):
            return None
        found = container.all_commands.get(word)
        if found is None:
            return None
        container = found
    return found


def in_slash_list(bot: commands.Bot, words: list[str]) -> bool:
    """Whether the slash list has a command or group at the path ``words``."""
    found: object = bot.tree.get_command(words[0])
    for word in words[1:]:
        if not isinstance(found, app_commands.Group):
            return False
        found = found.get_command(word)
    return found is not None


def names_a_command(bot: commands.Bot, kind: str, words: list[str]) -> bool:
    """Whether the command a text names exists: the longest run of its words
    that is a prefix command's name, which on slash must be in the slash
    list too.
    """
    for end in range(len(words), 0, -1):
        command = prefix_command(bot, words[:end])
        if command is not None:
            break
    else:
        return False
    return kind == ';' or in_slash_list(bot, command.qualified_name.split(' '))


def slash_form(command: Command) -> SlashCommand | None:
    """The slash command that runs ``command``: its own, or for a group's own
    command the group's fallback; None if it has neither.
    """
    app = getattr(command, 'app_command', None)
    if isinstance(app, app_commands.Group):
        fallback = getattr(command, 'fallback', None)
        app = None if fallback is None else app.get_command(fallback)
    return app if isinstance(app, app_commands.Command) else None


def texts_of(bot: commands.Bot, command: Command) -> Iterator[str]:
    """What /help and the slash list show of ``command``, but its examples."""
    yield command.brief or ''
    yield split_help(command.help)[0]
    app = slash_form(command)
    if app is not None:
        yield from option_descriptions(bot, app).values()


async def test_every_feature_command_says_what_it_does(bot: TLEBot) -> None:
    wrong = []
    for command in feature_commands(bot):
        brief = command.brief or ''
        description, _ = split_help(command.help)
        if not (
            brief
            and len(brief) <= BRIEF_LIMIT
            and brief[0].isupper()
            and not brief.endswith('.')
        ):
            wrong.append(f'{command.qualified_name}: the brief {brief!r}')
        if not description:
            wrong.append(f'{command.qualified_name}: no description')

    assert wrong == []


async def test_every_example_uses_its_own_command(bot: TLEBot) -> None:
    # As typed, so the slash command, a group's fallback for the group's own
    # command, or the prefix command.
    wrong = []
    examples = 0
    for command in feature_commands(bot):
        forms = [f';{command.qualified_name}']
        slash = bot.access.slash_path(command)
        if slash is not None:
            forms.append(slash)
        for line in split_help(command.help)[1]:
            examples += 1
            if not any(line == form or line.startswith(f'{form} ') for form in forms):
                wrong.append(f'{command.qualified_name}: {line}')

    assert wrong == []
    assert examples > 44


async def test_every_option_of_a_feature_command_is_described(bot: TLEBot) -> None:
    wrong = [
        f'{app.qualified_name} {name}: {description!r}'
        for app in feature_slash_commands(bot)
        for name, description in option_descriptions(bot, app).items()
        if description in UNDESCRIBED or len(description) > OPTION_LIMIT
    ]

    assert wrong == []


async def test_every_platform_option_says_what_it_is(bot: TLEBot) -> None:
    platforms = {}
    for app in feature_slash_commands(bot):
        descriptions = option_descriptions(bot, app)
        if 'platform' in descriptions:
            platforms[app.qualified_name] = descriptions['platform']

    assert sorted(platforms) == [
        'contests upcoming',
        'link verify',
        'randproblem',
        'rank',
        'unlink',
    ]
    for path, description in platforms.items():
        assert description.startswith('The platform '), path


async def test_the_platforms_option_needs_no_change_when_a_platform_is_added(
    bot: TLEBot,
) -> None:
    command = bot.get_command('kcpc contests platforms')
    assert command is not None
    description = described(bot, 'kcpc contests platforms')['platforms']

    # It names a few platforms as examples, not every one, which could
    # outgrow Discord's limit; /help lists every platform, a new one too.
    assert len(description) <= OPTION_LIMIT
    assert not all(platform in description for platform in PLATFORMS)
    listed, _ = split_help(command.help)
    assert [platform for platform in PLATFORMS if platform not in listed] == []


async def test_unlink_says_what_it_does_on_each_platform(bot: TLEBot) -> None:
    # It unlinks AtCoder accounts, and says who unlinks a Codeforces handle,
    # which TLE's commands use too.
    command = bot.get_command('unlink')
    assert command is not None and command.brief is not None
    app = bot.tree.get_command('unlink')
    assert isinstance(app, app_commands.Command)
    (platform,) = app.parameters

    assert 'AtCoder' in command.brief and 'Codeforces' in command.brief
    assert [choice.value for choice in platform.choices] == ['codeforces', 'atcoder']


async def test_twins_say_what_the_commands_they_are_twins_of_say(
    bot: TLEBot,
) -> None:
    for name, twin_name in TWINS.items():
        group, twin = bot.get_command(name), bot.get_command(twin_name)
        assert group is not None and twin is not None

        assert twin.brief == group.brief, twin_name
        assert split_help(twin.help)[0] == split_help(group.help)[0], twin_name


async def test_the_commands_that_feature_texts_name_exist(bot: TLEBot) -> None:
    # On slash, in the slash list too: ;handle remove, say, has no slash form,
    # as the slash list leaves out staff commands in members' groups.
    wrong = []
    named = 0
    for command in feature_commands(bot):
        for text in texts_of(bot, command):
            for match in NAMED.finditer(text):
                named += 1
                kind, words = match.group(1), match.group(2).split(' ')
                if not names_a_command(bot, kind, words):
                    wrong.append(f'{command.qualified_name}: {match.group(0)}')

    assert wrong == []
    assert named > 5
