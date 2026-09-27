"""The Recipe v2 document: a versioned, persisted, **URL-independent**
description of how to collect a caller-declared set of fields from one kind of
page. See docs/recipe-contract-v2.md, which is normative; this module is its
Python projection and the field names match one-for-one.

Three shapes here exist because v1 could not express something a real page
needed:

1. **The URL is run input, not recipe state.** v1's `url_pattern` was used
   literally as the navigate target, which pinned every recipe to the single
   page it was built on. `TargetSpec` is a guard instead -- it answers "does
   this recipe apply to this URL?" and nothing more.

2. **One `Locator` for both actions and reads.** v1 had two divergent types
   with different source unions and no shared base, so a reveal step and a
   field read could never share a descriptor.

3. **`RepeatSpec.kind`.** v1 could only produce table rows by clicking DOM
   options. On a Zara product page the entire size/price/stock table is already
   in the JSON-LD `hasVariant[]`, and on Walmart all six accordion sections are
   in `__NEXT_DATA__`; modelling those as clicks costs five page mutations and a
   re-render race to reproduce data one structured read already contains.

Nothing addressable is stored, exactly as in v1: a `ref` minted during a live
run is epoch-scoped and meaningless against a fresh page load, so every locator
here is a descriptor re-resolved against the live page at replay time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from agentpilot.recipe.v2.schema import FieldSpec, fields_to_dict, parse_fields
from agentpilot.recipe.v2.transform import Transform, parse_transforms

LocatorKind = Literal["css", "xpath", "ax_role", "text", "json_ld", "hydration", "meta"]
PathLang = Literal["simple", "jmespath"]
PredicateKind = Literal[
    "selector_present", "selector_absent", "visible", "hidden", "text_present",
    "url_matches", "json_path_present", "count_at_least", "meta_equals",
]
"""`hidden` is the inverse of `visible`, and it is not `selector_absent`.

