"""P2's Redis-backed `RegistryProtocol` implementation -- the "registry.py
-> Redis, same interface" swap `plan.md` calls for. Atomicity comes from the
five Lua scripts in `agentpilot/session/lua/`, loaded once at construction and
invoked via `redis.asyncio.Redis.register_script()` so callers never see
raw Lua or raw Redis commands.

**Two-phase acquire, not a lock held across `driver.open()`**: a Lua script
runs atomically but can't `await` a Python coroutine, so a fresh open can't
hold the identity's "lock" for the whole (possibly multi-second) browser
launch the way the in-memory `Registry`'s `asyncio.Lock` does. Instead:
`acquire_lease.lua` atomically reserves the identity (flips it ACTIVE,
blocking any concurrent acquire) and reports whether a warm `context_id`
already exists; only if not does `opener()` run, followed by
`bind_active_context.lua` recording the result. See that script's docstring
for the one accepted gap this creates (a `LEASE_LOST` race during the open()
window orphans the just-opened context from the registry's view -- rare,
and documented rather than silently papered over with a half-built
compensating-transaction mechanism).

**What lives where**: this class stores only registry *bookkeeping*
(`context_id`, `state`, `pid`, `node_id`, lease fields) in Redis --
never a live Playwright/Patchright object, which still can't leave
`agentpilot.driver`. The actual browser resources stay in the worker process's
`PatchrightDriver._live` dict, keyed by `context_id`; Redis's job is making
the identity -> context_id mapping and the <=1-ACTIVE invariant shared and
crash-resilient across restarts (and, once >1 worker exists, across nodes).
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentpilot.session.registry import Opener
from agentpilot.spi.errors import LeaseConflict
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState, Lease, LeaseId

_LUA_DIR = Path(__file__).resolve().parent / "lua"


def _load(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def active_key(identity: IdentityKey) -> str:
    return f"active:{identity.slug()}"


def _to_context_ref(
    identity: IdentityKey, context_id: str, pid_raw: str, node_id: str, state: ContextState
) -> ContextRef:
    pid = int(pid_raw) if pid_raw else None
    return ContextRef(
        context_id=context_id, identity=identity, state=state, pid=pid, node_id=node_id or "local"
    )


class RedisRegistry:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._acquire = redis.register_script(_load("acquire_lease.lua"))
        self._bind = redis.register_script(_load("bind_active_context.lua"))
        self._renew = redis.register_script(_load("renew_lease.lua"))
        self._release = redis.register_script(_load("release_lease.lua"))
        self._force_release = redis.register_script(_load("force_release.lua"))
        self._evict = redis.register_script(_load("evict.lua"))

    async def acquire(
        self, identity: IdentityKey, owner: str, ttl_seconds: float, opener: Opener
    ) -> tuple[ContextRef, Lease]:
        key = active_key(identity)
        now = time.time()
        lease_id = LeaseId(str(uuid.uuid4()))

        try:
            reuse, context_id, pid_raw, node_id = await self._acquire(
                keys=[key],
                args=[
                    owner,
                    ttl_seconds,
                    lease_id,
                    now,
                    identity.tenant,
                    identity.domain,
                    identity.name,
                ],
            )
        except ResponseError as exc:
            if "LEASE_CONFLICT" in str(exc):
                raise LeaseConflict(
                    f"identity {identity.slug()!r} already has an active session"
                ) from exc
            raise

        context_id = _decode(context_id)
        node_id = _decode(node_id)
        pid_raw = _decode(pid_raw)

        if not reuse:
            try:
                ctx = await opener()
            except BaseException:
                await self._release(keys=[key], args=[lease_id, time.time()])
                raise
            try:
                await self._bind(
                    keys=[key],
                    args=[lease_id, ctx.context_id, str(ctx.pid) if ctx.pid else "", ctx.node_id],
                )
            except ResponseError as exc:
                # See this module's docstring: the lease was reclaimed out
                # from under us while opener() was running. The context we
                # just opened is real but now untracked -- documented gap,
                # not silently swallowed.
                raise LeaseConflict(
                    f"lease for {identity.slug()!r} was reclaimed while opening"
                ) from exc
        else:
            ctx = _to_context_ref(identity, context_id, pid_raw, node_id, ContextState.ACTIVE)

        ctx.state = ContextState.ACTIVE
        lease = Lease(
            lease_id=lease_id,
            identity=identity,
            owner=owner,
            acquired_at=datetime.fromtimestamp(now, UTC),
            ttl_seconds=ttl_seconds,
            context_ref=ctx,
        )
        return ctx, lease

    async def renew(self, lease_id: LeaseId) -> Lease:
        owner_key = f"lease_owner:{lease_id}"
        key_raw = await self._redis.get(owner_key)
        if key_raw is None:
            raise KeyError(f"lease {lease_id!r} was reclaimed")
        key = _decode(key_raw)

        now = time.time()
        try:
            await self._renew(keys=[key], args=[lease_id, now])
        except ResponseError as exc:
            raise KeyError(f"lease {lease_id!r} was reclaimed") from exc

        raw = await self._redis.hgetall(key)
        identity = IdentityKey(
            tenant=_decode(raw.get(b"tenant", b"")),
            domain=_decode(raw.get(b"domain", b"")),
            name=_decode(raw.get(b"name", b"")),
        )
        ctx = _to_context_ref(
            identity,
            _decode(raw.get(b"context_id", b"")),
            _decode(raw.get(b"pid", b"")),
            _decode(raw.get(b"node_id", b"")),
            ContextState.ACTIVE,
        )
        return Lease(
            lease_id=lease_id,
            identity=identity,
            owner=_decode(raw.get(b"owner", b"")),
            acquired_at=datetime.fromtimestamp(now, UTC),
            ttl_seconds=float(_decode(raw.get(b"ttl_seconds", b"0")) or 0),
            context_ref=ctx,
        )

    async def release(self, lease_id: LeaseId) -> None:
        owner_key = f"lease_owner:{lease_id}"
        key_raw = await self._redis.get(owner_key)
        if key_raw is None:
            return
        await self._release(keys=[_decode(key_raw)], args=[lease_id, time.time()])

    async def snapshot(self) -> list[tuple[IdentityKey, ContextRef, Lease | None, float | None]]:
        results: list[tuple[IdentityKey, ContextRef, Lease | None, float | None]] = []
        async for key in self._redis.scan_iter(match="active:*"):
            raw = await self._redis.hgetall(key)
            if not raw:
                continue
            identity = IdentityKey(
                tenant=_decode(raw.get(b"tenant", b"")),
                domain=_decode(raw.get(b"domain", b"")),
                name=_decode(raw.get(b"name", b"")),
            )
            state = ContextState(_decode(raw.get(b"state", b"idle")))
            ctx = _to_context_ref(
                identity,
                _decode(raw.get(b"context_id", b"")),
                _decode(raw.get(b"pid", b"")),
                _decode(raw.get(b"node_id", b"")),
                state,
            )
            lease_id_raw = _decode(raw.get(b"lease_id", b""))
            lease = None
            if lease_id_raw:
                lease = Lease(
                    lease_id=LeaseId(lease_id_raw),
                    identity=identity,
                    owner=_decode(raw.get(b"owner", b"")),
                    acquired_at=datetime.fromtimestamp(
                        float(_decode(raw.get(b"acquired_at", b"0")) or 0), UTC
                    ),
                    ttl_seconds=float(_decode(raw.get(b"ttl_seconds", b"0")) or 0),
                    context_ref=ctx,
                )
            released_at_raw = _decode(raw.get(b"released_at", b""))
            released_at = float(released_at_raw) if released_at_raw else None
            results.append((identity, ctx, lease, released_at))
        return results

    async def evict(self, identity: IdentityKey) -> ContextRef | None:
        key = active_key(identity)
        context_id, pid_raw, node_id = await self._evict(keys=[key])
        context_id = _decode(context_id)
        if not context_id:
            return None
        return _to_context_ref(
            identity, context_id, _decode(pid_raw), _decode(node_id), ContextState.IDLE
        )

    async def force_release(self, identity: IdentityKey) -> None:
        await self._force_release(keys=[active_key(identity)], args=[time.time()])


def _decode(value: bytes | str | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
