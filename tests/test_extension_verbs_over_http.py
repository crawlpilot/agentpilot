"""A verb an extension contributed, dispatched over the HTTP boundary.

Before this, it could not be. `gateway/schemas.py` built its action union from
`CATALOG` at module import -- before any `ExtensionRegistry` exists -- so a
`ToolMount`'s namespaced verb had nothing on the wire that could accept it. The
namespacing that exists precisely so a third party can ship `walmart.solve_wall`
was unreachable from outside the process.

Two doors now, and these tests cover both plus the ways they fail:
the published union for built-ins, and `ExtensionActionIn` -> the deployment's
live registry for everything else.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import TypeAdapter, ValidationError

from agentpilot.gateway.action_conversion import to_spi_action
from agentpilot.gateway.schemas import ActionIn, ExtensionActionIn
from crawlpilot.tools import ToolSpec, UnknownToolError, browser_tools

ADAPTER = TypeAdapter(ActionIn)


@dataclasses.dataclass
class SolveWallAction:
    reason: str = ""


def _deployment_registry():  # type: ignore[no-untyped-def]
    """What `wiring.extensions.tools` is: built-ins plus contributions."""

    registry = browser_tools()
    registry.register(
        ToolSpec(
            name="solve_wall",
            description="Clear a known wall.",
            action_cls=SolveWallAction,
            wire_fields=("reason",),
            agent_fields=("reason",),
        ),
        namespace="walmart",
    )
    return registry


# ------------------------------------------------------------- the two doors


def test_a_builtin_verb_takes_the_typed_path() -> None:
    parsed = ADAPTER.validate_python({"type": "click", "ref": "e12"})
    assert type(parsed).__name__ == "ClickActionIn"
    assert to_spi_action(parsed, _deployment_registry()).ref == "e12"


def test_an_extension_verb_reaches_the_driver_through_the_registry() -> None:
    parsed = ADAPTER.validate_python({"type": "walmart.solve_wall", "reason": "captcha"})
    assert isinstance(parsed, ExtensionActionIn)
    assert to_spi_action(parsed, _deployment_registry()) == SolveWallAction(reason="captcha")


def test_a_deployment_without_the_extension_rejects_it_by_name() -> None:
    """And says what it *does* offer -- which a bare schema rejection cannot."""

    parsed = ADAPTER.validate_python({"type": "walmart.solve_wall"})
    with pytest.raises(UnknownToolError, match="no such tool 'walmart.solve_wall'"):
        to_spi_action(parsed, browser_tools())


# ------------------------------------------------------------- failure modes


def test_a_malformed_builtin_is_still_a_validation_error_not_a_silent_pass() -> None:
    """The regression the passthrough could have introduced.

    `{"type": "click"}` with no target no longer matches the discriminated
    union, so it falls through to `ExtensionActionIn` -- which accepts anything.
    It must then be re-validated against `click`'s real `ToolSpec` rather than
    reaching the driver as an empty action. `gateway/errors.py` maps the result
    to a 400, not the catch-all 500.
    """

    parsed = ADAPTER.validate_python({"type": "click"})
    assert isinstance(parsed, ExtensionActionIn)
    with pytest.raises(ValidationError):
        to_spi_action(parsed, _deployment_registry())


def test_an_action_with_no_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"ref": "e12"})


# ------------------------------------------------ domains reach the boundary


def test_a_site_specific_verb_is_only_offered_where_it_applies() -> None:
    """`ToolSpec.domains` existed but had no effect at the HTTP boundary, since
    the union never consulted a registry. `wire_union(page_url=...)` is the
    route from the declaration to the schema."""

    registry = browser_tools()
    registry.register(
        ToolSpec(
            name="solve_wall",
            description="Clear a known wall.",
            action_cls=SolveWallAction,
            wire_fields=("reason",),
            agent_fields=("reason",),
            domains=("*.walmart.com",),
        ),
        namespace="walmart",
    )

    on_site = registry.subset(page_url="https://www.walmart.com/ip/123").names
    off_site = registry.subset(page_url="https://example.com/").names

    assert "walmart.solve_wall" in on_site
    assert "walmart.solve_wall" not in off_site


def test_a_registry_can_project_its_own_union() -> None:
    """The primitive all of the above rests on: a registry projects itself, so
    the schema a boundary validates against follows from *that* registry rather
    than from the built-in catalog."""

    registry = _deployment_registry()
    schema = TypeAdapter(registry.wire_union()).json_schema()
    assert "SolveWallActionIn" in schema["$defs"]
    assert "ClickActionIn" in schema["$defs"]
