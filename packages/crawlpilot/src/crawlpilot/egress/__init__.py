"""Fail-closed network egress: the SSRF and cloud-metadata guard.

Multi-tenant browsers and the httpx fast-path tier run untrusted third-party
pages, so neither may reach cloud metadata endpoints or RFC1918 space, and the
check has to survive DNS rebinding -- which is why validation happens *after*
resolution, against the address actually being connected to.

Both modules are published: `policy` decides what is reachable, `httpx_guard`
enforces it on an outbound request. A consumer fetching a URL on the platform's
behalf -- `agentpilot.crawl` does, for robots.txt and sitemaps -- must go
through `guarded_get` rather than a bare httpx call, and that is the whole reason
this package is part of the published surface rather than an internal detail.
"""

from __future__ import annotations

from crawlpilot.egress import httpx_guard, policy

__all__ = ["httpx_guard", "policy"]
