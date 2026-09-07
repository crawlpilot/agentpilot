"""Stage 5b: what would make a collected value wrong.

Resolving is not the same as being right, and the gap is the failure that
silently poisons a dataset rather than breaking a run. `schema.py` states the
case this exists for: on a Walmart product page the site's own JSON carries a
*sponsored competitor's* name and price under the same key names, so a locator
can resolve to a perfectly well-typed value from the wrong product and keep
doing so forever.

Two tiers, and the cheap one carries most of the weight:

**Baseline** is mechanical -- no model, no page, no cost. It knows a required
field must not be empty, that a negative price is always wrong, and that a
field carrying candidates of two different kinds can have them checked against
each other. That last one is the good one: `cross_source_agrees` is already
wired in `replay.py` and is described there as "nearly free -- most fields
already carry two or three candidates and only the first is ever read". The DOM
fallback added in `selector_agent.py` means many more fields now qualify.

**Proposed** asks a model for the assertions that need to know what the value
*means* -- a SKU's shape, the set a size can come from. Kept deliberately
narrow: a wrong `matches` regex marks every future run suspect, which is worse
than having no assertion at all, so the prompt is told to decline when unsure
and anything malformed is dropped rather than repaired.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.models import Candidate
from agentpilot.recipe.v2.schema import Assertion, FieldSpec, render_fields_for_prompt

_NUMERIC = ("number", "float", "price", "integer")

# Types whose values are never negative in any catalogue this targets. A stock
# count or a price below zero is a parse that went wrong -- a currency symbol
# read as a minus sign, or a discount picked up instead of the price.
_NON_NEGATIVE = ("price",)

_MAX_ASSERTIONS_PER_FIELD = 3


def _kinds(candidates: list[Candidate]) -> set[str]:
    return {c.locator.kind for c in candidates}


def baseline_assertions(
    fields: dict[str, FieldSpec],
    bindings: dict[str, list[Candidate]] | None = None,
) -> dict[str, list[Assertion]]:
    """The assertions that follow from the schema alone, plus the candidate
    chain when one is supplied. Pure."""

    bindings = bindings or {}
    out: dict[str, list[Assertion]] = {}

    for name, spec in fields.items():
        proposed: list[Assertion] = []

        if spec.required:
            proposed.append(Assertion(kind="not_empty"))

        if spec.type.kind == "scalar" and spec.type.value_type in _NON_NEGATIVE:
            proposed.append(Assertion(kind="range", min=0))

        # Two candidates of *different kinds* means the page states this value
        # twice and the two can be compared. `_cross_source_value` deliberately
        # compares by kind rather than by family, so json_ld vs meta counts.
        if len(_kinds(bindings.get(name, []))) >= 2:
            proposed.append(Assertion(kind="cross_source_agrees"))

        if proposed:
            out[name] = proposed
    return out


def with_assertions(
    fields: dict[str, FieldSpec], proposed: dict[str, list[Assertion]]
) -> dict[str, FieldSpec]:
    """Merge assertions onto the specs, keeping any the caller already declared.

    A caller's own assertion always survives: they said what wrong looks like
    for their data, and a proposal is a suggestion.
    """

    merged: dict[str, FieldSpec] = {}
    for name, spec in fields.items():
        extra = [a for a in proposed.get(name, []) if a not in spec.assertions]
        if not extra:
            merged[name] = spec
            continue
        merged[name] = replace(
            spec, assertions=[*spec.assertions, *extra][:_MAX_ASSERTIONS_PER_FIELD]
        )
    return merged


_SYSTEM_PROMPT = """\
You add validity checks to the fields of a web-scraping recipe. A check runs on \
every future run and marks the value suspect when it fails.

Only propose a check when you are confident it would hold for EVERY page of \
this kind, not just the one whose sample values you were shown. A check that \
is merely usually true is worse than no check: it marks good data as suspect \
forever, and whoever reads the report stops trusting all of them.

