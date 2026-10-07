"""Tests for /handle show: the handles a member has linked, TLE's and KCPC's.

The Handles cog reads Codeforces handles from a real in-memory user database
(the ``user_db`` fixture). With KCPC running, it also lists the accounts linked
with /link, from a real in-memory kcpc.db. Discord is mocked.
"""

import logging
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from tle.kcpc.core.clock import UTC
from tle.kcpc.core.db import Database
from tle.kcpc.core.migrations import open_database
from tle.kcpc.features.accounts import directory
from tle.kcpc.features.accounts.repo import AccountRepo, LinkedAccount
from tle.util.db.user_db_conn import UserDbConn

GUILD_ID = 1_100_000_000_000_000_001
MEMBER_ID = 1_200_000_000_000_000_001
OTHER_MEMBER_ID = 1_200_000_000_000_000_002
CODEFORCES_LINE = (
    r'**Codeforces:** [Fake\_Coder](https://codeforces.com/profile/Fake_Coder)'
)
ATCODER_LINE = '**AtCoder:** [FakeAtCoder](https://atcoder.jp/users/FakeAtCoder)'


@pytest.fixture
def handles() -> ModuleType:
    """tle.cogs.handles, which draws with cairo and Pango through gi.

    Docker and CI have them; a bare virtualenv may not, so these tests skip there.
    """
    pytest.importorskip('gi')
    from tle.cogs import handles

    return handles


@pytest.fixture
async def kcpc_db() -> AsyncIterator[Database]:
    db = await open_database(':memory:')
    yield db
    await db.close()


def make_member(user_id: int, name: str) -> MagicMock:
    member = MagicMock(spec=discord.Member, id=user_id)
    member.mention = f'<@{user_id}>'
    member.display_name = name
    return member


@pytest.fixture
def member() -> MagicMock:
    return make_member(MEMBER_ID, 'Fake *Member*')


@pytest.fixture
def bot(user_db: UserDbConn) -> MagicMock:
    """TLE's bot, without KCPC's services until a test adds them."""
    bot = MagicMock(spec=commands.Bot)
    bot.user_db = user_db
    return bot


@pytest.fixture
def cog(handles: ModuleType, bot: MagicMock) -> Any:
    return handles.Handles(bot)


@pytest.fixture
def ctx(member: MagicMock) -> MagicMock:
    """The context of a command ``member`` runs; replies are recorded."""
    ctx = MagicMock(spec=commands.Context)
    ctx.guild = MagicMock(spec=discord.Guild, id=GUILD_ID)
    ctx.author = member
    ctx.send = AsyncMock()
    return ctx


async def show(cog: Any, ctx: MagicMock, *args: object) -> discord.Embed:
    """Run /handle show (or ;handle show) and return its one embed."""
    await type(cog).show.callback(cog, ctx, *args)
    return sent(ctx)


def sent(ctx: MagicMock) -> discord.Embed:
    ctx.send.assert_awaited_once_with(embed=ANY)
    embed = ctx.send.await_args.kwargs['embed']
    assert isinstance(embed, discord.Embed)
    return embed


async def link_atcoder(db: Database, user_id: int = MEMBER_ID) -> None:
    await AccountRepo(db).link(
        LinkedAccount(
            guild_id=GUILD_ID,
            user_id=user_id,
            platform='atcoder',
            handle='FakeAtCoder',
            method='affiliation-token',
            verified_at=discord.utils.utcnow().astimezone(UTC),
        )
    )


async def test_show_lists_a_members_codeforces_handle(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    other = make_member(OTHER_MEMBER_ID, 'Other Member')
    await user_db.set_handle(OTHER_MEMBER_ID, GUILD_ID, 'Fake_Coder')

    embed = await show(cog, ctx, other)

    assert embed.title == 'Handles of Other Member'
    assert embed.description == CODEFORCES_LINE


async def test_show_lists_your_own_handles_if_you_name_no_one(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'Fake_Coder')

    embed = await show(cog, ctx)

    assert embed.title == r'Handles of Fake \*Member\*'
    assert embed.description == CODEFORCES_LINE


async def test_with_kcpc_running_it_lists_the_accounts_linked_with_link_too(
    cog: Any,
    ctx: MagicMock,
    bot: MagicMock,
    user_db: UserDbConn,
    kcpc_db: Database,
) -> None:
    bot.kcpc = SimpleNamespace(db=kcpc_db)  # what it uses of KcpcServices
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'Fake_Coder')
    await link_atcoder(kcpc_db)

    embed = await show(cog, ctx)

    assert embed.description == f'{CODEFORCES_LINE}\n{ATCODER_LINE}'


