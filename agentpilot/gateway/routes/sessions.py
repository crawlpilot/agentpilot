"""The real session-lifecycle implementation: open -> execute (batched) ->
release. No baked-in path prefix -- `app.py` mounts this same router at
`/v1/sessions` (`monolith`/`worker`-serving-itself-in-tests) and at
`/internal/sessions` (`worker`'s VPC-internal surface), per
`agentpilot.gateway.role`. A `gateway`-role process never mounts this router at
all; it mounts `routes/gateway_proxy.py` at `/v1/sessions` instead, which
proxies to a worker's `/internal/sessions` copy of these exact routes.

`POST` (open) -> `POST /{id}/execute` (batched Action list, renewing the P1
lease each call) -> `DELETE /{id}` (release to IDLE -- the `session.Reaper`
is what actually destroys IDLE contexts now, on its own schedule, not this
route).
"""

from __future__ import annotations

import base64
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from agentpilot.gateway.action_conversion import to_spi_action
from agentpilot.gateway.auth_deps import optional_authed_tenant
from agentpilot.gateway.schemas import (
    ActionResultOut,
    ArtifactRefOut,
    AXSnapshotOut,
    BoundingBoxOut,
    ExecuteRequest,
    SessionListOut,
    SessionMetadata,
    SessionOpenRequest,
    SessionOpenResponse,
    SessionOut,
    SnapshotNodeOut,
    TabInfoOut,
)
from agentpilot.gateway.wiring import Session, Wiring, get_wiring
from agentpilot.observability.metrics import (
    execute_duration_seconds,
    requests_total,
    session_open_duration_seconds,
)
from agentpilot.session.interactive import (
    execute_on_session,
    open_interactive_session,
    release_interactive_session,
)
from agentpilot.session.reaper import _read_pid_rss_mb
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.errors import NodeLost
from agentpilot.spi.snapshot import SnapshotNode

router = APIRouter(tags=["sessions"])


def _snapshot_node_out(node: SnapshotNode) -> SnapshotNodeOut:
    bbox = (
        BoundingBoxOut(x=node.bbox.x, y=node.bbox.y, width=node.bbox.width, height=node.bbox.height)
        if node.bbox
        else None
    )
    return SnapshotNodeOut(
        epoch=node.epoch,
        ref=node.ref,
        role=node.role,
        name=node.name,
        children=[_snapshot_node_out(c) for c in node.children],
        bbox=bbox,
    )


def _to_action_result_out(result: spi_actions.ActionResult) -> ActionResultOut:
    return ActionResultOut(
        snapshots=[
            AXSnapshotOut(epoch=s.epoch, root=_snapshot_node_out(s.root)) for s in result.snapshots
        ],
        screenshots=[base64.b64encode(b).decode("ascii") for b in result.screenshots],
        extracts=result.extracts,
        js_returns=result.js_returns,
        downloads=[
            ArtifactRefOut(
                artifact_id=d.artifact_id,
                tenant=d.tenant,
                kind=d.kind,
                size=d.size,
                sha256=d.sha256,
            )
            for d in result.downloads
        ],
        tabs=[
            [
                TabInfoOut(page_id=t.page_id, url=t.url, title=t.title, active=t.active)
                for t in tab_list
            ]
            for tab_list in result.tabs
        ],
        sequence_aborted=result.sequence_aborted,
        page_changed=result.page_changed,
    )


def _get_session(wiring: Wiring, session_id: str) -> Session:
    session = wiring.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    return session


@router.post("", response_model=SessionOpenResponse)
async def open_session(
    req: SessionOpenRequest, request: Request, wiring: Wiring = Depends(get_wiring)
) -> SessionOpenResponse:
    # `authed` is non-None only when this call came in through a gated mount
    # (`/v1/sessions`, via `require_tenant_auth` at `include_router()` time in
    # `app.py`) -- `/internal/sessions` (worker's trusted-network surface)
    # sends no Authorization header, so this stays None there and `req.tenant`
    # is trusted exactly as before auth existed.
    authed = await optional_authed_tenant(request, wiring)
    if authed is not None and authed.tenant != req.tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch: api key does not own tenant")

    requests_total.labels(tenant=req.tenant, route="open_session").inc()

    session_id = str(uuid.uuid4())
    started = time.monotonic()
    with session_open_duration_seconds.time():
        session = await open_interactive_session(
            session_id=session_id,
            tenant=req.tenant,
            domain=req.domain,
            name=req.name,
            tier=req.tier,
            headful=req.headful,
            block_popups=req.block_popups,
            enable_cdp=req.enable_cdp,
            registry=wiring.registry,
            driver=wiring.driver,
            profiles_root=wiring.profiles_root,
            proxy_pinner=wiring.proxy_pinner,
            vault=wiring.vault,
            lease_ttl_seconds=wiring.lease_ttl_seconds,
        )

    wiring.sessions[session_id] = session

    return SessionOpenResponse(
        session_id=session_id,
        metadata=SessionMetadata(
            tier_used=req.tier,
            node_id=session.ctx.node_id,
            duration_ms=(time.monotonic() - started) * 1000,
        ),
    )


