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
import pathlib

import pytest

CRAWLPILOT_SRC = pathlib.Path(__file__).resolve().parents[1] / "packages/crawlpilot/src/crawlpilot"
"""Resolved from this file, not the working directory: these guards walk the
source tree, and a relative path that silently misses would let every one of
them pass vacuously -- which is exactly what happened when the packages moved
in Phase 7."""


def _browser_layer_files(*packages: str) -> list[pathlib.Path]:
    assert CRAWLPILOT_SRC.is_dir(), f"crawlpilot source not found at {CRAWLPILOT_SRC}"
    out: list[pathlib.Path] = []
    for pkg in packages:
        directory = CRAWLPILOT_SRC / pkg
        module = CRAWLPILOT_SRC / f"{pkg}.py"
        if directory.is_dir():
            out += sorted(directory.rglob("*.py"))
        elif module.is_file():
            out.append(module)
        else:
            raise AssertionError(f"no such crawlpilot package or module: {pkg}")
    return out


PUBLIC_SURFACE: dict[str, tuple[str, ...]] = {
    # The driver-agnostic seam -- becomes `browserpilot.contracts`.
    "crawlpilot.spi.driver": ("BrowserDriver",),
    "crawlpilot.spi.actions": (
        "NavigateAction", "GoBackAction", "SnapshotAction", "ExtractAction",
        "ScreenshotAction", "WaitAction", "ExecuteJsAction", "ClickAction",
        "FillAction", "SelectOptionAction", "HoverAction", "PressAction",
        "ScrollAction", "NewTabAction", "CloseTabAction", "SwitchTabAction",
        "ListTabsAction", "TabInfo", "ActionResult",
    ),
    "crawlpilot.spi.identity": ("ProfileKind", "IdentityRef"),
    "crawlpilot.spi.lease": ("ContextState", "ContextRef", "Lease"),
    "crawlpilot.spi.scrape": ("ExtractConfig", "ScrapeOptions", "DocumentMetadata", "Document"),
    "crawlpilot.spi.egress": ("EgressPolicy",),
    "crawlpilot.spi.proxy": ("ProxyEndpoint",),
    "crawlpilot.spi.storage_state": ("LocalStorageEntry", "OriginState", "StorageState"),
    "crawlpilot.spi.health": ("HealthStatus", "ContextHealth"),
    "crawlpilot.spi.geometry": ("BoundingBox",),
    "crawlpilot.spi.dom_tree": (
        "LayoutInfo", "NodeType", "EnhancedAXNode", "EnhancedDOMTreeNode",
    ),
    "crawlpilot.spi.errors": (
        "DriverError", "NavigationTimeout", "ContextCrashed", "ChallengeDetected",
        "StaleRefError", "TabNotFound", "LeaseConflict", "NodeLost",
        "CapacityExhausted", "EgressBlocked", "CdpNotAvailable",
    ),
    # Pure content pipeline -- becomes `browserpilot.content`. `extract` is the
    # function that Phase 6 also exposes as a tool and as `session.markdown()`.
    "crawlpilot.extraction.extractor": ("extract",),
    "crawlpilot.extraction.block_detect": ("classify_page",),
    # Session pooling -- becomes `browserpilot.pool`.
    "crawlpilot.session.registry": ("Registry", "RegistryProtocol"),
    # Identity/stealth -- split across `browserpilot.{identity,proxy,stealth}`.
    "crawlpilot.session.stealth_profile": ("StealthProfile", "resolve"),
    # Phase 1 consolidation. `stealth_from_tier`/`interact_profile_for_tier`
    # (was `spi.actions`) and `effective_tier`/`is_protected` (was
    # `session.stealth_profile`) are gone as free functions -- they are now
    # fields on `TierPolicy`, which is the single owner. Recorded here as the
    # deliberate public-API edit this file's maintenance contract requires.
    "crawlpilot.tiers": ("Tier", "TierName", "TierPolicy", "PROTECTED", "ESCALATION"),
    # Phase 3a: the shared-state seam. `InMemoryStateStore` is the shipped
    # default; `control.redis_store.RedisStateStore` is the injected one.
    # Phase 5: the facade -- the entry point a library consumer imports.
    "crawlpilot.api": ("Browser", "BrowserSession"),
    # Phase 4: the extension seam.
    "crawlpilot.extensions": (
        "ExtensionRegistry", "ExtensionManifest", "Extension", "Resolution",
        "BrowseHooks", "ContentHooks", "BlockHooks",
        "BrowseMount", "ContentMount", "BlockMount",
        "HookChain", "check_compatibility", "discover_extensions", "API_VERSION",
    ),
    "crawlpilot.policy": (
        "StateStore", "InMemoryStateStore",
        "ProxyProvider", "PrototypeProvider", "StaticProxies", "NullPrototypes",
    ),
    # Phase 2: the only module in the browser layer that reads the environment.
    "crawlpilot.config": (
        "BrowserConfig", "FingerprintConfig", "ProfileConfig",
        "ProxyHealthConfig", "EgressConfig", "ContentConfig", "DEFAULTS",
    ),
    # Phase 3b: `profile_store` keeps the mechanics; the prototype *catalog* and
    # the tenant-keyed proxy table moved to `agentpilot.control`, behind the
    # `policy` provider seams. Deliberate public-API edits, per this file's
    # maintenance contract.
    "crawlpilot.identity.profile_store": ("resolve_profile_dir", "seed_profile_dir"),
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

    import crawlpilot

    root = importlib.resources.files(crawlpilot)
    assert (root / "py.typed").is_file()


def test_browser_layer_carries_no_web_framework_import() -> None:
    """The future `browserpilot` closure must not contain FastAPI/Starlette or
    Redis (plan §3.3, D11). Enforced here at the source level before the split
    makes it enforceable at the dependency level."""

    import ast

    banned = {"fastapi", "starlette", "psycopg", "prometheus_client"}
    offenders: list[str] = []
    for pkg in ("spi", "driver", "extraction", "dom", "egress"):
        for path in _browser_layer_files(pkg):
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
                        offenders.append(f"{path.name}: {n}")
    assert not offenders, offenders


def test_browser_layer_reads_no_ambient_environment() -> None:
    """Leaves take configuration as arguments; only a composition root reads
    the environment (plan D8, Phase 2).

    Three exemptions, all deliberate and all narrow:

    - `driver/process_launcher.py` touches `DISPLAY`, which is the X11
      protocol's own channel rather than application configuration -- Chrome is
      launched as a child process and reads it from the inherited environment,
      so it must genuinely be there.
    - `identity/proxy_config.py` reads inside a `from_env()` constructor called
      once from `gateway.wiring`, which is the sanctioned pattern, not a read
      at the point of use.
    - `driver/browser_discovery.py` reads `PLAYWRIGHT_BROWSERS_PATH` and
      `LOCALAPPDATA`, which are likewise other people's channels, not ours: the
      first is where Playwright itself was told to unpack browsers, the second
      is where Windows puts them. The module's job is to find what another tool
      installed, so it has to look where that tool was told to put it. Its own
      configuration -- `executable_path`, `channel` -- arrives as arguments.

    Anything else is a regression: it makes the library unconfigurable by a
    caller that does not own the process.
    """

    import ast

    allowed = {
        "driver/process_launcher.py",
        "identity/proxy_config.py",
        "driver/browser_discovery.py",
    }
    offenders: list[str] = []
    for pkg in ("spi", "driver", "identity", "egress", "extraction", "dom", "session", "tiers"):
        for path in _browser_layer_files(pkg):
            rel = path.relative_to(CRAWLPILOT_SRC).as_posix()
            if rel in allowed:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"ambient environment reads: {offenders}"


def test_browser_layer_has_no_tenancy_vocabulary() -> None:
    """The browser layer must not know that an identity has a tenant (plan D10).

    Phase 3c made `IdentityRef.key` opaque and moved composition/decomposition
    to `agentpilot.control.identity`. This asserts the vocabulary did not survive
    in any *code* -- an attribute access, parameter, variable or field named for
    a tenant would put a customer concept back into a library whose whole purpose
    is to be usable by callers that have no customers.

    Deliberately AST-based rather than a text grep: the docstrings in
    `spi.identity` and `policy.providers` discuss tenancy at length, explaining
    precisely why it is *absent*, and that prose is worth keeping.

    `control`, `gateway`, `placement`, `jobs` and `auth` are platform code and
    are not scanned -- knowing about tenants is their job.

    Two exemptions, both recording a real finding rather than papering over one:
    `spi/jobs.py::Job.tenant` and `spi/artifact.py::ArtifactRef.tenant` are
    job-queue and artifact-store types that were filed in `spi` but describe
    *platform* concepts, not browsing. They must move out of `contracts/` when
    the packages are reorganised (plan Phase 7); until then they are listed here
    so the exemption is explicit and shrinking.
    """

    exempt = {("jobs.py", "tenant"), ("artifact.py", "tenant")}

    import ast

    offenders: list[str] = []
    for pkg in ("spi", "driver", "identity", "egress", "extraction", "dom",
                "session", "tiers", "config", "policy"):
        for path in _browser_layer_files(pkg):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Attribute):
                    names = [node.attr]
                elif isinstance(node, ast.Name):
                    names = [node.id]
                elif isinstance(node, ast.arg):
                    names = [node.arg]
                elif isinstance(node, ast.keyword) and node.arg:
                    names = [node.arg]
                for name in names:
                    if "tenant" in name.lower():
                        rel = path.relative_to(CRAWLPILOT_SRC)
                        if (path.name, name) in exempt:
                            continue
                        offenders.append(f"{rel}:{node.lineno}: {name}")
    assert not offenders, "tenancy leaked back into the browser layer:\n" + "\n".join(offenders)


