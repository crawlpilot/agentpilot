"""The Recipe artifact: a versioned, persisted definition of how to collect
a caller-specified schema of fields from a URL.

Two things every shape here exists to avoid, per the design review that
shaped this phase:

1. A single flat action trace can't represent "click each of N sibling
   options, read a value after each" (e.g. size swatches on a product page,
   where clicking size M *replaces* whatever size S showed) -- so reveal
   steps and locators are scoped per `FieldGroup`, and a group backing an
   `array`-typed field carries a `RepeatSpec` describing how to iterate its
   options, not a fixed one-shot click.
2. A `ref` minted during a live exploration run is epoch-scoped
   (`agentpilot.driver.ref_cache`) and meaningless against a fresh page load
   -- so `RevealStep.locator` is always a re-resolvable descriptor (an
   accessibility role+name, or a CSS selector), never a raw `ref`. See
   `agentpilot.recipe.stabilize`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

LocatorSource = Literal["ax_role", "css"]
FieldSource = Literal["json_ld", "hydration", "meta", "css", "ax_role"]
RevealActionType = Literal["click", "fill", "hover", "select_option", "scroll", "wait", "press"]
FieldType = Literal["scalar", "array"]
HealthStatus = Literal["healthy", "degraded", "broken"]


@dataclass
class Locator:
    """A re-resolvable element descriptor -- resolved fresh against a live
    snapshot/DOM at replay time, never a stored `ref`.

    `name_in` (as opposed to `name_contains`) is how a `RepeatSpec.option_locator`
    scopes to a specific option set (e.g. size labels `["S","M","L","XL"]`)
    discovered once at generalization time: `AXSnapshot` carries no DOM
    class/id/parent-selector info, so an exact enumerated name set is the
    stable, resolvable substitute for "these N sibling elements" -- matched
    against the whole tree, not scoped to a parent container. A same-named
    element unrelated to the option set elsewhere on the page is a known,
    accepted v1 false-positive risk (documented, not silently ignored)."""

    source: LocatorSource
    role: str | None = None
    name_contains: str | None = None
    name_in: list[str] | None = None
    selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "role": self.role,
            "name_contains": self.name_contains,
            "name_in": self.name_in,
            "selector": self.selector,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Locator:
        return cls(
            source=d["source"],
            role=d.get("role"),
            name_contains=d.get("name_contains"),
            name_in=d.get("name_in"),
            selector=d.get("selector"),
        )


@dataclass
class RevealStep:
    """One locator-based, re-resolvable action needed to reach a field
    group's data-bearing state. `locator=None` is valid for e.g. a plain
    `wait`/page-level `scroll` with no specific target element."""

    action: RevealActionType
    locator: Locator | None = None
    value: str | None = None  # fill text / select_option values (csv) / press key / wait ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "locator": self.locator.to_dict() if self.locator else None,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RevealStep:
        loc = d.get("locator")
        return cls(
            action=d["action"],
            locator=Locator.from_dict(loc) if loc else None,
            value=d.get("value"),
        )


@dataclass
class RepeatSpec:
    """Present on a `FieldGroup` iff it backs an `array`-typed schema field.
    `option_locator` must match MULTIPLE elements at replay time (e.g. every
    size-swatch button) -- `agentpilot.recipe.generalize` is what produces
    this from a single representative option found during exploration."""

    option_locator: Locator
    max_iterations: int
    array_field: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "option_locator": self.option_locator.to_dict(),
            "max_iterations": self.max_iterations,
            "array_field": self.array_field,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepeatSpec:
        return cls(
            option_locator=Locator.from_dict(d["option_locator"]),
            max_iterations=d["max_iterations"],
            array_field=d["array_field"],
        )


@dataclass
class FieldLocator:
    """Where and how to read one field's value once its group's
    `reveal_steps` (and, if repeating, the current iteration's option) are in
    place. `json_ld`/`hydration`/`meta` are preferred whenever the value is
    actually present there -- far more resilient to a redesign than a `css`
    selector."""

    source: FieldSource
    path: str | None = None  # json_ld/hydration/meta: dotted/bracket path, see jsonpath.py
    selector: str | None = None  # css
    attribute: str | None = None  # css: "text" | "href" | "src" | ...
    role: str | None = None  # ax_role
    name_contains: str | None = None  # ax_role

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "selector": self.selector,
            "attribute": self.attribute,
            "role": self.role,
            "name_contains": self.name_contains,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldLocator:
        return cls(
            source=d["source"],
            path=d.get("path"),
            selector=d.get("selector"),
            attribute=d.get("attribute"),
            role=d.get("role"),
            name_contains=d.get("name_contains"),
        )


def _locators_from_dict(value: Any) -> list[FieldLocator]:
    """A field's stored locators are an ordered *candidate list* (tried in
    order, first non-empty wins -- Pulsar's comma-union/coalesce idiom). Older
    recipes stored a single locator dict per field; accept both so existing
    `field_groups` JSONB deserializes unchanged."""

    if isinstance(value, list):
        return [FieldLocator.from_dict(v) for v in value]
    return [FieldLocator.from_dict(value)]


@dataclass
class FieldGroup:
    group_id: str
    field_names: list[str]
    reveal_steps: list[RevealStep]
    field_locators: dict[str, list[FieldLocator]]
    repeat: RepeatSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "field_names": self.field_names,
            "reveal_steps": [s.to_dict() for s in self.reveal_steps],
            "field_locators": {
                k: [loc.to_dict() for loc in v] for k, v in self.field_locators.items()
            },
            "repeat": self.repeat.to_dict() if self.repeat else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldGroup:
        return cls(
            group_id=d["group_id"],
            field_names=list(d["field_names"]),
            reveal_steps=[RevealStep.from_dict(s) for s in d["reveal_steps"]],
            field_locators={
                k: _locators_from_dict(v) for k, v in d["field_locators"].items()
            },
            repeat=RepeatSpec.from_dict(d["repeat"]) if d.get("repeat") else None,
        )


@dataclass
class Recipe:
    recipe_id: str
    tenant: str
    name: str
    url_pattern: str
    field_schema: dict[str, Any]  # the caller's raw data contract, see schema.py
    version: int
    global_setup: list[RevealStep] = field(default_factory=list)
    field_groups: list[FieldGroup] = field(default_factory=list)
    health_status: HealthStatus = "healthy"
    last_verified_at: datetime | None = None
    last_run_at: datetime | None = None
    schedule_interval_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "global_setup": [s.to_dict() for s in self.global_setup],
            "field_groups": [g.to_dict() for g in self.field_groups],
        }

    @classmethod
    def groups_from_dict(cls, d: dict[str, Any]) -> tuple[list[RevealStep], list[FieldGroup]]:
        global_setup = [RevealStep.from_dict(s) for s in d.get("global_setup", [])]
        field_groups = [FieldGroup.from_dict(g) for g in d.get("field_groups", [])]
        return global_setup, field_groups


@dataclass
class RecipeRunResult:
    """Returned by both `build_recipe` and `replay_recipe` -- `data` is
    `{field_name: value | list[dict]}` for a successful/partial run;
    `field_failures` maps a field name to why it couldn't be resolved."""

    success: bool
    data: dict[str, Any]
    field_failures: dict[str, str]
    error: str | None = None
