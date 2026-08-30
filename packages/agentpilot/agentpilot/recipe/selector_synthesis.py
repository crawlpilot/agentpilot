"""Algorithmic robust-CSS-selector synthesis -- the browser adaptation of
Pulsar's node-feature/uniqueness scoring (`Level1FeatureCalculator.kt`,
`DefinedFeatures.kt`). Given a seed CSS selector that currently resolves to
exactly one element, an in-page JS routine walks that element and emits
additional *ranked, verified-unique* CSS candidates, preferring stable
signals (id, `data-*`/`aria-*`/`itemprop`/`role`/`name`) over positional
paths and penalizing hashed / utility (Tailwind-like) class names.

These candidates are merged with the LLM's proposals in
`locator_proposal.propose_and_verify_fields` so a field ends up with an
ordered fallback list (Pulsar's comma-union/coalesce) rather than one brittle
selector. Pulsar's `:contains`/`:matches`/`:in-box` pseudo-selectors are
JVM-only (jsoup) and unusable via `document.querySelector`; the accessibility
`ax_role` + `name_contains` locator is our text-predicate equivalent, so
synthesis here sticks to native browser CSS (ids, attributes, `:nth-of-type`).
"""

from __future__ import annotations

import json

from agentpilot.recipe.models import FieldLocator
from agentpilot.session.interactive import InteractiveSession, execute_on_session
from agentpilot.session.registry import RegistryProtocol
from agentpilot.spi import actions as spi_actions
from agentpilot.spi.driver import BrowserDriver

# Max synthesized candidates to keep per field (before merge/verify).
_MAX_CANDIDATES = 4

# The synthesizer runs entirely in-page: it resolves the seed element, then
# builds candidates from most- to least-stable, keeping only those that select
# exactly one element. Returns an ordered JSON array of selector strings.
_SYNTH_JS = """
(() => {
  const SEED = %s;
  const seedEl = document.querySelector(SEED);
  if (!seedEl) return [];

  const cssEscape = (window.CSS && CSS.escape) ? CSS.escape : (s) => s.replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
  const isUnique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  // Hashed / utility class heuristic: CSS-modules hashes (foo-a1b2c3), long
  // random tokens, and atomic utilities (px-4, md:flex, text-sm) churn across
  // deploys -- avoid keying selectors on them.
  const stableClasses = (el) => Array.from(el.classList || []).filter((c) =>
    c.length >= 2 && !/(^|[-_])[a-z0-9]{6,}$/i.test(c) && !/[0-9]:|:|\\//.test(c) && !/^(is|has)-/.test(c));

  const STABLE_ATTRS = ['data-testid','data-test','data-qa','data-cy','data-id','itemprop','aria-label','name','role','type'];
  const tag = (el) => el.tagName.toLowerCase();
  const candidates = [];
  const push = (sel) => { if (sel && isUnique(sel) && !candidates.includes(sel)) candidates.push(sel); };

  // Tier 1: id.
  if (seedEl.id) push('#' + cssEscape(seedEl.id));

  // Tier 2: a stable attribute on the element itself.
  for (const a of STABLE_ATTRS) {
    const v = seedEl.getAttribute(a);
    if (v) push(tag(seedEl) + '[' + a + '=' + JSON.stringify(v) + ']');
  }

  // Tier 3: a stable class (or class pair) on the element.
  const cls = stableClasses(seedEl);
  if (cls.length) {
    push(tag(seedEl) + '.' + cls.map(cssEscape).join('.'));
    if (cls.length > 1) push(tag(seedEl) + '.' + cssEscape(cls[0]));
  }

  // Tier 4: scope by nearest stable ancestor (id or stable attr) + tag/nth.
  let anchor = seedEl.parentElement, hops = 0, anchorSel = null;
  while (anchor && hops < 4 && !anchorSel) {
    if (anchor.id) anchorSel = '#' + cssEscape(anchor.id);
    else for (const a of STABLE_ATTRS) {
      const v = anchor.getAttribute(a);
      if (v) { anchorSel = tag(anchor) + '[' + a + '=' + JSON.stringify(v) + ']'; break; }
    }
    anchor = anchor.parentElement; hops++;
  }
  if (anchorSel) {
    const c2 = stableClasses(seedEl);
    if (c2.length) push(anchorSel + ' ' + tag(seedEl) + '.' + cssEscape(c2[0]));
    const parent = seedEl.parentElement;
    if (parent) {
      const idx = Array.from(parent.children).filter((c) => c.tagName === seedEl.tagName).indexOf(seedEl) + 1;
      push(anchorSel + ' ' + tag(seedEl) + ':nth-of-type(' + idx + ')');
    }
  }

  return candidates.slice(0, %d);
})()
"""


async def synthesize_css_candidates(
    seed_selector: str,
    attribute: str,
    *,
    session: InteractiveSession,
    registry: RegistryProtocol,
    driver: BrowserDriver,
) -> list[FieldLocator]:
    """Ranked, verified-unique alternative CSS locators for the element the
    `seed_selector` currently resolves to (same `attribute` read as the seed).
    Empty on any failure -- synthesis is a best-effort robustness *addition*,
    never a hard requirement (the seed locator already works)."""

    script = _SYNTH_JS % (json.dumps(seed_selector), _MAX_CANDIDATES)
    result = await execute_on_session(
        session, [spi_actions.ExecuteJsAction(script=script)], registry=registry, driver=driver
    )
    selectors = result.js_returns[0] if result.js_returns else None
    if not isinstance(selectors, list):
        return []
    return [
        FieldLocator(source="css", selector=sel, attribute=attribute)
        for sel in selectors
        if isinstance(sel, str) and sel and sel != seed_selector
    ]
