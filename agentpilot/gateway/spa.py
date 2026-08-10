"""Optional same-origin serving of the `frontend/` dashboard.

The SPA is normally deployed separately (Vite dev server locally, a static
host in production). But for a self-contained deployment the built bundle can
be served straight from the tenant-facing process (`monolith`/`gateway`), so
no reverse proxy is needed to put the UI and the API under one origin (the
model `frontend/src/lib/config.ts` already assumes for production).

Opt in by pointing `AGENTPILOT_UI_DIR` at a `vite build` output, or just have
the default `frontend/dist` exist. It's inert when no build is present -- the
Chrome-free `gateway` image ships without Node/npm, so nothing serves here
unless a bundle is mounted in -- and `worker` never serves it (internal-only).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

# Paths owned by the API/instrumentation surface. An unmatched request under
# any of these fell through every real router, so it's a genuine 404 -- it
# must not be masked by handing back the 200 HTML SPA shell.
_API_PREFIXES: tuple[str, ...] = (
    "v1/",
    "internal/",
    "healthz",
    "readyz",
    "metrics",
    "docs",
    "redoc",
    "openapi.json",
)

_DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def resolve_ui_dir() -> Path | None:
    """The built SPA directory to serve, or `None` to skip (no build present)."""
    raw = os.environ.get("AGENTPILOT_UI_DIR")
    ui_dir = Path(raw).expanduser() if raw else _DEFAULT_UI_DIR
    return ui_dir if (ui_dir / "index.html").is_file() else None


def mount_spa(app: FastAPI, ui_dir: Path) -> None:
    """Serve `ui_dir` (a `vite build` output) same-origin.

    Vite's hashed bundles are served verbatim under `/assets`; every other
    unmatched path falls back to `index.html` so client-side routes
    (`/nodes`, `/recipes/:id`) resolve on a deep link or hard refresh.

    Call this *after* every API router is mounted: real routes are matched in
    registration order and always win, so only genuinely-unmatched paths reach
    the catch-all below.
    """
    index_file = ui_dir / "index.html"
    assets_dir = ui_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="spa-assets")

    root = ui_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str) -> Response:
        if full_path.startswith(_API_PREFIXES):
            raise HTTPException(status_code=404)
        if full_path:
            candidate = (root / full_path).resolve()
            # Serve a real static file (favicon, manifest, ...) only when it's a
            # file *inside* the build dir -- `is_relative_to` guards `../`
            # traversal out of `root`.
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        return FileResponse(index_file)
