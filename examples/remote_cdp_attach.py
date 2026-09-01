"""Drive a browser running on an agentpilot fleet, with the local object.

    uv run python examples/remote_cdp_attach.py

Needs `AGENTPILOT_URL` and `AGENTPILOT_API_KEY`, and a running gateway. There is
no fixture for this one: the whole point is that a real managed browser answers
Chrome's own protocol, and a fake would only prove the fake works.

There are two ways to drive a remote browser, and they are good at different
things:

  * **the wire path** -- `POST /v1/sessions/{id}/execute`, a batch of actions per
    round trip. No browser installed locally, the server's tier ladder, egress
    guard and audit all apply. This is the default and what you want at volume.

  * **CDP attach** -- what this file shows. You get the *full local object*, so
    anything you can write against a local `Crawlpilot` runs unchanged, and you
    can drop to raw CDP. In exchange it is one round trip per call rather than
    per batch, it needs a real browser driver installed locally, and it bypasses
    the server-side policy that the wire path applies.

Reach for this to debug something interactively, or to run an existing
Playwright-shaped script against a fleet browser. Reach for the wire path for
anything scheduled.
"""

from __future__ import annotations

import os

import httpx

from crawlpilot import Crawlpilot

GATEWAY = os.environ.get("AGENTPILOT_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("AGENTPILOT_API_KEY", "")
TENANT = os.environ.get("AGENTPILOT_TENANT", "demo")


def open_session() -> str:
    """A session with `enable_cdp=True` -- without it the discovery route
    answers `CdpNotAvailable`, deliberately: a raw CDP relay is opt-in per
    session, not a property of the deployment."""

    response = httpx.post(
        f"{GATEWAY}/v1/sessions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"tenant": TENANT, "domain": "example.com", "enable_cdp": True},
        timeout=60,
    )
    response.raise_for_status()
    return str(response.json()["session_id"])


def release(session_id: str) -> None:
    httpx.delete(
        f"{GATEWAY}/v1/sessions/{session_id}",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=30,
    )


def main() -> None:
    if not API_KEY:
        raise SystemExit("set AGENTPILOT_API_KEY (and AGENTPILOT_URL) first")

    session_id = open_session()
    print(f"opened remote session {session_id}")
    try:
        # The whole of the integration. `cdp_url` points at the gateway's
        # Chrome-shaped discovery endpoint; `cdp_headers` authenticates that one
        # hop. The websocket it hands back already carries a credential in its
        # query string, because a browser cannot set headers on a handshake.
        with Crawlpilot(
            cdp_url=f"{GATEWAY}/v1/sessions/{session_id}/cdp/json/version",
            cdp_headers={"Authorization": f"Bearer {API_KEY}"},
        ) as cp:
            with cp.session() as page:
                # From here on, nothing knows the browser is elsewhere.
                page.navigate("https://example.com")
                print("title:  ", page.get_title())
                print("visible:", page.is_visible(selector="h1"))
                print("markdown:", page.markdown()[:200])

                snapshot = page.snapshot()
                if snapshot is not None:
                    print(f"refs:    {len(snapshot.refs)} addressable elements")
    finally:
        release(session_id)
        print("released")


if __name__ == "__main__":
    main()
