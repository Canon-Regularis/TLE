"""Tests for tle.kcpc.core.handles: how features read members' linked handles."""

import pytest

from tle.kcpc.core.handles import HandleRegistry

GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002
MEMBER = 1_200_000_000_000_000_001
OTHER_MEMBER = 1_200_000_000_000_000_002


class FakeSource:
    """Members' handles by guild and member, as a feature might keep them.

    ``asked`` lists each lookup's guild, member and platform.
    """

    def __init__(self, handles: dict[tuple[int, int], str] | None = None) -> None:
        self.handles = dict(handles or {})
        self.asked: list[tuple[int, int, str]] = []

    async def linked_handle(
        self, guild_id: int, user_id: int, platform: str
    ) -> str | None:
        self.asked.append((guild_id, user_id, platform))
        return self.handles.get((guild_id, user_id))


async def test_a_platforms_handles_come_from_its_source() -> None:
    registry = HandleRegistry()
    atcoder = FakeSource({(GUILD, MEMBER): 'Fake_AtCoder'})
    registry.register('atcoder', atcoder)

    assert await registry.linked_handle(GUILD, MEMBER, 'atcoder') == 'Fake_AtCoder'
    assert await registry.linked_handle(GUILD, OTHER_MEMBER, 'atcoder') is None
    assert await registry.linked_handle(OTHER_GUILD, MEMBER, 'atcoder') is None
    # The source is told the platform, so that one may serve several.
    assert atcoder.asked == [
        (GUILD, MEMBER, 'atcoder'),
        (GUILD, OTHER_MEMBER, 'atcoder'),
        (OTHER_GUILD, MEMBER, 'atcoder'),
    ]


async def test_nobodys_handle_is_known_on_a_platform_without_a_source() -> None:
    registry = HandleRegistry()
    atcoder = FakeSource({(GUILD, MEMBER): 'Fake_AtCoder'})
    registry.register('atcoder', atcoder)

    # Codeforces handles are TLE's, which no feature registers.
    assert await registry.linked_handle(GUILD, MEMBER, 'codeforces') is None
    assert await HandleRegistry().linked_handle(GUILD, MEMBER, 'atcoder') is None
    assert atcoder.asked == []


async def test_a_platform_takes_one_source() -> None:
    registry = HandleRegistry()
    first = FakeSource({(GUILD, MEMBER): 'Fake_AtCoder'})
    registry.register('atcoder', first)

    with pytest.raises(ValueError, match="for 'atcoder' is already registered"):
        registry.register('atcoder', FakeSource({(GUILD, MEMBER): 'Other_Owl'}))
    with pytest.raises(ValueError, match='already registered'):
        registry.register('atcoder', first)

    assert registry.platforms == ['atcoder']
    assert await registry.linked_handle(GUILD, MEMBER, 'atcoder') == 'Fake_AtCoder'


def test_platforms_lists_those_with_a_source_in_order() -> None:
    registry = HandleRegistry()
    assert registry.platforms == []

    registry.register('codeforces', FakeSource())
    registry.register('atcoder', FakeSource())

    assert registry.platforms == ['atcoder', 'codeforces']


async def test_an_unregistered_source_is_asked_no_more() -> None:
    registry = HandleRegistry()
    atcoder = FakeSource({(GUILD, MEMBER): 'Fake_AtCoder'})
    registry.register('atcoder', atcoder)

    registry.unregister('atcoder')
    registry.unregister('atcoder')  # ignored, as is
    registry.unregister('never-registered')

    assert registry.platforms == []
    assert await registry.linked_handle(GUILD, MEMBER, 'atcoder') is None
    assert atcoder.asked == []

    # It can come back, as a reloaded cog's would.
    registry.register('atcoder', atcoder)
    assert await registry.linked_handle(GUILD, MEMBER, 'atcoder') == 'Fake_AtCoder'
