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
        name="snapshot",
        description="Capture the fused DOM/accessibility tree of the current page.",
        action_cls=sa.SnapshotAction,
        wire_fields=("viewport_only", "max_nodes", "roles"),
        # Not offered to agents: the loop takes its own snapshot each step, so
        # letting a model request one adds a turn without adding information.
        agent_fields=None,
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
        description="Fill a text input/textarea identified by `ref` with `text`.",
        action_cls=sa.FillAction,
        wire_fields=("ref", "text"),
        agent_fields=("ref", "text"),
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
        description="Scroll the page (or an element identified by `ref`) in `direction`.",
        action_cls=sa.ScrollAction,
        wire_fields=("direction", "ref"),
        agent_fields=("direction", "ref"),
        field_descriptions=_REF,
    ),
    # Tab management: complexity without a clear agent-facing need, so wire-only.
    ToolSpec(
        name="new_tab",
        description="Open a new tab, optionally at a URL.",
        action_cls=sa.NewTabAction,
        wire_fields=("url",),
        agent_fields=None,
    ),
    ToolSpec(
        name="close_tab",
        description="Close a tab by page id.",
        action_cls=sa.CloseTabAction,
        wire_fields=("page_id",),
        agent_fields=None,
    ),
    ToolSpec(
        name="switch_tab",
        description="Make a tab active by page id.",
        action_cls=sa.SwitchTabAction,
        wire_fields=("page_id",),
        agent_fields=None,
    ),
    ToolSpec(
        name="list_tabs",
        description="List the tabs open in this session.",
        action_cls=sa.ListTabsAction,
        agent_fields=None,
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in CATALOG}
