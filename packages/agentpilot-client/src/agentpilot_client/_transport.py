"""The lower layer: one authenticated HTTP conversation with a gateway.

`AgentPilot` is the 90% case; this is the assembly layer under it, injectable
for the same reason `Browser` takes a driver -- it is what lets a consumer test
against a fake gateway without running one, and what keeps the facade free of
retry and error-decoding logic.

Everything about *what* the endpoints mean lives in `resources/`. What lives
here is what every call shares: the credential, the base URL, turning a
`{success: false, code, error}` envelope back into the exception it names, and
honouring `Retry-After` on the two statuses where retrying is the expected
response rather than a gamble.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from crawlpilot.extensions import compare_api_versions
from crawlpilot.extensions.manifest import Compatibility
from crawlpilot.spi.errors import DriverError, error_from_wire
from crawlpilot.wire import WIRE_API_VERSION

DEFAULT_TIMEOUT = 120.0
"""Generous, because the operations behind it are: a protected scrape climbs a
tier ladder and a session open may wait for a warm context. A caller who wants
to fail fast passes their own."""

_PLACEHOLDER_TENANT = "-"
"""Every `/v1` route overwrites `tenant` from the authenticated key
(`req.model_copy(update={"tenant": authed.tenant})`), so the field is required
by the schema but its value is never read. The client therefore takes no
`tenant` argument at all -- offering one would imply a caller could act for
another tenant, which the gateway exists to prevent. This is what goes on the
wire to satisfy the schema."""


class IncompatibleServer(RuntimeError):
    """The gateway speaks a wire version this client cannot.

    Raised up front, from the capability handshake, rather than letting a call
    fail somewhere deep with a schema rejection that names a field instead of
    the real problem.
    """


class Transport:
    """Auth, retries and error decoding over one gateway."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._capabilities: dict[str, Any] | None = None

    @property
    def api_key(self) -> str:
        return self._api_key

    async def aclose(self) -> None:
        # An injected client belongs to whoever injected it.
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------- requests

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        """One call, with the envelope decoded and errors raised as exceptions.

        `retry` is off for anything non-idempotent. `execute` in particular must
        never be retried: the batch may have run and changed the page before the
        response was lost, and running it twice is worse than failing once.
        """

        attempt = 0
        while True:
            response = await self._client.request(
                method, self.url(path), json=json, params=params
            )
            if response.status_code < 400:
                return _decode(response)

            wait = _retry_after(response) if retry else None
            if wait is None or attempt >= self._max_retries:
                raise _error_from(response)
            await asyncio.sleep(wait)
            attempt += 1

    # -------------------------------------------------------- capabilities

    async def capabilities(self) -> dict[str, Any]:
        """Fetched once per client, lazily, and cached.

        Lazy because a caller who never makes a request should never make this
        one either; cached because it describes the deployment, which does not
        change under a running client.
        """

        if self._capabilities is None:
            self._capabilities = await self.request("GET", "/v1/capabilities", retry=False)
            self._check_compatible(self._capabilities)
        return self._capabilities

    def _check_compatible(self, capabilities: dict[str, Any]) -> None:
        """Reuses the extension loader's policy rather than inventing a second.

        Its asymmetry is exactly right here: a server older than this client
        loads with a warning (it simply lacks verbs we know about), a server
        *newer* is refused, because we cannot know what we would be getting
        wrong. See `crawlpilot.extensions.manifest.compare_api_versions`.
        """

        theirs = capabilities.get("wire_api")
        verdict = compare_api_versions(
            theirs, WIRE_API_VERSION, subject=f"server at {self.base_url}"
        )
        if verdict.compatibility is Compatibility.REFUSE:
            raise IncompatibleServer(verdict.reason)


# ----------------------------------------------------------------- decoding


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _retry_after(response: httpx.Response) -> float | None:
    """Only retry where the server said to.

    The gateway sets `Retry-After` on exactly the two codes where waiting is the
    expected response -- a contended lease and a full fleet, both of which
    resolve on their own (`spi.errors`' `retry_after_seconds`). Retrying
    anything else is guessing, and guessing on a browser action is how you
    double-submit a form.
    """

    if response.status_code not in (409, 503):
        return None
    header = response.headers.get("Retry-After")
    if header is None:
        return None
    try:
        return max(0.0, float(header))
    except ValueError:
        return None


def _error_from(response: httpx.Response) -> Exception:
    """The `{success, code, error}` envelope -> the exception it names.

    The mapping is crawlpilot's (`spi.errors.error_from_wire`), not a copy kept
    here: the codes are declared on the exception classes, so this client raises
    `StaleRefError` for `STALE_REF` without either side maintaining a table. An
    unrecognised code still yields a catchable `DriverError` carrying the
    server's own message, which is what lets a client one release behind its
    server keep working.
    """

    body = _decode(response)
    if isinstance(body, dict) and body.get("code"):
        error = error_from_wire(str(body["code"]), str(body.get("error") or ""))
        details = body.get("details")
        if details is not None:
            error.details = details  # type: ignore[attr-defined]
        return error
    return DriverError(f"HTTP {response.status_code}: {response.text[:500]}")
