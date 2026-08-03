"""Site-discovery: robots.txt, sitemap, and HTML-link-based URL discovery,
plus the include/exclude/depth/domain filtering and normalization that turn
raw discovered URLs into the candidate set `/v1/crawl` seeds its queue with
and `/v1/map` returns directly.

A pure, driver-free leaf module -- same tier as `agentpilot.extraction`, but
consumed by `agentpilot.gateway` (`routes/map.py`, synchronous) and
`agentpilot.jobs` (the crawl-worker's frontier expansion, P4) rather than by
the driver, since discovery runs over plain guarded HTTP
(`agentpilot.egress.httpx_guard.guarded_get`), never a rendered browser page.
Deliberately stdlib-only (`urllib.robotparser`, `xml.etree.ElementTree`,
`html.parser`) rather than `lxml`/`trafilatura`: those live in the `driver`
extras group (see `pyproject.toml`), and this module must stay importable
in a Chrome-free `gateway` process, which mounts `/v1/map` directly.
"""

from __future__ import annotations
