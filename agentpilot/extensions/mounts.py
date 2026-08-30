"""Mount points -- an extension declares which surfaces it participates in.

Deliberately several narrow Protocols rather than one wide `SiteExtension` with
optional no-op methods (Browser4's `skeleton/plugin/MountPoints.kt`). Two
benefits, both real: the host can tell what an extension touches *without
calling it*, and a content-only extension is never handed a `BrowserSession` it
has no business driving.

An extension implements any subset. It configures chains rather than being
polled, so one extension can register on many hooks, register several handlers
on one hook, and control its own ordering with `add_first` / `add_last`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from agentpilot.extensions.hooks import HookChain
from agentpilot.extensions.manifest import ExtensionManifest


class Resolution(Enum):
    """What an extension did about a detected block, and what should follow."""

    SOLVED = "solved"
    """The wall was cleared in place; retry the read on this same context."""
    RETRY = "retry"
    """Try again with a fresh identity on the same tier."""
    ESCALATE = "escalate"
    """Move up the tier ladder (`tiers.ESCALATION`)."""
    GIVE_UP = "give_up"
    """Do not spend more attempts on this page."""


@dataclass
class BrowseHooks:
    """Driver lifecycle, in execution order."""

    will_launch: HookChain[None] = field(default_factory=lambda: HookChain("will_launch"))
    launched: HookChain[None] = field(default_factory=lambda: HookChain("launched"))
    will_navigate: HookChain[str] = field(default_factory=lambda: HookChain("will_navigate"))
    """Filter-shaped: return a rewritten URL, or `None` to leave it alone."""
    navigated: HookChain[None] = field(default_factory=lambda: HookChain("navigated"))
    will_interact: HookChain[None] = field(default_factory=lambda: HookChain("will_interact"))
    document_loaded: HookChain[None] = field(default_factory=lambda: HookChain("document_loaded"))
    document_steady: HookChain[None] = field(default_factory=lambda: HookChain("document_steady"))
    """The DOM has stopped mutating -- distinct from "loaded", and the right
    place for a site-specific warm-up or a modal dismissal. Browser4 flags the
    equivalent hook as *the* one for automation; we had no equivalent before."""
    did_interact: HookChain[None] = field(default_factory=lambda: HookChain("did_interact"))
    will_close: HookChain[None] = field(default_factory=lambda: HookChain("will_close"))


@dataclass
class ContentHooks:
    """Fetch -> parse -> extract."""

    will_fetch: HookChain[None] = field(default_factory=lambda: HookChain("will_fetch"))
    fetched: HookChain[None] = field(default_factory=lambda: HookChain("fetched"))
    will_parse: HookChain[str] = field(default_factory=lambda: HookChain("will_parse"))
    """Filter-shaped: return repaired HTML, or `None` to leave it alone."""
    parsed: HookChain[None] = field(default_factory=lambda: HookChain("parsed"))
    will_extract: HookChain[None] = field(default_factory=lambda: HookChain("will_extract"))
    extracted: HookChain[Any] = field(default_factory=lambda: HookChain("extracted"))
    """Value-chaining: each handler may enrich the document."""


@dataclass
class BlockHooks:
    """Detection *and* resolution."""

    classify: HookChain[Any] = field(default_factory=lambda: HookChain("classify"))
    """Synchronous and filter-shaped, matching the `SiteChecker` contract this
    replaced -- so `block_detect.classify_page` stays a pure function."""
    resolve: HookChain[Resolution] = field(default_factory=lambda: HookChain("resolve"))
    """Asynchronous: may drive the session to clear a wall, then say what should
    follow. The capability with no equivalent before -- site knowledge could
    previously only *say* "this is a wall", never act on it."""


@runtime_checkable
class Extension(Protocol):
    manifest: ExtensionManifest


@runtime_checkable
class BrowseMount(Protocol):
    manifest: ExtensionManifest

    def configure_browse(self, hooks: BrowseHooks) -> None: ...


@runtime_checkable
class ContentMount(Protocol):
    manifest: ExtensionManifest

    def configure_content(self, hooks: ContentHooks) -> None: ...


@runtime_checkable
class BlockMount(Protocol):
    manifest: ExtensionManifest

    def configure_blocks(self, hooks: BlockHooks) -> None: ...
