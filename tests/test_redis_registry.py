"""Unit tests for `crawlpilot.session.redis_registry.RedisRegistry` -- against
`fakeredis` (with `lupa` for real Lua-script execution), not a live Redis
server, so these stay fast and Docker-free like `test_session_registry.py`.
Same behavioral contract as the in-memory `Registry` (`RegistryProtocol`) --
these tests mirror `test_session_registry.py`'s cases 1:1 to prove it."""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from agentpilot.control.identity import identity_for
from agentpilot.control.redis_registry import RedisRegistry
from crawlpilot.spi.errors import LeaseConflict, NodeAtCapacity
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState

IDENTITY = identity_for("t", "example.com", "a")

_next_ctx_id = iter(range(1, 10_000))


@pytest.fixture
def registry() -> RedisRegistry:
    return RedisRegistry(fakeredis.aioredis.FakeRedis())


def _make_ctx() -> ContextRef:
    n = next(_next_ctx_id)
    return ContextRef(
        context_id=f"ctx-{n}", identity=IDENTITY, state=ContextState.ACTIVE, pid=1000 + n
    )


async def _opener_for(identity: IdentityRef, opened: list[int]) -> ContextRef:
    """An opener for a named identity, for the multi-identity slot tests. The
    existing `_opener_counting` is pinned to the module-level IDENTITY."""

    n = next(_next_ctx_id)
    opened.append(n)
    return ContextRef(
        context_id=f"ctx-{n}", identity=identity, state=ContextState.ACTIVE, pid=1000 + n
    )


async def _opener_counting(calls: list[int]) -> ContextRef:
    calls.append(len(calls))
    return _make_ctx()


async def test_acquire_opens_fresh_context_once(registry: RedisRegistry) -> None:
    calls: list[int] = []
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting(calls))
    assert len(calls) == 1
    assert ctx.state is ContextState.ACTIVE
    assert lease.identity == IDENTITY


async def test_second_acquire_while_active_raises_lease_conflict(registry: RedisRegistry) -> None:
    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    with pytest.raises(LeaseConflict):
        await registry.acquire(IDENTITY, "owner2", 300.0, lambda: _opener_counting([]))


async def test_release_then_reacquire_reuses_warm_context_no_second_open(
    registry: RedisRegistry,
) -> None:
    calls: list[int] = []
    ctx1, lease1 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    await registry.release(lease1.lease_id)

    ctx2, lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert len(calls) == 1  # opener only called once across both acquires
    assert ctx2.context_id == ctx1.context_id
    assert ctx2.state is ContextState.ACTIVE
    assert lease2.lease_id != lease1.lease_id


async def test_concurrent_acquires_for_same_identity_only_one_wins(
    registry: RedisRegistry,
) -> None:
    calls: list[int] = []

    async def opener() -> ContextRef:
        await asyncio.sleep(0.01)
        return await _opener_counting(calls)

    results = await asyncio.gather(
        registry.acquire(IDENTITY, "a", 300.0, opener),
        registry.acquire(IDENTITY, "b", 300.0, opener),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, LeaseConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(calls) == 1  # only the winner actually opened a context


async def test_renew_extends_lease_and_release_clears_it(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 0.05, lambda: _opener_counting([]))

    renewed = await registry.renew(lease.lease_id)
    assert renewed.lease_id == lease.lease_id

    await registry.release(lease.lease_id)
    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)


