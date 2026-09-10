"""When the agent is stuck, ask the person.

v1's answer to a field it could not locate was to drop it and mark the recipe
`degraded`. The author found out later, from a recipe that quietly returns
nothing for that column -- and by then the page state that would have explained
why is long gone.

This is the other answer: the run **parks**, still holding its browser session
open on the page it gave up on, and asks. That the session survives is the
whole point. The run already opens `headful=True`, live-view is already wired,
and the picker already exists -- so the person sees the exact page the agent
failed on and clicks the element, producing the same `Candidate` the agent
would have. Nothing about picking had to be built.

Two kinds of ask, and they need different questions put to a person:

- **unresolved** -- never found. "Where is this?"
- **rejected** -- found, but the judge says it is the wrong thing, and repair
  could not do better. "You picked the breadcrumb; which one is the title?"

Collapsing them into "failed" would ask the wrong one half the time.

The park is bounded. A parked run holds one of `_IDENTITY_SLOTS` warm
identities plus a browser and a proxy pin, so a forgotten tab must not starve
the pool: past `assist_timeout_s` the run resumes on its own and reports those
fields unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.models import Candidate, FieldGroup, Locator, Recipe, Step
from agentpilot.recipe.v2.selector_agent import rank_candidates, verify_locators
from crawlpilot.session.interactive import InteractiveSession
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)

AskKind = Literal["unresolved", "rejected", "absent"]
ResolutionAction = Literal["pick", "scope", "steps", "describe", "skip"]
ScopeShape = Literal["one", "values", "map", "rows"]

# Ops a recording is allowed to carry. Everything else the page might produce is
# either not a reveal (`navigate` -- replay issues its own) or not something the
# recorder emits, and a stored recipe should not be the first place an unknown
# op is discovered.
_RECORDABLE_OPS = frozenset({
    "click", "fill", "select_option", "press", "scroll", "scroll_into_view", "hover",
})


@dataclass(frozen=True)
class PendingAsk:
    """One thing the run needs a person to settle."""

    field: str
    kind: AskKind
    reason: str
    step_trace: list[dict[str, Any]] = field(default_factory=list)
    """What the group's steps actually did. Without this an empty field behind
    a reveal click is unattributable: the selector may be wrong, or the click
    may never have run, and the two need opposite fixes from the person. Every
    reveal step is `optional: true, on_error: continue` by construction, so a
    step that matched nothing is otherwise completely silent."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind,
            "reason": self.reason,
            "step_trace": self.step_trace,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingAsk:
        return cls(
            field=str(d.get("field") or ""),
            kind=d.get("kind", "unresolved"),
            reason=str(d.get("reason") or ""),
            step_trace=list(d.get("step_trace") or []),
        )


@dataclass(frozen=True)
class Resolution:
    """A person's answer to one ask."""

    field: str
    action: ResolutionAction
    locators: list[Locator] = field(default_factory=list)
    """`pick`: what they clicked, already turned into locators by the picker."""
    hint: str = ""
    """`describe`: e.g. "it's inside the Details accordion, open that first"."""
    shape: ScopeShape = "one"
    """`scope`: what the person wants out of the region -- one value, a list of
    values, an open key->value map, or rows."""
    html: str = ""
    """`scope`: the region's markup, sent from the browser that already had the
    element. Prompt context only; every proposal is still verified against the
    live page."""

    steps: list[Step] = field(default_factory=list)
    """`steps`: what the person did to the page, recorded in their browser.

    The answer to "how do I get to it?", which pointing at an element cannot
    give. A field behind three clicks, a scroll and a dismissal has no selector
    that describes the route -- and this is the route."""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Resolution | None:
        name = str(d.get("field") or "")
        action = d.get("action")
        if not name or action not in ("pick", "scope", "steps", "describe", "skip"):
            return None
        locators: list[Locator] = []
        for raw in d.get("locators") or []:
            if isinstance(raw, dict) and raw.get("kind"):
                try:
                    locators.append(Locator.from_dict(raw))
                except Exception:  # noqa: BLE001 - a bad locator is not a bad request
                    continue
        hint = str(d.get("hint") or "").strip()
        steps = parse_recorded_steps(d.get("steps") or [])
        if action in ("pick", "scope") and not locators:
            return None
        if action == "describe" and not hint:
            return None
        if action == "steps" and not steps:
            return None
        shape = d.get("shape")
        return cls(
            field=name,
            action=action,
            locators=locators,
            hint=hint,
            shape=shape if shape in ("one", "values", "map", "rows") else "one",
            html=str(d.get("html") or ""),
            steps=steps,
        )


