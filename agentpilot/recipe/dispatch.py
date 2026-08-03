"""Re-resolves a stored `RevealStep`/`RepeatSpec.option_locator` against the
CURRENT live page and dispatches it -- the deterministic replay counterpart
of however the step was originally captured live during exploration. Never
uses a stored `ref` (see `stabilize.py`'s docstring for why).
"""

from __future__ import annotations

from agentpilot.recipe.models import Locator, RevealStep
from agentpilot.recipe.tree import find_matches
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot


class LocatorResolutionError(Exception):
    """A stored locator no longer resolves against the current page --
    callers (`replay.py`/`heal.py`) treat this as a field/group failure, not
    a crashed run."""


async def _fresh_snapshot(
    *, session: InteractiveSession, registry: RegistryProtocol, driver: BrowserDriver
) -> AXSnapshot:
    result = await execute_on_session(
        session, [spi_actions.SnapshotAction()], registry=registry, driver=driver
    )
    if not result.snapshots:
        raise LocatorResolutionError("no snapshot available")
    return result.snapshots[0]


async def resolve_ax_role_refs(
    locator: Locator,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    snapshot: AXSnapshot | None = None,
) -> list[str]:
    """Every CURRENT ref matching `locator` (ax_role source only), in
    document order."""

    snap = snapshot or await _fresh_snapshot(session=session, registry=registry, driver=driver)
    return find_matches_to_refs(locator, snap)


def find_matches_to_refs(locator: Locator, snapshot: AXSnapshot) -> list[str]:
    matches = find_matches(
        snapshot.root, locator.role or "", locator.name_contains, locator.name_in
    )
    return [n.ref for n in matches]


async def dispatch_reveal_step(
    step: RevealStep,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> None:
    """Dispatches one `RevealStep`, re-resolving its locator (if any) against
    the current live page first. Raises `LocatorResolutionError` if an
    expected locator can't be resolved -- callers decide whether that's a
    field/group failure worth continuing past."""

    ref = await _resolve_step_locator(step, session=session, registry=registry, driver=driver)
    action = _build_action(step, ref)
    await execute_on_session(session, [action], registry=registry, driver=driver)


async def _resolve_step_locator(
    step: RevealStep,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> str | None:
    if step.locator is None:
        return None
    if step.locator.source == "css":
        return None  # handled entirely inside _build_action's ExecuteJsAction
    refs = await resolve_ax_role_refs(
        step.locator, session=session, registry=registry, driver=driver
    )
    if not refs:
        raise LocatorResolutionError(
            f"locator (role={step.locator.role!r}, name_contains={step.locator.name_contains!r}, "
            f"name_in={step.locator.name_in!r}) matched no element"
        )
    return refs[0]


def _build_action(step: RevealStep, ref: str | None) -> spi_actions.Action:
    if step.locator is not None and step.locator.source == "css":
        return _css_action(step)
    if step.action == "click":
        return spi_actions.ClickAction(ref=ref or "")
    if step.action == "fill":
        return spi_actions.FillAction(ref=ref or "", text=step.value or "")
    if step.action == "hover":
        return spi_actions.HoverAction(ref=ref or "")
    if step.action == "select_option":
        return spi_actions.SelectOptionAction(ref=ref or "", values=(step.value or "").split(","))
    if step.action == "scroll":
        return spi_actions.ScrollAction(direction="down", ref=ref)
    if step.action == "wait":
        return spi_actions.WaitAction(ms=int(step.value) if step.value else None)
    if step.action == "press":
        return spi_actions.PressAction(key=step.value or "")
    raise AssertionError(f"unhandled reveal step action: {step.action!r}")


def _css_action(step: RevealStep) -> spi_actions.ExecuteJsAction:
    import json as _json

    selector = _json.dumps(step.locator.selector if step.locator else "")
    if step.action == "click":
        script = f"() => {{ const el = document.querySelector({selector}); if (el) el.click(); }}"
    elif step.action == "fill":
        text = _json.dumps(step.value or "")
        script = (
            f"() => {{ const el = document.querySelector({selector}); "
            f"if (el) {{ el.value = {text}; "
            "el.dispatchEvent(new Event('input', {bubbles: true})); } }"
        )
    elif step.action == "hover":
        script = (
            f"() => {{ const el = document.querySelector({selector}); "
            "if (el) el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); }"
        )
    else:
        script = f"() => {{ const el = document.querySelector({selector}); return !!el; }}"
    return spi_actions.ExecuteJsAction(script=script)
