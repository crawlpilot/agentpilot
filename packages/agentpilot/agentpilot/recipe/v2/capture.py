"""Turning what the agent *did* into what a recipe should *replay*.

Three jobs, all of them structural and none of them an LLM call:

1. **Stabilise** a dispatched action into a re-resolvable `Step`.
   `crawlpilot.driver.ref_cache.RefCache` ties every `ref` to one page/session
   epoch, so a `ClickAction(ref="e17")` recorded during exploration means
   nothing against a fresh page load. The accessible role+name the snapshot
   already holds for that ref does generalise.

2. **Generalise** the one option the agent clicked (size "M") into a
   `RepeatSpec` covering the whole set (S/M/L/XL).

3. **Synthesise the wait** that has to follow a revealing action.

Ported from v1's `stabilize.py` and `generalize.py`. Two things change, and
both are v2 capabilities closing a v1 hole rather than rewrites for their own
sake:

**Dispatchability is checked here, not discovered at run time.** `steps.py`
refuses an xpath action target (the driver resolves selectors with
`querySelector`, not an XPath engine) and a css action target carrying an
`index` (the actions take a selector or a ref, with no notion of the nth
match). A step violating either passes `validate_document` and then fails on
every single run. Capturing one is the bug; `dispatchability_error` is the
gate.

**The option set is scoped.** v1 matched `name_in` against the whole tree and
its own docstring called the resulting false positive an accepted risk. It is
not hypothetical: a size guide rendering `XS`, `S`, `M` inside a drawer while
the page behind it renders the same labels. v2's `Locator.within` exists for
exactly this, so when the option set's parent is itself addressable the repeat
is scoped to it.
"""

from __future__ import annotations

import re
from dataclasses import replace

from typing import Any

from agentpilot.recipe.v2.models import (
    Candidate,
    Locator,
    Predicate,
    RepeatSpec,
    Step,
    StepOp,
)
from agentpilot.recipe.v2.tree import find_node, find_nodes, find_parent, node_ref
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode

_MIN_SIBLINGS_TO_GENERALIZE = 2

# Ops whose effect the page renders *after* the driver returns. The driver
# reports a click as done once it is dispatched, so a group that reads
# immediately afterwards reads the page as it was before the drawer opened.
#
# MUST match `REVEALING_OPS` in `frontend/src/lib/recipe/fromPick.ts`. The two
# halves author the same recipes and a step that only one of them thinks needs
# a wait is a race that reproduces on one authoring path and not the other --
# `tests/test_recipe_v2_capture.py` pins them against each other.
REVEALING_OPS: frozenset[StepOp] = frozenset({
    "click", "double_click", "hover", "press", "send_keys", "select_option",
    "check", "uncheck", "scroll", "scroll_into_view", "tap", "swipe", "drag",
    "find_text", "new_tab", "switch_tab", "close_tab",
})

# The subset of `REVEALING_OPS` that can UNDO itself when run twice.
#
# A group's route is the whole way from a cold page, so replaying two groups on
# one page load replays the same accordion click twice -- and a second click on
# a toggle shuts what the first opened. That is the entire reason
# `guard_reveals` exists, and it is a property of toggles, not of reveals.
#
# A scroll is idempotent. So is `scroll_into_view`, `hover`, `find_text`, and
# switching to a tab that is already focused. Guarding those buys nothing and
# costs the whole field when the guard misreads -- a skipped step is silent
# (`run_steps` skips rather than fails), so it surfaces only as a field that
# came back empty.
TOGGLING_OPS: frozenset[StepOp] = frozenset({
    "click", "double_click", "tap", "check", "uncheck", "select_option",
})

# How long a reveal gets to appear before the group reads without it. Mirrors
# `REVEAL_TIMEOUT_MS`.
REVEAL_TIMEOUT_MS = 5_000

_ACTION_BUILDERS: dict[str, Any] = {
    "ClickAction": lambda d: spi_actions.ClickAction(ref=d["ref"]),
    "FillAction": lambda d: spi_actions.FillAction(ref=d["ref"], text=d["text"]),
    "HoverAction": lambda d: spi_actions.HoverAction(ref=d["ref"]),
    "SelectOptionAction": lambda d: spi_actions.SelectOptionAction(
        ref=d["ref"], values=d.get("values", [])
    ),
    "ScrollAction": lambda d: spi_actions.ScrollAction(
        direction=d["direction"], ref=d.get("ref")
    ),
    "WaitAction": lambda d: spi_actions.WaitAction(ms=d.get("ms")),
    "PressAction": lambda d: spi_actions.PressAction(key=d["key"]),
}


