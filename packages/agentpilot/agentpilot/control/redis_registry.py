"""P2's Redis-backed `RegistryProtocol` implementation -- the "registry.py
-> Redis, same interface" swap `plan.md` calls for. Atomicity comes from the
five Lua scripts in `agentpilot/control/lua/`, loaded once at construction and
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
`crawlpilot.driver`. The actual browser resources stay in the worker process's
`PatchrightDriver._live` dict, keyed by `context_id`; Redis's job is making
the identity -> context_id mapping and the <=1-ACTIVE invariant shared and
crash-resilient across restarts (and, once >1 worker exists, across nodes).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentpilot.control.identity import identity_for, parts_of
from agentpilot.observability.metrics import admission_refused_total
from crawlpilot.session.admission import Admit
from crawlpilot.session.registry import Opener
from crawlpilot.spi.errors import LeaseConflict, NodeAtCapacity
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState, Lease, LeaseId

log = structlog.get_logger(__name__)

_LUA_DIR = Path(__file__).resolve().parent / "lua"
def _identity_fields(identity: IdentityRef) -> tuple[str, str, str]:
    """The three Lua/hash fields this registry has always persisted. Read back
    through the control plane's own parser now that the browser layer's identity
    is opaque."""

    parts = parts_of(identity)
    return parts.tenant, parts.domain, parts.name


def _identity_from_hash(raw: Mapping[Any, Any]) -> IdentityRef:
    """Rebuild an identity from the persisted hash fields -- the inverse of
    `_identity_fields`, and the only place this registry knows the key is a
    `tenant/domain/name` triple."""

    return identity_for(
        _decode(raw.get(b"tenant", b"")),
        _decode(raw.get(b"domain", b"")),
        _decode(raw.get(b"name", b"")),
    )


def _load(name: str) -> str:
    return (_LUA_DIR / name).read_text()


def active_key(identity: IdentityRef) -> str:
    return f"active:{identity.slug()}"


def slots_key(node_id: str) -> str:
    """The node's browser slots: a sorted set whose SCORE is each slot's expiry.

    Per node, not per tenant, because the budget being spent is this machine's
    memory. See `acquire_lease.lua` for why the count lives in Redis rather than
    in the caller, and `crawlpilot.session.slots` for the shape it borrows.
    """

    return f"node_slots:{node_id}"


def _slug_of(key: str) -> str:
    """The identity slug back out of an `active:` key.

    `release` is handed a lease id and finds its key through `lease_owner:`, so
    the slug has to come from the key rather than from an `IdentityRef` it was
    never given. Cheaper and more honest than a second round trip to read the
    hash fields that spell the same thing.
    """

    return key[len("active:"):] if key.startswith("active:") else key


def _to_context_ref(
    identity: IdentityRef, context_id: str, pid_raw: str, node_id: str, state: ContextState
) -> ContextRef:
    pid = int(pid_raw) if pid_raw else None
    return ContextRef(
        context_id=context_id, identity=identity, state=state, pid=pid, node_id=node_id or "local"
    )


class RedisRegistry:
    def __init__(
        self,
        redis: Redis,
        *,
        admit: Admit | None = None,
        node_id: str = "local",
        max_contexts: int = 25,
        slot_ttl_seconds: float = 900.0,
        key_ttl_seconds: float = 3600.0,
    ) -> None:
        self._redis = redis
        self._admit = admit
        """The memory watermark, asked before a NEW browser is launched. This is
        the registry a worker actually runs on when `AGENTPILOT_REDIS_URL` is
        set, so without the hook here the gate does not exist for the workload
        that needs it. The CONTEXT COUNT is no longer asked here -- it moved into
        `acquire_lease.lua`, where counting and claiming are one step. See
        `crawlpilot.session.admission`."""
        self._node_id = node_id
        self._max_contexts = max_contexts
        self._slot_ttl_seconds = slot_ttl_seconds
        """How long a slot survives with nothing refreshing it. A crash backstop
        rather than a lifetime: the worker heartbeat renews every ~40s, so
        anything still running refreshes this many times over and only a holder
        that stopped existing ever reaches the deadline."""
        self._key_ttl_seconds = key_ttl_seconds
        """Expiry for the bookkeeping keys themselves.

        Every write in these scripts used to be unbounded, and the leak was
        measured: seventeen `node_sessions:*` keys against two live nodes, all
        `TTL = -1`, and not one of them belonging to a node that still existed. A
        registry whose keys outlive its processes reports a node as full forever
        after one unclean restart."""
        self._acquire = redis.register_script(_load("acquire_lease.lua"))
        self._bind = redis.register_script(_load("bind_active_context.lua"))
        self._renew = redis.register_script(_load("renew_lease.lua"))
        self._release = redis.register_script(_load("release_lease.lua"))
        self._force_release = redis.register_script(_load("force_release.lua"))
        self._evict = redis.register_script(_load("evict.lua"))

    async def acquire(
        self, identity: IdentityRef, owner: str, ttl_seconds: float, opener: Opener
    ) -> tuple[ContextRef, Lease]:
        key = active_key(identity)
        now = time.time()
        lease_id = LeaseId(str(uuid.uuid4()))

        # The memory watermark first, in Python, because it cannot run inside
        # Lua and because refusing on it costs nothing -- no slot has been
        # claimed yet, so there is nothing to unwind.
        if self._admit is not None:
            await self._admit(identity.slug())

        try:
            reuse, context_id, pid_raw, node_id = await self._acquire(
                keys=[key, slots_key(self._node_id)],
                args=[
                    owner,
                    ttl_seconds,
                    lease_id,
                    now,
                    *_identity_fields(identity),
                    identity.slug(),
                    self._max_contexts,
                    self._slot_ttl_seconds,
                    self._key_ttl_seconds,
                ],
            )
        except ResponseError as exc:
            if "LEASE_CONFLICT" in str(exc):
                raise LeaseConflict(
                    f"identity {identity.slug()!r} already has an active session"
                ) from exc
            if "CAPACITY_EXHAUSTED" in str(exc):
                # Refused before the script mutated anything, so there is no
                # half-taken lease to release here -- which is the difference
                # between this and the Python check it replaces.
                log.warning(
                    "admission.refused_max_contexts",
                    slug=identity.slug(),
                    node_id=self._node_id,
                    max_contexts=self._max_contexts,
                )
                admission_refused_total.labels(budget="contexts").inc()
                raise NodeAtCapacity(
                    f"this node already holds {self._max_contexts} browser contexts "
                    f"(max {self._max_contexts}); retry shortly"
                ) from exc
            raise

        context_id = _decode(context_id)
        node_id = _decode(node_id)
        pid_raw = _decode(pid_raw)

        if not reuse:
            try:
                # Both gates have already run: the memory watermark above, and
                # the context count inside `acquire_lease.lua` itself. Neither
                # can refuse from here, which is why this block no longer has to
                # unwind a lease it took before deciding it did not want one.
                ctx = await opener()
            except BaseException:
                # The script claimed a slot and no browser came of it. Releasing
                # the lease alone would leave the node billed for a context that
                # does not exist until the deadline passed -- right eventually,
                # wrong for as long as the TTL, and the TTL is sized as a crash
                # backstop rather than a retry interval.
                await self._release(
                    keys=[key, slots_key(self._node_id)],
                    args=[
                        lease_id,
                        time.time(),
                        identity.slug(),
                        self._slot_ttl_seconds,
                        self._key_ttl_seconds,
                    ],
                )
                # AFTER the release, not before: `release_lease.lua` re-adds the
                # slot, because an IDLE context is a running browser and the node
                # is still carrying it. That is right for an ordinary release and
                # wrong here, where the open is what failed and there is nothing
                # resident to account for.
                await self._redis.zrem(slots_key(self._node_id), identity.slug())
                raise
            try:
                await self._bind(
                    keys=[key],
                    args=[
                        lease_id,
                        ctx.context_id,
                        str(ctx.pid) if ctx.pid else "",
                        ctx.node_id,
                        self._key_ttl_seconds,
                    ],
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
            await self._renew(
                keys=[key, slots_key(self._node_id)],
                args=[
                    lease_id,
                    now,
                    _slug_of(key),
                    self._slot_ttl_seconds,
                    self._key_ttl_seconds,
                ],
            )
        except ResponseError as exc:
            raise KeyError(f"lease {lease_id!r} was reclaimed") from exc

        raw = await self._redis.hgetall(key)
        identity = _identity_from_hash(raw)
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
        key = _decode(key_raw)
        await self._release(
            keys=[key, slots_key(self._node_id)],
            args=[
                lease_id,
                time.time(),
                _slug_of(key),
                self._slot_ttl_seconds,
                self._key_ttl_seconds,
            ],
        )

    async def snapshot(self) -> list[tuple[IdentityRef, ContextRef, Lease | None, float | None]]:
        results: list[tuple[IdentityRef, ContextRef, Lease | None, float | None]] = []
        async for key in self._redis.scan_iter(match="active:*"):
            raw = await self._redis.hgetall(key)
            if not raw:
                continue
            identity = _identity_from_hash(raw)
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

    async def evict(self, identity: IdentityRef) -> ContextRef | None:
        key = active_key(identity)
        context_id, pid_raw, node_id = await self._evict(
            keys=[key, slots_key(self._node_id)], args=[identity.slug()]
        )
        context_id = _decode(context_id)
        if not context_id:
            return None
        return _to_context_ref(
            identity, context_id, _decode(pid_raw), _decode(node_id), ContextState.IDLE
        )

    async def live_slots(self) -> int:
        """Counted the way `acquire_lease.lua` counts: swept of expiries first.

        `ZCOUNT key now +inf` rather than `ZCARD`, so this reports what the next
        acquire would see without needing a write to get there. A dead holder is
        already gone from this number before anything has noticed it died."""

        return int(await self._redis.zcount(slots_key(self._node_id), time.time(), "+inf"))

    async def force_release(self, identity: IdentityRef) -> None:
        await self._force_release(
            keys=[active_key(identity)], args=[time.time(), self._key_ttl_seconds]
        )


def _decode(value: bytes | str | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)