def parse_recorded_steps(raw: list[Any]) -> list[Step]:
    """Recorded browser events as replayable `Step`s.

    Two filters, and both exist because a step that gets past them fails on
    every single run rather than here:

    - **Only ops a recording can honestly produce.** An unknown op should not
      first be discovered inside a stored recipe.
    - **Only targets the driver can dispatch.** `capture.dispatchability_error`
      is the same gate the exploration capture uses -- an xpath target or a css
      target carrying an index passes `validate_document` and then fails
      forever, because the driver resolves selectors with `querySelector` and
      has no notion of the nth match.

    Everything is `optional` with `on_error: continue`, matching every reveal
    step this system emits: a cookie banner that did not appear this time is not
    a failed run, and a recording is mostly reveals.
    """

    from agentpilot.recipe.v2.capture import dispatchability_error

    out: list[Step] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "")
        if op not in _RECORDABLE_OPS:
            continue

        # Two shapes reach here and both are the same steps. The browser sends
        # `{op, selector, kind, text}`; the run is then parked, and what comes
        # back out of the store is `Step.to_dict()`. Re-parsing rather than
        # re-deriving keeps the round trip lossless -- a `fill`'s text lives in
        # `args` by then, not in `text`, and rebuilding from the recorder shape
        # would silently empty it.
        if "target" in item or "args" in item:
            try:
                step = Step.from_dict(item)
            except Exception:  # noqa: BLE001 - a malformed step is not a bad request
                continue
            if dispatchability_error(step.target, step.op) is None:
                out.append(step)
            continue

        selector = str(item.get("selector") or "").strip()
        # The declared kind, not an assumed one. `record.ts` only ever emits
        # css, but forcing `kind="css"` here would turn an xpath expression that
        # arrived some other way into a css selector carrying `//div[1]` --
        # which sails past the dispatchability gate below and then throws inside
        # `querySelector` on every run. Honouring the kind is what lets the gate
        # see it for what it is.
        kind = "xpath" if item.get("kind") == "xpath" else "css"
        target = Locator(kind=kind, selector=selector) if selector else None
        # `press` and a page-level `scroll` legitimately have no target; every
        # other op needs one, and one without is not replayable.
        if target is None and op not in ("press", "scroll"):
            continue
        if dispatchability_error(target, op) is not None:
            continue

        text = str(item.get("text") or "")
        args: dict[str, Any] = {}
        if op == "fill":
            args = {"text": text}
        elif op == "select_option":
            args = {"values": [text]} if text else {}
        elif op == "press":
            args = {"key": text or "Enter"}
        elif op == "scroll":
            args = {"direction": "down"}

        out.append(
            Step(
                op=op,  # type: ignore[arg-type]
                target=target,
                args=args,
                on_error="continue",
                optional=True,
                label=_recorded_label(op, item.get("text")),
            )
        )
    return out


def _recorded_label(op: str, text: Any) -> str:
    """What the person did, in their words, so a recipe reads as a sequence
    rather than as anonymous selectors."""

    said = str(text or "").strip()
    if op == "click" and said:
        return f'click "{said[:40]}"'
    if op == "press" and said:
        return f"press {said}"
    return f"recorded {op}"


def parse_resolutions(
    raw: list[dict[str, Any]], asks: list[PendingAsk]
) -> dict[str, Resolution]:
    """Keep the answers that correspond to a real ask and carry what their
    action needs. Anything else is dropped rather than half-applied."""

    wanted = {a.field for a in asks}
    out: dict[str, Resolution] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        resolution = Resolution.from_dict(item)
        if resolution is not None and resolution.field in wanted:
            out[resolution.field] = resolution
    return out


