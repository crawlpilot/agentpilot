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
        wire_fields=("format", "main_content", "include_tags", "exclude_tags"),
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
