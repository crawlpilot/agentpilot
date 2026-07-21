"""Driver-agnostic health status."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthStatus:
    alive: bool
    reason: str | None = None
