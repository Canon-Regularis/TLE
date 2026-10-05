"""Component tests for the algorithm of the month's AlgoRepo, on kcpc.db."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database
from tle.kcpc.core.timeutil import zone
from tle.kcpc.features.algo.repo import AlgoPick, AlgoRepo

CLUB = zone('Europe/London')
HOUR = timedelta(hours=1)

# Discord IDs are 64-bit: too big for a float to hold exactly.
GUILD = 1_100_000_000_000_000_001
OTHER_GUILD = 1_100_000_000_000_000_002


def first(month: int, year: int = 2026) -> datetime:
    """The slot of the 1st of ``month``: noon in the club's time, in UTC."""
    return datetime(year, month, 1, 12, 0, tzinfo=CLUB).astimezone(UTC)


def pick_of(month: int = 10, slug: str = 'segment-tree', **changes: Any) -> AlgoPick:
    """The guild's pick for ``month`` of 2026, picked at its slot."""
    pick = AlgoPick(
        guild_id=GUILD,
        month=f'2026-{month:02d}',
        slot=first(month),
        slug=slug,
        revision=0,
        picked_at=first(month),
    )
    return replace(pick, **changes)


@pytest.fixture
def repo(db: Database) -> AlgoRepo:
    return AlgoRepo(db)


class TestRecords:
    def test_times_are_converted_to_whole_seconds_in_utc(self) -> None:
        # 11:00:00.999999 UTC, noon in London.
        local = datetime(2026, 10, 1, 20, 0, 0, 999_999, tzinfo=zone('Asia/Tokyo'))

        pick = pick_of(slot=local, picked_at=local)

        assert (pick.slot, pick.picked_at) == (first(10), first(10))
        assert pick.slot.tzinfo is UTC and pick.picked_at.tzinfo is UTC
        assert pick == pick_of()

    @pytest.mark.parametrize(
        'build',
        [
            lambda: pick_of(slot=datetime(2026, 10, 1, 11)),
            lambda: pick_of(picked_at=datetime(2026, 10, 1, 11)),
        ],
        ids=['slot', 'picked_at'],
    )
    def test_naive_times_are_refused(self, build: Callable[[], object]) -> None:
        with pytest.raises(ValueError):
            build()


