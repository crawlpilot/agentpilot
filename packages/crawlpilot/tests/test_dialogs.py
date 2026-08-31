"""Unit tests for `crawlpilot.driver.dialogs` -- the pending-dialog state machine
and the input-dispatch race, exercised with a fake `Dialog` and no browser.

The behaviour under test is the one that makes the dialog verbs safe to add at
all: registering a listener disables Playwright's auto-dismiss, after which an
unguarded CDP input command sits forever on a blocked renderer.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlpilot.driver.dialogs import (
    DialogInterrupt,
    DialogWatcher,
    GuardedSession,
    NoDialogOpen,
)


class FakeDialog:
    """Stands in for patchright's `Dialog`, recording how it was answered."""

    def __init__(self, kind: str = "confirm", message: str = "sure?", default: str = "") -> None:
        self.type = kind
        self.message = message
        self.default_value = default
        self.accepted: str | None | bool = False
        self.dismissed = False

    async def accept(self, prompt_text: str | None = None) -> None:
        self.accepted = prompt_text if prompt_text is not None else True

    async def dismiss(self) -> None:
        self.dismissed = True


class FakePage:
    def __init__(self) -> None:
        self.handler = None

    def on(self, event: str, handler) -> None:  # type: ignore[no-untyped-def]
        assert event == "dialog"
        self.handler = handler


async def _settle() -> None:
    """Let the watcher's auto-answer task run. The listener may not block, so an
    automatic policy schedules the reply rather than awaiting it."""

    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ----------------------------------------------------------------- policies


async def test_auto_dismiss_reproduces_playwrights_own_behaviour() -> None:
    watcher = DialogWatcher()  # default policy
    page = FakePage()
    watcher.attach(page)

    dialog = FakeDialog()
    page.handler(dialog)
    await _settle()

    assert dialog.dismissed
    assert watcher.pending is None


async def test_auto_accept_answers_yes() -> None:
    watcher = DialogWatcher("auto_accept")
    page = FakePage()
    watcher.attach(page)

    dialog = FakeDialog()
    page.handler(dialog)
    await _settle()

    assert dialog.accepted is True
    assert not dialog.dismissed


async def test_manual_holds_the_dialog_for_the_caller() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)

    page.handler(FakeDialog("prompt", "your name?", default="anon"))
    await _settle()

    pending = watcher.pending
    assert pending is not None
    assert pending.kind == "prompt"
    assert pending.message == "your name?"
    assert pending.default_value == "anon"
    assert "prompt dialog" in pending.describe()


# --------------------------------------------------------------- resolution


async def test_accept_and_dismiss_clear_the_pending_dialog() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)

    dialog = FakeDialog()
    page.handler(dialog)
    answered = await watcher.accept()

    assert answered.kind == "confirm"
    assert dialog.accepted is True
    assert watcher.pending is None


async def test_accept_passes_prompt_text_through() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog("prompt", "name?")
    page.handler(dialog)

    await watcher.accept("Ada")
    assert dialog.accepted == "Ada"


async def test_answering_nothing_raises_its_own_error() -> None:
    """A model calling `dialog_dismiss` speculatively should get a "nothing to
    answer" observation, not an opaque runtime error."""

    watcher = DialogWatcher("manual")
    with pytest.raises(NoDialogOpen):
        await watcher.dismiss()
    with pytest.raises(NoDialogOpen):
        await watcher.accept()


async def test_drain_dismisses_whatever_is_left_open() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog()
    page.handler(dialog)

    await watcher.drain()
    assert dialog.dismissed
    assert watcher.pending is None
    # Draining a page with nothing open is a no-op, not an error.
    await watcher.drain()


# -------------------------------------------------------------------- guard


async def test_guard_returns_the_commands_value_when_nothing_interrupts() -> None:
    watcher = DialogWatcher("manual")

    async def send() -> str:
        return "ok"

    assert await watcher.guard(send()) == "ok"


async def test_guard_gives_up_on_a_command_the_dialog_will_never_answer() -> None:
    """The case that would otherwise hang: Chrome blocks the renderer while the
    dialog is open, so the `Input.dispatchMouseEvent` acknowledgement never
    arrives."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)

    async def never() -> None:
        await asyncio.Event().wait()  # a response that never comes

    async def open_dialog() -> None:
        await asyncio.sleep(0.01)
        page.handler(FakeDialog("alert", "blocked"))

    asyncio.ensure_future(open_dialog())
    with pytest.raises(DialogInterrupt) as raised:
        await asyncio.wait_for(watcher.guard(never()), timeout=2)

    assert raised.value.info.kind == "alert"
    assert watcher.pending is not None


async def test_guard_short_circuits_once_a_dialog_is_already_open() -> None:
    """A dialog opened by an earlier command in the same click sequence must
    abort the later ones rather than have them wait on a blocked renderer."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    page.handler(FakeDialog())

    started = False

    async def never() -> None:
        nonlocal started
        started = True
        await asyncio.Event().wait()

    with pytest.raises(DialogInterrupt):
        await asyncio.wait_for(watcher.guard(never()), timeout=2)
    assert not started, "the command must not even be dispatched"


