"""`crawlpilot.verbs` -- the verb vocabulary, independent of any transport.

The point of the split is that a subclass supplying one method gets all sixty
verbs. So these tests supply exactly that -- a `SessionVerbs` whose `execute`
records the batch and returns a canned result -- and drive it with no browser,
no driver and no network anywhere. If that stops being possible, the layer has
re-acquired a transport dependency and the remote client is impossible again.
"""

from __future__ import annotations

import dataclasses

import pytest

from crawlpilot.spi.actions import ActionResult
from crawlpilot.spi.dom_tree import RefInfo, Snapshot
from crawlpilot.tools import ToolSpec, browser_tools
from crawlpilot.verbs import SessionVerbs


class FakeSession(SessionVerbs):
    """A transport that goes nowhere. The whole contract is `execute`."""

    def __init__(self, result: ActionResult | None = None) -> None:
        self.batches: list[list] = []
        self._result = result or ActionResult()

    async def execute(self, actions, *, page_id=None):  # type: ignore[no-untyped-def]
        self.batches.append(list(actions))
        return self._result


# ------------------------------------------------------- the split itself


async def test_a_transport_supplying_only_execute_gets_the_whole_vocabulary() -> None:
    page = FakeSession()

    await page.navigate("https://example.com")
    await page.click("#buy")
    await page.fill("#q", "shoes")
    await page.press("Enter")

    assert [type(b[0]).__name__ for b in page.batches] == [
        "NavigateAction",
        "ClickAction",
        "FillAction",
        "PressAction",
    ]


async def test_the_base_class_refuses_to_dispatch_on_its_own() -> None:
    """`execute` is abstract in the only sense that matters: a subclass that
    forgets it fails loudly rather than silently doing nothing."""

    with pytest.raises(NotImplementedError):
        await SessionVerbs().navigate("https://example.com")


async def test_queries_return_values_not_prose() -> None:
    """These read `ActionResult.values`, which is exactly the field the HTTP
    boundary used to drop -- so a remote transport could not implement them at
    all until the response became a projection."""

    page = FakeSession(ActionResult(values=[3]))
    assert await page.get_count(".item") == 3

    page = FakeSession(ActionResult(values=[False]))
    assert await page.is_visible("#buy") is False


# ------------------------------------------------------------- perception


async def test_snapshot_returns_a_snapshot_on_a_transport_that_sends_one() -> None:
    """The remote shape: `snapshots` already populated, no tree in sight."""

    snap = Snapshot(llm_text="[e1]<button/>", refs={"e1": RefInfo("button", "Buy")})
    page = FakeSession(ActionResult(snapshots=[snap]))

    got = await page.snapshot()
    assert got is not None
    assert got.llm_text == "[e1]<button/>"
    assert got.refs["e1"].name == "Buy"


async def test_a_transport_with_no_local_tree_returns_none_rather_than_guessing() -> None:
    assert await FakeSession(ActionResult()).snapshot() is None


def test_the_fused_tree_is_not_reachable_from_the_verb_layer() -> None:
    """`tree()` is declared on `BrowserSession`, not here. The tree does not
    survive a network hop, so the type system should say which callers can have
    it rather than leaving a method that fails at runtime on half of them."""

    assert not hasattr(SessionVerbs, "tree")


# ------------------------------------------------- open to extension (F8/C)


@dataclasses.dataclass
class _SolveWallAction:
    reason: str = ""


def _registry_with_an_extension_verb():  # type: ignore[no-untyped-def]
    registry = browser_tools()
    registry.register(
        ToolSpec(
            name="solve_wall",
            description="Clear a known wall.",
            action_cls=_SolveWallAction,
            wire_fields=("reason",),
            agent_fields=("reason",),
        ),
        namespace="walmart",
    )
    return registry


class _ExtendedSession(FakeSession):
    @property
    def tools(self):  # type: ignore[no-untyped-def]
        return _registry_with_an_extension_verb()


async def test_call_tool_reaches_a_verb_an_extension_contributed() -> None:
    """The bug this fixes: `call_tool` resolved through the module-level
    `browser_tools()` rather than the session's registry, so a `ToolMount`'s
    namespaced verb was unreachable -- the namespacing existed for a case that
    could not be called.
    """

    page = _ExtendedSession()
    await page.call_tool("walmart.solve_wall", {"reason": "captcha"})
    assert page.batches[0][0] == _SolveWallAction(reason="captcha")


async def test_a_registered_verb_is_callable_without_a_typed_method() -> None:
    """The sugar layer must never be what blocks a verb: if `verbs.py` needed a
    hand-written method per tool it would be a second place to edit whenever
    `tools/catalog.py` changes, and the one people forget."""

    page = _ExtendedSession()
    await page.solve_wall(reason="press-and-hold")
    assert page.batches[0][0] == _SolveWallAction(reason="press-and-hold")


async def test_a_typed_method_still_wins_over_the_fallback() -> None:
    page = _ExtendedSession()
    await page.click("#buy")
    assert type(page.batches[0][0]).__name__ == "ClickAction"


def test_an_unknown_attribute_is_still_an_attribute_error() -> None:
    with pytest.raises(AttributeError):
        getattr(_ExtendedSession(), "not_a_tool_at_all")  # noqa: B009


def test_a_private_attribute_never_reaches_the_registry() -> None:
    """`__getattr__` runs for anything missing, including the dunders `copy`,
    `pickle` and `pytest` probe for. Answering those with a tool lookup is how a
    fallback turns into a debugging mystery."""

    with pytest.raises(AttributeError):
        getattr(_ExtendedSession(), "_not_a_tool")  # noqa: B009
