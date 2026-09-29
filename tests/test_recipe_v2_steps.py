"""Step compilation: target resolution, guards, retry, and the two gaps that
are refused loudly rather than fudged.

Both refusals exist for the same reason: acting on the wrong element is a wrong
answer that looks like a right one, and this contract's whole purpose is to
make that class of failure impossible to ship quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fusion_fixtures import fnode

from agentpilot.recipe.v2 import evaluate as ev
from agentpilot.recipe.v2 import steps as steps_mod
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.models import Locator, Predicate, RetryPolicy, Step
from agentpilot.recipe.v2.steps import StepContext, StepError, build_action, dispatch_step


@dataclass
class FakeResult:
    extracts: list[str] = field(default_factory=list)
    js_returns: list[Any] = field(default_factory=list)
    fused_trees: list[Any] = field(default_factory=list)
    readouts: list[str] = field(default_factory=list)


class Fake:
    def __init__(self, *, tree=None, js=None, fail_times=0) -> None:
        self.tree = tree
        self.js = js or {}
        self.fail_times = fail_times
        self.dispatched: list[Any] = []

    async def execute(self, session, actions, *, registry, driver) -> FakeResult:
        out = FakeResult()
        for a in actions:
            name = type(a).__name__
            if name == "SnapshotAction":
                out.fused_trees = [self.tree] if self.tree is not None else []
                continue
            if name == "ExecuteJsAction":
                out.js_returns.append(
                    next((v for k, v in self.js.items() if k in a.script), None)
                )
                continue
            if name == "ExtractAction":
                out.extracts.append("{}")
                continue
            if self.fail_times > 0:
                self.fail_times -= 1
                raise RuntimeError("transient driver error")
            self.dispatched.append(a)
        return out


@pytest.fixture
def ctx(monkeypatch):
    def _make(**kw) -> tuple[StepContext, Fake]:
        fake = Fake(**kw)
        for mod in (ev, steps_mod):
            monkeypatch.setattr(mod, "execute_on_session", fake.execute)
        reader = PageReader(session=object(), registry=object(), driver=object(),
                            base_url="https://x.test/p")
        return StepContext(session=object(), registry=object(), driver=object(),
                           reader=reader, meta={}), fake
    return _make


# --- compilation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_compiles_to_a_real_click_action_not_synthetic_js(ctx) -> None:
    """v1 compiled a css click into ExecuteJs + el.click(): an untrusted click
    with no auto-wait and no scroll-into-view."""

    c, _ = ctx()
    action = await build_action(
        Step(op="click", target=Locator(kind="css", selector="#go")), c
    )
    assert type(action).__name__ == "ClickAction"
    assert action.selector == "#go"


@pytest.mark.asyncio
async def test_within_is_folded_into_the_selector(ctx) -> None:
    """Silently dropping `within` for actions would reintroduce, on the write
    side, exactly the false positive `within` exists to fix on the read side."""

    c, _ = ctx()
    action = await build_action(Step(op="click", target=Locator(
        kind="css", selector="button.size",
        within=Locator(kind="css", selector=".size-guide"),
    )), c)
    assert action.selector == ".size-guide button.size"


@pytest.mark.asyncio
async def test_wait_for_selector_carries_state_and_timeout(ctx) -> None:
    c, _ = ctx()
    action = await build_action(Step(
        op="wait_for_selector", target=Locator(kind="css", selector=".drawer"),
        args={"state": "visible"}, timeout_ms=8000,
    ), c)
    assert (action.selector, action.state, action.timeout_ms) == (".drawer", "visible", 8000)


@pytest.mark.asyncio
async def test_step_timeout_falls_back_to_the_recipe_default(ctx) -> None:
    c, _ = ctx()
    c.defaults_timeout_ms = 4321
    action = await build_action(Step(
        op="wait_for_text", args={"text": "hi"},
    ), c)
    assert action.timeout_ms == 4321


@pytest.mark.asyncio
async def test_scroll_preserves_direction(ctx) -> None:
    """v1 discarded it and always dispatched `down`."""
    c, _ = ctx()
    action = await build_action(
        Step(op="scroll", args={"direction": "up", "pages": 2}), c
    )
    assert (action.direction, action.pages) == ("up", 2.0)


@pytest.mark.asyncio
async def test_ax_role_target_resolves_to_a_ref(ctx) -> None:
    tree = fnode(children=[fnode("button", "PRODUCT MEASUREMENTS", "e7")])
    c, _ = ctx(tree=tree)
    action = await build_action(Step(op="click", target=Locator(
        kind="ax_role", role="button", name_contains="MEASUREMENT")), c)
    assert action.ref == "e7"


@pytest.mark.asyncio
async def test_ax_role_target_supports_index(ctx) -> None:
    """The reason ax_role is the recommended option locator for a dom repeat."""

    tree = fnode(children=[fnode("button", s, f"e{i}") for i, s in enumerate("ABC")])
    c, _ = ctx(tree=tree)
    action = await build_action(Step(op="click", target=Locator(
        kind="ax_role", role="button", index=2)), c)
    assert action.ref == "e2"


@pytest.mark.asyncio
async def test_an_unmatched_target_is_a_step_error(ctx) -> None:
    c, _ = ctx(tree=fnode(children=[]))
    with pytest.raises(StepError, match="matched no element"):
        await build_action(Step(op="click", timeout_ms=50, target=Locator(
            kind="ax_role", role="button", name_contains="absent")), c)


class _LateTree:
    """Serves an empty page for the first `empty_polls` snapshots, then the
    button -- the shape of the cos.com replay failure."""

    def __init__(self, empty_polls: int) -> None:
        self.empty_polls = empty_polls
        self.snapshots = 0

    def install(self, fake: Fake) -> None:
        real = fake.execute

        async def execute(session, actions, *, registry, driver):
            if any(type(a).__name__ == "SnapshotAction" for a in actions):
                self.snapshots += 1
                fake.tree = (
                    fnode(children=[])
                    if self.snapshots <= self.empty_polls
                    else fnode(children=[fnode("button", "Details & Description", "e9")])
                )
            return await real(session, actions, registry=registry, driver=driver)

        ev.execute_on_session = execute  # type: ignore[assignment]
        steps_mod.execute_on_session = execute  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_a_target_that_renders_late_is_waited_for(ctx, monkeypatch) -> None:
    """MEASURED on cos.com in the Docker worker: straight after the country
    dialog closed, the snapshot held 0 "Details & Description" buttons and 2.5s
    later it held 1. Resolving once and giving up failed the click, so the
    specs drawer never opened. The resolver must re-snapshot until it appears."""

    monkeypatch.setattr(steps_mod, "_RESOLVE_POLL_S", 0)
    c, fake = ctx(tree=fnode(children=[]))
    late = _LateTree(empty_polls=3)
    late.install(fake)

    action = await build_action(Step(op="click", target=Locator(
        kind="ax_role", role="button", name_contains="Details & Description")), c)

    assert action.ref == "e9"
    assert late.snapshots == 4  # three stale reads, then a fresh one that matched


@pytest.mark.asyncio
async def test_an_optional_step_gives_up_within_its_cap(ctx, monkeypatch) -> None:
    """Optional steps are mostly legitimately-absent banners; each must not
    cost the full step timeout."""

    monkeypatch.setattr(steps_mod, "_RESOLVE_POLL_S", 0)
    monkeypatch.setattr(steps_mod, "_OPTIONAL_RESOLVE_WAIT_MS", 30)
    c, _ = ctx(tree=fnode(children=[]))
    step = Step(op="click", optional=True, timeout_ms=60_000, target=Locator(
        kind="ax_role", role="button", name_contains="ACCEPT ALL"))

    import time as _time

    started = _time.monotonic()
    with pytest.raises(StepError, match="matched no element"):
        await build_action(step, c)
    assert _time.monotonic() - started < 5  # the 30ms cap, not the 60s timeout


@pytest.mark.asyncio
async def test_metadata_is_templated_into_step_args(ctx) -> None:
    c, _ = ctx()
    c.meta = {"query": "midi dress"}
    action = await build_action(Step(op="find_text", args={"text": "{{meta.query}}"}), c)
    assert action.text == "midi dress"


# --- the two refusals -------------------------------------------------------


@pytest.mark.asyncio
async def test_an_xpath_action_target_is_refused_with_a_useful_message(ctx) -> None:
    """Reads route around the driver's querySelector-only resolution with
    generated JS; an action needs a real node, which the driver cannot give
    for an xpath yet."""

    c, _ = ctx()
    with pytest.raises(StepError, match="not yet dispatchable"):
        await build_action(Step(op="click", target=Locator(
            kind="xpath", selector="//tr[th='ASIN']/td")), c)


@pytest.mark.asyncio
async def test_a_css_target_with_an_index_is_refused_rather_than_hitting_the_first(
    ctx,
) -> None:
    """THE bug this test exists for: a dom repeat sets index per iteration. If
    build_action dropped it, every iteration would click option 0 and the
    recipe would return N identical rows -- a wrong answer wearing a right
    answer's clothes."""

    c, _ = ctx()
    with pytest.raises(StepError, match="index=2 is not dispatchable"):
        await build_action(Step(op="click", target=Locator(
            kind="css", selector="button.size", index=2)), c)


