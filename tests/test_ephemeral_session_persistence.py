"""`run_ephemeral_scrape`'s warm-vs-throwaway profile behavior (anti-detection
A1) -- a real in-memory `Registry` + a fake driver that records what `open()`
receives, no real browser. Asserts a stable `session_name` reuses one
persistent profile dir across calls (cookie/return-visitor signal), while
the default throwaway path mints a fresh dir per call and deletes it."""

from __future__ import annotations

import uuid
from pathlib import Path

from agentpilot.session.ephemeral import run_ephemeral_scrape
from agentpilot.session.registry import Registry
from agentpilot.spi.actions import Action, ActionResult
from agentpilot.spi.egress import EgressPolicy
from agentpilot.spi.identity import IdentityKey
from agentpilot.spi.lease import ContextRef, ContextState
from agentpilot.spi.scrape import ScrapeOptions


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
    ) -> ContextRef:
        self.opens.append(
            {
                "identity": identity,
                "profile_dir": profile_dir,
                "locale": locale,
                "timezone_id": timezone_id,
                "warmup": warmup,
                "detect_blocks": detect_blocks,
                "user_agent": user_agent,
                "init_script": init_script,
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


async def test_default_tier_applies_no_stealth_path(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s")  # tier defaults to "auto"
    o = driver.opens[0]
    assert o["warmup"] is False
    assert o["detect_blocks"] is False
    assert o["user_agent"] is None
    assert o["init_script"] is None


async def test_protected_tier_pins_fingerprint_and_enables_stealth(tmp_path: Path) -> None:
    driver = _FakeDriver()
    await _scrape(driver, tmp_path, session_name="s", tier="stealth")
    o = driver.opens[0]
    assert o["warmup"] is True
    assert o["detect_blocks"] is True
    assert isinstance(o["user_agent"], str) and o["user_agent"].startswith("Mozilla/5.0")
    assert isinstance(o["init_script"], str) and "[native code]" in o["init_script"]
    # Fingerprint geo fills locale/timezone the caller didn't pin.
    assert o["locale"] is not None
    assert o["timezone_id"] is not None


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
