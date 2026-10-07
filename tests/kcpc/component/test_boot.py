"""Boot tests: ``TLEBot.setup_hook`` with different extensions switched off.

The bot boots as ``booting.booted`` boots it: it never logs in, and stand-ins
take the place of TLE's database setup, of Discord's description of the bot's
application and of the slash command sync. The rest runs for real: choosing
the extensions, starting KCPC, loading every cog, the access rules and, at the
end, closing the bot. KCPC's jobs wait for the bot to be ready, which it never
is here, so none of them runs.
"""

import asyncio
import importlib
import logging
import pkgutil
import sqlite3
import sys
from collections.abc import Collection, Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from tests.kcpc.component.booting import OWNER_ID, Stubs, booted
from tle import constants, extensions
from tle.__main__ import TLEBot
from tle.access import slash, table
from tle.access.cog import Access
from tle.access.help import Help
from tle.access.service import AccessService, AccessTree
from tle.access.settings import GuildAccess, decode
from tle.kcpc import bootstrap
from tle.kcpc.bot.cog import KcpcCog
from tle.kcpc.core.errors import MigrationError
from tle.kcpc.core.scheduler import ScheduledJob, Scheduler
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.features.contests.settings import ContestSettings
from tle.kcpc.features.problems.settings import WeeklySettings
from tle.kcpc.features.workshops.settings import WorkshopSettings
from tle.kcpc.services import KcpcServices
from tle.util import discord_common