def dispatchability_error(target: Locator | None, op: StepOp) -> str | None:
    """Why `steps.py` would refuse to dispatch this target, or None.

    Kept as a returned reason rather than a raise: the caller drops the step and
    carries on building, and the reason is worth logging. See the module
    docstring for why this is checked at capture rather than left to run time.
    """

    if target is None:
        return None
    if target.kind == "xpath":
        return (
            f"{op}: xpath targets are not dispatchable -- the driver resolves "
            "selectors with querySelector, not an XPath engine"
        )
    if target.kind == "css" and target.index is not None:
        return (
            f"{op}: a css target with index={target.index} is not dispatchable -- "
            "the driver's actions take a selector or a ref, not an nth match"
        )
    if target.kind == "css" and target.within is not None and target.within.kind != "css":
        # `_compose_css` folds a css `within` into the selector and raises for
        # any other kind rather than silently dropping the scope.
        return f"{op}: a css target can only be scoped by a css `within`"
    return None


def stabilize_action(
    action: spi_actions.Action, snapshot: EnhancedDOMTreeNode
) -> Step | None:
    """One dispatched action as a replayable `Step`, or None.

    None for actions that are not reveal steps at all (navigate, go_back,
    extract, screenshot, tab management -- replay issues its own navigate and
    never needs the rest), for a ref that cannot be resolved to a stable
    descriptor, and for a target `steps.py` would refuse.
    """

    ref = getattr(action, "ref", None)
    target: Locator | None = None
    if ref is not None:
        node = find_node(snapshot, ref)
        if node is None or not node.ax_name:
            return None
        target = Locator(kind="ax_role", role=node.ax_role, name_contains=node.ax_name)

    step = _to_step(action, target)
    if step is None:
        return None
    if dispatchability_error(step.target, step.op) is not None:
        return None
    return step


def _to_step(action: spi_actions.Action, target: Locator | None) -> Step | None:
    """One action as a step, always `optional` with `on_error: continue`.

    **Not the dataclass defaults, which are `on_error="fail"`.** Those were what
    these steps carried, and it contradicted the rule the rest of this system
    states and follows: `_route_for` says "every step is `optional`/`on_error:
    continue`, so a redundant one costs seconds where a missing one costs the
    field", and `dismiss_step_for` and `wait_step_for` both honour it. Only the
    steps captured from the agent did not, and the difference is not small --
    `replay._replay_group` treats a failed non-optional step as fatal for the
    WHOLE group, so a single click whose control has since moved took every
    field in that group down with it rather than costing its own reveal.

    Observed on a Zara product page: a group's route ended with a captured
    click on a "close" control, that control was not there on the next run, and
    the group returned nothing at all -- not a degraded read, nothing.
    """

    common = {"on_error": "continue", "optional": True}
    if isinstance(action, spi_actions.ClickAction):
        return Step(op="click", target=target, **common)
    if isinstance(action, spi_actions.FillAction):
        return Step(op="fill", target=target, args={"text": action.text}, **common)
    if isinstance(action, spi_actions.HoverAction):
        return Step(op="hover", target=target, **common)
    if isinstance(action, spi_actions.SelectOptionAction):
        return Step(
            op="select_option", target=target, args={"values": list(action.values)}, **common
        )
    if isinstance(action, spi_actions.ScrollAction):
        return Step(op="scroll", target=target, args={"direction": action.direction}, **common)
    if isinstance(action, spi_actions.WaitAction):
        return Step(op="wait", args={"ms": action.ms} if action.ms else {}, **common)
    if isinstance(action, spi_actions.PressAction):
        return Step(op="press", args={"key": action.key}, **common)
    return None


