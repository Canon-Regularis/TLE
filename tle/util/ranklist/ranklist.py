import itertools
from collections.abc import Sequence
from typing import Any

from discord.ext import commands

from tle.util import codeforces_api as cf
from tle.util.codeforces_api import RanklistRow, make_from_dict
from tle.util.handledict import HandleDict
from tle.util.ranklist.rating_calculator import CodeforcesRatingCalculator

# The participant type of a row made up for someone rated in a contest whom
# Codeforces' public standings leave out: it has their official rank, and no
# results.
UNLISTED = 'UNLISTED'


class RanklistError(commands.CommandError):
    def __init__(self, contest: Any, message: str | None = None) -> None:
        if message is not None:
            super().__init__(message)
        self.contest = contest


class ContestNotRatedError(RanklistError):
    def __init__(self, contest: Any) -> None:
        super().__init__(contest, f'`{contest.name}` is not rated')


class HandleNotPresentError(RanklistError):
    def __init__(self, contest: Any, handle: str) -> None:
        super().__init__(
            contest, f'Handle `{handle}`` not present in standings of `{contest.name}`'
        )
        self.handle = handle


class DeltasNotPresentError(RanklistError):
    def __init__(self, contest: Any) -> None:
        super().__init__(
            contest, f'Rating changes for `{contest.name}` not calculated or set.'
        )


