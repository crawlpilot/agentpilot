"""Validate-on-acquire with auto-restart -- the resilience layer over
`registry.acquire`.

The registry hands back a *reused* warm/IDLE context without proving Chrome is
still alive; a browser that died while idle (renderer crash, OOM-killed, proxy
dropped) would otherwise surface as a failed action mid-request. `acquire_validated`
pings the acquired context (`driver.is_alive`) and, on a dead one, evicts it and
retries so a fresh context is opened transparently -- agent-browser's
non-blocking process-exit check + auto-restart (changelog #1023, #1157), adapted
to the registry/opener seam. Backend-agnostic: works over both the in-memory
`Registry` and `RedisRegistry` through `RegistryProtocol`.
"""

from __future__ import annotations

import contextlib

import structlog

from agentpilot.session.registry import Opener, RegistryProtocol
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.errors import ContextCrashed
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, Lease

log = structlog.get_logger(__name__)

_DEFAULT_MAX_ATTEMPTS = 2


async def acquire_validated(
    *,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    identity: IdentityKey,
    owner: str,
    ttl_seconds: float,
    opener: Opener,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> tuple[ContextRef, Lease]:
    """`registry.acquire`, but a returned context that fails `is_alive` is
    evicted and reopened (up to `max_attempts`). A freshly opened context passes
    the cheap ping, so the only real cost is one CDP round trip on the warm-reuse
    path. `LeaseConflict` (identity already ACTIVE) propagates unchanged -- that
    is a caller error, not a dead browser."""

    for attempt in range(1, max_attempts + 1):
        ctx, lease = await registry.acquire(identity, owner, ttl_seconds, opener)
        if await driver.is_alive(ctx):
            return ctx, lease

        # The reused context is dead. Evict it (removes the entry + drops the
        # lease) and close it, so the next acquire opens fresh via `opener`.
        log.warning(
            "acquire.dead_context_reopen", identity=identity.slug(), attempt=attempt
        )
        evicted = await registry.evict(identity)
        if evicted is not None:
            with contextlib.suppress(Exception):
                await driver.close(evicted)

    raise ContextCrashed(
        f"could not acquire a live context for {identity.slug()!r} in {max_attempts} attempts"
    )
