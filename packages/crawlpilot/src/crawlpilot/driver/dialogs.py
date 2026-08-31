"""JavaScript dialogs: `alert`, `confirm`, `prompt` and `beforeunload`.

**Why this module has to exist before the dialog verbs do.**

Playwright (and so patchright) auto-dismisses every dialog *unless* a `dialog`
listener is registered on the page. That default has two consequences, and they
pull in opposite directions:

- With no listener -- crawlpilot until now -- a click that opens `confirm()`
  returns promptly and the page sees `false`. Nothing hangs, but nothing is
  reported either: an agent clicking "Delete account" is told `clicked e12`
  while the deletion was silently declined on its behalf. That is precisely the
  silent-wrong-outcome `ActionResult.verifications` exists to catch, and it is
  the live defect here.
- Register a listener so the dialog *can* be reported, and the auto-dismiss
  stops. Chrome then blocks the renderer until the dialog is resolved, and the
  `Input.dispatchMouseEvent` that opened it never gets a response -- the click
  hangs to its timeout, with the mouse button logically still held.

So the listener and the race below are one change, not two: adding dialog verbs
without `guard()` would trade a silent wrong answer for a hang.

The race, the session filtering and the pending-release handling are ported from
agent-browser's `dispatch_mouse_or_dialog` / `dispatch_click`
(`cli/src/native/interaction.rs:943-1115`).

`DialogPolicy` keeps the scraping path exactly as it was. Only a caller that
actually wants to answer dialogs -- an agent loop -- opts into `"manual"`; an
unattended `run_ephemeral_scrape` must not wedge on a page that greets it with
an `alert()`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from crawlpilot.spi.actions import DialogInfo, DialogPolicy
from crawlpilot.spi.errors import NoDialogOpen

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = [
    "DEFAULT_POLICY",
    "DialogInfo",
    "DialogInterrupt",
    "DialogPolicy",
    "DialogWatcher",
    "GuardedSession",
    "NoDialogOpen",
    "PendingRelease",
]
"""`DialogInfo`/`DialogPolicy` are defined in `spi.actions` (`ActionResult`
carries the one, session config the other, and `spi` may not import `driver`)
and re-exported here because this is the module that produces them."""

DEFAULT_POLICY: DialogPolicy = "auto_dismiss"


@dataclass
class PendingRelease:
    """A mouse button left logically down because a dialog opened between
    `mousePressed` and `mouseReleased`.

    Without replaying the release once the dialog is answered, the button stays
    held: the next click arrives as a drag, or as the second half of a
    double-click. agent-browser carries the same record for the same reason
    (`interaction.rs:21-26`).
    """

    session: Any
    x: float
    y: float


class DialogWatcher:
    """One tab's dialog state: the listener, the pending dialog and the race.

    Deliberately holds the patchright `Dialog` object rather than resolving it
    eagerly -- under `"manual"` the whole point is that the *caller* decides
    between accept and dismiss, and the page stays blocked until it does.
    """

    def __init__(self, policy: DialogPolicy = DEFAULT_POLICY) -> None:
        self._policy: DialogPolicy = policy
        self._dialog: Any | None = None
        self._info: DialogInfo | None = None
        self._opened = asyncio.Event()
        self.pending_release: PendingRelease | None = None

    # ------------------------------------------------------------- attachment

    def attach(self, page: Any) -> None:
        """Register the listener. **Registering it disables Playwright's
        auto-dismiss**, which is why every input dispatch on this page must go
        through `guard()` from here on."""

        page.on("dialog", self._on_dialog)

    @property
    def policy(self) -> DialogPolicy:
        return self._policy

    def set_policy(self, policy: DialogPolicy) -> None:
        self._policy = policy

    # ---------------------------------------------------------------- opening

    def _on_dialog(self, dialog: Any) -> None:
        """Playwright calls this synchronously; it must not block.

        Under an automatic policy the dialog is **never recorded as pending**
        and the answer is scheduled as a task. Both halves matter: returning
        promptly is what lets Playwright deliver the reply at all, and not
        marking it pending is what keeps `guard` from aborting a command that is
        about to be unblocked anyway. That is what preserves today's behaviour
        for the scraping path byte for byte.
        """

        if self._policy != "manual":
            accept = self._policy == "auto_accept"
            asyncio.get_event_loop().create_task(_answer(dialog, accept=accept))
            return

        self._dialog = dialog
        self._info = DialogInfo(
            kind=dialog.type,
            message=dialog.message,
            default_value=getattr(dialog, "default_value", "") or "",
        )
        self._opened.set()

    # -------------------------------------------------------------- the race

    async def guard(self, send: Awaitable[Any]) -> Any:
        """Await `send`, raising `DialogInterrupt` the moment a dialog blocks it.

        Wraps *every* renderer-bound CDP call, not only input: Chrome blocks the
        renderer wholesale while a dialog is up, so `DOM.getBoxModel` and
        `Runtime.evaluate` stall on it exactly as `Input.dispatchMouseEvent`
        does.

        An exception rather than a status flag because the caller is almost
        always a multi-step sequence -- approach, press, release -- and every
        step after the dialog opens is equally unanswerable. Raising unwinds all
        of them; a bool would need checking at each step and would be forgotten
        at one.

        The outstanding call is *not* cancelled: Chrome still owes a response
        and will send one once the dialog is answered, so the task is kept and
        quietly reaped.
        """

        if self._info is not None:
            # A dialog opened by an earlier command in this same sequence. Close
            # the coroutine here rather than leaving it to the caller -- callers
            # pass `guard(cdp.send(...))`, so dropping it un-awaited would raise
            # "coroutine was never awaited" from wherever the GC happened to run.
            close = getattr(send, "close", None)
            if close is not None:
                close()
            raise DialogInterrupt(self._info)

        send_task = asyncio.ensure_future(send)
        opened_task = asyncio.ensure_future(self._opened.wait())
        try:
            done, _ = await asyncio.wait(
                (send_task, opened_task), return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            send_task.cancel()
            opened_task.cancel()
            raise

        if send_task in done:
            opened_task.cancel()
            value = send_task.result()  # re-raises a genuine CDP failure
            # A dialog can open *because of* this command and still lose the
            # race to its own acknowledgement -- a `confirm()` in a mousedown
            # handler does exactly that. The page is blocked either way.
            if self._info is not None:
                raise DialogInterrupt(self._info)
            return value

        opened_task.cancel()
        _reap(send_task)
        assert self._info is not None
        raise DialogInterrupt(self._info)

    # ------------------------------------------------------------- resolution

    @property
    def pending(self) -> DialogInfo | None:
        return self._info

    async def accept(self, prompt_text: str | None = None) -> DialogInfo:
        return await self._answer(accept=True, prompt_text=prompt_text)

    async def dismiss(self) -> DialogInfo:
        return await self._answer(accept=False)

    async def _answer(self, *, accept: bool, prompt_text: str | None = None) -> DialogInfo:
        info = self._info
        if info is None or self._dialog is None:
            raise NoDialogOpen()
        await self._resolve(accept=accept, prompt_text=prompt_text)
        return info

    async def _resolve(self, *, accept: bool, prompt_text: str | None = None) -> None:
        dialog = self._dialog
        self._dialog = None
        self._info = None
        self._opened.clear()
        # `pending_release` is deliberately *not* cleared here: it has to outlive
        # answering the dialog, because replaying the held button is something
        # that can only happen once the page is unblocked. The caller consumes it
        # (`PatchrightDriver._after_dialog`); `drain` clears it for the case where
        # nobody does.
        if dialog is not None:
            await _answer(dialog, accept=accept, prompt_text=prompt_text)

    async def drain(self) -> None:
        """Dismiss anything still open. Called when a tab closes or a batch ends
        under an automatic policy, so a stranded dialog cannot wedge a context
        that is about to be reused from the warm pool."""

        if self._info is not None:
            await self._resolve(accept=False)
        self.pending_release = None


class GuardedSession:
    """A `CDPSession` whose every `send` gives up when a dialog blocks the page.

    A wrapper rather than a `watcher` parameter threaded through
    `driver.cdp_element`: that module has a dozen entry points which all funnel
    into `cdp.send`, and passing the watcher to each would put the same argument
    in a dozen signatures for one behaviour. Wrapping the session instead means
    `cdp_element` needs no change at all and cannot forget to guard a call.

    Everything but `send` is delegated, so a `GuardedSession` is usable wherever
    a real session is.
    """

    def __init__(self, session: Any, watcher: DialogWatcher) -> None:
        self._session = session
        self._watcher = watcher

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return await self._watcher.guard(self._session.send(method, params))
        except DialogInterrupt:
            # A dialog raised from a `mousedown` handler: the press *was*
            # delivered, and the `mouseReleased` that would have followed is the
            # command the dialog blocked. Recording it here rather than in the
            # driver covers every click path -- `click_at`, the fill focus
            # fallback, a future `dblclick` -- without any of them remembering
            # to. A dialog on the release needs no record: by then the button is
            # already up (agent-browser, `interaction.rs:1067-1070`).
            if method == "Input.dispatchMouseEvent" and params is not None:
                if params.get("type") == "mousePressed":
                    self._watcher.pending_release = PendingRelease(
                        session=self, x=params["x"], y=params["y"]
                    )
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class DialogInterrupt(RuntimeError):
    """A dialog opened and the page is blocked until it is answered.

    Carries the dialog so the driver can put it on `ActionResult` -- the caller
    needs to know *what* it was asked, not merely that something interrupted.
    """

    def __init__(self, info: DialogInfo) -> None:
        self.info = info
        super().__init__(f"page is blocked on a {info.describe()}")


async def _answer(dialog: Any, *, accept: bool, prompt_text: str | None = None) -> None:
    """Answer a dialog, tolerating one that has already gone.

    The page is blocked on this, so failing to answer would strand it -- but a
    navigation can take a dialog with it, and a stale handle is not a reason to
    fail the action that discovered it.

    Accepting a `prompt` with no text submits the dialog's **own default value**,
    not the empty string Playwright's bare `accept()` sends. A person pressing OK
    on `prompt('Your name?', 'anon')` gets back `"anon"`, and an accept that
    silently blanks a pre-filled field is a wrong answer dressed as a right one.
    Passing `""` explicitly still submits an empty string.
    """

    with contextlib.suppress(Exception):
        if not accept:
            await dialog.dismiss()
            return
        if prompt_text is None and dialog.type == "prompt":
            prompt_text = getattr(dialog, "default_value", "") or ""
        if prompt_text is not None:
            await dialog.accept(prompt_text)
        else:
            await dialog.accept()


def _reap(task: asyncio.Task[Any]) -> None:
    """Keep a still-pending CDP send from surfacing as "exception was never
    retrieved" when the page it was sent to goes away."""

    task.add_done_callback(lambda t: t.cancelled() or t.exception())
