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
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any

import structlog

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.models import Candidate, Locator
from agentpilot.recipe.v2.schema import FieldSpec, render_fields_for_prompt
from agentpilot.recipe.v2.transform import Transform, TransformContext, parse_transforms

log = structlog.get_logger(__name__)

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

# How much of a picked section's markup to show. A section is small by
# construction -- that is the point of scoping -- and a cap this size only bites
# on someone selecting most of the page, where scoping was buying nothing
# anyway.
_MAX_FRAGMENT_CHARS = 20_000

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


_WITHIN_SYSTEM_PROMPT = """\
A person has pointed at the REGION of a web page that contains a value they \
want, and you are given that region's HTML. Find the value inside it.

They pointed at the region, not the value, because the region is what a human \
can see and the value is what you are better at locating. Expect the region to \
contain a heading or label naming the thing -- that label is NOT the value. On \
a specifications section, "Specifications" is the heading and the data is the \
rows below it.

Propose 1-4 css or xpath locators, best first, RELATIVE TO THE REGION. Do not \
include the region's own selector in what you propose: it is applied \
separately as a scope, so repeating it would look for the region inside \
itself.

Prefer a selector that would still work if the page around this region were \
redesigned -- a `data-*` attribute, an `itemprop`, a semantic tag, a \
relationship like `dt:has(+dd)`. Inside a region that is already scoped you \
rarely need a class name at all.

Use xpath for the one thing css cannot do: select a cell by its sibling's \
text, e.g. `.//tr[th[normalize-space()='Item Weight']]/td`. Specification \
tables are usually shaped that way. Keep xpath RELATIVE (`.//`, not `//`) -- \
an absolute expression ignores the scope entirely and searches the whole page.

Set `all` to true when the field is a list and the selector matches every item. \
Set `attribute` to what you want to read: "text" (the default) reads \
textContent and DOES see collapsed content, or name a real attribute like \
"href" or "src".

If the value genuinely is not in this region, return no candidates for it \
rather than guessing. They may have pointed at the wrong region, and an empty \
answer says so.\
"""


