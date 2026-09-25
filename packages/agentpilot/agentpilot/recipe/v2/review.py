"""Stages 6 and 7: run the draft, judge what it collected, repair, decide.

`onboard.py` produces a recipe whose every binding resolved *at the moment it
was frozen*, on the page state that revealed it. That is a real guarantee and
it is not the same as the recipe working:

- Per-step verification cannot catch steps in the wrong order. Each field was
  read while the page happened to be in the right state; replay re-navigates
  and runs the sequence as written, which is the first time that sequence is
  tested as a sequence.
- Nothing so far has looked at whether a value is the *right* value. That is
  the judge.

Kadoa's published figure is that LLM first-attempt selectors fail to yield data
30-40% of the time, which is why `selector_agent.py` verifies rather than
trusts. This module is the same instinct applied one level up: do not hand a
human a draft without having run it.

The order matters and is the user-facing contract: **a draft reaches human
review only once the judge passes.** A rejection is specific enough to act on,
so it is fed back to the selector agent for a bounded number of repair rounds
first; only what survives that is worth a person's attention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.judge import DataVerdict, judge_collection
from agentpilot.recipe.v2.models import Candidate, FieldGroup, Recipe, RunInput
from agentpilot.recipe.v2.onboard import ProgressSink
from agentpilot.recipe.v2.replay import replay_recipe
from agentpilot.recipe.v2.schema import (
    FieldSpec,
    all_leaf_fields,
    column_to_table_map,
    is_free_text,
)
from agentpilot.recipe.v2.selector_agent import propose_and_verify
from crawlpilot.dom.serializer import serialize
from crawlpilot.spi.dom_tree import SnapshotView
from crawlpilot.session.interactive import InteractiveSession
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)

DEFAULT_MAX_REPAIRS = 2
DEFAULT_MAX_SAMPLE_RUNS = 2


@dataclass
class SampleRun:
    """One replay of the draft against one URL."""

    url: str
    outcome: str
    data: dict[str, Any] = field(default_factory=dict)
    field_status: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    step_trace: list[dict[str, Any]] = field(default_factory=list)
    """What each step actually did. Carried because an empty field behind a
    reveal click is unattributable without it: the selector may be wrong, or
    the click may never have run. Every reveal step is `optional: true,
    on_error: continue` by construction, so a step that matched nothing is
    otherwise silent."""

    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "outcome": self.outcome,
            # The values, not just their statuses. This is what a reviewer
            # actually decides on: "18 fields bound" says nothing about whether
            # the recipe works, while `name = "Home / Tops"` says immediately
            # that it does not.
            "data": self.data,
            "field_status": self.field_status,
            "step_trace": self.step_trace,
            "error": self.error,
        }


@dataclass
class ReviewResult:
    runs: list[SampleRun] = field(default_factory=list)
    verdict: DataVerdict | None = None
    repairs: int = 0
    unrepaired: dict[str, str] = field(default_factory=dict)
    """Fields the judge rejected that repair could not fix. These go to the
    assist loop -- a human is the next thing to try."""

    absent: dict[str, str] = field(default_factory=dict)
    """Fields the judge says the page does not contain at all.

    Kept apart from `unrepaired` because they need a different question put to a
    person. "This looks wrong, which one is right?" is answerable by pointing at
    something; "this is not on this page" is answered by dropping the field, or
    by realising it lives on a different page entirely."""

    @property
    def ready_for_review(self) -> bool:
        """Whether a person should be shown this draft.

        An errored judge counts as ready: it could not ask the question, and a
        human is the gate it was standing in front of.
        """

        if self.verdict is None:
            return False
        return self.verdict.passed or self.verdict.errored

    @property
    def step_trace(self) -> list[dict[str, Any]]:
        """The trace from the first run that actually executed, for showing a
        person why a field may be empty."""

        for run in self.runs:
            if run.step_trace:
                return run.step_trace
        return []

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": [r.to_dict() for r in self.runs],
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "repairs": self.repairs,
            "unrepaired": self.unrepaired,
        }


def merge_field_values(runs: list[SampleRun]) -> dict[str, Any]:
    """One representative value per field, preferring a run that produced one.

    A field that resolved on page two and not page one is a field that works;
    judging the empty one would report a completeness problem as a correctness
    problem, and they need opposite fixes.
    """

    merged: dict[str, Any] = {}
    for run in runs:
        for name, value in run.data.items():
            if name not in merged and value not in (None, "", [], {}):
                merged[name] = value
    return merged


def merge_provenance(runs: list[SampleRun]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for run in runs:
        for name, entry in run.provenance.items():
            merged.setdefault(name, entry)
    return merged


async def run_samples(
    recipe: Recipe,
    urls: list[str],
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    limit: int = DEFAULT_MAX_SAMPLE_RUNS,
) -> list[SampleRun]:
    """Replay the draft against its sample URLs.

    Capped, because each run is a full page load plus the whole step sequence
    and the value of the third one is mostly confirmation. Two is enough to
    catch the common "it only worked on the page it was built against".
    """

    runs: list[SampleRun] = []
    for url in urls[:limit]:
        try:
            result = await replay_recipe(
                recipe,
                RunInput(url=url),
                session=session,
                registry=registry,
                driver=driver,
            )
        except Exception as exc:  # noqa: BLE001 - a failed sample is data
            log.warning("review.sample_failed", url=url, error=str(exc))
            runs.append(SampleRun(url=url, outcome="failed", error=str(exc)))
            continue
        runs.append(
            SampleRun(
                url=url,
                outcome=result.outcome,
                data=dict(result.data),
                field_status=dict(result.field_status),
                provenance=dict(result.provenance),
                step_trace=[s.to_dict() for s in result.step_trace],
                error=result.error,
            )
        )
    return runs


def _group_of(recipe: Recipe, field_name: str) -> tuple[FieldGroup | None, str]:
    """The group binding `field_name`, and the binding key within it.

    A table's columns are bound one key at a time, so a rejected column is
    addressed by its column name while its group is found by the table's.
    """

    columns = column_to_table_map(recipe.fields)
    table = columns.get(field_name)
    wanted = table or field_name
    for group in recipe.field_groups:
        if wanted in group.field_names and field_name in group.bindings:
            return group, field_name
    return None, field_name


async def repair_fields(
    recipe: Recipe,
    rejected: dict[str, str],
    *,
    url: str,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
) -> tuple[Recipe, set[str]]:
    """Re-bind the rejected fields, with the judge's reason fed back.

    Returns the recipe and the set of fields actually re-bound. The judge's
    reason is the whole mechanism here: "this is the breadcrumb trail, the
    product title is in the h1 below it" is exactly the shape
    `propose_locators` already accepts as `failures`, so no new prompt is
    needed -- the model is told what it got wrong in the same place it is
    normally told what did not resolve.

    Best-effort: a field that cannot be re-bound keeps its existing binding.
    Replacing a wrong value with no value is not an improvement, and the caller
    escalates it to a human either way.
    """

    from agentpilot.recipe.v2.steps import run_steps

    leaves = all_leaf_fields(recipe.fields)
    wanted = {name: leaves[name] for name in rejected if name in leaves}
    if not wanted:
        return recipe, set()

    reader, ctx = await restore_page(
        recipe, url, session=session, registry=registry, driver=driver
    )

    repaired: set[str] = set()
    groups = list(recipe.field_groups)

    for index, group in enumerate(groups):
        targets = {n: spec for n, spec in wanted.items() if n in group.bindings}
        if not targets:
            continue
        if group.steps:
            await run_steps(group.steps, ctx)
        reader.invalidate()

        snapshot = await reader.snapshot()
        if snapshot is None:
            continue
        verified = await propose_and_verify(
            targets,
            snapshot_text=serialize(snapshot, view=SnapshotView(for_authoring=True, content_only=True)).llm_text,
            structured_data=await reader.structured_data(),
            llm_config=llm_config,
            verify=reader.read,
            page_url=url,
            failures={name: rejected[name] for name in targets},
            # The rejection is about *which element*, and a second opinion from
            # the same wrong source would not help. One pass is enough.
            max_retries=0,
            verified_on=1,
        )
        if not verified:
            continue
        bindings: dict[str, list[Candidate]] = dict(group.bindings)
        for name, candidates in verified.items():
            bindings[name] = candidates
            repaired.add(name)
        groups[index] = FieldGroup(
            group_id=group.group_id,
            field_names=list(group.field_names),
            bindings=bindings,
            steps=list(group.steps),
            repeat=group.repeat,
            expect=group.expect,
        )

    recipe.field_groups = groups
    return recipe, repaired


async def restore_page(
    recipe: Recipe,
    url: str,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> tuple[PageReader, Any]:
    """Load the page fresh and put it back into the state a recipe expects.

    Navigate, then run `global_setup`. Both callers need exactly this and for
    the same reason: whatever the session is currently showing is the state some
    earlier step or some person left it in, and reasoning about a recipe against
    that state is how a binding gets accepted here and fails on every real run.

    Returns the reader and the step context, because the caller invariably wants
    to run the group's own steps next.
    """

    from agentpilot.recipe.v2.steps import StepContext, run_steps
    from crawlpilot.session.interactive import execute_on_session
    from crawlpilot.spi import actions as spi_actions

    await execute_on_session(
        session,
        [
            spi_actions.NavigateAction(
                url=url, timeout_ms=recipe.defaults.navigate_timeout_ms, wait_until="load"
            )
        ],
        registry=registry,
        driver=driver,
    )

    reader = PageReader(session=session, registry=registry, driver=driver, base_url=url)
    ctx = StepContext(
        session=session, registry=registry, driver=driver, reader=reader, meta={},
        defaults_timeout_ms=recipe.defaults.step_timeout_ms,
    )
    reader.invalidate()
    if recipe.global_setup:
        await run_steps(recipe.global_setup, ctx)
    return reader, ctx


# Where a string stops being a label and starts being prose. A title, a brand,
# an SKU, a breadcrumb -- the things relocation genuinely fixes -- are all short.
_PROSE_CHARS = 200


def repairable_by_relocating(spec: FieldSpec | None, value: Any) -> bool:
    """Whether a rejection of this value is worth sending back to the selector
    agent, as opposed to straight to a person.

    Repair rebinds the field somewhere else, so it only helps when "wrong" means
    *read from the wrong place*. For a short string it usually does: `name`
    bound to the breadcrumb trail is the case this whole stage exists for, and
    the h1 below it is a better element that the agent can actually find.

    Long free text is the opposite. A description is rejected for carrying
    shipping boilerplate or a country of origin -- text the page really does put
    inside the description -- so there is no better element to move to. Repair
    then swaps a selector that reads the right region for one that reads less of
    it, and the judge, handed a shorter value, is no happier. The field arrives
    at a human anyway, two model calls later and now bound to something worse
    than it started with.

    So: prose goes to a person with its binding intact, and they can keep the
    value (`accept`) or point somewhere else. Everything with a shape to check
    against -- a price, a date, anything carrying assertions -- keeps repairing.
    """

    if not is_free_text(spec):
        return True
    return len(str(value or "")) < _PROSE_CHARS


async def verify_and_judge(
    recipe: Recipe,
    *,
    sample_urls: list[str],
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
    sample_limit: int = DEFAULT_MAX_SAMPLE_RUNS,
    revealed_page_text: str = "",
    on_progress: ProgressSink | None = None,
) -> tuple[Recipe, ReviewResult]:
    """Run it, judge it, repair what the judge rejected, run it again.

    The loop terminates on any of: the judge passing, the repair budget being
    spent, or a repair round changing nothing -- that last one matters, because
    a model that cannot find a better locator will happily propose the same one
    forever.

    `on_progress` narrates it. This phase is several page loads and two or three
    model calls long, and it is the part where a person most wants to see what
    is happening: it is deciding whether their recipe is any good. Without it
    the UI showed one "checking..." line and then went dark for minutes.
    """

    async def say(**payload: Any) -> None:
        """Narrate, never at the cost of the work. A progress line that can fail
        a build would be absurd."""

        if on_progress is None:
            return
        try:
            await on_progress({"phase": "verifying", "recipe": recipe.to_dict(), **payload})
        except Exception:  # noqa: BLE001 - see docstring
            log.debug("review.progress_write_failed", exc_info=True)

    result = ReviewResult()
    reader = PageReader(session=session, registry=registry, driver=driver)
    # Fields repair has already had a go at, so a second rejection of the
    # same field stops rather than starting another round.
    tried: set[str] = set()

    for attempt in range(max_repairs + 1):
        await say(step="replaying", attempt=attempt + 1,
                  urls=sample_urls[:sample_limit])
        result.runs = await run_samples(
            recipe, sample_urls, session=session, registry=registry,
            driver=driver, limit=sample_limit,
        )
        data = merge_field_values(result.runs)
        await say(step="replayed", runs=[r.to_dict() for r in result.runs], collected=data)

        reader.invalidate()
        snapshot = await reader.snapshot()
        page_text = serialize(snapshot).llm_text if snapshot is not None else ""
        await say(step="judging", collected=data)
        result.verdict = await judge_collection(
            recipe.fields,
            data=data,
            page_text=page_text,
            # The build's own fullest view of the page -- accordions open.
            # Without it the judge reads one post-replay snapshot with nothing
            # expanded, and for a field that never bound, nothing ever opened
            # the panel holding it. It then reports `absent`, which is terminal:
            # absence is excluded from repair by design and tells a person the
            # page does not have the value.
            #
            # Measured on a Zara shirt. The agent had recorded "care
            # instructions (machine wash max 30C, no bleach, iron max 150C...)"
            # from the opened accordion; the judge replied "no washing, drying
            # or ironing instructions appear anywhere in the rendered text.
            # Stop looking for care on this page." Both described what they
            # saw; only one had been shown the panel open.
            also_revealed=revealed_page_text,
            provenance=merge_provenance(result.runs),
            llm_config=llm_config,
        )
        rejected = result.verdict.rejected
        result.absent = result.verdict.absent
        await say(step="judged", verdict=result.verdict.to_dict(), collected=data)

        # A field already re-bound once and rejected again is not converging.
        # The model is picking a different wrong element each round, which is
        # what happens when it is asked to find something that is not there and
        # the judge has not said so outright. Two bites, then stop.
        thrashing = {name: why for name, why in rejected.items() if name in tried}
        if thrashing:
            log.info("review.repair_thrashing", fields=sorted(thrashing))
        # A rejected value that relocation cannot improve goes straight to a
        # person, with its binding left alone. See `repairable_by_relocating`:
        # repairing one trades a selector that reads the right region for one
        # that reads less of it.
        leaves = all_leaf_fields(recipe.fields)
        prose = {
            name: why
            for name, why in rejected.items()
            if not repairable_by_relocating(leaves.get(name), data.get(name))
        }
        if prose:
            log.info("review.rejection_not_relocatable", fields=sorted(prose))
        repairable = {
            name: why
            for name, why in rejected.items()
            if name not in tried and name not in prose
        }

        if not repairable or attempt >= max_repairs:
            result.unrepaired = rejected
            break

        log.info("review.repairing", fields=sorted(repairable), attempt=attempt + 1)
        await say(step="repairing", attempt=attempt + 1, rejected=repairable)
        tried.update(repairable)
        recipe, repaired = await repair_fields(
            recipe, repairable, url=sample_urls[0] if sample_urls else "",
            session=session, registry=registry, driver=driver, llm_config=llm_config,
        )
        result.repairs += 1
        await say(step="repaired", attempt=attempt + 1, fields=sorted(repaired))
        if not repaired:
            # Nothing changed, so another round would ask the same question of
            # the same page and get the same answer.
            result.unrepaired = rejected
            break

    return recipe, result
