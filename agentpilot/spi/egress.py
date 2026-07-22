"""Egress policy applied at the browser/httpx boundary.

Baseline (metadata + RFC1918 block) is enforced in P0 by `baas.egress.policy`;
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
