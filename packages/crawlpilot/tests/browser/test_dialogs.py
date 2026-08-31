"""JavaScript dialogs against a real browser, through the public facade only.

Every assertion checks what the *page* observed -- `#log`, which the fixture's
buttons write from inside their own handlers -- rather than that a call returned.
A dialog answered the wrong way, or answered silently on the caller's behalf,
looks exactly like success from the caller's side; the page is the only witness
that can tell the difference.

    uv run pytest packages/crawlpilot/tests/browser/test_dialogs.py -m browser -v
"""

from __future__ import annotations

import asyncio

import pytest

from crawlpilot.spi import actions as sa

pytestmark = pytest.mark.browser


async def _log(page) -> str:
    return await page.execute_js("document.getElementById('log').textContent")


async def _ref(page, name: str) -> str:
    """The ref of the element whose accessible name contains `name`."""

    from crawlpilot.dom.serializer import serialize  # noqa: PLC0415

    tree = await page.snapshot()
    assert tree is not None
    serialized = serialize(tree)
    for index, node in serialized.selector_map.items():
        if name.lower() in (node.ax_name or "").lower():
            return f"e{index}"
    raise AssertionError(f"no element named {name!r} in:\n{serialized.llm_text[:1500]}")


async def _open(page, toolbench) -> None:
    await page.navigate(toolbench.index)
    await page.snapshot()
    await page.execute_js("window.scrollTo(0, 0)")


# ------------------------------------------------------- the default: unchanged


async def test_the_default_policy_answers_dialogs_silently(page, toolbench) -> None:
    """The scraping path must behave exactly as it did before dialogs were
    modelled at all: Playwright's own auto-dismiss, nothing reported, nothing
    blocked. An unattended crawl cannot afford to wedge on an `alert()`."""

    await _open(page, toolbench)
    result = await page.click(await _ref(page, "Delete account"))

    assert result.dialog is None
    assert not result.sequence_aborted
    assert await _log(page) == "cancelled"


# ------------------------------------------------------------ manual: reported


@pytest.fixture
async def manual_page(browser, toolbench):
    """A session that holds its dialogs for the caller."""

    async with browser.session(dialogs="manual") as session:
        await _open(session, toolbench)
        yield session


async def test_a_confirm_is_reported_rather_than_declined_on_the_callers_behalf(
    manual_page,
) -> None:
    """The defect this whole module exists for: with the dialog auto-dismissed, a
    click on "Delete account" reports `clicked e12` while the deletion was
    quietly declined."""

    result = await manual_page.click(await _ref(manual_page, "Delete account"))

    assert result.dialog is not None
    assert result.dialog.kind == "confirm"
    assert result.dialog.message == "Delete account?"
    assert result.sequence_aborted
    await manual_page.dialog_dismiss()


async def test_accepting_lets_the_page_take_the_branch_it_was_asking_about(
    manual_page,
) -> None:
    await manual_page.click(await _ref(manual_page, "Delete account"))
    result = await manual_page.dialog_accept()

    assert await _log(manual_page) == "confirmed"
    assert any("accepted" in v for v in result.verifications)


async def test_dismissing_takes_the_other_branch(manual_page) -> None:
    await manual_page.click(await _ref(manual_page, "Delete account"))
    await manual_page.dialog_dismiss()

    assert await _log(manual_page) == "cancelled"


async def test_a_prompt_submits_the_text_it_is_given(manual_page) -> None:
    await manual_page.click(await _ref(manual_page, "Ask name"))
    await manual_page.dialog_accept("Ada")

    assert await _log(manual_page) == "name:Ada"


async def test_a_prompt_accepted_without_text_submits_its_own_default(manual_page) -> None:
    result = await manual_page.click(await _ref(manual_page, "Ask name"))
    assert result.dialog is not None
    assert result.dialog.default_value == "anon"

    await manual_page.dialog_accept()
    assert await _log(manual_page) == "name:anon"


async def test_a_dismissed_prompt_reports_null_to_the_page(manual_page) -> None:
    await manual_page.click(await _ref(manual_page, "Ask name"))
    await manual_page.dialog_dismiss()

    assert await _log(manual_page) == "name:null"


# ------------------------------------------------------- the blocked-page rules


async def test_a_blocked_page_refuses_further_work_instead_of_hanging(manual_page) -> None:
    """Chrome blocks the renderer while a dialog is up, so a `page.evaluate` on
    it never returns. The batch has to refuse rather than sit there: the
    difference between an answerable observation and a timeout."""

    await manual_page.click(await _ref(manual_page, "Delete account"))

    started = asyncio.get_running_loop().time()
    blocked = await manual_page.execute([sa.ExecuteJsAction(script="1 + 1")])
    elapsed = asyncio.get_running_loop().time() - started

    assert blocked.dialog is not None
    assert blocked.sequence_aborted
    assert not blocked.js_returns, "nothing should have been evaluated on a blocked page"
    assert elapsed < 5, f"should refuse immediately, took {elapsed:.1f}s"

    await manual_page.dialog_dismiss()


async def test_dialog_status_is_safe_to_call_speculatively(manual_page) -> None:
    """A model asking "did that click ask me something?" must not be punished for
    asking when the answer is no."""

    assert await manual_page.dialog_status() is None

    await manual_page.click(await _ref(manual_page, "Delete account"))
    pending = await manual_page.dialog_status()
    assert pending is not None and pending.kind == "confirm"

    await manual_page.dialog_dismiss()
    assert await manual_page.dialog_status() is None


# --------------------------------------------------------- the held mouse button


async def test_a_dialog_from_mousedown_does_not_leave_the_button_held(
    manual_page,
) -> None:
    """The dialog lands between the CDP press and release, so the release never
    arrives. Unless it is replayed the button stays logically down and the *next*
    click registers as a drag or a double-click -- so the proof is that an
    ordinary click still works afterwards.
    """

    await manual_page.click(await _ref(manual_page, "Confirm on mousedown"))
    await manual_page.dialog_accept()
    assert await _log(manual_page) == "down-confirmed"

    # The button is the witness: this second, unrelated click must land normally.
    await manual_page.click(await _ref(manual_page, "Delete account"))
    await manual_page.dialog_accept()
    assert await _log(manual_page) == "confirmed"


async def test_an_alert_has_only_one_answer(manual_page) -> None:
    await manual_page.click(await _ref(manual_page, "Say hello"))
    result = await manual_page.dialog_accept()

    assert await _log(manual_page) == "alerted"
    assert any("alert" in v for v in result.verifications)
