"""Pure unit tests for `agentpilot.llm.schema_extract` -- `chat_json` is
monkeypatched out entirely, so these only check prompt construction, never
touch the network."""

from __future__ import annotations

from agentpilot.llm import schema_extract
from agentpilot.llm.client import LLMConfig

CONFIG = LLMConfig(api_key="k", base_url="https://x.test", model="m", timeout_s=5.0)


async def test_extract_structured_includes_caller_prompt(monkeypatch) -> None:
    captured: dict = {}

    async def fake_chat_json(system, user, *, config, json_schema=None):
        captured["system"] = system
        captured["user"] = user
        captured["json_schema"] = json_schema
        return {"ok": True}

    monkeypatch.setattr(schema_extract, "chat_json", fake_chat_json)

    result = await schema_extract.extract_structured(
        "some markdown",
        json_schema={"type": "object"},
        prompt="Extract the price.",
        config=CONFIG,
    )

    assert result == {"ok": True}
    assert "Extract the price." in captured["system"]
    assert captured["user"] == "some markdown"
    assert captured["json_schema"] == {"type": "object"}


async def test_extract_structured_without_prompt_uses_base_system_only(monkeypatch) -> None:
    captured: dict = {}

    async def fake_chat_json(system, user, *, config, json_schema=None):
        captured["system"] = system
        return {}

    monkeypatch.setattr(schema_extract, "chat_json", fake_chat_json)

    await schema_extract.extract_structured(
        "markdown", json_schema=None, prompt=None, config=CONFIG
    )

    assert captured["system"] == schema_extract._SYSTEM_PROMPT


async def test_extract_structured_truncates_oversized_markdown(monkeypatch) -> None:
    captured: dict = {}

    async def fake_chat_json(system, user, *, config, json_schema=None):
        captured["user"] = user
        return {}

    monkeypatch.setattr(schema_extract, "chat_json", fake_chat_json)

    huge = "x" * (schema_extract._MAX_MARKDOWN_CHARS + 5_000)
    await schema_extract.extract_structured(huge, json_schema=None, prompt=None, config=CONFIG)

    assert len(captured["user"]) == schema_extract._MAX_MARKDOWN_CHARS
