"""Running the discovery sources: one interface, independent deadlines, and a
per-source account of what each contributed.

Before this, discovery was two hard-coded steps inside `seed.py` -- fetch the
sitemap, fetch the seed page -- and adding a third meant editing that function.
There are now eight, they have wildly different latencies (a sitemap answers in
200 ms, crt.sh can take thirty seconds), and any of them can be down. So they
need three things this module provides and inline code did not:

1. **A deadline each.** `asyncio.wait_for` per source, not one timeout for the
   whole gather. Without it the slowest source sets the latency of `/v1/map` --
   which is the single most common complaint about tools that query crt.sh.
2. **Independent failure.** A source that raises, times out or returns nonsense
   contributes nothing and is reported. It never fails the request. Discovery
   from six working sources is a good answer; a 500 because the seventh was
   rate-limited is not.
3. **An account of who found what.** `SourceReport` carries per-source counts and
   errors, so "map returned 40 URLs" is debuggable — the answer is usually "the
   sitemap 404s and Common Crawl has no record of this domain", and without this
   there is no way to see that.

Sources run concurrently, which is safe because each talks to a different host --
with one exception noted at `_PRE_SOURCES`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

import structlog

from agentpilot.crawl import cc, crt, feeds, probe, robots, sitemap, soft404, wayback
from crawlpilot.spi.egress import EgressPolicy

log = structlog.get_logger(__name__)

SourceName = Literal[
    "sitemap", "cc", "wayback", "crt", "feed", "probe", "robots", "homepage"
]

DEFAULT_SOURCES: tuple[SourceName, ...] = ("sitemap", "robots", "homepage", "feed")
"""What `/v1/map` uses unless asked otherwise: the four that only ever talk to
the target site itself, are fast, and cannot be rate-limited by a third party.

The external indexes (`cc`, `wayback`, `crt`) and `probe` are opt-in rather than
default because each changes the request's character -- the first three send the
caller's target domain to a third party, and `probe` sends dozens of speculative
requests to a site that did not ask for them. Both are reasonable things to do
deliberately and wrong things to do by surprise."""

ALL_SOURCES: tuple[SourceName, ...] = (
    "sitemap", "robots", "homepage", "feed", "cc", "wayback", "crt", "probe",
)  # fmt: skip

DEFAULT_SOURCE_TIMEOUT = 30.0
"""Per source. A source that has not answered in thirty seconds is not going to
change the shape of the result enough to justify the caller waiting for it."""


@dataclass
class SourceOutcome:
    urls: list[str] = field(default_factory=list)
    hosts: set[str] = field(default_factory=set)
    feed_entries: list[feeds.FeedEntry] = field(default_factory=list)
    """Kept distinct from `urls` because entries carry publication dates, which
    `scorers.freshness` is the only consumer of and no other source can supply."""
    feed_hints: tuple[str, ...] = ()
    """Feed URLs a source *declared* without fetching -- only `homepage` produces
    these, from `<link rel="alternate">`. Separate from `urls` because a feed
    document is not a page a caller asked to map; it is a pointer the `feed`
    source then follows."""
    error: str | None = None
    """`"timeout"`, or the exception's text. `None` on success -- including a
    successful source that happened to find nothing, which is a different thing
    and is visible as `error is None` with an empty `urls`."""


@dataclass
class SourceReport:
    outcomes: dict[str, SourceOutcome] = field(default_factory=dict)

    @property
    def urls(self) -> list[str]:
        """Every URL found, in source order, deduplicated on the raw string.

        Deliberately *not* normalized here -- `seed.py` owns normalization, and
        doing it in two places is how two callers end up disagreeing about
        whether a trailing slash matters."""

        seen: set[str] = set()
        out: list[str] = []
        for outcome in self.outcomes.values():
            for url in outcome.urls:
                if url not in seen:
                    seen.add(url)
                    out.append(url)
        return out

    @property
    def hosts(self) -> set[str]:
        return {host for outcome in self.outcomes.values() for host in outcome.hosts}

    @property
    def feed_entries(self) -> list[feeds.FeedEntry]:
        return [
            entry for outcome in self.outcomes.values() for entry in outcome.feed_entries
        ]

    def summary(self) -> dict[str, str]:
        """One line per source for the log and for `/v1/map`'s diagnostics: a
        count, or why there is none."""

        return {
            name: (f"error: {outcome.error}" if outcome.error else str(len(outcome.urls)))
            for name, outcome in self.outcomes.items()
        }


@dataclass(frozen=True)
class SourceContext:
    base_domain: str
    origin: str
    seed_url: str
    policy: EgressPolicy
    max_urls_per_source: int = 10_000
    include_subdomains: bool = True
    source_timeout: float = DEFAULT_SOURCE_TIMEOUT
    fingerprint: soft404.Soft404Fingerprint | None = None
    """Only `probe` uses it, and only usefully -- see `probe.probe_paths`."""
    declared_feed_urls: tuple[str, ...] = ()
    """Feed URLs the homepage declared. Filled in by `gather` when `homepage` runs
    before `feed` -- see `_PRE_SOURCES`."""


_PRE_SOURCES: frozenset[SourceName] = frozenset({"homepage", "robots"})
"""Run before the rest, and before `feed` in particular.

