"""Every command's default rule, and the categories /help sorts commands into.

Commands are named by their qualified names, as discord.py gives them: 'duel
register' is the register subcommand of the duel group. A group's own callback
is a command too, named like the group. A few commands are twins, two names
for the same thing; only the canonical name of each pair has a rule, and the
twin shares that rule and its limits.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from tle.access.rules import FAIL_CLOSED, Rule, Where, Who

RULES: Mapping[str, Rule] = MappingProxyType(
    {
        # CacheControl. The caches are shared by every server.
        'cache': Rule(Who.OWNER, Where.ANYWHERE),
        'cache contests': Rule(Who.OWNER, Where.ANYWHERE),
        'cache problems': Rule(Who.OWNER, Where.ANYWHERE),
        'cache ratingchanges': Rule(Who.OWNER, Where.ANYWHERE),
        'cache problemsets': Rule(Who.OWNER, Where.ANYWHERE),
        # Codeforces
        'upsolve': Rule(Who.EVERYONE, Where.BOT),
        'gimme': Rule(Who.EVERYONE, Where.BOT),
        'stalk': Rule(Who.EVERYONE, Where.BOT),
        'mashup': Rule(Who.EVERYONE, Where.BOT),
        'gitgud': Rule(Who.EVERYONE, Where.BOT),
        'gitlog': Rule(Who.EVERYONE, Where.BOT),
        'gotgud': Rule(Who.EVERYONE, Where.BOT),
        'nogud': Rule(Who.EVERYONE, Where.BOT),
        '_nogud': Rule(Who.MODERATOR, Where.BOT),
        'vc': Rule(Who.EVERYONE, Where.BOT),
        'fullsolve': Rule(Who.EVERYONE, Where.BOT),
        'teamrate': Rule(Who.EVERYONE, Where.BOT),
        # Contests. The "here" and channel setters act on the channel they are
        # used in, so they work in any channel.
        'clist': Rule(Who.EVERYONE, Where.BOT),
        'clist future': Rule(Who.EVERYONE, Where.BOT),
        'clist active': Rule(Who.EVERYONE, Where.BOT),
        'clist finished': Rule(Who.EVERYONE, Where.BOT),
        'remind': Rule(Who.EVERYONE, Where.BOT),
        'remind here': Rule(Who.ADMIN, Where.ANYWHERE),
        'remind clear': Rule(Who.ADMIN, Where.STAFF),
        'remind settings': Rule(Who.EVERYONE, Where.BOT),
        'remind on': Rule(Who.EVERYONE, Where.BOT),
        'remind off': Rule(Who.EVERYONE, Where.BOT),
        'ranklist': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'ratedvc': Rule(Who.EVERYONE, Where.BOT_ONLY),
        '_unregistervc': Rule(Who.MODERATOR, Where.BOT),
        'set_ratedvc_channel': Rule(Who.ADMIN, Where.ANYWHERE),
        'get_ratedvc_channel': Rule(Who.EVERYONE, Where.BOT),
        'vcratings': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'vcrating': Rule(Who.EVERYONE, Where.BOT),
        # Dueling. What involves or pings the other duelist stays in bot
        # channels.
        'duel': Rule(Who.EVERYONE, Where.BOT),
        'duel register': Rule(Who.MODERATOR, Where.BOT),
        'duel selfregister': Rule(Who.EVERYONE, Where.BOT),
        'duel challenge': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel decline': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel withdraw': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel accept': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel complete': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel draw': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel profile': Rule(Who.EVERYONE, Where.BOT),
        'duel vshistory': Rule(Who.EVERYONE, Where.BOT),
        'duel history': Rule(Who.EVERYONE, Where.BOT),
        'duel recent': Rule(Who.EVERYONE, Where.BOT),
        'duel ongoing': Rule(Who.EVERYONE, Where.BOT),
        'duel ranklist': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel invalidate': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'duel _invalidate': Rule(Who.MODERATOR, Where.BOT),
        'duel rating': Rule(Who.EVERYONE, Where.BOT),
        # Graphs
        'plot': Rule(Who.EVERYONE, Where.BOT),
        'plot rating': Rule(Who.EVERYONE, Where.BOT),
        'plot extreme': Rule(Who.EVERYONE, Where.BOT),
        'plot solved': Rule(Who.EVERYONE, Where.BOT),
        'plot hist': Rule(Who.EVERYONE, Where.BOT),
        'plot curve': Rule(Who.EVERYONE, Where.BOT),
        'plot scatter': Rule(Who.EVERYONE, Where.BOT),
        'plot distrib': Rule(Who.EVERYONE, Where.BOT),
        'plot cfdistrib': Rule(Who.EVERYONE, Where.BOT),
        'plot centile': Rule(Who.EVERYONE, Where.BOT),
        'plot howgud': Rule(Who.EVERYONE, Where.BOT),
        'plot country': Rule(Who.EVERYONE, Where.BOT),
        'plot visualrank': Rule(Who.EVERYONE, Where.BOT),
        'plot speed': Rule(Who.EVERYONE, Where.BOT),
        # Handles. The handle group's own callback is the twin of handle show.
        '_updatestatus': Rule(Who.ADMIN, Where.STAFF),
        'handle show': Rule(Who.EVERYONE, Where.BOT),
        'handle set': Rule(Who.MODERATOR, Where.BOT),
        'handle identify': Rule(Who.EVERYONE, Where.BOT),
        'handle get': Rule(Who.EVERYONE, Where.BOT),
        'handle rget': Rule(Who.EVERYONE, Where.BOT),
        'handle remove': Rule(Who.MODERATOR, Where.BOT),
        'handle unmagic': Rule(Who.EVERYONE, Where.BOT),
        'handle unmagic_all': Rule(Who.MODERATOR, Where.STAFF),
        'handle unmagic_debug': Rule(Who.MODERATOR, Where.STAFF),
        'gudgitters': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'handle list': Rule(Who.EVERYONE, Where.BOT_ONLY),
        'roleupdate': Rule(Who.MODERATOR, Where.STAFF),
        'roleupdate now': Rule(Who.MODERATOR, Where.STAFF),
        'roleupdate auto': Rule(Who.MODERATOR, Where.STAFF),
        'roleupdate publish': Rule(Who.MODERATOR, Where.ANYWHERE),
        'role': Rule(Who.EVERYONE, Where.BOT),
        'handle refer': Rule(Who.TRUSTED, Where.BOT_ONLY),
        'handle grandfather': Rule(Who.ADMIN, Where.STAFF),
        # Meta. Stopping the bot and listing its servers concern every server.
        'meta': Rule(Who.EVERYONE, Where.BOT),
        'meta kill': Rule(Who.OWNER, Where.ANYWHERE),
        'meta ping': Rule(Who.EVERYONE, Where.BOT),
        'meta git': Rule(Who.DEVELOPER, Where.STAFF),
        'meta uptime': Rule(Who.EVERYONE, Where.BOT),
        'meta guilds': Rule(Who.OWNER, Where.ANYWHERE),
        # Starboard
        'starboard': Rule(Who.ADMIN, Where.STAFF),
        'starboard add': Rule(Who.ADMIN, Where.STAFF),
        'starboard delete': Rule(Who.ADMIN, Where.STAFF),
        'starboard edit_threshold': Rule(Who.ADMIN, Where.STAFF),
        'starboard edit_color': Rule(Who.ADMIN, Where.STAFF),
        'starboard here': Rule(Who.ADMIN, Where.ANYWHERE),
        'starboard clear': Rule(Who.ADMIN, Where.STAFF),
        'starboard remove': Rule(Who.ADMIN, Where.STAFF),
        # KcpcAdmin. The status shows the bot's internals, so it stays in the
        # staff channel.
        'kcpc': Rule(Who.ADMIN, Where.STAFF),
        'kcpc status': Rule(Who.DEVELOPER, Where.STAFF_ONLY),
        'kcpc channel': Rule(Who.ADMIN, Where.STAFF),
        'kcpc role': Rule(Who.ADMIN, Where.STAFF),
        'kcpc enable': Rule(Who.ADMIN, Where.STAFF),
        'kcpc disable': Rule(Who.ADMIN, Where.STAFF),
        # KcpcWorkshops
        'event': Rule(Who.EVERYONE, Where.BOT),
        'event this-week': Rule(Who.EVERYONE, Where.BOT),
        'kcpc workshops': Rule(Who.ADMIN, Where.STAFF),
        'kcpc workshops calendar': Rule(Who.ADMIN, Where.STAFF),
        'kcpc workshops sync': Rule(Who.ADMIN, Where.STAFF),
        # KcpcContests. Club contests are shared by every server, so only the
        # bot owner changes them, as an admin of the server (OWNER_AND_ADMIN).
        'contests': Rule(Who.EVERYONE, Where.BOT),
        'contests live': Rule(Who.EVERYONE, Where.BOT),
        'kcpc contests': Rule(Who.ADMIN, Where.STAFF),
        'kcpc contests add': Rule(Who.OWNER, Where.ANYWHERE),
        'kcpc contests settime': Rule(Who.OWNER, Where.ANYWHERE),
        'kcpc contests remove': Rule(Who.OWNER, Where.ANYWHERE),
        'kcpc contests platforms': Rule(Who.ADMIN, Where.STAFF),
        'kcpc contests start-posts': Rule(Who.ADMIN, Where.STAFF),
        'kcpc contests results': Rule(Who.ADMIN, Where.STAFF),
        'kcpc contests sync': Rule(Who.OWNER, Where.ANYWHERE),
        # KcpcAccounts
        'link': Rule(Who.EVERYONE, Where.BOT),
        'link codeforces': Rule(Who.EVERYONE, Where.BOT),
        'link atcoder': Rule(Who.EVERYONE, Where.BOT),
        'link verify': Rule(Who.EVERYONE, Where.BOT),
        'unlink': Rule(Who.EVERYONE, Where.BOT),
        'kcpc accounts': Rule(Who.ADMIN, Where.STAFF),
        'kcpc accounts unlink': Rule(Who.ADMIN, Where.STAFF),
        'profile': Rule(Who.EVERYONE, Where.BOT),
        'rank': Rule(Who.EVERYONE, Where.BOT_ONLY),
        # KcpcNotify
        'notify': Rule(Who.EVERYONE, Where.BOT),
        # KcpcProblems
        'randproblem': Rule(Who.EVERYONE, Where.BOT),
        'weekly': Rule(Who.EVERYONE, Where.BOT),
        'weekly history': Rule(Who.EVERYONE, Where.BOT),
        'kcpc weekly': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly queue': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly unqueue': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly solution': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly rotation': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly preview': Rule(Who.ADMIN, Where.STAFF),
        'kcpc weekly post-now': Rule(Who.ADMIN, Where.STAFF),
        # KcpcAlgo
        'algo': Rule(Who.EVERYONE, Where.BOT),
        'algo history': Rule(Who.EVERYONE, Where.BOT),
        'kcpc algo': Rule(Who.ADMIN, Where.STAFF),
        'kcpc algo reroll': Rule(Who.ADMIN, Where.STAFF),
        'kcpc algo post-now': Rule(Who.ADMIN, Where.STAFF),
        'kcpc algo preview': Rule(Who.ADMIN, Where.STAFF),
        # Help. Its slash answers are always private, whatever the channel.
        'help': Rule(Who.EVERYONE, Where.BOT),
        # Access. Until a server has a staff channel, these work anywhere (see
        # tle.access.policy), so that an admin can set one.
        'access': Rule(Who.ADMIN, Where.STAFF),
        'access bot-channels': Rule(Who.ADMIN, Where.STAFF),
        'access bot-channels add': Rule(Who.ADMIN, Where.STAFF),
        'access bot-channels remove': Rule(Who.ADMIN, Where.STAFF),
        'access staff-channel': Rule(Who.ADMIN, Where.STAFF),
        'access limit': Rule(Who.ADMIN, Where.STAFF),
        'access reset': Rule(Who.ADMIN, Where.STAFF),
    }
)

# Each twin and the canonical name it shares a rule with. A group whose slash
# fallback runs the group's own callback, such as /contests upcoming, also has
# a prefix subcommand of the fallback's name, since prefix groups have no
# fallback. The handle group's callback shows handles, as handle show does.
TWINS: Mapping[str, str] = MappingProxyType(
    {
        'contests upcoming': 'contests',
        'weekly current': 'weekly',
        'algo current': 'algo',
        'handle': 'handle show',
    }
)

# Commands under these never take limits: admins need /access to undo limits,
# and /help to find their way.
PROTECTED_ROOTS = frozenset({'help', 'access'})

# The commands whose slash commands always answer privately, whatever the
# channel and the rules say, each with every command in it if it is a group:
# /help, the server setup under /access and /kcpc, and what members do with
# their own accounts and pings.
PRIVATE_ON_SLASH = frozenset(
    {'help', 'access', 'kcpc', 'link', 'unlink', 'notify', 'handle identify'}
)
# The commands that answer by direct message, never in the channel.
BY_DIRECT_MESSAGE = frozenset({'meta guilds'})
# The bot owner's commands that the owner may use only as an admin of the
# server they use them in: the club contest commands keep KCPC's own admin
# check, which KCPC needs where the bot has no access rules.
OWNER_AND_ADMIN = frozenset(
    {
        'kcpc contests add',
        'kcpc contests settime',
        'kcpc contests remove',
        'kcpc contests sync',
    }
)


def canonical(name: str) -> str:
    """The canonical name of command ``name``: its twin's, if it is a twin."""
    name = ' '.join(name.split())
    return TWINS.get(name, name)


