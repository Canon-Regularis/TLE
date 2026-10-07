"""Who may use a command, where, and how it answers there.

The model behind every access decision. It imports nothing from Discord, so
each decision can be tested on its own:

- a command's default ``Rule`` names the members it is for (``Who``) and the
  channels where it answers publicly (``Where``);
- a server's admins can tighten a rule with ``Limit``s, and ``effective``
  combines a rule with its limits into an ``Effective`` rule;
- ``decide`` turns an effective rule, the member asking (``Asker``) and the
  channel (``Spot``) into a ``Decision``: answer publicly, answer only that
  member, or refuse, saying why.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum


class Who(Enum):
    """The members a command is for.

    The levels are not a ladder. Admins pass every level but the owner's,
    moderators also pass trusted, and developers pass only their own. The bot
    owner passes the owner level, which nobody else passes, and no other level
    through it.
    """

    EVERYONE = 'everyone'
    TRUSTED = 'trusted'
    MODERATOR = 'moderator'
    DEVELOPER = 'developer'
    ADMIN = 'admin'
    OWNER = 'owner'


# The levels of staff commands, which are kept out of members' slash lists.
STAFF_LEVELS: frozenset[Who] = frozenset(
    {Who.MODERATOR, Who.DEVELOPER, Who.ADMIN, Who.OWNER}
)


class Where(Enum):
    """Where a command answers publicly, and what it does elsewhere.

    A server's bot channels include its staff channel. Elsewhere, a slash
    command answers only the member who used it, and a prefix command is
    refused; the ``_ONLY`` places refuse slash commands there too.
    """

    ANYWHERE = 'anywhere'
    BOT = 'bot'
    BOT_ONLY = 'bot-only'
    STAFF = 'staff'
    STAFF_ONLY = 'staff-only'

    @property
    def scope(self) -> int:
        """How narrow the place is: 0 anywhere, 1 bot channels, 2 staff channel."""
        return _SCOPES[self]

    @property
    def refuses_outside(self) -> bool:
        """Whether a slash command is refused outside the place, not answered
        privately.
        """
        return self in (Where.BOT_ONLY, Where.STAFF_ONLY)

    def tighten(self, other: 'Where') -> 'Where':
        """The narrower place, refusing outside it if either place does.

        The result is never looser than either place: bot-only tightened by
        staff is staff-only, since staff alone would answer privately where
        bot-only refuses.
        """
        refuses = self.refuses_outside or other.refuses_outside
        return _BY_SCOPE[max(self.scope, other.scope), refuses]


_SCOPES = {
    Where.ANYWHERE: 0,
    Where.BOT: 1,
    Where.BOT_ONLY: 1,
    Where.STAFF: 2,
    Where.STAFF_ONLY: 2,
}
_BY_SCOPE = {(where.scope, where.refuses_outside): where for where in Where}

# What a limit may require. Everyone would add nothing, and the bot owner is
# for the default rules alone: a limit must never lock a server's admins out.
LIMIT_WHO: tuple[Who, ...] = (Who.TRUSTED, Who.MODERATOR, Who.DEVELOPER, Who.ADMIN)
# Where a limit may move a command; anywhere would add nothing.
LIMIT_WHERE: tuple[Where, ...] = (
    Where.BOT,
    Where.BOT_ONLY,
    Where.STAFF,
    Where.STAFF_ONLY,
)


@dataclass(frozen=True)
class Rule:
    """A command's default: who may use it, and where it answers publicly.

    ``private`` makes every slash answer private and refuses the prefix form.
    """

    who: Who
    where: Where
    private: bool = False


@dataclass(frozen=True)
class Limit:
    """How a server's admins tightened a command; None leaves that part alone.

    ``who`` adds a level that members must pass as well, ``where`` narrows the
    place, ``private`` makes slash answers private and refuses the prefix form,
    and ``off`` refuses the command outright. ``ValueError`` if ``who`` is not
    in ``LIMIT_WHO`` or ``where`` not in ``LIMIT_WHERE``.
    """

    who: Who | None = None
    where: Where | None = None
    private: bool = False
    off: bool = False

    def __post_init__(self) -> None:
        if self.who is not None and self.who not in LIMIT_WHO:
            raise ValueError(f'A limit cannot require {self.who}')
        if self.where is not None and self.where not in LIMIT_WHERE:
            raise ValueError(f'A limit cannot move a command to {self.where}')

    @property
    def is_empty(self) -> bool:
        """Whether the limit changes nothing."""
        return (
            self.who is None
            and self.where is None
            and not self.private
            and not self.off
        )


@dataclass(frozen=True)
class Effective:
    """A command's rule in one server: its default rule with the server's limits.

    ``ValueError`` if ``who`` is empty: requiring no level at all would let
    everyone in, so it can only be a mistake.
    """

    who: frozenset[Who]  # every one of these levels must be passed
    where: Where
    private: bool = False
    off: bool = False
    limited: bool = False  # at least one of the server's limits changed something
    broken: bool = False  # the server's stored settings could not be read

    def __post_init__(self) -> None:
        if not self.who:
            raise ValueError('An effective rule needs at least one level')
        object.__setattr__(self, 'who', frozenset(self.who))


@dataclass(frozen=True)
class Asker:
    """The member using a command, as far as access goes, read at that moment."""

    manage_guild: bool = False  # has the Manage Server permission
    admin_role: bool = False
    moderator_role: bool = False
    trusted_role: bool = False
    developer_role: bool = False
    owner: bool = False  # owns the bot's application, or is on its team


@dataclass(frozen=True)
class Spot:
    """Where a command was used, and the server's channels that matter there."""

    slash: bool
    channel_id: int | None  # for a thread, the channel it is in
    bot_channels: frozenset[int] = frozenset()
    staff_channel: int | None = None


