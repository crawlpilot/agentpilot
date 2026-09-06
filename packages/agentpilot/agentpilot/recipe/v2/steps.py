"""Dispatching a v2 `Step` against the live page.

Every op maps onto an existing entry in `crawlpilot.tools.catalog` -- all 64 of
them were already there, and v1 could reach seven. Nothing here is new browser
capability; it is exposure of capability the driver has had all along.

Two ops are deliberately absent and their absence is the point: `execute_js`
and `wait_for_function` are `safety="sensitive"` in the catalog and both amount
to running arbitrary JS on a schedule. A recipe is authored by a model reading
an untrusted page, so neither belongs in its vocabulary. Lua is the sanctioned
escape hatch and it cannot touch the page.

`retry` applies to a single step and only for transient failures. It never
re-runs an earlier step: a dispatched batch is not idempotent, which is the
same reason the agent loop refuses to retry action dispatch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from agentpilot.recipe.v2.evaluate import PageReader
from agentpilot.recipe.v2.models import Locator, Step, StepOutcome
from crawlpilot.session.interactive import InteractiveSession, execute_on_session
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.driver import BrowserDriver

# Ops whose target is addressed by a CSS selector the driver resolves itself.
# `ax_role`/`text` targets are resolved here to a ref first, because the driver
# has no notion of them.
_SELECTOR_OPS = frozenset({
    "click", "double_click", "hover", "fill", "clear", "check", "uncheck",
    "scroll_into_view", "tap",
})


class StepError(Exception):
    """A step could not be completed. Whether that ends the run, the group, or
    nothing at all is the step's `on_error` policy, not this exception's
    business."""


@dataclass
class StepContext:
    session: InteractiveSession
    registry: RegistryProtocol
    driver: BrowserDriver
    reader: PageReader
    meta: dict[str, Any]
    defaults_timeout_ms: int = 10_000
    settle_ms: int = 250


def _template(value: Any, meta: dict[str, Any]) -> Any:
    """`{{meta.*}}` substitution, applied to step arg strings only.

    Never to a selector, an xpath, or Lua source -- those are the three places
    a caller-supplied string would become executable, and the boundary is worth
    more than the flexibility.
    """

    if not isinstance(value, str) or "{{meta." not in value:
        return value
    import re

    return re.sub(
        r"\{\{meta\.([A-Za-z0-9_]+)\}\}",
        lambda m: str(meta.get(m.group(1), "")),
        value,
    )


def _compose_css(target: Locator) -> str:
    """Fold a css `within` into the selector, which is what a css scope means.

    Without this, `within` was silently dropped for actions -- the reveal step
    would act on a match anywhere on the page rather than inside the container
    the author scoped it to. That is the same class of false positive `within`
    was introduced to fix on the read side.
    """

    if target.within is None:
        return target.selector or ""
    if target.within.kind != "css" or not target.within.selector:
        # Checked before reading `.selector`, because a non-css `within` has
        # none -- and falling through would drop the scope silently, which is
        # the very thing this function exists to stop.
        raise StepError(
            "a css action target can only be scoped by a css `within` with a "
            f"selector (got kind={target.within.kind!r})"
        )
    return f"{target.within.selector} {target.selector or ''}".strip()


async def _resolve_ref(locator: Locator, ctx: StepContext) -> str | None:
    """Turn an `ax_role`/`text` locator into a live ref. Returns None when it
    matches nothing -- the caller decides whether that is fatal."""

    from agentpilot.recipe.v2.tree import find_nodes, node_ref

    snapshot = await ctx.reader.snapshot()
    if snapshot is None:
        return None
    matches = find_nodes(snapshot, locator)
    if not matches:
        return None
    index = locator.index or 0
    if not -len(matches) <= index < len(matches):
        return None
    return node_ref(matches[index])


async def build_action(step: Step, ctx: StepContext) -> spi_actions.Action:  # noqa: C901
    """Compile one `Step` into the driver action it dispatches."""

    args = {k: _template(v, ctx.meta) for k, v in (step.args or {}).items()}
    timeout = step.timeout_ms if step.timeout_ms is not None else ctx.defaults_timeout_ms
    op = step.op
    target = step.target

    selector: str | None = None
    ref: str | None = None
    if target is not None:
        if target.kind in ("css", "xpath"):
            # The driver resolves a CSS selector itself. An xpath target has to
            # become a ref here, because `driver/queries.py` resolves selectors
            # with querySelector, not an XPath engine -- reads route around
            # that with generated JS, but an *action* needs a real node.
            if target.kind != "css":
                raise StepError(
                    "xpath targets are not yet dispatchable for actions -- the driver "
                    "resolves selectors with querySelector. Use a css or ax_role target, "
                    "or read (rather than act on) the xpath."
                )
            if target.index is not None:
                # ClickAction and friends take a selector or a ref, with no
                # notion of "the nth match", and CSS cannot express nth-match
                # in general. Silently acting on the first match instead would
                # make a dom repeat click the same option on every iteration
                # and return N identical rows -- a wrong answer that looks
                # like a right one, which is the failure this contract exists
                # to prevent. Refuse instead, and say what to use.
                raise StepError(
                    f"{op}: a css action target with index={target.index} is not "
                    "dispatchable -- the driver's actions take a selector or a ref, "
                    "not an nth match. Use an ax_role target (which resolves through "
                    "the fused tree and does support index), or add nth support to "
                    "crawlpilot's action layer."
                )
            selector = _compose_css(target)
        else:
            ref = await _resolve_ref(target, ctx)
            if ref is None:
                raise StepError(f"{op}: target matched no element")

    def _need_target() -> dict[str, Any]:
        if selector is not None:
            return {"selector": selector}
        if ref is not None:
            return {"ref": ref}
        raise StepError(f"{op} requires a target")

    if op == "navigate":
        return spi_actions.NavigateAction(
            url=str(args.get("url") or ""), timeout_ms=timeout,
            wait_until=args.get("wait_until", "load"),
        )
    if op == "click":
        return spi_actions.ClickAction(**_need_target(), all=bool(args.get("all", False)))
    if op == "double_click":
        return spi_actions.DoubleClickAction(**_need_target())
    if op == "hover":
        return spi_actions.HoverAction(**_need_target())
    if op == "fill":
        return spi_actions.FillAction(
            text=str(args.get("text", "")), clear=bool(args.get("clear", True)),
            **_need_target(),
        )
    if op == "clear":
        return spi_actions.ClearAction(**_need_target())
    if op == "check":
        return spi_actions.CheckAction(**_need_target())
    if op == "uncheck":
        return spi_actions.UncheckAction(**_need_target())
    if op == "scroll_into_view":
        return spi_actions.ScrollIntoViewAction(**_need_target())
    if op == "tap":
        return spi_actions.TapAction(**_need_target())
    if op == "select_option":
        if ref is None:
            raise StepError("select_option requires an ax_role/text target (it takes a ref)")
        values = args.get("values") or []
        return spi_actions.SelectOptionAction(ref=ref, values=list(values))
    if op == "press":
        return spi_actions.PressAction(key=str(args.get("key", "")))
    if op == "send_keys":
        return spi_actions.SendKeysAction(keys=str(args.get("keys", "")))
    if op == "scroll":
        return spi_actions.ScrollAction(
            direction=args.get("direction", "down"),
            pages=float(args.get("pages", 1.0)),
            ref=ref,
        )
    if op == "find_text":
        return spi_actions.FindTextAction(text=str(args.get("text", "")))
    if op == "swipe":
        return spi_actions.SwipeAction(
            direction=args.get("direction", "down"),
            distance=int(args.get("distance", 300)),
            ref=ref,
        )
    if op == "wait":
        return spi_actions.WaitAction(ms=int(args.get("ms", 0)) or None)
    if op == "wait_for_selector":
        if selector is None:
            raise StepError("wait_for_selector requires a css target")
        return spi_actions.WaitForSelectorAction(
            selector=selector, state=args.get("state", "visible"), timeout_ms=timeout,
        )
    if op == "wait_for_text":
        return spi_actions.WaitForTextAction(text=str(args.get("text", "")), timeout_ms=timeout)
    if op == "wait_for_url":
        return spi_actions.WaitForUrlAction(url=str(args.get("url", "")), timeout_ms=timeout)
    if op == "wait_for_load":
        return spi_actions.WaitForLoadAction(state=args.get("state", "load"), timeout_ms=timeout)
    if op == "dialog_accept":
        return spi_actions.DialogAcceptAction(prompt_text=args.get("prompt_text"))
    if op == "dialog_dismiss":
        return spi_actions.DialogDismissAction()
    if op == "new_tab":
        return spi_actions.NewTabAction(url=args.get("url"))
    if op == "switch_tab":
        return spi_actions.SwitchTabAction(page_id=str(args.get("page_id", "")))
    if op == "close_tab":
        return spi_actions.CloseTabAction(page_id=str(args.get("page_id", "")))
    if op == "download":
        if ref is None:
            raise StepError("download requires an ax_role/text target (it takes a ref)")
        return spi_actions.DownloadAction(ref=ref)

    raise StepError(f"unknown step op {op!r}")


# Ops whose effect the page renders asynchronously, so the next read has to
# wait for it. A subset of `_MUTATING`: `fill` and `clear` put text in an input
# and the value is readable the instant the action returns, while a click, a
# tab switch or a scroll starts work the browser finishes later.
#
# `navigate` is absent because the driver already awaits the new document
# (`patchright_driver`, the `_await_document` call) -- and, tellingly, does so
# ONLY when the URL changed. A click that opens a drawer is the same problem
# with no such handling, which is what `PageReader.settle` supplies.
_REVEALING = frozenset({
    "click", "double_click", "hover", "press", "send_keys", "select_option",
    "check", "uncheck", "scroll", "scroll_into_view", "find_text", "drag",
    "tap", "swipe", "dialog_accept", "dialog_dismiss", "new_tab",
    "switch_tab", "close_tab",
})

# Ops that change the page, and therefore invalidate the reader's caches.
_MUTATING = frozenset({
    "navigate", "click", "double_click", "fill", "clear", "press", "send_keys",
    "select_option", "check", "uncheck", "scroll", "find_text", "drag", "tap",
    "swipe", "dialog_accept", "dialog_dismiss", "new_tab", "switch_tab",
    "close_tab", "download", "hover", "scroll_into_view",
})


async def dispatch_step(step: Step, ctx: StepContext, index: int = 0) -> StepOutcome:
    """Run one step and report what happened.

    Never raises for an ordinary failure -- the outcome carries the status, and
    the caller applies the step's `on_error` policy. That split keeps the
    policy in one place instead of spread across every op.
    """

    started = time.monotonic()

    def _done(status: str, reason: str | None = None) -> StepOutcome:
        return StepOutcome(
            index=index, op=step.op, status=status,  # type: ignore[arg-type]
            duration_ms=int((time.monotonic() - started) * 1000),
            label=step.label, reason=reason,
        )

    for predicate in step.when:
        if not await ctx.reader.holds(predicate, meta=ctx.meta):
            return _done("skipped", f"guard {predicate.kind} not satisfied")

    attempts = step.retry.attempts if step.retry else 1
    backoff_ms = step.retry.backoff_ms if step.retry else 0
    last_error: str | None = None

    for attempt in range(max(1, attempts)):
        try:
            action = await build_action(step, ctx)
            await execute_on_session(
                ctx.session, [action], registry=ctx.registry, driver=ctx.driver
            )
            if step.op in _MUTATING:
                # Settle BEFORE invalidating, so the caches are dropped once
                # the page has finished changing rather than part-way through
                # it. Reversed, a snapshot taken during the re-render would be
                # cached as though it were the settled page.
                if ctx.settle_ms > 0 and step.op in _REVEALING:
                    await ctx.reader.settle(
                        quiet_ms=ctx.settle_ms, cap_ms=ctx.defaults_timeout_ms
                    )
                ctx.reader.invalidate()
            return _done("recovered" if attempt else "ok")
        except Exception as exc:  # noqa: BLE001 - a failed step is data, see docstring
            last_error = f"{type(exc).__name__}: {exc}"
            if step.op in _MUTATING:
                ctx.reader.invalidate()
            if attempt + 1 < max(1, attempts) and backoff_ms:
                await asyncio.sleep(backoff_ms / 1000)

    return _done("failed", last_error)


async def run_steps(
    steps: list[Step], ctx: StepContext, *, start_index: int = 0
) -> tuple[list[StepOutcome], str | None]:
    """Run a sequence, honouring each step's error policy.

    Returns the trace and, when the sequence was cut short, the policy that cut
    it (`"fail"` or `"skip_group"`) so the caller knows whether the run or only
    the group is affected.
    """

    trace: list[StepOutcome] = []
    for offset, step in enumerate(steps):
        outcome = await _run_one_with_repeat(step, ctx, start_index + offset)
        trace.extend(outcome)
        last = outcome[-1]
        if last.status == "failed":
            policy = step.effective_on_error
            if policy in ("fail", "skip_group"):
                return trace, policy
    return trace, None


async def _run_one_with_repeat(
    step: Step, ctx: StepContext, index: int
) -> list[StepOutcome]:
    """`repeat_until` + `max_repeats`: the lazy-load pattern.

    Hitting the cap without satisfying the predicate is NOT silent -- the final
    outcome says so, and the caller records it as a truncation. A recipe that
    cannot report finding too little is worse than one that fails.
    """

    if step.repeat_until is None:
        return [await dispatch_step(step, ctx, index)]

    outcomes: list[StepOutcome] = []
    for iteration in range(max(1, step.max_repeats)):
        if await ctx.reader.holds(step.repeat_until, meta=ctx.meta):
            return outcomes or [
                StepOutcome(index=index, op=step.op, status="skipped",
                            label=step.label, reason="repeat_until already satisfied")
            ]
        outcome = await dispatch_step(step, ctx, index)
        outcomes.append(outcome)
        if outcome.status == "failed":
            return outcomes
        if iteration + 1 >= max(1, step.max_repeats):
            if not await ctx.reader.holds(step.repeat_until, meta=ctx.meta):
                outcome.reason = (
                    f"repeat_until not satisfied after max_repeats={step.max_repeats}"
                )
                outcome.status = "recovered"
    return outcomes
