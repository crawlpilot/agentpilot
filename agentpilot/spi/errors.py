"""Closed set of driver-level errors, mirrored 1:1 by `gateway.errors.ErrorCode`."""

from __future__ import annotations


class DriverError(Exception):
    """Base class for all agentpilot.spi driver errors."""


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


class TabNotFound(DriverError):
    """A `page_id`/tab reference (execute's `page_id` arg, or
    `SwitchTabAction`/`CloseTabAction`'s) that doesn't resolve to a currently
    tracked tab -- already closed, or never existed. Same NOT_FOUND family as
    "no such session", not a distinct `ErrorCode` (see `gateway/errors.py`'s
    mapping)."""

    def __init__(self, page_id: str) -> None:
        super().__init__(f"no such tab {page_id!r}")
        self.page_id = page_id


class LeaseConflict(DriverError):
    pass


class NodeLost(DriverError):
    pass


class CapacityExhausted(DriverError):
    pass


class EgressBlocked(DriverError):
    pass


class CdpNotAvailable(DriverError):
    """No CDP endpoint exists for this session: either the deployed driver
    doesn't implement `CdpEndpointCapable` at all, or this session's
    underlying context wasn't opened with `enable_cdp=True`. Same "this
    specific thing isn't available" family as `TabNotFound`/`StaleRefError`,
    not a server fault."""
