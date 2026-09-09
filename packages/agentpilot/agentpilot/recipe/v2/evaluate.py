"""Resolving a v2 `Locator` and a v2 `Predicate` against the live page.

This is the browser-dependent half of replay; the ordering rules that decide
*which* locator wins live in `resolve.py` and are tested without a browser.

**Where XPath is implemented, and why here.** `crawlpilot/driver/queries.py`
resolves a selector with `document.querySelector`, not the Playwright locator
engine, so `xpath=` is not free anywhere in the stack. Reads are already done
from this layer with a generated JS snippet (v1 did exactly that for CSS), so
adding a `document.evaluate` branch alongside it costs nothing and touches no
strict-mypy, golden-tested `crawlpilot` code. The driver-side work is still
worth doing eventually -- but it is needed for *acting* on an XPath (clicking a
cell selected by its sibling's text), not for reading one, and reading is what
unblocks the recipes today.

One xpath caveat, stated because it will bite someone: `document.evaluate`
with a `//`-prefixed expression searches the whole document even when given a
context node, so `within` scoping does not constrain such an expression. Use a
`.//`-relative expression when scoping matters. The evaluator does not rewrite
the author's expression, because silently changing what a selector means is
worse than a documented sharp edge.
"""

from __future__ import annotations

import json
from typing import Any

from agentpilot.recipe.v2.models import Locator, Predicate
from agentpilot.recipe.v2.paths import PathError, resolve_path
from crawlpilot.session.interactive import InteractiveSession, execute_on_session
from crawlpilot.session.registry import RegistryProtocol
from crawlpilot.spi import actions as spi_actions
from crawlpilot.spi.dom_tree import EnhancedDOMTreeNode
from crawlpilot.spi.driver import BrowserDriver

_SOURCE_TO_CONTAINER = {"json_ld": "json_ld", "hydration": "hydration", "meta": "metadata"}

# One reader for css and xpath. Options are passed as a single JSON object
# rather than interpolated into the source, so a selector containing a quote is
# data rather than syntax -- the same discipline `driver/queries.py` uses.
_READ_JS = """() => {
  const opts = %s;
  const pick = (root, sel, isXpath) => {
    try {
      if (isXpath) {
        const r = document.evaluate(sel, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const out = [];
        for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i));
        return out;
      }
      return Array.from(root.querySelectorAll(sel));
    } catch (e) { return null; }
  };
  // `text` is textContent MINUS script/style/template subtrees. Raw
  // textContent includes the source of any <script> inside the element, which
  // on real pages is not an edge case: Amazon's #availability contains an
  // inline P.when(...) block, and its "Customer Reviews" spec row carries a
  // dpAcrHasRegisteredArcLinkClickAction handler. Both would otherwise be
  // returned as the field's value -- well-formed, plausible, and garbage.
  // innerText excludes them for free but also excludes collapsed content,
  // which is precisely what `text` exists to reach.
  const SKIP = {SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1};
  const textOf = (el) => {
    let out = '';
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1 || SKIP[n.tagName]) return;
      for (let c = n.firstChild; c; c = c.nextSibling) walk(c);
    };
    walk(el);
    return out;
  };
  const read = (el) => {
    if (!el) return null;
    const a = opts.attribute || 'text';
    if (a === 'text') { const t = textOf(el); return t == null ? null : t.trim(); }
    if (a === 'visible_text') return el.innerText == null ? null : el.innerText.trim();
    if (a === 'html') return el.outerHTML == null ? null : el.outerHTML;
    if (a === 'value') {
      if (el.value !== undefined && el.value !== null) return el.value;
      return el.getAttribute('value');
    }
    return el.getAttribute(a);
  };
  let root = document;
  if (opts.within) {
    const containers = pick(document, opts.within.selector, opts.within.kind === 'xpath');
    if (containers === null) return {error: 'invalid within selector'};
    if (!containers.length) return {value: opts.all ? [] : null};
    root = containers[0];
  }
  const nodes = pick(root, opts.selector, opts.kind === 'xpath');
  if (nodes === null) return {error: 'invalid selector'};
  if (opts.all) return {value: nodes.map(read).filter(v => v !== null)};
  const i = (opts.index === null || opts.index === undefined) ? 0 : opts.index;
  const el = i < 0 ? nodes[nodes.length + i] : nodes[i];
  return {value: read(el)};
}"""

