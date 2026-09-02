"""Candidate selection and field assertions -- the two pieces of replay that
are pure, and therefore the two worth testing without a browser.

Everything here takes the page as a *callback* rather than a session. That is
not indirection for its own sake: it means the ordering rules (which candidate
wins, and why) and the quality gate (whether the value that won is plausible)
can be exercised exhaustively in unit tests, while the browser-dependent half
stays in one place.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from agentpilot.recipe.v2.models import (
    AssertionResult,
    Candidate,
    FieldStatus,
    Locator,
)
from agentpilot.recipe.v2.schema import Assertion, FieldSpec
from agentpilot.recipe.v2.transform import (
    TransformContext,
    TransformError,
    apply_transforms,
)

# A callback that resolves one locator against the current page, returning the
# raw value or None. Injected so the ordering rules stay testable.
Evaluator = Callable[[Locator], Awaitable[Any]]
# A callback that says whether a guard predicate currently holds.
PredicateEvaluator = Callable[[Any], Awaitable[bool]]


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


async def applicable_candidates(
    candidates: list[Candidate],
    *,
    variant_id: str | None,
    predicate_holds: PredicateEvaluator | None = None,
) -> list[Candidate]:
    """Filter by variant and guard, then order by priority.

    Ties break by list position, so a recipe whose author never set `priority`
    behaves exactly as v1 did -- the ordering became explicit without becoming
    mandatory.
    """

    out: list[Candidate] = []
    for cand in candidates:
        if cand.variant_id is not None and cand.variant_id != variant_id:
            continue
        if cand.when and predicate_holds is not None:
            holds = True
            for predicate in cand.when:
                if not await predicate_holds(predicate):
                    holds = False
                    break
            if not holds:
                continue
        out.append(cand)
    # `sorted` is stable, so equal priorities keep their document order.
    return sorted(out, key=lambda c: c.priority)


class FieldResolution:
    """What resolving one field produced, including *which* candidate produced
    it. The candidate index is the drift signal the whole operational model
    turns on: a field that starts resolving from candidate 2 instead of
    candidate 0 is breaking, days before it breaks."""

    __slots__ = ("value", "raw", "status", "candidate_index", "source", "reason")

    def __init__(
        self,
        *,
        value: Any = None,
        raw: Any = None,
        status: FieldStatus = "empty",
        candidate_index: int | None = None,
        source: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.value = value
        self.raw = raw
        self.status = status
        self.candidate_index = candidate_index
        self.source = source
        self.reason = reason


async def resolve_field(
    spec: FieldSpec,
    candidates: list[Candidate],
    *,
    evaluate: Evaluator,
    ctx: TransformContext,
    variant_id: str | None = None,
    predicate_holds: PredicateEvaluator | None = None,
) -> FieldResolution:
    """Try each applicable candidate in priority order; the first non-empty
    *transformed* result wins.

    Transforming before deciding is deliberate. A candidate that resolves to
    markup which strips to nothing, or to a string that casts to None, has not
    actually produced a value -- and falling through to the next candidate is
    exactly what the ordered list is for.
    """

    ordered = await applicable_candidates(
        candidates, variant_id=variant_id, predicate_holds=predicate_holds
    )
    if not ordered:
        return FieldResolution(
            status="failed" if spec.required else "empty",
            reason="no candidate applies under the active variant and guards",
        )

    first_reason: str | None = None
    for position, cand in enumerate(ordered):
        try:
            raw = await evaluate(cand.locator)
        except Exception as exc:  # noqa: BLE001 - a broken locator is data, not a crash
            first_reason = first_reason or f"candidate {position} failed to evaluate: {exc}"
            continue
        if is_empty(raw):
            continue

        pipeline = cand.transform if cand.transform is not None else spec.transform
        try:
            value = apply_transforms(raw, pipeline, ctx)
        except TransformError as exc:
            first_reason = first_reason or f"candidate {position} transform failed: {exc}"
            continue
        if is_empty(value):
            continue

        return FieldResolution(
            value=value,
            raw=raw,
            status="resolved" if position == 0 else "fallback",
            candidate_index=position,
            source=cand.locator.kind,
        )

    return FieldResolution(
        status="failed" if spec.required else "empty",
        reason=first_reason or f"all {len(ordered)} candidate(s) resolved empty",
    )


# --- assertions -------------------------------------------------------------


def evaluate_assertions(
    value: Any,
    assertions: list[Assertion],
    *,
    cross_source_value: Any = None,
    previous_value: Any = None,
) -> list[AssertionResult]:
    """The cheap, model-free defence against a locator that resolves to a
    well-typed value from the wrong element -- the failure that silently
    poisons a dataset rather than breaking a run."""

    return [
        _evaluate_one(value, a, cross_source_value, previous_value) for a in assertions
    ]


def _evaluate_one(
    value: Any, a: Assertion, cross: Any, previous: Any
) -> AssertionResult:
    if a.kind == "not_empty":
        ok = not is_empty(value)
        return AssertionResult("not_empty", ok, None if ok else "value is empty")

    if a.kind == "range":
        number = _as_number(value)
        if number is None:
            return AssertionResult("range", False, f"{value!r} is not a number")
        if a.min is not None and number < a.min:
            return AssertionResult("range", False, f"{number} < {a.min}")
        if a.max is not None and number > a.max:
            return AssertionResult("range", False, f"{number} > {a.max}")
        return AssertionResult("range", True)

    if a.kind == "matches":
        if a.regex is None:
            return AssertionResult("matches", False, "no regex configured")
        try:
            ok = re.search(a.regex, "" if value is None else str(value)) is not None
        except re.error as exc:
            return AssertionResult("matches", False, f"invalid regex: {exc}")
        return AssertionResult("matches", ok, None if ok else f"{value!r} does not match")

    if a.kind == "in_set":
        ok = value in a.values
        return AssertionResult("in_set", ok, None if ok else f"{value!r} not in the allowed set")

    if a.kind == "length":
        length = len(value) if isinstance(value, (str, list, dict)) else None
        if length is None:
            return AssertionResult("length", False, f"{type(value).__name__} has no length")
        if a.min is not None and length < a.min:
            return AssertionResult("length", False, f"length {length} < {a.min}")
        if a.max is not None and length > a.max:
            return AssertionResult("length", False, f"length {length} > {a.max}")
        return AssertionResult("length", True)

    if a.kind == "cross_source_agrees":
        if cross is None:
            # Not a failure: the field simply has no second source on this page.
            # Reporting it as passing would overstate the evidence, so it is
            # recorded as passed-with-a-note rather than silently dropped.
            return AssertionResult("cross_source_agrees", True, "no second source to compare")
        return _compare_sources(value, cross, a.tolerance)

    if a.kind == "not_equals_previous":
        ok = previous is None or value != previous
        return AssertionResult(
            "not_equals_previous", ok, None if ok else "value is unchanged from the previous run"
        )

    return AssertionResult(a.kind, True, "unknown assertion kind, skipped")


def _compare_sources(value: Any, other: Any, tolerance: float) -> AssertionResult:
    a_num, b_num = _as_number(value), _as_number(other)
    if a_num is not None and b_num is not None:
        ok = abs(a_num - b_num) <= tolerance
        return AssertionResult(
            "cross_source_agrees", ok,
            None if ok else f"sources disagree: {a_num} vs {b_num}",
        )
    ok = str(value).strip() == str(other).strip()
    return AssertionResult(
        "cross_source_agrees", ok,
        None if ok else f"sources disagree: {value!r} vs {other!r}",
    )


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", ""))
        except ValueError:
            return None
    return None


def apply_assertions_to_status(
    status: FieldStatus, results: list[AssertionResult]
) -> FieldStatus:
    """A failed assertion downgrades a resolved field to `suspect` -- the value
    is still returned, because discarding data on a heuristic is worse than
    flagging it, and `suspect` is what the drift metrics trend on."""

    if status in ("resolved", "fallback") and any(not r.passed for r in results):
        return "suspect"
    return status
