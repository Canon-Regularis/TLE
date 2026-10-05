"""The ``Migration`` type, in a module of its own.

The migrations package imports every migration module, and each of those needs
``Migration``. Defining it here rather than in the package's ``__init__``
avoids an import cycle between the two.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tle.kcpc.core.db import Database


@dataclass(frozen=True)
class Migration:
    """One step of the kcpc.db schema, from ``version - 1`` to ``version``.

    ``apply`` runs inside an open transaction, with one ``db.execute()`` per
    statement: ``executescript`` would commit on its own.
    """

    version: int
    name: str
    apply: Callable[[Database], Awaitable[None]]
