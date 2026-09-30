"""`agentpilot.recipe.v2.assist` -- the park/resume contract and what a
person's answer does to a recipe.

The store half runs against a real Postgres (same skip idiom as
`test_recipe_store.py`), because the whole mechanism rests on which queries do
and do not see a parked row -- and that is a property of the SQL, not of a
mock.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agentpilot.jobs.recipe_store import PostgresRecipeStore
from agentpilot.recipe.v2.assist import (
    PendingAsk,
    build_asks,
    drop_field,
    parse_recorded_steps,
    parse_resolutions,
    split_route,
)
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Recipe,
    RepeatSpec,
    Step,
    TargetSpec,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.validate import validate_document

# --- asks -------------------------------------------------------------------


def test_rejected_fields_are_asked_about_first() -> None:
    """A person shown a concrete wrong value and told what it actually is has a
    far easier question than "this was never found anywhere"."""

    asks = build_asks(
        {"sku": "not located during exploration"},
        {"name": "this is the breadcrumb trail"},
    )
    assert [(a.field, a.kind) for a in asks] == [
        ("name", "rejected"),
        ("sku", "unresolved"),
    ]


def test_a_field_both_unresolved_and_rejected_is_asked_once() -> None:
    asks = build_asks({"name": "never found"}, {"name": "wrong element"})
    assert [a.field for a in asks] == ["name"]
    assert asks[0].kind == "rejected"


def test_the_step_trace_travels_with_the_ask() -> None:
    """An empty field behind a reveal click is unattributable without it: the
    selector may be wrong, or the click may never have run, and the two need
    opposite fixes from the person."""

    trace = [{"op": "click", "status": "failed", "reason": "target matched no element"}]
    asks = build_asks({"price": "no locator resolved"}, {}, step_trace=trace)
    assert asks[0].step_trace == trace


def test_what_was_already_tried_travels_with_the_ask() -> None:
    """`reason` says what is wrong now; `tried` says what has been ruled out.

    A person told "read 'Specifications' but this field is a table" knows the
    last attempt landed on the heading. One told only "not found" has to
    rediscover that -- which is the same work the build already did.
    """

    asks = build_asks(
        {"specifications": "no locator resolved"},
        {},
        tried={"specifications": "rows: every one of the 10 rows came back identical"},
    )
    assert asks[0].tried == "rows: every one of the 10 rows came back identical"


def test_an_ask_with_nothing_tried_says_nothing_about_it() -> None:
    """Absent from the payload rather than an empty string: the panel renders a
    section for it, and an empty one reads as "nothing was tried"."""

    asks = build_asks({"price": "never found"}, {})
    assert asks[0].tried == ""
    assert "tried" not in asks[0].to_dict()


def test_an_ask_round_trips_through_json() -> None:
    ask = PendingAsk(
        field="price", kind="rejected", reason="wrong one",
        step_trace=[{"a": 1}], tried="propose: read nothing",
    )
    assert PendingAsk.from_dict(ask.to_dict()) == ask


# --- resolutions ------------------------------------------------------------


def _asks() -> list[PendingAsk]:
    return [PendingAsk(field="price", kind="unresolved", reason="x")]


def test_a_pick_needs_locators() -> None:
    """Accepting an empty pick would flip the run back to running and change
    nothing, which reads to the person as "my answer was ignored"."""

    assert parse_resolutions([{"field": "price", "action": "pick"}], _asks()) == {}
    got = parse_resolutions(
        [{"field": "price", "action": "pick",
          "locators": [{"kind": "css", "selector": ".price"}]}],
        _asks(),
    )
    assert got["price"].locators[0].selector == ".price"


def test_a_describe_needs_a_hint() -> None:
    assert parse_resolutions([{"field": "price", "action": "describe"}], _asks()) == {}
    got = parse_resolutions(
        [{"field": "price", "action": "describe", "hint": "inside the Details accordion"}],
        _asks(),
    )
    assert got["price"].hint == "inside the Details accordion"


def test_a_skip_needs_nothing() -> None:
    got = parse_resolutions([{"field": "price", "action": "skip"}], _asks())
    assert got["price"].action == "skip"


def test_an_answer_to_something_nobody_asked_is_dropped() -> None:
    got = parse_resolutions([{"field": "invented", "action": "skip"}], _asks())
    assert got == {}


def test_an_unknown_action_is_dropped() -> None:
    got = parse_resolutions([{"field": "price", "action": "teleport"}], _asks())
    assert got == {}


def test_an_accept_needs_nothing_but_the_field() -> None:
    """Overruling the judge. The binding is already on the recipe and is what
    produced the value the person just read, so there is nothing to carry."""

    got = parse_resolutions([{"field": "price", "action": "accept"}], _asks())
    assert got["price"].action == "accept"


def test_a_pick_carries_the_type_and_cleanup_the_picker_derived() -> None:
    """The browser knows what the locator cannot: that a link pick needs
    `url_resolve` or its URLs stay relative for ever. `_bind` never wrote it,
    and the panel never sent it."""

    got = parse_resolutions(
        [{
            "field": "price", "action": "pick",
            "locators": [{"kind": "css", "selector": "a.more", "attribute": "href"}],
            "spec": {"type": {"kind": "scalar", "value_type": "url"},
                     "transform": [{"op": "url_resolve"}]},
        }],
        _asks(),
    )
    assert got["price"].spec["type"]["value_type"] == "url"
    assert got["price"].spec["transform"] == [{"op": "url_resolve"}]


def test_a_pick_without_a_spec_is_still_a_pick() -> None:
    got = parse_resolutions(
        [{"field": "price", "action": "pick",
          "locators": [{"kind": "css", "selector": ".p"}]}],
        _asks(),
    )
    assert got["price"].spec == {}


def test_a_rejected_ask_carries_the_value_it_is_about() -> None:
    """The panel asks somebody to overrule the judge, which is unanswerable
    without showing them what it read."""

    asks = build_asks(
        {}, {"description": "carries import boilerplate"},
        collected={"description": "100% cotton. Imported from China."},
    )
    assert asks[0].value == "100% cotton. Imported from China."
    assert PendingAsk.from_dict(asks[0].to_dict()).value == asks[0].value


def test_only_a_rejected_ask_carries_a_value() -> None:
    """`unresolved` and `absent` are about a field that read nothing at all --
    a value there would be a different field's, or a lie."""

    asks = build_asks(
        {"price": "never found"}, {}, absent={"warranty": "not on this page"},
        collected={"price": "£20", "warranty": "x"},
    )
    assert all(a.value == "" for a in asks)


