"""The one true end-to-end path: `docker compose up`, then hit the real
gateway over HTTP. Everything else in `tests/` runs against real Patchright
contexts directly; only this module needs the containers.

Skipped automatically when the compose stack isn't reachable, so a plain
`pytest tests/` doesn't require Docker -- run `docker compose up -d` first
to exercise this module. The auth-gated tests additionally need
`BAAS_ADMIN_TOKEN` set to whatever value the compose stack itself was started
with (`BAAS_ADMIN_TOKEN=some-secret docker compose up -d`) -- this test
bootstraps a real tenant API key through `/v1/api-keys` using that token,
exactly as an operator would, rather than hardcoding a backdoor.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
DETECTION_PAGE_INTERNAL = "http://detection-page:8090/"

_ADMIN_TOKEN = os.environ.get("BAAS_ADMIN_TOKEN")


def _unique_name(label: str) -> str:
    """P1's registry reuses a released (IDLE) identity's still-warm context on
    reopen, and the reaper eventually destroys genuinely idle ones -- so a
    fixed name is no longer unsafe. Kept anyway: a fresh identity per run
    means concurrent/repeated test runs against the same long-lived compose
    stack never contend for the same `IdentityKey`'s <=1-ACTIVE lock.
    """

    return f"{label}-{uuid.uuid4().hex[:8]}"


def _gateway_reachable() -> bool:
    try:
        httpx.get(f"{GATEWAY}/healthz", timeout=1.0)
        return True
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _gateway_reachable(),
        reason="gateway not reachable at :8000 -- run `docker compose up -d` first",
    ),
    pytest.mark.skipif(
        not _ADMIN_TOKEN,
        reason="set BAAS_ADMIN_TOKEN to the same value the compose stack was started with",
    ),
]


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bootstraps one real tenant API key via the admin-gated control plane,
    the same path a real operator/UI would take -- not a test-only bypass."""

    admin_client = httpx.Client(
        base_url=GATEWAY, timeout=10.0, headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"}
    )
    created = admin_client.post("/v1/api-keys", json={"tenant": "e2e", "name": "seam-e2e"})
    assert created.status_code == 200
    api_key = created.json()["api_key"]
    return {"Authorization": f"Bearer {api_key}"}


def test_full_session_lifecycle(auth_headers: dict[str, str]) -> None:
    client = httpx.Client(base_url=GATEWAY, timeout=30.0, headers=auth_headers)

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

        listed = client.get("/v1/sessions")
        assert listed.status_code == 200
        assert session_id in {s["session_id"] for s in listed.json()["sessions"]}

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


def test_egress_blocks_metadata_endpoint(auth_headers: dict[str, str]) -> None:
    client = httpx.Client(base_url=GATEWAY, timeout=30.0, headers=auth_headers)

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
