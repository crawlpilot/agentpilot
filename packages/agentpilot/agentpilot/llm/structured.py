"""The platform's structured extractor, injected into the browser layer.

`session.ephemeral` used to call `agentpilot.llm` directly to satisfy
`ScrapeOptions.extract`. That was the last platform import in the browser layer
and a hard blocker for the split: producing markdown is browsing, but deciding
to hand that markdown to a model is the platform's business.

This is the same call, on the other side of the seam.
"""

from __future__ import annotations

from typing import Any

from agentpilot.llm import schema_extract
from agentpilot.llm.client import LLMConfig


async def extract_structured(
    markdown: str,
    *,
    json_schema: dict[str, Any],
    prompt: str | None = None,
    fit_markdown: str | None = None,
) -> tuple[Any, str | None]:
    """`fit_markdown` is the boilerplate-pruned rendering of the same page, used
    when the full markdown overflows the model's input budget -- see
    `schema_extract._choose_input` for why that beats truncating."""

    return await schema_extract.extract_structured(
        markdown,
        json_schema=json_schema,
        prompt=prompt,
        fit_markdown=fit_markdown,
        config=LLMConfig.from_env(),
    )
