// Client-side lint for a v2 recipe document.
//
// This is not a substitute for `POST /v1/recipes/{id}/validate` -- that runs
// the real resolver. It is the feedback you get while typing, and it covers
// two classes of problem the JSON Schema cannot express:
//
//   1. Cross-references. A candidate naming a `variant_id` that no variant
//      declares is schema-valid and dead at replay.
//   2. Runtime refusals. `agentpilot/recipe/v2/steps.py` raises `StepError`
//      for a CSS action target carrying an `index`, because the driver's
//      actions take a selector or a ref and CSS has no general nth-match. A
//      recipe can encode that and only find out mid-run -- which is how five
//      identical rows get mistaken for five sizes.
//
// Plus the two lints `docs/recipe-studio.md` asks for by name: a bare `wait`
// where a condition exists, and a never-verified candidate on a recipe
// somebody has approved.
//
// **`sample_urls` is deliberately not linted.** It used to be an error to save
// without one, on the reasoning that a recipe fitted to a single page is a
// guess. That reasoning describes *authoring*, and it had turned into a
// property of the artefact: a recipe could not exist without naming the pages
// it came from. It does not work that way at runtime -- a recipe is selected
// by a person and applied to the URLs *they* submit, and the page it happened
// to be built against says nothing about the pages it will run on. The field
// stays in the document as optional provenance; nothing depends on it.

import type { Locator, Recipe, Step } from './types'
import { STRUCTURED_KINDS } from './types'
import { OPS_NEEDING_TARGET, WAIT_FOR_OPS, describeLocator } from './document'

export type LintSeverity = 'error' | 'warning' | 'info'

export interface LintIssue {
  severity: LintSeverity
  /** Human path into the document, e.g. `fields.price -> candidate 2`. */
  path: string
  message: string
  /** Which tab to open when the issue is clicked. */
  tab?: 'schema' | 'steps' | 'fields' | 'transforms' | 'variants' | 'quality'
}

