"""`gateway`-role's `/v1/scrape` -- a thin reverse proxy to whichever worker
`wiring.placer` places this one-shot scrape on (the real logic is
`routes/scrape.py`, proxied here to that worker's `/internal/scrape`).
Mounted **instead of** `routes/scrape.py` when `wiring.role == "gateway"`
(see `app.py`) -- a gateway process never imports/touches `agentpilot.driver`.

Simpler than `gateway_proxy.py`'s `open_session`: that route is necessarily
two-phase (reserve a node, proxy, *then* commit a route) because the worker
mints a `session_id` that a *later* call needs to look up again. `/v1/scrape`
has no later call -- the whole lifecycle is this one request -- so there is
no route to commit, just `place()` -> proxy -> (on failure)
`release_reservation()`.

The identity handed to `placer.place()` here is a per-(tenant, domain)
placeholder (`name="scrape"`), deliberately *not* the random per-call
identity `routes/scrape.py` mints worker-side for the real `registry
.acquire()`: `place()` only needs an identity to pick a node (affinity-first,
then least-loaded) and optimistically bump that node's capacity count, which
self-heals via the node's own next heartbeat regardless (see `placer.py`'s
`release_reservation()` docstring) -- it doesn't need to be the exact
identity that ends up ACTIVE in the registry. Using a stable placeholder
means repeated scrapes of the same tenant+domain get a mild, harmless
least-loaded-with-locality bias without any cross-process identity
coordination, and can never spuriously trip `place_session.lua`'s
affinity/`LeaseConflict` check -- no worker ever registers `active:{tenant}/
{domain}/scrape` as ACTIVE, since every real open uses a randomly named
identity instead.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Response

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.auth_deps import require_tenant_auth
from agentpilot.gateway.routing import resolve_node_addr
from agentpilot.gateway.schemas import ScrapeRequest
from agentpilot.gateway.wiring import Wiring, get_wiring
from agentpilot.observability.metrics import requests_total, scrape_duration_seconds
from agentpilot.spi.errors import NodeLost
from agentpilot.spi.identity import IdentityKey

log = structlog.get_logger(__name__)

router = APIRouter(tags=["scrape-gateway-proxy"])


@router.post("")
async def scrape(
    req: ScrapeRequest,
    wiring: Wiring = Depends(get_wiring),
    authed: AuthedTenant = Depends(require_tenant_auth),
) -> Response:
    if req.tenant != authed.tenant:
        req = req.model_copy(update={"tenant": authed.tenant})
    requests_total.labels(tenant=req.tenant, route="scrape").inc()
    started = time.monotonic()

    domain = urlparse(req.url).hostname
    if not domain:
        raise HTTPException(
            status_code=400, detail=f"cannot determine a domain from url {req.url!r}"
        )

    placement_identity = IdentityKey(tenant=req.tenant, domain=domain, name="scrape")
    node_id = await wiring.placer.place(placement_identity, wiring.affinity_ttl_seconds)
    try:
        addr = await resolve_node_addr(wiring, node_id)
    except NodeLost:
        # Half-dead: died between a fresh place() and this lookup -- same
        # one-retry pattern gateway_proxy.py's open_session uses, bounded to
        # one so a sick node can't absorb fleet latency.
        await wiring.placer.release_reservation(node_id)
        node_id = await wiring.placer.place(placement_identity, wiring.affinity_ttl_seconds)
        addr = await resolve_node_addr(wiring, node_id)

    with scrape_duration_seconds.time():
        try:
            resp = await wiring.http_client.post(
                f"{addr}/internal/scrape", json=req.model_dump()
            )
        except httpx.TransportError as exc:
            await wiring.placer.release_reservation(node_id)
            log.error("scrape_proxy.worker_unreachable", addr=addr, error=str(exc))
            raise HTTPException(status_code=502, detail="worker unreachable") from exc

    if resp.status_code != 200:
        await wiring.placer.release_reservation(node_id)

    log.info(
        "scrape_proxy.scrape", duration_ms=(time.monotonic() - started) * 1000, node_id=node_id
    )
    return Response(
        content=resp.content, status_code=resp.status_code, media_type="application/json"
    )
