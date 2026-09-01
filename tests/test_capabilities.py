"""`GET /v1/capabilities`, and selecting extensions by name.

Three distributions share one wire contract now, and the contract is *derived*
from crawlpilot's catalog rather than written out -- so client and server can
disagree the moment their crawlpilot versions differ, and a `{code, error}`
envelope has no way to say so. This endpoint is the one place that answers it.

It is also what makes selection-by-name usable: an `Extension` is code and
cannot cross a network, so a caller picks among what the worker already has
installed, and needs a way to see those names.

Unit-level against the route function, per this repo's convention (see
`test_sessions_list.py`).
"""

from __future__ import annotations

import dataclasses

import pytest

from agentpilot.gateway.routes.capabilities import capabilities
from crawlpilot.extensions import ExtensionManifest, ExtensionRegistry, compare_api_versions
from crawlpilot.extensions.manifest import Compatibility
from crawlpilot.tools import ToolSpec
from crawlpilot.wire import WIRE_API_VERSION


@dataclasses.dataclass
class SolveWallAction:
    reason: str = ""


class _WalmartExtension:
    manifest = ExtensionManifest(
        name="walmart", version="1.2", description="Walmart wall handling."
    )

    def tools(self):  # type: ignore[no-untyped-def]
        return [
            ToolSpec(
                name="solve_wall",
                description="Clear a known wall.",
                action_cls=SolveWallAction,
                wire_fields=("reason",),
                agent_fields=("reason",),
                domains=("*.walmart.com",),
            )
        ]


class _FakeWiring:
    role = "worker"

    def __init__(self, *, enabled: list[str] | None = None) -> None:
        self.extensions = ExtensionRegistry([_WalmartExtension()], enabled=enabled)


# ---------------------------------------------------------------- the endpoint


async def test_it_reports_the_wire_version_a_client_must_agree_on() -> None:
    body = await capabilities(_FakeWiring())  # type: ignore[arg-type]
    assert body["wire_api"] == WIRE_API_VERSION
    assert body["crawlpilot_version"]


async def test_it_lists_verbs_from_the_live_registry_not_the_catalog() -> None:
    """The distinction that makes the endpoint worth having: an extension's verb
    is exactly the part a client cannot know from its own crawlpilot version."""

    body = await capabilities(_FakeWiring())  # type: ignore[arg-type]
    names = {tool["name"] for tool in body["tools"]}

    assert "walmart.solve_wall" in names
    assert "browser.click" in names


async def test_a_site_restricted_verb_advertises_its_domains() -> None:
    body = await capabilities(_FakeWiring())  # type: ignore[arg-type]
    solve = next(t for t in body["tools"] if t["name"] == "walmart.solve_wall")
    assert solve["domains"] == ["*.walmart.com"]

    # ...and an unrestricted verb stays quiet rather than carrying `null`.
    click = next(t for t in body["tools"] if t["name"] == "browser.click")
    assert "domains" not in click


async def test_it_names_the_loaded_extensions_so_they_can_be_selected() -> None:
    body = await capabilities(_FakeWiring())  # type: ignore[arg-type]
    assert body["extensions"] == [
        {
            "name": "walmart",
            "version": "1.2",
            "api_version": ExtensionManifest.api_version,
            "description": "Walmart wall handling.",
        }
    ]


async def test_deselecting_an_extension_removes_its_verbs_too() -> None:
    body = await capabilities(_FakeWiring(enabled=[]))  # type: ignore[arg-type]
    assert body["extensions"] == []
    assert not [t for t in body["tools"] if t["namespace"] == "walmart"]


# -------------------------------------------------------------- the allowlist


def test_none_means_the_deployments_own_set() -> None:
    """The default has to be inert: a caller that never heard of this gets
    exactly what it got before."""

    assert ExtensionRegistry([_WalmartExtension()]).loaded == ("walmart",)


def test_an_empty_list_means_none_which_disabled_cannot_express() -> None:
    """`disabled` names exceptions to "all"; there is no way to spell "nothing"
    with it. Seeing a page exactly as served -- unrepaired by any extension --
    is a real thing to want."""

    assert ExtensionRegistry([_WalmartExtension()], enabled=[]).loaded == ()


def test_an_operators_disable_still_beats_a_callers_request() -> None:
    registry = ExtensionRegistry(
        [_WalmartExtension()], enabled=["walmart"], disabled=["walmart"]
    )
    assert registry.loaded == ()


# ------------------------------------------------- the version handshake policy


@pytest.mark.parametrize(
    "theirs, ours, expected",
    [
        ("1.0", "1.0", Compatibility.LOAD),
        ("1.4", "1.0", Compatibility.LOAD),  # minor differences are compatible
        ("1.0", "2.0", Compatibility.LOAD_WITH_WARNING),  # older peer: fine
        ("2.0", "1.0", Compatibility.REFUSE),  # newer peer: refuse
    ],
)
def test_the_wire_handshake_reuses_the_extension_compatibility_policy(
    theirs: str, ours: str, expected: Compatibility
) -> None:
    """Not a second policy. The asymmetry the extension loader already had --
    older-than-me loads with a warning, newer-than-me is refused rather than
    failing deep inside a call later -- is exactly right for a client talking to
    a server, so it is stated once and both callers use it.
    """

    assert compare_api_versions(theirs, ours, subject="server").compatibility is expected


def test_a_refusal_names_both_versions() -> None:
    """The whole point of refusing early is a message someone can act on."""

    verdict = compare_api_versions("2.0", "1.0", subject="server at https://gw")
    assert "2.0" in verdict.reason
    assert "1.0" in verdict.reason
    assert "server at https://gw" in verdict.reason
