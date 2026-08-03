"""The real `/v1/scrape` implementation: Navigate -> [pre-extract actions]
-> Extract(s) [-> Screenshot] -> immediate teardown, composed server-side
around one ephemeral identity per call -- not three round trips through
`/v1/sessions`'s open/execute/delete (which would also mean full lease/
affinity bookkeeping for what's meant to be a cheap one-shot page fetch at
scale). No baked-in path prefix -- `app.py` mounts this same router at
`/internal/scrape` (monolith/worker) and `/v1/scrape` (monolith, tenant-
auth-gated), the same dual-mount idiom `routes/sessions.py` uses. A
`gateway`-role process never mounts this; it mounts `scrape_proxy.py` at
`/v1/scrape` instead, proxying to a worker's `/internal/scrape`.

Ephemeral, but routed *through* `wiring.registry`/`resolve_profile_dir`, not
bypassing them: `NodeRegistry`'s capacity heartbeat derives active/idle
counts from a registry scan, so a mid-request crash here self-heals for
free the same way it does for an interactive session -- a bypass would need
its own explicit capacity accounting. The identity is minted fresh per call
with a random `name` (`kind` left at the dataclass's own `TEMPORARY`
default, deliberately -- see `routes/sessions.py`'s `open_session`, which
marks *its* identities `kind=DEFAULT` for exactly the reason that this
module needs `TEMPORARY` to mean something), so it can never collide with a
durable interactive identity for the same tenant+domain, and this module's
own `identity.is_temporary` checks are trustworthy.
"""

from __future__ import annotations

import base64
import time
import uuid
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from agentpilot.gateway.action_conversion import to_spi_action
from agentpilot.gateway.auth_deps import optional_authed_tenant
from agentpilot.gateway.schemas import (
    DocumentOut,
    ScrapeMetadataOut,
    ScrapeRequest,
    ScrapeResponse,
)
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.identity.profile_store import delete_profile_dir, resolve_profile_dir
from agentpilot.observability.metrics import requests_total, scrape_duration_seconds
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef

log = structlog.get_logger(__name__)

router = APIRouter(tags=["scrape"])


def _build_batch(req: ScrapeRequest) -> list[spi_actions.Action]:
    batch: list[spi_actions.Action] = [
        spi_actions.NavigateAction(url=req.url, timeout_ms=req.timeout_ms)
    ]
    if req.wait_for_ms:
        batch.append(spi_actions.WaitAction(ms=req.wait_for_ms))
    batch.extend(to_spi_action(a) for a in req.actions)
    batch.extend(
        spi_actions.ExtractAction(format=fmt, main_content=req.only_main_content)
        for fmt in req.formats
    )
    if req.screenshot:
        batch.append(spi_actions.ScreenshotAction(full_page=req.full_page_screenshot))
    return batch


@router.post("", response_model=ScrapeResponse)
async def scrape(
    req: ScrapeRequest, request: Request, wiring: Wiring = Depends(get_wiring)
) -> ScrapeResponse:
    # Same tenant-mismatch dance as routes/sessions.py's open_session --
    # `authed` is non-None only on the gated `/v1/scrape` mount.
    authed = await optional_authed_tenant(request, wiring)
    if authed is not None and authed.tenant != req.tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch: api key does not own tenant")

    domain = urlparse(req.url).hostname
    if not domain:
        raise HTTPException(
            status_code=400, detail=f"cannot determine a domain from url {req.url!r}"
        )

    identity = IdentityKey(tenant=req.tenant, domain=domain, name=f"scrape-{uuid.uuid4().hex}")
    owner = f"{req.tenant}:scrape"
    requests_total.labels(tenant=req.tenant, route="scrape").inc()

    async def _opener() -> ContextRef:
        profile_dir = resolve_profile_dir(wiring.profiles_root, identity)
        profile_dir.mkdir(parents=True, exist_ok=True)
        # pick_ephemeral(), not get_or_assign(): this identity is opened
        # exactly once, so there is nothing to keep "sticky" for -- see that
        # method's docstring for why get_or_assign() would leak a permanent,
        # never-read-again Redis key per scrape call instead.
        proxy = wiring.proxy_pinner.pick_ephemeral(identity) if wiring.proxy_pinner else None
        return await wiring.driver.open(
            identity,
            profile_dir,
            proxy,
            headful=False,
            egress=EgressPolicy(),
            block_popups=True,
            enable_cdp=False,
        )
        # No vault load/restore here (contrast routes/sessions.py's
        # _opener()): identity.is_temporary is always true for a freshly
        # minted scrape identity, and a one-shot identity never had a vault
        # entry to restore in the first place.

    batch = _build_batch(req)

    started = time.monotonic()
    with scrape_duration_seconds.time():
        ctx, _lease = await wiring.registry.acquire(
            identity, owner, wiring.lease_ttl_seconds, _opener
        )
        try:
            result = await wiring.driver.execute(ctx, batch)
        finally:
            # evict() + close() + delete the profile dir immediately, never
            # registry.release() (which would park this one-shot context in
            # the warm IDLE pool -- pointless, since `name` is never reused,
            # and real disk-fill risk at scale). Best-effort: a teardown
            # failure must not mask the scrape result (or its error) the
            # caller is waiting on.
            try:
                await wiring.registry.evict(identity)
                await wiring.driver.close(ctx)
            except Exception:
                log.warning("scrape.teardown_failed", url=req.url)
            finally:
                delete_profile_dir(wiring.profiles_root, identity)

    duration_ms = (time.monotonic() - started) * 1000

    # Non-strict: a pre-extract action that unexpectedly navigates aborts
    # the rest of the batch (`result.sequence_aborted`), which can leave
    # `result.extracts` shorter than `req.formats` -- surfaced as a
    # descriptive `error` below rather than a raised exception, matching
    # this route's "always return a Document, even a partial one" contract.
    extracted = dict(zip(req.formats, result.extracts, strict=False))
    screenshot_b64 = (
        base64.b64encode(result.screenshots[0]).decode("ascii") if result.screenshots else None
    )
    error = None
    if result.sequence_aborted:
        error = "page navigated away during a pre-extract action; some formats may be missing"

    return ScrapeResponse(
        success=True,
        data=DocumentOut(
            document_id=str(uuid.uuid4()),
            url=req.url,
            markdown=extracted.get("markdown"),
            text=extracted.get("text"),
            html=extracted.get("html"),
            links=[],
            # `driver.execute()`'s ActionResult has no title/status_code
            # field yet (NavigateAction returns nothing) -- always None
            # rather than fabricated, same "document what the engine can't
            # produce yet" choice as spi.scrape.Document.raw_html.
            screenshot=screenshot_b64,
            metadata=ScrapeMetadataOut(
                title=None,
                status_code=None,
                tier_used=req.tier,
                node_id=ctx.node_id,
                duration_ms=duration_ms,
                source_url=req.url,
            ),
            error=error,
        ),
    )