class Outcome(Enum):
    """What ``decide`` concluded."""

    PUBLIC = 'public'  # the command runs and answers in the channel
    PRIVATE = 'private'  # the command runs and answers only the member
    NOT_ALLOWED = 'not-allowed'  # the member doesn't pass the rule's who
    OFF = 'off'  # a limit switched the command off
    BROKEN = 'broken'  # the server's access settings need repair
    WRONG_CHANNEL = 'wrong-channel'  # the command doesn't work in this channel
    PRIVATE_ONLY = 'private-only'  # a prefix command whose answers must be private


@dataclass(frozen=True)
class Decision:
    """The outcome for one use of a command, and the rule that gave it."""

    outcome: Outcome
    rule: Effective
    slash: bool

    @property
    def allowed(self) -> bool:
        """Whether the command runs."""
        return self.outcome in (Outcome.PUBLIC, Outcome.PRIVATE)

    @property
    def private(self) -> bool:
        """Whether the command runs, answering only the member."""
        return self.outcome is Outcome.PRIVATE

    @property
    def silent(self) -> bool:
        """Whether the refusal gets no answer at all.

        A prefix command that the member may not use stays quiet, as if it
        didn't exist; a slash command must always be answered.
        """
        return self.outcome is Outcome.NOT_ALLOWED and not self.slash


# The rule of a command missing from the table: the bot owner alone, and only
# in the staff channel.
FAIL_CLOSED = Rule(Who.OWNER, Where.STAFF_ONLY)


def satisfies(asker: Asker, who: Who) -> bool:
    """Whether ``asker`` passes the level ``who``."""
    if who is Who.EVERYONE:
        return True
    if who is Who.OWNER:
        return asker.owner
    admin = asker.manage_guild or asker.admin_role
    if who is Who.ADMIN:
        return admin
    if who is Who.DEVELOPER:
        return admin or asker.developer_role
    moderator = admin or asker.moderator_role
    if who is Who.MODERATOR:
        return moderator
    if who is Who.TRUSTED:
        return moderator or asker.trusted_role
    raise ValueError(f'Unknown level {who!r}')


def effective(rule: Rule, limits: Iterable[Limit] = ()) -> Effective:
    """``rule`` with ``limits`` applied, each of which can only tighten it.

    Members must pass every level required, the place is the strictest of
    them all, and any limit can make answers private or switch the command off.
    """
    applied = tuple(limits)
    who = {rule.who} | {limit.who for limit in applied if limit.who is not None}
    where = rule.where
    for limit in applied:
        if limit.where is not None:
            where = where.tighten(limit.where)
    return Effective(
        who=frozenset(who),
        where=where,
        private=rule.private or any(limit.private for limit in applied),
        off=any(limit.off for limit in applied),
        limited=any(not limit.is_empty for limit in applied),
    )


def decide(rule: Effective, asker: Asker, spot: Spot) -> Decision:
    """Whether and how a command with ``rule`` runs for ``asker`` at ``spot``.

    Who comes first, so that a member who may not use a command learns nothing
    about it: not that it is off, nor where it would work.
    """
    if not all(satisfies(asker, who) for who in rule.who):
        outcome = Outcome.NOT_ALLOWED
    elif rule.broken:
        outcome = Outcome.BROKEN
    elif rule.off:
        outcome = Outcome.OFF
    elif spot.slash:
        outcome = _slash_outcome(rule, _in_place(rule.where, spot))
    elif rule.private:
        outcome = Outcome.PRIVATE_ONLY
    elif _in_place(rule.where, spot):
        outcome = Outcome.PUBLIC
    else:
        outcome = Outcome.WRONG_CHANNEL
    return Decision(outcome, rule, spot.slash)


def _in_place(where: Where, spot: Spot) -> bool:
    """Whether ``spot`` is a channel where ``where`` answers publicly."""
    channel, staff = spot.channel_id, spot.staff_channel
    if where is Where.ANYWHERE:
        return True
    if where in (Where.BOT, Where.BOT_ONLY):
        # The staff channel counts as a bot channel.
        return channel is not None and (
            channel in spot.bot_channels or channel == staff
        )
    return staff is not None and channel == staff


def _slash_outcome(rule: Effective, in_place: bool) -> Outcome:
    """A slash command answers privately outside its place, unless the place
    refuses that; a private rule makes even answers in place private.
    """
    if in_place:
        return Outcome.PRIVATE if rule.private else Outcome.PUBLIC
    if rule.where.refuses_outside:
        return Outcome.WRONG_CHANNEL
    return Outcome.PRIVATE
