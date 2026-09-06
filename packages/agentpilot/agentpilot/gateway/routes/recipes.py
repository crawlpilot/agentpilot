"""`/v1/recipes` -- Phase 2's selector-generation & self-healing
data-collection recipes. `POST` creates a recipe and queues its initial
`build` run; `GET /{id}` polls current state (`field_groups`/health);
`POST /{id}/run` queues a deterministic (no-LLM) `replay`; `POST /{id}/heal`
forces a heal cycle; `GET /{id}/versions` lists build/heal history;
`POST /{id}/codegen` queues LLM-authored scraper-code generation for a
target language. Mounted on the `gateway` (no `_proxy` variant) -- run CRUD
never touches `crawlpilot.driver`; the actual processing happens in
`agentpilot.jobs.recipe_worker_loop.RecipeWorkerLoop` and
`agentpilot.jobs.recipe_scheduler_loop.RecipeSchedulerLoop`, running
independently on every `worker` process.
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
    RecipeSaveRequest,
    RecipeSaveResponse,
    RecipeVersionOut,
    RecipeVersionsResponse,
    TemplateOut,
    TemplatesResponse,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.jobs.recipe_store import PostgresRecipeStore
from agentpilot.jobs.recipe_store import RecipeOut as RecipeRow
from agentpilot.jobs.recipe_store import RecipeRunOut as RecipeRunRow
from agentpilot.observability.metrics import requests_total
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.v2.validate import validate_document

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

    # Enforce the schedule-interval floor: without it a caller could set a
    # 1-second interval and the scheduler would hammer the target site every
    # tick (a DoS/abuse vector). This is the only ingress -- there is no
    # update endpoint -- so validating here covers every scheduled recipe.
    floor = RecipeConfig.from_env().min_schedule_interval_s
    if req.schedule_interval_seconds is not None and req.schedule_interval_seconds < floor:
        raise HTTPException(
            status_code=400,
            detail=f"schedule_interval_seconds must be >= {floor} (minimum schedule interval)",
        )

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


@router.get("/templates", response_model=TemplatesResponse)
async def list_templates(
    domain: str | None = None,
    page_type: str | None = None,
    limit: int = 100,
    wiring: Wiring = Depends(get_wiring),
    auth: AuthedTenant = Depends(require_tenant_auth),
) -> TemplatesResponse:
    """The scraper marketplace: recipes published as prebuilt templates.

    Declared BEFORE `/{recipe_id}` on purpose -- FastAPI matches in
    declaration order, so a later literal path would be swallowed by the
    parameterised one and "templates" would be looked up as a recipe id.
    """

    store = _require_recipe_store(wiring)
    rows = await store.list_templates(
        tenant=auth.tenant, domain=domain, page_type=page_type, limit=min(limit, 200)
    )
    return TemplatesResponse(
        templates=[
            TemplateOut(
                recipe_id=row["recipe_id"],
                name=row["name"],
                domain=row.get("domain"),
                page_type=row.get("page_type"),
                field_names=sorted((row.get("field_schema") or {}).keys()),
                version=row["version"],
                health_status=row["health_status"],
                updated_at=row["updated_at"].isoformat(),
            )
            for row in rows
        ]
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


@router.post("/v2", response_model=RecipeSaveResponse)
async def save_recipe_v2(
    req: RecipeSaveRequest,
    wiring: Wiring = Depends(get_wiring),
    auth: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeSaveResponse:
    """Persist a hand-authored v2 document as a NEW recipe.

    The counterpart to `POST /v1/recipes`, and deliberately not the same thing:
    that one takes a name and a URL and queues an agent *build*. This takes a
    finished document that a person authored and previewed against a live page,
    and stores it as-is. Queueing a build here would replace their work with
    the agent's answer, which is the opposite of what saving means.
    """

    store = _require_recipe_store(wiring)
    recipe_id, version, warnings = await _save(store, auth.tenant, req, recipe_id=None)
    requests_total.labels(route="recipes.save_v2", status="200").inc()
    return RecipeSaveResponse(
        success=True, recipe_id=recipe_id, version=version, warnings=warnings
    )


@router.put("/{recipe_id}", response_model=RecipeSaveResponse)
async def update_recipe(
    recipe_id: str,
    req: RecipeSaveRequest,
    wiring: Wiring = Depends(get_wiring),
    auth: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeSaveResponse:
    """Replace a recipe's document, as a new version.

    `recipe_versions` is append-only, so this is additive and reversible --
    the same guarantee build and heal already rely on. Until this existed, a
    recipe with one wrong selector had to be rebuilt from scratch.
    """

    store = _require_recipe_store(wiring)
    existing = await store.get_recipe(recipe_id, auth.tenant)
    if existing is None:
        raise HTTPException(status_code=404, detail="no such recipe")
    saved_id, version, warnings = await _save(store, auth.tenant, req, recipe_id=recipe_id)
    requests_total.labels(route="recipes.update", status="200").inc()
    return RecipeSaveResponse(
        success=True, recipe_id=saved_id, version=version, warnings=warnings
    )


async def _save(
    store: PostgresRecipeStore,
    tenant: str,
    req: RecipeSaveRequest,
    *,
    recipe_id: str | None,
) -> tuple[str, int, list[str]]:
    """Validate, then write. Shared so create and update cannot diverge."""

    errors, warnings = validate_document(req.recipe)
    if errors:
        # 422, not 400: the document is well-formed JSON and structurally
        # wrong. The full list goes back, because fixing one error at a time
        # through a round trip each is a miserable way to author anything.
        raise HTTPException(status_code=422, detail={"errors": errors, "warnings": warnings})

    # The tenant is the caller's, never the document's -- a recipe that names
    # someone else's tenant must not be able to write into it.
    document = {**req.recipe, "tenant": tenant}
    saved_id, version = await store.save_document(
        tenant=tenant,
        document=document,
        recipe_id=recipe_id,
        schedule_interval_seconds=req.schedule_interval_seconds,
        page_type=req.page_type,
        template_visibility=req.template_visibility,
    )
    return saved_id, version, warnings


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
