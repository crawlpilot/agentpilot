"""The architectural guarantees the browser layer makes about itself.

This file used to open with a 90-entry `PUBLIC_SURFACE` table naming every
symbol that had to survive the `crawlpilot` extraction, written when "none of
these symbols is re-exported from a package `__init__`" and so nothing else
could catch a rename. That premise is spent: `crawlpilot/__init__.py` now
curates a real `__all__`, and `test_project_boundary.py` enforces which
submodules agentpilot may reach for. A hand-maintained inventory of names that
are now exported *by name* only records them twice, and the second copy is the
one that goes stale. `test_public_api_is_a_curated_export` replaces it.

Two guards also left with it, having become duplicates of the import-linter
contract `crawlpilot never imports the platform` (see
`packages/crawlpilot/pyproject.toml`), which forbids `agentpilot`, `fastapi`,
`starlette`, `psycopg`, `prometheus_client` and `redis` across the *whole*
package rather than the hand-listed subset these two walked:

- `test_browser_layer_carries_no_web_framework_import`
- `test_the_browser_layer_never_imports_observability`

What remains is what nothing else checks.
"""

from __future__ import annotations

import ast
import importlib
import importlib.resources
import pathlib

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


EXPECTED_PUBLIC_API = {
    "BlockHooks", "BlockMount", "Browser", "BrowserConfig", "BrowserSession",
    "BrowseHooks", "BrowseMount", "ChallengeDetected", "ContentHooks",
    "ContentMount", "ContextCrashed", "Document", "DriverError", "EgressConfig",
    "Extension", "ExtensionManifest", "ExtensionRegistry", "FingerprintConfig",
    "IdentityRef", "InMemoryStateStore", "NavigationTimeout", "NullPrototypes",
    "ProfileConfig", "ProfileKind", "PrototypeProvider", "ProxyHealthConfig",
    "ProxyProvider", "Resolution", "ScrapeOptions", "StateStore",
    "StaticProxies", "Tier", "TierPolicy", "ToolMount", "ToolRegistry",
    "ToolSpec", "__version__", "browser_tools",
}
"""What `from crawlpilot import *` gives a consumer.

Adding to this is cheap and expected as the API grows. **Removing or renaming
one is a breaking change for every installed consumer** and must be a
deliberate edit here in the same commit, not silent drift -- the same
maintenance contract the old inventory carried, over the set that is now
actually authoritative."""


def test_public_api_is_a_curated_export() -> None:
    import crawlpilot

    assert set(crawlpilot.__all__) == EXPECTED_PUBLIC_API


def test_every_exported_name_actually_resolves() -> None:
    """`__all__` is just a list of strings: a name can be listed and not exist,
    and the only caller who finds out is the one doing `import *`."""

    import crawlpilot

    missing = [name for name in crawlpilot.__all__ if not hasattr(crawlpilot, name)]
    assert not missing, f"listed in __all__ but not importable: {missing}"


def test_package_ships_a_py_typed_marker() -> None:
    """A distribution without `py.typed` gives its consumers no types at all
    (PEP 561), which for a library whose whole value is a typed seam would be
    a silent regression."""

    import crawlpilot

    root = importlib.resources.files(crawlpilot)
    assert (root / "py.typed").is_file()


def test_browser_layer_reads_no_ambient_environment() -> None:
    """Leaves take configuration as arguments; only a composition root reads
    the environment (plan D8, Phase 2).

    Two exemptions, both deliberate and both narrow:

    - `driver/process_launcher.py` touches `DISPLAY`, which is the X11
      protocol's own channel rather than application configuration -- Chrome is
      launched as a child process and reads it from the inherited environment,
      so it must genuinely be there.
    - `driver/browser_discovery.py` reads `PLAYWRIGHT_BROWSERS_PATH` and
      `LOCALAPPDATA`, which are likewise other people's channels, not ours: the
      first is where Playwright itself was told to unpack browsers, the second
      is where Windows puts them. The module's job is to find what another tool
      installed, so it has to look where that tool was told to put it. Its own
      configuration -- `executable_path`, `channel` -- arrives as arguments.

    A third, `identity/proxy_config.py`, is gone: that module moved to
    `agentpilot.control` with the tenant-keyed proxy table, so the exemption
    retired with it.

    Anything else is a regression: it makes the library unconfigurable by a
    caller that does not own the process.
    """

    allowed = {
        "driver/process_launcher.py",
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

    **The exemption list is now empty**, which is the point. It used to carry
    `spi/jobs.py::Job.tenant` and `spi/artifact.py::ArtifactRef.tenant`, recorded
    as a real finding: two platform types filed in `spi` that had to move out
    when the packages were reorganised. Both have -- `jobs.py` to
    `agentpilot.jobs.types`, and `artifact.py` deleted outright as a type nothing
    ever produced -- so the debt is paid and the guard is absolute.
    """

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
                        offenders.append(f"{rel}:{node.lineno}: {name}")
    assert not offenders, "tenancy leaked back into the browser layer:\n" + "\n".join(offenders)


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
