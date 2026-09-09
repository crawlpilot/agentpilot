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
from agentpilot.recipe.v2.models import Candidate, FieldGroup, Locator, Recipe
from agentpilot.recipe.v2.selector_agent import rank_candidates, verify_locators
from crawlpilot.session.interactive import InteractiveSession
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)

AskKind = Literal["unresolved", "rejected", "absent"]
ResolutionAction = Literal["pick", "scope", "describe", "skip"]
ScopeShape = Literal["one", "values", "map", "rows"]


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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Resolution | None:
        name = str(d.get("field") or "")
        action = d.get("action")
        if not name or action not in ("pick", "scope", "describe", "skip"):
            return None
        locators: list[Locator] = []
        for raw in d.get("locators") or []:
            if isinstance(raw, dict) and raw.get("kind"):
                try:
                    locators.append(Locator.from_dict(raw))
                except Exception:  # noqa: BLE001 - a bad locator is not a bad request
                    continue
        hint = str(d.get("hint") or "").strip()
        if action in ("pick", "scope") and not locators:
            return None
        if action == "describe" and not hint:
            return None
        shape = d.get("shape")
        return cls(
            field=name,
            action=action,
            locators=locators,
            hint=hint,
            shape=shape if shape in ("one", "values", "map", "rows") else "one",
            html=str(d.get("html") or ""),
        )


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

    if describes:
        recipe, repaired = await repair_fields(
            recipe, describes, url=url, session=session, registry=registry,
            driver=driver, llm_config=llm_config,
        )
        for name in describes:
            if name not in repaired:
                unsettled[name] = "could not find it from that description either"

    return recipe, unsettled


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
    spec = leaves.get(resolution.field)
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
