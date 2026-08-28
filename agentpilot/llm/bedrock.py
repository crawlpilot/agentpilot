"""Claude in Amazon Bedrock backend for `agentpilot.llm.client`.

Bedrock serves Claude through the Messages API at
`https://bedrock-mantle.{region}.api.aws/anthropic/v1/messages` -- the same
request body as Anthropic's first-party API, and nothing like the
OpenAI-compatible `/chat/completions` the default backend speaks. So this is a
real second backend rather than another `base_url` value, and it owns three
translations the OpenAI shape gave us for free:

1. **System messages.** The Messages API takes `system` as a top-level string,
   not a `role: "system"` entry in `messages`.
2. **Images.** `agent/loop.py` builds OpenAI's `{"type": "image_url", ...}`
   parts with a base64 data URL; the Messages API wants
   `{"type": "image", "source": {"type": "base64", ...}}`.
3. **Structured output.** Bedrock does *not* support the structured-outputs
   (`output_config.format`) feature, so `response_format`'s strict JSON Schema
   has no counterpart. Schema conformance comes from a single forced tool
   instead: the schema becomes that tool's `input_schema`, and the model's
   `tool_use.input` *is* the parsed JSON object.

Authentication has two paths, both driven by `LLMConfig`: a bearer token
(`api_key` set) goes through the standard `AsyncAnthropic` client pointed at
the Bedrock base URL, and no token falls back to `AsyncAnthropicBedrockMantle`,
which signs with SigV4 off the ambient AWS credential chain (env keys, SSO,
assumed role, ECS task role, IMDS).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from agentpilot.llm.client import LLMConfig, LLMNotConfiguredError, LLMUsage

if TYPE_CHECKING:  # pragma: no cover -- typing-only, `anthropic` is optional
    from anthropic import AsyncAnthropic

_TOOL_NAME = "emit_result"

_TOOL_DESCRIPTION = (
    "Return the result as this tool's arguments. The arguments schema is the "
    "exact shape the caller requires."
)

_JSON_ONLY_INSTRUCTION = (
    "Reply with a single raw JSON object and nothing else -- no prose, no "
    "explanation, no markdown code fences."
)

_FORCED_TOOL_INSTRUCTION = (
    f"Call the `{_TOOL_NAME}` tool to deliver your answer. You may say one brief "
    "sentence before calling it. Do not include internal or system XML tags in "
    "your response."
)


async def chat_json_bedrock(
    messages: list[dict[str, Any]],
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], LLMUsage]:
    """One Messages API call against Bedrock, returning the parsed JSON object
    and its token usage -- the `chat_json_conversation_with_usage` contract,
    unchanged, so `agent/loop.py`'s step-record token accounting and the
    judge/compaction/extraction callers need no knowledge of the provider."""

    system, turns = _split_system(messages)
    request: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": turns,
    }

    if json_schema is not None:
        request["system"] = _join_system(system, _FORCED_TOOL_INSTRUCTION)
        request["tools"] = [
            {
                "name": _TOOL_NAME,
                "description": _TOOL_DESCRIPTION,
                "input_schema": json_schema,
            }
        ]
        request["tool_choice"] = {"type": "tool", "name": _TOOL_NAME}
        # A forced `tool_choice` is incompatible with extended thinking, so it
        # has to be off here. The two documented disabled-thinking failure
        # modes -- a tool call written as visible text, and leaked internal
        # tags -- are covered by `_FORCED_TOOL_INSTRUCTION` above and by the
        # text fallback in `_extract_json` below.
        request["thinking"] = {"type": "disabled"}
    else:
        # The analogue of OpenAI's `response_format: {"type": "json_object"}`:
        # no schema to hang a tool off, so ask for bare JSON and parse the text.
        request["system"] = _join_system(system, _JSON_ONLY_INSTRUCTION)

    client = _client_for(config)
    async with client:
        response = await client.messages.create(**request)

    return _extract_json(response), _extract_usage(response)


def _client_for(config: LLMConfig) -> AsyncAnthropic:
    """Build a fresh client per call -- the same lifecycle as the OpenAI
    backend's `async with httpx.AsyncClient(...)`, which keeps the client from
    outliving the event loop that created its connection pool (worker loops and
    per-test loops alike)."""

    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover -- exercised by deployment
        raise LLMNotConfiguredError(
            "AGENTPILOT_LLM_PROVIDER=bedrock needs the `anthropic` package -- "
            "install it with `uv sync --extra bedrock`"
        ) from exc

    if config.api_key:
        return anthropic.AsyncAnthropic(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_s,
        )

    mantle = getattr(anthropic, "AsyncAnthropicBedrockMantle", None)
    if mantle is None:  # pragma: no cover -- depends on the installed SDK
        raise LLMNotConfiguredError(
            "SigV4 auth needs the Bedrock client from `anthropic[bedrock]` -- "
            "install it with `uv sync --extra bedrock`, or set a bearer token "
            "in AGENTPILOT_LLM_API_KEY / ANTHROPIC_API_KEY instead"
        )
    return cast("AsyncAnthropic", mantle(aws_region=config.region, timeout=config.timeout_s))


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Pull every `role: "system"` entry out into one top-level system string
    (the Messages API has no system role inside `messages`) and translate the
    content of the turns that remain."""

    system_parts: list[str] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            else:
                system_parts.extend(
                    part["text"]
                    for part in content or []
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            continue
        turns.append(
            {"role": message["role"], "content": _translate_content(message.get("content"))}
        )
    return "\n\n".join(system_parts), turns


def _join_system(system: str, instruction: str) -> str:
    return f"{system}\n\n{instruction}" if system else instruction


def _translate_content(content: Any) -> Any:
    """OpenAI content -> Messages API content. Strings pass through; a parts
    list has its `image_url` entries rewritten into base64 image blocks."""

    if not isinstance(content, list):
        return content
    return [_translate_part(part) for part in content]


def _translate_part(part: Any) -> Any:
    if not isinstance(part, dict) or part.get("type") != "image_url":
        return part

    url = (part.get("image_url") or {}).get("url", "")
    if not url.startswith("data:"):
        # Bedrock has no URL image source. Raising `ValueError` (rather than a
        # bespoke type) matters: `agent/reliability.py:classify_error` maps it
        # to VALIDATION, which fails fast instead of burning retries.
        raise ValueError("Bedrock does not support URL image sources -- inline the image as base64")

    header, _, data = url.partition(",")
    media_type = header.removeprefix("data:").removesuffix(";base64") or "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _extract_json(response: Any) -> dict[str, Any]:
    """The forced tool's `input` when the model called it, else the text blocks
    parsed as JSON -- the fallback that catches a disabled-thinking response
    that narrated the call instead of emitting a `tool_use` block."""

    text_parts: list[str] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "tool_use":
            return cast("dict[str, Any]", block.input)
        if block_type == "text":
            text_parts.append(block.text)
    return _parse_json_text("\n".join(text_parts))


def _parse_json_text(text: str) -> dict[str, Any]:
    """Tolerant JSON parse: strips a markdown code fence, which models still
    add occasionally even when told not to."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.partition("\n")[2].rpartition("```")[0].strip()
    if not stripped:
        raise ValueError("Bedrock response contained neither a tool call nor JSON text")
    return cast("dict[str, Any]", json.loads(stripped))


def _extract_usage(response: Any) -> LLMUsage:
    usage = getattr(response, "usage", None)
    if usage is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )
