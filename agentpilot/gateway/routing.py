"""Shared `session_id -> worker` routing-table lookup, used by both
`routes/gateway_proxy.py` (HTTP) and `routes/live_view_proxy.py` (WebSocket)
so the Redis key scheme and 404 behavior are defined in exactly one place.
"""

from __future__ import annotations

from fastapi import HTTPException

from agentpilot.gateway.wiring import Wiring


def session_route_key(session_id: str) -> str:
    return f"session:{session_id}"


async def resolve_worker(wiring: Wiring, session_id: str) -> str:
    assert wiring.redis is not None  # role=="gateway" always has AGENTPILOT_REDIS_URL
    raw = await wiring.redis.get(session_route_key(session_id))
    if raw is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")
    return raw.decode() if isinstance(raw, bytes) else raw
