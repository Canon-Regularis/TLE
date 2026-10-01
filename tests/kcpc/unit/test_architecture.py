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
- services.py and bootstrap.py, which assemble everything, may import anything.

Relative imports are not allowed anywhere. One more test checks, in a fresh
interpreter, that tle.util.discord_common can be imported first.
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
STDLIB = frozenset(sys.stdlib_module_names)


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

    # Guards against a path mistake that would leave nothing to check.
    for expected in (
        'tle.kcpc.core.db',
        'tle.kcpc.bot.cog',
        'tle.kcpc.features.admin.cog',
        'tle.kcpc.services',
        'tle.kcpc.bootstrap',
    ):
        assert expected in modules


def test_no_kcpc_module_breaks_the_layering_rules() -> None:
    problems = [
        f'{ref}: {problem}'
        for module, path in kcpc_modules()
        for ref in imports_in(module, path.read_text(encoding='utf-8'))
        if (problem := violation(ref)) is not None
    ]

    assert problems == []


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
    refs = {
        (ref.module, ref.target, ref.deferred)
        for module, path in kcpc_modules()
        for ref in imports_in(module, path.read_text(encoding='utf-8'))
    }

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
        ('tle.kcpc.platforms.x', 'import icalendar', True),
        ('tle.kcpc.platforms.x', 'from tle.kcpc.core.http import HttpClient', True),
        ('tle.kcpc.platforms.x', 'from tle.util import codeforces_api', True),
        ('tle.kcpc.platforms.x', 'from tle.util.cache import CacheSystem', True),
        ('tle.kcpc.platforms.x', 'import discord', False),
        ('tle.kcpc.platforms.x', 'from tle.kcpc.bot import embeds', False),
        ('tle.kcpc.platforms.x', 'from tle.util import discord_common', False),
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
        ('tle.kcpc.services', 'from tle.kcpc.bot.publisher import X', True),
        ('tle.kcpc.bootstrap', 'from tle.kcpc.features.admin import cog', True),
        ('tle.kcpc.core.x', 'from . import db', False),
        ('tle.kcpc.bootstrap', 'from .services import KcpcServices', False),
    ],
)
def test_the_rules(module: str, source: str, allowed: bool) -> None:
    problems = [violation(ref) for ref in imports_in(module, source)]

    assert problems, 'the source imports something'
    assert all(problem is None for problem in problems) is allowed, problems
