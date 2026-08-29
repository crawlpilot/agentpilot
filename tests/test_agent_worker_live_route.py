"""Unit tests for `AgentWorkerLoop`'s live-view route publishing -- the two
`SessionPlacer` calls only, against a recording fake. No Postgres, no Redis, no
browser (the full run path lives in `tests/driver_contract/`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentpilot.control.identity import identity_for
from agentpilot.jobs.agent_worker_loop import AgentWorkerLoop
from agentpilot.spi.identity import IdentityRef


@dataclass
class _Ctx:
    node_id: str


@dataclass
class _Session:
    ctx: _Ctx
    identity: IdentityRef


class _RecordingPlacer:
    def __init__(self, *, fail: bool = False) -> None:
        self.committed: list[tuple[str, str]] = []
        self.forgotten: list[tuple[str, str]] = []
        self._fail = fail

    async def commit_route(
        self,
        session_id: str,
        node_id: str,
        identity: IdentityRef,
        tier: str,
        ttl_seconds: float,
    ) -> None:
        if self._fail:
            raise RuntimeError("redis is down")
        self.committed.append((session_id, node_id))

    async def forget_route(self, session_id: str, node_id: str) -> None:
        if self._fail:
            raise RuntimeError("redis is down")
        self.forgotten.append((session_id, node_id))


def _loop(placer: Any) -> AgentWorkerLoop:
    return AgentWorkerLoop(
        store=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        driver=None,  # type: ignore[arg-type]
        profiles_root=None,  # type: ignore[arg-type]
        proxy_pinner=None,
        sessions={},
        placer=placer,
    )


def _session() -> _Session:
    return _Session(
        ctx=_Ctx(node_id="node-a"),
        identity=identity_for("t", "example.com", "agent-run-1"),
    )


async def test_route_is_published_then_forgotten_for_the_same_node() -> None:
    placer = _RecordingPlacer()
    loop = _loop(placer)
    session = _session()

    await loop._publish_live_route("agent-run-1", session, "auto")
    await loop._forget_live_route("agent-run-1", session)

    assert placer.committed == [("agent-run-1", "node-a")]
    assert placer.forgotten == [("agent-run-1", "node-a")]


async def test_route_calls_are_no_ops_without_a_placer() -> None:
    loop = _loop(None)

    await loop._publish_live_route("agent-run-1", _session(), "auto")
    await loop._forget_live_route("agent-run-1", _session())


async def test_a_redis_failure_never_escapes_either_call() -> None:
    """Both are best-effort: the route carries a TTL, so a redis hiccup must
    degrade the watch-along UI, never fail a run that has done its work."""

    loop = _loop(_RecordingPlacer(fail=True))

    await loop._publish_live_route("agent-run-1", _session(), "auto")
    await loop._forget_live_route("agent-run-1", _session())
