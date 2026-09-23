/**
 * The picked region's markup, pruned in the page before it crosses the wire.
 *
 * `PRUNED_OUTER_HTML` is a string of JavaScript that runs inside the session's
 * page through `execute_js`, so the only honest way to test it is to run it --
 * against a real DOM, which is what jsdom is here for. Asserting on the source
 * text would prove nothing about what it does to a page.
 *
 * The Python counterpart is `selector_agent.prune_fragment`, the regex-based
 * guard for markup that arrives from anywhere but this picker. The two are kept
 * deliberately in step: `DEAD_TAGS` mirrors `_DEAD_MARKUP` and `KEEP_ATTR`
 * mirrors `_KEEP_ATTR`.
 */

// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import { PRUNED_OUTER_HTML } from './usePagePicker'

/** Run the injected expression the way `execute_js` would. */
function prune(html: string, selector = '#specs'): string {
  document.body.innerHTML = html
  // eslint-disable-next-line no-eval
  return String(eval(PRUNED_OUTER_HTML(selector)))
}

// A specifications section shaped the way a React page ships one: real content
// wrapped in generated class names, inline styles, event handlers, hydration
// comments and inert script/style/svg.
const SECTION = `
<section id="specs" class="dc_v3 f6 mv0" style="padding:12px" data-testid="spec-block"
         onclick="track()" data-tl-id="ItemSpec-abc123" tabindex="-1">
  <!--$-->
  <script>window.__NEXT_DATA__={huge:true}</script>
  <style>.dc_v3{color:red}</style>
  <h2 class="b f5" aria-label="Specifications">Specifications</h2>
  <table>
    <tr><th class="pv2 bb">Brand</th><td class="pv2 bb" itemprop="brand">Bodycology</td></tr>
    <tr><th class="pv2 bb">Scent</th><td class="pv2 bb">Pink Vanilla Wish</td></tr>
  </table>
  <svg viewBox="0 0 8 8" class="icon"><path d="M0 0L8 8"/></svg>
  <!--/$-->
</section>`

describe('PRUNED_OUTER_HTML', () => {
  it('keeps every value and every attribute a selector is built from', () => {
    const out = prune(SECTION)

    expect(out).toContain('Bodycology')
    expect(out).toContain('Pink Vanilla Wish')
    expect(out).toContain('Specifications')
    // The region's own identity, and the relationships `propose_within` is
    // told to prefer: a data-* attribute, an itemprop, a semantic tag.
    expect(out).toContain('id="specs"')
    expect(out).toContain('data-testid="spec-block"')
    expect(out).toContain('itemprop="brand"')
    expect(out).toContain('<table>')
    expect(out).toContain('aria-label="Specifications"')
  })

  it('drops the markup that is never the answer', () => {
    const out = prune(SECTION)

    expect(out).not.toContain('__NEXT_DATA__')
    expect(out).not.toContain('<script')
    expect(out).not.toContain('<style')
    expect(out).not.toContain('<svg')
    expect(out).not.toContain('viewBox')
    expect(out).not.toContain('style="padding')
    expect(out).not.toContain('onclick')
    expect(out).not.toContain('tabindex')
    // React's hydration comments say nothing about where a value is.
    expect(out).not.toContain('<!--')
  })

  it('is smaller than what it replaced', () => {
    const out = prune(SECTION)
    expect(out.length).toBeLessThan(SECTION.length)
  })

  it('returns an empty string when the region is gone', () => {
    // The person picked, then the page re-rendered. An empty answer is what
    // `propose_within` expects for that; a throw would lose the whole batch.
    expect(prune('<div id="other"></div>')).toBe('')
  })

  it('does not treat a selector containing a quote as syntax', () => {
    document.body.innerHTML = `<div data-name='a"b'>kept</div>`
    // eslint-disable-next-line no-eval
    const out = String(eval(PRUNED_OUTER_HTML('[data-name=\'a"b\']')))
    expect(out).toContain('kept')
  })
})
