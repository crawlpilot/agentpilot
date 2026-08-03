"""`routes/scrape.py`'s `scrape()` -- real Patchright context, real in-memory
`Registry`, called directly (this repo's route-testing convention, see
`test_sessions_list.py`) rather than through a `TestClient`. What matters
here specifically: an ephemeral scrape leaves no IDLE registry entry behind,
deletes its own profile dir, and never collides with a concurrent
interactive session for the same tenant+domain.
"""

from __future__ import annotations

from pytest_httpserver import HTTPServer

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.gateway.routes.scrape import scrape
from agentpilot.gateway.schemas import ScrapeRequest
from agentpilot.session.registry import Registry
from agentpilot.spi.identity import IdentityKey

ARTICLE_HTML = """<html><body>
<article>
<h1>Scrape Route Article</h1>
<p>This is the first paragraph of real body content, long enough and
distinct enough that trafilatura should treat it as the main article rather
than boilerplate noise.</p>
<p>A second paragraph continues with different wording so extraction has
real multi-paragraph content to check against.</p>
</article>
</body></html>"""


class _FakeHeaders:
    def get(self, _key: str) -> str | None:
        return None


class _FakeRequest:
    headers = _FakeHeaders()


class _FakeWiring:
    def __init__(self, driver: PatchrightDriver, profiles_root) -> None:
        self.registry = Registry()
        self.driver = driver
        self.profiles_root = profiles_root
        self.proxy_pinner = None
        self.vault = None
        self.lease_ttl_seconds = 300.0


async def test_scrape_returns_markdown_and_leaves_no_warm_entry(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    wiring = _FakeWiring(driver, tmp_path)

    resp = await scrape(
        ScrapeRequest(tenant="acme", url=httpserver.url_for("/")), _FakeRequest(), wiring
    )

    assert resp.success is True
    assert resp.data.markdown is not None
    assert "Scrape Route Article" in resp.data.markdown
    assert resp.data.error is None

    # Ephemeral: no entry left warm in the registry for the next scrape to
    # collide with or the reaper to find.
    assert await wiring.registry.snapshot() == []


async def test_scrape_deletes_its_profile_dir_afterward(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    wiring = _FakeWiring(driver, tmp_path)

    await scrape(ScrapeRequest(tenant="acme", url=httpserver.url_for("/")), _FakeRequest(), wiring)

    domain_dir = tmp_path / "acme" / "127.0.0.1"
    # The identity's random per-call name means we can't predict the exact
    # leaf dir, but the whole domain-level directory tree it lived under
    # must be empty -- nothing left behind for a one-shot identity.
    assert not domain_dir.exists() or not any(domain_dir.iterdir())


async def test_scrape_does_not_disturb_a_concurrent_interactive_session(
    driver: PatchrightDriver, tmp_path, httpserver: HTTPServer
) -> None:
    """A real interactive identity for the *same tenant+domain*, held ACTIVE
    in the registry across the scrape call -- scrape's own identity is a
    distinct, randomly named one (`kind=DEFAULT` vs. scrape's `TEMPORARY`,
    per `routes/sessions.py`'s `open_session`), so this must neither raise
    `LeaseConflict` nor have scrape's `registry.evict()` teardown touch the
    interactive session's still-ACTIVE entry."""

    from agentpilot.identity.profile_store import resolve_profile_dir
    from agentpilot.spi.egress import EgressPolicy
    from agentpilot.spi.identity import ProfileKind

    httpserver.expect_request("/").respond_with_data(ARTICLE_HTML, content_type="text/html")
    wiring = _FakeWiring(driver, tmp_path)

    domain = "127.0.0.1"
    interactive_identity = IdentityKey(
        tenant="acme", domain=domain, name="alice", kind=ProfileKind.DEFAULT
    )

    async def _open_interactive():
        profile_dir = resolve_profile_dir(tmp_path, interactive_identity)
        profile_dir.mkdir(parents=True)
        return await wiring.driver.open(
            interactive_identity, profile_dir, None, headful=False, egress=EgressPolicy()
        )

    interactive_ctx, interactive_lease = await wiring.registry.acquire(
        interactive_identity, "alice-owner", 300.0, _open_interactive
    )
    try:
        resp = await scrape(
            ScrapeRequest(tenant="acme", url=httpserver.url_for("/")), _FakeRequest(), wiring
        )
        assert resp.success is True

        snapshot = await wiring.registry.snapshot()
        assert len(snapshot) == 1  # only the interactive identity remains
        identity, _ctx, lease, _released_at = snapshot[0]
        assert identity == interactive_identity
        assert lease is not None and lease.lease_id == interactive_lease.lease_id
    finally:
        await wiring.registry.release(interactive_lease.lease_id)
        await wiring.driver.close(interactive_ctx)
