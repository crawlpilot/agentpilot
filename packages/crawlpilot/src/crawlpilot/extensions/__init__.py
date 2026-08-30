"""Site- and domain-specific behaviour, contributed by the *caller*.

`crawlpilot.policy` sends site knowledge out of the library as data the consumer
owns; this is the seam it comes back through as *code*, for what data cannot
express -- rewriting a URL before navigation, running a site-specific warm-up,
*resolving* a detected block, repairing markup, enriching a document.

Not a new mechanism: `extraction.block_detect` already had a chain
(`is_relevant(url)` -> `check(...) -> Verdict | None`, first non-`None` wins,
`None` defers), ported from Pulsar's `ChainedHtmlIntegrityChecker`. This promotes
it from a module-level global populated at import time into an injectable,
multi-hook, pip-installable registry, keeping the defer convention so porting the
existing checkers was mechanical.
"""

from crawlpilot.extensions.hooks import DEFAULT_HOOK_TIMEOUT_S, HookChain
from crawlpilot.extensions.manifest import (
    API_VERSION,
    Compatibility,
    ExtensionManifest,
    check_compatibility,
)
from crawlpilot.extensions.mounts import (
    BlockHooks,
    BlockMount,
    BrowseHooks,
    BrowseMount,
    ContentHooks,
    ContentMount,
    Extension,
    Resolution,
    ToolMount,
)
from crawlpilot.extensions.registry import (
    ENTRY_POINT_GROUP,
    ExtensionRegistry,
    discover_extensions,
)

__all__ = [
    "API_VERSION",
    "DEFAULT_HOOK_TIMEOUT_S",
    "ENTRY_POINT_GROUP",
    "BlockHooks",
    "BlockMount",
    "BrowseHooks",
    "BrowseMount",
    "Compatibility",
    "ContentHooks",
    "ContentMount",
    "Extension",
    "ExtensionManifest",
    "ExtensionRegistry",
    "HookChain",
    "Resolution",
    "ToolMount",
    "check_compatibility",
    "discover_extensions",
]
