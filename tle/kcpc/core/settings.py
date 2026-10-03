"""Per-guild feature settings, stored as JSON in ``guild_settings``.

Each feature registers a ``FeatureSpec`` naming its settings dataclass: a frozen
subclass of ``FeatureSettings`` whose fields all have defaults. Loading is
tolerant, so a settings class can gain or lose fields without a migration:
unknown keys are ignored when loading and kept when updating, and a missing or
invalid value falls back to the field's default. Supported field types are
bool, int, str, ``X | None`` and ``tuple[X, ...]`` of those.
"""

import json
import logging
import re
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import MISSING, asdict, dataclass, fields, replace
from operator import itemgetter
from typing import Any, TypeVar, cast

from tle.kcpc.core.clock import Clock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.timeutil import to_epoch

logger = logging.getLogger(__name__)

S = TypeVar('S', bound='FeatureSettings')


@dataclass(frozen=True)
class FeatureSettings:
    """The settings every feature has; subclass it to add more."""

    enabled: bool = False
    channel_id: int | None = None
    role_id: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls: type[S], raw: str | None) -> S:
        """Decode stored settings, never raising.

        None gives the defaults. Invalid JSON, or JSON that is not an object,
        also gives the defaults; unknown keys are ignored; and a value of the
        wrong type falls back to that field's default, with a warning.
        """
        settings, problems = _decode_settings(cls, raw)
        if problems:
            logger.warning(
                'Invalid stored %s (defaults used instead): %s',
                cls.__name__,
                '; '.join(problems),
            )
        return settings


_FEATURE_KEY = re.compile(r'[a-z][a-z0-9-]*')


@dataclass(frozen=True)
class FeatureSpec:
    """A feature whose settings each guild can store."""

    key: str  # lowercase letters, digits and hyphens, starting with a letter
    title: str
    description: str
    settings_type: type[FeatureSettings] = FeatureSettings

    def __post_init__(self) -> None:
        if not _FEATURE_KEY.fullmatch(self.key):
            raise ValueError(
                f'Invalid feature key {self.key!r}: use lowercase letters, digits '
                'and hyphens, starting with a letter'
            )
        _field_parsers(self.settings_type)  # fail now if the type can't be stored


class FeatureRegistry:
    """The features whose settings can be stored, by key."""

    def __init__(self) -> None:
        self._specs: dict[str, FeatureSpec] = {}

    def register(self, spec: FeatureSpec, *, replace: bool = False) -> None:
        if spec.key in self._specs and not replace:
            raise ValueError(f'Feature {spec.key!r} is already registered')
        self._specs[spec.key] = spec

    def get(self, key: str) -> FeatureSpec:
        """The feature's spec; ``KcpcUserError`` listing the known ones if unknown."""
        spec = self._specs.get(key)
        if spec is None:
            known = ', '.join(self.keys()) or 'none'
            raise KcpcUserError(f"Unknown feature '{key}'. Known features: {known}.")
        return spec

    def keys(self) -> list[str]:
        return sorted(self._specs)

    def all(self) -> list[FeatureSpec]:
        return [self._specs[key] for key in self.keys()]

    def __contains__(self, key: object) -> bool:
        return key in self._specs


_PLANNED_FEATURES = (
    ('algo', 'Algorithm of the month', 'Monthly data structure / algorithm pick'),
    ('contests', 'Contests', 'Contest reminders and results'),
    ('weekly', 'Weekly problem', 'Friday problem, solution the Friday after'),
    ('workshops', 'Workshops', 'Luma workshop reminders, 24h and 1h before'),
)


def default_registry() -> FeatureRegistry:
    """The planned features, with base settings until each gets its own type."""
    registry = FeatureRegistry()
    for key, title, description in _PLANNED_FEATURES:
        registry.register(FeatureSpec(key, title, description))
    return registry


_SELECT_ONE = 'SELECT data FROM guild_settings WHERE guild_id = ? AND feature = ?'
_UPSERT = """
    INSERT INTO guild_settings (guild_id, feature, data, updated_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT (guild_id, feature)
    DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at
"""


