"""Every `requests_total.labels(...)` call site must name the right labels.

`prometheus_client` validates label names at *call* time, not at import, so a
wrong one is invisible until a request hits that line and dies with
`ValueError: Incorrect label names` -- which is exactly how a save endpoint
shipped broken. A grep-and-check is unglamorous and catches all of them at
once, including the next route somebody adds.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agentpilot.observability.metrics import requests_total

ROUTES = (
    pathlib.Path(__file__).resolve().parents[1]
    / "packages/agentpilot/agentpilot/gateway/routes"
)
EXPECTED = set(requests_total._labelnames)


def _label_calls(path: pathlib.Path) -> list[tuple[int, set[str]]]:
    """Keyword names of every `requests_total.labels(...)` call in a file."""

    tree = ast.parse(path.read_text())
    out: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "labels":
            continue
        target = func.value
        if not isinstance(target, ast.Name) or target.id != "requests_total":
            continue
        out.append((node.lineno, {kw.arg for kw in node.keywords if kw.arg}))
    return out


@pytest.mark.parametrize("path", sorted(ROUTES.glob("*.py")), ids=lambda p: p.name)
def test_requests_total_call_sites_use_the_declared_labels(path: pathlib.Path) -> None:
    for lineno, names in _label_calls(path):
        assert names == EXPECTED, (
            f"{path.name}:{lineno} labels {sorted(names)}; "
            f"requests_total declares {sorted(EXPECTED)}"
        )


def test_the_counter_still_declares_tenant_and_route() -> None:
    # If this changes deliberately, the call sites above move with it.
    assert EXPECTED == {"tenant", "route"}
