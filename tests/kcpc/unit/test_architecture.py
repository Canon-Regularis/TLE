"""The import rules between KCPC's layers, checked on the source with ast.

Every import in tle/kcpc counts, wherever it is: at the top of a module, further
down, inside a function or under ``if TYPE_CHECKING:``. The rules (outlined
in tle/kcpc/__init__.py):

- core imports only the standard library, aiosqlite, aiohttp and core;
- bot imports only the standard library, discord, aiohttp, core, bot,
  tle.util.discord_common and tle.constants, and tle.kcpc.services only
  lazily (inside a function or for type checking), since services imports bot;
- platforms import no discord, and from TLE only core, platforms and the
  Codeforces modules tle.util.codeforces_api and tle.util.cache;
- a feature imports from tle.kcpc only core, bot, platforms and itself, and
  only its cog.py and views.py import discord;
- services.py and bootstrap.py, which assemble everything, may import anything
  but TLE's user database and its access package (below).

The contests feature, and no other KCPC module, may import TLE's event system,
tle.util.events, to hear when TLE has saved a contest's rating changes.

There is one exception. tle.kcpc.bot.codeforces_links, KCPC's only way to the
Codeforces handles in TLE's user database, may also import
tle.util.handle_linking and tle.util.codeforces_api, and no other KCPC module
may import tle.util.handle_linking. No KCPC module imports what reaches TLE's
user database otherwise, tle.util.db, tle.util.codeforces_common (which holds
it) or TLE's cogs, and only the bridge uses the database the bot carries, as
``bot.user_db``.

No KCPC module imports TLE's access package, tle.access, either. Where KCPC
needs the access service, it reaches it only as the bot's ``access``
attribute, through ``getattr(bot, 'access', None)``.

Relative imports are not allowed anywhere. TLE's cogs import KCPC only lazily.
One more test checks, in a fresh interpreter, that tle.util.discord_common can
be imported first.
"""

import ast
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
KCPC_DIR = REPO_ROOT / 'tle' / 'kcpc'
TLE_COGS_DIR = REPO_ROOT / 'tle' / 'cogs'
STDLIB = frozenset(sys.stdlib_module_names)
NOTIFY = 'tle.kcpc.features.notify'
WORKSHOPS = 'tle.kcpc.features.workshops'
CONTESTS = 'tle.kcpc.features.contests'
ACCOUNTS = 'tle.kcpc.features.accounts'
PROBLEMS = 'tle.kcpc.features.problems'
ALGO = 'tle.kcpc.features.algo'
CODEFORCES = 'tle.kcpc.platforms.codeforces'
ATCODER = 'tle.kcpc.platforms.atcoder'
ICPC = 'tle.kcpc.platforms.icpc'
CODEFORCES_LINKS = 'tle.kcpc.bot.codeforces_links'
HANDLES = 'tle.kcpc.core.handles'
HANDLE_LINKING = 'tle.util.handle_linking'
TLE_CODEFORCES = 'tle.util.codeforces_api'
TLE_EVENTS = 'tle.util.events'
# What reaches TLE's user database, besides the bridge's handle linking.
TLE_USER_DB = ('tle.util.db', 'tle.util.codeforces_common', 'tle.cogs')
# TLE's access rules and service, which KCPC reaches only through the bot.
TLE_ACCESS = 'tle.access'


@dataclass(frozen=True)
class ImportRef:
    """One imported name: ``from a.b import c`` imports ``a.b.c``."""

    module: str  # the importing module, e.g. 'tle.kcpc.bot.cog'
    target: str  # e.g. 'tle.kcpc.core.errors.KcpcUserError'
    line: int
    deferred: bool  # inside a function, or under `if TYPE_CHECKING:`
    relative: bool

    def __str__(self) -> str:
        return f'{self.module}:{self.line} imports {self.target}'


