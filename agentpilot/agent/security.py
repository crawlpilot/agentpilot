"""Agent-level safety helpers (D9): a URL allowlist for navigation and
placeholder-based sensitive-data handling.

`substitute_secrets` replaces placeholders with real secrets only in the copy
of an action handed to the driver; the *recorded* action keeps the placeholder
form, so secrets never enter the persisted step history by construction.
`redact_secrets` is the belt-and-suspenders scrub for text that can still
surface a real value -- e.g. a fill's read-back verification or an error
message -- before it reaches the model or the history.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_url_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """An empty allowlist means unrestricted (opt-in gate). A pattern matches
    its exact host and any subdomain: `example.com` and `*.example.com` both
    allow `example.com` and `app.example.com`; `sub.example.com` allows only
    that host and below."""

    if not allowed_domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    for pattern in allowed_domains:
        base = pattern.lower().lstrip("*.")
        if base and (host == base or host.endswith("." + base)):
            return True
    return False


def substitute_secrets(text: str, sensitive_data: dict[str, str] | None) -> str:
    """Replace each placeholder with its real secret (dispatch-side only)."""

    if not sensitive_data:
        return text
    for placeholder, secret in sensitive_data.items():
        text = text.replace(placeholder, secret)
    return text


def redact_secrets(text: str, sensitive_data: dict[str, str] | None) -> str:
    """Replace each real secret with its placeholder before text is stored or
    shown to the model -- the inverse of `substitute_secrets`."""

    if not sensitive_data:
        return text
    for placeholder, secret in sensitive_data.items():
        if secret:
            text = text.replace(secret, placeholder)
    return text
