"""Per-field locator proposal + mechanical verify -- one LLM call proposes an
ordered list of candidate `FieldLocator`s per not-yet-satisfied field, each
candidate is evaluated right away (no LLM), verified-resolving candidates are
merged with algorithmically-synthesized robust CSS alternatives
(`selector_synthesis`), and a field that fails entirely is retried once with
the failure fed back to the model. Run interleaved, once per exploration step,
by `build.py`'s `on_step` hook -- not as a single end-of-run pass -- so
locator capture happens while the page state that revealed a field is still
live and known.

Each field ends up with an ordered *candidate list* (Pulsar's comma-union /
coalesce, resolved first-non-empty-wins at replay), ordered most-robust-first:
json_ld/hydration/meta paths, then synthesized stable-attribute CSS, then the
model's remaining css/ax_role proposals.
"""

from __future__ import annotations

import json as _json
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.evaluate import evaluate_field_locator
from agentpilot.recipe.models import FieldLocator
from agentpilot.recipe.schema import FieldSpec
from agentpilot.recipe.selector_synthesis import synthesize_css_candidates
from agentpilot.session.interactive import InteractiveSession
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.snapshot import AXSnapshot

# Preference order for the final candidate list: structured-data paths are the
# most redesign-resilient, ax_role (accessibility) next, css last.
_SOURCE_RANK = {"json_ld": 0, "hydration": 0, "meta": 0, "ax_role": 1, "css": 2}

_SYSTEM_PROMPT = (
    "You are locating structured-data fields on a rendered web page in order "
    "to build a reusable scraping recipe. Given the page's accessibility "
    "snapshot and any already-parsed JSON-LD/hydration/meta data, propose an "
    "ORDERED LIST of 1-3 candidate locators per requested field, best first, "
    "each currently resolvable. Multiple candidates give the recipe resilient "
    "fallbacks. STRONGLY prefer a json_ld/hydration/meta source with a "
    "dotted/bracketed `path` (e.g. 'offers.price' or '[0].name') whenever the "
    "field's value is present in the provided JSON blob -- far more resilient "
    "to a redesign than a css selector. For css, prefer stable, "
    "human-meaningful selectors: an id, a data-* / aria-* / itemprop "
    "attribute, or a sibling/`:has()` relationship, over long positional "
    "paths. For ax_role, `role` must be one of the roles shown in brackets in "
    "the snapshot (e.g. 'button', 'link', 'text') and `name_contains` a short "
    "substring of that element's visible name. If a field cannot currently be "
    "located, omit it entirely -- do not guess."
)

_LOCATOR_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": ["json_ld", "hydration", "meta", "css", "ax_role"]},
        "path": {"type": ["string", "null"]},
        "selector": {"type": ["string", "null"]},
        "attribute": {"type": ["string", "null"]},
        "role": {"type": ["string", "null"]},
        "name_contains": {"type": ["string", "null"]},
    },
    "required": ["source"],
}

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "locators": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "candidates": {"type": "array", "items": _LOCATOR_ITEM_SCHEMA},
                },
                "required": ["field", "candidates"],
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


def _parse_candidate(item: dict[str, Any]) -> FieldLocator:
    return FieldLocator(
        source=item["source"],
        path=item.get("path"),
        selector=item.get("selector"),
        attribute=item.get("attribute"),
        role=item.get("role"),
        name_contains=item.get("name_contains"),
    )


def _parse_field_candidates(item: dict[str, Any]) -> list[FieldLocator]:
    # Accept the candidate-list shape; tolerate an older single-locator item.
    raw_candidates = item.get("candidates")
    if not isinstance(raw_candidates, list):
        raw_candidates = [item] if item.get("source") else []
    out: list[FieldLocator] = []
    for cand in raw_candidates:
        try:
            out.append(_parse_candidate(cand))
        except (KeyError, TypeError):
            continue
    return out


