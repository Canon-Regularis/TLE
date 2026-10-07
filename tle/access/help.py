"""/help: the commands each member can use, and how to use each one.

The ``Help`` cog's /help, also ``;help``, lists the commands that the member
asking can use in the channel they ask in, one page per category, and shows
how to use any one of them. A group's help also lists its subcommands that
work only in other channels, marked with where. The access service decides
what each member can use, so /help never shows a member a command that isn't
for them.
``send_help`` does the work, for /help and for ``TLEContext.send_help``,
through which a group's own command shows the group's help.

The help of a slash command is always private. That of a prefix command is
seen by the whole channel, so it shows only commands for everyone, and points
to /help for the others.
"""

import logging
import re
import types
import typing
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Union

import discord
from discord import app_commands
from discord.ext import commands

from tle.access import table
from tle.access.policy import describe_limit, describe_where, describe_who
from tle.access.rules import (
    FAIL_CLOSED,
    STAFF_LEVELS,
    Decision,
    Effective,
    Outcome,
    Where,
    Who,
    satisfies,
)
from tle.access.service import OFF_TEXT, AccessService
from tle.util import discord_common
from tle.util.paginator import Page, paginate

log = logging.getLogger(__name__)

# The replies besides the help itself. They never say whether a command the
# member may not use exists.
NO_COMMAND_TEXT = 'No command called `{name}` that you can use here.'
USE_SLASH_HELP_TEXT = 'Use `/help {name}` for this command.'
NO_COMMANDS_TEXT = 'There are no commands you can use here.'
UNAVAILABLE_TEXT = "Help isn't available right now."
# The footers of the overview's pages, which can't hold code spans.
SLASH_FOOTER = (
    'Commands you can use in this channel. Use /help with a command to learn '
    'more about it.'
)
MORE_IN_BOT_CHANNELS = 'More commands work in the bot channels.'
PREFIX_FOOTER = (
    'Commands everyone can use in this channel. Use ;help with a command to learn '
    'more about it, or /help to see all of yours privately.'
)
# Where a command works whose slash answers are always private: by a limit,
# or as /help's are. A command under a private limit works only as a slash
# command, which the member's slash list may not show, or it may have none.
PRIVATE_RULE_TEXT = 'Its answers are private, so it works only as a slash command.'
NO_SLASH_PRIVATE_TEXT = (
    "Its answers must be private, and it has no slash command, so it can't be "
    'used in this server.'
)
SLASH_ONLY_TEXT = (
    'Only the slash command works, {where}, and it always answers only you.'
)
PRIVATE_ON_SLASH_TEXT = 'The slash command works {where}, and always answers only you.'
ALWAYS_PRIVATE_ON_SLASH_TEXT = 'The slash command always answers only you.'
# Where a group's subcommand works that doesn't work here, after its line.
ONLY_IN_BOT_CHANNELS = 'bot channels only'
ONLY_IN_STAFF_CHANNEL = 'staff channel only'

# How long the buttons of the help's pages work, in seconds.
PAGE_TIMEOUT = 5 * 60.0
# The most suggestions Discord shows.
MAX_SUGGESTIONS = 25

_EVERYONE = frozenset({Who.EVERYONE})
_COLOUR = 0x198BCC
# Discord's limits on an embed's texts, and the room kept for a page's footer.
_DESCRIPTION_LIMIT = 4096
_FIELD_LIMIT = 1024
_FIELDS_PER_EMBED = 25
_EMBED_LIMIT = 6000
_FOOTER_ROOM = 300
# The most lines of commands on one page of the overview, and the longest name
# that a reply repeats.
_LINES_PER_PAGE = 20
_SHOWN_NAME = 60
# The longest summary of a command that a list shows, and Discord's limit on a
# suggestion.
_BRIEF_LIMIT = 100
_CHOICE_LIMIT = 100
_UNDESCRIBED = ('', '…', '...')

# What one form of a command does here, said of one form and of two.
_PUBLIC = ('answers everyone in this channel', 'answer everyone in this channel')
_PRIVATE = ('answers only you', 'answer only you')
_BY_DIRECT_MESSAGE = ('answers you by direct message', 'answer you by direct message')
_REFUSED = ("doesn't work here", "don't work here")

_BUCKETS: dict[commands.BucketType, str] = {
    commands.BucketType.default: ', for everyone together',
    commands.BucketType.user: ' for each member',
    commands.BucketType.member: ' for each member',
    commands.BucketType.guild: ' in this server',
    commands.BucketType.channel: ' in each channel',
    commands.BucketType.category: ' in each category of channels',
    commands.BucketType.role: ' for each role',
}


