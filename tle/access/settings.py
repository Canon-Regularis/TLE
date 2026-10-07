"""A server's access settings, and how they are stored.

A server's bot channels, staff channel and limits are stored together as one
JSON row. Reading a row fails closed. A row that can't be read at all makes
the server's settings ``broken``: its commands are then refused until an admin
resets them. A readable row with a bad part is read in the way that tightens:
a bad bot channel is dropped, a bad staff channel is cleared, and a bad limit
switches its command off.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, TypeGuard, TypeVar

from tle.access.rules import LIMIT_WHERE, LIMIT_WHO, Limit, Where, Who

VERSION = 1
# The most bot channels an admin can choose for a server.
MAX_BOT_CHANNELS = 25

# A limit's key: a command's name, such as 'duel register', which limits that
# command alone, or the name and ' *', such as 'duel *', which limits the
# command and all its subcommands.
_KEY = re.compile(r'[^\s*]+(?: [^\s*]+)*(?: \*)?')


def _no_limits() -> Mapping[str, Limit]:
    return MappingProxyType({})


@dataclass(frozen=True)
class GuildAccess:
    """One server's access settings.

    ``limits`` is read-only and holds no empty limit. Its keys are canonical
    command names, alone or followed by ' *'; any other key is a ``ValueError``.
    ``broken`` means that the stored row could not be read, so the rest is
    empty; a broken row is replaced, never stored again.
    """

    bot_channels: frozenset[int] = frozenset()
    staff_channel: int | None = None
    limits: Mapping[str, Limit] = field(default_factory=_no_limits, hash=False)
    broken: bool = False

    def __post_init__(self) -> None:
        for key in self.limits:
            _check_key(key)
        # A copy, so that the caller's dict can't change these settings later.
        limits = {
            key: limit for key, limit in self.limits.items() if not limit.is_empty
        }
        object.__setattr__(self, 'bot_channels', frozenset(self.bot_channels))
        object.__setattr__(self, 'limits', MappingProxyType(limits))

    def with_limit(self, key: str, limit: Limit | None) -> 'GuildAccess':
        """These settings with ``limit`` under ``key``.

        None or an empty limit removes the key's limit. Broken settings stay
        broken: what their row held is still unknown.
        """
        _check_key(key)
        limits = dict(self.limits)
        if limit is None or limit.is_empty:
            limits.pop(key, None)
        else:
            limits[key] = limit
        return replace(self, limits=limits)

    def without_limits(self) -> 'GuildAccess':
        """These settings with no limits.

        This also repairs broken settings: with no channels and no limits,
        nothing in them is unknown any more.
        """
        return GuildAccess(self.bot_channels, self.staff_channel)


def _check_key(key: str) -> None:
    if not _KEY.fullmatch(key):
        raise ValueError(
            f"Invalid limit key {key!r}: use a command's name, alone or followed "
            "by ' *'"
        )


def encode(access: GuildAccess) -> str:
    """``access`` as the JSON row stored for its server.

    ``ValueError`` if ``access`` is broken: a row that couldn't be read is
    replaced, never written back.
    """
    if access.broken:
        raise ValueError('Broken access settings are replaced, never stored')
    data = {
        'version': VERSION,
        'bot_channels': sorted(access.bot_channels),
        'staff_channel': access.staff_channel,
        'limits': {
            key: _encode_limit(access.limits[key]) for key in sorted(access.limits)
        },
    }
    return json.dumps(data)


def _encode_limit(limit: Limit) -> dict[str, Any]:
    return {
        'who': None if limit.who is None else limit.who.value,
        'where': None if limit.where is None else limit.where.value,
        'private': limit.private,
        'off': limit.off,
    }


_FIELDS = ('version', 'bot_channels', 'staff_channel', 'limits')
_LIMIT_FIELDS = ('who', 'where', 'private', 'off')
_WHO = {who.value: who for who in LIMIT_WHO}
_WHERE = {where.value: where for where in LIMIT_WHERE}
_MISSING = object()
_SHOWN = 60  # the longest stored value that a warning repeats in full

_Choice = TypeVar('_Choice', Who, Where)


def decode(text: str | None) -> tuple[GuildAccess, tuple[str, ...]]:
    """The settings stored as ``text``, and a warning for each problem found.

    Never raises. None, for a server with no row yet, gives the defaults. A
    row that isn't a JSON object of this version, or whose limits can't be
    read at all, gives broken settings, since ignoring what can't be read
    could loosen them. Every other bad part is read in the way that tightens,
    and an unknown field is ignored, each with a warning.
    """
    if text is None:
        return GuildAccess(), ()
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    except (TypeError, ValueError, RecursionError) as exc:
        return _broken(f'not valid JSON ({exc}): {_shown(text)}')
    if not isinstance(data, dict):
        return _broken(f'not a JSON object: {_shown(text)}')
    version = data.get('version', _MISSING)
    if version is _MISSING:
        return _broken('no version')
    if not _is_int(version) or version != VERSION:
        return _broken(f'version {_shown(version)}, but this bot reads {VERSION}')
    limits = data.get('limits', _MISSING)
    if limits is _MISSING:
        return _broken('no limits')
    if not isinstance(limits, dict):
        return _broken(f'limits is {_shown(limits)}, not an object')
    warnings: list[str] = []
    unknown = sorted(set(data) - set(_FIELDS))
    if unknown:
        warnings.append(f'ignored the unknown fields {", ".join(unknown)}')
    bot_channels = _decode_bot_channels(data.get('bot_channels', _MISSING), warnings)
    staff_channel = _decode_staff_channel(data.get('staff_channel', _MISSING), warnings)
    access = GuildAccess(bot_channels, staff_channel, _decode_limits(limits, warnings))
    return access, tuple(warnings)


def _broken(problem: str) -> tuple[GuildAccess, tuple[str, ...]]:
    return GuildAccess(broken=True), (problem,)


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object as a dict.

    ``ValueError`` if a key repeats, rather than letting the last copy win:
    the encoder never repeats one, and a later copy could undo a limit.
    """
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError(f'the key {key!r} repeats')
        data[key] = value
    return data


