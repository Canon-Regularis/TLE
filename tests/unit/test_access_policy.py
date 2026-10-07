"""Tests for tle.access.policy: a command's rule in one server, and rules in
plain English.
"""

import itertools
from dataclasses import replace

import pytest

from tle.access.policy import (
    describe_limit,
    describe_where,
    describe_who,
    effective_for,
)
from tle.access.rules import (
    FAIL_CLOSED,
    LIMIT_WHERE,
    LIMIT_WHO,
    Asker,
    Effective,
    Limit,
    Outcome,
    Spot,
    Where,
    Who,
    decide,
    effective,
    satisfies,
)
from tle.access.settings import GuildAccess
from tle.access.table import OWNER_AND_ADMIN, RULES, TWINS

EVERYONE, TRUSTED, MODERATOR = Who.EVERYONE, Who.TRUSTED, Who.MODERATOR
DEVELOPER, ADMIN, OWNER = Who.DEVELOPER, Who.ADMIN, Who.OWNER
ANYWHERE, BOT, BOT_ONLY = Where.ANYWHERE, Where.BOT, Where.BOT_ONLY
STAFF, STAFF_ONLY = Where.STAFF, Where.STAFF_ONLY

STAFF_CHANNEL = 2002
OFF = Limit(off=True)


def with_limits(limits: dict[str, Limit]) -> GuildAccess:
    return GuildAccess(limits=limits)


def rule(
    who: set[Who],
    where: Where,
    *,
    private: bool = False,
    off: bool = False,
    limited: bool = False,
    broken: bool = False,
) -> Effective:
    return Effective(frozenset(who), where, private, off, limited, broken)


# --- effective_for ---


@pytest.mark.parametrize('name', sorted(RULES))
def test_with_no_limits_a_command_has_its_default_rule(name: str) -> None:
    # /access works anywhere until there is a staff channel; see below.
    access = GuildAccess(staff_channel=STAFF_CHANNEL)
    expected = effective(RULES[name])
    if name in OWNER_AND_ADMIN:
        # The owner must be an admin too; see below.
        expected = replace(expected, who=frozenset({OWNER, ADMIN}))

    assert effective_for(name, access) == expected


def test_a_twin_has_its_canonical_names_rule() -> None:
    for twin, name in TWINS.items():
        assert effective_for(twin, GuildAccess()) == effective_for(name, GuildAccess())


def test_a_command_key_limits_that_command_alone() -> None:
    access = with_limits({'duel': Limit(who=MODERATOR)})

    assert effective_for('duel', access) == rule(
        {EVERYONE, MODERATOR}, BOT, limited=True
    )
    assert effective_for('duel register', access) == rule({MODERATOR}, BOT)
    assert effective_for('duel accept', access) == rule({EVERYONE}, BOT_ONLY)


def test_a_group_key_limits_the_group_and_every_subcommand() -> None:
    access = with_limits({'duel *': Limit(where=STAFF)})

    assert effective_for('duel', access) == rule({EVERYONE}, STAFF, limited=True)
    assert effective_for('duel register', access) == rule(
        {MODERATOR}, STAFF, limited=True
    )
    # Staff tightens bot-only to staff-only, never loosens it.
    assert effective_for('duel accept', access) == rule(
        {EVERYONE}, STAFF_ONLY, limited=True
    )
    assert not effective_for('gitgud', access).limited


def test_a_group_key_limits_nested_groups() -> None:
    access = with_limits({'kcpc *': Limit(who=DEVELOPER)})

    assert effective_for('kcpc weekly queue', access) == rule(
        {ADMIN, DEVELOPER}, STAFF, limited=True
    )
    assert effective_for('kcpc', access).limited
    assert not effective_for('weekly', access).limited


def test_a_group_key_never_reaches_another_tree() -> None:
    # The member contests group and the admin one under kcpc are different.
    access = with_limits({'contests *': OFF})

    assert effective_for('contests live', access).off
    assert not effective_for('kcpc contests platforms', access).off
    assert not effective_for('kcpc contests', access).off


