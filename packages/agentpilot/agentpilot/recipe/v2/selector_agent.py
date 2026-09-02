"""The selector agent: propose locators for a set of declared fields, verify
each one mechanically against the live page, and return ordered candidate lists
ready to be frozen into a recipe.

The shape is v1's and it was right -- propose with a model, verify without one,
feed failures back, retry once. Kadoa reports that LLM first-attempt selectors
fail to yield data 30-40% of the time, which is why validation-first authoring
is the industry norm rather than a refinement. What changes in v2 is *what* the
model is allowed to propose and how the results are ranked, and both changes
came from measuring real pages:

- **Structured data first, and say so loudly.** On a Zara product page the
  entire size/price/stock table is in the JSON-LD `hasVariant[]`; on Walmart all
  six accordion sections are in `__NEXT_DATA__`. A model that reaches for a CSS
  selector there produces a recipe that clicks six times for data one read
  already contains.
- **But not always.** Amazon ships zero JSON-LD scripts and no hydration state
  at all, so the same instruction must not make the model refuse to use CSS.
  The prompt states the preference and the exception together, because a rule
  the model has to break on a third of pages is a rule it will break on all of
  them.
- **Anchored paths only.** Walmart's own JSON carries a sponsored competitor's
  product under `contentLayout`, with the same key names as the real one.

Verification is injected as a callable rather than taking a session, so the
ranking and retry logic can be tested exhaustively without a browser.
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.models import Candidate, Locator
from agentpilot.recipe.v2.schema import FieldSpec, render_fields_for_prompt
from agentpilot.recipe.v2.transform import Transform, parse_transforms

# Priority tiers written onto the accepted candidates. Structured data is the
# most redesign-resistant source there is: a value in JSON-LD survives a
# rewrite that destroys every class name on the page.
SOURCE_PRIORITY: dict[str, int] = {
    "json_ld": 10,
    "hydration": 15,
    "meta": 20,
    "ax_role": 40,
    "text": 50,
    "css": 60,
    "xpath": 70,
}
_FALLBACK_PRIORITY = 90

# How much of the structured-data blob to show the model. Walmart's
# __NEXT_DATA__ is 352 KB; the whole thing would crowd out the snapshot and
# most of it is ad and telemetry payload.
_MAX_STRUCTURED_CHARS = 12_000
_MAX_SNAPSHOT_CHARS = 24_000

MAX_CANDIDATES_PER_FIELD = 4

# A callback that resolves one locator against the live page and returns the
# raw value, or None. Injected -- see the module docstring.
Verifier = Callable[[Locator], Awaitable[Any]]

_SYSTEM_PROMPT = """\
You are building a reusable scraping recipe by locating declared fields on a \
rendered web page. For each field, propose an ORDERED list of 1-4 candidate \
locators, best first. Multiple candidates give the recipe resilient fallbacks: \
at collection time the first one that resolves to a non-empty value wins.

SOURCE PREFERENCE, in order:
1. `json_ld`, `hydration` or `meta` with a `path`, whenever the value is \
present in the structured data shown to you. These survive a redesign that \
destroys every CSS selector on the page. If a whole TABLE of rows is present \
there (product variants, specification rows, an accordion's contents), say so \
-- reading it is far better than clicking through options one at a time.
2. `ax_role` with a role and a name substring.
3. `css` with a stable, human-meaningful selector: an id, a `data-*` / `aria-*` \
/ `itemprop` attribute, or a `:has()` relationship. Avoid long positional paths.
4. `xpath`, which you should use for exactly one thing CSS cannot express: \
selecting a cell by its sibling's text, e.g. \
`//tr[th[normalize-space()='Item Weight']]/td`. Specification tables are \
usually shaped this way.

Many pages have NO structured data at all. When the JSON blob below is empty or \
does not contain a field's value, go straight to css/xpath -- do not invent a \
path that is not there.

PATHS MUST BE ANCHORED from the root of the blob you were shown. Never use a \
recursive or wildcard search. Pages routinely embed advertisements and \
recommendations carrying the same key names as the real product, so an \
unanchored path can silently return a competitor's data.
Set `path_lang` to "jmespath" only when you need a filter or projection that \
plain dotted traversal cannot express, e.g. \
`specifications[?name=='Scent'].value | [0]`; otherwise leave it "simple".

For a css/xpath locator set `attribute` to what you want to read: "text" \
(default) reads textContent and DOES see collapsed/hidden content, so prefer it \
for accordion bodies that are present but not expanded; or name a real DOM \
attribute such as "href", "src", "content", "value".
Set `all` to true when the field is a list and the selector matches every item.

If a field's value is genuinely not on this page, OMIT it entirely. Do not \
guess: a candidate that never resolves costs a page interaction on every run \
and then reports "empty" as though the site had changed.\
"""

_LOCATOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["json_ld", "hydration", "meta", "ax_role", "css", "xpath", "text"],
        },
        "path": {"type": ["string", "null"]},
        "path_lang": {"type": ["string", "null"], "enum": ["simple", "jmespath", None]},
        "selector": {"type": ["string", "null"]},
        "attribute": {"type": ["string", "null"]},
        "all": {"type": ["boolean", "null"]},
        "index": {"type": ["integer", "null"]},
        "role": {"type": ["string", "null"]},
        "name_contains": {"type": ["string", "null"]},
        "text": {"type": ["string", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["kind"],
}

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "candidates": {"type": "array", "items": _LOCATOR_SCHEMA},
                },
                "required": ["field", "candidates"],
            },
        }
    },
    "required": ["fields"],
}


