"""Stage 9: the v2 document as a standalone Python scraper.

The point of this is that it leaves. A recipe in the catalogue is useful while
you are a customer; a script you can run with nothing but `pip install
playwright` is useful afterwards, and offering it is what makes the catalogue a
place to build something rather than a place to be kept.

Ported from v1's `codegen.py`, which reads `field_locators`/`locator.source` --
names only the v1 shape has. Every recipe the studio or the onboarding agent
produces is v2, so that module could not generate code for anything anyone
actually authors.

**The generated code is checked, not just returned.** v1 handed back whatever
the model wrote, unread. For a deliverable that is the product, that is not
enough: `verify_generated` parses it and looks for the specific failure a model
makes here, which is quietly dropping a field or a whole group and emitting
something that runs perfectly and collects less than it was asked to.

**Two semantics a naive script gets wrong**, so the language pack states both:

- Each field group **re-navigates and re-runs `global_setup`**. Groups are not
  steps in one page visit -- one group's reveal clicks would otherwise leak
  into the next, and a group that navigated away would take the rest with it.
- A field's candidates are tried in order and the **first non-empty one wins**.
  They are fallbacks, not alternatives to merge.
"""

from __future__ import annotations

import ast
import json as _json
from typing import Any

from agentpilot.llm.client import LLMConfig, chat_json_conversation
from agentpilot.recipe.v2.models import Recipe

LANGUAGES = ("python-playwright",)

_LANGUAGE_PACKS: dict[str, str] = {
    "python-playwright": (
        "Target: Python 3 with `playwright.sync_api`, and NOTHING else that is "
        "not in the standard library. The script must run after only "
        "`pip install playwright && playwright install chromium`.\n"
        "\n"
        "Shape it as: a `scrape(url)` function returning a dict, and a "
        "`if __name__ == '__main__':` block that takes URLs from `sys.argv` "
        "(falling back to the recipe's sample URL) and prints one JSON object "
        "per URL.\n"
        "\n"
        "Resolve locators like this:\n"
        "- `css`   -> `page.query_selector(selector)`; with `all: true` -> "
        "`page.query_selector_all`. A `within` is a css scope: prepend it.\n"
        "- `xpath` -> `page.query_selector('xpath=' + selector)`.\n"
        "- `ax_role` -> `page.get_by_role(role, name=...)`; `name_contains` is "
        "a substring match, `name_in` is membership in a fixed set.\n"
        "- `text`  -> `page.get_by_text(text)`.\n"
        "- `json_ld` -> parse every `<script type=\"application/ld+json\">`, "
        "flatten it (unwrap any `@graph`, spread a top-level array) into ONE "
        "list, then index the declared `path` against that list.\n"
        "- `hydration` -> find the declared root key (`__NEXT_DATA__`, "
        "`__NUXT__`, `__APOLLO_STATE__`, `__REDUX_STATE__`, ...) in a script "
        "tag, `json.loads` it, and walk the rest of the path.\n"
        "- `meta` -> read `<meta>` name/property content into a dict and index "
        "it; a duplicated key is a list.\n"
        "\n"
        "A `path` is dotted with bracket indices (`props.pageProps.items[0].name`) "
        "and supports negative indices. Only `.`, `[` and `]` delimit, so a key "
        "may contain a colon (`og:title` is one key). A path that matches "
        "nothing is None, not an error.\n"
        "\n"
        "The `attribute` on a css/xpath locator says what to read: `text` "
        "(default) is `inner_text()`, anything else is `get_attribute(name)`."
    ),
}

_SYSTEM_PROMPT = """\
You write standalone, runnable web scrapers from a declarative recipe.

The recipe has: a target URL pattern, an optional one-time `global_setup` step \
list, and `field_groups`. Each group has its own `steps` (interactions that make \
its data reachable), `bindings` (field name -> an ORDERED list of candidate \
locators), and optionally a `repeat` that produces rows.

Three rules decide whether your script is correct:

1. EACH GROUP RE-NAVIGATES. Before a group's steps run, navigate to the URL \
again and re-run `global_setup`. Groups are not sequential steps in one visit: \
one group's clicks must not leak into the next, and a group whose interaction \
navigates away would otherwise take every later group with it.

2. CANDIDATES ARE FALLBACKS, FIRST NON-EMPTY WINS. Try them in the given order \
and stop at the first that yields a non-empty value. Do not merge them, do not \
prefer the last, and do not collect them all.

3. TRANSFORMS RUN IN ORDER, PER CANDIDATE. A candidate's own `transform` list \
applies to what that candidate read, then the field's own `transform` applies \
to the result. This split is deliberate: the same field read from JSON and from \
the DOM needs different cleanup.

A `repeat` produces one row per iteration:
- `kind: "json"`   -- `rows_locator` resolves to a LIST in the page's JSON; one \
row per element, and each column path is relative to that element.
- `kind: "dom_rows"` -- `rows_locator` matches N row ELEMENTS already on the \
page; resolve each column selector INSIDE its own row, never across rows.
- `kind: "dom"`    -- `option_locator` matches an option set; click each in \
turn, up to `max_iterations`, re-reading the group's bindings after each click. \
Re-resolve the options every iteration, because a click may re-render them.

A step with `optional: true` or `on_error: "continue"` must not stop the run \
when it fails -- catch and carry on. A `wait_for_selector` step is how the \
recipe waits for a reveal to land; keep it, with its timeout, and treat a \
timeout there as non-fatal.

Emit ONLY what the recipe describes. Every field in `fields` must appear in the \
output dict, absent or null when nothing resolved. Write real code, not a \
sketch, and no placeholder comments standing in for logic.\
"""