async def test_kcpc_accounts_are_listed_without_a_codeforces_handle(
    cog: Any, ctx: MagicMock, bot: MagicMock, kcpc_db: Database
) -> None:
    bot.kcpc = SimpleNamespace(db=kcpc_db)
    await link_atcoder(kcpc_db)

    assert (await show(cog, ctx)).description == ATCODER_LINE


async def test_a_member_without_handles_is_told_so(
    handles: ModuleType, cog: Any, ctx: MagicMock, bot: MagicMock, kcpc_db: Database
) -> None:
    bot.kcpc = SimpleNamespace(db=kcpc_db)
    await link_atcoder(kcpc_db, OTHER_MEMBER_ID)

    with pytest.raises(handles.HandleCogError) as raised:
        await show(cog, ctx)

    assert str(raised.value) == f'<@{MEMBER_ID}> has not linked any handles.'
    ctx.send.assert_not_awaited()


async def test_the_codeforces_handle_is_still_shown_if_kcpc_fails(
    cog: Any,
    ctx: MagicMock,
    bot: MagicMock,
    user_db: UserDbConn,
    kcpc_db: Database,
    member: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot.kcpc = SimpleNamespace(db=kcpc_db)
    bug = RuntimeError('broken')
    monkeypatch.setattr(directory, 'linked_accounts', AsyncMock(side_effect=bug))
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'Fake_Coder')

    embed = await show(cog, ctx)

    assert embed.description == CODEFORCES_LINE
    (record,) = [record for record in caplog.records if record.name == 'Handles']
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f'Could not list the KCPC accounts of {member}'
    assert record.exc_info is not None and record.exc_info[1] is bug


async def test_show_is_refused_in_a_dm(cog: Any, ctx: MagicMock) -> None:
    ctx.guild = None

    with pytest.raises(commands.NoPrivateMessage):
        await show(cog, ctx)


async def test_the_group_on_its_own_shows_handles_too(
    cog: Any, ctx: MagicMock, user_db: UserDbConn
) -> None:
    # ;handle: prefix commands can run the group itself.
    await user_db.set_handle(MEMBER_ID, GUILD_ID, 'Fake_Coder')

    await type(cog).handle.callback(cog, ctx)

    assert sent(ctx).description == CODEFORCES_LINE


async def test_slash_handle_show_reaches_discord_saying_what_it_does(
    handles: ModuleType,
) -> None:
    """/handle show is in the tree that discord.py syncs, with its own description.

    discord.py copies the commands as it makes the cog, so the cog's copies, not
    the class's, are what count.
    """
    bot = commands.Bot(command_prefix=';', intents=discord.Intents.none())
    try:
        await bot.add_cog(handles.Handles(bot))

        group = bot.tree.get_command('handle')
        assert isinstance(group, discord.app_commands.Group)
        slash_show = group.get_command('show')
        assert isinstance(slash_show, discord.app_commands.Command)
        assert slash_show.description == "Show a member's linked handles"
        (parameter,) = slash_show.parameters
        assert (parameter.name, parameter.required) == ('member', False)
        assert parameter.description == 'Whose handles to show; yours if left out'
        # ;help keeps the group's brief, and ;handle show is there too.
        prefix_group = bot.get_command('handle')
        assert prefix_group is not None
        assert prefix_group.brief == 'Link, show and look up Codeforces handles'
        assert bot.get_command('handle show') is not None
    finally:
        await bot.close()


def test_help_stays_with_the_group_and_its_other_commands(handles: ModuleType) -> None:
    group = handles.Handles.handle

    assert group.help.startswith(
        'Show the handles a member has linked: yours, if you name no one.'
    )
    assert {'set', 'get', 'identify', 'remove', 'show'} <= set(group.all_commands)
