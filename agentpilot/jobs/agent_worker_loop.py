"""The agent-run processing loop: claims queued `agent_runs` rows, opens an
interactive session for each, drives `agentpilot.agent.loop.run_agent_loop`
to completion, persists the per-step history and final result. Folded into
the existing `AGENTPILOT_ROLE=worker` process (started in `gateway.wiring`'s
`_init_worker()`, next to `CrawlWorkerLoop`), same claim/lock/retry shape as
that loop -- but each claimed run is driven *fully to completion inside one
`_process()` call* (open session -> loop many steps -> release session)
rather than one quick unit per tick, since a single agent run can take
minutes across many LLM turns. The claim lock is renewed periodically
*during* that processing (via `run_agent_loop`'s `on_step` callback), not
just once at claim time, so a long-running run doesn't get reclaimed as
stale mid-flight -- a real requirement `CrawlWorkerLoop`'s quick per-URL
tasks never had.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import structlog

from agentpilot.agent.loop import run_agent_loop
from agentpilot.agent.state import AgentStepRecord
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.jobs.agent_store import ClaimedAgentRun, PostgresAgentStore
from agentpilot.llm.client import LLMConfig
from agentpilot.session.interactive import open_interactive_session, release_interactive_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.session.rotation import RotationConfig
from agentpilot.spi.actions import stealth_from_tier
from agentpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)


class AgentWorkerLoop:
    def __init__(
        self,
        store: PostgresAgentStore,
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
        max_failures: int = 5,
        step_timeout_s: float | None = None,
        enable_vision: bool = False,
        enable_judge: bool = False,
        rotation: RotationConfig | None = None,
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
        self._max_failures = max_failures
        self._step_timeout_s = step_timeout_s
        self._enable_vision = enable_vision
        self._enable_judge = enable_judge
        self._rotation = rotation
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
                log.exception("agent_worker_loop.tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self) -> None:
        await self._store.reclaim_stale_runs(self._stale_after_seconds)
        claimed = await self._store.claim_runs_batch(self._batch_size)
        if not claimed:
            return
        semaphore = asyncio.Semaphore(self._max_concurrent)
        await asyncio.gather(*(self._process(run, semaphore) for run in claimed))

    async def _process(self, run: ClaimedAgentRun, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                await self._process_run(run)
            except Exception as exc:
                log.warning("agent_worker_loop.run_failed", run_id=run.run_id, error=str(exc))
                await self._store.fail_run(run.run_id, run.lock, str(exc))

    async def _process_run(self, run: ClaimedAgentRun) -> None:
        session = await open_interactive_session(
            session_id=f"agent-run-{run.run_id}",
            tenant=run.tenant,
            domain=run.domain,
            name=f"agent-run-{run.run_id}",
            tier=run.tier,
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

        async def _on_step(step: AgentStepRecord) -> None:
            # Persist the step *and* renew the claim lock together -- this is
            # the "periodic renewal during processing" `reclaim_stale_runs`
            # depends on for a run that spans many minutes/steps.
            await self._store.append_step(run.run_id, step)
            await self._store.renew_lock(run.run_id, run.lock)

        try:
            result = await run_agent_loop(
                task=run.task,
                session=session,
                registry=self._registry,
                driver=self._driver,
                llm_config=LLMConfig.from_env(),
                max_steps=run.max_steps,
                output_schema=run.output_schema,
                max_failures=self._max_failures,
                step_timeout_s=self._step_timeout_s,
                enable_vision=self._enable_vision,
                enable_judge=self._enable_judge,
                # UI-driven stealth: the run's `tier` decides whether the fusion
                # engine may use CDP Runtime (`getEventListeners`). basic/stealth
                # -> Runtime-free; enhanced/auto -> full browser-use parity.
                no_runtime=stealth_from_tier(run.tier),
                on_step=_on_step,
            )
        finally:
            try:
                await release_interactive_session(
                    session,
                    registry=self._registry,
                    driver=self._driver,
                    vault=None,
                    rotation=self._rotation,
                    profiles_root=self._profiles_root,
                )
            except Exception:
                log.warning("agent_worker_loop.release_failed", run_id=run.run_id)

        # `run_agent_loop` never raises for an in-task failure (ran out of
        # steps, LLM kept erroring) -- that's still a *completed* run from
        # the queue's perspective (the processing finished; it just didn't
        # succeed), not a retry-eligible queue failure. Only an exception
        # escaping this whole method (session open, LLM not configured,
        # infra errors) goes through `_process()`'s `fail_run` path instead.
        await self._store.complete_run(
            run.run_id,
            run.lock,
            {
                "success": result.success,
                "result": result.result,
                "extracted_data": result.extracted_data,
                "error": result.error,
            },
        )
