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
from agentpilot.recipe.evaluate import evaluate_field_locator, fetch_structured_data
from agentpilot.recipe.models import FieldGroup, Recipe, RecipeRunResult, RevealStep
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver


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
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> tuple[dict[str, Any], dict[str, str]]:
    data: dict[str, Any] = {}
    failures: dict[str, str] = {}
    structured_data = await fetch_structured_data(session, registry=registry, driver=driver)
    for name, locator in group.field_locators.items():
        value = await evaluate_field_locator(
            locator,
            structured_data=structured_data,
            session=session,
            registry=registry,
            driver=driver,
        )
        if value is None:
            failures[name] = f"locator (source={locator.source}) resolved to no value"
        else:
            data[name] = value
    return data, failures


async def _replay_repeat_group(
    group: FieldGroup,
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
        for name, locator in group.field_locators.items():
            row[name] = await evaluate_field_locator(
                locator,
                structured_data=structured_data,
                session=session,
                registry=registry,
                driver=driver,
            )
        if all(v is not None for v in row.values()):
            rows.append(row)

    if not rows:
        return {}, {repeat.array_field: "no option iteration produced a complete row"}
    return {repeat.array_field: rows}, {}


async def _replay_group(
    group: FieldGroup,
    recipe: Recipe,
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
            group, session=session, registry=registry, driver=driver
        )
    return await _replay_repeat_group(group, session=session, registry=registry, driver=driver)


async def replay_recipe(
    recipe: Recipe,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> RecipeRunResult:
    data: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for group in recipe.field_groups:
        group_data, group_failures = await _replay_group(
            group, recipe, session=session, registry=registry, driver=driver
        )
        data.update(group_data)
        failures.update(group_failures)
    return RecipeRunResult(success=not failures, data=data, field_failures=failures)
