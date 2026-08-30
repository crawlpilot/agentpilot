"""Egress policy applied at the browser/httpx boundary.

Baseline (metadata + RFC1918 block) is enforced in P0 by `crawlpilot.egress.policy`;
full post-DNS-resolution IP validation for the httpx tier lands with P2.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EgressPolicy:
    block_metadata: bool = True
    block_private: bool = True
    allow_hosts: tuple[str, ...] = field(default_factory=tuple)
    deny_hosts: tuple[str, ...] = field(default_factory=tuple)


LIBRARY_EGRESS = EgressPolicy(block_metadata=False, block_private=False)
"""What `api.Browser` uses: no fence.

The fence is enforced with container-wide `iptables` rules
(`egress.policy.apply_baseline`). That is right for a multi-tenant worker whose
job is to run other people's navigation safely, and wrong for a library on
someone's own machine: opening a browser would insert REJECT rules into their
host firewall, and blocking RFC1918 would cut off the local dev server they are
most likely pointing at. On macOS this was invisible only because `iptables` is
absent; on a Linux laptop it bites silently.

Applied at the facade rather than by changing the session layer's default, so
every service call site keeps the fence it already had and no path can lose it by
omission. A library caller who wants it back passes
`Browser(egress=EgressPolicy())`."""
