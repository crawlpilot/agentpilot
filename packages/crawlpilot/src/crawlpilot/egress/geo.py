"""Where this worker's traffic actually exits, for sessions with no proxy.

A protected session's timezone has to agree with the IP the site sees. With a
proxy, the proxy declares its exit country. Without one, nothing did: the
fingerprint preset was picked by hashing the identity slug, so an identity was
New York, London or Kolkata by coin-toss. MEASURED on a Jio IP in Karnataka:
CreepJS reported `British Summer Time · Europe/London` and `en-GB` -- a clock
four and a half hours off the IP's own, which is one of the first things a bot
sensor compares.

The fix is to ask. Once per process, cached, and never fatal: a lookup that
fails leaves the geo unknown, which means "don't override the browser's own
timezone" -- the same as before, not worse.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import structlog

from crawlpilot.config import EgressConfig

log = structlog.get_logger(__name__)

_LOOKUP_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class EgressGeo:
    country: str | None
    """ISO-3166 alpha-2, upper case."""

    timezone: str | None
    """IANA name, as the lookup reports it."""


_cached: EgressGeo | None = None
_resolved = False


def _static(config: EgressConfig) -> EgressGeo | None:
    if config.country or config.timezone:
        return EgressGeo(
            country=config.country.upper() if config.country else None,
            timezone=config.timezone,
        )
    return None


async def _lookup(url: str) -> EgressGeo | None:
    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT_S) as client:
            payload = (await client.get(url)).json()
    except Exception as exc:  # noqa: BLE001 -- never fatal, see module docstring
        log.warning("egress_geo.lookup_failed", url=url, error=str(exc))
        return None
    country = payload.get("country") or payload.get("country_code")
    timezone = payload.get("timezone")
    if isinstance(timezone, dict):  # some providers nest it: {"id": "Asia/Kolkata"}
        timezone = timezone.get("id")
    if not (isinstance(country, str) or isinstance(timezone, str)):
        log.warning("egress_geo.lookup_unparseable", url=url)
        return None
    geo = EgressGeo(
        country=country.upper() if isinstance(country, str) else None,
        timezone=timezone if isinstance(timezone, str) else None,
    )
    log.info("egress_geo.resolved", country=geo.country, timezone=geo.timezone)
    return geo


async def resolve(config: EgressConfig) -> EgressGeo | None:
    """This process's egress geo, or `None` when it is unknown.

    Static config wins and never touches the network. Otherwise the lookup runs
    at most once per process -- a failed one included, so a worker with no
    route to the lookup does not pay its timeout on every session open.

    No lock: two sessions opening at the same instant on a cold worker may both
    look up, which costs one redundant request. A module-level `asyncio.Lock`
    would bind to whichever event loop first contended for it, and this library
    runs on more than one (the sync client's loop thread, each test's loop).
    """

    static = _static(config)
    if static is not None:
        return static
    if not config.geo_lookup_url:
        return None

    global _cached, _resolved
    if _resolved:
        return _cached
    _cached = await _lookup(config.geo_lookup_url)
    _resolved = True
    return _cached


def reset_cache() -> None:
    """Forget the cached lookup (tests, or after a known network change)."""

    global _cached, _resolved
    _cached = None
    _resolved = False
