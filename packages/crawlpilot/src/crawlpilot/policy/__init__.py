"""Injectable seams for state and policy the browser layer needs but must not own.

Phase 3 of the browserpilot extraction. `identity.burn_tracker`,
`identity.proxy_health` and `identity.proxy_pinning` each took a `redis.asyncio
.Redis` in their constructor, which made Redis a hard dependency of the browser
core. Every one of them is answering a *cross-process* question -- their own
docstrings say "shared across worker processes" -- which is a property of how
the platform is deployed, not of driving a browser.

The rule: **the policy stays here, only the storage moves.** `BurnTracker` keeps
its weighted-warning scoring, `ProxyHealth` keeps its jittered retirement cap,
`ProxyPinner` keeps its sticky-pick algorithm. What they lost is the `INCRBY`
and `HSETNX` calls, which now go through `StateStore`.
"""

from agentpilot.policy.providers import (
    NullPrototypes,
    PrototypeProvider,
    ProxyProvider,
    StaticProxies,
)
from agentpilot.policy.stores import InMemoryStateStore, StateStore

__all__ = [
    "InMemoryStateStore",
    "NullPrototypes",
    "PrototypeProvider",
    "ProxyProvider",
    "StateStore",
    "StaticProxies",
]
