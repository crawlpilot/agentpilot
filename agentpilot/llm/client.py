"""Thin, provider-agnostic HTTP client for schema-driven LLM extraction --
`httpx` against an OpenAI-compatible `/chat/completions` endpoint, not the
`openai` SDK: `httpx` is already a base dependency, and a configurable
`base_url` already gets provider-agnosticism (OpenAI, Azure OpenAI,
OpenRouter, a local vLLM/Ollama-compatible server) for free, without adding
a new dependency for what is, structurally, one POST request.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


class LLMNotConfiguredError(Exception):
    """Raised when `AGENTPILOT_LLM_API_KEY` is unset -- fails closed and
    explicit, the same "unset gates the feature off, doesn't silently
    degrade" discipline `AGENTPILOT_ADMIN_TOKEN` uses elsewhere in this
    codebase (`gateway/wiring.py`)."""


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: float

    @classmethod
    def from_env(cls) -> LLMConfig:
        api_key = os.environ.get("AGENTPILOT_LLM_API_KEY")
        if not api_key:
            raise LLMNotConfiguredError(
                "AGENTPILOT_LLM_API_KEY is not set -- schema-based extraction is unavailable"
            )
        return cls(
            api_key=api_key,
            base_url=os.environ.get("AGENTPILOT_LLM_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("AGENTPILOT_LLM_MODEL", "gpt-4o-mini"),
            timeout_s=float(os.environ.get("AGENTPILOT_LLM_TIMEOUT_S", "60")),
        )


async def chat_json(
    system: str,
    user: str,
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `/chat/completions` call, structured-output mode. Returns the
    parsed JSON object the model produced. A malformed/non-JSON model
    response raises `json.JSONDecodeError`/`KeyError` -- the caller
    (`schema_extract.extract_structured`, in turn `session.ephemeral`)
    catches broadly and surfaces it as `Document.extract_error` rather than
    failing the whole scrape."""

    if json_schema is not None:
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": "extract", "schema": json_schema, "strict": True},
        }
    else:
        response_format = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=config.timeout_s) as client:
        response = await client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={
                "model": config.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": response_format,
            },
        )
        response.raise_for_status()
        body = response.json()

    content = body["choices"][0]["message"]["content"]
    return json.loads(content)
