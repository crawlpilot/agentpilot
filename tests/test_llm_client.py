"""Pure unit tests for `agentpilot.llm.client` -- `httpx.MockTransport`
throughout, no real network call, no real LLM API ever hit."""

from __future__ import annotations

import json

import httpx
import pytest

from agentpilot.llm.client import (
    LLMConfig,
    LLMNotConfiguredError,
    chat_json,
    chat_json_conversation,
)

CONFIG = LLMConfig(
    api_key="test-key", base_url="https://llm.test/v1", model="test-model", timeout_s=5.0
)


def test_llm_config_from_env_raises_when_api_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPILOT_LLM_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        LLMConfig.from_env()


def test_llm_config_from_env_reads_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("AGENTPILOT_LLM_BASE_URL", "https://custom.example/v1")
    monkeypatch.setenv("AGENTPILOT_LLM_MODEL", "custom-model")
    monkeypatch.setenv("AGENTPILOT_LLM_TIMEOUT_S", "12")

    config = LLMConfig.from_env()

    assert config.api_key == "sk-test"
    assert config.base_url == "https://custom.example/v1"
    assert config.model == "custom-model"
    assert config.timeout_s == 12.0


async def test_chat_json_with_schema_uses_json_schema_response_format(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"answer": 42})}}]},
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    result = await chat_json(
        "system prompt", "user content", config=CONFIG, json_schema={"type": "object"}
    )

    assert result == {"answer": 42}
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["model"] == "test-model"
    assert captured["body"]["response_format"]["type"] == "json_schema"
    assert captured["body"]["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user content"},
    ]


async def test_chat_json_without_schema_uses_json_object_response_format(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    result = await chat_json("system", "user", config=CONFIG)
    assert result == {}


async def test_chat_json_conversation_sends_the_full_message_list_verbatim(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "turn 1 reply"},
        {"role": "user", "content": "turn 2"},
    ]
    result = await chat_json_conversation(messages, config=CONFIG)

    assert result == {}
    assert captured["body"]["messages"] == messages
