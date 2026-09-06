"""`TargetSpec` matching -- the guard that replaced v1's navigate target.

A run whose URL matches nothing is rejected **before a browser is opened**,
which is the cheapest possible failure and the reason this is a pure function
rather than a step in replay.

The same predicate, read from the other end, is how a domain agent routes a URL
to the right recipe among several bound to it.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from urllib.parse import urlsplit

from agentpilot.recipe.v2.models import TargetSpec, UrlMatcher

_REGEX_CACHE: dict[str, re.Pattern[str]] = {}


def matcher_matches(matcher: UrlMatcher, url: str) -> bool:
    """A malformed matcher does not match, and does not raise. A stored recipe
    with a bad pattern should decline the URL, not crash the worker that
    happened to pick it up."""

    if matcher.kind == "glob":
        return fnmatch(url, matcher.pattern)
    if matcher.kind == "host":
        host = (urlsplit(url).hostname or "").lower()
        want = matcher.pattern.lower().lstrip("*.")
        return host == want or host.endswith("." + want)
    if matcher.kind == "regex":
        compiled = _REGEX_CACHE.get(matcher.pattern)
        if compiled is None:
            try:
                compiled = re.compile(matcher.pattern)
            except re.error:
                return False
            _REGEX_CACHE[matcher.pattern] = compiled
        return compiled.search(url) is not None
    return False


def target_accepts(target: TargetSpec, url: str) -> bool:
    """An empty `match` means "applies to any URL" -- legitimate for a generic
    recipe, suspicious for a site-specific one, and the studio warns rather
    than the contract forbidding it."""

    if not target.match:
        return True
    return any(matcher_matches(m, url) for m in target.match)
