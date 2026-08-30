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

from crawlpilot.tools.catalog import CATALOG
from crawlpilot.tools.spec import Safety, ToolSpec

BROWSER_NAMESPACE = "browser"


class DuplicateToolError(ValueError):
    pass


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


def browser_tools() -> ToolRegistry:
    """Every built-in browser verb, in the `browser` namespace."""

    return ToolRegistry((BROWSER_NAMESPACE, spec) for spec in CATALOG)
