"""Tests for tle.config.Settings and its docs, and for how tle.constants loads .env."""

import ast
import inspect
import logging
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
    'ALLOWED_GUILD_IDS',
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
# The roles tle.constants reads at import, and those that the docs say to give
# by ID (the developer role takes nothing else).
ROLE_VARS = (
    'TLE_ADMIN',
    'TLE_MODERATOR',
    'TLE_TRUSTED',
    'TLE_PURGATORY',
    'TLE_DEVELOPER',
)
ROLE_VARS_BY_ID = ('TLE_ADMIN', 'TLE_MODERATOR', 'TLE_TRUSTED', 'TLE_DEVELOPER')
# Where the variables are documented, for operators and for developers.
ENV_DOCS = ('README.md', '.env.example')

GUILD = 123456789012345678
OTHER_GUILD = 234567890123456789
# Values that aren't Discord IDs, though int() takes some of them.
NOT_IDS = [
    pytest.param('KCPC', id='a name'),
    pytest.param('<@&123456789012345678>', id='a mention'),
    pytest.param('12x', id='a letter'),
    pytest.param('123 456', id='a space'),
    pytest.param('-123', id='a minus sign'),
    pytest.param('+123', id='a plus sign'),
    pytest.param('1_000', id='an underscore'),
    pytest.param('1.5', id='a decimal'),
    pytest.param('0x1F', id='hexadecimal'),
    pytest.param('\u00b2', id='a superscript digit'),
    pytest.param('\u0661\u0662\u0663', id='arabic-indic digits'),
    pytest.param('\uff11\uff12\uff13', id='fullwidth digits'),
    pytest.param('1' * 21, id='21 digits'),
]


def test_an_empty_environment_gives_the_defaults() -> None:
    settings = Settings.from_env({})

    assert settings == Settings()
    assert settings.kcpc_timezone == 'Europe/London'
    assert settings.disabled_extensions == frozenset()
    assert settings.allowed_guild_ids == frozenset()
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
            'ALLOWED_GUILD_IDS': f'{GUILD},{OTHER_GUILD}',
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
        allowed_guild_ids=frozenset({GUILD, OTHER_GUILD}),
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


def test_server_ids_are_stripped_and_empties_and_repeats_dropped() -> None:
    settings = Settings.from_env(
        {'ALLOWED_GUILD_IDS': f' {GUILD} , ,{OTHER_GUILD},,\t{GUILD} '}
    )

    assert settings.allowed_guild_ids == frozenset({GUILD, OTHER_GUILD})


@pytest.mark.parametrize(
    'item', ['7', '18446744073709551615'], ids=['1 digit', '20 digits']
)
def test_a_server_id_has_1_to_20_digits(item: str) -> None:
    settings = Settings.from_env({'ALLOWED_GUILD_IDS': item})

    assert settings.allowed_guild_ids == frozenset({int(item)})


@pytest.mark.parametrize('item', NOT_IDS)
def test_an_item_that_isnt_a_server_id_is_a_config_error(item: str) -> None:
    message = f"ALLOWED_GUILD_IDS: '{item}' is not a server ID"

    with pytest.raises(ConfigError, match=f'^{re.escape(message)}$'):
        Settings.from_env({'ALLOWED_GUILD_IDS': f'{GUILD}, {item} ,{OTHER_GUILD}'})


@pytest.mark.parametrize('name', ENV_VARS)
@pytest.mark.parametrize('blank', ['', '   '])
def test_a_blank_variable_keeps_its_default(name: str, blank: str) -> None:
    assert Settings.from_env({name: blank}) == Settings()


@pytest.mark.parametrize('separators', [',', ' , ,, '])
def test_a_list_without_items_keeps_its_default(separators: str) -> None:
    settings = Settings.from_env(
        {
            'ICPC_CONTEST_CODES': separators,
            'DISABLED_EXTENSIONS': separators,
            'ALLOWED_GUILD_IDS': separators,
        }
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
    monkeypatch.setenv('ALLOWED_GUILD_IDS', str(GUILD))

    settings = Settings.from_env()

    assert settings.kcpc_timezone == 'Europe/Paris'
    assert settings.disabled_extensions == frozenset({'tle.duel'})
    assert settings.allowed_guild_ids == frozenset({GUILD})
    assert settings.icpc_contest_codes == ('UKIEPC', 'Northwestern-Europe-2027')


def test_the_kcpc_database_sits_with_tles() -> None:
    assert constants.KCPC_DB_FILE_PATH == constants.DB_DIR / 'kcpc.db'
    assert Settings().kcpc_db_path == constants.KCPC_DB_FILE_PATH


def test_these_tests_know_every_variable_from_env_reads() -> None:
    source = inspect.getsource(Settings.from_env)
    read = re.findall(r"_(?:value|items)\(env, '([A-Z_]+)'\)", source)

    assert sorted(read) == sorted(ENV_VARS)


def test_these_tests_know_every_role_constants_reads() -> None:
    source = inspect.getsource(constants)
    read = re.findall(r"_get_role(?:_id)?_from_env\('([A-Z_]+)'", source)

    assert sorted(read) == sorted(ROLE_VARS)


@pytest.mark.parametrize('document', ENV_DOCS)
def test_every_variable_is_documented(document: str) -> None:
    text = (REPO_ROOT / document).read_text(encoding='utf-8')
    names = ENV_VARS + ROLE_VARS

    assert [name for name in names if not re.search(rf'\b{name}\b', text)] == []


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


@pytest.mark.parametrize('name', ROLE_VARS_BY_ID)
def test_env_example_gives_these_roles_by_id(name: str) -> None:
    assert env_example_values()[name].isdigit()


def developer_role_from(monkeypatch: pytest.MonkeyPatch, value: str | None) -> object:
    """The developer role that tle.constants reads from ``value``, or unset."""
    if value is None:
        monkeypatch.delenv('TLE_DEVELOPER', raising=False)
    else:
        monkeypatch.setenv('TLE_DEVELOPER', value)
    return constants._get_role_id_from_env('TLE_DEVELOPER')


@pytest.mark.parametrize(
    ('value', 'role_id'),
    [
        (str(GUILD), GUILD),
        (f' \t{OTHER_GUILD} ', OTHER_GUILD),
        ('7', 7),
        ('18446744073709551615', 2**64 - 1),
    ],
)
def test_the_developer_role_is_an_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
    role_id: int,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=constants.__name__):
        assert developer_role_from(monkeypatch, value) == role_id

    assert caplog.records == []


