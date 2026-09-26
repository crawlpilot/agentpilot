# Discovery

How `/v1/map` and `/v1/crawl` decide which URLs exist and which of them to fetch
first.

Before Phase 3 there were two sources — the site's sitemap and the seed page's
links — and the frontier was ordered by whatever the HTML parser saw first. There
are now eight sources, each with its own deadline, and the frontier is ranked.

---

## The sources

Named on `/v1/map` with `sources`. The default four only ever talk to the target
site:

| Source | What it reads | Finds |
|---|---|---|
| `sitemap` | `/sitemap.xml` | What the site publishes about itself |
| `robots` | `Sitemap:` lines in `robots.txt` | Sitemaps at non-standard paths — many sites declare them only here |
| `homepage` | The seed page's `<a href>` links, and its `<link rel="alternate">` feeds | The navigable site |
| `feed` | RSS/Atom, declared or at conventional paths | Content pages, *with publication dates* |

The other four are **opt-in**, because each changes what the request does rather
than only how well it works:

| Source | What it reads | Why it's opt-in |
|---|---|---|
| `crt` | Certificate Transparency logs (crt.sh) | Sends your target domain to a third party. Usually the largest single win on a domain-wide map: every subdomain ever put behind HTTPS, including ones never linked. |
| `cc` | The Common Crawl index | Third-party disclosure. Finds pages the site never linked and never sitemapped. |
| `wayback` | The Internet Archive CDX API | Third-party disclosure. Richest source for a long-lived site, and by far the noisiest. |
| `probe` | Conventional paths (`/about`, `/pricing`, …) | Sends dozens of speculative requests to a site that didn't ask for them. |

```json
POST /v1/map
{ "url": "https://example.com",
  "sources": ["sitemap", "robots", "homepage", "feed", "crt", "cc"],
  "search": "pricing enterprise",
  "include_metadata": true }
```

### Independent deadlines

Each source gets its own `source_timeout` (default 30s), not a share of one
overall budget. A source that times out contributes nothing, is reported, and
does not slow the request down — this is why crt.sh being slow does not make
`/v1/map` slow. `timeout` still bounds the whole call and returns 408 on expiry.

A source that fails — rate-limited, unreachable, or answering with an HTML error
page under a 200, which crt.sh really does — contributes nothing rather than
failing the request. Per-source counts and errors go to the log under
`crawl.sources.gathered`.

---

## Result quality

Three filters run over what the sources return. All three are on by default, and
they matter roughly in proportion to how many opt-in sources you enabled — the
archive and index sources return a great deal of junk that a sitemap never does.

**`filter_nonsense`** drops site machinery: assets, webpack chunks,
`/_next/` paths, `robots.txt`, archived `sitemap.xml`s, dotfiles, and the
URLs-with-encoded-newlines the CDX API returns.

**`detect_soft_404`** asks the site for a URL that cannot exist, keeps what it
answers, and drops later results that match. Single-page apps return the same 200
shell for every path, so without this a `probe` run reports every path it tried as
a real page — output that is a copy of its input, which is worse than nothing
because it looks like data. Costs one request per origin.

**`include_metadata`** (off by default) fetches each result's `<title>` and
description with a bounded 16 KB range request, and doubles as a liveness check —
URLs an index remembers and the site no longer serves are dropped. Off by default
because it costs one request per returned URL: worth it for a map a human reads,
wasteful for a hundred thousand feeding a pipeline.

---

## Ranking

`search` on `/v1/map` and `query` on `/v1/crawl` order results by a 0–1 composite
of four signals, all computed from the URL string alone — the decision has to be
made *before* fetching or it isn't a prioritization:

| Signal | Weight | What it reads |
|---|---|---|
| Relevance | 0.45 | Fraction of query terms in the host and path |
| Depth | 0.25 | Closeness to 2 path segments, falling off both ways |
| Shape | 0.20 | Content-ish segments up; `/tag/`, `/page/`, long query strings, numeric endings down |
| Freshness | 0.10 | Exponential decay on publication date, 180-day half-life |

Two non-obvious choices:

- **Depth peaks in the middle, not at the root.** A site's homepage and top-level
  sections are mostly navigation; the articles live two or three segments in. An
  eight-segment URL is usually a faceted-search permutation. Both extremes lose.
- **A missing date scores 0.5, not 0.** Only feeds and sitemaps supply dates.
  Scoring the rest at zero would rank every sitemap URL below every feed URL for a
  reason that is about the source rather than the page.

`MapLinkOut.score` returns the number, so the order is inspectable rather than
something to trust.

### Why this changes crawl results

`/v1/crawl` now ranks the frontier by default (`score_urls: true`). This is not
cosmetic. `limit: 500` against a 50,000-page site returns whichever 500 URLs the
frontier ordered first — and in DOM order that is the navigation, the footer, and
the cookie policy, because those are the first links in the markup on nearly
every page. Set `score_urls: false` for the previous first-seen behaviour.

Ranking also now happens **before** the cap, not after. Previously the cap was
applied while collecting candidates, so `search` only ever reordered the first
`limit` URLs discovery happened to find — with `limit: 2` the page you searched
for was often never in the candidate set at all.

---

## Egress

Every fetch here goes through the same SSRF guard as a scrape
(`crawlpilot.egress.httpx_guard`): DNS is resolved first and every resulting IP
checked against the metadata (169.254/16) and RFC1918 ranges before connecting.

No allowlist entry was needed for the external sources. The guard is about IP
ranges, not hostnames — `index.commoncrawl.org`, `crt.sh` and
`web.archive.org` resolve to public addresses and pass it like any other host.

---

## Known limits

- **Subdomain hosts are discovered but not yet scanned.** `crt` and `wayback`
  report hosts, and `probe.resolve_hosts` confirms which resolve, but `/v1/map`
  does not yet run a per-subdomain sitemap/homepage pass. So `crt` currently
  widens the *URL* set only through what `wayback`/`cc` already knew about those
  hosts.
- **Documents are still dropped.** `nonsense` deliberately keeps `.pdf`,
  `.docx` and `.csv`, but `crawl.filters._DENIED_EXTENSIONS` drops them
  independently, with its own "no pdf/document engine yet" note. Both lists have
  to change together.
- **The registrable-domain heuristic is still last-two-labels.** `example.co.uk`
  collapses to `co.uk`, so subdomain scoping is wrong on multi-part TLDs. Needs a
  public-suffix list; unchanged by this work.
