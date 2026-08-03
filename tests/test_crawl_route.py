"""`routes/crawl.py` -- unit-level route calls (this repo's convention, see
`test_map_route.py`/`test_sessions_list.py`) against a real `PostgresJobStore`
(job CRUD needs a real database to be meaningful) and a real `pytest_httpserver`
for the seed-discovery HTTP fetch `create_crawl` makes. Skipped automatically
when `AGENTPILOT_TEST_DATABASE_URL` isn't set/reachable, same idiom as
`tests/test_jobs_store.py`.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from fastapi import HTTPException
from pytest_httpserver import HTTPServer

from agentpilot.auth.models import AuthedTenant
from agentpilot.gateway.routes.crawl import cancel_crawl, create_crawl, get_crawl
from agentpilot.gateway.schemas import CrawlRequest, WebhookIn
from agentpilot.jobs.store import PostgresJobStore
from agentpilot.spi.errors import JobNotFound

_DATABASE_URL = os.environ.get("AGENTPILOT_TEST_DATABASE_URL")


def _database_reachable() -> bool:
    if not _DATABASE_URL:
        return False
    try:
        with psycopg.connect(_DATABASE_URL, connect_timeout=1):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _database_reachable(),
    reason="set AGENTPILOT_TEST_DATABASE_URL to a real local Postgres db with "
    "`alembic upgrade head` already applied",
)


class _FakeRequest:
    base_url = "http://gateway.internal/"


class _FakeWiring:
    def __init__(self, jobs_store: PostgresJobStore | None) -> None:
        self.jobs_store = jobs_store


@pytest.fixture
async def store():
    assert _DATABASE_URL is not None
    s = await PostgresJobStore.connect(_DATABASE_URL)
    yield s
    async with s._pool.connection() as conn:
        await conn.execute("DELETE FROM jobs WHERE tenant LIKE 'crawlroute-%'")
    await s.close()


def _tenant() -> str:
    return f"crawlroute-{uuid.uuid4().hex[:8]}"


def _register_simple_site(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/").respond_with_data(
        '<a href="/a">A</a><a href="/b">B</a>', content_type="text/html"
    )
    httpserver.expect_request("/sitemap.xml").respond_with_data("", status=404)
    httpserver.expect_request("/robots.txt").respond_with_data("", status=404)


async def test_create_crawl_splits_seed_and_discovered_links_by_depth(
    store: PostgresJobStore, httpserver: HTTPServer
) -> None:
    _register_simple_site(httpserver)
    tenant = _tenant()
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=tenant, key_id="k1")
    req = CrawlRequest(tenant=tenant, url=httpserver.url_for("/"))

    resp = await create_crawl(req, _FakeRequest(), wiring, authed)

    assert resp.success is True
    assert resp.url == f"http://gateway.internal/v1/crawl/{resp.id}"
    assert resp.webhook_secret is None

    job = await store.get_job(resp.id, tenant)
    assert job is not None
    assert job.status == "scraping"
    assert job.discovery_done is True
    assert job.total == 3  # seed + /a + /b

    async with store._pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT url, depth FROM crawl_tasks WHERE job_id = %s ORDER BY depth, url",
                (resp.id,),
            )
            rows = await cur.fetchall()
    depth_by_url = {url: depth for url, depth in rows}
    assert depth_by_url[httpserver.url_for("/")] == 0
    assert depth_by_url[httpserver.url_for("/a")] == 1
    assert depth_by_url[httpserver.url_for("/b")] == 1


async def test_create_crawl_overwrites_a_mismatched_tenant(
    store: PostgresJobStore, httpserver: HTTPServer
) -> None:
    _register_simple_site(httpserver)
    tenant = _tenant()
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=tenant, key_id="k1")
    req = CrawlRequest(tenant="someone-else", url=httpserver.url_for("/"))

    resp = await create_crawl(req, _FakeRequest(), wiring, authed)

    job = await store.get_job(resp.id, tenant)
    assert job is not None  # scoped to the authed tenant, not the request body


async def test_create_crawl_returns_webhook_secret_once(
    store: PostgresJobStore, httpserver: HTTPServer
) -> None:
    _register_simple_site(httpserver)
    tenant = _tenant()
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=tenant, key_id="k1")
    req = CrawlRequest(
        tenant=tenant,
        url=httpserver.url_for("/"),
        webhook=WebhookIn(url="https://example.com/hook"),
    )

    resp = await create_crawl(req, _FakeRequest(), wiring, authed)

    assert resp.webhook_secret is not None
    fetched = await store.get_job_for_worker(resp.id)
    assert fetched is not None
    assert fetched.webhook_secret == resp.webhook_secret
    assert fetched.webhook_url == "https://example.com/hook"


async def test_create_crawl_rejects_non_http_url(store: PostgresJobStore) -> None:
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=_tenant(), key_id="k1")
    req = CrawlRequest(tenant=authed.tenant, url="ftp://example.com/")

    with pytest.raises(HTTPException) as exc_info:
        await create_crawl(req, _FakeRequest(), wiring, authed)
    assert exc_info.value.status_code == 400


async def test_create_crawl_without_jobs_store_configured_raises_503() -> None:
    wiring = _FakeWiring(None)
    authed = AuthedTenant(tenant="acme", key_id="k1")
    req = CrawlRequest(tenant="acme", url="https://example.com")

    with pytest.raises(HTTPException) as exc_info:
        await create_crawl(req, _FakeRequest(), wiring, authed)
    assert exc_info.value.status_code == 503


async def test_get_crawl_reports_initial_status_and_empty_data(
    store: PostgresJobStore, httpserver: HTTPServer
) -> None:
    _register_simple_site(httpserver)
    tenant = _tenant()
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=tenant, key_id="k1")
    created = await create_crawl(
        CrawlRequest(tenant=tenant, url=httpserver.url_for("/")), _FakeRequest(), wiring, authed
    )

    status = await get_crawl(created.id, None, 100, wiring, authed)

    assert status.success is True
    assert status.status == "scraping"
    assert status.total == 3
    assert status.completed == 0
    assert status.data == []
    assert status.next is None


async def test_get_crawl_unknown_id_raises_job_not_found(store: PostgresJobStore) -> None:
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=_tenant(), key_id="k1")
    with pytest.raises(JobNotFound):
        await get_crawl("no-such-job", None, 100, wiring, authed)


async def test_get_crawl_scoped_to_tenant(store: PostgresJobStore, httpserver: HTTPServer) -> None:
    _register_simple_site(httpserver)
    wiring = _FakeWiring(store)
    owner = AuthedTenant(tenant=_tenant(), key_id="k1")
    created = await create_crawl(
        CrawlRequest(tenant=owner.tenant, url=httpserver.url_for("/")),
        _FakeRequest(),
        wiring,
        owner,
    )

    other = AuthedTenant(tenant=_tenant(), key_id="k2")
    with pytest.raises(JobNotFound):
        await get_crawl(created.id, None, 100, wiring, other)


async def test_cancel_crawl_marks_job_cancelled(
    store: PostgresJobStore, httpserver: HTTPServer
) -> None:
    _register_simple_site(httpserver)
    tenant = _tenant()
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=tenant, key_id="k1")
    created = await create_crawl(
        CrawlRequest(tenant=tenant, url=httpserver.url_for("/")), _FakeRequest(), wiring, authed
    )

    result = await cancel_crawl(created.id, wiring, authed)
    assert result == {"success": True}

    job = await store.get_job(created.id, tenant)
    assert job is not None
    assert job.status == "cancelled"


async def test_cancel_crawl_unknown_id_returns_false(store: PostgresJobStore) -> None:
    wiring = _FakeWiring(store)
    authed = AuthedTenant(tenant=_tenant(), key_id="k1")
    result = await cancel_crawl("no-such-job", wiring, authed)
    assert result == {"success": False}
