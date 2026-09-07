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

from agentpilot.jobs.recipe_store import ClaimedRecipeRun, PostgresRecipeStore, RecipeOut
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.build import DEFAULT_BUILD_MAX_STEPS, build_recipe
from agentpilot.recipe.codegen import generate_scraper_code
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.heal import check_and_heal
from agentpilot.recipe.models import Recipe, RecipeRunResult
from agentpilot.recipe.replay import replay_recipe
from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import NullPrototypes, PrototypeProvider
from crawlpilot.session.interactive import (
    InteractiveSession,
    open_interactive_session,
    release_interactive_session,
)
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.errors import LeaseConflict

log = structlog.get_logger(__name__)

_NO_PROTOTYPES = NullPrototypes()


def _domain_from_url(url: str) -> str:
    return urlparse(url).hostname or url


# How many warm identities a recipe run may cycle between, per (tenant, domain).
#
# The identity is `{tenant}/{domain}/{name}`, and `name` decides whether a run
# gets a *warm* browser profile or a brand-new one. This loop used to pass
# `recipe-run-{run_id}` -- unique per run -- which meant every single run opened
# a cookie-less, history-less, first-visit Chrome behind a freshly picked proxy.
#
# That is not a neutral default, it is the thing WAFs look for.
# `session/ephemeral.py` says so outright: a throwaway identity is "a
# cookie-less, first-visit browser every time, which is itself a bot signal to
# WAFs like Akamai" -- which is what Walmart and Zara run. It also leaked a
# profile directory per run, forever: 807 of them on one dev worker.
#
# A *single* stable name would warm perfectly and then serialise everything --
# `Registry.acquire` raises `LeaseConflict` when an identity already holds an
# active lease, so two concurrent runs on the same domain would collide. A
# small pool is the middle: each slot is reused across runs (warm profile, warm
# Chrome, sticky proxy), and there are enough slots that concurrent runs get
# their own. Sized above `max_concurrent` so the common case never contends.
_IDENTITY_SLOTS = 8


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
        browser_config: BrowserConfig = DEFAULTS,
        prototype_provider: PrototypeProvider = _NO_PROTOTYPES,
    ) -> None:
        self._store = store
        self._registry = registry
        self._driver = driver
        self._profiles_root = profiles_root
        self._browser_config = browser_config
        self._prototype_provider = prototype_provider
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

        # Every remaining kind needs a page, and the page needs a domain to
        # open the session against. A recipe with no `target.match` has an
        # empty `url_pattern`, which is legitimate -- it is applied to URLs a
        # caller submits -- but leaves a *scheduled* replay with nowhere to go.
        # `open_interactive_session` rejects the empty domain with "unsafe
        # identity path segment", which is true and says nothing about the
        # actual problem, so it is caught here where the cause is known.
        target_url = run.url or run.recipe.url_pattern
        if not target_url:
            await self._store.complete_run(
                run.run_id,
                run.lock,
                data=None,
                field_failures=None,
                error=(
                    "this recipe has no URL of its own to run against; submit URLs "
                    "to it as a job, or give it a target matcher to make it schedulable"
                ),
            )
            return

        # For a submitted run, the domain is the *submitted URL's* -- the
        # profile, the proxy pin and the cookie jar are all domain-scoped, so
        # taking them from the recipe's own `url_pattern` would open a session
        # pinned to a site this run never visits.
        session = await self._open_warm_session(run, _domain_from_url(target_url))
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

    async def _open_warm_session(
        self, run: ClaimedRecipeRun, domain: str
    ) -> InteractiveSession:
        """A session on a *reused* identity, so the browser is not a stranger.

        See `_IDENTITY_SLOTS`. The slot is claimed by trying to open one and
        moving on if it is taken, rather than by counting: two worker processes
        share a registry and neither can know what the other holds, so the only
        reliable claim is the acquire itself.

        Starting the scan at a per-run offset keeps concurrent runs from all
        queueing behind slot 0 -- without it, three parallel runs would each
        collide on 0, then on 1, doing the work of six opens to reach three.
        """

        start = hash(run.run_id) % _IDENTITY_SLOTS
        for offset in range(_IDENTITY_SLOTS):
            slot = (start + offset) % _IDENTITY_SLOTS
            try:
                return await open_interactive_session(
                    browser_config=self._browser_config,
                    prototype_provider=self._prototype_provider,
                    # Unique: this names the *session*, not the identity, and
                    # it is what live-view and the logs address.
                    session_id=f"recipe-run-{run.run_id}",
                    scope=run.tenant,
                    domain=domain,
                    # Stable: this names the *identity*, and decides whether
                    # the profile is warm.
                    name=f"recipe-{slot}",
                    tier="auto",
                    headful=True,
                    block_popups=True,
                    enable_cdp=False,
                    registry=self._registry,
                    driver=self._driver,
                    profiles_root=self._profiles_root,
                    proxy_pinner=self._proxy_pinner,
                    vault=None,
                    lease_ttl_seconds=self._lease_ttl_seconds,
                )
            except LeaseConflict:
                continue

        # Every slot busy. A cold identity still runs, and is far more likely
        # to be blocked -- but a run that executes and reports being blocked
        # beats one that never starts, and `classify.py` can tell the two
        # apart in the result.
        log.warning(
            "recipe_worker_loop.no_warm_identity", run_id=run.run_id, domain=domain
        )
        return await open_interactive_session(
            browser_config=self._browser_config,
            prototype_provider=self._prototype_provider,
            session_id=f"recipe-run-{run.run_id}",
            scope=run.tenant,
            domain=domain,
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
        # A submitted run and a scheduled one are both replays, and they differ
        # in the two ways this branches on.
        if run.job_id is not None and run.url:
            await self._process_job_run(run, session)
            return

        recipe = _to_recipe_model(run.recipe)
        result = await replay_recipe(
            recipe, session=session, registry=self._registry, driver=self._driver
        )
        await self._store.mark_replay_result(
            run.recipe_id, health_status="healthy" if result.success else "degraded"
        )
        await self._complete_from_result(run, result)

    async def _process_job_run(
        self, run: ClaimedRecipeRun, session: InteractiveSession
    ) -> None:
        """One URL of a submitted job, through the v2 engine.

        **v2, not v1, and not by preference.** The older `replay_recipe` reads
        the page to visit off the recipe's own `url_pattern`; there is nowhere
        to put a caller's URL. `recipe/v2/replay.py` takes it as `RunInput`,
        which is the whole reason a catalogue of recipes can be applied to
        somebody else's list of pages. A recipe with no v2 document was built
        by the agent against one URL pattern and cannot answer for another, so
        it is refused rather than silently run against the wrong page.

        **The result never touches recipe health.** A scheduled replay failing
        means the recipe is degraded; a submitted run failing usually means the
        URL was wrong, and letting one bad paste mark a public template broken
        would take a working scraper out of the catalogue for every tenant that
        can see it.
        """

        from agentpilot.recipe.v2.models import Recipe as RecipeV2
        from agentpilot.recipe.v2.models import RunInput
        from agentpilot.recipe.v2.replay import replay_recipe as replay_v2

        document = run.recipe.document
        if not document:
            await self._store.complete_run(
                run.run_id,
                run.lock,
                data=None,
                field_failures=None,
                error=(
                    "this recipe has no v2 document, so it can only run against "
                    "the URL pattern it was built for"
                ),
            )
            return

        result = await replay_v2(
            RecipeV2.from_dict(document),
            RunInput(url=run.url or "", metadata=(run.params or {}).get("metadata") or {}),
            session=session,
            registry=self._registry,
            driver=self._driver,
        )
        await self._store.complete_run(
            run.run_id,
            run.lock,
            data=result.data,
            # `field_status` is v2's per-field verdict (resolved / fallback /
            # suspect / empty / failed) and is what the job view colours each
            # URL by -- richer than v1's failures list, and the same shape a
            # caller reading the API needs to judge a partial result.
            #
            # `step_trace` is here because without it an empty field behind a
            # reveal click is unattributable: the selector may be wrong, or the
            # click may never have run, and the two need opposite fixes. Every
            # reveal step is `optional: true, on_error: continue` by
            # construction (see `RecipeWizardPage::pickStepTarget`), so a step
            # that matched nothing is *silent* -- the run completes, the group
            # reads an unrevealed page, and every field in it comes back empty
            # with nothing saying why.
            field_failures={
                "field_status": result.field_status,
                "truncated": result.truncated,
                "outcome": result.outcome,
                "step_trace": [s.to_dict() for s in result.step_trace],
            },
            error=result.error,
        )

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
