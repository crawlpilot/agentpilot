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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any

import structlog

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.build_trace import BuildTrace, record
from agentpilot.recipe.v2.json_index import (
    confident_value_arrays,
    find_value_arrays,
    outline,
)
from agentpilot.recipe.v2.locator_lint import (
    engine_only_selector_reason,
    partial_case_fold_reason,
    relativize_xpath,
    xpath_escape_reason,
)
from agentpilot.recipe.v2.models import Candidate, Locator
from agentpilot.recipe.v2.schema import FieldSpec, render_fields_for_prompt
from agentpilot.recipe.v2.selector_quality import filter_by_grade, selector_grade
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
#
# This budget buys an OUTLINE (`json_index.outline`), not a prefix of the raw
# dump. A prefix of 12 000 chars was 3.4% of that blob, cut mid-structure, and
# on the page this was measured against it did not reach `idml` at all -- so the
# model was asked to write an anchored path into data it had never seen. An
# outline of the same page fits every path that matters, which is why the budget
# is worth raising rather than the cap being the problem.
_MAX_STRUCTURED_CHARS = 16_000
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

XPATH MUST STAY INSIDE THE THING IT NAMES, for the same reason paths must be \
anchored. NEVER use `following::`, `preceding::`, `ancestor::` or \
`ancestor-or-self::`: those walk the whole document in order, so an expression \
aimed at one section collects the site header and the navigation menu along \
with it. `following-sibling::` and `preceding-sibling::` are fine -- they are \
bounded by the parent element.
When you match on text, fold the WHOLE alphabet: \
`translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')`. A \
short form like `translate(text(),'CARE','care')` lower-cases only those \
letters and then matches any word containing them. Prefer \
`normalize-space()` against the exact label when you know it.

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
A LIST field whose values are one key of an array of objects is a projection: \
`productHighlights[*].value` with `path_lang` "jmespath". Dotted traversal \
cannot say that, and reading the array itself would hand back the objects \
rather than the strings that were asked for.

For a css/xpath locator set `attribute` to what you want to read: "text" \
(default) reads textContent and DOES see collapsed/hidden content, so prefer it \
for accordion bodies that are present but not expanded; or name a real DOM \
attribute such as "href", "src", "content", "value".
Set `all` to true when the field is a list and the selector matches every item. \
A list field bound to a selector without `all` returns ONE value where an array \
was asked for, so point the selector at the items themselves rather than at the \
container that holds them.

ONE selector per candidate. `.a, .b` is CSS for "whichever of these exists",
and that is what the CANDIDATE LIST is for -- it is tried in order, each entry \
is verified on its own, and the recipe records which one won. A comma group is \
unordered, verified as a whole, and hides which branch matched; worse, a branch \
that is present before a panel is opened masks that another needs a click. Put \
your alternatives in the list, not in the selector. (A comma inside `:is(...)` \
or an attribute value is fine, and a LIST field with `all` may legitimately \
union two selectors.)

CSS here is what `document.querySelector` understands and NOTHING else. \
`:has-text()`, `:text()`, `:visible`, `:nth-match()` and `>>` are Playwright \
extensions -- they look like CSS, most examples you have seen use them, and \
every one of them throws here. `:has()`, `:not()` and attribute selectors are \
real CSS and work. To match on TEXT, use an xpath, which is what it is for.

Prefer a selector that names what the element IS over one that describes where \
it sits. A long chain of class names, or a positional path like \
`//*[@id="main"]/div[1]/div[1]/div[2]`, reads the right value today and then \
silently reads a different element the moment anything is inserted above it -- \
it does not fail, it returns the wrong thing.

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


def _value_array_hints(
    fields: dict[str, FieldSpec], structured_data: dict[str, Any]
) -> str:
    """Arrays in the page's JSON that may be a `list` field's values.

    The counterpart of the `find_row_arrays` block in `rows.build_user_message`,
    and it exists for the same reason: the projection a list field needs --
    `productHighlights[*].value` -- is not something a model writes for an array
    it is not certain is there. The outline above shows the array; this says
    which field it might answer, and in the exact form a locator takes.

    Everything is shown, including the low-affinity guesses that
    `confident_value_arrays` refuses to bind on its own. Here the model has the
    page snapshot beside it and can tell whether the site's word for the array
    means what the caller's word means.
    """

    lines: list[str] = []
    for name, spec in fields.items():
        if spec.type.kind != "list":
            continue
        for candidate in find_value_arrays(structured_data, spec, name)[:3]:
            lines.append(
                f"- {name}: kind={candidate.kind} path={candidate.path!r} "
                f"path_lang={candidate.path_lang!r} "
                f"first values: {_json.dumps(candidate.sample, ensure_ascii=False)[:200]}"
            )
    return "\n".join(lines)


# Words too common to point at anything. A field described with only these has
# no usable signal and falls back to the head of the page.
_SNAPSHOT_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "all", "any",
    "this", "that", "its", "it", "page", "product", "value", "text", "from",
    "when", "available", "displayed", "current", "full", "complete", "list",
    "url", "name", "information", "details", "item",
})

# How much of the head to keep regardless. The title, the price and the
# breadcrumb live there, and so does the page's identity.
_SNAPSHOT_HEAD_CHARS = 6_000
# Lines of context kept either side of a line that mentions a wanted field.
_FOCUS_CONTEXT_LINES = 12


