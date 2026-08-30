"""The extension seam: dispatch rules, isolation, compatibility, and wiring.

Phase 4 replaced `block_detect`'s module-level `_SITE_CHECKERS` list -- populated
by an `install_default_site_checkers()` call at *import* time -- with a
per-instance registry. These tests pin the behaviour that makes that safe to
depend on, plus the reference extension that keeps the seam honest.
"""

from __future__ import annotations

import asyncio

import pytest

from agentpilot.control.retail_extension import RetailExtension
from crawlpilot.extensions import (
    API_VERSION,
    BlockHooks,
    BrowseHooks,
    Compatibility,
    ContentHooks,
    ExtensionManifest,
    ExtensionRegistry,
    Resolution,
    check_compatibility,
)
from crawlpilot.extraction.block_detect import Verdict, classify_page

# --------------------------------------------------------------- reference


class ReferenceExtension:
    """Exercises **every** mount, and is loaded in CI.

    This is the PDK's in-tree test plugin (Browser4 ships
    `browser4-pdk-test-plugin` for exactly this reason): an SPI nobody builds
    against rots. If this still wires and dispatches, the seam is real.
    """

    def __init__(self) -> None:
        self.manifest = ExtensionManifest(
            name="reference",
            version="1.0",
            description="Exercises every mount; the seam's own smoke test.",
        )
        self.seen: list[str] = []

    def configure_browse(self, hooks: BrowseHooks) -> None:
        hooks.will_navigate.add_last(self._rewrite)
        hooks.document_steady.add_last(self._note("document_steady"))

    def configure_content(self, hooks: ContentHooks) -> None:
        hooks.will_parse.add_last(lambda html: html.replace("<broken>", "") or None)
        hooks.extracted.add_last(self._enrich)

    def configure_blocks(self, hooks: BlockHooks) -> None:
        hooks.classify.add_last(
            lambda **kw: Verdict.ROBOT_CHECK if "wall" in kw["url"] else None
        )
        hooks.resolve.add_last(lambda verdict: Resolution.ESCALATE)

    def _rewrite(self, url: str) -> str | None:
        return url.replace("?utm_source=x", "") if "utm_source" in url else None

    def _note(self, label: str):  # type: ignore[no-untyped-def]
        async def handler() -> None:
            self.seen.append(label)

        return handler

    async def _enrich(self, doc: dict[str, str]) -> dict[str, str]:
        return {**doc, "enriched": "yes"}


def test_reference_extension_wires_every_mount() -> None:
    ext = ReferenceExtension()
    registry = ExtensionRegistry([ext])
    assert registry.loaded == ("reference",)
    assert len(registry.browse.will_navigate) == 1
    assert len(registry.content.will_parse) == 1
    assert len(registry.blocks.classify) == 1
    assert len(registry.blocks.resolve) == 1


async def test_every_hook_shape_dispatches() -> None:
    ext = ReferenceExtension()
    r = ExtensionRegistry([ext])

    # filter-shaped, sync
    assert r.browse.will_navigate.first_result("https://x/?utm_source=x") == "https://x/"
    assert r.browse.will_navigate.first_result("https://x/") is None  # defers

    # notification-shaped
    await r.browse.document_steady.run_all()
    assert ext.seen == ["document_steady"]

    # value-chaining
    assert await r.content.extracted.chain({"a": "1"}) == {"a": "1", "enriched": "yes"}

    # filter-shaped, async
    assert await r.blocks.resolve.first_result_async(Verdict.ROBOT_CHECK) is Resolution.ESCALATE


# ---------------------------------------------------------- dispatch rules


def test_first_non_none_wins_and_none_defers() -> None:
    """The convention inherited from `ChainedHtmlIntegrityChecker`: `None` means
    "no opinion", so a later handler still gets a turn."""

    hooks = BlockHooks()
    hooks.classify.add_last(lambda **kw: None)
    hooks.classify.add_last(lambda **kw: Verdict.TOO_SMALL)
    hooks.classify.add_last(lambda **kw: Verdict.FORBIDDEN)  # never reached
    assert hooks.classify.first_result(html="", url="u", status=200) is Verdict.TOO_SMALL


def test_add_first_controls_ordering() -> None:
    hooks = BlockHooks()
    hooks.classify.add_last(lambda **kw: Verdict.TOO_SMALL)
    hooks.classify.add_first(lambda **kw: Verdict.FORBIDDEN)
    assert hooks.classify.first_result(html="", url="u", status=200) is Verdict.FORBIDDEN


def test_a_throwing_extension_does_not_break_the_chain() -> None:
    """The single most important rule: a broken extension degrades one site, it
    never kills a crawl."""

    def boom(**kw: object) -> Verdict:
        raise RuntimeError("extension is broken")

    hooks = BlockHooks()
    hooks.classify.add_last(boom)
    hooks.classify.add_last(lambda **kw: Verdict.TOO_SMALL)
    assert hooks.classify.first_result(html="", url="u", status=200) is Verdict.TOO_SMALL


