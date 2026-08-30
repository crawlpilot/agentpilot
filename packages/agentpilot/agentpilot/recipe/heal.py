"""Stage 4: health check + self-heal. Replays the recipe (no LLM); if any
field failed (or a `RepeatSpec.option_locator` now resolves to nothing), the
affected `FieldGroup`s are dropped and their fields re-explored -- scoped to
just those fields, not the whole page again -- via the same
`ExplorationState`/`run_agent_loop` machinery `build.py` uses. Unaffected
groups are carried over unchanged into a new recipe version.
"""

from __future__ import annotations

from agentpilot.agent.loop import run_agent_loop
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.build import (
    DEFAULT_BUILD_MAX_STEPS,
    ExplorationState,
    build_exploration_task,
)
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.models import Recipe, RecipeRunResult
from agentpilot.recipe.replay import replay_recipe
from agentpilot.recipe.schema import all_leaf_fields, leaf_to_array_map, parse_schema
from agentpilot.session.interactive import InteractiveSession
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.driver import BrowserDriver


async def check_and_heal(
    recipe: Recipe,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    config: RecipeConfig | None = None,
    max_steps: int = DEFAULT_BUILD_MAX_STEPS,
) -> tuple[Recipe, RecipeRunResult]:
    """Returns the (possibly-healed, possibly-version-bumped) recipe and the
    result of the check (pre-heal) or the post-heal re-verify, whichever
    ran last."""

    cfg = config or RecipeConfig.from_env()
    replay_result = await replay_recipe(recipe, session=session, registry=registry, driver=driver)
    if replay_result.success:
        recipe.health_status = "healthy"
        recipe.last_verified_at = None  # caller (recipe_store) stamps the real timestamp
        return recipe, replay_result

    affected_names = set(replay_result.field_failures.keys())
    affected_groups = [g for g in recipe.field_groups if set(g.field_names) & affected_names]
    unaffected_groups = [g for g in recipe.field_groups if g not in affected_groups]

    if not affected_groups:
        recipe.health_status = "degraded"
        return recipe, replay_result

    schema = parse_schema(recipe.field_schema)
    leaf_to_array = leaf_to_array_map(schema)
    array_fields_affected = {leaf_to_array[f] for f in affected_names if f in leaf_to_array}
    scoped_schema = {
        name: spec
        for name, spec in schema.items()
        if name in affected_names or name in array_fields_affected
    }
    if not scoped_schema:
        recipe.health_status = "broken"
        return recipe, replay_result

    state = ExplorationState(
        leaf_fields=all_leaf_fields(scoped_schema),
        leaf_to_array=leaf_to_array_map(scoped_schema),
        session=session,
        registry=registry,
        driver=driver,
        llm_config=llm_config,
        config=cfg,
    )
    await run_agent_loop(
        task=build_exploration_task(recipe.url_pattern, scoped_schema),
        session=session,
        registry=registry,
        driver=driver,
        llm_config=llm_config,
        max_steps=max_steps,
        output_schema=None,
        on_step=state.on_step,
    )

    healed = Recipe(
        recipe_id=recipe.recipe_id,
        tenant=recipe.tenant,
        name=recipe.name,
        url_pattern=recipe.url_pattern,
        field_schema=recipe.field_schema,
        version=recipe.version + 1,
        global_setup=state.global_setup or recipe.global_setup,
        field_groups=unaffected_groups + state.field_groups,
        health_status="healthy" if not state.unfound_fields else "degraded",
        schedule_interval_seconds=recipe.schedule_interval_seconds,
    )
    verify_result = await replay_recipe(healed, session=session, registry=registry, driver=driver)
    healed.health_status = "healthy" if verify_result.success else "degraded"
    return healed, verify_result