def test_a_malformed_locator_does_not_lose_the_good_ones() -> None:
    got = parse_resolutions(
        [{"field": "price", "action": "pick", "locators": [
            {"no_kind": True}, {"kind": "css", "selector": ".p"},
        ]}],
        _asks(),
    )
    assert [loc.selector for loc in got["price"].locators] == [".p"]


# --- skipping a field -------------------------------------------------------


def _recipe() -> Recipe:
    return Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={
            "name": FieldSpec(name="name", type=TypeSpec(kind="scalar")),
            "sku": FieldSpec(name="sku", type=TypeSpec(kind="scalar")),
        },
        field_groups=[
            FieldGroup(
                group_id="g0", field_names=["name", "sku"],
                bindings={
                    "name": [Candidate(locator=Locator(kind="css", selector="h1"))],
                    "sku": [Candidate(locator=Locator(kind="css", selector=".sku"))],
                },
            )
        ],
    )


def test_skipping_removes_the_field_and_its_binding() -> None:
    out = drop_field(_recipe(), "sku")
    assert "sku" not in out.fields
    assert "sku" not in out.field_groups[0].bindings
    assert out.field_groups[0].field_names == ["name"]


def test_skipping_the_last_field_of_a_group_removes_the_group() -> None:
    """`validate_document` rejects a group whose every field is unbound, so
    leaving an empty one behind produces a document that cannot be saved."""

    recipe = _recipe()
    recipe = drop_field(recipe, "sku")
    recipe = drop_field(recipe, "name")

    assert recipe.field_groups == []
    errors, _warnings = validate_document(recipe.to_dict())
    # No groups and no fields is its own (reported) problem, but critically not
    # a dangling-group one.
    assert not any("no candidates bound" in e for e in errors)


def test_the_document_still_validates_after_a_skip() -> None:
    recipe = drop_field(_recipe(), "sku")
    errors, _warnings = validate_document(recipe.to_dict())
    assert errors == []


def test_skipping_a_table_column_takes_the_table_when_it_was_the_last() -> None:
    recipe = Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={
            "variants": FieldSpec(
                name="variants", type=TypeSpec(kind="table", columns={"size": TypeSpec()})
            )
        },
        field_groups=[
            FieldGroup(
                group_id="g0", field_names=["variants"],
                bindings={"size": [Candidate(locator=Locator(kind="css", selector=".s"))]},
            )
        ],
    )
    out = drop_field(recipe, "size")
    assert out.field_groups == []
    assert "variants" not in out.fields


