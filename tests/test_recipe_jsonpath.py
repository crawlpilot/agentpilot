"""Pure unit tests for `agentpilot.recipe.jsonpath` -- dict/list traversal
only, no I/O."""

from __future__ import annotations

from agentpilot.recipe.jsonpath import resolve_path


def test_empty_path_returns_the_whole_document() -> None:
    data = {"a": 1}
    assert resolve_path(data, "") == data


def test_dotted_key_access_into_a_dict() -> None:
    data = {"offers": {"price": 42}}
    assert resolve_path(data, "offers.price") == 42


def test_bracket_index_access_into_a_list() -> None:
    data = [{"name": "first"}, {"name": "second"}]
    assert resolve_path(data, "[1].name") == "second"


def test_bare_numeric_segment_indexes_a_list() -> None:
    data = [{"name": "first"}, {"name": "second"}]
    assert resolve_path(data, "0.name") == "first"


def test_missing_key_returns_none() -> None:
    assert resolve_path({"a": 1}, "b") is None


def test_out_of_range_index_returns_none() -> None:
    assert resolve_path([1, 2], "[5]") is None


def test_non_numeric_segment_against_a_list_returns_none() -> None:
    assert resolve_path([1, 2], "name") is None


def test_traversal_through_a_non_container_returns_none() -> None:
    assert resolve_path({"a": 1}, "a.b") is None
