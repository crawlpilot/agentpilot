"""`CrawlWorkerLoop`'s Phase-2 decision logic, driven directly rather than
through a database.

`_process_task` needs Postgres, a driver and a browser, and is covered by the
DB-gated suites. The three decisions added here -- which cache policy a job gets,
what a response status does to a host's backoff, and whether robots.txt is
refetched -- are all synchronous or near-enough, and each has a failure mode that
is invisible at runtime: a policy that silently ignores the caller's mode, a 429
that never slows anything down, a robots.txt fetched once per page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentpilot.jobs.cache import DEFAULT_MAX_AGE_MS, CachePolicy
from agentpilot.jobs.worker_loop import CrawlWorkerLoop
from crawlpilot.spi.scrape import Document, DocumentMetadata


class _RecordingLimiter:
    def __init__(self) -> None:
        self.penalized: list[str] = []
        self.rewarded: list[str] = []
        self.acquired: list[tuple[str, int]] = []

    async def acquire(self, host: str, *, interval_ms: int) -> None:
        self.acquired.append((host, interval_ms))

    def penalize(self, host: str) -> None:
        self.penalized.append(host)

    def reward(self, host: str) -> None:
        self.rewarded.append(host)


def _loop(limiter: Any = None, *, max_age_ms: int = DEFAULT_MAX_AGE_MS) -> CrawlWorkerLoop:
    return CrawlWorkerLoop(
        store=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        driver=None,  # type: ignore[arg-type]
        profiles_root=Path("/tmp"),
        proxy_pinner=None,
        host_limiter=limiter,
        cache_policy=CachePolicy(max_age_ms=max_age_ms),
    )


def _document(status: int | None) -> Document:
    return Document(
        document_id="d1",
        url="https://example.com/a",
        metadata=(
            DocumentMetadata(
                title=None,
                status_code=status,
                tier_used="auto",
                node_id="n1",
                duration_ms=1.0,
                source_url="https://example.com/a",
            )
            if status is not None
            else None
        ),
    )


# ------------------------------------------------------------ cache policy


def test_the_jobs_cache_mode_always_wins() -> None:
    """A caller who asked for `bypass` on one crawl means it, whatever the
    deployment would prefer -- otherwise there is no way to force a refetch."""

    loop = _loop()
    assert loop._policy_for("bypass", None).mode == "bypass"
    assert loop._policy_for("disabled", None).mode == "disabled"


def test_an_unset_max_age_falls_back_to_the_deployment_default() -> None:
    loop = _loop(max_age_ms=900_000)
    assert loop._policy_for("enabled", None).max_age_ms == 900_000


def test_a_jobs_own_max_age_overrides_the_default() -> None:
    loop = _loop(max_age_ms=900_000)
    assert loop._policy_for("enabled", 60_000).max_age_ms == 60_000


# ---------------------------------------------------------- status feedback


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_slow_down_status_penalizes_the_host(status: int) -> None:
    """These arrive as *successful* scrapes -- a 429 returned a page, the page
    just says come back later -- so nothing raises and this is the only place the
    signal can be picked up."""

    limiter = _RecordingLimiter()
    _loop(limiter)._record_host_response("example.com", _document(status))
    assert limiter.penalized == ["example.com"]


@pytest.mark.parametrize("status", [200, 201, 301, 304])
def test_a_healthy_status_relaxes_the_host(status: int) -> None:
    limiter = _RecordingLimiter()
    _loop(limiter)._record_host_response("example.com", _document(status))
    assert limiter.rewarded == ["example.com"]


def test_a_404_neither_penalizes_nor_rewards() -> None:
    """A missing page is a fact about that URL, not about the host's willingness
    to serve traffic. Rewarding on it would relax a backoff on evidence that has
    nothing to do with load."""

    limiter = _RecordingLimiter()
    _loop(limiter)._record_host_response("example.com", _document(404))
    assert limiter.penalized == []
    assert limiter.rewarded == []


def test_a_document_with_no_metadata_is_ignored() -> None:
    limiter = _RecordingLimiter()
    _loop(limiter)._record_host_response("example.com", _document(None))
    assert limiter.penalized == []
    assert limiter.rewarded == []


# ------------------------------------------------------------------ robots


async def test_robots_is_fetched_once_per_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to be refetched on every frontier expansion, so a 500-page crawl
    of one host fetched the same robots.txt 500 times -- and reading
    `Crawl-delay` on the scrape path too would have doubled that."""

    calls: list[str] = []

    async def fake_fetch(origin: str, policy: Any) -> None:
        calls.append(origin)
        return None

    monkeypatch.setattr("agentpilot.jobs.worker_loop.fetch_robots", fake_fetch)
    loop = _loop()
    for _ in range(5):
        await loop._robots_for("https://example.com")
    assert calls == ["https://example.com"]


async def test_a_failed_robots_fetch_is_cached_as_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` is a legitimate value -- `crawl.robots.fetch` fails open -- so the
    memo has to test membership rather than truthiness, or a robots-less host
    refetches on every single page."""

    calls: list[str] = []

    async def fake_fetch(origin: str, policy: Any) -> None:
        calls.append(origin)
        return None

    monkeypatch.setattr("agentpilot.jobs.worker_loop.fetch_robots", fake_fetch)
    loop = _loop()
    assert await loop._robots_for("https://example.com") is None
    assert await loop._robots_for("https://example.com") is None
    assert len(calls) == 1


async def test_two_origins_are_memoized_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_fetch(origin: str, policy: Any) -> None:
        calls.append(origin)
        return None

    monkeypatch.setattr("agentpilot.jobs.worker_loop.fetch_robots", fake_fetch)
    loop = _loop()
    await loop._robots_for("https://a.example.com")
    await loop._robots_for("https://b.example.com")
    assert calls == ["https://a.example.com", "https://b.example.com"]
