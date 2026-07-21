"""P0 egress baseline: block cloud-metadata + RFC1918 for the browser process.

This is a best-effort baseline, NOT the final security boundary. It applies
`iptables` rules inside the *container's own* network namespace (never the
host's) blocking outbound access to `169.254.169.254` and private ranges, at
`driver.open()` time. It is intentionally narrow:

- P2's per-worker netns isolation is the real boundary; this baseline exists
  so P0 isn't wide open before that lands.
- The httpx "basic" tier gets full post-DNS-resolution IP validation (guards
  rebinding) starting in P2 -- see `baas.egress.httpx_guard` (not built yet).

Blocked ranges: link-local (metadata) 169.254.0.0/16, and RFC1918
10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16.
"""

from __future__ import annotations

import shutil
import subprocess

import structlog

from baas.spi.egress import EgressPolicy

log = structlog.get_logger(__name__)

_METADATA_RANGE = "169.254.0.0/16"
_PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _deny_ranges(policy: EgressPolicy) -> list[str]:
    ranges: list[str] = []
    if policy.block_metadata:
        ranges.append(_METADATA_RANGE)
    if policy.block_private:
        ranges.extend(_PRIVATE_RANGES)
    ranges.extend(policy.deny_hosts)
    return ranges


def _rule_exists(cidr: str) -> bool:
    result = subprocess.run(
        ["iptables", "-C", "OUTPUT", "-d", cidr, "-j", "REJECT"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def apply_baseline(policy: EgressPolicy) -> None:
    """Idempotently applies the deny-list. Safe to call on every `open()`."""

    if shutil.which("iptables") is None:
        log.warning(
            "egress.iptables_unavailable",
            reason="iptables binary not found -- expected outside the Linux worker "
            "container (e.g. local/macOS dev); the P0 baseline is a no-op here",
        )
        return

    for cidr in _deny_ranges(policy):
        if _rule_exists(cidr):
            continue
        result = subprocess.run(
            ["iptables", "-A", "OUTPUT", "-d", cidr, "-j", "REJECT"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"egress baseline could not deny {cidr}: {result.stderr.decode().strip()} "
                "-- the worker container needs NET_ADMIN to enforce egress policy"
            )