def test_twins_share_their_limits() -> None:
    access = with_limits({'contests': OFF, 'handle show': Limit(private=True)})

    assert effective_for('contests upcoming', access).off
    assert not effective_for('contests live', access).off
    assert effective_for('handle', access).private
    assert not effective_for('handle set', access).private

    access = with_limits({'weekly *': Limit(where=BOT_ONLY)})
    for name in ('weekly', 'weekly current', 'weekly history'):
        assert effective_for(name, access).where is BOT_ONLY


def test_a_group_key_on_the_handle_group_reaches_its_twin() -> None:
    access = with_limits({'handle *': OFF})

    for name in ('handle', 'handle show', 'handle set', 'handle refer'):
        assert effective_for(name, access).off, name


def test_every_limit_that_applies_counts() -> None:
    access = with_limits(
        {
            'duel *': Limit(who=TRUSTED),
            'duel complete': Limit(where=STAFF),
            'duel complete *': Limit(private=True),
            'duel accept': OFF,
        }
    )

    assert effective_for('duel complete', access) == rule(
        {EVERYONE, TRUSTED}, STAFF_ONLY, private=True, limited=True
    )


def test_an_empty_limit_changes_nothing() -> None:
    access = GuildAccess(limits={'duel': Limit()})

    assert effective_for('duel', access) == rule({EVERYONE}, BOT)


@pytest.mark.parametrize(
    'name',
    [
        'cache',
        'cache contests',
        'meta kill',
        'meta guilds',
        'kcpc contests add',
        'kcpc contests settime',
        'kcpc contests remove',
        'kcpc contests sync',
    ],
)
def test_the_bot_owners_commands_ignore_limits(name: str) -> None:
    root = name.split()[0]
    access = GuildAccess(
        limits={
            name: Limit(who=ADMIN, where=STAFF_ONLY, private=True, off=True),
            f'{name} *': OFF,
            f'{root} *': OFF,
            'kcpc contests *': OFF,
        }
    )
    # The club contest commands need the owner to be an admin too, as KCPC's
    # own check on them does.
    who = {OWNER, ADMIN} if name.startswith('kcpc contests') else {OWNER}

    assert effective_for(name, access) == rule(who, ANYWHERE)


@pytest.mark.parametrize('name', sorted(OWNER_AND_ADMIN))
def test_the_club_contest_commands_are_for_an_owner_who_is_an_admin(
    name: str,
) -> None:
    rule_here = effective_for(name, GuildAccess())
    anywhere = Spot(slash=False, channel_id=3003)

    def outcome(asker: Asker) -> Outcome:
        return decide(rule_here, asker, anywhere).outcome

    assert outcome(Asker(owner=True, manage_guild=True)) is Outcome.PUBLIC
    assert outcome(Asker(owner=True, admin_role=True)) is Outcome.PUBLIC
    assert outcome(Asker(owner=True)) is Outcome.NOT_ALLOWED
    assert outcome(Asker(manage_guild=True, admin_role=True)) is Outcome.NOT_ALLOWED
    assert describe_who(rule_here.who) == 'The bot owner, who must also be an admin'


def test_a_group_limit_still_reaches_the_groups_other_commands() -> None:
    access = with_limits({'meta *': OFF, 'kcpc contests *': OFF})

    assert effective_for('meta ping', access).off
    assert not effective_for('meta kill', access).off
    assert effective_for('kcpc contests platforms', access).off
    assert not effective_for('kcpc contests add', access).off


@pytest.mark.parametrize(
    'name',
    [
        'help',
        'access',
        'access bot-channels',
        'access bot-channels add',
        'access limit',
        'access reset',
    ],
)
def test_help_and_access_ignore_limits(name: str) -> None:
    root = name.split()[0]
    access = GuildAccess(
        staff_channel=STAFF_CHANNEL,
        limits={name: OFF, f'{name} *': OFF, f'{root} *': Limit(who=DEVELOPER)},
    )

    assert effective_for(name, access) == effective(RULES[name])


