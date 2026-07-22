"""P0 egress baseline: block cloud-metadata + RFC1918 for the browser process.

This is a best-effort baseline, NOT the final security boundary. It applies
`iptables` rules inside the *container's own* network namespace (never the
host's) blocking outbound access to `169.254.169.254` and private ranges, at
`driver.open()` time. It is intentionally narrow:

- P2's per-worker netns isolation is the real boundary; this baseline exists
  so P0 isn't wide open before that lands.
- The httpx "basic" tier gets full post-DNS-resolution IP validation (guards
  rebinding) starting in P2 -- see `baas.egress.httpx_guard` (not built yet).
- This is a container-wide block (every process in the container, not just
  Chrome) -- the plan's eventual "Chrome uid/netns"-scoped enforcement needs
  a distinct OS user for Chrome, which P0's single collapsed gateway+worker
  container doesn't yet have. That's a real gap, not an oversight: it means
  this baseline would currently also block the *gateway process's own*
  legitimate egress (e.g. to Redis in P2), same as it blocks Chrome's.

Blocked ranges: link-local (metadata) 169.254.0.0/16, and RFC1918
10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 -- **except** the container's own
directly-connected subnet(s) (read from `/proc/net/route`), which are
allow-listed ahead of the broader private-range block. Without this
exception, a blanket RFC1918 REJECT also cuts off the container's own
Docker/VPC bridge network -- discovered empirically in this repo's own
compose stack, where the `detection-page` sidecar sits inside the default
bridge's `172.18.0.0/16`, itself inside the blocked `172.16.0.0/12`. Real
cloud workers hit the identical problem: their own VPC subnet is RFC1918 and
they must reach their own Redis/registry on it, while still being denied
*other* private ranges (e.g. a peered VPC, or a different tenant's network).
Metadata (169.254.0.0/16) is never exempted this way -- a container's
connected subnet should never legitimately be link-local.
"""

from __future__ import annotations

import shutil
import subprocess

import structlog

from baas.spi.egress import EgressPolicy

log = structlog.get_logger(__name__)

_METADATA_RANGE = "169.254.0.0/16"
_PRIVATE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def _hex_to_ip(hex_str: str) -> str:
    value = int(hex_str, 16)
    return ".".join(str((value >> (8 * i)) & 0xFF) for i in range(4))


def _own_connected_subnets() -> list[str]:
    """Best-effort: directly-connected routes from `/proc/net/route` (Linux
    only; returns `[]` anywhere else, e.g. local macOS dev)."""

    subnets: list[str] = []
    try:
        with open("/proc/net/route") as f:
            next(f)  # header line
            for line in f:
                fields = line.split()
                if len(fields) < 8:
                    continue
                dest_hex, gateway_hex, mask_hex = fields[1], fields[2], fields[7]
                if gateway_hex != "00000000" or dest_hex == "00000000":
                    continue  # not a directly-connected route
                dest = _hex_to_ip(dest_hex)
                mask = _hex_to_ip(mask_hex)
                prefix = sum(bin(int(octet)).count("1") for octet in mask.split("."))
                subnets.append(f"{dest}/{prefix}")
    except OSError:
        pass
    return subnets


def _deny_ranges(policy: EgressPolicy) -> list[str]:
    ranges: list[str] = []
    if policy.block_metadata:
        ranges.append(_METADATA_RANGE)
    if policy.block_private:
        ranges.extend(_PRIVATE_RANGES)
    ranges.extend(policy.deny_hosts)
    return ranges


def _rule_exists(chain: str, target: str, cidr: str) -> bool:
    result = subprocess.run(
        ["iptables", "-C", chain, "-d", cidr, "-j", target],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _insert_accept(cidr: str) -> None:
    if _rule_exists("OUTPUT", "ACCEPT", cidr):
        return
    subprocess.run(
        ["iptables", "-I", "OUTPUT", "1", "-d", cidr, "-j", "ACCEPT"],
        capture_output=True,
        check=False,
    )


def _append_reject(cidr: str) -> None:
    if _rule_exists("OUTPUT", "REJECT", cidr):
        return
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


def apply_baseline(policy: EgressPolicy) -> None:
    """Idempotently applies the deny-list (plus the connected-subnet
    exception above it). Safe to call on every `open()`."""

    if shutil.which("iptables") is None:
        log.warning(
            "egress.iptables_unavailable",
            reason="iptables binary not found -- expected outside the Linux worker "
            "container (e.g. local/macOS dev); the P0 baseline is a no-op here",
        )
        return

    if policy.block_private:
        for cidr in _own_connected_subnets():
            _insert_accept(cidr)

    for cidr in _deny_ranges(policy):
        _append_reject(cidr)