class Ranklist:
    def __init__(
        self,
        contest: cf.Contest,
        problems: list[cf.Problem],
        standings: list[cf.RanklistRow],
        fetch_time: float,
        *,
        is_rated: bool,
    ) -> None:
        self.contest = contest
        self.problems = problems
        self.standings = standings
        self.fetch_time = fetch_time
        self.is_rated = is_rated
        self.delta_by_handle: dict[str, int] | None = None
        self.deltas_status: str | None = None
        self.standing_by_id: HandleDict | None = None
        # Whether the ranks are Codeforces' official ones (set_official_results).
        self.official = False
        # The rating changes of those rated whom the standings leave out.
        self._unlisted = HandleDict()
        self._create_inverse_standings()

    def _create_inverse_standings(self) -> None:
        self.standing_by_id = HandleDict()
        for row in self.standings:
            id_ = self.get_ranklist_lookup_key(row)
            self.standing_by_id[id_] = row

    def remove_unofficial_contestants(self) -> None:
        """Remove unofficial contestants from the ranklist.

        To be used for cases when official ranklist contains unofficial
        contestants Currently this is seen is Educational Contests ranklist
        where div1 contestants are marked official in api result

        Official ranks already count only those rated, so they are kept;
        predicted ranks are counted again among those left.
        """

        if self.delta_by_handle is None:
            raise DeltasNotPresentError(self.contest)

        if self.official:
            self.standings = [
                row
                for row in self.standings
                if self.get_ranklist_lookup_key(row) in self.delta_by_handle
            ]
            self._create_inverse_standings()
            return

        official_standings = []
        current_rated_rank = 1
        last_rated_rank = 0
        last_rated_score: tuple[float, int] = (-1, -1)
        for contestant in self.standings:
            handle = self.get_ranklist_lookup_key(contestant)
            if handle in self.delta_by_handle:
                current_score = (contestant.points, contestant.penalty)
                assert self.standing_by_id is not None
                standings_row = self.standing_by_id[handle]._asdict()
                standings_row['rank'] = (
                    current_rated_rank
                    if current_score != last_rated_score
                    else last_rated_rank
                )
                official_standings.append(make_from_dict(RanklistRow, standings_row))
                last_rated_rank = standings_row['rank']
                last_rated_score = current_score
                current_rated_rank += 1

        self.standings = official_standings
        self._create_inverse_standings()

    def set_deltas(self, delta_by_handle: dict[str, int]) -> None:
        if not self.is_rated:
            raise ContestNotRatedError(self.contest)
        self.delta_by_handle = delta_by_handle.copy()
        self.deltas_status = 'Final'

    def set_official_results(self, changes: Sequence[cf.RatingChange]) -> None:
        """Set the contest's rating changes, and with them Codeforces' official
        ranks.

        The public standings can leave rated contestants out, and then rank
        those listed among themselves. So every listed row takes its official
        rank: a rated row its own, and an unrated one, such as a Div. 1
        contestant's in an Educational round, the rank of a rated row it ties
        with, or else that of the nearest rated rows below it, or else the rank
        after everyone rated. Those rated whom the standings leave out are kept
        for ``get_standing_row``. In a team contest, only the changes are set.
        """
        self.set_deltas(
            {change.handle: change.newRating - change.oldRating for change in changes}
        )
        if any(row.party.teamId is not None for row in self.standings):
            return
        change_by_handle = {change.handle.lower(): change for change in changes}

        def score(row: cf.RanklistRow) -> tuple[float, int]:
            return -row.points, row.penalty

        groups = [
            list(group)
            for _, group in itertools.groupby(
                sorted(self.standings, key=score), key=score
            )
        ]
        below = len(changes) + 1  # the rank of the nearest rated rows below
        ranked = []
        for group in reversed(groups):
            group_changes = [
                change_by_handle.get(self.get_ranklist_lookup_key(row).lower())
                for row in group
            ]
            rated = [change.rank for change in group_changes if change is not None]
            # Tied rated rows share an official rank, but should one ever be
            # worse, the group still takes the best.
            group_rank = min(rated) if rated else below
            ranked.append(
                [
                    row._replace(rank=group_rank if change is None else change.rank)
                    for row, change in zip(group, group_changes, strict=True)
                ]
            )
            below = group_rank
        self.standings = [row for group in reversed(ranked) for row in group]
        self._create_inverse_standings()

        listed = {self.get_ranklist_lookup_key(row).lower() for row in self.standings}
        self._unlisted = HandleDict()
        for change in changes:
            if change.handle.lower() not in listed:
                self._unlisted[change.handle] = change
        self.official = True

    def predict(self, current_rating: dict[str, int]) -> None:
        if not self.is_rated:
            raise ContestNotRatedError(self.contest)
        assert self.standing_by_id is not None
        standings = [
            (id_, row.points, row.penalty, current_rating[id_])
            for id_, row in self.standing_by_id.items()
            if id_ in current_rating
        ]
        if standings:
            self.delta_by_handle = CodeforcesRatingCalculator(
                standings
            ).calculate_rating_changes()
        self.deltas_status = 'Predicted'

    def get_delta(self, handle: str) -> int | None:
        """The rating change of ``handle``, in any case, unlisted or not."""
        if not self.is_rated:
            raise ContestNotRatedError(self.contest)
        row = self.get_standing_row(handle)
        assert self.delta_by_handle is not None
        return self.delta_by_handle.get(self.get_ranklist_lookup_key(row))

    def get_standing_row(self, handle: str) -> cf.RanklistRow:
        """The row of ``handle``, in any case. For someone rated whom the
        standings leave out, an UNLISTED row: their official rank, and no
        results.
        """
        assert self.standing_by_id is not None
        try:
            return self.standing_by_id[handle]  # type: ignore[no-any-return]
        except KeyError:
            pass
        try:
            change = self._unlisted[handle]
        except KeyError:
            raise HandleNotPresentError(self.contest, handle)
        party = cf.Party(
            contestId=self.contest.id,
            members=[cf.Member(handle=change.handle)],
            participantType=UNLISTED,
            teamId=None,
            teamName=None,
            ghost=False,
            room=None,
            startTimeSeconds=None,
        )
        return cf.RanklistRow(
            party=party, rank=change.rank, points=0.0, penalty=0, problemResults=[]
        )

    @staticmethod
    def get_ranklist_lookup_key(contestant: cf.RanklistRow) -> str:
        return contestant.party.teamName or contestant.party.members[0].handle