@dataclass(frozen=True)
class Usable:
    """How the rules let a member use ``command`` in one channel."""

    command: commands.Command[Any, ..., Any]
    slash_path: str | None  # its slash form, if the member's slash list shows one
    slash: Decision | None  # the decision on that slash form; None without one
    prefix: Decision  # the decision on its prefix form

    @property
    def name(self) -> str:
        return self.command.qualified_name

    @property
    def rule(self) -> Effective:
        """The command's rule in this server."""
        return self.prefix.rule

    @property
    def for_member(self) -> bool:
        """Whether the member may use the command at all, in some channel."""
        return self.prefix.outcome is not Outcome.NOT_ALLOWED

    @property
    def for_everyone(self) -> bool:
        """Whether every member may use the command."""
        return self.rule.who == _EVERYONE

    @property
    def slash_works(self) -> bool:
        return self.slash is not None and self.slash.allowed

    @property
    def here(self) -> bool:
        """Whether the member can use the command in this channel, in some form."""
        return self.slash_works or self.prefix.allowed

    @property
    def form(self) -> str:
        """How to use the command here: its slash form if that works, otherwise
        its prefix form.
        """
        if self.slash_works and self.slash_path is not None:
            return self.slash_path
        return f';{self.name}'


async def usable(
    access: AccessService,
    command: commands.Command[Any, ..., Any],
    member: discord.Member,
    channel: object,
) -> Usable:
    """How ``member`` can use ``command`` in ``channel``."""
    path = access.listed_slash_path(command, member)
    slash = None
    if path is not None:
        slash = await access.decide(command, member, channel, slash=True)
    prefix = await access.decide(command, member, channel, slash=False)
    return Usable(command, path, slash, prefix)


async def usable_here(
    access: AccessService,
    bot: commands.Bot,
    member: discord.Member,
    channel: object,
    *,
    everyone_only: bool = False,
) -> list[Usable]:
    """The commands that ``member`` can use in ``channel``, in order.

    With ``everyone_only``, only those that every member may use. Twins are
    left out for the commands they are twins of.
    """
    found = []
    for command in bot.walk_commands():
        if not _listed(command, bot):
            continue
        each = await usable(access, command, member, channel)
        if each.here and (each.for_everyone or not everyone_only):
            found.append(each)
    found.sort(key=_order)
    return found


def _listed(command: commands.Command[Any, ..., Any], bot: commands.Bot) -> bool:
    """Whether lists of commands show ``command``: not if it is hidden or
    disabled, nor if it is the twin of a command they show instead.
    """
    if command.hidden or not command.enabled:
        return False
    name = command.qualified_name
    canonical = table.canonical(name)
    return canonical == name or _prefix_command(bot, canonical.split()) is None


def _order(each: Usable) -> tuple[str, str]:
    """Commands in alphabetical order, _nogud next to nogud."""
    return each.name.lstrip('_'), each.name


def find_command(
    bot: commands.Bot, name: str
) -> commands.Command[Any, ..., Any] | None:
    """The command called ``name``, or None.

    ``name`` is a command's qualified name, as ;clist future, or the path of
    its slash form, as /clist show for the group's own command, with or without
    its / or ; and in any case.
    """
    words = name.strip().lstrip('/;').split()
    if not words:
        return None
    for attempt in (words, [word.lower() for word in words]):
        found = _prefix_command(bot, attempt) or _slash_command(bot, attempt)
        if found is not None:
            return found
    return None


def _prefix_command(
    bot: commands.Bot, words: Sequence[str]
) -> commands.Command[Any, ..., Any] | None:
    """The prefix command whose qualified name is ``words``, exactly."""
    found: commands.Command[Any, ..., Any] | None = None
    container: commands.GroupMixin[Any] = bot
    for index, word in enumerate(words):
        found = container.all_commands.get(word)
        if found is None:
            return None
        if index < len(words) - 1:
            if not isinstance(found, commands.GroupMixin):
                return None
            container = found
    return found


def _tree_command(
    bot: commands.Bot, words: Sequence[str]
) -> app_commands.Command[Any, ..., Any] | None:
    """The slash command in the bot's tree whose path is ``words``."""
    if not words:
        return None
    found = bot.tree.get_command(words[0])
    for word in words[1:]:
        if not isinstance(found, app_commands.Group):
            return None
        found = found.get_command(word)
    return found if isinstance(found, app_commands.Command) else None


