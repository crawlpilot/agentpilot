"""`/v1/recipes` -- Phase 2's selector-generation & self-healing
data-collection recipes. `POST` creates a recipe and queues its initial
`build` run; `GET /{id}` polls current state (`field_groups`/health);
`POST /{id}/run` queues a deterministic (no-LLM) `replay`; `POST /{id}/heal`
forces a heal cycle; `GET /{id}/versions` lists build/heal history;
`POST /{id}/codegen` queues LLM-authored scraper-code generation for a
target language; `POST /{id}/jobs` applies a catalogue recipe to a caller's
own list of URLs (one queued run each) and `GET /{id}/jobs/{job_id}` reports
the batch. Mounted on the `gateway` (no `_proxy` variant) -- run CRUD
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
    RecipeAssistRequest,
    RecipeAssistResponse,
    RecipeCodegenRequest,
    RecipeCreateRequest,
    RecipeCreateResponse,
    RecipeGetResponse,
    RecipeJobOut,
    RecipeJobQueuedResponse,
    RecipeJobRequest,
    RecipeJobResponse,
    RecipeJobResultOut,
    RecipeJobsResponse,
    RecipeListResponse,
    RecipeOnboardRequest,
    RecipeOnboardResponse,
    RecipeOut,
    RecipeRunArtifactOut,
    RecipeRunArtifactsResponse,
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
from agentpilot.jobs.recipe_store import RecipeJobOut as RecipeJobRow
from agentpilot.jobs.recipe_store import RecipeOut as RecipeRow
from agentpilot.jobs.recipe_store import RecipeRunOut as RecipeRunRow
from agentpilot.observability.metrics import requests_total
from agentpilot.recipe.config import RecipeConfig
from agentpilot.recipe.v2.assist import PendingAsk, parse_resolutions
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
        document=recipe.document,
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
        pending_asks=run.pending_asks,
        progress=run.progress,
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
    # `onboard`, not `build`. The v1 build engine is gone: it wrote
    # `field_groups`, a column the studio, the marketplace and codegen all stopped
    # reading, so its output was a recipe that replayed to `{}`. This endpoint
    # keeps its shape -- name, url, field_schema -- and now produces a v2
    # document like every other path.
    build_run_id = await store.queue_run(
        recipe_id=recipe.recipe_id, tenant=req.tenant, kind="onboard"
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
    requests_total.labels(tenant=auth.tenant, route="save_recipe_v2").inc()
    return RecipeSaveResponse(
        success=True, recipe_id=recipe_id, version=version, warnings=warnings
    )


@router.post("/onboard", response_model=RecipeOnboardResponse)
async def onboard_recipe_route(
    req: RecipeOnboardRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeOnboardResponse:
    """Build a v2 recipe from a URL plus a description of the wanted data.

    Queues the work rather than doing it: onboarding drives a real browser
    through up to `build_max_steps` agent steps against a live site, which is
    minutes, not a request. Poll `GET /v1/recipes/{id}/runs/{run_id}`.

    The recipe row is created immediately so the caller has something to watch,
    and stays a `draft` until a human reviews it -- the agent's output is a
    proposal, not a published scraper.
    """

    if not req.fields and not req.output_schema and not (req.description or "").strip():
        raise HTTPException(
            status_code=400,
            detail="give `fields` (a v2 field map), `output_schema` (a JSON "
            "Schema or an example of the JSON you want back), or `description` "
            "(plain English) -- there is nothing to look for otherwise",
        )
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="`url` is required to build against")

    requests_total.labels(tenant=authed.tenant, route="onboard_recipe").inc()
    store = _require_recipe_store(wiring)

    recipe = await store.create_recipe(
        tenant=authed.tenant,
        name=req.name,
        url_pattern=req.url,
        # Empty when the caller described the data instead of declaring it --
        # the worker resolves the description and writes the real schema back.
        field_schema=req.fields or {},
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(
        recipe_id=recipe.recipe_id,
        tenant=authed.tenant,
        kind="onboard",
        params={
            "description": req.description or "",
            "output_schema": req.output_schema or None,
            "sample_urls": [req.url, *req.sample_urls],
            "mode": req.mode,
        },
    )
    return RecipeOnboardResponse(success=True, recipe_id=recipe.recipe_id, run_id=run_id)


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
    requests_total.labels(tenant=auth.tenant, route="update_recipe").inc()
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
    recipe = await store.get_recipe(recipe_id, authed.tenant)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")

    # A replay runs the recipe against *its own* URL, which it only has if it
    # declares a `target.match`. A recipe authored in the studio usually does
    # not -- it is meant to be applied to URLs a caller submits -- and there is
    # no URL here to fall back to. Refusing with the alternative named is far
    # better than queueing a run that dies in the worker opening a session for
    # the empty-string domain.
    if not recipe.url_pattern:
        raise HTTPException(
            status_code=409,
            detail=(
                f"recipe {recipe_id!r} has no URL of its own to run against; "
                f"submit URLs to POST /v1/recipes/{recipe_id}/jobs, or give the "
                "recipe a target matcher to make it schedulable"
            ),
        )

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


@router.post("/{recipe_id}/runs/{run_id}/assist/request", response_model=RecipeAssistResponse)
async def request_assist(
    recipe_id: str,
    run_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeAssistResponse:
    """Ask a running build to stop for a person before it finishes.

    Until this existed, taking over meant waiting for the build to give up: a
    run that bound every field -- correctly or not -- finished and saved without
    ever offering. That is the wrong way round for the failure that actually
    matters on a page carrying a sponsored competitor in its own JSON, where
    every field resolves and one of them is the wrong product.

    Takes effect at the build's next park point, before the document is
    validated and saved, with the browser session still open on the page. The
    caller then polls the run for `needs_input` as usual and answers through
    `POST .../assist`.
    """

    requests_total.labels(tenant=authed.tenant, route="request_assist").inc()
    store = _require_recipe_store(wiring)

    run = await store.get_run(run_id, authed.tenant)
    if run is None or run.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for recipe {recipe_id!r}")
    if not await store.request_assist(run_id, authed.tenant):
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {run_id!r} is {run.status!r} -- only a queued or running build "
                "can be asked to stop. A parked run is already waiting for you; a "
                "finished one has to be rebuilt or edited in the studio."
            ),
        )
    return RecipeAssistResponse(success=True, accepted=[])


@router.post(
    "/{recipe_id}/runs/{run_id}/assist/heartbeat", response_model=RecipeAssistResponse
)
async def heartbeat_assist(
    recipe_id: str,
    run_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeAssistResponse:
    """Say that somebody is still working on a parked run.

    The park is bounded because it holds a worker slot, a warm identity, a
    browser and a proxy pin -- but a fixed bound drops the person doing the
    careful thing, which is the reload, the recording, the pick and the look at
    what it read. The studio calls this while the assist panel is open, and that
    is what makes a longer ceiling safe rather than merely longer.

    Idempotent, and cheap enough to call on a timer.
    """

    store = _require_recipe_store(wiring)
    run = await store.get_run(run_id, authed.tenant)
    if run is None or run.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for recipe {recipe_id!r}")
    extended = await store.touch_park(
        run_id, authed.tenant, extend_by_s=RecipeConfig.from_env().assist_timeout_s
    )
    if extended is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {run_id!r} is {run.status!r} -- only a run parked for input has "
                "a deadline to extend."
            ),
        )
    return RecipeAssistResponse(success=True, accepted=[])


@router.get(
    "/{recipe_id}/runs/{run_id}/artifacts", response_model=RecipeRunArtifactsResponse
)
async def get_recipe_run_artifacts(
    recipe_id: str,
    run_id: str,
    kind: str | None = None,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeRunArtifactsResponse:
    """What the build proposed for each field, and why each attempt was rejected.

    The reasons here are the ones `verify_locators` and `rows._problems_with`
    already produce -- "column 'value' is empty in 9 of 10 rows, so it is not
    being resolved inside each row" -- written to be read by a person and, until
    this existed, logged to a worker's stdout and discarded. A build that came
    back with a wrong selector could only be investigated by running it again.
    """

    store = _require_recipe_store(wiring)
    run = await store.get_run(run_id, authed.tenant)
    if run is None or run.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for recipe {recipe_id!r}")
    rows = await store.run_artifacts(
        run_id, authed.tenant, kinds=(kind,) if kind else None
    )
    return RecipeRunArtifactsResponse(
        success=True,
        artifacts=[RecipeRunArtifactOut(**row) for row in rows],
    )


@router.post("/{recipe_id}/runs/{run_id}/assist", response_model=RecipeAssistResponse)
async def submit_assist(
    recipe_id: str,
    run_id: str,
    req: RecipeAssistRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeAssistResponse:
    """Answer what a parked onboarding run is waiting for.

    The run's worker is still holding a live browser session on the page it got
    stuck on -- that is why the answers can be element picks rather than
    descriptions. Accepting them flips the run back to `running`; the worker
    picks them up on its next poll, verifies any picked locator like it would
    its own, and finishes the build.

    Only a run in `needs_input` can be answered. A run whose park already
    expired has resumed without the answers, and reporting success for an
    update that changed nothing would leave the caller believing otherwise.
    """

    requests_total.labels(tenant=authed.tenant, route="submit_assist").inc()
    store = _require_recipe_store(wiring)

    run = await store.get_run(run_id, authed.tenant)
    if run is None or run.recipe_id != recipe_id:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for recipe {recipe_id!r}")

    asks = [PendingAsk.from_dict(a) for a in (run.pending_asks or [])]
    resolutions = parse_resolutions([r.model_dump() for r in req.resolutions], asks)
    if not resolutions:
        raise HTTPException(
            status_code=400,
            detail=(
                "no usable resolutions: each must name a field this run is actually "
                "waiting on, and carry what its action needs (`pick` needs locators, "
                "`scope` needs the region's locators, `describe` needs a hint)"
            ),
        )

    accepted = await store.submit_assist(
        run_id,
        authed.tenant,
        [
            {
                "field": r.field,
                "action": r.action,
                "locators": [loc.to_dict() for loc in r.locators],
                "hint": r.hint,
                "shape": r.shape,
                "html": r.html,
                # Re-serialised from the parsed `Step`s rather than passed
                # through from the request, so what the worker replays is what
                # `parse_recorded_steps` accepted -- an xpath target or an
                # unknown op is dropped here, not discovered on every run.
                "steps": [s.to_dict() for s in r.steps],
            }
            for r in resolutions.values()
        ],
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run {run_id!r} is not waiting for input (status: {run.status}) -- "
                "it may have already resumed on its own"
            ),
        )
    return RecipeAssistResponse(success=True, accepted=sorted(resolutions))


# --- extraction jobs --------------------------------------------------------
#
# The runtime half of the marketplace. `GET /templates` is how a caller finds
# a scraper; this is how they use one, and the two together are what make a
# recipe reusable by somebody who did not author it.


def _job_out(job: RecipeJobRow) -> RecipeJobOut:
    return RecipeJobOut(
        job_id=job.job_id,
        recipe_id=job.recipe_id,
        recipe_name=job.recipe_name,
        recipe_version=job.recipe_version,
        status=job.status,  # type: ignore[arg-type]
        total=job.total,
        queued=job.queued,
        running=job.running,
        completed=job.completed,
        failed=job.failed,
        created_at=job.created_at.isoformat(),
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )


def _job_result_out(run: RecipeRunRow) -> RecipeJobResultOut:
    # `field_failures` is where the worker parks the v2 verdict for a job run;
    # a run from any other path has the v1 shape and simply has none of these
    # keys, which reads as "no per-field detail" rather than as an error.
    detail = run.field_failures or {}
    return RecipeJobResultOut(
        run_id=run.run_id,
        url=run.url or "",
        status=run.status,  # type: ignore[arg-type]
        data=run.data,
        field_status=detail.get("field_status"),
        outcome=detail.get("outcome"),
        error=run.error,
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
    )


def _normalize_urls(raw: list[str]) -> list[str]:
    """Trim, drop blanks, and de-duplicate while keeping submission order.

    De-duplicating matters more than it looks: a caller pasting from a
    spreadsheet routinely repeats a URL, and each duplicate would otherwise
    cost a full browser session to produce a row identical to one already in
    the job.
    """

    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        url = item.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


@router.post("/{recipe_id}/jobs", response_model=RecipeJobQueuedResponse)
async def submit_recipe_job(
    recipe_id: str,
    req: RecipeJobRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeJobQueuedResponse:
    """Apply a recipe to the caller's own URLs.

    This is deliberately not `POST /{id}/run`. That queues *the recipe's own*
    scheduled replay against the URL pattern it was built for, and reports
    against the recipe's health. This queues one run per submitted URL, reports
    as a batch, and leaves health alone -- a URL somebody pasted is not
    evidence about the scraper.
    """

    store = _require_recipe_store(wiring)
    recipe = await store.get_recipe(recipe_id, authed.tenant)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"no recipe {recipe_id!r}")

    # Refused here rather than failing every run: the recipe cannot answer for
    # a URL it was not given, and finding that out N sessions later -- once per
    # submitted URL -- is a slow way to be told something knowable now.
    if not recipe.document:
        raise HTTPException(
            status_code=409,
            detail=(
                f"recipe {recipe_id!r} has no v2 document, so it can only run against "
                "the URL pattern it was built for; rebuild it in the studio to submit URLs"
            ),
        )

    urls = _normalize_urls(req.urls)
    if not urls:
        raise HTTPException(status_code=422, detail="urls: no non-empty URLs submitted")

    job_id, run_ids = await store.create_job(
        recipe_id=recipe_id,
        tenant=authed.tenant,
        recipe_version=recipe.version,
        urls=urls,
        metadata=req.metadata,
    )
    requests_total.labels(tenant=authed.tenant, route="submit_recipe_job").inc()
    return RecipeJobQueuedResponse(success=True, job_id=job_id, queued=len(run_ids))


@router.get("/{recipe_id}/jobs", response_model=RecipeJobsResponse)
async def list_recipe_jobs(
    recipe_id: str,
    limit: int = 50,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeJobsResponse:
    store = _require_recipe_store(wiring)
    jobs = await store.list_jobs(
        tenant=authed.tenant, recipe_id=recipe_id, limit=min(limit, 200)
    )
    return RecipeJobsResponse(success=True, jobs=[_job_out(j) for j in jobs])


@router.get("/{recipe_id}/jobs/{job_id}", response_model=RecipeJobResponse)
async def get_recipe_job(
    recipe_id: str,
    job_id: str,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> RecipeJobResponse:
    store = _require_recipe_store(wiring)
    job = await store.get_job(job_id, authed.tenant)
    if job is None or job.recipe_id != recipe_id:
        raise HTTPException(
            status_code=404, detail=f"no job {job_id!r} for recipe {recipe_id!r}"
        )
    runs = await store.list_job_runs(job_id, authed.tenant)
    return RecipeJobResponse(
        success=True, job=_job_out(job), results=[_job_result_out(r) for r in runs]
    )
