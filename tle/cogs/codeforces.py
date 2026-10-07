import datetime
import random
from collections import defaultdict
from collections.abc import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from tle.util import (
    codeforces_api as cf,
    codeforces_common as cf_common,
    discord_common,
    paginator,
)
from tle.util.cache import ContestNotFound, ProblemsetNotCached
from tle.util.db.user_db_conn import Gitgud

_GITGUD_NO_SKIP_TIME = 3 * 60 * 60
_GITGUD_SCORE_DISTRIB = (2, 3, 5, 8, 12, 17, 23)
_GITGUD_MAX_ABS_DELTA_VALUE = 300


class CodeforcesCogError(commands.CommandError):
    pass


# The gitgud commands and gimme run one at a time for each member (see
# cf_common.user_guard); one used while another is running gets this reply.
GITGUD_RUNNING_MESSAGE = (
    'You already have a gitgud command running. Try again when it finishes.'
)


def _gitgud_running() -> CodeforcesCogError:
    """The error for a gitgud command used while another one of the member's
    is still running.
    """
    return CodeforcesCogError(GITGUD_RUNNING_MESSAGE)


class Codeforces(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.converter = commands.MemberConverter()

    async def _validate_gitgud_status(
        self, ctx: commands.Context, delta: int | None
    ) -> None:
        # Codeforces hasn't been asked anything yet, so a refusal here gives
        # back the use that the command's cooldown counted.
        if delta is not None and delta % 100 != 0:
            discord_common.undo_cooldown(ctx)
            raise CodeforcesCogError('Delta must be a multiple of 100.')

        if delta is not None and abs(delta) > _GITGUD_MAX_ABS_DELTA_VALUE:
            discord_common.undo_cooldown(ctx)
            raise CodeforcesCogError(
                f'Delta must range from -{_GITGUD_MAX_ABS_DELTA_VALUE}'
                f' to {_GITGUD_MAX_ABS_DELTA_VALUE}.'
            )

        user_id = ctx.message.author.id
        active = await self.bot.user_db.check_challenge(user_id)
        if active is not None:
            _, _, name, contest_id, index, _ = active
            url = f'{cf.CONTEST_BASE_URL}{contest_id}/problem/{index}'
            discord_common.undo_cooldown(ctx)
            raise CodeforcesCogError(f'You have an active challenge {name} at {url}')

    async def _gitgud(
        self, ctx: commands.Context, handle: str, problem: cf.Problem, delta: int
    ) -> None:
        # The caller of this function is responsible for calling
        # `_validate_gitgud_status` first.
        user_id = ctx.author.id

        issue_time = datetime.datetime.now().timestamp()
        rc = await self.bot.user_db.new_challenge(user_id, issue_time, problem, delta)
        if rc != 1:
            raise CodeforcesCogError(
                'Your challenge has already been added to the database!'
            )

        title = f'{problem.index}. {problem.name}'
        desc = self.bot.cf_cache.contest_cache.get_contest(problem.contestId).name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=problem.rating)
        await ctx.send(f'Challenge problem for `{handle}`', embed=embed)

    # The cooldowns of the commands below count a use only once the command's
    # arguments are parsed, so that a mistyped command costs no wait.
    @commands.hybrid_command(
        brief='Upsolve a problem from a contest you took part in, for gitgud points',
        cooldown_after_parsing=True,
    )
    @app_commands.describe(
        choice="A problem's number in the list, to take it as your gitgud challenge; "
        'shows the list if left out'
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def upsolve(self, ctx: commands.Context, choice: int | None = None) -> None:
        """Upsolve a problem from a rated contest you took part in, for gitgud points.

        Without a number, it lists the newest problems you haven't solved from
        those contests, rated within 300 of your rating. With a problem's number,
        that problem becomes your gitgud challenge, and its points depend on its
        rating minus yours:

        delta  | -300 | -200 | -100 |  0  | +100 | +200 | +300
        points |   2  |   3  |   5  |  8  |  12  |  17  |  23

        Examples:
            /upsolve
            /upsolve choice:2
            ;upsolve 2
        """
        await self._validate_gitgud_status(ctx, delta=None)
        (handle,) = await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author),)
        )
        user = await self.bot.user_db.fetch_cf_user(handle)
        rating = round(user.effective_rating, -2)
        # Codeforces may take longer than the 3 seconds a slash command has to
        # answer, so typing defers the answer first.
        async with ctx.typing():
            resp = await cf.user.rating(handle=handle)
            submissions = await cf.user.status(handle=handle)
        contests = {change.contestId for change in resp}
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}
        problems = [
            prob
            for prob in self.bot.cf_cache.problem_cache.problems
            if prob.name not in solved
            and prob.contestId in contests
            and abs(rating - prob.rating) <= 300
        ]

        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(
            key=lambda problem: (
                self.bot.cf_cache.contest_cache.get_contest(
                    problem.contestId
                ).startTimeSeconds
            ),
            reverse=True,
        )

        if choice is not None and 0 < choice <= len(problems):
            problem = problems[choice - 1]
            await self._gitgud(ctx, handle, problem, problem.rating - rating)
        else:
            msg = '\n'.join(
                f'{i + 1}: [{prob.name}]({prob.url}) [{prob.rating}]'
                for i, prob in enumerate(problems[:5])
            )
            title = f'Select a problem to upsolve (1-{len(problems)}):'
            embed = discord_common.cf_color_embed(title=title, description=msg)
            await ctx.send(embed=embed)

    @commands.command(
        brief="Get a random Codeforces problem you haven't solved, by rating and tags",
        usage='[rating] [+tag...] [~tag...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def gimme(self, ctx: commands.Context, *args: str) -> None:
        """Get a random Codeforces problem you haven't solved, at your rating.

        Add a rating, such as 1800, for a problem of that rating instead. Add
        tags with `+` to ask for them, or with `~` to rule them out: part of a
        tag's name is enough, such as `+binary` for binary search.

        Examples:
            ;gimme
            ;gimme 1800
            ;gimme +dp ~math 1600
        """
        (handle,) = await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author),)
        )
        cf_user = await self.bot.user_db.fetch_cf_user(handle)
        rating = round(cf_user.effective_rating, -2)
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')
        rating = cf_common.parse_rating(args, rating)

        submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}

        problems = [
            prob
            for prob in self.bot.cf_cache.problem_cache.problems
            if prob.rating == rating
            and prob.name not in solved
            and not cf_common.is_contest_writer(prob.contestId, handle)
            and prob.matches_all_tags(tags)
            and not prob.matches_any_tag(bantags)
        ]

        if not problems:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(
            key=lambda problem: (
                self.bot.cf_cache.contest_cache.get_contest(
                    problem.contestId
                ).startTimeSeconds
            )
        )

        choice = max([random.randrange(len(problems)) for _ in range(2)])
        problem = problems[choice]

        title = f'{problem.index}. {problem.name}'
        desc = self.bot.cf_cache.contest_cache.get_contest(problem.contestId).name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=problem.rating)
        if tags:
            tagslist = ', '.join(problem.get_matched_tags(tags))
            embed.add_field(name='Matched tags', value=tagslist)
        await ctx.send(f'Recommended problem for `{handle}`', embed=embed)

    @commands.command(
        brief='List the problems you or other Codeforces users solved, newest first',
        usage='[handles...] [+hardest] [filters...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def stalk(self, ctx: commands.Context, *args: str) -> None:
        """List the problems you or other Codeforces users solved, newest first.

        Give up to 5 Codeforces handles, or members' names after `!`; without
        any, it lists yours. Add `+hardest` to list the highest-rated first, and
        any of these filters:
        - `+contest`, `+virtual`, `+practice`: solved that way only
        - `+outof`: solved out of competition only
        - `+team`: count team solutions too
        - `+dp`, `~math`: with that tag, or without it
        - `r>=1500`, `r<=2000`: rated at least, or at most, that
        - `d>=2024`, `d<01062025`: solved from, or before, that date
        - `c+edu`: from contests whose names contain that text
        - `i+A`: with that problem index

        Dates are yyyy, mmyyyy or ddmmyyyy.

        Examples:
            ;stalk
            ;stalk tourist +hardest
            ;stalk +dp r>=1800 d>=2024
        """
        (hardest,), remaining = cf_common.filter_flags(args, ['+hardest'])
        filt = cf_common.SubFilter(False)
        filtered_args = filt.parse(remaining)
        handles = filtered_args or ['!' + str(ctx.author)]
        handles = await cf_common.resolve_handles(ctx, self.converter, handles)
        all_subs = [await cf.user.status(handle=handle) for handle in handles]
        submissions = [sub for subs in all_subs for sub in subs]
        submissions = filt.filter_subs(submissions)

        if not submissions:
            raise CodeforcesCogError(
                'Submissions not found within the search parameters'
            )

        if hardest:
            submissions.sort(
                key=lambda sub: (sub.problem.rating or 0, sub.creationTimeSeconds),
                reverse=True,
            )
        else:
            submissions.sort(key=lambda sub: sub.creationTimeSeconds, reverse=True)

        def make_line(sub: cf.Submission) -> str:
            data = (
                f'[{sub.problem.name}]({sub.problem.url})',
                f'[{sub.problem.rating if sub.problem.rating else "?"}]',
                f'({cf_common.days_ago(sub.creationTimeSeconds)})',
            )
            return '\N{EN SPACE}'.join(data)

        def make_page(chunk: Sequence[cf.Submission]) -> tuple[str, discord.Embed]:
            title = '{} solved problems by `{}`'.format(
                'Hardest' if hardest else 'Recently', '`, `'.join(handles)
            )
            hist_str = '\n'.join(make_line(sub) for sub in chunk)
            embed = discord_common.cf_color_embed(description=hist_str)
            return title, embed

        pages = [
            make_page(chunk) for chunk in paginator.chunkify(submissions[:100], 10)
        ]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @commands.command(
        brief='Pick four problems that none of you has tried, for a mashup contest',
        usage='[handles...] [+tag...] [~tag...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def mashup(self, ctx: commands.Context, *args: str) -> None:
        """Pick four problems that none of you has tried, for a mashup contest.

        Give up to 5 Codeforces handles, or members' names after `!`; without
        any, it picks for you. The problems are rated within 100 of your average
        rating. Add tags with `+` to ask for them, or with `~` to rule them out.

        Examples:
            ;mashup
            ;mashup tourist !alice +greedy ~math
        """
        handles: list[str] = [arg for arg in args if arg[0] not in '+~']
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')

        handles = handles or ['!' + str(ctx.author)]
        handles = await cf_common.resolve_handles(ctx, self.converter, handles)
        resp = [await cf.user.status(handle=handle) for handle in handles]
        submissions = [sub for user in resp for sub in user]
        solved = {sub.problem.name for sub in submissions}
        info = await cf.user.info(handles=handles)
        rating = int(
            round(sum(user.effective_rating for user in info) / len(handles), -2)
        )
        problems = [
            prob
            for prob in self.bot.cf_cache.problem_cache.problems
            if abs(prob.rating - rating) <= 100
            and prob.name not in solved
            and not any(
                cf_common.is_contest_writer(prob.contestId, handle)
                for handle in handles
            )
            and not cf_common.is_nonstandard_problem(prob)
            and prob.matches_all_tags(tags)
            and not prob.matches_any_tag(bantags)
        ]

        if len(problems) < 4:
            raise CodeforcesCogError('Problems not found within the search parameters')

        problems.sort(
            key=lambda problem: (
                self.bot.cf_cache.contest_cache.get_contest(
                    problem.contestId
                ).startTimeSeconds
            )
        )

        choices: list[int] = []
        for i in range(4):
            k = max(random.randrange(len(problems) - i) for _ in range(2))
            for c in choices:
                if k >= c:
                    k += 1
            choices.append(k)
            choices.sort()

        problems = list(reversed([problems[k] for k in choices]))
        msg = '\n'.join(
            f'{"ABCD"[i]}: [{p.name}]({p.url}) [{p.rating}]'
            for i, p in enumerate(problems)
        )
        str_handles = '`, `'.join(handles)
        embed = discord_common.cf_color_embed(description=msg)
        await ctx.send(f'Mashup contest for `{str_handles}`', embed=embed)

    @commands.hybrid_command(
        brief='Get a problem to solve for gitgud points', cooldown_after_parsing=True
    )
    @app_commands.describe(
        delta="The problem's rating minus yours, from -300 to 300 in steps of 100; "
        '0 if left out'
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def gitgud(self, ctx: commands.Context, delta: int = 0) -> None:
        """Get a problem to solve for gitgud points.

        It is one you haven't tried, rated your rating plus delta, and is worth
        the points below. Claim them with gotgud once you have solved it, or skip
        it with nogud. You can have one challenge at a time.

        delta  | -300 | -200 | -100 |  0  | +100 | +200 | +300
        points |   2  |   3  |   5  |  8  |  12  |  17  |  23

        Examples:
            /gitgud
            /gitgud delta:200
            ;gitgud -100
        """
        await self._validate_gitgud_status(ctx, delta)
        (handle,) = await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author),)
        )
        user = await self.bot.user_db.fetch_cf_user(handle)
        rating = round(user.effective_rating, -2)
        # Codeforces may take longer than the 3 seconds a slash command has to
        # answer, so typing defers the answer first.
        async with ctx.typing():
            submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions}
        noguds = await self.bot.user_db.get_noguds(ctx.message.author.id)

        problems = [
            prob
            for prob in self.bot.cf_cache.problem_cache.problems
            if (
                prob.rating == rating + delta
                and prob.name not in solved
                and prob.name not in noguds
            )
        ]

        def check(problem: cf.Problem) -> bool:
            return not cf_common.is_nonstandard_problem(problem) and (
                problem.contestId is None
                or not cf_common.is_contest_writer(problem.contestId, handle)
            )

        problems = list(filter(check, problems))
        if not problems:
            raise CodeforcesCogError('No problem to assign')

        problems.sort(
            key=lambda problem: (
                self.bot.cf_cache.contest_cache.get_contest(
                    problem.contestId
                ).startTimeSeconds
            )
        )

        choice = max(random.randrange(len(problems)) for _ in range(2))
        await self._gitgud(ctx, handle, problems[choice], delta)

    @commands.hybrid_command(brief="Show your gitgud history, or another member's")
    @app_commands.describe(
        member='The member whose gitgud history to show; you if left out'
    )
    async def gitlog(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show your gitgud history, or another member's, newest first.

        Each problem shows its rating and, once solved, when and for how many
        points. Challenges that staff skipped are left out.

        Examples:
            /gitlog
            /gitlog member:@alice
            ;gitlog @alice
        """

        def make_line(entry: tuple) -> str:
            issue, finish, name, contest, index, delta, status = entry
            problem = self.bot.cf_cache.problem_cache.problem_by_name[name]
            line = f'[{name}]({problem.url})\N{EN SPACE}[{problem.rating}]'
            if finish:
                time_str = cf_common.days_ago(finish)
                points = f'{_GITGUD_SCORE_DISTRIB[delta // 100 + 3]:+}'
                line += f'\N{EN SPACE}{time_str}\N{EN SPACE}[{points}]'
            return line

        def make_page(chunk: Sequence[tuple]) -> tuple[str, discord.Embed]:
            message = discord.utils.escape_mentions(
                f'gitgud log for {member.display_name}'
            )
            log_str = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        assert isinstance(ctx.author, discord.Member)
        member = member or ctx.author
        data = await self.bot.user_db.gitlog(member.id)
        if not data:
            raise CodeforcesCogError(f'{member.mention} has no gitgud history.')

        pages = [make_page(chunk) for chunk in paginator.chunkify(data, 7)]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @commands.hybrid_command(
        brief='Claim the points for your gitgud challenge once you have solved it'
    )
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def gotgud(self, ctx: commands.Context) -> None:
        """Claim the points for your gitgud challenge once you have solved it.

        It counts as solved once Codeforces has accepted a solution of yours.

        Examples:
            /gotgud
            ;gotgud
        """
        (handle,) = await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author),)
        )
        user_id = ctx.message.author.id
        active = await self.bot.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError('You do not have an active challenge')

        # Codeforces may take longer than the 3 seconds a slash command has to
        # answer, so typing defers the answer first.
        async with ctx.typing():
            submissions = await cf.user.status(handle=handle)
        solved = {sub.problem.name for sub in submissions if sub.verdict == 'OK'}

        challenge_id, issue_time, name, contestId, index, delta = active
        if name not in solved:
            raise CodeforcesCogError("You haven't completed your challenge.")

        delta = _GITGUD_SCORE_DISTRIB[delta // 100 + 3]
        finish_time = int(datetime.datetime.now().timestamp())
        rc = await self.bot.user_db.complete_challenge(
            user_id, challenge_id, finish_time, delta
        )
        if rc == 1:
            duration = cf_common.pretty_time_format(finish_time - issue_time)
            await ctx.send(
                f'Challenge completed in {duration}. {handle} gained {delta} points.'
            )
        else:
            await ctx.send('You have already claimed your points')

    @commands.hybrid_command(brief='Skip your gitgud challenge, without points')
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def nogud(self, ctx: commands.Context) -> None:
        """Skip your gitgud challenge, without points.

        You can skip it once 3 hours have passed since you got it, and gitgud
        won't give you that problem again.

        Examples:
            /nogud
            ;nogud
        """
        await cf_common.resolve_handles(ctx, self.converter, ('!' + str(ctx.author),))
        user_id = ctx.message.author.id
        active = await self.bot.user_db.check_challenge(user_id)
        if not active:
            raise CodeforcesCogError('You do not have an active challenge')

        challenge_id, issue_time, name, contestId, index, delta = active
        finish_time = int(datetime.datetime.now().timestamp())
        if finish_time - issue_time < _GITGUD_NO_SKIP_TIME:
            skip_time = cf_common.pretty_time_format(
                issue_time + _GITGUD_NO_SKIP_TIME - finish_time
            )
            await ctx.send(f'Think more. You can skip your challenge in {skip_time}.')
            return
        await self.bot.user_db.skip_challenge(user_id, challenge_id, Gitgud.NOGUD)
        await ctx.send('Challenge skipped.')

    @commands.hybrid_command(brief="Skip a member's gitgud challenge for them")
    @app_commands.describe(member='The member whose gitgud challenge to skip')
    @cf_common.user_guard(group='gitgud', get_exception=_gitgud_running)
    async def _nogud(self, ctx: commands.Context, member: discord.Member) -> None:
        """Skip a member's gitgud challenge for them, at once and without points.

        The challenge leaves their gitgud history, and gitgud may give them the
        problem again.

        Examples:
            /_nogud member:@alice
            ;_nogud @alice
        """
        active = await self.bot.user_db.check_challenge(member.id)
        if active is None:
            raise CodeforcesCogError(
                f'{member.mention} has no gitgud challenge to skip.'
            )
        rc = await self.bot.user_db.skip_challenge(
            member.id, active[0], Gitgud.FORCED_NOGUD
        )
        if rc == 1:
            await ctx.send('Challenge skip forced.')
        else:
            await ctx.send('Failed to force challenge skip.')

    @commands.command(
        brief='Suggest past contests that none of you has tried, for a virtual contest',
        usage='[handles...] [+text...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def vc(self, ctx: commands.Context, *args: str) -> None:
        """Suggest past contests that none of you has tried, for a virtual contest.

        Give up to 25 Codeforces handles, or members' names after `!`; without
        any, it suggests contests for you. They suit your average rating: Div. 3
        below 1600, Div. 2 below 2100, and Div. 1, Global and similar rounds from
        2100. Add `+text` for contests whose names contain that text instead, such
        as `+edu`.

        Examples:
            ;vc
            ;vc tourist !alice +global
        """
        markers = [x for x in args if x[0] == '+']
        handles = [x for x in args if x[0] != '+'] or ['!' + str(ctx.author)]
        handles = await cf_common.resolve_handles(
            ctx, self.converter, handles, maxcnt=25
        )
        info = await cf.user.info(handles=handles)
        contests = self.bot.cf_cache.contest_cache.get_contests_in_phase('FINISHED')

        if not markers:
            divr = sum(user.effective_rating for user in info) / len(handles)
            div1_indicators = ['div1', 'global', 'avito', 'goodbye', 'hello']
            markers = (
                ['div3']
                if divr < 1600
                else ['div2']
                if divr < 2100
                else div1_indicators
            )

        recommendations = {
            contest.id
            for contest in contests
            if contest.matches(markers)
            and not cf_common.is_nonstandard_contest(contest)
            and not any(
                cf_common.is_contest_writer(contest.id, handle) for handle in handles
            )
        }

        # Discard contests in which user has non-CE submissions.
        visited_contests = await cf_common.get_visited_contests(handles)
        recommendations -= visited_contests

        if not recommendations:
            raise CodeforcesCogError('Unable to recommend a contest')

        rec_list = list(recommendations)
        random.shuffle(rec_list)
        contests = [
            self.bot.cf_cache.contest_cache.get_contest(contest_id)
            for contest_id in rec_list[:25]
        ]

        def make_line(c: cf.Contest) -> str:
            dur = cf_common.pretty_time_format(c.durationSeconds or 0)
            return f'[{c.name}]({c.url}) {dur}'

        def make_page(chunk: Sequence[cf.Contest]) -> tuple[str, discord.Embed]:
            str_handles = '`, `'.join(handles)
            message = f'Recommended contest(s) for `{str_handles}`'
            vc_str = '\n'.join(make_line(contest) for contest in chunk)
            embed = discord_common.cf_color_embed(description=vc_str)
            return message, embed

        pages = [make_page(chunk) for chunk in paginator.chunkify(contests, 5)]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @commands.command(
        brief='List the contests you have partly solved, fewest problems left first',
        usage='[+text...]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def fullsolve(self, ctx: commands.Context, *args: str) -> None:
        """List the contests you have partly solved, fewest problems left first.

        Add `+text` to list only the contests whose names contain that text, such
        as `+edu`.

        Examples:
            ;fullsolve
            ;fullsolve +edu
        """
        (handle,) = await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author),)
        )
        tags = [x for x in args if x[0] == '+']

        problem_to_contests = self.bot.cf_cache.problemset_cache.problem_to_contests
        contests = [
            contest
            for contest in self.bot.cf_cache.contest_cache.get_contests_in_phase(
                'FINISHED'
            )
            if (not tags or contest.matches(tags))
            and not cf_common.is_nonstandard_contest(contest)
        ]

        # subs_by_contest_id contains contest_id mapped to [list of problem.name]
        subs_by_contest_id: defaultdict[int, set[str]] = defaultdict(set)
        for sub in await cf.user.status(handle=handle):
            if sub.verdict == 'OK':
                try:
                    contest = self.bot.cf_cache.contest_cache.get_contest(
                        sub.problem.contestId
                    )
                    problem_id = (sub.problem.name, contest.startTimeSeconds)
                    for contestId in problem_to_contests[problem_id]:
                        subs_by_contest_id[contestId].add(sub.problem.name)
                except ContestNotFound:
                    pass

        contest_unsolved_pairs: list[tuple[cf.Contest, int, int]] = []
        for contest in contests:
            num_solved = len(subs_by_contest_id[contest.id])
            try:
                num_problems = len(
                    await self.bot.cf_cache.problemset_cache.get_problemset(contest.id)
                )
                if 0 < num_solved < num_problems:
                    contest_unsolved_pairs.append((contest, num_solved, num_problems))
            except ProblemsetNotCached:
                # In case of recent contents or cetain bugged contests
                pass

        contest_unsolved_pairs.sort(
            key=lambda p: (p[2] - p[1], -(p[0].startTimeSeconds or 0)),
        )

        if not contest_unsolved_pairs:
            raise CodeforcesCogError(
                f'`{handle}` has no contests to fullsolve :confetti_ball:'
            )

        def make_line(entry: tuple[cf.Contest, int, int]) -> str:
            contest, solved, total = entry
            return f'[{contest.name}]({contest.url})\N{EN SPACE}[{solved}/{total}]'

        def make_page(
            chunk: Sequence[tuple[cf.Contest, int, int]],
        ) -> tuple[str, discord.Embed]:
            message = f'Fullsolve list for `{handle}`'
            full_solve_list = '\n'.join(make_line(entry) for entry in chunk)
            embed = discord_common.cf_color_embed(description=full_solve_list)
            return message, embed

        pages = [
            make_page(chunk) for chunk in paginator.chunkify(contest_unsolved_pairs, 10)
        ]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @staticmethod
    def getEloWinProbability(ra: float, rb: float) -> float:
        return 1.0 / (1 + 10 ** ((rb - ra) / 400.0))

    @staticmethod
    def composeRatings(
        left: float,
        right: float,
        ratings: list[tuple[int | None, int]],
    ) -> int:
        for _tt in range(20):
            r = (left + right) / 2.0

            rWinsProbability = 1.0
            for rating, count in ratings:
                prob = Codeforces.getEloWinProbability(r, rating or 0)
                rWinsProbability *= prob**count

            if rWinsProbability < 0.5:
                left = r
            else:
                right = r
        return round((left + right) / 2)

    @commands.command(
        brief="Work out a team's rating from its members' ratings",
        usage='[handles...] [+peak] [+server]',
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def teamrate(self, ctx: commands.Context, *args: str) -> None:
        """Work out a team's rating from its members' ratings.

        Give their Codeforces handles, or members' names after `!`; without any,
        it rates you alone. Add `*2` after a handle to count it twice, `+peak` to
        use everyone's highest rating, or `+server` to rate everyone in this
        server who has linked a handle. Unrated players don't count.

        Examples:
            ;teamrate tourist Petr
            ;teamrate tourist*2 !alice +peak
            ;teamrate +server
        """

        (is_entire_server, peak), handles = cf_common.filter_flags(
            args, ['+server', '+peak']
        )
        handles = handles or ['!' + str(ctx.author)]

        def rating(user: cf.User) -> int | None:
            return user.maxRating if peak else user.rating

        if is_entire_server:
            res = await self.bot.user_db.get_cf_users_for_guild(ctx.guild.id)
            ratings = [
                (rating(user), 1) for user_id, user in res if user.rating is not None
            ]
            user_str = '+server'
        else:

            def normalize(x: list[str] | tuple[str, ...]) -> list[str]:
                return [i.lower() for i in x]

            handle_counts: dict[str, int] = {}
            parsed_handles: list[str] = []
            for i in handles:
                parse_str = normalize(i.split('*'))
                if len(parse_str) > 1:
                    try:
                        handle_counts[parse_str[0]] = int(parse_str[1])
                    except ValueError:
                        raise CodeforcesCogError("Can't multiply by non-integer")
                else:
                    handle_counts[parse_str[0]] = 1
                parsed_handles.append(parse_str[0])

            cf_handles = await cf_common.resolve_handles(
                ctx, self.converter, parsed_handles, mincnt=1, maxcnt=1000
            )
            cf_handles = normalize(cf_handles)
            cf_to_original = {
                a: b for a, b in zip(cf_handles, parsed_handles, strict=False)
            }
            original_to_cf = {
                a: b for a, b in zip(parsed_handles, cf_handles, strict=False)
            }
            users = await cf.user.info(handles=cf_handles)
            user_strs: list[str] = []
            for a, b in handle_counts.items():
                if b > 1:
                    user_strs.append(f'{original_to_cf[a]}*{b}')
                elif b == 1:
                    user_strs.append(original_to_cf[a])
                elif b <= 0:
                    raise CodeforcesCogError(
                        'How can you have nonpositive members in team?'
                    )

            user_str = ', '.join(user_strs)
            ratings = [
                (rating(user), handle_counts[cf_to_original[user.handle.lower()]])
                for user in users
                if user.rating
            ]

        if len(ratings) == 0:
            raise CodeforcesCogError('None of these Codeforces handles has a rating.')

        left = -100.0
        right = 10000.0
        teamRating = Codeforces.composeRatings(left, right, ratings)
        embed = discord.Embed(
            title=user_str,
            description=teamRating,
            color=cf.rating2rank(teamRating).color_embed,
        )
        await ctx.send(embed=embed)

    @discord_common.send_error_if(
        CodeforcesCogError, cf_common.ResolveHandleError, cf_common.FilterError
    )
    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Codeforces(bot))