def _slash_command(
    bot: commands.Bot, words: Sequence[str]
) -> commands.Command[Any, ..., Any] | None:
    """The prefix command that the slash command at ``words`` runs: a group's
    own command for its fallback, such as /clist show.
    """
    wrapped = getattr(_tree_command(bot, words), 'wrapped', None)
    return wrapped if isinstance(wrapped, commands.Command) else None


def split_help(text: str | None) -> tuple[str, tuple[str, ...]]:
    """A command's help text as its description and its examples.

    The examples are the lines after a line ``Examples:``, which ends the
    commands' docstrings. The description is the text before it, with the
    lines of each paragraph joined, except those of a list, which stay apart,
    and those of a table, which go in a code block to keep their columns.
    """
    if not text:
        return '', ()
    lines = text.strip().splitlines()
    for index, line in enumerate(lines):
        if line.strip() == 'Examples:':
            examples = tuple(
                example.strip() for example in lines[index + 1 :] if example.strip()
            )
            return _unwrap(lines[:index]), examples
    return _unwrap(lines), ()


# A line of a table, whose cells are set apart by ' | '.
_TABLE_ROW = re.compile(r'\s\|\s')


def _unwrap(lines: Sequence[str]) -> str:
    """``lines`` as Discord shows them best: see ``split_help``."""
    paragraphs: list[str] = []
    for paragraph in re.split(r'\n\s*\n', '\n'.join(lines).strip()):
        parts: list[str] = []
        prose: list[str] = []
        table: list[str] = []
        for line in paragraph.splitlines():
            if _TABLE_ROW.search(line):
                _add_prose(parts, prose)
                table.append(line.rstrip())
                continue
            _add_table(parts, table)
            stripped = line.strip()
            if line != line.lstrip() or stripped.startswith(('-', '*', '•')):
                # A list's item, or a line set in from the rest.
                _add_prose(parts, prose)
                parts.append(line.rstrip())
            else:
                prose.append(stripped)
        _add_prose(parts, prose)
        _add_table(parts, table)
        paragraphs.append('\n'.join(parts))
    return '\n\n'.join(paragraphs)


def _add_prose(parts: list[str], prose: list[str]) -> None:
    """Add the lines in ``prose`` to ``parts`` as one line, and clear them."""
    if prose:
        parts.append(' '.join(prose))
        prose.clear()


def _add_table(parts: list[str], table: list[str]) -> None:
    """Add the rows in ``table`` to ``parts`` as a code block, and clear them."""
    if table:
        rows = '\n'.join(table)
        parts.append(f'```\n{rows}\n```')
        table.clear()


def describe_cooldown(command: commands.Command[Any, ..., Any]) -> str | None:
    """How often ``command`` can be used, such as 'Once every 10 seconds for
    each member', or None if as often as anyone likes.
    """
    cooldown = command.cooldown
    if cooldown is None:
        return None
    rate = cooldown.rate
    times = 'Once' if rate == 1 else 'Twice' if rate == 2 else f'{rate} times'
    # discord.py keeps the bucket type with the cooldown, out of sight.
    bucket = getattr(getattr(command, '_buckets', None), 'type', None)
    scope = _BUCKETS.get(bucket, '') if isinstance(bucket, commands.BucketType) else ''
    return f'{times} every {_period(cooldown.per)}{scope}'


def _period(seconds: float) -> str:
    if seconds == 1:
        return 'second'
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return 'minute' if minutes == 1 else f'{minutes} minutes'
    return f'{seconds:g} seconds'


