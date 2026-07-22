"""FastAPI `Depends()` glue for `agentpilot.auth` -- kept out of that package so it
stays gateway-agnostic (import-linter layers: gateway -> ... -> auth -> spi).

Two enforcement shapes are used across the gateway, matching where a route is
mounted:
  - `require_tenant_auth` / `require_admin`: hard-gating dependencies, added
    via `include_router(..., dependencies=[Depends(...)])` *only* on a
    tenant-facing inclusion (`/v1/sessions`, `/v1/api-keys`) -- raises 401 on
    a missing/invalid credential. Never added to `/internal/sessions`
    (worker's VPC-internal, trusted-network surface).
  - `optional_authed_tenant`: a non-raising re-derivation of the same tenant,
    called directly (not via `Depends`) inside a shared-router handler (e.g.
    `routes/sessions.py`, mounted at both prefixes) so it can tell whether
    *this* call came in through the gated mount or the internal one, without
    running the auth check twice or duplicating the route.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.wiring import Wiring, get_wiring


def bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


async def _resolve_token(wiring: Wiring, token: str | None) -> AuthedTenant | None:
    if not token:
        return None
    record = await wiring.api_keys.resolve(token)
    if record is None:
        return None
    return AuthedTenant(tenant=record.tenant, key_id=record.key_id)


async def require_tenant_auth(
    authorization: str | None = Header(default=None),
    wiring: Wiring = Depends(get_wiring),
) -> AuthedTenant:
    authed = await _resolve_token(wiring, bearer_token(authorization))
    if authed is None:
        raise HTTPException(status_code=401, detail="missing or invalid api key")
    return authed


async def require_admin(
    authorization: str | None = Header(default=None),
    wiring: Wiring = Depends(get_wiring),
) -> None:
    token = bearer_token(authorization)
    if not token or not wiring.admin_token or not hmac.compare_digest(token, wiring.admin_token):
        raise HTTPException(status_code=401, detail="invalid admin token")


async def optional_authed_tenant(request: Request, wiring: Wiring) -> AuthedTenant | None:
    """Non-raising: `None` means either no credential was sent (the internal,
    trusted-network mount) or it didn't resolve. Callers on the gated mount
    can rely on this being non-`None` since `require_tenant_auth` already
    rejected the request otherwise; callers on the internal mount treat `None`
    as "trust the caller", exactly matching pre-auth behavior."""

    return await _resolve_token(wiring, bearer_token(request.headers.get("authorization")))


async def resolve_query_api_key(wiring: Wiring, api_key: str | None) -> AuthedTenant | None:
    """WebSocket variant: browsers can't set custom headers on a WS handshake,
    so the key travels as `?api_key=...` instead (see `routes/live_view.py`)."""

    return await _resolve_token(wiring, api_key)