class _ImportCollector(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self._module = module
        self._deferred_depth = 0
        self.refs: list[ImportRef] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_deferred(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_deferred(node.body)

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking(node.test):
            self.generic_visit(node)
            return
        self._visit_deferred(node.body)
        for statement in node.orelse:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name, node, relative=False)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ''
        for alias in node.names:
            name = alias.name
            target = base if name == '*' else f'{base}.{name}' if base else name
            self._add(target, node, relative=node.level > 0)

    def _visit_deferred(self, body: list[ast.stmt]) -> None:
        self._deferred_depth += 1
        for statement in body:
            self.visit(statement)
        self._deferred_depth -= 1

    def _add(self, target: str, node: ast.stmt, *, relative: bool) -> None:
        self.refs.append(
            ImportRef(
                module=self._module,
                target=target,
                line=node.lineno,
                deferred=self._deferred_depth > 0,
                relative=relative,
            )
        )


def _is_type_checking(test: ast.expr) -> bool:
    """``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``."""
    if isinstance(test, ast.Name):
        return test.id == 'TYPE_CHECKING'
    return isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING'


def imports_in(module: str, source: str) -> list[ImportRef]:
    collector = _ImportCollector(module)
    collector.visit(ast.parse(source))
    return collector.refs


def module_name(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix('').parts
    return '.'.join(parts[:-1] if parts[-1] == '__init__' else parts)


def kcpc_modules() -> Iterator[tuple[str, Path]]:
    for path in sorted(KCPC_DIR.rglob('*.py')):
        yield module_name(path), path


def kcpc_imports() -> Iterator[ImportRef]:
    """Every import in every KCPC module."""
    for module, path in kcpc_modules():
        yield from imports_in(module, path.read_text(encoding='utf-8'))


def _under(name: str, *packages: str) -> bool:
    """Whether ``name`` is one of ``packages`` or inside one."""
    return any(
        name == package or name.startswith(f'{package}.') for package in packages
    )


def _is_stdlib(name: str) -> bool:
    return name.split('.')[0] in STDLIB


def violation(ref: ImportRef) -> str | None:
    """How ``ref`` breaks the rules, or None if it is allowed."""
    if ref.relative:
        return 'relative imports are not allowed'
    if _under(ref.target, HANDLE_LINKING) and ref.module != CODEFORCES_LINKS:
        return f'only {CODEFORCES_LINKS} may import {HANDLE_LINKING}'
    if _under(ref.target, *TLE_USER_DB):
        return f"KCPC reaches TLE's user database only through {CODEFORCES_LINKS}"
    if _under(ref.target, TLE_ACCESS):
        return (
            f'KCPC never imports {TLE_ACCESS}: it reaches the access service as '
            "the bot's access attribute"
        )
    if _under(ref.target, TLE_EVENTS) and not _under(ref.module, CONTESTS):
        return f'only {CONTESTS} may import {TLE_EVENTS}'
    for layer, check in _LAYER_CHECKS:
        if _under(ref.module, layer):
            return check(ref)
    return None  # tle.kcpc itself, services.py and bootstrap.py


_CORE_ALLOWED = ('aiosqlite', 'aiohttp', 'tle.kcpc.core')
_BOT_ALLOWED = (
    'discord',
    'aiohttp',
    'tle.kcpc.core',
    'tle.kcpc.bot',
    'tle.util.discord_common',
    'tle.constants',
)
# What the bridge to TLE's handle table may import besides: TLE's handle
# linking, and its Codeforces client to look up the accounts it links.
_BRIDGE_ALLOWED = (HANDLE_LINKING, TLE_CODEFORCES)
_PLATFORM_ALLOWED_FROM_TLE = (
    'tle.kcpc.core',
    'tle.kcpc.platforms',
    'tle.util.codeforces_api',
    'tle.util.cache',
)
_FEATURE_ALLOWED_FROM_KCPC = ('tle.kcpc.core', 'tle.kcpc.bot', 'tle.kcpc.platforms')


def _check_core(ref: ImportRef) -> str | None:
    if _is_stdlib(ref.target) or _under(ref.target, *_CORE_ALLOWED):
        return None
    return 'core may import only the standard library, aiosqlite, aiohttp and core'


def _check_bot(ref: ImportRef) -> str | None:
    if _under(ref.target, 'tle.kcpc.services'):
        if ref.deferred:
            return None
        return (
            'bot may import tle.kcpc.services only in a function or for type '
            'checking, since services imports bot'
        )
    if ref.module == CODEFORCES_LINKS and _under(ref.target, *_BRIDGE_ALLOWED):
        return None
    if _is_stdlib(ref.target) or _under(ref.target, *_BOT_ALLOWED):
        return None
    return (
        'bot may import only the standard library, discord, aiohttp, core, bot, '
        'tle.util.discord_common and tle.constants'
    )


def _check_platform(ref: ImportRef) -> str | None:
    if _under(ref.target, 'discord'):
        return 'platforms must not import discord'
    if _under(ref.target, 'tle') and not _under(
        ref.target, *_PLATFORM_ALLOWED_FROM_TLE
    ):
        return (
            'platforms may import from TLE only core, platforms, '
            'tle.util.codeforces_api and tle.util.cache'
        )
    return None


def _check_feature(ref: ImportRef) -> str | None:
    parts = ref.module.split('.')  # tle.kcpc.features.<feature>.<module>
    if _under(ref.target, 'discord') and parts[-1] not in ('cog', 'views'):
        return "only a feature's cog.py and views.py may import discord"
    own_feature = ['.'.join(parts[:4])] if len(parts) > 3 else []
    if _under(ref.target, 'tle.kcpc') and not _under(
        ref.target, *_FEATURE_ALLOWED_FROM_KCPC, *own_feature
    ):
        return 'a feature may import from tle.kcpc only core, bot, platforms and itself'
    return None


_LAYER_CHECKS: tuple[tuple[str, Callable[[ImportRef], str | None]], ...] = (
    ('tle.kcpc.core', _check_core),
    ('tle.kcpc.bot', _check_bot),
    ('tle.kcpc.platforms', _check_platform),
    ('tle.kcpc.features', _check_feature),
)


def test_the_rules_are_checked_on_real_modules() -> None:
    modules = [name for name, _ in kcpc_modules()]

    # Guards against a path mistake that would leave a layer unchecked.
    for expected in (
        'tle.kcpc.core.db',
        'tle.kcpc.core.reminders',
        HANDLES,
        'tle.kcpc.bot.cog',
        CODEFORCES_LINKS,
        'tle.kcpc.platforms.luma',
        'tle.kcpc.platforms.difficulty',
        CODEFORCES,
        ATCODER,
        f'{ATCODER}.contests',
        f'{ATCODER}.profile',
        f'{ATCODER}.problems',
        f'{ATCODER}.editorials',
        ICPC,
        'tle.kcpc.features.admin.cog',
        f'{WORKSHOPS}.sync',
        f'{WORKSHOPS}.cog',
        f'{CONTESTS}.sources',
        f'{CONTESTS}.results_repo',
        f'{CONTESTS}.results',
        f'{CONTESTS}.cog',
        f'{ACCOUNTS}.repo',
        f'{ACCOUNTS}.service',
        f'{ACCOUNTS}.views',
        f'{ACCOUNTS}.cog',
        f'{PROBLEMS}.settings',
        f'{PROBLEMS}.repo',
        f'{PROBLEMS}.topics',
        f'{PROBLEMS}.rotation',
        f'{PROBLEMS}.catalog',
        f'{PROBLEMS}.randomizer',
        f'{PROBLEMS}.solved',
        f'{PROBLEMS}.editorials',
        f'{PROBLEMS}.markdown',
        f'{PROBLEMS}.weekly',
        f'{PROBLEMS}.cog',
        f'{ALGO}.catalog',
        f'{ALGO}.markdown',
        f'{ALGO}.repo',
        f'{ALGO}.service',
        f'{ALGO}.cog',
        'tle.kcpc.core.migrations.m0006_algo',
        'tle.kcpc.core.migrations.m0007_contest_results',
        f'{NOTIFY}.cog',
        'tle.kcpc.services',
        'tle.kcpc.bootstrap',
    ):
        assert expected in modules


def test_no_kcpc_module_breaks_the_layering_rules() -> None:
    problems = [
        f'{ref}: {problem}'
        for ref in kcpc_imports()
        if (problem := violation(ref)) is not None
    ]

    assert problems == []


def test_only_the_bridge_uses_tles_handle_linking() -> None:
    # KCPC links and reads members' Codeforces handles through
    # tle.kcpc.bot.codeforces_links alone, the one bot module that may use
    # TLE's handle linking and its Codeforces client.
    refs = list(kcpc_imports())
    handle_linking = {ref.module for ref in refs if _under(ref.target, HANDLE_LINKING)}
    bot_codeforces = {
        ref.module
        for ref in refs
        if _under(ref.module, 'tle.kcpc.bot') and _under(ref.target, TLE_CODEFORCES)
    }

    assert handle_linking == bot_codeforces == {CODEFORCES_LINKS}


def test_only_the_contests_feature_uses_tles_events() -> None:
    # Its cog listens for TLE's word that a contest's rating changes are saved.
    users = {ref.module for ref in kcpc_imports() if _under(ref.target, TLE_EVENTS)}

    assert users == {f'{CONTESTS}.cog'}


def test_no_kcpc_module_imports_tles_access_package() -> None:
    # KCPC asks the bot for the access service, as getattr(bot, 'access',
    # None), so it loads and runs whether or not the bot has one.
    refs = [str(ref) for ref in kcpc_imports() if _under(ref.target, TLE_ACCESS)]

    assert refs == []


def mentions_user_db(node: ast.AST) -> bool:
    """Whether ``node`` names ``user_db``, as ``bot.user_db`` or ``'user_db'`` do."""
    return (
        (isinstance(node, ast.Attribute) and node.attr == 'user_db')
        or (isinstance(node, ast.Name) and node.id == 'user_db')
        or (isinstance(node, ast.Constant) and node.value == 'user_db')
    )


def test_only_the_bridge_uses_tles_user_db() -> None:
    # TLE attaches its user database to the bot, where no import rule can see
    # a module reach it.
    users = {
        module
        for module, path in kcpc_modules()
        if any(
            mentions_user_db(node)
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        )
    }

    assert users == {CODEFORCES_LINKS}


@pytest.mark.parametrize(
    'source',
    [
        'self.bot.user_db.get_handle(1, 2)',
        "getattr(bot, 'user_db', None)",
        'user_db = None',
    ],
)
def test_uses_of_tles_user_db_are_found(source: str) -> None:
    assert any(mentions_user_db(node) for node in ast.walk(ast.parse(source)))


def test_notify_never_imports_the_features_it_serves() -> None:
    # /notify serves every feature through the settings registry alone, so it
    # keeps working with kcpc.workshops, kcpc.contests, kcpc.problems or
    # kcpc.algo disabled, or failing to load.
    refs = [ref for ref in kcpc_imports() if _under(ref.module, NOTIFY)]

    assert refs, 'the notify package has imports to check'
    served = [
        ref for ref in refs if _under(ref.target, WORKSHOPS, CONTESTS, PROBLEMS, ALGO)
    ]
    assert [str(ref) for ref in served] == []


def test_problems_never_imports_accounts() -> None:
    # /randproblem reads members' AtCoder handles through KcpcServices.handles,
    # where the accounts cog registers its service, so it keeps working, if
    # without leaving solved AtCoder problems out, with kcpc.accounts disabled
    # or failing to load.
    refs = [ref for ref in kcpc_imports() if _under(ref.module, PROBLEMS)]

    assert refs, 'the problems package has imports to check'
    assert [str(ref) for ref in refs if _under(ref.target, ACCOUNTS)] == []


def test_algo_never_imports_another_feature() -> None:
    # It copies the few helpers it shares with the problems feature, so that
    # it loads, and works, whichever other features are disabled.
    refs = [ref for ref in kcpc_imports() if _under(ref.module, ALGO)]

    assert refs, 'the algo package has imports to check'
    features = [
        ref
        for ref in refs
        if _under(ref.target, 'tle.kcpc.features') and not _under(ref.target, ALGO)
    ]
    assert [str(ref) for ref in features] == []


def test_tles_cogs_import_kcpc_only_lazily() -> None:
    # A TLE extension that fails to load stops the bot, and a KCPC one
    # doesn't. Importing KCPC only when a command runs, as /handle show does to
    # list a member's KCPC accounts, keeps a broken KCPC module from stopping
    # TLE.
    refs = [
        ref
        for path in sorted(TLE_COGS_DIR.glob('*.py'))
        for ref in imports_in(module_name(path), path.read_text(encoding='utf-8'))
        if _under(ref.target, 'tle.kcpc')
    ]

    assert refs, "TLE's cogs have KCPC imports to check"
    assert [str(ref) for ref in refs if not ref.deferred] == []


def test_imports_anywhere_in_a_module_are_found() -> None:
    source = '\n'.join(
        [
            'import os',
            'from typing import TYPE_CHECKING',
            'if TYPE_CHECKING:',
            '    from a import b',
            'else:',
            '    import c',
            'if typing.TYPE_CHECKING:',
            '    import d',
            'class C:',
            '    import e',
            'def f():',
            '    import g',
            'async def h():',
            '    from i import j, k',
            'import l.m as n',
            'from o import *',
        ]
    )

    refs = imports_in('tle.kcpc.x', source)

    assert [(ref.target, ref.line, ref.deferred) for ref in refs] == [
        ('os', 1, False),
        ('typing.TYPE_CHECKING', 2, False),
        ('a.b', 4, True),
        ('c', 6, False),
        ('d', 8, True),
        ('e', 10, False),
        ('g', 12, True),
        ('i.j', 14, True),
        ('i.k', 14, True),
        ('l.m', 15, False),
        ('o', 16, False),
    ]


def test_deferred_imports_in_real_modules_are_found() -> None:
    refs = {(ref.module, ref.target, ref.deferred) for ref in kcpc_imports()}

    # The base cog gets the services on use and their type for type checking,
    # since tle.kcpc.services imports the bot package.
    assert ('tle.kcpc.bot.cog', 'tle.kcpc.services.get_services', True) in refs
    assert ('tle.kcpc.bot.cog', 'tle.kcpc.services.KcpcServices', True) in refs


def test_tles_discord_common_imports_on_its_own() -> None:
    # The bot layer may import it. That used to fail unless codeforces_common
    # was imported first, because of the cycle tasks -> codeforces_common ->
    # cache -> tasks; importing it first in a fresh interpreter keeps it fixed.
    result = subprocess.run(
        [sys.executable, '-B', '-c', 'import tle.util.discord_common'],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ('module', 'source', 'allowed'),
    [
        ('tle.kcpc.core.x', 'import asyncio', True),
        ('tle.kcpc.core.x', 'from collections.abc import Sequence', True),
        ('tle.kcpc.core.x', 'import aiosqlite', True),
        ('tle.kcpc.core.x', 'from aiohttp import web', True),
        ('tle.kcpc.core.x', 'from tle.kcpc.core.db import Database', True),
        ('tle.kcpc.core.x', 'import discord', False),
        ('tle.kcpc.core.x', 'def f():\n    import discord', False),
        ('tle.kcpc.core.x', 'from tle.kcpc.bot import embeds', False),
        ('tle.kcpc.core.x', 'from tle.kcpc.features.admin import cog', False),
        ('tle.kcpc.core.x', 'from tle.kcpc.platforms import base', False),
        ('tle.kcpc.core.x', 'from tle.kcpc import services', False),
        ('tle.kcpc.core.x', 'from tle.util import db', False),
        ('tle.kcpc.core.x', 'import numpy', False),
        ('tle.kcpc.bot.x', 'from discord.ext import commands', True),
        ('tle.kcpc.bot.x', 'import aiohttp', True),
        ('tle.kcpc.bot.x', 'from tle.kcpc.core.ledger import Delivery', True),
        ('tle.kcpc.bot.x', 'from tle.kcpc.bot.embeds import to_embed', True),
        ('tle.kcpc.bot.x', 'from tle.util import discord_common', True),
        ('tle.kcpc.bot.x', 'from tle import constants', True),
        ('tle.kcpc.bot.x', 'from tle.util import codeforces_common', False),
        ('tle.kcpc.bot.x', 'from tle.kcpc.features.admin import cog', False),
        ('tle.kcpc.bot.x', 'from tle.kcpc.services import get_services', False),
        (
            'tle.kcpc.bot.x',
            'def f():\n    from tle.kcpc.services import get_services',
            True,
        ),
        (
            'tle.kcpc.bot.x',
            'if TYPE_CHECKING:\n    from tle.kcpc.services import KcpcServices',
            True,
        ),
        # The bridge to TLE's handle table, and only it, may also use TLE's
        # handle linking and Codeforces client.
        (CODEFORCES_LINKS, 'from tle.util import codeforces_api as cf', True),
        (CODEFORCES_LINKS, 'from tle.util import handle_linking', True),
        (CODEFORCES_LINKS, 'from tle.util.handle_linking import link_handle', True),
        (CODEFORCES_LINKS, 'import tle.util.codeforces_api', True),
        (CODEFORCES_LINKS, 'from tle.kcpc.core.errors import KcpcUserError', True),
        (CODEFORCES_LINKS, 'import discord', True),
        (CODEFORCES_LINKS, 'from tle.util import codeforces_common', False),
        (CODEFORCES_LINKS, 'from tle.util import db', False),
        (CODEFORCES_LINKS, 'from tle.util.cache import CacheSystem', False),
        (CODEFORCES_LINKS, f'from {ACCOUNTS}.repo import AccountRepo', False),
        (CODEFORCES_LINKS, 'from tle.kcpc.services import get_services', False),
        ('tle.kcpc.bot.x', 'from tle.util import handle_linking', False),
        ('tle.kcpc.bot.x', 'from tle.util import codeforces_api', False),
        (
            'tle.kcpc.bot.cog',
            'def f():\n    from tle.util import handle_linking',
            False,
        ),
        ('tle.kcpc.bot.pages', 'import tle.util.codeforces_api', False),
        ('tle.kcpc.bot', 'from tle.util import codeforces_api', False),
        (f'{CODEFORCES_LINKS}.x', 'from tle.util import handle_linking', False),
        ('tle.kcpc.platforms.x', 'import icalendar', True),
        ('tle.kcpc.platforms.x', 'from tle.kcpc.core.http import HttpClient', True),
        ('tle.kcpc.platforms.x', 'from tle.util import codeforces_api', True),
        ('tle.kcpc.platforms.x', 'from tle.util.cache import CacheSystem', True),
        ('tle.kcpc.platforms.x', 'import discord', False),
        ('tle.kcpc.platforms.x', 'def f():\n    import discord', False),
        ('tle.kcpc.platforms.x', 'from tle.kcpc.bot import embeds', False),
        ('tle.kcpc.platforms.x', 'from tle.util import discord_common', False),
        ('tle.kcpc.platforms.x', 'from tle.kcpc.features.workshops import repo', False),
        ('tle.kcpc.platforms.x', 'from tle.kcpc import services', False),
        ('tle.kcpc.platforms.luma', 'from icalendar import Calendar', True),
        ('tle.kcpc.platforms.luma', 'from tle.kcpc.core.http import HttpClient', True),
        # The Codeforces adapter reads TLE's cached contests, but nothing that
        # reaches TLE's database or Discord.
        (CODEFORCES, 'from tle.util import codeforces_api as cf', True),
        (CODEFORCES, 'from tle.util import codeforces_common', False),
        (CODEFORCES, f'from {CONTESTS}.repo import ContestInfo', False),
        (f'{ATCODER}.contests', 'from html.parser import HTMLParser', True),
        (f'{ATCODER}.contests', 'from tle.kcpc.core.http import HttpClient', True),
        (f'{ATCODER}.contests', 'import discord', False),
        (f'{ATCODER}.profile', 'from html.parser import HTMLParser', True),
        (f'{ATCODER}.profile', 'from tle.kcpc.core.http import HttpClient', True),
        (f'{ATCODER}.profile', 'import discord', False),
        (f'{ATCODER}.profile', 'from tle.kcpc.bot import codeforces_links', False),
        (f'{ATCODER}.profile', f'from {ACCOUNTS}.repo import AccountRepo', False),
        (CODEFORCES, 'from tle.util import handle_linking', False),
        (ICPC, 'from tle.kcpc.core.errors import ExternalServiceError', True),
        (ICPC, 'from tle.kcpc.bot.embeds import to_embed', False),
        ('tle.kcpc.features.a.cog', 'import discord', True),
        ('tle.kcpc.features.a.views', 'from discord import ui', True),
        ('tle.kcpc.features.a.service', 'import discord', False),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc.features.a import service', True),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc.bot.cog import KcpcCog', True),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc.platforms import luma', True),
        ('tle.kcpc.features.a.cog', 'from tle.util import paginator', True),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc.features.b import service', False),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc.features import b', False),
        ('tle.kcpc.features.a.cog', 'from tle.kcpc import services', False),
        ('tle.kcpc.features', 'from tle.kcpc.features import a', False),
        (f'{WORKSHOPS}.sync', 'from tle.kcpc.platforms.luma import LumaEvent', True),
        (f'{WORKSHOPS}.reminders', 'from tle.kcpc.core.reminders import Notice', True),
        (f'{WORKSHOPS}.reminders', 'import discord', False),
        (f'{WORKSHOPS}.cog', f'from {WORKSHOPS}.sync import EventSync', True),
        (f'{WORKSHOPS}.cog', 'from tle.kcpc.bot.admin import attach_admin_group', True),
        (f'{WORKSHOPS}.cog', f'from {NOTIFY} import cog', False),
        (f'{NOTIFY}.cog', f'from {WORKSHOPS}.settings import WORKSHOPS', False),
        (f'{NOTIFY}.cog', f'def f():\n    from {WORKSHOPS} import cog', False),
        (f'{NOTIFY}.cog', f'import {WORKSHOPS}.settings', False),
        (f'{NOTIFY}.cog', f'from {CONTESTS}.settings import CONTESTS', False),
        (f'{CONTESTS}.sources', 'from tle.kcpc.platforms.icpc import IcpcClient', True),
        (f'{CONTESTS}.sources', 'from tle.util import codeforces_api', True),
        (f'{CONTESTS}.sync', 'from tle.kcpc.core.db import Database', True),
        (f'{CONTESTS}.sync', 'import discord', False),
        (f'{CONTESTS}.reminders', 'from tle.kcpc.core.reminders import Notice', True),
        (f'{CONTESTS}.reminders', 'import discord', False),
        (f'{CONTESTS}.cog', f'from {CONTESTS}.sync import ContestSync', True),
        (f'{CONTESTS}.cog', 'from tle.kcpc.bot.admin import attach_admin_group', True),
        (f'{CONTESTS}.cog', f'from {WORKSHOPS}.settings import WORKSHOPS', False),
        # TLE's event system, which says when a contest's rating changes are
        # saved, is the contests feature's alone.
        (f'{CONTESTS}.cog', 'from tle.util import events', True),
        (f'{CONTESTS}.cog', 'import tle.util.events', True),
        (f'{CONTESTS}.results', 'from tle.util.events import Listener', True),
        (f'{CONTESTS}.results', 'from tle.kcpc.bot import codeforces_links', True),
        (f'{CONTESTS}.results', f'from {ATCODER}.profile import PROFILE_URL', True),
        (f'{CONTESTS}.results', 'import discord', False),
        (f'{CONTESTS}.results_repo', 'import discord', False),
        (f'{CONTESTS}.results', f'from {ACCOUNTS}.service import X', False),
        (f'{PROBLEMS}.cog', 'from tle.util import events', False),
        (f'{ACCOUNTS}.cog', 'def f():\n    from tle.util import events', False),
        ('tle.kcpc.features.a.cog', 'from tle.util.events import Listener', False),
        ('tle.kcpc.bot.x', 'from tle.util import events', False),
        ('tle.kcpc.platforms.x', 'from tle.util import events', False),
        ('tle.kcpc.core.x', 'from tle.util import events', False),
        ('tle.kcpc.services', 'import tle.util.events', False),
        ('tle.kcpc.bootstrap', 'from tle.util import events', False),
        (f'{WORKSHOPS}.cog', f'from {CONTESTS}.repo import ContestRepo', False),
        # Account linking reaches TLE's handle table through the bridge alone.
        (f'{ACCOUNTS}.cog', 'from tle.kcpc.bot import codeforces_links', True),
        (f'{ACCOUNTS}.cog', f'from {ACCOUNTS}.service import AccountService', True),
        (f'{ACCOUNTS}.views', 'from discord import ui', True),
        (f'{ACCOUNTS}.views', 'from tle.kcpc.bot.views import KcpcView', True),
        (
            f'{ACCOUNTS}.service',
            f'from {ATCODER}.profile import AtCoderProfileClient',
            True,
        ),
        (f'{ACCOUNTS}.service', f'from {CODEFORCES} import fetch_user', True),
        (f'{ACCOUNTS}.refresh', f'from {CODEFORCES} import fetch_users', True),
        (f'{ACCOUNTS}.service', 'import discord', False),
        (f'{ACCOUNTS}.refresh', 'import discord', False),
        (f'{ACCOUNTS}.directory', 'import discord', False),
        (f'{ACCOUNTS}.repo', 'import discord', False),
        (f'{ACCOUNTS}.cog', 'from tle.util import handle_linking', False),
        (f'{ACCOUNTS}.service', 'def f():\n    import tle.util.handle_linking', False),
        (f'{ACCOUNTS}.cog', f'from {CONTESTS}.repo import ContestRepo', False),
        (f'{CONTESTS}.cog', f'from {ACCOUNTS}.directory import linked_accounts', False),
        # Members' handles reach the problems feature through the bridge to
        # TLE's handle table and through core's handle registry, never from
        # the accounts feature itself; the features know nothing of each other.
        (f'{PROBLEMS}.cog', 'from tle.kcpc.bot import codeforces_links', True),
        (f'{PROBLEMS}.cog', f'from {HANDLES} import HandleRegistry', True),
        (f'{PROBLEMS}.cog', f'from {PROBLEMS}.weekly import WeeklyService', True),
        (f'{PROBLEMS}.cog', f'from {ATCODER}.editorials import ID_RE', True),
        (f'{PROBLEMS}.catalog', f'from {CODEFORCES} import fetch_problems', True),
        (f'{PROBLEMS}.solved', f'from {ATCODER} import problems', True),
        (f'{PROBLEMS}.weekly', 'from tle.kcpc.core.publishing import Publisher', True),
        (f'{PROBLEMS}.weekly', 'import discord', False),
        (f'{PROBLEMS}.catalog', 'import discord', False),
        (f'{PROBLEMS}.repo', 'import discord', False),
        (f'{PROBLEMS}.cog', f'from {ACCOUNTS}.service import AccountService', False),
        (f'{PROBLEMS}.cog', f'def f():\n    from {ACCOUNTS} import cog', False),
        (f'{PROBLEMS}.weekly', f'from {CONTESTS}.repo import ContestRepo', False),
        (f'{PROBLEMS}.cog', f'from {CONTESTS}.settings import CONTESTS', False),
        (f'{ACCOUNTS}.cog', f'from {PROBLEMS}.solved import SolvedProblems', False),
        (f'{CONTESTS}.cog', f'from {PROBLEMS}.settings import WEEKLY', False),
        (f'{NOTIFY}.cog', f'from {PROBLEMS}.settings import WEEKLY', False),
        # The algorithm of the month copies the problems feature's markdown
        # helpers rather than import them.
        (f'{ALGO}.cog', f'from {ALGO}.service import AlgoService', True),
        (f'{ALGO}.service', 'from tle.kcpc.core.publishing import Publisher', True),
        (f'{ALGO}.service', f'from {ALGO} import markdown', True),
        (f'{ALGO}.service', f'from {PROBLEMS} import markdown', False),
        (f'{ALGO}.cog', f'from {PROBLEMS}.markdown import link', False),
        (f'{ALGO}.service', 'import discord', False),
        (f'{ALGO}.catalog', 'import discord', False),
        (f'{ALGO}.repo', 'import discord', False),
        (f'{NOTIFY}.cog', f'from {ALGO}.service import ALGO', False),
        (f'{PROBLEMS}.cog', f'from {ALGO}.catalog import topic', False),
        (HANDLES, 'from typing import Protocol', True),
        (HANDLES, 'import discord', False),
        (HANDLES, f'from {ACCOUNTS}.service import AccountService', False),
        ('tle.kcpc.services', 'from tle.kcpc.bot.publisher import X', True),
        ('tle.kcpc.bootstrap', 'from tle.kcpc.features.admin import cog', True),
        ('tle.kcpc.bootstrap', f'from {CONTESTS}.settings import SPEC', True),
        ('tle.kcpc.bootstrap', f'from {PROBLEMS}.settings import SPEC', True),
        ('tle.kcpc.services', 'from tle.util import handle_linking', False),
        (
            'tle.kcpc.bootstrap',
            'from tle.util.handle_linking import link_handle',
            False,
        ),
        ('tle.kcpc.core.x', 'from tle.util import handle_linking', False),
        # Nothing but the bridge reaches TLE's user database, even where TLE's
        # other modules may be imported.
        (
            f'{ACCOUNTS}.cog',
            'from tle.util import codeforces_common as cf_common',
            False,
        ),
        (f'{ACCOUNTS}.service', 'from tle.util import db', False),
        (f'{ACCOUNTS}.cog', 'from tle.cogs.handles import Handles', False),
        ('tle.kcpc.features.a.cog', 'def f():\n    import tle.cogs.handles', False),
        ('tle.kcpc.services', 'from tle.util import codeforces_common', False),
        (
            'tle.kcpc.bootstrap',
            'from tle.util.db.user_db_conn import UserDbConn',
            False,
        ),
        ('tle.kcpc.bootstrap', 'import tle.cogs', False),
        # No KCPC module imports TLE's access package, not even where TLE's
        # other modules may be imported, nor lazily or for type checking.
        (f'{ACCOUNTS}.views', 'from tle.access.service import AccessService', False),
        (f'{ACCOUNTS}.cog', 'def f():\n    import tle.access', False),
        (
            'tle.kcpc.features.admin.cog',
            'if TYPE_CHECKING:\n    from tle.access import service',
            False,
        ),
        ('tle.kcpc.services', 'from tle.access.rules import Who', False),
        ('tle.kcpc.bootstrap', 'import tle.access.table', False),
        ('tle.kcpc', 'from tle.access import context', False),
        ('tle.kcpc.core.x', 'from . import db', False),
        ('tle.kcpc.bootstrap', 'from .services import KcpcServices', False),
    ],
)
def test_the_rules(module: str, source: str, allowed: bool) -> None:
    problems = [violation(ref) for ref in imports_in(module, source)]

    assert problems, 'the source imports something'
    assert all(problem is None for problem in problems) is allowed, problems
