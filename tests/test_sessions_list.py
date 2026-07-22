"""`routes/sessions.py`'s `list_sessions` -- unit-level, no HTTP layer (this
repo's existing convention, see `test_proxy_pinning.py`/`test_redis_registry.py`):
calls the route function directly against fake `Wiring`/`Request` stand-ins
instead of standing up a `TestClient`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentpilot.gateway.routes.sessions import list_sessions
from agentpilot.gateway.wiring import Session
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState, Lease, LeaseId


class _FakeHeaders:
    def get(self, _key: str) -> str | None:
        return None


class _FakeRequest:
    headers = _FakeHeaders()


class _FakeRegistry:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def snapshot(self) -> list:
        return self._rows


class _FakeApiKeys:
    async def resolve(self, _plaintext: str) -> None:
        return None


class _FakeWiring:
    def __init__(self, sessions: dict[str, Session], rows: list) -> None:
        self.sessions = sessions
        self.registry = _FakeRegistry(rows)
        self.api_keys = _FakeApiKeys()


def _identity(tenant: str, name: str) -> IdentityKey:
    return IdentityKey(tenant=tenant, domain="example.com", name=name)


def _ctx(identity: IdentityKey, pid: int | None = None) -> ContextRef:
    return ContextRef(context_id="ctx-1", identity=identity, state=ContextState.ACTIVE, pid=pid)


def _lease(identity: IdentityKey, ctx: ContextRef, lease_id: str) -> Lease:
    return Lease(
        lease_id=LeaseId(lease_id),
        identity=identity,
        owner="owner",
        acquired_at=datetime.now(UTC),
        ttl_seconds=300.0,
        context_ref=ctx,
    )


def _session(session_id: str, identity: IdentityKey, ctx: ContextRef, lease_id: str) -> Session:
    return Session(
        session_id=session_id,
        identity=identity,
        ctx=ctx,
        lease_id=LeaseId(lease_id),
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
    )


async def test_list_sessions_marks_live_lease_as_active() -> None:
    identity = _identity("acme", "alice")
    ctx = _ctx(identity)
    lease = _lease(identity, ctx, "lease-1")
    session = _session("sess-1", identity, ctx, "lease-1")
    wiring = _FakeWiring({"sess-1": session}, [(identity, ctx, lease, None)])

    result = await list_sessions(_FakeRequest(), tenant=None, wiring=wiring)

    assert len(result.sessions) == 1
    out = result.sessions[0]
    assert out.state == "active"
    assert out.lease_expires_at == pytest.approx(lease.acquired_at.timestamp() + 300.0)


async def test_list_sessions_marks_reclaimed_lease_as_expired() -> None:
    identity = _identity("acme", "bob")
    ctx = _ctx(identity)
    session = _session("sess-2", identity, ctx, "stale-lease")
    # No matching lease in the registry snapshot -- the reaper reclaimed it
    # since this process's `wiring.sessions` entry was last touched.
    wiring = _FakeWiring({"sess-2": session}, [])

    result = await list_sessions(_FakeRequest(), tenant=None, wiring=wiring)

    assert result.sessions[0].state == "expired"
    assert result.sessions[0].lease_expires_at is None


async def test_list_sessions_filters_by_tenant_query_param() -> None:
    acme = _identity("acme", "alice")
    globex = _identity("globex", "carol")
    sessions = {
        "sess-acme": _session("sess-acme", acme, _ctx(acme), "l1"),
        "sess-globex": _session("sess-globex", globex, _ctx(globex), "l2"),
    }
    wiring = _FakeWiring(sessions, [])

    result = await list_sessions(_FakeRequest(), tenant="acme", wiring=wiring)

    assert [s.session_id for s in result.sessions] == ["sess-acme"]


async def test_list_sessions_reports_rss_when_pid_known(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentpilot.gateway.routes.sessions as sessions_module

    monkeypatch.setattr(sessions_module, "_read_pid_rss_mb", lambda _pid: 123.5)
    identity = _identity("acme", "dana")
    ctx = _ctx(identity, pid=4242)
    session = _session("sess-3", identity, ctx, "lease-3")
    wiring = _FakeWiring({"sess-3": session}, [])

    result = await list_sessions(_FakeRequest(), tenant=None, wiring=wiring)

    assert result.sessions[0].rss_mb == 123.5