@pytest.mark.asyncio
async def test_a_non_css_within_on_a_css_target_is_refused(ctx) -> None:
    c, _ = ctx()
    with pytest.raises(StepError, match="only be scoped by a css"):
        await build_action(Step(op="click", target=Locator(
            kind="css", selector="button",
            within=Locator(kind="ax_role", role="group"))), c)


# --- dispatch policy --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unsatisfied_guard_skips_without_dispatching(ctx) -> None:
    c, fake = ctx(js={"querySelectorAll(opts.selector).length": 0})
    outcome = await dispatch_step(Step(
        op="click", target=Locator(kind="css", selector="#consent"),
        when=[Predicate(kind="selector_present", selector="#consent")],
    ), c)
    assert outcome.status == "skipped"
    assert fake.dispatched == []


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_and_reported_as_recovered(ctx) -> None:
    c, fake = ctx(fail_times=1)
    outcome = await dispatch_step(Step(
        op="click", target=Locator(kind="css", selector="#go"),
        retry=RetryPolicy(attempts=2, backoff_ms=0),
    ), c)
    assert outcome.status == "recovered"
    assert len(fake.dispatched) == 1


@pytest.mark.asyncio
async def test_exhausting_retries_reports_failed_with_the_reason(ctx) -> None:
    c, _ = ctx(fail_times=5)
    outcome = await dispatch_step(Step(
        op="click", target=Locator(kind="css", selector="#go"),
        retry=RetryPolicy(attempts=2, backoff_ms=0),
    ), c)
    assert outcome.status == "failed"
    assert "transient driver error" in outcome.reason


