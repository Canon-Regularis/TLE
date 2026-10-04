"""Boot tests: ``TLEBot.setup_hook`` with different extensions switched off.

The bot never logs in. TLE's database setup (``cf_common.initialize``) and the
slash command sync are stubbed out, OAuth is off, no log channel is set and
kcpc.db goes in a temporary directory. The rest runs for real: choosing the
extensions, starting KCPC, loading every cog and, at the end, closing the bot.
KCPC's jobs wait for the bot to be ready, which it never is here, so none of
them runs.
"""

import importlib
import logging
import pkgutil
import sqlite3
import sys
from collections.abc import AsyncIterator, Collection, Iterable
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tle import constants, extensions
from tle.config import Settings
from tle.kcpc import bootstrap
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.core.errors import MigrationError
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.features.contests.settings import ContestSettings
from tle.kcpc.features.problems.settings import WeeklySettings
from tle.kcpc.features.workshops.settings import WorkshopSettings
from tle.kcpc.services import KcpcServices

# TLE's cogs draw with cairo and pango, through gi, and tle.__main__ imports
# matplotlib and seaborn: Docker and CI have them, a bare virtualenv may not.
pytest.importorskip('gi')

from tle.__main__ import TLEBot  # noqa: E402
from tle.util import codeforces_common as cf_common  # noqa: E402

COGS_DIR = Path(__file__).resolve().parents[3] / 'tle' / 'cogs'
TLE_MODULES = frozenset(
    f'tle.cogs.{path.stem}'
    for path in COGS_DIR.glob('*.py')
    if not path.stem.startswith('_')
)
LOGGING_MODULE = 'tle.cogs.logging'
KCPC_FAILED = 'KCPC failed to start; KCPC extensions will not be loaded'
# Where TLE's and KCPC's extension modules live (KCPC's in features/*/cog.py).
EXTENSION_PACKAGES = ('tle.cogs.', 'tle.kcpc.features.')


@dataclass(frozen=True)
class KcpcExtension:
    """A KCPC extension, with the cog and the top-level commands it adds."""

    name: str  # as DISABLED_EXTENSIONS names it
    module: str
    cog: str
    commands: tuple[str, ...]  # hybrid commands or groups: prefix and slash


ADMIN = KcpcExtension(
    'kcpc.admin', 'tle.kcpc.features.admin.cog', 'KcpcAdmin', ('kcpc',)
)
WORKSHOPS = KcpcExtension(
    'kcpc.workshops', 'tle.kcpc.features.workshops.cog', 'KcpcWorkshops', ('event',)
)
CONTESTS = KcpcExtension(
    'kcpc.contests', 'tle.kcpc.features.contests.cog', 'KcpcContests', ('contests',)
)
ACCOUNTS = KcpcExtension(
    'kcpc.accounts',
    'tle.kcpc.features.accounts.cog',
    'KcpcAccounts',
    ('link', 'unlink', 'profile', 'rank'),
)
PROBLEMS = KcpcExtension(
    'kcpc.problems',
    'tle.kcpc.features.problems.cog',
    'KcpcProblems',
    ('randproblem', 'weekly'),
)
NOTIFY = KcpcExtension(
    'kcpc.notify', 'tle.kcpc.features.notify.cog', 'KcpcNotify', ('notify',)
)
KCPC = (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, NOTIFY)  # in load order
KCPC_BY_NAME = {extension.name: extension for extension in KCPC}
CORE_JOBS = [bootstrap.RECONCILE_JOB, bootstrap.REMINDERS_JOB]


@dataclass(frozen=True)
class KcpcFeature:
    """What a feature's extension starts besides its top-level commands.

    Its jobs, its reminder source if it posts reminders, and its admin
    commands in /kcpc <name> (if kcpc.admin is loaded). A feature's settings
    are registered by bootstrap, so they decode as their own type whether or
    not the extension loads.
    """

    extension: KcpcExtension
    name: str  # as the settings, the reminder engine and /kcpc name it
    jobs: tuple[str, ...]  # in the order the cog adds them
    settings: type[FeatureSettings] | None = None  # None: it has no settings
    reminders: bool = False  # whether it registers a reminder source
    admin_commands: frozenset[str] = frozenset()  # the subcommands of /kcpc <name>