def test_the_browser_layer_never_imports_observability() -> None:
    """It imports `prometheus_client`, which the extracted wheel must not carry
    -- and `observability` is used by fourteen platform modules besides, so it
    could not move across with the browser layer either.

    This was a *ratchet* through Phases 4-6, pinning the four offenders so the
    debt could not grow while it waited. Phase 7 paid it: the browser layer now
    emits through `crawlpilot.metrics`, a no-op by default, and the platform
    installs a `PrometheusRecorder` at its composition root. The counters, their
    names and their labels are unchanged.
    """

    import ast

    offenders: list[str] = []
    for pkg in ("spi", "driver", "identity", "egress", "extraction", "dom",
                "session", "tiers", "config", "policy", "extensions", "tools"):
        for path in _browser_layer_files(pkg):
            for node in ast.walk(ast.parse(path.read_text())):
                mod = ""
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                elif isinstance(node, ast.Import):
                    mod = node.names[0].name
                if mod.startswith("agentpilot.observability"):
                    offenders.append(f"{path.relative_to(CRAWLPILOT_SRC)}:{node.lineno}")

    assert not offenders, (
        "browser-layer dependency on observability (and so prometheus): "
        f"{offenders}. Emit through `crawlpilot.metrics` instead."
    )


def test_the_metrics_seam_is_silent_by_default_and_never_raises() -> None:
    """A library consumer that installs no recorder pays nothing, and a metrics
    backend must never be able to fail a crawl."""

    from crawlpilot import metrics

    metrics.set_recorder(None)
    metrics.incr("anything", label="value")  # no-op, no error

    class Exploding:
        def incr(self, name: str, amount: float = 1.0, **labels: str) -> None:
            raise RuntimeError("metrics backend is down")

    metrics.set_recorder(Exploding())
    try:
        metrics.incr("context_tasks_total")  # swallowed
    finally:
        metrics.set_recorder(None)


def test_the_prometheus_recorder_carries_the_same_counters() -> None:
    """The browser layer's counter names must all be routable, or a metric
    silently disappears from the platform's /metrics output."""

    from agentpilot.observability.metrics import PrometheusRecorder

    expected = {
        "context_tasks_total",
        "context_task_outcomes_total",
        "context_leak_warnings_total",
        "context_rotations_total",
        "reaper_destroyed_total",
        "reaper_lease_reclaimed_total",
        "extension_hook_calls_total",
    }
    assert set(PrometheusRecorder._COUNTERS) == expected  # noqa: SLF001
