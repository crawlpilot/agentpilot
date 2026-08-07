"""Client-configurable, tier-aware proxy pools.

Beating Akamai on hardened retail sites needs a *residential* egress -- a
datacenter IP is scored negatively before any JS runs. But which residential
provider, and per which client (tenant), is deployment-specific, so this stays
configuration rather than hardcoded: a pool is keyed by `(tenant, tier)` with
`"*"` wildcards for "any client" / "any tier", and a fallback chain so a sparse
config still resolves.

Two env sources (either or both):

- `AGENTPILOT_PROXY_POOL` -- the existing flat, comma-separated
  `scheme://[user:pass@]host:port` list. Becomes the `("*", "*")` default pool
  (every client, every tier), so nothing about the previous behaviour changes.
- `AGENTPILOT_PROXY_POOLS` -- JSON for per-client/per-tier pools:

      {
        "*":       { "residential": ["http://u:p@res-gw:8000?country=US"] },
        "acme":    { "residential": ["http://u:p@acme-res:8000?country=IN"],
                     "datacenter":  ["http://dc1:8080"] }
      }

  Each endpoint URL may carry `?country=XX` to declare its exit geo.

`resolve(tenant, tier)` walks `(tenant,tier) -> (tenant,*) -> (*,tier) ->
(*,*)`, and finally any endpoint at all, so a request never comes back empty
while any pool is configured.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from agentpilot.spi.proxy import ProxyEndpoint

_ANY = "*"


def _parse_endpoint(url_str: str, tier: str | None) -> ProxyEndpoint:
    parsed = urlparse(url_str)
    if not parsed.hostname:
        raise ValueError(f"proxy endpoint missing host: {url_str!r}")
    country = None
    q = parse_qs(parsed.query)
    if "country" in q and q["country"]:
        country = q["country"][0].upper() or None
    default_port = 443 if parsed.scheme == "https" else 80
    return ProxyEndpoint(
        scheme=parsed.scheme or "http",
        host=parsed.hostname,
        port=parsed.port or default_port,
        username=parsed.username or None,
        password=parsed.password or None,
        tier=tier,
        country=country,
    )


@dataclass(frozen=True)
class ProxyConfig:
    """`(tenant, tier) -> endpoints`, both keys `"*"`-wildcardable."""

    pools: dict[tuple[str, str], tuple[ProxyEndpoint, ...]]

    @property
    def is_empty(self) -> bool:
        return not any(self.pools.values())

    def resolve(self, tenant: str, tier: str | None) -> list[ProxyEndpoint]:
        """The candidate pool to pin from, most-specific first. Falls back
        through wildcards and finally to every endpoint, so a non-empty config
        never yields an empty pool for a valid request."""

        want_tier = tier or _ANY
        for key in (
            (tenant, want_tier),
            (tenant, _ANY),
            (_ANY, want_tier),
            (_ANY, _ANY),
        ):
            got = self.pools.get(key)
            if got:
                return list(got)
        return self.all_endpoints()

    def all_endpoints(self) -> list[ProxyEndpoint]:
        seen: list[ProxyEndpoint] = []
        for endpoints in self.pools.values():
            for e in endpoints:
                if e not in seen:
                    seen.append(e)
        return seen

    @classmethod
    def from_flat(cls, pool: list[ProxyEndpoint]) -> ProxyConfig:
        """Wrap a plain endpoint list as the `("*", "*")` default pool -- the
        back-compat path for callers (and tests) that still pass a flat list."""

        return cls({(_ANY, _ANY): tuple(pool)})

    @classmethod
    def from_env(cls) -> ProxyConfig:
        pools: dict[tuple[str, str], tuple[ProxyEndpoint, ...]] = {}

        flat = os.environ.get("AGENTPILOT_PROXY_POOL")
        if flat:
            entries = [
                _parse_endpoint(e.strip(), None)
                for e in flat.split(",")
                if e.strip()
            ]
            if entries:
                pools[(_ANY, _ANY)] = tuple(entries)

        structured = os.environ.get("AGENTPILOT_PROXY_POOLS")
        if structured:
            data = json.loads(structured)
            for tenant, by_tier in data.items():
                for tier, urls in by_tier.items():
                    endpoints = tuple(_parse_endpoint(u, tier) for u in urls)
                    if endpoints:
                        pools[(tenant, tier)] = endpoints

        return cls(pools)