# --- the park, against a real database --------------------------------------

_DATABASE_URL = os.environ.get("AGENTPILOT_TEST_DATABASE_URL")


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


needs_db = pytest.mark.skipif(
    not _database_reachable(),
    reason="set AGENTPILOT_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresRecipeStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM recipes WHERE tenant LIKE 'assisttest-%'")
    await s.close()


async def _parked(store: PostgresRecipeStore):
    """A run claimed and then parked, plus its tenant and lock."""

    tenant = f"assisttest-{uuid.uuid4().hex[:8]}"
    recipe = await store.create_recipe(
        tenant=tenant, name="n", url_pattern="https://x.test", field_schema={},
        schedule_interval_seconds=None,
    )
    run_id = await store.queue_run(
        recipe_id=recipe.recipe_id, tenant=tenant, kind="onboard"
    )
    claimed = [c for c in await store.claim_runs_batch(10) if c.run_id == run_id]
    assert claimed, "the onboard run was not claimable"
    run = claimed[0]
    ok = await store.park_run(
        run.run_id, run.lock,
        pending_asks=[{"field": "price", "kind": "unresolved", "reason": "x"}],
        parked_until=datetime.now(UTC) + timedelta(minutes=15),
    )
    assert ok
    return tenant, recipe.recipe_id, run


@needs_db
async def test_a_parked_run_is_not_claimed_again(store: PostgresRecipeStore) -> None:
    _tenant, _recipe_id, run = await _parked(store)
    again = [c for c in await store.claim_runs_batch(10) if c.run_id == run.run_id]
    assert again == []


@needs_db
async def test_a_parked_run_is_not_reclaimed_as_stale(store: PostgresRecipeStore) -> None:
    """THE regression this status exists for. `reclaim_stale_runs` requeues any
    `running` row whose lock has gone quiet, and a run waiting on a person is
    quiet by definition -- it would be restarted from the top every
    `stale_after_seconds`, forever, and the answer would arrive for a run that
    no longer exists."""

    _tenant, _recipe_id, run = await _parked(store)

    await store.reclaim_stale_runs(0.0)  # everything older than "now" is stale

    row = await store.get_run(run.run_id, run.tenant)
    assert row is not None
    assert row.status == "needs_input"


@needs_db
async def test_the_heartbeat_keeps_renewing_while_parked(store: PostgresRecipeStore) -> None:
    """The worker holding this run is alive and still has its browser session
    open on the page the person is being asked about."""

    _tenant, _recipe_id, run = await _parked(store)
    assert await store.renew_lock(run.run_id, run.lock) is True


@needs_db
async def test_the_asks_are_readable_by_whoever_will_answer(
    store: PostgresRecipeStore,
) -> None:
    _tenant, _recipe_id, run = await _parked(store)
    row = await store.get_run(run.run_id, run.tenant)
    assert row is not None
    assert row.pending_asks == [{"field": "price", "kind": "unresolved", "reason": "x"}]


@needs_db
async def test_an_answer_resumes_the_run_and_reaches_the_worker(
    store: PostgresRecipeStore,
) -> None:
    tenant, _recipe_id, run = await _parked(store)

    assert await store.poll_assist(run.run_id, run.lock) is None

    accepted = await store.submit_assist(
        run.run_id, tenant, [{"field": "price", "action": "skip"}]
    )
    assert accepted is True

    got = await store.poll_assist(run.run_id, run.lock)
    assert got == [{"field": "price", "action": "skip"}]

    row = await store.get_run(run.run_id, tenant)
    assert row is not None and row.status == "running"
    # Cleared, so a later poll of the same row cannot re-apply them.
    assert row.pending_asks is None


@needs_db
async def test_answering_a_run_that_already_resumed_is_refused(
    store: PostgresRecipeStore,
) -> None:
    """Its park expired and it carried on without the answers. Reporting
    success for an update that changed nothing would leave the caller believing
    otherwise."""

    tenant, _recipe_id, run = await _parked(store)
    await store.resume_run(run.run_id, run.lock)

    accepted = await store.submit_assist(
        run.run_id, tenant, [{"field": "price", "action": "skip"}]
    )
    assert accepted is False


@needs_db
async def test_another_tenant_cannot_answer_your_run(store: PostgresRecipeStore) -> None:
    _tenant, _recipe_id, run = await _parked(store)
    assert await store.submit_assist(run.run_id, "someone-else", []) is False


