"""Each server's contest settings: the platforms it follows and its reminders."""

from dataclasses import dataclass

from tle.kcpc.core.settings import FeatureSettings, FeatureSpec

CONTESTS = 'contests'
# The platform of contests that admins add by hand, which no site lists.
MANUAL = 'manual'
# Every platform a server can follow, in the order they are shown. CodeChef,
# LeetCode and TopCoder contests come from clist.by, so only once its
# credentials are set.
PLATFORMS = (
    'codeforces',
    'atcoder',
    'codechef',
    'leetcode',
    'topcoder',
    'icpc',
    MANUAL,
)


@dataclass(frozen=True)
class ContestSettings(FeatureSettings):
    """A server's contest settings.

    ``platforms`` are the platforms whose contests the server follows, from
    ``PLATFORMS``. ``reminder_minutes`` says how long before each contest
    starts to remind members, in minutes, and ``start_posts`` whether to post
    again when it starts.
    """

    platforms: tuple[str, ...] = PLATFORMS
    reminder_minutes: tuple[int, ...] = (60,)
    start_posts: bool = False


SPEC = FeatureSpec(
    CONTESTS,
    'Contests',
    'Contest reminders: Codeforces, AtCoder, CodeChef, LeetCode, TopCoder, ICPC '
    'and club contests',
    ContestSettings,
)


def contest_settings(settings: FeatureSettings) -> ContestSettings:
    """``settings`` as the contests feature's own, else ``TypeError``.

    They always are once its spec is registered, as ``tle.kcpc.bootstrap``
    does before anything reads them.
    """
    if not isinstance(settings, ContestSettings):
        raise TypeError(f'Expected ContestSettings, got {type(settings).__name__}')
    return settings
