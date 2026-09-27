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

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import structlog

from agentpilot.agent.loop import run_agent_loop
from agentpilot.agent.state import AgentStepRecord
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.v2 import capture
from agentpilot.recipe.v2.assertions import baseline_assertions, with_assertions
from agentpilot.recipe.v2.build_trace import BuildTrace
from agentpilot.recipe.v2.classify import classify_current_page
from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.json_index import outline
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Recipe,
    RepeatSpec,
    Step,
    TargetSpec,
    UrlMatcher,
)
from agentpilot.recipe.v2.rows import RowBinding, is_open_map, propose_rows
from agentpilot.recipe.v2.schema import (
    FieldSpec,
    all_leaf_fields,
    column_to_table_map,
    render_fields_for_prompt,
)
from agentpilot.recipe.v2.selector_agent import propose_and_verify
from crawlpilot.dom.serializer import serialize
from crawlpilot.spi.dom_tree import SnapshotView
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

# How many times a field may fail to resolve before it is presumed not to be on
# this page at all.
#
# Without this, a field the caller asked for that the page simply does not have
# is re-proposed on every single exploration step: fifteen model calls, fifteen
# verification rounds, and fifteen identical failures, for a value that was
# never there. The contract had no way to say "absent" -- only "not found yet"
# -- so the search never stopped.
#
# One miss is uninformative: the field may be behind an accordion the agent has
# not opened yet, which is the entire reason the loop explores rather than
# reading once. Consecutive misses *with no progress anywhere on the page in
# between* are a different thing.
#
# Four, not two, and the difference was measured rather than argued. A real
# Walmart build gave up on `specifications` at step 3 and the agent opened the
# spec dialog -- thirty name/value rows -- at step 4. Two was calibrated against
# a scalar behind an accordion, which is one interaction away; a table is
# routinely behind a dialog the agent has to *find* first, and that took three
# steps here. The budget has to cover the search, not just the reveal.
#
# The cost of being wrong in this direction is bounded and small: at most four
# wasted proposals for a field that genuinely is not there, against the fifteen
# it was before any limit existed.
_MAX_FIELD_ATTEMPTS = 4

# The same budget for a field the page shows evidence of.
#
# `_MAX_FIELD_ATTEMPTS` exists to stop the build re-proposing a value the page
# does not have. It cannot tell that apart from a value the agent has not
# reached yet, and on a page whose sections open on click those look identical
# for the first several steps.
#
# Measured on an Ulta product page. The agent's own goals at steps 5 and 6 were
# "Verify How To Use and Ingredients sections are present/expanded" and "Scroll
# down to locate How To Use and Ingredients accordions". The counter hit four at
# step 6 and wrote off `ingredients`, `how_to_use` and `origin` -- one step
# before it got there. `looking_for` emptied, the build stopped asking, and the
# agent finished reporting "all fields located". All three were behind
# click-to-expand sections that were on the page the whole time.
#
# Still bounded: evidence buys more rope, not unlimited rope, because a field
# whose words appear in the site chrome would otherwise be proposed on every
# step for the life of the run.
_MAX_FIELD_ATTEMPTS_WITH_EVIDENCE = 10

# What fraction of a field's own vocabulary has to be on the page before
# "it may simply not be on this page" is refused as an explanation.
_EVIDENCE_RATIO = 0.5

# Operating one of these is the agent saying "show me something that was not
# there before", and whatever it revealed has not been looked at yet -- so the
# patience budget must start again for it. See `_look`'s counter.
#
# Deliberately narrower than `capture.REVEALING_OPS`, which includes `scroll`:
# an agent scrolls on most steps, and treating that as progress would stop the
# counter ever firing -- which is the loop `_MAX_FIELD_ATTEMPTS` exists to end.
# These are rare, deliberate interactions with a control.
# How many batches a dialog may stay open while the build still wants something
# that might be inside it. Bounded so a genuinely obstructing overlay is still
# cleared -- the agent gets a couple more observations, never an open-ended
# stall.
_MAX_DIALOG_DEFERRALS = 3

_OPENING_OPS = frozenset({
    "click", "double_click", "tap", "select_option", "check", "uncheck",
})

ProgressSink = Callable[[dict[str, Any]], Awaitable[None]]
"""Called after each exploration step with a snapshot of what the build is
doing. Injected rather than imported so the module has no opinion about where
progress is written -- the worker sends it to the run row, and a test can just
collect it in a list."""

StepDispatcher = Callable[[Step], Awaitable[Any]]
"""Runs one step against the live page. Injected for the same reason the reader
is: everything else here decides *what* a recipe should do, and a test of that
should not need a browser. Only the dialog dismissal uses it, and None simply
means the step is recorded without also being performed."""


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

    revealed_page_text: str = ""
    """The most complete page text the build ever saw.

    The judge reads ONE snapshot, taken after replay, with nothing necessarily
    expanded -- so for a field that never bound, nothing ever opened the panel
    holding it and the judge is shown a page the content genuinely is not on.
    It then reports `absent`, which is terminal: absence is excluded from
    repair on purpose and tells a person "it may simply not be on this page".

    Measured on a Zara shirt. The agent opened the accordion at step 7 and
    recorded "care instructions (machine wash max 30\u00baC, no bleach, iron max
    150\u00baC, dry clean with tetrachloroethylene, do not tumble dry)". The judge,
    reading a later unexpanded snapshot, replied "no washing, drying or ironing
    instructions appear anywhere in the rendered text. Stop looking for care on
    this page." Both were describing what they saw; only one of them had been
    shown the page with the panel open.

    Carrying the build's own best view means the judge cannot claim absence for
    something it was simply never shown."""

    page_json_outline: str = ""
    """What the selector prompts were shown of the page's structured data.

    The first question about a build that wrote a path resolving to nothing --
    or wrote none at all -- is whether the path was ever visible. Nothing else
    can answer it once the browser is gone."""

    trace: BuildTrace = field(default_factory=BuildTrace)
    """Every locator the build proposed and why each rejection happened.

    Carried out of the build rather than logged away, because "it is failing"
    was previously a report about a run whose reasoning no longer existed. The
    worker persists it and `build_asks` puts the relevant part in front of the
    person being asked -- which turns "find this" into "here is what I tried"."""

    @property
    def complete(self) -> bool:
        return not self.unresolved


_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "all",
    "this", "that", "its", "it", "page", "product", "value", "text", "from",
})


def _keyword_hits(spec: FieldSpec, haystack: str) -> tuple[int, int]:
    """How many of a field's own words appear in the text, and how many it has.

    Crude on purpose. It cannot tell you a field IS locatable, but a field whose
    own vocabulary is all over the page is not one the page lacks -- and that is
    the only claim it is used to refuse.
    """

    words = {
        w
        for w in re.split(r"[^a-z0-9]+", f"{spec.name} {spec.description}".lower())
        if len(w) > 2 and w not in _STOPWORDS
    }
    if not words:
        return 0, 0
    low = haystack.lower()
    return sum(1 for w in words if w in low), len(words)


