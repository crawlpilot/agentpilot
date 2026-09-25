"""Stage 3 of the extraction pipeline: markdown string post-processing.

Ported from Firecrawl's `post_process_markdown`/`remove_skip_to_content_links`
(apps/api/native/src/html.rs). `to_citations` is additive and separate -- see
its own docstring.
"""

from __future__ import annotations

import re

_SKIP_LABEL = "skip to content"

# `[label](href)` and `[label](href "title")`, but not `![alt](src)`.
#
# The href alternation tolerates one level of balanced parentheses, which is
# not pedantry: `en.wikipedia.org/wiki/Python_(programming_language)` is an
# ordinary link, and a naive `[^)]+` truncates it mid-URL.
#
# The label allows `\.` escapes because `escape_link_label_newlines` has
# already run by the time this does, so a label that wrapped across lines in
# the source HTML now contains a backslash line-continuation.
_LINK_RE = re.compile(
    r"(?<!!)\[((?:[^\[\]\\]|\\.)*)\]\("
    r"([^\s)]*(?:\([^\s)]*\)[^\s)]*)*)"
    r'(?:\s+"[^"]*")?\)',
    re.DOTALL,
)

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

CITATION_HEADING = "## References"


def escape_link_label_newlines(markdown: str) -> str:
    """Escape embedded newlines inside `[...]` link-label spans with a
    trailing backslash line-continuation, so a label that wrapped across
    lines in the source HTML doesn't produce broken markdown."""
    out: list[str] = []
    link_open_count = 0
    for ch in markdown:
        if ch == "[":
            link_open_count += 1
        elif ch == "]":
            link_open_count = max(0, link_open_count - 1)

        if link_open_count > 0 and ch == "\n":
            out.append("\\\n")
        else:
            out.append(ch)
    return "".join(out)


def strip_skip_links(markdown: str) -> str:
    """Strip `[Skip to Content](#...)`-style skip-navigation links."""
    out: list[str] = []
    i = 0
    length = len(markdown)
    while i < length:
        if markdown[i] == "[":
            label_start = i + 1
            label_end = label_start + len(_SKIP_LABEL)
            label = markdown[label_start:label_end]
            if (
                label_end + 3 <= length
                and label.lower() == _SKIP_LABEL
                and markdown[label_end] == "]"
                and markdown[label_end + 1] == "("
                and markdown[label_end + 2] == "#"
            ):
                close = markdown.find(")", label_end + 3)
                if close != -1:
                    i = close + 1
                    continue
        out.append(markdown[i])
        i += 1
    return "".join(out)


def to_citations(markdown: str) -> str:
    """Rewrite inline links as numbered references, with the URLs collected
    into a trailing `## References` block.

    A link-dense page spends a startling share of its markdown on hrefs --
    tracking parameters, signed CDN paths, forty-character slugs -- and every
    one of them is billed to whatever model reads the page next. Moving each
    URL to a reference list costs one number at the point of use and one line at
    the bottom, and repeated links (a site that links its own homepage in every
    paragraph) collapse onto a single entry.

    Three things are deliberately left alone:

    * **Fenced code blocks.** A `[...](...)` inside a code sample is the sample,
      not a link. Inline code spans are *not* tracked -- a bracket-paren pair
      inside backticks on the same line as prose is rare enough that the
      tracking is not worth the false positives it would introduce elsewhere.
    * **Images.** `![alt](src)` keeps its source inline: the alt text is not a
      caption that reads sensibly with a number after it, and an image
      reference the far end cannot resolve is worse than a long URL.
    * **Fragment-only links** (`[see below](#pricing)`). They point inside the
      page, so a reference entry would list a URL that means nothing on its own.

    Returns the markdown unchanged when the page has no qualifying links, so a
    caller cannot tell "citations on" from "citations on, nothing to cite" by
    finding an empty references heading -- there is never an empty one.
    """

    urls: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        if not href or href.startswith("#"):
            return match.group(0)
        number = urls.setdefault(href, len(urls) + 1)
        return f"[{label}][{number}]"

    # Accumulate runs of prose and substitute over the whole run rather than
    # line by line: a label that wrapped in the source HTML spans two lines by
    # the time it gets here, and a per-line pass would never match it.
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None

    def flush() -> None:
        if prose:
            out.append(_LINK_RE.sub(replace, "\n".join(prose)))
            prose.clear()

    for line in markdown.split("\n"):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                flush()
                fence = marker
            elif marker == fence:
                fence = None
            out.append(line)
            continue
        if fence:
            out.append(line)
        else:
            prose.append(line)
    flush()

    body = "\n".join(out)
    if not urls:
        return body

    references = "\n".join(f"[{number}]: {href}" for href, number in urls.items())
    return f"{body.rstrip()}\n\n{CITATION_HEADING}\n\n{references}\n"


def postprocess(markdown: str, *, citations: bool = False) -> str:
    result = strip_skip_links(escape_link_label_newlines(markdown))
    return to_citations(result) if citations else result
