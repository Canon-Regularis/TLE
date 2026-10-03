"""The bot's extensions, and which of them to load.

TLE's extensions are its cogs, named ``tle.<file>`` after ``tle/cogs/<file>.py``.
KCPC's are listed in ``KCPC_EXTENSIONS``, named ``kcpc.<feature>`` after
``tle/kcpc/features/<feature>/cog.py``. The ``DISABLED_EXTENSIONS`` setting
turns off single extensions by name, or whole families (``tle`` or ``kcpc``).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path

TLE_FAMILY = 'tle'
KCPC_FAMILY = 'kcpc'

LOGGING_EXTENSION = 'tle.logging'
# Loaded in this order. kcpc.admin comes first: it owns the /kcpc group, which
# other features add their admin commands to as they load.
KCPC_EXTENSIONS: tuple[tuple[str, str], ...] = (
    ('kcpc.admin', 'tle.kcpc.features.admin.cog'),
    ('kcpc.workshops', 'tle.kcpc.features.workshops.cog'),
    ('kcpc.contests', 'tle.kcpc.features.contests.cog'),
    ('kcpc.accounts', 'tle.kcpc.features.accounts.cog'),
    ('kcpc.notify', 'tle.kcpc.features.notify.cog'),
)

# Found from this file rather than the working directory, so discovery works
# wherever the bot is started from.
TLE_COGS_DIR = Path(__file__).parent / 'cogs'


@dataclass(frozen=True)
class Extension:
    name: str  # what DISABLED_EXTENSIONS uses: 'tle.handles', 'kcpc.admin'
    module: str  # what gets loaded: 'tle.cogs.handles', 'tle.kcpc.features.admin.cog'
    family: str  # 'tle' or 'kcpc'


def discover() -> list[Extension]:
    """Every extension: TLE's cogs by file name, then KCPC's in listed order."""
    tle_extensions = [
        Extension(f'tle.{path.stem}', f'tle.cogs.{path.stem}', TLE_FAMILY)
        for path in sorted(TLE_COGS_DIR.glob('*.py'))
        if not path.stem.startswith('_')
    ]
    kcpc_extensions = [
        Extension(name, module, KCPC_FAMILY) for name, module in KCPC_EXTENSIONS
    ]
    return tle_extensions + kcpc_extensions


def select(
    extensions: Sequence[Extension], disabled: Iterable[str]
) -> tuple[list[Extension], list[str]]:
    """The extensions to load, in load order, and the unknown ``disabled`` tokens.

    Each token disables the extension with that name, or every extension of
    that family; case doesn't matter. Tokens that name neither are returned,
    sorted, so that the caller can report them. The load order is
    ``tle.logging`` first, so that warnings during startup reach the log
    channel, then TLE's other extensions by name, then the rest in the order
    given.
    """
    tokens = {token.strip().lower() for token in disabled} - {''}
    known = {ext.name.lower() for ext in extensions}
    known |= {ext.family.lower() for ext in extensions}
    enabled = [
        ext
        for ext in extensions
        if ext.name.lower() not in tokens and ext.family.lower() not in tokens
    ]
    logging_first = [ext for ext in enabled if ext.name == LOGGING_EXTENSION]
    other_tle = sorted(
        (
            ext
            for ext in enabled
            if ext.family == TLE_FAMILY and ext.name != LOGGING_EXTENSION
        ),
        key=attrgetter('name'),
    )
    the_rest = [ext for ext in enabled if ext.family != TLE_FAMILY]
    return logging_first + other_tle + the_rest, sorted(tokens - known)
