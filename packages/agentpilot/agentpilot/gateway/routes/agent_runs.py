"""`/v1/agent/runs` -- async, Postgres-queue-backed web-agent runs. `POST`
creates and queues a run, `GET /{id}` polls status + paginated per-step
history, `DELETE /{id}` cancels. Mounted on the `gateway` (no `_proxy`
variant): run CRUD never touches `agentpilot.driver`, exactly like
`routes/crawl.py` needs none either. The actual run *processing* happens in
`agentpilot.jobs.agent_worker_loop.AgentWorkerLoop`, running independently on
every `worker` process -- this route only creates/reads/cancels rows in
`agentpilot.jobs.agent_store.PostgresAgentStore`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import require_tenant_auth, resolve_query_api_key
from agentpilot.gateway.schemas import (
    AgentRunCreateRequest,
    AgentRunCreateResponse,
    AgentRunListResponse,
    AgentRunOut,
    AgentRunStatusResponse,
    AgentStepOut,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.jobs.agent_store import AgentRun, PostgresAgentStore
from agentpilot.jobs.agent_store import AgentStepOut as AgentStepRow
from agentpilot.observability.metrics import requests_total

router = APIRouter(tags=["agent"])

_TERMINAL = ("completed", "failed", "cancelled")
_SSE_POLL_INTERVAL_S = 0.75
_SSE_MAX_DURATION_S = 60 * 60  # a hard cap so a wedged run can't stream forever


def _require_agent_store(wiring: Wiring) -> PostgresAgentStore:
    if wiring.agent_store is None:
        raise HTTPException(
            status_code=503,
            detail="agent runs require AGENTPILOT_DATABASE_URL to be configured",
        )
    return wiring.agent_store


def _run_out(run: AgentRun) -> AgentRunOut:
    return AgentRunOut(
        run_id=run.run_id,
        tenant=run.tenant,
        task=run.task,
        status=run.status,  # type: ignore[arg-type]
        current_step=run.current_step,
        max_steps=run.max_steps,
        result=run.result,
        error=run.error,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


def _step_out(step: AgentStepRow) -> AgentStepOut:
    return AgentStepOut(
        seq=step.seq,
        step_number=step.step_number,
        evaluation_previous_goal=step.evaluation_previous_goal,
        memory=step.memory,
        next_goal=step.next_goal,
        actions=step.actions,
        action_results=step.action_results,
        thinking=step.thinking,
        duration_ms=step.duration_ms,
        input_tokens=step.input_tokens,
        output_tokens=step.output_tokens,
        has_screenshot=step.has_screenshot,
        created_at=step.created_at.isoformat(),
    )


@router.post("", response_model=AgentRunCreateResponse)
async def create_agent_run(
    req: AgentRunCreateRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> AgentRunCreateResponse:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="create_agent_run").inc()
    store = _require_agent_store(wiring)

    run = await store.create_run(
        tenant=req.tenant,
        task=req.task,
        domain=req.domain,
        tier=req.tier,
        output_schema=req.output_schema,
        max_steps=req.max_steps,
    )
    return AgentRunCreateResponse(success=True, run_id=run.run_id)


@router.get("", response_model=AgentRunListResponse)
async def list_agent_runs(
    after: str | None = None,
    limit: int = 50,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> AgentRunListResponse:
    store = _require_agent_store(wiring)
    runs, next_cursor = await store.list_runs(authed.tenant, after, limit)
    return AgentRunListResponse(success=True, runs=[_run_out(r) for r in runs], next=next_cursor)


@router.get("/{run_id}", response_model=AgentRunStatusResponse)
async def get_agent_run(
    run_id: str,
    after: str | None = None,
    limit: int = 100,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> AgentRunStatusResponse:
    store = _require_agent_store(wiring)
    run = await store.get_run(run_id, authed.tenant)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no agent run {run_id!r}")
    steps, next_cursor = await store.list_steps(run_id, authed.tenant, after, limit)
    return AgentRunStatusResponse(
        success=True,
        data=_run_out(run),
        steps=[_step_out(s) for s in steps],
        next=next_cursor,
    )


@router.get("/{run_id}/steps/{seq}/screenshot")
async def get_agent_step_screenshot(
    run_id: str,
    seq: int,
    api_key: str | None = None,
    wiring: Wiring = Depends(get_wiring),
) -> Response:
    # Auth via `?api_key=`: an <img src> can't set an Authorization header, so
    # this route takes the key on the query string like `routes/live_view.py`.
    store = _require_agent_store(wiring)
    authed = await resolve_query_api_key(wiring, api_key)
    if authed is None:
        raise HTTPException(status_code=401, detail="invalid or missing api_key")
    png = await store.get_step_screenshot(run_id, authed.tenant, seq)
    if png is None:
        raise HTTPException(status_code=404, detail="no screenshot for this step")
    # Immutable: a persisted step's screenshot never changes, so let the browser
    # cache it hard once fetched.
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "max-age=31536000, immutable"})


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/{run_id}/events")
async def stream_agent_run(
    run_id: str,
    request: Request,
    api_key: str | None = None,
    wiring: Wiring = Depends(get_wiring),
) -> StreamingResponse:
    """Server-Sent Events for one run: an initial `status` snapshot, a `step`
    event per new step as it's persisted, `status` on every status change, and
    a terminal `done`. Cross-process safe -- it reads the shared Postgres queue
    the worker writes to, so the gateway serving this stream and the worker
    running the agent can be (and are) separate processes. Auth travels as
    `?api_key=` because
    `EventSource` can't set headers, exactly like `routes/live_view.py`. The
    client keeps its plain-poll `GET /{id}` as a fallback if this stream drops.
    """

    store = _require_agent_store(wiring)
    # EventSource can't send an Authorization header, so authenticate the query
    # key and scope every read to that tenant.
    authed = await resolve_query_api_key(wiring, api_key)
    if authed is None:
        raise HTTPException(status_code=401, detail="invalid or missing api_key")
    run = await store.get_run(run_id, authed.tenant)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no agent run {run_id!r}")

    async def events() -> AsyncIterator[str]:
        tenant = authed.tenant
        cursor: str | None = None
        last_status: str | None = None
        last_step: int = -1
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _SSE_MAX_DURATION_S
        while True:
            if await request.is_disconnected():
                return
            current = await store.get_run(run_id, tenant)
            if current is None:
                yield _sse("error", {"detail": "run disappeared"})
                return

            # Drain any steps newer than the last one we sent. `list_steps`
            # keyset-paginates, so follow `next` until caught up in one tick.
            while True:
                steps, next_cursor = await store.list_steps(run_id, tenant, cursor, limit=100)
                for step in steps:
                    if step.seq > last_step:
                        yield _sse("step", _step_out(step).model_dump())
                        last_step = step.seq
                if next_cursor is None:
                    break
                cursor = next_cursor
            # Advance the cursor to the newest step so the next tick fetches
            # only newer rows (the single-page case never sets `next_cursor`).
            if last_step >= 0:
                cursor = str(last_step)

            if current.status != last_status:
                yield _sse("status", _run_out(current).model_dump())
                last_status = current.status

            if current.status in _TERMINAL:
                yield _sse("done", _run_out(current).model_dump())
                return
            if loop.time() > deadline:
                yield _sse("error", {"detail": "stream timed out"})
                return
            await asyncio.sleep(_SSE_POLL_INTERVAL_S)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/{run_id}")
async def cancel_agent_run(
    run_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> dict[str, bool]:
    store = _require_agent_store(wiring)
    ok = await store.cancel_run(run_id, authed.tenant)
    return {"success": ok}
