"""Deterministic recipe replay -- no LLM call, ever.

This is the path that runs on every scheduled collection, so its cost and its
honesty are the two things that matter. The execution order is normative and
lives in docs/recipe-contract-v2.md section 11; the two parts worth restating
here are the ones that look like inefficiency until you know why:

**It re-navigates before every field group.** State left by one group must not
corrupt the next. The concrete failure that motivates it was observed rather
than theorised: on a Zara product page, clicking *COMPOSITION, CARE & ORIGIN*
while the *PRODUCT MEASUREMENTS* drawer is open times out, because the open
drawer covers the other button. Two reveal steps that each work perfectly in
isolation break in sequence. The cost is O(groups) page loads, which is real
and is why merging provably read-only groups is a worthwhile future
optimisation -- an executor concern, not a contract change.

**It classifies the page before reading any field.** A challenge page has no
product name, no price and no measurements, which is indistinguishable from
"every selector broke". `outcome="blocked"` is what stops a heal from
rebuilding a working recipe against a CAPTCHA. See `classify.py`.
"""

from __future__ import annotations

from typing import Any

from agentpilot.recipe.v2.classify import classify_current_page
from agentpilot.recipe.v2.evaluate import PageReader, detect_variant
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Recipe,
    RecipeRunResult,
    RepeatSpec,
    RunInput,
    StepOutcome,
)
from agentpilot.recipe.v2.paths import resolve_path
from agentpilot.recipe.v2.resolve import (
    FieldResolution,
    apply_assertions_to_status,
    evaluate_assertions,
    resolve_field,
)
from agentpilot.recipe.v2.schema import FieldSpec
from agentpilot.recipe.v2.steps import StepContext, run_steps
from agentpilot.recipe.v2.transform import TransformContext, TransformError, apply_transforms
from agentpilot.recipe.v2.urlmatch import target_accepts
from crawlpilot.session.interactive import InteractiveSession, execute_on_session
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.driver import BrowserDriver

_STRUCTURED = frozenset({"json_ld", "hydration", "meta"})


