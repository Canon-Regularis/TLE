import argparse
import asyncio
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from os import environ
from typing import TYPE_CHECKING, Any

import discord
import seaborn as sns
from discord import app_commands
from discord.ext import commands
from matplotlib import pyplot as plt

from tle import constants, extensions
from tle.access.cog import Access
from tle.access.context import TLEContext
from tle.access.help import Help
from tle.access.service import AccessService, AccessTree
from tle.access.slash import apply_visibility
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


def warn_of_unusable_developer_role() -> None:
    """Repeat tle.constants' warning that ``TLE_DEVELOPER`` isn't a role ID.

    tle.constants reads the setting on import, before logging is set up, so
    its own warning reaches neither the log file nor the log channel.
    """
    value = environ.get('TLE_DEVELOPER', '').strip()
    if value and constants.TLE_DEVELOPER is None:
        logging.warning(
            f'TLE_DEVELOPER must be a role ID, not {value!r}, so it is ignored'
        )


class TLEBot(commands.Bot):
    """The bot: TLE's cogs, KCPC's features, and the access rules over them.

    The access service checks every command, prefix or slash, before it runs,
    and its command tree checks every slash command and autocomplete first.
    /help and /access belong to the bot itself rather than to an extension,
    so DISABLED_EXTENSIONS never turns them off.
    """

    # The user database, which cf_common.initialize attaches to the bot.
    user_db: Any

    def __init__(self, nodb: bool, settings: Settings, **kwargs: Any) -> None:
        super().__init__(
            # /help, from the Help cog, takes the place of discord.py's help.
            help_command=None,
            tree_cls=AccessTree,
            # A message pings the members it names, unless it says otherwise,
            # but never @everyone, a role, or the author of a message it
            # replies to.
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
            # Slash commands work in servers alone, and the bot is installed
            # in servers, never on a member's account.
            allowed_contexts=app_commands.AppCommandContext(
                guild=True, dm_channel=False, private_channel=False
            ),
            allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
            **kwargs,
        )
        self.nodb: bool = nodb
        self.settings: Settings = settings
        self.kcpc: KcpcServices | None = None
        self.oauth_server: Any = None
        self.oauth_state_store: Any = None
        self.access = AccessService(self, allowed_guilds=settings.allowed_guild_ids)
        self.add_check(self.access.check)
        self.add_listener(discord_common.bot_error_handler, name='on_command_error')
        self._started = False
        self._presence_task: asyncio.Task[None] | None = None

    async def get_context(
        self,
        origin: discord.Message | discord.Interaction,
        /,
        *,
        cls: type | None = None,
    ) -> commands.Context:
        # A hybrid command's slash form gets its context here too.
        return await super().get_context(origin, cls=cls or TLEContext)

    async def is_owner(self, user: discord.abc.User, /) -> bool:
        """Whether ``user`` owns the bot, as the access rules count owners.

        It never asks Discord: the owners are found as the bot starts up, and
        if that fails, an owner's command looks again, at most every few
        minutes.
        """
        return self.access.is_owner(user)

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
        warn_of_unusable_developer_role()
        await cf_common.initialize(self, self.nodb)
        await self._start_access()
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
        await self._finish_access()
        await self.tree.sync()
        logging.info('Slash commands synced')

    async def _start_access(self) -> None:
        """Load every server's access settings, and add /access and /help.

        Under --nodb the user database refuses every call, so the settings are
        kept in memory alone.
        """
        self.access.use_user_db(None if self.nodb else self.user_db)
        await self.access.load()
        await self.add_cog(Access(self))
        await self.add_cog(Help(self))

    async def _finish_access(self) -> None:
        """Once every command is in: find the bot's owners, report commands
        without a rule, and keep staff commands out of members' slash lists.

        The slash pass must run again before any later sync, should an
        extension ever be loaded after this.
        """
        try:
            await self.access.resolve_owners()
        except Exception:
            # It logs its own failures; this is a second safety.
            logging.exception("Could not find the bot's owners")
        self.access.report_unruled(self.walk_commands())
        apply_visibility(self)

    async def on_ready(self) -> None:
        """Once connected for the first time: name the servers that
        ALLOWED_GUILD_IDS doesn't list, and start showing a status. Later
        reconnects change nothing.
        """
        if self._started:
            return
        self._started = True
        self._report_unlisted_guilds()
        self._presence_task = asyncio.create_task(discord_common.presence(self))

    def _report_unlisted_guilds(self) -> None:
        """Log the servers the bot is in that ALLOWED_GUILD_IDS doesn't list.

        The bot stays in them, so that a mistake in the setting can't make it
        leave the club's server; the access rules ignore them anyway.
        """
        unlisted = [
            guild for guild in self.guilds if not self.access.guild_allowed(guild.id)
        ]
        if unlisted:
            names = ', '.join(f'{guild.name} ({guild.id})' for guild in unlisted)
            logging.warning(
                'ALLOWED_GUILD_IDS does not list these servers, so the bot '
                f'ignores commands there: {names}'
            )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Leave a server that ALLOWED_GUILD_IDS doesn't list, as it joins."""
        if self.access.guild_allowed(guild.id):
            return
        logging.warning(
            f'Leaving the server {guild.name} ({guild.id}), which '
            'ALLOWED_GUILD_IDS does not list'
        )
        try:
            await guild.leave()
        except discord.HTTPException as exc:
            logging.warning(f'Could not leave the server {guild.id}: {exc}')

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
    bot.run(token)


if __name__ == '__main__':
    main()
