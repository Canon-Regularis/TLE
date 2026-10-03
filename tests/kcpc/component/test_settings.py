"""Tests for tle.kcpc.core.settings: tolerant loading, updates and the registry."""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import pytest

from tle.kcpc.core.clock import FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.errors import KcpcUserError
from tle.kcpc.core.settings import (
    FeatureRegistry,
    FeatureSettings,
    FeatureSpec,
    GuildSettingsRepo,
    default_registry,
)
from tle.kcpc.core.timeutil import to_epoch

GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
CHANNEL = 1_200_000_000_000_000_001
ROLE = 1_300_000_000_000_000_001


@dataclass(frozen=True)
class WorkshopSettings(FeatureSettings):
    calendar_id: str | None = None
    offsets_minutes: tuple[int, ...] = (1440, 60)


WORKSHOPS = FeatureSpec('workshops', 'Workshops', 'Luma reminders', WorkshopSettings)


@dataclass(frozen=True)
class RatioSettings(FeatureSettings):
    ratio: float = 0.5


@dataclass(frozen=True, kw_only=True)
class RequiredFieldSettings(FeatureSettings):
    calendar_id: str


class UndecoratedSettings(FeatureSettings):
    calendar_id: str = ''


def registry_with_workshop_settings() -> FeatureRegistry:
    registry = default_registry()
    registry.register(WORKSHOPS, replace=True)
    return registry


async def store(db: Database, guild_id: int, feature: str, data: str) -> None:
    """Write raw settings, bypassing the repository's validation."""
    await db.execute(
        'INSERT INTO guild_settings (guild_id, feature, data, updated_at) '
        'VALUES (?, ?, ?, 0) '
        'ON CONFLICT (guild_id, feature) DO UPDATE SET data = excluded.data',
        (str(guild_id), feature, data),
    )


async def stored(db: Database, guild_id: int, feature: str) -> object:
    raw = await db.fetchval(
        'SELECT data FROM guild_settings WHERE guild_id = ? AND feature = ?',
        (str(guild_id), feature),
    )
    return None if raw is None else json.loads(raw)


def settings_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == 'tle.kcpc.core.settings' and record.levelno == logging.WARNING
    ]


async def test_settings_default_until_updated_and_persist(
    guild_settings: GuildSettingsRepo,
    db: Database,
    clock: FakeClock,
    feature_registry: FeatureRegistry,
) -> None:
    assert await guild_settings.get(GUILD, 'workshops') == FeatureSettings()

    updated = await guild_settings.update(
        GUILD, 'workshops', enabled=True, channel_id=CHANNEL
    )
    assert updated == FeatureSettings(enabled=True, channel_id=CHANNEL)
    await clock.advance(timedelta(minutes=5))
    updated = await guild_settings.update(GUILD, 'workshops', role_id=ROLE)
    assert updated == FeatureSettings(enabled=True, channel_id=CHANNEL, role_id=ROLE)

    reloaded = GuildSettingsRepo(db, clock, feature_registry)
    assert await reloaded.get(GUILD, 'workshops') == updated
    assert await reloaded.get(GUILD, 'contests') == FeatureSettings()
    assert await reloaded.get(OTHER_GUILD, 'workshops') == FeatureSettings()
    assert await stored(db, GUILD, 'workshops') == {
        'channel_id': CHANNEL,
        'enabled': True,
        'role_id': ROLE,
    }
    assert await db.fetchval('SELECT updated_at FROM guild_settings') == to_epoch(
        clock.now()
    )


async def test_repository_exposes_its_registry(
    guild_settings: GuildSettingsRepo, feature_registry: FeatureRegistry
) -> None:
    assert guild_settings.registry is feature_registry