class TestPicks:
    async def test_a_pick_reads_back_as_stored(self, repo: AlgoRepo) -> None:
        pick = pick_of(revision=2, picked_at=first(10) + HOUR)

        assert await repo.create(pick) == pick
        assert await repo.get(GUILD, '2026-10') == pick
        assert await repo.get(GUILD, '2026-11') is None
        assert await repo.get(OTHER_GUILD, '2026-10') is None

    async def test_a_month_keeps_the_pick_stored_first(self, repo: AlgoRepo) -> None:
        stored = await repo.create(pick_of(slug='segment-tree'))

        again = await repo.create(pick_of(slug='trie', picked_at=first(10) + HOUR))

        assert again == stored
        assert await repo.get(GUILD, '2026-10') == stored
        assert await repo.revisions(GUILD, '2026-10') == [stored]

        # Once rerolled, the month's pick is its newest revision.
        rerolled = await repo.reroll(GUILD, '2026-10', 'knapsack', first(10) + HOUR)

        assert await repo.create(pick_of(slug='trie')) == rerolled

    async def test_each_guild_has_its_own_months(self, repo: AlgoRepo) -> None:
        ours = await repo.create(pick_of())
        theirs = await repo.create(pick_of(guild_id=OTHER_GUILD, slug='trie'))

        assert await repo.get(GUILD, '2026-10') == ours
        assert await repo.get(OTHER_GUILD, '2026-10') == theirs
        assert theirs.guild_id == OTHER_GUILD  # 64 bits, read back exactly

    async def test_history_is_newest_first_up_to_a_limit(self, repo: AlgoRepo) -> None:
        picks = [
            await repo.create(pick_of(month, f'topic-{month}')) for month in (10, 8, 9)
        ]
        await repo.create(pick_of(11, guild_id=OTHER_GUILD))
        october, august, september = picks

        assert await repo.history(GUILD) == [october, september, august]
        assert await repo.history(GUILD, 2) == [october, september]
        assert await repo.history(GUILD, 0) == []
        assert await repo.history(1_100_000_000_000_000_003) == []

    async def test_history_holds_every_revision_of_its_months(
        self, repo: AlgoRepo
    ) -> None:
        september = await repo.create(pick_of(9, 'trie'))
        october = await repo.create(pick_of(10, 'segment-tree'))
        rerolled = await repo.reroll(GUILD, '2026-10', 'knapsack', first(10) + HOUR)

        assert await repo.history(GUILD) == [rerolled, october, september]
        # The limit counts months, each with all its revisions.
        assert await repo.history(GUILD, 1) == [rerolled, october]

    async def test_history_holds_the_newest_200_unless_asked_for_more(
        self, repo: AlgoRepo
    ) -> None:
        for number in range(201):
            year, month = divmod(number, 12)
            label = f'{2000 + year}-{month + 1:02d}'
            await repo.create(replace(pick_of(), month=label, slug=f'topic-{number}'))

        newest = await repo.history(GUILD)

        assert len(newest) == 200
        assert newest[0].month == '2016-09'
        assert newest[-1].month == '2000-02'
        assert len(await repo.history(GUILD, 500)) == 201

    async def test_the_picks_before_a_month_come_oldest_first(
        self, repo: AlgoRepo
    ) -> None:
        november = await repo.create(pick_of(11, 'trie'))
        september = await repo.create(pick_of(9, 'segment-tree'))
        october = await repo.create(pick_of(10, 'knapsack'))
        rerolled = await repo.reroll(GUILD, '2026-10', 'dijkstra', first(10) + HOUR)
        theirs = await repo.create(pick_of(8, 'dijkstra', guild_id=OTHER_GUILD))

        assert await repo.picks_before(GUILD, '2026-12') == [
            september,
            october,
            rerolled,
            november,
        ]
        assert await repo.picks_before(GUILD, '2026-11') == [
            september,
            october,
            rerolled,
        ]
        assert await repo.picks_before(GUILD, '2026-09') == []
        assert await repo.picks_before(OTHER_GUILD, '2026-12') == [theirs]

    async def test_a_reroll_adds_the_next_revision_and_keeps_the_ones_before(
        self, repo: AlgoRepo
    ) -> None:
        october = await repo.create(pick_of(10, 'segment-tree'))
        november = await repo.create(pick_of(11, 'trie'))
        theirs = await repo.create(pick_of(10, 'segment-tree', guild_id=OTHER_GUILD))
        later = first(10) + 3 * HOUR

        rerolled = await repo.reroll(GUILD, '2026-10', 'knapsack', later)

        assert rerolled == replace(
            october, slug='knapsack', revision=1, picked_at=later
        )
        # The month's pick is its newest revision, and the one it replaced
        # stays, as members may have been shown it.
        assert await repo.get(GUILD, '2026-10') == rerolled
        assert await repo.revisions(GUILD, '2026-10') == [rerolled, october]
        assert await repo.get(GUILD, '2026-11') == november
        assert await repo.revisions(GUILD, '2026-11') == [november]
        assert await repo.get(OTHER_GUILD, '2026-10') == theirs

        again = await repo.reroll(GUILD, '2026-10', 'segment-tree', later + HOUR)

        assert again == replace(
            october, slug='segment-tree', revision=2, picked_at=later + HOUR
        )
        assert await repo.revisions(GUILD, '2026-10') == [again, rerolled, october]
        assert await repo.revisions(GUILD, '2026-12') == []

    async def test_a_reroll_of_a_month_without_a_pick_changes_nothing(
        self, repo: AlgoRepo
    ) -> None:
        await repo.create(pick_of(10))

        assert await repo.reroll(GUILD, '2026-11', 'trie', first(11)) is None
        assert await repo.reroll(OTHER_GUILD, '2026-10', 'trie', first(11)) is None
        assert await repo.history(GUILD) == [pick_of(10)]
        assert await repo.history(OTHER_GUILD) == []
