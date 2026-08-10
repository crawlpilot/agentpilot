"""The UI-driven stealth mapping: `tier` -> fusion `no_runtime`. Pure."""

from __future__ import annotations

import pytest

from agentpilot.spi.actions import stealth_from_tier


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("basic", True),
        ("stealth", True),
        ("enhanced", False),
        ("auto", False),
        ("unknown", False),
    ],
)
def test_stealth_from_tier(tier: str, expected: bool) -> None:
    assert stealth_from_tier(tier) is expected
