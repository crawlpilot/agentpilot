"""Interactive, long-lived browser session lifecycle: open -> execute
(batched, many times) -> release. Extracted from `gateway/routes/sessions.py`
(where this logic used to live entirely inline, coupled to `gateway.wiring
.Wiring`/`Session`) so a caller other than the HTTP layer -- `agentpilot.agent`'s
step loop, which needs many `execute()` round trips against the same open
session over the life of an agent run -- can drive a session too, without
importing `agentpilot.gateway` (forbidden by the layering: `gateway -> agent
-> session -> llm -> spi`).

Takes explicit driver/registry/etc. parameters rather than a `Wiring` object,
same discipline `session/ephemeral.py` already follows and for the same
reason: `Wiring` lives in `agentpilot.gateway`, above this module in the
layering -- `crawlpilot.session` must never import it.

Distinct from `session/ephemeral.py`'s `run_ephemeral_scrape()`: that helper
is architected around exactly one `driver.execute()` call before hard
teardown (mint an identity, execute one batch, evict+close immediately). A
session opened here stays live across many independent `execute_on_session()`
calls, renewing its lease each time, until the caller explicitly releases it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from crawlpilot import metrics
from crawlpilot.config import DEFAULTS, BrowserConfig
from crawlpilot.identity.profile_store import (
    delete_profile_dir,
    resolve_profile_dir,
    seed_profile_dir,
)
from crawlpilot.identity.proxy_pinning import ProxyPinner
from crawlpilot.policy import NullPrototypes, PrototypeProvider
from crawlpilot.session import stealth_profile
from crawlpilot.session.acquire import acquire_validated
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.session.rotation import RotationConfig, RotationPolicy, should_retire
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.driver import BrowserDriver
from crawlpilot.spi.egress import EgressPolicy
from crawlpilot.spi.identity import IdentityRef, ProfileKind
from crawlpilot.spi.lease import ContextRef, LeaseId
from crawlpilot.tiers import TierPolicy

if TYPE_CHECKING:
    # Deferred: `Vault` pulls in `cryptography` (the `driver` extra), which
    # the gateway image deliberately never installs (see
    # `docker/gateway.Dockerfile`) -- `agentpilot.gateway.wiring` imports
    # `InteractiveSession` from this module at module level, so an
    # unconditional `Vault` import here broke gateway's own boot
    # (`ModuleNotFoundError: No module named 'cryptography'`) even though
    # this module only ever uses `vault` as a passed-in instance (`.load()`/
    # `.save()`), never the class itself, at runtime.
    from crawlpilot.identity.vault import Vault

log = structlog.get_logger(__name__)

_NO_PROTOTYPES = NullPrototypes()
"""Seeding is off unless a caller supplies a catalog -- the library ships no
catalog of its own (plan D10)."""


@dataclass
class InteractiveSession:
    session_id: str
    identity: IdentityRef
    ctx: ContextRef
    lease_id: LeaseId
    tier: str
    headful: bool
    block_popups: bool
    enable_cdp: bool


async def open_interactive_session(
    *,
    session_id: str,
    scope: str,
    domain: str,
    name: str,
    tier: str,
    headful: bool,
    block_popups: bool,
    enable_cdp: bool,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    profiles_root: Path,
    proxy_pinner: ProxyPinner | None,
    vault: Vault | None,
    lease_ttl_seconds: float,
    locale: str | None = None,
    timezone_id: str | None = None,
    browser_config: BrowserConfig = DEFAULTS,
    prototype_provider: PrototypeProvider = _NO_PROTOTYPES,
    egress: EgressPolicy | None = None,
) -> InteractiveSession:
    """`kind=ProfileKind.DEFAULT` (not the dataclass's own `TEMPORARY`
    default): an interactive, caller-named identity is kept warm across
    release/reopen and vault-backed -- see `spi.identity.ProfileKind`'s
    docstring. `ephemeral.py`'s one-shot scrape identities are the case that
    wants the actual `TEMPORARY` default instead."""

    # `scope` is an opaque caller-supplied prefix, not a tenant -- see
    # `ephemeral.run_ephemeral_scrape` for the same reasoning. The rendered
    # slug is unchanged from the pre-Phase-3c `IdentityKey(tenant, domain, name)`.
    identity = IdentityRef(key=f"{scope}/{domain}/{name}", kind=ProfileKind.DEFAULT)
    owner = f"{scope}:{name}"

    async def _opener() -> ContextRef:
        profile_dir = resolve_profile_dir(profiles_root, identity)
        # A profile dir existing *before* this call is exactly "warm dir" in
        # Vault's restore-trigger sense -- restore_state() must never run
        # against it. Checked before mkdir(), since mkdir() would otherwise
        # make every open look "already existed".
        is_fresh = not profile_dir.exists()
        profile_dir.mkdir(parents=True, exist_ok=True)
        # Only a genuinely fresh identity is seeded: a returning one already
        # has its own earned cookies, and overwriting them with the
        # prototype's would discard exactly the reputation this is for.
        if is_fresh:
            prototype = prototype_provider.prototype_for(domain)
            if prototype is not None and seed_profile_dir(profile_dir, prototype):
                log.info(
                    "interactive_session.profile_seeded",
                    session_id=session_id,
                    domain=domain,
                    prototype=str(prototype),
                )

        # Protected tiers want a residential exit, same as the scrape path --
        # datacenter IPs are the dominant Akamai edge-block.
        proxy_tier = TierPolicy.for_tier(tier).proxy_tier
        proxy = (
            await proxy_pinner.get_or_assign(identity, tier=proxy_tier) if proxy_pinner else None
        )
        # The tier's stealth kwargs, resolved by the same helper `/v1/scrape`
        # uses. Before this, an interactive session -- and therefore every agent
        # run -- opened with none of them no matter which tier was requested.
        stealth = stealth_profile.resolve(
            identity,
            tier,
            proxy=proxy,
            locale=locale,
            timezone_id=timezone_id,
            # Avoidance yes, reaction no -- see `stealth_profile.resolve`.
            # A session has no escalation ladder to answer a raised
            # `ChallengeDetected` with, and an agent legitimately passes
            # through empty/thin intermediate pages all run long.
            detect_blocks=False,
            config=browser_config,
        )
        ctx = await driver.open(
            identity,
            profile_dir,
            proxy,
            headful,
            egress if egress is not None else EgressPolicy(),
            block_popups,
            enable_cdp,
            **stealth.as_open_kwargs(),
        )

        if is_fresh and vault is not None:
            state = vault.load(identity)
            if state is not None:
                await driver.restore_state(ctx, state)
        return ctx

    # Validate-on-acquire: a reused warm context that died while idle is
    # transparently evicted + reopened rather than failing the first action.
    ctx, lease = await acquire_validated(
        registry=registry,
        driver=driver,
        identity=identity,
        owner=owner,
        ttl_seconds=lease_ttl_seconds,
        opener=_opener,
    )

    return InteractiveSession(
        session_id=session_id,
        identity=identity,
        ctx=ctx,
        lease_id=lease.lease_id,
        tier=tier,
        headful=headful,
        block_popups=block_popups,
        enable_cdp=enable_cdp,
    )


async def execute_on_session(
    session: InteractiveSession,
    actions: list[spi_actions.Action],
    *,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    page_id: str | None = None,
) -> spi_actions.ActionResult:
    """Renews the session's lease before every dispatch -- a `KeyError` here
    means the underlying context was reclaimed (idle-timeout, node loss)
    since the last call; callers should treat that as the session being
    gone, not retry blindly against a dead lease."""

    await registry.renew(session.lease_id)
    return await driver.execute(session.ctx, actions, page_id)


async def release_interactive_session(
    session: InteractiveSession,
    *,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    vault: Vault | None,
    rotation: RotationConfig | None = None,
    profiles_root: Path | None = None,
) -> None:
    """Checkpoints to the vault (best-effort -- a vault write failure must
    not block the release itself), then either releases the lease back to the
    warm IDLE pool (the normal path; `session.Reaper` destroys IDLE contexts
    later) or, when `rotation` is enabled and the context has degraded past
    `rotation.thresholds`, retires it instead so the next `acquire()` opens a
    fresh one. `profiles_root` is required for the FRESH policy (to wipe the
    profile dir)."""

    if vault is not None:
        try:
            state = await driver.export_state(session.ctx)
            vault.save(session.identity, state)
        except Exception:
            log.warning(
                "interactive_session.vault_checkpoint_failed", session_id=session.session_id
            )

    if rotation is not None and rotation.enabled and await _retire_if_degraded(
        session, registry=registry, driver=driver, rotation=rotation, profiles_root=profiles_root
    ):
        return

    await registry.release(session.lease_id)


async def _retire_if_degraded(
    session: InteractiveSession,
    *,
    registry: RegistryProtocol,
    driver: BrowserDriver,
    rotation: RotationConfig,
    profiles_root: Path | None,
) -> bool:
    """Retire + rotate the context if its health warrants it. Returns True
    when the context was retired (so the caller skips the ordinary
    release-to-IDLE). Best-effort throughout: a failure to read health or to
    tear down must fall back to a normal release, never leak an exception up."""

    try:
        health = await driver.context_health(session.ctx)
    except Exception:
        return False
    if health is None or not should_retire(health, rotation.thresholds):
        return False

    # Evict the registry entry (the next acquire opens fresh) and destroy the
    # underlying browser context.
    evicted = await registry.evict(session.identity)
    if evicted is not None:
        with contextlib.suppress(Exception):
            await driver.close(evicted)

    # FRESH additionally wipes the profile dir so the reopened context is a
    # clean browser identity to the site (RESTART keeps cookies/logins).
    if rotation.policy is RotationPolicy.FRESH and profiles_root is not None:
        with contextlib.suppress(Exception):
            delete_profile_dir(profiles_root, session.identity)

    metrics.incr("context_rotations_total", policy=rotation.policy.value)
    log.info(
        "interactive_session.retired",
        session_id=session.session_id,
        policy=rotation.policy.value,
        leak_warnings=health.leak_warnings,
        failure_rate=round(health.failure_rate, 2),
    )
    return True
