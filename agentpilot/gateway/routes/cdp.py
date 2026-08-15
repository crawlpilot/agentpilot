"""`GET /{id}/cdp/json/version` (HTTP discovery -- Chrome's own `/json/
version` shape, with `webSocketDebuggerUrl` rewritten to point at our own WS
sibling route) + `WS /{id}/cdp` (raw, full-duplex CDP relay -- Runtime
included, opt-in only via `enable_cdp=True` at session-open time).

No baked-in path prefix, same reasoning as `routes/sessions.py`: `app.py`
mounts this router only on the `worker` role, at `/internal/sessions`. A
`gateway` serves the tenant-facing `/v1/sessions/.../cdp` via
`routes/cdp_proxy.py` instead.

Auth is split by transport, not copied wholesale from `live_view.py`: the
discovery GET is a plain HTTP route, so it authenticates the same way every
other route in `sessions.py` does (`Authorization: Bearer` via
`optional_authed_tenant`) -- *not* `?api_key=`, which is a WS-handshake-only
convention (browsers can't set custom headers on a WS handshake). The GET's
*response*, however, must embed a working credential in `webSocketDebuggerUrl`
for the WS follow-up connect, so it separately re-extracts the raw
bearer-token string from this same request (`auth_deps` never retains a
resolved `AuthedTenant`'s plaintext key -- only a hash is ever persisted, see
`auth/store.py`) and echoes it into `?api_key=`. The WS route itself uses
`?api_key=`, matching `live_view.py` exactly.

`CdpEndpointCapable` is an *optional* capability: a driver without it, or a
session not opened with `enable_cdp=True`, gets a clean rejection here
(`CdpNotAvailable` on the HTTP route, a WS close code on the WS route) rather
than a route that was never wired up.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import structlog
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import ClientConnection

from agentpilot.gateway.auth_deps import bearer_token, optional_authed_tenant, resolve_query_api_key
from agentpilot.gateway.wiring import Session, Wiring, get_wiring
from agentpilot.spi.cdp import CdpEndpointCapable
from agentpilot.spi.errors import CdpNotAvailable

log = structlog.get_logger(__name__)

router = APIRouter(tags=["cdp"])

_UNAUTHORIZED = 4401
_NOT_FOUND = 4404
_UNSUPPORTED = 4501
_BAD_UPSTREAM = 4502
_CDP_DISABLED = 4503
_MAX_DURATION_EXCEEDED = 4504


def _get_session(wiring: Wiring, session_id: str) -> Session:
    session = wiring.sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    return session


def _rewrite_ws_url(request_base_url: str, session_id: str, api_key: str | None) -> str:
    ws_base = (
        str(request_base_url).replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    )
    url = f"{ws_base}/{session_id}/cdp"
    return f"{url}?api_key={api_key}" if api_key is not None else url


@router.get("/{session_id}/cdp/json/version")
async def cdp_json_version(
    session_id: str, request: Request, wiring: Wiring = Depends(get_wiring)
) -> dict[str, Any]:
    session = _get_session(wiring, session_id)
    authed = await optional_authed_tenant(request, wiring)
    if authed is not None and authed.tenant != session.identity.tenant:
        raise HTTPException(status_code=403, detail="tenant mismatch")

    driver = wiring.driver
    if not isinstance(driver, CdpEndpointCapable):
        raise CdpNotAvailable("driver does not support CDP")
    cdp_http_base = await driver.cdp_http_base(session.ctx)
    if cdp_http_base is None:
        raise CdpNotAvailable(f"session {session_id!r} was not opened with enable_cdp=True")

    try:
        resp = await wiring.cdp_http_client.get(f"{cdp_http_base}/json/version")
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="local CDP endpoint unreachable") from exc

    payload: dict[str, Any] = resp.json()
    token = bearer_token(request.headers.get("authorization"))
    payload["webSocketDebuggerUrl"] = _rewrite_ws_url(str(request.base_url), session_id, token)
    return payload


async def _pump_downstream_to_upstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    while True:
        msg = await websocket.receive_text()
        await upstream.send(msg)


async def _pump_upstream_to_downstream(websocket: WebSocket, upstream: ClientConnection) -> None:
    async for msg in upstream:
        await websocket.send_text(msg if isinstance(msg, str) else msg.decode())


async def _renew_loop(wiring: Wiring, session: Session) -> None:
    """Long-lived external CDP connections may never call `POST /execute`
    (the thing that currently renews leases) -- without this, the P1 reaper
    could reclaim the context mid-connection."""

    interval = wiring.lease_ttl_seconds / 2
    while True:
        await asyncio.sleep(interval)
        try:
            await wiring.registry.renew(session.lease_id)
        except KeyError:
            log.warning("cdp.lease_reclaimed_mid_connection", session_id=session.session_id)
            return


@router.websocket("/{session_id}/cdp")
async def cdp_relay(websocket: WebSocket, session_id: str, api_key: str | None = None) -> None:
    wiring = await get_wiring()
    session = wiring.sessions.get(session_id)
    if session is None:
        await websocket.close(code=_NOT_FOUND, reason="no such session")
        return

    if api_key is not None:
        authed = await resolve_query_api_key(wiring, api_key)
        if authed is None or authed.tenant != session.identity.tenant:
            await websocket.close(code=_UNAUTHORIZED, reason="invalid api key")
            return

    driver = wiring.driver
    if not isinstance(driver, CdpEndpointCapable):
        await websocket.close(code=_UNSUPPORTED, reason="driver does not support CDP")
        return
    cdp_http_base = await driver.cdp_http_base(session.ctx)
    if cdp_http_base is None:
        await websocket.close(
            code=_CDP_DISABLED, reason="session was not opened with enable_cdp=True"
        )
        return

    try:
        resp = await wiring.cdp_http_client.get(f"{cdp_http_base}/json/version")
        upstream_ws_url = resp.json()["webSocketDebuggerUrl"]
    except (httpx.HTTPError, KeyError):
        await websocket.close(code=_BAD_UPSTREAM, reason="local CDP endpoint unreachable")
        return

    await websocket.accept()
    renew_task = asyncio.create_task(_renew_loop(wiring, session))
    try:
        # max_size=None: CDP payloads (Page.captureScreenshot,
        # Network.getResponseBody on a large page, ...) routinely exceed
        # websockets' default 1MiB client frame-size cap.
        async with websockets.connect(upstream_ws_url, max_size=None) as upstream:
            sender = asyncio.create_task(_pump_downstream_to_upstream(websocket, upstream))
            receiver = asyncio.create_task(_pump_upstream_to_downstream(websocket, upstream))
            timeout_task = asyncio.create_task(asyncio.sleep(wiring.cdp_max_connection_seconds))
            done, pending = await asyncio.wait(
                {sender, receiver, timeout_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await asyncio.gather(*pending, return_exceptions=False)
            if timeout_task in done:
                await websocket.close(
                    code=_MAX_DURATION_EXCEEDED, reason="max cdp connection duration exceeded"
                )
    finally:
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew_task
