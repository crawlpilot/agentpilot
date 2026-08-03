"""LLM-schema-driven structured extraction: schema/prompt + markdown -> one
model call -> structured JSON. Ported from Firecrawl's simple one-shot
`json` format (`performLLMExtract`) -- deliberately NOT the heavier
`deterministicJson` codegen-sandbox-cache system (an LLM-authored, cached,
sandboxed JS extractor replayed without further LLM calls), which needs an
external Node/jsdom sandbox service, TypeScript-compiler-based code
validation, and Postgres extractor-caching tables -- a separate, much larger
infrastructure investment, out of scope here.
"""

from __future__ import annotations

from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json

_SYSTEM_PROMPT = (
    "Transform the following content into structured JSON. Only use "
    "information present in the content -- never fabricate values for "
    "fields the content doesn't actually contain; omit or null them instead."
)

_MAX_MARKDOWN_CHARS = 40_000
"""Bounds LLM input size (cost/context), same spirit as `postprocess.py`'s
other fixed limits -- not user-configurable in this pass."""


async def extract_structured(
    markdown: str,
    *,
    json_schema: dict[str, Any] | None,
    prompt: str | None,
    config: LLMConfig,
) -> dict[str, Any]:
    system = f"{_SYSTEM_PROMPT}\n\n{prompt}" if prompt else _SYSTEM_PROMPT
    content = markdown[:_MAX_MARKDOWN_CHARS]
    return await chat_json(system, content, config=config, json_schema=json_schema)