@needs_db
async def test_a_park_whose_worker_died_is_failed_not_requeued(
    store: PostgresRecipeStore,
) -> None:
    """A live worker resumes its own park on time. Still parked well past the
    deadline means the process holding it is gone -- and with it the browser
    session the park existed to keep open, so re-queueing would silently
    restart the build from nothing."""

    tenant, _recipe_id, run = await _parked(store)
    async with store._pool.connection() as conn:
        await conn.execute(
            "UPDATE recipe_runs SET parked_until = now() - INTERVAL '1 hour' "
            "WHERE run_id = %s",
            (run.run_id,),
        )

    swept = await store.reclaim_expired_parks(grace_seconds=60.0)
    assert swept >= 1

    row = await store.get_run(run.run_id, tenant)
    assert row is not None
    assert row.status == "failed"
    assert "went away" in (row.error or "")


@needs_db
async def test_a_park_still_within_its_deadline_is_left_alone(
    store: PostgresRecipeStore,
) -> None:
    tenant, _recipe_id, run = await _parked(store)
    await store.reclaim_expired_parks(grace_seconds=60.0)
    row = await store.get_run(run.run_id, tenant)
    assert row is not None and row.status == "needs_input"


# --- pointing at a section rather than a value -------------------------------


def test_a_scope_needs_the_regions_locators() -> None:
    """Without them there is nothing to scope to, and binding unscoped would
    search the whole page -- which is what the person was avoiding."""

    assert parse_resolutions([{"field": "price", "action": "scope"}], _asks()) == {}


def test_a_scope_carries_its_shape_and_markup() -> None:
    got = parse_resolutions(
        [{
            "field": "price", "action": "scope",
            "locators": [{"kind": "css", "selector": "#specs"}],
            "shape": "map",
            "html": "<dl><dt>Brand</dt><dd>Dove</dd></dl>",
        }],
        _asks(),
    )
    assert got["price"].action == "scope"
    assert got["price"].shape == "map"
    assert "Brand" in got["price"].html


def test_an_unknown_shape_falls_back_to_a_single_value() -> None:
    """A shape decides the field's TypeSpec, so an unrecognised one must not
    reach `TypeSpec` and produce something unbindable."""

    got = parse_resolutions(
        [{
            "field": "price", "action": "scope",
            "locators": [{"kind": "css", "selector": "#specs"}],
            "shape": "constellation",
        }],
        _asks(),
    )
    assert got["price"].shape == "one"


# --- a route and a region, in one answer -------------------------------------


def test_a_scope_may_carry_the_route_that_reveals_it() -> None:
    """The Walmart specifications case, and the reason it used to dead-end.

    Answering it needs both halves -- open the accordion, then point at what
    appeared -- and neither action could carry the other: `scope` alone reloads
    into a shut accordion, `steps` alone throws the region away.
    """

    got = parse_resolutions(
        [{
            "field": "price", "action": "scope",
            "locators": [{"kind": "css", "selector": "#specs"}],
            "shape": "rows",
            "html": "<table><tr><th>Brand</th><td>Bodycology</td></tr></table>",
            "steps": [
                {"op": "click", "kind": "css", "selector": "#spec-header",
                 "text": "Specifications"},
            ],
        }],
        _asks(),
    )
    resolution = got["price"]
    assert resolution.action == "scope"
    assert resolution.locators[0].selector == "#specs"
    assert [s.op for s in resolution.steps] == ["click"]
    assert resolution.steps[0].target is not None
    assert resolution.steps[0].target.selector == "#spec-header"


def test_a_pick_may_carry_the_route_that_reveals_it() -> None:
    got = parse_resolutions(
        [{
            "field": "price", "action": "pick",
            "locators": [{"kind": "css", "selector": ".price"}],
            "steps": [{"op": "click", "kind": "css", "selector": "#more"}],
        }],
        _asks(),
    )
    assert got["price"].action == "pick"
    assert [s.op for s in got["price"].steps] == ["click"]


def test_a_missing_region_says_which_of_three_things_went_wrong() -> None:
    """Reporting "record what you clicked" for all three is what made the
    accordion case look unanswerable even after the person had answered it."""

    from agentpilot.recipe.v2.assist import Resolution, _why_the_region_is_missing

    bare = Resolution(field="specifications", action="scope")
    assert "Record whatever you clicked" in _why_the_region_is_missing(bare, [])

    step = parse_recorded_steps([{"op": "click", "kind": "css", "selector": "#s"}])
    routed = Resolution(field="specifications", action="scope", steps=step)

    # The route matched nothing, so it started from state only their session had.
    assert "record it again from the top" in _why_the_region_is_missing(routed, [])

    # The route ran and the region still is not there -- a different problem,
    # and telling them to re-record would send them round the same loop.
    ran = _why_the_region_is_missing(routed, [object()])
    assert "1 of 1 did something" in ran
    assert "still was not on the page" in ran