Available kinds:
- "range" with min and/or max -- for numbers whose plausible bounds you know.
- "matches" with a regex -- for values with a genuinely fixed shape (a SKU \
format, an ISBN). Not for free text.
- "length" with min and/or max -- for lists that should never be empty or \
absurdly long, and for strings with a known size.
- "in_set" with values -- ONLY when the field can take a small, closed set you \
are certain of.

Do not propose "not_empty" or "cross_source_agrees"; those are added \
mechanically. Return an empty list for any field you are not sure about -- that \
is the expected answer for most free-text fields.\
"""

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "assertions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["range", "matches", "length", "in_set"],
                                },
                                "min": {"type": ["number", "null"]},
                                "max": {"type": ["number", "null"]},
                                "regex": {"type": ["string", "null"]},
                                "values": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["kind"],
                        },
                    },
                },
                "required": ["field", "assertions"],
            },
        }
    },
    "required": ["fields"],
}

_PROPOSABLE = frozenset({"range", "matches", "length", "in_set"})


def parse_assertions(
    raw: dict[str, Any], fields: dict[str, FieldSpec]
) -> dict[str, list[Assertion]]:
    """Parse the model's reply, dropping anything that could not be evaluated.

    An assertion missing the operand its kind needs would fail *every* run --
    `_evaluate_one` returns "no regex configured" as a failure, not a skip -- so
    a malformed proposal is dropped rather than stored.
    """

    import re

    out: dict[str, list[Assertion]] = {}
    for item in raw.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("field")
        if name not in fields:
            continue
        kept: list[Assertion] = []
        for entry in item.get("assertions") or []:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind not in _PROPOSABLE:
                continue
            minimum, maximum = entry.get("min"), entry.get("max")
            regex = entry.get("regex")
            values = [v for v in (entry.get("values") or []) if v is not None]

            if kind in ("range", "length") and minimum is None and maximum is None:
                continue
            if kind == "matches":
                if not regex:
                    continue
                try:
                    re.compile(regex)
                except re.error:
                    # A regex that does not compile reports "invalid regex" as a
                    # failure on every run, forever.
                    continue
            if kind == "in_set" and not values:
                continue

            kept.append(
                Assertion(
                    kind=kind,
                    min=float(minimum) if minimum is not None else None,
                    max=float(maximum) if maximum is not None else None,
                    regex=regex,
                    values=values,
                )
            )
        if kept:
            out[name] = kept
    return out


async def propose_assertions(
    fields: dict[str, FieldSpec],
    *,
    samples: dict[str, Any],
    llm_config: LLMConfig,
) -> dict[str, list[Assertion]]:
    """One call for the checks that need to know what the value means.

    Best-effort: an LLM failure returns nothing rather than raising, because a
    recipe with no proposed assertions is a working recipe and the baseline has
    already been applied.
    """

    import json as _json

    from agentpilot.agent.reliability import RetryStrategy

    numeric_or_shaped = {
        name: spec
        for name, spec in fields.items()
        if spec.type.kind != "table"
        and (
            spec.type.value_type in _NUMERIC
            or spec.type.kind == "list"
            or spec.type.value_type in ("string", "text")
        )
    }
    if not numeric_or_shaped:
        return {}

    user = (
        f"Fields:\n{render_fields_for_prompt(numeric_or_shaped)}\n\n"
        "Values collected from one real page (one sample -- do not infer bounds "
        "that only fit this single observation):\n"
        f"{_json.dumps({k: samples.get(k) for k in numeric_or_shaped}, default=str)[:4000]}"
    )
    try:
        raw = await RetryStrategy().execute(
            lambda: chat_json_conversation(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                config=llm_config,
                json_schema=_JSON_SCHEMA,
            )
        )
    except Exception:  # noqa: BLE001 - see docstring: nothing here is load-bearing
        return {}
    return parse_assertions(raw, numeric_or_shaped)