async def send_help(ctx: commands.Context[Any], entity: object = None) -> None:
    """Send the help of ``entity``, a command or a command's name; without one,
    the commands that ``ctx``'s author can use in its channel.

    The help of a slash command is private. That of a prefix command is seen
    by the channel, so it shows only commands for everyone; for any other, it
    says to use /help. A command that the author may not use gets the same
    reply as an unknown one.
    """
    access = getattr(ctx.bot, 'access', None)
    if not isinstance(access, AccessService):
        log.warning('There is no access service, so /help can show nothing')
        await _tell(ctx, UNAVAILABLE_TEXT)
        return
    member = ctx.author
    asked = _asked_for(entity)
    if ctx.guild is None or not isinstance(member, discord.Member):
        # The access check admits members of a server alone.
        await _tell(ctx, NO_COMMANDS_TEXT if asked is None else _no_command(asked))
        return
    public = ctx.interaction is None
    if asked is None:
        pages = await _overview(access, ctx, member, public=public)
        if not pages:
            await _tell(ctx, NO_COMMANDS_TEXT)
            return
    else:
        if isinstance(entity, commands.Command):
            command: commands.Command[Any, ..., Any] | None = entity
        else:
            command = find_command(ctx.bot, asked)
        each = None
        if command is not None and command.enabled:
            each = await usable(access, command, member, ctx.channel)
        if each is None or not each.for_member:
            await _tell(ctx, _no_command(asked))
            return
        if public and not each.for_everyone:
            await _tell(ctx, USE_SLASH_HELP_TEXT.format(name=each.name))
            return
        pages = await _detail(access, ctx, member, each, public=public)
    # ephemeral makes the help private on slash; prefix replies ignore it.
    await paginate(ctx.channel, pages, wait_time=PAGE_TIMEOUT, ctx=ctx, ephemeral=True)


def _asked_for(entity: object) -> str | None:
    """The name of what help was asked for; None for the overview."""
    if entity is None:
        return None
    if isinstance(entity, commands.Command):
        return entity.qualified_name
    name = ' '.join(str(entity).split()).lstrip('/;').strip()
    return name or None


def _no_command(name: str) -> str:
    shown = name.replace('`', "'")
    if len(shown) > _SHOWN_NAME:
        shown = f'{shown[: _SHOWN_NAME - 1]}…'
    return NO_COMMAND_TEXT.format(name=shown)


async def _tell(ctx: commands.Context[Any], text: str) -> None:
    await ctx.send(embed=discord_common.embed_alert(text), ephemeral=True)


# The overview


async def _overview(
    access: AccessService,
    ctx: commands.Context[Any],
    member: discord.Member,
    *,
    public: bool,
) -> list[Page]:
    """The overview's pages: for each category, the commands that ``member``
    can use here; only those for everyone if ``public``.
    """
    found = await usable_here(
        access, ctx.bot, member, ctx.channel, everyone_only=public
    )
    by_category: dict[str, list[Usable]] = {}
    for each in found:
        category = table.category_of(each.name, each.command.cog_name)
        by_category.setdefault(category.key, []).append(each)
    note = ''
    if access.guild_access(member.guild.id).broken:
        note = f'{access.broken_text(member.guild.id)}\n\n'
    embeds = []
    for category in table.CATEGORIES:
        entries = by_category.get(category.key)
        if not entries:
            continue
        lines = [_line(each) for each in entries]
        for chunk in _chunks(lines, _DESCRIPTION_LIMIT - 500, _LINES_PER_PAGE):
            description = f'{note}{category.description}.\n\n{chunk}'
            embeds.append(_embed(category.title, description))
    if public:
        footer = PREFIX_FOOTER
    elif _outside_bot_channels(access, member, ctx.channel):
        footer = f'{SLASH_FOOTER} {MORE_IN_BOT_CHANNELS}'
    else:
        footer = SLASH_FOOTER
    return _numbered(embeds, footer)


def _outside_bot_channels(
    access: AccessService, member: discord.Member, channel: object
) -> bool:
    """Whether the server has bot channels, but ``channel`` is none of them."""
    spot = access.spot(channel, member.guild.id, slash=True)
    if not spot.bot_channels:
        return False
    place = spot.channel_id
    return place is None or (
        place not in spot.bot_channels and place != spot.staff_channel
    )


def _line(each: Usable) -> str:
    """A command as lists show it: how to use it here, and what it does."""
    brief = _brief(each.command)
    return f'{_code(each.form)}: {brief}' if brief else _code(each.form)


def _brief(command: commands.Command[Any, ..., Any]) -> str:
    """What ``command`` does, in a line: its brief, or else the first
    paragraph of its description.
    """
    brief = (command.brief or '').strip()
    if not brief:
        description, _ = split_help(command.help)
        brief = description.split('\n', 1)[0].strip()
    return _fit(brief, _BRIEF_LIMIT)


# The help of one command


