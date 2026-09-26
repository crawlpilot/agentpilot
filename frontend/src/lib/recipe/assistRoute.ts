/**
 * The state behind one answer on the manual-review screen.
 *
 * Kept out of the panel for the reason `submit.ts` gives — so it is testable
 * without rendering anything — but also because the bug that made that screen
 * unusable was a state bug, not a rendering one. The panel used to derive
 * "this ask is answered" from its work-in-progress maps and then use that
 * derivation to decide whether to render any controls at all, so the moment a
 * route gained its first entry the row replaced its own editor with a summary
 * and there was no way back. `AskMode` exists so that answering is something a
 * person does, never something inferred from a half-built route.
 *
 * **A route is a list of elements somebody pointed at, in order.** That is the
 * extension's model — `ElementDefinition { selectors, action: 'extract' |
 * 'click' }`, one ordered list, dispatched by `ElementProcessor.performAction`
 * — and it replaced a recorder here. A recording captured what the DOM emitted
 * rather than what the person meant: incidental clicks, scrolls, entries with
 * no stable selector, a cap that truncated in silence. Nothing in a picked list
 * is inferred, so there is nothing to wonder about.
 *
 * Position carries the meaning, which is why `splitAtRead` is the whole of the
 * server contract: clicks before the read are how you get to the value, clicks
 * after it are how you tidy up.
 */
import { detailPickToDraft, setReadAttribute } from './fromPick'
import type { FieldDraft } from './fromPick'
import type { PreviewStep } from '@/lib/picker/preview'
import type { PickPayload } from '@/lib/picker/protocol'
import type { PendingAsk, RecipeResolution } from '@/lib/api/types'

/**
 * Which mechanism an ask is being answered by, if any.
 *
 * `choosing` is the resting state and shows only the mechanism buttons — the
 * screen used to stack all six at once in a 22rem column. `done` is reached by
 * committing, never by having started.
 */
export type AskMode = 'choosing' | 'pick' | 'scope' | 'route' | 'describe' | 'done'

export interface AskState {
  mode: AskMode
  /** The picked list: clicks and one read, in the order they were added. */
  route: PreviewStep[]
  /** A standalone pick — "point at the value" without a route around it. */
  pick?: { candidates: FieldDraft['candidates']; spec: FieldDraft['spec']; preview: string }
  /** What a region pick selected, so the person can see they hit the heading. */
  scope?: { selector: string; matched: number; preview: string }
  /** A committed answer that carries no editable state: accept, describe, skip, scope. */
  answer?: RecipeResolution
}

export const EMPTY: AskState = { mode: 'choosing', route: [] }

/**
 * Reset one ask to unanswered.
 *
 * Returns a whole fresh state rather than deleting selected keys, which is the
 * bug this replaces: Change used to prune three of the panel's four per-field
 * maps and leave the recorded route behind, so the answer was immediately
 * re-derived from it and the row snapped shut again. One state object per ask
 * makes the incomplete version of this unwriteable.
 */
export function clearAnswer(): AskState {
  return { ...EMPTY, route: [] }
}

/**
 * One route entry for a finished pick.
 *
 * Moved here from the deleted `record.ts`, which is the only part of it worth
 * keeping. An `extract` pick becomes the `select` the binding is read from; a
 * `click` pick becomes a step that clicks the element. That is the extension's
 * two interactions with the ordering a list adds on top.
 */
export function stepForPick(payload: PickPayload): PreviewStep {
  const selector = payload.itemSelector || payload.containerSelector || ''
  const text = (payload.previewValue || payload.value || '').trim().slice(0, 60)
  const target = selector ? { kind: 'css' as const, selector } : {}
  if (payload.action === 'click') {
    return { op: 'click', intent: 'reveal', ...target, text }
  }
  return { op: 'select', intent: 'select', ...target, text, pick: payload }
}

/** Whether this row is the one the value is read from. */
export function isRead(step: PreviewStep): boolean {
  return step.intent === 'select'
}

