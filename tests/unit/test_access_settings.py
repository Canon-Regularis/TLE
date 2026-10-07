"""Tests for tle.access.settings: a server's settings and how they are stored.

Decoding must fail closed: whatever a row holds, it never raises, and what it
can't trust reads tighter, never looser.
"""

import itertools
import json
import random
from typing import Any

import pytest

from tle.access.rules import LIMIT_WHERE, LIMIT_WHO, Limit, Where, Who
from tle.access.settings import (
    MAX_BOT_CHANNELS,
    VERSION,
    GuildAccess,
    decode,
    encode,
)

CHANNEL = 123456789012345678  # a realistic Discord id
BIGGEST_ID = 2**64 - 1


def row(**changes: Any) -> str:
    """The text of a valid row with no settings, with ``changes`` made to it."""
    data: dict[str, Any] = {
        'version': 1,
        'bot_channels': [],
        'staff_channel': None,
        'limits': {},
    }
    data.update(changes)
    return json.dumps(data)


def without(field: str) -> str:
    data = json.loads(row())
    del data[field]
    return json.dumps(data)


def stored_limit(**changes: Any) -> dict[str, Any]:
    """A stored limit that changes nothing, with ``changes`` made to it."""
    data: dict[str, Any] = {'who': None, 'where': None, 'private': False, 'off': False}
    data.update(changes)
    return data


# --- GuildAccess ---


def test_the_defaults() -> None:
    access = GuildAccess()

    assert access.bot_channels == frozenset()
    assert access.staff_channel is None
    assert access.limits == {}
    assert not access.broken
    assert VERSION == 1
    assert MAX_BOT_CHANNELS == 25


def test_limits_are_read_only() -> None:
    access = GuildAccess(limits={'duel': Limit(off=True)})

    with pytest.raises(TypeError):
        access.limits['gitgud'] = Limit(off=True)  # type: ignore[index]


def test_limits_are_copied_from_the_callers_dict() -> None:
    limits = {'duel': Limit(off=True)}
    access = GuildAccess(limits=limits)

    limits['gitgud'] = Limit(off=True)

    assert dict(access.limits) == {'duel': Limit(off=True)}


def test_empty_limits_are_left_out() -> None:
    access = GuildAccess(limits={'duel': Limit(), 'gitgud': Limit(off=True)})

    assert dict(access.limits) == {'gitgud': Limit(off=True)}
    assert access == GuildAccess(limits={'gitgud': Limit(off=True)})


def test_settings_compare_and_hash_by_value() -> None:
    first = GuildAccess({1, 2}, 3, {'duel': Limit(off=True)})  # type: ignore[arg-type]
    second = GuildAccess(frozenset({2, 1}), 3, {'duel': Limit(off=True)})

    assert isinstance(first.bot_channels, frozenset)
    assert first == second
    assert hash(first) == hash(second)
    assert first != GuildAccess(frozenset({1, 2}), 3, {'duel': Limit(private=True)})
    assert first != second.without_limits()


@pytest.mark.parametrize(
    'key',
    [
        'gitgud',
        'duel register',
        'duel *',
        'kcpc contests add',
        'kcpc contests add *',
        'access bot-channels',
        'set_ratedvc_channel',
        'event this-week',
    ],
)
def test_good_limit_keys(key: str) -> None:
    limit = Limit(off=True)

    assert GuildAccess(limits={key: limit}).limits == {key: limit}
    assert GuildAccess().with_limit(key, limit).limits == {key: limit}


@pytest.mark.parametrize(
    'key',
    [
        '',
        ' ',
        '*',
        ' duel',
        'duel ',
        'duel  register',
        'duel\tregister',
        'duel*',
        'duel **',
        'duel * register',
        '* duel',
    ],
)
def test_bad_limit_keys(key: str) -> None:
    with pytest.raises(ValueError, match='Invalid limit key'):
        GuildAccess(limits={key: Limit(off=True)})
    with pytest.raises(ValueError, match='Invalid limit key'):
        GuildAccess().with_limit(key, Limit(off=True))
    with pytest.raises(ValueError, match='Invalid limit key'):
        GuildAccess().with_limit(key, None)


