"""The real bot, booted for tests: ``TLEBot.setup_hook`` runs, but nothing
reaches Discord or TLE's database files.

``booted`` puts its own stand-ins (``Stubs``) in place for the calls that
would: TLE's database setup gives the bot a user database in memory, the
bot's application is owned by OWNER_ID, and the slash command sync sends
nothing. OAuth is off, no log channel is set, and kcpc.db goes where the test
says. The rest runs for real: choosing the extensions, starting KCPC, loading
every cog, the access settings and cogs, and the slash pass. The bot never
logs in. KCPC's jobs wait for it to be ready, which it never is here, so none
of them runs.
"""

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from tle import constants
from tle.config import Settings
from tle.util import db

# TLE's cogs draw with cairo and pango, through gi, and tle.__main__ imports
# matplotlib and seaborn: Docker and CI have them, a bare virtualenv may not.
pytest.importorskip('gi')

from tle.__main__ import TLEBot  # noqa: E402
from tle.util import codeforces_common as cf_common  # noqa: E402

# Where TLE's and KCPC's extension modules live (KCPC's in features/*/cog.py).
EXTENSION_PACKAGES = ('tle.cogs.', 'tle.kcpc.features.')
# The owner of the bot's application, as Discord describes it.
OWNER_ID = 1_400_000_000_000_000_099


async def attach_user_db(bot: Any, nodb: bool) -> None:
    """What cf_common.initialize attaches to the bot, without TLE's database
    files: a user database in memory, or with --nodb the stand-in that refuses
    every call.
    """
    if nodb:
        bot.user_db = db.DummyUserDbConn()
        return
    user_db = db.UserDbConn(':memory:')
    await user_db.connect()
    bot.user_db = user_db


def application(owner_id: int = OWNER_ID) -> MagicMock:
    """The bot's application, owned by ``owner_id`` rather than a team."""
    app = MagicMock(spec=discord.AppInfo)
    app.owner = MagicMock(spec=discord.User, id=owner_id)
    app.team = None
    return app


@dataclass(frozen=True)
class Stubs:
    """Stand-ins for the calls that would reach TLE's database files or Discord.

    ``initialize`` stands in for cf_common.initialize, ``application_info``
    for the request that finds the bot's owners, and ``sync`` for the slash
    command sync. Each is an ``AsyncMock``, so tests can see how it was called
    and change what it does.
    """

    initialize: AsyncMock = field(
        default_factory=lambda: AsyncMock(side_effect=attach_user_db)
    )
    application_info: AsyncMock = field(
        default_factory=lambda: AsyncMock(return_value=application())
    )
    sync: AsyncMock = field(default_factory=lambda: AsyncMock(return_value=[]))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Put the stand-ins in place, until ``monkeypatch`` takes them away."""
        monkeypatch.setattr(cf_common, 'initialize', self.initialize)
        monkeypatch.setattr(TLEBot, 'application_info', self.application_info)
        monkeypatch.setattr(app_commands.CommandTree, 'sync', self.sync)
        monkeypatch.setattr(constants, 'OAUTH_CONFIGURED', False)
        # Unset, the logging extension loads but installs no log handler.
        monkeypatch.delenv('LOGGING_COG_CHANNEL_ID', raising=False)


@asynccontextmanager
async def booted(
    db_path: Path,
    *,
    disabled: str = '',
    nodb: bool = False,
    allowed_guilds: str = '',
    stubs: Stubs | None = None,
) -> AsyncIterator[TLEBot]:
    """A bot whose ``setup_hook`` has run; it is closed on the way out.

    ``disabled`` and ``allowed_guilds`` are its DISABLED_EXTENSIONS and
    ALLOWED_GUILD_IDS settings, and ``nodb`` is --nodb. ``stubs``, or new ones,
    are in place while it runs. Afterwards, the extension modules imported
    before it booted are put back in sys.modules (see ``extension_modules``).
    """
    settings = Settings.from_env(
        {
            'DISABLED_EXTENSIONS': disabled,
            'ALLOWED_GUILD_IDS': allowed_guilds,
            'KCPC_DB_PATH': str(db_path),
        }
    )
    intents = discord.Intents.default()  # as tle.__main__.main sets them
    intents.members = True
    intents.message_content = True
    with pytest.MonkeyPatch.context() as monkeypatch:
        (Stubs() if stubs is None else stubs).install(monkeypatch)
        bot = TLEBot(nodb=nodb, settings=settings, command_prefix=';', intents=intents)
        imported = extension_modules()
        try:
            # The context manager sets the bot up for the running loop, as
            # logging in would; KCPC's jobs wait on bot.wait_until_ready, which
            # needs that.
            async with bot:
                await bot.setup_hook()
                yield bot
        finally:
            sys.modules.update(imported)
            # bot.close() should have done this already (shutting down twice
            # is fine), but a database left open would keep pytest from
            # exiting.
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
