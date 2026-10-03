"""Fixtures shared by the KCPC tests: a fake clock and a migrated in-memory kcpc.db."""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest

from tle.kcpc.core.clock import UTC, FakeClock
from tle.kcpc.core.db import Database
from tle.kcpc.core.ledger import DeliveryLedger
from tle.kcpc.core.migrations import open_database
from tle.kcpc.core.settings import FeatureRegistry, GuildSettingsRepo, default_registry

CLOCK_START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def opened_databases(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[list[Database]]:
    """Every database the test opens, in order; all are closed when it ends.

    aiosqlite's worker threads are not daemon threads. A database left open by
    a failing test (whose traceback pytest keeps) would stop pytest exiting.
    """
    opened: list[Database] = []
    open_connection = Database.open

    async def open_and_remember(path: str | Path) -> Database:
        db = await open_connection(path)
        opened.append(db)
        return db

    monkeypatch.setattr(Database, 'open', open_and_remember)
    yield opened
    for db in opened:
        await db.close()  # idempotent, so closing a closed one is fine


@pytest.fixture
def clock() -> FakeClock:
    """A FakeClock starting at 2026-10-01T12:00:00Z."""
    return FakeClock(CLOCK_START)


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """An in-memory kcpc.db migrated to the latest schema, closed afterwards."""
    database = await open_database(':memory:')
    yield database
    await database.close()


@pytest.fixture
def ledger(db: Database, clock: FakeClock) -> DeliveryLedger:
    return DeliveryLedger(db, clock)


@pytest.fixture
def feature_registry() -> FeatureRegistry:
    return default_registry()


@pytest.fixture
def guild_settings(
    db: Database, clock: FakeClock, feature_registry: FeatureRegistry
) -> GuildSettingsRepo:
    return GuildSettingsRepo(db, clock, feature_registry)
