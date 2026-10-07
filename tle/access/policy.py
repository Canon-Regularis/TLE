"""A command's rule in one server, and rules in plain English.

``effective_for`` combines a command's default rule from ``table`` with a
server's settings from ``settings``, ready for ``rules.decide``.
``describe_who`` and ``describe_where`` say what a rule means, and
``describe_limit`` what a limit changes, in the same words for /help and
/access.
"""

from dataclasses import replace

from tle.access.rules import FAIL_CLOSED, Effective, Limit, Where, Who, effective
from tle.access.settings import GuildAccess
from tle.access.table import (
    canonical,
    default_who,
    is_protected,
    limit_keys,
    rule_for,
)


def effective_for(name: str, access: GuildAccess) -> Effective:
    """Command ``name``'s rule in a server whose settings are ``access``.

    A command missing from the table gets ``FAIL_CLOSED``, which nothing
    loosens. Limits never apply to the bot owner's commands, nor to /help and
    /access, which admins need to undo limits; broken settings refuse every
    other command. A few of the owner's commands need the owner to be an
    admin too (``table.OWNER_AND_ADMIN``). Until the server has a staff
    channel, the /access commands work in any channel, so that an admin can
    set one.
    """
    ruled = rule_for(name)
    rule = ruled or FAIL_CLOSED
    if rule.who is Who.OWNER or is_protected(name):
        result = replace(effective(rule), who=default_who(name))
        bootstrap = canonical(name).split(' ')[0] == 'access' and ruled is not None
        if bootstrap and access.staff_channel is None:
            result = replace(result, where=Where.ANYWHERE)
        return result
    if access.broken:
        return replace(effective(rule), broken=True)
    limits = [access.limits[key] for key in limit_keys(name) if key in access.limits]
    return effective(rule, limits)


# What each level, or each pair of levels that neither implies, means. The
# plural for most rules; the singular for the bot owner's, which can require
# a level as well.
_PLURAL: dict[frozenset[Who], str] = {
    frozenset({Who.EVERYONE}): 'Everyone',
    frozenset({Who.TRUSTED}): 'Trusted members, moderators and admins',
    frozenset({Who.MODERATOR}): 'Moderators and admins',
    frozenset({Who.DEVELOPER}): 'Developers and admins',
    frozenset({Who.ADMIN}): 'Admins',
    frozenset({Who.TRUSTED, Who.DEVELOPER}): (
        'Admins, and developers who are also trusted members or moderators'
    ),
    frozenset({Who.MODERATOR, Who.DEVELOPER}): (
        'Admins, and moderators who are also developers'
    ),
}
_SINGULAR: dict[frozenset[Who], str] = {
    frozenset({Who.TRUSTED}): 'a trusted member, moderator or admin',
    frozenset({Who.MODERATOR}): 'a moderator or admin',
    frozenset({Who.DEVELOPER}): 'a developer or admin',
    frozenset({Who.ADMIN}): 'an admin',
    frozenset({Who.TRUSTED, Who.DEVELOPER}): (
        'an admin, or a developer who is also a trusted member or moderator'
    ),
    frozenset({Who.MODERATOR, Who.DEVELOPER}): (
        'an admin, or a moderator who is also a developer'
    ),
}
# The levels that passing each level also passes, besides everyone.
_IMPLIED: dict[Who, frozenset[Who]] = {
    Who.EVERYONE: frozenset(),
    Who.TRUSTED: frozenset(),
    Who.MODERATOR: frozenset({Who.TRUSTED}),
    Who.DEVELOPER: frozenset(),
    Who.ADMIN: frozenset({Who.TRUSTED, Who.MODERATOR, Who.DEVELOPER}),
    Who.OWNER: frozenset(),
}


def describe_who(who: frozenset[Who]) -> str:
    """Who passes every level in ``who``, such as 'Moderators and admins'.

    Levels that another one in ``who`` implies are left out, so that sets
    which let in the same members read the same. ``ValueError`` if ``who`` is
    empty.
    """
    if not who:
        raise ValueError('No level to describe')
    implied = {level for each in who for level in _IMPLIED[each]}
    levels = who - implied - {Who.EVERYONE, Who.OWNER}
    if Who.OWNER not in who:
        return _PLURAL[levels or frozenset({Who.EVERYONE})]
    if not levels:
        return 'The bot owner'
    return f'The bot owner, who must also be {_SINGULAR[levels]}'


def describe_where(where: Where, *, slash: bool = True) -> str:
    """Where a command with ``where`` works, as a short sentence.

    With ``slash`` False, for a command with no slash form, it leaves out what
    the slash command does elsewhere.
    """
    if where is Where.ANYWHERE:
        return 'Any channel'
    if where is Where.BOT_ONLY:
        return 'Bot channels only'
    if where is Where.STAFF_ONLY:
        return 'The staff channel only'
    place = 'Bot channels' if where is Where.BOT else 'The staff channel'
    if not slash:
        return place
    return f'{place}; elsewhere the slash command answers only you'


def describe_limit(limit: Limit) -> str:
    """What ``limit`` changes, as the end of a sentence, such as 'switched
    off' or 'for moderators and admins only; in bot channels only'.

    It says what the limit itself sets: /help's and /access's other fields
    say what that means for each command, slash or not.
    """
    parts: list[str] = []
    if limit.off:
        parts.append('switched off')
    if limit.who is not None:
        who = describe_who(frozenset({limit.who}))
        parts.append(f'for {_lower_first(who)} only')
    if limit.where is not None:
        parts.append(f'in {_lower_first(describe_where(limit.where, slash=False))}')
    if limit.private:
        parts.append('answers only the person who uses it')
    return '; '.join(parts) or 'no change'


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]
