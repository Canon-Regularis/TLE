"""Text that Discord's markdown shows as it is written: escaped text and links.

Posts and replies repeat what others wrote, such as problems' names and what
members typed, which mustn't make a link or bold text, say.
"""

import re

# The longest link that a message shows as a link, rather than as its text
# alone: ten weeks of /weekly history, or a field of a reply, stay within
# Discord's limits with links this long. Admins' links are no longer.
LINK_LIMIT = 300

# The characters that Discord's markdown gives a meaning to within a line.
_MARKDOWN = re.compile(r'([\\\[\]*_~`|])')


def escape(text: str) -> str:
    """``text`` with the characters Discord's markdown reads escaped.

    For text within a line: text that starts a line may still be read as a
    heading or a quote.
    """
    return _MARKDOWN.sub(r'\\\1', text)


def link(text: str, url: str) -> str:
    """A Markdown link to ``url`` showing ``text``; the text alone if the link
    is too long (see ``fits``).
    """
    shown = escape(text)
    if not fits(url):
        return shown
    return f'[{shown}]({_target(url)})'


def fits(url: str) -> bool:
    """Whether ``link`` shows ``url`` as a link: it is at most ``LINK_LIMIT``
    characters long, as written in the link.
    """
    return len(_target(url)) <= LINK_LIMIT


def _target(url: str) -> str:
    """``url`` as a Markdown link gives it: a ')' would end the link early."""
    return url.replace('(', '%28').replace(')', '%29')
