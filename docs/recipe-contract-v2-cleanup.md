# Recipe v2 cleanup inventory

**Status:** specification. The demolition list for the
[v2 contract](recipe-contract-v2.md) rewrite.

v1 has no production users and there are no stored recipes to preserve. So this is a **replacement,
not a migration**: no dual-format support, no compatibility shims, no `contract_version` column.
Every v1 symbol below is deleted or rewritten, and this document exists so the implementation has
no ambiguity about which.

The reference lists were produced by grepping the tree, not from memory. Re-run the greps in
§"Completeness check" before declaring the work done.

---

## 1. Replaced outright

| File | Fate |
|---|---|
| `packages/agentpilot/agentpilot/recipe/models.py` | rewritten — `Locator`, `RevealStep`, `RepeatSpec`, `FieldLocator`, `FieldGroup`, `Recipe`, `RecipeRunResult` all change shape |
| `packages/agentpilot/agentpilot/recipe/schema.py` | rewritten — `FieldNormalization` (fixed-order struct) becomes an ordered `Transform` list; `FieldSpec` gains `TypeSpec` and `assertions` |
| `packages/agentpilot/agentpilot/recipe/jsonpath.py` | kept as the `simple` dialect, plus a `jmespath` branch (see [`recipe-json-extraction.md`](recipe-json-extraction.md)) |

## 2. Deleted, not ported

- **`models._locators_from_dict`** — the legacy single-locator-per-field deserialization shim, and
  its test in `tests/test_recipe_models.py`. There is no old format left to accept. It is the only
  reference to itself in the tree.
- **`Recipe.url_pattern`** as a navigation target. The URL becomes run input (contract §1). This is
  the highest-fan-out deletion in the list — see §4.

## 3. New modules