def build_asks(
    unresolved: dict[str, str],
    rejected: dict[str, str],
    *,
    absent: dict[str, str] | None = None,
    step_trace: list[dict[str, Any]] | None = None,
) -> list[PendingAsk]:
    """The asks, easiest to answer first.

    Three kinds, and the ordering is about how much work each costs a person:

    - `rejected` leads. They are shown a concrete wrong value and told what it
      actually is, which is nearly always answerable by pointing at the right
      one.
    - `absent` next. The page does not have it, and the honest answers are
      "drop it" or "that lives on another page" -- a decision, not a search.
    - `unresolved` last. "This was never found anywhere" is the open-ended one.

    A field cannot be two of these at once: absence wins over having been
    rejected, and both win over never having been found, because each is a more
    specific account of the same field than the one below it.
    """

    trace = list(step_trace or [])
    gone = absent or {}
    asks = [
        PendingAsk(field=name, kind="rejected", reason=reason, step_trace=trace)
        for name, reason in sorted(rejected.items())
        if name not in gone
    ]
    asks += [
        PendingAsk(field=name, kind="absent", reason=reason, step_trace=trace)
        for name, reason in sorted(gone.items())
    ]
    asks += [
        PendingAsk(field=name, kind="unresolved", reason=reason, step_trace=trace)
        for name, reason in sorted(unresolved.items())
        if name not in rejected and name not in gone
    ]
    return asks


def drop_field(recipe: Recipe, name: str) -> Recipe:
    """Remove a field the person chose to skip, and any group binding it.

    A group left with no bindings is removed outright: `validate_document`
    rejects a group whose every field is unbound, so leaving an empty one would
    produce a document that cannot be saved.
    """

    from agentpilot.recipe.v2.schema import column_to_table_map

    columns = column_to_table_map(recipe.fields)
    table = columns.get(name)

    fields = {k: v for k, v in recipe.fields.items() if k != name}
    groups: list[FieldGroup] = []
    for group in recipe.field_groups:
        bindings = {k: v for k, v in group.bindings.items() if k != name}
        names = [n for n in group.field_names if n != name]
        # Dropping the last column of a table takes the table with it: its rows
        # would have no columns left to fill.
        if table and not any(c in bindings for c in group.bindings if c != name):
            names = [n for n in names if n != table]
            fields.pop(table, None)
        if not bindings or not names:
            continue
        groups.append(
            FieldGroup(
                group_id=group.group_id, field_names=names, bindings=bindings,
                steps=list(group.steps), repeat=group.repeat, expect=group.expect,
            )
        )

    recipe.fields = fields
    recipe.field_groups = groups
    return recipe


async def apply_resolutions(
    recipe: Recipe,
    resolutions: dict[str, Resolution],
    *,
    url: str,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
) -> tuple[Recipe, dict[str, str]]:
    """Apply the answers. Returns the recipe and what still could not be settled.

    A picked locator is verified like any other -- a person pointing at the
    right element on screen can still produce a selector that does not resolve
    through the driver, and binding it unchecked would swap a known gap for a
    silent one.
    """

    from agentpilot.recipe.v2.review import repair_fields

    unsettled: dict[str, str] = {}

    skips = [r for r in resolutions.values() if r.action == "skip"]
    picks = [r for r in resolutions.values() if r.action == "pick"]
    scopes = [r for r in resolutions.values() if r.action == "scope"]
    recordings = [r for r in resolutions.values() if r.action == "steps"]
    describes = {r.field: r.hint for r in resolutions.values() if r.action == "describe"}

    for resolution in skips:
        recipe = drop_field(recipe, resolution.field)
        log.info("assist.field_skipped", field=resolution.field)

    if picks:
        reader = PageReader(
            session=session, registry=registry, driver=driver, base_url=url
        )
        from agentpilot.recipe.v2.schema import all_leaf_fields
        from agentpilot.recipe.v2.transform import TransformContext

        leaves = all_leaf_fields(recipe.fields)
        ctx = TransformContext(url=url)
        for resolution in picks:
            # Held to the same rule as anything the model proposes: a person
            # pointing at the right element does not make its text clean up into
            # the type the field wants.
            resolving, reason = await verify_locators(
                resolution.locators,
                verify=reader.read,
                spec=leaves.get(resolution.field),
                ctx=ctx,
            )
            if not resolving:
                unsettled[resolution.field] = (
                    reason or "the picked element did not resolve through the driver"
                )
                continue
            recipe = _bind(
                recipe,
                resolution.field,
                rank_candidates([v.locator for v in resolving]),
            )

    for resolution in scopes:
        recipe, problem = await _apply_scope(
            recipe, resolution, url=url, session=session, registry=registry,
            driver=driver, llm_config=llm_config,
        )
        if problem:
            unsettled[resolution.field] = problem

    for resolution in recordings:
        recipe, problem = await _apply_steps(
            recipe, resolution, url=url, session=session, registry=registry,
            driver=driver, llm_config=llm_config,
        )
        if problem:
            unsettled[resolution.field] = problem

    if describes:
        recipe, repaired = await repair_fields(
            recipe, describes, url=url, session=session, registry=registry,
            driver=driver, llm_config=llm_config,
        )
        for name in describes:
            if name not in repaired:
                unsettled[name] = "could not find it from that description either"

    return recipe, unsettled


