"""`GET /v1/nodes` -- admin-gated fleet visibility for the enterprise UI's
nodes dashboard. Pure read-only projection of the same Redis keys
`SessionPlacer`/`NodeReaper` already use for placement/liveness
(`live_nodes`, `node:{id}`, `capacity:{id}`) -- no new state written here.

Gateway-role only: fleet visibility (`live_nodes`) is a `_init_gateway()`
concept (see `wiring.py`) -- a `worker` never constructs it, so this router is
only mounted for `AGENTPILOT_ROLE=gateway` in `app.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from agentpilot.gateway.schemas import NodeListOut, NodeOut
from agentpilot.gateway.wiring import Wiring, get_wiring

router = APIRouter(tags=["nodes"])


def _decode(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _str_field(raw: dict[bytes | str, bytes | str], key: bytes) -> str | None:
    val = raw.get(key)
    return _decode(val) if val is not None else None


def _float_field(raw: dict[bytes | str, bytes | str], key: bytes) -> float | None:
    val = _str_field(raw, key)
    return float(val) if val else None


def _int_field(raw: dict[bytes | str, bytes | str], key: bytes) -> int | None:
    val = _str_field(raw, key)
    return int(float(val)) if val else None


@router.get("", response_model=NodeListOut)
async def list_nodes(wiring: Wiring = Depends(get_wiring)) -> NodeListOut:
    redis = wiring.redis
    assert redis is not None  # guaranteed by _init_gateway(), which requires it

    node_ids = [_decode(n) for n in await redis.smembers("live_nodes")]

    nodes = []
    for node_id in node_ids:
        node_info = await redis.hgetall(f"node:{node_id}")
        capacity = await redis.hgetall(f"capacity:{node_id}")
        nodes.append(
            NodeOut(
                node_id=node_id,
                addr=_str_field(node_info, b"addr"),
                started_at=_float_field(node_info, b"started_at"),
                live=bool(capacity),
                max_contexts=_int_field(capacity, b"max_contexts"),
                active=_int_field(capacity, b"active"),
                idle=_int_field(capacity, b"idle"),
                mem_used_pct=_float_field(capacity, b"mem_used_pct"),
                cpu_used_pct=_float_field(capacity, b"cpu_used_pct"),
            )
        )
    return NodeListOut(nodes=nodes)
