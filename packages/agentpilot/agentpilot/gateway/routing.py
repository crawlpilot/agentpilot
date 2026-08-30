"""Shared `session_id -> (node_id, addr)` routing-table lookup, used by
`routes/gateway_proxy.py` (HTTP), `routes/live_view_proxy.py`, and
`routes/cdp_proxy.py` (WebSocket) so the Redis key scheme, tenant-ownership
check, and error behavior are defined in exactly one place.

`session:{id}` is a hash (written by `placement.placer.SessionPlacer.
commit_route()`), not a plain string -- carrying `tenant` is what makes the
tenant-ownership check below possible without a second round trip. Resolving
`addr` is a second hop through `node:{node_id}` rather than caching `addr`
directly on the session hash: a `node_id` occasionally outliving its `addr`
(a node restarting under the same id) is exactly the case a single source
of truth for `addr` avoids -- see `agentpilot/placement/node_registry.py`.
"""

from __future__ import annotations

from fastapi import HTTPException

from agentpilot.gateway.wiring import Wiring
from agentpilot.spi.errors import NodeLost


def session_route_key(session_id: str) -> str:
    return f"session:{session_id}"


def node_key(node_id: str) -> str:
    return f"node:{node_id}"


def _decode(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def resolve_node_addr(wiring: Wiring, node_id: str) -> str:
    """Raises `NodeLost` (mapped 502 in `gateway/errors.py`) if the node's
    registration is gone -- died and was cleaned up by the node-reaper, or
    (the "half-dead" case) died between a fresh `place()` and this lookup."""

    assert wiring.redis is not None
    addr = _decode(await wiring.redis.hget(node_key(node_id), "addr"))
    if addr is None:
        raise NodeLost(f"node {node_id!r} is gone")
    return addr


async def resolve_route(wiring: Wiring, session_id: str, authed_tenant: str) -> tuple[str, str]:
    """Returns `(node_id, addr)`. Raises `HTTPException(404)` if the session
    is unknown, `HTTPException(403)` if `authed_tenant` doesn't own it, or
    `NodeLost` if the node it was placed on has since died and been reaped."""

    assert wiring.redis is not None  # role=="gateway" always has AGENTPILOT_REDIS_URL
    raw = await wiring.redis.hgetall(session_route_key(session_id))
    if not raw:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")

    tenant = _decode(raw.get(b"tenant") or raw.get("tenant"))
    if tenant != authed_tenant:
        raise HTTPException(status_code=403, detail="tenant does not own this session")

    node_id = _decode(raw.get(b"node_id") or raw.get("node_id"))
    if not node_id:
        raise HTTPException(status_code=404, detail=f"no session {session_id!r}")

    addr = await resolve_node_addr(wiring, node_id)
    return node_id, addr
