"""Stage 3 of the extraction pipeline: markdown string post-processing.

Ported from Firecrawl's `post_process_markdown`/`remove_skip_to_content_links`
(apps/api/native/src/html.rs).
"""

from __future__ import annotations

_SKIP_LABEL = "skip to content"


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


def postprocess(markdown: str) -> str:
    return strip_skip_links(escape_link_label_newlines(markdown))