@pytest.mark.asyncio
async def test_a_failing_step_never_raises_out_of_dispatch(ctx) -> None:
    """The outcome carries the status; the caller applies on_error. Keeping the
    policy in one place beats spreading it across every op."""

    c, _ = ctx()
    outcome = await dispatch_step(Step(op="click", target=Locator(
        kind="xpath", selector="//x")), c)
    assert outcome.status == "failed"
    assert "not yet dispatchable" in outcome.reason


@pytest.mark.asyncio
async def test_a_mutating_step_invalidates_the_reader_cache(ctx) -> None:
    """Holding a snapshot across a click is how a recipe reads the state it was
    trying to change."""

    c, _ = ctx()
    await c.reader.structured_data()
    assert c.reader._structured is not None
    await dispatch_step(Step(op="click", target=Locator(kind="css", selector="#go")), c)
    assert c.reader._structured is None


@pytest.mark.asyncio
async def test_a_ref_that_goes_stale_before_the_click_is_re_resolved(ctx) -> None:
    """MEASURED on cos.com in the Docker worker: the button resolved, then the
    page re-rendered before the click landed and the driver raised
    `StaleRefError`. A fresh snapshot and a fresh ref is the whole answer."""

    from crawlpilot.spi.errors import StaleRefError

    tree = fnode(children=[fnode("button", "Details & Description", "e9")])
    c, fake = ctx(tree=tree)
    real = fake.execute
    stale_left = [1]

    async def execute(session, actions, *, registry, driver):
        if any(type(a).__name__ == "ClickAction" for a in actions) and stale_left[0]:
            stale_left[0] -= 1
            raise StaleRefError("ref 'e9' is not available -- the page may have changed")
        return await real(session, actions, registry=registry, driver=driver)

    ev.execute_on_session = execute  # type: ignore[assignment]
    steps_mod.execute_on_session = execute  # type: ignore[assignment]

    outcome = await dispatch_step(Step(op="click", target=Locator(
        kind="ax_role", role="button", name_contains="Details & Description")), c)

    assert outcome.status == "ok"
    assert [type(a).__name__ for a in fake.dispatched] == ["ClickAction"]