def test_with_limit_adds_replaces_and_removes() -> None:
    start = GuildAccess(frozenset({1}), 2)

    added = start.with_limit('duel *', Limit(who=Who.TRUSTED))
    assert added.limits == {'duel *': Limit(who=Who.TRUSTED)}
    assert (added.bot_channels, added.staff_channel) == (frozenset({1}), 2)

    replaced = added.with_limit('duel *', Limit(off=True))
    assert replaced.limits == {'duel *': Limit(off=True)}

    both = replaced.with_limit('duel', Limit(where=Where.STAFF))
    assert both.limits == {'duel *': Limit(off=True), 'duel': Limit(where=Where.STAFF)}

    assert both.with_limit('duel *', None).limits == {'duel': Limit(where=Where.STAFF)}
    assert both.with_limit('duel', Limit()).limits == {'duel *': Limit(off=True)}
    assert both.with_limit('gitgud', None) == both

    # Each call made new settings and left the old ones alone.
    assert start.limits == {}
    assert added.limits == {'duel *': Limit(who=Who.TRUSTED)}


def test_with_limit_keeps_broken_settings_broken() -> None:
    broken = GuildAccess(broken=True)

    assert broken.with_limit('duel', Limit(off=True)).broken


def test_without_limits_keeps_the_channels() -> None:
    access = GuildAccess(frozenset({1, 2}), 3, {'duel': Limit(off=True)})

    assert access.without_limits() == GuildAccess(frozenset({1, 2}), 3)


def test_without_limits_repairs_broken_settings() -> None:
    repaired = GuildAccess(broken=True).without_limits()

    assert repaired == GuildAccess()
    assert not repaired.broken
    assert decode(encode(repaired)) == (GuildAccess(), ())


# --- encode ---


def test_encode_writes_versioned_json() -> None:
    access = GuildAccess(
        frozenset({30, 10, 20}),
        40,
        {
            'gitgud': Limit(off=True),
            'duel *': Limit(who=Who.TRUSTED, where=Where.BOT_ONLY, private=True),
        },
    )

    data = json.loads(encode(access))

    assert data == {
        'version': 1,
        'bot_channels': [10, 20, 30],
        'staff_channel': 40,
        'limits': {
            'duel *': {
                'who': 'trusted',
                'where': 'bot-only',
                'private': True,
                'off': False,
            },
            'gitgud': {'who': None, 'where': None, 'private': False, 'off': True},
        },
    }
    assert list(data['limits']) == ['duel *', 'gitgud']


def test_encode_writes_the_defaults() -> None:
    assert json.loads(encode(GuildAccess())) == {
        'version': 1,
        'bot_channels': [],
        'staff_channel': None,
        'limits': {},
    }


def test_encode_is_the_same_whatever_the_order_things_were_added_in() -> None:
    limits = [('b', Limit(off=True)), ('a *', Limit(private=True)), ('c', Limit())]
    first, second = GuildAccess(frozenset({3, 1, 2})), GuildAccess(frozenset({2, 3, 1}))
    for key, limit in limits:
        first = first.with_limit(key, limit)
    for key, limit in reversed(limits):
        second = second.with_limit(key, limit)

    assert encode(first) == encode(second)


def test_encode_refuses_broken_settings() -> None:
    with pytest.raises(ValueError, match='never stored'):
        encode(GuildAccess(broken=True))


EVERY_LIMIT = {
    f'command{index}': Limit(who=who, where=where, private=private, off=off)
    for index, (who, where, private, off) in enumerate(
        itertools.product(
            (None, *LIMIT_WHO), (None, *LIMIT_WHERE), (False, True), (False, True)
        )
    )
}


@pytest.mark.parametrize(
    'access',
    [
        GuildAccess(),
        GuildAccess(bot_channels=frozenset({CHANNEL})),
        GuildAccess(staff_channel=CHANNEL),
        GuildAccess(frozenset(range(1, MAX_BOT_CHANNELS + 1)), BIGGEST_ID),
        GuildAccess(
            frozenset({CHANNEL}),
            CHANNEL + 1,
            {
                'duel *': Limit(who=Who.MODERATOR),
                'duel register': Limit(where=Where.STAFF_ONLY, private=True),
                'gitgud': Limit(off=True),
                'kcpc contests platforms *': Limit(who=Who.ADMIN),
            },
        ),
        GuildAccess(limits=EVERY_LIMIT),
    ],
)
def test_round_trip(access: GuildAccess) -> None:
    assert decode(encode(access)) == (access, ())


def test_every_limit_value_is_stored() -> None:
    # Every who and where, and both flags, except the one empty combination.
    assert len(GuildAccess(limits=EVERY_LIMIT).limits) == 5 * 5 * 2 * 2 - 1