async def test_guard_reports_a_dialog_that_lost_the_race_to_its_own_ack() -> None:
    """A `confirm()` in a mousedown handler can open *and* have the CDP command
    acknowledged before the listener runs. The click still needs reporting as
    dialog-interrupted, or the caller acts on a blocked page."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)

    async def send_then_dialog() -> None:
        page.handler(FakeDialog())

    with pytest.raises(DialogInterrupt):
        await watcher.guard(send_then_dialog())


async def test_guard_propagates_a_genuine_cdp_failure() -> None:
    """A transport error must not be laundered into "a dialog opened"."""

    watcher = DialogWatcher("manual")

    async def boom() -> None:
        raise RuntimeError("target closed")

    with pytest.raises(RuntimeError, match="target closed"):
        await watcher.guard(boom())


# ---------------------------------------------------------- GuardedSession


class FakeSession:
    def __init__(self, on_send=None) -> None:  # type: ignore[no-untyped-def]
        self.sent: list[tuple[str, dict | None]] = []
        self._on_send = on_send
        self.url = "https://example.test"  # an attribute that must delegate

    async def send(self, method: str, params: dict | None = None):  # type: ignore[no-untyped-def]
        self.sent.append((method, params))
        if self._on_send is not None:
            self._on_send()
        return {"ok": method}


async def test_guarded_session_passes_calls_and_results_through() -> None:
    session = FakeSession()
    guarded = GuardedSession(session, DialogWatcher("manual"))

    assert await guarded.send("DOM.getBoxModel", {"backendNodeId": 3}) == {"ok": "DOM.getBoxModel"}
    assert session.sent == [("DOM.getBoxModel", {"backendNodeId": 3})]
    assert guarded.url == "https://example.test"  # delegation


async def test_guarded_session_raises_when_a_dialog_blocks_a_non_input_call() -> None:
    """Chrome blocks the renderer wholesale, so a geometry read stalls on an open
    dialog exactly as an input dispatch does -- which is why the guard wraps
    every send rather than just `Input.*`."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    session = FakeSession(on_send=lambda: page.handler(FakeDialog("confirm", "leave?")))
    guarded = GuardedSession(session, watcher)

    with pytest.raises(DialogInterrupt):
        await guarded.send("DOM.getBoxModel")


async def test_an_automatic_policy_never_interrupts() -> None:
    """The scraping path must behave exactly as it did before this module
    existed: dialogs answered silently, no command ever aborted."""

    watcher = DialogWatcher()  # auto_dismiss
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog()
    session = FakeSession(on_send=lambda: page.handler(dialog))
    guarded = GuardedSession(session, watcher)

    assert await guarded.send("Input.dispatchMouseEvent") == {"ok": "Input.dispatchMouseEvent"}
    await _settle()
    assert dialog.dismissed
    assert watcher.pending is None


# ------------------------------------------------------- the held mouse button


async def test_a_dialog_from_a_mousedown_handler_records_the_held_button() -> None:
    """The press was delivered; the `mouseReleased` that would have followed is
    the command the dialog blocked. Left unreplayed the button stays down and the
    next click reads as a drag."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    session = FakeSession(on_send=lambda: page.handler(FakeDialog()))
    guarded = GuardedSession(session, watcher)

    with pytest.raises(DialogInterrupt):
        await guarded.send(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": 12.0, "y": 34.0, "button": "left"},
        )

    release = watcher.pending_release
    assert release is not None
    assert (release.x, release.y) == (12.0, 34.0)


async def test_a_dialog_on_the_release_records_nothing_to_replay() -> None:
    """By the time a `click`/`mouseup` handler runs, the button is already up."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    guarded = GuardedSession(FakeSession(on_send=lambda: page.handler(FakeDialog())), watcher)

    with pytest.raises(DialogInterrupt):
        await guarded.send(
            "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": 1.0, "y": 2.0}
        )

    assert watcher.pending_release is None


async def test_a_non_input_call_records_nothing_to_replay() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    guarded = GuardedSession(FakeSession(on_send=lambda: page.handler(FakeDialog())), watcher)

    with pytest.raises(DialogInterrupt):
        await guarded.send("DOM.getBoxModel", {"backendNodeId": 1})

    assert watcher.pending_release is None


async def test_the_held_button_survives_answering_the_dialog() -> None:
    """Replaying the release can only happen once the page is unblocked, so the
    record has to outlive `accept()` -- clearing it inside the answer would leave
    the caller nothing to replay."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    guarded = GuardedSession(FakeSession(on_send=lambda: page.handler(FakeDialog())), watcher)

    with pytest.raises(DialogInterrupt):
        await guarded.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 5.0, "y": 6.0})

    await watcher.accept()
    assert watcher.pending_release is not None, "the caller still has a button to release"

    await watcher.drain()
    assert watcher.pending_release is None


async def test_accepting_a_prompt_without_text_submits_its_default_not_an_empty_string() -> None:
    """A person pressing OK on `prompt('Your name?', 'anon')` gets `"anon"`.
    Playwright's bare `accept()` sends `""`, which is a wrong answer dressed as a
    right one -- the field looked pre-filled and came back blank."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog("prompt", "Your name?", default="anon")
    page.handler(dialog)

    await watcher.accept()
    assert dialog.accepted == "anon"


async def test_an_explicit_empty_string_still_submits_an_empty_string() -> None:
    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog("prompt", "Your name?", default="anon")
    page.handler(dialog)

    await watcher.accept("")
    assert dialog.accepted == ""


async def test_the_default_substitution_is_confined_to_prompts() -> None:
    """A confirm has no text to submit; passing one would be meaningless."""

    watcher = DialogWatcher("manual")
    page = FakePage()
    watcher.attach(page)
    dialog = FakeDialog("confirm", "sure?")
    page.handler(dialog)

    await watcher.accept()
    assert dialog.accepted is True
