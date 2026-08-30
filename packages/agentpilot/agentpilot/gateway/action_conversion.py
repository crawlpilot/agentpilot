"""`ActionIn` (the HTTP-boundary Pydantic union) -> `spi.actions.Action`.

This was a 17-entry table of `Model: lambda a: SpiAction(field=a.field, ...)`
converters, hand-written and hand-maintained alongside two other copies of the
same vocabulary (plan D5). Every entry restated field names that were already
identical on both sides, so the only thing it could do was drift.

Both sides are now projections of one `tools.ToolSpec`, which knows the
dataclass it builds and exposes fields under the same names -- so the conversion
is `spec.from_model(parsed)` and there is nothing left to keep in sync. Adding a
verb touches `tools/catalog.py` and nothing here.
"""

from __future__ import annotations

from typing import Any

from crawlpilot.spi import actions as spi_actions
from crawlpilot.tools import BY_NAME


def to_spi_action(action_in: Any) -> spi_actions.Action:
    spec = BY_NAME.get(action_in.type)
    if spec is None:
        # Unreachable through the API -- the discriminated union rejects an
        # unknown `type` at parse time. Explicit anyway: a verb present in the
        # union but missing from the catalog would otherwise fail as a confusing
        # `NoneType` attribute error somewhere deeper.
        raise ValueError(f"no tool spec for action type {action_in.type!r}")
    return spec.from_model(action_in)  # type: ignore[no-any-return]
