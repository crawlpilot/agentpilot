"""The minimal internal scheduler for recipes with a
`schedule_interval_seconds` set -- a lightweight ticker (same shape as the
other worker loops) that periodically enqueues a `replay` run for every due
recipe and bumps its next-due timestamp. One fixed interval per recipe, not
a general cron-expression engine -- satisfies "collect data on schedule"
without the added complexity of a full calendar/cron parser.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from agentpilot.jobs.recipe_store import PostgresRecipeStore

log = structlog.get_logger(__name__)


class RecipeSchedulerLoop:
    def __init__(
        self,
        store: PostgresRecipeStore,
        *,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 20,
    ) -> None:
        self._store = store
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
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
                log.exception("recipe_scheduler_loop.tick_failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self) -> None:
        due = await self._store.due_recipes(self._batch_size)
        for recipe_id, tenant, interval_seconds in due:
            await self._store.queue_run(recipe_id=recipe_id, tenant=tenant, kind="replay")
            await self._store.bump_next_due(recipe_id, interval_seconds)
