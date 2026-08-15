"""`agentpilot.recipe.normalize.normalize_value` -- the declarative cleanup /
coercion layer (port of Pulsar's Strings/RegexExtractor + str_* composition)."""

from __future__ import annotations

from agentpilot.recipe.normalize import normalize_value
from agentpilot.recipe.schema import FieldNormalization


def _n(**kw: object) -> FieldNormalization:
    return FieldNormalization(**kw)  # type: ignore[arg-type]


# --- type coercion ---


def test_price_extracts_first_float_stripping_currency_and_separators() -> None:
    value, ok = normalize_value("$1,299.00", _n(value_type="price"))
    assert value == 1299.0
    assert ok is True


def test_number_handles_leading_label_and_percent() -> None:
    assert normalize_value("Rating: 4.5 out of 5", _n(value_type="number"))[0] == 4.5


def test_integer_truncates_float_text() -> None:
    assert normalize_value("1,024 reviews", _n(value_type="integer"))[0] == 1024


def test_boolean_maps_known_tokens() -> None:
    assert normalize_value("In Stock", _n(value_type="boolean"))[0] is True
    assert normalize_value("out of stock", _n(value_type="boolean"))[0] is False


def test_unrecognized_boolean_falls_back_to_default() -> None:
    assert normalize_value("maybe", _n(value_type="boolean", default=False))[0] is False


def test_url_absolutizes_against_base() -> None:
    value, _ = normalize_value("/p/42", _n(value_type="url"), base_url="https://x.test/cat/")
    assert value == "https://x.test/p/42"


def test_date_parses_common_layout_to_iso() -> None:
    assert normalize_value("Jan 5, 2026", _n(value_type="date"))[0] == "2026-01-05"


def test_datetime_parses_iso_with_zulu() -> None:
    value, _ = normalize_value("2026-01-05T10:30:00Z", _n(value_type="datetime"))
    assert value.startswith("2026-01-05T10:30:00")


# --- regex / replace / text cleanup (order + composition) ---


def test_regex_extract_group_then_coerce() -> None:
    spec = _n(value_type="integer", regex=r"(\d+)\s*g", regex_group=1)
    assert normalize_value("Net weight 500 g", spec)[0] == 500


def test_replace_rules_apply_in_order() -> None:
    spec = _n(replace=((" ", " "), ("SALE ", "")))
    assert normalize_value("SALE Widget Pro", spec)[0] == "Widget Pro"


def test_collapse_ws_and_trim_and_case_and_accents() -> None:
    spec = _n(collapse_ws=True, case="lower", strip_accents=True)
    assert normalize_value("  Café   Crème  ", spec)[0] == "cafe creme"


def test_control_chars_are_stripped() -> None:
    assert normalize_value("a\x00b\x1fc", _n())[0] == "abc"


# --- defaults & required quality-gate ---


def test_none_raw_uses_default() -> None:
    assert normalize_value(None, _n(default="n/a"))[0] == "n/a"


def test_required_empty_fails_the_gate() -> None:
    value, ok = normalize_value("   ", _n(required=True))
    assert ok is False


def test_required_with_value_passes() -> None:
    value, ok = normalize_value("present", _n(required=True))
    assert value == "present"
    assert ok is True


def test_regex_no_match_falls_back_to_default_not_crash() -> None:
    spec = _n(regex=r"(\d+)", regex_group=1, default="none")
    assert normalize_value("no digits here", spec)[0] == "none"


def test_invalid_user_regex_is_swallowed() -> None:
    # An unbalanced group should not raise -- falls back to default.
    spec = _n(regex=r"(unclosed", default="safe")
    assert normalize_value("anything", spec)[0] == "safe"
