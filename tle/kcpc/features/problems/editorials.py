"""Where a weekly problem's solution is: the links its solution post gives.

AtCoder's editorials are read from each task's editorial page, which lists
the official ones (``EditorialFinder``). Codeforces' are never fetched: its
contest pages are behind a bot check, and its API has no editorials. So a
Codeforces problem's solution is the link an admin set, or else its contest's
page, where Codeforces lists the editorial under Contest materials for members
to open.
"""

from dataclasses import dataclass

from tle.kcpc.features.problems.catalog import ATCODER
from tle.kcpc.features.problems.repo import WeeklyProblem
from tle.kcpc.platforms import codeforces
from tle.kcpc.platforms.atcoder.editorials import (
    AtCoderEditorials,
    AtCoderEditorialsClient,
)

EDITORIAL = 'Editorial'
ALL_EDITORIALS = 'All editorials'
CONTEST_MATERIALS = 'Contest materials'


@dataclass(frozen=True)
class SolutionLink:
    """A link that a solution post gives, and what it is."""

    url: str
    label: str  # EDITORIAL, ALL_EDITORIALS or CONTEST_MATERIALS


class EditorialFinder:
    """Finds problems' editorials on the sites that list them."""

    def __init__(self, atcoder: AtCoderEditorialsClient) -> None:
        self._atcoder = atcoder

    async def atcoder(
        self, contest_id: str, problem_id: str
    ) -> AtCoderEditorials | None:
        """The editorials on the AtCoder task's page; None if AtCoder has no
        such task, or doesn't show it yet.

        Raises ``ExternalServiceError`` if AtCoder can't be reached or sends a
        page that can't be read, and ``KcpcUserError`` if an ID can't be one.
        """
        return await self._atcoder.fetch(contest_id, problem_id)


def solution_links(row: WeeklyProblem) -> tuple[SolutionLink, ...]:
    """The links to a weekly problem's solution, the one to show first first.

    The link an admin set or the bot found, if there is one; then, on
    AtCoder, the task's page of editorials, always; on Codeforces without a
    link, the contest's page.
    """
    links: list[SolutionLink] = []
    if row.solution_url is not None:
        links.append(SolutionLink(row.solution_url, EDITORIAL))
    if row.source == ATCODER:
        page = AtCoderEditorials(row.contest_id, row.problem_id, ()).page_url
        links.append(SolutionLink(page, ALL_EDITORIALS))
    elif row.solution_url is None:
        page = codeforces.CONTEST_URL.format(contest_id=row.contest_id)
        links.append(SolutionLink(page, CONTEST_MATERIALS))
    return tuple(links)
