"""`run_ephemeral_scrape`'s warm-vs-throwaway profile behavior (anti-detection
A1) -- a real in-memory `Registry` + a fake driver that records what `open()`
receives, no real browser. Asserts a stable `session_name` reuses one
persistent profile dir across calls (cookie/return-visitor signal), while
the default throwaway path mints a fresh dir per call and deletes it."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from agentpilot.session.ephemeral import run_ephemeral_scrape
from agentpilot.session.registry import Registry
from agentpilot.spi.actions import Action, ActionResult
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.errors import ChallengeDetected
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState
from agentpilot.spi.proxy import ProxyEndpoint
from agentpilot.spi.scrape import ScrapeOptions


class _FakeProxyPinner:
    """Returns one fixed endpoint for both sticky and ephemeral picks --
    enough to assert the scrape threads the proxy's country into the pinned
    fingerprint's geo."""

    def __init__(self, proxy: ProxyEndpoint) -> None:
        self.proxy = proxy
        self.tiers_requested: list[str | None] = []
        self.successes: list[ProxyEndpoint] = []
        self.rotated: list[tuple[IdentityKey, str | None]] = []
        self.released: list[IdentityKey] = []

    async def get_or_assign(self, identity: IdentityKey, tier: str | None = None) -> ProxyEndpoint:
        self.tiers_requested.append(tier)
        return self.proxy

    async def pick_ephemeral(self, identity: IdentityKey, tier: str | None = None) -> ProxyEndpoint:
        self.tiers_requested.append(tier)
        return self.proxy

    async def record_success(self, proxy: ProxyEndpoint) -> None:
        self.successes.append(proxy)

    async def rotate(
        self, identity: IdentityKey, tier: str | None = None
    ) -> ProxyEndpoint | None:
        self.rotated.append((identity, tier))
        return self.proxy

    async def release(self, identity: IdentityKey) -> None:
        self.released.append(identity)


class _FakeDriver:
    """Records every `open()` call and returns a live-looking `ContextRef`;
    `execute()` returns a minimal markdown result, `close()` is a no-op."""

    def __init__(self) -> None:
        self.opens: list[dict[str, object]] = []

    async def open(
        self,
        identity: IdentityKey,
        profile_dir: Path,
        proxy: object,
        headful: bool,
        egress: EgressPolicy,
        block_popups: bool = False,
        enable_cdp: bool = False,
        locale: str | None = None,
        timezone_id: str | None = None,
        warmup: bool = False,
        detect_blocks: bool = False,
        user_agent: str | None = None,
        init_script: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        extra_launch_args: list[str] | None = None,
        interact_profile: str | None = None,
        block_resource_types: tuple[str, ...] | None = None,
        block_hosts: tuple[str, ...] | None = None,
    ) -> ContextRef:
        self.opens.append(
            {
                "identity": identity,
                "profile_dir": profile_dir,
                "headful": headful,
                "locale": locale,
                "timezone_id": timezone_id,
                "warmup": warmup,
                "detect_blocks": detect_blocks,
                "user_agent": user_agent,
                "init_script": init_script,
                "extra_http_headers": extra_http_headers,
                "extra_launch_args": extra_launch_args,
                "interact_profile": interact_profile,
                "block_resource_types": block_resource_types,
                "block_hosts": block_hosts,
            }
        )
        return ContextRef(
            context_id=str(uuid.uuid4()), identity=identity, state=ContextState.ACTIVE, pid=None
        )

    async def execute(
        self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
    ) -> ActionResult:
        return ActionResult(extracts=["# hello"], page_title="Hello")

    async def close(self, ctx: ContextRef) -> None:
        return None

    async def is_alive(self, ctx: ContextRef) -> bool:
        # Validate-on-acquire pings the acquired context; a freshly opened one
        # in these tests is always live.
        return True


async def _scrape(driver: _FakeDriver, profiles_root: Path, **kwargs: object) -> None:
    await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=profiles_root,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_session_name_reuses_one_persistent_profile_dir(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="shopper-1")
    await _scrape(driver, tmp_path, session_name="shopper-1")

    dir1 = driver.opens[0]["profile_dir"]
    dir2 = driver.opens[1]["profile_dir"]
    assert dir1 == dir2  # same identity -> same profile dir both calls
    assert isinstance(dir1, Path)
    assert dir1.exists()  # NOT deleted on teardown -- the whole point


