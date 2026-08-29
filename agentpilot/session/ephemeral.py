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

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import structlog

from agentpilot.config import DEFAULTS, BrowserConfig
from agentpilot.identity.burn_tracker import BurnTracker
from agentpilot.identity.fingerprint import generate as generate_fingerprint
from agentpilot.identity.profile_store import (
    delete_profile_dir,
    resolve_profile_dir,
    seed_profile_dir,
)
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.llm import schema_extract
from agentpilot.llm.client import LLMConfig
from agentpilot.policy import NullPrototypes, PrototypeProvider
from agentpilot.session import browser_headers, stealth_profile
from agentpilot.session.acquire import acquire_validated
from agentpilot.session.registry import RegistryProtocol
from agentpilot.session.warm_pool import WarmPool
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.actions import ActionResult, ExtractFormat
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import ChallengeDetected
from agentpilot.spi.identity import IdentityKey, ProfileKind
from agentpilot.spi.lease import ContextRef
from agentpilot.spi.scrape import Document, DocumentMetadata, ScrapeOptions
from agentpilot.tiers import TierPolicy

log = structlog.get_logger(__name__)

_NO_PROTOTYPES = NullPrototypes()
"""Seeding is off unless a caller supplies a catalog -- the library ships no
catalog of its own (plan D10)."""

# The `basic` HTTP fast-path fetcher seam (dependency-injected for tests). Must
# match `http_fetch.fetch_via_http`'s keyword signature.
HttpFetcher = Callable[..., Awaitable[ActionResult]]

_DEFAULT_CRAWL_RETRY_MAX = 2
"""How many times a *soft* (CRAWL-scope) verdict -- thin/rate-limited/wrong-geo,
which returned content rather than raising -- is retried on the same tier before
the page is accepted as-is. Pulsar re-queues CRAWL retries in the scheduler; a
synchronous scrape instead retries in place a bounded number of times."""

_DEFAULT_RETRY_DELAY_S = 5.0
"""Base backoff between soft retries -- Walmart's cadence (`WalmartCrawler
.retryDelayPolicy`: ~10 s for the first couple of retries). Injectable (tests
pass 0.0) so the unit suite doesn't actually sleep."""


def _retry_delay_s(attempt: int, verdict: str | None, base: float) -> float:
    """Backoff before a soft-verdict retry: linear in the attempt number, with a
    longer wait for `rate_limited` (a fresh identity/IP needs time to matter)."""
    if base <= 0:
        return 0.0
    factor = 2.0 if verdict == "rate_limited" else 1.0
    return base * factor * attempt


def _effective_formats(options: ScrapeOptions) -> tuple[ExtractFormat, ...]:
    """`options.formats` plus an internal `"markdown"` request when
    `options.extract` needs input the caller didn't otherwise ask for --
    mirrors Firecrawl's "json format requires markdown" derivation
    (`deriveMarkdownFromHTML`). The internal markdown never leaks into
    `Document.markdown` unless the caller actually requested it -- see
    `run_ephemeral_scrape`."""

    if options.extract is not None and "markdown" not in options.formats:
        return (*options.formats, "markdown")
    return options.formats


def _search_engine_referer(url: str) -> str | None:
    """A plausible referrer for a *cold, cookieless* first hit: an organic
    search landing. A deep product URL reached straight from Google (no prior
    same-site navigation, no cookies) is the single most common legitimate path
    a real user takes to a product page, and Chrome derives a coherent
    `Sec-Fetch-Site: cross-site` from it -- unlike a bare no-referrer hit, which
    reads as a scripted deep-link. Pulsar's referrer handling (CommonRPA
    .waitForReferrer) achieves this by actually visiting the referrer; we get
    the same behavioural signal in a *single* navigation by setting the header.

    Only for URLs with a host; `None` (no referer) for relative/malformed URLs.
    """

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return "https://www.google.com/"