def stabilize_action_dict(
    action_dict: dict[str, Any], snapshot: EnhancedDOMTreeNode
) -> Step | None:
    """`AgentStepRecord.actions` stores dispatched actions as plain
    `{"type": <ClassName>, **fields}` dicts (`agent/loop.py::_action_to_dict`),
    not the original dataclasses -- this is the adapter the exploration hook
    uses, since that is all it receives."""

    builder = _ACTION_BUILDERS.get(action_dict.get("type", ""))
    if builder is None:
        return None
    try:
        action = builder(action_dict)
    except KeyError:
        return None
    return stabilize_action(action, snapshot)


def document_scoped_selector(
    *, candidates: list[Candidate], repeat: RepeatSpec | None
) -> str | None:
    """The document-scoped CSS a group's value lives at, if it has one.

    A locator is stored relative to its scope -- a table's rows sit inside a
    container -- while a step target resolves against the document, so the two
    are composed here rather than one being passed where the other is meant.

    Mirrors `documentScopedSelector` in `fromPick.ts`.
    """

    rows = repeat.rows_locator if repeat is not None else None
    if rows is not None:
        # For a table, what appears is the container the rows live in. Waiting
        # on the container rather than a row is deliberate: a drawer can render
        # its list element before it has any children.
        if rows.within is not None and rows.within.kind == "css" and rows.within.selector:
            return rows.within.selector
        return rows.selector if rows.kind == "css" else None

    for candidate in candidates:
        locator = candidate.locator
        if locator.kind != "css" or not locator.selector:
            continue
        within = locator.within
        if within is not None and within.kind == "css" and within.selector:
            return f"{within.selector} {locator.selector}"
        return locator.selector
    return None


def wait_step_for(
    *, candidates: list[Candidate], repeat: RepeatSpec | None
) -> Step | None:
    """The wait that has to follow a revealing action, or None when the group
    has no css locator to wait on.

    This is the whole of the engine's answer to "the click has not landed yet".
    There is deliberately no implicit settle in `replay.py`: a quiet-period
    heuristic fails on any page with a carousel or a countdown, and the author
    -- here, the agent -- is the one who knows what the click was meant to
    reveal. `on_error: continue` and `optional` because a reveal that does not
    render is a field that comes back empty, not a run that fails.

    **The state is chosen from what the field actually reads**, and asking for
    the wrong one cost five seconds a group, every run. `attribute: "text"` is
    `textContent`, which includes content that is in the DOM and merely
    unpainted -- a collapsed accordion. `docs/recipe-contract-v2.md` §2 says
    what happens when such a field is made to wait for `visible`:

        A `wait_for_selector` with `state: "visible"` on a collapsed section
        will time out even though the data is right there. That is not a bug;
        it is the two meanings of "present" diverging.

    Because this step is `optional` with `on_error: continue`, that timeout
    never failed anything -- it silently burned `REVEAL_TIMEOUT_MS` and taught
    nobody. So only a field that genuinely reads what a user can see
    (`visible_text`) waits for `visible`; everything else waits for `attached`,
    which is the condition a `textContent` read actually needs.

    Mirrors `waitStepFor` in `fromPick.ts`.
    """

    selector = document_scoped_selector(candidates=candidates, repeat=repeat)
    if not selector:
        return None
    return Step(
        op="wait_for_selector",
        target=Locator(kind="css", selector=selector),
        args={"state": wait_state_for(candidates)},
        timeout_ms=REVEAL_TIMEOUT_MS,
        on_error="continue",
        optional=True,
        label="wait for the reveal to render",
    )


def wait_state_for(candidates: list[Candidate]) -> str:
    """`visible` only when a candidate reads what a user can actually see.

    Separate from `wait_step_for` so the rule is testable on its own and so
    `fromPick.ts` has one named thing to mirror rather than an inline
    conditional. See `wait_step_for` for why the default flipped.
    """

    return (
        "visible"
        if any(c.locator.attribute == "visible_text" for c in candidates)
        else "attached"
    )


