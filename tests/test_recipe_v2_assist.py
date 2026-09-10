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
)
from agentpilot.recipe.v2.models import Candidate, FieldGroup, Locator, Recipe, TargetSpec
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


def test_an_ask_round_trips_through_json() -> None:
    ask = PendingAsk(field="price", kind="rejected", reason="wrong one", step_trace=[{"a": 1}])
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
    # A recording is mostly reveals, and a reveal that does not land is an empty
    # field rather than a failed run -- the rule every step here follows.
    assert all(s.optional and s.on_error == "continue" for s in steps)


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
    assert all(s.optional and s.on_error == "continue" for s in back)
