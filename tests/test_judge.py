"""Unit tests for `agentpilot.agent.judge` -- verdict parsing and the
fail-open behaviour when the judge LLM call errors. Mocks httpx; no network."""

from __future__ import annotations

import json

import httpx

from agentpilot.agent.judge import judge_completion
from agentpilot.llm.client import LLMConfig

CONFIG = LLMConfig(api_key="k", base_url="https://x.test", model="m", timeout_s=5.0)


def _mock_httpx(monkeypatch, handler) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def _completion(payload: dict) -> httpx.Response:
    body = {"choices": [{"message": {"content": json.dumps(payload)}}]}
    return httpx.Response(200, json=body)


async def _judge() -> object:
    return await judge_completion(
        task="Buy the blue widget",
        claimed_result="Purchased the blue widget",
        extracted_data={"order_id": "123"},
        page_state='button "Checkout"',
        history_summary="Step 1: clicked add-to-cart",
        config=CONFIG,
    )


async def test_judge_accepts_supported_completion(monkeypatch) -> None:
    _mock_httpx(monkeypatch, lambda req: _completion({"passed": True, "reason": "order confirmed"}))
    verdict = await _judge()
    assert verdict.passed is True
    assert verdict.errored is False


async def test_judge_rejects_unsupported_completion(monkeypatch) -> None:
    _mock_httpx(
        monkeypatch,
        lambda req: _completion({"passed": False, "reason": "still on the cart page"}),
    )
    verdict = await _judge()
    assert verdict.passed is False
    assert "cart page" in verdict.reason


async def test_judge_fails_open_on_llm_error(monkeypatch) -> None:
    _mock_httpx(monkeypatch, lambda req: httpx.Response(500))
    verdict = await _judge()
    # A judge outage must not fabricate a failure -- the agent's verdict stands.
    assert verdict.passed is True
    assert verdict.errored is True
