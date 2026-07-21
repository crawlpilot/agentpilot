"""Parses Playwright/Patchright's `aria_snapshot(mode="ai")` text format into
`spi.snapshot` types.

The format is indented (2 spaces/level) lines like:

    - generic [active] [ref=e1]:
      - heading "Hello" [level=1] [ref=e2]
      - link "A link" [ref=e3] [cursor=pointer]:
        - /url: "#"

Lines are `- role "name"? [attr]* :?`. Metadata lines (e.g. `/url:`) carry no
`ref=` attribute; they're kept as informational tree nodes with `ref=""` so
depth/nesting stays correct, but the ref->locator cascade (P1) only ever
resolves nodes with a non-empty ref.
"""

from __future__ import annotations

import re

from baas.spi.snapshot import SnapshotNode

_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_HEAD_RE = re.compile(r'^(?P<role>\S+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?\s*(?P<rest>.*)$')
_ATTR_RE = re.compile(r"\[([^\]]*)\]")


def parse_aria_snapshot(text: str, epoch: int) -> SnapshotNode:
    root = SnapshotNode(epoch=epoch, ref="", role="root", name="")
    stack: list[tuple[int, SnapshotNode]] = [(-1, root)]

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue

        depth = len(m.group("indent")) // 2
        body = m.group("body").rstrip()
        if body.endswith(":"):
            body = body[:-1].rstrip()

        hm = _HEAD_RE.match(body)
        if not hm:
            continue

        ref = ""
        for attr in _ATTR_RE.findall(hm.group("rest")):
            if attr.startswith("ref="):
                ref = attr[len("ref=") :]

        node = SnapshotNode(
            epoch=epoch,
            ref=ref,
            role=hm.group("role"),
            name=hm.group("name") or "",
        )

        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((depth, node))

    return root