def build_user_message(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    failures: dict[str, str] | None = None,
) -> str:
    blob = _json.dumps(structured_data, ensure_ascii=False)
    truncated = len(blob) > _MAX_STRUCTURED_CHARS
    parts = [
        f"Fields to locate:\n{render_fields_for_prompt(fields)}",
        f"\nPage snapshot:\n{snapshot_text[:_MAX_SNAPSHOT_CHARS]}",
        "\nParsed structured data (json_ld / hydration / metadata)"
        + (" -- TRUNCATED, deeper keys may exist" if truncated else "")
        + f":\n{blob[:_MAX_STRUCTURED_CHARS]}",
    ]
    if not structured_data or not any(structured_data.values()):
        parts.append(
            "\nNOTE: this page publishes no usable structured data. Use css/xpath."
        )
    if failures:
        lines = "\n".join(f"- {name}: {reason}" for name, reason in failures.items())
        parts.append(
            "\nThese proposals from your last attempt did NOT resolve against the live "
            f"page. Propose something different for them:\n{lines}"
        )
    return "\n".join(parts)


def _parse_locator(item: dict[str, Any]) -> Locator | None:
    kind = item.get("kind")
    if kind not in SOURCE_PRIORITY:
        return None
    if kind in ("json_ld", "hydration", "meta") and not item.get("path"):
        return None
    if kind in ("css", "xpath") and not item.get("selector"):
        return None
    if kind == "ax_role" and not item.get("role"):
        return None
    return Locator(
        kind=kind,
        selector=item.get("selector"),
        index=item.get("index"),
        all=bool(item.get("all") or False),
        attribute=item.get("attribute") or "text",
        role=item.get("role"),
        name_contains=item.get("name_contains"),
        path=item.get("path"),
        path_lang=item.get("path_lang") or "simple",
        text=item.get("text"),
    )


def parse_proposals(
    raw: dict[str, Any], fields: dict[str, FieldSpec]
) -> dict[str, list[Locator]]:
    """Parse the model's reply, dropping anything malformed or for a field we
    did not ask about. A bad item is skipped rather than failing the batch: the
    other fields in the same reply are still useful."""

    out: dict[str, list[Locator]] = {}
    for item in raw.get("fields") or []:
        name = item.get("field")
        if name not in fields:
            continue
        locators = [
            loc
            for loc in (_parse_locator(c) for c in (item.get("candidates") or []))
            if loc is not None
        ]
        if locators:
            out[name] = locators[:MAX_CANDIDATES_PER_FIELD]
    return out


async def propose_locators(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    failures: dict[str, str] | None = None,
) -> dict[str, list[Locator]]:
    from agentpilot.agent.reliability import RetryStrategy

    user = build_user_message(
        fields,
        snapshot_text=snapshot_text,
        structured_data=structured_data,
        failures=failures,
    )
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
    return parse_proposals(raw, fields)