def _site_root(url: str) -> str | None:
    """The site root to warm up on before a deep link, or `None` when the
    request *is* the root (nothing to warm) or the URL has no host."""

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    if parts.path in ("", "/") and not parts.query:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def _build_batch(
    url: str,
    options: ScrapeOptions,
    *,
    referer: str | None = None,
    warm_up_root: bool = False,
) -> list[spi_actions.Action]:
    # `warm_up_root` prepends a navigation to the site root. This is the
    # canonical Akamai pattern and what Pulsar's `warnUpBrowser` does
    # (`WalmartCrawler.kt`: `visit("https://www.walmart.com/")` before any
    # product URL): the sensor script runs on the root, the per-navigate
    # warm-up feeds it pointer/scroll telemetry, and `_abck` gets a chance to
    # validate on a page that is *expected* to be entered cold -- so the deep
    # link is then requested by a browser that already holds a matured cookie,
    # from an origin it has already visited, rather than as a cold deep-link
    # with a bare Google referer and nothing else.
    #
    # Only for protected tiers: it costs a full extra navigation plus its
    # warm-up, which is the wrong trade on a site that isn't scoring you.
    batch: list[spi_actions.Action] = []
    if warm_up_root:
        root = _site_root(url)
        if root is not None:
            batch.append(
                spi_actions.NavigateAction(
                    url=root, timeout_ms=options.timeout_ms, referer=referer
                )
            )
            # The deep link is now same-site, so the referer that fits is the
            # page we just came from -- not the search engine.
            referer = root
    batch.append(
        spi_actions.NavigateAction(url=url, timeout_ms=options.timeout_ms, referer=referer)
    )
    if options.wait_for_ms:
        batch.append(spi_actions.WaitAction(ms=options.wait_for_ms))
    batch.extend(options.actions)
    batch.extend(
        spi_actions.ExtractAction(
            format=fmt,
            main_content=options.only_main_content,
            include_tags=options.include_tags,
            exclude_tags=options.exclude_tags,
        )
        for fmt in _effective_formats(options)
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
    session_name: str | None = None,
    locale: str | None = None,
    timezone_id: str | None = None,
    warm_pool: WarmPool | None = None,
    burn_tracker: BurnTracker | None = None,
    crawl_retry_max: int = _DEFAULT_CRAWL_RETRY_MAX,
    retry_delay_base_s: float = _DEFAULT_RETRY_DELAY_S,
    http_fetcher: HttpFetcher | None = None,
    browser_config: BrowserConfig = DEFAULTS,
    prototype_provider: PrototypeProvider = _NO_PROTOTYPES,
) -> tuple[Document, bytes | None]:
    """Returns `(document, screenshot_png_bytes)` -- the raw screenshot
    bytes are handed back separately rather than folded into `Document`
    (whose `screenshot_artifact_id` field expects a reference into the
    artifact store, not inline bytes; see that field's docstring) so each
    caller decides for itself: `/v1/scrape` base64-encodes them straight
    into its response, while the crawl-worker loop currently has nowhere to
    put them (no artifact store exists yet) and just discards them.

    `session_name` is the anti-detection lever: absent (the default), each
    call mints a throwaway `scrape-{uuid}` identity whose profile dir is
    deleted on teardown -- a cookie-less, first-visit browser every time,
    which is itself a bot signal to WAFs like Akamai. When a caller passes a
    stable `session_name`, the scrape instead reuses a *warm, persistent*
    identity `(tenant, domain, session_name)` whose profile dir (cookies,
    Chrome's own state) survives across calls -- so repeat scrapes of the
    same site look like a returning visitor. `locale`/`timezone_id` flow
    straight through to `driver.open()` for locale/timezone consistency."""

    warm = session_name is not None
    owner = f"{tenant}:scrape"

    def _make_identity() -> IdentityKey:
        # A fresh throwaway identity per attempt (new proxy pick + fingerprint);
        # a warm identity is fixed by session_name and reused across visits.
        if warm:
            assert session_name is not None  # narrows for the type checker
            return IdentityKey(
                tenant=tenant, domain=domain, name=session_name, kind=ProfileKind.DEFAULT
            )
        return IdentityKey(tenant=tenant, domain=domain, name=f"scrape-{uuid.uuid4().hex}")

    async def _opener(identity: IdentityKey, attempt_tier: str) -> ContextRef:
        # Protected rungs want a residential exit (datacenter IPs are the
        # dominant Akamai edge-block); the proxy config resolves that per-tenant
        # with a fallback to whatever pool is configured.
        proxy_tier = TierPolicy.for_tier(attempt_tier).proxy_tier
        if warm:
            # Sticky proxy for a reused identity, same as an interactive
            # session -- the same profile should keep the same egress IP so
            # cookies/fingerprint/IP stay coherent across visits.
            proxy = (
                await proxy_pinner.get_or_assign(identity, tier=proxy_tier)
                if proxy_pinner
                else None
            )
        else:
            # pick_ephemeral(), not get_or_assign(): a throwaway identity is
            # opened exactly once, so there is nothing to keep "sticky" for.
            proxy = (
                await proxy_pinner.pick_ephemeral(identity, tier=proxy_tier)
                if proxy_pinner
                else None
            )
            # Anticipatory warm pool: a throwaway identity has no persistent
            # profile, so it can adopt a context pre-launched for its proxy
            # tier and skip the cold Chrome launch entirely. On a miss, fall
            # through to a cold open below.
            #
            # Never on a protected tier. A pooled context is launched *before*
            # the adopting identity exists, so it cannot carry that identity's
            # pinned fingerprint, warm-up, block detection or interact profile
            # -- `warm_pool._open` passes none of them. Adopting one would
            # silently downgrade a stealth run to a bare browser, which is the
            # exact failure `stealth_profile` exists to prevent. The cold
            # launch is the cost of a coherent identity.
            if warm_pool is not None and not TierPolicy.for_tier(attempt_tier).protected:
                pooled_ctx = await warm_pool.take(proxy)
                if pooled_ctx is not None:
                    pooled_ctx.identity = identity  # re-label the adopted ref
                    return pooled_ctx
        profile_dir = resolve_profile_dir(profiles_root, identity)
        profile_dir.mkdir(parents=True, exist_ok=True)
        # Seed from a hand-warmed prototype when one is configured, so this
        # throwaway identity is born with a plausible history instead of being
        # the cookieless first-visit browser this module's docstring flags as
        # a bot signal. No-op when unset.
        prototype = prototype_provider.prototype_for(domain)
        if prototype is not None and seed_profile_dir(profile_dir, prototype):
            log.info("ephemeral.profile_seeded", domain=domain, prototype=str(prototype))
        # Shared with `session/interactive.py` so a tier means the same thing on
        # /v1/scrape, /v1/sessions and agent runs -- see that module's docstring.
        stealth = stealth_profile.resolve(
            identity,
            attempt_tier,
            proxy=proxy,
            locale=locale,
            timezone_id=timezone_id,
            config=browser_config,
        )
        # `enhanced` is the top rung: request headful (the driver runs it under
        # Xvfb on the worker, or degrades to headless where no display exists),
        # since headless is itself a detection vector on hardened targets.
        headful = attempt_tier == "enhanced"
        return await driver.open(
            identity,
            profile_dir,
            proxy,
            headful=headful,
            egress=EgressPolicy(),
            block_popups=True,
            enable_cdp=False,
            block_resource_types=block_resource_types,
            block_hosts=block_hosts,
            **stealth.as_open_kwargs(),
        )
        # No vault load/restore: cookie persistence for the warm case comes
        # from the on-disk profile dir surviving teardown (below), not from
        # the vault -- ephemeral.py has no vault handle by design.

    # Escalation ladder (resolved up front so it also drives batch shape). A
    # warm identity is reused, so escalation across fresh identities would defeat
    # it -- warm takes a single attempt at the ladder's first tier.
    ladder = TierPolicy.for_tier(tier).escalation
    attempts = ladder[:1] if warm else ladder

    # A search-engine referer on the (single) navigation is a protected-path
    # anti-detection lever only; `basic` keeps a bare, refererless hit.
    protected = any(TierPolicy.for_tier(t).protected for t in attempts)
    referer = _search_engine_referer(url) if protected else None
    def _batch_for(attempt_tier: str) -> list[spi_actions.Action]:
        # Homepage-first is an *escalation* behaviour, not a default one. It
        # costs a whole extra navigation plus its warm-up -- measured at roughly
        # 3x the latency of a direct hit -- which is wasted on a site that was
        # going to serve us anyway. `enhanced` is the rung that already means
        # "spend whatever it takes": `auto` climbs to it after a block, and an
        # explicit `enhanced` asks for it outright. A warm identity is excluded
        # regardless: it already holds the site's cookies from its last visit,
        # so the root navigation buys it nothing.
        return _build_batch(
            url,
            options,
            referer=referer,
            warm_up_root=attempt_tier == "enhanced" and not warm,
        )

    # Protected scrapes pin a residential exit; a warm identity being retired
    # rotates within that same tier (see `_retire_warm`).
    warm_proxy_tier = "residential" if protected else None

    # Resource blocking (default off). `block_images` expands to the safe media
    # set; scripts/xhr/fetch/document are never blocked (JS content + Akamai
    # sensor need them).
    _block_types = set(options.block_resource_types)
    if options.block_images:
        _block_types |= {"image", "media", "font"}
    block_resource_types = tuple(sorted(_block_types)) or None
    block_hosts = tuple(options.block_hosts) or None

    async def _http_attempt(identity: IdentityKey) -> ActionResult:
        # The `basic` rung: a plain HTTP GET, no browser. Coherent UA + Client
        # Hints from a pinned fingerprint (httpx's default UA is an instant
        # block); proxy resolved the same way the browser path would. Raises
        # `ChallengeDetected` on a hard wall so the loop escalates to `stealth`.
        #
        # `fetch_via_http` is imported lazily (not at module top) on purpose: it
        # pulls in `agentpilot.extraction` -> `lxml`, which only the worker image
        # bundles. The gateway imports this module (via its scrape route) but has
        # no lxml and never runs a scrape, so a top-level import would crash it.
        fetcher = http_fetcher
        if fetcher is None:
            from agentpilot.session.http_fetch import fetch_via_http

            fetcher = fetch_via_http
        proxy = None
        if proxy_pinner is not None:
            proxy = await (
                proxy_pinner.get_or_assign(identity, tier=None)
                if warm
                else proxy_pinner.pick_ephemeral(identity, tier=None)
            )
        fp = generate_fingerprint(identity.slug(), region=proxy.country if proxy else None)
        # The full Chrome navigation header set, in Chrome's order. This path
        # used to send five headers and nothing else -- no Accept, no
        # Sec-Fetch-*, no Upgrade-Insecure-Requests, no Referer -- which is a
        # one-line rule at any WAF edge, long before TLS is even looked at.
        headers = browser_headers.navigation_headers(
            user_agent=fp.user_agent,
            accept_language=", ".join(fp.geo.languages),
            client_hints=fp.client_hint_headers(),
            referer=_search_engine_referer(url),
        )
        return await fetcher(
            url=url,
            formats=_effective_formats(options),
            options=options,
            headers=headers,
            proxy=proxy,
            timeout_ms=options.timeout_ms,
        )

    async def _attempt(identity: IdentityKey, attempt_tier: str) -> tuple[ActionResult, str]:
        # One acquire -> execute -> teardown. Raises `ChallengeDetected` (from
        # the driver's post-navigate body check) straight through the finally,
        # so the caller can escalate. Returns `(result, node_id)`.
        if attempt_tier == "basic":
            # HTTP fast-path -- no browser context to acquire or tear down.
            return await _http_attempt(identity), "http"
        ctx, _lease = await acquire_validated(
            registry=registry,
            driver=driver,
            identity=identity,
            owner=owner,
            ttl_seconds=lease_ttl_seconds,
            opener=lambda: _opener(identity, attempt_tier),
        )
        try:
            return await driver.execute(ctx, _batch_for(attempt_tier)), ctx.node_id
        finally:
            try:
                await registry.evict(identity)
                await driver.close(ctx)
            except Exception:
                log.warning("ephemeral_scrape.teardown_failed", url=url)
            finally:
                # The warm case's whole point is that the profile dir (cookies,
                # Chrome state) survives for the next scrape of this identity --
                # only the throwaway case deletes it.
                if not warm:
                    delete_profile_dir(profiles_root, identity)

    async def _retire_warm(identity: IdentityKey) -> None:
        # A burned warm identity is started fresh: drop its cookies/profile,
        # clear its warning counter, AND rotate its pinned egress IP so the next
        # open is a clean first visit from a *different* exit -- Pulsar's PRIVACY
        # reset rotates fingerprint + proxy together, not the profile alone.
        delete_profile_dir(profiles_root, identity)
        if burn_tracker is not None:
            await burn_tracker.reset(identity)
        if proxy_pinner is not None:
            await proxy_pinner.rotate(identity, tier=warm_proxy_tier)

    # Burn accounting applies only to warm identities (a throwaway is one-shot
    # and deleted on teardown regardless). Retire a warm identity that is
    # already burned *before* reusing it, so this scrape doesn't carry a
    # known-bad profile into the request.
    if warm and burn_tracker is not None:
        warm_identity = _make_identity()
        if await burn_tracker.is_burned(warm_identity):
            log.info("ephemeral_scrape.retiring_burned_identity", identity=warm_identity.slug())
            await _retire_warm(warm_identity)

    started = time.monotonic()
    result: ActionResult | None = None
    used_tier = tier
    used_node_id = "unknown"
    last_challenge: ChallengeDetected | None = None
    used_identity: IdentityKey | None = None
    # Two nested loops mirror Pulsar's retry-scope split: the outer loop climbs
    # the tier ladder on a hard PRIVACY wall (fresh identity => new proxy +
    # fingerprint); the inner loop retries the *same* tier on a soft CRAWL-scope
    # verdict (thin/rate-limited page that still rendered) before accepting it.
    for attempt_tier in attempts:
        crawl_tries = 0
        while True:
            attempt_identity = _make_identity()
            try:
                attempt_result, node_id = await _attempt(attempt_identity, attempt_tier)
            except ChallengeDetected as exc:
                last_challenge = exc
                log.info(
                    "ephemeral_scrape.challenge_escalate",
                    url=url,
                    tier=attempt_tier,
                    scope=exc.scope,
                    detail=str(exc),
                )
                break  # hard wall -> next tier with a fresh identity
            if attempt_result.soft_verdict and crawl_tries < crawl_retry_max:
                crawl_tries += 1
                # A warm identity's soft failures accrue minor warnings (5 => 1
                # real warning), so a run of thin pages eventually retires it.
                if warm and burn_tracker is not None:
                    await burn_tracker.record_minor_block(attempt_identity)
                log.info(
                    "ephemeral_scrape.soft_retry",
                    url=url,
                    tier=attempt_tier,
                    verdict=attempt_result.soft_verdict,
                    attempt=crawl_tries,
                )
                await asyncio.sleep(
                    _retry_delay_s(crawl_tries, attempt_result.soft_verdict, retry_delay_base_s)
                )
                continue  # same tier, same-scope retry
            # Accept: a clean page, or a soft verdict whose retries are spent
            # (return the content best-effort rather than fail on a non-hard tell).
            result, used_node_id = attempt_result, node_id
            used_tier = attempt_tier
            used_identity = attempt_identity
            break
        if result is not None:
            break
    if result is None:
        # Every tier on the ladder hit a wall. Charge the warm identity's burn
        # counter and retire it if it crossed the threshold, then surface the
        # wall (the gateway maps `ChallengeDetected` to 422 CHALLENGE_UNRESOLVED).
        assert last_challenge is not None
        if warm and burn_tracker is not None:
            identity = _make_identity()
            await burn_tracker.record_block(identity, last_challenge.weight)
            if await burn_tracker.is_burned(identity):
                await _retire_warm(identity)
        raise last_challenge

    # Success self-heals a warm identity's warning counter (Pulsar's
    # `markSuccess()` decrement).
    if warm and burn_tracker is not None:
        await burn_tracker.record_success(_make_identity())

    # Count the served page toward the proxy's retirement cap (recomputing the
    # winning attempt's proxy -- the pick is deterministic per identity+tier, so
    # this is the exact endpoint the successful open used).
    if proxy_pinner is not None and used_identity is not None:
        success_proxy_tier = TierPolicy.for_tier(used_tier).proxy_tier
        used_proxy = (
            await proxy_pinner.get_or_assign(used_identity, tier=success_proxy_tier)
            if warm
            else await proxy_pinner.pick_ephemeral(used_identity, tier=success_proxy_tier)
        )
        await proxy_pinner.record_success(used_proxy)

    duration_ms = (time.monotonic() - started) * 1000

    # Non-strict: a pre-extract action that unexpectedly navigates aborts
    # the rest of the batch (`result.sequence_aborted`), which can leave
    # `result.extracts` shorter than `options.formats` -- surfaced as a
    # descriptive `error` below rather than a raised exception.
    extracted = dict(zip(_effective_formats(options), result.extracts, strict=False))
    screenshot_bytes = result.screenshots[0] if result.screenshots else None
    error = None
    if result.sequence_aborted:
        error = "page navigated away during a pre-extract action; some formats may be missing"

    structured_data_raw = extracted.get("structured_data")
    internal_markdown = extracted.get("markdown")
    # `internal_markdown` may exist only to feed `options.extract` below --
    # don't leak it into the response unless the caller actually asked for
    # the `"markdown"` format themselves.
    document_markdown = internal_markdown if "markdown" in options.formats else None

    extract_result = None
    extract_error = None
    extract_warning = None
    if options.extract is not None:
        if not internal_markdown:
            extract_error = "no markdown content available for structured extraction"
        else:
            try:
                config = LLMConfig.from_env()
                extract_result, extract_warning = await schema_extract.extract_structured(
                    internal_markdown,
                    json_schema=options.extract.json_schema,
                    prompt=options.extract.prompt,
                    config=config,
                )
            except Exception as exc:
                extract_error = str(exc)

    document = Document(
        document_id=str(uuid.uuid4()),
        url=url,
        markdown=document_markdown,
        text=extracted.get("text"),
        html=extracted.get("html"),
        structured_data=json.loads(structured_data_raw) if structured_data_raw else None,
        links=(),
        screenshot_artifact_id=None,
        metadata=DocumentMetadata(
            title=result.page_title,
            status_code=result.status_code,
            tier_used=used_tier,
            node_id=used_node_id,
            duration_ms=duration_ms,
            source_url=url,
        ),
        error=error,
        extract=extract_result,
        extract_error=extract_error,
        extract_warning=extract_warning,
    )
    return document, screenshot_bytes
