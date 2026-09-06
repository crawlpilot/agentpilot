/**
 * Run the recipe's bindings against the live page and report what comes back.
 *
 * This is the validation step: an author has named some fields and reordered
 * some selectors, and the only question that matters is whether the thing
 * actually yields the data they expect. Everything up to here is a claim about
 * the page; this is the check.
 *
 * **It is a faithful port of `recipe/v2/evaluate.py::_READ_JS` and
 * `resolve.py::resolve_field`, not an approximation.** That is the whole
 * point. A preview that reads elements even slightly differently from replay
 * -- `textContent` instead of the script-stripped walk, first-match instead of
 * `index`, ignoring `within` -- would show green for a recipe that returns
 * garbage in production, which is worse than showing nothing. Where this file
 * looks over-careful, it is matching the engine.
 *
 * What it deliberately does NOT do is apply transforms. Those run server-side
 * (`transform.py`, including Lua) and cannot be reproduced here honestly, so
 * the preview reports the *pre-transform* value and says so. Showing a
 * plausible-looking transformed value computed by different code would be the
 * same lie in a different place.
 */

/** A locator, flattened to what the reader needs. Mirrors `Locator`. */
export interface PreviewLocator {
  kind: 'css' | 'xpath'
  selector: string
  attribute?: string
  all?: boolean
  index?: number | null
  within?: { kind: 'css' | 'xpath'; selector: string }
}

export interface PreviewField {
  name: string
  required?: boolean
  /** In priority order. The first that yields a non-empty value wins. */
  candidates: PreviewLocator[]
}

export type PreviewStatus = 'resolved' | 'fallback' | 'empty' | 'failed' | 'error'

export interface PreviewResult {
  name: string
  status: PreviewStatus
  /** Pre-transform, exactly as the page gave it. */
  value: string | string[] | null
  /** Which candidate produced it, 1-based; null when none did. */
  candidate: number | null
  /** How many nodes the winning selector matched. */
  matches: number
  error?: string
}

/**
 * The in-page reader.
 *
 * Kept as a source string rather than a function so it is unambiguous that
 * this runs in the *remote* page, and so it stays diffable against the Python
 * original it is ported from.
 */
export const PREVIEW_JS = `(fields) => {
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

  // textContent minus script/style/template subtrees. Raw textContent would
  // include the source of any inline <script>, which on real pages is common
  // enough to matter -- see the note in evaluate.py.
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

  const read = (el, attribute) => {
    if (!el) return null;
    const a = attribute || 'text';
    if (a === 'text') { const t = textOf(el); return t == null ? null : t.trim(); }
    if (a === 'visible_text') return el.innerText == null ? null : el.innerText.trim();
    if (a === 'html') return el.outerHTML == null ? null : el.outerHTML;
    if (a === 'value') {
      if (el.value !== undefined && el.value !== null) return el.value;
      return el.getAttribute('value');
    }
    return el.getAttribute(a);
  };

  const evaluate = (opts) => {
    let root = document;
    if (opts.within) {
      const containers = pick(document, opts.within.selector, opts.within.kind === 'xpath');
      if (containers === null) return {error: 'invalid within selector'};
      if (!containers.length) return {value: opts.all ? [] : null, matches: 0};
      root = containers[0];
    }
    const nodes = pick(root, opts.selector, opts.kind === 'xpath');
    if (nodes === null) return {error: 'invalid selector'};
    if (opts.all) {
      return {value: nodes.map((n) => read(n, opts.attribute)).filter((v) => v !== null), matches: nodes.length};
    }
    const i = (opts.index === null || opts.index === undefined) ? 0 : opts.index;
    const el = i < 0 ? nodes[nodes.length + i] : nodes[i];
    return {value: read(el, opts.attribute), matches: nodes.length};
  };

  // Empty means "did not produce a value", which is what makes the ordered
  // candidate list a fallback chain rather than a list of equals.
  const isEmpty = (v) =>
    v === null || v === undefined ||
    (typeof v === 'string' && v.trim() === '') ||
    (Array.isArray(v) && v.length === 0);

  return fields.map((field) => {
    let firstError = null;
    for (let i = 0; i < field.candidates.length; i++) {
      const out = evaluate(field.candidates[i]);
      if (out.error) { if (!firstError) firstError = out.error; continue; }
      if (isEmpty(out.value)) continue;
      return {
        name: field.name,
        status: i === 0 ? 'resolved' : 'fallback',
        value: out.value,
        candidate: i + 1,
        matches: out.matches,
      };
    }
    return {
      name: field.name,
      status: firstError ? 'error' : (field.required ? 'failed' : 'empty'),
      value: null,
      candidate: null,
      matches: 0,
      error: firstError || undefined,
    };
  });
}`

