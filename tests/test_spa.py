"""`agentpilot.gateway.spa.mount_spa` in isolation -- a bare FastAPI app plus a
fake `vite build` dir, driven through a `TestClient`, so the SPA fallback and
static-asset behavior are asserted without importing the whole role-aware
`app` module or standing up a real frontend build."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentpilot.gateway.spa import mount_spa, resolve_ui_dir


def _fake_build(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>agentpilot</title>")
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    (tmp_path / "favicon.ico").write_text("icon-bytes")
    return tmp_path


def _app_with_spa(ui_dir: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/ping")
    async def _ping() -> dict[str, bool]:
        return {"ok": True}

    mount_spa(app, ui_dir)
    return app


def test_root_serves_index(tmp_path: Path) -> None:
    client = TestClient(_app_with_spa(_fake_build(tmp_path)))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "agentpilot" in resp.text


def test_client_route_falls_back_to_index(tmp_path: Path) -> None:
    # A deep link into a client-side route (no such file) must return the SPA
    # shell, not a 404, so a hard refresh on /nodes or /recipes/:id works.
    client = TestClient(_app_with_spa(_fake_build(tmp_path)))
    for path in ("/nodes", "/recipes/abc123"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "<!doctype html>" in resp.text


def test_static_asset_served_verbatim(tmp_path: Path) -> None:
    client = TestClient(_app_with_spa(_fake_build(tmp_path)))
    assert client.get("/assets/app.js").text == "console.log('hi')"
    assert client.get("/favicon.ico").text == "icon-bytes"


def test_real_api_route_wins_over_catchall(tmp_path: Path) -> None:
    client = TestClient(_app_with_spa(_fake_build(tmp_path)))
    resp = client.get("/v1/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_unmatched_api_path_is_404_not_spa(tmp_path: Path) -> None:
    # An unmatched /v1/... path fell through every router: it's a genuine 404,
    # not a client-side route -- don't hand an API client the 200 HTML shell.
    client = TestClient(_app_with_spa(_fake_build(tmp_path)))
    resp = client.get("/v1/nonexistent")
    assert resp.status_code == 404
    assert "<!doctype html>" not in resp.text


def test_resolve_ui_dir_env_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Present build -> returned; empty dir (no index.html) -> None (skip mount).
    (tmp_path / "dist").mkdir()
    build = _fake_build(tmp_path / "dist")
    monkeypatch.setenv("AGENTPILOT_UI_DIR", str(build))
    assert resolve_ui_dir() == build

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("AGENTPILOT_UI_DIR", str(empty))
    assert resolve_ui_dir() is None
