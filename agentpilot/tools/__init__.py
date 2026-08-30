"""One definition per browser verb; every surface projected from it.

`catalog.py` is the file you edit to add a verb. The wire model, the agent
model, the JSON schema, the provider adapters and the `spi.actions` conversion
all follow from that entry (plan D5).

The registry is **vendor-neutral**: its unit is a name, a description, a Pydantic
model and plain JSON Schema. Provider shapes live in `tools.adapters` as pure
dict re-shaping, and nothing in this package imports an LLM SDK.
"""

from agentpilot.tools.adapters import to_anthropic, to_mcp, to_openai
from agentpilot.tools.catalog import BY_NAME, CATALOG
from agentpilot.tools.registry import (
    BROWSER_NAMESPACE,
    DuplicateToolError,
    ToolRegistry,
    browser_tools,
)
from agentpilot.tools.spec import REF_DESCRIPTION, ToolSpec, union_of

__all__ = [
    "BROWSER_NAMESPACE",
    "BY_NAME",
    "CATALOG",
    "REF_DESCRIPTION",
    "DuplicateToolError",
    "ToolRegistry",
    "ToolSpec",
    "browser_tools",
    "to_anthropic",
    "to_mcp",
    "to_openai",
    "union_of",
]
