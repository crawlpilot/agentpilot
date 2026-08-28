"""Pure unit tests for `agentpilot.llm.bedrock` -- the Anthropic client is
replaced wholesale via `_client_for`, so no real network call and no real
Bedrock endpoint is ever hit (the suite also runs without `anthropic`
installed, since the stub short-circuits the import)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import pytest

from agentpilot.llm import bedrock
from agentpilot.llm.client import LLMConfig, chat_json_conversation_with_usage

CONFIG = LLMConfig(
    api_key="bearer-token",
    base_url="https://bedrock-mantle.us-east-1.api.aws/anthropic",
    model="anthropic.claude-opus-5",
    timeout_s=5.0,
    provider="bedrock",
    region="us-east-1",
    max_tokens=1024,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "integer"}},
    "required": ["answer"],
}


@dataclass
class _Block:
    type: str
    text: str = ""
    input: dict[str, Any] | None = None


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage | None = None


class _FakeMessages:
    def __init__(self, response: _Response, captured: dict[str, Any]) -> None:
        self._response = response
        self._captured = captured

    async def create(self, **request: Any) -> _Response:
        self._captured.update(request)
        return self._response


class _FakeClient:
    """Mimics the async context-manager surface `chat_json_bedrock` uses."""

    def __init__(self, response: _Response, captured: dict[str, Any]) -> None:
        self.messages = _FakeMessages(response, captured)
        self.closed = False

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True


def _install(
    monkeypatch: pytest.MonkeyPatch, response: _Response
) -> tuple[dict[str, Any], list[_FakeClient]]:
    captured: dict[str, Any] = {}
    clients: list[_FakeClient] = []

    def factory(config: LLMConfig) -> _FakeClient:
        client = _FakeClient(response, captured)
        clients.append(client)
        return client

    monkeypatch.setattr(bedrock, "_client_for", factory)
    return captured, clients


def _tool_use_response() -> _Response:
    return _Response(
        content=[_Block(type="tool_use", input={"answer": 42})],
        usage=_Usage(input_tokens=11, output_tokens=7),
    )


async def test_schema_call_forces_a_tool_and_returns_its_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, clients = _install(monkeypatch, _tool_use_response())

    parsed, usage = await bedrock.chat_json_bedrock(
        [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user content"},
        ],
        config=CONFIG,
        json_schema=SCHEMA,
    )

    assert parsed == {"answer": 42}
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)
    assert captured["model"] == "anthropic.claude-opus-5"
    assert captured["max_tokens"] == 1024
    assert captured["tools"] == [
        {
            "name": "emit_result",
            "description": bedrock._TOOL_DESCRIPTION,
            "input_schema": SCHEMA,
        }
    ]
    assert captured["tool_choice"] == {"type": "tool", "name": "emit_result"}
    # Forced tool_choice is incompatible with extended thinking.
    assert captured["thinking"] == {"type": "disabled"}
    # The system role is hoisted out of `messages` into the top-level field.
    assert captured["system"].startswith("system prompt")
    assert captured["messages"] == [{"role": "user", "content": "user content"}]
    assert clients[0].closed


async def test_multiple_system_messages_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    captured, _ = _install(monkeypatch, _tool_use_response())

    await bedrock.chat_json_bedrock(
        [
            {"role": "system", "content": "first"},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "second"},
        ],
        config=CONFIG,
        json_schema=SCHEMA,
    )

    assert captured["system"].startswith("first\n\nsecond")
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


async def test_image_url_part_becomes_a_base64_image_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, _ = _install(monkeypatch, _tool_use_response())
    data = base64.b64encode(b"not-really-a-png").decode("ascii")

    await bedrock.chat_json_bedrock(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                ],
            }
        ],
        config=CONFIG,
        json_schema=SCHEMA,
    )

    assert captured["messages"][0]["content"] == [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
    ]


async def test_non_data_image_url_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _tool_use_response())

    with pytest.raises(ValueError, match="URL image sources"):
        await bedrock.chat_json_bedrock(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}
                    ],
                }
            ],
            config=CONFIG,
            json_schema=SCHEMA,
        )


async def test_schemaless_call_sends_no_tools_and_parses_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured, _ = _install(
        monkeypatch,
        _Response(content=[_Block(type="text", text='{"city": "Berlin"}')]),
    )

    parsed, usage = await bedrock.chat_json_bedrock(
        [{"role": "user", "content": "extract"}], config=CONFIG, json_schema=None
    )

    assert parsed == {"city": "Berlin"}
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)
    assert "tools" not in captured
    assert "thinking" not in captured
    assert bedrock._JSON_ONLY_INSTRUCTION in captured["system"]


async def test_text_response_in_a_code_fence_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Response(content=[_Block(type="text", text='```json\n{"city": "Berlin"}\n```')]),
    )

    parsed, _usage = await bedrock.chat_json_bedrock(
        [{"role": "user", "content": "extract"}], config=CONFIG, json_schema=None
    )

    assert parsed == {"city": "Berlin"}


async def test_falls_back_to_text_when_the_model_skipped_the_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Response(content=[_Block(type="text", text='{"answer": 1}')]))

    parsed, _usage = await bedrock.chat_json_bedrock(
        [{"role": "user", "content": "go"}], config=CONFIG, json_schema=SCHEMA
    )

    assert parsed == {"answer": 1}


async def test_empty_response_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Response(content=[]))

    with pytest.raises(ValueError, match="empty response"):
        await bedrock.chat_json_bedrock(
            [{"role": "user", "content": "go"}], config=CONFIG, json_schema=SCHEMA
        )


async def test_client_dispatches_to_the_bedrock_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider switch lives in `chat_json_conversation_with_usage`, which
    is what every caller in `agent/` and `recipe/` actually goes through."""

    captured, _ = _install(monkeypatch, _tool_use_response())

    parsed, usage = await chat_json_conversation_with_usage(
        [{"role": "user", "content": "go"}], config=CONFIG, json_schema=SCHEMA
    )

    assert parsed == {"answer": 42}
    assert usage.input_tokens == 11
    assert captured["tool_choice"] == {"type": "tool", "name": "emit_result"}