def dismiss_step_for(overlay: dict[str, Any]) -> Step | None:
    """The step that closes a modal a reveal left open, or None.

    A dialog opened to expose one field covers everything under it and
    scroll-locks the document, so the next click and every later scroll act
    against a page that will not accept them. During exploration that costs the
    agent its remaining steps; inside a merged group at replay it costs the
    fields behind it. Nothing about the step that opened it says it opened a
    dialog rather than an accordion -- `PageReader.overlay` asks the page
    instead.

    Escape when there is no close control, because a great many dialogs answer
    it and the alternative is no step at all. Always `optional` and
    `on_error: continue`: a dialog that did not appear this time is not a failed
    run, which is the rule every reveal step here already follows.
    """

    if not overlay.get("open") and not overlay.get("locked"):
        return None

    close = overlay.get("close")
    if isinstance(close, str) and close:
        label = overlay.get("label") or "close"
        return Step(
            op="click",
            target=Locator(kind="css", selector=close),
            on_error="continue",
            optional=True,
            label=f"close the dialog ({label})",
        )
    return Step(
        op="press",
        args={"key": "Escape"},
        on_error="continue",
        optional=True,
        label="close the dialog",
    )


#: How a step added by `dismiss_step_for` announces itself, so a route can be
#: told from the tidying that happened to be interleaved with it.
DISMISS_LABEL_PREFIX = "close the dialog"

#: What a control is called when its job is to make something go away. Matched
#: against an `ax_role` target's `name_contains`, which is the agent's own
#: click, and deliberately narrow: mislabelling a reveal as cleanup drops it
#: from the route and costs the field, which is the expensive direction.
_CLEANUP_NAMES = ("close", "dismiss", "cerrar", "×", "✕")


# Attribute values that mark a control as a dismissal on a CSS target, where
# there is no accessible name to read. Ported from the recorder's `DISMISS_ATTR`
# so the two halves agree on what a close button looks like.
_CLEANUP_ATTR_RE = re.compile(
    r"\[\s*(?:data-dismiss|data-bs-dismiss|data-close)\b"
    r"|\[\s*(?:aria-label|title|data-testid|data-qa|id|class)\s*[~|^$*]?=\s*"
    r"[\"']?[^\"'\]]*\b(?:close|dismiss|reject|decline)\b",
    re.IGNORECASE,
)


def is_cleanup(step: Step) -> bool:
    """Whether this step's job is to put something away rather than reveal it."""

    if step.label and step.label.startswith(DISMISS_LABEL_PREFIX):
        return True
    if step.op == "press" and str(step.args.get("key", "")).lower() == "escape":
        return True
    if step.op != "click" or step.target is None:
        return False
    name = (step.target.name_contains or "").strip().casefold()
    if name:
        # Whole-name only. "Close" is cleanup; "Close fit" is a product
        # attribute, and a substring test would drop the click that reveals it.
        return name in _CLEANUP_NAMES
    # No accessible name means a CSS target, and those used to be invisible
    # here: the function returned False for every one of them. That is backwards
    # -- `dismiss_step_for` builds its steps from `PageReader.overlay()`, which
    # yields CSS, so the dismissals this exists to catch were exactly the ones it
    # could not see.
    selector = step.target.selector or ""
    return bool(selector) and bool(_CLEANUP_ATTR_RE.search(selector))


def guard_reveals(steps: list[Step], revealed: str | None) -> list[Step]:
    """The same route, with each TOGGLE refusing to fire on an open section.

    What makes a group's route safe to replay when the page is no longer fresh.
    A group's steps are the whole way from a cold page, so running two groups on
    one page load replays the same accordion click twice -- and a second click
    on a toggle closes what the first opened. Reloading between groups is how
    that used to be avoided, at O(groups) page loads.

    **The guard is the control's own declared state where it has one, and
    nothing at all where it does not.** Both halves of that are corrections.

    It used to guard on `hidden(<the field's read selector>)`. Those are
    different elements: the read selector points at the CONTENT, and the thing
    being clicked is the CONTROL. Whenever the content's element is already
    painted while the section is shut -- an accordion header, a wrapper div, a
    selector broad enough to include the collapsed region -- `hidden` is false,
    the guard fails, and the click is **skipped**. The section never opens and
    every field in the group reads an unrevealed page. `run_steps` skips rather
    than fails, so the only symptom is an empty field.

    `aria-expanded="false"` is what "still shut" actually means, and it is the
    page's own answer rather than an inference from paint. When the control does
    not declare one, no guard is emitted: getting the reveal to run matters more
    than avoiding a double-toggle, because a double-toggle costs one group's
    fields once while a skipped reveal costs them on every run, and `teardown`
    already exists to put the page back.

    Only `TOGGLING_OPS` are guarded. A scroll cannot undo itself, and guarding
    one was pure risk.
    """

    return [_guarded(step, revealed) for step in steps]


