"""Tests for tle.config.Settings and its docs, and for how tle.constants loads .env."""

import inspect
import os
import re
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tle import constants
from tle.config import Settings
from tle.kcpc.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[3]

ENV_VARS = (
    'KCPC_TIMEZONE',
    'DISABLED_EXTENSIONS',
    'HTTP_USER_AGENT',
    'LUMA_CALENDAR_ID',
    'ICPC_CONTEST_CODES',
    'CLIST_USERNAME',
    'CLIST_API_KEY',
    'KCPC_DB_PATH',
)
# The variables whose defaults .env.example shows; for the rest it shows
# examples.
ENV_VARS_WITH_DEFAULTS = (
    'KCPC_TIMEZONE',
    'HTTP_USER_AGENT',
    'ICPC_CONTEST_CODES',
    'KCPC_DB_PATH',
)
# Where the variables are documented, for operators and for developers.
ENV_DOCS = ('README.md', '.env.example')


def test_an_empty_environment_gives_the_defaults() -> None:
    settings = Settings.from_env({})

    assert settings == Settings()
    assert settings.kcpc_timezone == 'Europe/London'
    assert settings.disabled_extensions == frozenset()
    assert (
        settings.http_user_agent == 'KCPC-bot (+https://github.com/Canon-Regularis/TLE)'
    )
    assert settings.luma_calendar_id is None
    assert settings.icpc_contest_codes == ('UKIEPC', 'Northwestern-Europe-2027')
    assert settings.clist_username is None
    assert settings.clist_api_key is None
    assert settings.kcpc_db_path == Path('data', 'db', 'kcpc.db')


def test_every_variable_is_read() -> None:
    settings = Settings.from_env(
        {
            'KCPC_TIMEZONE': 'America/New_York',
            'DISABLED_EXTENSIONS': 'tle.duel,kcpc',
            'HTTP_USER_AGENT': 'kcpc-test/1.0',
            'LUMA_CALENDAR_ID': 'cal-abc123',
            'ICPC_CONTEST_CODES': 'UKIEPC',
            'CLIST_USERNAME': 'kcpc',
            'CLIST_API_KEY': 'secret-key',
            'KCPC_DB_PATH': '/srv/kcpc/kcpc.db',
        }
    )

    assert settings == Settings(
        kcpc_timezone='America/New_York',
        disabled_extensions=frozenset({'tle.duel', 'kcpc'}),
        http_user_agent='kcpc-test/1.0',
        luma_calendar_id='cal-abc123',
        icpc_contest_codes=('UKIEPC',),
        clist_username='kcpc',
        clist_api_key='secret-key',
        kcpc_db_path=Path('/srv/kcpc/kcpc.db'),
    )


def test_values_are_stripped() -> None:
    settings = Settings.from_env(
        {
            'KCPC_TIMEZONE': ' Asia/Tokyo\t',
            'HTTP_USER_AGENT': '  kcpc-test/1.0 ',
            'LUMA_CALENDAR_ID': ' cal-abc123 ',
            'CLIST_USERNAME': ' kcpc ',
            'KCPC_DB_PATH': ' kcpc.db ',
        }
    )

    assert settings.kcpc_timezone == 'Asia/Tokyo'
    assert settings.http_user_agent == 'kcpc-test/1.0'
    assert settings.luma_calendar_id == 'cal-abc123'
    assert settings.clist_username == 'kcpc'
    assert settings.kcpc_db_path == Path('kcpc.db')


def test_extension_names_are_stripped_and_lowercased_and_empties_dropped() -> None:
    settings = Settings.from_env(
        {'DISABLED_EXTENSIONS': ' TLE.Duel , ,tle.graphs,,KCPC '}
    )

    assert settings.disabled_extensions == frozenset({'tle.duel', 'tle.graphs', 'kcpc'})


def test_contest_codes_keep_their_case_and_order() -> None:
    settings = Settings.from_env(
        {'ICPC_CONTEST_CODES': ' Northwestern-Europe-2027 ,, UKIEPC ,'}
    )

    assert settings.icpc_contest_codes == ('Northwestern-Europe-2027', 'UKIEPC')


@pytest.mark.parametrize('name', ENV_VARS)
@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_variable_keeps_its_default(name: str, blank: str) -> None:
    assert Settings.from_env({name: blank}) == Settings()


@pytest.mark.parametrize('separators', [',', ' , ,, '])
def test_a_list_without_items_keeps_its_default(separators: str) -> None:
    settings = Settings.from_env(
        {'ICPC_CONTEST_CODES': separators, 'DISABLED_EXTENSIONS': separators}
    )

    assert settings == Settings()