WORKSHOPS_FEATURE = KcpcFeature(
    WORKSHOPS,
    'workshops',
    ('workshops.sync',),
    settings=WorkshopSettings,
    reminders=True,
    admin_commands=frozenset({'calendar', 'sync'}),
)
CONTESTS_FEATURE = KcpcFeature(
    CONTESTS,
    'contests',
    ('contests.sync.codeforces', 'contests.sync.atcoder', 'contests.sync.icpc'),
    settings=ContestSettings,
    reminders=True,
    admin_commands=frozenset(
        {'add', 'settime', 'remove', 'platforms', 'start-posts', 'sync'}
    ),
)
# Account linking posts nothing and has no settings; admins unlink AtCoder
# accounts with /kcpc accounts unlink.
ACCOUNTS_FEATURE = KcpcFeature(
    ACCOUNTS,
    'accounts',
    ('accounts.refresh', 'accounts.purge-challenges'),
    admin_commands=frozenset({'unlink'}),
)
# /randproblem and the weekly problem, which posts at its own slots rather
# than through the reminder engine. Its settings, /kcpc weekly and
# /notify weekly go by the feature's name, not the extension's.
PROBLEMS_FEATURE = KcpcFeature(
    PROBLEMS,
    'weekly',
    ('problems.refresh', 'weekly.post'),
    settings=WeeklySettings,
    admin_commands=frozenset(
        {'queue', 'unqueue', 'solution', 'rotation', 'preview', 'post-now'}
    ),
)
FEATURES = (WORKSHOPS_FEATURE, CONTESTS_FEATURE, ACCOUNTS_FEATURE, PROBLEMS_FEATURE)
FEATURE_BY_EXTENSION = {feature.extension.name: feature for feature in FEATURES}


@dataclass(frozen=True)
class Stubs:
    """Stand-ins for the calls that would reach TLE's databases or Discord."""

    initialize: AsyncMock  # cf_common.initialize
    sync: AsyncMock  # CommandTree.sync


@pytest.fixture(autouse=True)
def stubs(monkeypatch: pytest.MonkeyPatch) -> Stubs:
    stubs = Stubs(initialize=AsyncMock(), sync=AsyncMock(return_value=[]))
    monkeypatch.setattr(cf_common, 'initialize', stubs.initialize)
    monkeypatch.setattr(app_commands.CommandTree, 'sync', stubs.sync)
    monkeypatch.setattr(constants, 'OAUTH_CONFIGURED', False)
    # Unset, the logging extension loads but installs no log handler.
    monkeypatch.delenv('LOGGING_COG_CHANNEL_ID', raising=False)
    return stubs


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'db' / 'kcpc.db'


@asynccontextmanager
async def booted(
    db_path: Path, *, disabled: str = '', nodb: bool = False
) -> AsyncIterator[TLEBot]:
    """A bot whose ``setup_hook`` has run; it is closed on the way out.

    The extension modules imported before it booted are then put back in
    sys.modules (see ``extension_modules``).
    """
    settings = Settings.from_env(
        {'DISABLED_EXTENSIONS': disabled, 'KCPC_DB_PATH': str(db_path)}
    )
    intents = discord.Intents.default()  # as tle.__main__.main sets them
    intents.members = True
    intents.message_content = True
    bot = TLEBot(nodb=nodb, settings=settings, command_prefix=';', intents=intents)
    imported = extension_modules()
    try:
        # The context manager sets the bot up for the running loop, as logging
        # in would; KCPC's jobs wait on bot.wait_until_ready, which needs that.
        async with bot:
            await bot.setup_hook()
            yield bot
    finally:
        sys.modules.update(imported)
        # bot.close() should have done this already (shutting down twice is
        # fine), but a database left open would keep pytest from exiting.
        if bot.kcpc is not None:
            await bot.kcpc.shutdown()