# --- decode: rows that can't be read ---


def test_no_row_is_the_defaults() -> None:
    assert decode(None) == (GuildAccess(), ())


def test_an_empty_row_is_the_defaults() -> None:
    assert decode(row()) == (GuildAccess(), ())


LIMIT_TEXT = '{"who": null, "where": null, "private": false, "off": true}'


@pytest.mark.parametrize(
    ('text', 'problem'),
    [
        ('', 'not valid JSON'),
        ('{', 'not valid JSON'),
        ('not json', 'not valid JSON'),
        (row()[:-1], 'not valid JSON'),
        pytest.param('[' * 50_000, 'not valid JSON', id='deeply-nested'),
        ('[]', 'not a JSON object'),
        ('"text"', 'not a JSON object'),
        ('1', 'not a JSON object'),
        ('null', 'not a JSON object'),
        ('true', 'not a JSON object'),
        ('{}', 'no version'),
        (without('version'), 'no version'),
        (row(version=0), 'version 0,'),
        (row(version=2), 'version 2,'),
        (row(version='1'), "version '1',"),
        (row(version=True), 'version True,'),
        (row(version=1.0), 'version 1.0,'),
        (row(version=None), 'version None,'),
        (without('limits'), 'no limits'),
        (row(limits=[]), 'limits is [], not an object'),
        (row(limits=None), 'limits is None, not an object'),
        (row(limits='duel'), "limits is 'duel', not an object"),
        # A repeated key could undo a limit, so the row can't be trusted at all.
        (
            '{"version": 1, "bot_channels": [], "staff_channel": null, '
            '"limits": {}, "version": 1}',
            "the key 'version' repeats",
        ),
        (
            '{"version": 1, "bot_channels": [], "staff_channel": null, '
            f'"limits": {{"duel": {LIMIT_TEXT}, "duel": {LIMIT_TEXT}}}}}',
            "the key 'duel' repeats",
        ),
        (
            '{"version": 1, "bot_channels": [], "staff_channel": null, '
            '"limits": {"duel": {"who": null, "where": null, "private": false, '
            '"off": true, "off": false}}}',
            "the key 'off' repeats",
        ),
    ],
)
def test_an_unreadable_row_is_broken(text: str, problem: str) -> None:
    access, warnings = decode(text)

    assert access == GuildAccess(broken=True)
    assert access.broken
    assert len(warnings) == 1
    assert problem in warnings[0]


def test_a_long_unreadable_row_is_cut_short_in_its_warning() -> None:
    _, warnings = decode('x' * 10_000)

    assert len(warnings[0]) < 300


# --- decode: bad parts of a readable row ---


@pytest.mark.parametrize(
    ('stored', 'kept', 'dropped'),
    [
        ([1, 2], {1, 2}, None),
        ([2, 1, 2], {1, 2}, None),
        ([BIGGEST_ID], {BIGGEST_ID}, None),
        ([1, True], {1}, '[True]'),
        ([1, False], {1}, '[False]'),
        ([1, '2'], {1}, "['2']"),
        ([1, 2.0], {1}, '[2.0]'),
        ([0, -5, 3], {3}, '[0, -5]'),
        ([2**64], set(), f'[{2**64}]'),
        ([None, [3], {'id': 4}], set(), "[None, [3], {'id': 4}]"),
    ],
)
def test_bad_bot_channels_are_dropped(
    stored: list[Any], kept: set[int], dropped: str | None
) -> None:
    access, warnings = decode(row(bot_channels=stored, staff_channel=CHANNEL))

    assert access == GuildAccess(frozenset(kept), CHANNEL)
    if dropped is None:
        assert warnings == ()
    else:
        assert warnings == (f'bot_channels: dropped {dropped}, not channel ids',)


@pytest.mark.parametrize('stored', [5, '1,2', {'1': 1}, None, True])
def test_bot_channels_that_are_not_a_list_are_none(stored: Any) -> None:
    access, warnings = decode(row(bot_channels=stored, staff_channel=CHANNEL))

    assert access == GuildAccess(frozenset(), CHANNEL)
    assert len(warnings) == 1
    assert warnings[0].startswith(f'bot_channels is {stored!r}, not a list')


def test_missing_bot_channels_are_none() -> None:
    access, warnings = decode(without('bot_channels'))

    assert access == GuildAccess()
    assert warnings == ('bot_channels is missing: no bot channels',)