async def _detail(
    access: AccessService,
    ctx: commands.Context[Any],
    member: discord.Member,
    each: Usable,
    *,
    public: bool,
) -> list[Page]:
    """The pages of ``each``'s help: what it does, how to use it, who may use
    it and where, and for admins, privately, its default rule and this
    server's limits on it.
    """
    command = each.command
    description, examples = split_help(command.help)
    if not description:
        description = (command.brief or '').strip()
    has_slash = each.slash_path is not None
    private = table.private_on_slash(each.name)
    where = _where(
        each.rule,
        has_slash=has_slash,
        slash_exists=access.slash_path(command) is not None,
        private_on_slash=private,
    )
    here = _here(
        each,
        private_on_slash=private,
        by_direct_message=table.by_direct_message(each.name),
        broken=access.broken_text(member.guild.id),
    )
    fields: list[tuple[str, list[str]]] = [('Usage', _usage(ctx.bot, each))]
    options = _options(ctx.bot, each)
    if options:
        fields.append(('Options', options))
    if examples:
        fields.append(('Examples', [_code(example) for example in examples]))
    fields += [
        ('Who', [f'{describe_who(each.rule.who)}.']),
        ('Where', [where]),
        ('In this channel', [here]),
    ]
    cooldown = describe_cooldown(command)
    if cooldown is not None:
        fields.append(('Cooldown', [f'{cooldown}.']))
    if isinstance(command, commands.Group):
        children = await _children(
            access, ctx.bot, command, member, ctx.channel, public=public
        )
        if children:
            fields.append(('Commands', children))
    if not public and satisfies(access.asker(member), Who.ADMIN):
        default = _describe_rule(each.name, slash=has_slash)
        fields.append(('Default rule', [default]))
        limits = _limits(access, member.guild.id, each.name)
        fields.append(("This server's limits", limits))
    return _numbered(_pages_of(each.name, description, fields), None)


def _usage(bot: commands.Bot, each: Usable) -> list[str]:
    """How to type the command: its slash form, if the member has one, and its
    prefix form, unless its answers are private, which refuses that form.
    """
    lines = []
    app = _listed_app(bot, each)
    if app is not None:
        options = ''.join(
            f' <{parameter.display_name}>'
            if parameter.required
            else f' [{parameter.display_name}]'
            for parameter in app.parameters
        )
        lines.append(_code(f'{each.slash_path}{options}'))
    if not (each.rule.private and lines):
        lines.append(_code(prefix_usage(each.command)))
    return lines


def prefix_usage(command: commands.Command[Any, ..., Any]) -> str:
    """How to type ``command`` as a prefix command, from its signature.

    discord.py shows a flags parameter, which takes the rest of the message,
    as ``<flags>``; here it is spelt out flag by flag, as in
    ``;access limit <command> [off: yes|no]``.
    """
    signature = command.signature
    flags = _flags_parameter(command)
    if flags is not None:
        parameter, converter = flags
        shown = _signature_token(parameter)
        if signature.endswith(shown):
            signature = signature[: -len(shown)] + _flags_usage(converter)
    return f';{command.qualified_name} {signature}'.rstrip()


def _flags_parameter(
    command: commands.Command[Any, ..., Any],
) -> tuple[commands.Parameter, type[commands.FlagConverter]] | None:
    """``command``'s flags parameter and its converter, if it has one.

    A flags parameter takes the rest of the message, so it is the last one.
    A command with ``usage`` has its signature written by hand.
    """
    if command.usage is not None or not command.clean_params:
        return None
    parameter = list(command.clean_params.values())[-1]
    converter = _without_none(parameter.converter)
    if isinstance(converter, type) and issubclass(converter, commands.FlagConverter):
        return parameter, converter
    return None


def _without_none(annotation: Any) -> Any:
    """``annotation`` without its None, if it is optional, such as
    ``bool | None``.
    """
    if typing.get_origin(annotation) in (Union, types.UnionType):
        kinds = [kind for kind in typing.get_args(annotation) if kind is not type(None)]
        if len(kinds) == 1:
            return kinds[0]
    return annotation


def _signature_token(parameter: commands.Parameter) -> str:
    """How ``Command.signature`` shows ``parameter``, a flags parameter."""
    name = parameter.displayed_name or parameter.name
    if parameter.required:
        return f'<{name}>'
    if parameter.displayed_default:
        return f'[{name}={parameter.displayed_default}]'
    return f'[{name}]'


