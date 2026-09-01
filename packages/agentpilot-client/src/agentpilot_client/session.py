"""A session on a remote fleet, driven with the local vocabulary.

This is the file the whole integration was aimed at, and it is short on purpose.
`SessionVerbs` (in crawlpilot) owns ~60 browser verbs over one abstract
`execute`; this supplies `execute` and inherits every one of them. No verb is
restated here, and adding one to `tools/catalog.py` reaches this class without
an edit.

    with Crawlpilot() as cp:            # local browser
    with AgentPilot(api_key=K) as cp:   # remote fleet
        with cp.session() as page:
            page.navigate(url)
            page.click("#buy")
            assert page.is_visible("#cart")

Both bodies are the same because both objects are the same object.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.actions import ActionResult
from crawlpilot.tools import ToolRegistry, ToolSpec, browser_tools
from crawlpilot.verbs import SessionVerbs
from crawlpilot.wire import from_wire

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport


class RemoteSession(SessionVerbs):
    """One open session on a worker, addressed by id."""

    def __init__(
        self,
        transport: Transport,
        session_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._transport = transport
        self._session_id = session_id
        self._metadata = metadata or {}
        self._tools = tools or browser_tools()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def tier(self) -> str:
        """Which tier actually served this session, which is not always the one
        asked for -- `auto` climbs the ladder."""

        return str(self._metadata.get("tier_used", ""))

    @property
    def node_id(self) -> str:
        """Which fleet node holds this session's Chrome. A session is physically
        pinned to it, which is why the gateway proxies rather than redirects."""

        return str(self._metadata.get("node_id", ""))

    @property
    def tools(self) -> ToolRegistry:
        """What the *server* said it can dispatch, not what this build ships.

        Built from `GET /v1/capabilities`, so an extension's verb is callable
        through `call_tool` -- and through `SessionVerbs.__getattr__` -- even
        though this client's own `CATALOG` has never heard of it.
        """

        return self._tools

    # ------------------------------------------------------------- transport

    async def execute(
        self, actions: Sequence[spi_actions.Action], *, page_id: str | None = None
    ) -> ActionResult:
        """The one method a transport supplies. Everything else is inherited.

        Never retried, even on a `Retry-After`: a batch that reached the browser
        may have clicked something before the response was lost, and running it
        twice is worse than failing once.
        """

        payload = await self._transport.request(
            "POST",
            f"/v1/sessions/{self._session_id}/execute",
            json={
                "actions": [_wire_action(action, self._tools) for action in actions],
                "page_id": page_id,
            },
            retry=False,
        )
        return from_wire(payload)

    # ------------------------------------------------------ escape hatches

    def cdp_url(self) -> str:
        """The gateway's Chrome-shaped discovery endpoint for this session.

        Hand it to a local `Crawlpilot(cdp_url=..., cdp_headers=...)` and you get
        the full in-process object driving this same remote browser -- useful for
        debugging, or for running a Playwright-shaped script against the fleet.
        Needs the session to have been opened with `enable_cdp=True`.

        The wire path this class uses is the better default: one round trip per
        *batch* rather than per call, and the server's tier ladder and egress
        guard still apply. See `docs/client.md`.
        """

        return self._transport.url(f"/v1/sessions/{self._session_id}/cdp/json/version")

    def cdp_headers(self) -> dict[str, str]:
        """What `cdp_url()`'s discovery request needs. The websocket it returns
        carries its own credential in the query string."""

        return {"Authorization": f"Bearer {self._transport.api_key}"}

    def live_view_url(self) -> str:
        """A websocket that streams this session's screen. Browsers cannot set
        headers on a handshake, so the credential travels as a query parameter --
        the same convention `routes/live_view.py` expects."""

        base = self._transport.base_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/v1/sessions/{self._session_id}/live-view?api_key={self._transport.api_key}"

    async def close(self) -> None:
        """Release the session back to IDLE. The reaper destroys the context on
        its own schedule; this just gives up the lease."""

        await self._transport.request(
            "DELETE", f"/v1/sessions/{self._session_id}", retry=False
        )


def _wire_action(action: spi_actions.Action, tools: ToolRegistry) -> dict[str, Any]:
    """One `spi.actions` dataclass -> the JSON the gateway validates.

    Both sides project from the same `ToolSpec`, so this is a field-for-field
    dump rather than a conversion table -- the thing `gateway/action_conversion`
    stopped needing for the inbound direction, for the same reason.
    """

    spec = _spec_for(action, tools)
    fields = spec.wire_fields or ()
    payload: dict[str, Any] = {"type": spec.name}
    for field in fields:
        value = getattr(action, field, None)
        if value is not None:
            payload[field] = value
    return payload


def _spec_for(action: spi_actions.Action, tools: ToolRegistry) -> ToolSpec:
    for spec in tools:
        if spec.action_cls is type(action):
            return spec
    raise ValueError(
        f"no tool spec for {type(action).__name__}; this client's crawlpilot and "
        "the server's may disagree -- check GET /v1/capabilities"
    )
