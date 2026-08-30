"""Provider-shaped views of a `ToolSpec`.

Pure dict re-shaping over the JSON Schema the registry already emits. **No
vendor SDK is imported here, and none may be**: the moment an adapter needs a
provider package, it stops being a re-shaping of our own artifact and belongs in
the caller's code, not in a browser library. A test asserts the whole `tools`
package imports no LLM SDK.

Kept separate from `registry.py` for the same reason: the registry's unit is a
name, a description and a schema, and an agent framework, an MCP server, a CLI
and a plain crawler are all equally first-class consumers of that. Vendor
formats are a convenience on top, not the core contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from crawlpilot.tools.registry import BROWSER_NAMESPACE, ToolRegistry
from crawlpilot.tools.spec import ToolSpec


def _qualified(spec: ToolSpec, namespace: str) -> str:
    return f"{namespace}_{spec.name}" if namespace != BROWSER_NAMESPACE else spec.name


def _params(spec: ToolSpec) -> dict[str, Any]:
    """The schema minus the `type` discriminator: a provider passes the tool
    name out of band, so repeating it as a required property would make every
    call carry a redundant field the model can get wrong."""

    schema = spec.json_schema()
    properties = {k: v for k, v in schema.get("properties", {}).items() if k != "type"}
    required = [r for r in schema.get("required", []) if r != "type"]
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    if "$defs" in schema:
        out["$defs"] = schema["$defs"]
    return out


def to_anthropic(
    specs: Iterable[ToolSpec] | ToolRegistry, *, namespace: str = BROWSER_NAMESPACE
) -> list[dict[str, Any]]:
    return [
        {
            "name": _qualified(spec, namespace),
            "description": spec.description,
            "input_schema": _params(spec),
        }
        for spec in specs
    ]


def to_openai(
    specs: Iterable[ToolSpec] | ToolRegistry, *, namespace: str = BROWSER_NAMESPACE
) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _qualified(spec, namespace),
                "description": spec.description,
                "parameters": _params(spec),
            },
        }
        for spec in specs
    ]


def to_mcp(
    specs: Iterable[ToolSpec] | ToolRegistry, *, namespace: str = BROWSER_NAMESPACE
) -> list[dict[str, Any]]:
    """MCP `tools/list` descriptors."""

    return [
        {
            "name": _qualified(spec, namespace),
            "description": spec.description,
            "inputSchema": _params(spec),
        }
        for spec in specs
    ]