async def _apply_steps(
    recipe: Recipe,
    resolution: Resolution,
    *,
    url: str,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
) -> tuple[Recipe, str | None]:
    """Bind a field from the route a person recorded to it.

    **On a fresh page, not on theirs.** Someone answering an ask has usually
    already opened the thing they were about to record -- it is the natural way
    to go looking for it -- so their first recorded click may land on something
    only their session has. Replaying the recording against a reloaded page is
    the only way to find out, and the alternative is a recipe that verifies here
    and returns nothing on every real run. `_apply_scope` reloads for the same
    reason.

    Then the ordinary selector agent looks at what the route revealed. The
    person contributed the part they knew -- how to get there -- and the model
    contributes the part it is good at, which is which node holds the value.
    """

    from agentpilot.recipe.v2.review import restore_page
    from agentpilot.recipe.v2.schema import all_leaf_fields
    from agentpilot.recipe.v2.selector_agent import propose_and_verify
    from agentpilot.recipe.v2.steps import run_steps
    from crawlpilot.dom.serializer import serialize

    leaves = all_leaf_fields(recipe.fields)
    table = recipe.fields.get(resolution.field)
    is_table = table is not None and table.type.is_rows
    spec = table if is_table else leaves.get(resolution.field)
    if spec is None:
        return recipe, "that field is not in this recipe any more"

    reader, ctx = await restore_page(
        recipe, url, session=session, registry=registry, driver=driver
    )
    trace, policy = await run_steps(resolution.steps, ctx)
    reader.invalidate()

    ran = [o for o in trace if o.status in ("ok", "recovered")]
    if not ran:
        # Every step is `optional`, so a recording that matches nothing anywhere
        # is completely silent -- and would bind the field against the page as
        # loaded, which is not what the person recorded.
        return recipe, (
            "none of the recorded steps found anything on a freshly loaded page. "
            "The route probably starts from something your session already had "
            "open -- reload the page and record it again from the top."
        )

    snapshot = await reader.snapshot()
    if snapshot is None:
        return recipe, "could not read the page after replaying those steps"

    if is_table:
        from agentpilot.recipe.v2.rows import propose_rows

        binding = await propose_rows(
            spec,
            snapshot_text=serialize(snapshot).llm_text,
            structured_data=await reader.structured_data(),
            reader=reader,
            llm_config=llm_config,
            page_url=url,
        )
        if binding is None:
            return recipe, (
                "those steps ran, but what they revealed does not read as "
                "repeating rows"
            )
        recipe = _bind_repeat(recipe, spec.name, binding)
        return _with_steps(recipe, spec.name, resolution.steps), None

    verified = await propose_and_verify(
        {resolution.field: spec},
        snapshot_text=serialize(snapshot).llm_text,
        structured_data=await reader.structured_data(),
        llm_config=llm_config,
        verify=reader.read,
        page_url=url,
    )
    candidates = verified.get(resolution.field)
    if not candidates:
        return recipe, (
            f"those steps ran ({len(ran)} of {len(resolution.steps)} did something), "
            "but the field still could not be located on what they revealed"
        )

    log.info(
        "assist.bound_from_recording",
        field=resolution.field, steps=len(resolution.steps), ran=len(ran),
        policy=policy,
    )
    recipe = _bind(recipe, resolution.field, candidates)
    return _with_steps(recipe, resolution.field, resolution.steps), None


