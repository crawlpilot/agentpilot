"""Per-field locator proposal + mechanical verify -- one LLM call proposes a
`FieldLocator` per not-yet-satisfied field, each proposal is evaluated right
away (no LLM), and a field that fails is retried once with the failure fed
back to the model. Run interleaved, once per exploration step, by
`build.py`'s `on_step` hook -- not as a single end-of-run pass -- so locator
capture happens while the page state that revealed a field is still live and
known.
"""

from __future__ import annotations

import json as _json
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.evaluate import evaluate_field_locator
from agentpilot.recipe.models import FieldLocator
from agentpilot.recipe.schema import FieldSpec
from agentpilot.session.interactive import InteractiveSession
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot

_SYSTEM_PROMPT = (
    "You are locating structured-data fields on a rendered web page in order "
    "to build a reusable scraping recipe. Given the page's accessibility "
    "snapshot and any already-parsed JSON-LD/hydration/meta data, propose "
    "ONE stable locator per requested field that is currently resolvable. "
    "STRONGLY prefer a json_ld/hydration/meta source with a dotted/bracketed "
    "`path` (e.g. 'offers.price' or '[0].name') whenever the field's value "
    "is actually present in the provided JSON blob -- it is far more "
    "resilient to a site redesign than a css selector. Only propose "
    "source=css or source=ax_role when the value is not present in the JSON "
    "blob. For ax_role, `role` must be one of the roles shown in brackets in "
    "the snapshot (e.g. 'button', 'link', 'text') and `name_contains` a "
    "short substring of that element's visible name. If a field cannot "
    "currently be located, omit it from your response entirely -- do not "
    "guess."
)

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "locators": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": ["json_ld", "hydration", "meta", "css", "ax_role"],
                    },
                    "path": {"type": ["string", "null"]},
                    "selector": {"type": ["string", "null"]},
                    "attribute": {"type": ["string", "null"]},
                    "role": {"type": ["string", "null"]},
                    "name_contains": {"type": ["string", "null"]},
                },
                "required": ["field", "source"],
            },
        }
    },
    "required": ["locators"],
}


def _build_user_message(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    failures: dict[str, str] | None,
) -> str:
    field_lines = "\n".join(f"- {f.name}: {f.description}" for f in fields.values())
    parts = [
        f"Fields to locate:\n{field_lines}",
        f"\nPage snapshot:\n{snapshot_text}",
        "\nParsed structured data (json_ld/hydration/metadata), truncated:\n"
        f"{_json.dumps(structured_data)[:6000]}",
    ]
    if failures:
        failure_lines = "\n".join(f"- {name}: {reason}" for name, reason in failures.items())
        parts.append(
            "\nThese proposals from your last attempt did not verify -- "
            f"try a different locator:\n{failure_lines}"
        )
    return "\n".join(parts)


def _parse_locator(item: dict[str, Any]) -> FieldLocator:
    return FieldLocator(
        source=item["source"],
        path=item.get("path"),
        selector=item.get("selector"),
        attribute=item.get("attribute"),
        role=item.get("role"),
        name_contains=item.get("name_contains"),
    )


async def propose_field_locators(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    failures: dict[str, str] | None = None,
) -> dict[str, FieldLocator]:
    user = _build_user_message(
        fields, snapshot_text=snapshot_text, structured_data=structured_data, failures=failures
    )
    raw = await chat_json_conversation(
        [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}],
        config=llm_config,
        json_schema=_JSON_SCHEMA,
    )
    proposals: dict[str, FieldLocator] = {}
    for item in raw.get("locators", []):
        name = item.get("field")
        if name in fields:
            try:
                proposals[name] = _parse_locator(item)
            except (KeyError, TypeError):
                continue
    return proposals


async def propose_and_verify_fields(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    snapshot: AXSnapshot,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    max_retries: int = 1,
) -> dict[str, FieldLocator]:
    """Propose -> mechanically verify -> (on failure) retry with the failure
    fed back, up to `max_retries` times. Returns only fields that verified
    successfully; unresolved fields are simply absent (not an error) -- the
    caller keeps them in its own unfound-fields set for a later step."""

    verified: dict[str, FieldLocator] = {}
    remaining = dict(fields)
    failures: dict[str, str] | None = None

    for _attempt in range(max_retries + 1):
        if not remaining:
            break
        proposals = await propose_field_locators(
            remaining,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            llm_config=llm_config,
            failures=failures,
        )
        next_failures: dict[str, str] = {}
        for name in list(remaining):
            locator = proposals.get(name)
            if locator is None:
                next_failures[name] = "model did not propose a locator for this field"
                continue
            value = await evaluate_field_locator(
                locator,
                structured_data=structured_data,
                session=session,
                registry=registry,
                driver=driver,
                snapshot=snapshot,
            )
            if value is None or (isinstance(value, str) and not value.strip()):
                next_failures[name] = (
                    f"proposed locator (source={locator.source}) resolved to no value"
                )
                continue
            verified[name] = locator
            del remaining[name]
        failures = next_failures
        if not failures:
            break

    return verified