class GuildSettingsRepo:
    """Reads and writes each guild's settings for the registered features."""

    def __init__(self, db: Database, clock: Clock, registry: FeatureRegistry) -> None:
        self._db = db
        self._clock = clock
        self._registry = registry
        # Invalid stored settings are reported once per distinct row content, not
        # on every read: jobs read settings every few minutes.
        self._reported: set[tuple[int, str, str | None]] = set()

    @property
    def registry(self) -> FeatureRegistry:
        return self._registry

    async def get(self, guild_id: int, feature: str) -> FeatureSettings:
        """The guild's settings for ``feature``, as the feature's settings type."""
        return await self._load(guild_id, self._registry.get(feature))

    async def get_typed(self, guild_id: int, feature: str, settings_type: type[S]) -> S:
        """``get``, checked to be a ``settings_type`` (else ``TypeError``)."""
        spec = self._registry.get(feature)
        if not issubclass(spec.settings_type, settings_type):
            raise TypeError(
                f'{feature!r} settings are {spec.settings_type.__name__}, '
                f'not {settings_type.__name__}'
            )
        # _load builds spec.settings_type, which is settings_type or a subclass.
        return cast(S, await self._load(guild_id, spec))

    async def update(
        self, guild_id: int, feature: str, **changes: Any
    ) -> FeatureSettings:
        """Change some fields of the guild's settings and return the result.

        An unknown field name raises ``ValueError`` and a value the field cannot
        hold raises ``TypeError``; nothing is written in either case.

        Stored keys that the registered settings type lacks are kept. So a run
        that registers a narrower type (the base type while the feature's
        extension is disabled, or an older release after a rollback) never
        erases the settings of a wider one.
        """
        spec = self._registry.get(feature)
        values = _checked_changes(spec.settings_type, changes)
        async with self._db.transaction():
            raw = await self._db.fetchval(_SELECT_ONE, (str(guild_id), spec.key))
            updated = replace(self._decode(guild_id, spec, raw), **values)
            await self._db.execute(
                _UPSERT,
                (
                    str(guild_id),
                    spec.key,
                    _merged_json(raw, updated),
                    to_epoch(self._clock.now()),
                ),
            )
        return updated

    async def all_for_guild(self, guild_id: int) -> dict[str, FeatureSettings]:
        """Settings for every registered feature, by key.

        Stored settings of features that are no longer registered are ignored.
        """
        rows = await self._db.fetchall(
            'SELECT feature, data FROM guild_settings WHERE guild_id = ?',
            (str(guild_id),),
        )
        stored = {row['feature']: row['data'] for row in rows}
        return {
            spec.key: self._decode(guild_id, spec, stored.get(spec.key))
            for spec in self._registry.all()
        }

    async def enabled_guilds(self, feature: str) -> list[tuple[int, FeatureSettings]]:
        """The guilds with ``feature`` enabled and their settings, by guild id."""
        spec = self._registry.get(feature)
        rows = await self._db.fetchall(
            'SELECT guild_id, data FROM guild_settings WHERE feature = ?', (spec.key,)
        )
        enabled: list[tuple[int, FeatureSettings]] = []
        for row in rows:
            guild_id = int(row['guild_id'])
            settings = self._decode(guild_id, spec, row['data'])
            if settings.enabled:
                enabled.append((guild_id, settings))
        return sorted(enabled, key=itemgetter(0))

    async def _load(self, guild_id: int, spec: FeatureSpec) -> FeatureSettings:
        raw = await self._db.fetchval(_SELECT_ONE, (str(guild_id), spec.key))
        return self._decode(guild_id, spec, raw)

    def _decode(
        self, guild_id: int, spec: FeatureSpec, raw: str | None
    ) -> FeatureSettings:
        settings, problems = _decode_settings(spec.settings_type, raw)
        memo = (guild_id, spec.key, raw)
        if problems and memo not in self._reported:
            self._reported.add(memo)
            logger.warning(
                'Invalid stored %s settings for guild %d (defaults used instead): %s',
                spec.key,
                guild_id,
                '; '.join(problems),
            )
        return settings


_INTEGER_TEXT = re.compile(r'[+-]?[0-9]+')