# --- a recorded route -------------------------------------------------------


def test_a_recording_becomes_replayable_steps() -> None:
    """The answer to "how do I get to it?", which pointing at an element cannot
    give. A field behind three clicks and a scroll has no selector describing
    the route."""

    steps = parse_recorded_steps([
        {"op": "click", "kind": "css", "selector": "#specs", "text": "Specifications"},
        {"op": "scroll"},
        {"op": "fill", "kind": "css", "selector": "#q", "text": "hello"},
        {"op": "press", "text": "Escape"},
        {"op": "select_option", "kind": "css", "selector": "#size", "text": "M"},
    ])

    assert [s.op for s in steps] == ["click", "scroll", "fill", "press", "select_option"]
    assert steps[0].target is not None and steps[0].target.selector == "#specs"
    assert steps[0].label == 'click "Specifications"'
    assert steps[2].args == {"text": "hello"}
    assert steps[3].args == {"key": "Escape"}
    assert steps[4].args == {"values": ["M"]}
    # No step here fails the run: a route is a best effort and a half-revealed
    # page can still yield most of its fields.
    assert all(s.on_error == "continue" for s in steps)
    # But only the ones that may legitimately be missing are allowed to go
    # quietly. Every step used to be `optional`, which made a reveal that
    # stopped working indistinguishable from one that was never needed -- it is
    # skipped, the field comes back empty, and `step_trace` has nothing to
    # attribute it to. Absent intent means `reveal`, which is what these are.
    assert [s.optional for s in steps] == [False, False, False, False, False]


def test_a_step_the_driver_could_never_dispatch_is_dropped() -> None:
    """`steps.py` resolves selectors with `querySelector` and has no XPath
    engine, so an xpath target passes `validate_document` and then fails on
    every single run. Dropping it here is the same gate the exploration capture
    applies."""

    steps = parse_recorded_steps([
        {"op": "click", "kind": "xpath", "selector": "//div[1]"},
        {"op": "click", "kind": "css", "selector": ".ok"},
    ])
    assert len(steps) == 1
    assert steps[0].target is not None and steps[0].target.selector == ".ok"


def test_only_a_dismissal_is_allowed_to_go_missing_quietly() -> None:
    """The distinction the whole intent vocabulary exists for.

    Every recorded step used to be `optional`, on the reasoning that a cookie
    banner which did not appear this time is not a failed run. That is right
    about the banner and wrong about the click that opens the section the field
    lives in: marked optional, a reveal that stops working is skipped, the
    field comes back empty, and `step_trace` has nothing to attribute it to --
    which is the exact failure `PendingAsk.step_trace` was added to diagnose.
    """

    steps = parse_recorded_steps([
        {"op": "click", "kind": "css", "selector": "#accept", "intent": "dismiss"},
        {"op": "click", "kind": "css", "selector": "#specs", "intent": "reveal"},
        {"op": "scroll", "intent": "incidental"},
    ])

    assert [s.optional for s in steps] == [True, False, True]
    # None of them fails the run: a route is a best effort, and a half-revealed
    # page still yields most of its fields.
    assert all(s.on_error == "continue" for s in steps)
    # The intent leads the label, because the label is what `step_trace` shows
    # the next person: "reveal: click ... -- no match" says what is wrong.
    assert steps[0].label.startswith("dismiss: ")
    assert not steps[1].label.startswith("reveal: ")


def test_an_unknown_intent_reads_as_reveal() -> None:
    """Absent or nonsense means `reveal`, which is what every step recorded
    before intents existed was in practice. A stored route re-parsed after a
    park has to come back meaning what it meant going in."""

    steps = parse_recorded_steps([
        {"op": "click", "kind": "css", "selector": "#a"},
        {"op": "click", "kind": "css", "selector": "#b", "intent": "teleport"},
    ])
    assert [s.optional for s in steps] == [False, False]