def is_protected(name: str) -> bool:
    """Whether command ``name`` is under /help or /access, which take no limits."""
    return canonical(name).split(' ')[0] in PROTECTED_ROOTS


def private_on_slash(name: str) -> bool:
    """Whether command ``name``'s slash command always answers privately: it,
    or a group it is in, is in PRIVATE_ON_SLASH.
    """
    words = canonical(name).split()
    return any(
        ' '.join(words[:end]) in PRIVATE_ON_SLASH for end in range(1, len(words) + 1)
    )


def by_direct_message(name: str) -> bool:
    """Whether command ``name`` answers by direct message, never in the channel."""
    return canonical(name) in BY_DIRECT_MESSAGE


def rule_for(name: str) -> Rule | None:
    """Command ``name``'s default rule, or None if the table lacks it."""
    return RULES.get(canonical(name))


def default_who(name: str) -> frozenset[Who]:
    """Every level that command ``name``'s default rule requires: its rule's,
    and admin too for those in OWNER_AND_ADMIN. A command missing from the
    table gets FAIL_CLOSED's.
    """
    rule = rule_for(name) or FAIL_CLOSED
    if canonical(name) in OWNER_AND_ADMIN:
        return frozenset({rule.who, Who.ADMIN})
    return frozenset({rule.who})


