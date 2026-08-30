"""robots.txt fetch + parse -- via the egress-guarded client, never
`RobotFileParser.read()` (which does its own raw `urllib` fetch, bypassing
`agentpilot.egress`'s SSRF/metadata guard entirely -- a robots.txt URL is
attacker-influenceable the same way any crawl-seed URL is, so it gets the
same guard as every other discovery fetch in this package).
"""

from __future__ import annotations

from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

from agentpilot.egress.httpx_guard import guarded_get
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import EgressBlocked


async def fetch(origin: str, policy: EgressPolicy) -> RobotFileParser | None:
    """`origin` is a scheme+host (e.g. `https://example.com`), not a full
    page URL. Returns `None` on any fetch failure or non-2xx/3xx status --
    treated as "no robots.txt restrictions" (matching real crawlers' and
    Firecrawl's own default behavior), not a hard error that would abort
    discovery over a robots.txt that's merely missing or briefly down."""

    url = urljoin(origin, "/robots.txt")
    try:
        resp = await guarded_get(url, policy, timeout=10.0, follow_redirects=True)
    except (EgressBlocked, httpx.HTTPError):
        return None
    if resp.status_code >= 400:
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser


def is_allowed(parser: RobotFileParser | None, url: str, user_agent: str = "*") -> bool:
    """`parser is None` (no robots.txt, or it failed to fetch) means
    "unrestricted", matching `fetch()`'s own fail-open contract."""

    if parser is None:
        return True
    return parser.can_fetch(user_agent, url)


def crawl_delay(parser: RobotFileParser | None, user_agent: str = "*") -> float | None:
    if parser is None:
        return None
    delay = parser.crawl_delay(user_agent)
    return float(delay) if delay is not None else None
