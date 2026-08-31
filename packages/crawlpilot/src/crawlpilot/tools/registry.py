"""The vendor-neutral tool registry.

Its unit is a `ToolSpec`: a name, a description, a Pydantic param model and
plain JSON Schema. **Nothing here knows what an LLM provider is.** An agent
framework, an MCP server, a CLI, a test harness and a plain crawler are all
equally first-class consumers, so the core has no business privileging one
vendor's tool format -- provider shapes live in `tools.adapters` as pure dict
re-shaping over the JSON Schema this already emits.

Names are **namespaced** (`browser.click`, `walmart.solve_wall`) and registering
a duplicate **raises** rather than silently overriding -- Browser4's
`CustomToolRegistry.register` contract, and the right one: a silent override
means an extension can shadow a built-in verb and nothing says so.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from crawlpilot.tools.catalog import CATALOG
from crawlpilot.tools.spec import Safety, ToolSpec, union_of

BROWSER_NAMESPACE = "browser"


class DuplicateToolError(ValueError):
    pass


class UnknownToolError(ValueError):
    """A wire payload named a verb this registry does not have.

    Distinct from a `ValidationError` (the verb exists, the arguments do not
    fit) because the two mean different things to a caller and deserve different
    HTTP statuses -- and because the message can name what *is* available, which
    a schema rejection cannot.
    """


class ToolRegistry:
    """A filterable set of `ToolSpec`s, addressed by `namespace.name`."""

    def __init__(self, specs: Iterable[tuple[str, ToolSpec]] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for namespace, spec in specs:
            self.register(spec, namespace=namespace)

    # --------------------------------------------------------------- loading

    def register(self, spec: ToolSpec, *, namespace: str = BROWSER_NAMESPACE) -> str:
        key = f"{namespace}.{spec.name}"
        if key in self._specs:
            raise DuplicateToolError(
                f"tool {key!r} is already registered. Tool names are namespaced "
                "precisely so two providers can share a verb; pick a different "
                "namespace rather than overriding a registered tool."
            )
        self._specs[key] = spec
        return key

    # ---------------------------------------------------------------- access

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[ToolSpec]:
        return iter(self._specs.values())

    def __contains__(self, key: str) -> bool:
        return key in self._specs

    def __getitem__(self, key: str) -> ToolSpec:
        return self._specs[key]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, name: str, *, namespace: str = BROWSER_NAMESPACE) -> ToolSpec | None:
        return self._specs.get(name if "." in name else f"{namespace}.{name}")

    # -------------------------------------------------------------- filtering

    def subset(
        self,
        *,
        allowed: Sequence[str] | None = None,
        exclude: Iterable[str] = (),
        safety: Safety | None = None,
        agent_exposed: bool = False,
        page_url: str | None = None,
    ) -> ToolRegistry:
        """A narrower registry.

        `agent_exposed=True` keeps only verbs a model may call -- the replacement
        for `agent.actions.DEFAULT_ALLOWED_ACTIONS`, except that the exclusion
        lives with the verb (`agent_fields=None`) rather than in a separate tuple
        that had to be kept in step with it.

        `page_url` applies each spec's `domains`, so a site-specific verb is
        offered only on the sites it works on (browser-use's per-page action
        filtering, `tools/registry/views.py:96-149`). Passing `None` keeps only
        the unrestricted verbs, which is the right default for a schema built
        before any page is open: a verb that needs a URL to be applicable cannot
        be applicable when there is no URL.
        """

        excluded = set(exclude)
        out = ToolRegistry()
        for key, spec in self._specs.items():
            bare = key.split(".", 1)[1]
            if allowed is not None and bare not in allowed and key not in allowed:
                continue
            if bare in excluded or key in excluded:
                continue
            if safety is not None and spec.safety != safety:
                continue
            if agent_exposed and spec.agent_fields is None:
                continue
            if not spec.applies_to(page_url):
                continue
            out._specs[key] = spec  # noqa: SLF001 -- same class, bypasses the dup check
        return out

    # ---------------------------------------------------------------- unions
    #
    # The registry can project itself, so the schema a boundary validates
    # against follows from *this* registry rather than from the built-in
    # catalog. That distinction is the whole point.
    #
    # `gateway/schemas.py` built its union as
    # `union_of([spec.wire_model() for spec in CATALOG])` at module import --
    # before any `ExtensionRegistry` exists. Three consequences, none intended:
    # a `ToolMount` extension's verbs could never be dispatched over HTTP at
    # all; `ToolSpec.domains` had no effect at the boundary, so a site-specific
    # verb was offered everywhere; and `safety="sensitive"` could not be
    # withheld per deployment. Adapted from browser-use's
    # `Registry.create_action_model`, which builds its union from the live
    # registry for exactly these reasons.

    def wire_union(self, **narrow: Any) -> Any:
        """A discriminated union of this registry's verbs, HTTP projection.

        Keyword arguments are passed to `subset()`, so a caller narrows and
        projects in one step -- `wire_union(page_url=url)` is the schema that
        page's verbs actually justify.
        """

        return union_of([spec.wire_model() for spec in self.subset(**narrow)])

    def agent_union(self, **narrow: Any) -> Any:
        """The same, in the agent projection -- descriptions included, narrower
        fields. Implies `agent_exposed=True`: a verb with no agent projection
        cannot be in a union built for a model."""

        narrow.setdefault("agent_exposed", True)
        return union_of([spec.agent_model() for spec in self.subset(**narrow)])

    def parse_wire_action(self, payload: dict[str, Any]) -> Any:
        """One `{"type": ..., ...}` from the wire -> its `spi.actions` dataclass.

        Validated against *this* registry, so a verb an extension contributed is
        accepted here and a verb this deployment withheld is not -- which is the
        thing a union frozen at import could not express either way.

        `type` may be bare (`"click"`) or namespaced (`"walmart.solve_wall"`).
        Bare names resolve in the `browser` namespace, preserving what every
        existing client sends.

        Raises `UnknownToolError` for a verb this registry does not have, and
        `pydantic.ValidationError` for one whose arguments do not fit -- the
        caller decides which HTTP status each deserves.
        """

        if not isinstance(payload, dict) or "type" not in payload:
            raise UnknownToolError("each action needs a 'type'")
        name = payload["type"]
        spec = self.get(name) if isinstance(name, str) else None
        if spec is None:
            raise UnknownToolError(
                f"no such tool {name!r}; this deployment offers: "
                f"{', '.join(sorted(self.names))}"
            )
        parsed = spec.wire_model().model_validate({**payload, "type": spec.name})
        return spec.from_model(parsed)


def browser_tools() -> ToolRegistry:
    """Every built-in browser verb, in the `browser` namespace."""

    return ToolRegistry((BROWSER_NAMESPACE, spec) for spec in CATALOG)


# --------------------------------------------------------------------- profiles

CORE_PROFILE: frozenset[str] = frozenset(
    {
        "navigate", "go_back", "forward", "reload",
        "click", "fill", "select_option", "dropdown_options", "hover", "press",
        "send_keys", "scroll", "find_text", "wait",
        "dialog_status", "dialog_accept", "dialog_dismiss",
        "search_page", "find_elements", "extract", "screenshot",
        "new_tab", "switch_tab", "close_tab",
    }
)
"""Everyday browsing: navigate, interact, read, answer a dialog, manage tabs.

