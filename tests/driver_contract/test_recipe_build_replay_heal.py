"""End-to-end tests for `agentpilot.recipe`'s Phase 2 pipeline -- real
Patchright driver, real in-memory `Registry`, a stubbed `/chat/completions`
endpoint (no real LLM API call, ever). Covers the two cases named explicitly
in the design review that shaped this phase (accordion/hidden-JSON-reveal,
and a size-swatch "variant loop"), plus a heal cycle that repairs a group
whose locator stops resolving.
"""

from __future__ import annotations

import json
import re

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from agentpilot.driver.patchright_driver import PatchrightDriver
from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.build import build_recipe
from agentpilot.recipe.heal import check_and_heal
from agentpilot.recipe.replay import replay_recipe
from agentpilot.session.interactive import open_interactive_session, release_interactive_session
from agentpilot.session.registry import Registry

ACCORDION_PAGE_HTML = """<html><body>
<button id="btn">Show details</button>
<div id="marker"></div>
<script>
document.getElementById('btn').addEventListener('click', function () {
    var s = document.createElement('script');
    s.type = 'application/ld+json';
    s.textContent = JSON.stringify({product: {price: '19.99'}});
    document.body.appendChild(s);
    document.getElementById('marker').textContent = 'revealed';
});
</script>
</body></html>"""

VARIANT_PAGE_HTML = """<html><body>
<div id="swatches">
<button class="size">S</button>
<button class="size">M</button>
<button class="size">L</button>
</div>
<div id="price"></div>
<script>
document.querySelectorAll('.size').forEach(function (btn) {
    btn.addEventListener('click', function () {
        var prices = {S: '9.99', M: '19.99', L: '29.99'};
        document.getElementById('price').textContent = prices[btn.textContent];
    });
});
</script>
</body></html>"""


@pytest.fixture
def llm_httpserver():
    server = HTTPServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _is_proposal_call(body: dict) -> bool:
    schema = body["response_format"]["json_schema"]["schema"]
    return "locators" in schema["properties"]


def _respond(payload: dict) -> Response:
    return Response(
        json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}),
        content_type="application/json",
    )


def _llm_config(llm_httpserver: HTTPServer) -> LLMConfig:
    return LLMConfig(
        api_key="test-key", base_url=llm_httpserver.url_for("/"), model="test-model", timeout_s=10.0
    )


