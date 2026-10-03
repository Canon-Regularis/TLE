"""Contest reminders: the contests feature's ``ReminderSource``.

Each server follows the platforms in its settings, and its contests are the
stored contests of those platforms whose start time is known: a contest known
only by its date (an ICPC regional, until an admin sets its time) gets none.
The reminder engine (``tle.kcpc.core.reminders``) reminds members before each
one starts, at the server's ``reminder_minutes``, posts again as it starts if
the server has ``start_posts`` on, and tells members when one they have heard
about moves, is cancelled or is back on. This module says what each of those
posts looks like.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tle.kcpc.core.messages import EmbedField, OutgoingMessage, shorten
from tle.kcpc.core.reminders import Notice, NoticeKind, Occurrence, ReminderPolicy
from tle.kcpc.core.settings import FeatureSettings
from tle.kcpc.core.timeutil import describe_duration, discord_timestamp
from tle.kcpc.features.contests.repo import ContestRepo, StoredContest
from tle.kcpc.features.contests.settings import CONTESTS, MANUAL, contest_settings

logger = logging.getLogger(__name__)

SUBJECT = 'contest'  # the ledger's subject for a contest, whose id is its key
FOOTER = 'KCPC contests'

# How posts and replies name each platform.
_PLATFORM_NAMES = {
    'codeforces': 'Codeforces',
    'atcoder': 'AtCoder',
    'codechef': 'CodeChef',
    'leetcode': 'LeetCode',
    'topcoder': 'TopCoder',
    'icpc': 'ICPC',
    MANUAL: 'Club',
}

# How far ahead the engine looks for contests. A reminder any earlier than
# this before the start could not go out on time.
_HORIZON = timedelta(days=400)
_LONGEST_REMINDER_MINUTES = _HORIZON // timedelta(minutes=1)
# A reminder of several contests shows each as an embed field: its name, then
# contest_details. The post must show all of them, up to the engine's
# MAX_GROUP, and Discord takes at most 6000 characters per embed. With each
# name cut to this length, and a link left out if longer than this, MAX_GROUP
# fields always fit.
_FIELD_NAME_LIMIT = 100
_LINK_LIMIT = 200


@dataclass(frozen=True)
class ContestOccurrence(Occurrence):
    """A contest as the reminder engine sees it, with its platform for posts."""

    platform: str = ''

    @classmethod
    def from_contest(cls, contest: StoredContest) -> 'ContestOccurrence':
        """The contest, at the times members are told; ``ValueError`` if its
        start time isn't known yet.
        """
        if contest.start is None:
            raise ValueError(f'Contest {contest.key} has no start time yet')
        return cls(
            subject=SUBJECT,
            subject_id=contest.key,
            title=contest.name,
            start=contest.start,
            end=contest.end,
            url=contest.url,
            revision=contest.revision,
            cancelled=contest.cancelled,
            platform=contest.platform,
        )


class ContestReminders:
    """The contests of each server, and the posts about them."""

    def __init__(self, repo: ContestRepo) -> None:
        self._repo = repo
        # The platforms this process has tried to sync (see occurrences). An
        # admin is the only source of the club's own contests.
        self._synced: set[str] = {MANUAL}
        # Settings already warned about: the engine asks for a policy every
        # minute, and warnings reach the Discord log channel.
        self._warned: set[tuple[int, ...]] = set()

    @property
    def feature(self) -> str:
        return CONTESTS

    def is_synced(self, platform: str) -> bool:
        """Whether this process has tried to sync the platform (``mark_synced``)."""
        return platform in self._synced

    def mark_synced(self, platform: str) -> None:
        """Note that this process has tried to sync the platform's source.

        Called after every attempt, whether or not it worked: while a site is
        down, reminders go by the contests stored before rather than not at
        all.
        """
        self._synced.add(platform)

    def policy(self, settings: FeatureSettings) -> ReminderPolicy:
        """Reminders at the server's ``reminder_minutes``, and at the start if
        it has ``start_posts`` on.

        An entry that isn't from 1 minute to 400 days, or that repeats an
        earlier one, is left out, with a warning once per distinct setting.
        """
        contest = contest_settings(settings)
        minutes = contest.reminder_minutes
        kept: list[int] = []
        ignored: list[int] = []
        for value in minutes:
            if 0 < value <= _LONGEST_REMINDER_MINUTES and value not in kept:
                kept.append(value)
            else:
                ignored.append(value)
        if ignored and minutes not in self._warned:
            self._warned.add(minutes)
            logger.warning(
                'Ignoring %s in the contest reminder_minutes %s: each reminder is '
                'from 1 to %d minutes before the start, and listed once',
                ', '.join(map(str, ignored)),
                minutes,
                _LONGEST_REMINDER_MINUTES,
            )
        return ReminderPolicy(
            offsets=tuple(timedelta(minutes=value) for value in kept),
            announce_start=contest.start_posts,
            horizon=_HORIZON,
        )

    async def occurrences(
        self,
        guild_id: int,
        settings: FeatureSettings,
        start: datetime,
        end: datetime,
    ) -> Sequence[ContestOccurrence]:
        """The server's contests starting in [start, end), cancelled ones too.

        Those are the contests of the server's platforms with a known start
        time, but none of a platform that this process hasn't tried to sync
        yet. Its stored contests may be from before the bot restarted: a
        contest that moved meanwhile would get a reminder at its old time,
        then a time change notice, and then perhaps the reminder again.
        """
        platforms = [
            platform
            for platform in contest_settings(settings).platforms
            if self.is_synced(platform)
        ]
        contests = await self._repo.between(
            start, end, platforms=platforms, include_cancelled=True
        )
        return [ContestOccurrence.from_contest(contest) for contest in contests]

    def render(self, notice: Notice) -> OutgoingMessage:
        """The post for ``notice``. It mentions the contests role."""
        if notice.kind is NoticeKind.REMINDER:
            return _reminder(notice)
        # Only reminders are ever about several contests.
        (contest,) = notice.occurrences
        if notice.kind is NoticeKind.MOVED:
            lines = [
                *_platform_lines(contest),
                f'**New time:** {_start(contest.start)}',
            ]
            if notice.previous_start is not None:
                previous = discord_timestamp(notice.previous_start, 'F')
                lines.append(f'**Previously:** {previous}')
            return _post(
                f'Time changed: {contest.title}', '\n'.join(lines), url=contest.url
            )
        if notice.kind is NoticeKind.CANCELLED:
            planned = discord_timestamp(contest.start, 'F')
            lines = [*_platform_lines(contest), f'**Was planned for:** {planned}']
            return _post(f'Cancelled: {contest.title}', '\n'.join(lines))
        return _post(
            f'Back on: {contest.title}', contest_details(contest), url=contest.url
        )


def platform_name(platform: str) -> str:
    """The platform as posts name it: 'Codeforces', or 'Club' for an admin's."""
    return _PLATFORM_NAMES.get(platform, platform)


