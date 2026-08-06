"""Context retire/rotate policy (CC): decide when a browser context has
degraded enough to retire, and how to rotate it. Ports Browser4's
privacy-context health thresholds (`privacyLeakWarnings`, `failureRate`,
`smallPageRate`) onto crawlpilot's per-identity context model.

Two rotation policies, selectable per run:
- RESTART: tear down the context; the next `acquire()` reopens the *same*
  profile (cookies/logged-in session preserved). Resets health + in-memory
  browser state; cheap; weak against persistent fingerprinting.
- FRESH: also wipe the identity's profile dir, so the next open starts from a
  clean browser identity to the site. (Proxy rotation is a separate follow-up:
  `ProxyPinner._pick` is deterministic per identity, so a genuinely different
  egress IP needs a pinner change; FRESH rotates the profile only for now.)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agentpilot.spi.health import ContextHealth


class RotationPolicy(Enum):
    RESTART = "restart"
    FRESH = "fresh"

    @classmethod
    def parse(cls, raw: str | None) -> RotationPolicy:
        """Lenient parse for env/config -- unknown/blank falls back to the
        safe default (RESTART: preserves logins)."""
        try:
            return cls((raw or "").strip().lower())
        except ValueError:
            return cls.RESTART


@dataclass(frozen=True)
class RotationThresholds:
    """Retire once *any* signal crosses its bound. Defaults track Browser4's
    (`maxWarnings=8`, `failureRateThreshold=0.6`)."""

    max_leak_warnings: int = 8
    failure_rate_threshold: float = 0.6
    min_tasks_for_rate: int = 5
    """Failure rate is meaningless with only a handful of tasks -- don't retire
    on rate until at least this many `execute()` batches have run."""


def should_retire(health: ContextHealth, thresholds: RotationThresholds) -> bool:
    """True when the context has degraded enough to retire. Leak warnings trip
    immediately (a bot-detection signal is serious even once it hits the cap);
    failure rate only trips once there's enough signal to trust it."""

    if health.leak_warnings >= thresholds.max_leak_warnings:
        return True
    if (
        health.tasks >= thresholds.min_tasks_for_rate
        and health.failure_rate >= thresholds.failure_rate_threshold
    ):
        return True
    return False


@dataclass(frozen=True)
class RotationConfig:
    """Per-call rotation settings threaded into `release_interactive_session`.
    `enabled=False` (the default) makes retirement a no-op -- existing callers
    are unaffected."""

    enabled: bool = False
    policy: RotationPolicy = RotationPolicy.RESTART
    thresholds: RotationThresholds = RotationThresholds()