@router.get("", response_model=SessionListOut)
async def list_sessions(
    request: Request, tenant: str | None = None, wiring: Wiring = Depends(get_wiring)
) -> SessionListOut:
    """Sourced from `wiring.sessions` (the `session_id` dict), not
    `registry.snapshot()` -- the registry is keyed by `IdentityKey`/lease and
    has no `session_id` at all, since one warm context can be reused across
    many session_ids over its lifetime. `snapshot()` is only consulted here to
    tell whether each session's lease is still live (state="active") or has
    since been reclaimed by the reaper (state="expired") without this
    process's `wiring.sessions` entry having been cleaned up yet.

    On the gated `/v1/sessions` mount, `tenant` is always the authed tenant
    (the query param is ignored if a caller inconsistently passes one) --
    every tenant only ever sees its own sessions. On `/internal/sessions`
    (trusted network, e.g. a future multi-worker fan-out from the gateway),
    `tenant` is an optional filter; omitting it returns every session this
    one process holds.
    """

    authed = await optional_authed_tenant(request, wiring)
    effective_tenant = authed.tenant if authed is not None else tenant

    lease_by_id = {
        lease.lease_id: lease for _, _, lease, _ in await wiring.registry.snapshot() if lease
    }

    out = []
    for session_id, session in wiring.sessions.items():
        if effective_tenant is not None and session.identity.tenant != effective_tenant:
            continue
        lease = lease_by_id.get(session.lease_id)
        rss_mb = _read_pid_rss_mb(session.ctx.pid) if session.ctx.pid is not None else None
        out.append(
            SessionOut(
                session_id=session_id,
                tenant=session.identity.tenant,
                domain=session.identity.domain,
                name=session.identity.name,
                tier=session.tier,
                headful=session.headful,
                enable_cdp=session.enable_cdp,
                node_id=session.ctx.node_id,
                pid=session.ctx.pid,
                rss_mb=rss_mb,
                state="active" if lease is not None else "expired",
                lease_expires_at=(
                    lease.acquired_at.timestamp() + lease.ttl_seconds if lease is not None else None
                ),
            )
        )
    return SessionListOut(sessions=out)


@router.post("/{session_id}/execute", response_model=ActionResultOut)
async def execute_session(
    session_id: str, req: ExecuteRequest, wiring: Wiring = Depends(get_wiring)
) -> ActionResultOut:
    session = _get_session(wiring, session_id)
    requests_total.labels(tenant=session.identity.tenant, route="execute_session").inc()

    actions = [to_spi_action(a) for a in req.actions]
    try:
        with execute_duration_seconds.time():
            result = await execute_on_session(
                session,
                actions,
                registry=wiring.registry,
                driver=wiring.driver,
                page_id=req.page_id,
            )
    except KeyError as exc:
        raise NodeLost(f"session {session_id!r}'s underlying context was reclaimed") from exc
    return _to_action_result_out(result)


@router.delete("/{session_id}")
async def release_session(
    session_id: str, wiring: Wiring = Depends(get_wiring)
) -> dict[str, bool | str]:
    session = _get_session(wiring, session_id)
    requests_total.labels(tenant=session.identity.tenant, route="release_session").inc()

    # Checkpoint on release-to-IDLE (not only at destroy) bounds node-loss
    # staleness to one session's delta -- plan.md's vault trigger #2.
    await release_interactive_session(
        session, registry=wiring.registry, driver=wiring.driver, vault=wiring.vault
    )
    wiring.sessions.pop(session_id, None)
    return {"success": True, "state": "idle"}
