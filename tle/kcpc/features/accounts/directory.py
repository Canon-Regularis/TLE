"""The accounts members linked through KCPC, for TLE's ``/handle show``.

TLE's cogs call these functions, importing this module only when a command
runs, so that a broken KCPC module can't stop TLE from loading. Codeforces
handles aren't listed here: they are in TLE's own table.
"""

from typing import Protocol

from tle.kcpc.core.db import Database
from tle.kcpc.features.accounts.repo import AccountRepo
from tle.kcpc.features.accounts.service import link_platform


class HasDatabase(Protocol):
    """What the directory needs of the KCPC services (``bot.kcpc``)."""

    @property
    def db(self) -> Database: ...


async def linked_accounts(
    services: HasDatabase, guild_id: int, user_id: int
) -> list[tuple[str, str]]:
    """``(platform, handle)`` of each account the member linked, by platform."""
    links = await AccountRepo(services.db).links_for_user(guild_id, user_id)
    return [(link.platform, link.handle) for link in links]


def platform_name(platform: str) -> str:
    """How to name ``platform`` to members: 'AtCoder' for 'atcoder'."""
    return link_platform(platform).name


def profile_url(platform: str, handle: str) -> str:
    """The profile page of ``handle`` on ``platform``."""
    return link_platform(platform).url_for(handle)