def _with_steps(recipe: Recipe, name: str, steps: list[Step]) -> Recipe:
    """Put the recorded route onto the group that owns the field.

    Replaces rather than appends: the recording is the whole route from a fresh
    page, which is exactly what a group's `steps` are, and running whatever was
    there before it would repeat half of it.
    """

    from agentpilot.recipe.v2.schema import column_to_table_map

    owner = column_to_table_map(recipe.fields).get(name) or name
    groups = list(recipe.field_groups)
    for index, group in enumerate(groups):
        if name in group.bindings or owner in group.field_names:
            groups[index] = FieldGroup(
                group_id=group.group_id,
                field_names=list(group.field_names),
                bindings=dict(group.bindings),
                steps=list(steps),
                repeat=group.repeat,
                expect=group.expect,
            )
            recipe.field_groups = groups
            return recipe
    return recipe


async def _apply_scope(
    recipe: Recipe,
    resolution: Resolution,
    *,
    url: str,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
) -> tuple[Recipe, str | None]:
    """Bind a field from the region a person pointed at.

    Two things happen here and the order matters.

    **The region is checked on a FRESH page first.** A person answering an ask
    is looking at a session they may have interacted with -- opening the very
    accordion the section lives in is the most natural thing to do before
    pointing at it. Binding against that state produces a locator that verifies
    here and returns nothing on every real run, because replay loads the page
    closed. So the page is reloaded and the recipe's own steps re-run, and the
    region has to survive that. If it does not, the answer is not "bad pick" but
    "we are missing the step that opens it", and that is what the person is told.

    **Then the model looks inside it.** They supplied the region, which is the
    part they know; the model supplies which node in it holds the value, which is
    the part it is good at. See `propose_within`.
    """

    from agentpilot.recipe.v2.review import restore_page
    from agentpilot.recipe.v2.schema import all_leaf_fields
    from agentpilot.recipe.v2.selector_agent import propose_within
    from agentpilot.recipe.v2.steps import run_steps

    leaves = all_leaf_fields(recipe.fields)
    # A table field has no entry in `leaves` -- its columns are the leaves -- so
    # looking only there rejected every ask about a table with "that field is
    # not in this recipe any more", which is exactly the ask a person is most
    # likely to be answering by pointing at a section.
    table = recipe.fields.get(resolution.field)
    is_table = table is not None and table.type.is_rows
    spec = table if is_table else leaves.get(resolution.field)
    if spec is None:
        return recipe, "that field is not in this recipe any more"

    scope = resolution.locators[0]

    reader, ctx = await restore_page(
        recipe, url, session=session, registry=registry, driver=driver
    )
    group, _key = _group_of(recipe, resolution.field)
    if group is not None and group.steps:
        await run_steps(group.steps, ctx)
    reader.invalidate()

    if not await _resolves(reader, scope):
        return recipe, (
            "that section is not on the page when it loads fresh -- something has "
            "to open it first. Point at whatever you clicked to reveal it."
        )

    if is_table:
        return await _bind_rows_in(
            recipe, spec, scope,
            reader=reader, llm_config=llm_config, url=url, html=resolution.html,
        )

    verified = await propose_within(
        {resolution.field: _shaped(spec, resolution.shape)},
        scope=scope,
        fragment_html=resolution.html,
        llm_config=llm_config,
        verify=reader.read,
        page_url=url,
    )
    candidates = verified.get(resolution.field)
    if not candidates:
        return recipe, "nothing in that section resolved to a value for this field"

    recipe.fields[resolution.field] = _shaped(spec, resolution.shape)
    return _bind(recipe, resolution.field, candidates), None


