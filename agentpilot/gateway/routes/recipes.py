"""`/v1/recipes` -- Phase 2's selector-generation & self-healing
data-collection recipes. `POST` creates a recipe and queues its initial
`build` run; `GET /{id}` polls current state (`field_groups`/health);
`POST /{id}/run` queues a deterministic (no-LLM) `replay`; `POST /{id}/heal`
forces a heal cycle; `GET /{id}/versions` lists build/heal history;
`POST /{id}/codegen` queues LLM-authored scraper-code generation for a
target language. Mounted identically on `monolith` and `gateway` (no
`_proxy` variant) -- run CRUD never touches `agentpilot.driver`; the actual
processing happens in `agentpilot.jobs.recipe_worker_loop.RecipeWorkerLoop`
and `agentpilot.jobs.recipe_scheduler_loop.RecipeSchedulerLoop`, running
independently on every worker/monolith process.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.schemas import (
    RecipeCodegenRequest,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeGetResponse,
    RecipeListResponse,
    RecipeOut,
    RecipeRunOut,
    RecipeRunQueuedResponse,
    RecipeRunResponse,
    RecipeVersionOut,
    RecipeVersionsResponse,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.jobs.recipe_store import PostgresRecipeStore
from agentpilot.jobs.recipe_store import RecipeOut as RecipeRow
from agentpilot.jobs.recipe_store import RecipeRunOut as RecipeRunRow
from agentpilot.observability.metrics import requests_total

router = APIRouter(tags=["recipes"])


def _require_recipe_store(wiring: Wiring) -> PostgresRecipeStore:
    if wiring.recipe_store is None:
        raise HTTPException(
            status_code=503,
            detail="recipes require AGENTPILOT_DATABASE_URL to be configured",
        )
    return wiring.recipe_store


def _recipe_out(recipe: RecipeRow) -> RecipeOut:
    return RecipeOut(
        recipe_id=recipe.recipe_id,
        tenant=recipe.tenant,
        name=recipe.name,
        url_pattern=recipe.url_pattern,
        field_schema=recipe.field_schema,
        version=recipe.version,
        global_setup=recipe.global_setup,
        field_groups=recipe.field_groups,
        health_status=recipe.health_status,  # type: ignore[arg-type]
        last_verified_at=recipe.last_verified_at.isoformat() if recipe.last_verified_at else None,
        last_run_at=recipe.last_run_at.isoformat() if recipe.last_run_at else None,
        schedule_interval_seconds=recipe.schedule_interval_seconds,
        created_at=recipe.created_at.isoformat(),
        updated_at=recipe.updated_at.isoformat(),
    )


def _run_out(run: RecipeRunRow) -> RecipeRunOut:
    return RecipeRunOut(
        run_id=run.run_id,
        recipe_id=run.recipe_id,
        tenant=run.tenant,
        kind=run.kind,  # type: ignore[arg-type]
        status=run.status,  # type: ignore[arg-type]
        data=run.data,
        field_failures=run.field_failures,
        error=run.error,
        created_at=run.created_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


@router.post("", response_model=RecipeCreateResponse)
async def create_recipe(
    req: RecipeCreateRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeCreateResponse:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="create_recipe").inc()
    store = _require_recipe_store(wiring)

    recipe = await store.create_recipe(
        tenant=req.tenant,
        name=req.name,
        url_pattern=req.url,
        field_schema=req.field_schema,
        schedule_interval_seconds=req.schedule_interval_seconds,
    )
    build_run_id = await store.queue_run(
        recipe_id=recipe.recipe_id, tenant=req.tenant, kind="build"
    )
    return RecipeCreateResponse(success=True, recipe_id=recipe.recipe_id, build_run_id=build_run_id)


@router.get("", response_model=RecipeListResponse)
async def list_recipes(
    after: str | None = None,
    limit: int = 50,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeListResponse:
    store = _require_recipe_store(wiring)
    recipes, next_cursor = await store.list_recipes(authed.tenant, after, limit)
    return RecipeListResponse(
        success=True, recipes=[_recipe_out(r) for r in recipes], next=next_cursor
    )


@router.get("/{recipe_id}", response_model=RecipeGetResponse)
async def get_recipe(
    recipe_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeGetResponse:
    store = _require_recipe_store(wiring)
    recipe = await store.get_recipe(recipe_id, authed.tenant)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    return RecipeGetResponse(success=True, data=_recipe_out(recipe))


@router.post("/{recipe_id}/run", response_model=RecipeRunQueuedResponse)
async def run_recipe(
    recipe_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeRunQueuedResponse:
    store = _require_recipe_store(wiring)
    if await store.get_recipe(recipe_id, authed.tenant) is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    run_id = await store.queue_run(recipe_id=recipe_id, tenant=authed.tenant, kind="replay")
    return RecipeRunQueuedResponse(success=True, run_id=run_id)


@router.post("/{recipe_id}/heal", response_model=RecipeRunQueuedResponse)
async def heal_recipe(
    recipe_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeRunQueuedResponse:
    store = _require_recipe_store(wiring)
    if await store.get_recipe(recipe_id, authed.tenant) is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    run_id = await store.queue_run(recipe_id=recipe_id, tenant=authed.tenant, kind="heal")
    return RecipeRunQueuedResponse(success=True, run_id=run_id)


@router.post("/{recipe_id}/codegen", response_model=RecipeRunQueuedResponse)
async def codegen_recipe(
    recipe_id: str,
    req: RecipeCodegenRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeRunQueuedResponse:
    store = _require_recipe_store(wiring)
    if await store.get_recipe(recipe_id, authed.tenant) is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")
    run_id = await store.queue_run(
        recipe_id=recipe_id, tenant=authed.tenant, kind="codegen", params={"language": req.language}
    )
    return RecipeRunQueuedResponse(success=True, run_id=run_id)


@router.get("/{recipe_id}/versions", response_model=RecipeVersionsResponse)
async def list_recipe_versions(
    recipe_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeVersionsResponse:
    store = _require_recipe_store(wiring)
    rows = await store.list_versions(recipe_id, authed.tenant)
    versions = [
        RecipeVersionOut(
            version=row["version"],
            diff_summary=row["diff_summary"],
            created_at=row["created_at"].isoformat(),
        )
        for row in rows
    ]
    return RecipeVersionsResponse(success=True, versions=versions)


@router.get("/{recipe_id}/runs/{run_id}", response_model=RecipeRunResponse)
async def get_recipe_run(
    recipe_id: str,
    run_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeRunResponse:
    store = _require_recipe_store(wiring)
    run = await store.get_run(run_id, authed.tenant)
    if run is None or run.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for recipe {recipe_id!r}")
    return RecipeRunResponse(success=True, data=_run_out(run))
