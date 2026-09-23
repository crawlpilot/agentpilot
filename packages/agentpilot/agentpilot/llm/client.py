"""Thin, provider-agnostic client for schema-driven LLM extraction.

The default backend is `httpx` against an OpenAI-compatible
`/chat/completions` endpoint, not the `openai` SDK: `httpx` is already a base
dependency, and a configurable `base_url` already gets provider-agnosticism
(OpenAI, Azure OpenAI, OpenRouter, a local vLLM/Ollama-compatible server) for
free, without adding a new dependency for what is, structurally, one POST
request.

`AGENTPILOT_LLM_PROVIDER=bedrock` selects the second backend
(`agentpilot.llm.bedrock`): Claude in Amazon Bedrock, whose Messages API at
`/anthropic/v1/messages` has no OpenAI-compatible shape and so cannot be
reached by pointing `base_url` at it. The dispatch lives inside
`chat_json_conversation_with_usage` -- the single chokepoint every caller
funnels through -- so no call site knows which provider it is talking to.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger(__name__)

# How much of each message to log. The page snapshot a selector prompt carries
# runs to 24 000 characters and the JSON outline another 16 000, so logging
# them whole would bury every other line in the run.
#
# `AGENTPILOT_LLM_LOG_CHARS=0` turns the content off and leaves only the
# shape/timing line; a large value logs the prompt in full, which is what you
# want when the question is "was the accordion's text even in what the model
# was shown?".
_LOG_CHARS = int(os.environ.get("AGENTPILOT_LLM_LOG_CHARS", "1200"))


def _as_text(content: Any) -> str:
    """A message's content as text. Vision messages carry a parts list, whose
    image blocks are megabytes of base64 and say nothing here."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content)


def _clip(text: str) -> str:
    if len(text) <= _LOG_CHARS:
        return text
    half = _LOG_CHARS // 2
    return f"{text[:half]}\n  …[{len(text) - _LOG_CHARS} chars elided]…\n{text[-half:]}"

_BEDROCK_DEFAULT_MODEL = "anthropic.claude-opus-5"
"""Bedrock model IDs carry an `anthropic.` provider prefix; a bare
first-party `claude-*` id is rejected by the endpoint."""

_DEFAULT_MAX_TOKENS = 16_000
"""The Messages API requires an explicit `max_tokens`; OpenAI's
`/chat/completions` does not. Sized for the small JSON objects these calls
produce while leaving room for a large `done` payload."""


def _env(name: str, default: str | None = None) -> str | None:
    """`os.environ.get`, but an empty/whitespace value counts as unset.

    Not defensive padding -- it is the shape docker-compose hands us.
    `VAR: ${VAR:-}` in a service's `environment` block *defines* VAR as the
    empty string when the host has no value for it, so `os.environ.get(name,
    default)` never sees its default and `int("")`/`float("")` raise. Every
    read below goes through this."""

    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


class LLMNotConfiguredError(Exception):
    """Raised when the selected provider's required settings are unset --
    fails closed and explicit, the same "unset gates the feature off, doesn't
    silently degrade" discipline `AGENTPILOT_ADMIN_TOKEN` uses elsewhere in
    this codebase (`gateway/wiring.py`)."""