export function lintRecipe(recipe: Recipe): LintIssue[] {
  const issues: LintIssue[] = []
  const add = (
    severity: LintSeverity,
    path: string,
    message: string,
    tab?: LintIssue['tab'],
  ) => issues.push({ severity, path, message, tab })

  if (!recipe.name.trim()) add('error', 'name', 'The recipe needs a name.', 'schema')

  const declared = Object.keys(recipe.fields)
  if (declared.length === 0) {
    add('error', 'fields', 'No fields declared. The output schema is the input the build agent works from.', 'schema')
  }

  lintTarget(recipe, add)

  const variantIds = new Set((recipe.variants ?? []).map((v) => v.variant_id))
  const seenVariants = new Set<string>()
  for (const variant of recipe.variants ?? []) {
    if (seenVariants.has(variant.variant_id)) {
      add('error', `variants.${variant.variant_id}`, 'Duplicate variant_id.', 'variants')
    }
    seenVariants.add(variant.variant_id)
    if (variant.detect.length === 0) {
      add(
        'warning',
        `variants.${variant.variant_id}`,
        'No detect predicates, so this variant can never be selected.',
        'variants',
      )
    }
  }

  for (const [index, step] of (recipe.global_setup ?? []).entries()) {
    lintStep(step, `global_setup[${index}]`, add)
  }

  const bound = new Set<string>()
  const seenGroups = new Set<string>()

  for (const group of recipe.field_groups) {
    const where = `field_groups.${group.group_id}`
    if (seenGroups.has(group.group_id)) add('error', where, 'Duplicate group_id.', 'fields')
    seenGroups.add(group.group_id)

    for (const [index, step] of (group.steps ?? []).entries()) {
      lintStep(step, `${where}.steps[${index}]`, add)
    }

    for (const name of group.field_names) {
      bound.add(name)
      if (!recipe.fields[name]) {
        add('error', `${where}.${name}`, 'Group collects a field that the schema does not declare.', 'fields')
        continue
      }
      const spec = recipe.fields[name]
      // A `table` field is bound one *column* at a time: `field_names` carries
      // the field, while `bindings` is keyed by the column names declared in
      // `type.columns` (contract §8, and the shape both the Zara and Walmart
      // examples use). Looking for `bindings[name]` on a table therefore finds
      // nothing and reports a correctly-bound table as unresolvable.
      const bindingKeys =
        spec.type.kind === 'table' ? Object.keys(spec.type.columns ?? {}) : [name]

      if (bindingKeys.length === 0) {
        add('error', `${where}.${name}`, 'Declared as a table but has no columns.', 'fields')
        continue
      }

      const unbound = bindingKeys.filter((key) => (group.bindings[key] ?? []).length === 0)
      if (unbound.length === bindingKeys.length) {
        add('error', `${where}.${name}`, 'No candidates bound -- this field can never resolve.', 'fields')
        continue
      }
      for (const key of unbound) {
        add('error', `${where}.${name}.${key}`, `Column "${key}" has no candidates bound.`, 'fields')
      }

      const candidates = bindingKeys.flatMap((key) =>
        (group.bindings[key] ?? []).map((candidate) => ({ candidate, key })),
      )

      let anyStructured = false
      for (const [index, { candidate, key }] of candidates.entries()) {
        const at = `${key} -> candidate ${index + 1}`
        if (STRUCTURED_KINDS.includes(candidate.locator.kind)) anyStructured = true
        lintLocator(candidate.locator, at, false, add)
        if (candidate.variant_id && !variantIds.has(candidate.variant_id)) {
          add('error', at, `Scoped to variant "${candidate.variant_id}", which no variant declares.`, 'fields')
        }
        // The spec's second named lint. `verified_on` is written by build-time
        // multi-page induction; zero on an approved recipe means nobody --
        // human or agent -- has ever seen this candidate resolve.
        if ((candidate.verified_on ?? 0) === 0 && recipe.status !== 'draft') {
          add('warning', at, 'Never verified against a sample page on an approved recipe.', 'fields')
        }
        if ((candidate.transform ?? []).some((t) => t.op === 'lua') && !recipe.has_script) {
          add('warning', at, 'Uses a Lua transform but the recipe is not flagged has_script.', 'transforms')
        }
      }

      if (!anyStructured && candidates.length === 1) {
        add(
          'info',
          `${name} -> candidate 1`,
          'Only a DOM candidate. Check whether the value is also in JSON-LD, hydration state or a meta tag -- those survive a redesign.',
          'fields',
        )
      }

      if (spec.type.kind === 'table' && !group.repeat) {
        add(
          'warning',
          `${where}.${name}`,
          'Declared as a table but its group has no repeat, so it can only ever yield one row.',
          'fields',
        )
      }
      if (spec.required && (spec.assertions ?? []).length === 0) {
        add(
          'info',
          `fields.${name}`,
          'Required but has no assertions. A wrong-but-resolving value passes every automated check without one.',
          'quality',
        )
      }
    }

    if (group.repeat) {
      const rep = group.repeat
      if (!group.field_names.includes(rep.row_field)) {
        add('error', `${where}.repeat`, `row_field "${rep.row_field}" is not collected by this group.`, 'fields')
      }
      if (rep.kind === 'dom') {
        if (!rep.option_locator) {
          add('error', `${where}.repeat`, 'A dom repeat needs an option_locator.', 'fields')
        } else {
          // A dom repeat sets `index` per iteration, so its option locator is
          // an *action* target -- the CSS+index refusal applies here too, and
          // this is the exact shape that silently returns N identical rows.
          lintLocator(rep.option_locator, `${where}.repeat.option_locator`, true, add)
        }
      } else if (!rep.rows_locator) {
        add('error', `${where}.repeat`, 'A json repeat needs a rows_locator.', 'fields')
      }
      if (!group.expect?.min_rows) {
        add(
          'info',
          `${where}.expect`,
          'No min_rows. A run that captured 8 of 40 rows looks identical to a complete one without it.',
          'quality',
        )
      }
    }
  }

  for (const name of declared) {
    if (!bound.has(name)) {
      add('warning', `fields.${name}`, 'Declared in the schema but not collected by any group.', 'schema')
    }
  }

  return issues
}