COGS_DIR = Path(__file__).resolve().parents[3] / 'tle' / 'cogs'
TLE_MODULES = frozenset(
    f'tle.cogs.{path.stem}'
    for path in COGS_DIR.glob('*.py')
    if not path.stem.startswith('_')
)
LOGGING_MODULE = 'tle.cogs.logging'
KCPC_FAILED = 'KCPC failed to start; KCPC extensions will not be loaded'
ACCESS_LOGGER = 'tle.access'
# Real snowflakes are 64-bit, so use big ones.
GUILD_ID = 1_100_000_000_000_000_001
OTHER_GUILD_ID = 1_100_000_000_000_000_002
STAFF_CHANNEL_ID = 1_200_000_000_000_000_010
DEVELOPER_ROLE_ID = 1_300_000_000_000_000_005


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
ALGO = KcpcExtension('kcpc.algo', 'tle.kcpc.features.algo.cog', 'KcpcAlgo', ('algo',))
NOTIFY = KcpcExtension(
    'kcpc.notify', 'tle.kcpc.features.notify.cog', 'KcpcNotify', ('notify',)
)
# In load order.
KCPC = (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, ALGO, NOTIFY)
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
    (
        'contests.sync.codeforces',
        'contests.sync.atcoder',
        'contests.sync.icpc',
        'contests.results',
    ),
    settings=ContestSettings,
    reminders=True,
    admin_commands=frozenset(
        {'add', 'settime', 'remove', 'platforms', 'start-posts', 'results', 'sync'}
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
# The algorithm of the month posts at its own slots too. Its settings are the
# base ones, which default_registry() has.
ALGO_FEATURE = KcpcFeature(
    ALGO,
    'algo',
    ('algo.post',),
    settings=FeatureSettings,
    admin_commands=frozenset({'reroll', 'post-now', 'preview'}),
)
FEATURES = (
    WORKSHOPS_FEATURE,
    CONTESTS_FEATURE,
    ACCOUNTS_FEATURE,
    PROBLEMS_FEATURE,
    ALGO_FEATURE,
)
FEATURE_BY_EXTENSION = {feature.extension.name: feature for feature in FEATURES}


@pytest.fixture
def stubs() -> Stubs:
    """The stand-ins of a test that looks at them: it passes them to booted."""
    return Stubs()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / 'db' / 'kcpc.db'


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


def assert_access_in_place(bot: TLEBot, *, nodb: bool) -> None:
    """The access rules are in place, whichever extensions are switched off:
    the service and its command tree, and /help and /access, prefix and
    slash, which take the place of discord.py's help. Settings are stored in
    the user database, but under --nodb in memory alone.
    """
    assert isinstance(bot.access, AccessService)
    assert isinstance(bot.tree, AccessTree)
    assert bot.help_command is None
    for name, cog in (('help', Help), ('access', Access)):
        command = bot.get_command(name)
        assert command is not None and isinstance(command.cog, cog), name
        assert bot.tree.get_command(name) is not None, name
    assert bot.access.persistent is not nodb


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


def access_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """What the access service logged at WARNING or above."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == ACCESS_LOGGER and record.levelno >= logging.WARNING
    ]


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
            (WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, ALGO, NOTIFY),
        ),
        (
            'kcpc.workshops',
            False,
            TLE_MODULES,
            (ADMIN, CONTESTS, ACCOUNTS, PROBLEMS, ALGO, NOTIFY),
        ),
        (
            'kcpc.contests',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, ACCOUNTS, PROBLEMS, ALGO, NOTIFY),
        ),
        (
            'kcpc.accounts',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, PROBLEMS, ALGO, NOTIFY),
        ),
        (
            'kcpc.problems',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, ALGO, NOTIFY),
        ),
        (
            'kcpc.algo',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, NOTIFY),
        ),
        (
            'kcpc.notify',
            False,
            TLE_MODULES,
            (ADMIN, WORKSHOPS, CONTESTS, ACCOUNTS, PROBLEMS, ALGO),
        ),
        ('kcpc', False, TLE_MODULES, ()),
        (
            'kcpc.admin,kcpc.workshops,kcpc.contests,kcpc.accounts,kcpc.problems,'
            'kcpc.algo,kcpc.notify',
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
        'kcpc.algo',
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
    async with booted(db_path, disabled=disabled, nodb=nodb, stubs=stubs) as bot:
        assert set(bot.extensions) == tle_modules | modules_of(kcpc)
        assert_kcpc_extensions(bot, kcpc)
        assert_access_in_place(bot, nodb=nodb)
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
    attach_user_db = stubs.initialize.side_effect

    async def initialize(bot: TLEBot, nodb: bool) -> None:
        steps.append('cf_common.initialize')
        await attach_user_db(bot, nodb)

    stubs.initialize.side_effect = initialize
    stubs.sync.side_effect = lambda: steps.append('tree.sync')
    real_build_services = bootstrap.build_services
    real_load_extension = TLEBot.load_extension
    real_add_cog = TLEBot.add_cog
    real_load = AccessService.load
    real_resolve_owners = AccessService.resolve_owners
    real_report_unruled = AccessService.report_unruled
    real_apply_visibility = slash.apply_visibility

    async def build_services(*args: Any, **kwargs: Any) -> KcpcServices:
        steps.append('kcpc.build_services')
        return await real_build_services(*args, **kwargs)

    async def load_extension(bot: TLEBot, name: str) -> None:
        steps.append(name)
        await real_load_extension(bot, name)

    async def add_cog(bot: TLEBot, cog: commands.Cog, **options: Any) -> None:
        if isinstance(cog, (Access, Help)):
            steps.append(f'add_cog {cog.qualified_name}')
        await real_add_cog(bot, cog, **options)

    async def load(service: AccessService) -> None:
        steps.append('access.load')
        await real_load(service)

    async def resolve_owners(service: AccessService) -> None:
        steps.append('access.resolve_owners')
        await real_resolve_owners(service)

    def report_unruled(service: AccessService, found: Any) -> list[str]:
        steps.append('access.report_unruled')
        return real_report_unruled(service, found)

    def apply_visibility(bot: commands.Bot) -> Any:
        steps.append('apply_visibility')
        return real_apply_visibility(bot)

    monkeypatch.setattr(bootstrap, 'build_services', build_services)
    monkeypatch.setattr(TLEBot, 'load_extension', load_extension)
    monkeypatch.setattr(TLEBot, 'add_cog', add_cog)
    monkeypatch.setattr(AccessService, 'load', load)
    monkeypatch.setattr(AccessService, 'resolve_owners', resolve_owners)
    monkeypatch.setattr(AccessService, 'report_unruled', report_unruled)
    monkeypatch.setattr('tle.__main__.apply_visibility', apply_visibility)

    async with booted(db_path, stubs=stubs):
        pass

    # The log channel first, so that it hears of problems while starting up.
    # Then the access settings, and /access and /help, which no extension can
    # switch off. KCPC's services come before the extensions that use them,
    # and kcpc.admin before the features that add their admin commands to
    # /kcpc. Once every command is in: the owners, whose commands need them,
    # the report of commands without a rule, and the slash pass, which must
    # come before the sync.
    assert steps == [
        LOGGING_MODULE,
        'cf_common.initialize',
        'access.load',
        'add_cog Access',
        'add_cog Help',
        'kcpc.build_services',
        *sorted(TLE_MODULES - {LOGGING_MODULE}),
        *(extension.module for extension in KCPC),
        'access.resolve_owners',
        'access.report_unruled',
        'apply_visibility',
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
        ('kcpc.algo', 'its last job cannot be added'),
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

    async with booted(db_path, stubs=stubs) as bot:
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
        async with booted(db_path, stubs=stubs):
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


# The access rules


@pytest.mark.parametrize('nodb', [False, True], ids=['database', 'nodb'])
async def test_access_settings_are_stored_in_the_user_database(
    db_path: Path, nodb: bool
) -> None:
    settings = GuildAccess(frozenset({STAFF_CHANNEL_ID + 1}), STAFF_CHANNEL_ID)

    async with booted(db_path, nodb=nodb) as bot:
        # Under --nodb the database refuses every call, so it isn't asked.
        await bot.access.change(GUILD_ID, lambda _: settings)

        assert bot.access.guild_access(GUILD_ID) == settings
        if not nodb:
            rows = await bot.user_db.get_all_access_settings()
            assert [(guild_id, decode(text)) for guild_id, text in rows] == [
                (GUILD_ID, (settings, ()))
            ]


async def test_commands_without_a_rule_are_reported_as_the_bot_boots(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules = dict(table.RULES)
    del rules['gitgud']
    monkeypatch.setattr(table, 'RULES', MappingProxyType(rules))

    with caplog.at_level(logging.WARNING, logger=ACCESS_LOGGER):
        async with booted(db_path):
            pass

    assert access_messages(caplog) == [
        'Command gitgud has no access rule, so only the bot owner can use it, and '
        'only in the staff channel'
    ]


async def test_the_bot_s_owner_is_found_once_as_it_boots(
    stubs: Stubs, db_path: Path
) -> None:
    async with booted(db_path, stubs=stubs) as bot:
        owner = MagicMock(spec=discord.User, id=OWNER_ID)
        member = MagicMock(spec=discord.User, id=OWNER_ID + 1)

        assert await bot.is_owner(owner)
        assert not await bot.is_owner(member)

    # While the bot booted: bot.is_owner never asks Discord.
    stubs.application_info.assert_awaited_once_with()


async def test_owners_that_cannot_be_found_never_stop_the_bot(
    stubs: Stubs, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stubs.application_info.side_effect = discord.HTTPException(
        MagicMock(status=503, reason='Service Unavailable'), 'down'
    )

    async with booted(db_path, stubs=stubs) as bot:
        # Until Discord says who owns the bot, nobody does.
        assert not await bot.is_owner(MagicMock(spec=discord.User, id=OWNER_ID))

    stubs.sync.assert_awaited_once_with()
    stubs.application_info.assert_awaited_once_with()
    (warning,) = access_messages(caplog)
    assert warning.startswith("Could not find the bot's owners (")


async def test_a_failure_to_find_the_owners_is_logged_and_the_bot_boots(
    monkeypatch: pytest.MonkeyPatch,
    stubs: Stubs,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The service logs its own failures; this is the bot's second safety.
    monkeypatch.setattr(
        AccessService, 'resolve_owners', AsyncMock(side_effect=RuntimeError('boom'))
    )

    async with booted(db_path, stubs=stubs):
        pass

    stubs.sync.assert_awaited_once_with()
    (record,) = bot_records(caplog, logging.ERROR)
    assert record.getMessage() == "Could not find the bot's owners"
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


@pytest.mark.parametrize(
    ('value', 'role', 'warned'),
    [
        ('Developers', None, True),
        ('', None, False),
        (str(DEVELOPER_ROLE_ID), DEVELOPER_ROLE_ID, False),
    ],
    ids=['not an id', 'blank', 'an id'],
)
async def test_an_unusable_developer_role_is_reported_once_logging_is_set_up(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: str,
    role: int | None,
    warned: bool,
) -> None:
    # As tle.constants reads it on import, before logging is set up, and
    # warns about a value that isn't an id where only the console sees it.
    monkeypatch.setenv('TLE_DEVELOPER', value)
    monkeypatch.setattr(constants, 'TLE_DEVELOPER', role)

    async with booted(db_path):
        pass

    warning = "TLE_DEVELOPER must be a role ID, not 'Developers', so it is ignored"
    assert (warning in bot_messages(caplog, logging.WARNING)) is warned


# ALLOWED_GUILD_IDS


def make_guild(guild_id: int, name: str) -> MagicMock:
    guild = MagicMock(spec=discord.Guild, id=guild_id)
    guild.name = name
    guild.leave = AsyncMock()
    return guild


async def test_the_bot_leaves_a_server_that_is_not_allowed_as_it_joins(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    listed = make_guild(GUILD_ID, 'The club')
    unlisted = make_guild(OTHER_GUILD_ID, 'Elsewhere')

    async with booted(db_path, allowed_guilds=str(GUILD_ID)) as bot:
        await bot.on_guild_join(listed)
        await bot.on_guild_join(unlisted)

    listed.leave.assert_not_awaited()
    unlisted.leave.assert_awaited_once_with()
    assert bot_messages(caplog, logging.WARNING) == [
        f'Leaving the server Elsewhere ({OTHER_GUILD_ID}), which ALLOWED_GUILD_IDS '
        'does not list'
    ]


async def test_without_allowed_guild_ids_the_bot_stays_in_every_server(
    db_path: Path,
) -> None:
    guild = make_guild(OTHER_GUILD_ID, 'Elsewhere')

    async with booted(db_path) as bot:
        await bot.on_guild_join(guild)

    guild.leave.assert_not_awaited()


async def test_a_server_the_bot_cannot_leave_is_only_logged(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    guild = make_guild(OTHER_GUILD_ID, 'Elsewhere')
    guild.leave.side_effect = discord.HTTPException(
        MagicMock(status=500, reason='Server Error'), 'oops'
    )

    async with booted(db_path, allowed_guilds=str(GUILD_ID)) as bot:
        await bot.on_guild_join(guild)  # raises nothing

    assert bot_messages(caplog, logging.WARNING)[-1] == (
        f'Could not leave the server {OTHER_GUILD_ID}: '
        '500 Server Error (error code: 0): oops'
    )


@pytest.mark.parametrize(
    ('allowed', 'named'), [(str(GUILD_ID), True), ('', False)], ids=['set', 'unset']
)
async def test_once_ready_the_bot_names_the_servers_that_are_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    allowed: str,
    named: bool,
) -> None:
    listed = make_guild(GUILD_ID, 'The club')
    unlisted = make_guild(OTHER_GUILD_ID, 'Elsewhere')
    monkeypatch.setattr(TLEBot, 'guilds', property(lambda bot: [listed, unlisted]))
    presence = AsyncMock()
    monkeypatch.setattr(discord_common, 'presence', presence)

    async with booted(db_path, allowed_guilds=allowed) as bot:
        await bot.on_ready()
        await bot.on_ready()  # after a reconnect, which changes nothing
        await asyncio.sleep(0)  # the status's turn to start

    # It stays in every one, so that a mistake in the setting can't make it
    # leave the club's server.
    listed.leave.assert_not_awaited()
    unlisted.leave.assert_not_awaited()
    presence.assert_awaited_once_with(bot)
    named_them = (
        'ALLOWED_GUILD_IDS does not list these servers, so the bot ignores '
        f'commands there: Elsewhere ({OTHER_GUILD_ID})'
    )
    assert bot_messages(caplog, logging.WARNING) == ([named_them] if named else [])