async def test_build_and_replay_accordion_json_ld_case(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data(
        ACCORDION_PAGE_HTML, content_type="text/html"
    )
    url = httpserver.url_for("/")
    calls = {"explore": 0, "propose": 0}

    def handler(request: Request) -> Response:
        body = json.loads(request.get_data())
        if _is_proposal_call(body):
            calls["propose"] += 1
            payload = {
                "locators": [
                    {"field": "price", "source": "json_ld", "path": "[0].product.price"}
                ]
            }
            return _respond(payload)

        calls["explore"] += 1
        user_content = body["messages"][-1]["content"]
        if calls["explore"] == 1:
            # `open_interactive_session` never pre-navigates -- the agent's
            # own first move must be to navigate there itself.
            payload = {
                "evaluation_previous_goal": "starting",
                "memory": "",
                "next_goal": "navigate to the target page",
                "action": [{"type": "navigate", "url": url}],
            }
        elif calls["explore"] == 2:
            match = re.search(r'\[(\w+)\]<button "Show details"', user_content)
            assert match is not None, user_content
            payload = {
                "evaluation_previous_goal": "navigated successfully",
                "memory": "",
                "next_goal": "reveal the price",
                "action": [{"type": "click", "ref": match.group(1)}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "revealed the price",
                "memory": "",
                "next_goal": "",
                "action": [{"type": "done", "success": True, "result": "found price"}],
            }
        return _respond(payload)

    llm_httpserver.expect_request("/chat/completions").respond_with_handler(handler)

    registry = Registry()
    session = await open_interactive_session(
        session_id="recipe-build-hydration",
        tenant="acme",
        domain="127.0.0.1",
        name="recipe-build-hydration",
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        vault=None,
        lease_ttl_seconds=300.0,
    )
    try:
        recipe, result = await build_recipe(
            recipe_id="r-hydration",
            tenant="acme",
            name="hydration-test",
            url=url,
            raw_schema={"price": {"type": "scalar", "description": "the product price"}},
            session=session,
            registry=registry,
            driver=driver,
            llm_config=_llm_config(llm_httpserver),
            max_steps=5,
        )

        assert result.success is True
        assert recipe.health_status == "healthy"
        assert len(recipe.field_groups) == 1
        group = recipe.field_groups[0]
        assert group.field_names == ["price"]
        assert group.field_locators["price"][0].source == "json_ld"
        assert len(recipe.global_setup) == 1  # the "Show details" click
        assert calls["propose"] >= 1  # at least one successful proposal; earlier
        # steps may have tried (and failed to verify) before the click revealed it

        calls_before_replay = dict(calls)
        replay_result = await replay_recipe(
            recipe, session=session, registry=registry, driver=driver
        )
    finally:
        await release_interactive_session(session, registry=registry, driver=driver, vault=None)

    assert replay_result.success is True
    assert replay_result.data["price"] == "19.99"
    assert calls == calls_before_replay  # zero further LLM calls during replay


async def test_build_and_replay_size_variant_loop_case(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
) -> None:
    httpserver.expect_request("/").respond_with_data(VARIANT_PAGE_HTML, content_type="text/html")
    url = httpserver.url_for("/")
    calls = {"explore": 0, "propose": 0}

    def handler(request: Request) -> Response:
        body = json.loads(request.get_data())
        if _is_proposal_call(body):
            calls["propose"] += 1
            payload = {
                "locators": [
                    {"field": "price", "source": "css", "selector": "#price", "attribute": "text"}
                ]
            }
            return _respond(payload)

        calls["explore"] += 1
        user_content = body["messages"][-1]["content"]
        if calls["explore"] == 1:
            payload = {
                "evaluation_previous_goal": "starting",
                "memory": "",
                "next_goal": "navigate to the target page",
                "action": [{"type": "navigate", "url": url}],
            }
        elif calls["explore"] == 2:
            match = re.search(r'\[(\w+)\]<button "M"', user_content)
            assert match is not None, user_content
            payload = {
                "evaluation_previous_goal": "navigated successfully",
                "memory": "",
                "next_goal": "select a representative size",
                "action": [{"type": "click", "ref": match.group(1)}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "selected a size and saw the price",
                "memory": "",
                "next_goal": "",
                "action": [{"type": "done", "success": True, "result": "found variants"}],
            }
        return _respond(payload)

    llm_httpserver.expect_request("/chat/completions").respond_with_handler(handler)

    registry = Registry()
    session = await open_interactive_session(
        session_id="recipe-build-variants",
        tenant="acme",
        domain="127.0.0.1",
        name="recipe-build-variants",
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        vault=None,
        lease_ttl_seconds=300.0,
    )
    try:
        raw_schema = {
            "variants": {
                "type": "array",
                "description": "one row per size",
                "item_schema": {
                    "price": {"type": "scalar", "description": "price shown for this size"}
                },
            }
        }
        recipe, result = await build_recipe(
            recipe_id="r-variants",
            tenant="acme",
            name="variants-test",
            url=url,
            raw_schema=raw_schema,
            session=session,
            registry=registry,
            driver=driver,
            llm_config=_llm_config(llm_httpserver),
            max_steps=5,
        )

        assert result.success is True
        assert len(recipe.field_groups) == 1
        group = recipe.field_groups[0]
        assert group.repeat is not None
        assert group.repeat.array_field == "variants"
        assert set(group.repeat.option_locator.name_in or []) == {"S", "M", "L"}

        calls_before_replay = dict(calls)
        replay_result = await replay_recipe(
            recipe, session=session, registry=registry, driver=driver
        )
    finally:
        await release_interactive_session(session, registry=registry, driver=driver, vault=None)

    assert replay_result.success is True
    rows = replay_result.data["variants"]
    assert {row["price"] for row in rows} == {"9.99", "19.99", "29.99"}
    assert calls == calls_before_replay  # zero further LLM calls during replay


async def test_heal_repairs_a_group_whose_css_locator_stops_resolving(
    driver: PatchrightDriver,
    tmp_path,
    httpserver: HTTPServer,
    llm_httpserver: HTTPServer,
) -> None:
    """Build against a page with `#price`; the site then "redesigns" to
    `#price-v2` (a fresh httpserver handler swapped in mid-test). Replay
    should report a field failure, and a heal cycle (scripted to propose the
    new selector) should repair it without needing a fresh recipe."""

    original_html = """<html><body><div id="price">19.99</div></body></html>"""
    redesigned_html = """<html><body><div id="price-v2">24.99</div></body></html>"""

    current_html = {"value": original_html}
    httpserver.expect_request("/").respond_with_handler(
        lambda request: Response(current_html["value"], content_type="text/html")
    )
    url = httpserver.url_for("/")
    calls = {"explore": 0, "propose": 0}

    def handler(request: Request) -> Response:
        body = json.loads(request.get_data())
        if _is_proposal_call(body):
            calls["propose"] += 1
            selector = "#price" if current_html["value"] == original_html else "#price-v2"
            payload = {
                "locators": [
                    {"field": "price", "source": "css", "selector": selector, "attribute": "text"}
                ]
            }
            return _respond(payload)

        calls["explore"] += 1
        if calls["explore"] % 2 == 1:
            # Odd-numbered explore call in every exploration run (build's
            # and heal's own) -- the agent's first move must navigate there
            # itself, since `open_interactive_session` never pre-navigates.
            payload = {
                "evaluation_previous_goal": "starting",
                "memory": "",
                "next_goal": "navigate to the target page",
                "action": [{"type": "navigate", "url": url}],
            }
        else:
            payload = {
                "evaluation_previous_goal": "checked the page",
                "memory": "",
                "next_goal": "",
                "action": [{"type": "done", "success": True, "result": "found price"}],
            }
        return _respond(payload)

    llm_httpserver.expect_request("/chat/completions").respond_with_handler(handler)

    registry = Registry()
    session = await open_interactive_session(
        session_id="recipe-heal",
        tenant="acme",
        domain="127.0.0.1",
        name="recipe-heal",
        tier="auto",
        headful=False,
        block_popups=False,
        enable_cdp=False,
        registry=registry,
        driver=driver,
        profiles_root=tmp_path,
        proxy_pinner=None,
        vault=None,
        lease_ttl_seconds=300.0,
    )
    try:
        recipe, result = await build_recipe(
            recipe_id="r-heal",
            tenant="acme",
            name="heal-test",
            url=url,
            raw_schema={"price": {"type": "scalar", "description": "the product price"}},
            session=session,
            registry=registry,
            driver=driver,
            llm_config=_llm_config(llm_httpserver),
            max_steps=5,
        )
        assert result.success is True
        assert recipe.field_groups[0].field_locators["price"][0].selector == "#price"

        current_html["value"] = redesigned_html

        broken_replay = await replay_recipe(
            recipe, session=session, registry=registry, driver=driver
        )
        assert broken_replay.success is False
        assert "price" in broken_replay.field_failures

        healed, heal_result = await check_and_heal(
            recipe,
            session=session,
            registry=registry,
            driver=driver,
            llm_config=_llm_config(llm_httpserver),
            max_steps=5,
        )
    finally:
        await release_interactive_session(session, registry=registry, driver=driver, vault=None)

    assert healed.version == recipe.version + 1
    assert healed.health_status == "healthy"
    assert heal_result.success is True
    assert heal_result.data["price"] == "24.99"
    assert healed.field_groups[0].field_locators["price"][0].selector == "#price-v2"