def _decode_bot_channels(value: object, warnings: list[str]) -> frozenset[int]:
    if not isinstance(value, list):
        shown = 'missing' if value is _MISSING else f'{_shown(value)}, not a list'
        warnings.append(f'bot_channels is {shown}: no bot channels')
        return frozenset()
    bad = [item for item in value if not _is_channel_id(item)]
    if bad:
        warnings.append(f'bot_channels: dropped {_shown(bad)}, not channel ids')
    return frozenset(item for item in value if _is_channel_id(item))


def _decode_staff_channel(value: object, warnings: list[str]) -> int | None:
    if value is None:
        return None
    if _is_channel_id(value):
        return value
    shown = 'missing' if value is _MISSING else f'{_shown(value)}, not a channel id'
    warnings.append(f'staff_channel is {shown}: no staff channel')
    return None


def _decode_limits(data: dict[str, Any], warnings: list[str]) -> dict[str, Limit]:
    limits: dict[str, Limit] = {}
    for raw_key, value in data.items():
        key = ' '.join(raw_key.split())
        if not _KEY.fullmatch(key):
            warnings.append(f'limit {_shown(raw_key)}: not a command name, dropped')
            continue
        limit, problems = _decode_limit(value)
        if key != raw_key:
            problems.append(f'the key should be {key!r}')
        if key in limits:
            problems.append('another key names the same command')
        if problems:
            joined = '; '.join(problems)
            warnings.append(f'limit {_shown(raw_key)}: {joined}; switched off')
            limit = Limit(off=True)
        limits[key] = limit
    return limits


def _decode_limit(value: object) -> tuple[Limit, list[str]]:
    """A stored limit and what is wrong with it; if anything is, the limit is
    empty and the caller switches the command off instead.
    """
    if not isinstance(value, dict):
        return Limit(), [f'{_shown(value)} is not an object']
    problems: list[str] = []
    missing = [name for name in _LIMIT_FIELDS if name not in value]
    if missing:
        problems.append(f'no {", ".join(missing)}')
    unknown = sorted(set(value) - set(_LIMIT_FIELDS))
    if unknown:
        problems.append(f'the unknown fields {", ".join(unknown)}')
    who = _decode_choice(value, 'who', _WHO, problems)
    where = _decode_choice(value, 'where', _WHERE, problems)
    private = _decode_flag(value, 'private', problems)
    off = _decode_flag(value, 'off', problems)
    if problems:
        return Limit(), problems
    return Limit(who=who, where=where, private=private, off=off), problems


def _decode_choice(
    value: dict[str, Any],
    name: str,
    choices: Mapping[str, _Choice],
    problems: list[str],
) -> _Choice | None:
    raw = value.get(name)
    if raw is None:
        return None
    if isinstance(raw, str) and raw in choices:
        return choices[raw]
    problems.append(f'{name} is {_shown(raw)}')
    return None


def _decode_flag(value: dict[str, Any], name: str, problems: list[str]) -> bool:
    raw = value.get(name, False)
    if isinstance(raw, bool):
        return raw
    problems.append(f'{name} is {_shown(raw)}, not true or false')
    return False


def _is_int(value: object) -> TypeGuard[int]:
    """Whether ``value`` is an int; JSON's true and false never count as 1 and 0."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_channel_id(value: object) -> TypeGuard[int]:
    return _is_int(value) and 0 < value < 2**64


def _shown(value: object) -> str:
    """``value`` for a warning, cut short if long."""
    text = repr(value)
    return text if len(text) <= _SHOWN else f'{text[: _SHOWN - 3]}...'