def extension_modules() -> dict[str, ModuleType]:
    """The extension modules imported so far, which booting takes away.

    discord.py loads each extension as a new module, which replaces the one in
    sys.modules, and removes it when the bot closes. Other tests import TLE's
    cogs and patch them by name, e.g. ``patch('tle.cogs.codeforces.cf_common')``,
    and since Python 3.11 such a name is looked up through sys.modules. If
    these modules weren't put back, those patches would import new copies and
    miss the modules the tests use.
    """
    return {
        name: module
        for name, module in sys.modules.items()
        if name.startswith(EXTENSION_PACKAGES)
    }


async def is_closed(services: KcpcServices) -> bool:
    try:
        await services.db.fetchval('SELECT 1')
    except sqlite3.ProgrammingError:
        return True
    return False


def make_newer_kcpc_db(path: Path) -> None:
    """A kcpc.db written by a newer version of the bot, which KCPC must refuse."""
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            'CREATE TABLE schema_version (version INTEGER PRIMARY KEY NOT NULL, '
            'name TEXT NOT NULL, applied_at INTEGER NOT NULL)'
        )
        conn.execute("INSERT INTO schema_version VALUES (999, 'future', 0)")
        conn.commit()


def modules_of(loaded: Iterable[KcpcExtension]) -> frozenset[str]:
    return frozenset(extension.module for extension in loaded)


def assert_kcpc_extensions(bot: TLEBot, loaded: Collection[KcpcExtension]) -> None:
    """Exactly the ``loaded`` KCPC extensions are in, with their cogs and commands."""
    for extension in KCPC:
        present = extension in loaded
        assert (extension.module in bot.extensions) is present, extension
        assert (bot.get_cog(extension.cog) is not None) is present, extension
        for command in extension.commands:
            assert (bot.get_command(command) is not None) is present, command
            assert (bot.tree.get_command(command) is not None) is present, command


def assert_features_started(
    bot: TLEBot, services: KcpcServices, loaded: Collection[KcpcExtension]
) -> None:
    """What the ``loaded`` KCPC extensions started: jobs, reminders, admin commands.

    Only the features (``FEATURES``) start anything. Their admin commands join
    /kcpc when kcpc.admin is loaded, and are never added at the top level.
    """
    started = [feature for feature in FEATURES if feature.extension in loaded]
    feature_jobs = [job for feature in started for job in feature.jobs]
    assert [job.name for job in services.scheduler.status()] == sorted(
        CORE_JOBS + feature_jobs
    )
    assert services.reminders.features == sorted(
        feature.name for feature in started if feature.reminders
    )
    kcpc_group = bot.tree.get_command('kcpc')
    slash_children = (
        [] if not isinstance(kcpc_group, app_commands.Group) else kcpc_group.commands
    )
    for feature in FEATURES:
        in_kcpc = (
            ADMIN in loaded and feature in started and bool(feature.admin_commands)
        )
        assert (bot.get_command(f'kcpc {feature.name}') is not None) is in_kcpc
        assert (feature.name in [child.name for child in slash_children]) is in_kcpc
        # At the top level the feature's name is free (workshops) or names its
        # member commands (contests), never its admin commands.
        for top_level in (
            bot.get_command(feature.name),
            bot.tree.get_command(feature.name),
        ):
            assert feature.admin_commands.isdisjoint(subcommand_names(top_level))


def subcommand_names(command: object) -> set[str]:
    """The names of a prefix or slash group's subcommands; none for a command."""
    if isinstance(command, (commands.Group, app_commands.Group)):
        return {child.name for child in command.commands}
    return set()


def fail_cog_load(monkeypatch: pytest.MonkeyPatch, cog_name: str) -> None:
    """Make ``cog_load`` raise for the KCPC cog called ``cog_name``.

    It is patched on the base class, because discord.py runs each extension's
    module afresh: the cog classes that the tests could import are not the
    ones that load. So it suits only a cog without a ``cog_load`` of its own.
    """
    real_cog_load = KcpcCog.cog_load

    async def cog_load(cog: KcpcCog) -> None:
        if type(cog).__name__ == cog_name:
            raise RuntimeError('boom')
        await real_cog_load(cog)

    monkeypatch.setattr(KcpcCog, 'cog_load', cog_load)