# Rows of a `dom_rows` repeat, read in one pass.
#
# The row element is the *root* each column resolves against, which is what
# `within` cannot express: `within` resolves to `containers[0]`, so a
# row-scoped read through it would silently return row one, every time. Here
# the root is supplied by the iteration, exactly as `_rows_from_json` supplies
# the current array member.
#
# A column that matches nothing in a given row yields null for that row rather
# than shifting -- which is the entire point of reading row-wise instead of
# collecting each column with `all: true` and zipping by index.
_READ_ROWS_JS = """() => {
  const opts = %s;
  const pick = (root, sel, isXpath) => {
    try {
      if (isXpath) {
        const r = document.evaluate(sel, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        const out = [];
        for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i));
        return out;
      }
      return Array.from(root.querySelectorAll(sel));
    } catch (e) { return null; }
  };
  const SKIP = {SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1};
  const textOf = (el) => {
    let out = '';
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeType !== 1 || SKIP[n.tagName]) return;
      for (let c = n.firstChild; c; c = c.nextSibling) walk(c);
    };
    walk(el);
    return out;
  };
  const read = (el, a) => {
    if (!el) return null;
    a = a || 'text';
    if (a === 'text') { const t = textOf(el); return t == null ? null : t.trim(); }
    if (a === 'visible_text') return el.innerText == null ? null : el.innerText.trim();
    if (a === 'html') return el.outerHTML == null ? null : el.outerHTML;
    if (a === 'value') {
      if (el.value !== undefined && el.value !== null) return el.value;
      return el.getAttribute('value');
    }
    return el.getAttribute(a);
  };

  let root = document;
  if (opts.rows.within) {
    const containers = pick(document, opts.rows.within.selector, opts.rows.within.kind === 'xpath');
    if (containers === null) return {error: 'invalid within selector'};
    if (!containers.length) return {rows: []};
    root = containers[0];
  }
  const rowEls = pick(root, opts.rows.selector, opts.rows.kind === 'xpath');
  if (rowEls === null) return {error: 'invalid rows selector'};

  const rows = rowEls.map((rowEl) => {
    const row = {};
    for (const col of opts.columns) {
      const nodes = pick(rowEl, col.selector, col.kind === 'xpath');
      if (nodes === null || !nodes.length) { row[col.name] = null; continue; }
      if (col.all) {
        row[col.name] = nodes.map((n) => read(n, col.attribute)).filter((v) => v !== null);
        continue;
      }
      const i = (col.index === null || col.index === undefined) ? 0 : col.index;
      row[col.name] = read(i < 0 ? nodes[nodes.length + i] : nodes[i], col.attribute);
    }
    return row;
  });
  return {rows: rows};
}"""

_COUNT_JS = """() => {
  const opts = %s;
  try { return document.querySelectorAll(opts.selector).length; } catch (e) { return -1; }
}"""

_VISIBLE_JS = """() => {
  const opts = %s;
  let el;
  try { el = document.querySelector(opts.selector); } catch (e) { return null; }
  if (!el) return false;
  const style = getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
    return false;
  }
  const box = el.getBoundingClientRect();
  return box.width > 0 && box.height > 0;
}"""

_TEXT_PRESENT_JS = """() => {
  const opts = %s;
  return (document.body ? document.body.innerText : '').includes(opts.text);
}"""


class LocatorError(Exception):
    """A locator could not be evaluated -- a malformed selector or path. A
    locator that evaluates fine and matches nothing is NOT this: that is an
    empty field, which the candidate list exists to handle."""