def _flags_usage(converter: type[commands.FlagConverter]) -> str:
    """The flags of ``converter`` as they are typed: the positional one first,
    then each other one with its name, such as ``[off: yes|no]``.
    """
    prefix = converter.__commands_flag_prefix__
    delimiter = converter.__commands_flag_delimiter__
    # As discord.py's documentation writes them: 'off: yes', but '--off yes'.
    space = ' ' if delimiter == ':' else ''
    first: list[str] = []
    rest: list[str] = []
    for flag in converter.get_flags().values():
        if flag.positional:
            first.append(f'<{flag.name}>' if flag.required else f'[{flag.name}]')
            continue
        typed = f'{prefix}{flag.name}{delimiter}{space}{_flag_value(flag)}'
        rest.append(f'<{typed}>' if flag.required else f'[{typed}]')
    return ' '.join(first + rest)


def _flag_value(flag: commands.Flag) -> str:
    """What a flag takes: its choices, yes or no, or anything."""
    annotation = _without_none(flag.annotation)
    if typing.get_origin(annotation) is Literal:
        return '|'.join(str(choice) for choice in typing.get_args(annotation))
    if annotation is bool:
        return 'yes|no'
    return '…'


def _options(bot: commands.Bot, each: Usable) -> list[str]:
    """The command's options that have a description, each with it.

    They are the slash form's, also when the member's slash list doesn't
    show it: its options are the prefix command's parameters.
    """
    app = _listed_app(bot, each) or _app_form(each.command)
    if app is None:
        return []
    return [
        f'{_code(parameter.display_name)}: {parameter.description}'
        for parameter in app.parameters
        if parameter.description.strip() not in _UNDESCRIBED
    ]


def _listed_app(
    bot: commands.Bot, each: Usable
) -> app_commands.Command[Any, ..., Any] | None:
    """The slash command that the member's slash list shows for ``each``."""
    if each.slash_path is None:
        return None
    return _tree_command(bot, each.slash_path.lstrip('/').split())


def _app_form(
    command: commands.Command[Any, ..., Any],
) -> app_commands.Command[Any, ..., Any] | None:
    """The slash command made for ``command``, in the tree or not: its own,
    or for a group's own command the group's fallback.
    """
    if isinstance(command, commands.HybridGroup):
        group = command.app_command
        if group is None or command.fallback is None:
            return None
        fallback = group.get_command(command.fallback)
        return fallback if isinstance(fallback, app_commands.Command) else None
    if isinstance(command, commands.HybridCommand):
        return command.app_command
    return None


def _where(
    rule: Effective, *, has_slash: bool, slash_exists: bool, private_on_slash: bool
) -> str:
    """Where the command works, and when only the member sees its answers.

    ``has_slash`` says that the member's slash list shows the command,
    ``slash_exists`` that it has a slash command at all, and
    ``private_on_slash`` that its slash command always answers privately, as
    /help's does.
    """
    # A slash command whose answer is private works outside the command's
    # place too, unless the place refuses that.
    slash_place = rule.where if rule.where.refuses_outside else Where.ANYWHERE
    if rule.private:
        if not has_slash:
            # Private answers refuse its prefix form, and without a slash
            # command nothing is left.
            text = PRIVATE_RULE_TEXT if slash_exists else NO_SLASH_PRIVATE_TEXT
            return f'{describe_where(rule.where, slash=False)}. {text}'
        return SLASH_ONLY_TEXT.format(where=_in_place(slash_place))
    if not (has_slash and private_on_slash):
        return f'{describe_where(rule.where, slash=has_slash)}.'
    if rule.where is Where.ANYWHERE:
        slash = ALWAYS_PRIVATE_ON_SLASH_TEXT
    else:
        slash = PRIVATE_ON_SLASH_TEXT.format(where=_in_place(slash_place))
    return f'{describe_where(rule.where, slash=False)}. {slash}'


def _in_place(where: Where) -> str:
    """``where`` as the end of a sentence, such as 'in bot channels only'."""
    return f'in {_lower_first(describe_where(where, slash=False))}'