@pytest.mark.parametrize('zone', ['Europe/Lodnon', 'europe/london', 'UTC+1', 'BST'])
def test_an_unknown_time_zone_is_a_config_error(zone: str) -> None:
    message = f"KCPC_TIMEZONE: Unknown time zone '{zone}'"

    with pytest.raises(ConfigError, match=f'^{re.escape(message)}$'):
        Settings.from_env({'KCPC_TIMEZONE': zone})


def test_settings_made_directly_are_checked_too() -> None:
    with pytest.raises(ConfigError, match="Unknown time zone 'Mars/Olympus_Mons'"):
        Settings(kcpc_timezone='Mars/Olympus_Mons')


def test_tz_is_the_zone() -> None:
    assert Settings.from_env({'KCPC_TIMEZONE': 'Asia/Tokyo'}).tz == ZoneInfo(
        'Asia/Tokyo'
    )
    assert Settings().tz == ZoneInfo('Europe/London')


@pytest.mark.parametrize(
    ('username', 'api_key', 'configured'),
    [
        ('kcpc', 'secret-key', True),
        ('kcpc', None, False),
        (None, 'secret-key', False),
        (None, None, False),
    ],
)
def test_clist_is_configured_only_with_both_credentials(
    username: str | None, api_key: str | None, configured: bool
) -> None:
    settings = Settings(clist_username=username, clist_api_key=api_key)

    assert settings.clist_configured is configured


def test_the_api_key_stays_out_of_the_repr() -> None:
    settings = Settings(clist_username='kcpc', clist_api_key='secret-key')

    assert 'secret-key' not in repr(settings)
    assert "clist_username='kcpc'" in repr(settings)


def test_settings_are_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        Settings().kcpc_timezone = 'Asia/Tokyo'  # type: ignore[misc]


def test_from_env_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('KCPC_TIMEZONE', 'Europe/Paris')
    monkeypatch.setenv('DISABLED_EXTENSIONS', 'tle.duel')

    settings = Settings.from_env()

    assert settings.kcpc_timezone == 'Europe/Paris'
    assert settings.disabled_extensions == frozenset({'tle.duel'})
    assert settings.icpc_contest_codes == ('UKIEPC', 'Northwestern-Europe-2027')


def test_the_kcpc_database_sits_with_tles() -> None:
    assert constants.KCPC_DB_FILE_PATH == constants.DB_DIR / 'kcpc.db'
    assert Settings().kcpc_db_path == constants.KCPC_DB_FILE_PATH


def test_these_tests_know_every_variable_from_env_reads() -> None:
    source = inspect.getsource(Settings.from_env)
    read = re.findall(r"_(?:value|items)\(env, '([A-Z_]+)'\)", source)

    assert sorted(read) == sorted(ENV_VARS)


@pytest.mark.parametrize('document', ENV_DOCS)
def test_every_variable_is_documented(document: str) -> None:
    text = (REPO_ROOT / document).read_text(encoding='utf-8')

    assert [name for name in ENV_VARS if not re.search(rf'\b{name}\b', text)] == []


def env_example_values() -> dict[str, str]:
    """The value .env.example gives each variable, commented out or not."""
    text = (REPO_ROOT / '.env.example').read_text(encoding='utf-8')
    return dict(re.findall(r'^#? ?([A-Z_]+)="([^"]*)"$', text, re.MULTILINE))


@pytest.mark.parametrize('name', ENV_VARS)
def test_env_example_shows_the_defaults_and_otherwise_examples(name: str) -> None:
    shown = Settings.from_env({name: env_example_values()[name]})

    if name in ENV_VARS_WITH_DEFAULTS:
        assert shown == Settings()
    else:
        assert shown != Settings()


def admin_role_seen_by_constants(
    tmp_path: Path, dotenv: str, environment: dict[str, str]
) -> str:
    """TLE_ADMIN as tle.constants reads it at import, next to a .env file.

    Runs a copy of constants.py in a fresh interpreter, so the import (and
    load_dotenv) happens for real.
    """
    package = tmp_path / 'tle'
    package.mkdir()
    (package / '__init__.py').write_text('')
    shutil.copy(constants.__file__, package / 'constants.py')
    (tmp_path / '.env').write_text(dotenv)
    env = {name: value for name, value in os.environ.items() if name != 'TLE_ADMIN'}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [sys.executable, '-c', 'from tle import constants; print(constants.TLE_ADMIN)'],
        cwd=tmp_path,
        env=env | environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_constants_reads_dotenv_before_its_settings(tmp_path: Path) -> None:
    assert admin_role_seen_by_constants(tmp_path, 'TLE_ADMIN=Committee\n', {}) == (
        'Committee'
    )


def test_the_environment_wins_over_dotenv(tmp_path: Path) -> None:
    role = admin_role_seen_by_constants(
        tmp_path, 'TLE_ADMIN=Committee\n', {'TLE_ADMIN': 'Board'}
    )

    assert role == 'Board'
