"""Tripwire over the symbols that must survive the `browserpilot` extraction.

Phase 7 of docs/browser-module-rearchitecture-plan.md moves the browser layer
out of `agentpilot` into a separately built `browserpilot` distribution. Today
none of these symbols is re-exported from a package `__init__`, so any of them
could be renamed or moved without a single test failing (defect D1: every
`__init__.py` in the repo is empty or a bare docstring).

This file pins the inventory *before* the move, so the move can be verified as
surface-preserving rather than hoped to be. It asserts importability and
identity of location -- not behaviour, which the rest of the suite covers.

Maintenance contract: adding a symbol here is cheap and expected as the public
API grows. **Removing or relocating one is a public API change** and must be a
deliberate edit to this file in the same commit, not a silent drift.
"""

from __future__ import annotations

import importlib

import pytest

PUBLIC_SURFACE: dict[str, tuple[str, ...]] = {
    # The driver-agnostic seam -- becomes `browserpilot.contracts`.
    "agentpilot.spi.driver": ("BrowserDriver",),
    "agentpilot.spi.actions": (
        "NavigateAction", "GoBackAction", "SnapshotAction", "ExtractAction",
        "ScreenshotAction", "WaitAction", "ExecuteJsAction", "ClickAction",
        "FillAction", "SelectOptionAction", "HoverAction", "PressAction",
        "ScrollAction", "NewTabAction", "CloseTabAction", "SwitchTabAction",
        "ListTabsAction", "TabInfo", "ActionResult",
    ),
    "agentpilot.spi.identity": ("ProfileKind", "IdentityKey"),
    "agentpilot.spi.lease": ("ContextState", "ContextRef", "Lease"),
    "agentpilot.spi.scrape": ("ExtractConfig", "ScrapeOptions", "DocumentMetadata", "Document"),
    "agentpilot.spi.egress": ("EgressPolicy",),
    "agentpilot.spi.proxy": ("ProxyEndpoint",),
    "agentpilot.spi.storage_state": ("LocalStorageEntry", "OriginState", "StorageState"),
    "agentpilot.spi.health": ("HealthStatus", "ContextHealth"),
    "agentpilot.spi.geometry": ("BoundingBox",),
    "agentpilot.spi.dom_tree": (
        "LayoutInfo", "NodeType", "EnhancedAXNode", "EnhancedDOMTreeNode",
    ),
    "agentpilot.spi.errors": (
        "DriverError", "NavigationTimeout", "ContextCrashed", "ChallengeDetected",
        "StaleRefError", "TabNotFound", "LeaseConflict", "NodeLost",
        "CapacityExhausted", "EgressBlocked", "CdpNotAvailable",
    ),
    # Pure content pipeline -- becomes `browserpilot.content`. `extract` is the
    # function that Phase 6 also exposes as a tool and as `session.markdown()`.
    "agentpilot.extraction.extractor": ("extract",),
    "agentpilot.extraction.block_detect": ("classify_page",),
    # Session pooling -- becomes `browserpilot.pool`.
    "agentpilot.session.registry": ("Registry", "RegistryProtocol"),
    # Identity/stealth -- split across `browserpilot.{identity,proxy,stealth}`.
    "agentpilot.session.stealth_profile": ("StealthProfile", "resolve"),
    # Phase 1 consolidation. `stealth_from_tier`/`interact_profile_for_tier`
    # (was `spi.actions`) and `effective_tier`/`is_protected` (was
    # `session.stealth_profile`) are gone as free functions -- they are now
    # fields on `TierPolicy`, which is the single owner. Recorded here as the
    # deliberate public-API edit this file's maintenance contract requires.
    "agentpilot.tiers": ("Tier", "TierName", "TierPolicy", "PROTECTED", "ESCALATION"),
    # Phase 3a: the shared-state seam. `InMemoryStateStore` is the shipped
    # default; `control.redis_store.RedisStateStore` is the injected one.
    "agentpilot.policy": ("StateStore", "InMemoryStateStore"),
    # Phase 2: the only module in the browser layer that reads the environment.
    "agentpilot.config": (
        "BrowserConfig", "FingerprintConfig", "ProfileConfig",
        "ProxyHealthConfig", "EgressConfig", "ContentConfig", "DEFAULTS",
    ),
    "agentpilot.identity.profile_store": ("resolve_profile_dir", "prototype_dir_for"),
    "agentpilot.identity.proxy_config": ("ProxyConfig",),
}

_CASES = [(mod, name) for mod, names in PUBLIC_SURFACE.items() for name in names]


@pytest.mark.parametrize(("module", "symbol"), _CASES, ids=[f"{m}.{s}" for m, s in _CASES])
def test_public_symbol_is_importable(module: str, symbol: str) -> None:
    mod = importlib.import_module(module)
    assert hasattr(mod, symbol), f"{module}.{symbol} is gone -- public API change?"


def test_package_ships_a_py_typed_marker() -> None:
    """A distribution without `py.typed` gives its consumers no types at all
    (PEP 561), which for a library whose whole value is a typed seam would be
    a silent regression."""

    import agentpilot

    root = importlib.resources.files(agentpilot)
    assert (root / "py.typed").is_file()


def test_browser_layer_carries_no_web_framework_import() -> None:
    """The future `browserpilot` closure must not contain FastAPI/Starlette or
    Redis (plan §3.3, D11). Enforced here at the source level before the split
    makes it enforceable at the dependency level."""

    import ast
    import pathlib

    banned = {"fastapi", "starlette", "psycopg", "prometheus_client"}
    offenders: list[str] = []
    for pkg in ("spi", "driver", "extraction", "dom", "egress"):
        for path in pathlib.Path("agentpilot", pkg).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for n in names:
                    if n in banned:
                        offenders.append(f"{path}: {n}")
    assert not offenders, offenders


def test_browser_layer_reads_no_ambient_environment() -> None:
    """Leaves take configuration as arguments; only a composition root reads
    the environment (plan D8, Phase 2).

    Two exemptions, both deliberate and both narrow:

    - `driver/process_launcher.py` touches `DISPLAY`, which is the X11
      protocol's own channel rather than application configuration -- Chrome is
      launched as a child process and reads it from the inherited environment,
      so it must genuinely be there.
    - `identity/proxy_config.py` reads inside a `from_env()` constructor called
      once from `gateway.wiring`, which is the sanctioned pattern, not a read
      at the point of use.

    Anything else is a regression: it makes the library unconfigurable by a
    caller that does not own the process.
    """

    import ast
    import pathlib

    allowed = {"driver/process_launcher.py", "identity/proxy_config.py"}
    offenders: list[str] = []
    for pkg in ("spi", "driver", "identity", "egress", "extraction", "dom", "session", "tiers"):
        for path in pathlib.Path("agentpilot", pkg).rglob("*.py"):
            rel = path.relative_to("agentpilot").as_posix()
            if rel in allowed:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"ambient environment reads: {offenders}"
