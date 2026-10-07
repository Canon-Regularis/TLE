"""Tests for tle.access.rules: levels, places, limits and every decision.

The expected outcomes are written out in full rather than computed, so that a
change to ``decide`` shows up as the exact cases it changes. The last tests
check, on the source and in a fresh interpreter, that the access package's
pure modules import nothing from Discord.
"""

import ast
import itertools
import subprocess
import sys
from pathlib import Path

import pytest

from tle.access.rules import (
    FAIL_CLOSED,
    LIMIT_WHERE,
    LIMIT_WHO,
    STAFF_LEVELS,
    Asker,
    Decision,
    Effective,
    Limit,
    Outcome,
    Rule,
    Spot,
    Where,
    Who,
    decide,
    effective,
    satisfies,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCESS_DIR = REPO_ROOT / 'tle' / 'access'
PURE_MODULES = ('__init__', 'rules', 'settings', 'table', 'policy')

BOT_CHANNEL, STAFF_CHANNEL, OTHER_CHANNEL = 1001, 2002, 3003
CHANNELS = {'bot': BOT_CHANNEL, 'staff': STAFF_CHANNEL, 'other': OTHER_CHANNEL}

PUBLIC = Outcome.PUBLIC
PRIVATE = Outcome.PRIVATE
WRONG = Outcome.WRONG_CHANNEL
ONLY = Outcome.PRIVATE_ONLY

ANYWHERE, BOT, BOT_ONLY = Where.ANYWHERE, Where.BOT, Where.BOT_ONLY
STAFF, STAFF_ONLY = Where.STAFF, Where.STAFF_ONLY

MEMBER = Asker()
ADMIN = Asker(admin_role=True)


def spot(slash: bool, place: str, *, channels: bool = True) -> Spot:
    """A use in the ``place`` channel, in a server with or without its bot
    channel and staff channel set.
    """
    return Spot(
        slash=slash,
        channel_id=CHANNELS[place],
        bot_channels=frozenset({BOT_CHANNEL}) if channels else frozenset(),
        staff_channel=STAFF_CHANNEL if channels else None,
    )


def every_spot() -> list[Spot]:
    """Both paths, in every channel, with and without the channels set."""
    return [
        spot(slash, place, channels=channels)
        for slash, place, channels in itertools.product(
            (True, False), CHANNELS, (True, False)
        )
    ]


def member_rule(where: Where, *, private: bool = False) -> Effective:
    return Effective(frozenset({Who.EVERYONE}), where, private=private)


# --- Who and where ---


def test_the_staff_levels() -> None:
    assert STAFF_LEVELS == {Who.MODERATOR, Who.DEVELOPER, Who.ADMIN, Who.OWNER}


def test_the_values_are_the_stored_names() -> None:
    # Stored limits name levels and places by these values, so they never change.
    assert [who.value for who in Who] == [
        'everyone',
        'trusted',
        'moderator',
        'developer',
        'admin',
        'owner',
    ]
    assert [where.value for where in Where] == [
        'anywhere',
        'bot',
        'bot-only',
        'staff',
        'staff-only',
    ]


def test_what_a_limit_may_choose() -> None:
    assert LIMIT_WHO == (Who.TRUSTED, Who.MODERATOR, Who.DEVELOPER, Who.ADMIN)
    assert LIMIT_WHERE == (BOT, BOT_ONLY, STAFF, STAFF_ONLY)


@pytest.mark.parametrize(
    ('where', 'scope', 'refuses_outside'),
    [
        (ANYWHERE, 0, False),
        (BOT, 1, False),
        (BOT_ONLY, 1, True),
        (STAFF, 2, False),
        (STAFF_ONLY, 2, True),
    ],
)
def test_scope_and_refusing_outside(
    where: Where, scope: int, refuses_outside: bool
) -> None:
    assert where.scope == scope
    assert where.refuses_outside is refuses_outside


# Each row tightened by each column, the columns in the order ANYWHERE, BOT,
# BOT_ONLY, STAFF, STAFF_ONLY.
TIGHTENED = {
    ANYWHERE: (ANYWHERE, BOT, BOT_ONLY, STAFF, STAFF_ONLY),
    BOT: (BOT, BOT, BOT_ONLY, STAFF, STAFF_ONLY),
    BOT_ONLY: (BOT_ONLY, BOT_ONLY, BOT_ONLY, STAFF_ONLY, STAFF_ONLY),
    STAFF: (STAFF, STAFF, STAFF_ONLY, STAFF, STAFF_ONLY),
    STAFF_ONLY: (STAFF_ONLY, STAFF_ONLY, STAFF_ONLY, STAFF_ONLY, STAFF_ONLY),
}


@pytest.mark.parametrize(
    ('where', 'other', 'expected'),
    [
        (where, other, expected)
        for where, row in TIGHTENED.items()
        for other, expected in zip(Where, row, strict=True)
    ],
)
def test_tighten(where: Where, other: Where, expected: Where) -> None:
    assert where.tighten(other) is expected


def test_tighten_is_commutative_associative_and_idempotent() -> None:
    for a, b, c in itertools.product(Where, repeat=3):
        assert a.tighten(b) is b.tighten(a)
        assert a.tighten(b).tighten(c) is a.tighten(b.tighten(c))
        assert a.tighten(a) is a


# How much each outcome lets through: everyone sees the answer, only the member
# sees it, or the command is refused.
OPENNESS = {PUBLIC: 2, PRIVATE: 1, WRONG: 0}


def openness(where: Where, at: Spot) -> int:
    return OPENNESS[decide(member_rule(where), MEMBER, at).outcome]


@pytest.mark.parametrize(('where', 'other'), list(itertools.product(Where, Where)))
def test_tighten_is_never_looser_than_either_place(where: Where, other: Where) -> None:
    # Bot-only and staff are the pair where taking the narrower place would
    # loosen: staff alone answers privately where bot-only refuses.
    tightened = where.tighten(other)

    for at in every_spot():
        assert openness(tightened, at) <= openness(where, at), (where, at)
        assert openness(tightened, at) <= openness(other, at), (other, at)


@pytest.mark.parametrize(('where', 'other'), list(itertools.product(Where, Where)))
def test_tighten_is_the_loosest_place_that_is_no_looser(
    where: Where, other: Where
) -> None:
    spots = every_spot()
    no_looser = [
        candidate
        for candidate in Where
        if all(
            openness(candidate, at) <= min(openness(where, at), openness(other, at))
            for at in spots
        )
    ]
    tightened = where.tighten(other)

    assert tightened in no_looser
    for candidate in no_looser:
        assert all(openness(candidate, at) <= openness(tightened, at) for at in spots)


# --- Who passes which level ---

ALL_LEVELS = frozenset(Who)
MODERATOR_PASSES = frozenset({Who.EVERYONE, Who.TRUSTED, Who.MODERATOR})


@pytest.mark.parametrize(
    ('asker', 'passes'),
    [
        (Asker(), {Who.EVERYONE}),
        (Asker(manage_guild=True), ALL_LEVELS - {Who.OWNER}),
        (Asker(admin_role=True), ALL_LEVELS - {Who.OWNER}),
        (Asker(moderator_role=True), MODERATOR_PASSES),
        (Asker(trusted_role=True), {Who.EVERYONE, Who.TRUSTED}),
        (Asker(developer_role=True), {Who.EVERYONE, Who.DEVELOPER}),
        # The bot owner passes none of a server's staff levels through it.
        (Asker(owner=True), {Who.EVERYONE, Who.OWNER}),
        (Asker(owner=True, admin_role=True), ALL_LEVELS),
        (
            Asker(moderator_role=True, developer_role=True),
            MODERATOR_PASSES | {Who.DEVELOPER},
        ),
        (
            Asker(trusted_role=True, developer_role=True),
            {Who.EVERYONE, Who.TRUSTED, Who.DEVELOPER},
        ),
        (
            Asker(
                trusted_role=True,
                moderator_role=True,
                developer_role=True,
                owner=True,
            ),
            ALL_LEVELS - {Who.ADMIN},
        ),
    ],
)
def test_satisfies(asker: Asker, passes: frozenset[Who]) -> None:
    assert {who for who in Who if satisfies(asker, who)} == passes


def test_every_asker_passes_everyone_and_only_owners_pass_owner() -> None:
    for flags in itertools.product((False, True), repeat=6):
        asker = Asker(*flags)
        assert satisfies(asker, Who.EVERYONE)
        assert satisfies(asker, Who.OWNER) is asker.owner


# --- Rules, limits and effective rules ---


def test_fail_closed_is_the_owner_in_the_staff_channel_only() -> None:
    assert FAIL_CLOSED == Rule(Who.OWNER, Where.STAFF_ONLY)
    assert not FAIL_CLOSED.private


def test_a_rule_is_public_unless_made_private() -> None:
    assert Rule(Who.EVERYONE, BOT).private is False


@pytest.mark.parametrize('who', [Who.EVERYONE, Who.OWNER])
def test_a_limit_cannot_require_everyone_or_the_owner(who: Who) -> None:
    with pytest.raises(ValueError, match='cannot require'):
        Limit(who=who)


def test_a_limit_cannot_move_a_command_anywhere() -> None:
    with pytest.raises(ValueError, match='cannot move'):
        Limit(where=ANYWHERE)


def test_a_limit_can_choose_every_allowed_value() -> None:
    for who in LIMIT_WHO:
        assert Limit(who=who).who is who
    for where in LIMIT_WHERE:
        assert Limit(where=where).where is where


@pytest.mark.parametrize(
    ('limit', 'empty'),
    [
        (Limit(), True),
        (Limit(who=Who.TRUSTED), False),
        (Limit(where=BOT), False),
        (Limit(private=True), False),
        (Limit(off=True), False),
    ],
)
def test_limit_is_empty(limit: Limit, empty: bool) -> None:
    assert limit.is_empty is empty


def test_an_effective_rule_needs_a_level() -> None:
    # Requiring no level would let everyone in, which a mistake must never do.
    with pytest.raises(ValueError, match='at least one level'):
        Effective(frozenset(), BOT)


def test_an_effective_rule_keeps_its_levels_as_a_frozenset() -> None:
    # A set, as a careless caller might pass.
    rule = Effective({Who.ADMIN}, BOT)  # type: ignore[arg-type]

    assert isinstance(rule.who, frozenset)
    assert rule.who == {Who.ADMIN}
    assert hash(rule) == hash(Effective(frozenset({Who.ADMIN}), BOT))


def test_effective_without_limits_is_the_rule() -> None:
    assert effective(Rule(Who.MODERATOR, STAFF)) == Effective(
        frozenset({Who.MODERATOR}),
        STAFF,
        private=False,
        off=False,
        limited=False,
        broken=False,
    )
    assert effective(Rule(Who.EVERYONE, BOT, private=True)).private


def test_effective_requires_every_level() -> None:
    rule = effective(
        Rule(Who.TRUSTED, BOT),
        [Limit(who=Who.DEVELOPER), Limit(who=Who.MODERATOR), Limit(off=False)],
    )

    assert rule.who == {Who.TRUSTED, Who.DEVELOPER, Who.MODERATOR}


@pytest.mark.parametrize(
    ('rule', 'places', 'expected'),
    [
        (BOT, [STAFF], STAFF),
        (STAFF, [BOT], STAFF),
        (ANYWHERE, [BOT], BOT),
        (BOT, [BOT_ONLY], BOT_ONLY),
        (BOT_ONLY, [STAFF], STAFF_ONLY),
        (STAFF, [BOT_ONLY], STAFF_ONLY),
        (ANYWHERE, [STAFF, BOT_ONLY, BOT], STAFF_ONLY),
        (STAFF_ONLY, [BOT], STAFF_ONLY),
    ],
)
def test_effective_takes_the_strictest_place(
    rule: Where, places: list[Where], expected: Where
) -> None:
    limits = [Limit(where=place) for place in places]

    assert effective(Rule(Who.EVERYONE, rule), limits).where is expected


def test_effective_private_and_off_come_from_any_limit() -> None:
    rule = effective(
        Rule(Who.EVERYONE, BOT),
        [Limit(who=Who.TRUSTED), Limit(private=True), Limit(where=STAFF)],
    )
    assert rule.private and not rule.off

    rule = effective(Rule(Who.EVERYONE, BOT), [Limit(who=Who.TRUSTED), Limit(off=True)])
    assert rule.off and not rule.private


@pytest.mark.parametrize(
    ('limits', 'limited'),
    [
        ([], False),
        ([Limit()], False),
        ([Limit(), Limit()], False),
        ([Limit(), Limit(where=BOT)], True),
        ([Limit(off=True)], True),
        # A limit that repeats the rule still counts: an admin set it.
        ([Limit(who=Who.ADMIN)], True),
    ],
)
def test_effective_is_limited_by_any_limit_that_sets_something(
    limits: list[Limit], limited: bool
) -> None:
    rule = effective(Rule(Who.ADMIN, BOT), limits)

    assert rule.limited is limited
    assert rule.who == {Who.ADMIN}


def test_effective_reads_a_one_off_iterable_once() -> None:
    limits = iter([Limit(who=Who.MODERATOR), Limit(where=STAFF), Limit(off=True)])

    rule = effective(Rule(Who.EVERYONE, BOT), limits)

    assert rule == Effective(
        frozenset({Who.EVERYONE, Who.MODERATOR}), STAFF, off=True, limited=True
    )


# --- decide: every place, channel and path ---

# Per place and channel: slash, slash with a private rule, prefix, and prefix
# with a private rule. The staff channel counts as a bot channel.
WITH_CHANNELS = {
    (ANYWHERE, 'bot'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (ANYWHERE, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (ANYWHERE, 'other'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT, 'bot'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT, 'other'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (BOT_ONLY, 'bot'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT_ONLY, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT_ONLY, 'other'): (WRONG, WRONG, WRONG, ONLY),
    (STAFF, 'bot'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (STAFF, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (STAFF, 'other'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (STAFF_ONLY, 'bot'): (WRONG, WRONG, WRONG, ONLY),
    (STAFF_ONLY, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (STAFF_ONLY, 'other'): (WRONG, WRONG, WRONG, ONLY),
}
# With no bot channel and no staff channel set, no channel is either.
WITHOUT_CHANNELS = {
    (ANYWHERE, 'bot'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (ANYWHERE, 'staff'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (ANYWHERE, 'other'): (PUBLIC, PRIVATE, PUBLIC, ONLY),
    (BOT, 'bot'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (BOT, 'staff'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (BOT, 'other'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (BOT_ONLY, 'bot'): (WRONG, WRONG, WRONG, ONLY),
    (BOT_ONLY, 'staff'): (WRONG, WRONG, WRONG, ONLY),
    (BOT_ONLY, 'other'): (WRONG, WRONG, WRONG, ONLY),
    (STAFF, 'bot'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (STAFF, 'staff'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (STAFF, 'other'): (PRIVATE, PRIVATE, WRONG, ONLY),
    (STAFF_ONLY, 'bot'): (WRONG, WRONG, WRONG, ONLY),
    (STAFF_ONLY, 'staff'): (WRONG, WRONG, WRONG, ONLY),
    (STAFF_ONLY, 'other'): (WRONG, WRONG, WRONG, ONLY),
}
# The columns of the tables above: (slash, private).
PATHS = ((True, False), (True, True), (False, False), (False, True))


def matrix_cases() -> list[object]:
    cases: list[object] = []
    for channels, table in ((True, WITH_CHANNELS), (False, WITHOUT_CHANNELS)):
        for (where, place), row in table.items():
            for (slash, private), expected in zip(PATHS, row, strict=True):
                name = '-'.join(
                    [where.value, place, 'slash' if slash else 'prefix']
                    + (['private'] if private else [])
                    + ['channels' if channels else 'no-channels']
                )
                case = (where, place, slash, private, channels, expected)
                cases.append(pytest.param(*case, id=name))
    return cases


@pytest.mark.parametrize(
    ('where', 'place', 'slash', 'private', 'channels', 'expected'), matrix_cases()
)
def test_decide_matrix(
    where: Where,
    place: str,
    slash: bool,
    private: bool,
    channels: bool,
    expected: Outcome,
) -> None:
    rule = member_rule(where, private=private)

    decision = decide(rule, MEMBER, spot(slash, place, channels=channels))

    assert decision == Decision(expected, rule, slash)


def test_the_matrix_covers_every_place_and_channel() -> None:
    keys = set(itertools.product(Where, CHANNELS))

    assert set(WITH_CHANNELS) == set(WITHOUT_CHANNELS) == keys
    assert len(matrix_cases()) == 2 * len(keys) * len(PATHS)


@pytest.mark.parametrize('where', [BOT, BOT_ONLY])
def test_the_staff_channel_is_a_bot_channel_even_with_none_set(where: Where) -> None:
    at = Spot(slash=False, channel_id=STAFF_CHANNEL, staff_channel=STAFF_CHANNEL)

    assert decide(member_rule(where), MEMBER, at).outcome is PUBLIC


@pytest.mark.parametrize('where', [STAFF, STAFF_ONLY])
def test_a_bot_channel_is_not_the_staff_channel(where: Where) -> None:
    at = Spot(
        slash=False, channel_id=BOT_CHANNEL, bot_channels=frozenset({BOT_CHANNEL})
    )

    assert decide(member_rule(where), MEMBER, at).outcome is WRONG


@pytest.mark.parametrize(
    ('where', 'expected'),
    [
        (ANYWHERE, PUBLIC),
        (BOT, WRONG),
        (BOT_ONLY, WRONG),
        (STAFF, WRONG),
        (STAFF_ONLY, WRONG),
    ],
)
def test_an_unknown_channel_is_neither_a_bot_nor_the_staff_channel(
    where: Where, expected: Outcome
) -> None:
    # With no staff channel set, the unknown channel's None must not match it.
    at = Spot(slash=False, channel_id=None)

    assert decide(member_rule(where), MEMBER, at).outcome is expected


def test_several_bot_channels() -> None:
    channels = frozenset({BOT_CHANNEL, OTHER_CHANNEL})

    for channel in (BOT_CHANNEL, OTHER_CHANNEL):
        at = Spot(slash=False, channel_id=channel, bot_channels=channels)
        assert decide(member_rule(BOT_ONLY), MEMBER, at).outcome is PUBLIC
    at = Spot(slash=False, channel_id=STAFF_CHANNEL, bot_channels=channels)
    assert decide(member_rule(BOT_ONLY), MEMBER, at).outcome is WRONG


# --- decide: refusals before places ---


def test_not_allowed_comes_first_and_reveals_nothing() -> None:
    # Neither that the command is off or broken, nor where it would work.
    for where, at in itertools.product(Where, every_spot()):
        rule = Effective(
            frozenset({Who.ADMIN}), where, private=True, off=True, broken=True
        )

        decision = decide(rule, Asker(trusted_role=True), at)

        assert decision.outcome is Outcome.NOT_ALLOWED
        assert decision.silent is (not at.slash)
        assert not decision.allowed


def test_broken_comes_before_off_and_places() -> None:
    for where, at in itertools.product(Where, every_spot()):
        rule = Effective(frozenset({Who.EVERYONE}), where, off=True, broken=True)

        assert decide(rule, MEMBER, at).outcome is Outcome.BROKEN


def test_off_comes_before_places() -> None:
    for where, private, at in itertools.product(Where, (False, True), every_spot()):
        rule = Effective(frozenset({Who.EVERYONE}), where, private=private, off=True)

        assert decide(rule, MEMBER, at).outcome is Outcome.OFF


@pytest.mark.parametrize(
    ('asker', 'allowed'),
    [
        (Asker(), False),
        (Asker(trusted_role=True), False),
        (Asker(developer_role=True), False),
        (Asker(trusted_role=True, developer_role=True), True),
        (Asker(moderator_role=True, developer_role=True), True),
        (Asker(manage_guild=True), True),
        (Asker(owner=True), False),
    ],
)
def test_every_level_must_be_passed(asker: Asker, allowed: bool) -> None:
    rule = Effective(frozenset({Who.TRUSTED, Who.DEVELOPER}), ANYWHERE)

    decision = decide(rule, asker, spot(True, 'other'))

    assert decision.outcome is (PUBLIC if allowed else Outcome.NOT_ALLOWED)


def test_fail_closed_lets_only_the_owner_in_and_only_in_the_staff_channel() -> None:
    rule = effective(FAIL_CLOSED)

    for at in every_spot():
        assert decide(rule, ADMIN, at).outcome is Outcome.NOT_ALLOWED
        outcome = decide(rule, Asker(owner=True), at).outcome
        if at.staff_channel is not None and at.channel_id == at.staff_channel:
            assert outcome is PUBLIC
        else:
            assert outcome is WRONG


# --- Decision ---


@pytest.mark.parametrize('slash', [True, False])
@pytest.mark.parametrize('outcome', list(Outcome))
def test_decision_properties(outcome: Outcome, slash: bool) -> None:
    decision = Decision(outcome, member_rule(BOT), slash)

    assert decision.allowed is (outcome in (PUBLIC, PRIVATE))
    assert decision.private is (outcome is PRIVATE)
    assert decision.silent is (outcome is Outcome.NOT_ALLOWED and not slash)


# --- The pure modules import nothing from Discord ---

PURE = tuple(f'tle.access.{name}' for name in PURE_MODULES if name != '__init__')


def imported(source: str) -> list[str]:
    """Every name ``source`` imports, anywhere in it.

    ``from a import b`` imports ``a.b``, and a relative import starts with dots.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = '.' * node.level + (f'{node.module}.' if node.module else '')
            names += [f'{base}{alias.name}' for alias in node.names]
    return names


def impure(source: str) -> list[str]:
    """What ``source`` imports besides the standard library and pure modules."""
    return sorted(
        name
        for name in imported(source)
        if name.startswith('.')
        or not (
            name.split('.')[0] in sys.stdlib_module_names
            or any(name == module or name.startswith(f'{module}.') for module in PURE)
        )
    )


@pytest.mark.parametrize('module', PURE_MODULES)
def test_the_pure_modules_import_only_the_standard_library_and_each_other(
    module: str,
) -> None:
    source = (ACCESS_DIR / f'{module}.py').read_text(encoding='utf-8')

    assert impure(source) == []
    assert not [name for name in imported(source) if 'discord' in name]


def test_the_import_check_finds_imports_anywhere() -> None:
    source = '\n'.join(
        [
            'import json',
            'import discord',
            'from tle.access.rules import Who',
            'from tle.access import settings',
            'from tle.access import service',
            'from . import table',
            'def f():',
            '    from discord.ext import commands',
            'if TYPE_CHECKING:',
            '    import aiohttp',
        ]
    )

    assert impure(source) == [
        '.table',
        'aiohttp',
        'discord',
        'discord.ext.commands',
        'tle.access.service',
    ]


def test_the_package_init_is_only_a_docstring() -> None:
    body = ast.parse((ACCESS_DIR / '__init__.py').read_text(encoding='utf-8')).body

    assert len(body) == 1
    assert isinstance(body[0], ast.Expr)
    assert isinstance(body[0].value, ast.Constant)
    assert isinstance(body[0].value.value, str)


def test_importing_the_pure_modules_loads_no_discord() -> None:
    # In a fresh interpreter, so that other tests' imports don't count.
    code = '\n'.join(
        [
            'import sys',
            'import tle.access.policy, tle.access.rules, tle.access.settings',
            'import tle.access.table',
            "loaded = [m for m in sys.modules if m.split('.')[0] == 'discord']",
            'assert not loaded, loaded',
        ]
    )

    result = subprocess.run(
        [sys.executable, '-B', '-c', code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