async def _bind_rows_in(
    recipe: Recipe,
    spec: Any,
    scope: Locator,
    *,
    reader: PageReader,
    llm_config: LLMConfig,
    url: str,
    html: str,
) -> tuple[Recipe, str | None]:
    """Bind a table field from the region a person pointed at.

    The same question `propose_rows` asks during a build -- where are the rows,
    and where inside a row is each column -- with the search narrowed to what
    the person supplied. That narrowing is the whole value of the ask: across a
    page the model is choosing among thousands of nodes, inside a section among
    dozens, and the person has already contributed the piece of knowledge they
    actually had.
    """

    from agentpilot.recipe.v2.rows import propose_rows

    binding = await propose_rows(
        spec,
        snapshot_text=html,
        structured_data=await reader.structured_data(),
        reader=reader,
        llm_config=llm_config,
        page_url=url,
        scope=scope,
    )
    if binding is None:
        return recipe, (
            "that section does not read as repeating rows -- every row came back "
            "the same, or the columns were not inside one. If it is not a table, "
            "pick a different shape for it."
        )

    log.info(
        "assist.table_bound_as_rows",
        field=spec.name, kind=binding.repeat.kind, rows=len(binding.rows),
    )
    return _bind_repeat(recipe, spec.name, binding), None


def _bind_repeat(recipe: Recipe, name: str, binding: Any) -> Recipe:
    """Put a repeat and its column chains onto the group that owns the table.

    Distinct from `_bind`, which writes a single candidate chain: a table is
    satisfied by its group's `RepeatSpec` plus one binding per column, and
    `validate_document` rejects a table group carrying no repeat.
    """

    groups = list(recipe.field_groups)
    for index, group in enumerate(groups):
        if name in group.field_names:
            groups[index] = FieldGroup(
                group_id=group.group_id,
                field_names=list(group.field_names),
                bindings=binding.bindings,
                steps=list(group.steps),
                repeat=binding.repeat,
                expect=group.expect,
            )
            recipe.field_groups = groups
            return recipe

    groups.append(
        FieldGroup(
            group_id=f"assist-{name}",
            field_names=[name],
            bindings=binding.bindings,
            repeat=binding.repeat,
        )
    )
    recipe.field_groups = groups
    return recipe


async def _resolves(reader: PageReader, locator: Locator) -> bool:
    """Whether a locator reads anything at all right now."""

    from agentpilot.recipe.v2.resolve import is_empty

    try:
        return not is_empty(await reader.read(locator))
    except Exception:  # noqa: BLE001 - a locator that raises has not resolved
        return False


def _shaped(spec: Any, shape: ScopeShape) -> Any:
    """The field's spec, retyped for what the person asked the region to yield.

    A section is rarely one value -- "specifications" is the case in point, an
    open key->value map -- and binding it as a scalar is how a whole table comes
    back as its own heading.
    """

    from dataclasses import replace as _replace

    from agentpilot.recipe.v2.schema import TypeSpec

    if shape == "values":
        return _replace(spec, type=TypeSpec(kind="list", items=TypeSpec(kind="scalar")))
    if shape == "map":
        # `properties` stays empty: the keys come from the page, which is what
        # `render_fields_for_prompt` calls an "open key->value map".
        return _replace(spec, type=TypeSpec(kind="object"))
    # `rows` needs columns the person has not named, so it stays whatever the
    # schema already declared -- a table field keeps its columns, and anything
    # else keeps its scalar type rather than becoming an unbindable table.
    return spec


def _group_of(recipe: Recipe, name: str):
    from agentpilot.recipe.v2.review import _group_of as find

    return find(recipe, name)


def _bind(recipe: Recipe, name: str, candidates: list[Candidate]) -> Recipe:
    """Put a chain onto the group that owns the field, creating one if the
    field never made it into a group at all."""

    from agentpilot.recipe.v2.schema import column_to_table_map

    columns = column_to_table_map(recipe.fields)
    owner = columns.get(name) or name

    groups = list(recipe.field_groups)
    for index, group in enumerate(groups):
        if name in group.bindings or owner in group.field_names:
            bindings = dict(group.bindings)
            bindings[name] = candidates
            names = list(group.field_names)
            if owner not in names:
                names.append(owner)
            groups[index] = FieldGroup(
                group_id=group.group_id, field_names=names, bindings=bindings,
                steps=list(group.steps), repeat=group.repeat, expect=group.expect,
            )
            recipe.field_groups = groups
            return recipe

    # The field was never located, so no group claims it. It reads off the page
    # as loaded -- whatever revealed it for the person is not knowledge this
    # path has, and inventing steps would be worse than a group with none.
    groups.append(
        FieldGroup(
            group_id=f"assist-{name}", field_names=[owner], bindings={name: candidates}
        )
    )
    recipe.field_groups = groups
    return recipe
