"""Typed error codes end-to-end: `ErrorCode` is mirrored 1:1 from `spi.errors`.

A closed set of `{success: false, code, error, details?}` responses -- the
central exception handler translating validation errors and driver errors
into Firecrawl's friendly-4xx/5xx shape.
"""

from __future__ import annotations

from enum import Enum

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from baas.spi import errors as spi_errors


class ErrorCode(str, Enum):
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    SESSION_LEASE_CONFLICT = "SESSION_LEASE_CONFLICT"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    NODE_LOST = "NODE_LOST"
    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    CHALLENGE_UNRESOLVED = "CHALLENGE_UNRESOLVED"
    CONTEXT_CRASHED = "CONTEXT_CRASHED"
    EGRESS_BLOCKED = "EGRESS_BLOCKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_DRIVER_ERROR_MAPPING: dict[type[Exception], tuple[int, ErrorCode]] = {
    spi_errors.LeaseConflict: (409, ErrorCode.SESSION_LEASE_CONFLICT),
    spi_errors.CapacityExhausted: (503, ErrorCode.CAPACITY_EXHAUSTED),
    spi_errors.NodeLost: (502, ErrorCode.NODE_LOST),
    spi_errors.NavigationTimeout: (504, ErrorCode.NAVIGATION_TIMEOUT),
    spi_errors.ChallengeDetected: (422, ErrorCode.CHALLENGE_UNRESOLVED),
    spi_errors.ContextCrashed: (500, ErrorCode.CONTEXT_CRASHED),
    spi_errors.EgressBlocked: (403, ErrorCode.EGRESS_BLOCKED),
}

_RETRY_AFTER_CODES = (ErrorCode.SESSION_LEASE_CONFLICT, ErrorCode.CAPACITY_EXHAUSTED)


def _error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    retry_after: int | None = None,
    details: object | None = None,
) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "code": code.value, "error": message, "details": details},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(spi_errors.DriverError)
    async def _driver_error_handler(request: Request, exc: spi_errors.DriverError) -> JSONResponse:
        status_code, code = _DRIVER_ERROR_MAPPING.get(type(exc), (500, ErrorCode.INTERNAL_ERROR))
        retry_after = 5 if code in _RETRY_AFTER_CODES else None
        return _error_response(status_code, code, str(exc), retry_after=retry_after)

    @app.exception_handler(NotImplementedError)
    async def _not_implemented_handler(request: Request, exc: NotImplementedError) -> JSONResponse:
        return _error_response(400, ErrorCode.BAD_REQUEST, str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(400, ErrorCode.BAD_REQUEST, "invalid request", details=exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.BAD_REQUEST
        return _error_response(exc.status_code, code, str(exc.detail))