def _field_words(fields: dict[str, FieldSpec]) -> set[str]:
    words: set[str] = set()
    for name, spec in fields.items():
        for w in re.split(r"[^a-z0-9]+", f"{name} {spec.description}".lower()):
            if len(w) > 3 and w not in _SNAPSHOT_STOPWORDS:
                words.add(w)
    return words


def focus_snapshot(snapshot_text: str, fields: dict[str, FieldSpec]) -> str:
    """The snapshot, trimmed to the budget around what is being looked for.

    A head-first prefix spends the budget in document order, and on a commerce
    page document order is the header. Measured on an Ulta product page: the
    render is 72 000 characters, the cap is 24 000, so the model was shown the
    first third -- "SKIP TO MAIN", "Join / Sign in", "Track an Order" -- and
    none of the accordion content it was being asked to locate. It declined
    every field, correctly, because from where it stood the page did not have
    them.

    So the budget follows the FIELDS instead: the head is kept for the page's
    identity, and the rest is spent on windows around lines that mention what
    was asked for. Elisions are marked so the model knows it is seeing an
    excerpt rather than the end of the page.
    """

    if len(snapshot_text) <= _MAX_SNAPSHOT_CHARS:
        return snapshot_text

    words = _field_words(fields)
    lines = snapshot_text.splitlines()
    if not words:
        return snapshot_text[:_MAX_SNAPSHOT_CHARS]

    keep: set[int] = set()
    spent = 0
    head_lines = 0
    for i, line in enumerate(lines):
        spent += len(line) + 1
        if spent > _SNAPSHOT_HEAD_CHARS:
            break
        keep.add(i)
        head_lines = i

    budget = _MAX_SNAPSHOT_CHARS - spent
    for i, line in enumerate(lines):
        if i <= head_lines or budget <= 0:
            continue
        low = line.lower()
        if not any(w in low for w in words):
            continue
        for j in range(max(0, i - _FOCUS_CONTEXT_LINES), min(len(lines), i + _FOCUS_CONTEXT_LINES + 1)):
            if j not in keep:
                cost = len(lines[j]) + 1
                if budget - cost < 0:
                    break
                keep.add(j)
                budget -= cost

    # Nothing matched: the field's vocabulary and the content's need not
    # overlap at all -- "Apply an adequate amount" shares no word with
    # "Directions or instructions for using". Falling back to the plain prefix
    # keeps this strictly no worse than not having it, which is the only honest
    # thing for a heuristic that can miss.
    if not any(i > head_lines for i in keep):
        return snapshot_text[:_MAX_SNAPSHOT_CHARS]

    out: list[str] = []
    previous = -1
    for i in sorted(keep):
        if previous >= 0 and i > previous + 1:
            out.append(f"… [{i - previous - 1} lines not shown] …")
        out.append(lines[i])
        previous = i
    if previous < len(lines) - 1:
        out.append(f"… [{len(lines) - 1 - previous} lines not shown] …")
    return "\n".join(out)


def build_user_message(
    fields: dict[str, FieldSpec],
    *,
    snapshot_text: str,
    structured_data: dict[str, Any],
    failures: dict[str, str] | None = None,
) -> str:
    parts = [
        f"Fields to locate:\n{render_fields_for_prompt(fields)}",
        f"\nPage snapshot:\n{focus_snapshot(snapshot_text, fields)}",
        "\nPaths available in this page's structured data (json_ld / hydration / "
        "metadata). Each path is written the way a locator takes it, and is "
        "anchored -- use one of these rather than composing your own:\n"
        + outline(structured_data, wanted=fields, max_chars=_MAX_STRUCTURED_CHARS),
    ]
    hints = _value_array_hints(fields, structured_data)
    if hints:
        parts.append(
            "\nArrays already in this page's JSON that may be these list fields. "
            "If one is right, use its path and path_lang verbatim -- it costs no "
            "clicks and survives a redesign. Where two readings of the SAME array "
            "are offered, they are the two halves of one record: pick whichever "
            "the field's description actually asks for, judging by the sample "
            f"values:\n{hints}"
        )
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
    if kind == "xpath":
        # Dropped here as well as at verification, so a candidate that cannot be
        # contained never reaches the page at all. `paths.py` refuses recursive
        # descent for the JSON dialects for the same reason and with the same
        # measured consequence -- an expression that wanders picks up a
        # competitor's data, or a site header, and reports it as the answer.
        selector = str(item.get("selector") or "")
        if xpath_escape_reason(selector, scoped=bool(item.get("within"))) is not None:
            log.info("selector_agent.xpath_escapes_scope", selector=selector)
            return None
        fold = partial_case_fold_reason(selector)
        if fold is not None:
            log.info("selector_agent.partial_case_fold", selector=selector, note=fold)
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
    trace: BuildTrace | None = None,
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
    if trace is not None:
        trace.exchanged("propose", sorted(fields), user, raw)
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