class PageReader:
    """Everything a recipe needs to read from one page, with the per-group
    caches that make it affordable.

    `structured_data` is fetched once per reader and shared by every field in
    the group. A group reading twelve fields out of `__NEXT_DATA__` should cost
    one extraction, not twelve -- Walmart's blob is 352 KB.
    """

    def __init__(
        self,
        *,
        session: InteractiveSession,
        registry: RegistryProtocol,
        driver: BrowserDriver,
        base_url: str = "",
    ) -> None:
        self._session = session
        self._registry = registry
        self._driver = driver
        self.base_url = base_url
        self._structured: dict[str, Any] | None = None
        self._snapshot: EnhancedDOMTreeNode | None = None
        self._url: str | None = None

    def invalidate(self) -> None:
        """Drop the caches. Called after any step that mutates the page --
        holding a snapshot across a click is how a recipe reads the state it
        was trying to change."""

        self._structured = None
        self._snapshot = None
        self._url = None

    async def current_url(self) -> str:
        """Where the page actually is now.

        `base_url` is where it was *asked* to go. The two diverge the moment a
        click navigates, and telling them apart is what says whether two reads
        happened on the same page at all.
        """

        if self._url is None:
            got = await self._eval_js("location.href")
            self._url = str(got) if isinstance(got, str) else ""
        return self._url

    async def structured_data(self) -> dict[str, Any]:
        if self._structured is None:
            result = await execute_on_session(
                self._session,
                [spi_actions.ExtractAction(format="structured_data")],
                registry=self._registry,
                driver=self._driver,
            )
            if result.extracts:
                self._structured = dict(json.loads(result.extracts[0]))
            else:
                self._structured = {"metadata": {}, "json_ld": [], "hydration": {}}
        return self._structured

    async def snapshot(self) -> EnhancedDOMTreeNode | None:
        if self._snapshot is None:
            result = await execute_on_session(
                self._session,
                [spi_actions.SnapshotAction()],
                registry=self._registry,
                driver=self._driver,
            )
            self._snapshot = result.fused_trees[0] if result.fused_trees else None
        return self._snapshot

    async def _eval_js(self, script: str) -> Any:
        result = await execute_on_session(
            self._session,
            [spi_actions.ExecuteJsAction(script=script)],
            registry=self._registry,
            driver=self._driver,
        )
        return result.js_returns[0] if result.js_returns else None

    async def read_rows(
        self, rows_locator: Locator, columns: dict[str, Locator]
    ) -> list[dict[str, Any]] | None:
        """Resolve N row elements and read every column *relative to its row*.

        One round trip, not N x M. A results page with 48 rows and 6 columns
        would otherwise cost 288 `execute_js` calls, and the rows would be read
        at 288 different moments -- on a page that lazy-loads or re-renders,
        that is not a snapshot of anything.

        Returns `None` when the locator kind cannot address DOM elements, which
        is the caller's signal that this recipe is malformed rather than that
        the page was empty.
        """

        if rows_locator.kind not in ("css", "xpath"):
            return None

        cols = [
            {
                "name": name,
                "kind": loc.kind,
                "selector": loc.selector or "",
                "attribute": loc.attribute,
                "all": loc.all,
                "index": loc.index,
            }
            for name, loc in columns.items()
            if loc.kind in ("css", "xpath")
        ]
        opts = {
            "rows": {
                "kind": rows_locator.kind,
                "selector": rows_locator.selector or "",
                "within": (
                    {"kind": rows_locator.within.kind, "selector": rows_locator.within.selector}
                    if rows_locator.within is not None
                    else None
                ),
            },
            "columns": cols,
        }
        out = await self._eval_js(_READ_ROWS_JS % json.dumps(opts))
        if isinstance(out, dict) and out.get("error"):
            raise LocatorError(f"{out['error']}: {rows_locator.selector!r}")
        rows = out.get("rows") if isinstance(out, dict) else None
        return rows if isinstance(rows, list) else []

    async def read(self, locator: Locator) -> Any:
        """Resolve one locator, returning its raw value (pre-transform)."""

        if locator.kind in _SOURCE_TO_CONTAINER:
            data = await self.structured_data()
            container = data.get(_SOURCE_TO_CONTAINER[locator.kind])
            try:
                return resolve_path(container, locator.path or "", locator.path_lang)
            except PathError as exc:
                raise LocatorError(str(exc)) from exc

        if locator.kind in ("css", "xpath"):
            opts = {
                "kind": locator.kind,
                "selector": locator.selector or "",
                "attribute": locator.attribute,
                "all": locator.all,
                "index": locator.index,
                "within": (
                    {"kind": locator.within.kind, "selector": locator.within.selector}
                    if locator.within is not None
                    else None
                ),
            }
            out = await self._eval_js(_READ_JS % json.dumps(opts))
            if isinstance(out, dict) and out.get("error"):
                raise LocatorError(f"{out['error']}: {locator.selector!r}")
            return out.get("value") if isinstance(out, dict) else None

        if locator.kind in ("ax_role", "text"):
            return await self._read_tree(locator)

        raise LocatorError(f"unknown locator kind {locator.kind!r}")

    async def _read_tree(self, locator: Locator) -> Any:
        """Read from the fused accessibility+DOM tree.

        v1 could only ever return the accessible name here, so an `ax_role`
        locator could never read an href. The fused node carries its
        attributes, so v2 reads them -- with `attribute="text"` preserving the
        old accessible-name behaviour as the default.
        """

        from agentpilot.recipe.v2.tree import find_nodes, node_attribute

        snap = await self.snapshot()
        if snap is None:
            return None
        matches = find_nodes(snap, locator)
        if not matches:
            return None
        if locator.all:
            values = [node_attribute(n, locator.attribute) for n in matches]
            return [v for v in values if v is not None]
        index = locator.index or 0
        if not -len(matches) <= index < len(matches):
            return None
        return node_attribute(matches[index], locator.attribute)

    async def holds(self, predicate: Predicate, *, meta: dict[str, Any] | None = None) -> bool:
        """Evaluate a guard. Side-effect free, and **false rather than raising**
        when it cannot be evaluated: a guard that explodes is worse than a
        guard that declines."""

        try:
            return await self._holds(predicate, meta or {})
        except Exception:  # noqa: BLE001 - see the docstring; this is the contract
            return False

    async def _holds(self, p: Predicate, meta: dict[str, Any]) -> bool:
        if p.kind in ("selector_present", "selector_absent"):
            count = await self._eval_js(_COUNT_JS % json.dumps({"selector": p.selector or ""}))
            present = isinstance(count, (int, float)) and count > 0
            return present if p.kind == "selector_present" else not present

        if p.kind == "visible":
            got = await self._eval_js(_VISIBLE_JS % json.dumps({"selector": p.selector or ""}))
            return bool(got)

        if p.kind == "count_at_least":
            count = await self._eval_js(_COUNT_JS % json.dumps({"selector": p.selector or ""}))
            return isinstance(count, (int, float)) and count >= (p.n or 1)

        if p.kind == "text_present":
            got = await self._eval_js(_TEXT_PRESENT_JS % json.dumps({"text": p.text or ""}))
            return bool(got)

        if p.kind == "url_matches":
            from fnmatch import fnmatch

            pattern = p.url or ""
            return fnmatch(self.base_url, pattern) or pattern in self.base_url

        if p.kind == "json_path_present":
            data = await self.structured_data()
            container = data.get(_SOURCE_TO_CONTAINER.get(p.source or "", ""), None)
            value = resolve_path(container, p.path or "")
            return value is not None and value != [] and value != {}

        if p.kind == "meta_equals":
            return str(meta.get(p.key or "", "")) == str(p.value or "")

        return False


async def detect_variant(
    variants: list[Any], reader: PageReader, *, meta: dict[str, Any] | None = None
) -> str | None:
    """The first fully-satisfied variant, by priority.

    Returning `None` when nothing matches is *degraded, not failed*: the recipe
    still runs, and only its variant-agnostic candidates apply. It is also a
    heal trigger, because "no variant matched" usually means the site shipped a
    layout nobody has seen yet.
    """

    for variant in sorted(variants, key=lambda v: v.priority):
        if not variant.detect:
            continue
        satisfied = True
        for predicate in variant.detect:
            if not await reader.holds(predicate, meta=meta):
                satisfied = False
                break
        if satisfied:
            return str(variant.variant_id)
    return None
