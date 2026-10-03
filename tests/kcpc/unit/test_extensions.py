"""Tests for tle.extensions: finding the extensions, and choosing which to load."""

import importlib.util
from pathlib import Path

import pytest

from tle import extensions
from tle.config import Settings
from tle.extensions import (
    KCPC_EXTENSIONS,
    LOGGING_EXTENSION,
    Extension,
    discover,
    select,
)

REPO_COGS_DIR = Path(__file__).resolve().parents[3] / 'tle' / 'cogs'

KCPC_ADMIN = Extension('kcpc.admin', 'tle.kcpc.features.admin.cog', 'kcpc')
KCPC_WORKSHOPS = Extension('kcpc.workshops', 'tle.kcpc.features.workshops.cog', 'kcpc')
KCPC_CONTESTS = Extension('kcpc.contests', 'tle.kcpc.features.contests.cog', 'kcpc')
KCPC_NOTIFY = Extension('kcpc.notify', 'tle.kcpc.features.notify.cog', 'kcpc')
# The real KCPC extensions, in load order.
KCPC = [KCPC_ADMIN, KCPC_WORKSHOPS, KCPC_CONTESTS, KCPC_NOTIFY]

# Made-up extensions, listed out of order.
ZETA = Extension('tle.zeta', 'tle.cogs.zeta', 'tle')
KCPC_B = Extension('kcpc.b', 'tle.kcpc.features.b.cog', 'kcpc')
LOGGING = Extension('tle.logging', 'tle.cogs.logging', 'tle')
ALPHA = Extension('tle.alpha', 'tle.cogs.alpha', 'tle')
KCPC_A = Extension('kcpc.a', 'tle.kcpc.features.a.cog', 'kcpc')
EXTENSIONS = [ZETA, KCPC_B, LOGGING, ALPHA, KCPC_A]


def names(selected: list[Extension]) -> list[str]:
    return [extension.name for extension in selected]


def kcpc_names(selected: list[Extension]) -> list[str]:
    return [extension.name for extension in selected if extension.family == 'kcpc']


def test_discover_finds_every_tle_cog_then_the_kcpc_extensions() -> None:
    cogs = sorted(
        path.stem
        for path in REPO_COGS_DIR.glob('*.py')
        if not path.stem.startswith('_')
    )

    found = discover()

    assert 'logging' in cogs  # the glob above found the real cogs
    assert found == [
        *(Extension(f'tle.{cog}', f'tle.cogs.{cog}', 'tle') for cog in cogs),
        *KCPC,
    ]


def test_kcpc_extensions_are_listed_in_order() -> None:
    # kcpc.admin first: it owns /kcpc, which the features that load after it
    # add their admin commands to.
    assert KCPC_EXTENSIONS == (
        ('kcpc.admin', 'tle.kcpc.features.admin.cog'),
        ('kcpc.workshops', 'tle.kcpc.features.workshops.cog'),
        ('kcpc.contests', 'tle.kcpc.features.contests.cog'),
        ('kcpc.notify', 'tle.kcpc.features.notify.cog'),
    )
    kcpc = [extension for extension in discover() if extension.family == 'kcpc']

    assert [(ext.name, ext.module) for ext in kcpc] == list(KCPC_EXTENSIONS)


def test_each_kcpc_extension_is_named_after_its_feature_package() -> None:
    # As tle.extensions documents: kcpc.<feature> loads
    # tle/kcpc/features/<feature>/cog.py. The boot tests find KCPC's extension
    # modules by this layout.
    for name, module in KCPC_EXTENSIONS:
        family, _, feature = name.partition('.')
        assert family == 'kcpc' and feature, name
        assert module == f'tle.kcpc.features.{feature}.cog', name
    assert len({name for name, _ in KCPC_EXTENSIONS}) == len(KCPC_EXTENSIONS)


def test_every_discovered_module_exists() -> None:
    # find_spec locates a module without running it, so the cairo stack that
    # some cogs import isn't needed.
    for extension in discover():
        assert importlib.util.find_spec(extension.module) is not None, extension


