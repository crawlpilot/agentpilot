"""Stage 5: LLM-authored multi-language scraper code generation.

Given a built recipe (`global_setup` + `field_groups`, including any
`RepeatSpec`), one structured-output LLM call writes a standalone runnable
script implementing the same reveal -> iterate -> extract logic
`replay.py` executes for that language/framework -- genuinely LLM-authored,
not a fixed per-language template. Grounded by a short "language pack"
snippet per target so output stays idiomatic and consistent.
"""

from __future__ import annotations

import json as _json
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.models import Recipe

_LANGUAGE_PACKS: dict[str, str] = {
    "python-playwright": (
        "Target: Python 3, using the `playwright.sync_api` package. Resolve "
        "an `ax_role` locator via `page.get_by_role(role, name=...)` (substring "
        "match for `name_contains`, exact-match-against-a-set for `name_in`), "
        "a `css` locator via `page.query_selector(selector)`. For a repeat "
        "group, resolve `option_locator` to all matching elements and loop up "
        "to `max_iterations`, clicking each option (re-resolving fresh each "
        "iteration -- a prior click may re-render the option set) before "
        "reading that iteration's fields."
    ),
    "node-puppeteer": (
        "Target: Node.js, using the `puppeteer` package. Resolve an `ax_role` "
        "locator via `page.$$('[role=\"ROLE\"]')` plus a text-content filter, "
        "a `css` locator via `page.$(selector)`. For a repeat group, resolve "
        "all matching option handles and loop up to `max_iterations`, "
        "clicking each (re-resolving fresh each iteration) before reading "
        "that iteration's fields."
    ),
    "python-requests-only": (
        "Target: Python 3, using only the `requests` and `json` standard "
        "packages -- no browser. Valid ONLY because every locator in this "
        "recipe reads from json_ld/hydration/meta. Fetch the URL once with "
        "`requests.get`, parse the JSON-LD script tag(s) / hydration script "
        "and walk the declared dotted/bracket `path` for each field -- no "
        "interaction steps are needed or possible without a browser."
    ),
}

_SYSTEM_PROMPT = (
    "You write standalone, runnable web-scraper scripts from a structured "
    "'recipe': a URL, a one-time global setup sequence, and a list of field "
    "groups. Each group has its own reveal steps (interactions needed to "
    "expose that group's data -- action + a re-resolvable locator: role+name "
    "or a css selector) and field locators (where to read each value from: a "
    "json_ld/hydration/meta dotted/bracket path, a css selector+attribute, or "
    "an accessibility role+name). A group may declare a repeat spec: an "
    "option_locator matching multiple page elements (e.g. size swatches) -- "
    "iterate it, clicking each option and re-reading the group's field "
    "locators to produce one output row per option, up to max_iterations. "
    "Write idiomatic, complete code for the requested language/framework "
    "implementing exactly this reveal -> iterate -> extract logic. Do not "
    "invent features beyond what the recipe describes."
)


def _is_pure_json_recipe(recipe: Recipe) -> bool:
    return all(
        locator.source in ("json_ld", "hydration", "meta")
        for group in recipe.field_groups
        for locator in group.field_locators.values()
    )


def supported_languages(recipe: Recipe) -> list[str]:
    languages = ["python-playwright", "node-puppeteer"]
    if _is_pure_json_recipe(recipe):
        languages.append("python-requests-only")
    return languages


def _recipe_to_prompt_dict(recipe: Recipe) -> dict[str, Any]:
    return {"url": recipe.url_pattern, "schema": recipe.field_schema, **recipe.to_dict()}


async def generate_scraper_code(recipe: Recipe, *, language: str, llm_config: LLMConfig) -> str:
    if language not in _LANGUAGE_PACKS:
        supported = sorted(_LANGUAGE_PACKS)
        raise ValueError(f"unsupported language: {language!r} (supported: {supported})")
    if language == "python-requests-only" and not _is_pure_json_recipe(recipe):
        raise ValueError(
            "python-requests-only is only valid when every field locator is "
            "json_ld/hydration/meta -- this recipe has at least one css/ax_role locator"
        )

    user = (
        f"Language/framework: {language}\n{_LANGUAGE_PACKS[language]}\n\n"
        f"Recipe:\n{_json.dumps(_recipe_to_prompt_dict(recipe), indent=2)}"
    )
    raw = await chat_json_conversation(
        [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user}],
        config=llm_config,
        json_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}, "notes": {"type": "string"}},
            "required": ["code"],
        },
    )
    return str(raw["code"])
