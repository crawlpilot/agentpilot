"""Stage 7: does the collected data actually satisfy the declared contract?

Everything before this answers "did the locator resolve?". Nothing before this
answers "is this the right value?", and the gap between those two questions is
the failure that silently poisons a dataset rather than breaking a run.

`schema.py` states the case this exists for: on a Walmart product page the
site's own JSON carries a *sponsored competitor's* name and price under the
same key names. A locator can resolve to a perfectly well-typed value from the
wrong product and keep doing so forever. Assertions catch the coarse shape of
that -- a negative price, an empty required field -- because they are cheap
enough to run on every future replay. They cannot catch `name` bound to the
breadcrumb trail, or nine image URLs collected from a page showing thirty.

Three things make this a judge rather than another extraction pass:

**It reads the page, not the extractor's report.** `agent/judge.py` already
says it: "treat the observed page state as ground truth, not the agent's
self-report". Handed only the values, a model has nothing to check them
against and will ratify whatever it is shown.

**It fails open.** A judge outage must never fabricate a failure -- that is
`agent/judge.py`'s documented contract and it holds here for a second reason:
failing open sends an unjudged draft to a human, who is the next gate anyway,
whereas failing closed parks the run waiting on a person for a question the
judge could not even ask. The verdict records that it was unavailable so the
reviewer knows the draft is unvetted.

**Its reasons are actionable.** A rejection is fed straight back to
`propose_locators` as `failures` feedback, which is why the prompt demands a
concrete statement of what the value *is* instead of a grade.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.schema import FieldSpec, render_fields_for_prompt

# The judge only needs enough page to corroborate a value, and a full snapshot
# of a product page routinely runs past 100 KB.
_MAX_PAGE_CHARS = 16_000
_MAX_VALUE_CHARS = 600


@dataclass(frozen=True)
class FieldVerdict:
    field: str
    ok: bool
    reason: str = ""
    absent: bool = False
    """The page does not contain this at all -- as opposed to containing it
    somewhere the scraper did not look.

    The distinction is the difference between a fixable problem and an
    unfixable one, and without it the two were indistinguishable: a rejection
    sent the selector agent back to find a better locator for a value that was
    never there, it returned a different wrong element, and the judge rejected
    that too. Repair cannot fix absence, so absence must be sayable."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field, "ok": self.ok,
            "reason": self.reason, "absent": self.absent,
        }


@dataclass
class DataVerdict:
    passed: bool
    verdicts: dict[str, FieldVerdict] = field(default_factory=dict)
    errored: bool = False
    """True when the judge itself failed. The caller must fail open -- see the
    module docstring."""

    @property
    def rejected(self) -> dict[str, str]:
        """Field -> why it is wrong, for the ones repair can actually help.

        Absent fields are excluded on purpose. Feeding one back to the selector
        agent asks it to find something that is not there; it obliges, returns a
        different wrong element, and the next round rejects that instead. That
        is the loop this property exists to not start.
        """

        return {
            v.field: v.reason
            for v in self.verdicts.values()
            if not v.ok and not v.absent
        }

    @property
    def absent(self) -> dict[str, str]:
        """Field -> why it cannot be collected from this page.

        Goes to a person, not to another repair round: the useful answers are
        "drop it", "it's on a different page", or "here is where it actually
        is", and none of them are things the selector agent can decide.
        """

        return {v.field: v.reason for v in self.verdicts.values() if v.absent}

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "errored": self.errored,
            "fields": {k: v.to_dict() for k, v in self.verdicts.items()},
        }


