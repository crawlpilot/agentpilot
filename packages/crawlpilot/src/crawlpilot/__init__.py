"""crawlpilot -- an embeddable browser platform.

    from crawlpilot import Browser

    async with Browser() as browser:
        async with browser.session() as page:
            await page.navigate("https://example.com")
            print(await page.markdown())

Everything is optional and defaults to something inert-but-working: a real
Chrome driver, an in-process session registry, no proxies, no prototype
catalog, no extensions. A crawler passes nothing; a platform injects its own
registry, proxy provider and extensions and gets the same object.

**This module is the public API.** Names re-exported here are the supported
surface; everything under `crawlpilot.<submodule>` is reachable but subject to
change without a major bump. `agentpilot`'s own test suite asserts it imports
only from here and the documented submodules (`crawlpilot.tools`,
`crawlpilot.content`, `crawlpilot.spi`), which is the check that keeps the two
projects genuinely separate rather than one codebase in two directories.

`Browser` builds its driver lazily, so importing this module pulls no Chrome
machinery -- a Chrome-free deployment installs `crawlpilot` without `[engine]`
and never imports `crawlpilot.driver` at all.
"""

from crawlpilot.api import Browser, BrowserSession
from crawlpilot.client import (
    AsyncCrawlpilot,
    Crawlpilot,
    SyncSession,
    proxy_endpoint,
)
from crawlpilot.config import (
    BrowserConfig,
    EgressConfig,
    FingerprintConfig,
    ProfileConfig,
    ProxyHealthConfig,
)
from crawlpilot.extensions import (
    Extension,
    ExtensionManifest,
    ExtensionRegistry,
    Resolution,
)
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import (
    InMemoryStateStore,
    NullPrototypes,
    PrototypeProvider,
    ProxyProvider,
    StateStore,
    StaticProxies,
)
from crawlpilot.spi.dom_tree import RefInfo, Snapshot
from crawlpilot.spi.errors import (
    ChallengeDetected,
    ContextCrashed,
    DriverError,
    NavigationTimeout,
    SelectorNotFound,
    StaleRefError,
    WaitTimeout,
)
from crawlpilot.spi.identity import IdentityRef, ProfileKind
from crawlpilot.spi.proxy import ProxyEndpoint
from crawlpilot.spi.scrape import Document, ScrapeOptions
from crawlpilot.tiers import Tier, TierPolicy
from crawlpilot.tools import ToolRegistry, ToolSpec, browser_tools
from crawlpilot.verbs import SessionVerbs

__version__ = "0.2.0"

__all__ = [
    "AsyncCrawlpilot",
    "Browser",
    "BrowserConfig",
    "BrowserSession",
    "ChallengeDetected",
    "ContextCrashed",
    "Crawlpilot",
    "Document",
    "DriverError",
    "EgressConfig",
    "Extension",
    "ExtensionManifest",
    "ExtensionRegistry",
    "FingerprintConfig",
    "IdentityRef",
    "InMemoryStateStore",
    "NavigationTimeout",
    "NullPrototypes",
    "ProfileConfig",
    "ProfileKind",
    "PrototypeProvider",
    "ProxyEndpoint",
    "ProxyHealthConfig",
    "ProxyPinner",
    "ProxyProvider",
    "RefInfo",
    "Resolution",
    "ScrapeOptions",
    "SelectorNotFound",
    "SessionVerbs",
    "Snapshot",
    "StaleRefError",
    "StateStore",
    "StaticProxies",
    "SyncSession",
    "Tier",
    "TierPolicy",
    "ToolRegistry",
    "ToolSpec",
    "WaitTimeout",
    "__version__",
    "browser_tools",
    "proxy_endpoint",
]
