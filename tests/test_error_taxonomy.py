"""The error taxonomy, after it moved onto the exception classes.

It used to be a 14-entry table in `gateway/errors.py`: adding an error type
meant editing the class, the `ErrorCode` enum, that table, and -- once a client
existed -- a fourth, inverse copy the client could not import from `agentpilot`
anyway. Each class now carries its own `code`, `http_status` and
`retry_after_seconds`, and both directions are derived.

Two things to hold: the wire behaviour did not change, and the two vocabularies
that still exist (the classes, and the `ErrorCode` enum the frontend reads)
cannot drift apart.
"""

from __future__ import annotations

import pytest

from agentpilot.gateway.errors import ErrorCode
from crawlpilot.spi import errors as spi_errors
from crawlpilot.spi.errors import code_of, error_for_code, error_from_wire

# The mapping exactly as `_DRIVER_ERROR_MAPPING` and `_RETRY_AFTER_CODES`
# expressed it, transcribed here *before* the move. This is the characterisation:
# if the refactor changed any status or code, it fails.
BEFORE: list[tuple[type[spi_errors.DriverError], int, str, int | None]] = [
    (spi_errors.LeaseConflict, 409, "SESSION_LEASE_CONFLICT", 5),
    (spi_errors.CapacityExhausted, 503, "CAPACITY_EXHAUSTED", 5),
    (spi_errors.NodeLost, 502, "NODE_LOST", None),
    (spi_errors.NavigationTimeout, 504, "NAVIGATION_TIMEOUT", None),
    (spi_errors.ChallengeDetected, 422, "CHALLENGE_UNRESOLVED", None),
    (spi_errors.ContextCrashed, 500, "CONTEXT_CRASHED", None),
    (spi_errors.EgressBlocked, 403, "EGRESS_BLOCKED", None),
    (spi_errors.TabNotFound, 404, "NOT_FOUND", None),
    (spi_errors.StaleRefError, 409, "STALE_REF", None),
    (spi_errors.CdpNotAvailable, 409, "CDP_NOT_AVAILABLE", None),
    (spi_errors.JobNotFound, 404, "JOB_NOT_FOUND", None),
    (spi_errors.JobCancelled, 409, "JOB_CANCELLED", None),
]


@pytest.mark.parametrize("cls, status, code, retry", BEFORE)
def test_every_error_keeps_the_status_and_code_it_had(
    cls: type[spi_errors.DriverError], status: int, code: str, retry: int | None
) -> None:
    assert cls.http_status == status
    assert cls.code == code
    assert cls.retry_after_seconds == retry


def test_an_unclassified_error_is_still_a_500(
) -> None:
    """`WaitTimeout`, `SelectorNotFound` and `NoDialogOpen` were never in the
    table, so they fell through to `(500, INTERNAL_ERROR)`. They inherit exactly
    that from `DriverError` now -- unchanged, and worth pinning, because
    inheriting a default is a quieter way to be wrong than omitting a row."""

    for cls in (spi_errors.WaitTimeout, spi_errors.SelectorNotFound, spi_errors.NoDialogOpen):
        assert cls.http_status == 500
        assert cls.code == "INTERNAL_ERROR"


def test_the_enum_covers_every_code_an_exception_declares() -> None:
    """The guard that keeps the remaining two vocabularies in step. The classes
    are the source of truth; this asserts the enum the frontend reads has not
    fallen behind them."""

    declared = {
        cls.__dict__["code"]
        for cls in spi_errors.all_error_types()
        if "code" in cls.__dict__
    }
    assert declared <= {code.value for code in ErrorCode}


# ------------------------------------------------- the direction a client needs


@pytest.mark.parametrize("cls, _status, code, _retry", BEFORE)
def test_a_code_round_trips_back_to_its_class(
    cls: type[spi_errors.DriverError], _status: int, code: str, _retry: int | None
) -> None:
    assert code_of(cls) == code
    assert error_for_code(code) is cls


def test_an_unknown_code_still_yields_something_catchable() -> None:
    """A client one release behind its server must get a `DriverError` it can
    catch, not a crash on an unrecognised string."""

    error = error_from_wire("SOMETHING_FROM_A_LATER_VERSION", "the server said so")
    assert isinstance(error, spi_errors.DriverError)
    assert str(error) == "the server said so"


def test_internal_error_resolves_to_the_base_not_a_random_subclass() -> None:
    """`WaitTimeout` and friends *inherit* `INTERNAL_ERROR`. Matching on the
    inherited attribute would resolve that code to whichever unclassified
    subclass was walked first, handing a caller an exception type with nothing
    to do with what went wrong. Only a class that *declares* a code is a
    candidate."""

    assert error_for_code("INTERNAL_ERROR") is spi_errors.DriverError


def test_reconstruction_survives_a_custom_init() -> None:
    """`StaleRefError(ref, *, epoch_superseded)` and `WaitTimeout(condition,
    timeout_ms)` take arguments the wire does not carry. Calling `__init__`
    would raise `TypeError: missing argument` instead of the error the server
    reported -- so reconstruction goes through `__new__`."""

    error = error_from_wire("STALE_REF", "stale ref 'e12' (epoch superseded)")
    assert isinstance(error, spi_errors.StaleRefError)
    assert str(error) == "stale ref 'e12' (epoch superseded)"
