import asyncio
import datetime
import random
from collections import defaultdict, namedtuple
from collections.abc import Sequence
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands
from matplotlib import pyplot as plt

from tle import constants
from tle.util import (
    codeforces_api as cf,
    codeforces_common as cf_common,
    discord_common,
    graph_common as gc,
    paginator,
    table,
)
from tle.util.db.user_db_conn import Duel, DuelType, Winner

_DUEL_INVALIDATE_TIME = 2 * 60
_DUEL_EXPIRY_TIME = 5 * 60
_DUEL_RATING_DELTA = -400
_DUEL_NO_DRAW_TIME = 10 * 60
_ELO_CONSTANT = 60
# The most duels that duel recent lists.
_RECENT_DUELS = 7

DuelRank = namedtuple('DuelRank', 'low high title title_abbr color_graph color_embed')

DUEL_RANKS = (
    DuelRank(-(10**9), 1300, 'Newbie', 'N', '#CCCCCC', 0x808080),
    DuelRank(1300, 1400, 'Pupil', 'P', '#77FF77', 0x008000),
    DuelRank(1400, 1500, 'Specialist', 'S', '#77DDBB', 0x03A89E),
    DuelRank(1500, 1600, 'Expert', 'E', '#AAAAFF', 0x0000FF),
    DuelRank(1600, 1700, 'Candidate Master', 'CM', '#FF88FF', 0xAA00AA),
    DuelRank(1700, 1800, 'Master', 'M', '#FFCC88', 0xFF8C00),
    DuelRank(1800, 1900, 'International Master', 'IM', '#FFBB55', 0xF57500),
    DuelRank(1900, 2000, 'Grandmaster', 'GM', '#FF7777', 0xFF3030),
    DuelRank(2000, 2100, 'International Grandmaster', 'IGM', '#FF3333', 0xFF0000),
    DuelRank(2100, 10**9, 'Legendary Grandmaster', 'LGM', '#AA0000', 0xCC0000),
)


def rating2rank(rating: int) -> DuelRank | None:
    for rank in DUEL_RANKS:
        if rank.low <= rating < rank.high:
            return rank
    return None


class DuelCogError(commands.CommandError):
    pass


def elo_prob(player: float, opponent: float) -> float:
    return (1 + 10 ** ((opponent - player) / 400)) ** -1


def elo_delta(player: float, opponent: float, win: float) -> float:
    return _ELO_CONSTANT * (win - elo_prob(player, opponent))


def check_if_allow_self_register(ctx: commands.Context) -> bool:
    if not constants.ALLOW_DUEL_SELF_REGISTER:
        raise DuelCogError(
            'Self-registration is switched off. Ask a moderator to register you.'
        )
    return True


def _are_members(guild: discord.Guild, *user_ids: int) -> bool:
    """Whether every one of ``user_ids`` is a member of ``guild``.

    The duel tables don't record the server a duel was fought in, so a
    server's lists of duels hold those whose two duelists are its members.
    """
    return all(guild.get_member(user_id) is not None for user_id in user_ids)


async def _button_allowed(interaction: discord.Interaction, command: str) -> bool:
    """Whether the access rules let the member press a button of ``command``,
    such as 'duel accept'; if not, the access service has told them why.

    A bot without an access service lets every press through.
    """
    access = getattr(interaction.client, 'access', None)
    if access is None:
        return True
    return bool(await access.component_allowed(interaction, command))