# Markup that is never the value and is routinely most of the bytes.
_DEAD_MARKUP = re.compile(
    r"<!--.*?-->|<(script|style|svg|noscript|template|iframe|canvas)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Attributes worth keeping: the ones a selector can be built out of, plus the
# few that carry a value. Everything else on a React page is generated.
_KEEP_ATTR = re.compile(
    r"\s(?:id|class|role|itemprop|itemtype|href|src|alt|title|value|content|type|name"
    r"|colspan|rowspan|(?:data|aria)-[\w:.-]+)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_OPEN_TAG = re.compile(r"<([a-zA-Z][\w:-]*)((?:\s[^<>]*)?)/?>")
_WHITESPACE = re.compile(r"[ \t\r\n]+")


def prune_fragment(html: str, *, limit: int = _MAX_FRAGMENT_CHARS) -> str:
    """A picked region's markup with the parts that are never the answer removed.

    A real specifications section on a React page is mostly inline styles and
    generated class names, so a raw 20 000-character slice of `outerHTML`
    routinely cuts off mid-table -- the person pointed at the right region and
    the model was shown the first third of it.

    Best-effort and regex-based on purpose. This shapes a prompt; it is not a
    correctness gate, and every proposal made from it is still verified against
    the live page by `verify_locators`. The picker prunes in the page before
    sending, where a real DOM is available -- this is the guard for markup that
    arrives from anywhere else, and for a client that has not been updated.
    """

    if not html:
        return ""
    text = _DEAD_MARKUP.sub("", html)

    def strip_attrs(match: re.Match[str]) -> str:
        tag, attrs = match.group(1), match.group(2)
        if not attrs.strip():
            return match.group(0)
        kept = "".join(m.group(0) for m in _KEEP_ATTR.finditer(attrs))
        return f"<{tag}{kept}>"

    text = _OPEN_TAG.sub(strip_attrs, text)
    return _WHITESPACE.sub(" ", text).strip()[:limit]


async def propose_within(
    fields: dict[str, FieldSpec],
    *,
    scope: Locator,
    fragment_html: str,
    llm_config: LLMConfig,
    verify: Verifier,
    verified_on: int = 1,
    page_url: str = "",
    trace: BuildTrace | None = None,
    probe: ScopeProbe | None = None,
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
        f"Region HTML:\n{prune_fragment(fragment_html)}"
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
    if trace is not None:
        trace.exchanged("within", sorted(fields), user, raw)

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
            scoped, verify=verify, spec=spec, ctx=ctx, trace=trace,
            stage="within", probe=probe,
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

    # Brittle candidates lose to good ones and beat nothing at all.
    #
    # A Zara build verified four selectors for one field, two of which were a
    # six-level class chain and `//*[@id="main"]/div[1]/div[1]/...`. Both read
    # the right paragraph; neither survives an element being inserted above it,
    # and the positional one does not even fail when that happens -- it resolves
    # to whatever now sits at that position and reports it as data. They were
    # frozen anyway, because verification only ever asked whether a selector
    # works *now*.
    #
    # Comparative, not absolute: a field whose only working selector is brittle
    # keeps it, since a field with no binding collects nothing on every run.
    # `filter_by_grade` holds that rule, matching `filterPersistableChain` on
    # the picker side.
    # Graded among the DOM candidates only. A `json_ld` path and a css selector
    # are not competing on brittleness -- they are each other's insurance, and
    # `needs_dom_fallback` spends a whole extra model call to make sure a
    # JSON-only field gets a DOM candidate precisely because a renamed hydration
    # key fails silently and totally. Dropping that candidate for being
    # inelegant would undo the thing the call was made for.
    dom = [v for v in verified if v.locator.kind in _DOM_KINDS]
    kept = set(
        map(
            id,
            filter_by_grade([
                (selector_grade(v.locator.kind, v.locator.selector), v.locator)
                for v in dom
            ]),
        )
    )
    verified = [
        v for v in verified if v.locator.kind not in _DOM_KINDS or id(v.locator) in kept
    ]

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


def tautological_read(loc: Locator, raw: Any, field_name: str = "") -> str | None:
    """Why this locator's value is its own search term, or None if it is not.

    A locator that finds an element BY its text and then reads that same text
    back has learned nothing from the page: the value was in the selector
    before the page was ever consulted. It is always the label of the thing
    matched -- a section heading, an accordion trigger, a tab -- never the
    content behind it.

    This is how a real Zara build lost `care`. The agent scrolled the
    "Composition, care & origin" accordion into view; the model proposed
    `ax_role button name_contains="Composition, care & origin"` for `care`;
    that read the string "COMPOSITION, CARE & ORIGIN", which is a non-empty
    string that casts cleanly to the declared type. Nothing objected, so the
    field was frozen -- *twelve seconds before the click that opens the panel*.
    Freezing removes a field from `unfound`, so when the real care text
    appeared a step later nothing was looking for it any more, and the recipe
    shipped with `care` reading the button's label for ever.

    A strict superset is fine and must stay fine: matching on "Made in" and
    reading "Made in China" is a real reading of the page. Only an exact
    round-trip is refused.

    **The two checks below answer different questions, and only the first one
    needs a needle.** That distinction was wrong here for a while, and it let
    the very bug this function is named after come back through another door.
    Both checks sat behind `if not needle: return None`, so a locator that
    found the caption by CSS rather than by its text was never examined at all
    -- and `focus_snapshot` made exactly that the likely proposal. Once the
    snapshot budget followed the field's own vocabulary, the model was reliably
    shown the region around the word "care" on a collapsed Zara page, where the
    nearest element is the CleverCare badge; it stopped needing
    `name_contains="Composition, care & origin"` to reach the caption and
    started proposing `.product-detail-actions__clevercare` instead. Same
    caption, same freeze, same field lost -- through a selector this refused to
    look at.

    So: reading back your own search term is only possible when you searched
    for a term, but *being a caption* is a property of the value, not of how
    you got to it. The second check runs for every locator kind.
    """

    if not isinstance(raw, str):
        return None
    value = " ".join(raw.split())
    if not value:
        return None

    needle = " ".join((loc.name_contains or loc.text or "").split())
    matcher = "name_contains" if loc.name_contains else "text"

    if needle and value.casefold() == needle.casefold():
        return (
            f"this reads back exactly the {matcher} it searched for ({needle!r}), so "
            f"it is the label of the element you matched rather than anything on the "
            f"page -- for a section heading or an accordion trigger, the value lives "
            f"in the content it reveals, not in the control itself"
        )

    # The same mistake, one step cleverer. Told that an exact round-trip is
    # refused, the model shortens the needle until it is not one: it asked for
    # `name_contains="Composition, care"` and for `name_contains="origin"`, both
    # of which match the same accordion button and both of which read back
    # "COMPOSITION, CARE & ORIGIN". Neither is an exact echo, and both are the
    # button's label.
    #
    # What gives it away is that the value NAMES the field instead of answering
    # it. A control captioned "care" is the way to the care instructions, never
    # the instructions themselves -- that is what a caption is. Length is the
    # guard that keeps this from touching real prose: a description that happens
    # to contain the word "description" is a paragraph, not a caption.
    if (
        len(value) <= _MAX_CAPTION_CHARS
        and _names_the_field(field_name, value)
        and _bare_label(value)
    ):
        return (
            f"this reads {value!r}, which is a caption NAMING {field_name!r} rather "
            f"than a value for it -- you matched the control or heading that leads "
            f"to the content, not the content. Open it and read what it reveals, or "
            f"point at the text inside the revealed panel"
        )
    return None


# A caption is short. Past this a string is prose, and prose that mentions the
# field's own name is a paragraph about it rather than a label for it.
_MAX_CAPTION_CHARS = 80

# And a caption is a handful of words. Past this it is a sentence.
_MAX_CAPTION_WORDS = 4


def _bare_label(value: str) -> bool:
    """Whether this value is a label and nothing else.

    Length alone was not enough, and the gap was expensive. Panel content
    routinely repeats its own heading -- "Ingredients: Water, Glycerin",
    "Composition: 100% cotton", "Care instructions: machine wash at 30" -- so a
    rule of "short, and mentions the field's name" threw away real values for
    exactly the fields that live behind a click, which are the ones most likely
    to be captioned with their own name. `how_to_use` contributes the word
    "use", which put every short instruction containing it out of reach.

    What separates the two is whether anything follows the label. A colon
    introduces a value; a digit is a value. Either means the string is
    label-plus-content, which is a reading of the page, not a caption for it.
    """

    if ":" in value or any(character.isdigit() for character in value):
        return False
    return len(re.findall(r"[A-Za-z0-9]+", value)) <= _MAX_CAPTION_WORDS


def _names_the_field(field_name: str, value: str) -> bool:
    """Whether `value` reads as a caption for a field called `field_name`.

    Word-boundary matching on purpose: `origin` must match "Composition, care &
    origin" but not "original price", and `care` must match "CARE" but not
    "careful".
    """

    words = {w for w in re.split(r"[^a-z0-9]+", field_name.casefold()) if len(w) > 2}
    if not words:
        return False
    present = set(re.split(r"[^a-z0-9]+", value.casefold()))
    return bool(words & present)


# Ops that take a list and hand back one thing. A pipeline containing one of
# these is a field that MEANS to read several elements and combine them.
_COLLAPSING_OPS = frozenset({"join", "index", "to_object", "to_pairs"})


def _collapses_list(spec: FieldSpec) -> bool:
    """Whether this field's cleanup turns a list into a single value.

    The arity guard runs before the pipeline does, so without this it rejects
    the very reads the pipeline exists to handle. `_repair_transform` had
    already answered correctly -- `filter_empty -> join -> collapse_ws -> trim
    -> cast` for a care panel whose lines live in separate elements -- and the
    guard threw it away three seconds later with "reads 11 values but this
    field is one value", which is exactly the arrangement `join` was proposed
    for.

    `filter_empty` and `unique` are deliberately NOT here: both take a list and
    return a list, so a field carrying only those is still handing back several
    values and the guard should still fire.
    """

    return any(t.op in _COLLAPSING_OPS for t in spec.transform)


# Phrases from the guards in this module. A rejection carrying one of these
# told the model something it can act on -- "right idea, wrong form" -- as
# opposed to "that resolved to nothing", which is a dead end.
#
# Coupled to the message text on purpose, and pinned by a test that feeds every
# guard through `is_correctable`: the alternative is threading a flag out of
# `verify_locators`, whose signature is shared with the assist path.
_CORRECTABLE_MARKERS = (
    "selectors joined by a comma",
    "reads back exactly the",
    "caption NAMING",
    "Playwright selector syntax",
    "very same locator as",
    "but this field is one value",
    "but this field is a list",
    "spread across the whole page",
)

# How many extra rounds a correctable answer may buy. Bounded: a model that
# keeps making the same fixable mistake has to stop somewhere, and each round
# is a model call plus a verification pass.
_MAX_CORRECTABLE_RETRIES = 2


def is_correctable(reason: str) -> bool:
    """Whether this rejection handed the model something to act on.

    A guard rejection and a dead end used to cost the same single retry, and
    they are not the same thing. Measured on an Ulta product page: attempt one
    went on "this is 3 selectors joined by a comma", attempt two came back
    "model did not propose a locator for this field" -- it had been told its
    form was wrong, had no budget left to try the right one, and declined. Four
    fields failed that way in one batch, every one of them present on the page
    with its panel already open.
    """

    return any(marker in reason for marker in _CORRECTABLE_MARKERS)


def comma_group_reason(loc: Locator, spec: FieldSpec | None) -> str | None:
    """Why a single-value CSS selector must not be an alternation, or None.

    `a, b` means "whichever of these happens to exist", and for one value that
    is a worse version of the candidate chain: the chain is ORDERED, each entry
    is verified on its own, and the recipe records which one won. A comma group
    is unordered, verifies as a whole, and hides which branch matched.

    It also conceals a dependency, which is how it did real damage. A build
    proposed `.product-detail-extra-detail__content, .product-detail-composition`
    for a field whose content sits behind an accordion. The second branch is
    rendered inline, so the selector verified on the unopened page -- reading
    the wrong section -- and the field was frozen as satisfied without the
    click that reveals the right one. The resulting recipe had no reveal step
    at all, and the binding it did have was pointing somewhere else.

    Only for a single value. A list with `all` is a genuine union: it wants
    every match from both branches, and the ordering that matters for a chain
    is meaningless there.
    """

    if loc.kind != "css" or loc.all:
        return None
    if spec is not None and (spec.type.kind == "list" or spec.type.is_rows):
        return None
    selector = loc.selector or ""
    if "," not in _strip_bracketed(selector):
        return None
    branches = [part.strip() for part in selector.split(",") if part.strip()]
    return (
        f"this is {len(branches)} selectors joined by a comma, which reads "
        f"whichever happens to exist rather than naming one thing -- and a "
        f"branch that is present on the unopened page hides that another needs "
        f"a click. Give the one selector you mean; put the alternatives in the "
        f"candidate list, which is tried in order and records which one won"
    )


def _strip_bracketed(selector: str) -> str:
    """The selector with `[...]` and `(...)` contents removed, so a comma inside
    `:is(a, b)` or `[x='1,2']` is not mistaken for an alternation."""

    out: list[str] = []
    depth = 0
    for char in selector:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out)


def locator_key(loc: Locator) -> tuple[Any, ...]:
    """Everything that decides what a locator reads.

    Two locators with the same key are the same read, so they cannot be two
    different values -- which is what `dedupe_locators` uses it for within one
    field's chain, and `shared_locator_failures` across fields.
    """

    return (
        loc.kind, loc.selector, loc.path, loc.path_lang,
        loc.attribute, loc.all, loc.index, loc.role, loc.name_contains,
    )


def dedupe_locators(locators: list[Locator]) -> list[Locator]:
    seen: set[tuple[Any, ...]] = set()
    out: list[Locator] = []
    for loc in locators:
        key = locator_key(loc)
        if key not in seen:
            seen.add(key)
            out.append(loc)
    return out


def shared_locator_failures(
    verified: dict[str, list[Candidate]], *, trace: BuildTrace | None = None
) -> dict[str, str]:
    """Fields whose winning locator is another field's winning locator.

    `rows.py::_problems_with` has had this check for table columns since a Zara
    build bound `material` and `percentage` to the same json_ld path and the
    row looked perfectly fine. Scalars had no equivalent, and the same failure
    arrived the same way: on a Zara product page the agent clicked the
    "Composition, care & origin" accordion open, and the model then bound BOTH
    `care` and `origin` to the accordion's own button --

        {"kind": "ax_role", "role": "button",
         "name_contains": "Composition, care & origin"}

    -- so both fields collected the string "COMPOSITION, CARE & ORIGIN". Every
    per-field check passed: the locator resolved, it read a non-empty string,
    and the string cast cleanly to the declared type. Nothing that looks at one
    field at a time can see the problem, because the problem is the *relationship*
    between two of them.

    Only the winning locator is compared. A chain's later candidates are
    fallbacks that did not produce this value, and two fields are welcome to
    share a fallback.

    Both fields are returned, not one. When a model binds two declared fields to
    a single element it has not decided between them -- it found one salient
    thing and used it twice -- so there is no basis for calling either the right
    one. Re-asking with the collision named lets it differentiate; failing that
    they become asks, and an ask beats a confidently wrong value.
    """

    winners: dict[tuple[Any, ...], list[str]] = {}
    for name, candidates in verified.items():
        if not candidates:
            continue
        winners.setdefault(locator_key(candidates[0].locator), []).append(name)

    out: dict[str, str] = {}
    for names in winners.values():
        if len(names) < 2:
            continue
        for name in names:
            others = ", ".join(repr(n) for n in names if n != name)
            reason = (
                f"this read through the very same locator as {others}, so the two "
                f"are one value reported twice rather than two fields -- most "
                f"often the heading or control that labels a section, rather than "
                f"the content inside it. Find the element that holds {name!r} "
                f"specifically."
            )
            out[name] = reason
            record(
                trace, name, "propose", "rejected",
                locator=verified[name][0].locator, reason=reason,
            )
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


# A read that also says where the matches live. Injected like `Verifier`, and
# for the same reason: the scoping rules below decide what a build accepts, and
# deciding that should be testable without a browser.
ScopeProbe = Callable[[Locator], Awaitable[tuple[Any, dict[str, Any] | None]]]


def scope_problem(scope: dict[str, Any] | None, *, expects_many: bool) -> str | None:
    """Why a locator's matches are not all one thing, or None.

    The list-shaped counterpart of `rows._problems_with`, and the same kind of
    check: a question about *shape*, which a plausible-but-wrong answer cannot
    fake. A table is caught by rows that do not vary; a list is caught by
    matches that do not share a container.

    Measured on the build this exists for -- Zara's `care` bound to an xpath
    using the `following::` axis. Its ten matches were "Bag0", "LOG IN", "Help",
    two nav blocks and the five real washing instructions. Every one of them is
    a legitimate match for the expression. The only thing separating the answer
    from the noise is that the noise lives in the page header, so the nearest
    ancestor of the whole set is `<body>`.

    Only for a field that expects many values. A scalar matching one element has
    a common ancestor of *itself*, which says nothing at all.
    """

    if scope is None or not expects_many:
        return None
    if not scope.get("spans_document"):
        return None
    matched = scope.get("matched") or 0
    return (
        f"the {matched} matches are spread across the whole page -- their nearest "
        f"common ancestor is <{scope.get('tag') or 'body'}>, so this is picking up "
        "site chrome and navigation alongside the values. Scope it to the section "
        "the values actually live in"
    )


def scoped_variant(locator: Locator, scope: dict[str, Any] | None) -> Locator | None:
    """The same locator confined to where its matches live, or None.

    None when there is nothing to gain or the move would be unsafe:

    - the locator is already scoped -- the author said where to look;
    - the scope spans the document -- `scope_problem` has already refused it;
    - the page offered no stable selector for the container;
    - **a single match**, where the "common ancestor" is the element itself.
      Scoping an element to itself makes the read resolve `containers[0]` and
      then look for the element *inside* it, which finds nothing. The guard is
      not theoretical: every scalar field takes this path.
    """

    if locator.within is not None or scope is None:
        return None
    if scope.get("spans_document") or (scope.get("matched") or 0) < 2:
        return None
    selector = scope.get("selector")
    if not selector:
        return None

    inner = locator.selector or ""
    if locator.kind == "xpath":
        # An absolute expression ignores the context node, so scoping it would
        # record a `within` that changes nothing -- the sharp edge the module
        # docstring of `evaluate.py` warns about.
        relative = relativize_xpath(inner)
        if relative is None and inner.startswith("/"):
            return None
        inner = relative or inner
    return replace(
        locator, selector=inner, within=Locator(kind="css", selector=str(selector))
    )


async def verify_locators(
    locators: list[Locator],
    *,
    verify: Verifier,
    spec: FieldSpec | None = None,
    ctx: TransformContext | None = None,
    trace: BuildTrace | None = None,
    stage: str = "propose",
    probe: ScopeProbe | None = None,
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

    `trace`, when given, records every attempt and the reason it was turned
    down. The reasons below are written to be read by a person and were going to
    a worker's stdout; this is where they become part of the run's record. See
    `build_trace`.

    `probe`, when given, also asks the page where each DOM locator's matches
    live. That answers two questions at once -- whether they belong to one thing
    at all (`scope_problem`) and, if so, which container to confine the locator
    to (`scoped_variant`). Without it the old behaviour is kept exactly, which
    is what lets every browser-free test here stay as it was.
    """

    from agentpilot.recipe.v2.resolve import evaluate_assertions, is_empty
    from agentpilot.recipe.v2.transform import TransformError, apply_transforms

    context = ctx or TransformContext()
    resolving: list[VerifiedLocator] = []
    last_error: str | None = None
    name = spec.name if spec is not None else ""
    # A field that expects many values is the one a document-wide match can
    # masquerade as. A scalar reading one element says nothing about scope, and
    # the arity guard below is what catches a scalar matching a whole set.
    expects_many = bool(
        spec is not None and (spec.type.kind == "list" or spec.type.is_rows)
    )

    def rejected(loc: Locator, reason: str, raw: Any = None, pipeline: Any = None) -> str:
        record(
            trace, name, stage, "rejected",
            locator=loc, read=raw, transform=pipeline, reason=reason,
        )
        return reason

    async def read(loc: Locator) -> tuple[Any, dict[str, Any] | None]:
        if probe is not None and loc.kind in ("css", "xpath"):
            return await probe(loc)
        return await verify(loc), None

    for loc in locators:
        # Refused before it is even run. An expression on one of these axes
        # cannot be contained by any scope, so there is no version of it worth
        # verifying -- see `locator_lint`.
        if loc.kind == "css":
            # Refused before it is run, like the xpath axis check below: the
            # DOM's own error for this says the string was malformed, not that
            # the dialect was wrong, and the model answers it with another
            # variant of the same thing.
            dialect = engine_only_selector_reason(loc.selector or "")
            if dialect is not None:
                last_error = rejected(loc, dialect)
                continue

        if loc.kind == "xpath":
            escape = xpath_escape_reason(
                loc.selector or "", scoped=loc.within is not None
            )
            if escape is not None:
                last_error = rejected(loc, escape)
                continue

        try:
            raw, scope = await read(loc)
        except Exception as exc:  # noqa: BLE001 - a bad selector is data, not a crash
            last_error = rejected(loc, f"{loc.kind} locator raised: {exc}")
            continue

        problem = scope_problem(scope, expects_many=expects_many)
        if problem is not None:
            last_error = rejected(loc, problem, raw=raw)
            continue

        circular = tautological_read(loc, raw, name)
        if circular is not None:
            last_error = rejected(loc, circular, raw=raw)
            continue

        alternation = comma_group_reason(loc, spec)
        if alternation is not None:
            last_error = rejected(loc, alternation, raw=raw)
            continue

        # Confine it to the container its own matches share. Re-read through the
        # scoped form and keep it only if it still produces the same value: the
        # scope selector resolves to `containers[0]`, and on a page carrying two
        # same-shaped sections that need not be the one measured.
        narrowed = scoped_variant(loc, scope)
        if narrowed is not None:
            try:
                rescoped, _ = await read(narrowed)
            except Exception:  # noqa: BLE001 - fall back to the unscoped locator
                rescoped = None
            if rescoped == raw:
                loc = narrowed
            else:
                log.info(
                    "selector_agent.scope_rejected",
                    field=name, selector=loc.selector,
                    within=narrowed.within.selector if narrowed.within else None,
                )

        if is_empty(raw):
            # Not `last_error`: an empty read is the ordinary "not on this page"
            # and says nothing a model could act on. It is still traced, because
            # "every candidate read nothing" and "one read the wrong thing" are
            # different diagnoses and only the trace can tell them apart.
            record(trace, name, stage, "rejected", locator=loc, reason="read nothing")
            continue

        if spec is None:
            resolving.append(VerifiedLocator(locator=loc, raw=raw, value=raw))
            continue

        if spec.type.kind == "scalar" and isinstance(raw, list) and not _collapses_list(spec):
            # One value was asked for and many came back, which is what an
            # `all: true` locator on a scalar field means. It used to be
            # accepted: the read is non-empty, and the scalar cleanup maps over
            # a list element-wise, so nothing objected until the data was used.
            #
            # This is how a table's columns became parallel arrays -- see
            # `rows.py`. The guard is here rather than only there because the
            # same function verifies what a *person* picks in the assist panel,
            # where pointing at a container is just as easy to do by accident.
            last_error = rejected(
                loc,
                f"reads {len(raw)} values but this field is one value -- the "
                "selector is matching a whole set rather than a single element. "
                "If these are genuinely the parts of ONE value (the lines of a "
                "care panel, a multi-line address), point at the element that "
                "contains them all, or keep this selector and add a `join` "
                "cleanup; if they are separate values, the field was declared "
                "wrong",
                raw=raw,
            )
            continue

        if spec.type.kind == "list" and not isinstance(raw, list):
            # The mirror of the guard above, and it was missing. A Zara build
            # bound `highlights` -- declared a list -- to a selector with no
            # `all`, so the field came back as one string and the recipe
            # promised an array it never produced. The guard is worth as much in
            # this direction: both are the model answering a different question
            # from the one the schema asked.
            last_error = rejected(
                loc,
                f"reads one value ({_show(raw)}) but this field is a list -- set "
                '`all` to true so the selector returns every match, or point it '
                "at the elements rather than their container",
                raw=raw,
            )
            continue

        pipeline = pipeline_for(spec, loc)
        try:
            value = apply_transforms(raw, pipeline, context) if pipeline else raw
        except TransformError as exc:
            last_error = rejected(
                loc, f"read {_show(raw)} but the cleanup failed: {exc}",
                raw=raw, pipeline=pipeline,
            )
            continue
        if is_empty(value):
            last_error = rejected(
                loc, f"read {_show(raw)} but cleaning it up left nothing",
                raw=raw, pipeline=pipeline,
            )
            continue

        # Checked, not enforced. See `VerifiedLocator.notes`.
        notes = [
            f"{r.kind}: {r.detail}" if r.detail else r.kind
            for r in evaluate_assertions(value, spec.assertions)
            if not r.passed
        ]
        record(
            trace, name, stage, "bound",
            locator=loc, read=value, transform=pipeline,
            reason="; ".join(notes) or None,
        )
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
    trace: BuildTrace | None = None,
    probe: ScopeProbe | None = None,
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
        trace=trace,
    )
    for name, locators in proposals.items():
        dom_only = [loc for loc in locators if loc.kind in _DOM_KINDS]
        if not dom_only:
            continue
        spec = wanted[name]
        resolving, _reason = await verify_locators(
            dedupe_locators(dom_only), verify=verify, spec=spec, ctx=ctx,
            trace=trace, stage="dom_fallback", probe=probe,
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
- filter_empty then join {sep} -- when the read is a LIST and the caller asked \
for one string, because the value arrives in parts: the lines of a care panel, \
an address over several elements. Only when the parts are one value between \
them. Several separate values joined into a string is a field that was \
declared wrong, and a pipeline cannot fix that -- return an empty list and say \
so by returning nothing.
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


async def bind_value_arrays(
    fields: dict[str, FieldSpec],
    *,
    structured_data: dict[str, Any],
    verify: Verifier,
    ctx: TransformContext,
    verified_on: int = 1,
    trace: BuildTrace | None = None,
) -> dict[str, list[Candidate]]:
    """Bind `list` fields straight from the page's own JSON, with no model call.

    The same move `propose_rows` already makes for tables: an array the site
    itself names the way the caller named the field is not a guess, and it is
    put through `verify_locators` like everything else, so a coincidental match
    still cannot get through.

    Only `confident_value_arrays` is offered here -- candidates the site's own
    naming agrees with. Verification alone is not a sufficient gate for a list
    field, because "reads a non-empty array" is true of a great many arrays on a
    page like this one; affinity is what stops a well-shaped array under an
    unrelated key from binding silently. The rest reach the model as hints,
    where the snapshot is available to judge them against.
    """

    bound: dict[str, list[Candidate]] = {}
    for name, spec in fields.items():
        if spec.type.kind != "list":
            continue
        candidates = confident_value_arrays(structured_data, spec, name)
        if not candidates:
            continue
        locators = [
            Locator(**c.as_locator_args())  # type: ignore[arg-type]
            for c in candidates
        ]
        resolving, _reason = await verify_locators(
            locators, verify=verify, spec=spec, ctx=ctx,
            trace=trace, stage="page_json",
        )
        if not resolving:
            continue
        bound[name] = _to_candidates(spec, resolving, verified_on=verified_on)
        log.info(
            "selector_agent.bound_from_page_json",
            field=name,
            kind=resolving[0].locator.kind,
            path=resolving[0].locator.path,
        )
    return bound


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
    trace: BuildTrace | None = None,
    probe: ScopeProbe | None = None,
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

    # The page's own JSON first, and without a model call -- the same order
    # `propose_rows` uses for tables. A field bound here is dropped from the
    # prompt entirely, which also leaves more of the budget for the ones that
    # actually need looking at.
    for name, candidates in (
        await bind_value_arrays(
            remaining,
            structured_data=structured_data,
            verify=verify,
            ctx=ctx,
            verified_on=verified_on,
            trace=trace,
        )
    ).items():
        verified[name] = candidates
        del remaining[name]

    # One transform repair per field. If a pipeline proposed with the value in
    # hand still does not work, another guess will not help -- that goes to a
    # person, who can see the raw value and decide.
    retyped: set[str] = set()
    # The spec each field was actually bound under, which is not always the one
    # it came in with: `_repair_transform` rewrites it. Needed so a field put
    # back for a retry is retried as itself.
    bound_under: dict[str, FieldSpec] = {}

    _attempt = -1
    budget = max_retries
    corrections = 0
    while True:
        _attempt += 1
        if _attempt > budget or not remaining:
            break
        proposals = await propose_locators(
            remaining,
            snapshot_text=snapshot_text,
            structured_data=structured_data,
            llm_config=llm_config,
            failures=failures,
            trace=trace,
        )
        next_failures: dict[str, str] = {}
        for name in list(remaining):
            locators = proposals.get(name)
            if not locators:
                next_failures[name] = "model did not propose a locator for this field"
                record(
                    trace, name, "propose", "rejected",
                    reason="the model proposed no locator for this field",
                )
                continue
            spec = remaining[name]
            deduped = dedupe_locators(locators)
            resolving, reason = await verify_locators(
                deduped, verify=verify, spec=spec, ctx=ctx, trace=trace, probe=probe
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
                        deduped, verify=verify, spec=spec, ctx=ctx,
                        trace=trace, stage="transform_repair", probe=probe,
                    )

            if not resolving:
                next_failures[name] = reason or "no candidate resolved"
                continue
            verified[name] = _to_candidates(spec, resolving, verified_on=verified_on)
            bound_under[name] = spec
            del remaining[name]

        # Two fields reading through ONE locator are one value reported twice.
        # Checked here rather than per field, because that is the only place the
        # whole batch is visible -- see `shared_locator_failures`. A collision
        # unbinds both and puts them back for another round with the clash
        # named; what does not converge becomes an ask, which is the right end
        # for it.
        for name, why in shared_locator_failures(verified, trace=trace).items():
            del verified[name]
            remaining[name] = bound_under.get(name, fields[name])
            next_failures[name] = why

        # DEBUG: why each field that did not bind did not bind, in the logs
        # rather than only in the trace artifact. These are the strings fed back
        # to the model verbatim, so they also show what the next round is being
        # asked -- "read nothing" and "reads 27 values but this field is one
        # value" send it in opposite directions.
        if next_failures:
            log.info(
                "propose.unbound",
                attempt=_attempt + 1,
                failures={k: v[:160] for k, v in next_failures.items()},
            )
        if verified:
            log.info(
                "propose.bound",
                attempt=_attempt + 1,
                bound={
                    name: chain[0].locator.to_dict()
                    for name, chain in verified.items()
                    if chain
                },
            )

        failures = next_failures
        if not failures:
            break

        # A round that only produced correctable answers buys another one. The
        # model was told what was wrong with its form; refusing it the chance to
        # apply that is how a field that IS on the page ends up unresolved --
        # see `is_correctable`.
        # Evaluated on EVERY round, not only the last. Gating it on
        # `_attempt >= budget` meant the grant was considered only after the
        # model had run out of rope -- and by then its answers are "did not
        # propose a locator", which is not correctable, so the allowance never
        # fired once in production. The correctable answer arrives on the FIRST
        # round; that is the one that has to buy the next.
        if (
            corrections < _MAX_CORRECTABLE_RETRIES
            # ANY, not all. A batch asks about several fields at once and they
            # fail for different reasons, so requiring every one to be
            # correctable meant the allowance never fired at all -- measured on
            # an Ulta page where four fields failed together, two with "joined
            # by a comma" and two with "did not propose a locator", and the
            # round was denied to both. The retry is one shared model call: if
            # a single field can act on what it was told, it is worth making.
            and any(is_correctable(reason) for reason in failures.values())
        ):
            corrections += 1
            budget += 1
            log.info(
                "propose.retry_granted",
                fields=sorted(failures), correction=corrections,
            )

    # Once more, because the loop above can exit without ever reaching the
    # check: a batch whose every field was bound from the page's own JSON
    # breaks at `if not remaining` before the first proposal round. There is no
    # retry left here, so a collision simply unbinds -- and an unlocated field
    # is reported as such, which is what puts it in front of a person.
    for name in shared_locator_failures(verified, trace=trace):
        del verified[name]

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
            trace=trace,
            probe=probe,
        )

    return verified