async def test_evict_removes_entry_and_forgets_lease(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.release(lease.lease_id)

    evicted = await registry.evict(IDENTITY)
    assert evicted is not None
    assert evicted.context_id == ctx.context_id
    assert await registry.snapshot() == []

    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert len(calls) == 1
    assert ctx2.context_id != ctx.context_id


async def test_force_release_reclaims_active_lease_to_idle(registry: RedisRegistry) -> None:
    ctx, lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    await registry.force_release(IDENTITY)

    with pytest.raises(KeyError):
        await registry.renew(lease.lease_id)

    calls: list[int] = []
    ctx2, _lease2 = await registry.acquire(
        IDENTITY, "owner", 300.0, lambda: _opener_counting(calls)
    )
    assert ctx2.context_id == ctx.context_id
    assert len(calls) == 0


async def test_snapshot_reflects_identity_fields_losslessly(registry: RedisRegistry) -> None:
    await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    snap = await registry.snapshot()
    assert len(snap) == 1
    identity, ctx, lease, released_at = snap[0]
    assert identity == IDENTITY
    assert ctx.state is ContextState.ACTIVE
    assert lease is not None
    assert released_at is None


async def test_opener_failure_releases_the_reservation(registry: RedisRegistry) -> None:
    async def failing_opener() -> ContextRef:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await registry.acquire(IDENTITY, "owner", 300.0, failing_opener)

    # The reservation must not be left stuck ACTIVE -- a fresh acquire should
    # succeed immediately rather than raising LeaseConflict forever.
    ctx, _lease = await registry.acquire(IDENTITY, "owner", 300.0, lambda: _opener_counting([]))
    assert ctx.state is ContextState.ACTIVE


# --- node slots: the budget, and why it is inside the script -----------------


def _bounded(max_contexts: int = 2, slot_ttl: float = 60.0) -> RedisRegistry:
    return RedisRegistry(
        fakeredis.aioredis.FakeRedis(),
        node_id="node-a",
        max_contexts=max_contexts,
        slot_ttl_seconds=slot_ttl,
    )


async def test_the_script_refuses_the_context_over_the_limit() -> None:
    reg = _bounded(max_contexts=2)
    opened: list[int] = []

    for name in ("a", "b"):
        ident = identity_for("t", "example.com", name)
        await reg.acquire(
            ident, "owner", 60.0, lambda i=ident: _opener_for(i, opened)  # type: ignore[misc]
        )

    third = identity_for("t", "example.com", "c")
    with pytest.raises(NodeAtCapacity):
        await reg.acquire(third, "owner", 60.0, lambda: _opener_for(third, opened))
    assert len(opened) == 2, "a refusal must not launch a browser"


async def test_a_refusal_leaves_no_half_taken_lease() -> None:
    """The reason admission moved INTO `acquire_lease.lua`.

    The Python version reserved the identity first and then asked whether it was
    allowed, so a refusal had to unwind a lease it had already taken. Refusing
    inside the script means nothing was mutated, and the proof is that the
    refused identity is still cleanly acquirable the moment room appears.
    """

    reg = _bounded(max_contexts=1)
    opened: list[int] = []
    first = identity_for("t", "example.com", "a")
    second = identity_for("t", "example.com", "b")

    _ctx, lease = await reg.acquire(first, "owner", 60.0, lambda: _opener_for(first, opened))
    with pytest.raises(NodeAtCapacity):
        await reg.acquire(second, "owner", 60.0, lambda: _opener_for(second, opened))

    # Nothing was written for the refused identity.
    assert await reg._redis.exists(f"active:{second.slug()}") == 0

    await reg.release(lease.lease_id)
    await reg.evict(first)
    await reg.acquire(second, "owner", 60.0, lambda: _opener_for(second, opened))
    assert len(opened) == 2


async def test_concurrent_acquires_for_different_identities_cannot_exceed_the_limit() -> None:
    """The measured regression, at the layer it actually happened on.

    `admission.refused_max_contexts live=5 max_contexts=4` is in the worker log:
    a ceiling of four, breached, by the thing enforcing it. The count was read in
    Python one step before the browser opened, and the registry's lock is
    per-IDENTITY -- so the callers that race for the last slot, being different
    identities, serialized against nothing. Counting and claiming are now one
    script, and this is the test that would have caught it.
    """

    reg = _bounded(max_contexts=3)
    opened: list[int] = []
    identities = [identity_for("t", "example.com", f"s{i}") for i in range(10)]

    results = await asyncio.gather(
        *(
            reg.acquire(i, "owner", 60.0, lambda i=i: _opener_for(i, opened))  # type: ignore[misc]
            for i in identities
        ),
        return_exceptions=True,
    )

    won = [r for r in results if not isinstance(r, BaseException)]
    assert len(won) == 3
    assert len(opened) == 3
    assert all(isinstance(r, NodeAtCapacity) for r in results if isinstance(r, BaseException))


async def test_a_holder_that_stops_renewing_frees_its_slot_with_no_reaper() -> None:
    """Expiry is the score, so nothing has to notice a crash.

    The slot set is swept by `ZREMRANGEBYSCORE ... -inf now` at the top of every
    acquire, so a holder whose deadline passed stops counting without the reaper,
    the heartbeat, or any cleanup path running at all. That is the property the
    old count could not have: it scanned `active:*` keys that no script expired,
    so one unclean restart left a node permanently "full".
    """

    reg = _bounded(max_contexts=1, slot_ttl=0.05)
    opened: list[int] = []
    dead = identity_for("t", "example.com", "dead")
    await reg.acquire(dead, "owner", 60.0, lambda: _opener_for(dead, opened))

    await asyncio.sleep(0.06)

    fresh = identity_for("t", "example.com", "fresh")
    await reg.acquire(fresh, "owner", 60.0, lambda: _opener_for(fresh, opened))
    assert len(opened) == 2


async def test_a_warm_reuse_is_never_refused_even_at_the_limit() -> None:
    """Reusing a warm browser costs no new memory, so refusing frees nothing --
    and a node at its ceiling would become unable to run the work that releases
    a context. The script spells this as `reuse == 0 and ZSCORE == false`."""

    reg = _bounded(max_contexts=1)
    opened: list[int] = []
    ident = identity_for("t", "example.com", "a")

    _ctx, lease = await reg.acquire(ident, "owner", 60.0, lambda: _opener_for(ident, opened))
    await reg.release(lease.lease_id)

    await reg.acquire(ident, "owner", 60.0, lambda: _opener_for(ident, opened))
    assert len(opened) == 1, "the warm context was reused, and was not refused"


async def test_every_bookkeeping_key_carries_an_expiry() -> None:
    """MEASURED: seventeen `node_sessions:*` keys against two live nodes, all
    `TTL = -1`, and not one of them belonging to a node still alive. Not one
    of the six Lua scripts set an expiry on anything it wrote, so a registry
    whose keys outlive its processes reported a node as full forever after a
    single unclean restart."""

    reg = _bounded()
    ident = identity_for("t", "example.com", "a")
    opened: list[int] = []
    _ctx, lease = await reg.acquire(ident, "owner", 60.0, lambda: _opener_for(ident, opened))

    for key in (f"active:{ident.slug()}", f"lease_owner:{lease.lease_id}", "node_slots:node-a"):
        assert await reg._redis.ttl(key) > 0, f"{key} would outlive the process that wrote it"


async def test_destroying_a_context_is_what_frees_its_slot() -> None:
    """`release` moves ACTIVE -> IDLE and Chrome keeps running, so the node is
    still carrying it; only `evict` gives the memory back. Freeing at release
    would admit a fifth browser while four were resident."""

    reg = _bounded(max_contexts=1)
    opened: list[int] = []
    first = identity_for("t", "example.com", "a")
    second = identity_for("t", "example.com", "b")
    _ctx, lease = await reg.acquire(first, "owner", 60.0, lambda: _opener_for(first, opened))

    await reg.release(lease.lease_id)
    assert await reg._redis.zcard("node_slots:node-a") == 1, "an IDLE browser is still resident"
    with pytest.raises(NodeAtCapacity):
        await reg.acquire(second, "owner", 60.0, lambda: _opener_for(second, opened))

    await reg.evict(first)
    assert await reg._redis.zcard("node_slots:node-a") == 0
    await reg.acquire(second, "owner", 60.0, lambda: _opener_for(second, opened))
    assert len(opened) == 2


async def test_live_slots_excludes_a_holder_whose_deadline_passed() -> None:
    """`ZCOUNT key now +inf`, so a dead holder is already out of the number
    before anything has noticed it died -- no sweep, no reaper, no write."""

    reg = _bounded(max_contexts=4, slot_ttl=0.05)
    opened: list[int] = []
    ident = identity_for("t", "example.com", "a")
    await reg.acquire(ident, "owner", 60.0, lambda: _opener_for(ident, opened))
    assert await reg.live_slots() == 1

    await asyncio.sleep(0.06)

    assert await reg.live_slots() == 0
