"""Component tests for CF API response parsing.

Tests make_from_dict, and the client's own parsers of submissions and standings,
with JSON fixtures shaped like Codeforces' answers.
"""

import json
from pathlib import Path

import pytest

from tle.util import codeforces_api as cf
from tle.util.codeforces_api import (
    Contest,
    Member,
    Party,
    Problem,
    ProblemResult,
    RanklistRow,
    RatingChange,
    Submission,
    User,
    make_from_dict,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'fixtures' / 'cf_api_responses'


def _load_fixture(name):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


# --- contest.list parsing ---


class TestParseContestList:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_fixture('contest_list.json')

    def test_parse_all_contests(self):
        contests = [make_from_dict(Contest, d) for d in self.data]
        assert len(contests) == 2

    def test_field_values(self):
        contest = make_from_dict(Contest, self.data[0])
        assert contest.id == 1
        assert contest.name == 'Codeforces Beta Round #1'
        assert contest.type == 'CF'
        assert contest.phase == 'FINISHED'
        assert contest.durationSeconds == 7200
        assert contest.startTimeSeconds == 1265979600

    def test_missing_optional_defaults_none(self):
        contest = make_from_dict(Contest, self.data[0])
        assert contest.preparedBy is None

    def test_prepared_by_present(self):
        contest = make_from_dict(Contest, self.data[1])
        assert contest.preparedBy == 'MikeMirzayanov'


# --- user.info parsing ---


class TestParseUserInfo:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_fixture('user_info.json')

    def test_parse_user(self):
        user = make_from_dict(User, self.data[0])
        assert isinstance(user, User)

    def test_field_values(self):
        user = make_from_dict(User, self.data[0])
        assert user.handle == 'tourist'
        assert user.firstName == 'Gennady'
        assert user.lastName == 'Korotkevich'
        assert user.country == 'Belarus'
        assert user.city == 'Gomel'
        assert user.organization == 'ITMO University'
        assert user.contribution == 158
        assert user.rating == 3825
        assert user.maxRating == 3979
        assert user.lastOnlineTimeSeconds == 1700000000
        assert user.registrationTimeSeconds == 1265987288
        assert user.friendOfCount == 30000

    def test_effective_rating(self):
        user = make_from_dict(User, self.data[0])
        assert user.effective_rating == 3825

    def test_rank_property(self):
        user = make_from_dict(User, self.data[0])
        assert user.rank.title == 'Legendary Grandmaster'


# --- user.status parsing (nested) ---


class TestParseUserStatus:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_fixture('user_status.json')

    def _parse_submission(self, raw):
        """Parse one submission as cf.user.status and cf.contest.status do."""
        (sub,) = cf._parse_submissions([raw])
        return sub

    def test_parse_submission(self):
        sub = self._parse_submission(self.data[0])
        assert isinstance(sub, Submission)
        assert sub.id == 123456789

    def test_nested_problem(self):
        sub = self._parse_submission(self.data[0])
        assert isinstance(sub.problem, Problem)
        assert sub.problem.name == 'Theatre Square'
        assert sub.problem.rating == 1000
        assert sub.problem.tags == ['math']

    def test_nested_party(self):
        sub = self._parse_submission(self.data[0])
        assert isinstance(sub.author, Party)
        assert sub.author.participantType == 'CONTESTANT'

    def test_nested_member(self):
        sub = self._parse_submission(self.data[0])
        assert isinstance(sub.author.members[0], Member)
        assert sub.author.members[0].handle == 'tourist'

    def test_verdict_and_language(self):
        sub = self._parse_submission(self.data[0])
        assert sub.verdict == 'OK'
        assert sub.programmingLanguage == 'GNU C++17'

    def test_passed_test_count(self):
        sub = self._parse_submission(self.data[0])
        assert sub.passedTestCount == 10

    def test_parses_every_submission_in_order(self):
        subs = cf._parse_submissions(self.data)
        assert [sub.id for sub in subs] == [raw['id'] for raw in self.data]


class TestSubmissionFields:
    def test_passed_test_count_is_the_last_field(self):
        assert Submission._fields[-1] == 'passedTestCount'

    def test_passed_test_count_defaults_to_none(self, make_submission):
        # Keyword constructions that predate it, in tests and KCPC, still work.
        assert make_submission().passedTestCount is None

    def test_passed_test_count_is_none_when_codeforces_leaves_it_out(self):
        raw = _load_fixture('user_status.json')[0]
        del raw['passedTestCount']
        (sub,) = cf._parse_submissions([raw])
        assert sub.passedTestCount is None


# --- contest.status parsing (virtual, practice and team parties) ---


class TestParseContestStatus:
    @pytest.fixture(autouse=True)
    def _load(self):
        subs = cf._parse_submissions(_load_fixture('contest_status_virtual.json'))
        self.by_id = {sub.id: sub for sub in subs}

    def test_every_submission_parses(self):
        assert all(isinstance(sub, Submission) for sub in self.by_id.values())
        assert all(isinstance(sub.author, Party) for sub in self.by_id.values())

    def test_virtual_party(self):
        sub = self.by_id[340000006]
        assert sub.author.participantType == 'VIRTUAL'
        assert sub.author.startTimeSeconds == 1760000000
        assert sub.author.teamId is None
        assert [member.handle for member in sub.author.members] == ['Alice_VC']
        assert sub.relativeTimeSeconds == 930
        assert sub.passedTestCount == 40

    def test_practice_after_the_virtual_contest(self):
        sub = self.by_id[340000010]
        assert sub.author.participantType == 'PRACTICE'
        assert sub.relativeTimeSeconds == 2147483647

    def test_team_party(self):
        sub = self.by_id[339999990]
        assert sub.author.participantType == 'VIRTUAL'
        assert (sub.author.teamId, sub.author.teamName) == (190001, 'Pair')
        assert [member.handle for member in sub.author.members] == [
            'Alice_VC',
            'Bob_VC',
        ]

    def test_failure_on_test_1(self):
        sub = self.by_id[340000004]
        assert (sub.verdict, sub.passedTestCount) == ('WRONG_ANSWER', 0)

    def test_compilation_error(self):
        sub = self.by_id[340000002]
        assert (sub.verdict, sub.passedTestCount) == ('COMPILATION_ERROR', 0)


# --- contest.ratingChanges parsing ---


class TestParseRatingChanges:
    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_fixture('rating_changes.json')

    def test_parse_changes(self):
        changes = [make_from_dict(RatingChange, d) for d in self.data]
        assert len(changes) == 1

    def test_field_values(self):
        change = make_from_dict(RatingChange, self.data[0])
        assert change.contestId == 1
        assert change.contestName == 'Codeforces Beta Round #1'
        assert change.handle == 'tourist'
        assert change.rank == 1
        assert change.ratingUpdateTimeSeconds == 1265986800
        assert change.oldRating == 1500
        assert change.newRating == 1602


# --- contest.standings parsing (nested) ---


class TestParseContestStandings:
    """The fixture is shaped like the standings of 2273, a Div. 1 round, as
    Codeforces gave them a day after it ended (12 October 2026): official
    contestants only, each party with participantId, ghost, room and
    startTimeSeconds, hack counts on each row, CF-type penalties of 0, and
    problems not rated yet. Its rows are made up.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_fixture('contest_standings.json')

    def _parse_standings(self):
        return cf._parse_standings(self.data)

    def test_parse_contest(self):
        contest, _, _ = self._parse_standings()
        assert isinstance(contest, Contest)
        assert contest.id == 2273
        assert (contest.type, contest.phase) == ('CF', 'FINISHED')
        assert contest.startTimeSeconds == 1791743700
        assert contest.durationSeconds == 7200
        assert contest.preparedBy is None

    def test_parse_problems(self):
        _, problems, _ = self._parse_standings()
        assert all(isinstance(problem, Problem) for problem in problems)
        assert [problem.index for problem in problems] == list('ABCDEF')
        assert [problem.points for problem in problems] == [
            500.0,
            1000.0,
            1250.0,
            2000.0,
            2250.0,
            3500.0,
        ]

    def test_problems_of_a_new_contest_have_no_rating(self):
        _, problems, _ = self._parse_standings()
        assert [problem.rating for problem in problems] == [None] * 6
        assert not any(problem.has_metadata() for problem in problems)

    def test_ranklist_row_fields(self):
        _, _, ranklist = self._parse_standings()
        assert all(isinstance(row, RanklistRow) for row in ranklist)
        row = ranklist[0]
        assert row.rank == 1
        assert row.points == 4405.0  # its problems' 4305 and one hack's 100
        assert row.penalty == 0

    def test_rows_are_official_contestants_with_shared_ranks(self):
        _, _, ranklist = self._parse_standings()
        assert {row.party.participantType for row in ranklist} == {'CONTESTANT'}
        assert [row.rank for row in ranklist] == [1, 2, 3, 3, 5, 5]
        assert {row.penalty for row in ranklist} == {0}

    def test_problem_result_fields(self):
        _, _, ranklist = self._parse_standings()
        pr = ranklist[0].problemResults[0]
        assert isinstance(pr, ProblemResult)
        assert pr.points == 492.0
        assert pr.penalty is None
        assert pr.rejectedAttemptCount == 0
        assert pr.type == 'FINAL'
        assert pr.bestSubmissionTimeSeconds == 240

    def test_unsolved_problem_result(self):
        _, _, ranklist = self._parse_standings()
        pr = ranklist[0].problemResults[4]
        assert (pr.points, pr.rejectedAttemptCount) == (0.0, 2)
        assert pr.bestSubmissionTimeSeconds is None

    def test_party_in_standings(self):
        _, _, ranklist = self._parse_standings()
        party = ranklist[0].party
        assert isinstance(party, Party)
        assert party.contestId == 2273
        assert party.participantType == 'CONTESTANT'
        assert len(party.members) == 1
        assert isinstance(party.members[0], Member)
        assert party.members[0].handle == 'Alice'
        assert party.ghost is False
        assert party.room == 7
        assert party.startTimeSeconds == 1791743700
        # Codeforces leaves the team keys out of a solo party.
        assert (party.teamId, party.teamName) == (None, None)

    def test_keys_the_client_does_not_model_are_left_out(self):
        # participantId on the party, the hack counts on the row
        _, _, ranklist = self._parse_standings()
        assert 'participantId' not in Party._fields
        assert not {'successfulHackCount', 'unsuccessfulHackCount'} & set(
            RanklistRow._fields
        )
        assert len(ranklist) == len(self.data['rows'])
