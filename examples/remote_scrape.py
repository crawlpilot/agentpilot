"""The same script, against a local browser and against a fleet.

    uv run python examples/remote_scrape.py

Needs `AGENTPILOT_URL` and `AGENTPILOT_API_KEY` for the remote half; the local
half needs a browser (`crawlpilot[engine]` + `patchright install chrome`). Run
it with neither and it explains what is missing rather than failing obscurely.

The point is `flow()` below: one function body, driven by two clients, with no
branch inside it. That works because a remote session and a local one are the
same class -- `crawlpilot.verbs.SessionVerbs` -- differing only in where
`execute()` sends the batch.
"""

from __future__ import annotations

import os

GATEWAY = os.environ.get("AGENTPILOT_URL", "").rstrip("/")
API_KEY = os.environ.get("AGENTPILOT_API_KEY", "")
URL = "https://example.com"


def flow(client) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """Everything below is transport-agnostic.

    `get_title` and `is_visible` are worth noticing: both read
    `ActionResult.values`, which the HTTP boundary used to drop entirely. Until
    the response became a projection of the dataclass, neither could work
    remotely at all -- `is_visible` returned the *sentence* "#x is not visible",
    which is non-empty and therefore truthy.
    """

    with client.session(domain="example.com") as page:
        page.navigate(URL)
        snapshot = page.snapshot()
        return {
            "title": page.get_title(),
            "h1 visible": page.is_visible(selector="h1"),
            "markdown chars": len(page.markdown()),
            "addressable refs": len(snapshot.refs) if snapshot else 0,
        }


def run_local() -> None:
    try:
        from crawlpilot import Crawlpilot
    except ImportError:
        print("local:  crawlpilot not installed, skipping")
        return
    try:
        with Crawlpilot() as cp:
            print("local: ", flow(cp))
    except Exception as exc:  # noqa: BLE001 -- an example, not a test
        print(f"local:  needs a browser ({type(exc).__name__}: {exc})")


def run_remote() -> None:
    if not (GATEWAY and API_KEY):
        print("remote: set AGENTPILOT_URL and AGENTPILOT_API_KEY to run this half")
        return

    from agentpilot_client import AgentPilot

    with AgentPilot(api_key=API_KEY, base_url=GATEWAY) as ap:
        # Worth doing once before anything else: it settles the wire version and
        # tells you what this deployment can actually do.
        caps = ap.capabilities()
        print(f"remote: gateway speaks wire {caps['wire_api']}, "
              f"{len(caps['tools'])} verbs, "
              f"extensions={[e['name'] for e in caps['extensions']]}")

        print("remote:", flow(ap))

        # The one-shot path, for when you do not need a session at all.
        document = ap.scrape(URL, formats=("markdown", "structured_data"))
        print(f"remote: scrape -> {document.metadata.tier_used if document.metadata else '?'} "
              f"tier, {len(document.markdown or '')} chars")


if __name__ == "__main__":
    run_local()
    run_remote()
