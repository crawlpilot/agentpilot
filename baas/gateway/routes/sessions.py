"""The real session-lifecycle implementation: open -> execute (batched) ->
release. No baked-in path prefix -- `app.py` mounts this same router at
`/v1/sessions` (`monolith`/`worker`-serving-itself-in-tests) and at
`/internal/sessions` (`worker`'s VPC-internal surface), per
`baas.gateway.role`. A `gateway`-role process never mounts this router at
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

import structlog
from fastapi import APIRouter, Depends, HTTPException

from baas.gateway.schemas import (
    ActionResultOut,
    ArtifactRefOut,
    AXSnapshotOut,
    ClickActionIn,
    ExecuteJsActionIn,
    ExecuteRequest,
    ExtractActionIn,
    FillActionIn,
    GoBackActionIn,
    HoverActionIn,
    NavigateActionIn,
    PressActionIn,
    ScreenshotActionIn,
    ScrollActionIn,
    SelectOptionActionIn,
    SessionMetadata,
    SessionOpenRequest,
    SessionOpenResponse,
    SnapshotActionIn,
    SnapshotNodeOut,
    WaitActionIn,
)
from baas.gateway.wiring import Session, Wiring, get_wiring
from baas.identity.profile_store import resolve_profile_dir
from baas.observability.metrics import (
    execute_duration_seconds,
    requests_total,
    session_open_duration_seconds,
)
from baas.spi import actions as spi_actions
from baas.spi.egress import EgressPolicy
from baas.spi.errors import NodeLost
from baas.spi.identity import IdentityKey
from baas.spi.lease import ContextRef

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions"])

_ACTION_CONVERTERS = {
    NavigateActionIn: lambda a: spi_actions.NavigateAction(url=a.url, timeout_ms=a.timeout_ms),
    GoBackActionIn: lambda a: spi_actions.GoBackAction(),
    SnapshotActionIn: lambda a: spi_actions.SnapshotAction(
        viewport_only=a.viewport_only,
        max_nodes=a.max_nodes,
        roles=tuple(a.roles) if a.roles is not None else None,
    ),
    ExtractActionIn: lambda a: spi_actions.ExtractAction(
        format=a.format, main_content=a.main_content
    ),
    ScreenshotActionIn: lambda a: spi_actions.ScreenshotAction(full_page=a.full_page),
    WaitActionIn: lambda a: spi_actions.WaitAction(ms=a.ms, ref=a.ref),
    ExecuteJsActionIn: lambda a: spi_actions.ExecuteJsAction(script=a.script),
    ClickActionIn: lambda a: spi_actions.ClickAction(ref=a.ref, all=a.all),
    FillActionIn: lambda a: spi_actions.FillAction(ref=a.ref, text=a.text),
    SelectOptionActionIn: lambda a: spi_actions.SelectOptionAction(ref=a.ref, values=a.values),
    HoverActionIn: lambda a: spi_actions.HoverAction(ref=a.ref),
    PressActionIn: lambda a: spi_actions.PressAction(key=a.key),
    ScrollActionIn: lambda a: spi_actions.ScrollAction(direction=a.direction, ref=a.ref),
}


def _to_spi_action(action_in) -> spi_actions.Action:
    return _ACTION_CONVERTERS[type(action_in)](action_in)


def _snapshot_node_out(node) -> SnapshotNodeOut:
    return SnapshotNodeOut(
        epoch=node.epoch,
        ref=node.ref,
        role=node.role,
        name=node.name,
        children=[_snapshot_node_out(c) for c in node.children],
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
    req: SessionOpenRequest, wiring: Wiring = Depends(get_wiring)
) -> SessionOpenResponse:
    identity = IdentityKey(tenant=req.tenant, domain=req.domain, name=req.name)
    owner = f"{req.tenant}:{req.name}"  # P1 has no tenant auth yet; informational until P2
    requests_total.labels(tenant=req.tenant, route="open_session").inc()

    async def _opener() -> ContextRef:
        profile_dir = resolve_profile_dir(wiring.profiles_root, identity)
        # A profile dir existing *before* this call is exactly "warm dir" in
        # vault.py's restore-trigger sense -- restore_state() must never run
        # against it (see Vault's module docstring on the persistent-dir
        # conflict). Checked before mkdir(), since mkdir() would otherwise
        # make every open look "already existed".
        is_fresh = not profile_dir.exists()
        profile_dir.mkdir(parents=True, exist_ok=True)

        proxy = await wiring.proxy_pinner.get_or_assign(identity) if wiring.proxy_pinner else None
        ctx = await wiring.driver.open(identity, profile_dir, proxy, req.headful, EgressPolicy())

        if is_fresh and wiring.vault is not None:
            state = wiring.vault.load(identity)
            if state is not None:
                await wiring.driver.restore_state(ctx, state)
        return ctx

    started = time.monotonic()
    with session_open_duration_seconds.time():
        ctx, lease = await wiring.registry.acquire(
            identity, owner, wiring.lease_ttl_seconds, _opener
        )

    session_id = str(uuid.uuid4())
    wiring.sessions[session_id] = Session(
        session_id=session_id,
        identity=identity,
        ctx=ctx,
        lease_id=lease.lease_id,
        tier=req.tier,
        headful=req.headful,
        block_popups=req.block_popups,
    )

    return SessionOpenResponse(
        session_id=session_id,
        metadata=SessionMetadata(
            tier_used=req.tier,
            node_id=ctx.node_id,
            duration_ms=(time.monotonic() - started) * 1000,
        ),
    )


@router.post("/{session_id}/execute", response_model=ActionResultOut)
async def execute_session(
    session_id: str, req: ExecuteRequest, wiring: Wiring = Depends(get_wiring)
) -> ActionResultOut:
    session = _get_session(wiring, session_id)
    requests_total.labels(tenant=session.identity.tenant, route="execute_session").inc()
    try:
        await wiring.registry.renew(session.lease_id)
    except KeyError as exc:
        raise NodeLost(f"session {session_id!r}'s underlying context was reclaimed") from exc

    actions = [_to_spi_action(a) for a in req.actions]
    with execute_duration_seconds.time():
        result = await wiring.driver.execute(session.ctx, actions)
    return _to_action_result_out(result)


@router.delete("/{session_id}")
async def release_session(session_id: str, wiring: Wiring = Depends(get_wiring)) -> dict:
    session = _get_session(wiring, session_id)
    requests_total.labels(tenant=session.identity.tenant, route="release_session").inc()

    if wiring.vault is not None:
        # Checkpoint on release-to-IDLE (not only at destroy) bounds
        # node-loss staleness to one session's delta -- plan.md's vault
        # trigger #2. Best-effort: a vault write failure must not block the
        # release itself (the context still needs to go IDLE either way).
        try:
            state = await wiring.driver.export_state(session.ctx)
            wiring.vault.save(session.identity, state)
        except Exception:
            log.warning("sessions.vault_checkpoint_failed", session_id=session_id)

    await wiring.registry.release(session.lease_id)
    wiring.sessions.pop(session_id, None)
    return {"success": True, "state": "idle"}