def _keywords_in(spec: FieldSpec, haystack: str) -> str:
    """The same signal, formatted for the log.

    The question every failed field raises first is "was it even there?", and
    nothing in the trace answered it: a field that could not be located looks
    identical whether the reveal never happened or the selector was simply
    wrong. Those need opposite fixes.
    """

    hits, total = _keyword_hits(spec, haystack)
    if not total:
        return "-"
    low = haystack.lower()
    words = sorted(
        w
        for w in re.split(r"[^a-z0-9]+", f"{spec.name} {spec.description}".lower())
        if len(w) > 2 and w not in _STOPWORDS and w in low
    )
    return f"{hits}/{total} {words[:5]}"


def _same_document(before: str, after: str) -> bool:
    """Whether two URLs are the same document, ignoring query and fragment.

    `history.pushState` is how a single-page site records that a panel opened,
    a tab changed or an image viewer came up. None of those left the document,
    so the clicks that caused them still belong to it.
    """

    a, b = urlsplit(before), urlsplit(after)
    return (a.scheme, a.netloc, a.path) == (b.scheme, b.netloc, b.path)


def silently_unbound(
    fields: dict[str, FieldSpec],
    groups: list[FieldGroup],
    failures: dict[str, str],
) -> dict[str, str]:
    """Declared fields that reached neither a binding nor a failure.

    A field in that state has simply vanished: the recipe promises it, no run
    will ever produce it, and nothing anywhere says so. Measured on a Zara
    build, where `origin` disappeared exactly this way -- declared as an open
    key->value map, routed to `propose_rows` because that is what an open map
    is, declined by it, and then not put back among the scalars because
    `_column_to_table` has no entry for a field with no declared columns.

    Deliberately a sweep over the *outcome* rather than a fix to that path.
    Every route into this state ends the same way, and the next one will not be
    that one. An unresolved field becomes an ask a person can answer; a silent
    one cannot be answered because nobody is told.
    """

    bound = {
        name for group in groups for name in (*group.bindings, *group.field_names)
    }
    out: dict[str, str] = {}
    for name, spec in fields.items():
        if name in bound or name in failures:
            continue
        # A table is bound one column at a time and its own name may never
        # appear in `bindings`; it is satisfied when its columns are.
        columns = set(spec.type.columns)
        if columns and columns <= bound:
            continue
        out[name] = (
            "never located on this page, and no attempt was recorded against it -- "
            "it may not be present, or the exploration may have run out of steps "
            "before reaching it"
        )
    return out


def looks_variable(segment: str) -> bool:
    """Whether a path segment identifies *this* page rather than its kind.

    The distinction a marketplace recipe lives or dies on. In
    `/ip/<product-slug>/5013580` the `ip` says what kind of page this is and the
    other two say which one; a matcher that keeps all three accepts exactly one
    product, which is what `derive_target` used to emit from a single sample.

    Deliberately cautious in the safe direction. Calling a structural segment
    variable widens the recipe past what was verified; calling a variable one
    structural only leaves the matcher narrow, which is visible and editable.
    So a bare word is structural unless it carries positive evidence otherwise:
    digits (`productpage.129.html`, `a-p041.html`, `5013580`), a long hyphenated
    slug, or a long hex run.
    """

    if not segment:
        return False
    if any(c.isdigit() for c in segment) and len(segment) >= 3:
        return True
    if "-" in segment and len(segment) > 12:
        return True
    return len(segment) >= 16 and all(c in "0123456789abcdefABCDEF-" for c in segment)


def _generalized_prefix(url: str) -> str | None:
    """`https://host/path/up/to/` the first segment that identifies one page.

    None when every segment looks structural -- there is nothing to generalise,
    and the caller keeps its old everything-but-the-last-segment behaviour.
    """

    split = urlsplit(url)
    authority = f"{split.scheme}://{split.netloc}"
    segments = [s for s in split.path.split("/") if s]
    for index, segment in enumerate(segments):
        if looks_variable(segment):
            kept = "".join(f"/{s}" for s in segments[:index])
            return f"{authority}{kept}/"
    return None