def _here(
    each: Usable, *, private_on_slash: bool, by_direct_message: bool, broken: str
) -> str:
    """What each form of the command does in this channel.

    ``private_on_slash`` says that its slash command always answers privately,
    ``by_direct_message`` that it answers by direct message, and ``broken``
    is what to say if the server's settings couldn't be read.
    """
    outcomes = {each.prefix.outcome}
    if each.slash is not None:
        outcomes.add(each.slash.outcome)
    if Outcome.BROKEN in outcomes:
        return broken
    if Outcome.OFF in outcomes:
        return OFF_TEXT
    effects: list[tuple[str, tuple[str, str]]] = []
    if each.slash_path is not None and each.slash is not None:
        effect = _slash_effect(each.slash, private_on_slash=private_on_slash)
        effects.append((each.slash_path, effect))
    # Private answers refuse the prefix form everywhere, which the usage
    # leaves out then.
    if not (each.rule.private and effects):
        if not each.prefix.allowed:
            effect = _REFUSED
        elif by_direct_message:
            effect = _BY_DIRECT_MESSAGE
        else:
            effect = _PUBLIC
        effects.append((f';{each.name}', effect))
    if len(effects) == 2 and effects[0][1] == effects[1][1]:
        (slash, effect), (prefix, _) = effects
        return f'{_code(slash)} and {_code(prefix)} {effect[1]}.'
    return '; '.join(f'{_code(form)} {effect[0]}' for form, effect in effects) + '.'


def _slash_effect(decision: Decision, *, private_on_slash: bool) -> tuple[str, str]:
    if not decision.allowed:
        return _REFUSED
    if decision.private or private_on_slash:
        return _PRIVATE
    return _PUBLIC


async def _children(
    access: AccessService,
    bot: commands.Bot,
    group: commands.Group[Any, ..., Any],
    member: discord.Member,
    channel: object,
    *,
    public: bool,
) -> list[str]:
    """The lines of ``group``'s subcommands that ``member`` can use here, and
    of those they can use only elsewhere, marked with where; only those for
    everyone if ``public``.

    The staff channel is named to staff alone, and never in public.
    """
    staff = not public and any(
        satisfies(access.asker(member), level) for level in STAFF_LEVELS
    )
    found: list[tuple[Usable, str | None]] = []
    for command in group.commands:
        if not _listed(command, bot):
            continue
        each = await usable(access, command, member, channel)
        if public and not each.for_everyone:
            continue
        if each.here:
            found.append((each, None))
            continue
        place = _only_elsewhere(each, staff=staff)
        if place is not None:
            found.append((each, place))
    found.sort(key=lambda entry: _order(entry[0]))
    return [
        _line(each) if place is None else _elsewhere_line(each, place)
        for each, place in found
    ]


def _only_elsewhere(each: Usable, *, staff: bool) -> str | None:
    """Where the member can use ``each``, which doesn't work here: in bot
    channels, or for ``staff`` in the staff channel. None if it works nowhere
    for them, as when it is switched off.
    """
    rule = each.rule
    if not each.for_member or rule.off or rule.broken:
        return None
    if rule.private and each.slash_path is None:
        # Its answers are private, which refuses the prefix form everywhere.
        return None
    if rule.where.scope == Where.BOT.scope:
        return ONLY_IN_BOT_CHANNELS
    if rule.where.scope == Where.STAFF.scope and staff:
        return ONLY_IN_STAFF_CHANNEL
    return None


def _elsewhere_line(each: Usable, place: str) -> str:
    """A command that works only elsewhere, as lists show it: the form that
    works there, what it does, and where.
    """
    form = each.slash_path or f';{each.name}'
    brief = _brief(each.command)
    line = f'{_code(form)}: {brief}' if brief else _code(form)
    return f'{line} ({place})'


def _describe_rule(name: str, *, slash: bool) -> str:
    """Command ``name``'s default rule, before any server's limits."""
    rule = table.rule_for(name) or FAIL_CLOSED
    who = describe_who(table.default_who(name))
    text = f'{who}. {describe_where(rule.where, slash=slash)}.'
    if rule.private:
        text += ' Its answers are private.'
    return text


def _limits(access: AccessService, guild_id: int, name: str) -> list[str]:
    """This server's limits on command ``name``, for its admins, in /access's
    words.
    """
    rule = table.rule_for(name) or FAIL_CLOSED
    if rule.who is Who.OWNER or table.is_protected(name):
        return ["Limits don't apply to this command."]
    settings = access.guild_access(guild_id)
    if settings.broken:
        return [access.broken_text(guild_id)]
    found = [
        f'{_code(key)}: {describe_limit(settings.limits[key])}.'
        for key in table.limit_keys(name)
        if key in settings.limits
    ]
    return found or ['None. Add one with `/access limit`.']


# Pages


def _embed(title: str, description: str | None) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=_COLOUR)


