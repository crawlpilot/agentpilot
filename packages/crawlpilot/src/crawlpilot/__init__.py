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
from crawlpilot.config import (
    BrowserConfig,
    EgressConfig,
    FingerprintConfig,
    ProfileConfig,
    ProxyHealthConfig,
)
from crawlpilot.extensions import (
    BlockHooks,
    BlockMount,
    BrowseHooks,
    BrowseMount,
    ContentHooks,
    ContentMount,
    Extension,
    ExtensionManifest,
    ExtensionRegistry,
    Resolution,
    ToolMount,
)
from crawlpilot.policy import (
    InMemoryStateStore,
    NullPrototypes,
    PrototypeProvider,
    ProxyProvider,
    StateStore,
    StaticProxies,
)
from crawlpilot.spi.errors import (
    ChallengeDetected,
    ContextCrashed,
    DriverError,
    NavigationTimeout,
)
from crawlpilot.spi.identity import IdentityRef, ProfileKind
from crawlpilot.spi.scrape import Document, ScrapeOptions
from crawlpilot.tiers import Tier, TierPolicy
from crawlpilot.tools import ToolRegistry, ToolSpec, browser_tools

__version__ = "0.1.0"

__all__ = [
    "BlockHooks",
    "BlockMount",
    "Browser",
    "BrowserConfig",
    "BrowserSession",
    "BrowseHooks",
    "BrowseMount",
    "ChallengeDetected",
    "ContentHooks",
    "ContentMount",
    "ContextCrashed",
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
    "ProxyHealthConfig",
    "ProxyProvider",
    "Resolution",
    "ScrapeOptions",
    "StateStore",
    "StaticProxies",
    "Tier",
    "TierPolicy",
    "ToolMount",
    "ToolRegistry",
    "ToolSpec",
    "__version__",
    "browser_tools",
]