@pytest.mark.parametrize('value', [None, '', ' \t '], ids=['unset', 'empty', 'blank'])
def test_without_an_id_there_is_no_developer_role(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=constants.__name__):
        assert developer_role_from(monkeypatch, value) is None

    assert caplog.records == []


@pytest.mark.parametrize('value', NOT_IDS)
def test_a_developer_role_that_isnt_an_id_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    with caplog.at_level(logging.DEBUG, logger=constants.__name__):
        assert developer_role_from(monkeypatch, value) is None

    warning = f'TLE_DEVELOPER must be a role ID, not {value!r}, so it is ignored'
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ('tle.constants', logging.WARNING, warning)
    ]


def import_constants(
    tmp_path: Path, dotenv: str, environment: dict[str, str], name: str
) -> tuple[object, str]:
    """``name`` as tle.constants reads it at import, next to a .env file, and
    what the import wrote to stderr.

    Runs a copy of constants.py in a fresh interpreter, so the import (and
    load_dotenv) happens for real, without the roles that this process's
    environment sets.
    """
    package = tmp_path / 'tle'
    package.mkdir()
    (package / '__init__.py').write_text('')
    shutil.copy(constants.__file__, package / 'constants.py')
    (tmp_path / '.env').write_text(dotenv)
    env = {key: value for key, value in os.environ.items() if key not in ROLE_VARS}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(
        [
            sys.executable,
            '-c',
            f'from tle import constants; print(repr(constants.{name}))',
        ],
        cwd=tmp_path,
        env=env | environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return ast.literal_eval(result.stdout), result.stderr


def admin_role_seen_by_constants(
    tmp_path: Path, dotenv: str, environment: dict[str, str]
) -> object:
    """TLE_ADMIN as tle.constants reads it at import, next to a .env file."""
    role, _ = import_constants(tmp_path, dotenv, environment, 'TLE_ADMIN')
    return role


def test_constants_reads_dotenv_before_its_settings(tmp_path: Path) -> None:
    assert admin_role_seen_by_constants(tmp_path, 'TLE_ADMIN=Committee\n', {}) == (
        'Committee'
    )


def test_the_environment_wins_over_dotenv(tmp_path: Path) -> None:
    role = admin_role_seen_by_constants(
        tmp_path, 'TLE_ADMIN=Committee\n', {'TLE_ADMIN': 'Board'}
    )

    assert role == 'Board'


def test_constants_reads_a_role_given_by_id_as_an_id(tmp_path: Path) -> None:
    # As the docs recommend for the admin, moderator and trusted roles.
    assert admin_role_seen_by_constants(tmp_path, f'TLE_ADMIN={GUILD}\n', {}) == GUILD


def test_constants_reads_the_developer_role_id_at_import(tmp_path: Path) -> None:
    role, stderr = import_constants(
        tmp_path, f'TLE_DEVELOPER={GUILD}\n', {}, 'TLE_DEVELOPER'
    )

    assert role == GUILD
    assert 'TLE_DEVELOPER' not in stderr


def test_constants_has_no_developer_role_by_default(tmp_path: Path) -> None:
    role, stderr = import_constants(tmp_path, '', {}, 'TLE_DEVELOPER')

    assert role is None
    assert 'TLE_DEVELOPER' not in stderr


def test_a_developer_role_name_is_ignored_with_a_warning_at_import(
    tmp_path: Path,
) -> None:
    # The bot imports tle.constants before it sets up logging (tle.__main__),
    # so the warning has to show without any: on stderr.
    role, stderr = import_constants(
        tmp_path, 'TLE_DEVELOPER=Developer\n', {}, 'TLE_DEVELOPER'
    )

    assert role is None
    assert stderr.splitlines() == [
        "TLE_DEVELOPER must be a role ID, not 'Developer', so it is ignored"
    ]