def test_json_round_trip() -> None:
    settings = FeatureSettings(enabled=True, channel_id=CHANNEL)
    raw = settings.to_json()
    assert raw == f'{{"channel_id": {CHANNEL}, "enabled": true, "role_id": null}}'
    assert FeatureSettings.from_json(raw) == settings
    assert FeatureSettings.from_json(None) == FeatureSettings()


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('{}', FeatureSettings()),
        ('{"enabled": true, "added_later": [1]}', FeatureSettings(enabled=True)),
        (
            '{"channel_id": "123", "role_id": " -4 "}',
            FeatureSettings(channel_id=123, role_id=-4),
        ),
        ('{"channel_id": null}', FeatureSettings()),
    ],
)
def test_valid_stored_settings_load_quietly(
    raw: str, expected: FeatureSettings, caplog: pytest.LogCaptureFixture
) -> None:
    assert FeatureSettings.from_json(raw) == expected
    assert settings_warnings(caplog) == []


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('not json', FeatureSettings()),
        ('[true]', FeatureSettings()),
        ('null', FeatureSettings()),
        ('"enabled"', FeatureSettings()),
        ('{"enabled": "yes", "channel_id": 5}', FeatureSettings(channel_id=5)),
        ('{"enabled": 1, "role_id": 6}', FeatureSettings(role_id=6)),
        ('{"enabled": true, "channel_id": true}', FeatureSettings(enabled=True)),
        ('{"channel_id": 1.5}', FeatureSettings()),
        ('{"channel_id": "12x"}', FeatureSettings()),
        ('{"channel_id": "1_000"}', FeatureSettings()),
        ('{"channel_id": {"id": 5}}', FeatureSettings()),
    ],
)
def test_invalid_stored_values_fall_back_to_their_defaults(
    raw: str, expected: FeatureSettings, caplog: pytest.LogCaptureFixture
) -> None:
    assert FeatureSettings.from_json(raw) == expected
    assert len(settings_warnings(caplog)) == 1


def test_subclass_fields_are_loaded_and_validated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert WorkshopSettings.from_json(
        '{"calendar_id": "cal-1", "offsets_minutes": [30, "15"]}'
    ) == WorkshopSettings(calendar_id='cal-1', offsets_minutes=(30, 15))
    assert settings_warnings(caplog) == []

    for raw in (
        '{"offsets_minutes": 30}',
        '{"offsets_minutes": [30, true]}',
        '{"calendar_id": 5}',
    ):
        assert WorkshopSettings.from_json(raw) == WorkshopSettings()
    assert len(settings_warnings(caplog)) == 3


