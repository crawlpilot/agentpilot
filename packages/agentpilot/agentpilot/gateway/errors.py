"""Typed error codes end-to-end: `ErrorCode` is mirrored 1:1 from `spi.errors`.

A closed set of `{success: false, code, error, details?}` responses -- the
central exception handler translating validation errors and driver errors
into Firecrawl's friendly-4xx/5xx shape.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocket

from agentpilot.observability.metrics import error_responses_total
from crawlpilot.spi import errors as spi_errors
from crawlpilot.tools import UnknownToolError

log = structlog.get_logger(__name__)


class ErrorCode(StrEnum):
    """The wire vocabulary, kept as an enum for the HTTP layer's own use.

    Not the source of truth any more -- `crawlpilot.spi.errors` is, and a test
    asserts every code an exception declares appears here, so the two cannot
    drift. What justifies keeping it: `BAD_REQUEST` and `INTERNAL_ERROR` are
    HTTP-level outcomes with no exception behind them (a malformed body, an
    unhandled crash), and this is what the frontend reads.
    """

    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    SESSION_LEASE_CONFLICT = "SESSION_LEASE_CONFLICT"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    NODE_LOST = "NODE_LOST"
    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    CHALLENGE_UNRESOLVED = "CHALLENGE_UNRESOLVED"
    CONTEXT_CRASHED = "CONTEXT_CRASHED"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"
    STALE_REF = "STALE_REF"
    CDP_NOT_AVAILABLE = "CDP_NOT_AVAILABLE"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_CANCELLED = "JOB_CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# The 14-entry `_DRIVER_ERROR_MAPPING` and the `_RETRY_AFTER_CODES` tuple that
# used to sit here are gone. Each exception carries its own `code`,
# `http_status` and `retry_after_seconds` now (`crawlpilot.spi.errors`), for two
# reasons.
#
# It was four edits to add an error type -- the class, the enum below, this
# table, and eventually a client's inverse copy -- and this table was on the
# *server* side of a distribution boundary, so a client could not have imported
# it even if we wanted the fourth copy to be shared. Now adding an error type is
# adding a class.
#
# And the lookup was `type(exc)`, an exact match: a subclass of
# `NavigationTimeout` would have quietly become a 500. Reading a class attribute
# inherits, which is what anyone would have assumed was happening.
#
# The rationale for the less obvious statuses moved onto the classes themselves:
# `StaleRefError` is a 409 rather than a 404 (re-snapshot and retry -- a client
# problem, not a server fault) and `CdpNotAvailable` likewise (the session
# exists, it just cannot do CDP).


def _error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    retry_after: int | None = None,
    details: object | None = None,
) -> JSONResponse:
    error_responses_total.labels(code=code.value).inc()
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "code": code.value, "error": message, "details": details},
        headers=headers,
    )


_HttpHandler = Callable[[Any, Any], Awaitable[JSONResponse]]

# A websocket close reason is capped at 123 bytes on the wire; a longer one is
# a protocol error, not a truncated message.
_WS_REASON_MAX_BYTES = 123


def _ws_reason(exc: Exception) -> str:
    return str(exc).encode()[:_WS_REASON_MAX_BYTES].decode(errors="ignore")


def _ws_safe(handler: _HttpHandler) -> Callable[[Any, Any], Awaitable[JSONResponse | None]]:
    """Adapt an HTTP-shaped handler so it is also correct on a websocket.

    Starlette dispatches these app-level handlers for websocket connections
    too, passing the `WebSocket` in the `Request` slot -- and a handler that
    returns a `Response` there makes Starlette send `http.response.start`
    into an already-accepted socket. uvicorn refuses that (`RuntimeError:
    Expected ASGI message 'websocket.send' or 'websocket.close', but got
    'websocket.http.response.start'`), the connection dies with a bare 1006,
    and a client that distinguishes deliberate close codes can only read 1006
    as "try again" -- which is how one `TabNotFound` out of
    `routes/live_view.py`'s `start_screencast` became an endless reconnect
    loop and a traceback per attempt in the worker's log.

    So for a websocket we build the response (which still counts the error
    metric) but send it as a close code instead: `4000 + http_status`, the
    same 4000-range vocabulary `routes/live_view.py` and
    `routes/live_view_proxy.py` already close with (4404, 4401, 4502, ...).
    """

    @functools.wraps(handler)
    async def _dispatch(conn: Any, exc: Exception) -> JSONResponse | None:
        response = await handler(conn, exc)
        if not isinstance(conn, WebSocket):
            return response
        log.info(
            "gateway.websocket_error_close",
            path=conn.url.path,
            exc_type=type(exc).__name__,
            status=response.status_code,
        )
        with contextlib.suppress(RuntimeError):  # peer may already be gone
            await conn.close(code=4000 + response.status_code, reason=_ws_reason(exc))
        return None

    return _dispatch


def register_exception_handlers(app: FastAPI) -> None:
    def handles(exc_class: type[Exception]) -> Callable[[_HttpHandler], _HttpHandler]:
        """`app.exception_handler`, but registering through `_ws_safe`."""

        def _register(fn: _HttpHandler) -> _HttpHandler:
            app.add_exception_handler(exc_class, _ws_safe(fn))
            return fn

        return _register

    @handles(spi_errors.DriverError)
    async def _driver_error_handler(request: Request, exc: spi_errors.DriverError) -> JSONResponse:
        """Every field read off the exception's own class -- see the note above
        the `ErrorCode` enum."""

        return _error_response(
            exc.http_status,
            ErrorCode(exc.code),
            str(exc),
            retry_after=exc.retry_after_seconds,
        )

    @handles(NotImplementedError)
    async def _not_implemented_handler(request: Request, exc: NotImplementedError) -> JSONResponse:
        return _error_response(400, ErrorCode.BAD_REQUEST, str(exc))

    @handles(UnknownToolError)
    async def _unknown_tool_handler(request: Request, exc: UnknownToolError) -> JSONResponse:
        """A verb this deployment does not offer.

        A client error, not a server one: the request named something real-
        looking that this registry has no spec for -- an extension not installed
        here, or a verb newer than this build. The message names what *is*
        available, which is the part a bare schema rejection cannot do.
        """

        return _error_response(400, ErrorCode.BAD_REQUEST, str(exc))

    @handles(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(400, ErrorCode.BAD_REQUEST, "invalid request", details=exc.errors())

    @handles(ValidationError)
    async def _model_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        """A Pydantic error raised *inside* a handler, not by FastAPI's own body
        parsing -- so `RequestValidationError` above never sees it.

        This became reachable when the action union gained its passthrough
        branch: a malformed *built-in* (`{"type": "click"}` with no `ref`) no
        longer matches the discriminated union, falls through to
        `ExtensionActionIn`, and is re-validated against its real `ToolSpec` in
        `action_conversion`. That is still a bad request and must not surface as
        a 500 through the catch-all below.
        """

        return _error_response(400, ErrorCode.BAD_REQUEST, "invalid request", details=exc.errors())

    @handles(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.BAD_REQUEST
        return _error_response(exc.status_code, code, str(exc.detail))

    @handles(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all so a raw Chromium/CDP error (e.g. "Cannot navigate to
        invalid URL") surfaces as a diagnosable typed response instead of an
        opaque "Internal Server Error" -- found by actually using the UI:
        a schemeless navigate URL crashed with no useful message. Registering
        this handler means Starlette's own console traceback logging no
        longer fires for these (it already produced a response), so we log
        it here instead.
        """

        log.error(
            "gateway.unhandled_exception",
            path=request.url.path,
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        return _error_response(500, ErrorCode.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
