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
    """`WaitTimeout(condition, timeout_ms)` and `TabNotFound(page_id)` take
    arguments the wire does not carry. Calling `__init__` would raise
    `TypeError: missing argument` instead of the error the server reported -- so
    reconstruction goes through `__new__`."""

    error = error_from_wire("STALE_REF", "ref 'e12' is not available")
    assert isinstance(error, spi_errors.StaleRefError)
    assert str(error) == "ref 'e12' is not available"


# --- a structured HTTPException detail stays structured ---------------------
#
# `_save` in `gateway/routes/recipes.py` is the one route whose error IS a list:
# it raises 422 with `{"errors": [...], "warnings": [...]}` from
# `validate_document`, and that list names the exact field group and reason a
# recipe document was refused. The handler ran `str(exc.detail)` over it, so the
# `error` field carried a Python dict repr and `details` carried nothing. In the
# studio that made a refused save look like a broken save.


def test_a_mapping_detail_is_summarised_and_carried_whole() -> None:
    from agentpilot.gateway.errors import _summarize_detail

    detail = {
        "errors": ["field_groups[1].repeat: a dom_rows repeat needs a rows_locator"],
        "warnings": ["fields.brand: declared but not collected by any group"],
    }
    assert _summarize_detail(detail) == (
        "field_groups[1].repeat: a dom_rows repeat needs a rows_locator"
    )


def test_a_long_error_list_is_truncated_with_a_count() -> None:
    """A toast has to stay readable; the whole list still travels in `details`."""

    from agentpilot.gateway.errors import _summarize_detail

    summary = _summarize_detail({"errors": ["a", "b", "c", "d", "e"]})
    assert summary == "a; b; c (and 2 more)"


def test_a_mapping_with_no_errors_key_never_yields_a_dict_repr() -> None:
    from agentpilot.gateway.errors import _summarize_detail

    assert _summarize_detail({"message": "not allowed here"}) == "not allowed here"
    # The last resort names the keys rather than printing the mapping. A repr is
    # the one output this function exists to make impossible.
    fallback = _summarize_detail({"shape": {"nested": 1}, "count": 2})
    assert "{" not in fallback and "'" not in fallback
    assert "count" in fallback and "shape" in fallback


@pytest.mark.asyncio
async def test_the_handler_puts_the_list_in_details() -> None:
    """End to end through the registered handler, because the bug was not in the
    summary -- it was in `details` never being passed at all."""

    import json

    from fastapi import FastAPI, HTTPException

    from agentpilot.gateway.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=422, detail={"errors": ["x is wrong"], "warnings": []})

    @app.get("/plain")
    async def plain() -> None:
        raise HTTPException(status_code=404, detail="no such recipe")

    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as client:
        res = client.get("/boom")
        assert res.status_code == 422
        body = json.loads(res.content)
        assert body["error"] == "x is wrong", "not a dict repr"
        assert body["details"] == {"errors": ["x is wrong"], "warnings": []}

        # A string detail is unchanged -- every other route in the app.
        res = client.get("/plain")
        assert res.status_code == 404
        body = json.loads(res.content)
        assert body["error"] == "no such recipe"
        assert body["code"] == "NOT_FOUND"