def test_a_settle_step_is_kept_and_needs_a_target() -> None:
    """A reveal that animates is open in the DOM long before it has finished
    moving. The person never noticed -- they were never going to beat the
    animation -- and replay is, so the route has to be able to say "wait"."""

    steps = parse_recorded_steps([
        {"op": "wait_for_selector", "kind": "css", "selector": "#drawer",
         "intent": "settle", "text": "Composition"},
        {"op": "wait", "ms": 400, "intent": "settle"},
        # No target, and `wait_for_selector` is meaningless without one.
        {"op": "wait_for_selector", "intent": "settle"},
    ])

    assert [s.op for s in steps] == ["wait_for_selector", "wait"]
    assert steps[0].target is not None and steps[0].target.selector == "#drawer"
    assert steps[1].args == {"ms": 400}


def test_a_wait_longer_than_the_cap_is_clamped() -> None:
    """A mistyped 60000 must not park every single run for a minute."""

    steps = parse_recorded_steps([{"op": "wait", "ms": 60_000, "intent": "settle"}])
    assert steps[0].args == {"ms": 5_000}


def test_a_route_is_cut_at_the_point_the_value_is_read() -> None:
    """A group runs its steps and THEN reads, so a trailing "close this" folded
    in with the setup would run before the binding and shut the value away."""

    before, after = split_route([
        {"op": "click", "selector": "#accept", "intent": "dismiss"},
        {"op": "click", "selector": "#specs", "intent": "reveal"},
        {"op": "select", "intent": "select", "selector": "table.specs"},
        {"op": "click", "selector": "#close", "intent": "dismiss"},
    ])

    assert [s["selector"] for s in before] == ["#accept", "#specs"]
    assert [s["selector"] for s in after] == ["#close"]
    # The marker itself is not a step -- `parse_recorded_steps` refuses it, so
    # a route cannot smuggle an op the driver has never heard of into a recipe.
    assert parse_recorded_steps([{"op": "select", "selector": "x"}]) == []


def test_a_route_with_no_selection_is_all_setup() -> None:
    """`action: 'steps'` -- they showed the way there and left the model to
    find the value. Nothing to cut on, so nothing becomes teardown."""

    before, after = split_route([{"op": "click", "selector": "#a"}])
    assert len(before) == 1
    assert after == []


def test_a_route_is_cut_at_the_LAST_selection() -> None:
    """Somebody who picks, reads the preview and picks again has corrected
    themselves -- and everything between the two attempts is still part of
    getting there, not tidying up after."""

    before, after = split_route([
        {"op": "select", "intent": "select", "selector": "first"},
        {"op": "click", "selector": "#wider", "intent": "reveal"},
        {"op": "select", "intent": "select", "selector": "second"},
        {"op": "click", "selector": "#close", "intent": "dismiss"},
    ])

    assert [s["selector"] for s in before] == ["first", "#wider"]
    assert [s["selector"] for s in after] == ["#close"]


def test_the_teardown_half_of_a_route_reaches_the_resolution() -> None:
    """End to end through `parse_resolutions`, because the split happening in
    `Resolution.from_dict` is what makes it reach `_with_steps` at all."""

    got = parse_resolutions(
        [{
            "field": "price", "action": "pick",
            "locators": [{"kind": "css", "selector": ".price"}],
            "steps": [
                {"op": "click", "kind": "css", "selector": "#specs", "intent": "reveal"},
                {"op": "select", "intent": "select"},
                {"op": "click", "kind": "css", "selector": "#close", "intent": "dismiss"},
            ],
        }],
        _asks(),
    )

    resolution = got["price"]
    assert [s.target.selector for s in resolution.steps] == ["#specs"]
    assert [s.target.selector for s in resolution.teardown] == ["#close"]
    assert resolution.teardown[0].optional is True


def test_a_field_group_round_trips_its_teardown() -> None:
    """It is stored, or the back half of a recorded route is thrown away the
    first time the document is serialised."""

    group = FieldGroup(
        group_id="g", field_names=["price"], bindings={},
        teardown=[Step(op="click", target=Locator(kind="css", selector="#close"))],
    )
    back = FieldGroup.from_dict(group.to_dict())
    assert back.teardown == group.teardown
    # Absent rather than empty when there is none, so an ordinary group's
    # document does not grow a key that says nothing.
    assert "teardown" not in FieldGroup(
        group_id="g", field_names=["price"], bindings={}
    ).to_dict()


def test_an_op_a_recording_cannot_produce_is_dropped() -> None:
    """Replay issues its own navigate, and an unknown op should not first be
    discovered inside a stored recipe."""

    assert parse_recorded_steps([
        {"op": "navigate", "selector": "#x"},
        {"op": "execute_js", "selector": "#x"},
        {"op": "nonsense"},
        "not even a dict",
    ]) == []