_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["code"],
}


def unsupported_reasons(recipe: Recipe) -> list[str]:
    """Why this recipe cannot become a standalone script, if it cannot.

    Refusing beats emitting something that looks right and silently does less:
    a script that drops a Lua transform still runs, still prints a row, and is
    wrong in a way nobody notices until the data is used.
    """

    reasons: list[str] = []

    if recipe.uses_lua():
        reasons.append(
            "this recipe uses a `lua` transform, which runs in a sandbox the "
            "generated script has no way to provide"
        )
    if not recipe.field_groups:
        reasons.append("this recipe has no field groups, so there is nothing to collect")
    if not recipe.fields:
        reasons.append("this recipe declares no fields")

    return reasons


def supported_languages(recipe: Recipe) -> list[str]:
    return [] if unsupported_reasons(recipe) else list(LANGUAGES)


def _recipe_for_prompt(recipe: Recipe) -> dict[str, Any]:
    doc = recipe.to_dict()
    # `built_under` is provenance about the build machine, and `health_status`
    # and `heal_attempts` are operational state. None of it describes what to
    # collect, and all of it is context the model would try to honour.
    for noise in ("built_under", "health_status", "heal_attempts", "status"):
        doc.pop(noise, None)
    return doc


def verify_generated(code: str, recipe: Recipe) -> list[str]:
    """Problems with the generated script, most serious first. Empty is a pass.

    Deliberately narrow. The check is not "is this good code" -- that is not
    decidable here and a false rejection costs the caller their deliverable. It
    is aimed at the one failure this task actually produces: a script that runs
    and collects less than it was asked to.
    """

    problems: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        # Nothing below is meaningful on a file that does not parse.
        return [f"the generated script is not valid Python: {exc}"]

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else node.names)
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    if "playwright" not in imported:
        problems.append("the generated script does not import playwright")

    # A dropped field is the failure mode worth catching: the script runs, the
    # output looks plausible, and one column is simply missing.
    missing = [name for name in recipe.fields if name not in code]
    if missing:
        problems.append(
            "the generated script never mentions these fields: " + ", ".join(sorted(missing))
        )

    # A dropped group is the same failure one level up, and shows as its reveal
    # steps being absent. Checked by op name rather than by exact selector,
    # because a selector legitimately arrives escaped or split across lines.
    reveal_ops = {
        step.op for group in recipe.field_groups for step in group.steps
    } | {step.op for step in recipe.global_setup}
    missing_ops = [op for op in sorted(reveal_ops) if op not in code]
    if missing_ops:
        problems.append(
            "the generated script performs none of these steps the recipe needs: "
            + ", ".join(missing_ops)
        )

    return problems


async def generate_scraper_code(
    recipe: Recipe,
    *,
    language: str = "python-playwright",
    llm_config: LLMConfig,
    max_attempts: int = 2,
) -> tuple[str, list[str]]:
    """Write the script, check it, and retry once with the problems fed back.

    Returns `(code, problems)`. A non-empty `problems` on the last attempt is
    returned WITH the code rather than raised: a script with one missing field
    is still worth handing over, as long as what is wrong with it is said out
    loud. Silently returning it as though it were complete is the thing to
    avoid.
    """

    if language not in _LANGUAGE_PACKS:
        raise ValueError(
            f"unsupported language: {language!r} (supported: {sorted(_LANGUAGE_PACKS)})"
        )
    reasons = unsupported_reasons(recipe)
    if reasons:
        raise ValueError("; ".join(reasons))

    from agentpilot.agent.reliability import RetryStrategy

    base = (
        f"Language/framework: {language}\n{_LANGUAGE_PACKS[language]}\n\n"
        f"Recipe:\n{_json.dumps(_recipe_for_prompt(recipe), indent=2)}"
    )

    code = ""
    problems: list[str] = []
    for attempt in range(max(1, max_attempts)):
        user = base
        if problems:
            user += (
                "\n\nYour previous attempt had these problems. Fix them and return "
                "the WHOLE script again:\n- " + "\n- ".join(problems)
            )
        raw = await RetryStrategy().execute(
            lambda u=user: chat_json_conversation(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": u},
                ],
                config=llm_config,
                json_schema=_JSON_SCHEMA,
            )
        )
        code = str(raw.get("code") or "")
        problems = verify_generated(code, recipe)
        if not problems:
            break
        if attempt + 1 >= max(1, max_attempts):
            break

    return code, problems