async def replay_recipe(
    recipe: Recipe,
    run_input: RunInput,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> RecipeRunResult:
    result = RecipeRunResult()

    if not target_accepts(recipe.target, run_input.url):
        result.outcome = "failed"
        result.error = f"url does not match this recipe's target: {run_input.url}"
        return result

    reader = PageReader(
        session=session, registry=registry, driver=driver, base_url=run_input.url
    )
    ctx = StepContext(
        session=session, registry=registry, driver=driver, reader=reader,
        meta=run_input.metadata,
        defaults_timeout_ms=recipe.defaults.step_timeout_ms,
    )

    await _navigate(recipe, run_input, session=session, registry=registry, driver=driver)
    reader.invalidate()

    verdict = await classify_current_page(
        session=session, registry=registry, driver=driver, requested_url=run_input.url
    )
    if verdict.blocked:
        result.outcome = "blocked"
        result.error = verdict.reason
        return result

    setup_trace, setup_policy = await run_steps(recipe.global_setup, ctx)
    result.step_trace.extend(setup_trace)
    if setup_policy == "fail":
        result.outcome = "failed"
        result.error = "global_setup failed"
        return result

    result.variant_id = await detect_variant(
        recipe.variants, reader, meta=run_input.metadata
    )

    for group in recipe.field_groups:
        await _replay_group(
            group, recipe, run_input, result,
            session=session, registry=registry, driver=driver,
        )

    _finalize(result, recipe)
    return result


async def _navigate(
    recipe: Recipe,
    run_input: RunInput,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> None:
    await execute_on_session(
        session,
        [spi_actions.NavigateAction(
            url=run_input.url, timeout_ms=recipe.defaults.navigate_timeout_ms
        )],
        registry=registry,
        driver=driver,
    )


async def _replay_group(
    group: FieldGroup,
    recipe: Recipe,
    run_input: RunInput,
    result: RecipeRunResult,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> None:
    reader = PageReader(
        session=session, registry=registry, driver=driver, base_url=run_input.url
    )
    ctx = StepContext(
        session=session, registry=registry, driver=driver, reader=reader,
        meta=run_input.metadata,
        defaults_timeout_ms=recipe.defaults.step_timeout_ms,
    )

    await _navigate(recipe, run_input, session=session, registry=registry, driver=driver)
    reader.invalidate()

    base = len(result.step_trace)
    setup_trace, policy = await run_steps(recipe.global_setup, ctx, start_index=base)
    result.step_trace.extend(setup_trace)
    if policy is not None:
        _fail_group(group, result, "global_setup failed for this group")
        return

    group_trace, policy = await run_steps(
        group.steps, ctx, start_index=len(result.step_trace)
    )
    result.step_trace.extend(group_trace)
    if policy is not None:
        reason = next(
            (o.reason for o in reversed(group_trace) if o.status == "failed"),
            "a reveal step failed",
        )
        _fail_group(group, result, f"reveal step failed: {reason}")
        return

    tctx = TransformContext(url=run_input.url, meta=run_input.metadata,
                            variant=result.variant_id)

    if group.repeat is not None:
        await _replay_repeat(group, group.repeat, recipe, result, reader, ctx, tctx)
    else:
        await _replay_scalar(group, recipe, result, reader, tctx)


async def _replay_scalar(
    group: FieldGroup,
    recipe: Recipe,
    result: RecipeRunResult,
    reader: PageReader,
    tctx: TransformContext,
) -> None:
    for field_name, candidates in group.bindings.items():
        spec = recipe.fields.get(field_name)
        if spec is None:
            continue
        res = await resolve_field(
            spec, candidates,
            evaluate=reader.read,
            ctx=TransformContext(
                url=tctx.url, meta=tctx.meta, field_name=field_name, variant=tctx.variant
            ),
            variant_id=result.variant_id,
            predicate_holds=lambda p: reader.holds(p, meta=tctx.meta),
        )
        await _record(field_name, spec, candidates, res, result, reader, tctx)


async def _replay_repeat(
    group: FieldGroup,
    repeat: RepeatSpec,
    recipe: Recipe,
    result: RecipeRunResult,
    reader: PageReader,
    ctx: StepContext,
    tctx: TransformContext,
) -> None:
    field_name = repeat.row_field
    spec = recipe.fields.get(field_name)
    rows: list[dict[str, Any]] = []
    truncated = False

    if repeat.kind == "json":
        rows, truncated = await _rows_from_json(group, repeat, reader, tctx)
    elif repeat.kind == "dom_rows":
        rows, truncated = await _rows_from_dom_rows(group, repeat, reader, tctx)
    else:
        rows, truncated = await _rows_from_dom(group, repeat, reader, ctx, tctx, result)

    expect = group.expect
    if expect is not None and expect.min_rows is not None and len(rows) < expect.min_rows:
        truncated = True

    result.truncated[field_name] = truncated
    if not rows:
        result.field_status[field_name] = "failed" if (spec and spec.required) else "empty"
        return

    # The field's own transforms, over the whole row set.
    #
    # Columns are transformed individually inside `_resolve_columns`; this is
    # the pipeline declared on the *repeat field*, and it had no effect at all
    # until now -- `result.data[field_name] = rows` went straight out. The
    # omission was invisible for the row-shaped cases and wrong for exactly one
    # thing the contract names: `to_object`, whose entire purpose is collapsing
    # `[{name, value}, ...]` into the `{name: value}` map a specification table
    # actually means. A recipe could declare it, the studio could show it, and
    # replay would silently return rows.
    #
    # A transform that cannot be applied leaves the rows as they are and marks
    # the field suspect: the data was read correctly and only the reshaping
    # failed, so discarding it would lose more than it protects.
    value: Any = rows
    reshape_failed = False
    if spec is not None and spec.transform:
        try:
            value = apply_transforms(rows, spec.transform, tctx)
        except TransformError:
            reshape_failed = True

    result.data[field_name] = value
    result.field_status[field_name] = (
        "suspect" if (truncated or reshape_failed) else "resolved"
    )
    # A table has no candidate chain -- it has a rows locator and one binding
    # per column, and those are what a person debugging it needs to see. The
    # misalignment `rows.py` exists to prevent (a column resolving against the
    # whole page rather than inside a row) is visible in exactly this shape:
    # a column selector that is not relative.
    result.provenance[field_name] = {
        "candidate": 0,
        "candidates": 1,
        "source": "json" if repeat.kind == "json" else "dom",
        "variant": result.variant_id,
        "rows": len(rows),
        "repeat_kind": repeat.kind,
        "locator": repeat.rows_locator.to_dict() if repeat.rows_locator else None,
        "columns": {
            name: chain[0].locator.to_dict()
            for name, chain in group.bindings.items()
            if chain
        },
    }


async def _rows_from_json(
    group: FieldGroup,
    repeat: RepeatSpec,
    reader: PageReader,
    tctx: TransformContext,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows from an array already in the page's structured data.

    This is the one to reach for. On a Zara product page the entire
    size/price/stock table is in the JSON-LD `hasVariant[]`; modelling it as
    five clicks costs five page mutations and a re-render race to reproduce
    data one read already contains.
    """

    if repeat.rows_locator is None:
        return [], False
    raw = await reader.read(repeat.rows_locator)
    if not isinstance(raw, list):
        return [], False

    limit = repeat.max_iterations
    truncated = len(raw) > limit
    rows: list[dict[str, Any]] = []

    for element in raw[:limit]:
        async def _row_evaluate(loc: Locator, _el: Any = element) -> Any:
            # Column paths are relative to the row, not the document.
            if loc.kind in _STRUCTURED:
                return resolve_path(_el, loc.path or "", loc.path_lang)
            return None

        row = await _resolve_columns(group, _row_evaluate, tctx)
        if row:
            rows.append(row)
    return rows, truncated


async def _rows_from_dom_rows(
    group: FieldGroup,
    repeat: RepeatSpec,
    reader: PageReader,
    tctx: TransformContext,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows from N row elements already rendered on the page.

    The commonest extraction there is, and the one v2.0 could not express: a
    results page, a listing, an HTML table. `json` needs the data to already be
    an array and `dom` clicks -- which on a results page navigates away on the
    first row.

    Structurally this is `_rows_from_json` against elements instead of array
    members: read the rows once, then resolve every column *relative to its own
    row*. Rows therefore stay aligned by construction. The alternative authors
    were forced into -- one `all: true` read per column, zipped by index --
    silently shifts every value below a row that happens to lack a cell, and
    nothing in the output says it happened.

    Transforms still run per column, through the same `resolve_field` path as
    every other read, so a column's `Candidate[]` fallback chain works here too.
    """

    if repeat.rows_locator is None:
        return [], False

    # Every candidate of every column, not just the first: `resolve_field`
    # walks a column's whole chain until one yields, and reading only the
    # primary would quietly turn each fallback into a miss -- the chain would
    # still be in the document and would simply never be tried.
    #
    # Slots are keyed by the candidate's identity so the evaluator below can
    # answer without re-deriving anything; the locator objects are frozen
    # dataclasses held by the recipe for the whole run, so identity is stable.
    slots: dict[str, Locator] = {}
    slot_of: dict[int, str] = {}
    for name, candidates in group.bindings.items():
        for index, candidate in enumerate(candidates):
            key = f"{name}#{index}"
            slots[key] = candidate.locator
            slot_of[id(candidate.locator)] = key

    # The whole matrix in one round trip, so every row is read at the same
    # moment -- see `PageReader.read_rows`.
    raw = await reader.read_rows(repeat.rows_locator, slots)
    if raw is None:
        return [], False

    limit = repeat.max_iterations
    truncated = len(raw) > limit
    rows: list[dict[str, Any]] = []

    for element in raw[:limit]:
        async def _row_evaluate(loc: Locator, _el: dict[str, Any] = element) -> Any:
            # The row's cells were already read; resolution is a lookup, which
            # is what keeps this to one round trip. A locator the read could
            # not address (a `json_ld` path on a DOM row) has no slot and
            # answers None, so `resolve_field` falls through to the next
            # candidate exactly as it would for an empty read.
            key = slot_of.get(id(loc))
            return _el.get(key) if key is not None else None

        row = await _resolve_columns(group, _row_evaluate, tctx)
        if row:
            rows.append(row)
    return rows, truncated


async def _rows_from_dom(
    group: FieldGroup,
    repeat: RepeatSpec,
    reader: PageReader,
    ctx: StepContext,
    tctx: TransformContext,
    result: RecipeRunResult,
) -> tuple[list[dict[str, Any]], bool]:
    """Rows by clicking through an option set.

    The option locator is re-resolved before every iteration: a prior click may
    have re-rendered the set, invalidating everything matched earlier.

    Unlike v1, an incomplete row is KEPT and reported rather than silently
    dropped. Dropping partial rows to make a run look clean is precisely the
    failure `truncated` exists to prevent -- a run that captured 8 of 40 sizes
    must never report success.
    """

    if repeat.option_locator is None:
        return [], False

    from agentpilot.recipe.v2.steps import dispatch_step
    from agentpilot.recipe.v2.tree import find_nodes

    first = await reader.read(_count_probe(repeat.option_locator))
    total = len(first) if isinstance(first, list) else 0
    if total == 0:
        snapshot = await reader.snapshot()
        total = len(find_nodes(snapshot, repeat.option_locator)) if snapshot else 0
    if total == 0:
        return [], False

    limit = min(total, repeat.max_iterations)
    truncated = total > repeat.max_iterations
    rows: list[dict[str, Any]] = []

    for i in range(limit):
        step = _option_step(repeat, i)
        outcome = await dispatch_step(step, ctx, len(result.step_trace))
        result.step_trace.append(outcome)
        if outcome.status == "failed":
            truncated = True
            break
        if repeat.settle is not None:
            result.step_trace.append(
                await dispatch_step(repeat.settle, ctx, len(result.step_trace))
            )
        reader.invalidate()
        row = await _resolve_columns(group, reader.read, tctx)
        if row:
            rows.append(row)
    return rows, truncated


def _count_probe(option_locator: Locator) -> Locator:
    """Read every match at once, to learn how many options there are."""
    from dataclasses import replace

    return replace(option_locator, all=True)


def _option_step(repeat: RepeatSpec, index: int) -> Any:
    from dataclasses import replace

    from agentpilot.recipe.v2.models import Step

    assert repeat.option_locator is not None
    return Step(
        op=repeat.action,
        target=replace(repeat.option_locator, all=False, index=index),
        label=f"repeat option {index}",
        on_error="continue",
    )


async def _resolve_columns(
    group: FieldGroup, evaluate: Any, tctx: TransformContext
) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for column, candidates in group.bindings.items():
        spec = FieldSpec(name=column)
        res = await resolve_field(
            spec, candidates, evaluate=evaluate,
            ctx=TransformContext(
                url=tctx.url, meta=tctx.meta, field_name=column, variant=tctx.variant
            ),
        )
        if res.status in ("resolved", "fallback"):
            row[column] = res.value
    return row


async def _record(
    field_name: str,
    spec: FieldSpec,
    candidates: list[Candidate],
    res: FieldResolution,
    result: RecipeRunResult,
    reader: PageReader,
    tctx: TransformContext,
) -> None:
    if res.status in ("resolved", "fallback"):
        result.data[field_name] = res.value
        if spec.emit_raw and res.raw is not None:
            result.data[f"{field_name}_raw"] = res.raw
        # Which selector actually produced this, in enough detail to act on.
        # `source` is only the locator's *kind*, and on a page with four css
        # candidates the kind is the one thing that does not distinguish them --
        # so the winning locator and the chain around it travel with the value.
        result.provenance[field_name] = {
            "candidate": res.candidate_index,
            "candidates": res.considered,
            "source": res.source,
            "variant": tctx.variant,
            "locator": res.locator.to_dict() if res.locator is not None else None,
            # The losers. A field that fell through to candidate 2 is breaking,
            # and what happened to 0 and 1 is what says whether the selector
            # stopped matching or its value stopped surviving the cleanup.
            "attempts": [a.to_dict() for a in res.attempts],
        }

    status = res.status
    if spec.assertions and res.status in ("resolved", "fallback"):
        cross = await _cross_source_value(
            candidates, res, reader, tctx
        ) if _wants_cross_source(spec) else None
        checks = evaluate_assertions(res.value, spec.assertions, cross_source_value=cross)
        result.assertions[field_name] = checks
        status = apply_assertions_to_status(status, checks)

    result.field_status[field_name] = status


def _wants_cross_source(spec: FieldSpec) -> bool:
    return any(a.kind == "cross_source_agrees" for a in spec.assertions)


async def _cross_source_value(
    candidates: list[Candidate],
    res: FieldResolution,
    reader: PageReader,
    tctx: TransformContext,
) -> Any:
    """Read the field a second time from a *different kind* of locator and hand
    it back for comparison.

    Nearly free -- most fields already carry two or three candidates and only
    the first is ever read -- and it is the specific defence against the
    failure that silently poisons a dataset: a locator drifting onto a
    sponsored ad's price, which stays a well-typed plausible number forever.

    "Different kind", not "different family": `json_ld` disagreeing with `meta`
    is exactly as informative as either disagreeing with `css`, and treating
    all three structured sources as one bucket would silently skip the check on
    the many fields whose only two candidates are both structured.
    """

    from agentpilot.recipe.v2.transform import TransformError, apply_transforms

    for cand in candidates:
        if cand.locator.kind == res.source:
            continue
        try:
            raw = await reader.read(cand.locator)
        except Exception:  # noqa: BLE001 - a second opinion is best-effort
            continue
        if raw is None:
            continue
        pipeline = cand.transform if cand.transform is not None else []
        try:
            return apply_transforms(raw, pipeline, tctx) if pipeline else raw
        except TransformError:
            continue
    return None


def _fail_group(group: FieldGroup, result: RecipeRunResult, reason: str) -> None:
    for name in group.field_names:
        result.field_status.setdefault(name, "failed")
    result.step_trace.append(
        StepOutcome(index=len(result.step_trace), op="group", status="failed",
                    label=group.group_id, reason=reason)
    )


def _finalize(result: RecipeRunResult, recipe: Recipe) -> None:
    """`ok` only when every declared field resolved cleanly.

    A field the recipe never bound at all counts as failed-or-empty by its
    requiredness, so a recipe that quietly stopped covering one of its declared
    fields cannot report `ok`.
    """

    for name, spec in recipe.fields.items():
        if name not in result.field_status:
            result.field_status[name] = "failed" if spec.required else "empty"

    statuses = set(result.field_status.values())
    required_failed = any(
        result.field_status.get(name) == "failed"
        for name, spec in recipe.fields.items()
        if spec.required
    )
    if required_failed:
        result.outcome = "failed"
    elif statuses & {"failed", "suspect", "empty"} or any(result.truncated.values()):
        result.outcome = "partial"
    else:
        result.outcome = "ok"
