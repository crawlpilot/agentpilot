"""The `Browser` / `BrowserSession` facade.

Phase 5's point is that a caller can drive a browser without assembling a
driver, a registry, a proxy pinner, a vault, a profiles root and lease TTLs by
hand -- which previously only `gateway.wiring` knew how to do, and which sits
above the session layer so it could not be reused as a library entry point
(plan D2, D9).

These run against a recording fake driver: the facade's job is *assembly and
batch composition*, and both are fully observable without Chrome. The real
driver is covered by `tests/driver_contract/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crawlpilot.api import Browser, BrowserSession
from crawlpilot.extensions import ExtensionManifest, ExtensionRegistry
from crawlpilot.policy import NullPrototypes
from crawlpilot.session.registry import Registry
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.actions import ActionResult
from crawlpilot.spi.identity import IdentityRef
from crawlpilot.spi.lease import ContextRef, ContextState


class RecordingDriver:
    """Records every batch the facade dispatches."""

    def __init__(self) -> None:
        self.batches: list[list[spi_actions.Action]] = []
        self.opened: list[IdentityRef] = []
        self.closed: list[str] = []

    async def open(self, identity: IdentityRef, *args: object, **kwargs: object) -> ContextRef:
        self.opened.append(identity)
        return ContextRef(
            context_id=f"ctx-{len(self.opened)}",
            identity=identity,
            state=ContextState.ACTIVE,
            pid=None,
        )

    async def close(self, ctx: ContextRef) -> None:
        self.closed.append(ctx.context_id)

    async def execute(
        self, ctx: ContextRef, actions: list[spi_actions.Action], page_id: str | None = None
    ) -> ActionResult:
        self.batches.append(actions)
        return ActionResult(
            extracts=["# Title\n\nbody"],
            screenshots=[b"png"],
            page_title="Title",
        )

    async def is_alive(self, ctx: ContextRef) -> bool:
        return True

    async def context_health(self, ctx: ContextRef) -> None:
        return None

    async def export_state(self, ctx: ContextRef) -> None:
        return None


@pytest.fixture
def browser(tmp_path: Path) -> Browser:
    return Browser(driver=RecordingDriver(), profiles_root=tmp_path)


# ------------------------------------------------------------- construction


def test_a_caller_can_pass_nothing() -> None:
    """The headline claim. Constructing must not require -- or import -- Chrome:
    `crawlpilot.driver` pulls Patchright, which Chrome-free deployments
    deliberately do not install."""

    b = Browser()
    assert isinstance(b.registry, Registry)
    assert isinstance(b.prototype_provider, NullPrototypes)
    assert b.proxy_pinner is None
    assert b.extensions.loaded == ()
    assert b._driver is None  # noqa: SLF001 -- deferred until first use


async def test_an_owned_profiles_root_is_cleaned_up() -> None:
    b = Browser()
    root = b.profiles_root
    assert root.exists()
    await b.close()
    assert not root.exists()


async def test_a_supplied_profiles_root_is_left_alone(tmp_path: Path) -> None:
    """Warm identities only survive across runs when profiles outlive the
    process, so a caller that supplies a root owns it."""

    b = Browser(profiles_root=tmp_path)
    await b.close()
    assert tmp_path.exists()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    b = Browser(profiles_root=tmp_path)
    await b.close()
    await b.close()


def test_an_extension_registry_is_accepted_as_well_as_a_list() -> None:
    class Ext:
        manifest = ExtensionManifest(name="e")

    assert Browser(extensions=[Ext()]).extensions.loaded == ("e",)
    assert Browser(extensions=ExtensionRegistry([Ext()])).extensions.loaded == ("e",)


# ------------------------------------------------------------- sessions


async def test_session_opens_and_always_releases(browser: Browser) -> None:
    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session() as page:
        assert isinstance(page, BrowserSession)
        assert page.session_id
    assert len(driver.opened) == 1


async def test_session_releases_even_when_the_body_raises(browser: Browser) -> None:
    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        async with browser.session():
            raise RuntimeError("boom")
    # The lease went back to the registry rather than leaking a live context.
    snapshot = await browser.registry.snapshot()
    assert all(lease is None for _identity, _ctx, lease, _at in snapshot)
    assert len(driver.opened) == 1


async def test_identity_defaults_to_a_fresh_scope(browser: Browser) -> None:
    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session():
        pass
    async with browser.session():
        pass
    assert driver.opened[0].key != driver.opened[1].key


async def test_a_named_identity_reuses_its_warm_context(browser: Browser) -> None:
    """The opposite of the default: repeat visits reuse one profile, so the site
    sees a returning visitor.

    The observable proof is stronger than "same key": the driver is opened
    *once*. The second `session()` acquires the context the first released to
    the warm IDLE pool rather than launching a fresh Chrome -- which is the
    whole reason a caller names an identity.
    """

    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session(identity="alice") as first:
        first_id = first.session_id
    async with browser.session(identity="alice") as second:
        assert second.session_id != first_id  # a new session over the same context

    assert len(driver.opened) == 1
    assert driver.closed == []


# --------------------------------------------------- batch composition


async def test_each_method_composes_the_right_action(browser: Browser) -> None:
    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session() as page:
        await page.navigate("https://example.com")
        await page.click("e1")
        await page.fill("e2", "hello")
        await page.press("Enter")
        await page.scroll("down")

    kinds = [type(batch[0]).__name__ for batch in driver.batches]
    assert kinds == [
        "NavigateAction",
        "ClickAction",
        "FillAction",
        "PressAction",
        "ScrollAction",
    ]
    assert driver.batches[2][0].ref == "e2"
    assert driver.batches[2][0].text == "hello"


async def test_markdown_returns_the_content_not_an_index(browser: Browser) -> None:
    """`ActionResult.extracts` is a positionally-correlated list; reaching
    content through it was the D6 awkwardness this removes."""

    async with browser.session() as page:
        assert await page.markdown() == "# Title\n\nbody"
        assert await page.screenshot() == b"png"


async def test_execute_stays_the_escape_hatch_for_real_batching(browser: Browser) -> None:
    """Batching is the transport, not an implementation detail: several actions
    must reach the driver in ONE call, not one call each."""

    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session() as page:
        await page.execute(
            [
                spi_actions.NavigateAction(url="https://example.com"),
                spi_actions.ClickAction(ref="e1"),
                spi_actions.ExtractAction(format="markdown"),
            ]
        )
    assert len(driver.batches) == 1
    assert len(driver.batches[0]) == 3


async def test_convenience_methods_are_sugar_over_execute(browser: Browser) -> None:
    """One round trip per call -- never a second code path that bypasses the
    batch transport."""

    driver: RecordingDriver = browser.driver  # type: ignore[assignment]
    async with browser.session() as page:
        await page.navigate("https://example.com")
    assert len(driver.batches) == 1
    assert len(driver.batches[0]) == 1
