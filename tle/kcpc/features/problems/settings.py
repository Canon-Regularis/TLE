"""Each server's weekly problem settings: the rotation its problems are picked by."""

from dataclasses import dataclass

from tle.kcpc.core.settings import FeatureSettings, FeatureSpec

WEEKLY = 'weekly'


@dataclass(frozen=True)
class WeeklySettings(FeatureSettings):
    """A server's weekly problem settings.

    ``rotation`` holds one 'platform:band:topic' entry per week, in the order
    the weeks take them, e.g. 'codeforces:medium:graphs'. Empty means the
    default rotation.
    """

    rotation: tuple[str, ...] = ()


SPEC = FeatureSpec(
    WEEKLY,
    'Weekly problem',
    'Friday problem, solution the Friday after',
    WeeklySettings,
)


def weekly_settings(settings: FeatureSettings) -> WeeklySettings:
    """``settings`` as the weekly problem's own, else ``TypeError``.

    They always are once its spec is registered, as ``tle.kcpc.bootstrap``
    does before anything reads them.
    """
    if not isinstance(settings, WeeklySettings):
        raise TypeError(f'Expected WeeklySettings, got {type(settings).__name__}')
    return settings
