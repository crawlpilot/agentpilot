"""`GET /v1/capabilities` -- what this deployment can actually do.

Three distributions now share one wire contract (`crawlpilot`, `agentpilot`, and
a client), and the contract is *derived* from crawlpilot's catalog rather than
written out. So client and server can disagree the moment their crawlpilot
versions differ, and nothing in a `{success, code, error}` envelope would say
so: a verb the server has never heard of comes back as a bare rejection, and a
verb the *client* has never heard of is simply unusable with no explanation.

This is the one endpoint that answers that, once, at the start of a session --
what the wire version is, which verbs exist here, and which extensions are
loaded. A client applies `compare_api_versions` to the first field and refuses
loudly on a major mismatch, rather than failing deep inside a call later.

Unauthenticated and mounted on every role, like `/healthz`: it exposes no tenant
data, and a client needs it *before* it can be sure its credential will be
understood. It is also what makes `POST /v1/sessions {"extensions": [...]}`
usable -- there is no point selecting extensions by name without a way to see
which names exist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from agentpilot.gateway.wiring import Wiring, get_wiring
from crawlpilot import __version__ as crawlpilot_version
from crawlpilot.tools import ToolSpec
from crawlpilot.wire import WIRE_API_VERSION

router = APIRouter(tags=["capabilities"])


def _tool(name: str, spec: ToolSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "namespace": name.split(".", 1)[0],
        "description": spec.description,
        "safety": spec.safety,
    }
    # Only present when the verb is restricted to particular sites, so the
    # common case stays quiet rather than carrying an explicit `null`.
    if spec.domains:
        entry["domains"] = list(spec.domains)
    return entry


@router.get("")
async def capabilities(wiring: Wiring = Depends(get_wiring)) -> dict[str, Any]:
    """Read off the live registry, not off `CATALOG`.

    That distinction is the point: `tools` here includes whatever `ToolMount`
    extensions this deployment loaded, which is precisely the part a client
    cannot know from its own crawlpilot version.
    """

    registry = wiring.extensions.tools
    return {
        "wire_api": WIRE_API_VERSION,
        "crawlpilot_version": crawlpilot_version,
        "role": wiring.role,
        "tools": [_tool(name, registry[name]) for name in sorted(registry.names)],
        "extensions": [
            {
                "name": manifest.name,
                "version": manifest.version,
                "api_version": manifest.api_version,
                "description": manifest.description,
            }
            for manifest in wiring.extensions.manifests
        ],
    }
