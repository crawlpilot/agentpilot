"""`routes/sessions.py`'s `open_session` -> `execute_session` (called twice,
proving the same live session survives multiple round trips) -> `release_session`
-- real Patchright context, real in-memory `Registry`. Exercises the
`agentpilot.session.interactive` extraction end-to-end (this repo's own
route-testing convention: call the route functions directly with a fake
`Wiring`, see `test_scrape_route.py`), since that module is the load-bearing
foundation the agent-loop feature builds on.
"""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.gateway.routes.sessions import execute_session, open_session, release_session
from agentpilot.gateway.schemas import (
    ExecuteRequest,
    NavigateActionIn,
    SessionOpenRequest,
    SnapshotActionIn,
)
from agentpilot.session.registry import Registry

ARTICLE_HTML = """<html><body><h1>Session Lifecycle Article</h1></body></html>"""


class _FakeHeaders:
    def get(self, _key: str) -> str | None:
        return None


class _FakeRequest:
    headers = _FakeHeaders()


class _FakeWiring:
    def __init__(self, driver: PatchrightDriver, profiles_root) -> None:
        self.registry = Registry()
        self.driver = driver
        self.profiles_root = profiles_root
        self.proxy_pinner = None
        self.vault = None
        self.lease_ttl_seconds = 300.0
        self.sessions: dict = {}


async def test_open_execute_twice_release_reuses_same_live_session(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    wiring = _FakeWiring(driver, tmp_path)

    open_resp = await open_session(
        SessionOpenRequest(tenant="acme", domain="127.0.0.1", name="agent-run-1"),
        _FakeRequest(),
        wiring,
    )
    session_id = open_resp.session_id
    assert session_id in wiring.sessions

    result1 = await execute_session(
        session_id,
        ExecuteRequest(
            actions=[NavigateActionIn(type="navigate", url=httpserver.url_for("/"))]
        ),
        wiring,
    )
    assert result1.sequence_aborted is False

    # Second round trip against the *same* still-open session -- proves the
    # session survives multiple execute() calls, the whole point of the
    # interactive lifecycle vs. ephemeral.py's one-shot model.
    result2 = await execute_session(
        session_id, ExecuteRequest(actions=[SnapshotActionIn(type="snapshot")]), wiring
    )
    assert len(result2.snapshots) == 1

    release_resp = await release_session(session_id, wiring)
    assert release_resp == {"success": True, "state": "idle"}
    assert session_id not in wiring.sessions
