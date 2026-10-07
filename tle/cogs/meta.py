import asyncio
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterable

import discord
from discord.ext import commands

from tle.util.codeforces_common import pretty_time_format

# The replies of meta git and meta guilds, besides what they show.
GIT_FAILED_TEXT = "Couldn't read the bot's git history."
GUILDS_SENT_TEXT = 'I sent you the list in a direct message.'
NO_DM_TEXT = "I couldn't DM you."
# How long the reply that a direct message failed stays, in seconds.
NO_DM_DELETE_AFTER = 20.0
# How long each git command may take, in seconds.
GIT_TIMEOUT = 10
# Discord's limit on the length of a message.
MESSAGE_LIMIT = 2000


# Adapted from numpy sources.
# https://github.com/numpy/numpy/blob/master/setup.py#L64-85
def git_history() -> str | None:
    """The git branch and the last five commits of the bot's code, or None if
    git can't tell.

    It waits for git, so the bot calls it in a worker thread.
    """

    def _minimal_ext_cmd(cmd: list[str]) -> bytes:
        # construct minimal environment
        env = {}
        for k in ['SYSTEMROOT', 'PATH']:
            v = os.environ.get(k)
            if v is not None:
                env[k] = v
        # LANGUAGE is used on win32
        env['LANGUAGE'] = 'C'
        env['LANG'] = 'C'
        env['LC_ALL'] = 'C'
        # On a timeout, run() kills git before it raises.
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, env=env, timeout=GIT_TIMEOUT, check=True
        )
        return proc.stdout

    try:
        out = _minimal_ext_cmd(['git', 'rev-parse', '--abbrev-ref', 'HEAD'])
        # Branch names and commit messages can hold any character.
        branch = out.strip().decode('utf-8', errors='replace')
        out = _minimal_ext_cmd(['git', 'log', '--oneline', '-5'])
        history = out.strip().decode('utf-8', errors='replace')
        return (
            'Branch:\n'
            + textwrap.indent(branch, '  ')
            + '\nCommits:\n'
            + textwrap.indent(history, '  ')
        )
    except (OSError, subprocess.SubprocessError):
        # No git or no repository where the bot runs, git failed, or it took
        # too long.
        return None


def _code_blocks(lines: Iterable[str]) -> list[str]:
    """``lines`` in code blocks, as few as Discord's limit on a message allows;
    a line too long for a block of its own is cut short.
    """
    room = MESSAGE_LIMIT - len('```\n\n```')
    blocks: list[list[str]] = []
    size = 0
    for line in lines:
        fitted = line[:room]
        if blocks and size + 1 + len(fitted) <= room:
            blocks[-1].append(fitted)
            size += 1 + len(fitted)
        else:
            blocks.append([fitted])
            size = len(fitted)
    return ['```\n' + '\n'.join(block) + '\n```' for block in blocks]


class Meta(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.start_time = time.time()

    @commands.hybrid_group(brief='Show the bot status commands', fallback='show')
    async def meta(self, ctx: commands.Context) -> None:
        """Check how the bot is doing, such as whether it is up and how long it
        has been running.
        """
        await ctx.send_help(ctx.command)

    @meta.command(brief='Shut the bot down')
    async def kill(self, ctx: commands.Context) -> None:
        """Shut the bot down, in every server. Under Docker Compose it then
        starts again, so this restarts it. To stop it for good, use
        `docker compose stop`.
        """
        await ctx.send('Shutting down...')
        await self.bot.close()
        sys.exit(0)

    @meta.command(brief='Check that the bot is up, and how fast it answers')
    async def ping(self, ctx: commands.Context) -> None:
        """Check that the bot is up. Its answer shows two delays in
        milliseconds: how long sending a message took, and the delay of the
        bot's live connection to Discord.
        """
        start = time.perf_counter()
        message = await ctx.send(':ping_pong: Pong!')
        end = time.perf_counter()
        duration = (end - start) * 1000
        await message.edit(
            content=(
                f'REST API latency: {int(duration)}ms\n'
                f'Gateway API latency: {int(self.bot.latency * 1000)}ms'
            )
        )

    @meta.command(brief="Show the bot's git branch and latest commits")
    async def git(self, ctx: commands.Context) -> None:
        """Show which version of the bot's code is running: its git branch and
        its last five commits. It needs git and the bot's repository where the
        bot runs.
        """
        # git can take seconds, which on the event loop would hold up every
        # other command.
        history = await asyncio.to_thread(git_history)
        if history is None:
            await ctx.send(GIT_FAILED_TEXT)
            return
        await ctx.send(f'```yaml\n{history}```')

    @meta.command(brief='Show how long the bot has been running')
    async def uptime(self, ctx: commands.Context) -> None:
        """Show how long the bot has been running since it last started."""
        running = pretty_time_format(time.time() - self.start_time)
        await ctx.send(f'The bot has been running for {running}.')

    @meta.command(brief="Send you a direct message listing the bot's servers")
    async def guilds(self, ctx: commands.Context) -> None:
        """Send you a direct message listing every server the bot is in. The
        list never goes to a channel: if the bot can't message you, you get
        only a short note, deleted after 20 seconds.
        """
        lines = [
            ' | '.join(
                [
                    f'Guild ID: {guild.id}',
                    f'Name: {guild.name}',
                    # The owner's id, as the owner may not be cached.
                    f'Owner: {guild.owner_id}',
                    f'Icon: {guild.icon.url if guild.icon else None}',
                ]
            )
            for guild in self.bot.guilds
        ]
        try:
            for block in _code_blocks(lines):
                await ctx.author.send(block)
        except discord.Forbidden:
            # Never the list itself: it is for the bot owner alone.
            await ctx.send(NO_DM_TEXT, ephemeral=True, delete_after=NO_DM_DELETE_AFTER)
            return
        if ctx.interaction is not None:
            # A slash command must be answered.
            await ctx.send(GUILDS_SENT_TEXT, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
