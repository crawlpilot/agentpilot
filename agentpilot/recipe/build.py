"""Stage 1 (+2): schema-driven guided exploration with incremental,
interleaved locator capture -- the recipe-building half of the pipeline.
Reuses `agentpilot.agent.loop.run_agent_loop` as-is for the actual browser
exploration; everything recipe-specific happens in the `on_step` hook that
function already exposes.

Verification runs on EVERY step, not as a single end-of-run pass: after each
dispatched action, this module takes its own fresh snapshot + structured-data
read, proposes and mechanically verifies a locator for every not-yet-found
field, and -- the moment a field's value first resolves -- freezes its
`FieldLocator` together with every reveal action taken since the previous
freeze (translated into re-resolvable `RevealStep`s via `stabilize.py`, never
raw refs). This is why capture happens *at the moment* a field becomes
visible, while the page state that revealed it is still live, rather than
being reconstructed later from an undifferentiated action trace.

The batch of reveal steps captured before the very first field is satisfied
becomes `Recipe.global_setup` (dismiss a cookie banner, wait for initial
load, etc.) -- `replay.py` re-navigates fresh before every field group, so
`global_setup` must run before each one; everything captured after the first
freeze is scoped to its own group only. Cross-group dependencies beyond that
shared setup (a later group needing state a *different* group's reveal path
left behind) are out of scope for this v1 -- `heal.py` is the mechanism that
corrects a group whose assumptions turn out to be wrong.
"""

from __future__ import annotations

import uuid
from typing import Any

from agentpilot.agent.dom_view import render_snapshot_for_llm
from agentpilot.agent.loop import run_agent_loop
from agentpilot.agent.state import AgentStepRecord
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.evaluate import fetch_structured_data
from agentpilot.recipe.generalize import generalize_option_locator, single_option_fallback
from agentpilot.recipe.locator_proposal import propose_and_verify_fields
from agentpilot.recipe.models import FieldGroup, FieldLocator, Recipe, RecipeRunResult, RevealStep
from agentpilot.recipe.schema import (
    FieldSpec,
    all_leaf_fields,
    leaf_to_array_map,
    parse_schema,
    render_schema_for_prompt,
)
from agentpilot.recipe.stabilize import stabilize_action_dict
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot

DEFAULT_BUILD_MAX_STEPS = 15


def build_exploration_task(url: str, schema: dict[str, FieldSpec]) -> str:
    return (
        f"Navigate to {url}. The following fields must each be located "
        f"somewhere on the page (directly visible, or reachable via one "
        f"more interaction):\n{render_schema_for_prompt(schema)}\n\n"
        "Dismiss any cookie/consent dialog first if one appears. For each "
        "field, interact with whatever is needed to reveal it (open an "
        "accordion/tab, click a toggle, scroll to load more). For an "
        "'array' field, interact with ONE representative option only (e.g. "
        "click a single size or color) -- do not click through every "
        "option. Call done once you believe every field has been located "
        "at least once, or if you're stuck."
    )


def _last_click_ref(actions: list[dict[str, Any]]) -> str | None:
    for action_dict in reversed(actions):
        if action_dict.get("type") == "ClickAction":
            ref = action_dict.get("ref")
            if isinstance(ref, str):
                return ref
    return None