async def propose_within(
    fields: dict[str, FieldSpec],
    *,
    scope: Locator,
    fragment_html: str,
    llm_config: LLMConfig,
    verify: Verifier,
    verified_on: int = 1,
    page_url: str = "",
) -> dict[str, list[Candidate]]:
    """Locate fields inside a region a person pointed at.

    The scoped counterpart to `propose_and_verify`, and it exists because the
    two questions are different. Across a whole page the model is choosing among
    thousands of nodes and the prompt is dominated by everything irrelevant;
    inside a section it is choosing among dozens, and the person has already
    supplied the piece of knowledge they actually had.

    Every DOM proposal comes back carrying `within=scope`. That is not just
    accuracy: a selector scoped to `#specifications` keeps working when the page
    around it is redesigned, and stops matching a same-shaped table somewhere
    else entirely.

    Structured locators are deliberately NOT scoped. A `json_ld` path has no DOM
    container, so attaching one would make it unresolvable -- and those are the
    candidates worth keeping most.
    """

    # Imported here, matching the other call sites: this module is reached from
    # the agent loop, and `agent.reliability` reaching back would close a cycle.
    from agentpilot.agent.reliability import RetryStrategy

    ctx = TransformContext(url=page_url)
    user = (
        f"Fields to find in this region:\n{render_fields_for_prompt(fields)}\n\n"
        f"Region HTML:\n{fragment_html[:_MAX_FRAGMENT_CHARS]}"
    )
    raw = await RetryStrategy().execute(
        lambda: chat_json_conversation(
            [
                {"role": "system", "content": _WITHIN_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            config=llm_config,
            json_schema=_JSON_SCHEMA,
        )
    )

    verified: dict[str, list[Candidate]] = {}
    for name, locators in parse_proposals(raw, fields).items():
        scoped = [
            loc if loc.is_structured else replace(loc, within=scope)
            for loc in dedupe_locators(locators)
        ]
        # Verified against the live page like anything else. A person pointing
        # at the right region does not make a proposal correct, and binding one
        # unchecked would swap a known gap for a silent one.
        spec = fields[name]
        resolving, _reason = await verify_locators(
            scoped, verify=verify, spec=spec, ctx=ctx
        )
        if not resolving:
            continue
        verified[name] = _to_candidates(spec, resolving, verified_on=verified_on)
    return verified


def _to_candidates(
    spec: FieldSpec, verified: list[VerifiedLocator], *, verified_on: int
) -> list[Candidate]:
    """Ranked candidates from what actually produced a value.

    The transform written onto each candidate is the one that was *run* during
    verification, not one inferred again afterwards -- otherwise the recipe
    could ship a pipeline nothing ever tested.
    """

    by_locator = {id(v.locator): v for v in verified}
    out: list[Candidate] = []
    for ranked in rank_candidates([v.locator for v in verified], verified_on=verified_on):
        hit = by_locator.get(id(ranked.locator))
        # `None` when the field carries its own pipeline, so `resolve_field`
        # falls through to `spec.transform` -- which is the pipeline that was
        # verified. Writing the inferred one here would shadow it at replay,
        # exactly as it did at build time before `pipeline_for`.
        transform = None if spec.transform else infer_transform(spec, ranked.locator)
        out.append(
            Candidate(
                locator=ranked.locator,
                priority=ranked.priority,
                verified_on=ranked.verified_on,
                transform=transform,
                note="; ".join(hit.notes) if hit and hit.notes else None,
            )
        )
    return out


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


_NUMERIC = ("number", "float", "price", "integer")


def _scalar_cleanup(value_type: str, *, structured: bool) -> list[dict[str, Any]]:
    """The cleanup one scalar value needs, given where it was read from."""

    if value_type in _NUMERIC:
        if structured:
            return [{"op": "cast", "to": value_type}]
        return [
            {"op": "regex_extract", "pattern": r"([\d.,]+)"},
            {"op": "regex_replace", "pattern": ",", "repl": ""},
            {"op": "cast", "to": value_type},
        ]
    if value_type == "url":
        # Relative hrefs are the commonest broken output there is: `/p/123`
        # read off a page and handed to a caller who has no idea what it was
        # relative to. `url_resolve` joins against the page URL, and applies to
        # structured sources too -- JSON-LD carries relative URLs just as often
        # as the DOM does.
        return [{"op": "url_resolve"}]
    if value_type in ("date", "datetime"):
        # Structured dates are usually already ISO-8601, and `cast` is a no-op
        # on those; a DOM date needs the surrounding text trimmed first.
        return (
            [{"op": "cast", "to": value_type}]
            if structured
            else [{"op": "trim"}, {"op": "collapse_ws"}, {"op": "cast", "to": value_type}]
        )
    if value_type in ("string", "text") and not structured:
        # Rendered text arrives with the page's indentation in it.
        return [{"op": "trim"}, {"op": "collapse_ws"}]
    return []


def infer_transform(spec: FieldSpec, locator: Locator) -> list[Transform] | None:
    """A per-candidate transform for the cases where the source implies the
    cleanup, so the model does not have to get it right.

    The motivating pair is real: Zara's JSON-LD gives `"9550"` while its DOM
    gives `"₹ 9,550.00"`. One field-level pipeline cannot serve both without
    being written for the worse case and mangling the better one -- so a numeric
    field reading from structured data gets a bare cast, and the same field
    reading from the DOM gets the digit-extraction it actually needs. That split
    is *why* this is per-candidate rather than per-field, and it generalises:
    every branch below asks the same question of a different type.

    A `table` field gets nothing, because it has no value of its own -- its
    columns are separate leaf fields with their own specs, and each one comes
    back through here.
    """

    kind = spec.type.kind
    structured = locator.is_structured

    if kind == "scalar":
        ops = _scalar_cleanup(spec.type.value_type, structured=structured)
        return parse_transforms(ops) if ops else None

    if kind == "list":
        item_type = spec.type.items.value_type if spec.type.items else "string"
        # Scalar ops map over a list element-wise (`_apply_one`), so the item
        # cleanup is written once and applies to each member. `filter_empty`
        # last: a list read off the DOM routinely carries blank entries from
        # layout elements caught by the same selector, and a caller asked for
        # the values, not the gaps.
        ops = [
            *_scalar_cleanup(item_type, structured=structured),
            {"op": "filter_empty"},
        ]
        return parse_transforms(ops)

    return None


@dataclass
class VerifiedLocator:
    """A locator that produced a usable value, and the value it produced."""

    locator: Locator
    raw: Any
    value: Any
    """What the field will actually contain -- the raw read with its transform
    applied. Nothing at build time could see this before; it existed only at
    replay, minutes later and behind a judge."""

    notes: list[str] = dataclass_field(default_factory=list)
    """Assertions this value fails. Recorded, not enforced: an assertion the
    build guessed is a weaker claim than a value read off the page."""


def pipeline_for(spec: FieldSpec, locator: Locator) -> list[Transform]:
    """The cleanup a candidate on this locator will actually run.

    An explicit `spec.transform` wins over the inferred one. `infer_transform`
    is a rule-of-thumb -- numbers get digit extraction, urls get resolving --
    while `spec.transform` is either the caller's own declaration or a pipeline
    that was proposed against a real value and verified against it. A guess must
    not override either.

    Getting this backwards is not academic: it silently defeated the transform
    repair, which writes its verified pipeline onto the spec and then watched
    `infer_transform` shadow it on the very next read.

    Never composed. `resolve_field` reads `cand.transform if cand.transform is
    not None else spec.transform`, so a build that composed the two would
    validate a pipeline replay never runs.
    """

    if spec.transform:
        return spec.transform
    return infer_transform(spec, locator) or []


async def verify_locators(
    locators: list[Locator],
    *,
    verify: Verifier,
    spec: FieldSpec | None = None,
    ctx: TransformContext | None = None,
) -> tuple[list[VerifiedLocator], str | None]:
    """Keep the locators that produce a usable value right now.

    "Usable" means what `resolve_field` means by it, and that is the point of
    this function's existence in this shape. Replay's rule is:

        Transforming before deciding is deliberate. A candidate that resolves
        to markup which strips to nothing, or to a string that casts to None,
        has not actually produced a value.

    This used to check only that the RAW read was non-empty, throw the value
    away, and accept. So a price reading "Contact us for pricing" verified
    perfectly, `cast to price` turned it into nothing at replay, and the judge
    was the first thing in the pipeline to notice -- a full page load and two
    model calls later, to learn something that was knowable here.

    Without `spec` the old raw-only behaviour is kept, for callers that have no
    field to transform against.
    """

    from agentpilot.recipe.v2.resolve import evaluate_assertions, is_empty
    from agentpilot.recipe.v2.transform import TransformError, apply_transforms

    context = ctx or TransformContext()
    resolving: list[VerifiedLocator] = []
    last_error: str | None = None

    for loc in locators:
        try:
            raw = await verify(loc)
        except Exception as exc:  # noqa: BLE001 - a bad selector is data, not a crash
            last_error = f"{loc.kind} locator raised: {exc}"
            continue
        if is_empty(raw):
            continue

        if spec is None:
            resolving.append(VerifiedLocator(locator=loc, raw=raw, value=raw))
            continue

        pipeline = pipeline_for(spec, loc)
        try:
            value = apply_transforms(raw, pipeline, context) if pipeline else raw
        except TransformError as exc:
            last_error = f"read {_show(raw)} but the cleanup failed: {exc}"
            continue
        if is_empty(value):
            last_error = f"read {_show(raw)} but cleaning it up left nothing"
            continue

        # Checked, not enforced. See `VerifiedLocator.notes`.
        notes = [
            f"{r.kind}: {r.detail}" if r.detail else r.kind
            for r in evaluate_assertions(value, spec.assertions)
            if not r.passed
        ]
        resolving.append(VerifiedLocator(locator=loc, raw=raw, value=value, notes=notes))

    if resolving:
        return resolving, None
    return [], last_error or "no proposed candidate resolved to a value"


def _show(value: Any) -> str:
    """A raw read, short enough to put in a reason a model will read back."""

    text = value if isinstance(value, str) else _json.dumps(value, default=str)
    text = " ".join(text.split())
    return f'"{text[:120]}…"' if len(text) > 120 else f'"{text}"' 


_DOM_KINDS = frozenset({"css", "xpath", "ax_role", "text"})

_DOM_FALLBACK_INSTRUCTION = """\
For each field below you already have a locator that reads it out of the page's \
JSON. Now propose a SECOND way to read the same value, from the RENDERED DOM \
ONLY -- a css, xpath or ax_role locator. Do not propose another json_ld, \
hydration or meta path for these; a JSON path is what they already have.

This is the fallback for the day the site renames a key in its hydration state. \
The rendered value survives that; the path does not, and a field bound only to \
JSON goes silently empty while the page still shows the value to a human.\
"""


def needs_dom_fallback(candidates: list[Candidate]) -> bool:
    """Whether a field is bound only to structured sources.

    A JSON-backed field is the *right* answer -- `SOURCE_PRIORITY` ranks
    `json_ld` at 10 against `css` at 60 because a value in JSON-LD survives a
    redesign that destroys every class name on the page. But the failure modes
    are not symmetric. A CSS selector breaking is loud and common, and the
    candidate chain is built for it. A hydration key being renamed is silent and
    total: nothing resolves, the field is simply absent, and the page still
    renders the value perfectly to anyone who looks.

    So the chain wants one of each, not the best of one.
    """

    if not candidates:
        return False
    return all(c.locator.is_structured for c in candidates)


async def _add_dom_fallbacks(
    verified: dict[str, list[Candidate]],
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    verify: Verifier,
    verified_on: int,
    ctx: TransformContext,
) -> None:
    """One extra call for the fields that resolved only out of JSON.

    Mutates `verified` in place. Best-effort throughout: a field that already
    has a working JSON locator is not made worse by failing to find a DOM one,
    so nothing here raises and nothing is removed.
    """

    wanted = {
        name: fields[name]
        for name, candidates in verified.items()
        if name in fields and needs_dom_fallback(candidates)
    }
    if not wanted:
        return

    proposals = await propose_locators(
        wanted,
        snapshot_text=snapshot_text,
        structured_data=structured_data,
        llm_config=llm_config,
        failures={name: _DOM_FALLBACK_INSTRUCTION for name in wanted},
    )
    for name, locators in proposals.items():
        dom_only = [loc for loc in locators if loc.kind in _DOM_KINDS]
        if not dom_only:
            continue
        spec = wanted[name]
        resolving, _reason = await verify_locators(
            dedupe_locators(dom_only), verify=verify, spec=spec, ctx=ctx
        )
        if not resolving:
            continue
        existing = verified[name]
        # Ranked from the end of the existing chain so the JSON candidate keeps
        # winning: the fallback is for when the primary stops resolving, not a
        # competitor for the common case.
        fallbacks = _to_candidates(spec, resolving, verified_on=verified_on)
        verified[name] = sorted(
            [*existing, *fallbacks], key=lambda c: c.priority
        )[:MAX_CANDIDATES_PER_FIELD]


_TRANSFORM_SYSTEM_PROMPT = """\
A scraper read a value off a web page and could not clean it into the type the \
caller asked for. You are given the raw text and the wanted type. Return the \
pipeline that turns one into the other.

The selector is not in question -- this text IS the right text. Your job is \
only the cleanup, and the commonest reasons it failed are worth knowing: a \
price with a range in it ("From $12.99 - $45.00"), a number with units \
("2.5 kg"), a date wrapped in prose ("Ships in 3-5 days"), a label repeated \
before the value ("Weight: 400g").

Available ops, applied in order:
- regex_extract {pattern, group} -- pull one part out. The commonest fix.
- regex_replace {pattern, repl} -- strip separators, symbols, units.
- trim, collapse_ws, strip_html, strip_accents
- split {sep} then index {i} -- take one side of a range or a list.
- cast {to} -- the last step, into the wanted type.
- default {value} -- only when a missing value has a sensible stand-in.

Prefer the shortest pipeline that works on the text you were shown, and prefer \
extracting what you want over stripping everything you do not.

If the text plainly does not contain the wanted value at all -- "Contact us \
for pricing" is not a price in any cleanup -- return an EMPTY list. Saying so \
is useful; a pipeline that cannot work is not.\
"""

_TRANSFORM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "transform": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                    "pattern": {"type": ["string", "null"]},
                    "group": {"type": ["integer", "null"]},
                    "repl": {"type": ["string", "null"]},
                    "sep": {"type": ["string", "null"]},
                    "i": {"type": ["integer", "null"]},
                    "to": {"type": ["string", "null"]},
                    "value": {"type": ["string", "number", "null"]},
                },
                "required": ["op"],
            },
        }
    },
    "required": ["transform"],
}


