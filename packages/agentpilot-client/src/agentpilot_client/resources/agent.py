"""`/v1/agent/runs` -- give it a task in words, poll the run.

Same queued/polled shape as `crawl`, and deliberately the same ergonomics:
create returns a handle, `.wait()` blocks, `.status()` is there for anyone
driving their own loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from crawlpilot.spi.errors import DriverError

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport

TERMINAL = frozenset({"completed", "failed", "cancelled"})


class AgentRunFailed(DriverError):
    """A run ended as `failed` or `cancelled`. Carries the server's own error
    text, and `run` for the full record."""

    def __init__(self, message: str, run: dict[str, Any]) -> None:
        super().__init__(message)
        self.run = run


class AgentRun:
    """A queued agent run."""

    def __init__(self, transport: Transport, run_id: str) -> None:
        self._transport = transport
        self.id = run_id

    async def status(self) -> dict[str, Any]:
        """`{data: {...run...}, steps: [...], next: cursor}`."""

        return await self._transport.request("GET", f"/v1/agent/runs/{self.id}")

    async def steps(self) -> list[dict[str, Any]]:
        """Every step recorded so far -- what the model saw, chose, and got."""

        payload = await self.status()
        return list(payload.get("steps", []))

    async def wait(
        self, *, poll_interval: float = 2.0, timeout: float | None = None
    ) -> dict[str, Any]:
        """Block until the run finishes, then return its `result`.

        Raises `AgentRunFailed` rather than returning a `None` result that a
        caller would have to remember to check -- a failed run and a run that
        succeeded with no structured output are different things, and returning
        `None` for both loses that.
        """

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            payload = await self.status()
            run = payload.get("data", {})
            state = run.get("status")
            if state in TERMINAL:
                if state != "completed":
                    raise AgentRunFailed(
                        f"agent run {self.id} ended as {state!r}: "
                        f"{run.get('error') or 'no reason given'}",
                        run,
                    )
                return dict(run.get("result") or {})
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"agent run {self.id} still {state!r} after {timeout}s "
                    f"(step {run.get('current_step')}/{run.get('max_steps')})"
                )
            await asyncio.sleep(poll_interval)

    async def cancel(self) -> None:
        await self._transport.request("DELETE", f"/v1/agent/runs/{self.id}", retry=False)

    async def screenshot(self, step: int) -> bytes:
        """What the page looked like at one step -- the fastest way to see why
        a run went the way it did."""

        payload = await self._transport.request(
            "GET", f"/v1/agent/runs/{self.id}/steps/{step}/screenshot"
        )
        return payload if isinstance(payload, bytes) else str(payload).encode()


class AgentResource:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def run(
        self,
        task: str,
        *,
        domain: str,
        tier: str = "auto",
        max_steps: int = 50,
        output_schema: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Queue a run. `domain` scopes the identity and proxy pinning for the
        session it opens -- not necessarily the task's first URL, since the agent
        may navigate anywhere the task requires."""

        body: dict[str, Any] = {
            "tenant": "-",
            "domain": domain,
            "task": task,
            "tier": tier,
            "max_steps": max_steps,
        }
        if output_schema is not None:
            body["output_schema"] = output_schema
        payload = await self._transport.request("POST", "/v1/agent/runs", json=body)
        return AgentRun(self._transport, payload["run_id"])

    async def get(self, run_id: str) -> AgentRun:
        return AgentRun(self._transport, run_id)

    async def list(self, *, after: str | None = None, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        if limit is not None:
            params["limit"] = limit
        return await self._transport.request("GET", "/v1/agent/runs", params=params or None)
