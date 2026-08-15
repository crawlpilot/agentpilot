"""Mechanically evaluates a `FieldLocator` against the current live page
state -- no LLM. Shared by `build.py`'s interleaved per-step verify,
`replay.py`'s per-run field extraction, and `heal.py`'s health check.
"""

from __future__ import annotations

import json
from typing import Any

from agentpilot.recipe.jsonpath import resolve_path
from agentpilot.recipe.models import FieldLocator
from agentpilot.recipe.tree import find_matches
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot

_SOURCE_TO_CONTAINER = {"json_ld": "json_ld", "hydration": "hydration", "meta": "metadata"}


async def fetch_structured_data(
    session: InteractiveSession, *, registry: RegistryProtocol, driver: BrowserDriver
) -> dict[str, Any]:
    """One `ExtractAction(format="structured_data")` dispatch -- already
    does the static JSON-LD/meta/hydration parse plus, when that finds no
    hydration script, the live `_probe_hydration_globals()` JS-eval fallback
    for client-only SPA state (`extraction/extractor.py`'s `extract()`).
    Reused as-is rather than reimplemented here."""

    result = await execute_on_session(
        session,
        [spi_actions.ExtractAction(format="structured_data")],
        registry=registry,
        driver=driver,
    )
    if not result.extracts:
        return {"metadata": {}, "json_ld": [], "hydration": {}}
    return dict(json.loads(result.extracts[0]))


def find_ax_role_refs(
    snapshot: AXSnapshot,
    role: str,
    name_contains: str | None = None,
    name_in: list[str] | None = None,
) -> list[str]:
    """Every CURRENT ref matching role+name (document order) -- used both to
    resolve a single `ax_role` `FieldLocator`/`RevealStep.locator` and, by
    `generalize.py`, to discover a `RepeatSpec.option_locator`'s full match
    set."""

    return [n.ref for n in find_matches(snapshot.root, role, name_contains, name_in)]


def _css_read_script(selector: str, attribute: str) -> str:
    sel = json.dumps(selector)
    expr = (
        "el.textContent ? el.textContent.trim() : null"
        if attribute == "text"
        else f"el.getAttribute({json.dumps(attribute)})"
    )
    return (
        f"() => {{ const el = document.querySelector({sel}); "
        f"if (!el) return null; return {expr}; }}"
    )


async def evaluate_field_locator(
    locator: FieldLocator,
    *,
    structured_data: dict[str, Any] | None,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    snapshot: AXSnapshot | None = None,
) -> Any:
    """Returns the resolved value, or `None` if it can't currently be
    resolved -- callers (verify/replay/heal) decide whether that's a
    field/group failure."""

    if locator.source in _SOURCE_TO_CONTAINER:
        if structured_data is None:
            structured_data = await fetch_structured_data(session, registry=registry, driver=driver)
        container = structured_data.get(_SOURCE_TO_CONTAINER[locator.source])
        return resolve_path(container, locator.path or "")

    if locator.source == "css":
        script = _css_read_script(locator.selector or "", locator.attribute or "text")
        result = await execute_on_session(
            session, [spi_actions.ExecuteJsAction(script=script)], registry=registry, driver=driver
        )
        return result.js_returns[0] if result.js_returns else None

    if locator.source == "ax_role":
        snap = snapshot
        if snap is None:
            result = await execute_on_session(
                session, [spi_actions.SnapshotAction()], registry=registry, driver=driver
            )
            snap = result.snapshots[0] if result.snapshots else None
        if snap is None:
            return None
        matches = find_matches(snap.root, locator.role or "", locator.name_contains)
        return matches[0].name if matches else None

    return None


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


async def evaluate_field_locators(
    candidates: list[FieldLocator],
    *,
    structured_data: dict[str, Any] | None,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    snapshot: AXSnapshot | None = None,
) -> Any:
    """Try each candidate in order, returning the first non-empty resolution
    -- Pulsar's coalesce/comma-union idiom (`array_first_not_blank(make_array(
    a, b))`). Candidates are stored most-preferred-first (json_ld/hydration/
    meta ahead of css/ax_role, stable-attribute CSS ahead of positional), so a
    broken primary selector transparently falls back to the next without an
    LLM. Returns `None` only if every candidate resolves empty."""

    for locator in candidates:
        value = await evaluate_field_locator(
            locator,
            structured_data=structured_data,
            session=session,
            registry=registry,
            driver=driver,
            snapshot=snapshot,
        )
        if not _is_empty(value):
            return value
    return None