# Converts a JSON value, or an ``update`` argument, for one field; raises
# _InvalidValue if the value does not fit the field's type.
_Parser = Callable[[object], Any]


class _InvalidValue(ValueError):
    pass


def _decode_settings(settings_type: type[S], raw: str | None) -> tuple[S, list[str]]:
    """Settings decoded from ``raw``, plus a description of each problem found."""
    if raw is None:
        return settings_type(), []
    try:
        data = json.loads(raw)
    except ValueError:
        return settings_type(), [f'not valid JSON: {raw[:100]!r}']
    if not isinstance(data, dict):
        return settings_type(), [f'not a JSON object: {raw[:100]!r}']
    values: dict[str, Any] = {}
    problems: list[str] = []
    for name, parse in _field_parsers(settings_type).items():
        if name not in data:
            continue
        try:
            values[name] = parse(data[name])
        except _InvalidValue as exc:
            problems.append(f'{name} is {data[name]!r}, {exc}')
    return settings_type(**values), problems


def _merged_json(raw: str | None, settings: FeatureSettings) -> str:
    """``settings`` as JSON over the stored object, keeping the keys it lacks.

    Stored content that is not a JSON object is replaced outright.
    """
    data: dict[str, Any] = {}
    if raw is not None:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = None
        if isinstance(stored, dict):
            data.update(stored)
    data.update(asdict(settings))
    return json.dumps(data, sort_keys=True)


def _checked_changes(
    settings_type: type[FeatureSettings], changes: Mapping[str, Any]
) -> dict[str, Any]:
    """``update`` arguments, validated with the rules used for loading."""
    parsers = _field_parsers(settings_type)
    unknown = sorted(set(changes) - set(parsers))
    if unknown:
        raise ValueError(f'{settings_type.__name__} has no field {", ".join(unknown)}')
    checked: dict[str, Any] = {}
    for name, value in changes.items():
        try:
            checked[name] = parsers[name](value)
        except _InvalidValue as exc:
            raise TypeError(
                f'{settings_type.__name__}.{name} cannot be {value!r}: {exc}'
            ) from None
    return checked


def _field_parsers(settings_type: type[FeatureSettings]) -> Mapping[str, _Parser]:
    """A parser per field of ``settings_type``; ``TypeError`` if it can't be stored."""
    name = settings_type.__name__
    # A subclass without its own @dataclass would silently ignore its new fields.
    if '__dataclass_fields__' not in vars(settings_type):
        raise TypeError(f'{name} must be decorated with @dataclass(frozen=True)')
    hints = typing.get_type_hints(settings_type)
    parsers: dict[str, _Parser] = {}
    for field in fields(settings_type):
        if field.default is MISSING and field.default_factory is MISSING:
            raise TypeError(f'{name}.{field.name} needs a default')
        parsers[field.name] = _parser_for(hints[field.name])
    return parsers


def _parser_for(annotation: object) -> _Parser:
    scalar = _SCALAR_PARSERS.get(annotation)
    if scalar is not None:
        return scalar
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType) and len(args) == 2:
        if args[1] is types.NoneType:
            return _optional(_parser_for(args[0]))
        if args[0] is types.NoneType:
            return _optional(_parser_for(args[1]))
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        return _tuple_of(_parser_for(args[0]))
    raise TypeError(f'Unsupported settings field type: {annotation!r}')


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise _InvalidValue('expected true or false')


def _parse_int(value: object) -> int:
    # bool is a subclass of int, but true/false is never a valid id or count.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        return int(value)
    raise _InvalidValue('expected an integer')


def _parse_str(value: object) -> str:
    if isinstance(value, str):
        return value
    raise _InvalidValue('expected a string')


def _optional(parse: _Parser) -> _Parser:
    def parse_optional(value: object) -> Any:
        return None if value is None else parse(value)

    return parse_optional


def _tuple_of(parse: _Parser) -> _Parser:
    def parse_tuple(value: object) -> tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise _InvalidValue('expected a list')
        return tuple(parse(item) for item in value)

    return parse_tuple


_SCALAR_PARSERS: dict[object, _Parser] = {
    bool: _parse_bool,
    int: _parse_int,
    str: _parse_str,
}
