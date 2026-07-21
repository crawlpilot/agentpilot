"""Closed set of driver-level errors, mirrored 1:1 by `gateway.errors.ErrorCode`."""

from __future__ import annotations


class DriverError(Exception):
    """Base class for all baas.spi driver errors."""


class NavigationTimeout(DriverError):
    pass


class ContextCrashed(DriverError):
    pass


class ChallengeDetected(DriverError):
    pass


class StaleRefError(DriverError):
    def __init__(self, ref: str, *, epoch_superseded: bool) -> None:
        super().__init__(
            f"stale ref {ref!r} ({'epoch superseded' if epoch_superseded else 'gone within epoch'})"
        )
        self.ref = ref
        self.epoch_superseded = epoch_superseded


class LeaseConflict(DriverError):
    pass


class NodeLost(DriverError):
    pass


class CapacityExhausted(DriverError):
    pass


class EgressBlocked(DriverError):
    pass
