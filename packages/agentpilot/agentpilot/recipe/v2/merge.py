"""What the sample runs proved, written back onto the candidates that earned it.

`verified_on` is documented as "how many sample URLs this resolved on" and was
hardcoded to `1` at every site that produced a candidate -- `selector_agent`,
`rows`, `onboard`, `review`, and `fromPick.ts` on the studio side. Nothing ever
counted anything. The frontend lint already warned on `verified_on == 0` with a
comment claiming the field was "written by build-time multi-page induction",
which was the intent and not the behaviour, so the one signal separating a
selector three pages agree on from a selector nobody has tested twice did not
exist.

The evidence was already being collected and thrown away. `review.run_samples`
replays the draft against `sample_urls[:sample_limit]`, and every run carries
per-candidate `attempts` (`resolve.CandidateAttempt`) saying which candidate won
and what each loser did. `merge_provenance` kept only the *first* run's entry per
field, so the second page's verdict never left the function.

**What `verified_on` counts, and the tempting definition that is wrong.**
`resolve_field` stops at the first non-empty transformed value, so on a page
where candidate 0 wins, candidates 1 and 2 are never evaluated at all. The
obvious-looking metric -- "pages on which the field resolved at or before this
candidate" -- therefore credits a chain of three, where candidate 0 always wins,
as `3, 3, 3`: the last-resort candidate nothing has ever executed reads as the
best-verified one in the recipe. That is backwards, and it inflates precisely the
positional/brittle tail that `selector_quality` exists to demote.

So:

    verified_on = the number of sample pages on which THIS candidate produced
                  the value.

A page where an earlier candidate won says nothing about a later one. A page
where a *later* one won is evidence against the earlier one, not for it. A
healthy fallback therefore reads `0`, which is correct and is why the "never
verified" lint has to ask about a field's whole chain rather than about each
candidate -- see `frontend/src/lib/recipe/lint.ts`.

Counting only the winner is also what keeps this inside the build's page budget:
proving each candidate individually would cost an extra read per candidate per
page, twelve reads for a 4-candidate chain over three pages, to answer a
ranking question.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from agentpilot.recipe.v2.models import Candidate, FieldGroup, Locator, Recipe

# A locator's identity, borrowed rather than reinvented: `dedupe_locators`
# already needed one and `locator_key` is it.
LocatorKey = tuple[Any, ...]

# A table column's provenance carries only its FIRST candidate's locator, so a
# column that won from a fallback can be credited by position alone. A position
# is namespaced so it can share a map with real locator keys without colliding
# with one.
_POSITION = "@position"


class RunLike(Protocol):
    """Just enough of `review.SampleRun` to count from.

    A Protocol rather than an import so this module stays free of `review`,
    which reaches the browser, the judge and the LLM client -- and so the unit
    tests can hand it plain objects.
    """

    outcome: str
    provenance: dict[str, dict[str, Any]]


def _locator_key(loc: Locator) -> LocatorKey:
    from agentpilot.recipe.v2.selector_agent import locator_key

    return locator_key(loc)


def _key_of_dict(raw: dict[str, Any]) -> LocatorKey:
    """The same key, from a locator that came back through provenance as JSON.

    Provenance carries `Locator.to_dict()`, which omits defaults, so the dict is
    round-tripped through `from_dict` before it is keyed. Building a key from the
    raw dict instead would miss against every locator whose defaults were
    omitted -- which is most of them.
    """

    return _locator_key(Locator.from_dict(raw))


def _winning_key(entry: dict[str, Any]) -> LocatorKey | None:
    """The locator that produced this field's value in one run, if any.

    Prefers the `won` attempt over the top-level `locator`: the two agree, and
    the attempts list is also what says what the losers did, so reading both from
    one place keeps them from drifting.
    """

    for attempt in entry.get("attempts") or ():
        if attempt.get("outcome") == "won" and isinstance(attempt.get("locator"), dict):
            return _key_of_dict(attempt["locator"])
    locator = entry.get("locator")
    return _key_of_dict(locator) if isinstance(locator, dict) else None


def verified_counts(runs: list[RunLike]) -> dict[str, dict[LocatorKey, int]]:
    """Per binding key, how many runs each candidate produced the value on.

    Keyed by BINDING key rather than by field name, because a table's columns are
    bindings in their own right and each carries its own chain. A scalar field's
    binding key is its field name, so both cases land in one map.

    A `blocked` run contributes nothing. A wall in front of the page is not
    evidence about a selector, and letting it count as a page the chain failed on
    is the same mistake `classify.py` exists to prevent one rung up.
    """

    counts: dict[str, dict[LocatorKey, int]] = {}

    def credit(binding: str, key: LocatorKey) -> None:
        by_key = counts.setdefault(binding, {})
        by_key[key] = by_key.get(key, 0) + 1

    for run in runs:
        if getattr(run, "outcome", "") == "blocked":
            continue
        for field_name, entry in (run.provenance or {}).items():
            if not isinstance(entry, dict):
                continue
            columns = entry.get("columns")
            if isinstance(columns, dict):
                for column, col in columns.items():
                    if not isinstance(col, dict) or not col.get("filled"):
                        continue
                    won_at = col.get("won_at")
                    if isinstance(won_at, int):
                        credit(column, (_POSITION, won_at))
                continue
            key = _winning_key(entry)
            if key is not None:
                credit(field_name, key)
    return counts


def apply_verified_counts(
    recipe: Recipe, counts: dict[str, dict[LocatorKey, int]]
) -> Recipe:
    """Write the counts onto the recipe's candidates, replacing what was there.

    Replacing, not adding: the counts are the verdict of *this* review over
    *these* sample pages, and a recipe re-reviewed after a redesign must not
    carry credit its selectors earned against the old markup. The whole point is
    that the number stops being an assumption.

    Returns a new `Recipe`. `Candidate` and `FieldGroup` are frozen, and a save
    reading the old object while this rewrote it in place is the class of bug
    those frozen dataclasses exist to make impossible.
    """

    groups = [
        FieldGroup(
            group_id=group.group_id,
            field_names=list(group.field_names),
            bindings={
                binding: _credit_chain(chain, counts.get(binding, {}))
                for binding, chain in group.bindings.items()
            },
            steps=list(group.steps),
            teardown=list(group.teardown),
            repeat=group.repeat,
            expect=group.expect,
        )
        for group in recipe.field_groups
    ]
    return replace(recipe, field_groups=groups)


def _credit_chain(chain: list[Candidate], wins: dict[LocatorKey, int]) -> list[Candidate]:
    """One chain's candidates, each carrying the pages it actually won on."""

    if not chain:
        return []

    by_position: dict[int, int] = {}
    for index, candidate in enumerate(chain):
        won = wins.get(_locator_key(candidate.locator))
        if won:
            by_position[index] = by_position.get(index, 0) + won
    for key, won in wins.items():
        if len(key) == 2 and key[0] == _POSITION and isinstance(key[1], int):
            position = key[1]
            if 0 <= position < len(chain):
                by_position[position] = by_position.get(position, 0) + won

    return [
        replace(candidate, verified_on=by_position.get(index, 0))
        for index, candidate in enumerate(chain)
    ]
