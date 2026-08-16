"""`agentpilot.agent.loop.run_agent_loop` end-to-end -- real Patchright
session (via `session.interactive`), a stubbed `/chat/completions` endpoint
scripted to click a button then call `done`. No real LLM API call, ever.
"""

from __future__ import annotations

import json
import re

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from agentpilot.agent.loop import run_agent_loop
from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.llm.client import LLMConfig
from agentpilot.session.interactive import open_interactive_session, release_interactive_session
from agentpilot.session.registry import Registry
from agentpilot.spi.actions import NavigateAction

PAGE_HTML = """<html><body>
<button id="probe" onclick="document.getElementById('result').textContent='clicked'">
Click me</button>
<div id="result"></div>
</body></html>"""


@pytest.fixture
def llm_httpserver():
    server = HTTPServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _scripted_llm_handler():
    """First call: find the button's `ref` from the rendered snapshot text
    in the prompt and click it. Second call: report done. A real model
    would reason about this; this stub just proves the loop's plumbing
    (observe -> prompt -> parse -> dispatch -> record -> repeat) works."""

    calls = {"n": 0}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        body = json.loads(request.get_data())
        user_content = body["messages"][-1]["content"]

        if calls["n"] == 1:
            match = re.search(r'\[(\w+)\]<button "Click me"', user_content)
            assert match is not None, user_content
            payload = {
                "evaluation_previous_goal": "starting the task",
                "memory": "",
                "next_goal": "click the button",
                "action": [{"type": "click", "ref": match.group(1)}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "the button was clicked successfully",
                "memory": "clicked the button",
                "next_goal": "",
                "action": [
                    {"type": "done", "success": True, "result": "clicked the button"}
                ],
            }

        return Response(
            json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}),
            content_type="application/json",
        )

    return handler


async def test_run_agent_loop_clicks_button_and_calls_done(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data(PAGE_HTML, content_type="text/html")
    llm_httpserver.expect_request("/chat/completions").respond_with_handler(
        _scripted_llm_handler()
    )

    registry = Registry()
    session = await open_interactive_session(
        session_id="test-agent-run",
        tenant="acme",
        domain="127.0.0.1",
        name="agent-loop-test",
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        vault=None,
        lease_ttl_seconds=300.0,
    )

    try:
        await driver.execute(session.ctx, [NavigateAction(url=httpserver.url_for("/"))])

        result = await run_agent_loop(
            task="Click the 'Click me' button.",
            session=session,
            registry=registry,
            driver=driver,
            llm_config=LLMConfig(
                api_key="test-key",
                base_url=llm_httpserver.url_for("/"),
                model="test-model",
                timeout_s=10.0,
            ),
            max_steps=5,
        )
    finally:
        await release_interactive_session(session, registry=registry, driver=driver, vault=None)

    assert result.success is True
    assert result.result == "clicked the button"
    assert len(result.steps.steps) == 2
    assert result.error is None


def _hallucinated_ref_llm_handler():
    """Step 1 clicks a ref the page never issued (the failure mode from the
    field report). The loop must turn that into actionable feedback -- not a
    dispatched action that fails deep in the driver, and not a circuit-breaker
    trip. Step 2 clicks the real button; step 3 reports done."""

    calls = {"n": 0}

    def handler(request: Request) -> Response:
        calls["n"] += 1
        body = json.loads(request.get_data())
        user_content = body["messages"][-1]["content"]

        if calls["n"] == 1:
            payload = {
                "evaluation_previous_goal": "starting the task",
                "memory": "",
                "next_goal": "click the add to cart button",
                "action": [{"type": "click", "ref": "add-to-cart-btn"}],
            }
        elif calls["n"] == 2:
            match = re.search(r'\[(\w+)\]<button "Click me"', user_content)
            assert match is not None, user_content
            payload = {
                "evaluation_previous_goal": "failure: that ref did not exist",
                "memory": "the ref add-to-cart-btn was invalid",
                "next_goal": "click the real button",
                "action": [{"type": "click", "ref": match.group(1)}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "the button was clicked successfully",
                "memory": "clicked the button",
                "next_goal": "",
                "action": [{"type": "done", "success": True, "result": "clicked the button"}],
            }

        return Response(
            json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}),
            content_type="application/json",
        )

    return handler


async def test_run_agent_loop_recovers_from_hallucinated_ref(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data(PAGE_HTML, content_type="text/html")
    llm_httpserver.expect_request("/chat/completions").respond_with_handler(
        _hallucinated_ref_llm_handler()
    )

    registry = Registry()
    session = await open_interactive_session(
        session_id="test-agent-hallucinated-ref",
        tenant="acme",
        domain="127.0.0.1",
        name="agent-loop-test",
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        vault=None,
        lease_ttl_seconds=300.0,
    )

    try:
        await driver.execute(session.ctx, [NavigateAction(url=httpserver.url_for("/"))])

        result = await run_agent_loop(
            task="Click the 'Click me' button.",
            session=session,
            registry=registry,
            driver=driver,
            llm_config=LLMConfig(
                api_key="test-key",
                base_url=llm_httpserver.url_for("/"),
                model="test-model",
                timeout_s=10.0,
            ),
            max_steps=5,
        )
    finally:
        await release_interactive_session(session, registry=registry, driver=driver, vault=None)

    # The run recovered: it did not trip the breaker on the bad ref, and reached done.
    assert result.success is True
    assert result.error is None
    assert len(result.steps.steps) == 3
    # Step 1's bogus ref produced actionable feedback (not a raw "stale ref").
    first_results = " ".join(result.steps.steps[0].action_results)
    assert "does not exist" in first_results
    assert "add-to-cart-btn" in first_results