function lintTarget(recipe: Recipe, add: AddIssue) {
  for (const [index, matcher] of recipe.target.match.entries()) {
    if (!matcher.pattern.trim()) {
      add('error', `target.match[${index}]`, 'Empty pattern.', 'schema')
      continue
    }
    if (matcher.kind === 'regex') {
      try {
        new RegExp(matcher.pattern)
      } catch (err) {
        add('error', `target.match[${index}]`, `Invalid regex: ${(err as Error).message}`, 'schema')
      }
    }
  }
  if (recipe.target.match.length === 0) {
    add(
      'info',
      'target.match',
      'No URL matchers, so this recipe accepts any URL. That is legal, but it also means nothing stops a scheduled run pointing it at the wrong page type.',
      'schema',
    )
  }
}

function lintStep(step: Step, where: string, add: AddIssue) {
  if (OPS_NEEDING_TARGET.has(step.op) && !step.target) {
    add('error', where, `${step.op} needs a target locator.`, 'steps')
  }
  if (step.target) lintLocator(step.target, where, true, add)

  // The spec's first named lint. A fixed sleep is a guess about the page;
  // a condition is an observation of it.
  if (step.op === 'wait') {
    add(
      'warning',
      where,
      'Fixed sleep. Prefer wait_for_selector / wait_for_text / wait_for_url when a condition is available -- it is both faster and more reliable.',
      'steps',
    )
  }
  if (step.op === 'wait_for_load' && (step.args?.state as string) === 'networkidle') {
    add(
      'warning',
      where,
      'networkidle never settles on pages with polling or streaming ads, and then burns the whole timeout on every run. Wait for the element you actually need.',
      'steps',
    )
  }
  if (WAIT_FOR_OPS.has(step.op) && step.timeout_ms == null) {
    add('info', where, 'No timeout_ms, so this falls back to the recipe default.', 'steps')
  }
  if (step.retry && step.retry.attempts > 3 && step.on_error === 'fail') {
    add('info', where, 'Many retries on a failing step multiply the run time before it gives up.', 'steps')
  }
}

function lintLocator(loc: Locator, where: string, isActionTarget: boolean, add: AddIssue) {
  switch (loc.kind) {
    case 'css':
    case 'xpath':
      if (!loc.selector?.trim()) add('error', where, `Empty ${loc.kind} selector.`, 'fields')
      break
    case 'ax_role':
      if (!loc.role?.trim()) add('error', where, 'ax_role locator needs a role.', 'fields')
      break
    case 'text':
      if (!loc.text?.trim()) add('error', where, 'text locator needs text to match.', 'fields')
      break
    default:
      if (!loc.path?.trim()) add('error', where, `Empty ${loc.kind} path.`, 'fields')
  }

  if (isActionTarget) {
    if (loc.kind === 'xpath') {
      add(
        'error',
        where,
        'XPath cannot be an action target: the driver resolves action selectors through querySelector, not the locator engine. Use css, or ax_role.',
        'steps',
      )
    }
    if (loc.kind === 'css' && loc.index != null) {
      add(
        'error',
        where,
        `CSS action targets cannot carry an index -- the driver has no nth-match for them, so every iteration would act on the first match. Use ax_role, or make "${describeLocator(loc)}" select one element.`,
        'steps',
      )
    }
    if (STRUCTURED_KINDS.includes(loc.kind)) {
      add('error', where, `A ${loc.kind} locator reads JSON; it cannot be clicked or filled.`, 'steps')
    }
  }

  if (loc.within) {
    if (loc.kind === 'css' && loc.within.kind !== 'css') {
      add(
        'error',
        where,
        'A css locator can only be scoped by another css locator -- the two are composed into one selector string.',
        'fields',
      )
    }
    lintLocator(loc.within, `${where} (within)`, false, add)
  }
}

type AddIssue = (
  severity: LintSeverity,
  path: string,
  message: string,
  tab?: LintIssue['tab'],
) => void

export function countBySeverity(issues: LintIssue[]): Record<LintSeverity, number> {
  return {
    error: issues.filter((i) => i.severity === 'error').length,
    warning: issues.filter((i) => i.severity === 'warning').length,
    info: issues.filter((i) => i.severity === 'info').length,
  }
}
