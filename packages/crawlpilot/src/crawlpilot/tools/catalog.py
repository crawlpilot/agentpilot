"""Every browser verb, declared once.

**This is the file you edit to add a verb.** The wire model, the agent model,
the JSON schema, the provider adapters and the dataclass conversion all follow
from the entry below; nothing else needs to change.

Descriptions are lifted verbatim from `agent.actions._ACTION_DESCRIPTIONS` so
the prompt text an agent sees is unchanged by the consolidation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field as PydanticField
from pydantic import field_validator

from crawlpilot.spi import actions as sa
from crawlpilot.tools.spec import REF_DESCRIPTION, ToolSpec

_REF = {"ref": REF_DESCRIPTION}

_REF_REQUIRED: dict[str, tuple[type, object]] = {"ref": (str, ...)}
"""Pin `ref` back to a required string for the tool boundary.

The interaction actions gained an optional `selector` alongside `ref` in 0.2, so
on the dataclass both are now `str | None`. That is right for a *programmatic*
caller choosing one or the other, and wrong here twice over. It would relax
`ref` from required to nullable in the published wire schema, breaking an
integrator's existing request for no reason; and `selector` is deliberately not
in any `wire_fields`/`agent_fields` list, because a model addresses elements by
ref -- refs are unambiguous, and they reach into iframes and shadow roots, which
is the whole argument in `driver/node_index.py`. This keeps the tool schema
byte-identical to what it was before the selector work."""

_TARGET = {
    "ref": (
        "The element ref from the current page state, e.g. 'e12'. Give this or "
        "`selector`; a ref is cheaper and reaches inside iframes and shadow roots."
    ),
    "selector": (
        "A CSS selector, when you have no ref for the element. Give this or `ref`."
    ),
}


@field_validator("url")
def _http_or_https_only(cls: object, v: str) -> str:  # noqa: N805
    """A model must not be able to steer the browser to `file://`,
    `javascript:` or a `data:` payload -- a security guard, not tidiness."""

    if not v.startswith(("http://", "https://")):
        raise ValueError("navigate url must be an absolute http:// or https:// URL")
    return v

CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="navigate",
        description="Navigate the browser to an absolute http(s) URL.",
        action_cls=sa.NavigateAction,
        wire_fields=("url", "timeout_ms"),
        agent_fields=("url",),
        validators={"_http_or_https_only": _http_or_https_only},
    ),
    ToolSpec(
        name="go_back",
        description="Go back to the previous page in history.",
        action_cls=sa.GoBackAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="forward",
        description="Go forward to the next page in history.",
        action_cls=sa.ForwardAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="reload",
        description="Reload the current page.",
        action_cls=sa.ReloadAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="snapshot",
        description="Capture the fused DOM/accessibility tree of the current page.",
        action_cls=sa.SnapshotAction,
        wire_fields=("viewport_only", "max_nodes", "roles", "selector", "depth"),
        # Not offered to agents: the loop takes its own snapshot each step, so
        # letting a model request one adds a turn without adding information.
        agent_fields=None,
    ),
    ToolSpec(
        name="diff_snapshot",
        description=(
            "Report what changed on the page since the last snapshot -- elements "
            "added, removed, moved or changed state. Much cheaper than re-reading "
            "the whole page to work out what a click did."
        ),
        action_cls=sa.DiffSnapshotAction,
        wire_fields=("settle",),
        # Agent-exposed where `snapshot` is not, and for the opposite reason:
        # the loop already hands the model a fresh observation each step, but it
        # cannot know when the model wants to check the effect of an action
        # *within* a step without paying for a second full read.
        agent_fields=(),
    ),
    ToolSpec(
        name="extract",
        description="Read the current page's main content as markdown or plain text.",
        action_cls=sa.ExtractAction,
        wire_fields=(
            "format",
            "main_content",
            "include_tags",
            "exclude_tags",
            "relevance_query",
            "citations",
        ),
        agent_fields=("format",),
        wire_overrides={
            "include_tags": (list[str], PydanticField(default_factory=list)),
            "exclude_tags": (list[str], PydanticField(default_factory=list)),
        },
        agent_overrides={"format": (Literal["markdown", "text"], "markdown")},
    ),
    ToolSpec(
        name="screenshot",
        description="Capture a screenshot of the current page.",
        action_cls=sa.ScreenshotAction,
        wire_fields=("full_page",),
        agent_fields=(),
    ),
    ToolSpec(
        name="wait",
        description="Wait `ms` milliseconds before the next action.",
        action_cls=sa.WaitAction,
        wire_fields=("ms", "ref"),
        agent_fields=("ms",),
    ),
    ToolSpec(
        name="execute_js",
        description="Evaluate JavaScript in the page and return its result.",
        action_cls=sa.ExecuteJsAction,
        wire_fields=("script",),
        # Arbitrary JS execution is security-sensitive; never in the agent's
        # default set. A caller may still opt in by widening `allowed_actions`.
        agent_fields=None,
        safety="sensitive",
    ),
    ToolSpec(
        name="click",
        description="Click the element identified by `ref` (from the most recent page state).",
        action_cls=sa.ClickAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref", "all"),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="fill",
        description=(
            "Fill a text input/textarea identified by `ref` with `text`. Replaces "
            "any existing value; pass clear=false to append instead."
        ),
        action_cls=sa.FillAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref", "text", "clear"),
        agent_fields=("ref", "text", "clear"),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="select_option",
        description="Select one or more `values` in a <select> element identified by `ref`.",
        action_cls=sa.SelectOptionAction,
        wire_fields=("ref", "values"),
        agent_fields=("ref", "values"),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="hover",
        description="Hover the mouse over the element identified by `ref`.",
        action_cls=sa.HoverAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="press",
        description="Press a keyboard key (e.g. 'Enter', 'Tab').",
        action_cls=sa.PressAction,
        wire_fields=("key",),
        agent_fields=("key",),
    ),
    ToolSpec(
        name="scroll",
        description=(
            "Scroll the page, or an element identified by `ref`, in `direction`. "
            "`pages` is how far in screenfuls: 0.5 is half a screen, 10 effectively "
            "reaches the end."
        ),
        action_cls=sa.ScrollAction,
        wire_fields=("direction", "pages", "ref"),
        agent_fields=("direction", "pages", "ref"),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="send_keys",
        description=(
            "Send a key or keyboard shortcut to whatever has focus, e.g. 'Escape', "
            "'PageDown', 'Control+a'. Use this for shortcuts and for keys that have "
            "no element to click."
        ),
        action_cls=sa.SendKeysAction,
        wire_fields=("keys",),
        agent_fields=("keys",),
    ),
    ToolSpec(
        name="find_text",
        description=(
            "Scroll to the first occurrence of `text` on the page. Use this to reach "
            "content you know is present but which is not listed in the page state."
        ),
        action_cls=sa.FindTextAction,
        wire_fields=("text",),
        agent_fields=("text",),
    ),
    ToolSpec(
        name="dropdown_options",
        description=(
            "List the options of a <select> identified by `ref`. Call this before "
            "select_option when you do not already know the exact option text."
        ),
        action_cls=sa.DropdownOptionsAction,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="search_page",
        description=(
            "Search the page's text for a pattern and return the matches with "
            "surrounding context. Cheap and instant -- prefer it over extract when "
            "you only need to check whether something is present or find where it is."
        ),
        action_cls=sa.SearchPageAction,
        wire_fields=(
            "pattern",
            "regex",
            "case_sensitive",
            "context_chars",
            "max_results",
            "css_scope",
        ),
        agent_fields=("pattern", "regex"),
    ),
    ToolSpec(
        name="find_elements",
        description=(
            "Query the page with a CSS selector and return each match's tag, text and "
            "requested attributes. Use it to read repeated structure -- table rows, "
            "product cards, links -- without spending a full page observation."
        ),
        action_cls=sa.FindElementsAction,
        wire_fields=("selector", "attributes", "max_results", "include_text"),
        agent_fields=("selector", "attributes"),
        wire_overrides={"attributes": (list[str], PydanticField(default_factory=list))},
        agent_overrides={"attributes": (list[str], PydanticField(default_factory=list))},
    ),
    ToolSpec(
        name="upload_file",
        description="Attach a local file to a file input identified by `ref`.",
        action_cls=sa.UploadFileAction,
        wire_fields=("ref", "path"),
        # Never offered to an agent: `path` is read from the machine the driver
        # runs on, so a model choosing it could upload any file the worker can
        # read to a site it controls. Safe exposure needs a caller-supplied path
        # allowlist (browser-use's `available_file_paths`), which is a
        # session-config concept crawlpilot does not have yet.
        agent_fields=None,
        safety="sensitive",
    ),
    # Interaction verbs beyond click/fill. Each is a shape a click cannot
    # express: an idempotent end state, a held key, a touch event, a drag.
    ToolSpec(
        name="double_click",
        description="Double-click the element identified by `ref`.",
        action_cls=sa.DoubleClickAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="focus",
        description="Move keyboard focus to the element identified by `ref`.",
        action_cls=sa.FocusAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="check",
        description=(
            "Ensure a checkbox or radio is checked. Prefer this over click: it is "
            "idempotent, so it cannot un-check a box that was already checked."
        ),
        action_cls=sa.CheckAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="uncheck",
        description="Ensure a checkbox is unchecked.",
        action_cls=sa.UncheckAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="scroll_into_view",
        description="Scroll until the element identified by `ref` is on screen.",
        action_cls=sa.ScrollIntoViewAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="clear",
        description="Empty the text field identified by `ref`.",
        action_cls=sa.ClearAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="drag",
        description="Drag the element identified by `ref` onto the one at `to_ref`.",
        action_cls=sa.DragAction,
        wire_fields=("ref", "to_ref"),
        agent_fields=("ref", "to_ref"),
        field_descriptions={
            "ref": REF_DESCRIPTION,
            "to_ref": "The ref of the element to drop onto, in the same format.",
        },
    ),
    ToolSpec(
        name="key_down",
        description="Press and hold a key, e.g. 'Shift'. Release it with key_up.",
        action_cls=sa.KeyDownAction,
        wire_fields=("key",),
        agent_fields=("key",),
    ),
    ToolSpec(
        name="key_up",
        description="Release a key held with key_down.",
        action_cls=sa.KeyUpAction,
        wire_fields=("key",),
        agent_fields=("key",),
    ),
    ToolSpec(
        name="insert_text",
        description=(
            "Insert text at the cursor in one go, as a paste would. Prefer fill for "
            "ordinary typing -- it fires the per-key events autocomplete and "
            "validation widgets rely on."
        ),
        action_cls=sa.InsertTextAction,
        wire_fields=("text",),
        agent_fields=("text",),
    ),
    ToolSpec(
        name="tap",
        description=(
            "Tap the element identified by `ref` with a touch event. Use on pages "
            "that respond to touch but not to clicks."
        ),
        action_cls=sa.TapAction,
        wire_overrides=_REF_REQUIRED,
        agent_overrides=_REF_REQUIRED,
        wire_fields=("ref",),
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="swipe",
        description=(
            "Swipe with a touch gesture, for carousels and pull-to-refresh. The "
            "direction is the finger's, as on a phone."
        ),
        action_cls=sa.SwipeAction,
        wire_fields=("direction", "distance", "ref"),
        agent_fields=("direction", "distance", "ref"),
        field_descriptions=_REF,
    ),
    # Waiting for a state (see `driver.waits`). A wait that expires raises, so a
    # model can trust that the action after it ran against the state it asked
    # for -- the whole value of the verb.
    ToolSpec(
        name="wait_for_selector",
        description=(
            "Wait until a CSS selector matches an element in the given state. Use "
            "this after an action that triggers loading, instead of guessing at a "
            "fixed wait."
        ),
        action_cls=sa.WaitForSelectorAction,
        wire_fields=("selector", "state", "timeout_ms"),
        agent_fields=("selector", "state"),
    ),
    ToolSpec(
        name="wait_for_text",
        description="Wait until the given text appears anywhere on the page.",
        action_cls=sa.WaitForTextAction,
        wire_fields=("text", "timeout_ms"),
        agent_fields=("text",),
    ),
    ToolSpec(
        name="wait_for_url",
        description=(
            "Wait until the page URL contains the given string, or matches it as a "
            "glob. Use after a click you expect to navigate."
        ),
        action_cls=sa.WaitForUrlAction,
        wire_fields=("url", "timeout_ms"),
        agent_fields=("url",),
    ),
    ToolSpec(
        name="wait_for_load",
        description="Wait until the page reaches a load state.",
        action_cls=sa.WaitForLoadAction,
        wire_fields=("state", "timeout_ms"),
        agent_fields=("state",),
    ),
    ToolSpec(
        name="wait_for_function",
        description="Wait until a JavaScript expression evaluates truthy.",
        action_cls=sa.WaitForFunctionAction,
        wire_fields=("expression", "timeout_ms", "poll_ms"),
        # `execute_js` on a timer. Same reasoning, same answer.
        agent_fields=None,
        safety="sensitive",
    ),
    # Reading one element (see `driver.queries`). These are what let a model
    # check an action instead of assuming it worked.
    ToolSpec(
        name="get_text",
        description="Read the visible text of one element.",
        action_cls=sa.GetTextAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="get_html",
        description=(
            "Read one element's inner HTML. Element-scoped only -- to read the "
            "page, use extract."
        ),
        action_cls=sa.GetHtmlAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="get_value",
        description="Read the current value of an input, textarea or select.",
        action_cls=sa.GetValueAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="get_attribute",
        description="Read one attribute of one element.",
        action_cls=sa.GetAttributeAction,
        wire_fields=("name", "ref", "selector"),
        agent_fields=("name", "ref", "selector"),
        field_descriptions={**_TARGET, "name": "The attribute to read, e.g. 'href'."},
    ),
    ToolSpec(
        name="get_count",
        description="Count how many elements match a CSS selector.",
        action_cls=sa.GetCountAction,
        wire_fields=("selector",),
        agent_fields=("selector",),
    ),
    ToolSpec(
        name="get_box",
        description="Read an element's position and size in page coordinates.",
        action_cls=sa.GetBoxAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="get_styles",
        description="Read an element's computed CSS properties.",
        action_cls=sa.GetStylesAction,
        wire_fields=("ref", "selector", "properties"),
        agent_fields=("ref", "selector", "properties"),
        wire_overrides={"properties": (list[str], PydanticField(default_factory=list))},
        agent_overrides={"properties": (list[str], PydanticField(default_factory=list))},
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="get_url",
        description="The current page URL.",
        action_cls=sa.GetUrlAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="get_title",
        description="The current page title.",
        action_cls=sa.GetTitleAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="is_visible",
        description="Whether an element is visible to a user.",
        action_cls=sa.IsVisibleAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="is_enabled",
        description="Whether an element is enabled rather than disabled.",
        action_cls=sa.IsEnabledAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="is_checked",
        description="Whether a checkbox or radio is checked.",
        action_cls=sa.IsCheckedAction,
        wire_fields=("ref", "selector"),
        agent_fields=("ref", "selector"),
        field_descriptions=_TARGET,
    ),
    ToolSpec(
        name="pdf",
        description="Render the current page to a PDF.",
        action_cls=sa.PdfAction,
        wire_fields=("landscape", "print_background", "scale"),
        agent_fields=(),
    ),
    ToolSpec(
        name="list_frames",
        description="List the frames (iframes) on this page.",
        action_cls=sa.ListFramesAction,
        # Agent-exposed, unlike `list_tabs`: the observation carries the tab
        # list but not the frame list, so this is information a model has no
        # other way to get. Refs already reach into frames, so there is no
        # frame_switch to go with it.
        agent_fields=(),
    ),
    ToolSpec(
        name="download",
        description=(
            "Click something that produces a file, and wait for the file. Returns "
            "where it was saved."
        ),
        action_cls=sa.DownloadAction,
        wire_fields=("ref", "timeout_ms"),
        # Agent-exposed where `upload_file` is not, and the difference is who
        # names the path: here the driver does, into a session-scoped directory,
        # so a model can neither read nor overwrite the operator's files.
        agent_fields=("ref",),
        field_descriptions=_REF,
    ),
    ToolSpec(
        name="wait_for_download",
        description="Wait for a download the page has already started.",
        action_cls=sa.WaitForDownloadAction,
        wire_fields=("timeout_ms",),
        agent_fields=(),
    ),
    ToolSpec(
        name="clipboard_read",
        description="Read the browser clipboard.",
        action_cls=sa.ClipboardReadAction,
        # Host state that may hold whatever the operator last copied. A model
        # able to read it is a model able to paste it into any page it can type
        # into -- `upload_file`'s argument exactly.
        agent_fields=None,
        safety="sensitive",
    ),
    ToolSpec(
        name="clipboard_write",
        description="Write text to the browser clipboard.",
        action_cls=sa.ClipboardWriteAction,
        wire_fields=("text",),
        agent_fields=None,
        safety="sensitive",
    ),
    # JavaScript dialogs. Agent-exposed, and they have to be: a `confirm()` is
    # the page asking a question only the caller can answer, and the alternative
    # -- Playwright's silent auto-dismiss -- makes a click on "Delete account"
    # report success while quietly declining on the model's behalf. See
    # `driver.dialogs` for why the three verbs and the input guard are one
    # change.
    ToolSpec(
        name="dialog_status",
        description=(
            "Report the JavaScript dialog (alert/confirm/prompt) currently blocking "
            "the page, if any. Safe to call at any time -- use it after a click that "
            "seemed to do nothing."
        ),
        action_cls=sa.DialogStatusAction,
        agent_fields=(),
    ),
    ToolSpec(
        name="dialog_accept",
        description=(
            "Answer the open dialog affirmatively: OK on an alert, Yes on a confirm, "
            "submit on a prompt. Nothing else can happen on this page until the "
            "dialog is answered."
        ),
        action_cls=sa.DialogAcceptAction,
        wire_fields=("prompt_text",),
        agent_fields=("prompt_text",),
        field_descriptions={
            "prompt_text": (
                "Text to submit for a prompt dialog. Omit for alert and confirm, or "
                "to submit a prompt's own default value."
            )
        },
    ),
    ToolSpec(
        name="dialog_dismiss",
        description=(
            "Answer the open dialog negatively: Cancel on a confirm or prompt, and "
            "the only available answer to an alert."
        ),
        action_cls=sa.DialogDismissAction,
        agent_fields=(),
    ),
    # Tab management. Agent-exposed, as in browser-use: a link that opens in a
    # new tab, a checkout that pops one, a comparison across two pages are all
    # ordinary tasks, and without these verbs the agent is stranded on whichever
    # tab it happens to be on. The tab list is already in every observation, so
    # the model has the `page_id`s to name.
    ToolSpec(
        name="new_tab",
        description="Open a new tab, optionally at a URL, and make it active.",
        action_cls=sa.NewTabAction,
        wire_fields=("url",),
        agent_fields=("url",),
    ),
    ToolSpec(
        name="close_tab",
        description="Close a tab by page id, as shown in the tab list.",
        action_cls=sa.CloseTabAction,
        wire_fields=("page_id",),
        agent_fields=("page_id",),
    ),
    ToolSpec(
        name="switch_tab",
        description="Make a tab active by page id, as shown in the tab list.",
        action_cls=sa.SwitchTabAction,
        wire_fields=("page_id",),
        agent_fields=("page_id",),
    ),
    ToolSpec(
        name="list_tabs",
        description="List the tabs open in this session.",
        action_cls=sa.ListTabsAction,
        # Not agent-exposed: every observation already carries the tab list, so
        # this could only ever return what the model was just shown.
        agent_fields=None,
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in CATALOG}
