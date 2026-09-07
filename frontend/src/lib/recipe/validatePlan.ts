import type { PreviewField, PreviewRowsField, PreviewStep } from '@/lib/picker/preview'
import { STRUCTURED_KINDS } from './types'
import type { Candidate, Locator, Recipe, Step } from './types'

/**
 * Compile a recipe into the sequence a real run would perform.
 *
 * The preview answers "does this resolve against the page in front of me?".
 * That is a weaker question than it looks, because the page in front of the
 * author is one they have been *clicking on* -- they opened the drawer by hand
 * to pick the field inside it, so a recipe that forgot to record that click
 * still previews perfectly. It fails the first time it runs unattended.
 *
 * This asks the real question instead: from a **fresh navigation**, does the
 * recipe reach its own data? It mirrors `replay.py::_replay_group` exactly,
 * and the mirroring is the point:
 *
 *   for each group:
 *     navigate(url)          <- every group starts from a clean load
 *     run global_setup       <- re-run per group, not once overall
 *     run the group's steps
 *     read the group's fields
 *
 * Per-group re-navigation is the part that looks wasteful and is not. It is
 * what makes `global_setup` re-run, and it is why a group's steps are
 * cumulative rather than incremental. Validating any other way would pass
 * recipes that replay then fails.
 */

export interface ValidationGroup {
  groupId: string
  /** `global_setup` then the group's own steps, in execution order. */
  steps: PreviewStep[]
  /** Scalar fields this group collects. */
  fields: PreviewField[]
  /** Table fields this group collects, read row-wise. */
  rowFields: PreviewRowsField[]
  /**
   * Every scalar field's *full* candidate list, structured ones included.
   *
   * `fields` above is filtered to what the in-page reader can resolve, which
   * is the DOM. A field bound to a JSON path is not in the DOM, so without
   * this it validated as blank -- and a JSON-first recipe, the shape the
   * studio recommends, validated as entirely blank. The merge needs the whole
   * list rather than just the structured part, because whether a JSON path
   * wins depends on the DOM candidates it is ranked against.
   */
  scalarCandidates: { name: string; candidates: Candidate[] }[]
}

function toPreviewStep(step: Step): PreviewStep | null {
  const target = step.target
  if (target && target.kind !== 'css' && target.kind !== 'xpath') return null
  return {
    op: step.op,
    selector: target?.selector,
    kind: target?.kind === 'xpath' ? 'xpath' : 'css',
    text: typeof step.args?.text === 'string' ? step.args.text : undefined,
    ms: typeof step.args?.ms === 'number' ? step.args.ms : undefined,
  }
}

function toLocators(candidates: Candidate[]) {
  return candidates
    .filter((c) => c.locator.kind === 'css' || c.locator.kind === 'xpath')
    .map((c) => ({
      kind: c.locator.kind as 'css' | 'xpath',
      selector: c.locator.selector ?? '',
      attribute: c.locator.attribute,
      all: c.locator.all,
      index: c.locator.index,
      within: withinOf(c.locator),
    }))
}

function withinOf(locator: Locator) {
  const within = locator.within
  if (!within || (within.kind !== 'css' && within.kind !== 'xpath')) return undefined
  return { kind: within.kind as 'css' | 'xpath', selector: within.selector ?? '' }
}

export function buildValidationPlan(recipe: Recipe): ValidationGroup[] {
  const globalSteps = (recipe.global_setup ?? [])
    .map(toPreviewStep)
    .filter((s): s is PreviewStep => s !== null)

  return recipe.field_groups.map((group) => {
    const steps = [
      ...globalSteps,
      ...(group.steps ?? []).map(toPreviewStep).filter((s): s is PreviewStep => s !== null),
    ]

    const fields: PreviewField[] = []
    const rowFields: PreviewRowsField[] = []
    const scalarCandidates: { name: string; candidates: Candidate[] }[] = []

    for (const name of group.field_names) {
      const spec = recipe.fields[name]
      if (!spec) continue

      if (spec.type.kind === 'table') {
        const rows = group.repeat?.rows_locator
        if (!rows?.selector || (rows.kind !== 'css' && rows.kind !== 'xpath')) continue
        const columns: Record<string, ReturnType<typeof toLocators>> = {}
        for (const column of Object.keys(spec.type.columns ?? {})) {
          const candidates = group.bindings[column]
          if (candidates?.length) columns[column] = toLocators(candidates)
        }
        if (Object.keys(columns).length === 0) continue
        rowFields.push({
          name,
          rows: {
            kind: rows.kind as 'css' | 'xpath',
            selector: rows.selector,
            within: withinOf(rows),
          },
          columns,
          maxRows: group.repeat?.max_iterations ?? 100,
        })
        continue
      }

      const candidates = group.bindings[name]
      if (!candidates?.length) continue
      fields.push({
        name,
        required: spec.required,
        candidates: toLocators(candidates),
      })
      // Only worth carrying when there is actually something the DOM reader
      // cannot answer for; a purely-CSS field needs no second pass.
      if (candidates.some((c) => STRUCTURED_KINDS.includes(c.locator.kind))) {
        scalarCandidates.push({ name, candidates })
      }
    }

    return { groupId: group.group_id, steps, fields, rowFields, scalarCandidates }
  })
}

/**
 * A field bound only to structured data (`json_ld`, `hydration`, `meta`).
 *
 * The in-page reader addresses the DOM, so these have no candidate it can
 * evaluate and would report as `empty` — which would read as a failure when
 * the binding is in fact the most durable kind available. Naming them lets the
 * UI say "not checked here" instead of "broken".
 */
export function structuredOnlyFields(recipe: Recipe): string[] {
  const out: string[] = []
  for (const group of recipe.field_groups) {
    for (const [name, candidates] of Object.entries(group.bindings)) {
      if (candidates.length === 0) continue
      const anyDom = candidates.some(
        (c) => c.locator.kind === 'css' || c.locator.kind === 'xpath',
      )
      if (!anyDom) out.push(name)
    }
  }
  return out
}