_SYSTEM_PROMPT = """\
You are a strict, skeptical reviewer of data a scraper collected from a web \
page. For each field you are given what the caller ASKED FOR, what the scraper \
RETURNED, and the rendered text of the page it came from.

Treat the page text as ground truth. Be initially doubtful that a returned \
value is correct: a scraper locator can land on a plausible, well-typed value \
belonging to something else entirely -- a related product, a sponsored \
placement, a breadcrumb, a "customers also bought" tile -- and such a value \
looks perfectly fine in isolation. Isolation is exactly what you are being \
asked to look past.

Judge each field on:
- Is this value the thing the field asked for, or a different thing that \
happens to have the right type? A product title and a breadcrumb trail are \
both strings.
- Does it belong to the page's MAIN subject rather than to something \
recommended, sponsored or related?
- For a list: does its length look right against the page, or does it look \
truncated to whatever the first container held?
- For rows: do they line up, and is the column labelled `x` really x?

Answer ok=false ONLY when the page text gives you a concrete reason. A value \
you cannot corroborate either way is ok=true with a short note -- an unproven \
value is not a wrong one, and a false rejection sends a working recipe back to \
a human for nothing.

When the page simply DOES NOT CONTAIN what the field asked for, set \
absent=true as well as ok=false, and say what is missing. This matters more \
than it looks. "Wrong value" sends the scraper back to find a better one; \
"not on this page" tells it to stop looking. Get that backwards on a field the \
page does not have and it will keep returning different wrong elements for \
ever, each rejected in turn. If the caller asked for a warranty period and \
this page is about a dress, that is absent -- not a bad selector.

When you reject, say what the value ACTUALLY is, not that it is wrong. Your \
reason is fed back verbatim to the component that will pick a new locator, so \
"this is the breadcrumb trail, the product title is in the h1 below it" is \
useful and "incorrect value" is not.

A field the scraper returned nothing for is NOT yours to judge -- skip it. \
Absence is already reported elsewhere.\
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
                    "ok": {"type": "boolean"},
                    "absent": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["field", "ok"],
            },
        }
    },
    "required": ["fields"],
}


def _render_value(value: Any) -> str:
    text = _json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= _MAX_VALUE_CHARS:
        return text
    return f"{text[:_MAX_VALUE_CHARS]}... (truncated, {len(text)} chars total)"


def build_user_message(
    fields: dict[str, FieldSpec],
    *,
    data: dict[str, Any],
    page_text: str,
    provenance: dict[str, dict[str, Any]] | None = None,
) -> str:
    lines = [f"Fields the caller asked for:\n{render_fields_for_prompt(fields)}", ""]
    lines.append("What the scraper returned:")
    for name in fields:
        if name not in data:
            continue
        source = (provenance or {}).get(name, {}).get("source")
        origin = f"  [read from: {source}]" if source else ""
        lines.append(f"- {name} = {_render_value(data[name])}{origin}")
    lines.append("")
    lines.append(f"Rendered page text:\n{page_text[:_MAX_PAGE_CHARS]}")
    return "\n".join(lines)


def parse_verdict(raw: dict[str, Any], fields: dict[str, FieldSpec]) -> DataVerdict:
    """Parse the reply. An entry for a field that was not judged, or one with no
    usable reason for a rejection, is treated as a pass.

    Rejecting on a malformed reply would send working recipes back for human
    review with nothing to tell the human, which is the same cost as a false
    positive and none of the benefit.
    """

    verdicts: dict[str, FieldVerdict] = {}
    for item in raw.get("fields") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("field")
        if name not in fields:
            continue
        ok = bool(item.get("ok", True))
        absent = bool(item.get("absent", False))
        reason = str(item.get("reason") or "").strip()
        if not ok and not reason:
            ok = True
            absent = False
            reason = "rejected without a reason, so it was not acted on"
        # `absent` only means anything on a rejection. A field marked present
        # and absent at once is a confused reply, and the safe reading is that
        # the value stands.
        verdicts[name] = FieldVerdict(
            field=name, ok=ok, reason=reason, absent=absent and not ok
        )

    return DataVerdict(passed=all(v.ok for v in verdicts.values()), verdicts=verdicts)


async def judge_collection(
    fields: dict[str, FieldSpec],
    *,
    data: dict[str, Any],
    page_text: str,
    provenance: dict[str, dict[str, Any]] | None = None,
    llm_config: LLMConfig,
) -> DataVerdict:
    """One skeptical pass over the collected values.

    Returns a fail-open verdict (`passed=True, errored=True`) when the call
    raises -- see the module docstring for why that is the right direction here.
    """

    judgeable = {
        name: spec
        for name, spec in fields.items()
        if name in data and data[name] not in (None, "", [], {})
    }
    if not judgeable:
        # Nothing resolved, so there is nothing to be wrong about. That is a
        # completeness problem, and the unresolved list already reports it.
        return DataVerdict(passed=True)

    user = build_user_message(
        judgeable, data=data, page_text=page_text, provenance=provenance
    )
    try:
        raw = await chat_json_conversation(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            config=llm_config,
            json_schema=_JSON_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - fail open, deliberately
        return DataVerdict(
            passed=True,
            errored=True,
            verdicts={
                name: FieldVerdict(field=name, ok=True, reason=f"judge unavailable: {exc}")
                for name in judgeable
            },
        )
    return parse_verdict(raw, judgeable)