def rank_candidates(locators: list[Locator], *, verified_on: int = 1) -> list[Candidate]:
    """Turn verified locators into ordered candidates.

    Priority comes from the source tier, spaced so a human or a heal can insert
    between tiers without renumbering. Within a tier the model's own ordering is
    preserved -- `sorted` is stable, and the model saw the page.
    """

    candidates = [
        Candidate(
            locator=loc,
            priority=SOURCE_PRIORITY.get(loc.kind, _FALLBACK_PRIORITY) + position,
            verified_on=verified_on,
        )
        for position, loc in enumerate(locators)
    ]
    return sorted(candidates, key=lambda c: c.priority)


def dedupe_locators(locators: list[Locator]) -> list[Locator]:
    seen: set[tuple[Any, ...]] = set()
    out: list[Locator] = []
    for loc in locators:
        key = (
            loc.kind, loc.selector, loc.path, loc.path_lang,
            loc.attribute, loc.all, loc.index, loc.role, loc.name_contains,
        )
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def infer_transform(spec: FieldSpec, locator: Locator) -> list[Transform] | None:
    """A per-candidate transform for the cases where the source implies the
    cleanup, so the model does not have to get it right.

    The motivating pair is real: Zara's JSON-LD gives `"9550"` while its DOM
    gives `"₹ 9,550.00"`. One field-level pipeline cannot serve both without
    being written for the worse case and mangling the better one -- so a
    numeric field reading from structured data gets a bare cast, and the same
    field reading from the DOM gets the digit-extraction it actually needs.
    """

    if spec.type.kind != "scalar":
        return None
    value_type = spec.type.value_type
    if value_type not in ("number", "float", "price", "integer"):
        return None
    if locator.is_structured:
        return parse_transforms([{"op": "cast", "to": value_type}])
    return parse_transforms([
        {"op": "regex_extract", "pattern": r"([\d.,]+)"},
        {"op": "regex_replace", "pattern": ",", "repl": ""},
        {"op": "cast", "to": value_type},
    ])


async def verify_locators(
    locators: list[Locator], *, verify: Verifier
) -> tuple[list[Locator], str | None]:
    """Keep the locators that actually resolve to a non-empty value right now.

    Returns the survivors and, when none survive, a reason to feed back to the
    model on the retry -- which is the whole point of doing this per-step while
    the page state that revealed the field is still live.
    """

    from agentpilot.recipe.v2.resolve import is_empty

    resolving: list[Locator] = []
    last_error: str | None = None
    for loc in locators:
        try:
            value = await verify(loc)
        except Exception as exc:  # noqa: BLE001 - a bad selector is data, not a crash
            last_error = f"{loc.kind} locator raised: {exc}"
            continue
        if not is_empty(value):
            resolving.append(loc)
    if resolving:
        return resolving, None
    return [], last_error or "no proposed candidate resolved to a value"


async def propose_and_verify(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    verify: Verifier,
    max_retries: int = 1,
    verified_on: int = 1,
) -> dict[str, list[Candidate]]:
    """Propose -> verify -> (on total failure) retry with the failure fed back.

    Fields that could not be located are simply absent from the result. That is
    not an error: the caller keeps them and tries again on a later exploration
    step, once the page has been interacted with further.
    """

    verified: dict[str, list[Candidate]] = {}
    remaining = dict(fields)
    failures: dict[str, str] | None = None

    for _attempt in range(max_retries + 1):
        if not remaining:
            break
        proposals = await propose_locators(
            remaining,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            llm_config=llm_config,
            failures=failures,
        )
        next_failures: dict[str, str] = {}
        for name in list(remaining):
            locators = proposals.get(name)
            if not locators:
                next_failures[name] = "model did not propose a locator for this field"
                continue
            resolving, reason = await verify_locators(
                dedupe_locators(locators), verify=verify
            )
            if not resolving:
                next_failures[name] = reason or "no candidate resolved"
                continue
            spec = remaining[name]
            candidates = rank_candidates(resolving, verified_on=verified_on)
            verified[name] = [
                Candidate(
                    locator=c.locator,
                    priority=c.priority,
                    verified_on=c.verified_on,
                    transform=infer_transform(spec, c.locator),
                )
                for c in candidates
            ]
            del remaining[name]
        failures = next_failures
        if not failures:
            break

    return verified