def derive_target(urls: list[str]) -> TargetSpec:
    """A matcher for the pages this recipe applies to.

    `target_accepts` is a disjunction -- any matcher accepting the URL accepts
    it -- so this emits exactly one. Adding a host matcher "as well" would widen
    the recipe to the whole domain and make the path pattern decorative.

    With several URLs the pattern is their common prefix, trimmed back to a path
    boundary, which lands on the real shared shape
    (`https://www.zara.com/in/en/` for two products).

    With one URL the prefix is cut at the first segment that *identifies a
    page* rather than describing its kind -- see `looks_variable`. This is the
    difference between a marketplace recipe and a bookmark: keeping everything
    but the final segment turns
    `walmart.com/ip/<product-slug>/5013580` into a matcher for that one
    product's sub-paths, and `replay_recipe` then refuses every other Walmart
    product before it opens a browser. Cutting at the slug gives
    `walmart.com/ip/*`, which is the kind of page the recipe was actually built
    against. A URL with no variable-looking segment keeps the old rule, because
    there is nothing there to generalise from.

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

    if len(kept) == 1:
        generalized = _generalized_prefix(kept[0])
        if generalized is not None:
            return TargetSpec(match=[UrlMatcher(kind="glob", pattern=f"{generalized}*")])

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


def _without_last_click(steps: list[Step]) -> list[Step]:
    """The route with its final click removed, wait and all.

    A table satisfied by clicking one representative option has that click
    superseded by the group's own `RepeatSpec`, which clicks every option
    itself -- so leaving it in the reveal steps clicks the representative one
    twice. The `wait_for_selector` that followed it goes too: it was waiting on
    what that click revealed.

    Searches backwards rather than checking the last element, because the route
    now ends with the wait rather than the click.
    """

    for index in range(len(steps) - 1, -1, -1):
        if steps[index].op == "click":
            return steps[:index]
    return list(steps)


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
        dispatch_step: StepDispatcher | None = None,
    ) -> None:
        self._on_progress = on_progress
        self._dispatch_step = dispatch_step
        self._steps: list[dict[str, Any]] = []
        self._all_fields = fields
        self._unfound = all_leaf_fields(fields)
        self._column_to_table = column_to_table_map(fields)
        self._reader = reader
        self._llm_config = llm_config
        self._max_repeat_iterations = max_repeat_iterations
        self._verify_max_retries = verify_max_retries

        # The reveal route: everything the agent did to this page since it
        # loaded, in order. A group's steps are ALL of this -- see `_route_for`.
        self._path: list[Step] = []
        # The page the route belongs to. A navigation invalidates it.
        self._here: str | None = None
        # Whether the freshly-loaded page has been checked for a site popup yet.
        self._cleanup_probed = False
        self._pending_steps: list[Step] = []
        self._failures: dict[str, str] = {}
        # Consecutive misses per field, and what that count is allowed to
        # conclude. See `_MAX_FIELD_ATTEMPTS`.
        self._misses: dict[str, int] = {}
        # Consecutive batches a dialog dismissal has been held back, and
        # whether the batch just processed bound anything. See
        # `_close_any_dialog`.
        self._dialog_deferred = 0
        self._froze_this_batch = False
        self.presumed_absent: set[str] = set()
        # The page each group was frozen on. Two groups can only share a page
        # load if nothing navigated between them.
        self._group_urls: list[str] = []

        self.global_setup: list[Step] = []
        self.field_groups: list[FieldGroup] = []
        # Every locator proposed and every reason one was turned down. Collected
        # unconditionally: the reasons are already being computed and written for
        # a person to read, and throwing them away is what made "the build
        # failed" an unanswerable report. See `build_trace`.
        self.trace = BuildTrace()
        # Fields that have ever been bound, so a re-bind can be reported as the
        # loop it is rather than as fresh progress.
        self._ever_bound: set[str] = set()
        self.page_json_outline = ""
        self.revealed_page_text = ""
        """What the selector prompts were shown of the page's JSON, as first
        read. Saved as an artifact only when asked for -- it is large -- but
        collected either way, because by the time anyone wants it the browser is
        gone."""
        # Field name -> the pipeline its whole row set goes through. Only an
        # open map has one (`to_object`), and it has to reach the recipe's
        # `fields`, which is where `_replay_repeat` reads it from.
        self.field_transforms: dict[str, list[Any]] = {}
        # Field name -> its spec with columns the page cannot fill removed.
        # `verify_rows` drops a column that came back empty in every row rather
        # than rejecting the whole table for it, and the declared contract has
        # to follow the binding: `validate_document` refuses a table column with
        # no candidates, so narrowing one without the other trades an unbound
        # field for an unsaveable document.
        self.narrowed_specs: dict[str, FieldSpec] = {}

    @property
    def unfound_fields(self) -> dict[str, FieldSpec]:
        return self._unfound

    def _record_step(self, step: Step) -> None:
        """Add a step to the running route, and to the batch being frozen."""

        self._path.append(step)
        self._pending_steps.append(step)
        # DEBUG: the route is what replay re-runs to get back to the state a
        # field was readable in, so a reveal click that never lands here means
        # the recipe opens nothing and the field reads empty for ever -- while
        # the build itself looked fine, because the agent HAD opened the panel.
        # `stabilize_action_dict` returning None is the silent way that happens.
        log.info(
            "onboard.route_step",
            op=step.op,
            target=step.target.to_dict() if step.target else None,
            label=step.label,
            route_len=len(self._path),
        )

    async def _reset_route_if_navigated(self) -> None:
        """Drop the route when the page underneath it changed.

        A route is a sequence of things done to *one* page. Carrying it across a
        navigation would have replay re-load the URL, run `global_setup`, and
        then replay clicks that belong to a page it is no longer on -- which
        either fails silently or, worse, hits a same-named control somewhere
        else.

        The first page load is not a navigation in this sense: there is no route
        yet to lose.
        """

        here = await self._reader.current_url()
        if self._here is None:
            self._here = here
            return
        if here == self._here:
            return

        # A query or fragment change on the same document is not a navigation.
        #
        # This mattered on a real build and cost it every reveal step it had.
        # A modern site opens a panel and calls `history.pushState` -- the
        # accordion, the size guide, the image viewer all do it -- so the URL
        # changes while the document does not. Treating that as a navigation
        # threw away the route, INCLUDING the click that had just opened the
        # panel, and the fields frozen immediately afterwards were written with
        # no steps at all. The recipe then bound content that only exists after
        # a click and had no way to produce it: on every future run those
        # selectors read an empty page, silently, because every reveal step is
        # `optional`/`on_error: continue`.
        #
        # The docstring's reasoning still holds for a real navigation -- clicks
        # belonging to a page you have left are worse than useless. It does not
        # hold here: replay navigates to the recorded URL and re-runs these
        # steps, which is exactly what reproduces the state.
        if _same_document(self._here, here):
            log.info("onboard.route_kept_same_document", frm=self._here, to=here)
            self._here = here
            return

        log.info("onboard.route_reset", frm=self._here, to=here, dropped=len(self._path))
        self._here = here
        self._path = []
        self._pending_steps = []
        # A new page can throw its own popup, so it is worth probing again.
        self._cleanup_probed = False

    async def _probe_site_popup(self) -> None:
        """Clear whatever the SITE put in the way, and remember it as setup.

        `global_setup` and a group's reveal steps are different in kind and are
        kept apart rather than sliced out of one list:

        - **Setup is cleanup after a navigation.** A cookie wall, a newsletter
          modal, a region interstitial -- things the site shows on load, that
          nobody asked for, and that must be cleared again on *every* page load.
          Replay runs `global_setup` after each of its navigations for exactly
          this.
        - **A reveal opens something on a page already loaded** to expose data:
          an accordion, a "More details" dialog. It belongs to the field it
          reveals, and running it before every group would have one field's
          drawer covering another field's control.

        Telling them apart does not need a guess about intent, only about
        timing: this runs while the route is still empty, so anything open is
        something the page did to itself. Anything that appears later, the agent
        opened -- and `_close_any_dialog` handles that one, into the route.
        """

        if self._cleanup_probed or self._path:
            return
        self._cleanup_probed = True

        overlay = await self._reader.overlay()
        step = capture.dismiss_step_for(overlay)
        if step is None:
            return

        self.global_setup.append(step)
        log.info(
            "onboard.site_popup_dismissed",
            close=overlay.get("close"), locked=overlay.get("locked"), op=step.op,
        )
        if self._dispatch_step is None:
            return
        try:
            await self._dispatch_step(step)
        except Exception:  # noqa: BLE001 - a dismissal that fails is not a failed build
            log.debug("onboard.dismiss_failed", exc_info=True)
            return
        self._reader.invalidate()

    def _record_wait(
        self,
        scalar: dict[str, list[Candidate]],
        rows: dict[str, RowBinding],
        by_table: dict[str, dict[str, list[Candidate]]],
    ) -> None:
        """Append the wait for whatever this batch just revealed, to the route.

        One wait for the batch rather than one per group. The groups frozen
        together were revealed by the same interaction, and the question the
        wait answers -- has the reveal rendered? -- has one answer for all of
        them.
        """

        if not self._path or self._path[-1].op not in capture.REVEALING_OPS:
            return

        candidates = [c for chain in scalar.values() for c in chain]
        for columns in by_table.values():
            candidates += [c for chain in columns.values() for c in chain]
        repeat = next((b.repeat for b in rows.values()), None)
        for binding in rows.values():
            candidates += [c for chain in binding.bindings.values() for c in chain]

        wait = capture.wait_step_for(candidates=candidates, repeat=repeat)
        if wait is not None:
            self._path.append(wait)

    def _route_for(self) -> list[Step]:
        """The whole way from a loaded, cleaned-up page to the state being frozen.

        **Not the steps since the last freeze**, which is what this used to
        hand out. `replay.py` re-navigates before EVERY group and then runs
        `global_setup` plus that group's steps -- so a group's steps have to
        stand on their own from a cold page. Handing it only its own slice
        means a group frozen after an earlier one silently loses whatever the
        earlier freeze consumed: the Walmart specifications group carried
        `click "More details"` and not the accordion click that puts it on
        screen, so it read an empty page on every run and said nothing about
        why.

        Reveals only. Site-popup cleanup is `global_setup` and is never mixed in
        here -- see `_probe_site_popup`.

        The cost is that a group replays reveals it does not need. That is the
        right way round -- every step is `optional`/`on_error: continue`, so a
        redundant one costs seconds where a missing one costs the field -- and
        it is why closing a dialog afterwards matters: a redundant click into
        an open modal is where a harmless extra step turns harmful.
        """

        return list(self._path)

    def _declared_names(self, names: Any) -> list[str]:
        """Leaf names as the things the CALLER asked for.

        A table is located one column at a time, so `name` and `value` are how
        `specifications` gets found -- they are not fields anybody declared.
        Reporting them raw is how a build that was busily binding `composition`
        told the person watching it "found name, value", over and over, naming
        neither the field it had bound nor the two other tables that happen to
        declare columns by the same names.

        Shared by `failures` and `_narrate` so the two cannot drift: the rule was
        written down for the first and quietly not applied to the second.
        """

        return sorted({self._column_to_table.get(n) or n for n in names})

    @property
    def failures(self) -> dict[str, str]:
        """Why each still-unfound field failed, most recent attempt wins.

        Keyed by what the caller declared -- see `_declared_names`.
        """

        out: dict[str, str] = {}
        for name in self._unfound:
            key = self._column_to_table.get(name) or name
            out.setdefault(
                key, self._failures.get(name, "not located during exploration")
            )
        return out

    async def _narrate(self, step_record: AgentStepRecord, just_found: list[str]) -> None:
        """Say what happened, for whoever is watching a build that takes
        minutes. Never allowed to break the build: a progress indicator that can
        fail a run is worse than no progress indicator."""

        if self._on_progress is None:
            return

        bound = self._declared_names(just_found)
        # A field bound on a step that had already bound it is not progress, it
        # is a loop -- and it was completely invisible. A build that re-bound
        # `composition` every thirty seconds for its whole budget reported
        # "found name, value" each time, which reads like steady progress.
        again = sorted(f for f in bound if f in self._ever_bound)
        self._ever_bound.update(bound)

        self._steps.append({
            "n": step_record.step_number,
            "goal": step_record.next_goal,
            "actions": [a.get("type", "") for a in step_record.actions],
            "found": bound,
            # Why the fields this step did NOT bind were turned down, in the
            # verifier's own words. Without it a step says what it achieved and
            # nothing about what it attempted, which is the whole of "I cannot
            # see what is happening".
            "rejected": {
                field: self.trace.explain(field)
                for field in self._declared_names(self._unfound)
                if self.trace.explain(field)
            },
            **({"rebound": again} if again else {}),
        })
        del self._steps[:-_MAX_NARRATED_STEPS]
        try:
            await self._on_progress({
                "phase": "exploring",
                "steps": list(self._steps),
                "found": self._declared_names(
                    set(self._all_fields) - set(self._unfound)
                ),
                "remaining": self._declared_names(self._unfound),
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

        # Refs are only meaningful in the tree they were allocated from -- a ref
        # is `e{backend_node_id}`, and a click that re-renders a subtree gets
        # every id in it reassigned. So actions are resolved against the tree
        # the agent actually chose them from, which the loop now hands over.
        #
        # This used to resolve against "the snapshot taken at the end of the
        # previous step", which is a different tree taken at a different moment.
        # On a static page the two agree; on a React page they do not, and
        # `find_node` misses. The miss is silent -- `stabilize_action` returns
        # None both for an unresolvable ref and for an action that is simply not
        # a reveal step -- so a build recorded no steps at all and looked exactly
        # like a page that needed none. That is why the Walmart specifications
        # clicks never reached the recipe.
        #
        # `observed_tree` is None only for a step the loop failed to observe, and
        # falling back to the post-action snapshot there is better than dropping
        # the batch: it is what recovers a cookie-banner dismissal on step one.
        reference = step_record.observed_tree
        if reference is None:
            reference = snapshot
        # A route is only a route on one page. If the agent navigated, whatever
        # came before belongs to a page this one no longer is, and replaying it
        # after a fresh load would replay someone else's clicks. Checked BEFORE
        # this batch's steps are recorded, so a batch that navigated and then
        # acted keeps the acting part.
        await self._reset_route_if_navigated()

        # Whatever the site itself put in the way of a freshly loaded page.
        # Runs while the route is still empty, which is what distinguishes a
        # popup the page threw from a dialog the agent opened.
        await self._probe_site_popup()

        # This batch's steps only. `_freeze` clears it too, but a batch that
        # froze nothing never reaches `_freeze`.
        self._pending_steps = []
        acted = False
        opened = False
        for action_dict in step_record.actions:
            step = capture.stabilize_action_dict(action_dict, reference)
            if step is not None:
                self._record_step(step)
                acted = True
                opened = opened or step.op in _OPENING_OPS
            else:
                # DEBUG: the agent did something the route cannot express --
                # most often a ref-targeted click whose element could not be
                # given a stable selector. The panel opens for the build and
                # the recipe has no way to open it again, which reads later as
                # "the field is empty on the sample page" with nothing to say
                # why. This is the only place that fact exists.
                log.info(
                    "onboard.action_not_stabilized",
                    action=str(action_dict)[:300],
                )

        try:
            await self._look(
                step_record, snapshot=snapshot, clicked_ref=clicked_ref, opened=opened
            )
        finally:
            # After the reads, always. The batch that opens a dialog is very
            # often the batch that binds nothing -- which is exactly when this
            # used not to run, because it was tied to a successful freeze. A
            # dialog blocks the agent either way.
            if acted:
                await self._close_any_dialog()

    async def _look(
        self,
        step_record: AgentStepRecord,
        *,
        snapshot: Any,
        clicked_ref: str | None,
        opened: bool = False,
    ) -> None:
        """Propose, verify and freeze whatever this page state can satisfy.

        `opened` says this batch operated a control (see `_OPENING_OPS`), which
        is the agent asserting the page now shows something it did not before.
        """

        # Reset per batch: `_look` can return early (nothing left to look for),
        # and a stale value would tell `_close_any_dialog` this batch read the
        # dialog when it never looked.
        self._froze_this_batch = False
        structured = await self._reader.structured_data()
        # The structure a SELECTOR is written against, not the click-target
        # list. See `SnapshotView.for_authoring`.
        snapshot_text = serialize(snapshot, view=SnapshotView(for_authoring=True, content_only=True)).llm_text

        # Exactly what the selector prompts were shown of the page's JSON, kept
        # once. When a build writes a path that resolves to nothing -- or writes
        # none at all -- the first question is whether the path was even visible,
        # and this is the only thing that can answer it. `outline` replaced a raw
        # 12 000-character prefix of a 352 KB blob for precisely that reason.
        if not self.page_json_outline:
            self.page_json_outline = outline(structured, wanted=self._all_fields)
        # The fullest view of the page this build ever had. Longest wins as a
        # proxy for "most revealed": every accordion the agent opens adds text
        # and none of it is ever removed. See `OnboardOutcome.revealed_page_text`.
        if len(snapshot_text) > len(self.revealed_page_text):
            self.revealed_page_text = snapshot_text

        # A field presumed absent is not proposed again. This is the whole of
        # the fix for the loop: the model was being asked, every step, to find
        # something that is not there, and answering honestly ("I could not")
        # did nothing to stop it being asked again.
        looking_for = {
            name: spec
            for name, spec in self._unfound.items()
            if name not in self.presumed_absent
        }
        # DEBUG: what this page state was actually asked to satisfy, and
        # whether the words each field is described by are even present in the
        # text the model will be shown. A field that fails here with its
        # keywords MISSING is a page-state problem (the reveal did not happen,
        # or it happened and this snapshot predates it); one that fails with
        # them PRESENT is a selector problem. Those need opposite fixes and the
        # trace could not previously tell them apart.
        log.info(
            "onboard.look",
            step=step_record.number if hasattr(step_record, "number") else None,
            clicked_ref=clicked_ref,
            looking_for=sorted(looking_for),
            presumed_absent=sorted(self.presumed_absent),
            snapshot_chars=len(snapshot_text),
            structured_keys=sorted(structured)[:12] if structured else [],
            keywords_present={
                name: _keywords_in(spec, snapshot_text)
                for name, spec in looking_for.items()
            },
        )

        if not looking_for:
            await self._narrate(step_record, [])
            return

        # A table's columns are ONE question, asked of `propose_rows`. Asking
        # for them individually here is what produced a spec table as two
        # document-wide `all: true` lists zipped into a single row -- see
        # `rows.py`. Scalars are unaffected and go the way they always did.
        scalars, tables = self._split_by_shape(looking_for)

        row_bindings: dict[str, RowBinding] = {}
        for table_name, table_spec in tables.items():
            binding = await propose_rows(
                table_spec,
                snapshot_text=snapshot_text,
                structured_data=structured,
                reader=self._reader,
                llm_config=self._llm_config,
                page_url=self._reader.base_url,
                max_rows=self._max_repeat_iterations,
                trace=self.trace,
            )
            if binding is not None:
                row_bindings[table_name] = binding
                continue
            # Not row-shaped on this page. That is a real answer -- a size/price
            # variant set genuinely has to be clicked through -- so its columns
            # fall back to the per-column path, where the agent's representative
            # click becomes a `dom` repeat.
            fallback = {
                name: spec
                for name, spec in looking_for.items()
                if self._column_to_table.get(name) == table_name
            }
            scalars.update(fallback)
            if not fallback:
                # An open key -> value map has no columns of its own, so the
                # comprehension above yields nothing and there is no second path
                # to try: `propose_rows` is the only way a specifications block
                # can ever bind. Left alone it is re-asked every batch, burns its
                # attempts, and is written off as "may simply not be there" --
                # which is the one description that is certainly wrong for a
                # block whose heading is on the page.
                #
                # Say what actually happened instead. `build_asks` carries this
                # into the ask, where "point at the section" with shape `map` is
                # the answer the panel already has for it.
                self._failures[table_name] = (
                    "this page does not present it as rows, and an open "
                    "key -> value map has no other way to bind -- point at the "
                    "section it lives in"
                )
                log.info("onboard.open_map_not_row_shaped", field=table_name)

        verified = (
            await propose_and_verify(
                scalars,
                snapshot_text=snapshot_text,
                structured_data=structured,
                llm_config=self._llm_config,
                verify=self._reader.read,
                max_retries=self._verify_max_retries,
                verified_on=1,
                # `url_resolve` needs it, and without it every url field would
                # validate a relative href against nothing and pass.
                page_url=self._reader.base_url,
                trace=self.trace,
                # Ask the page where each DOM match lives, so a selector that
                # reaches across the whole document is refused and one that does
                # not is confined to the container it found its values in. See
                # `selector_agent.scope_problem`.
                probe=self._reader.read_with_scope,
            )
            if scalars
            else {}
        )
        # Only what actually reached a group counts as found. A column whose
        # rows could not be iterated resolved to a value and still has no way to
        # produce rows, so it stays unfound and reaches the assist loop -- rather
        # than being dropped for having been "verified".
        #
        # Freezing happens BEFORE the patience bookkeeping below, and that order
        # is the whole of the fix. There are two notions of "found" here --
        # "the selector agent resolved it" and "it became a binding" -- and the
        # counters used to be advanced from the first one, twenty lines before
        # `_freeze` decided the second.
        frozen: set[str] = set()
        if verified or row_bindings:
            frozen = await self._freeze(
                verified, row_bindings, snapshot=snapshot, clicked_ref=clicked_ref,
                option_tree=step_record.observed_tree,
            )
        # DEBUG: the three-way gap that matters. `asked` is what this page state
        # was told to find; `verified` is what the selector agent could resolve;
        # `frozen` is what actually became a binding. A field in `asked` but not
        # `verified` is a locator problem (see `trace` for each rejection); one
        # verified but not frozen reached a value and still has no way to
        # produce it -- a table whose rows would not iterate, most often -- and
        # those two used to be indistinguishable from the outside.
        log.info(
            "onboard.look_result",
            asked=sorted(looking_for),
            verified=sorted(verified),
            rows_bound=sorted(row_bindings),
            frozen=sorted(frozen),
            verified_not_frozen=sorted(set(verified) - frozen),
            asked_not_verified=sorted(set(looking_for) - set(verified) - set(row_bindings)),
            bound_to={
                name: chain[0].locator.to_dict()
                for name, chain in verified.items()
                if chain
            },
        )
        # Read by `_close_any_dialog`, which runs after this in the same batch:
        # a dialog that yielded something has been read, one that yielded
        # nothing has not.
        self._froze_this_batch = bool(frozen)
        for name in frozen:
            self._unfound.pop(name, None)
            self._failures.pop(name, None)

        # A step that bound something is evidence the page moved somewhere
        # useful, so every field gets its patience back: what was invisible a
        # moment ago may be on screen now. A step that bound nothing is not, so
        # the counters advance.
        #
        # Measured, on a Zara build whose `Composition, care & origin` accordion
        # never yielded a `RepeatSpec`: its two columns verified on every single
        # step, so `_misses` was cleared on every single step and
        # `_MAX_FIELD_ATTEMPTS` could never fire. The field was re-proposed for
        # the whole budget, and because `progressed` was also true every step,
        # every OTHER field's counter was pinned at zero too -- one unfreezable
        # table stopped the build giving up on anything at all. The agent, told
        # each time that a field it had just located was still missing, spent its
        # remaining steps hunting for "fresh refs" it did not need.
        # Resolved to a value and still not bound -- the table whose rows could
        # not be iterated. Giving up on these is right, but describing them as
        # absent is not: the value is demonstrably on the page, and telling a
        # person it "may simply not be there" invites them to drop a field that
        # only needed the reveal recording. `_freeze` already wrote the accurate
        # reason, so it is left alone.
        resolved = set(verified) | {
            column
            for table in row_bindings
            for column in self._all_fields[table].type.columns
        }
        unbound = resolved - frozen

        # A batch that opened something counts as progress even if it bound
        # nothing yet. The reveal and the read are two different steps -- the
        # click lands, and only the NEXT observation carries what it exposed --
        # so charging the opening step as a miss writes a field off for the one
        # action most likely to find it.
        #
        # Measured on a Zara build: `care` and `origin` exhausted their four
        # attempts on `extract` steps that returned nothing, were presumed
        # absent at step 6, and the accordion holding both was clicked open at
        # step 7. The very next observation carried "Do not wash ... Made in
        # China" and nothing was looking for them any more.
        progressed = bool(frozen) or opened
        for name in looking_for:
            if name in frozen:
                self._misses.pop(name, None)
                continue
            if progressed:
                self._misses[name] = 0
                if name not in unbound:
                    self._failures[name] = "not found yet on this page state"
                continue
            missed = self._misses.get(name, 0) + 1
            self._misses[name] = missed
            # A field whose own words are on the page is not one the page
            # lacks, whatever the counter says -- see
            # `_MAX_FIELD_ATTEMPTS_WITH_EVIDENCE`.
            hits, total = _keyword_hits(looking_for[name], snapshot_text)
            evident = bool(total) and hits / total >= _EVIDENCE_RATIO
            ceiling = (
                _MAX_FIELD_ATTEMPTS_WITH_EVIDENCE if evident else _MAX_FIELD_ATTEMPTS
            )
            if evident and missed == _MAX_FIELD_ATTEMPTS:
                log.info(
                    "onboard.absence_refused_words_on_page",
                    field=name, attempts=missed, keywords=f"{hits}/{total}",
                )
            if missed >= ceiling:
                self.presumed_absent.add(name)
                if name not in unbound:
                    self._failures[name] = (
                        f"looked for it {missed} times and found nothing -- it may "
                        "simply not be on this page"
                    )
                log.info(
                    "onboard.field_presumed_absent",
                    field=name, attempts=missed, resolved_but_unbound=name in unbound,
                )
            elif name not in self._failures:
                self._failures[name] = "no proposed locator resolved on this page state"

        await self._narrate(step_record, sorted(frozen))

    def _split_by_shape(
        self, looking_for: dict[str, FieldSpec]
    ) -> tuple[dict[str, FieldSpec], dict[str, FieldSpec]]:
        """Leaves that are their own field, and the table fields whose columns
        are among them.

        The table's OWN spec is what comes back -- columns and all -- because
        the question `propose_rows` asks is about the table, not about any one
        column of it.
        """

        scalars: dict[str, FieldSpec] = {}
        tables: dict[str, FieldSpec] = {}
        for name, spec in looking_for.items():
            table = self._column_to_table.get(name)
            if table and table in self._all_fields:
                tables[table] = self._all_fields[table]
            elif is_open_map(spec):
                # A specifications block: rows underneath, a `{name: value}` map
                # to the caller. It is the same question a table asks -- where
                # are the rows, and where in a row is each side of the pair --
                # so it goes to the same place, and `to_object` collapses the
                # rows afterwards. Asked as a scalar it could only ever return
                # the block's own heading.
                tables[name] = spec
            else:
                scalars[name] = spec
        return scalars, tables

    async def _freeze(
        self,
        verified: dict[str, list[Candidate]],
        row_bindings: dict[str, RowBinding] | None = None,
        *,
        snapshot: Any,
        clicked_ref: str | None,
        option_tree: Any = None,
    ) -> set[str]:
        """Returns the names that made it into a group.

        `option_tree` is the tree the agent's `clicked_ref` was allocated
        against, and generalising an option set has to use it rather than
        `snapshot`.

        The distinction is the one `on_step` documents at length and this used to
        ignore: a ref is `e{backend_node_id}`, and a click that re-renders a
        subtree gets every id in it reassigned. `snapshot` is taken *after* the
        click, so on any React page `find_node(snapshot, clicked_ref)` misses --
        `generalize_option_locator` and `single_option_fallback` both return
        None, the table gets no `RepeatSpec`, and it is reported as "could not
        work out how to iterate its rows" on every step for the rest of the
        build. `stabilize_action_dict` was fixed to resolve against the observed
        tree; this was left behind, so the fix only covered recording the step
        and not binding what the step revealed.
        """
        rows = dict(row_bindings or {})
        by_table: dict[str, dict[str, list[Candidate]]] = {}
        scalar: dict[str, list[Candidate]] = {}
        for name, candidates in verified.items():
            table = self._column_to_table.get(name)
            if table:
                by_table.setdefault(table, {})[name] = candidates
            else:
                scalar[name] = candidates

        # The reveal has to have landed before anything reads, and -- because
        # routes are cumulative -- before the NEXT reveal acts. So the wait goes
        # into the route, not just into this group's copy of it. A route of two
        # clicks with no wait between them races: the driver returns from a
        # click as soon as it is dispatched, so the second one fires against the
        # page as it was before the first drawer opened.
        self._record_wait(scalar, rows, by_table)

        group_steps = self._route_for()

        # When a table is satisfied by a representative CLICK, that click is
        # superseded by the group's own RepeatSpec and must not also appear as a
        # reveal step, or the option gets clicked twice. Stripped from this
        # group's copy only -- it is a real interaction and the route keeps it
        # for whatever is frozen later.
        #
        # Only for the click-through kind. A `json` or `dom_rows` table has no
        # representative click, so the trailing click is an ordinary reveal --
        # stripping it would delete the very step that opened the drawer the
        # rows are in.
        if by_table:
            group_steps = _without_last_click(group_steps)

        frozen: set[str] = set()
        here = await self._reader.current_url()

        if scalar:
            if not await self._absorb(scalar, group_steps, here):
                self.field_groups.append(
                    self._group(list(scalar), scalar, group_steps, repeat=None)
                )
                self._group_urls.append(here)
            frozen |= set(scalar)

        for table_name, binding in rows.items():
            # A `json` or `dom_rows` table reads what is already on the page, so
            # unlike the click-through kind it mutates nothing -- but it still
            # gets its own group, because `_replay_repeat` and `_replay_scalar`
            # are alternatives for one group and a group cannot do both.
            self.field_groups.append(
                self._group(
                    [table_name], binding.bindings, group_steps, repeat=binding.repeat
                )
            )
            self._group_urls.append(here)
            frozen |= set(binding.bindings)
            # `binding.bindings` is keyed by COLUMN, and for a declared table
            # the columns are the leaves -- `all_leaf_fields` replaces the table
            # with them -- so freezing those is freezing the right names.
            #
            # An open key -> value map is the exception, and it cost a whole
            # build. Its leaf is the field itself; its columns are the synthetic
            # `name`/`value` pair `MAP_COLUMNS` invents, which are in no
            # `_unfound` and belong to nobody. So a specifications block bound
            # its rows, was never marked found, and was asked for again on the
            # very next batch -- binding the same 17 rows over and over until
            # the step budget ran out, with the field still "remaining" at the
            # end. Observed on an Amazon product page.
            if is_open_map(self._all_fields[table_name]):
                frozen.add(table_name)
            # `to_object` for an open map, so the rows underneath become the
            # `{name: value}` the caller asked for. Carried out to the recipe's
            # field spec, which is where `_replay_repeat` looks for it.
            if binding.field_transform:
                self.field_transforms[table_name] = binding.field_transform
            # Columns this page cannot fill, dropped from the contract as well
            # as from the bindings -- see `narrowed_specs`.
            if binding.narrowed_spec is not None:
                self.narrowed_specs[table_name] = binding.narrowed_spec
                self._all_fields[table_name] = binding.narrowed_spec
            log.info(
                "onboard.table_bound_as_rows",
                field=table_name, kind=binding.repeat.kind, rows=len(binding.rows),
                reshape=[t.op for t in binding.field_transform] or None,
            )

        for table_name, columns in by_table.items():
            # Every declared column, or none of them. A table bound to half its
            # columns emits rows in a shape the caller did not ask for, and the
            # missing column looks like the page not having a value rather than
            # like the recipe never looking for one.
            #
            # Observed rather than anticipated: a real Walmart build bound
            # `name` and had `value` rejected by the scalar/list guard, and
            # froze a `dom` repeat producing `{name: ...}` rows. Waiting costs
            # nothing -- the columns keep their place in `_unfound` and the next
            # exploration step, or a person, can still satisfy them.
            declared = set(self._all_fields[table_name].type.columns)
            missing = declared - set(columns)
            if missing:
                log.info(
                    "onboard.table_partially_bound",
                    field=table_name, missing=sorted(missing), got=sorted(columns),
                )
                for column in columns:
                    self._failures[column] = (
                        "found this column but not "
                        + ", ".join(sorted(missing))
                        + " -- a table needs every column it declares"
                    )
                continue

            repeat: RepeatSpec | None = None
            if clicked_ref is not None:
                # Against the tree the ref came from -- see this method's
                # docstring for why `snapshot` cannot answer this.
                tree = option_tree if option_tree is not None else snapshot
                repeat = capture.generalize_option_locator(
                    snapshot=tree,
                    clicked_ref=clicked_ref,
                    row_field=table_name,
                    max_iterations=self._max_repeat_iterations,
                ) or capture.single_option_fallback(
                    snapshot=tree, clicked_ref=clicked_ref, row_field=table_name
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
            # Never merged. A repeat mutates the page -- it clicks through an
            # option set -- so anything sharing its page load would be read
            # against whichever option happened to be selected last.
            self.field_groups.append(
                self._group([table_name], columns, group_steps, repeat=repeat)
            )
            self._group_urls.append(here)
            frozen |= set(columns)

        self._pending_steps = []
        return frozen

    async def _close_any_dialog(self) -> None:
        """Shut a modal this batch left open.

        Two things happen and both are needed. The dismissal is **dispatched**,
        because otherwise the agent spends the rest of its step budget clicking
        at a page under an overlay -- the exact failure that produced a build
        where the specifications dialog was never closed. And it is **appended
        to the route**, so replay does the same thing at the same point.

        Called after every batch that did something, not only after a
        successful freeze. A dialog blocks the agent whether or not the freeze
        worked, and the batch that opens one is very often the batch that fails
        to bind anything -- which is precisely when this used not to run.

        Appending to the route, rather than to the group just frozen, is the
        only correct position: a group reads *after* its steps run, and the
        fields just frozen are usually inside the dialog. So this group keeps
        the open dialog it read from, and every group frozen later gets the
        dismissal ahead of its own reveals.
        """

        overlay = await self._reader.overlay()
        step = capture.dismiss_step_for(overlay)
        if step is None:
            self._dialog_deferred = 0
            return

        # "Closed once the fields inside it have been read" -- which is what
        # this was always for, and the case it missed is a dialog that has not
        # been read yet.
        #
        # A batch that bound something got what it opened the dialog for, and
        # closing is right. A batch that bound NOTHING while fields are still
        # outstanding has not read it: `_look` runs once per batch with
        # `max_retries=1`, so a field inside got exactly two proposals ever, and
        # if the first went on a correctable mistake -- an alternation, a
        # caption -- the second was the last before the content left the DOM.
        # Measured twice on the same Zara page: click, two attempts, close, and
        # every later attempt unwinnable because `care` was no longer rendered.
        #
        # Bounded, so the protection survives: an overlay genuinely in the way
        # costs at most `_MAX_DIALOG_DEFERRALS` further observations rather than
        # the rest of the run.
        if (
            not self._froze_this_batch
            and self._unfound
            and self._dialog_deferred < _MAX_DIALOG_DEFERRALS
        ):
            self._dialog_deferred += 1
            log.info(
                "onboard.dialog_kept_open",
                still_wanted=sorted(self._unfound),
                deferred=self._dialog_deferred,
                locked=overlay.get("locked"),
            )
            return

        self._dialog_deferred = 0
        self._path.append(step)
        log.info(
            "onboard.dialog_left_open",
            close=overlay.get("close"), locked=overlay.get("locked"), op=step.op,
        )
        if self._dispatch_step is None:
            return
        try:
            await self._dispatch_step(step)
        except Exception:  # noqa: BLE001 - a dismissal that fails is not a failed build
            log.debug("onboard.dismiss_failed", exc_info=True)
            return
        self._reader.invalidate()

    async def _absorb(
        self,
        scalar: dict[str, list[Candidate]],
        group_steps: list[Step],
        here: str,
    ) -> bool:
        """Fold these fields into the previous group instead of starting a new
        one. True when it worked.

        **Every group costs a page load.** `replay.py` re-navigates before each
        one so that one group's clicks cannot leak into the next, which is the
        right default and the wrong price to pay when nothing needed isolating.
        A build that opens four accordions on one page produced four groups and
        therefore four full page loads, to read a page that never changed.

        Merging is allowed only when the evidence says the page state is
        genuinely shared:

        - **Nothing navigated.** If the URL moved, the earlier fields belong to
          a different page. Merging would re-read them after the navigation and
          silently collect the wrong page's values -- a wrong answer that looks
          like a right one.
        - **The earlier fields still read.** This is checked, not assumed.
          Opening a second accordion usually leaves the first open; switching to
          a second *tab* usually does not, and nothing about a step says which
          kind it is. So the previous group's bindings are re-read against the
          page as it stands now, which is the only thing that actually answers
          the question -- and it is free, because the page is already open.

        A group carrying a repeat is never a merge target: it clicks through an
        option set, so anything sharing its page load would be read against
        whichever option was selected last.
        """

        if not self.field_groups:
            return False
        previous = self.field_groups[-1]
        if previous.repeat is not None:
            return False
        if self._group_urls and self._group_urls[-1] != here:
            log.info("onboard.no_merge_navigated", frm=self._group_urls[-1], to=here)
            return False

        if group_steps and not await self._still_reads(previous):
            log.info("onboard.no_merge_state_lost", group=previous.group_id)
            return False

        # The newer route REPLACES the older one; it does not extend it.
        #
        # Both groups' steps are now the whole way from a cold page (see
        # `_route_for`), and the merged group is frozen at the later point, so
        # the later route is the one that reaches it. Concatenating them -- what
        # this did while a group carried only its own slice -- would replay the
        # shared prefix twice and add a second `wait_for_selector` for the same
        # reveal.
        bindings = {**previous.bindings, **scalar}
        self.field_groups[-1] = self._group(
            [*previous.field_names, *scalar],
            bindings,
            group_steps,
            repeat=None,
            group_id=previous.group_id,
        )
        log.info(
            "onboard.merged_group",
            group=previous.group_id, added=sorted(scalar), steps=len(group_steps),
        )
        return True

    async def _still_reads(self, group: FieldGroup) -> bool:
        """Whether everything the group already binds still resolves right now.

        The empirical answer to "did the step I just ran close what the last one
        opened?", which no amount of reading the step could tell us.
        """

        from agentpilot.recipe.v2.resolve import is_empty

        for chain in group.bindings.values():
            if not chain:
                continue
            try:
                value = await self._reader.read(chain[0].locator)
            except Exception:  # noqa: BLE001 - a locator that raises has not read
                return False
            if is_empty(value):
                return False
        return True

    def _group(
        self,
        field_names: list[str],
        bindings: dict[str, list[Candidate]],
        steps: list[Step],
        *,
        repeat: RepeatSpec | None,
        group_id: str | None = None,
    ) -> FieldGroup:
        # Tidying that happened after the last reveal is not how the field got
        # on screen -- it is how it came off. The route is the whole path since
        # the page loaded, so a dismissal dispatched mid-exploration and the
        # agent's own clicks on close controls both reach here. Kept as the
        # group's TEARDOWN rather than discarded: closing what was opened is the
        # whole of what lets the next group share this page load instead of
        # reloading it. See `capture.split_cleanup`.
        steps, teardown = capture.split_cleanup(list(steps))
        revealed = capture.document_scoped_selector(
            candidates=[c for chain in bindings.values() for c in chain], repeat=repeat
        )
        # Replay loads the page once and runs every group against it, so this
        # route may well run against a page some earlier group already opened.
        # A second click on a toggle closes it, so each reveal is made to fire
        # only while the thing it reveals is still hidden.
        steps = capture.guard_reveals(steps, revealed)
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
            group_id=group_id or f"group-{len(self.field_groups)}-{uuid.uuid4().hex[:6]}",
            field_names=field_names,
            bindings=bindings,
            steps=steps,
            teardown=teardown,
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

    from agentpilot.recipe.v2.steps import StepContext, dispatch_step

    samples = [u for u in (sample_urls or [url]) if u]
    reader = PageReader(
        session=session, registry=registry, driver=driver, base_url=url
    )
    step_ctx = StepContext(
        session=session, registry=registry, driver=driver, reader=reader, meta={}
    )

    state = ExplorationState(
        fields=fields,
        reader=reader,
        llm_config=llm_config,
        max_repeat_iterations=max_repeat_iterations,
        on_progress=on_progress,
        dispatch_step=lambda step: dispatch_step(step, step_ctx, 0),
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
    # A field whose rows have to be reshaped before they are the thing the
    # caller asked for -- an open map, collapsed by `to_object`. Applied here
    # rather than in `_freeze` because it belongs to the FIELD, and `_freeze`
    # only ever builds groups.
    # A table narrowed to the columns this page actually carries. Applied
    # before the transforms below so a narrowed spec does not overwrite one.
    for name, narrowed in state.narrowed_specs.items():
        if name in checked:
            checked[name] = replace(
                checked[name], type=narrowed.type, assertions=narrowed.assertions
            )
    for name, pipeline in state.field_transforms.items():
        if name in checked:
            checked[name] = replace(checked[name], transform=pipeline)

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
    for field_name, reason in silently_unbound(
        checked, state.field_groups, state.failures
    ).items():
        state.failures[field_name] = reason
        log.info("onboard.field_silently_unbound", field=field_name)

    outcome = OnboardOutcome(
        unresolved=state.failures,
        landed_url=verdict.landed_url,
        steps_taken=len(run_result.steps.steps),
        agent_result=run_result.result,
        trace=state.trace,
        page_json_outline=state.page_json_outline,
        revealed_page_text=state.revealed_page_text,
    )
    return recipe, outcome
