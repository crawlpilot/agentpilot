"""Unit tests for `RecipeWorkerLoop`'s live-view route publishing, against
recording fakes. No Postgres, no Redis, no browser.

The counterpart to `tests/test_agent_worker_live_route.py`, and it exists
because the difference between the two loops was a bug. `AgentWorkerLoop`
re-publishes its route on every agent step; `RecipeWorkerLoop` published once
and never again. `commit_route` stamps `session:{id}` with `lease_ttl_seconds`
(300s by default), a build runs for minutes and then parks for up to
`assist_timeout_s` (1800s), and the only other thing that refreshes that key is
a *successful* execute through the gateway proxy. So the route lapsed before
anybody opened the assist panel, `resolve_route` 404'd every picker injection,
and -- because the TTL is only extended after a successful execute -- nothing
could revive it. The session still listed in `/v1/sessions` (that listing reads
the workers' in-process dicts and never consults the route), so it looked
healthy while silently refusing to pick.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from agentpilot.control.identity import identity_for
from agentpilot.jobs.recipe_worker_loop import RecipeWorkerLoop
from crawlpilot.spi.identity import IdentityRef


@dataclass
class _Ctx:
    node_id: str


@dataclass
class _Session:
    ctx: _Ctx
    identity: IdentityRef


@dataclass
class _Run:
    run_id: str = "run-1"
    lock: str = "lock-1"


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


class _RecordingStore:
    def __init__(self) -> None:
        self.renewals: list[str] = []

    async def renew_lock(self, run_id: str, lock: str) -> None:
        self.renewals.append(run_id)


def _loop(placer: Any, *, store: Any = None, stale_after: float = 3.0) -> RecipeWorkerLoop:
    return RecipeWorkerLoop(
        store=store,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        driver=None,  # type: ignore[arg-type]
        profiles_root=None,  # type: ignore[arg-type]
        proxy_pinner=None,
        sessions={},
        placer=placer,
        stale_after_seconds=stale_after,
    )


def _session() -> _Session:
    return _Session(
        ctx=_Ctx(node_id="node-a"),
        identity=identity_for("t", "example.com", "recipe-0"),
    )


async def test_route_is_published_then_forgotten_for_the_same_node() -> None:
    placer = _RecordingPlacer()
    loop = _loop(placer)
    session = _session()

    await loop._publish_live_route("recipe-run-1", session, "auto")
    await loop._forget_live_route("recipe-run-1", session)

    assert placer.committed == [("recipe-run-1", "node-a")]
    assert placer.forgotten == [("recipe-run-1", "node-a")]


async def test_route_calls_are_no_ops_without_a_placer() -> None:
    loop = _loop(None)

    await loop._publish_live_route("recipe-run-1", _session(), "auto")
    await loop._forget_live_route("recipe-run-1", _session())


async def test_a_redis_failure_never_escapes_either_call() -> None:
    """Both are best-effort: the route carries a TTL, so a redis hiccup must
    degrade the watch-along UI, never fail a run that has done its work."""

    loop = _loop(_RecordingPlacer(fail=True))

    await loop._publish_live_route("recipe-run-1", _session(), "auto")
    await loop._forget_live_route("recipe-run-1", _session())


async def test_the_heartbeat_republishes_a_registered_route() -> None:
    """The fix. A run that outlives the route TTL keeps a resolvable route,
    because the heartbeat re-commits it on the same cadence it renews the DB
    claim lock."""

    placer = _RecordingPlacer()
    store = _RecordingStore()
    loop = _loop(placer, store=store)
    run = _Run()

    await loop._publish_live_route("recipe-run-1", _session(), "auto")
    loop._live_routes[run.run_id] = ("recipe-run-1", _session(), "auto")

    beat = asyncio.create_task(loop._heartbeat(run))  # type: ignore[arg-type]
    # `_heartbeat` sleeps `max(stale_after/3, 1.0)` before its first pass.
    await asyncio.sleep(1.2)
    beat.cancel()
    await asyncio.gather(beat, return_exceptions=True)

    # The publish at open, plus at least one from the heartbeat.
    assert len(placer.committed) >= 2
    assert set(placer.committed) == {("recipe-run-1", "node-a")}
    assert store.renewals == ["run-1"]


async def test_the_heartbeat_renews_the_lock_with_no_route_registered() -> None:
    """A `codegen` run opens no browser session and so registers no route. The
    claim lock must still be renewed -- the route is the addition, not a
    precondition."""

    placer = _RecordingPlacer()
    store = _RecordingStore()
    loop = _loop(placer, store=store)

    beat = asyncio.create_task(loop._heartbeat(_Run()))  # type: ignore[arg-type]
    await asyncio.sleep(1.2)
    beat.cancel()
    await asyncio.gather(beat, return_exceptions=True)

    assert store.renewals == ["run-1"]
    assert placer.committed == []