def test_an_op_that_needs_a_target_and_has_none_is_dropped() -> None:
    steps = parse_recorded_steps([
        {"op": "click"},
        {"op": "fill", "text": "x"},
        # These two are legitimately page-level.
        {"op": "press", "text": "Enter"},
        {"op": "scroll"},
    ])
    assert [s.op for s in steps] == ["press", "scroll"]


def test_a_recorded_answer_needs_steps_to_be_an_answer() -> None:
    assert parse_resolutions(
        [{"field": "specs", "action": "steps", "steps": []}],
        [PendingAsk(field="specs", kind="unresolved", reason="")],
    ) == {}

    got = parse_resolutions(
        [{"field": "specs", "action": "steps",
          "steps": [{"op": "click", "selector": "#s"}]}],
        [PendingAsk(field="specs", kind="unresolved", reason="")],
    )
    assert got["specs"].action == "steps"
    assert len(got["specs"].steps) == 1


def test_a_recording_survives_the_park_and_resume_round_trip() -> None:
    """The browser sends `{op, selector, text}`; the run parks; what comes back
    out of the store is `Step.to_dict()`. Both are the same recording, and a
    `fill` whose text lives in `args` by then must not come back empty."""

    sent = parse_recorded_steps([
        {"op": "click", "kind": "css", "selector": "#specs", "text": "Specifications"},
        {"op": "fill", "kind": "css", "selector": "#q", "text": "hello"},
        {"op": "press", "text": "Escape"},
    ])
    stored = [s.to_dict() for s in sent]
    back = parse_recorded_steps(stored)

    assert [s.op for s in back] == ["click", "fill", "press"]
    assert back[1].args == {"text": "hello"}
    assert back[2].args == {"key": "Escape"}
    assert back[0].target is not None and back[0].target.selector == "#specs"
    assert all(s.on_error == "continue" for s in back)
    # `Step` has no `intent` field -- it is an authoring concept, not a replay
    # one -- so the second pass cannot re-derive optionality and must carry it.
    # `Step.from_dict` is what preserves what the first pass decided, instead
    # of defaulting the whole route back to `reveal`.
    assert [s.optional for s in back] == [s.optional for s in sent]


# --- applying a pick ---------------------------------------------------------


def _pick_recipe() -> Recipe:
    return Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={"more": FieldSpec(name="more", type=TypeSpec(kind="scalar"))},
        field_groups=[
            FieldGroup(
                group_id="g0", field_names=["more"],
                bindings={"more": [Candidate(locator=Locator(kind="css", selector=".old"))]},
            )
        ],
    )


@pytest.fixture
def picked(monkeypatch):
    """`verify_locators` records the spec it was handed and accepts the pick."""

    from agentpilot.recipe.v2 import assist as assist_mod
    from agentpilot.recipe.v2.resolve import CandidateAttempt

    seen: dict[str, Any] = {}

    class _Reader:
        def __init__(self, **kwargs): ...
        async def read(self, locator): return "x"

    async def fake_verify(locators, *, verify, spec, ctx):
        seen["spec"] = spec
        return [CandidateAttempt(index=0, locator=locators[0], outcome="won")], None

    monkeypatch.setattr(assist_mod, "PageReader", _Reader)
    monkeypatch.setattr(assist_mod, "verify_locators", fake_verify)
    monkeypatch.setattr(assist_mod, "rank_candidates", lambda locs: [
        Candidate(locator=loc) for loc in locs
    ])
    return seen


async def _apply(recipe, raw):
    from agentpilot.recipe.v2.assist import apply_resolutions

    return await apply_resolutions(
        recipe,
        parse_resolutions(raw, [PendingAsk(field="more", kind="unresolved", reason="x")]),
        url="https://x.test/p", session=None, registry=None, driver=None, llm_config=None,
    )


async def test_a_picks_cleanup_reaches_the_field_before_it_is_verified(picked) -> None:
    """`verify_locators` transforms and THEN decides, so a cleanup applied after
    verification would have been judged against the value it exists to clean."""

    recipe, unsettled = await _apply(
        _pick_recipe(),
        [{
            "field": "more", "action": "pick",
            "locators": [{"kind": "css", "selector": "a.more", "attribute": "href"}],
            "spec": {"type": {"kind": "scalar", "value_type": "url"},
                     "transform": [{"op": "url_resolve"}]},
        }],
    )

    assert unsettled == {}
    # The spec handed to the verifier already carries the cleanup.
    assert picked["spec"].type.value_type == "url"
    assert [t.op for t in picked["spec"].transform] == ["url_resolve"]
    # And it is on the saved recipe, which is what replay reads.
    assert recipe.fields["more"].type.value_type == "url"
    assert [t.op for t in recipe.fields["more"].transform] == ["url_resolve"]
    assert recipe.field_groups[0].bindings["more"][0].locator.selector == "a.more"


