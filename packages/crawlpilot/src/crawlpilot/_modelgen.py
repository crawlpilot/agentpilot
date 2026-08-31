"""One place that turns a dataclass into a Pydantic model.

The dataclass stays the source of truth for *types and defaults* -- they are read
off it, never restated -- and each caller supplies only what a dataclass cannot
carry: which fields a surface exposes, their descriptions, and overrides for the
fields whose wire form genuinely differs from their in-process form.

Two callers, deliberately at different layers and neither importing the other:

- `tools.spec` projects `spi.actions` *request* dataclasses onto the HTTP and
  agent surfaces (`ToolSpec.wire_model()` / `agent_model()`).
- `wire` projects `spi.actions.ActionResult` -- the *response* -- onto its HTTP
  form.

This module was `tools/spec.py::_dataclass_fields`, private to the request side.
It moved here when the response side needed exactly the same reader: a second
copy would have been the beginning of the drift the request-side consolidation
existed to remove.

Imports pydantic and nothing else of ours, so it sits below every package that
uses it and can never become a cycle.
"""

from __future__ import annotations

import dataclasses
from typing import Any, get_type_hints

from pydantic import Field


def dataclass_fields(
    cls: type, *, localns: dict[str, Any] | None = None
) -> dict[str, tuple[Any, Any]]:
    """`{name: (type, default)}` read off the dataclass.

    A `default_factory` is passed through to Pydantic as a factory rather than
    called here. Resolving it to a literal would emit `"default": []` into the
    JSON Schema, where a hand-written model emitted no `default` at all -- a
    visible wire-schema change for what should be a pure refactor.

    `localns` supplies names the dataclass annotates with but only imports under
    `TYPE_CHECKING` -- `get_type_hints` evaluates *every* annotation on the
    class, so one such name makes the whole read fail even when the caller
    intends to override that very field. `spi.actions.ActionResult` is the case:
    it annotates `EnhancedDOMTreeNode` and `SnapshotView`, imports neither at
    runtime, and `wire` overrides both.
    """

    hints = get_type_hints(cls, localns=localns)
    out: dict[str, tuple[Any, Any]] = {}
    for f in dataclasses.fields(cls):
        if f.default is not dataclasses.MISSING:
            default: Any = f.default
        elif f.default_factory is not dataclasses.MISSING:
            default = Field(default_factory=f.default_factory)
        else:
            default = ...  # required
        out[f.name] = (hints[f.name], default)
    return out
