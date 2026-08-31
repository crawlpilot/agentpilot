"""Capturing a file the page hands back.

Ported from agent-browser's `download` / `wait_for_download`
(`actions.rs:6584, 9507`), but through Playwright's `Download` object rather than
raw `Page.downloadWillBegin` / `Page.downloadProgress`: patchright already
brokers those events into a handle that reports completion and failure, and
re-deriving it from CDP would mean reimplementing the part Chrome makes hardest
-- knowing when the bytes have actually finished landing.

**The driver owns the path.** Every download goes to a per-context temporary
directory and the caller is told where it landed. This is the whole reason
`download` can be offered to an agent while `upload_file` cannot: the model
names an element, never a filesystem location, so it can neither read the
operator's files nor write over them.

Nothing here deletes the directory; `PatchrightDriver.close` does, with the rest
of the context's scratch space, because a download is only useful for as long as
the session that produced it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from crawlpilot.spi.actions import DownloadInfo
from crawlpilot.spi.errors import WaitTimeout


async def capture(download: Any, into: Path, *, timeout_ms: int) -> DownloadInfo:
    """Save a Playwright `Download` into `into` and describe it.

    The suggested filename is used but never trusted: it comes from the page, via
    `Content-Disposition` or the `download` attribute, so it is attacker-supplied
    on any site an agent did not choose. `Path(...).name` strips directory
    components, which is what stops `../../.ssh/authorized_keys` from being a
    valid suggestion.
    """

    into.mkdir(parents=True, exist_ok=True)
    suggested = Path(download.suggested_filename or "download").name or "download"
    destination = _unique(into / suggested)

    failure = await download.failure()
    if failure is not None:
        raise WaitTimeout(f"download to complete ({failure})", timeout_ms)

    await download.save_as(destination)
    return DownloadInfo(
        path=str(destination),
        filename=destination.name,
        size_bytes=destination.stat().st_size if destination.exists() else 0,
        url=download.url or "",
    )


def _unique(path: Path) -> Path:
    """A path that does not exist yet.

    Two downloads of the same name in one session are ordinary -- paging through
    a report, or retrying one that looked stuck -- and silently overwriting the
    first would lose a file the caller was told it had.
    """

    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 1000):
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{id(path)}{suffix}")