def _guarded(step: Step, revealed: str | None) -> Step:
    """A toggle guarded on its own state, or not guarded at all.

    **There is deliberately no fallback to the content selector.** That fallback
    is what this function used to do unconditionally, and it is worse than no
    guard, because `revealed` is `document_scoped_selector` over the group's
    WHOLE binding set -- whichever field happens to iterate first, which is
    rarely the field the reveal was for.

    MEASURED on the Zara shirt page. The group bound ten fields, and the first
    css candidate belonged to `images`, so all three reveal clicks were guarded
    on `hidden("button.product-detail-image img")`. Product images are visible,
    so `hidden` was false, and the replay's step trace reads:

        0 click  skipped  guard hidden not satisfied
        1 click  skipped  guard hidden not satisfied
        2 click  skipped  guard hidden not satisfied

    The drawer never opened and `care` and `origin` failed with no provenance at
    all. A guard that cannot be tied to the thing being revealed does not
    protect the reveal; it deletes it.
    """

    del revealed  # see the docstring: never guard on another field's selector
    if step.op not in TOGGLING_OPS or step.when:
        return step
    guard = _toggle_guard(step)
    return replace(step, when=[guard]) if guard is not None else step


def _toggle_guard(step: Step) -> Predicate | None:
    """`still shut`, as the control itself declares it.

    Only for a css target: the predicate is evaluated with `querySelector`
    (`PageReader._holds`), so an `ax_role` target has nothing to hand it. The
    selector is narrowed with `[aria-expanded="false"]` rather than replaced, so
    it still identifies the same control.
    """

    target = step.target
    if target is None or target.kind != "css" or not target.selector:
        return None
    if "aria-expanded" in target.selector:
        return None
    return Predicate(
        kind="selector_present",
        selector=f'{target.selector}[aria-expanded="false"]',
    )


def content_guard(revealed: str | None) -> Predicate | None:
    """`still shut`, measured on the content rather than on the control.

    Not used by `guard_reveals` -- see `_guarded` for the measurement that
    removed it from that path. Kept because it is the right guard when the
    caller knows the selector belongs to the field this reveal is *for*, which
    `guard_reveals` does not: `hidden` rather than `selector_absent`, because a
    collapsed panel's content is in the DOM and merely unpainted, so only the
    paint test can tell open from shut.
    """

    return Predicate(kind="hidden", selector=revealed) if revealed else None


def split_cleanup(steps: list[Step]) -> tuple[list[Step], list[Step]]:
    """A route, cut into how it opens a section and how it closes it again.

    A group runs its steps and *then* reads, so a route that ends by closing
    something cannot be what put the field on screen -- it is what took it off.
    `_route_for` hands out the whole path since the page loaded, which is right
    for reveals and wrong here: a dismissal dispatched mid-exploration
    (`_close_any_dialog`) and the agent's own clicks on close controls both land
    in that path, so every group frozen afterwards inherited them.

    Observed on a Zara product page. The `origin` group's route read *open
    COMPOSITION, CARE & ORIGIN → wait → close the dialog → close → wait for the
    composition panel to be visible* — it opened the drawer, shut it twice, and
    then waited for content inside it. The binding verified anyway, because the
    panel's text stays in the DOM once rendered and the reader walks
    `textContent`, so nothing objected until the recipe ran.

    Only TRAILING cleanup is teardown. A dismissal in the middle was followed by
    a reveal that must have worked from the closed state, so it is load-bearing
    setup and stays where it is.

    This used to discard the trailing half, which left every recipe knowing how
    to open a section and not how to close it -- and closing it is the whole of
    what lets the next group share the page rather than reload it.

    **Waits do not anchor the split**, and that is what made the route quoted
    above split wrongly. It ends with a `wait_for_selector`, so the scan stopped
    on the very first step it looked at and *nothing* moved to teardown: the two
    closes stayed in `steps`, and the group opened the drawer, shut it twice and
    then read. It verified anyway -- the panel's text stays in the DOM once
    rendered and the reader walks `textContent` -- so the docstring above could
    describe this as fixed while the case it cites still failed.

    A wait is neither setup nor cleanup; it belongs to whatever it follows. So
    trailing waits are walked over while looking for the boundary, and any wait
    that ends up in the teardown half is dropped -- waiting for a panel to
    appear is not part of closing it.
    """

    end = len(steps)
    while end > 0 and (is_cleanup(steps[end - 1]) or _is_wait(steps[end - 1])):
        end -= 1
    # Everything from `end` on is the trailing cleanup, minus the waits that
    # were only walked over to reach it.
    teardown = [s for s in steps[end:] if not _is_wait(s)]
    if not teardown:
        # Nothing to put away, so the waits were ordinary route steps and the
        # scan should not have moved past them.
        return list(steps), []
    # A wait that sat *before* the first cleanup step is still part of setup.
    keep = end
    while keep < len(steps) and _is_wait(steps[keep]):
        keep += 1
    return steps[:keep], [s for s in steps[keep:] if not _is_wait(s)]