@dataclass
class LLMUsage:
    """Token counts from a completion's `usage` block. Zeroed when the endpoint
    omits `usage` (some OpenAI-compatible servers do), so callers can always
    sum without a None check."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMConfig:
    api_key: str | None
    base_url: str
    model: str
    timeout_s: float
    provider: str = "openai"
    region: str | None = None
    max_tokens: int = _DEFAULT_MAX_TOKENS
    """`api_key` is `None` only on Bedrock's SigV4 path, where the AWS
    credential chain -- not a bearer token -- authenticates the request. The
    trailing fields default so the OpenAI construction stays a four-argument
    call everywhere it already appears."""

    @classmethod
    def from_env(cls) -> LLMConfig:
        provider = (_env("AGENTPILOT_LLM_PROVIDER", "openai") or "openai").lower()
        if provider == "bedrock":
            return cls._bedrock_from_env()
        if provider != "openai":
            raise LLMNotConfiguredError(
                f"AGENTPILOT_LLM_PROVIDER={provider!r} is not a known provider "
                "(expected 'openai' or 'bedrock')"
            )

        api_key = _env("AGENTPILOT_LLM_API_KEY")
        if not api_key:
            raise LLMNotConfiguredError(
                "AGENTPILOT_LLM_API_KEY is not set -- schema-based extraction is unavailable"
            )
        return cls(
            api_key=api_key,
            base_url=_env("AGENTPILOT_LLM_BASE_URL", "https://api.openai.com/v1") or "",
            model=_env("AGENTPILOT_LLM_MODEL", "gpt-4o-mini") or "",
            timeout_s=float(_env("AGENTPILOT_LLM_TIMEOUT_S", "60") or "60"),
        )

    @classmethod
    def _bedrock_from_env(cls) -> LLMConfig:
        """Claude in Amazon Bedrock. Reads the `ANTHROPIC_*` names first: the
        Anthropic SDK already understands them, so the documented three-export
        setup (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
        `ANTHROPIC_WORKSPACE_ID`) works without a crawlpilot-specific rename,
        and they are the only names that unambiguously belong to this provider
        (see the comment on the resolution order below). No API key is
        *required* here -- absent
        one, the dedicated Bedrock client signs with SigV4 off the standard AWS
        credential chain -- so the OpenAI branch's fail-closed key check would
        be wrong; the region takes its place as the must-be-set setting."""

        # Provider-specific names beat the shared generic ones here, the
        # reverse of what you might expect. `AGENTPILOT_LLM_BASE_URL` /
        # `AGENTPILOT_LLM_API_KEY` are shared with the OpenAI provider, so in
        # any config that has ever run against OpenAI or Ollama they still
        # hold *that* endpoint and *that* key -- switching provider does not
        # blank them. Reading them first sends Bedrock traffic to a leftover
        # `localhost:11434/v1` with `ollama` as the bearer token. The
        # `ANTHROPIC_*` names can only have been set for this provider.
        base_url = _env("ANTHROPIC_BASE_URL") or _env("AGENTPILOT_LLM_BASE_URL")
        region = (
            _env("AGENTPILOT_LLM_AWS_REGION")
            or _env("AWS_REGION")
            or _env("AWS_DEFAULT_REGION")
            or (region_from_bedrock_url(base_url) if base_url else None)
        )
        if not region:
            raise LLMNotConfiguredError(
                "AGENTPILOT_LLM_PROVIDER=bedrock needs a region -- set "
                "AGENTPILOT_LLM_AWS_REGION or AWS_REGION, or point "
                "ANTHROPIC_BASE_URL at https://bedrock-mantle.{region}.api.aws/anthropic"
            )
        return cls(
            api_key=(
                _env("ANTHROPIC_API_KEY")
                or _env("AWS_BEARER_TOKEN_BEDROCK")
                or _env("AGENTPILOT_LLM_API_KEY")
            ),
            base_url=base_url or bedrock_base_url(region),
            model=_env("AGENTPILOT_LLM_MODEL", _BEDROCK_DEFAULT_MODEL) or _BEDROCK_DEFAULT_MODEL,
            timeout_s=float(_env("AGENTPILOT_LLM_TIMEOUT_S", "60") or "60"),
            provider="bedrock",
            region=region,
            max_tokens=int(_env("AGENTPILOT_LLM_MAX_TOKENS") or _DEFAULT_MAX_TOKENS),
        )


def bedrock_base_url(region: str) -> str:
    """The Claude-in-Bedrock (Mantle) endpoint for `region`. `/v1/messages` is
    appended by the SDK, matching the first-party client's `base_url` shape."""

    return f"https://bedrock-mantle.{region}.api.aws/anthropic"


def region_from_bedrock_url(base_url: str) -> str | None:
    """`https://bedrock-mantle.us-east-1.api.aws/anthropic` -> `us-east-1`.
    Lets the region be implied by the one URL an operator is most likely to
    have exported, rather than demanded twice."""

    host = urlparse(base_url).hostname or ""
    parts = host.split(".")
    if len(parts) >= 2 and parts[0] == "bedrock-mantle":
        return parts[1]
    return None


