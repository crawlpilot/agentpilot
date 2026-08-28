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


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`from_env()` now consults the ambient `ANTHROPIC_*`/`AWS_*` names too,
    so a developer's own shell must not leak into these assertions."""

    for name in (
        "AGENTPILOT_LLM_PROVIDER",
        "AGENTPILOT_LLM_API_KEY",
        "AGENTPILOT_LLM_BASE_URL",
        "AGENTPILOT_LLM_MODEL",
        "AGENTPILOT_LLM_TIMEOUT_S",
        "AGENTPILOT_LLM_AWS_REGION",
        "AGENTPILOT_LLM_MAX_TOKENS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_llm_config_from_env_raises_when_api_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTPILOT_LLM_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError):
        LLMConfig.from_env()


def test_llm_config_from_env_defaults_to_the_openai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_API_KEY", "sk-test")

    config = LLMConfig.from_env()

    assert config.provider == "openai"
    assert config.base_url == "https://api.openai.com/v1"
    assert config.region is None


def test_llm_config_from_env_rejects_an_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_PROVIDER", "vertex")
    monkeypatch.setenv("AGENTPILOT_LLM_API_KEY", "sk-test")

    with pytest.raises(LLMNotConfiguredError):
        LLMConfig.from_env()


def test_bedrock_config_reads_the_anthropic_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented three-export setup, verbatim -- no crawlpilot-specific
    variable is required, and the region falls out of the base URL."""

    monkeypatch.setenv("AGENTPILOT_LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bearer-token")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "default")

    config = LLMConfig.from_env()

    assert config.provider == "bedrock"
    assert config.api_key == "bearer-token"
    assert config.base_url == "https://bedrock-mantle.us-east-1.api.aws/anthropic"
    assert config.region == "us-east-1"
    assert config.model == "anthropic.claude-opus-5"
    assert config.max_tokens == 16_000


def test_bedrock_config_does_not_require_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """SigV4 auth has no bearer token, so the OpenAI branch's fail-closed key
    check must not apply here."""

    monkeypatch.setenv("AGENTPILOT_LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    config = LLMConfig.from_env()

    assert config.api_key is None
    assert config.base_url == "https://bedrock-mantle.eu-west-1.api.aws/anthropic"


def test_bedrock_config_raises_without_a_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bearer-token")

    with pytest.raises(LLMNotConfiguredError, match="region"):
        LLMConfig.from_env()


def test_bedrock_config_explicit_region_and_max_tokens_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTPILOT_LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AGENTPILOT_LLM_AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AGENTPILOT_LLM_MAX_TOKENS", "2048")
    monkeypatch.setenv("AGENTPILOT_LLM_MODEL", "anthropic.claude-sonnet-5")

    config = LLMConfig.from_env()

    assert config.region == "us-west-2"
    assert config.max_tokens == 2048
    assert config.model == "anthropic.claude-sonnet-5"


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