async def test_default_throwaway_mints_a_fresh_dir_per_call_and_deletes_it(
    tmp_path: Path,
) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path)
    await _scrape(driver, tmp_path)

    dir1 = driver.opens[0]["profile_dir"]
    dir2 = driver.opens[1]["profile_dir"]
    assert dir1 != dir2  # fresh scrape-{uuid} identity each call
    assert isinstance(dir1, Path) and isinstance(dir2, Path)
    assert not dir1.exists()  # deleted on teardown
    assert not dir2.exists()


async def test_locale_and_timezone_flow_through_to_driver_open(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(
        driver, tmp_path, session_name="s", locale="en-US", timezone_id="America/New_York"
    )
    assert driver.opens[0]["locale"] == "en-US"
    assert driver.opens[0]["timezone_id"] == "America/New_York"


async def test_warm_identity_uses_default_profile_kind(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s")
    identity = driver.opens[0]["identity"]
    assert isinstance(identity, IdentityKey)
    assert identity.name == "s"
    assert identity.is_permanent  # ProfileKind.DEFAULT -> warm/persistent


async def test_basic_tier_uses_http_fast_path_not_the_browser(tmp_path: Path) -> None:
    driver = _FakeDriver()
    calls: list[dict[str, object]] = []

    async def fetcher(**kwargs: object) -> ActionResult:
        calls.append(kwargs)
        return ActionResult(extracts=["# http"], page_title="HTTP", status_code=200)

    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="basic",
        http_fetcher=fetcher,
    )
    # The HTTP path served it -- the browser was never launched.
    assert len(calls) == 1
    assert driver.opens == []
    assert document.markdown == "# http"
    # Coherent UA + Client Hints were sent (httpx's default UA is an instant block).
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["User-Agent"].startswith("Mozilla/5.0")  # type: ignore[union-attr]
    assert "sec-ch-ua" in headers


async def test_basic_tier_escalates_to_stealth_browser_on_http_block(tmp_path: Path) -> None:
    driver = _FakeDriver()  # its execute() returns a clean markdown result

    async def blocking_fetcher(**kwargs: object) -> ActionResult:
        raise ChallengeDetected("robot_check at https://example.com/blocked (http)")

    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="basic",  # ladder: basic (http) -> stealth (browser)
        http_fetcher=blocking_fetcher,
    )
    # HTTP hit a wall, so it escalated to the real stealth browser rung.
    assert len(driver.opens) == 1
    assert driver.opens[0]["warmup"] is True  # protected stealth open
    assert document.metadata is not None
    assert document.metadata.tier_used == "stealth"