async def propose_field_locators(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    failures: dict[str, str] | None = None,
) -> dict[str, list[FieldLocator]]:
    from agentpilot.agent.reliability import RetryStrategy

    user = _build_user_message(
        fields, snapshot_text=snapshot_text, structured_data=structured_data, failures=failures
    )
    # Wrap the network call in RetryStrategy so a transient 429/5xx/timeout is
    # retried with backoff (a bad-JSON/shape ValueError is classified
    # non-retryable, so it fails fast) -- mirrors the agent loop's own LLM
    # retry, which the recipe's direct calls previously lacked.
    raw = await RetryStrategy().execute(
        lambda: chat_json_conversation(
            [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}],
            config=llm_config,
            json_schema=_JSON_SCHEMA,
        )
    )
    proposals: dict[str, list[FieldLocator]] = {}
    for item in raw.get("locators", []):
        name = item.get("field")
        if name in fields:
            candidates = _parse_field_candidates(item)
            if candidates:
                proposals[name] = candidates
    return proposals


async def _verify_and_enrich(
    candidates: list[FieldLocator],
    *,
    structured_data: dict[str, Any],
    snapshot: AXSnapshot,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> list[FieldLocator]:
    """Keep the candidates that actually resolve, augment with synthesized
    robust CSS alternatives (from the first resolving css candidate), and
    return them ordered most-robust-first. Empty if none resolve."""

    resolving: list[FieldLocator] = []
    for locator in candidates:
        value = await evaluate_field_locator(
            locator,
            structured_data=structured_data,
            session=session,
            registry=registry,
            driver=driver,
            snapshot=snapshot,
        )
        if value is not None and not (isinstance(value, str) and not value.strip()):
            resolving.append(locator)

    if not resolving:
        return []

    seed = next((loc for loc in resolving if loc.source == "css" and loc.selector), None)
    if seed is not None:
        synthesized = await synthesize_css_candidates(
            seed.selector or "",
            seed.attribute or "text",
            session=session,
            registry=registry,
            driver=driver,
        )
        for cand in synthesized:
            value = await evaluate_field_locator(
                cand,
                structured_data=structured_data,
                session=session,
                registry=registry,
                driver=driver,
                snapshot=snapshot,
            )
            if value is not None and not (isinstance(value, str) and not value.strip()):
                resolving.append(cand)

    # Stable sort by source rank keeps within-source order (the model's own
    # ranking, and synthesized-before-LLM css since synthesized are appended
    # after the seed but we want stable-attribute css ahead -- rank handles
    # source tiers; css order is preserved as discovered).
    deduped = _dedupe(resolving)
    return sorted(deduped, key=lambda loc: _SOURCE_RANK.get(loc.source, 3))


def _dedupe(locators: list[FieldLocator]) -> list[FieldLocator]:
    seen: set[tuple[Any, ...]] = set()
    out: list[FieldLocator] = []
    for loc in locators:
        key = (loc.source, loc.path, loc.selector, loc.attribute, loc.role, loc.name_contains)
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


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
) -> dict[str, list[FieldLocator]]:
    """Propose -> mechanically verify each candidate -> synthesize robust CSS
    alternatives -> (on total failure) retry with the failure fed back, up to
    `max_retries` times. Returns, per verified field, an ordered candidate
    list (most-robust-first); unresolved fields are simply absent (not an
    error) -- the caller keeps them for a later step."""

    verified: dict[str, list[FieldLocator]] = {}
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
            candidates = proposals.get(name)
            if not candidates:
                next_failures[name] = "model did not propose a locator for this field"
                continue
            resolved = await _verify_and_enrich(
                candidates,
                structured_data=structured_data,
                snapshot=snapshot,
                session=session,
                registry=registry,
                driver=driver,
            )
            if not resolved:
                next_failures[name] = "no proposed candidate resolved to a value"
                continue
            verified[name] = resolved
            del remaining[name]
        failures = next_failures
        if not failures:
            break

    return verified
