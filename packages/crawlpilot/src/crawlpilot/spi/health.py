"""Driver-agnostic health status."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthStatus:
    alive: bool
    reason: str | None = None


@dataclass
class ContextHealth:
    """Driver-agnostic per-context health tallies, surfaced across the SPI so
    the session layer can decide whether to retire/rotate a context without
    reaching into driver internals. Mirrors the driver's in-memory counters
    (Browser4's `AbstractPrivacyContext` signals: leak warnings, failure rate,
    small-page rate). A "task" is one `execute()` batch."""

    tasks: int
    successes: int
    failures: int
    small_pages: int
    leak_warnings: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.tasks if self.tasks else 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.tasks if self.tasks else 0.0