async def test_a_throwing_async_handler_is_isolated() -> None:
    async def boom(_v: object) -> Resolution:
        raise RuntimeError("nope")

    hooks = BlockHooks()
    hooks.resolve.add_last(boom)
    hooks.resolve.add_last(lambda _v: Resolution.RETRY)
    assert await hooks.resolve.first_result_async(Verdict.ROBOT_CHECK) is Resolution.RETRY


async def test_a_hanging_handler_hits_its_deadline_and_the_chain_continues() -> None:
    async def hangs(_v: object) -> Resolution:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    hooks = BlockHooks()
    hooks.resolve.timeout_s = 0.05
    hooks.resolve.add_last(hangs)
    hooks.resolve.add_last(lambda _v: Resolution.GIVE_UP)
    assert await hooks.resolve.first_result_async(Verdict.ROBOT_CHECK) is Resolution.GIVE_UP


async def test_run_all_runs_every_handler_even_after_one_fails() -> None:
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("broken")

    hooks = BrowseHooks()
    hooks.document_steady.add_last(boom)
    hooks.document_steady.add_last(lambda: calls.append("second"))
    await hooks.document_steady.run_all()
    assert calls == ["second"]


def test_handlers_are_attributed_to_their_extension() -> None:
    """Metrics and failure logs are labelled by extension; without attribution a
    misbehaving one is invisible in production."""

    registry = ExtensionRegistry([ReferenceExtension()])
    assert {h.extension for h in registry.blocks.classify.handlers} == {"reference"}


# ------------------------------------------------------------- loading


def test_a_newer_major_api_is_refused() -> None:
    manifest = ExtensionManifest(name="future", api_version="99.0")
    assert check_compatibility(manifest).compatibility is Compatibility.REFUSE

    class Future:
        def __init__(self) -> None:
            self.manifest = manifest

        def configure_blocks(self, hooks: BlockHooks) -> None:
            raise AssertionError("must not be wired")

    registry = ExtensionRegistry([Future()])
    assert registry.loaded == ()
    assert len(registry.blocks.classify) == 0


def test_an_older_major_api_still_loads() -> None:
    """Old extensions keep working -- the host only added things."""

    manifest = ExtensionManifest(name="legacy", api_version="0.0")
    verdict = check_compatibility(manifest, host_api_version="1.4")
    assert verdict.compatibility is Compatibility.LOAD_WITH_WARNING
    assert verdict.loads is True


def test_an_extension_can_be_disabled_by_config() -> None:
    registry = ExtensionRegistry([ReferenceExtension()], disabled=["reference"])
    assert registry.loaded == ()


def test_a_mount_that_raises_disables_only_that_surface() -> None:
    class HalfBroken:
        manifest = ExtensionManifest(name="half")

        def configure_blocks(self, hooks: BlockHooks) -> None:
            hooks.classify.add_last(lambda **kw: Verdict.EMPTY)

        def configure_browse(self, hooks: BrowseHooks) -> None:
            raise RuntimeError("bad wiring")

    registry = ExtensionRegistry([HalfBroken()])
    assert registry.loaded == ("half",)
    assert len(registry.blocks.classify) == 1
    assert len(registry.browse.will_navigate) == 0


def test_host_api_version_is_declared() -> None:
    assert ExtensionManifest(name="x").api_version == API_VERSION


# ------------------------------------------------------- no global state


def test_importing_the_content_pipeline_registers_nothing() -> None:
    """The D12 defect, asserted directly: importing `block_detect` used to
    install three retailers' policy into a module-level list."""

    import crawlpilot.extraction.block_detect as bd

    # Deliberately *not* `importlib.reload`: reloading rebinds `Verdict` to a new
    # enum class, so every `is` comparison in the rest of this file would then
    # compare members of two different enums and fail confusingly.
    assert not hasattr(bd, "_SITE_CHECKERS")
    assert not hasattr(bd, "register_site_checker")
    assert not hasattr(bd, "install_default_site_checkers")

    # With no hooks passed, only the generic classifier runs -- a Walmart page
    # under that site's own floor but over the generic one reads as OK.
    assert bd.classify_page(
        html="<html>" + "x" * 1_000 + "</html>",
        url="https://www.walmart.com/ip/1",
        status=200,
    ) is Verdict.OK


def test_two_registries_do_not_share_state() -> None:
    a = ExtensionRegistry([ReferenceExtension()])
    b = ExtensionRegistry()
    assert len(a.blocks.classify) == 1
    assert len(b.blocks.classify) == 0


# ------------------------------------------------- the retail port itself


@pytest.mark.parametrize(
    ("url", "html", "expected"),
    [
        ("https://www.walmart.com/ip/1", "<html>" + "x" * 1_000 + "</html>", Verdict.TOO_SMALL),
        ("https://www.amazon.com/dp/B1", "<html>" + "x" * 1_000 + "</html>", Verdict.TOO_SMALL),
    ],
)
def test_retail_detection_survives_the_port(url: str, html: str, expected: Verdict) -> None:
    """The regression the risk register named: deleting the import-time install
    must not silently disable Walmart/Amazon detection. Through the extension it
    still fires; without it, the generic classifier alone says OK."""

    hooks = ExtensionRegistry([RetailExtension()]).blocks
    assert classify_page(html=html, url=url, status=200, hooks=hooks) is expected
    assert classify_page(html=html, url=url, status=200) is Verdict.OK
