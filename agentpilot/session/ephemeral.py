"""Runs one ephemeral, one-shot browser fetch: mint a temporary identity,
`registry.acquire()` -> `driver.execute()` a batch -> `registry.evict()` +
`driver.close()` + delete the profile dir immediately (never `release()`,
which would park the context in the warm IDLE pool -- pointless for an
identity that's never reused, and a disk-fill risk at scale).

Shared by `gateway.routes.scrape` (the synchronous `/v1/scrape` endpoint)
and `agentpilot.jobs`'s crawl-worker loop (P4, folded into the existing
`worker` role rather than a separate one -- see `jobs/worker_loop.py`), so
both compose the exact same Navigate -> [actions] -> Extract(s) ->
Screenshot? sequence and the exact same ephemeral-teardown discipline,
rather than drifting into two slightly different "run a one-shot scrape"
implementations. Takes explicit driver/registry/etc. parameters rather than
a `Wiring` object: `Wiring` lives in `agentpilot.gateway`, which is *above*
this module in the layering (`gateway -> session -> identity -> ... -> spi`)
-- `agentpilot.session` must never import it.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import structlog

from agentpilot.identity.profile_store import delete_profile_dir, resolve_profile_dir
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef
from agentpilot.spi.scrape import Document, DocumentMetadata, ScrapeOptions

log = structlog.get_logger(__name__)


def _build_batch(url: str, options: ScrapeOptions) -> list[spi_actions.Action]:
    batch: list[spi_actions.Action] = [
        spi_actions.NavigateAction(url=url, timeout_ms=options.timeout_ms)
    ]
    if options.wait_for_ms:
        batch.append(spi_actions.WaitAction(ms=options.wait_for_ms))
    batch.extend(options.actions)
    batch.extend(
        spi_actions.ExtractAction(format=fmt, main_content=options.only_main_content)
        for fmt in options.formats
    )
    if options.screenshot:
        batch.append(spi_actions.ScreenshotAction(full_page=options.full_page_screenshot))
    return batch


async def run_ephemeral_scrape(
    *,
    tenant: str,
    domain: str,
    url: str,
    options: ScrapeOptions,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    profiles_root: Path,
    proxy_pinner: ProxyPinner | None,
    lease_ttl_seconds: float,
    tier: str = "auto",
) -> tuple[Document, bytes | None]:
    """Returns `(document, screenshot_png_bytes)` -- the raw screenshot
    bytes are handed back separately rather than folded into `Document`
    (whose `screenshot_artifact_id` field expects a reference into the
    artifact store, not inline bytes; see that field's docstring) so each
    caller decides for itself: `/v1/scrape` base64-encodes them straight
    into its response, while the crawl-worker loop currently has nowhere to
    put them (no artifact store exists yet) and just discards them."""

    identity = IdentityKey(tenant=tenant, domain=domain, name=f"scrape-{uuid.uuid4().hex}")
    owner = f"{tenant}:scrape"

    async def _opener() -> ContextRef:
        profile_dir = resolve_profile_dir(profiles_root, identity)
        profile_dir.mkdir(parents=True, exist_ok=True)
        # pick_ephemeral(), not get_or_assign(): this identity is opened
        # exactly once, so there is nothing to keep "sticky" for -- see
        # that method's docstring.
        proxy = proxy_pinner.pick_ephemeral(identity) if proxy_pinner else None
        return await driver.open(
            identity,
            profile_dir,
            proxy,
            headful=False,
            egress=EgressPolicy(),
            block_popups=True,
            enable_cdp=False,
        )
        # No vault load/restore: identity.is_temporary is always true for a
        # freshly minted scrape identity, and a one-shot identity never had
        # a vault entry to restore in the first place.

    batch = _build_batch(url, options)

    started = time.monotonic()
    ctx, _lease = await registry.acquire(identity, owner, lease_ttl_seconds, _opener)
    try:
        result = await driver.execute(ctx, batch)
    finally:
        try:
            await registry.evict(identity)
            await driver.close(ctx)
        except Exception:
            log.warning("ephemeral_scrape.teardown_failed", url=url)
        finally:
            delete_profile_dir(profiles_root, identity)

    duration_ms = (time.monotonic() - started) * 1000

    # Non-strict: a pre-extract action that unexpectedly navigates aborts
    # the rest of the batch (`result.sequence_aborted`), which can leave
    # `result.extracts` shorter than `options.formats` -- surfaced as a
    # descriptive `error` below rather than a raised exception.
    extracted = dict(zip(options.formats, result.extracts, strict=False))
    screenshot_bytes = result.screenshots[0] if result.screenshots else None
    error = None
    if result.sequence_aborted:
        error = "page navigated away during a pre-extract action; some formats may be missing"

    document = Document(
        document_id=str(uuid.uuid4()),
        url=url,
        markdown=extracted.get("markdown"),
        text=extracted.get("text"),
        html=extracted.get("html"),
        links=(),
        screenshot_artifact_id=None,
        metadata=DocumentMetadata(
            title=None,
            status_code=None,
            tier_used=tier,
            node_id=ctx.node_id,
            duration_ms=duration_ms,
            source_url=url,
        ),
        error=error,
    )
    return document, screenshot_bytes