/** A reveal step, flattened to what the in-page runner needs. */
export interface PreviewStep {
  op: string
  selector?: string
  kind?: 'css' | 'xpath'
  text?: string
  ms?: number
}

export interface StepOutcome {
  op: string
  status: 'ok' | 'skipped' | 'failed'
  detail?: string
}

/**
 * Apply the reveal steps in the page, so the preview reads the state the
 * fields actually expect.
 *
 * **This is a rehearsal, not the real thing, and the UI says so.** Replay
 * dispatches a *trusted* CDP click (`Input.dispatchMouseEvent`, via the
 * driver's `_human_click`); this calls `element.click()` from page script.
 * The two are identical to most handlers and different to a few -- anything
 * gated on `event.isTrusted`, and anything that needs real pointer movement
 * first. So a step that works here is not proof it works at replay, though a
 * step that fails here is a genuine problem worth seeing now.
 *
 * The alternative was to leave reveal steps unapplied and let every field
 * behind an accordion preview as `empty`, which teaches the author nothing.
 */
export const APPLY_STEPS_JS = `async (steps) => {
  const pickOne = (sel, isXpath) => {
    try {
      if (isXpath) {
        const r = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        return r.singleNodeValue;
      }
      return document.querySelector(sel);
    } catch (e) { return null; }
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = [];

  for (const step of steps) {
    try {
      if (step.op === 'wait') {
        await sleep(Math.min(step.ms || 0, 5000));
        out.push({op: step.op, status: 'ok'});
        continue;
      }

      const el = step.selector ? pickOne(step.selector, step.kind === 'xpath') : null;
      if (!el) { out.push({op: step.op, status: 'skipped', detail: 'no match'}); continue; }

      if (step.op === 'click') {
        el.click();
        // Give a re-render a moment to land before the next step reads the
        // page. Replay has settle/timeout machinery for this; here a short
        // fixed pause is the honest approximation.
        await sleep(250);
      } else if (step.op === 'scroll_into_view') {
        el.scrollIntoView({behavior: 'auto', block: 'center'});
        await sleep(250);
      } else if (step.op === 'fill') {
        el.focus();
        el.value = step.text == null ? '' : step.text;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      } else if (step.op === 'wait_for_selector') {
        // Already resolved above, so it is present.
      } else {
        out.push({op: step.op, status: 'skipped', detail: 'not simulated'});
        continue;
      }
      out.push({op: step.op, status: 'ok'});
    } catch (e) {
      out.push({op: step.op, status: 'failed', detail: String(e && e.message || e)});
    }
  }
  return out;
}`

/**
 * Zip list-valued results into rows, the way a caller would read them.
 *
 * A repeating list is stored as one `all: true` read per column (see
 * `lib/picker/README.md`), so what comes back is parallel arrays. Rows are
 * what the author actually wants to look at, and -- more usefully -- zipping
 * is what *exposes the misalignment* that model allows: a column that yielded
 * fewer values than its neighbours shows up here as a short column with blanks
 * at the bottom, which is exactly the failure worth catching before saving.
 */
export function toRows(results: PreviewResult[]): {
  columns: string[]
  rows: (string | null)[][]
  /** Per-column value counts; unequal counts mean the rows are not aligned. */
  counts: Record<string, number>
  aligned: boolean
} {
  const listResults = results.filter((r) => Array.isArray(r.value))
  const columns = listResults.map((r) => r.name)
  const counts: Record<string, number> = {}
  for (const r of listResults) counts[r.name] = (r.value as string[]).length

  const height = Math.max(0, ...Object.values(counts))
  const rows: (string | null)[][] = []
  for (let i = 0; i < height; i++) {
    rows.push(listResults.map((r) => (r.value as string[])[i] ?? null))
  }

  const distinct = new Set(Object.values(counts))
  return { columns, rows, counts, aligned: distinct.size <= 1 }
}
