"""Text that Discord's markdown shows as it is written: escaped text and links.

Copied from the problems feature's helpers, which this feature may not import.
"""

import re

# The characters that Discord's markdown gives a meaning to within a line.
_MARKDOWN = re.compile(r'([\\\[\]*_~`|])')


def escape(text: str) -> str:
    """``text`` with the characters Discord's markdown reads escaped.

    For text within a line: text that starts a line may still be read as a
    heading or a quote.
    """
    return _MARKDOWN.sub(r'\\\1', text)


def link(text: str, url: str) -> str:
    """A Markdown link to ``url`` showing ``text``, escaped."""
    # A ')' in the URL would end the link early.
    target = url.replace('(', '%28').replace(')', '%29')
    return f'[{escape(text)}]({target})'
