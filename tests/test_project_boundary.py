"""The rule that keeps `crawlpilot` and `agentpilot` two projects.

import-linter cannot express this: its contracts are rooted at one package, and
it refuses to forbid subpackages of an *external* one -- which `crawlpilot` now
is. So the cross-project boundary is a test, exactly as the plan anticipated.

What it protects: after Phase 7 the two projects build, version and ship
separately. Nothing enforces that at import time -- `agentpilot` could reach
into `crawlpilot.session.warm_pool` and keep working, right up until a
crawlpilot patch release moved it. That is how a split quietly becomes one
codebase in two directories again.
"""

from __future__ import annotations

import ast
import pathlib

import crawlpilot

AGENTPILOT = pathlib.Path(__file__).resolve().parents[1] / "packages/agentpilot/agentpilot"

PUBLIC_SUBMODULES = {
    "crawlpilot.api",
    "crawlpilot.config",
    "crawlpilot.metrics",
    # The HTTP form of `ActionResult`, projected from the dataclass. Published
    # because the platform *and* any client both encode/decode against it -- a
    # shape owned by one side would be the drift this module exists to remove.
    "crawlpilot.wire",
    # The ~60 browser verbs over an abstract `execute`. Published because it is
    # the seam a second transport subclasses -- a remote session inherits the
    # vocabulary rather than restating it.
    "crawlpilot.verbs",
    "crawlpilot.tiers",
    "crawlpilot.policy",
    "crawlpilot.extensions",
    "crawlpilot.tools",
    "crawlpilot.spi",
    "crawlpilot.extraction",
    "crawlpilot.dom",
    "crawlpilot.session",
    "crawlpilot.identity",
    "crawlpilot.egress",
}
"""Package prefixes agentpilot may import from.

`crawlpilot.driver` is deliberately absent: only the composition root
constructs a driver, and it reaches it through the facade.

The rule is **prefix-level today**, so `crawlpilot.spi.scrape` passes because
`crawlpilot.spi` is published. That is the honest state at 0.1: these packages
are the supported surface, and what it catches is an import of an *unlisted*
one -- which is the actual failure mode, since that is how the split turns back
into one codebase in two directories. Tightening this to `crawlpilot.__all__`
only means giving each package a curated `__init__`, and is worth doing before
the first external consumer."""

COMPOSITION_ROOTS = {"gateway/wiring.py"}
"""The only files allowed to name the concrete driver."""


def _crawlpilot_imports() -> list[tuple[pathlib.Path, int, str]]:
    found: list[tuple[pathlib.Path, int, str]] = []
    for path in AGENTPILOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("crawlpilot"):
                found.append((path, node.lineno, node.module or ""))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("crawlpilot"):
                        found.append((path, node.lineno, alias.name))
    return found


def test_the_boundary_is_actually_exercised() -> None:
    """A guard that finds nothing proves nothing -- and after the Phase 7 move
    several path-based guards silently started passing on empty directories."""

    assert AGENTPILOT.is_dir(), AGENTPILOT
    assert len(_crawlpilot_imports()) > 20


def test_agentpilot_imports_only_published_crawlpilot_modules() -> None:
    offenders = [
        f"{path.relative_to(AGENTPILOT)}:{lineno}: {module}"
        for path, lineno, module in _crawlpilot_imports()
        if module != "crawlpilot"
        and not any(module == pub or module.startswith(f"{pub}.") for pub in PUBLIC_SUBMODULES)
        # The concrete driver has its own, stricter rule below: a composition
        # root may name it, nothing else may. Exempting it here keeps the two
        # tests from disagreeing about the same import.
        and not (
            module.startswith("crawlpilot.driver")
            and path.relative_to(AGENTPILOT).as_posix() in COMPOSITION_ROOTS
        )
    ]
    assert not offenders, (
        "agentpilot reached past crawlpilot's published API:\n  " + "\n  ".join(offenders)
    )


def test_only_the_composition_root_names_the_concrete_driver() -> None:
    offenders = [
        f"{path.relative_to(AGENTPILOT)}:{lineno}"
        for path, lineno, module in _crawlpilot_imports()
        if module.startswith("crawlpilot.driver")
        and path.relative_to(AGENTPILOT).as_posix() not in COMPOSITION_ROOTS
    ]
    assert not offenders, offenders


def test_the_public_api_is_importable_and_complete() -> None:
    for name in crawlpilot.__all__:
        assert hasattr(crawlpilot, name), f"crawlpilot.__all__ names a missing {name!r}"


def test_crawlpilot_does_not_import_agentpilot() -> None:
    """The other direction, which would be worse: a library depending on the
    application built on it."""

    src = pathlib.Path(crawlpilot.__file__).parent
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            mod = ""
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            if mod.startswith("agentpilot"):
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    assert not offenders, offenders