def test_discover_works_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = discover()
    monkeypatch.chdir(tmp_path)

    assert discover() == expected


def test_discover_skips_private_and_other_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ('zeta.py', 'alpha.py', '_private.py', '__init__.py', 'notes.txt'):
        (tmp_path / name).write_text('')
    (tmp_path / 'package').mkdir()
    (tmp_path / 'package' / 'inner.py').write_text('')
    monkeypatch.setattr(extensions, 'TLE_COGS_DIR', tmp_path)

    assert names(discover()) == ['tle.alpha', 'tle.zeta', *names(KCPC)]


def test_select_orders_logging_then_tle_by_name_then_kcpc_as_listed() -> None:
    enabled, unknown = select(EXTENSIONS, [])

    assert enabled == [LOGGING, ALPHA, ZETA, KCPC_B, KCPC_A]
    assert unknown == []


def test_select_without_logging_keeps_the_order_of_the_rest() -> None:
    enabled, _ = select(EXTENSIONS, [LOGGING_EXTENSION])

    assert enabled == [ALPHA, ZETA, KCPC_B, KCPC_A]


@pytest.mark.parametrize(
    ('disabled', 'expected'),
    [
        (['tle.zeta'], [LOGGING, ALPHA, KCPC_B, KCPC_A]),
        (['kcpc.b', 'tle.alpha'], [LOGGING, ZETA, KCPC_A]),
        (['tle'], [KCPC_B, KCPC_A]),
        (['kcpc'], [LOGGING, ALPHA, ZETA]),
        (['tle', 'kcpc'], []),
        (['tle', 'tle.zeta'], [KCPC_B, KCPC_A]),
    ],
)
def test_select_disables_by_name_or_family(
    disabled: list[str], expected: list[Extension]
) -> None:
    enabled, unknown = select(EXTENSIONS, disabled)

    assert enabled == expected
    assert unknown == []


def test_select_ignores_case_and_surrounding_space() -> None:
    enabled, unknown = select(EXTENSIONS, ['TLE.Zeta', ' KCPC ', ''])

    assert enabled == [LOGGING, ALPHA]
    assert unknown == []


def test_select_reports_unknown_tokens_sorted() -> None:
    enabled, unknown = select(EXTENSIONS, ['zeta', 'tle.nope', 'tle.zeta', 'tle.'])

    assert enabled == [LOGGING, ALPHA, KCPC_B, KCPC_A]
    assert unknown == ['tle.', 'tle.nope', 'zeta']


def test_select_accepts_the_disabled_extensions_setting() -> None:
    settings = Settings.from_env(
        {'DISABLED_EXTENSIONS': 'tle.duel,tle.graphs,tle.starboard'}
    )

    enabled, unknown = select(discover(), settings.disabled_extensions)

    assert enabled[0].name == LOGGING_EXTENSION
    assert {'tle.duel', 'tle.graphs', 'tle.starboard'}.isdisjoint(names(enabled))
    # KCPC's last, as listed.
    assert names(enabled)[-len(KCPC) :] == names(KCPC)
    assert len(enabled) == len(discover()) - 3
    assert unknown == []


@pytest.mark.parametrize('disabled', KCPC, ids=names(KCPC))
def test_each_kcpc_extension_can_be_disabled_alone(disabled: Extension) -> None:
    settings = Settings.from_env({'DISABLED_EXTENSIONS': disabled.name})

    enabled, unknown = select(discover(), settings.disabled_extensions)

    assert kcpc_names(enabled) == [ext.name for ext in KCPC if ext != disabled]
    assert len(enabled) == len(discover()) - 1
    assert unknown == []


def test_the_kcpc_family_disables_every_kcpc_extension_and_nothing_else() -> None:
    settings = Settings.from_env({'DISABLED_EXTENSIONS': 'kcpc'})

    enabled, unknown = select(discover(), settings.disabled_extensions)

    assert kcpc_names(enabled) == []
    assert len(enabled) == len(discover()) - len(KCPC)
    assert unknown == []
