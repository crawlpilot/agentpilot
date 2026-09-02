# Recipe operations: correctness, completeness, drift, and heal safety

**Status:** specification. The *why* behind the quality fields in
[`recipe-contract-v2.md`](recipe-contract-v2.md) §9 and §10.

A recipe that extracts correctly today is easy. A recipe that is still extracting correctly in
six weeks, across ten thousand pages, and that tells you honestly when it is not — that is the
whole problem. This document is about the second one.

Everything marked **MEASURED** was observed on a live page with real Chrome on 2026-09-02, not
inferred.

---

## D1. The failure taxonomy

Ordered by how hard each is to detect, which is roughly the inverse of how often systems design
for it.

| # | Failure | Detected by | Status today |
|---|---|---|---|
| 1 | **Hallucinated locator** — never resolves | build-time verify | ✅ handled: `propose_and_verify_fields` keeps only resolvers and retries once with feedback |
| 2 | **Overfit locator** — resolves on the build page only | replay on a *different* page of the same type | ❌ build explores exactly one URL |
| 3 | **Incomplete extraction** — missing rows, sections, variants | row-count expectations, completeness probes | ❌ truncates silently |
| 4 | **Right shape, wrong value** — resolves, plausible type, wrong element | assertions, cross-source agreement, sampled judge | ❌ nothing checks |
| 5 | **Non-determinism** — A/B layout, personalisation, geo, auth state | repeat runs, variant detection | ⚠️ variants are new in v2 |
| 6 | **Drift** — class churn, attribute rename, redesign | fill-rate + candidate-demotion trends | ❌ only hard failure |
| 7 | **Blocked ≠ broken** — a bot wall read as missing data | page classification | ❌ **actively dangerous — §D5** |
| 8 | **Authoring-time prompt injection** — page content steers the recipe author | sandboxing + review gate | ⚠️ the *extract* prompt has a guard; the *build* prompt does not |

Class 4 is the one that quietly poisons a dataset, and it is not hypothetical:

> **MEASURED (Walmart).** The product page's own `__NEXT_DATA__` contains a sponsored competitor
> listing at `contentLayout.modules[1].configs.ad.adContentV2.data.products[0]` — *"St. Ives
> Soothing Body Wash for Women, Oatmeal & Shea Butter, 22 fl oz"*, with its own `name`, `brand`
> and `priceInfo`, sitting in the same document as the Dove product being scraped. An unanchored
> `$..name` matches **203 nodes** on that page and returns the St. Ives string among its first
> few distinct results.

Nothing errors. The types are right. The values are plausible. You just silently collect a
competitor's price under your product's ID, every run, forever.

> **MEASURED (Amazon).** In the 22-row spec table, the row `Customer Reviews` extracts as
> `'4.9 4.9 out of 5 stars (13,222) var dpAcrHasRegisteredArcLinkClickAction…'` — inline
> JavaScript leaking into the text. `Best Sellers Rank` similarly carries
> `(See Top 100 in Toys & Games)` navigation chrome.

A key→value spec map built by naively pairing `th`/`td` ships both as data.

Class 2 is the most *common*, class 7 the most *destructive*.

---

## D2. Why completeness is harder than correctness

Correctness is checkable against the page in front of you. Completeness requires knowing what you
did **not** see: a section behind a click nobody tried, row 21 when the cap is 20, the variant
that only renders when an item is out of stock.

Concretely, today:

- `_replay_repeat_group` computes `n = min(len(initial_refs), repeat.max_iterations)`, breaks
  early when the option set shrinks, and appends **only rows where every field resolved**. A run
  that captured 8 of 40 sizes and dropped 12 partial rows returns `success=True` with no signal.
  `RecipeConfig.max_repeat_iterations` defaults to **20**.
- `build.py` carries a documented v1 limitation in-comment: a scalar field satisfied in the same
  batch as an array field's representative click **loses that click from its reveal path**.
- Nothing distinguishes "the agent finished" from "the agent ran out of steps".

The fix is not cleverness, it is **declared expectation**. `FieldGroup.expect.min_rows` and the
result's `truncated` map (contract §9, §10) let a recipe state how much it should have found, so
finding less is a reportable event rather than a quiet success.

> A recipe that cannot say how much it should have found can never report that it found too
> little.

### Completeness is also a *sourcing* question

