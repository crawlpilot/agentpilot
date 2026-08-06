"""`/v1/agent/runs` -- async, Postgres-queue-backed web-agent runs. `POST`
creates and queues a run, `GET /{id}` polls status + paginated per-step
history, `DELETE /{id}` cancels. Mounted identically on both `monolith` and
`gateway` (no `_proxy` variant): run CRUD never touches `agentpilot.driver`,
exactly like `routes/crawl.py` needs none either. The actual run
*processing* happens in `agentpilot.jobs.agent_worker_loop.AgentWorkerLoop`,
running independently on every worker/monolith process -- this route only
creates/reads/cancels rows in `agentpilot.jobs.agent_store.PostgresAgentStore`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.schemas import (
    AgentRunCreateRequest,
    AgentRunCreateResponse,
    AgentRunOut,
    AgentRunStatusResponse,
    AgentStepOut,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.jobs.agent_store import AgentRun, PostgresAgentStore
from agentpilot.jobs.agent_store import AgentStepOut as AgentStepRow
from agentpilot.observability.metrics import requests_total

router = APIRouter(tags=["agent"])


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


@router.delete("/{run_id}")
async def cancel_agent_run(
    run_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> dict[str, bool]:
    store = _require_agent_store(wiring)
    ok = await store.cancel_run(run_id, authed.tenant)
    return {"success": ok}
