"""Unit tests for prototype-profile cloning (`identity.profile_store`) and the
homepage-first warm-up batch shape (`session.ephemeral`). Pure filesystem and
pure list-building -- no browser.

Both surveys of PulsarRPA/Browser4 independently ranked prototype cloning the
highest-value portable technique: it is what stops every anonymous scrape being
a cookieless first-visit browser, which `ephemeral.py`'s own docstring already
named as a bot signal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentpilot.identity.profile_store import (
    PROTOTYPE_ENV,
    prototype_dir_for,
    seed_profile_dir,
)
from agentpilot.session.ephemeral import _build_batch, _site_root
from agentpilot.spi.actions import NavigateAction
from agentpilot.spi.scrape import ScrapeOptions


@pytest.fixture(autouse=True)
def _no_ambient_prototype(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PROTOTYPE_ENV, raising=False)


# --- prototype resolution ---------------------------------------------


def test_no_prototype_configured_is_a_no_op() -> None:
    assert prototype_dir_for("www.zara.com") is None


def test_per_site_prototype_wins_over_default(tmp_path: Path) -> None:
    """The cookies that matter (`_abck`, `datadome`) are origin-scoped, so a
    profile warmed on one retailer carries nothing useful for another."""

    (tmp_path / "www.zara.com").mkdir()
    (tmp_path / "default").mkdir()

    assert prototype_dir_for("www.zara.com", root=tmp_path) == tmp_path / "www.zara.com"


def test_falls_back_to_default(tmp_path: Path) -> None:
    (tmp_path / "default").mkdir()

    assert prototype_dir_for("www.hm.com", root=tmp_path) == tmp_path / "default"


def test_missing_root_resolves_to_nothing(tmp_path: Path) -> None:
    assert prototype_dir_for("www.zara.com", root=tmp_path / "absent") is None


def test_env_var_drives_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "default").mkdir()
    monkeypatch.setenv(PROTOTYPE_ENV, str(tmp_path))

    assert prototype_dir_for("anything.test") == tmp_path / "default"


def test_blank_env_var_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """docker-compose's `VAR: ${VAR:-}` defines the variable as an empty
    string, which must not be read as a path."""

    monkeypatch.setenv(PROTOTYPE_ENV, "   ")

    assert prototype_dir_for("www.zara.com") is None


# --- seeding ----------------------------------------------------------


def test_seed_copies_the_prototype(tmp_path: Path) -> None:
    prototype = tmp_path / "proto"
    (prototype / "Default").mkdir(parents=True)
    (prototype / "Default" / "Cookies").write_bytes(b"cookie-jar")
    (prototype / "Local State").write_text("{}")
    target = tmp_path / "target"

    assert seed_profile_dir(target, prototype) is True
    assert (target / "Default" / "Cookies").read_bytes() == b"cookie-jar"
    assert (target / "Local State").read_text() == "{}"


def test_seed_skips_singleton_locks(tmp_path: Path) -> None:
    """Chrome refuses to reuse a profile another process holds, and those
    entries are per-run state rather than the history we want to clone."""

    prototype = tmp_path / "proto"
    prototype.mkdir()
    (prototype / "SingletonLock").write_text("pid")
    (prototype / "Local State").write_text("{}")
    target = tmp_path / "target"

    seed_profile_dir(target, prototype)

    assert (target / "Local State").exists()
    assert not (target / "SingletonLock").exists()


def test_seed_never_overwrites_a_profile_that_has_content(tmp_path: Path) -> None:
    """Re-seeding a live profile would discard the cookies it has since
    earned, which is the opposite of the point."""

    prototype = tmp_path / "proto"
    prototype.mkdir()
    (prototype / "Local State").write_text("from-prototype")
    target = tmp_path / "target"
    target.mkdir()
    (target / "Local State").write_text("earned")

    assert seed_profile_dir(target, prototype) is False
    assert (target / "Local State").read_text() == "earned"


def test_seed_into_an_empty_existing_dir_works(tmp_path: Path) -> None:
    """The callers `mkdir(exist_ok=True)` before seeding, so the target
    already exists and is empty by the time we get here."""

    prototype = tmp_path / "proto"
    prototype.mkdir()
    (prototype / "Local State").write_text("{}")
    target = tmp_path / "target"
    target.mkdir()

    assert seed_profile_dir(target, prototype) is True
    assert (target / "Local State").exists()


def test_seed_failure_is_not_fatal(tmp_path: Path) -> None:
    """A cold profile is a worse scrape, not a failed one."""

    assert seed_profile_dir(tmp_path / "target", tmp_path / "does-not-exist") is False


# --- homepage-first warm-up -------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.zara.com/uk/en/dress-p123.html", "https://www.zara.com/"),
        ("https://www.zara.com/uk/", "https://www.zara.com/"),
        # Already the root -- nothing to warm up on.
        ("https://www.zara.com/", None),
        ("https://www.zara.com", None),
        # No host.
        ("/relative/path", None),
        ("not-a-url", None),
    ],
)
def test_site_root(url: str, expected: str | None) -> None:
    assert _site_root(url) == expected


def _navigations(batch) -> list[NavigateAction]:
    return [a for a in batch if isinstance(a, NavigateAction)]


def test_warm_up_root_prepends_a_root_navigation() -> None:
    """The canonical Akamai pattern (Pulsar's `warnUpBrowser`): the sensor runs
    on the root, `_abck` matures there, and the deep link is then requested by
    a browser that already holds the cookie."""

    batch = _build_batch(
        "https://www.zara.com/uk/en/dress-p123.html",
        ScrapeOptions(formats=("markdown",)),
        referer="https://www.google.com/",
        warm_up_root=True,
    )
    navs = _navigations(batch)

    assert [n.url for n in navs] == [
        "https://www.zara.com/",
        "https://www.zara.com/uk/en/dress-p123.html",
    ]
    # The deep link is now same-site, so its referer is the page we came from.
    assert navs[0].referer == "https://www.google.com/"
    assert navs[1].referer == "https://www.zara.com/"


def test_warm_up_root_is_off_by_default() -> None:
    batch = _build_batch(
        "https://www.zara.com/uk/en/dress-p123.html", ScrapeOptions(formats=("markdown",))
    )

    assert len(_navigations(batch)) == 1


def test_warm_up_root_does_not_double_navigate_the_root_itself() -> None:
    batch = _build_batch(
        "https://www.zara.com/", ScrapeOptions(formats=("markdown",)), warm_up_root=True
    )

    assert len(_navigations(batch)) == 1


def test_extract_actions_are_unaffected_by_the_extra_navigation() -> None:
    """`run_ephemeral_scrape` zips `_effective_formats` against
    `result.extracts`; an extra navigation must not perturb that pairing."""

    from agentpilot.spi.actions import ExtractAction

    options = ScrapeOptions(formats=("markdown", "html"))
    plain = _build_batch("https://x.test/a", options)
    warmed = _build_batch("https://x.test/a", options, warm_up_root=True)

    assert [a.format for a in plain if isinstance(a, ExtractAction)] == ["markdown", "html"]
    assert [a.format for a in warmed if isinstance(a, ExtractAction)] == ["markdown", "html"]