> **MEASURED (Walmart).** Six accordion sections — Product details, Specifications, Indications,
> Directions, Ingredients, About the brand — are all fully populated in `__NEXT_DATA__` before
> any click. In the rendered DOM, across two loads with the full page scrolled: only **2 of 9**
> `.expand-collapse-content` elements had content, and the Ingredients and Directions text was
> **absent from `document.body.innerText` on both runs**. Body text length itself varied between
> otherwise identical loads: **18,525 vs 32,183 characters**.

So on that page a DOM-only recipe cannot collect ingredients or directions *at all* — not
because the selector is wrong, but because the data is never painted. Completeness therefore
depends on choosing the right *source*, not only on iterating far enough. This is why the
contract orders candidates by source and why
[`recipe-json-extraction.md`](recipe-json-extraction.md) exists.

---

## D3. What the industry does, and which of it applies

| Approach | Who | Trade-off | Applies here? |
|---|---|---|---|
| **Multi-page wrapper induction** — induce from N pages of one template, generalise, reject rules fitting only one | the classical literature; still the backbone of commercial template extractors | trades precision for generality; needs N pages | **Yes — the highest-leverage fix available.** Build uses one URL today |
| **ML/CV + ontology, no per-site rules** | Diffbot | robust, zero setup, but you get *their* schema | No — the caller declares the schema here |
| **Per-vertical trained models** | Zyte AutoExtract | zero per-site setup, fixed vertical schema | No, same reason |
| **Point-and-click + generalise to siblings** | Octoparse, ParseHub, Import.io, Browse AI | human-anchored, reliable; Browse AI markets pattern matching over fixed CSS selectors | **Yes — this is the studio's centre pane** |
| **LLM per page** | Firecrawl `extract`, ScrapeGraphAI | maximum flexibility, worst economics and determinism | Exists here as `llm/schema_extract.py`; recipes exist to escape it |
| **LLM authors a deterministic extractor, cached, regenerated on failure** | Kadoa; Firecrawl's `deterministicJson` | best economics; needs a validation story | **This repo's architecture** |
| **Agent per page** | Skyvern, browser-use | handles anything; same economics problem | Exists as the agent loop; right as a *fallback*, wrong as the default |
| **Structured-data-first** | everyone | most redesign-resistant *when present* | ✅ already the `_SOURCE_RANK` preference — but see Amazon |

Two findings worth importing wholesale. Kadoa reports that LLM first-attempt selectors fail to
yield data roughly **30–40%** of the time, which is why validation-first authoring — generate,
test against the live page, feed the diagnostic back, bounded retry — is the industry norm; this
repo already does exactly that in `propose_and_verify_fields`. More importantly, Kadoa validates a
*regenerated* extractor **against previously extracted data**, not merely against "does it
resolve". That is precisely the gate `check_and_heal` lacks (§D5).

---

## D4. The validation and monitoring model

Layered, cheapest first. Only the last needs a model.

**1. Multi-page induction at build.** Require `sample_urls` (≥3 same-type pages). Keep a candidate
only if it resolves on all of them, and record `verified_on`. This alone removes most of failure
class 2 and is the difference between an induced wrapper and a one-page guess. It is why every
example recipe in `docs/examples/recipes/` carries an explicit note that it has `verified_on: 1`
and must not leave `draft`.

**2. Golden fixtures.** Freeze the DOM snapshot per sample URL at build time; replay offline in
CI. This is the only mechanism that cleanly separates *our recipe broke* from *the site changed*
from *we were blocked*. The pipeline already snapshots — this is storage, not new capability.

**3. Per-field assertions.** Range, regex, enum set, length band, `not_empty`. Catches class 4
without a model: a price of `0.0`, a currency outside ISO-4217, an ASIN that is not ten
characters. Cheap enough to run on every field on every run.

**4. Cross-source agreement.** When a field has candidates from two different source families,
evaluate both and compare. Agreement is strong evidence; disagreement is a high-signal drift
alarm. **This is nearly free and entirely unused today** — most fields already carry both a
structured-data and a DOM candidate, and only the first is ever read. It is the specific defence
against the St. Ives ad and against a price selector drifting onto a "compare at" value.

**5. Statistical drift over a rolling window.** Per-field fill rate, null rate, distinct-value
cardinality, value-length distribution, and candidate-demotion frequency from `provenance`. This
is where *"attribute collection drops"* is actually detected — as a data-quality trend, not an
exception. It matches the field-level-completeness / distribution-drift / coverage metric set
that production scraping monitoring converges on.

**6. Canary URLs.** A small pinned set per recipe on a tighter schedule than the bulk job, so a
redesign surfaces in minutes rather than at the next full run.