async def test_auto_tier_starts_on_the_protected_rung(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s")  # tier defaults to "auto"
    o = driver.opens[0]
    # `auto` enters the escalation ladder at "stealth" -> protected from the
    # first attempt, so a default scrape is robust out of the box.
    assert o["warmup"] is True
    assert o["detect_blocks"] is True
    assert isinstance(o["user_agent"], str)


async def test_protected_tier_pins_fingerprint_and_enables_stealth(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s", tier="stealth")
    o = driver.opens[0]
    assert o["warmup"] is True
    assert o["detect_blocks"] is True
    assert isinstance(o["user_agent"], str) and o["user_agent"].startswith("Mozilla/5.0")
    assert isinstance(o["init_script"], str) and "[native code]" in o["init_script"]
    # Client-Hint headers + curated hardening launch flags are threaded through.
    assert isinstance(o["extra_http_headers"], dict) and "sec-ch-ua" in o["extra_http_headers"]
    assert isinstance(o["extra_launch_args"], list)
    assert any(a.startswith("--window-size=") for a in o["extra_launch_args"])
    # Protected tiers interact on the slow, most-human STEALTH timing table.
    assert o["interact_profile"] == "stealth"
    # Fingerprint geo fills locale/timezone the caller didn't pin.
    assert o["locale"] is not None
    assert o["timezone_id"] is not None


async def test_resource_blocking_is_off_by_default(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s", tier="stealth")
    o = driver.opens[0]
    assert o["block_resource_types"] is None
    assert o["block_hosts"] is None


async def test_block_images_expands_to_the_safe_media_set(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",), block_images=True, block_hosts=("track.co",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="stealth",
    )
    o = driver.opens[0]
    assert set(o["block_resource_types"]) == {"image", "media", "font"}  # type: ignore[arg-type]
    # Never blocks scripts/xhr/fetch/document (JS content + Akamai sensor).
    assert "script" not in o["block_resource_types"]  # type: ignore[operator]
    assert o["block_hosts"] == ("track.co",)


async def test_protected_tier_respects_explicit_locale(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(
        driver, tmp_path, session_name="s", tier="enhanced", locale="fr-FR",
        timezone_id="Europe/Paris",
    )
    o = driver.opens[0]
    # Explicit request locale/timezone win over the fingerprint's own geo.
    assert o["locale"] == "fr-FR"
    assert o["timezone_id"] == "Europe/Paris"


class _BlockThenPassDriver(_FakeDriver):
    """First `execute()` raises `ChallengeDetected` (the wall); the next
    succeeds -- models an `auto` scrape that gets blocked on the `stealth` rung
    and passes after escalating to `enhanced` with a fresh identity."""

    def __init__(self) -> None:
        super().__init__()
        self.execs = 0

    async def execute(
        self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
    ) -> ActionResult:
        self.execs += 1
        if self.execs == 1:
            raise ChallengeDetected("robot_check at https://zara.com/x")
        return ActionResult(extracts=["# hello"], page_title="Hello")


async def test_auto_escalates_on_challenge_to_a_fresh_identity(tmp_path: Path) -> None:
    driver = _BlockThenPassDriver()
    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="auto",  # ladder: stealth -> enhanced
    )
    assert driver.execs == 2  # first blocked, escalated, second passed
    assert len(driver.opens) == 2
    # Each attempt used a fresh throwaway identity (new proxy pick + fingerprint).
    assert driver.opens[0]["identity"] != driver.opens[1]["identity"]
    # The stealth rung is headless; the enhanced rung requests headful.
    assert driver.opens[0]["headful"] is False
    assert driver.opens[1]["headful"] is True
    # Truthful reporting: the tier that actually succeeded.
    assert document.metadata is not None
    assert document.metadata.tier_used == "enhanced"


async def test_auto_raises_when_every_rung_is_blocked(tmp_path: Path) -> None:
    class _AlwaysBlock(_FakeDriver):
        async def execute(
            self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
        ) -> ActionResult:
            raise ChallengeDetected("robot_check")

    driver = _AlwaysBlock()
    with pytest.raises(ChallengeDetected):
        await run_ephemeral_scrape(
            tenant="acme",
            domain="example.com",
            url="https://example.com/",
            options=ScrapeOptions(formats=("markdown",)),
            registry=Registry(),
            driver=driver,  # type: ignore[arg-type]
            profiles_root=tmp_path,
            proxy_pinner=None,
            lease_ttl_seconds=300.0,
            tier="auto",
        )
    assert len(driver.opens) == 2  # tried both rungs before giving up


class _SoftThenCleanDriver(_FakeDriver):
    """First `execute()` returns a soft (CRAWL-scope) verdict; the next returns a
    clean page -- models a thin/rate-limited render that recovers on a
    same-tier retry (no tier escalation)."""

    def __init__(self, soft_verdict: str = "too_small") -> None:
        super().__init__()
        self.execs = 0
        self._soft = soft_verdict

    async def execute(
        self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
    ) -> ActionResult:
        self.execs += 1
        if self.execs == 1:
            return ActionResult(extracts=["# thin"], page_title="Thin", soft_verdict=self._soft)
        return ActionResult(extracts=["# hello"], page_title="Hello")


async def test_soft_verdict_retries_same_tier_then_accepts(tmp_path: Path) -> None:
    driver = _SoftThenCleanDriver()
    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="stealth",
        retry_delay_base_s=0.0,  # no real sleeping in the unit suite
    )
    assert driver.execs == 2  # soft, retried, then clean
    assert document.markdown == "# hello"


async def test_soft_verdict_exhausts_retries_then_returns_content(tmp_path: Path) -> None:
    class _AlwaysSoft(_FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.execs = 0

        async def execute(
            self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
        ) -> ActionResult:
            self.execs += 1
            return ActionResult(extracts=["# thin"], page_title="Thin", soft_verdict="too_small")

    driver = _AlwaysSoft()
    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="stealth",
        crawl_retry_max=2,
        retry_delay_base_s=0.0,
    )
    # A soft verdict is not a hard block: after the bounded retries are spent the
    # content is returned as-is rather than raising (initial + 2 retries).
    assert driver.execs == 3
    assert document.markdown == "# thin"


async def test_soft_verdict_does_not_escalate_the_tier(tmp_path: Path) -> None:
    class _AlwaysSoft(_FakeDriver):
        async def execute(
            self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
        ) -> ActionResult:
            return ActionResult(extracts=["# thin"], page_title="Thin", soft_verdict="too_small")

    driver = _AlwaysSoft()
    document, _ = await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=None,
        lease_ttl_seconds=300.0,
        tier="auto",  # ladder stealth -> enhanced
        crawl_retry_max=1,
        retry_delay_base_s=0.0,
    )
    # CRAWL soft retries stay on the stealth rung; the enhanced (headful) rung is
    # never reached, and the served tier is reported truthfully as stealth.
    assert document.metadata is not None
    assert document.metadata.tier_used == "stealth"
    assert all(o["headful"] is False for o in driver.opens)


class _FakeBurnTracker:
    """Records burn-accounting calls and lets a test force `is_burned`."""

    def __init__(self, *, burned: bool = False) -> None:
        self.burned = burned
        self.blocks: list[int] = []
        self.successes = 0
        self.resets = 0

    async def is_burned(self, identity: IdentityKey) -> bool:
        return self.burned

    async def record_block(self, identity: IdentityKey, weight: int) -> int:
        self.blocks.append(weight)
        return weight

    async def record_success(self, identity: IdentityKey) -> int:
        self.successes += 1
        return 0

    async def reset(self, identity: IdentityKey) -> None:
        self.resets += 1


async def test_warm_success_self_heals_the_burn_counter(tmp_path: Path) -> None:
    tracker = _FakeBurnTracker()
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s", burn_tracker=tracker)
    assert tracker.successes == 1  # a clean scrape decrements the warning count
    assert tracker.blocks == []


async def test_warm_pre_burned_identity_is_retired_before_reuse(tmp_path: Path) -> None:
    tracker = _FakeBurnTracker(burned=True)
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s", burn_tracker=tracker)
    # A burned identity is reset (profile retired) before the scrape reuses it.
    assert tracker.resets >= 1


async def test_warm_all_blocked_charges_burn_and_retires_when_over_threshold(
    tmp_path: Path,
) -> None:
    class _AlwaysBlockForbidden(_FakeDriver):
        async def execute(
            self, ctx: ContextRef, actions: list[Action], page_id: str | None = None
        ) -> ActionResult:
            raise ChallengeDetected("forbidden", verdict="forbidden", weight=8)

    tracker = _FakeBurnTracker(burned=True)  # crosses threshold after the charge
    driver = _AlwaysBlockForbidden()
    with pytest.raises(ChallengeDetected):
        await _scrape(driver, tmp_path, session_name="s", tier="stealth", burn_tracker=tracker)
    assert tracker.blocks == [8]  # charged the forbidden weight
    assert tracker.resets >= 1  # and retired the now-burned identity


async def test_protected_tier_requests_residential_and_aligns_geo(tmp_path: Path) -> None:
    proxy = ProxyEndpoint(
        scheme="http", host="res", port=1, tier="residential", country="IN"
    )
    pinner = _FakeProxyPinner(proxy)
    driver = _FakeDriver()
    await run_ephemeral_scrape(
        tenant="acme",
        domain="example.com",
        url="https://example.com/",
        options=ScrapeOptions(formats=("markdown",)),
        registry=Registry(),
        driver=driver,  # type: ignore[arg-type]
        profiles_root=tmp_path,
        proxy_pinner=pinner,  # type: ignore[arg-type]
        lease_ttl_seconds=300.0,
        tier="stealth",
        session_name="s",
    )
    # Protected rung asked the pool for a residential exit (both at open and at
    # the post-success proxy-health recompute)...
    assert pinner.tiers_requested and all(t == "residential" for t in pinner.tiers_requested)
    # ...the served page was counted toward the proxy's retirement cap...
    assert pinner.successes == [proxy]
    # ...and the proxy's country (IN) seeded the fingerprint's geo, so the
    # browser's timezone/locale match the egress.
    o = driver.opens[0]
    assert o["timezone_id"] == "Asia/Kolkata"
    assert o["locale"] == "en-IN"
