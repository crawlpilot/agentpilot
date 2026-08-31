"""Named tool profiles -- the answer to a 64-verb schema every request carries.

Behavioural, not a restatement of the constants: the tests assert what a profile
guarantees a caller, so adding a verb to one cannot break them but removing a
guarantee can.
"""

from __future__ import annotations

import pytest

from crawlpilot.tools import PROFILES, UnknownProfileError, browser_tools, profile


def _names(registry) -> set[str]:  # type: ignore[no-untyped-def]
    return {name.split(".", 1)[1] for name in registry.names}


def test_core_is_enough_to_browse_without_reading_the_rest_of_the_catalog() -> None:
    core = _names(profile("core"))
    for verb in ("navigate", "click", "fill", "extract", "scroll", "new_tab"):
        assert verb in core


def test_core_carries_the_dialog_verbs_because_a_confirm_is_not_optional() -> None:
    """A model that cannot answer a `confirm()` is stuck on the page, where one
    without `get_styles` merely has to work harder."""

    core = _names(profile("core"))
    assert {"dialog_status", "dialog_accept", "dialog_dismiss"} <= core


def test_core_is_much_smaller_than_the_whole_catalog() -> None:
    """The entire point: a scrape run should not pay context for `swipe`."""

    assert len(profile("core")) < len(browser_tools().subset(agent_exposed=True)) / 2


def test_profiles_compose() -> None:
    combined = _names(profile("core", "query", "wait"))
    assert _names(profile("core")) <= combined
    assert "get_value" in combined
    assert "wait_for_selector" in combined
    # ...and nothing from a profile that was not asked for.
    assert "swipe" not in combined


def test_a_profile_never_offers_a_verb_an_agent_may_not_call() -> None:
    """`agent_exposed` filtering still applies, so a profile cannot become a
    back door around `agent_fields=None`."""

    everything = _names(profile(*PROFILES))
    for sensitive in ("execute_js", "upload_file", "clipboard_read", "wait_for_function"):
        assert sensitive not in everything


def test_every_profiled_name_is_a_real_verb() -> None:
    """A typo in a profile would silently shrink it rather than fail."""

    catalog = {spec.name for spec in browser_tools()}
    for name, members in PROFILES.items():
        assert members <= catalog, f"{name} names verbs that do not exist"


def test_an_unknown_profile_is_an_error_not_an_empty_set() -> None:
    with pytest.raises(UnknownProfileError, match="unknown tool profile"):
        profile("core", "netwrok")


def test_no_profile_selects_nothing() -> None:
    assert len(profile()) == 0
