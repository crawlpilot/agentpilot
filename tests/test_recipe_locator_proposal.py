"""Unit tests for `agentpilot.recipe.locator_proposal` -- `httpx.MockTransport`
stands in for the LLM throughout (no real API call, ever), and `structured_data`
is passed in directly so `evaluate_field_locator`'s verify step never touches
a live driver either."""

from __future__ import annotations

import json

import httpx
import pytest

from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.locator_proposal import propose_and_verify_fields, propose_field_locators
from agentpilot.recipe.schema import FieldSpec
from tests.fusion_fixtures import fnode

CONFIG = LLMConfig(api_key="test-key", base_url="https://llm.test/v1", model="m", timeout_s=5.0)

FIELDS = {"price": FieldSpec(name="price", type="scalar", description="the price")}
STRUCTURED_DATA = {"json_ld": [{"offers": {"price": "9.99"}}], "hydration": {}, "metadata": {}}
SNAPSHOT = fnode("root")


def _mock_llm(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )


async def test_propose_field_locators_parses_candidate_list_per_field(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "locators": [
                {
                    "field": "price",
                    "candidates": [
                        {"source": "json_ld", "path": "[0].offers.price"},
                        {"source": "css", "selector": "#price", "attribute": "text"},
                    ],
                },
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    proposals = await propose_field_locators(
        FIELDS, snapshot_text="(empty page)", structured_data=STRUCTURED_DATA, llm_config=CONFIG
    )
    assert [loc.source for loc in proposals["price"]] == ["json_ld", "css"]
    assert proposals["price"][0].path == "[0].offers.price"


async def test_propose_field_locators_accepts_legacy_single_locator_item(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"locators": [{"field": "price", "source": "json_ld", "path": "[0].offers.price"}]}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    proposals = await propose_field_locators(
        FIELDS, snapshot_text="(empty page)", structured_data=STRUCTURED_DATA, llm_config=CONFIG
    )
    assert [loc.path for loc in proposals["price"]] == ["[0].offers.price"]


async def test_propose_field_locators_ignores_a_field_not_in_the_request(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"locators": [{"field": "not_requested", "source": "css", "selector": "#x"}]}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    proposals = await propose_field_locators(
        FIELDS, snapshot_text="(empty page)", structured_data=STRUCTURED_DATA, llm_config=CONFIG
    )
    assert proposals == {}


async def test_propose_and_verify_returns_only_fields_that_resolve_to_a_value(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "locators": [
                {"field": "price", "source": "json_ld", "path": "[0].offers.price"},
            ]
        }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    verified = await propose_and_verify_fields(
        FIELDS,
        snapshot_text="(empty page)",
        structured_data=STRUCTURED_DATA,
        snapshot=SNAPSHOT,
        session=None,
        registry=None,
        driver=None,
        llm_config=CONFIG,
    )
    assert verified["price"][0].path == "[0].offers.price"


async def test_propose_and_verify_retries_once_on_a_bad_proposal(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if calls["n"] == 1:
            payload = {"locators": [{"field": "price", "source": "json_ld", "path": "[0].nope"}]}
        else:
            assert "did not verify" in body["messages"][-1]["content"]
            payload = {
                "locators": [{"field": "price", "source": "json_ld", "path": "[0].offers.price"}]
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    verified = await propose_and_verify_fields(
        FIELDS,
        snapshot_text="(empty page)",
        structured_data=STRUCTURED_DATA,
        snapshot=SNAPSHOT,
        session=None,
        registry=None,
        driver=None,
        llm_config=CONFIG,
        max_retries=1,
    )
    assert calls["n"] == 2
    assert verified["price"][0].path == "[0].offers.price"


async def test_propose_and_verify_gives_up_after_max_retries(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"locators": [{"field": "price", "source": "json_ld", "path": "[0].nope"}]}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    _mock_llm(monkeypatch, handler)
    verified = await propose_and_verify_fields(
        FIELDS,
        snapshot_text="(empty page)",
        structured_data=STRUCTURED_DATA,
        snapshot=SNAPSHOT,
        session=None,
        registry=None,
        driver=None,
        llm_config=CONFIG,
        max_retries=1,
    )
    assert verified == {}
