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

AskKind = Literal["unresolved", "rejected"]
ResolutionAction = Literal["pick", "describe", "skip"]


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

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Resolution | None:
        name = str(d.get("field") or "")
        action = d.get("action")
        if not name or action not in ("pick", "describe", "skip"):
            return None
        locators: list[Locator] = []
        for raw in d.get("locators") or []:
            if isinstance(raw, dict) and raw.get("kind"):
                try:
                    locators.append(Locator.from_dict(raw))
                except Exception:  # noqa: BLE001 - a bad locator is not a bad request
                    continue
        hint = str(d.get("hint") or "").strip()
        if action == "pick" and not locators:
            return None
        if action == "describe" and not hint:
            return None
        return cls(field=name, action=action, locators=locators, hint=hint)


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
    step_trace: list[dict[str, Any]] | None = None,
) -> list[PendingAsk]:
    """The asks, most-answerable first.

    `rejected` leads: the person is being shown a concrete wrong value and told
    what it actually is, which is a far easier question than "this was never
    found anywhere".
    """

    trace = list(step_trace or [])
    asks = [
        PendingAsk(field=name, kind="rejected", reason=reason, step_trace=trace)
        for name, reason in sorted(rejected.items())
    ]
    asks += [
        PendingAsk(field=name, kind="unresolved", reason=reason, step_trace=trace)
        for name, reason in sorted(unresolved.items())
        if name not in rejected
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
    describes = {r.field: r.hint for r in resolutions.values() if r.action == "describe"}

    for resolution in skips:
        recipe = drop_field(recipe, resolution.field)
        log.info("assist.field_skipped", field=resolution.field)

    if picks:
        reader = PageReader(
            session=session, registry=registry, driver=driver, base_url=url
        )
        for resolution in picks:
            resolving, reason = await verify_locators(
                resolution.locators, verify=reader.read
            )
            if not resolving:
                unsettled[resolution.field] = (
                    reason or "the picked element did not resolve through the driver"
                )
                continue
            recipe = _bind(recipe, resolution.field, rank_candidates(resolving))

    if describes:
        recipe, repaired = await repair_fields(
            recipe, describes, url=url, session=session, registry=registry,
            driver=driver, llm_config=llm_config,
        )
        for name in describes:
            if name not in repaired:
                unsettled[name] = "could not find it from that description either"

    return recipe, unsettled


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
