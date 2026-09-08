"""Onboarding: a URL and a wanted-data contract in, a verified v2 recipe out.

The orchestration is v1's and it was right -- explore with an agent, and freeze
each field's locators *at the moment that field first becomes readable*, while
the page state that revealed it is still live. Reconstructing which clicks
mattered afterwards, from an undifferentiated action trace, is strictly harder
and strictly less reliable.

What changes is the contract it writes into. v1 emitted `field_locators` and
`reveal_steps`; nothing that consumes a recipe today reads those. The
marketplace refuses a recipe without a v2 `document`, the studio renders
`bindings`/`steps`, and codegen reads the same. So this module emits the v2
document, and that is the whole reason it exists.

Four things this does that v1 could not:

- **Recon before exploration.** A challenge page has no fields, which is
  indistinguishable from every selector being broken. `classify.py` gives a
  verdict to branch on, so a blocked build fails as blocked instead of
  producing a recipe derived from a CAPTCHA.
- **Explicit waits.** The engine has no implicit settle by design, so a
  revealing action is followed by a `wait_for_selector` on what it revealed.
  See `capture.wait_step_for`.
- **A target matcher.** Derived from the sample URLs, so the recipe declares
  what it applies to instead of accepting every URL in the catalogue.
- **Escalation.** A field the agent cannot locate is reported as an unresolved
  ask rather than silently dropped, so the caller can park the run and put it
  to a human. Stage 4 of the plan consumes this; stage 3 only has to produce it
  honestly.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import structlog

from agentpilot.agent.loop import run_agent_loop
from agentpilot.agent.state import AgentStepRecord
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.v2 import capture
from agentpilot.recipe.v2.assertions import baseline_assertions, with_assertions
from agentpilot.recipe.v2.classify import classify_current_page
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Recipe,
    RepeatSpec,
    Step,
    TargetSpec,
    UrlMatcher,
)
from agentpilot.recipe.v2.schema import (
    FieldSpec,
    all_leaf_fields,
    column_to_table_map,
    render_fields_for_prompt,
)
from agentpilot.recipe.v2.selector_agent import propose_and_verify
from crawlpilot.dom.serializer import serialize
from crawlpilot.session.interactive import InteractiveSession
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi.driver import BrowserDriver

log = structlog.get_logger(__name__)

DEFAULT_ONBOARD_MAX_STEPS = 15
DEFAULT_MAX_REPEAT_ITERATIONS = 20

# How many steps of narration to keep. A build is capped at ~15 agent steps, so
# this holds all of them; the bound exists so a future longer run cannot grow
# the row without limit.
_MAX_NARRATED_STEPS = 40

ProgressSink = Callable[[dict[str, Any]], Awaitable[None]]
"""Called after each exploration step with a snapshot of what the build is
doing. Injected rather than imported so the module has no opinion about where
progress is written -- the worker sends it to the run row, and a test can just
collect it in a list."""


class BlockedError(Exception):
    """Recon found a wall rather than a page. Distinct from "no fields
    resolved" because the two need opposite responses: retry behind a different
    identity, versus fix the schema."""


@dataclass
class OnboardOutcome:
    """What the build produced, beyond the recipe itself."""

    unresolved: dict[str, str] = field(default_factory=dict)
    """Field name -> why it could not be located. The input to the assist loop."""

    landed_url: str = ""
    steps_taken: int = 0
    agent_result: str | None = None

    @property
    def complete(self) -> bool:
        return not self.unresolved


def derive_target(urls: list[str]) -> TargetSpec:
    """A matcher for the pages this recipe applies to.

    `target_accepts` is a disjunction -- any matcher accepting the URL accepts
    it -- so this emits exactly one. Adding a host matcher "as well" would widen
    the recipe to the whole domain and make the path pattern decorative.

    The pattern is the common prefix of the sample URLs, trimmed back to a path
    boundary. With several URLs that lands on the real shared shape
    (`https://www.zara.com/in/en/` for two products); with one it keeps
    everything but the final segment, which is narrower than the host and
    obviously editable in the studio. Guessing which parts of a single URL are
    variable is not something one sample can support, so it does not try.

    The prefix is never allowed to stop inside the authority. `https://` is a
    common prefix of every URL on the internet, so samples on two different
    hosts would otherwise yield the glob `https://*` -- a recipe that silently
    accepts everything, which is the exact failure this matcher exists to
    prevent. Cross-host samples get an empty matcher instead, and
    `validate_document` already warns that an empty match accepts any URL.
    """

    kept = [u for u in urls if u]
    if not kept:
        return TargetSpec()

    hosts = {(urlsplit(u).hostname or "").lower() for u in kept}
    if len(hosts) > 1 or not next(iter(hosts)):
        return TargetSpec()
    host = next(iter(hosts))

    prefix = kept[0]
    for url in kept[1:]:
        limit = min(len(prefix), len(url))
        cut = limit
        for index in range(limit):
            if prefix[index] != url[index]:
                cut = index
                break
        prefix = prefix[:cut]

    split = urlsplit(kept[0])
    authority_end = len(f"{split.scheme}://{split.netloc}")
    boundary = prefix.rfind("/")
    if boundary < authority_end:
        # The URLs diverge at or before the start of the path, so there is no
        # honest path pattern -- but they do share a host, which is a real
        # narrowing and the best this input supports.
        return TargetSpec(match=[UrlMatcher(kind="host", pattern=host)])

    return TargetSpec(match=[UrlMatcher(kind="glob", pattern=f"{prefix[: boundary + 1]}*")])


def bindings_by_field(groups: list[FieldGroup]) -> dict[str, list[Candidate]]:
    """Each field's own candidate chain, across every group.

    A `table` field is deliberately absent: its `bindings` are keyed by column,
    and the columns are not top-level fields. Assertions attach to the field, so
    a table's are about its rows (how many) rather than about a cell.
    """

    out: dict[str, list[Candidate]] = {}
    for group in groups:
        for name in group.field_names:
            chain = group.bindings.get(name)
            if chain:
                out[name] = chain
    return out


def build_exploration_task(url: str, fields: dict[str, FieldSpec]) -> str:
    return (
        f"Navigate to {url}. The following fields must each be located "
        f"somewhere on the page (directly visible, or reachable via one "
        f"more interaction):\n{render_fields_for_prompt(fields)}\n\n"
        "Dismiss any cookie/consent dialog first if one appears. For each "
        "field, interact with whatever is needed to reveal it (open an "
        "accordion/tab, click a toggle, scroll to load more). For a field "
        "made of repeating rows, interact with ONE representative option "
        "only (e.g. click a single size or colour) -- do not click through "
        "every option; the recipe generalises from the one. Call done once "
        "you believe every field has been located at least once, or if "
        "you're stuck."
    )


class ExplorationState:
    """Freezes bindings as fields become readable, and the steps that got them
    there.

    The batch of steps captured before the first field is satisfied becomes
    `global_setup` -- replay re-navigates before every group, so those have to
    run for each one. Everything after the first freeze is scoped to its own
    group.
    """

    def __init__(
        self,
        *,
        fields: dict[str, FieldSpec],
        reader: PageReader,
        llm_config: LLMConfig,
        max_repeat_iterations: int = DEFAULT_MAX_REPEAT_ITERATIONS,
        verify_max_retries: int = 1,
        on_progress: ProgressSink | None = None,
    ) -> None:
        self._on_progress = on_progress
        self._steps: list[dict[str, Any]] = []
        self._all_fields = fields
        self._unfound = all_leaf_fields(fields)
        self._column_to_table = column_to_table_map(fields)
        self._reader = reader
        self._llm_config = llm_config
        self._max_repeat_iterations = max_repeat_iterations
        self._verify_max_retries = verify_max_retries

        self._last_snapshot: Any = None
        self._pending_steps: list[Step] = []
        self._global_setup_captured = False
        self._failures: dict[str, str] = {}

        self.global_setup: list[Step] = []
        self.field_groups: list[FieldGroup] = []

    @property
    def unfound_fields(self) -> dict[str, FieldSpec]:
        return self._unfound

    @property
    def failures(self) -> dict[str, str]:
        """Why each still-unfound field failed, most recent attempt wins."""

        return {
            name: self._failures.get(name, "not located during exploration")
            for name in self._unfound
        }

    async def _narrate(self, step_record: AgentStepRecord, just_found: list[str]) -> None:
        """Say what happened, for whoever is watching a build that takes
        minutes. Never allowed to break the build: a progress indicator that can
        fail a run is worse than no progress indicator."""

        if self._on_progress is None:
            return
        self._steps.append({
            "n": step_record.step_number,
            "goal": step_record.next_goal,
            "actions": [a.get("type", "") for a in step_record.actions],
            "found": just_found,
        })
        del self._steps[:-_MAX_NARRATED_STEPS]
        try:
            await self._on_progress({
                "phase": "exploring",
                "steps": list(self._steps),
                "found": sorted(set(self._all_fields) - set(self._unfound)),
                "remaining": sorted(self._unfound),
            })
        except Exception:  # noqa: BLE001 - see docstring
            log.debug("onboard.progress_write_failed", exc_info=True)

    async def on_step(self, step_record: AgentStepRecord) -> None:
        if not self._unfound:
            return

        clicked_ref = capture.last_click_ref(step_record.actions)

        # The agent's dispatch mutated the page, so everything cached is stale.
        self._reader.invalidate()
        snapshot = await self._reader.snapshot()
        if snapshot is None:
            return

        # Refs were allocated against the state the agent *observed*, so the
        # pre-action snapshot is the right thing to resolve them against. On the
        # very first step there is no prior snapshot, and v1 simply dropped that
        # batch -- which threw away the cookie-banner dismissal, the single most
        # common thing `global_setup` is for. Falling back to the post-action
        # snapshot recovers it: a ref is a backend node id, stable across the
        # click for any element the click did not remove.
        reference = self._last_snapshot if self._last_snapshot is not None else snapshot
        for action_dict in step_record.actions:
            step = capture.stabilize_action_dict(action_dict, reference)
            if step is not None:
                self._pending_steps.append(step)
        self._last_snapshot = snapshot

        structured = await self._reader.structured_data()
        snapshot_text = serialize(snapshot).llm_text

        verified = await propose_and_verify(
            self._unfound,
            snapshot_text=snapshot_text,
            structured_data=structured,
            llm_config=self._llm_config,
            verify=self._reader.read,
            max_retries=self._verify_max_retries,
            verified_on=1,
        )
        for name in self._unfound:
            if name not in verified:
                self._failures[name] = "no proposed locator resolved on this page state"
        if not verified:
            await self._narrate(step_record, [])
            return

        # Only what actually reached a group counts as found. A column whose
        # rows could not be iterated resolved to a value and still has no way to
        # produce rows, so it stays unfound and reaches the assist loop -- rather
        # than being dropped for having been "verified".
        frozen = self._freeze(verified, snapshot=snapshot, clicked_ref=clicked_ref)
        for name in frozen:
            self._unfound.pop(name, None)
            self._failures.pop(name, None)
        await self._narrate(step_record, sorted(frozen))

    def _freeze(
        self,
        verified: dict[str, list[Candidate]],
        *,
        snapshot: Any,
        clicked_ref: str | None,
    ) -> set[str]:
        """Returns the names that made it into a group."""
        by_table: dict[str, dict[str, list[Candidate]]] = {}
        scalar: dict[str, list[Candidate]] = {}
        for name, candidates in verified.items():
            table = self._column_to_table.get(name)
            if table:
                by_table.setdefault(table, {})[name] = candidates
            else:
                scalar[name] = candidates

        # When a table field is satisfied by this batch, its trailing click is
        # the representative-option click -- superseded by the group's own
        # RepeatSpec, so it must not also appear as a reveal step or the option
        # gets clicked twice. Stripped once, shared by every group frozen here:
        # a scalar satisfied in the same batch as a table's representative click
        # would then miss that click in its own path. Rare, and an accepted
        # limitation rather than a silent misbehaviour.
        steps = list(self._pending_steps)
        if by_table and steps and steps[-1].op == "click":
            steps = steps[:-1]

        if not self._global_setup_captured:
            self.global_setup = steps
            group_steps: list[Step] = []
        else:
            group_steps = steps

        frozen: set[str] = set()

        if scalar:
            self.field_groups.append(
                self._group(list(scalar), scalar, group_steps, repeat=None)
            )
            frozen |= set(scalar)

        for table_name, columns in by_table.items():
            repeat: RepeatSpec | None = None
            if clicked_ref is not None:
                repeat = capture.generalize_option_locator(
                    snapshot=snapshot,
                    clicked_ref=clicked_ref,
                    row_field=table_name,
                    max_iterations=self._max_repeat_iterations,
                ) or capture.single_option_fallback(
                    snapshot=snapshot, clicked_ref=clicked_ref, row_field=table_name
                )
            if repeat is None:
                # `validate_document` rejects a table whose group has no repeat,
                # and a table field with no way to produce rows cannot resolve.
                # Leave it unfound so it reaches the assist loop with a reason,
                # rather than emitting a document that will not save.
                log.info(
                    "onboard.table_without_repeat", field=table_name,
                )
                for column in columns:
                    self._failures[column] = (
                        "found the value but could not work out how to iterate its rows"
                    )
                continue
            self.field_groups.append(
                self._group([table_name], columns, group_steps, repeat=repeat)
            )
            frozen |= set(columns)

        self._pending_steps = []
        self._global_setup_captured = True
        return frozen

    def _group(
        self,
        field_names: list[str],
        bindings: dict[str, list[Candidate]],
        steps: list[Step],
        *,
        repeat: RepeatSpec | None,
    ) -> FieldGroup:
        steps = list(steps)
        # The reveal has to have landed before the group reads. The driver
        # returns from a click as soon as it is dispatched, so without this the
        # group reads the page as it was before the drawer opened.
        if steps and steps[-1].op in capture.REVEALING_OPS:
            wait = capture.wait_step_for(
                candidates=[c for chain in bindings.values() for c in chain],
                repeat=repeat,
            )
            if wait is not None:
                steps.append(wait)
        return FieldGroup(
            group_id=f"group-{len(self.field_groups)}-{uuid.uuid4().hex[:6]}",
            field_names=field_names,
            bindings=bindings,
            steps=steps,
            repeat=repeat,
        )


async def onboard_recipe(
    *,
    recipe_id: str,
    tenant: str,
    name: str,
    url: str,
    fields: dict[str, FieldSpec],
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    llm_config: LLMConfig,
    sample_urls: list[str] | None = None,
    max_steps: int = DEFAULT_ONBOARD_MAX_STEPS,
    max_repeat_iterations: int = DEFAULT_MAX_REPEAT_ITERATIONS,
    on_progress: ProgressSink | None = None,
) -> tuple[Recipe, OnboardOutcome]:
    """Stages 2 and 3: recon, then explore-and-freeze.

    Raises `BlockedError` when recon finds a wall. Returns a recipe plus the
    fields that could not be located -- an incomplete recipe is still worth
    keeping, because the assist loop resolves the rest against the same live
    page rather than starting over.
    """

    samples = [u for u in (sample_urls or [url]) if u]
    reader = PageReader(
        session=session, registry=registry, driver=driver, base_url=url
    )

    state = ExplorationState(
        fields=fields,
        reader=reader,
        llm_config=llm_config,
        max_repeat_iterations=max_repeat_iterations,
        on_progress=on_progress,
    )

    run_result = await run_agent_loop(
        task=build_exploration_task(url, fields),
        session=session,
        registry=registry,
        driver=driver,
        llm_config=llm_config,
        max_steps=max_steps,
        output_schema=None,
        on_step=state.on_step,
    )

    # Recon runs *after* the loop, not before it: the loop is what navigates,
    # and classifying before it would classify whatever the reused warm profile
    # happened to be showing. A wall is only detectable once the target URL has
    # actually been requested.
    verdict = await classify_current_page(
        session=session, registry=registry, driver=driver, requested_url=url
    )
    if verdict.blocked and not state.field_groups:
        # Blocked AND nothing found. Blocked with fields found is a soft wall
        # that let the page through -- worth keeping what was learned.
        raise BlockedError(verdict.reason)

    # The mechanical assertions, now that the candidate chains exist: a field
    # that ended up with two *kinds* of locator can have them checked against
    # each other on every future run, which is the specific defence against a
    # locator drifting onto a sponsored ad's price and staying a well-typed
    # plausible number forever. The model-proposed ones need real collected
    # values to justify a bound, so they belong with the self-verification pass.
    checked = with_assertions(
        fields, baseline_assertions(fields, bindings_by_field(state.field_groups))
    )

    recipe = Recipe(
        recipe_id=recipe_id,
        tenant=tenant,
        name=name,
        version=1,
        target=derive_target(samples),
        fields=checked,
        sample_urls=samples,
        global_setup=state.global_setup,
        field_groups=state.field_groups,
        status="draft",
        health_status="healthy" if not state.unfound_fields else "degraded",
        built_under={"landed_url": verdict.landed_url, "page_verdict": verdict.verdict.value},
    )
    outcome = OnboardOutcome(
        unresolved=state.failures,
        landed_url=verdict.landed_url,
        steps_taken=len(run_result.steps.steps),
        agent_result=run_result.result,
    )
    return recipe, outcome