def test_a_staff_channel_is_read() -> None:
    assert decode(row(staff_channel=CHANNEL)) == (
        GuildAccess(staff_channel=CHANNEL),
        (),
    )


@pytest.mark.parametrize('stored', [True, False, '123', 1.5, 0, -1, 2**64, [1], {}])
def test_a_bad_staff_channel_is_cleared(stored: Any) -> None:
    access, warnings = decode(row(bot_channels=[1], staff_channel=stored))

    assert access == GuildAccess(frozenset({1}), None)
    assert warnings == (
        f'staff_channel is {stored!r}, not a channel id: no staff channel',
    )


def test_a_missing_staff_channel_is_cleared() -> None:
    access, warnings = decode(without('staff_channel'))

    assert access == GuildAccess()
    assert warnings == ('staff_channel is missing: no staff channel',)


def test_limits_are_read() -> None:
    limits = {
        'duel *': stored_limit(who='moderator', where='staff-only'),
        'gitgud': stored_limit(private=True),
        'meta ping': stored_limit(off=True),
        'kcpc': stored_limit(who='developer', where='bot', private=True, off=True),
    }

    access, warnings = decode(row(limits=limits))

    assert warnings == ()
    assert access.limits == {
        'duel *': Limit(who=Who.MODERATOR, where=Where.STAFF_ONLY),
        'gitgud': Limit(private=True),
        'meta ping': Limit(off=True),
        'kcpc': Limit(Who.DEVELOPER, Where.BOT, private=True, off=True),
    }


def test_a_stored_limit_that_changes_nothing_is_left_out() -> None:
    assert decode(row(limits={'duel': stored_limit()})) == (GuildAccess(), ())


@pytest.mark.parametrize(
    ('stored', 'problem'),
    [
        (stored_limit(who='owner'), "who is 'owner'"),
        (stored_limit(who='everyone'), "who is 'everyone'"),
        (stored_limit(who='Admin'), "who is 'Admin'"),
        (stored_limit(who=1), 'who is 1'),
        (stored_limit(who=True), 'who is True'),
        (stored_limit(where='anywhere'), "where is 'anywhere'"),
        (stored_limit(where='STAFF'), "where is 'STAFF'"),
        (stored_limit(where=['bot']), "where is ['bot']"),
        (stored_limit(private=1), 'private is 1, not true or false'),
        (stored_limit(private='true'), "private is 'true', not true or false"),
        (stored_limit(private=None), 'private is None, not true or false'),
        (stored_limit(off=0), 'off is 0, not true or false'),
        (stored_limit(off=None), 'off is None, not true or false'),
        ({'who': 'admin'}, 'no where, private, off'),
        ({}, 'no who, where, private, off'),
        ({**stored_limit(), 'expires': 5}, 'the unknown fields expires'),
        ([], '[] is not an object'),
        ('off', "'off' is not an object"),
        (None, 'None is not an object'),
        (True, 'True is not an object'),
    ],
)
def test_a_bad_limit_switches_its_command_off(stored: Any, problem: str) -> None:
    limits = {'duel register': stored, 'gitgud': stored_limit(where='bot-only')}

    access, warnings = decode(row(limits=limits))

    assert access.limits == {
        'duel register': Limit(off=True),
        'gitgud': Limit(where=Where.BOT_ONLY),
    }
    assert warnings == (f"limit 'duel register': {problem}; switched off",)


def test_every_problem_of_a_limit_is_named() -> None:
    stored = {'who': 'owner', 'where': 'nowhere', 'private': 1, 'surprise': True}

    _, warnings = decode(row(limits={'duel': stored}))

    assert warnings == (
        "limit 'duel': no off; the unknown fields surprise; who is 'owner'; "
        "where is 'nowhere'; private is 1, not true or false; switched off",
    )


@pytest.mark.parametrize(
    ('stored', 'key'),
    [
        ('duel  register', 'duel register'),
        (' duel *', 'duel *'),
        ('duel\tregister ', 'duel register'),
        ('kcpc contests  *', 'kcpc contests *'),
    ],
)
def test_a_key_with_stray_spaces_switches_its_command_off(
    stored: str, key: str
) -> None:
    access, warnings = decode(row(limits={stored: stored_limit(who='admin')}))

    assert access.limits == {key: Limit(off=True)}
    assert warnings == (f'limit {stored!r}: the key should be {key!r}; switched off',)


