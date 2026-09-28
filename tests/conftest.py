"""Suite-wide fixtures for the platform tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _no_egress_geo_lookup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the suite off the network.

    `BrowserConfig.from_env()` -- which `gateway.wiring` calls -- turns on the
    egress-geo lookup (`crawlpilot.egress.geo`) by default, so any test that
    builds the real wiring and opens a protected session would otherwise ask
    ipinfo where this machine is. A test that wants a geo sets
    `CRAWLPILOT_EGRESS_COUNTRY` / `_TIMEZONE`, which never touch the network.
    """

    from crawlpilot.egress import geo

    monkeypatch.setenv("CRAWLPILOT_EGRESS_GEO_URL", "off")
    geo.reset_cache()
    yield
    geo.reset_cache()