def platform_line(platform: str) -> str:
    """The line of a contest's details that names its platform."""
    return f'**Platform:** {platform_name(platform)}'


def contest_details(contest: Occurrence) -> str:
    """The contest's platform, start, duration and link, one per line.

    Times show in each reader's own time zone. What isn't known is left out,
    and so is a link too long for a post about several contests.
    """
    lines = [*_platform_lines(contest), f'**Starts:** {_start(contest.start)}']
    if contest.end is not None:
        duration = describe_duration(contest.end - contest.start, precise=True)
        lines.append(f'**Duration:** {duration}')
    link = contest_link(contest.url)
    if link is not None:
        lines.append(link)
    return '\n'.join(lines)


def contest_link(url: str | None) -> str | None:
    """A Markdown link to the contest's page; None if it has none or it's too long."""
    if url is None:
        return None
    # A ')' would end the link early.
    target = url.replace('(', '%28').replace(')', '%29')
    return f'[Contest page]({target})' if len(target) <= _LINK_LIMIT else None


def contest_field(name: str, details: str) -> EmbedField:
    """An embed field about one contest, for a post about several."""
    return EmbedField(shorten(name, _FIELD_NAME_LIMIT) or name, details)


def _reminder(notice: Notice) -> OutgoingMessage:
    """'Starting now' at the start; 'Starting soon' before it."""
    heading = 'Starting now' if not notice.offset else 'Starting soon'
    contests = notice.occurrences
    if len(contests) == 1:
        (contest,) = contests
        return _post(
            f'{heading}: {contest.title}', contest_details(contest), url=contest.url
        )
    # Contests at one time, such as a round's Div. 1 and Div. 2, share a post.
    return _post(
        f'{heading}: {len(contests)} contests',
        fields=tuple(
            contest_field(contest.title, contest_details(contest))
            for contest in contests
        ),
    )


def _post(
    title: str,
    description: str | None = None,
    *,
    url: str | None = None,
    fields: tuple[EmbedField, ...] = (),
) -> OutgoingMessage:
    return OutgoingMessage(
        title=title,
        description=description,
        url=url,
        fields=fields,
        footer=FOOTER,
        mention_role=True,
    )


def _platform_lines(contest: Occurrence) -> list[str]:
    """The line naming the contest's platform; none for a plain ``Occurrence``."""
    # The occurrences this source lists are all ContestOccurrences.
    if isinstance(contest, ContestOccurrence) and contest.platform:
        return [platform_line(contest.platform)]
    return []


def _start(moment: datetime) -> str:
    """A start as a date and time, then how far off it is."""
    return f'{discord_timestamp(moment, "F")} ({discord_timestamp(moment, "R")})'
