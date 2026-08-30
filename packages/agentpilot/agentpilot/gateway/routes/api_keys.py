"""`POST/GET/DELETE /v1/api-keys` -- the admin-gated control-plane surface
that issues the tenant-facing API keys `routes/sessions.py`/`gateway_proxy.py`
authenticate against. Gated by `require_admin` (a single operator-controlled
`AGENTPILOT_ADMIN_TOKEN`, not per-tenant credentials) at `include_router()` time in
`app.py` -- see `agentpilot.gateway.auth_deps`'s docstring on why a real per-tenant
login is out of scope for this pass (you can't call this endpoint with an API
key you don't have yet, so something has to bootstrap the first one).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentpilot.auth.models import ApiKeyRecord
from agentpilot.gateway.schemas import (
    ApiKeyCreateOut,
    ApiKeyCreateRequest,
    ApiKeyListOut,
    ApiKeyOut,
)
from agentpilot.gateway.wiring import Wiring, get_wiring

router = APIRouter(tags=["api-keys"])


def _to_out(record: ApiKeyRecord) -> ApiKeyOut:
    return ApiKeyOut(
        key_id=record.key_id,
        tenant=record.tenant,
        name=record.name,
        prefix=record.prefix,
        created_at=record.created_at.isoformat(),
        last_used_at=record.last_used_at.isoformat() if record.last_used_at else None,
        revoked_at=record.revoked_at.isoformat() if record.revoked_at else None,
    )


@router.post("", response_model=ApiKeyCreateOut)
async def create_api_key(
    req: ApiKeyCreateRequest, wiring: Wiring = Depends(get_wiring)
) -> ApiKeyCreateOut:
    record, plaintext = await wiring.api_keys.create(req.tenant, req.name)
    return ApiKeyCreateOut(**_to_out(record).model_dump(), api_key=plaintext)


@router.get("", response_model=ApiKeyListOut)
async def list_api_keys(tenant: str, wiring: Wiring = Depends(get_wiring)) -> ApiKeyListOut:
    records = await wiring.api_keys.list(tenant)
    return ApiKeyListOut(api_keys=[_to_out(r) for r in records])


@router.delete("/{key_id}")
async def revoke_api_key(key_id: str, wiring: Wiring = Depends(get_wiring)) -> dict[str, bool]:
    await wiring.api_keys.revoke(key_id)
    return {"success": True}