**7. Sampled LLM judge.** `agent/judge.py` already exists: a strict, skeptical verifier that can
only veto, never fabricate a pass, and fails open. Reuse it on a cost-bounded sample to grade
extracted values against the page, and as a **completeness probe** — "is there a section on this
page matching field X that was not extracted?"

### The metrics table

A `recipe_field_metrics` rollup, written per run and aggregated per field:

| Metric | Alarms when |
|---|---|
| `fill_rate` | drops below its trailing baseline by more than a threshold |
| `null_rate`, `empty_rate` | rise |
| `suspect_rate` | assertions start failing |
| `fallback_rate` | the primary candidate stops winning — drift, pre-breakage |
| `distinct_cardinality` | collapses (every product suddenly has the same price) |
| `truncated_rate` | tables start hitting their caps |
| `blocked_rate` | separates infrastructure problems from recipe problems |

`recipe_runs` already stores per-run data and failures, so these are computable from rows that
exist.

---

## D5. Heal safety — three gates the current design lacks

Today: replay → if any field failed, drop the affected groups → re-explore them with the LLM →
accept if the new version replays clean. Three ways that goes wrong.

### D5.1 Healing against a bot wall — the live landmine

`session/interactive.py:104` sets `detect_blocks: bool = False`, and
`packages/crawlpilot/tests/test_stealth_profile.py:142` is an explicit regression guard for it:

> "A scrape has an escalation ladder to answer `ChallengeDetected` with; a session does not, so
> raising mid-run only converts the situation into a failed run. […] turning detection on for the
> interactive path made every agent-loop, recipe and session-lifecycle contract test fail with
> `ChallengeDetected: empty` on ordinary thin pages."

That reasoning is correct for sessions. But recipe build, replay **and heal** all run on
`InteractiveSession`. So a challenge page produces "every field failed", `check_and_heal` reads
that as layout drift, re-explores **against the wall**, and writes a new version of the recipe
derived from a CAPTCHA. `max_heal_attempts = 3` bounds the thrash; it does not prevent the
corruption. A working recipe is replaced by a broken one, and the version that gets promoted is
the broken one.

> **MEASURED.** A plain HTTP fetch of the Zara product URL returns **403**. The same URL in real
> Chrome with a warm profile returns the full page. The difference between "this recipe is
> broken" and "this fetch was refused" is invisible to anything that only looks at whether
> fields resolved.

**Gate: classify the page before evaluating fields; `outcome: "blocked"` never triggers a heal.**

