import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any

from discord import app_commands
from discord.ext import commands

# The replies when the contest given isn't one that the commands can fetch.
NOT_A_RATING_CHANGES_MODE = 'Give a contest ID, `all` or `missing`.'
NOT_A_PROBLEMSETS_MODE = 'Give a contest ID or `all`.'
UNKNOWN_CONTEST_TEXT = (
    "No contest in the bot's Codeforces contest list has that ID. If the "
    'contest is new, reload the list with `;cache contests` first.'
)


def timed_command(
    coro: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., Coroutine[Any, Any, None]]:
    @functools.wraps(coro)
    async def wrapper(cog: commands.Cog, ctx: commands.Context, *args: Any) -> None:
        await ctx.send('Running...')
        begin = time.time()
        await coro(cog, ctx, *args)
        elapsed = time.time() - begin
        await ctx.send(f'Completed in {elapsed:.2f} seconds')

    return wrapper


class CacheControl(commands.Cog):
    """Commands that update the bot's Codeforces caches now, for the bot owner.

    Every server shares the caches.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _contest_id(self, text: str, not_a_number: str) -> int:
        """The id of the contest in the bot's contest list that ``text`` names.

        ``BadArgument`` with ``not_a_number`` if ``text`` isn't a number, and
        with ``UNKNOWN_CONTEST_TEXT`` if no contest in the list has that id.
        """
        try:
            contest_id = int(text)
        except ValueError:
            raise commands.BadArgument(not_a_number)
        if contest_id not in self.bot.cf_cache.contest_cache.contest_by_id:
            raise commands.BadArgument(UNKNOWN_CONTEST_TEXT)
        return contest_id

    @commands.hybrid_group(brief='Show the Codeforces cache commands', fallback='show')
    async def cache(self, ctx: commands.Context) -> None:
        """Update the bot's copies of Codeforces data now, instead of at their
        next scheduled update. Every server shares them.
        """
        await ctx.send_help('cache')

    @cache.command(brief='Reload the Codeforces contest list')
    @timed_command
    async def contests(self, ctx: commands.Context) -> None:
        """Reload the list of Codeforces contests now. The bot otherwise reloads
        it every 30 minutes, and more often around contests.
        """
        await self.bot.cf_cache.contest_cache.reload_now()

    @cache.command(brief='Reload the Codeforces problem list')
    @timed_command
    async def problems(self, ctx: commands.Context) -> None:
        """Reload the list of Codeforces problems now, with their ratings and
        tags. The bot otherwise reloads it every 6 hours.
        """
        await self.bot.cf_cache.problem_cache.reload_now()

    @cache.command(brief='Fetch the rating changes of Codeforces contests')
    @app_commands.describe(
        contest_id='A contest ID, all, or missing for the contests not fetched yet '
        '(the default)'
    )
    @timed_command
    async def ratingchanges(
        self, ctx: commands.Context, contest_id: str = 'missing'
    ) -> None:
        """Fetch the rating changes of finished Codeforces contests, then update
        the ratings the bot keeps for handles. By default, or with `missing`,
        it fetches the contests not fetched yet. `all` fetches every contest
        again, and a contest ID fetches that contest again.

        Examples:
            ;cache ratingchanges
            ;cache ratingchanges all
            ;cache ratingchanges 1950
        """
        if contest_id not in ('all', 'missing'):
            contest_id_int = self._contest_id(contest_id, NOT_A_RATING_CHANGES_MODE)
            count = await self.bot.cf_cache.rating_changes_cache.fetch_contest(
                contest_id_int
            )
        elif contest_id == 'all':
            await ctx.send('This will take a while')
            count = await self.bot.cf_cache.rating_changes_cache.fetch_all_contests()
        else:
            await ctx.send('This may take a while')
            count = (
                await self.bot.cf_cache.rating_changes_cache.fetch_missing_contests()
            )
        await ctx.send(f'Done, fetched {count} changes and recached handle ratings')

    @cache.command(brief='Fetch the problems of Codeforces contests')
    @app_commands.describe(contest_id='A contest ID, or all for every finished contest')
    @timed_command
    async def problemsets(self, ctx: commands.Context, contest_id: str) -> None:
        """Fetch the problems of one Codeforces contest again, or of every
        finished contest with `all`. The bot itself fetches those of contests
        that ended in the last 14 days, every hour.

        Examples:
            ;cache problemsets 1950
            ;cache problemsets all
        """
        if contest_id == 'all':
            await ctx.send('This will take a while')
            count = await self.bot.cf_cache.problemset_cache.update_for_all()
        else:
            contest_id_int = self._contest_id(contest_id, NOT_A_PROBLEMSETS_MODE)
            count = await self.bot.cf_cache.problemset_cache.update_for_contest(
                contest_id_int
            )
        await ctx.send(f'Done, fetched {count} problems')


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CacheControl(bot))
