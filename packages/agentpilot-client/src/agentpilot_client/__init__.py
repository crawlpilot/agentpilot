"""agentpilot-client -- an agentpilot fleet, as the same object as a local browser.

    from agentpilot_client import AgentPilot

    with AgentPilot(api_key=KEY) as ap:
        print(ap.scrape("https://example.com").markdown)

The point of this package is what it does *not* contain. The browser verbs, the
action models, the result decoding, the `Document` type and the exceptions all
come from `crawlpilot`, which the server uses too -- so the two cannot disagree
about a shape, and a verb added to `crawlpilot/tools/catalog.py` reaches this
client with no edit here. What is here is transport, auth, job polling and
ergonomics.

That is what makes this true:

    def flow(cp):                        # one body
        with cp.session() as page:
            page.navigate("https://example.com")
            return page.get_title(), page.is_visible("h1")

    flow(Crawlpilot())                   # a browser on this machine
    flow(AgentPilot(api_key=KEY))        # a browser on the fleet

`AsyncAgentPilot` is the same object with `await` on each call, for when you
already have an event loop.

Errors are `crawlpilot.spi.errors` -- `StaleRefError`, `CapacityExhausted`,
`NavigationTimeout` -- raised from the server's `{code, error}` envelope, so the
`except` clauses you write against a local browser catch the remote one too.
"""

from agentpilot_client._transport import IncompatibleServer, Transport
from agentpilot_client.client import AgentPilot, AsyncAgentPilot
from agentpilot_client.resources.agent import AgentRun, AgentRunFailed
from agentpilot_client.resources.crawl import CrawlFailed, CrawlJob
from agentpilot_client.resources.maps import Link
from agentpilot_client.session import RemoteSession
from crawlpilot.spi.errors import (
    CapacityExhausted,
    ChallengeDetected,
    DriverError,
    LeaseConflict,
    NavigationTimeout,
    StaleRefError,
    WaitTimeout,
)
from crawlpilot.spi.scrape import Document

__version__ = "0.1.0"

__all__ = [
    "AgentPilot",
    "AgentRun",
    "AgentRunFailed",
    "AsyncAgentPilot",
    "CapacityExhausted",
    "ChallengeDetected",
    "CrawlFailed",
    "CrawlJob",
    "Document",
    "DriverError",
    "IncompatibleServer",
    "LeaseConflict",
    "Link",
    "NavigationTimeout",
    "RemoteSession",
    "StaleRefError",
    "Transport",
    "WaitTimeout",
    "__version__",
]