def limit_keys(name: str) -> tuple[str, ...]:
    """The keys of every limit that applies to command ``name``.

    For a canonical name n whose groups are a1 (the nearest) to ak (the
    top-level one), that is n, 'n *', 'a1 *' and so on to 'ak *'.
    """
    words = canonical(name).split()
    if not words:
        return ()
    groups = (' '.join(words[:end]) for end in range(len(words) - 1, 0, -1))
    full = ' '.join(words)
    return (full, f'{full} *', *(f'{group} *' for group in groups))


@dataclass(frozen=True)
class Category:
    """A page of /help."""

    key: str
    title: str
    description: str


CATEGORIES: tuple[Category, ...] = (
    Category(
        'practice',
        'Practice',
        'Problems to solve, gitgud challenges and the weekly problem',
    ),
    Category(
        'contests',
        'Contests',
        'Upcoming contests, reminders, ranklists and rated virtual contests',
    ),
    Category(
        'club',
        'Club',
        'KCPC workshops, the algorithm of the month and the pings you get',
    ),
    Category(
        'accounts',
        'Handles and accounts',
        'Link your Codeforces and AtCoder accounts, and look up other members',
    ),
    Category('duels', 'Duels', 'Challenge other members to rated duels'),
    Category(
        'graphs',
        'Graphs',
        "Plots of ratings and solved problems, and the server's rating spread",
    ),
    Category('bot', 'Bot', 'Help, and how the bot is doing'),
    Category(
        'setup',
        'Server setup',
        'Bot channels, the staff channel, command limits, KCPC and the starboard',
    ),
    Category('owner', 'Bot owner', 'Commands for the bot owner alone'),
)
_CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}

# The category of each cog's commands, by the cog's name.
COG_CATEGORY: Mapping[str, str] = MappingProxyType(
    {
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
)
# Commands whose category isn't their cog's.
NAME_CATEGORY: Mapping[str, str] = MappingProxyType({'gudgitters': 'practice'})


def category_of(name: str, cog_name: str | None) -> Category:
    """The /help page of command ``name``, from cog ``cog_name``.

    The bot owner's commands come first, then the server setup under /kcpc
    and /access, then the commands placed by name, and then each cog's; the
    rest are the bot's.
    """
    name = canonical(name)
    if (rule_for(name) or FAIL_CLOSED).who is Who.OWNER:
        key = 'owner'
    elif name.split(' ')[0] in ('kcpc', 'access'):
        key = 'setup'
    elif name in NAME_CATEGORY:
        key = NAME_CATEGORY[name]
    else:
        key = COG_CATEGORY.get(cog_name or '', 'bot')
    return _CATEGORY_BY_KEY[key]
