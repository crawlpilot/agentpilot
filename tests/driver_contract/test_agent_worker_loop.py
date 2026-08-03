"""`agentpilot.jobs.agent_worker_loop.AgentWorkerLoop` -- real Patchright
driver, real in-memory `Registry`, real Postgres (`PostgresAgentStore`), and
a stubbed `/chat/completions` endpoint (no real LLM API call, ever).
Skipped automatically when `AGENTPILOT_TEST_DATABASE_URL` isn't set/reachable,
same idiom as `tests/driver_contract/test_crawl_worker_loop.py`.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import psycopg
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.jobs.agent_store import PostgresAgentStore
from agentpilot.jobs.agent_worker_loop import AgentWorkerLoop
from agentpilot.session.registry import Registry

_DATABASE_URL = os.environ.get("AGENTPILOT_TEST_DATABASE_URL")

PAGE_HTML = """<html><body>
<button id="probe" onclick="document.getElementById('result').textContent='clicked'">
Click me</button>
<div id="result"></div>
</body></html>"""


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="set AGENTPILOT_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresAgentStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM agent_runs WHERE tenant LIKE 'agentworker-%'")
    await s.close()


@pytest.fixture
def llm_httpserver():
    server = HTTPServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _tenant() -> str:
    return f"agentworker-{uuid.uuid4().hex[:8]}"


def _scripted_llm_handler(target_url: str):
    """The worker loop opens a fresh session and never navigates anywhere
    itself -- the agent decides to. So this 3-step script mirrors a real
    task: navigate first, then click once the button is visible, then done."""

    calls = {"n": 0}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        body = json.loads(request.get_data())
        user_content = body["messages"][-1]["content"]

        if calls["n"] == 1:
            payload = {
                "evaluation_previous_goal": "starting",
                "memory": "",
                "next_goal": "navigate to the target page",
                "action": [{"type": "navigate", "url": target_url}],
            }
        elif calls["n"] == 2:
            match = re.search(r'\[(\w+)\]<button "Click me"', user_content)
            assert match is not None, user_content
            payload = {
                "evaluation_previous_goal": "navigated successfully",
                "memory": "",
                "next_goal": "click the button",
                "action": [{"type": "click", "ref": match.group(1)}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "clicked successfully",
                "memory": "",
                "next_goal": "",
                "action": [{"type": "done", "success": True, "result": "clicked the button"}],
            }

        return Response(
            json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}),
            content_type="application/json",
        )

    return handler


async def _run_until_terminal(
    loop: AgentWorkerLoop, store: PostgresAgentStore, tenant: str, run_id: str, max_ticks: int = 10
):
    for _ in range(max_ticks):
        run = await store.get_run(run_id, tenant)
        assert run is not None
        if run.status in ("completed", "failed", "cancelled"):
            return run
        await loop.tick()
    raise AssertionError(f"run {run_id} did not reach a terminal status within {max_ticks} ticks")


async def test_agent_worker_loop_processes_a_queued_run_to_completion(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
    store: PostgresAgentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpserver.expect_request("/").respond_with_data(PAGE_HTML, content_type="text/html")
    target_url = httpserver.url_for("/")
    llm_httpserver.expect_request("/chat/completions").respond_with_handler(
        _scripted_llm_handler(target_url)
    )
    monkeypatch.setenv("AGENTPILOT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENTPILOT_LLM_BASE_URL", llm_httpserver.url_for("/"))

    tenant = _tenant()
    domain = "127.0.0.1"
    run = await store.create_run(
        tenant=tenant,
        task=f"Go to {target_url} and click the 'Click me' button.",
        domain=domain,
        tier="auto",
        output_schema=None,
        max_steps=5,
    )

    loop = AgentWorkerLoop(store, Registry(), driver, tmp_path, None, poll_interval_seconds=0.01)
    final = await _run_until_terminal(loop, store, tenant, run.run_id)

    assert final.status == "completed"
    assert final.result is not None
    assert final.result["success"] is True
    assert final.result["result"] == "clicked the button"

    steps, _cursor = await store.list_steps(run.run_id, tenant, after=None)
    assert len(steps) == 3