async def propose_transform(
    spec: FieldSpec, raw: Any, *, llm_config: LLMConfig
) -> list[Transform] | None:
    """A cleanup pipeline for a value that defeated the inferred one.

    Returns None when the model declines or answers unusably. The caller must
    treat that as "this text is not the value", not as an error: declining is a
    real answer here and often the right one.
    """

    from agentpilot.agent.reliability import RetryStrategy

    wanted = spec.type.value_type if spec.type.kind == "scalar" else spec.type.kind
    user = (
        f"Field: {spec.name}\n"
        f"Wanted type: {wanted}\n"
        f"{spec.description}\n\n"
        f"Raw text read from the page:\n{_show(raw)}"
    )
    try:
        answer = await RetryStrategy().execute(
            lambda: chat_json_conversation(
                [
                    {"role": "system", "content": _TRANSFORM_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                config=llm_config,
                json_schema=_TRANSFORM_SCHEMA,
            )
        )
    except Exception:  # noqa: BLE001 - a failed repair keeps the field unresolved
        return None

    raw_ops = answer.get("transform") or []
    if not raw_ops:
        return None
    try:
        return parse_transforms(raw_ops)
    except Exception:  # noqa: BLE001 - a malformed pipeline is a declined answer
        return None


async def _repair_transform(
    spec: FieldSpec,
    locators: list[Locator],
    *,
    verify: Verifier,
    ctx: TransformContext,
    llm_config: LLMConfig,
) -> FieldSpec | None:
    """Ask for a pipeline that fits the value actually on the page, and only
    keep it if it does.

    The same propose-then-verify discipline this module applies to locators,
    applied to the other half of a binding: the proposal is run against the very
    text that defeated the inferred pipeline, so an answer that does not work
    never reaches the recipe.
    """

    from agentpilot.recipe.v2.resolve import is_empty
    from agentpilot.recipe.v2.transform import TransformError, apply_transforms

    raw: Any = None
    for loc in locators:
        try:
            candidate_raw = await verify(loc)
        except Exception:  # noqa: BLE001 - already reported by verify_locators
            continue
        if not is_empty(candidate_raw):
            raw = candidate_raw
            break
    if raw is None:
        return None

    pipeline = await propose_transform(spec, raw, llm_config=llm_config)
    if not pipeline:
        return None
    try:
        value = apply_transforms(raw, pipeline, ctx)
    except TransformError:
        return None
    if is_empty(value):
        return None

    log.info("selector_agent.transform_repaired", field=spec.name,
             ops=[t.op for t in pipeline])
    # Written onto the FIELD, not a candidate: the text that defeated the
    # inferred pipeline will look the same whichever locator read it, and
    # `resolve_field` falls back to the field's pipeline for any candidate that
    # carries none of its own.
    return replace(spec, transform=pipeline)


async def propose_and_verify(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    llm_config: LLMConfig,
    verify: Verifier,
    max_retries: int = 1,
    verified_on: int = 1,
    dom_fallbacks: bool = True,
    failures: dict[str, str] | None = None,
    page_url: str = "",
) -> dict[str, list[Candidate]]:
    """Propose -> verify -> (on total failure) retry with the failure fed back,
    then top up any JSON-only field with a DOM fallback.

    Fields that could not be located are simply absent from the result. That is
    not an error: the caller keeps them and tries again on a later exploration
    step, once the page has been interacted with further.

    `dom_fallbacks` costs one extra call on pages that publish their data as
    JSON, and buys a chain that survives a renamed hydration key. See
    `needs_dom_fallback` for why that asymmetry is worth paying for.

    `failures` seeds the feedback the first attempt is given, for callers that
    already know something was wrong with the previous answer. A repair round
    passes the judge's rejection here -- "this is the breadcrumb trail, the
    product title is in the h1 below it" -- so the model is told what it got
    wrong in the same place it is normally told what did not resolve, and no
    second prompt has to exist.
    """

    verified: dict[str, list[Candidate]] = {}
    remaining = dict(fields)
    failures = dict(failures) if failures else None
    ctx = TransformContext(url=page_url)
    # One transform repair per field. If a pipeline proposed with the value in
    # hand still does not work, another guess will not help -- that goes to a
    # person, who can see the raw value and decide.
    retyped: set[str] = set()

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
            spec = remaining[name]
            deduped = dedupe_locators(locators)
            resolving, reason = await verify_locators(
                deduped, verify=verify, spec=spec, ctx=ctx
            )

            if not resolving and name not in retyped:
                # Every candidate read something and none survived its cleanup:
                # that is a pipeline problem wearing a selector problem's
                # clothes. Re-asking for a different locator -- which is what
                # this used to do -- returns another one onto the same kind of
                # text, and it fails the same way. Ask for the cleanup instead,
                # with the value that broke it in hand.
                retyped.add(name)
                repaired = await _repair_transform(
                    spec, deduped, verify=verify, ctx=ctx, llm_config=llm_config
                )
                if repaired is not None:
                    spec = repaired
                    remaining[name] = repaired
                    resolving, reason = await verify_locators(
                        deduped, verify=verify, spec=spec, ctx=ctx
                    )

            if not resolving:
                next_failures[name] = reason or "no candidate resolved"
                continue
            verified[name] = _to_candidates(spec, resolving, verified_on=verified_on)
            del remaining[name]
        failures = next_failures
        if not failures:
            break

    if dom_fallbacks and verified:
        await _add_dom_fallbacks(
            verified,
            fields,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            llm_config=llm_config,
            verify=verify,
            verified_on=verified_on,
            ctx=ctx,
        )

    return verified