class DuelChallengeView(discord.ui.View):
    """The Accept, Decline and Withdraw buttons of a challenge to a duel.

    Each button counts as its command (duel accept, duel decline or duel
    withdraw) for the access rules: who may use it, and whether this server
    switched it off. Channels don't count, as the challenge was posted where
    duels are allowed.
    """

    def __init__(
        self,
        bot: commands.Bot,
        duelid: int,
        challenger_id: int,
        challengee_id: int,
        problem_name: str,
        *,
        timeout: float,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.duelid = duelid
        self.challenger_id = challenger_id
        self.challengee_id = challengee_id
        self.problem_name = problem_name
        self.message: discord.Message | None = None

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.success)
    async def accept_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        if not await _button_allowed(interaction, 'duel accept'):
            return
        if interaction.user.id != self.challengee_id:
            await interaction.response.send_message(
                'Only the challenged user can accept.',
                ephemeral=True,
            )
            return

        await self._disable_all(interaction)

        challenger = interaction.guild.get_member(self.challenger_id)
        challengee = interaction.guild.get_member(self.challengee_id)
        channel = interaction.channel

        await channel.send(
            f'Duel between {challenger.mention} and'
            f' {challengee.mention} starting in 15 seconds!'
        )
        await asyncio.sleep(15)

        start_time = datetime.datetime.now().timestamp()
        rc = await self.bot.user_db.start_duel(self.duelid, start_time)
        if rc != 1:
            await channel.send(
                f'Unable to start the duel between {challenger.mention}'
                f' and {challengee.mention}.'
            )
            return

        problem = self.bot.cf_cache.problem_cache.problem_by_name[self.problem_name]
        title = f'{problem.index}. {problem.name}'
        desc = self.bot.cf_cache.contest_cache.get_contest(problem.contestId).name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=problem.rating)
        await channel.send(
            f'Starting duel: {challenger.mention} vs {challengee.mention}',
            embed=embed,
        )

    @discord.ui.button(label='Decline', style=discord.ButtonStyle.danger)
    async def decline_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        if not await _button_allowed(interaction, 'duel decline'):
            return
        if interaction.user.id != self.challengee_id:
            await interaction.response.send_message(
                'Only the challenged user can decline.',
                ephemeral=True,
            )
            return

        await self._disable_all(interaction)
        rc = await self.bot.user_db.cancel_duel(self.duelid, Duel.DECLINED)
        challenger = interaction.guild.get_member(self.challenger_id)
        challengee = interaction.guild.get_member(self.challengee_id)
        if rc:
            message = (
                f'{challengee.mention} declined a challenge by {challenger.mention}.'
            )
            embed = discord_common.embed_alert(message)
            await interaction.channel.send(embed=embed)
        else:
            await interaction.channel.send('This duel has already been resolved.')

    @discord.ui.button(label='Withdraw', style=discord.ButtonStyle.secondary)
    async def withdraw_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        if not await _button_allowed(interaction, 'duel withdraw'):
            return
        if interaction.user.id != self.challenger_id:
            await interaction.response.send_message(
                'Only the challenger can withdraw.',
                ephemeral=True,
            )
            return

        await self._disable_all(interaction)
        rc = await self.bot.user_db.cancel_duel(self.duelid, Duel.WITHDRAWN)
        challenger = interaction.guild.get_member(self.challenger_id)
        challengee = interaction.guild.get_member(self.challengee_id)
        if rc:
            message = (
                f'{challenger.mention} withdrew a challenge to {challengee.mention}.'
            )
            embed = discord_common.embed_alert(message)
            await interaction.channel.send(embed=embed)
        else:
            await interaction.channel.send('This duel has already been resolved.')

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass
        rc = await self.bot.user_db.cancel_duel(self.duelid, Duel.EXPIRED)
        if rc and self.message:
            challenger = self.message.guild.get_member(self.challenger_id)
            challengee = self.message.guild.get_member(self.challengee_id)
            message = (
                f'{challenger.mention}, your request to duel'
                f' {challengee.mention} has expired!'
            )
            embed = discord_common.embed_alert(message)
            await self.message.channel.send(embed=embed)


