"""Each server's workshop settings: its Luma calendar and when to remind members."""

from dataclasses import dataclass

from tle.kcpc.core.settings import FeatureSettings, FeatureSpec

WORKSHOPS = 'workshops'


@dataclass(frozen=True)
class WorkshopSettings(FeatureSettings):
    """A server's workshop settings.

    ``calendar_id`` is the Luma calendar the server follows; None means the
    bot's default, ``LUMA_CALENDAR_ID`` (see ``effective_calendar``).
    ``reminder_minutes`` says how long before each workshop starts to remind
    members, in minutes.
    """

    calendar_id: str | None = None
    reminder_minutes: tuple[int, ...] = (1440, 60)


SPEC = FeatureSpec(
    WORKSHOPS,
    'Workshops',
    'Luma workshop reminders, 24h and 1h before',
    WorkshopSettings,
)


def effective_calendar(settings: WorkshopSettings, default: str | None) -> str | None:
    """The calendar a server follows: its own, else ``default``, else None.

    An empty ID counts as unset.
    """
    return settings.calendar_id or default or None
