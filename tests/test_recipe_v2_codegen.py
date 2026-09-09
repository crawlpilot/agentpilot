"""`agentpilot.recipe.v2.codegen` -- the v2 document as a standalone scraper.

The prompt is not tested (it is prose). What is tested is the gate around it:
what this refuses to generate at all, and what it catches in what came back.
Both exist for the same reason -- a scraper that runs and quietly collects less
than it was asked to is worse than one that fails, because nobody finds out.
"""

from __future__ import annotations

import pytest

from agentpilot.recipe.v2 import codegen as codegen_mod
from agentpilot.recipe.v2.codegen import (
    generate_scraper_code,
    supported_languages,
    unsupported_reasons,
    verify_generated,
)
from agentpilot.recipe.v2.models import (
    Candidate,
    FieldGroup,
    Locator,
    Recipe,
    Step,
    TargetSpec,
)
from agentpilot.recipe.v2.schema import FieldSpec, TypeSpec
from agentpilot.recipe.v2.transform import parse_transforms

GOOD = """\
import json
import sys
from playwright.sync_api import sync_playwright


def scrape(url):
    with sync_playwright() as p:
        page = p.chromium.launch().new_page()
        page.goto(url)
        page.click(".more")
        return {"name": page.inner_text("h1"), "price": page.inner_text(".price")}


if __name__ == "__main__":
    for u in sys.argv[1:]:
        print(json.dumps(scrape(u)))
"""


def _recipe(**over) -> Recipe:
    base = dict(
        recipe_id="r", tenant="t", name="n", version=1, target=TargetSpec(),
        fields={
            "name": FieldSpec(name="name", type=TypeSpec(kind="scalar")),
            "price": FieldSpec(name="price", type=TypeSpec(kind="scalar", value_type="price")),
        },
        field_groups=[
            FieldGroup(
                group_id="g0",
                field_names=["name", "price"],
                bindings={
                    "name": [Candidate(locator=Locator(kind="css", selector="h1"))],
                    "price": [Candidate(locator=Locator(kind="css", selector=".price"))],
                },
                steps=[Step(op="click", target=Locator(kind="css", selector=".more"))],
            )
        ],
    )
    base.update(over)
    return Recipe(**base)  # type: ignore[arg-type]


# --- what it refuses to attempt ---------------------------------------------


def test_a_lua_transform_cannot_become_a_standalone_script() -> None:
    """Lua runs in a sandbox the generated script has no way to provide. A
    script that silently drops the transform still runs, still prints a row,
    and is wrong in a way nobody notices until the data is used."""

    recipe = _recipe()
    recipe.fields["name"] = FieldSpec(
        name="name", transform=parse_transforms([{"op": "lua", "value": "return v"}])
    )
    reasons = unsupported_reasons(recipe)
    assert reasons and "lua" in reasons[0]
    assert supported_languages(recipe) == []


def test_a_recipe_with_nothing_to_collect_is_refused() -> None:
    assert unsupported_reasons(_recipe(field_groups=[]))
    assert unsupported_reasons(_recipe(fields={}))


def test_an_ordinary_recipe_is_supported() -> None:
    assert unsupported_reasons(_recipe()) == []
    assert supported_languages(_recipe()) == ["python-playwright"]


async def test_an_unsupported_language_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        await generate_scraper_code(_recipe(), language="cobol", llm_config=None)


async def test_an_inexpressible_recipe_raises_rather_than_generating() -> None:
    recipe = _recipe()
    recipe.fields["name"] = FieldSpec(
        name="name", transform=parse_transforms([{"op": "lua", "value": "return v"}])
    )
    with pytest.raises(ValueError, match="lua"):
        await generate_scraper_code(recipe, llm_config=None)


# --- what it catches in what came back --------------------------------------


def test_a_script_that_does_not_parse_is_reported_first() -> None:
    problems = verify_generated("def oops(", _recipe())
    assert len(problems) == 1
    assert "not valid Python" in problems[0]


def test_a_dropped_field_is_caught() -> None:
    """THE failure this task produces: the script runs, the output looks
    plausible, and one column is simply missing."""

    without_price = GOOD.replace('"price": page.inner_text(".price")', "")
    problems = verify_generated(without_price, _recipe())
    assert any("price" in p for p in problems)


