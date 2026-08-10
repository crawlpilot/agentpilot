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

from agentpilot.identity.burn_tracker import BurnTracker
from agentpilot.identity.fingerprint import generate as generate_fingerprint
from agentpilot.identity.profile_store import delete_profile_dir, resolve_profile_dir
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.llm import schema_extract
from agentpilot.llm.client import LLMConfig
from agentpilot.session.acquire import acquire_validated
from agentpilot.session.http_fetch import fetch_via_http
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

log = structlog.get_logger(__name__)

_PROTECTED_TIERS = frozenset({"stealth", "enhanced"})
"""Tiers that opt into the ported stealth path: human warm-up after each
navigation plus body-level block detection (raises `ChallengeDetected` on a bot
wall). `basic` keeps the cheap path; `auto` starts on the ladder below and
escalates into the protected path on a block signal."""

_ESCALATION: dict[str, tuple[str, ...]] = {
    # `auto` climbs the ladder on `ChallengeDetected` (Firecrawl's start-cheap-
    # escalate-on-failure semantics). Each retry mints a fresh throwaway
    # identity, so it also gets a new proxy pick and a new pinned fingerprint --
    # the PRIVACY-scope rotation ported from `BrowserResponseHandlerImpl.kt`.
    # An explicitly requested tier does *not* auto-escalate: the caller chose it.
    # `basic` fetches over plain HTTP first (no browser) and, only if that hits a
    # hard wall, escalates to the real `stealth` browser -- the cheap-first path.
    "auto": ("stealth", "enhanced"),
    "basic": ("basic", "stealth"),
    "stealth": ("stealth",),
    "enhanced": ("enhanced",),
}

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


def _build_batch(
    url: str, options: ScrapeOptions, *, referer: str | None = None
) -> list[spi_actions.Action]:
    # One navigation, straight to the requested URL -- Pulsar's `visit()` shape
    # (navigate -> settle on body -> in-place human warm-up), never a
    # homepage-first double navigation. The `referer` (protected tiers) makes the
    # single hit look like an organic-search landing rather than a cold
    # scripted deep-link; the driver's per-navigate warm-up + block detection do
    # the rest on the target page itself.
    batch: list[spi_actions.Action] = [
        spi_actions.NavigateAction(url=url, timeout_ms=options.timeout_ms, referer=referer)
    ]
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
        proxy_tier = "residential" if attempt_tier in _PROTECTED_TIERS else None
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
            if warm_pool is not None:
                pooled_ctx = await warm_pool.take(proxy)
                if pooled_ctx is not None:
                    pooled_ctx.identity = identity  # re-label the adopted ref
                    return pooled_ctx
        profile_dir = resolve_profile_dir(profiles_root, identity)
        profile_dir.mkdir(parents=True, exist_ok=True)
        protected = attempt_tier in _PROTECTED_TIERS
        eff_locale, eff_timezone = locale, timezone_id
        user_agent: str | None = None
        init_script: str | None = None
        extra_http_headers: dict[str, str] | None = None
        extra_launch_args: list[str] | None = None
        if protected:
            # Pin one coherent fingerprint to this identity for life. When the
            # resolved proxy declares an exit-IP country, seed the fingerprint
            # from it so the pinned timezone/locale match the egress geo;
            # otherwise the family is a stable function of the identity slug.
            # The fingerprint's own geo fills any locale/timezone the caller
            # didn't pin -- but an explicit request value still wins.
            region = proxy.country if proxy else None
            fp = generate_fingerprint(identity.slug(), region=region)
            user_agent = fp.user_agent
            init_script = fp.init_script()
            # Pin the Client-Hint headers to the same Chrome build as the UA, so
            # Sec-CH-UA / navigator.userAgentData / UA all agree (the trio Akamai
            # cross-checks). Without this the header leaked the real, newer Chrome.
            extra_http_headers = fp.client_hint_headers()
            extra_launch_args = fp.launch_args()
            eff_locale = locale or fp.geo.locale
            eff_timezone = timezone_id or fp.geo.timezone_id
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
            locale=eff_locale,
            timezone_id=eff_timezone,
            warmup=protected,
            detect_blocks=protected,
            user_agent=user_agent,
            init_script=init_script,
            extra_http_headers=extra_http_headers,
            extra_launch_args=extra_launch_args,
            # Protected tiers interact on the slow, most-human STEALTH timing
            # table (click/fill/type/gap); other tiers keep the default cadence.
            interact_profile="stealth" if protected else None,
            block_resource_types=block_resource_types,
            block_hosts=block_hosts,
        )
        # No vault load/restore: cookie persistence for the warm case comes
        # from the on-disk profile dir surviving teardown (below), not from
        # the vault -- ephemeral.py has no vault handle by design.

    # Escalation ladder (resolved up front so it also drives batch shape). A
    # warm identity is reused, so escalation across fresh identities would defeat
    # it -- warm takes a single attempt at the ladder's first tier.
    ladder = _ESCALATION.get(tier, ("stealth",))
    attempts = ladder[:1] if warm else ladder

    # A search-engine referer on the (single) navigation is a protected-path
    # anti-detection lever only; `basic` keeps a bare, refererless hit.
    protected = any(t in _PROTECTED_TIERS for t in attempts)
    referer = _search_engine_referer(url) if protected else None
    batch = _build_batch(url, options, referer=referer)

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

    fetcher = http_fetcher or fetch_via_http

    async def _http_attempt(identity: IdentityKey) -> ActionResult:
        # The `basic` rung: a plain HTTP GET, no browser. Coherent UA + Client
        # Hints from a pinned fingerprint (httpx's default UA is an instant
        # block); proxy resolved the same way the browser path would. Raises
        # `ChallengeDetected` on a hard wall so the loop escalates to `stealth`.
        proxy = None
        if proxy_pinner is not None:
            proxy = await (
                proxy_pinner.get_or_assign(identity, tier=None)
                if warm
                else proxy_pinner.pick_ephemeral(identity, tier=None)
            )
        fp = generate_fingerprint(identity.slug(), region=proxy.country if proxy else None)
        headers = {
            "User-Agent": fp.user_agent,
            "Accept-Language": ", ".join(fp.geo.languages),
            **fp.client_hint_headers(),
        }
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
            return await driver.execute(ctx, batch), ctx.node_id
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
        success_proxy_tier = "residential" if used_tier in _PROTECTED_TIERS else None
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
    if options.extract is not None:
        if not internal_markdown:
            extract_error = "no markdown content available for structured extraction"
        else:
            try:
                config = LLMConfig.from_env()
                extract_result = await schema_extract.extract_structured(
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
    )
    return document, screenshot_bytes
