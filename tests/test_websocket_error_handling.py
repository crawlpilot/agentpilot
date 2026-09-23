"""A `DriverError` raised *after* a websocket was accepted.

The regression, seen in a worker log as a traceback per second: a live-view
client reconnected with a `page_id` whose tab had gone away, so
`start_screencast` raised `TabNotFound` past `websocket.accept()`. Starlette
dispatches the app-level handlers in `gateway/errors.py` for websocket
connections too, and every one of them returns a `JSONResponse` -- which
Starlette then tried to send as `http.response.start` on an accepted socket:

    RuntimeError: Expected ASGI message 'websocket.send' or 'websocket.close',
    but got 'websocket.http.response.start'.

uvicorn killed the connection with a bare 1006, which `liveView.ts` reads as
"retry" rather than as one of its terminal codes -- so the browser reconnected
into the same `TabNotFound` forever.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentpilot.gateway.errors import register_exception_handlers
from crawlpilot.spi import errors as spi_errors


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.websocket("/raises-after-accept")
    async def _raises_after_accept(websocket: WebSocket) -> None:
        await websocket.accept()
        raise spi_errors.TabNotFound("no such tab 'abc'")

    @app.websocket("/raises-before-accept")
    async def _raises_before_accept(websocket: WebSocket) -> None:
        raise spi_errors.TabNotFound("no such tab 'abc'")

    @app.get("/http-raises")
    async def _http_raises() -> None:
        raise spi_errors.TabNotFound("no such tab 'abc'")

    return app


def test_driver_error_after_accept_closes_with_a_4000_range_code() -> None:
    """`TabNotFound.http_status` is 404, so the peer must see 4404 -- one of
    `liveView.ts`'s terminal codes -- and never an ASGI protocol violation."""

    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: PT012
        with client.websocket_connect("/raises-after-accept") as ws:
            ws.receive_bytes()
    assert excinfo.value.code == 4404
    assert "no such tab" in excinfo.value.reason


def test_driver_error_before_accept_also_closes_rather_than_answering_http() -> None:
    client = TestClient(_app())
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/raises-before-accept"):
            pass
    assert excinfo.value.code == 4404


def test_the_http_shape_is_unchanged() -> None:
    """The websocket branch must not cost HTTP callers their typed body."""

    client = TestClient(_app())
    resp = client.get("/http-raises")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"
    assert resp.json()["success"] is False


def test_a_long_error_message_fits_the_123_byte_close_reason_cap() -> None:
    """A close reason over 123 bytes is a protocol error in its own right --
    it would replace this crash with a different one."""

    app = FastAPI()
    register_exception_handlers(app)

    @app.websocket("/long")
    async def _long(websocket: WebSocket) -> None:
        await websocket.accept()
        raise spi_errors.TabNotFound("é" * 500)

    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as excinfo:  # noqa: PT012
        with client.websocket_connect("/long") as ws:
            ws.receive_bytes()
    assert len(excinfo.value.reason.encode()) <= 123