| Module | Purpose |
|---|---|
| `recipe/transform.py` | the ordered transform pipeline (absorbs `normalize.py`'s semantics) |
| `recipe/lua.py` | the sandboxed Lua evaluator (contract §7.1) |
| `recipe/predicate.py` | `Predicate` evaluation for `when` / `repeat_until` / variant `detect` |
| `recipe/assertions.py` | field assertions and the `suspect` status |
| `recipe/classify.py` | the recipe-scoped block classification that makes `outcome: "blocked"` real ([`recipe-operations.md`](recipe-operations.md) §D5.1) |

All live under `agentpilot/recipe/`, which the import-linter layering
(`jobs → recipe → agent → llm`) already permits.

## 4. Consumers updated in lockstep

Grepped, not guessed. `url_pattern` alone appears in **16 files**.

**Recipe layer** — `build.py`, `replay.py`, `heal.py`, `evaluate.py`, `dispatch.py`,
`stabilize.py`, `generalize.py`, `normalize.py`, `codegen.py`, `locator_proposal.py`,
`selector_synthesis.py`

**Jobs layer** — `jobs/recipe_store.py` (`RecipeOut`), `jobs/recipe_worker_loop.py`

**Gateway** — `gateway/schemas.py`, `gateway/routes/recipes.py`

**Frontend** — `lib/api/types.ts`, `components/app/RecipesTable.tsx`,
`routes/RecipeDetailPage.tsx`

Three behavioural fixes land with the rewrite and should be verified explicitly, because each is a
silent-wrong-answer bug rather than a crash:

1. `dispatch.py` — CSS clicks currently compile to `ExecuteJsAction` running `el.click()`: an
   untrusted synthetic click with no auto-wait and no scroll-into-view. Replace with
   `ClickAction(selector=…)`, which the SPI already accepts.
2. `dispatch.py` — `scroll` discards direction and always dispatches `down`.
3. `replay.py` — `_replay_repeat_group` appends only fully-complete rows and silently drops
   partial ones. v2 keeps them and reports `truncated`.

## 5. Frontend deleted, not adapted

| File | Reason |
|---|---|
| `frontend/src/components/app/RecipeCreateDialog.tsx` | a raw-JSON textarea cannot author a v2 document |
| `frontend/src/components/app/RecipeFieldGroupsList.tsx` | a `JSON.stringify` dump cannot review one |

`frontend/src/hooks/useRecipes.ts` and `frontend/src/lib/api/recipes.ts` are rewritten against the
v2 shapes and the six new endpoints in [`recipe-studio.md`](recipe-studio.md).

## 6. Database

**One Alembic revision.** With no rows to preserve it drops and recreates rather than backfilling:

- `recipes` — the document collapses to a single `spec JSONB` column. `url_pattern` is **dropped**.
  Adds `status` (`draft`/`approved`/`published`), `has_script`, `sample_urls` JSONB,
  `built_under` JSONB.
- `recipe_versions` — unchanged in shape, still append-only (it is what makes heal rollback
  possible; see [`recipe-operations.md`](recipe-operations.md) §D5.3).
- `recipe_runs` — adds `outcome` (`ok`/`partial`/`failed`/**`blocked`**), `field_status`,
  `provenance`, `truncated`, `assertions` JSONB.
- `recipe_field_metrics` — **new**, the rollup behind §D4's drift detection.
- `domain_agents`, `domain_agent_recipes` — **new**, per [`domain-agents.md`](domain-agents.md).

House style throughout: raw SQL, `TEXT` PK from `uuid4()`, `TEXT` + `CHECK` for enums, JSONB for
blobs, partial index on the claim predicate.

> **This revision is destructive, and it is safe only because v1 is unused.** If that ever stops
> being true before it ships, this document is wrong and the work needs a real migration.

`alembic/versions/0006_create_recipes.py` is superseded but stays in history — Alembic revisions
are append-only.

## 7. Dependencies

```toml
# packages/agentpilot/pyproject.toml
dependencies = [ ..., "jmespath>=1.0" ]

[project.optional-dependencies]
recipe-lua = ["lupa>=2.1"]
```

`jmespath` is already in `uv.lock` transitively (via `boto3`/`botocore`); this promotes it to
direct. `lupa` is currently in the **dev** group, used for testing the Redis Lua scripts; it moves
to an opt-in extra. Both are pure-Python or self-contained, so the Chrome-free gateway image is
unaffected.

## 8. API surface

`POST /v1/recipes` loses `url` and gains `sample_urls`. Response shapes change with the contract.
`tests/golden/` will flag the resulting OpenAPI diff — **that is the intended signal, not a
failure**; regenerate the goldens as part of the change.

Six endpoints are added (see [`recipe-studio.md`](recipe-studio.md)) and seven more for domain
agents (see [`domain-agents.md`](domain-agents.md)).

## 9. Tests

Rewritten against v2, not kept in parallel:

| File | Fate |
|---|---|
| `tests/test_recipe_models.py` | replaced wholesale — every case is a v1 structural round-trip |
| `tests/test_recipe_schema.py` | rewritten for `TypeSpec` / `assertions` |
| `tests/test_recipe_normalize.py` | **behaviour preserved**, restated as transform-list cases. All 18 cases are the correctness bar for the rewrite — see below |
| `tests/test_recipe_evaluate.py`, `test_recipe_multi_selector.py`, `test_recipe_stabilize.py`, `test_recipe_generalize.py`, `test_recipe_locator_proposal.py`, `test_recipe_codegen.py` | updated to v2 shapes |
| `tests/test_recipe_store.py` | updated for the new columns |
| `tests/driver_contract/test_recipe_build_replay_heal.py` | updated; **add a blocked-page scenario** — the §D5.1 gate needs a regression test or it will regress |

New tests worth writing from the start:

- `recipe/lua.py` sandbox escapes: `io`, `os`, `require`, `load`, `debug` all absent; instruction
  and wall-clock limits produce a *field failure*, never a crash.
- `jmespath` locator resolution, including that a malformed expression is a field failure.
- `to_object` / `html_select` / `strip_html` against the fixtures captured from the three worked
  examples.
- A `blocked` classification never triggering a heal.

## 10. Stale artifacts to remove while in the area

These are not v1 recipe code, but they actively mislead anyone reading the layering rules that
this work has to respect:

- `.import_linter_cache/agentpilot.meta.json` and `.import_linter_cache/baas.meta.json` — both
  describe a module layout that no longer exists (`agentpilot.spi`, `agentpilot.driver`,
  `agentpilot.session`, all moved to `crawlpilot`; `baas` is the project's former name).
- `CONTRIBUTING.md` — still documents the pre-split tree and pre-split commands
  (`uv run mypy agentpilot`, `agentpilot.identity` as a strict-mypy layer). Trust
  `pyproject.toml` and `.github/workflows/ci.yml`.

---

## Completeness check

Before calling the rewrite done, re-run:

```bash
cd /Users/rahulbisht/Documents/Github/baas-crawlpilot
for s in RevealStep FieldLocator RepeatSpec FieldNormalization url_pattern _locators_from_dict; do
  echo "--- $s ---"
  grep -rn --include='*.py' --include='*.ts' --include='*.tsx' "$s" \
    packages frontend/src tests alembic 2>/dev/null
done
```

Every surviving hit must be either (a) inside an Alembic revision, which is history and stays, or
(b) accounted for in §1–§5 above. **A live reference this document does not mention is a gap in
the demolition list, not an acceptable leftover.**

Then the standard gates:

```bash
uv run pytest -q --ignore=tests/driver_contract
uv run mypy packages/crawlpilot/src/crawlpilot packages/agentpilot/agentpilot
uv run ruff check packages tests examples
(cd packages/agentpilot && lint-imports)     # and for crawlpilot, agentpilot-client
(cd frontend && npm run lint && npm run build)
```

And the schema round-trip for the worked examples:

```bash
uv run --with jsonschema python -c "
import json, glob
from jsonschema import Draft202012Validator
s = json.load(open('docs/schemas/recipe-v2.schema.json'))
Draft202012Validator.check_schema(s)
v = Draft202012Validator(s)
for f in sorted(glob.glob('docs/examples/recipes/*.json')):
    errs = list(v.iter_errors(json.load(open(f))))
    print(('OK   ' if not errs else f'FAIL({len(errs)}) ') + f)"
```
