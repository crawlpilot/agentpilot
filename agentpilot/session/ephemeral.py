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

import json
import time
import uuid
from pathlib import Path

import structlog

from agentpilot.identity.fingerprint import generate as generate_fingerprint
from agentpilot.identity.profile_store import delete_profile_dir, resolve_profile_dir
from agentpilot.identity.proxy_pinning import ProxyPinner
from agentpilot.llm import schema_extract
from agentpilot.llm.client import LLMConfig
from agentpilot.session.acquire import acquire_validated
from agentpilot.session.registry import RegistryProtocol
from agentpilot.session.warm_pool import WarmPool
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.actions import ExtractFormat
from agentpilot.spi.driver import BrowserDriver
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey, ProfileKind
from agentpilot.spi.lease import ContextRef
from agentpilot.spi.scrape import Document, DocumentMetadata, ScrapeOptions

log = structlog.get_logger(__name__)

_PROTECTED_TIERS = frozenset({"stealth", "enhanced"})
"""Tiers that opt into the ported stealth path: human warm-up after each
navigation plus body-level block detection (raises `ChallengeDetected` on a bot
wall). `basic`/`auto` keep the cheap path -- warm-up adds seconds per scrape and
the body fetch adds a round trip, neither worth it until a caller asks for the
stealth tier. Wiring `auto` to *escalate* into this path on a block signal is
the Stage 4 follow-up."""


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


def _build_batch(url: str, options: ScrapeOptions) -> list[spi_actions.Action]:
    batch: list[spi_actions.Action] = [
        spi_actions.NavigateAction(url=url, timeout_ms=options.timeout_ms)
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
    if warm:
        assert session_name is not None  # narrows for the type checker
        identity = IdentityKey(
            tenant=tenant, domain=domain, name=session_name, kind=ProfileKind.DEFAULT
        )
    else:
        identity = IdentityKey(tenant=tenant, domain=domain, name=f"scrape-{uuid.uuid4().hex}")
    owner = f"{tenant}:scrape"

    async def _opener() -> ContextRef:
        if warm:
            # Sticky proxy for a reused identity, same as an interactive
            # session -- the same profile should keep the same egress IP so
            # cookies/fingerprint/IP stay coherent across visits.
            proxy = await proxy_pinner.get_or_assign(identity) if proxy_pinner else None
        else:
            # pick_ephemeral(), not get_or_assign(): a throwaway identity is
            # opened exactly once, so there is nothing to keep "sticky" for.
            proxy = proxy_pinner.pick_ephemeral(identity) if proxy_pinner else None
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
        protected = tier in _PROTECTED_TIERS
        eff_locale, eff_timezone = locale, timezone_id
        user_agent: str | None = None
        init_script: str | None = None
        if protected:
            # Pin one coherent fingerprint to this identity for life. `region`
            # is None until Stage 1 (residential proxy) can supply the exit-IP
            # country; today the family is a stable function of the identity
            # slug. The fingerprint's own geo fills any locale/timezone the
            # caller didn't pin, so UA/language/timezone stay mutually
            # consistent -- but an explicit request value still wins.
            fp = generate_fingerprint(identity.slug())
            user_agent = fp.user_agent
            init_script = fp.init_script()
            eff_locale = locale or fp.geo.locale
            eff_timezone = timezone_id or fp.geo.timezone_id
        return await driver.open(
            identity,
            profile_dir,
            proxy,
            headful=False,
            egress=EgressPolicy(),
            block_popups=True,
            enable_cdp=False,
            locale=eff_locale,
            timezone_id=eff_timezone,
            warmup=protected,
            detect_blocks=protected,
            user_agent=user_agent,
            init_script=init_script,
        )
        # No vault load/restore: cookie persistence for the warm case comes
        # from the on-disk profile dir surviving teardown (below), not from
        # the vault -- ephemeral.py has no vault handle by design.

    batch = _build_batch(url, options)

    started = time.monotonic()
    # Validate-on-acquire: a reused warm/IDLE context that died while idle is
    # transparently reopened rather than failing this scrape.
    ctx, _lease = await acquire_validated(
        registry=registry,
        driver=driver,
        identity=identity,
        owner=owner,
        ttl_seconds=lease_ttl_seconds,
        opener=_opener,
    )
    try:
        result = await driver.execute(ctx, batch)
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
            status_code=None,
            tier_used=tier,
            node_id=ctx.node_id,
            duration_ms=duration_ms,
            source_url=url,
        ),
        error=error,
        extract=extract_result,
        extract_error=extract_error,
    )
    return document, screenshot_bytes