def _pages_of(
    title: str, description: str, fields: Iterable[tuple[str, list[str]]]
) -> list[discord.Embed]:
    """Embeds of ``fields``, each field's lines in one or more fields: as many
    embeds as Discord's limits need.
    """
    pages = [_embed(title, _fit(description, _DESCRIPTION_LIMIT) or None)]
    for name, lines in fields:
        for index, value in enumerate(_chunks(lines, _FIELD_LIMIT)):
            field_name = name if index == 0 else f'{name}, continued'
            page = pages[-1]
            room = _EMBED_LIMIT - _FOOTER_ROOM - len(page)
            if (
                len(page.fields) == _FIELDS_PER_EMBED
                or len(field_name) + len(value) > room
            ):
                page = _embed(f'{title}, continued', None)
                pages.append(page)
            page.add_field(name=field_name, value=value, inline=False)
    return pages


def _numbered(embeds: Sequence[discord.Embed], footer: str | None) -> list[Page]:
    """``embeds`` as pages, each footer ending with its page number if there
    are several.
    """
    pages: list[Page] = []
    for number, embed in enumerate(embeds, start=1):
        parts = [footer] if footer else []
        if len(embeds) > 1:
            parts.append(f'Page {number} / {len(embeds)}')
        if parts:
            embed.set_footer(text=' '.join(parts))
        pages.append((None, embed))
    return pages


def _chunks(lines: Iterable[str], limit: int, most: int | None = None) -> list[str]:
    """``lines`` joined in chunks of at most ``limit`` characters, and of at
    most ``most`` lines; a line too long alone is cut short.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        fitted = _fit(line, limit)
        full = most is not None and len(current) == most
        if current and (full or size + 1 + len(fitted) > limit):
            chunks.append('\n'.join(current))
            current, size = [], 0
        size += len(fitted) + (1 if current else 0)
        current.append(fitted)
    if current:
        chunks.append('\n'.join(current))
    return chunks


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else f'{text[: limit - 1]}…'


def _code(text: str) -> str:
    """``text`` in a code span."""
    if '`' in text:
        return f'`` {text} ``'
    return f'`{text}`'


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


# Suggestions


def suggestions(found: Iterable[Usable], typed: str) -> list[app_commands.Choice[str]]:
    """The commands in ``found`` whose names hold ``typed``, at most
    MAX_SUGGESTIONS: first those whose name starts with it, then those with a
    word that does, then the rest, each in order.

    A command's names are its qualified name and the form that works here,
    such as 'contests upcoming' for the group contests.
    """
    wanted = ' '.join(typed.strip().lstrip('/;').lower().split())
    # Where a word starts: the start, or after a space, _ or -.
    word = re.compile(rf'(?:^|[\s_-]){re.escape(wanted)}')
    ranked = []
    for each in found:
        names = (each.name.lower(), each.form.lstrip('/;').lower())
        if any(name.startswith(wanted) for name in names):
            rank = 0
        elif any(word.search(name) for name in names):
            rank = 1
        elif any(wanted in name for name in names):
            rank = 2
        else:
            continue
        ranked.append((rank, _order(each), each))
    ranked.sort(key=lambda entry: entry[:2])
    return [
        app_commands.Choice(name=_fit(_choice(each), _CHOICE_LIMIT), value=each.name)
        for _, _, each in ranked[:MAX_SUGGESTIONS]
    ]


def _choice(each: Usable) -> str:
    brief = _brief(each.command)
    return f'{each.form}: {brief}' if brief else each.form


class Help(commands.Cog):
    """/help, which shows each member the commands they can use."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def command_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggests the commands that the member can use where they are."""
        access = getattr(self.bot, 'access', None)
        member = interaction.user
        if not isinstance(access, AccessService) or not isinstance(
            member, discord.Member
        ):
            return []
        try:
            found = await usable_here(access, self.bot, member, interaction.channel)
        except Exception:
            log.exception('Could not suggest commands for /help')
            return []
        return suggestions(found, current)

    # mypy solves the types of discord.py's hybrid command decorators to Never,
    # so it rejects every callback; hence the type: ignore.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name='help', brief='Show the commands you can use, or how to use one'
    )
    @app_commands.describe(
        command='A command, such as gitgud or clist future; all you can use here '
        'if left out'
    )
    @app_commands.autocomplete(command=command_autocomplete)
    async def help_command(
        self, ctx: commands.Context[Any], *, command: str | None = None
    ) -> None:
        """Show the commands you can use here, or how to use one of them.

        Only you see the answer to /help. The answer to ;help is for the whole
        channel, so it shows only the commands for everyone.

        Examples:
            /help
            /help command:clist future
            ;help gitgud
        """
        await send_help(ctx, command)
