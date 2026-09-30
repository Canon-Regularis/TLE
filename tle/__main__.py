import argparse
import asyncio
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from os import environ
from typing import TYPE_CHECKING, Any

import discord
import seaborn as sns
from discord.ext import commands
from matplotlib import pyplot as plt

from tle import constants, extensions
from tle.config import Settings
from tle.kcpc.core.errors import ConfigError
from tle.util import codeforces_common as cf_common, db, discord_common

if TYPE_CHECKING:
    from tle.kcpc.services import KcpcServices


def setup() -> None:
    # Make required directories.
    for path in constants.ALL_DIRS:
        os.makedirs(path, exist_ok=True)

    # logging to console and file on daily interval
    logging.basicConfig(
        format='{asctime}:{levelname}:{name}:{message}',
        style='{',
        datefmt='%d-%m-%Y %H:%M:%S',
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            TimedRotatingFileHandler(
                constants.LOG_FILE_PATH, when='D', backupCount=3, utc=True
            ),
        ],
    )

    # matplotlib and seaborn
    plt.rcParams['figure.figsize'] = 7.0, 3.5
    sns.set()
    options = {
        'axes.edgecolor': '#A0A0C5',
        'axes.spines.top': False,
        'axes.spines.right': False,
    }
    sns.set_style('darkgrid', options)


def strtobool(value: str) -> bool:
    """
    Convert a string representation of truth to true (1) or false (0).

    True values are y, yes, t, true, on and 1; false values are n, no, f,
    false, off and 0. Raises ValueError if val is anything else.
    """
    value = value.lower()
    if value in ('y', 'yes', 't', 'true', 'on', '1'):
        return True
    if value in ('n', 'no', 'f', 'false', 'off', '0'):
        return False
    raise ValueError(f'Invalid truth value {value!r}.')


class TLEContext(commands.Context):
    async def send(self, *args: Any, **kwargs: Any) -> discord.Message:
        if self.interaction is None and 'reference' not in kwargs:
            kwargs['reference'] = self.message
            kwargs.setdefault('mention_author', False)
        return await super().send(*args, **kwargs)


class TLEBot(commands.Bot):
    def __init__(self, nodb: bool, settings: Settings, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.nodb: bool = nodb
        self.settings: Settings = settings
        self.kcpc: KcpcServices | None = None
        self.oauth_server: Any = None
        self.oauth_state_store: Any = None

    async def get_context(
        self, message: discord.Message, *, cls: type | None = None
    ) -> commands.Context:
        return await super().get_context(message, cls=cls or TLEContext)

    async def setup_hook(self) -> None:
        enabled, unknown = extensions.select(
            extensions.discover(), self.settings.disabled_extensions
        )
        # First, so that problems while starting up reach the log channel.
        if enabled and enabled[0].name == extensions.LOGGING_EXTENSION:
            await self.load_extension(enabled.pop(0).module)
        for token in unknown:
            logging.warning(
                f'Ignoring unknown extension {token!r} in DISABLED_EXTENSIONS'
            )
        await cf_common.initialize(self, self.nodb)
        kcpc_enabled = any(ext.family == extensions.KCPC_FAMILY for ext in enabled)
        if kcpc_enabled and not await self._start_kcpc():
            enabled = [ext for ext in enabled if ext.family != extensions.KCPC_FAMILY]
        for extension in enabled:
            if extension.family == extensions.KCPC_FAMILY:
                await self._load_kcpc_extension(extension)
            else:
                # As before KCPC, a TLE extension that fails to load stops the bot.
                await self.load_extension(extension.module)
        logging.info(f'Cogs loaded: {", ".join(self.cogs)}')
        if constants.OAUTH_CONFIGURED:
            from tle.util.oauth import OAuthServer, OAuthStateStore

            self.oauth_state_store = OAuthStateStore()
            self.oauth_server = OAuthServer(
                self, self.oauth_state_store, constants.OAUTH_SERVER_PORT
            )
            await self.oauth_server.start()
            logging.info('OAuth callback server started')
        await self.tree.sync()
        logging.info('Slash commands synced')

    async def _start_kcpc(self) -> bool:
        """Start the KCPC services; False if KCPC can't run this time."""
        if self.nodb:
            logging.info('KCPC is disabled with --nodb')
            return False
        try:
            # Imported here, so that a KCPC module that fails to import is
            # logged below like any other failure to build the services.
            from tle.kcpc import bootstrap

            self.kcpc = await bootstrap.build_services(self, self.settings)
        except Exception:
            logging.exception(
                'KCPC failed to start; KCPC extensions will not be loaded'
            )
            return False
        return True

    async def _load_kcpc_extension(self, extension: extensions.Extension) -> None:
        """Load a KCPC extension; if that fails, log why and carry on without it.

        A broken KCPC extension must not take TLE down with it. KCPC's services
        keep running even if no KCPC extension loads: their reconcile job still
        settles the posts left unconfirmed when the bot last stopped.
        """
        try:
            await self.load_extension(extension.module)
        except Exception:
            # Not only commands.ExtensionError: importlib.util.find_spec raises
            # a missing parent package's import error as it is. discord.py has
            # already removed the extension's cogs, commands and listeners;
            # what a failed cog_load started is its own to undo (see KcpcCog).
            logging.exception(
                f'KCPC extension {extension.name} failed to load; '
                'the bot carries on without it'
            )

    async def close(self) -> None:
        if self.kcpc is not None:
            await self.kcpc.shutdown()
        if self.oauth_server is not None:
            await self.oauth_server.stop()
        try:
            user_db = getattr(self, 'user_db', None)
            if user_db is not None:
                await user_db.close()
        except db.DatabaseDisabledError:
            pass
        cf_cache = getattr(self, 'cf_cache', None)
        if cf_cache is not None:
            await cf_cache.conn.close()
        await super().close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--nodb', action='store_true')
    args = parser.parse_args()

    token = environ.get('BOT_TOKEN')
    if not token:
        logging.error('Token required')
        return

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.error(f'Invalid configuration: {exc}')
        return

    allow_self_register = environ.get('ALLOW_DUEL_SELF_REGISTER')
    if allow_self_register:
        constants.ALLOW_DUEL_SELF_REGISTER = strtobool(allow_self_register)

    setup()

    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True

    bot = TLEBot(
        nodb=args.nodb,
        settings=settings,
        command_prefix=commands.when_mentioned_or(';'),
        intents=intents,
    )

    def no_dm_check(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage('Private messages not permitted.')
        return True

    # Restrict bot usage to inside guild channels only.
    bot.add_check(no_dm_check)

    async def interaction_guild_check(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                'Private messages not permitted.', ephemeral=True
            )
            return False
        return True

    bot.tree.interaction_check = interaction_guild_check

    @bot.event
    @discord_common.once
    async def on_ready() -> None:
        asyncio.create_task(discord_common.presence(bot))

    bot.add_listener(discord_common.bot_error_handler, name='on_command_error')

    bot.run(token)


if __name__ == '__main__':
    main()
