"""One module per endpoint family, each owning its own paths and polling.

`AgentPilot` only delegates. Adding a gateway route means adding a resource
here, not editing the facade -- the same reason the gateway itself splits
`routes/` per family rather than growing one module.
"""

from agentpilot_client.resources.agent import AgentResource, AgentRun
from agentpilot_client.resources.crawl import CrawlJob, CrawlResource
from agentpilot_client.resources.maps import Link, MapResource
from agentpilot_client.resources.recipes import RecipeResource
from agentpilot_client.resources.scrape import ScrapeResource

__all__ = [
    "AgentResource",
    "AgentRun",
    "CrawlJob",
    "CrawlResource",
    "Link",
    "MapResource",
    "RecipeResource",
    "ScrapeResource",
]
