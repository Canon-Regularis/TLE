"""Linking a Codeforces account by signing in to Codeforces (OpenID Connect).

``;handle identify`` and ``/handle identify`` give a member a sign-in link,
whose ``state`` the ``OAuthStateStore`` keeps for 5 minutes. Codeforces sends
the member back to the ``OAuthServer``'s callback, which links the account
through ``tle.util.handle_linking`` and tells the member how it went: through
the slash command's interaction, privately, or else by direct message. It
never posts in a channel.
"""

import html
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
import discord
import jwt
from aiohttp import web

from tle.util import codeforces_api as cf, handle_linking

logger = logging.getLogger(__name__)

_CF_AUTHORIZE_URL = 'https://codeforces.com/oauth/authorize'
_CF_TOKEN_URL = 'https://codeforces.com/oauth/token'
_CF_ISSUER = 'https://codeforces.com'

_STATE_TTL = 5 * 60  # 5 minutes

# What the member is told, privately, when their account couldn't be linked.
LINK_FAILED_TEXT = (
    "I couldn't link your Codeforces account. Try `/handle identify` again, or "
    'ask a moderator if it keeps failing.'
)
# ... and when it can't be linked for a reason that trying again won't change,
# such as another member having linked the handle; ``reason`` is that of the
# ``handle_linking.HandleLinkError``.
LINK_REFUSED_TEXT = (
    "I couldn't link your Codeforces account: {reason} Ask a moderator to sort it out."
)


@dataclass
class OAuthPending:
    """A sign-in that a member started, until Codeforces sends them back.

    ``interaction`` is that of the slash command that started it, through
    which the member hears how it went; None for the prefix command, whose
    member hears by direct message.
    """

    user_id: int
    guild_id: int
    interaction: discord.Interaction | None
    created_at: float


class OAuthStateStore:
    """In-memory store mapping state tokens to pending OAuth requests."""

    def __init__(self) -> None:
        self._pending: dict[str, OAuthPending] = {}

    def create(
        self,
        user_id: int,
        guild_id: int,
        *,
        interaction: discord.Interaction | None = None,
    ) -> str:
        """A new state for a sign-in by ``user_id`` in ``guild_id``, started
        by the slash command of ``interaction``, or by the prefix command.
        """
        self._prune()
        state = secrets.token_urlsafe(32)
        self._pending[state] = OAuthPending(
            user_id=user_id,
            guild_id=guild_id,
            interaction=interaction,
            created_at=time.monotonic(),
        )
        return state

    def consume(self, state: str) -> OAuthPending | None:
        self._prune()
        return self._pending.pop(state, None)

    def has_pending(self, user_id: int) -> bool:
        self._prune()
        return any(p.user_id == user_id for p in self._pending.values())

    def revoke(self, user_id: int) -> None:
        """Remove all pending states for a user, invalidating old links."""
        to_remove = [s for s, p in self._pending.items() if p.user_id == user_id]
        for s in to_remove:
            del self._pending[s]

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [
            s for s, p in self._pending.items() if now - p.created_at > _STATE_TTL
        ]
        for s in expired:
            del self._pending[s]


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': 'openid',
        'state': state,
    }
    return f'{_CF_AUTHORIZE_URL}?{urlencode(params)}'


async def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    session: aiohttp.ClientSession,
) -> dict[str, Any]:
    data = {
        'grant_type': 'authorization_code',
        'code': code,
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri,
    }
    async with session.post(_CF_TOKEN_URL, data=data) as resp:
        body = await resp.json()
        if resp.status != 200:
            raise ValueError(f'Token exchange failed: {body}')
        return body  # type: ignore[no-any-return]


def decode_id_token(
    id_token: str,
    client_secret: str,
    client_id: str,
) -> dict[str, Any]:
    return jwt.decode(
        id_token,
        client_secret,
        algorithms=['HS256'],
        audience=client_id,
        issuer=_CF_ISSUER,
    )


_SUCCESS_HTML = """\
<!DOCTYPE html>
<html><head><title>Success</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h1>&#10004; Account linked!</h1>
<p>You can close this tab.</p>
</body></html>"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html><head><title>Error</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h1>Something went wrong</h1>
<p>{message}</p>
</body></html>"""