def fail_to_add_job(monkeypatch: pytest.MonkeyPatch, job_name: str) -> None:
    """Make ``Scheduler.add`` raise for the job called ``job_name``."""
    real_add = Scheduler.add

    def add(scheduler: Scheduler, job: ScheduledJob) -> None:
        if job.name == job_name:
            raise RuntimeError('boom')
        real_add(scheduler, job)

    monkeypatch.setattr(Scheduler, 'add', add)


def errors_logged(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every record at ERROR or above, whichever logger it came from."""
    return [record for record in caplog.records if record.levelno >= logging.ERROR]


def bot_records(
    caplog: pytest.LogCaptureFixture, level: int
) -> list[logging.LogRecord]:
    """What tle.__main__, which logs to the root logger, logged at ``level``."""
    return [
        record
        for record in caplog.records
        if record.name == 'root' and record.levelno == level
    ]


def bot_messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in bot_records(caplog, level)]


@pytest.mark.parametrize(
    ('disabled', 'nodb', 'tle_modules', 'kcpc'),
    [
        ('', False, TLE_MODULES, KCPC),
        (
            'tle.duel,tle.graphs,tle.starboard',
            False,
            TLE_MODULES - {'tle.cogs.duel', 'tle.cogs.graphs', 'tle.cogs.starboard'},
            KCPC,
        ),
        (
            'kcpc.admin',
            False,
            TLE_MODULES,
            (WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, NOTIFY),
        ),
        (
            'kcpc.workshops',
            False,
            TLE_MODULES,
            (ADMIN, CONTESTS, ACCOUNTS, PROBLEMS, NOTIFY),
        ),
        (
            'kcpc.contests',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, ACCOUNTS, PROBLEMS, NOTIFY),
        ),
        (
            'kcpc.accounts',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, PROBLEMS, NOTIFY),
        ),
        (
            'kcpc.problems',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, NOTIFY),
        ),
        (
            'kcpc.notify',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS),
        ),
        ('kcpc', False, TLE_MODULES, ()),
        (
            'kcpc.admin,kcpc.workshops,kcpc.contests,kcpc.accounts,kcpc.problems,'
            'kcpc.notify',
            False,
            TLE_MODULES,
            (),
        ),
        # The whole TLE family, tle.logging included.
        ('tle', False, frozenset(), KCPC),
        ('', True, TLE_MODULES, ()),
    ],
    ids=[
        'nothing disabled',
        'some TLE cogs disabled',
        'kcpc.admin',
        'kcpc.workshops',
        'kcpc.contests',
        'kcpc.accounts',
        'kcpc.problems',
        'kcpc.notify',
        'kcpc',
        'every kcpc extension by name',
        'tle',
        'nodb',
    ],
)
async def test_the_bot_boots_with_the_enabled_extensions(
    stubs: Stubs,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    disabled: str,
    nodb: bool,
    tle_modules: frozenset[str],
    kcpc: tuple[KcpcExtension, ...],
) -> None:
    async with booted(db_path, disabled=disabled, nodb=nodb) as bot:
        assert set(bot.extensions) == tle_modules | modules_of(kcpc)
        assert_kcpc_extensions(bot, kcpc)
        services = bot.kcpc
        # KCPC runs if any of its extensions is enabled, and not with --nodb.
        if kcpc:
            assert isinstance(services, KcpcServices)
            assert services.scheduler.running
            assert_features_started(bot, services, kcpc)
            # /kcpc shows each feature's settings, whether or not its
            # extension is loaded. Account linking has none to show.
            for feature in FEATURES:
                if feature.settings is None:
                    assert feature.name not in services.features
                    continue
                spec = services.features.get(feature.name)
                assert spec.settings_type is feature.settings
        else:
            assert services is None
        # Where KCPC doesn't run, it doesn't even create its database.
        assert db_path.exists() is bool(kcpc)
        stubs.initialize.assert_awaited_once_with(bot, nodb)
        stubs.sync.assert_awaited_once_with()

        await bot.close()

        assert bot.extensions == {}
        if services is not None:
            assert not services.scheduler.running
            assert await is_closed(services)
            # Unloading the features undid what they had started, although the
            # services had shut down first.
            assert [job.name for job in services.scheduler.status()] == CORE_JOBS
            assert services.reminders.features == []

    assert errors_logged(caplog) == []


async def test_kcpc_adds_its_commands_and_puts_its_admin_commands_in_kcpc(
    db_path: Path,
) -> None:
    def from_kcpc(module: str | None) -> bool:
        return module is not None and module.startswith('tle.kcpc.')

    async with booted(db_path) as bot:
        prefix = {command.name for command in bot.commands if from_kcpc(command.module)}
        slash = {
            command.name
            for command in bot.tree.get_commands()
            if from_kcpc(command.module)
        }
        # Only the commands listed in KCPC, each both a prefix and a slash one.
        assert prefix == slash == {name for ext in KCPC for name in ext.commands}

        kcpc_group = bot.get_command('kcpc')
        assert isinstance(kcpc_group, commands.HybridGroup)
        kcpc_slash = bot.tree.get_command('kcpc')
        assert isinstance(kcpc_slash, app_commands.Group)
        for feature in FEATURES:
            if not feature.admin_commands:
                assert kcpc_group.get_command(feature.name) is None
                assert kcpc_slash.get_command(feature.name) is None
                continue
            admin = kcpc_group.get_command(feature.name)
            assert isinstance(admin, commands.HybridGroup)
            assert admin.cog is bot.get_cog(feature.extension.cog)
            assert subcommand_names(admin) == feature.admin_commands
            admin_slash = kcpc_slash.get_command(feature.name)
            assert isinstance(admin_slash, app_commands.Group)
            assert subcommand_names(admin_slash) == feature.admin_commands


async def test_setup_hook_starts_things_in_order(
    monkeypatch: pytest.MonkeyPatch, stubs: Stubs, db_path: Path
) -> None:
    steps: list[str] = []
    stubs.initialize.side_effect = lambda *args: steps.append('cf_common.initialize')
    stubs.sync.side_effect = lambda: steps.append('tree.sync')
    real_build_services = bootstrap.build_services
    real_load_extension = TLEBot.load_extension

    async def build_services(*args: Any, **kwargs: Any) -> KcpcServices:
        steps.append('kcpc.build_services')
        return await real_build_services(*args, **kwargs)

    async def load_extension(bot: TLEBot, name: str) -> None:
        steps.append(name)
        await real_load_extension(bot, name)

    monkeypatch.setattr(bootstrap, 'build_services', build_services)
    monkeypatch.setattr(TLEBot, 'load_extension', load_extension)

    async with booted(db_path):
        pass

    # The log channel first, so that it hears of problems while starting up,
    # and KCPC's services before the extensions that use them. kcpc.admin
    # comes before the features that add their admin commands to /kcpc.
    assert steps == [
        LOGGING_MODULE,
        'cf_common.initialize',
        'kcpc.build_services',
        *sorted(TLE_MODULES - {LOGGING_MODULE}),
        *(extension.module for extension in KCPC),
        'tree.sync',
    ]


@pytest.mark.parametrize('cause', ['newer database', 'bug'])
async def test_tle_boots_when_kcpc_fails_to_start(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    cause: str,
) -> None:
    error: type[Exception]
    if cause == 'newer database':
        make_newer_kcpc_db(db_path)
        error = MigrationError
    else:
        error = RuntimeError
        monkeypatch.setattr(
            bootstrap, 'build_services', AsyncMock(side_effect=RuntimeError('boom'))
        )

    async with booted(db_path) as bot:
        assert set(bot.extensions) == TLE_MODULES
        assert bot.kcpc is None
        assert_kcpc_extensions(bot, ())
        await bot.close()

    (record,) = [r for r in caplog.records if r.getMessage() == KCPC_FAILED]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and record.exc_info[0] is error


@pytest.mark.parametrize(
    ('failing', 'cause'),
    [
        ('kcpc.admin', 'cog_load raises'),
        # A feature adds its jobs last, so the steps before its last job must
        # be undone, the other jobs among them.
        ('kcpc.workshops', 'its last job cannot be added'),
        ('kcpc.contests', 'its last job cannot be added'),
        ('kcpc.accounts', 'its last job cannot be added'),
        ('kcpc.problems', 'its last job cannot be added'),
        ('kcpc.notify', 'cog_load raises'),
        ('kcpc.missing', 'package missing'),
    ],
)
async def test_the_bot_boots_without_a_kcpc_extension_that_fails_to_load(
    monkeypatch: pytest.MonkeyPatch,
    stubs: Stubs,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    cause: str,
) -> None:
    error: type[Exception] = commands.ExtensionFailed
    if cause == 'cog_load raises':
        fail_cog_load(monkeypatch, KCPC_BY_NAME[failing].cog)
    elif cause == 'its last job cannot be added':
        fail_to_add_job(monkeypatch, FEATURE_BY_EXTENSION[failing].jobs[-1])
    else:
        # Its package doesn't exist either, so load_extension raises the import
        # error itself rather than an ExtensionError. It comes first, and the
        # extensions after it must still load.
        missing = (failing, 'tle.kcpc.features.missing.cog')
        monkeypatch.setattr(
            extensions, 'KCPC_EXTENSIONS', (missing, *extensions.KCPC_EXTENSIONS)
        )
        error = ModuleNotFoundError
    loaded = [extension for extension in KCPC if extension.name != failing]

    async with booted(db_path) as bot:
        assert set(bot.extensions) == TLE_MODULES | modules_of(loaded)
        assert_kcpc_extensions(bot, loaded)
        # KCPC's services keep running, so that the reconcile job still settles
        # the posts left unconfirmed when the bot last stopped.
        assert bot.kcpc is not None
        assert bot.kcpc.scheduler.running
        assert_features_started(bot, bot.kcpc, loaded)
        stubs.sync.assert_awaited_once_with()

    (record,) = bot_records(caplog, logging.ERROR)
    assert record.getMessage() == (
        f'KCPC extension {failing} failed to load; the bot carries on without it'
    )
    assert record.exc_info is not None and record.exc_info[0] is error


async def test_a_tle_extension_that_fails_to_load_still_stops_the_bot(
    monkeypatch: pytest.MonkeyPatch, stubs: Stubs, db_path: Path
) -> None:
    missing = extensions.Extension(
        'tle.missing', 'tle.cogs.missing', extensions.TLE_FAMILY
    )
    discover = extensions.discover
    monkeypatch.setattr(extensions, 'discover', lambda: [*discover(), missing])

    with pytest.raises(commands.ExtensionNotFound):
        async with booted(db_path):
            pass

    stubs.sync.assert_not_awaited()


async def test_booting_puts_back_the_extension_modules_tests_imported(
    db_path: Path,
) -> None:
    # As tests/integration/test_codeforces_cog.py does before it patches
    # 'tle.cogs.codeforces.cf_common'. Since Python 3.11, mock finds the module
    # to patch with pkgutil.resolve_name.
    names = ('tle.cogs.codeforces', *(extension.module for extension in KCPC))
    imported = {name: importlib.import_module(name) for name in names}

    async with booted(db_path):
        for name, module in imported.items():
            # discord.py loaded a module of its own in its place.
            assert sys.modules[name] is not module

    for name, module in imported.items():
        assert pkgutil.resolve_name(name) is module


async def test_nodb_is_reported(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        async with booted(db_path, nodb=True):
            pass

    assert 'KCPC is disabled with --nodb' in bot_messages(caplog, logging.INFO)
    assert bot_messages(caplog, logging.ERROR) == []


async def test_unknown_disabled_extensions_are_reported(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with booted(db_path, disabled='tle.duel,tle.nope,kcpc.nope') as bot:
        assert set(bot.extensions) == (TLE_MODULES - {'tle.cogs.duel'}) | (
            modules_of(KCPC)
        )

    assert bot_messages(caplog, logging.WARNING) == [
        "Ignoring unknown extension 'kcpc.nope' in DISABLED_EXTENSIONS",
        "Ignoring unknown extension 'tle.nope' in DISABLED_EXTENSIONS",
    ]
