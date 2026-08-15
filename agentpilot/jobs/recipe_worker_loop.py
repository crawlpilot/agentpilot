"""The recipe-run processing loop: claims queued `recipe_runs` rows (any of
`build`/`replay`/`heal`/`codegen`) and dispatches to
`agentpilot.recipe`'s corresponding stage. Folded into the existing
`AGENTPILOT_ROLE=worker` process next to `CrawlWorkerLoop`/`AgentWorkerLoop`,
same claim/lock/retry shape. `codegen` needs no browser session at all (a
pure LLM call over the already-built recipe); the other three kinds open one
`InteractiveSession` per run, mirroring `AgentWorkerLoop`.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog

from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.jobs.recipe_store import ClaimedRecipeRun, PostgresRecipeStore, RecipeOut
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.build import DEFAULT_BUILD_MAX_STEPS, build_recipe
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.codegen import generate_scraper_code
from agentpilot.recipe.heal import check_and_heal
from agentpilot.recipe.models import Recipe, RecipeRunResult
from agentpilot.recipe.replay import replay_recipe
from agentpilot.session.interactive import (
    InteractiveSession,
    open_interactive_session,
    release_interactive_session,
)
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)


def _domain_from_url(url: str) -> str:
    return urlparse(url).hostname or url


def _to_recipe_model(recipe_out: RecipeOut) -> Recipe:
    global_setup, field_groups = Recipe.groups_from_dict(
        {"global_setup": recipe_out.global_setup, "field_groups": recipe_out.field_groups}
    )
    return Recipe(
        recipe_id=recipe_out.recipe_id,
        tenant=recipe_out.tenant,
        name=recipe_out.name,
        url_pattern=recipe_out.url_pattern,
        field_schema=recipe_out.field_schema,
        version=recipe_out.version,
        global_setup=global_setup,
        field_groups=field_groups,
        health_status=recipe_out.health_status,  # type: ignore[arg-type]
        last_verified_at=recipe_out.last_verified_at,
        last_run_at=recipe_out.last_run_at,
        schedule_interval_seconds=recipe_out.schedule_interval_seconds,
    )


class RecipeWorkerLoop:
    def __init__(
        self,
        store: PostgresRecipeStore,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        profiles_root: Path,
        proxy_pinner: ProxyPinner | None,
        *,
        lease_ttl_seconds: float = 300.0,
        batch_size: int = 5,
        max_concurrent: int = 3,
        poll_interval_seconds: float = 2.0,
        stale_after_seconds: float = 120.0,
        build_max_steps: int = DEFAULT_BUILD_MAX_STEPS,
    ) -> None:
        self._store = store
        self._registry = registry
        self._driver = driver
        self._profiles_root = profiles_root
        self._proxy_pinner = proxy_pinner
        self._lease_ttl_seconds = lease_ttl_seconds
        self._batch_size = batch_size
        self._max_concurrent = max_concurrent
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._build_max_steps = build_max_steps
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("recipe_worker_loop.tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self) -> None:
        await self._store.reclaim_stale_runs(self._stale_after_seconds)
        claimed = await self._store.claim_runs_batch(self._batch_size)
        if not claimed:
            return
        semaphore = asyncio.Semaphore(self._max_concurrent)
        await asyncio.gather(*(self._process(run, semaphore) for run in claimed))

    async def _process(self, run: ClaimedRecipeRun, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            # Keep this run's lease fresh for its whole (potentially long)
            # duration so `reclaim_stale_runs` can't hand it to a second worker
            # -- a build is up to 15 agent steps, well past `stale_after`.
            heartbeat = asyncio.create_task(self._heartbeat(run))
            try:
                await self._process_run(run)
            except Exception as exc:
                log.warning("recipe_worker_loop.run_failed", run_id=run.run_id, error=str(exc))
                await self._store.fail_run(run.run_id, run.lock, str(exc))
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, run: ClaimedRecipeRun) -> None:
        interval = max(self._stale_after_seconds / 3.0, 1.0)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._store.renew_lock(run.run_id, run.lock)
            except Exception:
                log.warning("recipe_worker_loop.renew_failed", run_id=run.run_id)

    async def _process_run(self, run: ClaimedRecipeRun) -> None:
        if run.kind == "codegen":
            await self._process_codegen(run)
            return

        if run.kind == "heal":
            cfg = RecipeConfig.from_env()
            if run.recipe.heal_attempts >= cfg.max_heal_attempts:
                # Exhausted the heal budget -- stop burning LLM/browser cycles;
                # only a fresh build (which resets the streak) recovers it.
                await self._store.mark_recipe_broken(run.recipe_id)
                await self._store.complete_run(
                    run.run_id,
                    run.lock,
                    data=None,
                    field_failures=None,
                    error=(
                        f"max heal attempts ({cfg.max_heal_attempts}) exhausted; "
                        "recipe marked broken, rebuild required"
                    ),
                )
                return

        session = await open_interactive_session(
            session_id=f"recipe-run-{run.run_id}",
            tenant=run.tenant,
            domain=_domain_from_url(run.recipe.url_pattern),
            name=f"recipe-run-{run.run_id}",
            tier="auto",
            headful=False,
            block_popups=True,
            enable_cdp=False,
            registry=self._registry,
            driver=self._driver,
            profiles_root=self._profiles_root,
            proxy_pinner=self._proxy_pinner,
            vault=None,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        try:
            if run.kind == "build":
                await self._process_build(run, session)
            elif run.kind == "replay":
                await self._process_replay(run, session)
            elif run.kind == "heal":
                await self._process_heal(run, session)
            else:
                raise AssertionError(f"unhandled recipe run kind: {run.kind!r}")
        finally:
            try:
                await release_interactive_session(
                    session, registry=self._registry, driver=self._driver, vault=None
                )
            except Exception:
                log.warning("recipe_worker_loop.release_failed", run_id=run.run_id)

    async def _process_build(self, run: ClaimedRecipeRun, session: InteractiveSession) -> None:
        recipe, result = await build_recipe(
            recipe_id=run.recipe_id,
            tenant=run.tenant,
            name=run.recipe.name,
            url=run.recipe.url_pattern,
            raw_schema=run.recipe.field_schema,
            session=session,
            registry=self._registry,
            driver=self._driver,
            llm_config=LLMConfig.from_env(),
            max_steps=self._build_max_steps,
        )
        await self._store.apply_recipe_update(
            run.recipe_id,
            version=run.recipe.version + 1,
            global_setup=[s.to_dict() for s in recipe.global_setup],
            field_groups=[g.to_dict() for g in recipe.field_groups],
            health_status=recipe.health_status,
            diff_summary="initial build",
            heal_attempts="reset",  # a fresh build clears any prior failed-heal streak
        )
        await self._complete_from_result(run, result)

    async def _process_replay(self, run: ClaimedRecipeRun, session: InteractiveSession) -> None:
        recipe = _to_recipe_model(run.recipe)
        result = await replay_recipe(
            recipe, session=session, registry=self._registry, driver=self._driver
        )
        await self._store.mark_replay_result(
            run.recipe_id, health_status="healthy" if result.success else "degraded"
        )
        await self._complete_from_result(run, result)

    async def _process_heal(self, run: ClaimedRecipeRun, session: InteractiveSession) -> None:
        recipe = _to_recipe_model(run.recipe)
        healed, result = await check_and_heal(
            recipe,
            session=session,
            registry=self._registry,
            driver=self._driver,
            llm_config=LLMConfig.from_env(),
            max_steps=self._build_max_steps,
        )
        await self._store.apply_recipe_update(
            run.recipe_id,
            version=healed.version,
            global_setup=[s.to_dict() for s in healed.global_setup],
            field_groups=[g.to_dict() for g in healed.field_groups],
            health_status=healed.health_status,
            diff_summary=f"heal cycle (fields: {sorted(result.field_failures)})",
            # Reset the streak on a healthy heal; otherwise count it toward the
            # max_heal_attempts cutoff.
            heal_attempts="reset" if healed.health_status == "healthy" else "increment",
        )
        await self._complete_from_result(run, result)

    async def _process_codegen(self, run: ClaimedRecipeRun) -> None:
        recipe = _to_recipe_model(run.recipe)
        params: dict[str, Any] = run.params or {}
        language = params.get("language", "python-playwright")
        try:
            code = await generate_scraper_code(
                recipe, language=language, llm_config=LLMConfig.from_env()
            )
        except ValueError as exc:
            await self._store.complete_run(
                run.run_id, run.lock, data=None, field_failures=None, error=str(exc)
            )
            return
        await self._store.complete_run(
            run.run_id, run.lock, data={"code": code, "language": language}, field_failures=None
        )

    async def _complete_from_result(self, run: ClaimedRecipeRun, result: RecipeRunResult) -> None:
        await self._store.complete_run(
            run.run_id,
            run.lock,
            data=result.data,
            field_failures=result.field_failures,
            error=result.error,
        )