@pytest.mark.parametrize('stored', ['', '   ', '*', 'duel**', 'duel * x', ' * '])
def test_a_key_that_names_no_command_is_dropped(stored: str) -> None:
    limits = {stored: stored_limit(off=True), 'gitgud': stored_limit(off=True)}

    access, warnings = decode(row(limits=limits))

    assert access.limits == {'gitgud': Limit(off=True)}
    assert warnings == (f'limit {stored!r}: not a command name, dropped',)


@pytest.mark.parametrize('first', ['duel register', 'duel  register'])
def test_two_keys_for_one_command_switch_it_off(first: str) -> None:
    # Whichever comes first, the result is never the looser of the two.
    second = 'duel  register' if first == 'duel register' else 'duel register'
    limits = {first: stored_limit(who='admin'), second: stored_limit(who='trusted')}

    access, warnings = decode(row(limits=limits))

    assert access.limits == {'duel register': Limit(off=True)}
    assert any('another key names the same command' in each for each in warnings)


def test_unknown_fields_are_ignored_with_a_warning() -> None:
    access, warnings = decode(row(staff_channel=CHANNEL, zebra=1, apple=[2]))

    assert access == GuildAccess(staff_channel=CHANNEL)
    assert warnings == ('ignored the unknown fields apple, zebra',)


def test_each_bad_part_gets_its_own_warning() -> None:
    text = row(
        bot_channels=[1, 'x'],
        staff_channel='y',
        limits={'duel': stored_limit(who='owner'), 'gitgud': stored_limit(off=True)},
        extra=True,
    )

    access, warnings = decode(text)

    assert access == GuildAccess(
        frozenset({1}),
        None,
        {'duel': Limit(off=True), 'gitgud': Limit(off=True)},
    )
    assert len(warnings) == 4


# --- decode never raises ---

ODD_VALUES: list[Any] = [
    None,
    True,
    False,
    0,
    1,
    -1,
    2**64,
    1.5,
    '',
    '1',
    'bot',
    'staff-only',
    'admin',
    'owner',
    [],
    {},
    [1, 2],
    {'who': 'admin'},
]
ODD_KEYS = ['version', 'limits', 'who', 'where', 'private', 'off', 'x', '*', 'a  b']


def odd_value(rng: random.Random, depth: int = 0) -> Any:
    if depth >= 2 or rng.random() < 0.7:
        return rng.choice([*ODD_VALUES, rng.randrange(1, 2**64)])
    if rng.random() < 0.5:
        return [odd_value(rng, depth + 1) for _ in range(rng.randrange(3))]
    return {rng.choice(ODD_KEYS): odd_value(rng, depth + 1) for _ in range(3)}


def mangled_row(rng: random.Random) -> str:
    """A valid row with a few random parts replaced, removed or added, and
    sometimes cut short.
    """
    limits = {
        key: stored_limit(
            who=rng.choice([None, *(who.value for who in LIMIT_WHO)]),
            where=rng.choice([None, *(where.value for where in LIMIT_WHERE)]),
            private=rng.random() < 0.3,
            off=rng.random() < 0.3,
        )
        for key in rng.sample(['duel', 'duel *', 'gitgud', 'kcpc weekly *'], 2)
    }
    data: dict[str, Any] = json.loads(
        row(
            bot_channels=[rng.randrange(1, 2**63) for _ in range(rng.randrange(4))],
            staff_channel=rng.choice([None, rng.randrange(1, 2**63)]),
            limits=limits,
        )
    )
    entries = list(data['limits'].values())  # each limit stored in the row
    for _ in range(rng.randrange(4)):
        target = data if rng.random() < 0.5 else rng.choice(entries)
        key = rng.choice([*target, *ODD_KEYS])
        if rng.random() < 0.2:
            target.pop(key, None)
        else:
            target[key] = odd_value(rng)
    text = json.dumps(data)
    if rng.random() < 0.1:
        text = text[: rng.randrange(len(text))]
    return text


def test_decode_never_raises_and_what_it_reads_can_be_stored() -> None:
    rng = random.Random(20261006)
    broken = readable = 0

    for _ in range(3000):
        text = mangled_row(rng)

        access, warnings = decode(text)

        assert all(isinstance(warning, str) and warning for warning in warnings)
        if access.broken:
            broken += 1
            assert access == GuildAccess(broken=True)
            assert len(warnings) == 1
        else:
            readable += 1
            assert decode(encode(access)) == (access, ())

    # Both kinds of row came up often enough for the test to mean something.
    assert broken > 100
    assert readable > 1000
