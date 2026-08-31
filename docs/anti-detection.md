# Anti-detection

What the library does to avoid being blocked, and what you have to decide.

Most of this page is the record of an actual debugging session against a
PerimeterX-protected retail site, where the script was served a *"Robot or
human?"* wall on every attempt. The causes were, in order of how much they
mattered: running headless without meaning to, no residential exit, and
detection being off so the wall came back looking like content.

## Tiers

`tier=` decides how much effort goes into not being detected.

| Tier | What it does |
|---|---|
| `basic` | plain HTTP first, no browser; escalates to `stealth` on a hard wall |
| `stealth` | pinned fingerprint, human warm-up, slow interaction cadence, block detection, residential proxy requested |
| `enhanced` | the above plus a real window and full Runtime access |
| `auto` | **the default** — starts at `stealth`, climbs to `enhanced` on a wall |

`auto` is the default because it is both the fastest and the most robust: it
only pays for escalation when something actually blocks. Each rung of the ladder
mints a *fresh identity*, so a retry gets a new proxy and a new fingerprint
rather than hammering the same one.

An explicitly requested tier does **not** auto-escalate — you chose it.

## Headful

```python
Crawlpilot(headful=True)
```

Not cosmetic. Headless Chrome is a strong bot signal on its own, and it is what
got the retail scrape walled on every attempt. Headful also gives the driver an
OS-level input path it does not otherwise have.

Set it on the client, not the session: `scrape()` derives headful from the tier
rung it is on, and the first rung is headless — so a session-level flag is
ignored by exactly the call that usually succeeds.

`None` (the default) means "headful if a display exists". `True` still degrades
to headless where there is no display rather than failing to launch, so it is
safe in CI and over SSH.

## Proxies

```python
Crawlpilot(proxy="http://user:pass@gateway:8080", proxy_country="US")
```

The single biggest factor on a scored site. Protected tiers *ask* for a
residential exit; with no proxy configured every request leaves from your own
IP, and a home or datacenter address hitting a retail site repeatedly is the
dominant block signal.

Pass a URL. The client builds the `ProxyPinner`, its in-memory state store and
the `ProxyEndpoint` behind it. Endpoints are pinned per identity, so a returning
identity keeps its exit IP.

Several is fine — `proxy=["http://a:8080", "http://b:8080"]` — and the pick is a
deterministic hash of the identity, so it needs no shared counter.

## Block detection

```python
Crawlpilot(detect_blocks=True)   # the default here
```

Two things happen when this is on, and both matter:

1. The warm-up **waits for Akamai's `_abck` cookie to validate** before you
   read. Off, the warm-up still scrolls and drifts the pointer but never waits —
   so on an Akamai target the warm-up accomplishes nothing.
2. Every navigation is **classified**. A hard wall raises `ChallengeDetected`; a
   soft verdict (`too_small`, `rate_limited`, `wrong_geo`) lands on
   `ActionResult.soft_verdict` with the content still returned.

Off, a CAPTCHA interstitial is extracted and handed back as if it were the page
you asked for, and the run reports success.

It defaults **off** on a bare `Browser.session()` and **on** for the client.
That is deliberate: an agent run passes through blank pages, SPA shells and
post-click transitions where `EMPTY`/`TOO_SMALL` are the expected state, and a
session has no escalation ladder to answer a raised challenge with. A caller who
came here to fetch a page wants to be told.

## Which browser

Real Chrome by default. Bundled Chromium is a bot tell in its own right — the
UA, missing Widevine and proprietary codecs, a different `navigator.plugins` —
and the pinned fingerprint describes a *Chrome* build.

```python
Crawlpilot(channel="chromium")   # override when you must
```

You must on arm64: there is no arm64 Chrome build to install. Note that the
bundled Chromium unpacks on macOS as *"Google Chrome for Testing.app"*, Chrome
branding and all, so it looks like your own Chrome in the dock. Check the
`driver.browser_launched` log line for the binary that actually started.

## Site-specific knowledge

The library ships none, on purpose — an import-linter contract keeps
site-specific policy out of the browser layer. It arrives as an extension:

```python
from agentpilot.control.retail_extension import RetailExtension

Crawlpilot(extensions=[RetailExtension()])
```

`RetailExtension` is the reference implementation, contributing Walmart, Amazon,
JD and fashion-retail block signals — a landed `/blocked` URL, a per-page-type
size floor — through the ordinary `BlockMount` seam, with no privileged path.
Write your own the same way; see `crawlpilot.extensions`.

Extensions can also *act* on a wall through the `resolve` hook, which returns
`SOLVED` / `RETRY` / `ESCALATE` / `GIVE_UP`. That hook runs on the `scrape()`
path, where there is a ladder to act on the answer.

## A caution about detection markers

A vendor's name in the page body proves the site is *protected*, not that this
response is a *block*. A served Walmart product page carries `*.perimeterx.net`
in its CSP allowlist and `"perimeterX":{"enable":true}` in its bootstrap JSON.
Matching on the bare vendor name classified every successful product page as a
robot check — a PRIVACY-scope verdict that burns the identity, rotates the
proxy, and climbs the whole ladder on a page that had already been fetched
successfully.

So in `block_detect`, unambiguous challenge markers (`px-captcha`, the
challenge's own prompt text) are ungated, while vendor names and ordinary-English
phrases only count on a page too short to be real content. If you add markers of
your own, follow the same split — and capture the real wall first. The
`"Press & Hold"` wording everyone knows the challenge by does not appear on the
live page at all; it says *"Activate and hold the button"*.

## Identities and profiles

```python
with cp.session(identity="shopper", ...) as page:
    ...
```

An `identity` is an opaque scope handle, not a domain. It keeps a profile dir,
a pinned proxy and a fingerprint together, so repeat visits read as a returning
visitor rather than a first-time one. It only survives across runs if
`profiles_root` does — otherwise the client uses a temp dir and deletes it on
close, which is right for a one-shot crawl and wrong for a warmed identity.

Identities accrue *burn*: soft verdicts add minor warnings, hard walls add
weighted ones, and a burned identity is retired — profile deleted, proxy
rotated. A poisoned cookie jar is a real cause of persistent blocking, so if one
identity keeps getting walled, delete its profile directory.