async def test_invalid_stored_settings_are_reported_once_per_content(
    guild_settings: GuildSettingsRepo, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await store(db, GUILD, 'workshops', '{"enabled": true, "channel_id": "abc"}')
    for _ in range(3):
        assert await guild_settings.get(GUILD, 'workshops') == FeatureSettings(
            enabled=True
        )
    assert await guild_settings.enabled_guilds('workshops') == [
        (GUILD, FeatureSettings(enabled=True))
    ]
    (warning,) = settings_warnings(caplog)
    assert str(GUILD) in warning
    assert 'workshops' in warning
    assert "channel_id is 'abc'" in warning

    await store(db, GUILD, 'workshops', '{oops')  # new content is reported anew
    assert await guild_settings.get(GUILD, 'workshops') == FeatureSettings()
    assert len(settings_warnings(caplog)) == 2


async def test_update_rewrites_invalid_stored_settings_cleanly(
    guild_settings: GuildSettingsRepo, db: Database
) -> None:
    await store(db, GUILD, 'workshops', '{"enabled": "yes", "channel_id": 5}')
    assert await guild_settings.update(GUILD, 'workshops', role_id=7) == (
        FeatureSettings(channel_id=5, role_id=7)
    )
    assert await stored(db, GUILD, 'workshops') == {
        'channel_id': 5,
        'enabled': False,
        'role_id': 7,
    }


async def test_update_rejects_unknown_fields_and_invalid_values(
    guild_settings: GuildSettingsRepo, db: Database
) -> None:
    with pytest.raises(ValueError, match='colour'):
        await guild_settings.update(GUILD, 'workshops', enabled=True, colour=5)
    with pytest.raises(TypeError, match='enabled'):
        await guild_settings.update(GUILD, 'workshops', enabled='yes')
    with pytest.raises(TypeError, match='channel_id'):
        await guild_settings.update(GUILD, 'workshops', channel_id=True)
    assert await db.fetchval('SELECT COUNT(*) FROM guild_settings') == 0


async def test_update_accepts_numeric_strings_for_ids(
    guild_settings: GuildSettingsRepo,
) -> None:
    updated = await guild_settings.update(GUILD, 'workshops', channel_id=str(CHANNEL))
    assert updated.channel_id == CHANNEL


async def test_concurrent_updates_keep_every_change(
    guild_settings: GuildSettingsRepo,
) -> None:
    await asyncio.gather(
        guild_settings.update(GUILD, 'workshops', enabled=True),
        guild_settings.update(GUILD, 'workshops', channel_id=CHANNEL),
        guild_settings.update(GUILD, 'workshops', role_id=ROLE),
    )
    assert await guild_settings.get(GUILD, 'workshops') == FeatureSettings(
        enabled=True, channel_id=CHANNEL, role_id=ROLE
    )


async def test_subclass_and_base_class_read_each_others_settings(
    db: Database, clock: FakeClock
) -> None:
    base = GuildSettingsRepo(db, clock, default_registry())
    typed = GuildSettingsRepo(db, clock, registry_with_workshop_settings())

    await base.update(GUILD, 'workshops', enabled=True, channel_id=CHANNEL)
    loaded = await typed.get(GUILD, 'workshops')
    assert loaded == WorkshopSettings(enabled=True, channel_id=CHANNEL)

    await typed.update(GUILD, 'workshops', calendar_id='cal-1', offsets_minutes=[60])
    assert await base.get(GUILD, 'workshops') == FeatureSettings(
        enabled=True, channel_id=CHANNEL
    )
    assert await typed.get(GUILD, 'workshops') == WorkshopSettings(
        enabled=True, channel_id=CHANNEL, calendar_id='cal-1', offsets_minutes=(60,)
    )


async def test_base_class_update_keeps_the_subclass_fields(
    db: Database, clock: FakeClock
) -> None:
    # E.g. /kcpc disable while the extension that registers WorkshopSettings
    # is disabled: the workshop settings must survive until it is back.
    base = GuildSettingsRepo(db, clock, default_registry())
    typed = GuildSettingsRepo(db, clock, registry_with_workshop_settings())
    await typed.update(
        GUILD,
        'workshops',
        enabled=True,
        channel_id=CHANNEL,
        calendar_id='cal-1',
        offsets_minutes=[60],
    )

    assert await base.update(GUILD, 'workshops', enabled=False) == FeatureSettings(
        channel_id=CHANNEL
    )

    assert await typed.get(GUILD, 'workshops') == WorkshopSettings(
        channel_id=CHANNEL, calendar_id='cal-1', offsets_minutes=(60,)
    )
    assert await stored(db, GUILD, 'workshops') == {
        'calendar_id': 'cal-1',
        'channel_id': CHANNEL,
        'enabled': False,
        'offsets_minutes': [60],
        'role_id': None,
    }


@pytest.mark.parametrize('raw', ['not json', '[true]', 'null'])
async def test_update_replaces_stored_settings_that_are_not_an_object(
    guild_settings: GuildSettingsRepo, db: Database, raw: str
) -> None:
    await store(db, GUILD, 'workshops', raw)

    await guild_settings.update(GUILD, 'workshops', role_id=7)

    assert await stored(db, GUILD, 'workshops') == {
        'channel_id': None,
        'enabled': False,
        'role_id': 7,
    }


async def test_get_typed_checks_the_registered_type(
    db: Database, clock: FakeClock
) -> None:
    repo = GuildSettingsRepo(db, clock, registry_with_workshop_settings())

    workshops = await repo.get_typed(GUILD, 'workshops', WorkshopSettings)
    assert workshops.offsets_minutes == (1440, 60)
    assert await repo.get_typed(GUILD, 'workshops', FeatureSettings) == workshops
    with pytest.raises(TypeError, match='FeatureSettings, not WorkshopSettings'):
        await repo.get_typed(GUILD, 'contests', WorkshopSettings)


async def test_all_for_guild_covers_every_registered_feature(
    guild_settings: GuildSettingsRepo, db: Database
) -> None:
    await guild_settings.update(GUILD, 'weekly', enabled=True)
    await guild_settings.update(OTHER_GUILD, 'algo', enabled=True)
    await store(db, GUILD, 'retired-feature', '{"enabled": true}')

    everything = await guild_settings.all_for_guild(GUILD)
    assert list(everything) == ['algo', 'contests', 'weekly', 'workshops']
    assert everything['weekly'] == FeatureSettings(enabled=True)
    assert everything['algo'] == FeatureSettings()


async def test_enabled_guilds_in_numeric_guild_order(
    guild_settings: GuildSettingsRepo,
) -> None:
    await guild_settings.update(10, 'workshops', enabled=True, channel_id=CHANNEL)
    await guild_settings.update(9, 'workshops', enabled=True)
    await guild_settings.update(11, 'workshops', channel_id=CHANNEL)  # not enabled
    await guild_settings.update(12, 'contests', enabled=True)

    assert await guild_settings.enabled_guilds('workshops') == [
        (9, FeatureSettings(enabled=True)),
        (10, FeatureSettings(enabled=True, channel_id=CHANNEL)),
    ]


async def test_unknown_feature_is_a_user_error(
    guild_settings: GuildSettingsRepo,
) -> None:
    message = (
        "Unknown feature 'quiz'. Known features: algo, contests, weekly, workshops."
    )
    with pytest.raises(KcpcUserError) as excinfo:
        await guild_settings.get(GUILD, 'quiz')
    assert str(excinfo.value) == message
    with pytest.raises(KcpcUserError, match='quiz'):
        await guild_settings.get_typed(GUILD, 'quiz', FeatureSettings)
    with pytest.raises(KcpcUserError, match='quiz'):
        await guild_settings.update(GUILD, 'quiz', enabled=True)
    with pytest.raises(KcpcUserError, match='quiz'):
        await guild_settings.enabled_guilds('quiz')


def test_registry_lookup_and_registration() -> None:
    registry = FeatureRegistry()
    beta = FeatureSpec('beta', 'Beta', 'Second')
    alpha = FeatureSpec('alpha', 'Alpha', 'First')
    registry.register(beta)
    registry.register(alpha)

    assert registry.keys() == ['alpha', 'beta']
    assert registry.all() == [alpha, beta]
    assert registry.get('alpha') is alpha
    assert 'alpha' in registry
    assert 'gamma' not in registry
    assert 5 not in registry

    with pytest.raises(ValueError, match='already registered'):
        registry.register(FeatureSpec('alpha', 'Alpha 2', 'Replacement'))
    replacement = FeatureSpec('alpha', 'Alpha 2', 'Replacement')
    registry.register(replacement, replace=True)
    assert registry.get('alpha') is replacement


def test_empty_registry_says_so() -> None:
    with pytest.raises(KcpcUserError, match=r'Known features: none\.$'):
        FeatureRegistry().get('quiz')


def test_default_registry_lists_the_planned_features() -> None:
    specs = default_registry().all()
    assert [(spec.key, spec.title, spec.description) for spec in specs] == [
        ('algo', 'Algorithm of the month', 'Monthly data structure / algorithm pick'),
        ('contests', 'Contests', 'Contest reminders and results'),
        ('weekly', 'Weekly problem', 'Friday problem, solution the Friday after'),
        ('workshops', 'Workshops', 'Luma workshop reminders, 24h and 1h before'),
    ]
    assert all(spec.settings_type is FeatureSettings for spec in specs)


@pytest.mark.parametrize(
    'key', ['', 'Workshops', '1st', 'two words', 'under_score', '-lead', 'café']
)
def test_invalid_feature_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match='Invalid feature key'):
        FeatureSpec(key, 'Title', 'Description')


def test_feature_keys_may_use_digits_and_hyphens() -> None:
    assert FeatureSpec('icpc-2027', 'ICPC', 'Regional contests').key == 'icpc-2027'


@pytest.mark.parametrize(
    ('settings_type', 'problem'),
    [
        (RatioSettings, 'Unsupported settings field type'),
        (RequiredFieldSettings, 'needs a default'),
        (UndecoratedSettings, 'must be decorated'),
    ],
)
def test_settings_types_that_cannot_be_stored_are_rejected(
    settings_type: type[FeatureSettings], problem: str
) -> None:
    with pytest.raises(TypeError, match=problem):
        FeatureSpec('feature', 'Title', 'Description', settings_type)
