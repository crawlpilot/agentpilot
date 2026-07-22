"""The one true end-to-end path: `docker compose up`, then hit the real
gateway over HTTP. Everything else in `tests/` runs against real Patchright
contexts directly; only this module needs the containers.

Skipped automatically when the compose stack isn't reachable, so a plain
`pytest tests/` doesn't require Docker -- run `docker compose up -d` first
to exercise this module.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
DETECTION_PAGE_INTERNAL = "http://detection-page:8090/"


def _unique_name(label: str) -> str:
    """P0 has no reaper -- a released (IDLE) session's Chrome process is
    never actually closed, so its profile dir keeps holding Chrome's
    SingletonLock indefinitely. Reusing a fixed identity name across repeated
    test runs against the same long-lived compose stack collides with that
    leftover lock (`ProcessSingleton ... already in use`); a fresh identity
    per run sidesteps the documented P0->P1 gap instead of tripping over it.
    """

    return f"{label}-{uuid.uuid4().hex[:8]}"


def _gateway_reachable() -> bool:
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=1.0)
        return True
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _gateway_reachable(),
    reason="gateway not reachable at :8000 -- run `docker compose up -d` first",
)


def test_full_session_lifecycle() -> None:
    client = httpx.Client(base_url=GATEWAY, timeout=30.0)

    name = _unique_name("seam-test")
    resp = client.post(
        "/v1/sessions", json={"tenant": "e2e", "domain": "example.com", "name": name}
    )
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]

    try:
        conflict = client.post(
            "/v1/sessions", json={"tenant": "e2e", "domain": "example.com", "name": name}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "SESSION_LEASE_CONFLICT"
        assert "Retry-After" in conflict.headers

        exec_resp = client.post(
            f"/v1/sessions/{session_id}/execute",
            json={
                "actions": [
                    {"type": "navigate", "url": DETECTION_PAGE_INTERNAL},
                    {"type": "snapshot"},
                    {"type": "extract", "format": "markdown"},
                    {
                        "type": "execute_js",
                        "script": 'document.getElementById("baas-detection-result").textContent',
                    },
                ]
            },
        )
        assert exec_resp.status_code == 200
        body = exec_resp.json()

        assert len(body["snapshots"]) == 1
        assert body["snapshots"][0]["epoch"] == 1
        assert "Sample Article" in body["extracts"][0]
        assert body["sequence_aborted"] is False

        import json

        detection = json.loads(body["js_returns"][0])
        assert detection["webdriver"] is False
        assert detection["hasChromeObject"] is True
        assert detection["coordinateMismatch"] is False
        # `runtimeEnableSuspected` is intentionally not asserted here: its
        # timing threshold is only valid on native hardware (see
        # docs/capacity-planning.md "Spike findings" #2) and false-positives
        # under QEMU/Rosetta emulation, which is exactly how this image runs
        # when built for linux/amd64 on an Apple Silicon host.
    finally:
        release = client.delete(f"/v1/sessions/{session_id}")
        assert release.status_code == 200
        assert release.json()["state"] == "idle"


def test_egress_blocks_metadata_endpoint() -> None:
    client = httpx.Client(base_url=GATEWAY, timeout=30.0)

    resp = client.post(
        "/v1/sessions",
        json={"tenant": "e2e", "domain": "example.com", "name": _unique_name("egress-test")},
    )
    session_id = resp.json()["session_id"]
    try:
        exec_resp = client.post(
            f"/v1/sessions/{session_id}/execute",
            json={
                "actions": [
                    {
                        "type": "navigate",
                        "url": "http://169.254.169.254/latest/meta-data/",
                        "timeout_ms": 5000,
                    }
                ]
            },
        )
        # The egress baseline REJECTs at the netfilter level, which surfaces
        # to Playwright as a navigation failure -- typed NAVIGATION_TIMEOUT
        # here since P0 doesn't yet distinguish "connection refused by
        # egress policy" from "slow site" (that finer-grained EGRESS_BLOCKED
        # mapping needs the full httpx-tier post-DNS validation work in P2).
        assert exec_resp.status_code in (504, 500)
    finally:
        client.delete(f"/v1/sessions/{session_id}")
