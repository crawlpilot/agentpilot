"""`/v1/recipes` -- a scrape learned once and replayed cheaply.

CRUD plus the three things you do to a recipe: run it, heal it when the site
moves, and generate code from it. Each of the three queues a run and returns its
id, so they share one handle type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentpilot_client._transport import Transport


class RecipeResource:
    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    # ------------------------------------------------------------------ CRUD

    async def create(self, **fields: Any) -> dict[str, Any]:
        return await self._transport.request_object(
            "POST", "/v1/recipes", json={"tenant": "-", **fields}
        )

    async def list(self, *, after: str | None = None, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if after is not None:
            params["after"] = after
        if limit is not None:
            params["limit"] = limit
        return await self._transport.request_object("GET", "/v1/recipes", params=params or None)

    async def get(self, recipe_id: str) -> dict[str, Any]:
        return await self._transport.request_object("GET", f"/v1/recipes/{recipe_id}")

    async def versions(self, recipe_id: str) -> dict[str, Any]:
        """Every version of a recipe. A heal writes a new one rather than
        editing in place, so this is the audit trail of what the site did."""

        return await self._transport.request_object("GET", f"/v1/recipes/{recipe_id}/versions")

    # ------------------------------------------------------------ run / heal

    async def run(self, recipe_id: str, **options: Any) -> str:
        """Queue a replay. Returns the run id -- poll it with `run_status`."""

        payload = await self._transport.request(
            "POST", f"/v1/recipes/{recipe_id}/run", json=options or {}
        )
        return str(payload["run_id"])

    async def heal(self, recipe_id: str, **options: Any) -> str:
        """Queue a heal: re-derive the locators a moved site broke, and write a
        new version if it succeeds."""

        payload = await self._transport.request(
            "POST", f"/v1/recipes/{recipe_id}/heal", json=options or {}
        )
        return str(payload["run_id"])

    async def codegen(self, recipe_id: str, **options: Any) -> str:
        """Queue code generation -- the recipe as a script you can keep."""

        payload = await self._transport.request(
            "POST", f"/v1/recipes/{recipe_id}/codegen", json=options or {}
        )
        return str(payload["run_id"])

    async def run_status(self, recipe_id: str, run_id: str) -> dict[str, Any]:
        return await self._transport.request_object(
            "GET", f"/v1/recipes/{recipe_id}/runs/{run_id}"
        )