class OAuthServer:
    def __init__(self, bot: Any, state_store: OAuthStateStore, port: int) -> None:
        self.bot = bot
        self.state_store = state_store
        self.port = port
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_get('/callback', self._handle_callback)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, '0.0.0.0', self.port)
        await site.start()
        logger.info('OAuth callback server listening on port %d', self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        if self._session:
            await self._session.close()

    async def _handle_callback(self, request: web.Request) -> web.Response:
        from tle import constants

        state = request.query.get('state')
        code = request.query.get('code')
        error = request.query.get('error')

        if error:
            # Anyone can open the callback with any error, so it is escaped.
            return web.Response(
                text=_ERROR_HTML.format(
                    message=f'Authorization denied: {html.escape(error)}'
                ),
                content_type='text/html',
            )

        if not state or not code:
            return web.Response(
                text=_ERROR_HTML.format(message='Missing state or code parameter.'),
                content_type='text/html',
            )

        pending = self.state_store.consume(state)
        if pending is None:
            return web.Response(
                text=_ERROR_HTML.format(
                    message='Link expired or already used.'
                    ' Please run the command again.'
                ),
                content_type='text/html',
            )

        try:
            assert constants.OAUTH_CLIENT_ID is not None
            assert constants.OAUTH_CLIENT_SECRET is not None
            assert constants.OAUTH_REDIRECT_URI is not None
            assert self._session is not None
            tokens = await exchange_code(
                code,
                constants.OAUTH_CLIENT_ID,
                constants.OAUTH_CLIENT_SECRET,
                constants.OAUTH_REDIRECT_URI,
                self._session,
            )
            if 'id_token' not in tokens:
                logger.error(
                    'Token response missing id_token. '
                    'Received keys: %s. Ensure your Codeforces OAuth app '
                    'has OpenID Connect enabled.',
                    list(tokens.keys()),
                )
                raise ValueError(
                    'Token response missing id_token; '
                    'check OAuth app OpenID Connect configuration'
                )
            claims = decode_id_token(
                tokens['id_token'],
                constants.OAUTH_CLIENT_SECRET,
                constants.OAUTH_CLIENT_ID,
            )
            handle = claims['handle']

            (user,) = await cf.user.info(handles=[handle])

            guild = self.bot.get_guild(pending.guild_id)
            if guild is None:
                raise ValueError('Guild not found')
            member = guild.get_member(pending.user_id)
            if member is None:
                raise ValueError('Member not found in guild')

            await handle_linking.link_handle(self.bot.user_db, guild, member, user)
        except handle_linking.HandleLinkError as error:
            # Not the bot's fault, and the same however often it is tried, so
            # the member hears why rather than to try again.
            logger.info(
                'Could not link the Codeforces account of user %d: %s',
                pending.user_id,
                error,
            )
            refused = LINK_REFUSED_TEXT.format(reason=error)
            await self._tell(
                pending, discord.Embed(description=refused, color=discord.Color.red())
            )
            # The page says it too, in case the message can't reach them; it
            # shows no Markdown, so without the code spans' backticks.
            page = html.escape(refused.replace('`', ''))
            return web.Response(
                text=_ERROR_HTML.format(message=page), content_type='text/html'
            )
        except Exception:
            logger.exception('OAuth callback error')
            failed = discord.Embed(
                description=LINK_FAILED_TEXT, color=discord.Color.red()
            )
            await self._tell(pending, failed)
            return web.Response(
                text=_ERROR_HTML.format(
                    message='An error occurred.'
                    ' Please try the command again in Discord.'
                ),
                content_type='text/html',
            )

        # Outside the try above: the account is linked, whatever happens to
        # the message that says so.
        from tle.cogs.handles import _make_profile_embed

        await self._tell(pending, _make_profile_embed(member, user, mode='set'))
        return web.Response(text=_SUCCESS_HTML, content_type='text/html')

    async def _tell(self, pending: OAuthPending, embed: discord.Embed) -> None:
        """Tell the member who signed in how it went, in ``embed``.

        The slash command's interaction answers them privately; without one,
        they get a direct message. Nothing is ever posted in a channel, so if
        neither works, it is only logged.
        """
        interaction = pending.interaction
        try:
            if interaction is not None:
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            user = self.bot.get_user(pending.user_id)
            if user is None:
                logger.info(
                    'Could not tell user %d how linking their Codeforces account '
                    'went: they are not in any server of the bot',
                    pending.user_id,
                )
                return
            await user.send(embed=embed)
        except discord.HTTPException as exc:
            # Direct messages closed, or the interaction expired.
            logger.info(
                'Could not tell user %d how linking their Codeforces account went: %s',
                pending.user_id,
                exc,
            )
        except Exception:
            # Whatever went wrong, the page the member sees says how it went.
            logger.exception(
                'Could not tell user %d how linking their Codeforces account went',
                pending.user_id,
            )