def _is_wait(step: Step) -> bool:
    return step.op in ("wait", "wait_for_selector", "wait_for_text", "wait_for_load")


def generalize_option_locator(
    *,
    snapshot: EnhancedDOMTreeNode,
    clicked_ref: str,
    row_field: str,
    max_iterations: int,
) -> RepeatSpec | None:
    """The clicked option generalised to its whole sibling set.

    Returns None when that is not possible -- no resolvable parent, or fewer
    than two same-role siblings -- and the caller falls back to a
    single-iteration repeat rather than failing the build.
    """

    clicked = find_node(snapshot, clicked_ref)
    if clicked is None or not clicked.ax_name:
        return None
    parent = find_parent(snapshot, clicked_ref)
    if parent is None:
        return None

    siblings = [c for c in parent.children if c.ax_role == clicked.ax_role and c.ax_name]
    if len(siblings) < _MIN_SIBLINGS_TO_GENERALIZE:
        return None

    names = [str(s.ax_name) for s in siblings]
    # Scope to the parent when it is itself addressable. This is the v1
    # false-positive fix: without it, `name_in` matches the whole tree, so a
    # size guide rendering the same labels in a drawer is indistinguishable
    # from the real option set.
    within = (
        Locator(kind="ax_role", role=parent.ax_role, name_contains=str(parent.ax_name))
        if parent.ax_role and parent.ax_name
        else None
    )
    locator = Locator(
        kind="ax_role", role=clicked.ax_role, name_in=names, within=within
    )

    # Mechanical verify: does this locator resolve back to at least the same
    # sibling count against THIS snapshot? Under-matching means the descriptor
    # is broken, and shipping a repeat that reads fewer rows than the page has
    # is a wrong answer that looks like a right one.
    if len(find_nodes(snapshot, locator)) < len(siblings):
        return None

    return RepeatSpec(
        kind="dom",
        row_field=row_field,
        max_iterations=max_iterations,
        option_locator=locator,
    )


def single_option_fallback(
    *, snapshot: EnhancedDOMTreeNode, clicked_ref: str, row_field: str
) -> RepeatSpec | None:
    """When generalisation fails, cover just the one option already found
    rather than dropping the field -- a partial result a later heal can
    improve on."""

    clicked = find_node(snapshot, clicked_ref)
    if clicked is None or not clicked.ax_name:
        return None
    return RepeatSpec(
        kind="dom",
        row_field=row_field,
        max_iterations=1,
        option_locator=Locator(
            kind="ax_role", role=clicked.ax_role, name_in=[str(clicked.ax_name)]
        ),
    )


def last_click_ref(actions: list[dict[str, Any]]) -> str | None:
    """The ref of the last click in a dispatched batch -- the representative
    option click, when that batch satisfied a table field."""

    for action_dict in reversed(actions):
        if action_dict.get("type") == "ClickAction":
            ref = action_dict.get("ref")
            if isinstance(ref, str):
                return ref
    return None


__all__ = [
    "REVEALING_OPS",
    "REVEAL_TIMEOUT_MS",
    "dismiss_step_for",
    "dispatchability_error",
    "document_scoped_selector",
    "generalize_option_locator",
    "last_click_ref",
    "node_ref",
    "single_option_fallback",
    "stabilize_action",
    "stabilize_action_dict",
    "wait_step_for",
]