class Dueling(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot: commands.Bot = bot
        self.converter: commands.MemberConverter = commands.MemberConverter()
        self.draw_offers: dict[int, int] = {}

    async def _get_cf_user(self, userid: int, guild_id: int) -> Any:
        handle = await self.bot.user_db.get_handle(userid, guild_id)
        return await self.bot.user_db.fetch_cf_user(handle)

    async def _complete_duel(
        self,
        duelid: int,
        guild_id: int,
        win_status: Winner,
        winner: discord.Member,
        loser: discord.Member,
        finish_time: float,
        score: float,
        dtype: DuelType,
    ) -> discord.Embed | None:
        winner_r = await self.bot.user_db.get_duel_rating(winner.id)
        loser_r = await self.bot.user_db.get_duel_rating(loser.id)
        delta = round(elo_delta(winner_r, loser_r, score))
        rc = await self.bot.user_db.complete_duel(
            duelid, win_status, finish_time, winner.id, loser.id, delta, dtype
        )
        if rc == 0:
            # Its other duelist ended it first, or it was invalidated.
            raise DuelCogError('This duel has already ended.')

        if dtype == DuelType.UNOFFICIAL:
            return None

        winner_cf = await self._get_cf_user(winner.id, guild_id)
        loser_cf = await self._get_cf_user(loser.id, guild_id)
        desc = (
            f'Rating change after **[{winner_cf.handle}]({winner_cf.url})**'
            f' vs **[{loser_cf.handle}]({loser_cf.url})**:'
        )
        embed = discord_common.cf_color_embed(description=desc)
        embed.add_field(
            name=f'{winner.display_name}',
            value=f'{winner_r} -> {winner_r + delta}',
            inline=False,
        )
        embed.add_field(
            name=f'{loser.display_name}',
            value=f'{loser_r} -> {loser_r - delta}',
            inline=False,
        )
        return embed

    @commands.hybrid_group(brief='Show the duel commands', fallback='show')
    async def duel(self, ctx: commands.Context) -> None:
        """Show the duel commands.

        In a duel, two members race to solve the same Codeforces problem, and
        the first to solve it wins. Register with /duel selfregister, or ask a
        moderator to register you if self-registration is switched off, then
        challenge someone with ;duel challenge.

        Examples:
            /duel show
            ;duel
        """
        await ctx.send_help(ctx.command)

    @duel.command(brief='Register a member as a duelist')
    @app_commands.describe(member='The member to register')
    async def register(self, ctx: commands.Context, member: discord.Member) -> None:
        """Register a member as a duelist, so that they can take part in duels.

        Examples:
            ;duel register @alice
        """
        rc = await self.bot.user_db.register_duelist(member.id)
        if rc == 0:
            raise DuelCogError(f'{member.mention} is already a registered duelist.')
        await ctx.send(f'{member.mention} successfully registered as a duelist.')

    @duel.command(brief='Register yourself as a duelist')
    @commands.check(check_if_allow_self_register)
    async def selfregister(self, ctx: commands.Context) -> None:
        """Register yourself as a duelist, so that you can take part in duels,
        if the bot allows it; otherwise ask a moderator to register you. Link
        your Codeforces account with /link codeforces first.

        Examples:
            /duel selfregister
        """
        if not await self.bot.user_db.get_handle(ctx.author.id, ctx.guild.id):
            raise DuelCogError(
                f'{ctx.author.mention}, link your Codeforces account with'
                ' `/link codeforces` before you register as a duelist.'
            )
        rc = await self.bot.user_db.register_duelist(ctx.author.id)
        if rc == 0:
            raise DuelCogError(f'{ctx.author.mention} is already a registered duelist.')
        await ctx.send(f'{ctx.author.mention} successfully registered as a duelist.')

    # The help quotes the constants above, so it is an f-string passed as
    # help=, not a docstring. A challenge asks Codeforces for both duelists'
    # submissions, so it has a cooldown, which a mistyped one doesn't spend.
    @duel.command(
        brief='Challenge a member to a duel',
        usage='<opponent> [rating] [+tag...] [~tag...]',
        with_app_command=False,
        cooldown_after_parsing=True,
        help=f"""Challenge a member to a duel on a Codeforces problem that neither
        of you has solved, rated about {-_DUEL_RATING_DELTA} below the lower of
        your Codeforces ratings unless you give a rating. A higher rating, or
        tags to include (+tag) or leave out (~tag), make the duel unofficial: it
        changes no duel ratings. The challenge expires after
        {cf_common.pretty_time_format(_DUEL_EXPIRY_TIME)}.

        Examples:
            ;duel challenge @alice
            ;duel challenge @alice 1600
            ;duel challenge @alice +greedy ~math
        """,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def challenge(
        self, ctx: commands.Context, opponent: discord.Member, *args: str
    ) -> None:
        challenger_id = ctx.author.id
        challengee_id = opponent.id

        await cf_common.resolve_handles(
            ctx, self.converter, ('!' + str(ctx.author), '!' + str(opponent))
        )
        userids = [challenger_id, challengee_id]
        handles = [
            await self.bot.user_db.get_handle(userid, ctx.guild.id)
            for userid in userids
        ]

        if not await self.bot.user_db.is_duelist(challenger_id):
            raise DuelCogError(
                f'{ctx.author.mention}, you are not a registered duelist!'
            )
        if not await self.bot.user_db.is_duelist(challengee_id):
            raise DuelCogError(f'{opponent.mention} is not a registered duelist!')
        if challenger_id == challengee_id:
            raise DuelCogError(f'{ctx.author.mention}, you cannot challenge yourself!')
        if await self.bot.user_db.check_duel_challenge(challenger_id):
            raise DuelCogError(f'{ctx.author.mention}, you are currently in a duel!')
        if await self.bot.user_db.check_duel_challenge(challengee_id):
            raise DuelCogError(f'{opponent.mention} is currently in a duel!')

        # Only now, so that a refused challenge asks Codeforces nothing.
        submissions = [await cf.user.status(handle=handle) for handle in handles]
        tags = cf_common.parse_tags(args, prefix='+')
        bantags = cf_common.parse_tags(args, prefix='~')
        rating = cf_common.parse_rating(args)
        users = [await self.bot.user_db.fetch_cf_user(handle) for handle in handles]
        lowest_rating = min(user.rating or 0 for user in users)
        suggested_rating = max(round(lowest_rating, -2) + _DUEL_RATING_DELTA, 500)
        rating = round(rating, -2) if rating else suggested_rating
        unofficial = rating > suggested_rating or tags or bantags
        dtype = DuelType.UNOFFICIAL if unofficial else DuelType.OFFICIAL

        solved = {
            sub.problem.name
            for subs in submissions
            for sub in subs
            if sub.verdict != 'COMPILATION_ERROR'
        }
        seen = {
            name
            for userid in userids
            for (name,) in await self.bot.user_db.get_duel_problem_names(userid)
        }

        def get_problems(rating: int) -> list[Any]:
            return [
                prob
                for prob in self.bot.cf_cache.problem_cache.problems
                if prob.rating == rating
                and prob.name not in solved
                and prob.name not in seen
                and not any(
                    cf_common.is_contest_writer(prob.contestId, handle)
                    for handle in handles
                )
                and not cf_common.is_nonstandard_problem(prob)
                and prob.matches_all_tags(tags)
                and not prob.matches_any_tag(bantags)
            ]

        for problems in map(get_problems, range(rating, 400, -100)):
            if problems:
                break

        rstr = f'{rating} rated ' if rating else ''
        if not problems:
            raise DuelCogError(
                f'No unsolved {rstr}problems left for'
                f' {ctx.author.mention} vs {opponent.mention}.'
            )

        problems.sort(
            key=lambda problem: (
                self.bot.cf_cache.contest_cache.get_contest(
                    problem.contestId
                ).startTimeSeconds
            )
        )

        choice = max(random.randrange(len(problems)) for _ in range(2))
        problem = problems[choice]

        issue_time = datetime.datetime.now().timestamp()
        duelid = await self.bot.user_db.create_duel(
            challenger_id, challengee_id, issue_time, problem, dtype
        )

        ostr = 'an **unofficial**' if unofficial else 'a'
        view = DuelChallengeView(
            self.bot,
            duelid,
            challenger_id,
            challengee_id,
            problem.name,
            timeout=_DUEL_EXPIRY_TIME,
        )
        view.message = await ctx.send(
            f'{ctx.author.mention} is challenging'
            f' {opponent.mention} to {ostr} {rstr}duel!\n\u200b',
            view=view,
        )

    @duel.command(brief='Decline a challenge to a duel')
    async def decline(self, ctx: commands.Context) -> None:
        """Decline the challenge to a duel that you received. The Decline button
        under the challenge does the same.

        Examples:
            /duel decline
        """
        active = await self.bot.user_db.check_duel_decline(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not being challenged!')

        duelid, challenger = active
        challenger = ctx.guild.get_member(challenger)
        await self.bot.user_db.cancel_duel(duelid, Duel.DECLINED)
        message = f'{ctx.author.mention} declined a challenge by {challenger.mention}.'
        embed = discord_common.embed_alert(message)
        await ctx.send(embed=embed)

    @duel.command(brief='Withdraw your challenge to a duel')
    async def withdraw(self, ctx: commands.Context) -> None:
        """Withdraw your challenge to a duel before it is accepted. The Withdraw
        button under the challenge does the same.

        Examples:
            /duel withdraw
        """
        active = await self.bot.user_db.check_duel_withdraw(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not challenging anyone.')

        duelid, challengee = active
        challengee = ctx.guild.get_member(challengee)
        await self.bot.user_db.cancel_duel(duelid, Duel.WITHDRAWN)
        message = f'{ctx.author.mention} withdrew a challenge to {challengee.mention}.'
        embed = discord_common.embed_alert(message)
        await ctx.send(embed=embed)

    @duel.command(brief='Accept a challenge to a duel')
    async def accept(self, ctx: commands.Context) -> None:
        """Accept the challenge to a duel that you received. The duel and its
        problem start 15 seconds later. The Accept button under the challenge
        does the same.

        Examples:
            /duel accept
        """
        active = await self.bot.user_db.check_duel_accept(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not being challenged.')

        duelid, challenger_id, name = active
        challenger = ctx.guild.get_member(challenger_id)
        await ctx.send(
            f'Duel between {challenger.mention} and'
            f' {ctx.author.mention} starting in 15 seconds!'
        )
        await asyncio.sleep(15)

        start_time = datetime.datetime.now().timestamp()
        rc = await self.bot.user_db.start_duel(duelid, start_time)
        if rc != 1:
            raise DuelCogError(
                f'Unable to start the duel between {challenger.mention}'
                f' and {ctx.author.mention}.'
            )

        problem = self.bot.cf_cache.problem_cache.problem_by_name[name]
        title = f'{problem.index}. {problem.name}'
        desc = self.bot.cf_cache.contest_cache.get_contest(problem.contestId).name
        embed = discord.Embed(title=title, url=problem.url, description=desc)
        embed.add_field(name='Rating', value=problem.rating)
        await ctx.send(
            f'Starting duel: {challenger.mention} vs {ctx.author.mention}', embed=embed
        )

    @duel.command(brief='End your duel once one of you has solved its problem')
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def complete(self, ctx: commands.Context) -> None:
        """End your duel once one of you has solved its problem on Codeforces:
        the first to solve it wins. An official duel changes both duelists'
        duel ratings.

        Examples:
            /duel complete
        """
        active = await self.bot.user_db.check_duel_complete(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not in a duel.')

        (
            duelid,
            challenger_id,
            challengee_id,
            start_time,
            problem_name,
            contest_id,
            index,
            dtype,
        ) = active

        UNSOLVED = 0
        TESTING = -1

        async def get_solve_time(userid: int) -> int:
            handle = await self.bot.user_db.get_handle(userid, ctx.guild.id)
            subs = [
                sub
                for sub in await cf.user.status(handle=handle)
                if (sub.verdict == 'OK' or sub.verdict == 'TESTING')
                and sub.problem.contestId == contest_id
                and sub.problem.index == index
            ]

            if not subs:
                return UNSOLVED
            if 'TESTING' in [sub.verdict for sub in subs]:
                return TESTING
            return min(
                subs, key=lambda sub: sub.creationTimeSeconds
            ).creationTimeSeconds

        # Codeforces can take longer to answer than the 3 seconds a slash
        # command has, so the answer is deferred first.
        async with ctx.typing():
            challenger_time = await get_solve_time(challenger_id)
            challengee_time = await get_solve_time(challengee_id)

        if challenger_time == TESTING or challengee_time == TESTING:
            await ctx.send(
                f'Wait a bit, {ctx.author.mention}. A submission is still being judged.'
            )
            return

        challenger = ctx.guild.get_member(challenger_id)
        challengee = ctx.guild.get_member(challengee_id)

        if challenger_time and challengee_time:
            if challenger_time != challengee_time:
                diff = cf_common.pretty_time_format(
                    abs(challengee_time - challenger_time), always_seconds=True
                )
                winner = challenger if challenger_time < challengee_time else challengee
                loser = challenger if challenger_time > challengee_time else challengee
                win_status = (
                    Winner.CHALLENGER if winner == challenger else Winner.CHALLENGEE
                )
                embed = await self._complete_duel(
                    duelid,
                    ctx.guild.id,
                    win_status,
                    winner,
                    loser,
                    min(challenger_time, challengee_time),
                    1,
                    dtype,
                )
                await ctx.send(
                    f'Both {challenger.mention} and {challengee.mention}'
                    f' solved it but {winner.mention} was {diff} faster!',
                    embed=embed,
                )
            else:
                embed = await self._complete_duel(
                    duelid,
                    ctx.guild.id,
                    Winner.DRAW,
                    challenger,
                    challengee,
                    challenger_time,
                    0.5,
                    dtype,
                )
                await ctx.send(
                    f'{challenger.mention} and {challengee.mention} solved the problem'
                    " in the exact same amount of time! It's a draw!",
                    embed=embed,
                )

        elif challenger_time:
            embed = await self._complete_duel(
                duelid,
                ctx.guild.id,
                Winner.CHALLENGER,
                challenger,
                challengee,
                challenger_time,
                1,
                dtype,
            )
            await ctx.send(
                f'{challenger.mention} beat {challengee.mention} in a duel!',
                embed=embed,
            )
        elif challengee_time:
            embed = await self._complete_duel(
                duelid,
                ctx.guild.id,
                Winner.CHALLENGEE,
                challengee,
                challenger,
                challengee_time,
                1,
                dtype,
            )
            await ctx.send(
                f'{challengee.mention} beat {challenger.mention} in a duel!',
                embed=embed,
            )
        else:
            await ctx.send('Nobody solved the problem yet.')

    # The help quotes a constant above, as challenge's does.
    @duel.command(
        brief='Offer a draw, or accept the draw offered to you',
        help=f"""Offer your opponent a draw, or accept the draw they offered. You
        can offer one once the duel has lasted
        {cf_common.pretty_time_format(_DUEL_NO_DRAW_TIME)}.

        Examples:
            /duel draw
        """,
    )
    async def draw(self, ctx: commands.Context) -> None:
        active = await self.bot.user_db.check_duel_draw(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not in a duel.')

        duelid, challenger_id, challengee_id, start_time, dtype = active
        now = datetime.datetime.now().timestamp()
        if now - start_time < _DUEL_NO_DRAW_TIME:
            draw_time = cf_common.pretty_time_format(
                start_time + _DUEL_NO_DRAW_TIME - now
            )
            await ctx.send(
                f'Think more {ctx.author.mention}. You can offer a draw in {draw_time}.'
            )
            return

        if duelid not in self.draw_offers:
            self.draw_offers[duelid] = ctx.author.id
            offeree_id = (
                challenger_id if ctx.author.id != challenger_id else challengee_id
            )
            offeree = ctx.guild.get_member(offeree_id)
            await ctx.send(
                f'{ctx.author.mention} is offering a draw to {offeree.mention}!'
            )
            return

        if self.draw_offers[duelid] == ctx.author.id:
            await ctx.send(f"{ctx.author.mention}, you've already offered a draw.")
            return

        offerer = ctx.guild.get_member(self.draw_offers[duelid])
        embed = await self._complete_duel(
            duelid, ctx.guild.id, Winner.DRAW, offerer, ctx.author, now, 0.5, dtype
        )
        await ctx.send(
            f'{ctx.author.mention} accepted draw offer by {offerer.mention}.',
            embed=embed,
        )

    @duel.command(brief="Show a duelist's duel rating and record")
    @app_commands.describe(member='The duelist whose profile to show; you if left out')
    async def profile(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show a duelist's duel rating, their wins, losses and draws, and their
        fastest and slowest wins.

        Examples:
            /duel profile
            /duel profile member:@alice
        """
        assert isinstance(ctx.author, discord.Member)
        member = member or ctx.author
        if not await self.bot.user_db.is_duelist(member.id):
            raise DuelCogError(f'{member.mention} is not a registered duelist.')

        user = await self._get_cf_user(member.id, ctx.guild.id)
        rating = await self.bot.user_db.get_duel_rating(member.id)
        rank = rating2rank(rating)
        rank_title = rank.title if rank else 'Unknown'
        rank_color = rank.color_embed if rank else None
        desc = (
            f'Duelist profile of {rank_title} {member.mention}'
            f' aka **[{user.handle}]({user.url})**'
        )
        embed = discord.Embed(description=desc, color=rank_color)
        embed.add_field(name='Rating', value=rating, inline=True)

        wins = await self.bot.user_db.get_duel_wins(member.id)
        num_wins = len(wins)
        embed.add_field(name='Wins', value=num_wins, inline=True)
        num_losses = await self.bot.user_db.get_num_duel_losses(member.id)
        embed.add_field(name='Losses', value=num_losses, inline=True)
        num_draws = await self.bot.user_db.get_num_duel_draws(member.id)
        embed.add_field(name='Draws', value=num_draws, inline=True)
        num_declined = await self.bot.user_db.get_num_duel_declined(member.id)
        embed.add_field(name='Declined', value=num_declined, inline=True)
        num_rdeclined = await self.bot.user_db.get_num_duel_rdeclined(member.id)
        embed.add_field(name='Got declined', value=num_rdeclined, inline=True)

        async def duel_to_string(duel: tuple[Any, ...]) -> str:
            start_time, finish_time, problem_name, challenger, challengee = duel
            duel_time = cf_common.pretty_time_format(
                finish_time - start_time, shorten=True, always_seconds=True
            )
            when = cf_common.days_ago(start_time)
            loser_id = challenger if member.id != challenger else challengee
            loser = await self._get_cf_user(loser_id, ctx.guild.id)
            problem = self.bot.cf_cache.problem_cache.problem_by_name[problem_name]
            return (
                f'**[{problem.name}]({problem.url})** [{problem.rating}]'
                f' versus [{loser.handle}]({loser.url}) {when} in {duel_time}'
            )

        if wins:
            # sort by finish_time - start_time
            wins.sort(key=lambda duel: duel[1] - duel[0])
            embed.add_field(
                name='Fastest win', value=await duel_to_string(wins[0]), inline=False
            )
            embed.add_field(
                name='Slowest win', value=await duel_to_string(wins[-1]), inline=False
            )

        embed.set_thumbnail(url=f'{user.titlePhoto}')
        await ctx.send(embed=embed)

    async def _paginate_duels(
        self, data: list[Any], message: str, guild_id: int, show_id: bool
    ) -> list[tuple[str, discord.Embed]]:
        async def make_line(entry: Any) -> str:
            duelid, start_time, finish_time, name, challenger, challengee, winner = (
                entry
            )
            duel_time = cf_common.pretty_time_format(
                finish_time - start_time, shorten=True, always_seconds=True
            )
            problem = self.bot.cf_cache.problem_cache.problem_by_name[name]
            when = cf_common.days_ago(start_time)
            idstr = f'{duelid}: '
            if winner != Winner.DRAW:
                loser = await self._get_cf_user(
                    challenger if winner == Winner.CHALLENGEE else challengee, guild_id
                )
                winner = await self._get_cf_user(
                    challenger if winner == Winner.CHALLENGER else challengee, guild_id
                )
                return (
                    f'{idstr if show_id else str()}[{name}]({problem.url})'
                    f' [{problem.rating}] won by [{winner.handle}]({winner.url})'
                    f' vs [{loser.handle}]({loser.url}) {when} in {duel_time}'
                )
            else:
                challenger = await self._get_cf_user(challenger, guild_id)
                challengee = await self._get_cf_user(challengee, guild_id)
                return (
                    f'{idstr if show_id else str()}[{name}]({problem.url})'
                    f' [{problem.rating}] drawn by'
                    f' [{challenger.handle}]({challenger.url}) and'
                    f' [{challengee.handle}]({challengee.url}) {when} after {duel_time}'
                )

        async def make_page(chunk: Sequence[Any]) -> tuple[str, discord.Embed]:
            lines = [await make_line(entry) for entry in chunk]
            log_str = '\n'.join(lines)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        if not data:
            raise DuelCogError('There are no duels to show.')

        return [await make_page(chunk) for chunk in paginator.chunkify(data, 7)]

    @duel.command(brief='Show the duels between two members', aliases=['versushistory'])
    @app_commands.describe(
        member1='One of the two members; name at least this one',
        member2='The other member; you if left out',
    )
    async def vshistory(
        self,
        ctx: commands.Context,
        member1: discord.Member | None = None,
        member2: discord.Member | None = None,
    ) -> None:
        """Show the duels finished between two members, and how many each won.
        Name one member to see their duels with you.

        Examples:
            /duel vshistory member1:@alice
            /duel vshistory member1:@alice member2:@bob
        """
        if not member1:
            raise DuelCogError(
                'Name a member to see your duels with them, or two members.'
            )

        assert isinstance(ctx.author, discord.Member)
        member2 = member2 or ctx.author
        data = await self.bot.user_db.get_pair_duels(member1.id, member2.id)
        wins, losses, draws = 0, 0, 0
        for _, _, _, _, challenger, challengee, winner in data:
            if winner != Winner.DRAW:
                winnerid = challenger if winner == Winner.CHALLENGER else challengee
                if winnerid == member1.id:
                    wins += 1
                else:
                    losses += 1
            else:
                draws += 1
        message = discord.utils.escape_mentions(
            f'`{member1.display_name}` ({wins}/{draws}/{losses})'
            f' `{member2.display_name}`'
        )
        pages = await self._paginate_duels(data, message, ctx.guild.id, False)
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @duel.command(brief="Show a member's finished duels")
    @app_commands.describe(member='The member whose duels to show; you if left out')
    async def history(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show a member's finished duels, the newest first.

        Examples:
            /duel history
            /duel history member:@alice
        """
        assert isinstance(ctx.author, discord.Member)
        member = member or ctx.author
        data = await self.bot.user_db.get_duels(member.id)
        message = discord.utils.escape_mentions(
            f'Finished duels of `{member.display_name}`'
        )
        pages = await self._paginate_duels(data, message, ctx.guild.id, False)
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    async def _recent_duels(self, guild: discord.Guild) -> list[Any]:
        """The latest finished duels between members of ``guild``, at most
        _RECENT_DUELS of them, the newest first, as the duels table orders
        them: by start time.

        Each member's duels are read, rather than the latest duels of every
        server, so that other servers' duels can't crowd this server's out.
        """
        found: dict[int, Any] = {}
        for user_id, _rating in await self.bot.user_db.get_duelists():
            if guild.get_member(user_id) is None:
                continue
            for entry in await self.bot.user_db.get_duels(user_id):
                duelid, _, _, _, challenger, challengee, _ = entry
                if _are_members(guild, challenger, challengee):
                    found[duelid] = entry
        newest = sorted(
            found.values(), key=lambda entry: (entry[1], entry[0]), reverse=True
        )
        return newest[:_RECENT_DUELS]

    @duel.command(brief="Show this server's most recent duels")
    async def recent(self, ctx: commands.Context) -> None:
        """Show the latest finished duels between members of this server.

        Examples:
            /duel recent
        """
        data = await self._recent_duels(ctx.guild)
        pages = await self._paginate_duels(
            data, 'Recent duels in this server', ctx.guild.id, True
        )
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @duel.command(brief='Show the duels going on in this server')
    @app_commands.describe(
        member='The member whose duel to show; every duel in this server if left out'
    )
    async def ongoing(
        self, ctx: commands.Context, member: discord.Member | None = None
    ) -> None:
        """Show the duels going on between members of this server, or only a
        given member's duel.

        Examples:
            /duel ongoing
            /duel ongoing member:@alice
        """

        async def make_line(entry: Any) -> str:
            start_time, name, challenger, challengee = entry
            problem = self.bot.cf_cache.problem_cache.problem_by_name[name]
            now = datetime.datetime.now().timestamp()
            when = cf_common.pretty_time_format(
                now - start_time, shorten=True, always_seconds=True
            )
            challenger = await self._get_cf_user(challenger, ctx.guild.id)
            challengee = await self._get_cf_user(challengee, ctx.guild.id)
            return (
                f'[{challenger.handle}]({challenger.url})'
                f' vs [{challengee.handle}]({challengee.url}):'
                f' [{name}]({problem.url}) [{problem.rating}] {when}'
            )

        async def make_page(chunk: Sequence[Any]) -> tuple[str, discord.Embed]:
            message = 'Ongoing duels in this server'
            lines = [await make_line(entry) for entry in chunk]
            log_str = '\n'.join(lines)
            embed = discord_common.cf_color_embed(description=log_str)
            return message, embed

        def shown(entry: Any) -> bool:
            _start_time, _name, challenger, challengee = entry
            if not _are_members(ctx.guild, challenger, challengee):
                return False
            return member is None or member.id in (challenger, challengee)

        duels = await self.bot.user_db.get_ongoing_duels()
        data = [entry for entry in duels if shown(entry)]
        if not data:
            if member is not None:
                raise DuelCogError(
                    f'{member.mention} has no ongoing duel in this server.'
                )
            raise DuelCogError('There are no ongoing duels in this server.')

        pages = [await make_page(chunk) for chunk in paginator.chunkify(data, 7)]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    @duel.command(brief="Rank this server's duelists by duel rating")
    async def ranklist(self, ctx: commands.Context) -> None:
        """Rank this server's duelists by duel rating. Only those who have
        finished a duel are listed.

        Examples:
            /duel ranklist
        """
        user_pairs = [
            (ctx.guild.get_member(user_id), rating)
            for user_id, rating in await self.bot.user_db.get_duelists()
        ]
        users = [
            (
                member,
                await self.bot.user_db.get_handle(member.id, ctx.guild.id),
                rating,
            )
            for member, rating in user_pairs
            if member is not None
            and await self.bot.user_db.get_num_duel_completed(member.id) > 0
        ]

        _PER_PAGE = 10

        def make_page(chunk: Sequence[Any], page_num: int) -> tuple[str, discord.Embed]:
            style = table.Style('{:>}  {:<}  {:<}  {:<}')
            t = table.Table(style)
            t += table.Header('#', 'Name', 'Handle', 'Rating')
            t += table.Line()
            for index, (member, handle, rating) in enumerate(chunk):
                duel_rank = rating2rank(rating)
                rating_str = f'{rating} ({duel_rank.title_abbr if duel_rank else "?"})'
                t += table.Data(
                    _PER_PAGE * page_num + index,
                    f'{member.display_name}',
                    handle,
                    rating_str,
                )

            table_str = f'```\n{t}\n```'
            embed = discord_common.cf_color_embed(description=table_str)
            return 'List of duelists', embed

        if not users:
            raise DuelCogError('There are no active duelists.')

        pages = [
            make_page(chunk, k)
            for k, chunk in enumerate(paginator.chunkify(users, _PER_PAGE))
        ]
        await paginator.paginate(
            ctx.channel,
            pages,
            wait_time=5 * 60,
            set_pagenum_footers=True,
            ctx=ctx,
        )

    async def invalidate_duel(
        self, ctx: commands.Context, duelid: int, challenger_id: int, challengee_id: int
    ) -> None:
        rc = await self.bot.user_db.invalidate_duel(duelid)
        if rc == 0:
            raise DuelCogError(f'Unable to invalidate duel {duelid}.')

        challenger = ctx.guild.get_member(challenger_id)
        challengee = ctx.guild.get_member(challengee_id)
        await ctx.send(
            f'Duel between {challenger.mention} and'
            f' {challengee.mention} has been invalidated.'
        )

    # The help quotes a constant above, as challenge's does.
    @duel.command(
        brief='Invalidate your duel if you had solved its problem before it began',
        help=f"""Invalidate your duel if you had solved its problem before the
        duel began. An invalid duel has no winner and changes no ratings. You
        can invalidate a duel only in its first
        {cf_common.pretty_time_format(_DUEL_INVALIDATE_TIME)}.

        Examples:
            /duel invalidate
        """,
    )
    async def invalidate(self, ctx: commands.Context) -> None:
        active = await self.bot.user_db.check_duel_complete(ctx.author.id)
        if not active:
            raise DuelCogError(f'{ctx.author.mention}, you are not in a duel.')

        duelid, challenger_id, challengee_id, start_time, _, _, _, _ = active
        if datetime.datetime.now().timestamp() - start_time > _DUEL_INVALIDATE_TIME:
            window = cf_common.pretty_time_format(_DUEL_INVALIDATE_TIME)
            raise DuelCogError(
                f'{ctx.author.mention}, you can invalidate a duel only in its'
                f' first {window}.'
            )
        await self.invalidate_duel(ctx, duelid, challenger_id, challengee_id)

    @duel.command(brief="Invalidate a member's ongoing duel")
    @app_commands.describe(member='A member in the duel to invalidate')
    async def _invalidate(self, ctx: commands.Context, member: discord.Member) -> None:
        """Invalidate a member's ongoing duel: it ends with no winner and
        changes no ratings.

        Examples:
            ;duel _invalidate @alice
        """
        active = await self.bot.user_db.check_duel_complete(member.id)
        if not active:
            raise DuelCogError(f'{member.mention} is not in a duel.')

        duelid, challenger_id, challengee_id, _, _, _, _, _ = active
        await self.invalidate_duel(ctx, duelid, challenger_id, challengee_id)

    # Plotting is slow, so it has a cooldown, which a mistyped member doesn't
    # spend.
    @duel.command(
        brief='Plot the duel ratings of up to 5 members',
        with_app_command=False,
        cooldown_after_parsing=True,
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def rating(self, ctx: commands.Context, *members: discord.Member) -> None:
        """Plot how the duel ratings of up to 5 members changed over their
        official duels; yours if you name nobody.

        Examples:
            ;duel rating
            ;duel rating @alice @bob
        """
        assert isinstance(ctx.author, discord.Member)
        members = members or (ctx.author,)
        if len(members) > 5:
            raise DuelCogError('Cannot plot more than 5 duelists at once.')

        duelists = [member.id for member in members]
        duels = await self.bot.user_db.get_complete_official_duels()
        rating: dict[int, int] = {}
        plot_data = defaultdict(list)
        time_tick = 0
        for challenger, challengee, winner, _finish_time in duels:
            challenger_r = rating.get(challenger, 1500)
            challengee_r = rating.get(challengee, 1500)
            if winner == Winner.CHALLENGER:
                delta = round(elo_delta(challenger_r, challengee_r, 1))
            elif winner == Winner.CHALLENGEE:
                delta = round(elo_delta(challenger_r, challengee_r, 0))
            else:
                delta = round(elo_delta(challenger_r, challengee_r, 0.5))

            rating[challenger] = challenger_r + delta
            rating[challengee] = challengee_r - delta
            if challenger in duelists or challengee in duelists:
                if challenger in duelists:
                    plot_data[challenger].append((time_tick, rating[challenger]))
                if challengee in duelists:
                    plot_data[challengee].append((time_tick, rating[challengee]))
                time_tick += 1

        if time_tick == 0:
            raise DuelCogError('Nothing to plot.')

        plt.clf()
        # plot at least from mid gray to mid purple
        min_rating = 1350
        max_rating = 1550
        for rating_data in plot_data.values():
            for _tick, r in rating_data:
                min_rating = min(min_rating, r)
                max_rating = max(max_rating, r)

            x, y = zip(*rating_data, strict=False)
            plt.plot(
                x,
                y,
                linestyle='-',
                marker='o',
                markersize=2,
                markerfacecolor='white',
                markeredgewidth=0.5,
            )

        gc.plot_rating_bg(cast(Sequence[cf.Rank], DUEL_RANKS))
        plt.xlim(0, time_tick - 1)
        plt.ylim(min_rating - 100, max_rating + 100)

        labels = [
            gc.StrWrap(
                '{} ({})'.format(
                    ctx.guild.get_member(duelist).display_name, rating_data[-1][1]
                )
            )
            for duelist, rating_data in plot_data.items()
        ]
        plt.legend(labels, loc='upper left', prop=gc.fontprop)

        discord_file = gc.get_current_figure_as_file()
        embed = discord_common.cf_color_embed(title='Duel rating graph')
        discord_common.attach_image(embed, discord_file)
        discord_common.set_author_footer(embed, ctx.author)
        await ctx.send(embed=embed, file=discord_file)

    @discord_common.send_error_if(DuelCogError, cf_common.ResolveHandleError)
    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Dueling(bot))