A collapsed accordion's content is in the DOM and simply not painted, so
`selector_absent` is false for it while `hidden` is true. That distinction is
what lets a reveal step guard itself: click only if the thing it reveals is not
already showing, which is what makes a group's route safe to replay after
another group has already opened the same section."""
StepOp = Literal[
    "navigate", "click", "double_click", "hover", "fill", "clear", "press", "send_keys",
    "select_option", "check", "uncheck", "scroll", "scroll_into_view", "find_text",
    "drag", "tap", "swipe", "wait",
    "wait_for_selector", "wait_for_text", "wait_for_url", "wait_for_load",
    "dialog_accept", "dialog_dismiss",
    "new_tab", "switch_tab", "close_tab", "download",
]
OnError = Literal["fail", "continue", "skip_group"]
RepeatKind = Literal["dom", "dom_rows", "json"]
MatcherKind = Literal["glob", "regex", "host"]
RecipeStatus = Literal["draft", "approved", "published"]
HealthStatus = Literal["healthy", "degraded", "broken"]
RunOutcome = Literal["ok", "partial", "failed", "blocked"]
FieldStatus = Literal["resolved", "fallback", "suspect", "empty", "failed"]
StepStatus = Literal["ok", "skipped", "recovered", "failed"]

# Ops the recipe vocabulary deliberately does NOT include, kept here so the
# omission is discoverable rather than looking like an oversight. Both are
# `safety="sensitive"` in the browser catalog and both amount to running
# arbitrary JS on a schedule; a recipe is authored by a model reading an
# untrusted page, so neither belongs in it. Lua (contract 7.1) is the
# sanctioned escape hatch, and it cannot touch the page.
EXCLUDED_OPS = frozenset({"execute_js", "wait_for_function"})


def _drop_defaults(pairs: list[tuple[str, Any, Any]]) -> dict[str, Any]:
    """Serialize only what differs from the default. A stored recipe is read by
    humans and diffed by heal; a document full of `"index": null` is noise in
    both."""

    return {name: got for name, got, default in pairs if got != default}


@dataclass(frozen=True)
class UrlMatcher:
    kind: MatcherKind
    pattern: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "pattern": self.pattern}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UrlMatcher:
        return cls(kind=d["kind"], pattern=d["pattern"])


@dataclass(frozen=True)
class TargetSpec:
    """A guard, not a navigation target. An empty `match` means the recipe
    applies to any URL -- legitimate for a generic recipe, suspicious for a
    site-specific one."""

    match: list[UrlMatcher] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"match": [m.to_dict() for m in self.match]}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> TargetSpec:
        return cls(match=[UrlMatcher.from_dict(m) for m in ((d or {}).get("match") or [])])


@dataclass(frozen=True)
class Locator:
    kind: LocatorKind
    # css / xpath
    selector: str | None = None
    index: int | None = None
    all: bool = False
    attribute: str = "text"
    # ax_role
    role: str | None = None
    name_contains: str | None = None
    name_in: list[str] | None = None
    name_regex: str | None = None
    # json_ld / hydration / meta
    path: str | None = None
    path_lang: PathLang = "simple"
    # text
    text: str | None = None
    # scoping
    within: Locator | None = None
    frame: str | None = None

    @property
    def is_structured(self) -> bool:
        return self.kind in ("json_ld", "hydration", "meta")

    def to_dict(self) -> dict[str, Any]:
        out = _drop_defaults([
            ("selector", self.selector, None),
            ("index", self.index, None),
            ("all", self.all, False),
            ("attribute", self.attribute, "text"),
            ("role", self.role, None),
            ("name_contains", self.name_contains, None),
            ("name_in", self.name_in, None),
            ("name_regex", self.name_regex, None),
            ("path", self.path, None),
            ("path_lang", self.path_lang, "simple"),
            ("text", self.text, None),
            ("frame", self.frame, None),
        ])
        out["kind"] = self.kind
        if self.within is not None:
            out["within"] = self.within.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Locator:
        within = d.get("within")
        return cls(
            kind=d["kind"],
            selector=d.get("selector"),
            index=d.get("index"),
            all=bool(d.get("all", False)),
            attribute=d.get("attribute", "text"),
            role=d.get("role"),
            name_contains=d.get("name_contains"),
            name_in=list(d["name_in"]) if d.get("name_in") else None,
            name_regex=d.get("name_regex"),
            path=d.get("path"),
            path_lang=d.get("path_lang", "simple"),
            text=d.get("text"),
            within=cls.from_dict(within) if within else None,
            frame=d.get("frame"),
        )


@dataclass(frozen=True)
class Predicate:
    """Side-effect free, and false rather than raising when it cannot be
    evaluated: a guard that explodes is worse than a guard that declines."""

    kind: PredicateKind
    selector: str | None = None
    text: str | None = None
    url: str | None = None
    path: str | None = None
    source: str | None = None
    key: str | None = None
    value: str | None = None
    n: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out = _drop_defaults([
            ("selector", self.selector, None), ("text", self.text, None),
            ("url", self.url, None), ("path", self.path, None),
            ("source", self.source, None), ("key", self.key, None),
            ("value", self.value, None), ("n", self.n, None),
        ])
        out["kind"] = self.kind
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Predicate:
        return cls(
            kind=d["kind"], selector=d.get("selector"), text=d.get("text"),
            url=d.get("url"), path=d.get("path"), source=d.get("source"),
            key=d.get("key"), value=d.get("value"), n=d.get("n"),
        )


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 1
    backoff_ms: int = 250

    def to_dict(self) -> dict[str, Any]:
        return {"attempts": self.attempts, "backoff_ms": self.backoff_ms}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RetryPolicy:
        return cls(attempts=int(d["attempts"]), backoff_ms=int(d.get("backoff_ms", 250)))


@dataclass(frozen=True)
class Step:
    """One unit of "make the data reachable".

    `retry` applies only to this step and only for transient failures. It never
    re-runs an earlier step: a batch of dispatched actions is not idempotent,
    which is the same reason the agent loop refuses to retry action dispatch.
    """

    op: StepOp
    target: Locator | None = None
    args: dict[str, Any] = field(default_factory=dict)
    timeout_ms: int | None = None
    retry: RetryPolicy | None = None
    on_error: OnError = "fail"
    optional: bool = False
    when: list[Predicate] = field(default_factory=list)
    repeat_until: Predicate | None = None
    max_repeats: int = 0
    label: str | None = None

    @property
    def effective_on_error(self) -> OnError:
        return "continue" if self.optional else self.on_error

    def to_dict(self) -> dict[str, Any]:
        out = _drop_defaults([
            ("args", self.args, {}),
            ("timeout_ms", self.timeout_ms, None),
            ("on_error", self.on_error, "fail"),
            ("optional", self.optional, False),
            ("max_repeats", self.max_repeats, 0),
            ("label", self.label, None),
        ])
        out["op"] = self.op
        if self.target is not None:
            out["target"] = self.target.to_dict()
        if self.retry is not None:
            out["retry"] = self.retry.to_dict()
        if self.when:
            out["when"] = [p.to_dict() for p in self.when]
        if self.repeat_until is not None:
            out["repeat_until"] = self.repeat_until.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Step:
        target = d.get("target")
        repeat_until = d.get("repeat_until")
        retry = d.get("retry")
        return cls(
            op=d["op"],
            target=Locator.from_dict(target) if target else None,
            args=dict(d.get("args") or {}),
            timeout_ms=d.get("timeout_ms"),
            retry=RetryPolicy.from_dict(retry) if retry else None,
            on_error=d.get("on_error", "fail"),
            optional=bool(d.get("optional", False)),
            when=[Predicate.from_dict(p) for p in (d.get("when") or [])],
            repeat_until=Predicate.from_dict(repeat_until) if repeat_until else None,
            max_repeats=int(d.get("max_repeats", 0)),
            label=d.get("label"),
        )


@dataclass(frozen=True)
class PageVariant:
    variant_id: str
    detect: list[Predicate] = field(default_factory=list)
    priority: int = 100
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "variant_id": self.variant_id,
            "detect": [p.to_dict() for p in self.detect],
        }
        if self.priority != 100:
            out["priority"] = self.priority
        if self.label:
            out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PageVariant:
        return cls(
            variant_id=d["variant_id"],
            detect=[Predicate.from_dict(p) for p in (d.get("detect") or [])],
            priority=int(d.get("priority", 100)),
            label=d.get("label"),
        )


@dataclass(frozen=True)
class Candidate:
    """One way to read a field. A field carries an ordered list of these and
    the first non-empty one wins -- Pulsar's coalesce idiom, with the ordering
    now stated rather than implied by list position."""

    locator: Locator
    priority: int = 100
    when: list[Predicate] = field(default_factory=list)
    variant_id: str | None = None
    transform: list[Transform] | None = None
    verified_on: int = 0
    confidence: float | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = _drop_defaults([
            ("priority", self.priority, 100),
            ("variant_id", self.variant_id, None),
            ("verified_on", self.verified_on, 0),
            ("confidence", self.confidence, None),
            ("note", self.note, None),
        ])
        out["locator"] = self.locator.to_dict()
        if self.when:
            out["when"] = [p.to_dict() for p in self.when]
        if self.transform is not None:
            out["transform"] = [t.to_dict() for t in self.transform]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candidate:
        return cls(
            locator=Locator.from_dict(d["locator"]),
            priority=int(d.get("priority", 100)),
            when=[Predicate.from_dict(p) for p in (d.get("when") or [])],
            variant_id=d.get("variant_id"),
            transform=(
                parse_transforms(d["transform"]) if d.get("transform") is not None else None
            ),
            verified_on=int(d.get("verified_on", 0)),
            confidence=d.get("confidence"),
            note=d.get("note"),
        )


@dataclass(frozen=True)
class RepeatSpec:
    """Produces the rows of a `table` field.

    Three kinds, in descending order of preference:

    `kind="json"` is the one to reach for. On the pages this contract was
    written against, the size table, the spec sheet and every accordion section
    were all already in the page's JSON -- clicking for them costs page
    mutations, a re-render race, and worse resilience, to reproduce data one
    read already contains.

    `kind="dom_rows"` reads N row *elements* already rendered on the page --
    search results, a listing, an HTML table -- resolving each column relative
    to its own row. This is the commonest extraction there is and until v2.1 it
    had no representation at all: `json` needs the data to already be an array,
    and `dom` clicks, which on a results page means navigating away on the
    first row. Authors were left emitting one `all: true` list per column and
    zipping by index, which silently misaligns the moment one row lacks a cell.

    `kind="dom"` clicks through an option set, re-reading the page after each
    click. The last resort, and the only one that mutates the page.
    """

    kind: RepeatKind
    row_field: str
    max_iterations: int
    option_locator: Locator | None = None
    action: StepOp = "click"
    settle: Step | None = None
    rows_locator: Locator | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "row_field": self.row_field,
            "max_iterations": self.max_iterations,
        }
        if self.option_locator is not None:
            out["option_locator"] = self.option_locator.to_dict()
        if self.action != "click":
            out["action"] = self.action
        if self.settle is not None:
            out["settle"] = self.settle.to_dict()
        if self.rows_locator is not None:
            out["rows_locator"] = self.rows_locator.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepeatSpec:
        option = d.get("option_locator")
        rows = d.get("rows_locator")
        settle = d.get("settle")
        return cls(
            kind=d["kind"],
            row_field=d["row_field"],
            max_iterations=int(d["max_iterations"]),
            option_locator=Locator.from_dict(option) if option else None,
            action=d.get("action", "click"),
            settle=Step.from_dict(settle) if settle else None,
            rows_locator=Locator.from_dict(rows) if rows else None,
        )


@dataclass(frozen=True)
class Expectation:
    min_rows: int | None = None
    max_rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _drop_defaults([("min_rows", self.min_rows, None),
                               ("max_rows", self.max_rows, None)])

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Expectation | None:
        if not d:
            return None
        return cls(min_rows=d.get("min_rows"), max_rows=d.get("max_rows"))


@dataclass(frozen=True)
class FieldGroup:
    group_id: str
    field_names: list[str]
    bindings: dict[str, list[Candidate]]
    steps: list[Step] = field(default_factory=list)
    teardown: list[Step] = field(default_factory=list)
    """What to do after this group's fields have been read.

    The counterpart `steps` never had. A recorded route is one ordered thing --
    *dismiss the banner, open the accordion, read the table, close the modal* --
    and with nowhere to put that last part it had to be folded in with the
    setup, where it ran BEFORE the binding and shut the value away. Separating
    it is the fix; running it is close to a no-op.

    **It changes no outcome today, and that is expected.** This is the same
    problem re-navigation solves, solved the cheap way. `replay` reloads the
    page before every group precisely because state one group leaves corrupts
    the next -- on a Zara product page, an open PRODUCT MEASUREMENTS drawer
    physically covers the COMPOSITION button, so two reveal steps that each
    work alone time out in sequence. Re-navigation answers that by throwing the
    whole page away, at O(groups) page loads; teardown answers it by closing
    what was opened.

    So while both run, this is redundant. It is here because the alternative
    was to discard the half of a recorded route that comes after the pick --
    silently truncating what somebody deliberately recorded -- and because a
    group that tidies up after itself is the precondition for the optimisation
    `replay`'s docstring already names: skipping the reload for groups that
    provably leave the page as they found it.

    Runs best-effort and never fails the group: by the time it executes the
    values are already collected. See `replay._replay_group`.
    """
    repeat: RepeatSpec | None = None
    expect: Expectation | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "group_id": self.group_id,
            "field_names": list(self.field_names),
            "bindings": {k: [c.to_dict() for c in v] for k, v in self.bindings.items()},
        }
        if self.steps:
            out["steps"] = [s.to_dict() for s in self.steps]
        if self.teardown:
            out["teardown"] = [s.to_dict() for s in self.teardown]
        if self.repeat is not None:
            out["repeat"] = self.repeat.to_dict()
        if self.expect is not None:
            out["expect"] = self.expect.to_dict()
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FieldGroup:
        repeat = d.get("repeat")
        return cls(
            group_id=d["group_id"],
            field_names=list(d["field_names"]),
            bindings={
                k: [Candidate.from_dict(c) for c in v]
                for k, v in (d.get("bindings") or {}).items()
            },
            steps=[Step.from_dict(s) for s in (d.get("steps") or [])],
            teardown=[Step.from_dict(s) for s in (d.get("teardown") or [])],
            repeat=RepeatSpec.from_dict(repeat) if repeat else None,
            expect=Expectation.from_dict(d.get("expect")),
        )


@dataclass(frozen=True)
class ExecutionDefaults:
    step_timeout_ms: int = 10_000
    navigate_timeout_ms: int = 30_000
    max_repeat_iterations: int = 20
    lua_timeout_ms: int = 250

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_timeout_ms": self.step_timeout_ms,
            "navigate_timeout_ms": self.navigate_timeout_ms,
            "max_repeat_iterations": self.max_repeat_iterations,
            "lua_timeout_ms": self.lua_timeout_ms,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> ExecutionDefaults:
        d = d or {}
        return cls(
            step_timeout_ms=int(d.get("step_timeout_ms", 10_000)),
            navigate_timeout_ms=int(d.get("navigate_timeout_ms", 30_000)),
            max_repeat_iterations=int(d.get("max_repeat_iterations", 20)),
            lua_timeout_ms=int(d.get("lua_timeout_ms", 250)),
        )


@dataclass
class Recipe:
    recipe_id: str
    tenant: str
    name: str
    version: int
    target: TargetSpec
    fields: dict[str, FieldSpec]
    sample_urls: list[str] = field(default_factory=list)
    variants: list[PageVariant] = field(default_factory=list)
    global_setup: list[Step] = field(default_factory=list)
    field_groups: list[FieldGroup] = field(default_factory=list)
    defaults: ExecutionDefaults = field(default_factory=ExecutionDefaults)
    status: RecipeStatus = "draft"
    built_under: dict[str, Any] = field(default_factory=dict)
    has_script: bool = False
    health_status: HealthStatus = "healthy"
    heal_attempts: int = 0
    last_verified_at: datetime | None = None
    last_run_at: datetime | None = None
    schedule_interval_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """The full portable document -- what a `spec JSONB` column stores and
        what `docs/schemas/recipe-v2.schema.json` validates. Unlike v1, this is
        the whole recipe rather than only its groups, so a recipe can be
        exported, diffed, reviewed and re-imported without the database."""

        out: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "target": self.target.to_dict(),
            "fields": fields_to_dict(self.fields),
            "sample_urls": list(self.sample_urls),
            "field_groups": [g.to_dict() for g in self.field_groups],
            "defaults": self.defaults.to_dict(),
        }
        if self.recipe_id:
            out["recipe_id"] = self.recipe_id
        if self.tenant:
            out["tenant"] = self.tenant
        if self.variants:
            out["variants"] = [v.to_dict() for v in self.variants]
        if self.global_setup:
            out["global_setup"] = [s.to_dict() for s in self.global_setup]
        if self.built_under:
            out["built_under"] = dict(self.built_under)
        if self.has_script:
            out["has_script"] = True
        if self.health_status != "healthy":
            out["health_status"] = self.health_status
        if self.heal_attempts:
            out["heal_attempts"] = self.heal_attempts
        if self.schedule_interval_seconds is not None:
            out["schedule_interval_seconds"] = self.schedule_interval_seconds
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, recipe_id: str = "", tenant: str = "") -> Recipe:
        return cls(
            recipe_id=d.get("recipe_id", recipe_id),
            tenant=d.get("tenant", tenant),
            name=d["name"],
            version=int(d.get("version", 1)),
            target=TargetSpec.from_dict(d.get("target")),
            fields=parse_fields(d.get("fields") or {}),
            sample_urls=list(d.get("sample_urls") or []),
            variants=[PageVariant.from_dict(v) for v in (d.get("variants") or [])],
            global_setup=[Step.from_dict(s) for s in (d.get("global_setup") or [])],
            field_groups=[FieldGroup.from_dict(g) for g in (d.get("field_groups") or [])],
            defaults=ExecutionDefaults.from_dict(d.get("defaults")),
            status=d.get("status", "draft"),
            built_under=dict(d.get("built_under") or {}),
            has_script=bool(d.get("has_script", False)),
            health_status=d.get("health_status", "healthy"),
            heal_attempts=int(d.get("heal_attempts", 0)),
            schedule_interval_seconds=d.get("schedule_interval_seconds"),
        )

    def uses_lua(self) -> bool:
        """Whether any transform anywhere in the document is a `lua` op.

        Kept as a computed check rather than trusting the stored `has_script`
        flag, because that flag gates a stricter publication review and a flag
        that can drift from the thing it describes is worse than no flag.
        """

        def _has_lua(transforms: list[Transform] | None) -> bool:
            return any(t.op == "lua" for t in (transforms or []))

        if any(_has_lua(spec.transform) for spec in self.fields.values()):
            return True
        return any(
            _has_lua(cand.transform)
            for group in self.field_groups
            for candidates in group.bindings.values()
            for cand in candidates
        )


@dataclass(frozen=True)
class RunInput:
    """What a scheduled run supplies. `metadata` is addressable from the recipe
    as `{{meta.*}}` in step args, `template` transforms and predicate operands
    -- and nowhere else. It is never interpolated into a selector, an xpath, or
    Lua source: those are the three places a caller-supplied string would
    become executable."""

    url: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepOutcome:
    index: int
    op: str
    status: StepStatus
    duration_ms: int = 0
    label: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index, "op": self.op, "status": self.status,
            "duration_ms": self.duration_ms,
        }
        if self.label:
            out["label"] = self.label
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class AssertionResult:
    kind: str
    passed: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "passed": self.passed}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class RecipeRunResult:
    """`outcome` carries the distinction the whole operational model rests on:
    `blocked` is not `failed`. A challenge page has no product name, no price
    and no measurements, which is indistinguishable from "every selector broke"
    unless something says otherwise -- and a heal that runs against a bot wall
    rebuilds the recipe from the wall. See docs/recipe-operations.md D5.1."""

    outcome: RunOutcome = "ok"
    data: dict[str, Any] = field(default_factory=dict)
    field_status: dict[str, FieldStatus] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    truncated: dict[str, bool] = field(default_factory=dict)
    assertions: dict[str, list[AssertionResult]] = field(default_factory=dict)
    step_trace: list[StepOutcome] = field(default_factory=list)
    variant_id: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.outcome == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "data": self.data,
            "field_status": self.field_status,
            "provenance": self.provenance,
            "truncated": self.truncated,
            "assertions": {
                k: [a.to_dict() for a in v] for k, v in self.assertions.items()
            },
            "step_trace": [s.to_dict() for s in self.step_trace],
            "variant_id": self.variant_id,
            "error": self.error,
        }