def test_broken_settings_refuse_with_the_default_rule() -> None:
    broken = GuildAccess(broken=True)

    assert effective_for('gitgud', broken) == rule({EVERYONE}, BOT, broken=True)
    assert effective_for('kcpc status', broken) == rule(
        {DEVELOPER}, STAFF_ONLY, broken=True
    )
    assert effective_for('contests upcoming', broken).broken


@pytest.mark.parametrize(
    'name', ['cache', 'meta kill', 'kcpc contests sync', 'help', 'access reset']
)
def test_broken_settings_leave_the_owners_commands_help_and_access(name: str) -> None:
    assert not effective_for(name, GuildAccess(broken=True)).broken


@pytest.mark.parametrize(
    'name',
    [
        'access',
        'access bot-channels',
        'access bot-channels add',
        'access bot-channels remove',
        'access staff-channel',
        'access limit',
        'access reset',
    ],
)
def test_access_works_anywhere_until_there_is_a_staff_channel(name: str) -> None:
    assert effective_for(name, GuildAccess()) == rule({ADMIN}, ANYWHERE)
    assert effective_for(name, GuildAccess(bot_channels=frozenset({1}))).where is (
        ANYWHERE
    )
    assert effective_for(name, GuildAccess(broken=True)) == rule({ADMIN}, ANYWHERE)
    with_staff = GuildAccess(staff_channel=STAFF_CHANNEL)
    assert effective_for(name, with_staff) == rule({ADMIN}, STAFF)


@pytest.mark.parametrize('name', ['help', 'kcpc', 'kcpc status', 'starboard'])
def test_only_access_works_anywhere_without_a_staff_channel(name: str) -> None:
    assert effective_for(name, GuildAccess()) == effective(RULES[name])


@pytest.mark.parametrize('name', ['not a command', 'clist show', '', 'access x y'])
def test_a_command_missing_from_the_table_fails_closed(name: str) -> None:
    for access in (
        GuildAccess(),
        with_limits({'clist *': OFF}),
        GuildAccess(broken=True),
    ):
        assert effective_for(name, access) == effective(FAIL_CLOSED)


def test_a_broken_server_can_still_repair_its_settings() -> None:
    # In any channel, admins reach /access reset, and members are refused.
    broken = GuildAccess(broken=True)
    anywhere = Spot(slash=False, channel_id=3003)

    admin = decide(
        effective_for('access reset', broken), Asker(admin_role=True), anywhere
    )
    member = decide(effective_for('gitgud', broken), Asker(), anywhere)
    stranger = decide(effective_for('access reset', broken), Asker(), anywhere)

    assert admin.outcome is Outcome.PUBLIC
    assert member.outcome is Outcome.BROKEN
    assert stranger.outcome is Outcome.NOT_ALLOWED and stranger.silent


# --- describe_who and describe_where ---


@pytest.mark.parametrize(
    ('who', 'text'),
    [
        ({EVERYONE}, 'Everyone'),
        ({TRUSTED}, 'Trusted members, moderators and admins'),
        ({MODERATOR}, 'Moderators and admins'),
        ({DEVELOPER}, 'Developers and admins'),
        ({ADMIN}, 'Admins'),
        ({OWNER}, 'The bot owner'),
        # Levels that another one implies add nothing.
        ({EVERYONE, ADMIN}, 'Admins'),
        ({TRUSTED, MODERATOR}, 'Moderators and admins'),
        ({DEVELOPER, ADMIN}, 'Admins'),
        ({TRUSTED, MODERATOR, DEVELOPER, ADMIN}, 'Admins'),
        ({EVERYONE, OWNER}, 'The bot owner'),
        # Levels that neither implies.
        (
            {TRUSTED, DEVELOPER},
            'Admins, and developers who are also trusted members or moderators',
        ),
        ({MODERATOR, DEVELOPER}, 'Admins, and moderators who are also developers'),
        (
            {TRUSTED, MODERATOR, DEVELOPER},
            'Admins, and moderators who are also developers',
        ),
        ({OWNER, ADMIN}, 'The bot owner, who must also be an admin'),
        (
            {OWNER, TRUSTED},
            'The bot owner, who must also be a trusted member, moderator or admin',
        ),
        (
            {OWNER, MODERATOR, DEVELOPER},
            'The bot owner, who must also be an admin, or a moderator who is also '
            'a developer',
        ),
    ],
)
def test_describe_who(who: set[Who], text: str) -> None:
    assert describe_who(frozenset(who)) == text


