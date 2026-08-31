"""`ActionIn` (the HTTP-boundary Pydantic union) -> `spi.actions.Action`.

This was a 17-entry table of `Model: lambda a: SpiAction(field=a.field, ...)`
converters, hand-written and hand-maintained alongside two other copies of the
same vocabulary (plan D5). Every entry restated field names that were already
identical on both sides, so the only thing it could do was drift.

Both sides are now projections of one `tools.ToolSpec`, which knows the
dataclass it builds and exposes fields under the same names -- so the conversion
is `spec.from_model(parsed)` and there is nothing left to keep in sync. Adding a
verb touches `tools/catalog.py` and nothing here.

What *is* here is the second door: a verb the published union does not contain
(an extension's, or one newer than the client) arrives as
`schemas.ExtensionActionIn` and is resolved against the deployment's live
registry instead of against `CATALOG`. See that class's docstring for why the
published schema stays static.
"""

from __future__ import annotations

from typing import Any

from crawlpilot.spi import actions as spi_actions
from crawlpilot.tools import BY_NAME, ToolRegistry, browser_tools


def to_spi_action(action_in: Any, registry: ToolRegistry | None = None) -> spi_actions.Action:
    """One parsed action -> its dataclass.

    `registry` is the deployment's -- `wiring.extensions.tools`, built-ins plus
    whatever extensions contributed. It is only consulted for a verb the
    published union did not match; a built-in takes the same path it always did.
    Defaulting to `browser_tools()` keeps every existing caller and test working
    against the built-in set.
    """

    # A passthrough: this build's union did not recognise the `type`, so the
    # live registry decides. `model_extra` carries the arguments, since the
    # passthrough model declares only `type`.
    if type(action_in).__name__ == "ExtensionActionIn":
        payload = {"type": action_in.type, **(action_in.model_extra or {})}
        return (registry or browser_tools()).parse_wire_action(payload)

    spec = BY_NAME.get(action_in.type)
    if spec is None:
        # Unreachable through the API -- an unknown `type` becomes an
        # `ExtensionActionIn` above. Explicit anyway: a verb present in the
        # union but missing from the catalog would otherwise fail as a confusing
        # `NoneType` attribute error somewhere deeper.
        raise ValueError(f"no tool spec for action type {action_in.type!r}")
    return spec.from_model(action_in)  # type: ignore[no-any-return]
