"""A live check that a real Luma feed still reads the way the parser expects.

It reaches api.lu.ma, so it is marked ``network`` and skipped unless
KCPC_NETWORK_TESTS=1.
"""

import os

import pytest

from tle.config import DEFAULT_HTTP_USER_AGENT
from tle.kcpc.core.clock import UTC, SystemClock
from tle.kcpc.core.http import HttpClient
from tle.kcpc.core.timeutil import zone
from tle.kcpc.platforms.luma import LumaCalendarClient

# A busy public calendar that lists both Luma-hosted and external events.
PUBLIC_CALENDAR_ID = 'cal-E74MDlDKBaeAwXK'

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        os.environ.get('KCPC_NETWORK_TESTS') != '1',
        reason='reaches api.lu.ma; set KCPC_NETWORK_TESTS=1 to run it',
    ),
]


async def test_a_public_calendar_fetches_and_parses() -> None:
    http = HttpClient(user_agent=DEFAULT_HTTP_USER_AGENT, clock=SystemClock())
    client = LumaCalendarClient(http, tz=zone('Europe/London'))
    try:
        events = await client.fetch(PUBLIC_CALENDAR_ID)
    finally:
        await http.close()

    assert events, 'a busy public calendar lists events'
    assert len({event.luma_id for event in events}) == len(events)
    for event in events:
        assert event.luma_id.startswith(('evt-', 'calev-')), event
        assert '@' not in event.luma_id, event
        assert event.name, event
        assert event.start.tzinfo is UTC, event
        assert event.end is None or event.end > event.start, event
        assert event.url.startswith('https://'), event
    # Luma-hosted events link their own page on Luma, from the description.
    # Not by ID: that is the parser's fallback, and most of these events'
    # LOCATION, so it would mean the description's link went missing.
    hosted = [event for event in events if event.luma_id.startswith('evt-')]
    assert hosted
    assert all(
        event.url.startswith(('https://luma.com/', 'https://lu.ma/'))
        for event in hosted
    )
    assert all(
        not event.url.startswith(('https://luma.com/event/', 'https://lu.ma/event/'))
        for event in hosted
    )
