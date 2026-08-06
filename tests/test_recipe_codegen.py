"""Unit tests for `agentpilot.recipe.codegen` -- prompt construction only,
`httpx.MockTransport` standing in for the LLM. Never executes generated code
-- that's out of scope for this test suite (see the module docstring)."""

from __future__ import annotations

import json

import httpx
import pytest

from agentpilot.llm.client import LLMConfig
from agentpilot.recipe.codegen import generate_scraper_code, supported_languages
from agentpilot.recipe.models import FieldGroup, FieldLocator, Locator, Recipe, RepeatSpec

CONFIG = LLMConfig(api_key="test-key", base_url="https://llm.test/v1", model="m", timeout_s=5.0)

JSON_ONLY_RECIPE = Recipe(
    recipe_id="r1",
    tenant="t1",
    name="test",
    url_pattern="https://example.test/p",
    field_schema={"title": {"type": "scalar", "description": "d"}},
    version=1,
    global_setup=[],
    field_groups=[
        FieldGroup(
            group_id="g0",
            field_names=["title"],
            reveal_steps=[],
            field_locators={"title": FieldLocator(source="json_ld", path="[0].name")},
        )
    ],
)

REPEAT_RECIPE = Recipe(
    recipe_id="r2",
    tenant="t1",
    name="test-variants",
    url_pattern="https://example.test/p",
    field_schema={"variants": {"type": "array", "description": "d", "item_schema": {}}},
    version=1,
    global_setup=[],
    field_groups=[
        FieldGroup(
            group_id="g0",
            field_names=["size", "price"],
            reveal_steps=[],
            field_locators={"price": FieldLocator(source="css", selector=".price")},
            repeat=RepeatSpec(
                option_locator=Locator(source="ax_role", role="button", name_in=["S", "M"]),
                max_iterations=20,
                array_field="variants",
            ),
        )
    ],
)


def test_supported_languages_includes_requests_only_for_a_pure_json_recipe() -> None:
    assert "python-requests-only" in supported_languages(JSON_ONLY_RECIPE)


def test_supported_languages_excludes_requests_only_when_a_css_locator_is_present() -> None:
    assert "python-requests-only" not in supported_languages(REPEAT_RECIPE)


async def test_generate_scraper_code_rejects_requests_only_for_a_non_json_recipe() -> None:
    with pytest.raises(ValueError, match="python-requests-only"):
        await generate_scraper_code(
            REPEAT_RECIPE, language="python-requests-only", llm_config=CONFIG
        )


async def test_generate_scraper_code_rejects_an_unknown_language() -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        await generate_scraper_code(JSON_ONLY_RECIPE, language="rust-nope", llm_config=CONFIG)


async def test_generate_scraper_code_sends_the_repeat_spec_to_the_model(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        payload = {"code": "print('hello')", "notes": ""}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=httpx.MockTransport(handler), **kw),
    )

    code = await generate_scraper_code(
        REPEAT_RECIPE, language="python-playwright", llm_config=CONFIG
    )
    assert code == "print('hello')"
    user_message = captured["body"]["messages"][-1]["content"]
    assert "option_locator" in user_message
    assert "max_iterations" in user_message
