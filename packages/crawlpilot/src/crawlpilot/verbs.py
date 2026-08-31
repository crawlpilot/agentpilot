"""The ~60 browser verbs, defined once, over one abstract transport.

Every method here is sugar over a single `execute([Action])` call -- that was
already true when they lived on `api.BrowserSession`, but the sugar and the
driver binding were the same class, so the only way to drive a browser somewhere
*else* was to restate all sixty methods against a different transport.

They are separated now. `SessionVerbs` knows the vocabulary and nothing about
where a batch goes; a subclass supplies `execute` and inherits the vocabulary:

    crawlpilot.api.BrowserSession      execute -> the local driver
    a remote client's session          execute -> POST /v1/sessions/{id}/execute

That is the whole mechanism, and it is why a script written against a local
browser runs unchanged against a remote fleet.

**One deliberate asymmetry.** `snapshot()` returns a `Snapshot` -- the serialized
`llm_text` + per-ref role/name/box -- on both transports, because the fused
`EnhancedDOMTreeNode` cannot cross a network (see `spi.dom_tree.Snapshot`).
Reaching the tree is `BrowserSession.tree()`, local-only and declared there
rather than here, so the type system says which callers can have it.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.actions import ActionResult
from crawlpilot.spi.dom_tree import Snapshot

if TYPE_CHECKING:
    from crawlpilot.tools import ToolRegistry


class SessionVerbs:
    """One open page, driven action by action.

    Abstract in exactly one place: `execute`. Everything else follows from it.
    """

    # ------------------------------------------------------------- transport

    async def execute(
        self, actions: Sequence[spi_actions.Action], *, page_id: str | None = None
    ) -> ActionResult:
        """Dispatch a batch. The escape hatch every method below is sugar over --
        several actions in one round trip rather than one call each.

        The only thing a transport has to provide.
        """

        raise NotImplementedError

    @property
    def tools(self) -> ToolRegistry:
        """The verbs this session can dispatch, built-ins plus whatever
        extensions contributed.

        Overridden by a transport that knows better: `BrowserSession` returns its
        `Browser`'s extension registry, and a remote session returns what the
        server told it. The default is the built-in set, which is correct for a
        session with no extensions and keeps `call_tool` working on any subclass
        that does not care.
        """

        from crawlpilot.tools import browser_tools  # noqa: PLC0415

        return browser_tools()

    # ------------------------------------------------------------ navigation

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
        referer: str | None = None,
    ) -> ActionResult:
        return await self.execute(
            [
                spi_actions.NavigateAction(
                    url=url,
                    wait_until=wait_until,  # type: ignore[arg-type]
                    timeout_ms=timeout_ms,
                    referer=referer,
                )
            ]
        )

    async def go_back(self) -> ActionResult:
        return await self.execute([spi_actions.GoBackAction()])

    async def forward(self) -> ActionResult:
        return await self.execute([spi_actions.ForwardAction()])

    async def reload(self) -> ActionResult:
        return await self.execute([spi_actions.ReloadAction()])

    # ----------------------------------------------------------- interaction

    async def click(
        self, selector: str | None = None, *, ref: str | None = None, all: bool = False
    ) -> ActionResult:
        """Click an element, addressed by CSS selector or by snapshot ref.

            await page.click("#add-to-cart")     # positional -> CSS selector
            await page.click(ref="e42")          # keyword    -> snapshot ref

        **The parameter decides, never the string.** A ref is `e<index>`, and
        `e42` is itself a valid CSS type selector, so no rule could tell them
        apart by looking -- and a wrong guess clicks the wrong element in
        silence. Passing both is a `ValueError`.

        Prefer a ref when you have one: `DOM.querySelector` is document-scoped,
        so a selector cannot reach inside a cross-origin iframe or a shadow
        root, and an ambiguous selector takes the first match. A selector saves
        you a `snapshot()` when you already know the page.
        """

        return await self.execute([spi_actions.ClickAction(ref=ref, selector=selector, all=all)])

    async def fill(
        self,
        selector: str | None = None,
        text: str = "",
        *,
        ref: str | None = None,
        clear: bool = True,
    ) -> ActionResult:
        """Type into a field, by CSS selector or by ref -- see `click`.

        `clear=False` appends to what is already there.
        """

        return await self.execute(
            [spi_actions.FillAction(ref=ref, selector=selector, text=text, clear=clear)]
        )

    async def select_option(self, ref: str, *values: str) -> ActionResult:
        return await self.execute(
            [spi_actions.SelectOptionAction(ref=ref, values=list(values))]
        )

    async def hover(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.HoverAction(ref=ref, selector=selector)])

    async def press(self, key: str) -> ActionResult:
        return await self.execute([spi_actions.PressAction(key=key)])

    async def scroll(
        self, direction: str = "down", *, pages: float = 1.0, ref: str | None = None
    ) -> ActionResult:
        return await self.execute(
            [spi_actions.ScrollAction(direction=direction, pages=pages, ref=ref)]  # type: ignore[arg-type]
        )

    async def wait(self, ms: int, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.WaitAction(ms=ms, ref=ref)])

    async def send_keys(self, keys: str) -> ActionResult:
        """A key or shortcut (`'Escape'`, `'Control+a'`) to whatever has focus."""

        return await self.execute([spi_actions.SendKeysAction(keys=keys)])

    async def find_text(self, text: str) -> ActionResult:
        """Scroll to the first occurrence of `text`."""

        return await self.execute([spi_actions.FindTextAction(text=text)])

    async def upload_file(self, ref: str, path: str | Path) -> ActionResult:
        """Attach a local file to a file input, without a file chooser."""

        return await self.execute(
            [spi_actions.UploadFileAction(ref=ref, path=str(path))]
        )

    async def execute_js(self, script: str) -> Any:
        result = await self.execute([spi_actions.ExecuteJsAction(script=script)])
        return result.js_returns[0] if result.js_returns else None

    # ----------------------------------------------------------------- queries
    #
    # These return their answer directly rather than an `ActionResult` whose
    # `readouts[0]` the caller has to index into -- the same reason `extract()`
    # returns a string.
    #
    # And they return the *value*, not the sentence. Until 0.2 they returned
    # `readouts[0]`, which is prose written for an agent's prompt: `get_count()`
    # gave `"count: 3 element(s) match '.item'"` rather than `3`, and
    # `is_visible()` gave `"#buy is visible"` or `"#buy is not visible"` -- two
    # non-empty strings, so `if await page.is_visible(x)` was always True. The
    # prose still exists on `ActionResult.readouts` for the agent path; these
    # read `ActionResult.values` instead.

    async def _value(self, action: spi_actions.Action, default: Any = None) -> Any:
        result = await self.execute([action])
        return result.values[0] if result.values else default

    async def dropdown_options(self, ref: str) -> list[dict[str, str]]:
        """The options of a `<select>`, read off the last snapshot.

        One `{"text": ..., "value": ...}` per `<option>`; empty when the element
        has none (which usually means it is not a real `<select>`).
        """

        return cast(
            "list[dict[str, str]]",
            await self._value(spi_actions.DropdownOptionsAction(ref=ref), []),
        )

    async def search_page(
        self, pattern: str, *, regex: bool = False, **kwargs: Any
    ) -> list[str]:
        """Grep the rendered page text. Cheap; no model, no full observation.

        One string per match, each with a little surrounding context. Empty when
        nothing matched -- so `if await page.search_page(...)` reads correctly.
        """

        return cast(
            "list[str]",
            await self._value(
                spi_actions.SearchPageAction(pattern=pattern, regex=regex, **kwargs), []
            ),
        )

    async def find_elements(
        self, selector: str, *, attributes: Sequence[str] = (), **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Query the DOM by CSS selector; returns tags, text and attributes."""

        return cast(
            "list[dict[str, Any]]",
            await self._value(
                spi_actions.FindElementsAction(
                    selector=selector, attributes=list(attributes), **kwargs
                ),
                [],
            ),
        )

    # -------------------------------------------------------------------- tabs

    async def new_tab(self, url: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.NewTabAction(url=url)])

    async def switch_tab(self, page_id: str) -> ActionResult:
        return await self.execute([spi_actions.SwitchTabAction(page_id=page_id)])

    async def close_tab(self, page_id: str) -> ActionResult:
        return await self.execute([spi_actions.CloseTabAction(page_id=page_id)])

    async def list_tabs(self) -> list[spi_actions.TabInfo]:
        result = await self.execute([spi_actions.ListTabsAction()])
        return result.tabs[0] if result.tabs else []

    async def diff_snapshot(self, *, settle: bool = False) -> str:
        """What changed on the page since the last snapshot on this tab.

        Cheaper than re-reading the page to work out what an action did. The
        first call on a tab has nothing to compare against and says so.
        """

        result = await self.execute([spi_actions.DiffSnapshotAction(settle=settle)])
        return result.readouts[0] if result.readouts else ""

    # ------------------------------------------------------------------ waits
    #
    # Each raises `WaitTimeout` when the condition does not come true, so the
    # call after it can rely on the state it asked for.

    async def wait_for_selector(
        self, selector: str, *, state: str = "visible", timeout_ms: int = 10_000
    ) -> ActionResult:
        return await self.execute(
            [
                spi_actions.WaitForSelectorAction(
                    selector=selector,
                    state=state,  # type: ignore[arg-type]
                    timeout_ms=timeout_ms,
                )
            ]
        )

    async def wait_for_text(self, text: str, *, timeout_ms: int = 10_000) -> ActionResult:
        return await self.execute(
            [spi_actions.WaitForTextAction(text=text, timeout_ms=timeout_ms)]
        )

    async def wait_for_url(self, url: str, *, timeout_ms: int = 10_000) -> ActionResult:
        """Wait until the URL contains `url`, or matches it as a glob."""

        return await self.execute(
            [spi_actions.WaitForUrlAction(url=url, timeout_ms=timeout_ms)]
        )

    async def wait_for_load(
        self, state: str = "load", *, timeout_ms: int = 10_000
    ) -> ActionResult:
        return await self.execute(
            [
                spi_actions.WaitForLoadAction(
                    state=state,  # type: ignore[arg-type]
                    timeout_ms=timeout_ms,
                )
            ]
        )

    # ---------------------------------------------------------------- getters
    #
    # Each returns its answer directly rather than an `ActionResult` whose
    # `values[0]` the caller indexes into -- as `dropdown_options` does -- and
    # each returns a real Python value. See `_value` and `ActionResult.values`
    # for what these used to return and why it was a trap.
    #
    # Every one takes `selector=` as well as `ref=`, unchanged from before.

    async def get_text(self, ref: str | None = None, *, selector: str | None = None) -> str:
        return cast(
            str, await self._value(spi_actions.GetTextAction(ref=ref, selector=selector), "")
        )

    async def get_html(self, ref: str | None = None, *, selector: str | None = None) -> str:
        return cast(
            str, await self._value(spi_actions.GetHtmlAction(ref=ref, selector=selector), "")
        )

    async def get_value(self, ref: str | None = None, *, selector: str | None = None) -> str:
        return cast(
            str, await self._value(spi_actions.GetValueAction(ref=ref, selector=selector), "")
        )

    async def get_attribute(
        self, name: str, ref: str | None = None, *, selector: str | None = None
    ) -> str | None:
        """The attribute's value, or `None` when the element does not carry it.

        `None` and `""` are different answers here -- absent versus present but
        empty (`<input required="">`) -- so this does not flatten them.
        """

        return cast(
            "str | None",
            await self._value(
                spi_actions.GetAttributeAction(name=name, ref=ref, selector=selector)
            ),
        )

    async def get_count(self, selector: str) -> int:
        return cast(int, await self._value(spi_actions.GetCountAction(selector=selector), 0))

    async def get_box(
        self, ref: str | None = None, *, selector: str | None = None
    ) -> dict[str, float] | None:
        """The element's bounding box (`x`/`y`/`width`/`height`), or `None` when
        it has no geometry -- a hidden input, a zero-size wrapper."""

        return cast(
            "dict[str, float] | None",
            await self._value(spi_actions.GetBoxAction(ref=ref, selector=selector)),
        )

    async def get_styles(
        self,
        ref: str | None = None,
        *,
        selector: str | None = None,
        properties: list[str] | None = None,
    ) -> dict[str, str]:
        return cast(
            "dict[str, str]",
            await self._value(
                spi_actions.GetStylesAction(
                    ref=ref, selector=selector, properties=properties or []
                ),
                {},
            ),
        )

    async def get_url(self) -> str:
        return cast(str, await self._value(spi_actions.GetUrlAction(), ""))

    async def get_title(self) -> str:
        return cast(str, await self._value(spi_actions.GetTitleAction(), ""))

    async def is_visible(self, ref: str | None = None, *, selector: str | None = None) -> bool:
        return cast(
            bool,
            await self._value(spi_actions.IsVisibleAction(ref=ref, selector=selector), False),
        )

    async def is_enabled(self, ref: str | None = None, *, selector: str | None = None) -> bool:
        return cast(
            bool,
            await self._value(spi_actions.IsEnabledAction(ref=ref, selector=selector), False),
        )

    async def is_checked(
        self, ref: str | None = None, *, selector: str | None = None
    ) -> bool | None:
        """`True`/`False`, or `None` for a tri-state checkbox set to
        `indeterminate` -- where neither answer is true, and `False` would
        report an unanswered box as deliberately unchecked."""

        return cast(
            "bool | None",
            await self._value(spi_actions.IsCheckedAction(ref=ref, selector=selector)),
        )

    # ----------------------------------------------------- files and frames

    async def pdf(self, *, landscape: bool = False, scale: float = 1.0) -> bytes:
        """The page as PDF bytes. Headless only -- Chrome's limitation."""

        result = await self.execute(
            [spi_actions.PdfAction(landscape=landscape, scale=scale)]
        )
        return result.pdfs[0] if result.pdfs else b""

    async def list_frames(self) -> list[spi_actions.FrameInfo]:
        result = await self.execute([spi_actions.ListFramesAction()])
        return result.frames[0] if result.frames else []

    async def download(self, ref: str, *, timeout_ms: int = 30_000):  # type: ignore[no-untyped-def]
        """Click `ref` and wait for the file. The driver chooses where it lands."""

        result = await self.execute(
            [spi_actions.DownloadAction(ref=ref, timeout_ms=timeout_ms)]
        )
        return result.downloads[0] if result.downloads else None

    # --------------------------------------------------------- interaction (2)

    async def double_click(
        self, selector: str | None = None, *, ref: str | None = None
    ) -> ActionResult:
        return await self.execute([spi_actions.DoubleClickAction(ref=ref, selector=selector)])

    async def focus(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.FocusAction(ref=ref, selector=selector)])

    async def check(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        """Ensure a checkbox or radio is checked. Idempotent, unlike a click."""

        return await self.execute([spi_actions.CheckAction(ref=ref, selector=selector)])

    async def uncheck(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.UncheckAction(ref=ref, selector=selector)])

    async def scroll_into_view(
        self, selector: str | None = None, *, ref: str | None = None
    ) -> ActionResult:
        return await self.execute([spi_actions.ScrollIntoViewAction(ref=ref, selector=selector)])

    async def clear(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.ClearAction(ref=ref, selector=selector)])

    async def drag(self, ref: str, to_ref: str) -> ActionResult:
        return await self.execute([spi_actions.DragAction(ref=ref, to_ref=to_ref)])

    async def key_down(self, key: str) -> ActionResult:
        return await self.execute([spi_actions.KeyDownAction(key=key)])

    async def key_up(self, key: str) -> ActionResult:
        return await self.execute([spi_actions.KeyUpAction(key=key)])

    async def insert_text(self, text: str) -> ActionResult:
        """Paste-shaped text entry. Prefer `fill` for ordinary typing."""

        return await self.execute([spi_actions.InsertTextAction(text=text)])

    async def tap(self, selector: str | None = None, *, ref: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.TapAction(ref=ref, selector=selector)])

    async def swipe(
        self, direction: str, *, distance: int = 300, ref: str | None = None
    ) -> ActionResult:
        return await self.execute(
            [
                spi_actions.SwipeAction(
                    direction=direction,  # type: ignore[arg-type]
                    distance=distance,
                    ref=ref,
                )
            ]
        )

    # ----------------------------------------------------------------- dialogs

    async def dialog_status(self) -> spi_actions.DialogInfo | None:
        """The dialog blocking this page, or None.

        Always safe to call, including when nothing is open -- unlike
        `dialog_accept`/`dialog_dismiss`, which raise `NoDialogOpen`.
        """

        return (await self.execute([spi_actions.DialogStatusAction()])).dialog

    async def dialog_accept(self, prompt_text: str | None = None) -> ActionResult:
        return await self.execute([spi_actions.DialogAcceptAction(prompt_text=prompt_text)])

    async def dialog_dismiss(self) -> ActionResult:
        return await self.execute([spi_actions.DialogDismissAction()])

    # ------------------------------------------------------------------- tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ActionResult:
        """Dispatch a tool by name, as an LLM would call it.

        `browser_tools()` and the `to_anthropic` / `to_openai` / `to_mcp`
        adapters hand a caller tool *definitions*; this is the other half, and
        without it every consumer had to write the same
        name-and-dict -> validate -> `spi.actions` -> `execute()` loop themselves.

        Resolved against `self.tools` -- the session's registry -- not the
        module-level built-in set. That distinction is the whole point of
        namespaced tools: an extension's `ToolMount` registers `walmart.solve_wall`
        into the *session's* registry, and while this looked up `browser_tools()`
        instead, nothing could ever call one. The namespacing existed for a case
        that could not be reached.

        Arguments are validated against the tool's own schema before dispatch, so
        a malformed call is a `ValidationError` naming the field rather than a
        `TypeError` from somewhere inside the driver.
        """

        spec = self.tools.get(name)
        if spec is None:
            raise ValueError(f"no such tool {name!r}")
        if spec.agent_fields is None:
            raise ValueError(
                f"tool {name!r} is not callable this way: it is wire-only "
                "(security-sensitive, or it needs arguments a caller must supply "
                "explicitly). Build the action and pass it to execute()."
            )
        parsed = spec.agent_model().model_validate({"type": spec.name, **(arguments or {})})
        return await self.execute([spec.from_model(parsed)])

    def __getattr__(self, name: str) -> Callable[..., Coroutine[Any, Any, ActionResult]]:
        """A registered tool with no typed method here is still callable.

        The typed methods above are ergonomics, not dispatch. If a verb *needed*
        one, this module would be a second place to edit whenever
        `tools/catalog.py` changes -- and the one people forget. So an unknown
        attribute that names a tool in this session's registry becomes a bound
        caller instead of an `AttributeError`:

            await page.solve_wall(reason="captcha")   # an extension's verb

        That covers extension verbs and any verb newer than the code calling it,
        which is what keeps a client one release behind its server useful rather
        than stuck. Typed methods stay for the built-in catalog, where the real
        signatures and the non-`ActionResult` return types (`get_text` -> str,
        `is_visible` -> bool) are worth having.

        Only reached for attributes that do not otherwise exist, so it can never
        shadow a real method.
        """

        if name.startswith("_"):
            raise AttributeError(name)

        registry = self.tools
        # An attribute cannot contain a dot, so a bare name has to be resolved
        # across namespaces -- `registry.get` alone would only ever look in
        # `browser.`, which is precisely the namespace an extension's verb is
        # not in.
        keys = [key for key in registry.names if key.split(".", 1)[1] == name]
        if len(keys) > 1:
            raise AttributeError(
                f"{name!r} is ambiguous across namespaces ({', '.join(sorted(keys))}); "
                f"call it by its full name: await page.call_tool({keys[0]!r}, {{...}})"
            )
        if not keys:
            raise AttributeError(name)

        spec = registry[keys[0]]
        if spec.agent_fields is None:
            raise AttributeError(name)
        full_name = keys[0]

        async def call(**arguments: Any) -> ActionResult:
            return await self.call_tool(full_name, arguments)

        call.__name__ = name
        call.__doc__ = spec.description
        return call

    # --------------------------------------------------------------- content

    async def extract(self, fmt: str = "markdown", *, main_content: bool = True) -> str:
        """One extraction, returned directly rather than as an index into
        `ActionResult.extracts` -- the positional correlation that made content
        awkward to reach before (plan D6)."""

        result = await self.execute(
            [
                spi_actions.ExtractAction(
                    format=fmt,  # type: ignore[arg-type]
                    main_content=main_content,
                )
            ]
        )
        return result.extracts[0] if result.extracts else ""

    async def markdown(self, *, main_content: bool = True) -> str:
        return await self.extract("markdown", main_content=main_content)

    async def html(self) -> str:
        return await self.extract("html", main_content=False)

    async def text(self, *, main_content: bool = True) -> str:
        return await self.extract("text", main_content=main_content)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        result = await self.execute([spi_actions.ScreenshotAction(full_page=full_page)])
        return result.screenshots[0] if result.screenshots else b""

    async def snapshot(self, *, settle: bool = True) -> Snapshot | None:
        """What the page offers, as a model reads it: the indexed-element text
        plus the role/name/box behind each ref in it.

        Settles first by default. A snapshot is an act of perception, and
        capturing a page whose frames and scripts have not finished is how you
        get a tree that is missing the element you were about to act on --
        cross-origin iframes in particular attach late, so an unsettled capture
        routinely misses them entirely. `settle=False` for the cheap read when
        you know the page is already still.

        Returns a `Snapshot`, not the fused `EnhancedDOMTreeNode`, and does so on
        every transport -- the tree does not survive a network hop, and this
        carries the same information the model would have had. An in-process
        caller that needs the tree itself uses `BrowserSession.tree()`.
        """

        result = await self.execute([spi_actions.SnapshotAction(settle=settle)])
        if result.snapshots:
            return result.snapshots[0]
        return self._serialize_first_tree(result)

    def _serialize_first_tree(self, result: ActionResult) -> Snapshot | None:
        """Serialize a locally-produced tree, for transports that have one.

        A remote result arrives with `snapshots` already filled and never takes
        this path. A local one arrives with `fused_trees` and no `snapshots` --
        the driver produces trees; serializing them is the caller's job -- so the
        local transport overrides this.

        A hook rather than doing it in `execute` so the cost is paid only when
        someone actually asks for a snapshot: `tree()` wants the tree itself and
        should not pay to render text nobody reads. And a hook here rather than
        an import there, because `verbs` may not import `crawlpilot.dom`.
        """

        return None