Dialogs are in the core set and the other new families are not, because a
`confirm()` is not optional -- a model that cannot answer one is simply stuck on
the page, where a model without `get_styles` merely has to work a little harder.
"""

QUERY_PROFILE: frozenset[str] = frozenset(
    {
        "get_text", "get_html", "get_value", "get_attribute", "get_count",
        "get_box", "get_styles", "get_url", "get_title",
        "is_visible", "is_enabled", "is_checked",
        "diff_snapshot", "list_frames",
    }
)
"""Checking the page rather than acting on it -- what turns an assumption that
an action worked into a verified fact."""

WAIT_PROFILE: frozenset[str] = frozenset(
    {"wait_for_selector", "wait_for_text", "wait_for_url", "wait_for_load"}
)

INTERACTION_PROFILE: frozenset[str] = frozenset(
    {
        "double_click", "focus", "check", "uncheck", "scroll_into_view",
        "clear", "drag", "key_down", "key_up", "insert_text", "tap", "swipe",
    }
)
"""The verbs beyond click/fill -- each a shape a click cannot express."""

FILES_PROFILE: frozenset[str] = frozenset({"download", "wait_for_download", "pdf"})

PROFILES: dict[str, frozenset[str]] = {
    "core": CORE_PROFILE,
    "query": QUERY_PROFILE,
    "wait": WAIT_PROFILE,
    "interaction": INTERACTION_PROFILE,
    "files": FILES_PROFILE,
}
"""Named, composable sets -- agent-browser's `ToolProfile` (`mcp.rs:215-300`),
which exists for the reason this now does too.

Sixty-four verbs is a schema every request carries and every model reads before
choosing one of them. A scrape run should not pay context for `get_styles`, and a
form-filling agent should not pay it for `swipe`. `subset` already did the
filtering; these are the names worth having for it.

Deliberately not exhaustive: `"all"` is `browser_tools()` itself, and a verb in
no profile is still reachable by name through `allowed`.
"""


class UnknownProfileError(ValueError):
    pass


def profile(*names: str, agent_exposed: bool = True) -> ToolRegistry:
    """The union of the named profiles.

    Composable because the useful sets overlap in practice -- a checkout agent
    wants `core` plus `wait` plus `query`, and enumerating that combination as a
    seventh profile would just be a name for someone else's guess.
    """

    unknown = sorted(set(names) - PROFILES.keys())
    if unknown:
        raise UnknownProfileError(
            f"unknown tool profile(s) {unknown}; known: {sorted(PROFILES)}"
        )
    allowed = frozenset().union(*(PROFILES[name] for name in names)) if names else frozenset()
    return browser_tools().subset(allowed=sorted(allowed), agent_exposed=agent_exposed)
