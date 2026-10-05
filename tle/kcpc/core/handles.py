"""Members' linked handles, for features other than the one that links them.

Features never import each other. The accounts feature keeps members' AtCoder
handles in kcpc.db, so its cog registers a ``HandleSource`` for 'atcoder' with
``KcpcServices.handles`` when it loads, and unregisters it when it unloads:
while it isn't loaded, no feature knows anyone's AtCoder handle. Codeforces
handles are TLE's: read them with ``tle.kcpc.bot.codeforces_links``.
"""

from typing import Protocol


class HandleSource(Protocol):
    """Where a feature keeps members' handles on a platform."""

    async def linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        """The member's handle on ``platform``, in the platform's case, or None."""
        ...

    async def linked_handles(
        self, guild_id: int, platform: str
    ) -> list[tuple[int, str]]:
        """``(user_id, handle)`` of each link on ``platform`` in the guild.

        Links of members who have left the guild are included.
        """
        ...


class HandleRegistry:
    """The source of members' handles on each platform, as features register them.

    Lookups happen when a handle is wanted, so features may load in any order.
    """

    def __init__(self) -> None:
        self._sources: dict[str, HandleSource] = {}

    def register(self, platform: str, source: HandleSource) -> None:
        """Read members' handles on ``platform`` from ``source``, from now on.

        ``ValueError`` if the platform has a source already.
        """
        if platform in self._sources:
            raise ValueError(f'A handle source for {platform!r} is already registered')
        self._sources[platform] = source

    def unregister(self, platform: str) -> None:
        """Forget the platform's source. An unknown platform is ignored."""
        self._sources.pop(platform, None)

    @property
    def platforms(self) -> list[str]:
        """The platforms with a registered source, sorted."""
        return sorted(self._sources)

    async def linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        """The member's handle on ``platform``, in the platform's case, or None.

        None too when no feature keeps handles on the platform: its extension
        isn't loaded, say.
        """
        source = self._sources.get(platform)
        if source is None:
            return None
        return await source.linked_handle(guild_id, user_id, platform)

    async def linked_handles(
        self, guild_id: int, platform: str
    ) -> list[tuple[int, str]]:
        """``(user_id, handle)`` of each link on ``platform`` in the guild.

        Links of members who have left the guild are included, so a caller
        that wants only members checks. None are known when no feature keeps
        handles on the platform.
        """
        source = self._sources.get(platform)
        if source is None:
            return []
        return await source.linked_handles(guild_id, platform)
