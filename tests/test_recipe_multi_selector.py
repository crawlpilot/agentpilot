"""Multi-selector coalesce (`evaluate.evaluate_field_locators`) and replay's
normalization/quality-gate glue (`replay._read_field`) -- both exercised as
pure units (locator evaluation monkeypatched; no live driver)."""

from __future__ import annotations

from typing import Any

import agentpilot.recipe.evaluate as evaluate_mod
from agentpilot.recipe.evaluate import evaluate_field_locators
from agentpilot.recipe.models import FieldLocator
from agentpilot.recipe.replay import _read_field
from agentpilot.recipe.schema import FieldNormalization


# --- coalesce: first non-empty candidate wins ---


async def test_evaluate_field_locators_returns_first_non_empty(monkeypatch) -> None:
    resolved = {"#a": None, "#b": "  ", "#c": "hit", "#d": "later"}

    async def fake_eval(locator: FieldLocator, **kw: Any) -> Any:
        return resolved[locator.selector]

    monkeypatch.setattr(evaluate_mod, "evaluate_field_locator", fake_eval)
    candidates = [FieldLocator(source="css", selector=s) for s in ("#a", "#b", "#c", "#d")]
    value = await evaluate_field_locators(
        candidates, structured_data=None, session=None, registry=None, driver=None
    )
    assert value == "hit"


async def test_evaluate_field_locators_none_when_all_empty(monkeypatch) -> None:
    async def fake_eval(locator: FieldLocator, **kw: Any) -> Any:
        return None

    monkeypatch.setattr(evaluate_mod, "evaluate_field_locator", fake_eval)
    candidates = [FieldLocator(source="css", selector="#a")]
    value = await evaluate_field_locators(
        candidates, structured_data=None, session=None, registry=None, driver=None
    )
    assert value is None


# --- replay._read_field: normalization + emit_raw + required gate ---


def _cands() -> list[FieldLocator]:
    return [FieldLocator(source="css", selector="#p")]


def test_read_field_normalizes_and_emits_raw() -> None:
    out: dict[str, Any] = {}
    spec = FieldNormalization(value_type="price", emit_raw=True)
    reason = _read_field("price", _cands(), "$1,299.00", {"price": spec}, "https://x.test/", out)
    assert reason is None
    assert out["price"] == 1299.0
    assert out["price_raw"] == "$1,299.00"


def test_read_field_required_empty_is_a_failure() -> None:
    out: dict[str, Any] = {}
    spec = FieldNormalization(required=True)
    reason = _read_field("title", _cands(), None, {"title": spec}, "https://x.test/", out)
    assert reason is not None
    assert "title" not in out


def test_read_field_missing_optional_records_none_without_raw() -> None:
    out: dict[str, Any] = {}
    reason = _read_field("desc", _cands(), None, {"desc": FieldNormalization()}, "https://x.test/", out)
    assert reason is not None  # optional but unresolved -> reported as a soft miss
    assert out == {}
