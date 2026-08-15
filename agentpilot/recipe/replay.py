"""Stage 3: deterministic recipe replay -- no LLM call, ever.

Re-navigates fresh before EVERY field group (not once per whole replay):
`build.py` only guarantees `global_setup` as a prefix every group can rely
on, not state left behind by a *previously replayed* group in the same run
-- re-navigating avoids exactly the double-dismiss-a-cookie-banner failure
mode a single continuous session would hit on a second group's copy of the
same reveal step.
"""

from __future__ import annotations

from typing import Any

from agentpilot.recipe.dispatch import (
    LocatorResolutionError,
    dispatch_reveal_step,
    resolve_ax_role_refs,
)
from agentpilot.recipe.evaluate import evaluate_field_locators, fetch_structured_data
from agentpilot.recipe.models import FieldGroup, FieldLocator, Recipe, RecipeRunResult, RevealStep
from agentpilot.recipe.normalize import normalize_value
from agentpilot.recipe.schema import FieldNormalization, all_leaf_fields, parse_schema
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver

NormalizationMap = dict[str, FieldNormalization]


def _normalization_map(recipe: Recipe) -> NormalizationMap:
    """Per-leaf-field normalization directives, parsed once from the recipe's
    stored `field_schema` (scalar fields + array item sub-fields)."""

    leaves = all_leaf_fields(parse_schema(recipe.field_schema))
    return {name: spec.normalization for name, spec in leaves.items()}


def _read_field(
    name: str,
    candidates: list[FieldLocator],
    raw: Any,
    normalization: NormalizationMap,
    base_url: str,
    out: dict[str, Any],
) -> str | None:
    """Normalize a field's resolved `raw` value into `out`, emitting an
    optional `<name>_raw`. Returns a failure reason string, or `None` on
    success -- a `required` field that normalizes empty is a failure (Pulsar's
    `isQualified` quality gate)."""

    spec = normalization.get(name, FieldNormalization())
    value, ok = normalize_value(raw, spec, base_url=base_url)
    if not ok:
        return "required field resolved to no value"
    if raw is None and value is None:
        return f"locators ({len(candidates)} candidate(s)) resolved to no value"
    out[name] = value
    if spec.emit_raw and raw is not None:
        out[f"{name}_raw"] = raw
    return None


async def _dispatch_steps(
    steps: list[RevealStep],
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> None:
    for step in steps:
        await dispatch_reveal_step(step, session=session, registry=registry, driver=driver)


async def _replay_scalar_group(
    group: FieldGroup,
    normalization: NormalizationMap,
    base_url: str,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> tuple[dict[str, Any], dict[str, str]]:
    data: dict[str, Any] = {}
    failures: dict[str, str] = {}
    structured_data = await fetch_structured_data(session, registry=registry, driver=driver)
    for name, candidates in group.field_locators.items():
        raw = await evaluate_field_locators(
            candidates,
            structured_data=structured_data,
            session=session,
            registry=registry,
            driver=driver,
        )
        reason = _read_field(name, candidates, raw, normalization, base_url, data)
        if reason is not None:
            failures[name] = reason
    return data, failures


async def _replay_repeat_group(
    group: FieldGroup,
    normalization: NormalizationMap,
    base_url: str,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> tuple[dict[str, Any], dict[str, str]]:
    assert group.repeat is not None
    repeat = group.repeat
    initial_refs = await resolve_ax_role_refs(
        repeat.option_locator, session=session, registry=registry, driver=driver
    )
    if not initial_refs:
        return {}, {repeat.array_field: "option_locator matched no elements"}

    n = min(len(initial_refs), repeat.max_iterations)
    rows: list[dict[str, Any]] = []
    for i in range(n):
        # Re-resolve fresh before every click -- a prior click in this same
        # loop may have re-rendered the option set, invalidating earlier refs.
        refs = await resolve_ax_role_refs(
            repeat.option_locator, session=session, registry=registry, driver=driver
        )
        if i >= len(refs):
            break
        await execute_on_session(
            session, [spi_actions.ClickAction(ref=refs[i])], registry=registry, driver=driver
        )
        structured_data = await fetch_structured_data(session, registry=registry, driver=driver)
        row: dict[str, Any] = {}
        complete = True
        for name, candidates in group.field_locators.items():
            raw = await evaluate_field_locators(
                candidates,
                structured_data=structured_data,
                session=session,
                registry=registry,
                driver=driver,
            )
            if _read_field(name, candidates, raw, normalization, base_url, row) is not None:
                complete = False
        if complete:
            rows.append(row)

    if not rows:
        return {}, {repeat.array_field: "no option iteration produced a complete row"}
    return {repeat.array_field: rows}, {}


async def _replay_group(
    group: FieldGroup,
    recipe: Recipe,
    normalization: NormalizationMap,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> tuple[dict[str, Any], dict[str, str]]:
    await execute_on_session(
        session,
        [spi_actions.NavigateAction(url=recipe.url_pattern)],
        registry=registry,
        driver=driver,
    )
    try:
        await _dispatch_steps(
            recipe.global_setup, session=session, registry=registry, driver=driver
        )
        await _dispatch_steps(
            group.reveal_steps, session=session, registry=registry, driver=driver
        )
    except LocatorResolutionError as exc:
        return {}, {name: f"reveal step failed: {exc}" for name in group.field_names}

    if group.repeat is None:
        return await _replay_scalar_group(
            group, normalization, recipe.url_pattern,
            session=session, registry=registry, driver=driver,
        )
    return await _replay_repeat_group(
        group, normalization, recipe.url_pattern,
        session=session, registry=registry, driver=driver,
    )


async def replay_recipe(
    recipe: Recipe,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> RecipeRunResult:
    normalization = _normalization_map(recipe)
    data: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for group in recipe.field_groups:
        group_data, group_failures = await _replay_group(
            group, recipe, normalization, session=session, registry=registry, driver=driver
        )
        data.update(group_data)
        failures.update(group_failures)
    return RecipeRunResult(success=not failures, data=data, field_failures=failures)