/**
 * A route, cut at the point the value is read.
 *
 * `before` is how you reach the value and becomes the group's `steps`; `after`
 * is how you tidy up and becomes its teardown. They cannot be one list: a
 * group runs its steps and *then* reads, so a trailing "close this" folded in
 * with the setup would run before the binding and shut the value away.
 *
 * Mirrors `assist.py::split_route`, which cuts the same array server-side. The
 * LAST read wins, there as here — somebody who picks, reads the preview and
 * picks again has corrected themselves.
 */
export function splitAtRead(route: PreviewStep[]): {
  before: PreviewStep[]
  read: PreviewStep | null
  after: PreviewStep[]
} {
  let at = -1
  for (let i = route.length - 1; i >= 0; i--) {
    if (isRead(route[i])) {
      at = i
      break
    }
  }
  if (at === -1) return { before: [...route], read: null, after: [] }
  return { before: route.slice(0, at), read: route[at], after: route.slice(at + 1) }
}

/**
 * The binding a route carries, if it has a read in it.
 *
 * The conversion is the one the standalone pick button does — the same
 * `detailPickToDraft` — so a value chosen inside a route gets the same derived
 * type and cleanup as one chosen on its own, rather than a second, poorer path
 * to the same thing. The row's attribute override is applied here because this
 * is the last point at which the candidate chain still exists.
 */
export function routeDraft(route: PreviewStep[]): FieldDraft | null {
  const { read } = splitAtRead(route)
  if (!read?.pick) return null
  const draft = detailPickToDraft(read.pick as PickPayload)
  if (draft.candidates.length === 0) return null
  return read.attribute
    ? { ...draft, candidates: setReadAttribute(draft.candidates, read.attribute) }
    : draft
}

/**
 * One route step as the server wants it.
 *
 * `pick` is the whole enriched `PickPayload` — candidate chains, extracted
 * rows, inferred columns — which for a list pick runs to tens of kilobytes. It
 * has done its job by the time a route is submitted: `routeDraft` turned it
 * into the locators travelling alongside. The server wants the `select` marker
 * so it knows where to cut, and nothing else from it.
 */
export function forWire(step: PreviewStep): PreviewStep {
  if (!step.pick) return step
  const { pick: _drop, ...rest } = step
  return rest
}

/**
 * What to send for one ask, or null if it has not been answered.
 *
 * A route with a read in it is a `pick` whose steps happen to describe how to
 * reach it — not a bare `steps` answer the model has to search all over again.
 * The read stays in the array as the marker `split_route` cuts on, so the
 * server knows which clicks run before the binding and which run after.
 */
export function resolutionFor(field: string, state: AskState): RecipeResolution | null {
  if (state.mode !== 'done') return null

  if (state.route.length) {
    const wire = state.route.map(forWire) as unknown as Array<Record<string, unknown>>
    const draft = routeDraft(state.route)
    if (draft) {
      return {
        field,
        action: 'pick',
        locators: draft.candidates.map((c) => c.locator as unknown as Record<string, unknown>),
        spec: draft.spec as unknown as Record<string, unknown>,
        steps: wire,
      }
    }
    // Clicks and no read: they showed the way there and left the model to find
    // the value in what it revealed.
    return { field, action: 'steps', steps: wire }
  }

  if (state.pick) {
    return {
      field,
      action: 'pick',
      locators: state.pick.candidates.map(
        (c) => c.locator as unknown as Record<string, unknown>,
      ),
      spec: state.pick.spec as unknown as Record<string, unknown>,
    }
  }

  return state.answer ?? null
}

/** Whether this ask has something to send. */
export function isAnswered(state: AskState | undefined): boolean {
  return state?.mode === 'done'
}

/**
 * The next ask still wanting an answer, so finishing one moves you on.
 *
 * Wraps, and falls back to staying put: the screen should never select nothing
 * and look empty, and when the last ask is answered there is nowhere to go.
 */
export function nextUnanswered(
  asks: PendingAsk[],
  states: Record<string, AskState>,
  current: string,
): string {
  if (asks.length === 0) return current
  const at = asks.findIndex((a) => a.field === current)
  for (let i = 1; i <= asks.length; i++) {
    const ask = asks[(Math.max(at, 0) + i) % asks.length]
    if (!isAnswered(states[ask.field])) return ask.field
  }
  return current
}
