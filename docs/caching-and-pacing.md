# Caching and pacing

Two things the platform does between accepting a scrape request and opening a
browser: check whether it already has the page, and wait if it has been asking
this host too often.

Both are on by default. Neither changes what a response looks like, except for
one new field (`cached`) and one new block (`progress`).

---

## The scrape cache

A `/v1/scrape` call or a crawl task whose page is already stored, fresh enough,
and keyed identically is served from Postgres without launching a browser.

### Modes

Set `cache_mode` on `/v1/scrape` or `/v1/crawl` (job-level, alongside
`delay_ms`):

| Mode | Reads | Writes | Use it for |
|---|---|---|---|
| `enabled` *(default)* | ✅ | ✅ | Normal operation |
| `bypass` | ❌ | ✅ | Forcing one URL to refetch while refreshing the entry |
| `read_only` | ✅ | ❌ | A backfill that must not pollute the cache with its own results |
| `write_only` | ❌ | ✅ | Warming the cache ahead of a run |
| `disabled` | ❌ | ❌ | Behaving as though the cache did not exist |

`max_age_ms` sets how old a stored page may be and still be served. Unset takes
the deployment default — one hour, via `AGENTPILOT_CACHE_MAX_AGE_MS`.

Freshness is evaluated **at read time against the caller's own `max_age_ms`**,
not by storing a per-row expiry. A nightly archival crawl and a price checker
disagree about how old is too old, and one stored row serves both.

### What is never cached

Three kinds of request bypass the cache entirely, because the result depends on
something the key cannot faithfully capture. In each case a hit would be *wrong*
rather than merely stale:

- **Pre-extract `actions`.** The page is mutated before extraction — a banner
  dismissed, a "load more" clicked. A cached result silently skips the
  interaction, and a caller cannot tell that from the interaction having had no
  effect.
- **A named `session_name`.** That opts into a persistent profile whose
  accumulated cookies are the point; the second scrape under one name is
  deliberately a *returning visitor*, not a repeat of the first.
- **`screenshot: true`.** The image travels base64-inline on the scrape path and
  as an artifact id on the job path. Neither survives the cache table, so a hit
  would return a document whose screenshot silently went missing.

### The key

A digest over the tenant, the URL, every option that changes the output, and the
request-level fields that do the same without living on `scrape_options`
(`locale`, `timezone_id`, `tier`, `extensions`).

Two properties worth knowing:

- **The cache is per tenant.** Sharing across tenants would be the larger cost
  win. It is also how one tenant's page — fetched behind their cookies, their
  proxy, their locale — becomes another tenant's response. That cannot be
  un-leaked, so it is not the default.
- **`formats` is order-insensitive, `timeout_ms` is not keyed.** Asking for
  `["markdown", "html"]` and `["html", "markdown"]` produces the same document,
  so it hits the same entry; `timeout_ms` decides whether a scrape finishes, not
  what it says, so keying on it would only split the cache into per-timeout
  buckets holding the same page.

**Errors are never stored.** An error document describes one attempt, not the
page — caching it would turn a transient blip into an hour of confidently-served
failures.

### Telling a hit from a fetch

`DocumentOut.cached` is `true` when the document came from the cache. Worth
checking before comparing timings: `metadata.duration_ms` on a hit is the cache
lookup's duration, not the page's.

It is only meaningful on `/v1/scrape`. Crawl documents come back from the
`documents` table, which does not record whether the scrape behind a row was
itself a hit, so `cached` is always `false` there.

---

## Host pacing

The crawler now waits between requests to the same host, at whichever is larger
of:

- `delay_ms` on the crawl request, and
- the host's robots.txt `Crawl-delay`.

The larger, not the caller's value: `Crawl-delay` is a host stating a rate it is
willing to serve, and `delay_ms: 0` is not a way to instruct this service to
ignore it. Both inputs existed before and were read by nobody.

`batch_scrape` jobs have no `delay_ms` but still get the robots delay — a host
that published a limit meant it regardless of which endpoint is asking.

### Backoff

A response of 429, 500, 502, 503 or 504 doubles that host's interval, capped at
32×; a healthy response halves it back down. Two details:

- Halving rather than clearing on success. A host recovering from overload serves
  one request and fails the next; dropping straight back to the base interval
  walks into the same wall.
- A 404 does neither. A missing page is a fact about that URL, not about the
  host's willingness to serve traffic.

Every computed wait carries ±15% jitter, so a worker fleet that all backs off by
the same multiplier does not re-converge into synchronised bursts.

### Across workers

With Redis configured, pacing is shared: N workers pace a host like one crawler.
The decision is a single Lua script (`jobs/lua/acquire_host_slot.lua`) because
the read and the reservation have to be one step — two workers that each read
"next allowed" and then each write it have both concluded they may start now, and
the error grows with the worker count.

Without Redis, pacing is per process and therefore under-paces by roughly the
worker count. Single-worker deployments are unaffected. If Redis goes down
mid-run, pacing falls back to local rather than stalling the fleet, and says so
in the log (`host_limiter.redis_unavailable_pacing_locally`).

Pacing applies only on a cache **miss**. A hit makes no request to the host, so
waiting before serving one would protect nobody.

---

## Admission control

`CrawlWorkerLoop` admitted `max_concurrent` tasks per batch regardless of what
the machine was doing. Each admitted task opens a browser context, and a Chrome
context on a heavy page is hundreds of megabytes, so the failure mode was not
gradual: the box ran out of memory and the OOM killer took the worker, losing
every in-flight lease with it.

It now reads memory pressure and admits fewer tasks when the answer is "not
much": full batch below 70%, half above it, one at a time above 85%. Never zero —
a worker that admits nothing never drains the queue, so the job would stall
silently instead of finishing slowly.

The number comes from the **cgroup**, not `psutil`. Inside a container `psutil`
reports the host's memory: a worker capped at 2 GB on a 64 GB box sees 2% usage
while it is being OOM-killed. Order is cgroup v2, then v1, then
`/proc/meminfo` (using `MemAvailable`, since `MemFree` excludes page cache and
would throttle a healthy Linux box permanently). A platform with none of them —
a macOS dev machine — reports nothing and gets no throttling, which is better
than throttling on a number we do not have.

---

## Crawl progress

`GET /v1/crawl/{id}` gained a `progress` block:

```json
"progress": {
  "queued": 912, "active": 0, "completed": 88, "failed": 3,
  "recent_failures": [{"url": "…", "error": "…", "attempts": 3}]
}
```

`total`/`completed`/`failed` already said how far along a crawl is. This says
what it is *doing*: 912 queued with 0 active is a crawl being paced or starved of
workers, 0 queued with 5 active is one finishing, and the progress counters
cannot tell those apart. The failure sample is capped at ten, newest first — a
crawl that fails four thousand pages fails them for two or three reasons.

---

## Operator knobs

| Variable | Default | Effect |
|---|---|---|
| `AGENTPILOT_CACHE_MAX_AGE_MS` | `3600000` | Default freshness for requests that do not state their own |

The cache lives in `scrape_cache` (migration `0017`), with indexes on
`created_at` and `tenant` for the eviction sweep and per-tenant purge. Nothing
evicts automatically yet — a `DELETE FROM scrape_cache WHERE created_at < now() -
interval '7 days'` on a cron is the current answer.