Do this recipe-side, **not** by re-enabling the session-wide `ChallengeDetected` raise — that
default exists for a good reason and the regression test protects it. The pieces are already
built: `crawlpilot/extraction/block_detect.py` (`classify_page`, the `Verdict` taxonomy ported
from Pulsar's `HtmlIntegrity`) and `agentpilot/control/retail_extension.py`'s per-site checkers.
The recipe executor calls the classifier and branches; the session's behaviour is untouched.

Note the asymmetry that makes this urgent: a blocked run that is *treated* as blocked costs one
wasted fetch. A blocked run treated as drift costs the recipe.

### D5.2 Healing to a wrong-but-resolving selector

Replay success means "fields resolved", not "values are right". A heal that repoints `price` at
the sponsored ad's `$5.22` passes every check the current design applies, and keeps passing.

**Gate: a healed version must clear the same assertions, reproduce values on the golden fixtures,
and be diffed against the previous version's output on the same sample URLs before promotion.**
Regression, not resolution — Kadoa's "validate against previously extracted data". The studio
shows that diff side by side (see [`recipe-studio.md`](recipe-studio.md)) so a human can reject a
heal that "fixed" a field by pointing it somewhere plausible and wrong.

### D5.3 Heal thrash and silent degradation

`max_heal_attempts` caps attempts, but a heal that "succeeds" with a *worse* recipe still
replaces a better one. `recipe_versions` is append-only, so rollback is possible — it is simply
not wired.

**Gate: automatic rollback when the new version's fill rate is worse than the previous version's
on the same fixtures; `draft` status pending human review when the comparison is ambiguous.**
Plus a per-tenant heal budget alongside `max_heal_attempts`: a site-wide redesign triggers every
recipe for that domain simultaneously, and heal is the only LLM-expensive path in the system.

---

## D6. Operational realities beyond correctness

**Geo, locale and delivery gating.** The same URL serves different content by IP and by saved
address.

> **MEASURED (Amazon).** `.a-price .a-offscreen` resolved to the empty string, and `#availability`
> read *"This item cannot be shipped to your selected delivery location."* The page rendered
> perfectly. There was simply no price to collect from where we were standing.

A recipe built through one proxy pool and replayed through another can legitimately find nothing.
`Recipe.built_under` records tier and region; replaying from elsewhere should warn, and a missing
price under a geo-gate should read as `suspect`, never as a failed run or a silent null. Per-
(tenant, tier) proxy pools already exist in `control/proxy_config.py`; the recipe just needs to
remember which one it was born under.

**Auth and consent state.** Logged-out vs logged-in, cookie walls, region interstitials. These
are `global_setup` steps with `on_error: "continue"` when they merely overlay, and page variants
when they change the layout. Note that a consent banner absent on a warm profile and present on a
cold one is a *variance*, not a bug — the Zara recipe's dismiss step is written to tolerate both.

**Non-determinism is real and measurable.** Two identical Walmart loads produced body text of
18,525 and 32,183 characters, and the "Product details" heading was present on one earlier load
and absent on two later ones. Any threshold tuned on a single observation will be wrong.

**Cost.** Replay is LLM-free by design; heal is not. Budget it per tenant, not just per recipe.

**Load.** `replay.py` re-navigates before **every field group** — deliberate, to avoid the
double-dismiss-a-cookie-banner failure, and empirically justified:

> **MEASURED (Zara).** Clicking *COMPOSITION, CARE & ORIGIN* while the *PRODUCT MEASUREMENTS*
> drawer was open **timed out** — the open drawer covers the other button. Two reveal steps that
> each work perfectly in isolation break when run in sequence.

But it costs O(groups) page loads per run, which at catalogue scale is a rate-limit and spend
problem. The optimisation — merging groups whose steps provably do not mutate shared state — is
worth doing and is an executor concern, not a contract change.

**Prompt injection at authoring time.** `schema_extract.py`'s prompt explicitly instructs the
model to treat page text as data and never as instruction. The **build and locator-proposal
prompts have no such guard**, and they are the ones reading untrusted pages in order to author
executable Lua. Adding it is a security requirement, not a nicety, and it is cheap.

**Legal, robots and ToS.** Out of scope for the contract, squarely in scope for the operator.
`crawl/robots.py` exists.

---

## D7. What this document forces back into the contract

All folded into [`recipe-contract-v2.md`](recipe-contract-v2.md) §6, §9 and §10:

`sample_urls` · `verified_on` · `assertions` · `expect.min_rows` · `truncated` · `provenance` ·
`outcome: "blocked"` · `field_status: "suspect"` · `built_under` · `has_script` ·
`draft → approved → published`

Part D is the justification for each; the contract is the normative definition. If a field in
this list ever looks like ceremony, the corresponding row in §D1 is the reason it is there.

---

## Sources

- [How AI Is Changing Web Scraping in 2026 — Kadoa](https://www.kadoa.com/blog/how-ai-is-changing-web-scraping-2026)
- [Introducing Self-Healing Web Scrapers — Kadoa](https://www.kadoa.com/blog/autogenerate-self-healing-web-scrapers)
- [The Best AI Web Scrapers of 2026: An Honest Review — Kadoa](https://www.kadoa.com/blog/best-ai-web-scrapers-2026)
- [AI web scraping tools compared (2026) — Browse AI](https://www.browse.ai/blog/the-best-ai-web-scraper-tools)
- [Web scraping tools comparison (2026) — Browse AI](https://www.browse.ai/blog/web-scraping-tools-comparison-guide)
- [Diffbot vs Zyte — SelectHub](https://www.selecthub.com/data-extraction-tools/diffbot-vs-zyte/)
- [Scraper Monitoring in Production — Context.dev](https://www.context.dev/blog/scraper-monitoring-in-production)
- [Web Scraping Monitoring & Failure Detection Guide 2026 — PromptCloud](https://www.promptcloud.com/blog/web-scraping-monitoring-challenges/)
- [Data Quality Assurance in Web Scraping Pipelines — Grepsr](https://www.grepsr.com/blog/data-quality-assurance-web-scraping-grepsr/)
- [AMBER: Automatic Supervision for Multi-Attribute Extraction (arXiv)](https://arxiv.org/pdf/1210.5984)
- [Web wrapper induction: a brief survey](https://www.researchgate.net/publication/220308841_Web_wrapper_induction_A_brief_survey)
- [Learning Robust Web Wrappers](https://www.researchgate.net/publication/221464180_Learning_Robust_Web_Wrappers)