Both discover *where to look*: the homepage declares feed URLs in
`<link rel="alternate">`, and robots.txt declares sitemaps. Running them
concurrently with the sources that consume their output would mean guessing
conventional paths at a site that was about to tell us the real ones. They are
fast (one request each, to the target site) so the serialization costs little.
"""


async def gather(
    names: tuple[SourceName, ...],
    ctx: SourceContext,
) -> SourceReport:
    """Run the named sources and collect what each found.

    Never raises. A source that fails is recorded in its `SourceOutcome.error`
    and the rest proceed.
    """

    report = SourceReport()

    pre = tuple(name for name in names if name in _PRE_SOURCES)
    rest = tuple(name for name in names if name not in _PRE_SOURCES)

    if pre:
        await _run_batch(pre, ctx, report)
        # The homepage may have named the site's real feed. Hand it to `feed`,
        # which would otherwise guess conventional paths at a site that already
        # answered the question.
        ctx = _with_declared_feeds(ctx, report)

    if rest:
        await _run_batch(rest, ctx, report)

    log.info("crawl.sources.gathered", domain=ctx.base_domain, **report.summary())
    return report


def _with_declared_feeds(ctx: SourceContext, report: SourceReport) -> SourceContext:
    homepage = report.outcomes.get("homepage")
    declared = tuple(homepage.feed_hints) if homepage is not None else ()
    if not declared:
        return ctx
    from dataclasses import replace

    return replace(ctx, declared_feed_urls=declared)


async def _run_batch(
    names: tuple[SourceName, ...], ctx: SourceContext, report: SourceReport
) -> None:
    async def run(name: SourceName) -> tuple[SourceName, SourceOutcome]:
        try:
            outcome = await asyncio.wait_for(
                _dispatch(name, ctx), timeout=ctx.source_timeout
            )
        except TimeoutError:
            log.warning("crawl.sources.timeout", source=name, domain=ctx.base_domain)
            return name, SourceOutcome(error="timeout")
        except Exception as exc:  # noqa: BLE001 -- recorded, never propagated
            log.warning(
                "crawl.sources.failed", source=name, domain=ctx.base_domain, error=str(exc)
            )
            return name, SourceOutcome(error=str(exc))
        return name, outcome

    for name, outcome in await asyncio.gather(*(run(name) for name in names)):
        report.outcomes[name] = outcome


async def _dispatch(name: SourceName, ctx: SourceContext) -> SourceOutcome:
    if name == "sitemap":
        return await _sitemap(ctx)
    if name == "robots":
        return await _robots(ctx)
    if name == "homepage":
        return await _homepage(ctx)
    if name == "feed":
        return await _feed(ctx)
    if name == "cc":
        return SourceOutcome(
            urls=await cc.fetch_urls(
                ctx.base_domain,
                ctx.policy,
                max_urls=ctx.max_urls_per_source,
                timeout_seconds=ctx.source_timeout,
                include_subdomains=ctx.include_subdomains,
            )
        )
    if name == "wayback":
        urls = await wayback.fetch_urls(
            ctx.base_domain,
            ctx.policy,
            max_urls=ctx.max_urls_per_source,
            timeout_seconds=ctx.source_timeout,
            include_subdomains=ctx.include_subdomains,
        )
        return SourceOutcome(urls=urls, hosts=_hosts_of(urls, ctx.base_domain))
    if name == "crt":
        return SourceOutcome(
            hosts=await crt.fetch_hosts(
                ctx.base_domain, ctx.policy, timeout_seconds=ctx.source_timeout
            )
        )
    if name == "probe":
        return SourceOutcome(
            urls=await probe.probe_paths(
                ctx.origin,
                ctx.policy,
                timeout_seconds=min(ctx.source_timeout, 10.0),
                fingerprint=ctx.fingerprint,
            )
        )
    raise ValueError(f"unknown discovery source {name!r}")


async def _sitemap(ctx: SourceContext) -> SourceOutcome:
    return SourceOutcome(
        urls=await sitemap.fetch_urls(
            sitemap.default_sitemap_url(ctx.origin), ctx.policy
        )
    )


async def _robots(ctx: SourceContext) -> SourceOutcome:
    """robots.txt as a *source*, not as a policy: the `Sitemap:` lines.

    Many sites declare their sitemap only here and never at `/sitemap.xml`, so
    for those this is the difference between mapping the site and mapping its
    homepage.
    """

    parser = await robots.fetch(ctx.origin, ctx.policy)
    if parser is None:
        return SourceOutcome()
    urls: list[str] = []
    for declared in parser.site_maps() or []:
        urls.extend(await sitemap.fetch_urls(declared, ctx.policy))
    return SourceOutcome(urls=urls)


async def _homepage(ctx: SourceContext) -> SourceOutcome:
    """The seed page's own links, plus any feed it declares.

    This is what `seed.py` already did inline; it is a source like any other now,
    and it additionally reports `<link rel="alternate">` feed URLs so `feed` does
    not have to guess.
    """

    from agentpilot.crawl import link_extractor

    html = await _fetch_html(ctx.seed_url, ctx.policy, ctx.source_timeout)
    if html is None:
        return SourceOutcome()
    outcome = SourceOutcome(urls=link_extractor.extract_links(html, ctx.seed_url))
    outcome.feed_hints = _declared_feeds(html, ctx.seed_url)
    return outcome


async def _feed(ctx: SourceContext) -> SourceOutcome:
    entries = await feeds.discover(
        ctx.origin,
        ctx.policy,
        declared_urls=ctx.declared_feed_urls,
        timeout_seconds=min(ctx.source_timeout, 10.0),
    )
    return SourceOutcome(urls=[entry.url for entry in entries], feed_entries=entries)


async def _fetch_html(url: str, policy: EgressPolicy, timeout: float) -> str | None:
    import httpx

    from crawlpilot.egress.httpx_guard import guarded_get
    from crawlpilot.spi.errors import EgressBlocked

    try:
        response = await guarded_get(
            url, policy, timeout=min(timeout, 15.0), follow_redirects=True
        )
    except (EgressBlocked, httpx.HTTPError):
        return None
    if response.status_code >= 400 or "html" not in response.headers.get("content-type", ""):
        return None
    return response.text


def _declared_feeds(html: str, base_url: str) -> tuple[str, ...]:
    from urllib.parse import urljoin

    from lxml import html as lxml_html

    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return ()
    out: list[str] = []
    for link in root.cssselect('link[rel~="alternate"]'):
        href = link.get("href")
        kind = (link.get("type") or "").lower()
        if href and ("rss" in kind or "atom" in kind or "xml" in kind):
            resolved = urljoin(base_url, href)
            if resolved not in out:
                out.append(resolved)
    return tuple(out)


def _hosts_of(urls: list[str], base_domain: str) -> set[str]:
    from urllib.parse import urlparse

    hosts: set[str] = set()
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue
        if host and (host == base_domain or host.endswith(f".{base_domain}")):
            hosts.add(host)
    return hosts