async def chat_json(
    system: str,
    user: str,
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One-shot system+user call -- a thin wrapper over `chat_json_conversation`
    for the single-turn callers (`schema_extract.extract_structured`) that
    predate the agent loop's multi-turn need."""

    return await chat_json_conversation(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        config=config,
        json_schema=json_schema,
    )


async def chat_json_conversation(
    messages: list[dict[str, Any]],
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parsed-JSON-only convenience wrapper over
    `chat_json_conversation_with_usage` for the callers that don't track
    tokens (schema extraction, history compaction, the judge)."""

    parsed, _usage = await chat_json_conversation_with_usage(
        messages, config=config, json_schema=json_schema
    )
    return parsed


async def chat_json_conversation_with_usage(
    messages: list[dict[str, Any]],
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], LLMUsage]:
    """One structured-output completion over an arbitrary message list -- the
    primitive `agentpilot.agent`'s step loop needs (system + rendered-history-
    as-text + current state each turn, not a raw growing transcript -- see
    `agent/prompts.py`). Returns the parsed JSON object the model produced
    *and* its token usage. A malformed/non-JSON model response raises
    `json.JSONDecodeError`/`ValueError`/`KeyError` -- callers catch broadly and
    surface it as an error field rather than crashing the run.

    A message's `content` may be a plain string or, for a vision-capable
    model, the OpenAI multimodal parts list (`[{"type": "text", ...},
    {"type": "image_url", ...}]`). The OpenAI backend passes that through
    as-is; the Bedrock backend translates it into Messages-API content blocks.

    This is the one place either backend is chosen, which is why every caller
    (`agent/loop.py`, `agent/judge.py`, `agent/state.py`,
    `llm/schema_extract.py`, `recipe/locator_proposal.py`, `recipe/codegen.py`)
    is provider-blind."""

    # Every LLM call in the system funnels through here -- the selector agent,
    # the rows proposal, the judge, the contract, the agent loop -- so this is
    # the one place that can answer "what was the model actually shown, and what
    # did it say?". Without it a build's reasoning is only inferable from its
    # outcomes: a field that bound to a section heading looks identical whether
    # the heading was all the model was given or it simply chose badly.
    schema_name = ""
    if json_schema:
        props = json_schema.get("properties") or {}
        schema_name = ",".join(sorted(props)[:6])
    log.info(
        "llm.request",
        model=config.model,
        provider=config.provider,
        answers=schema_name,
        messages=[
            {"role": m.get("role"), "chars": len(_as_text(m.get("content")))}
            for m in messages
        ],
        # System messages are static per call site and repeat on every step of
        # every run -- 7 700 characters of the agent's instructions, fifteen
        # times a build, saying nothing that differs between them. Their length
        # is in `messages` above; the content worth reading is the part that
        # changes, which is the page state and the field list.
        content=[
            {"role": m.get("role"), "text": _clip(_as_text(m.get("content")))}
            for m in messages
            if m.get("role") != "system"
        ] if _LOG_CHARS else None,
    )

    started = time.monotonic()
    if config.provider == "bedrock":
        # Imported here, not at module scope: `anthropic` is an optional extra
        # and the OpenAI path must keep working without it installed.
        from agentpilot.llm.bedrock import chat_json_bedrock

        parsed, usage = await chat_json_bedrock(
            messages, config=config, json_schema=json_schema
        )
    else:
        parsed, usage = await _chat_openai_compatible(
            messages, config=config, json_schema=json_schema
        )

    log.info(
        "llm.response",
        model=config.model,
        ms=int((time.monotonic() - started) * 1000),
        reply=_clip(json.dumps(parsed, ensure_ascii=False, default=str))
        if _LOG_CHARS
        else None,
    )
    return parsed, usage


async def _chat_openai_compatible(
    messages: list[dict[str, Any]],
    *,
    config: LLMConfig,
    json_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], LLMUsage]:
    """One `/chat/completions` call against an OpenAI-compatible endpoint."""

    if json_schema is not None:
        response_format: dict[str, Any] = {
            "type": "json_schema",
            "json_schema": {"name": "extract", "schema": json_schema, "strict": True},
        }
    else:
        response_format = {"type": "json_object"}

    # Split connect vs read: a hung TCP/TLS connect gets its own short budget
    # so it can't silently consume the full (long) generation read timeout.
    timeout = httpx.Timeout(config.timeout_s, connect=min(10.0, config.timeout_s))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={
                "model": config.model,
                "messages": messages,
                "response_format": response_format,
            },
        )
        response.raise_for_status()
        body = response.json()

    content = body["choices"][0]["message"]["content"]
    usage_raw = body.get("usage") or {}
    usage = LLMUsage(
        input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
        output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
    )
    return parse_json_text(content), usage


def parse_json_text(text: str) -> dict[str, Any]:
    """`json.loads`, but tolerant of a markdown code fence around the object.

    Models wrap JSON in ```json fences even when the response format forbids
    prose -- reliably enough that Firecrawl carries a dedicated repair step
    (`experimental_repairText`) for it. A bare `json.loads` turns that into a
    `JSONDecodeError` and throws away a perfectly good extraction."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.partition("\n")[2].rpartition("```")[0].strip()
    if not stripped:
        raise ValueError("model returned an empty response where JSON was expected")
    return cast("dict[str, Any]", json.loads(stripped))