def test_a_dropped_reveal_step_is_caught() -> None:
    """A dropped group shows as its interaction being absent -- so the fields
    behind it come back empty on every run, for no stated reason."""

    without_click = GOOD.replace('page.click(".more")', "")
    problems = verify_generated(without_click, _recipe())
    assert any("click" in p for p in problems)


def test_a_script_that_does_not_drive_a_browser_is_caught() -> None:
    problems = verify_generated("name = 1\nprice = 2\nclick = 3", _recipe())
    assert any("playwright" in p for p in problems)


def test_a_complete_script_passes() -> None:
    assert verify_generated(GOOD, _recipe()) == []


def test_the_check_tolerates_a_differently_shaped_import() -> None:
    """`import playwright.sync_api` and `from playwright... import x` are both
    a script that drives a browser; a false rejection costs the caller their
    deliverable."""

    variant = GOOD.replace(
        "from playwright.sync_api import sync_playwright", "import playwright.sync_api"
    )
    assert verify_generated(variant, _recipe()) == []


# --- the retry --------------------------------------------------------------


class _StubLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.prompts: list[str] = []

    async def __call__(self, messages, **kwargs):
        self.prompts.append(messages[1]["content"])
        return {"code": self.replies.pop(0)}


async def test_a_failed_check_is_fed_back_and_the_retry_can_fix_it(monkeypatch) -> None:
    stub = _StubLLM([GOOD.replace('"price": page.inner_text(".price")', ""), GOOD])
    monkeypatch.setattr(codegen_mod, "chat_json_conversation", stub)

    code, problems = await generate_scraper_code(_recipe(), llm_config=None)

    assert problems == []
    assert code == GOOD
    # The second attempt was told what was wrong with the first.
    assert "price" in stub.prompts[1]
    assert "previous attempt" in stub.prompts[1]


async def test_a_script_that_still_fails_is_returned_with_its_problems(
    monkeypatch,
) -> None:
    """Handed over rather than withheld: a script with one missing field is
    still worth having, as long as what is wrong with it is said out loud."""

    broken = GOOD.replace('"price": page.inner_text(".price")', "")
    stub = _StubLLM([broken, broken])
    monkeypatch.setattr(codegen_mod, "chat_json_conversation", stub)

    code, problems = await generate_scraper_code(_recipe(), llm_config=None)

    assert code == broken
    assert any("price" in p for p in problems)


async def test_the_prompt_does_not_carry_operational_noise(monkeypatch) -> None:
    """`built_under` is provenance about the build machine and `health_status`
    is operational state. Neither describes what to collect, and both are
    context the model would try to honour."""

    stub = _StubLLM([GOOD])
    monkeypatch.setattr(codegen_mod, "chat_json_conversation", stub)

    recipe = _recipe()
    recipe.built_under = {"landed_url": "https://x.test"}
    recipe.health_status = "degraded"
    await generate_scraper_code(recipe, llm_config=None)

    assert "built_under" not in stub.prompts[0]
    assert "degraded" not in stub.prompts[0]


async def test_the_prompt_states_the_two_rules_a_naive_script_gets_wrong(
    monkeypatch,
) -> None:
    stub = _StubLLM([GOOD])
    monkeypatch.setattr(codegen_mod, "chat_json_conversation", stub)
    await generate_scraper_code(_recipe(), llm_config=None)

    assert "EACH GROUP RE-NAVIGATES" in codegen_mod._SYSTEM_PROMPT
    assert "FIRST NON-EMPTY WINS" in codegen_mod._SYSTEM_PROMPT


def test_the_language_pack_states_the_either_or_transform_rule() -> None:
    """`resolve_field` reads `cand.transform if cand.transform is not None else
    spec.transform` -- either/or. The pack used to say the candidate's applied
    "then the field's applies to the result", so a generated script would
    double-transform any field carrying both and quietly disagree with the
    recipe it was generated from."""

    pack = codegen_mod._LANGUAGE_PACKS["python-playwright"]
    prompt = codegen_mod._SYSTEM_PROMPT
    assert "REPLACES THE FIELD'S" in prompt
    assert "NEVER run both" in prompt
    assert pack  # the pack itself stays about the target language