async def test_a_pick_without_a_spec_leaves_the_declared_contract_alone(picked) -> None:
    """The contract is the caller's. An absent key means "unchanged", not
    "reset to the default"."""

    recipe, unsettled = await _apply(
        _pick_recipe(),
        [{"field": "more", "action": "pick",
          "locators": [{"kind": "css", "selector": ".new"}]}],
    )

    assert unsettled == {}
    assert recipe.fields["more"].type.value_type == "string"
    assert recipe.fields["more"].transform == []


async def test_a_pick_at_a_field_the_recipe_no_longer_has_says_so(picked) -> None:
    """`spec=None` used to go straight into `verify_locators`, which then
    degrades to raw-only checking -- so a pick at a renamed or table field bound
    silently and wrongly. The routed-pick path always guarded this."""

    recipe = _pick_recipe()
    recipe.fields = {}

    _out, unsettled = await _apply(
        recipe,
        [{"field": "more", "action": "pick",
          "locators": [{"kind": "css", "selector": ".new"}]}],
    )

    assert unsettled == {"more": "that field is not in this recipe any more"}
    assert "spec" not in picked, "it must not reach the verifier at all"


async def test_accepting_a_value_changes_nothing_and_settles_the_field(picked) -> None:
    recipe, unsettled = await _apply(
        _pick_recipe(), [{"field": "more", "action": "accept"}]
    )

    assert unsettled == {}
    # The binding that produced the value is exactly what it was.
    assert recipe.field_groups[0].bindings["more"][0].locator.selector == ".old"
    assert "more" in recipe.fields


async def test_a_refused_pick_does_not_retype_the_field(monkeypatch) -> None:
    """MEASURED on a Uniqlo build (run 21faca15). `product_specifications` was an
    open key->value map bound through `name`/`value`. The person picked a text
    block; the picker reported a scalar; the block cleaned up to nothing and the
    pick was refused -- but the scalar type had already been written onto the
    field. Its `name`/`value` bindings then no longer counted, `validate_document`
    said "no candidates bound", and the whole recipe was discarded.

    A refused pick must leave the field exactly as it was."""

    from agentpilot.recipe.v2 import assist as assist_mod
    from agentpilot.recipe.v2.assist import apply_resolutions

    class _Reader:
        def __init__(self, **kwargs): ...
        async def read(self, locator): return "- Sheer: Not Sheer- Fit: Oversized"

    async def refuse(locators, *, verify, spec, ctx):
        return [], 'read "- Sheer: Not Sheer- Fit: Oversized" but cleaning it up left nothing'

    monkeypatch.setattr(assist_mod, "PageReader", _Reader)
    monkeypatch.setattr(assist_mod, "verify_locators", refuse)

    open_map = TypeSpec(kind="object")
    recipe = Recipe(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={"specs": FieldSpec(name="specs", type=open_map)},
        field_groups=[
            FieldGroup(
                group_id="g-specs", field_names=["specs"],
                bindings={
                    "name": [Candidate(locator=Locator(kind="json_ld", path="name"))],
                    "value": [Candidate(locator=Locator(kind="json_ld", path="value"))],
                },
                repeat=RepeatSpec(
                    kind="json", row_field="specs",
                    rows_locator=Locator(kind="json_ld", path="[2].additionalProperty"),
                    max_iterations=20,
                ),
            )
        ],
    )
    assert validate_document(recipe.to_dict())[0] == []

    recipe, unsettled = await apply_resolutions(
        recipe,
        parse_resolutions(
            [{"field": "specs", "action": "pick",
              "locators": [{"kind": "css", "selector": "#product-description"}],
              "spec": {"type": {"kind": "scalar", "value_type": "string"}}}],
            [PendingAsk(field="specs", kind="rejected", reason="seller boilerplate")],
        ),
        url="https://x.test/p", session=None, registry=None, driver=None, llm_config=None,
    )

    assert "cleaning it up left nothing" in unsettled["specs"]
    assert recipe.fields["specs"].type == open_map
    assert validate_document(recipe.to_dict())[0] == []