class ExplorationState:
    def __init__(
        self,
        *,
        leaf_fields: dict[str, FieldSpec],
        leaf_to_array: dict[str, str],
        session: InteractiveSession,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        llm_config: LLMConfig,
        config: RecipeConfig,
    ) -> None:
        self._unfound = dict(leaf_fields)
        self._leaf_to_array = leaf_to_array
        self._session = session
        self._registry = registry
        self._driver = driver
        self._llm_config = llm_config
        self._config = config

        self._last_snapshot: AXSnapshot | None = None
        self._pending_reveal_steps: list[RevealStep] = []
        self._global_setup_captured = False

        self.global_setup: list[RevealStep] = []
        self.field_groups: list[FieldGroup] = []

    @property
    def unfound_fields(self) -> dict[str, FieldSpec]:
        return self._unfound

    async def on_step(self, step_record: AgentStepRecord) -> None:
        if not self._unfound:
            return

        if self._last_snapshot is not None:
            for action_dict in step_record.actions:
                stabilized = stabilize_action_dict(action_dict, self._last_snapshot)
                if stabilized is not None:
                    self._pending_reveal_steps.append(stabilized)

        last_click_ref = _last_click_ref(step_record.actions)

        result = await execute_on_session(
            self._session,
            [spi_actions.SnapshotAction()],
            registry=self._registry,
            driver=self._driver,
        )
        snapshot = result.snapshots[0] if result.snapshots else None
        if snapshot is None:
            return
        self._last_snapshot = snapshot

        structured_data = await fetch_structured_data(
            self._session, registry=self._registry, driver=self._driver
        )
        snapshot_text = render_snapshot_for_llm(snapshot)

        verified = await propose_and_verify_fields(
            self._unfound,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            snapshot=snapshot,
            session=self._session,
            registry=self._registry,
            driver=self._driver,
            llm_config=self._llm_config,
            max_retries=self._config.field_verify_max_retries,
        )
        if not verified:
            return

        self._freeze_groups(verified, snapshot=snapshot, last_click_ref=last_click_ref)
        for name in verified:
            del self._unfound[name]

    def _freeze_groups(
        self,
        verified: dict[str, list[FieldLocator]],
        *,
        snapshot: AXSnapshot,
        last_click_ref: str | None,
    ) -> None:
        by_array: dict[str, dict[str, list[FieldLocator]]] = {}
        scalar: dict[str, list[FieldLocator]] = {}
        for name, locators in verified.items():
            array_name = self._leaf_to_array.get(name)
            if array_name:
                by_array.setdefault(array_name, {})[name] = locators
            else:
                scalar[name] = locators

        # If an array field is satisfied THIS call, the batch's trailing
        # click is the representative-option click -- superseded by that
        # group's own RepeatSpec below, so it must be stripped from whatever
        # container (global_setup on the very first freeze, or this call's
        # shared reveal_steps otherwise) it would otherwise land in. Stripped
        # ONCE, upfront, shared by every group frozen this call -- a scalar
        # field satisfied in the exact same batch as an array field's
        # representative click (rare) would then miss that click in its own
        # reveal path; an accepted v1 limitation, not silently mishandled.
        steps = list(self._pending_reveal_steps)
        if by_array and steps and steps[-1].action == "click":
            steps = steps[:-1]

        if not self._global_setup_captured:
            self.global_setup = steps
            reveal_steps: list[RevealStep] = []
        else:
            reveal_steps = steps

        if scalar:
            self.field_groups.append(
                FieldGroup(
                    group_id=f"group-{len(self.field_groups)}-{uuid.uuid4().hex[:6]}",
                    field_names=list(scalar.keys()),
                    reveal_steps=list(reveal_steps),
                    field_locators=scalar,
                )
            )

        for array_name, locators in by_array.items():
            repeat = None
            if last_click_ref is not None:
                repeat = generalize_option_locator(
                    snapshot=snapshot,
                    clicked_ref=last_click_ref,
                    array_field=array_name,
                    max_iterations=self._config.max_repeat_iterations,
                ) or single_option_fallback(
                    clicked_ref=last_click_ref, snapshot=snapshot, array_field=array_name
                )

            self.field_groups.append(
                FieldGroup(
                    group_id=f"group-{len(self.field_groups)}-{uuid.uuid4().hex[:6]}",
                    field_names=list(locators.keys()),
                    reveal_steps=list(reveal_steps),
                    field_locators=locators,
                    repeat=repeat,
                )
            )

        self._pending_reveal_steps = []
        self._global_setup_captured = True


async def build_recipe(
    *,
    recipe_id: str,
    tenant: str,
    name: str,
    url: str,
    raw_schema: dict[str, Any],
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    config: RecipeConfig | None = None,
    max_steps: int = DEFAULT_BUILD_MAX_STEPS,
) -> tuple[Recipe, RecipeRunResult]:
    cfg = config or RecipeConfig.from_env()
    schema = parse_schema(raw_schema)
    leaf_fields = all_leaf_fields(schema)
    leaf_to_array = leaf_to_array_map(schema)

    state = ExplorationState(
        leaf_fields=leaf_fields,
        leaf_to_array=leaf_to_array,
        session=session,
        registry=registry,
        driver=driver,
        llm_config=llm_config,
        config=cfg,
    )

    run_result = await run_agent_loop(
        task=build_exploration_task(url, schema),
        session=session,
        registry=registry,
        driver=driver,
        llm_config=llm_config,
        max_steps=max_steps,
        output_schema=None,
        on_step=state.on_step,
    )

    recipe = Recipe(
        recipe_id=recipe_id,
        tenant=tenant,
        name=name,
        url_pattern=url,
        field_schema=raw_schema,
        version=1,
        global_setup=state.global_setup,
        field_groups=state.field_groups,
        health_status="healthy" if not state.unfound_fields else "degraded",
    )

    found_fields = {name for group in state.field_groups for name in group.field_names}
    field_failures = {
        name: "not located during exploration" for name in state.unfound_fields
    }
    result = RecipeRunResult(
        success=not state.unfound_fields,
        data={name: None for name in found_fields},  # values read at replay time, not stored here
        field_failures=field_failures,
        error=None if run_result.success or not state.unfound_fields else run_result.result,
    )
    return recipe, result
