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
import importlib
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

This is the first of three rules, and the coarsest -- it catches an import of an
*unlisted* package, which is how a split turns back into one codebase in two
directories. `test_no_private_names_cross_the_boundary` and
`test_imported_names_are_in_their_modules_all` narrow it from there."""

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


def _imported_names() -> list[tuple[pathlib.Path, int, str, str]]:
    """`(file, line, module, name)` for every `from crawlpilot.x import name`.

    The name is what the two narrower rules below need and what the module-level
    walk above discards.
    """

    found: list[tuple[pathlib.Path, int, str, str]] = []
    for path in AGENTPILOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("crawlpilot"):
                for alias in node.names:
                    found.append((path, node.lineno, node.module or "", alias.name))
    return found


def test_the_boundary_is_actually_exercised() -> None:
    """A guard that finds nothing proves nothing -- and after the Phase 7 move
    several path-based guards silently started passing on empty directories.

    The floor is near the real count rather than a token `> 0`, so a refactor
    that empties the walk fails here instead of quietly passing every rule.
    """

    assert AGENTPILOT.is_dir(), AGENTPILOT
    assert len(_crawlpilot_imports()) > 60
    assert len(_imported_names()) > 90


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


def test_no_private_names_cross_the_boundary() -> None:
    """The second rule, and the one that caught a real defect.

    A module can be published while a name inside it is not. `routes/sessions.py`
    imported `crawlpilot.session.reaper._read_pid_rss_mb` -- through the
    underscore -- and the prefix rule had nothing to say about it, because
    `crawlpilot.session` is published. A leading underscore is the author saying
    "this may move without notice"; reaching through one across a distribution
    boundary is how a patch release breaks a consumer.

    The fix is never to rename the caller's import. It is to decide: either the
    name is part of what the module offers, and loses its underscore (which is
    what happened -- its sibling `read_meminfo_used_pct` was already public), or
    the caller needs a different answer.
    """

    offenders: list[str] = []
    for path, lineno, module, name in _imported_names():
        if not name.startswith("_"):
            continue
        # An explicit `__all__` is the author saying what is public, and it wins
        # over the naming convention -- `crawlpilot.__version__` is a dunder and
        # is exported deliberately. Without this the rule would forbid the one
        # underscore-prefixed name the library most obviously means to publish.
        try:
            published = getattr(importlib.import_module(module), "__all__", ())
        except Exception:
            published = ()
        if name in published:
            continue
        offenders.append(f"{path.relative_to(AGENTPILOT)}:{lineno}: {module}.{name}")

    assert not offenders, (
        "agentpilot imported a private name from crawlpilot:\n  " + "\n  ".join(offenders)
    )


def test_imported_names_are_in_their_modules_all() -> None:
    """The third rule: where a module curates an `__all__`, honour it.

    Applied only to modules that actually declare one, which makes it a ratchet
    rather than a cliff -- adding `__all__` to a crawlpilot module tightens the
    boundary around it, and no module is forced to declare one before its
    surface has settled. `spi`, `session`, `dom`, `identity`, `egress`,
    `extraction`, `policy`, `tiers`, `extensions`, `tools` and `wire` declare
    theirs today.

    A module that fails to import is skipped rather than failing: this suite runs
    against installs with and without the driver extras, and an absent optional
    dependency is not a boundary violation.
    """

    offenders: list[str] = []
    for path, lineno, module, name in _imported_names():
        try:
            imported = importlib.import_module(module)
        except Exception:
            continue
        published = getattr(imported, "__all__", None)
        if published is None or name in published:
            continue
        offenders.append(f"{path.relative_to(AGENTPILOT)}:{lineno}: {module}.{name}")

    assert not offenders, (
        "agentpilot imported a name its module does not publish in __all__:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither add it to that module's __all__ (if it is meant to be part "
        "of the surface) or stop depending on it."
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