EVERY_ASKER = [Asker(*flags) for flags in itertools.product((False, True), repeat=6)]


def let_in(who: frozenset[Who]) -> frozenset[Asker]:
    """Every kind of member who passes every level in ``who``."""
    return frozenset(
        asker for asker in EVERY_ASKER if all(satisfies(asker, w) for w in who)
    )


def test_describe_who_reads_the_same_exactly_when_the_same_members_pass() -> None:
    levels = [
        frozenset(subset)
        for size in range(1, len(Who) + 1)
        for subset in itertools.combinations(Who, size)
    ]
    texts = {who: describe_who(who) for who in levels}
    members = {who: let_in(who) for who in levels}

    for first, second in itertools.combinations(levels, 2):
        same_members = members[first] == members[second]
        assert (texts[first] == texts[second]) is same_members, (first, second)
    for text in texts.values():
        assert text[0].isupper() and not text.endswith('.'), text


def test_describe_who_needs_a_level() -> None:
    with pytest.raises(ValueError, match='No level'):
        describe_who(frozenset())


@pytest.mark.parametrize(
    ('where', 'text', 'prefix_only'),
    [
        (ANYWHERE, 'Any channel', 'Any channel'),
        (
            BOT,
            'Bot channels; elsewhere the slash command answers only you',
            'Bot channels',
        ),
        (BOT_ONLY, 'Bot channels only', 'Bot channels only'),
        (
            STAFF,
            'The staff channel; elsewhere the slash command answers only you',
            'The staff channel',
        ),
        (STAFF_ONLY, 'The staff channel only', 'The staff channel only'),
    ],
)
def test_describe_where(where: Where, text: str, prefix_only: str) -> None:
    assert describe_where(where) == text
    assert describe_where(where, slash=True) == text
    assert describe_where(where, slash=False) == prefix_only


def test_describe_where_says_slash_answers_privately_exactly_where_they_do() -> None:
    # A slash command answers privately outside its place unless it refuses.
    outside = Spot(slash=True, channel_id=3003, staff_channel=STAFF_CHANNEL)
    for where in Where:
        private = decide(rule({EVERYONE}, where), Asker(), outside).private
        assert ('answers only you' in describe_where(where)) is private, where


# --- describe_limit ---


@pytest.mark.parametrize(
    ('limit', 'text'),
    [
        (OFF, 'switched off'),
        (Limit(who=TRUSTED), 'for trusted members, moderators and admins only'),
        (Limit(who=MODERATOR), 'for moderators and admins only'),
        (Limit(who=DEVELOPER), 'for developers and admins only'),
        (Limit(who=ADMIN), 'for admins only'),
        (Limit(where=BOT), 'in bot channels'),
        (Limit(where=BOT_ONLY), 'in bot channels only'),
        (Limit(where=STAFF), 'in the staff channel'),
        (Limit(where=STAFF_ONLY), 'in the staff channel only'),
        (Limit(private=True), 'answers only the person who uses it'),
        # Off first, as it matters most.
        (
            Limit(who=MODERATOR, where=BOT_ONLY, private=True, off=True),
            'switched off; for moderators and admins only; in bot channels only; '
            'answers only the person who uses it',
        ),
        (Limit(), 'no change'),
    ],
)
def test_describe_limit(limit: Limit, text: str) -> None:
    assert describe_limit(limit) == text


def test_describe_limit_reads_differently_for_every_limit() -> None:
    # As /help and /access both show it, two limits never look alike.
    limits = [
        Limit(who=who, where=where, private=private, off=off)
        for who in (None, *LIMIT_WHO)
        for where in (None, *LIMIT_WHERE)
        for private in (False, True)
        for off in (False, True)
    ]
    texts = {describe_limit(limit) for limit in limits}

    assert len(texts) == len(limits) == 100
    for text in texts:
        assert text[:1].islower() and not text.endswith('.'), text
