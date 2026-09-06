// Constructors and immutable edits over a v2 recipe document.
//
// Every mutation the studio performs goes through a function here rather than
// being spelled out at the call site, for one reason: a field name is a
// *key* in three places at once -- `fields`, `field_groups[].field_names` and
// `field_groups[].bindings` -- so renaming or deleting one in the UI touches
// three structures that must agree. `renameField`/`removeField` keep them in
// step; ad-hoc setState in a component would not.

import type {
  Assertion,
  AssertionKind,
  Candidate,
  FieldGroup,
  FieldSpec,
  Locator,
  LocatorKind,
  PageVariant,
  Predicate,
  PredicateKind,
  Recipe,
  Step,
  StepOp,
  Transform,
  TransformOp,
  TypeSpec,
} from './types'

export const LOCATOR_KINDS: LocatorKind[] = [
  'json_ld',
  'hydration',
  'meta',
  'ax_role',
  'text',
  'css',
  'xpath',
]

// Mirrors `SOURCE_PRIORITY` in agentpilot/recipe/v2/selector_agent.py.
// Lower sorts first: page-embedded JSON survives a redesign that renames
// every CSS class, so a structured candidate outranks a DOM one by default.
export const SOURCE_PRIORITY: Record<LocatorKind, number> = {
  json_ld: 10,
  hydration: 15,
  meta: 20,
  ax_role: 40,
  text: 50,
  css: 60,
  xpath: 70,
}

export const STEP_OPS: StepOp[] = [
  'click',
  'double_click',
  'hover',
  'fill',
  'clear',
  'press',
  'send_keys',
  'select_option',
  'check',
  'uncheck',
  'scroll',
  'scroll_into_view',
  'find_text',
  'drag',
  'tap',
  'swipe',
  'wait',
  'wait_for_selector',
  'wait_for_text',
  'wait_for_url',
  'wait_for_load',
  'dialog_accept',
  'dialog_dismiss',
  'navigate',
  'new_tab',
  'switch_tab',
  'close_tab',
  'download',
]

/** Ops that need a `target` locator to mean anything. */
export const OPS_NEEDING_TARGET = new Set<StepOp>([
  'click',
  'double_click',
  'hover',
  'fill',
  'clear',
  'select_option',
  'check',
  'uncheck',
  'scroll_into_view',
  'drag',
  'tap',
  'swipe',
  'wait_for_selector',
])

/** Ops that wait for a *condition*, and so make a bare `wait` avoidable. */
export const WAIT_FOR_OPS = new Set<StepOp>([
  'wait_for_selector',
  'wait_for_text',
  'wait_for_url',
  'wait_for_load',
])

export const TRANSFORM_OPS: TransformOp[] = [
  'trim',
  'collapse_ws',
  'strip_control',
  'strip_accents',
  'case',
  'regex_extract',
  'regex_replace',
  'split',
  'join',
  'slice',
  'index',
  'unique',
  'filter_empty',
  'map_lookup',
  'template',
  'url_resolve',
  'json_parse',
  'json_path',
  'strip_html',
  'html_select',
  'to_object',
  'to_pairs',
  'cast',
  'default',
  'lua',
]

export const PREDICATE_KINDS: PredicateKind[] = [
  'selector_present',
  'selector_absent',
  'visible',
  'text_present',
  'url_matches',
  'json_path_present',
  'count_at_least',
  'meta_equals',
]

export const ASSERTION_KINDS: AssertionKind[] = [
  'not_empty',
  'range',
  'matches',
  'in_set',
  'length',
  'cross_source_agrees',
  'not_equals_previous',
]

// --- constructors ---

export function emptyRecipe(name = ''): Recipe {
  return {
    name,
    status: 'draft',
    target: { match: [] },
    fields: {},
    variants: [],
    global_setup: [],
    field_groups: [{ group_id: 'core', field_names: [], bindings: {} }],
    sample_urls: [],
  }
}

export function newFieldSpec(): FieldSpec {
  return { type: { kind: 'scalar', value_type: 'string' }, description: '' }
}

export function newLocator(kind: LocatorKind): Locator {
  switch (kind) {
    case 'css':
    case 'xpath':
      return { kind, selector: '', attribute: 'text' }
    case 'ax_role':
      return { kind, role: 'button' }
    case 'text':
      return { kind, text: '' }
    default:
      return { kind, path: '', path_lang: 'simple' }
  }
}

export function newCandidate(kind: LocatorKind = 'json_ld'): Candidate {
  return { priority: SOURCE_PRIORITY[kind], locator: newLocator(kind), verified_on: 0 }
}

export function newStep(op: StepOp = 'click'): Step {
  const step: Step = { op, on_error: 'fail' }
  if (OPS_NEEDING_TARGET.has(op)) step.target = newLocator('css')
  return step
}

export function newGroup(existing: FieldGroup[]): FieldGroup {
  let n = existing.length + 1
  while (existing.some((g) => g.group_id === `group_${n}`)) n += 1
  return { group_id: `group_${n}`, field_names: [], steps: [], bindings: {} }
}

export function newVariant(existing: PageVariant[]): PageVariant {
  let n = existing.length + 1
  while (existing.some((v) => v.variant_id === `variant_${n}`)) n += 1
  return { variant_id: `variant_${n}`, priority: 100, detect: [] }
}

export function newPredicate(kind: PredicateKind = 'selector_present'): Predicate {
  return { kind, ...(kind === 'count_at_least' ? { n: 1 } : {}) }
}

export function newAssertion(kind: AssertionKind = 'not_empty'): Assertion {
  return { kind }
}

export function newTransform(op: TransformOp = 'trim'): Transform {
  return { op }
}

// --- generic array helpers ---

export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return items
  const next = items.slice()
  const [item] = next.splice(from, 1)
  next.splice(to, 0, item)
  return next
}

export function replaceAt<T>(items: T[], index: number, item: T): T[] {
  const next = items.slice()
  next[index] = item
  return next
}

export function removeAt<T>(items: T[], index: number): T[] {
  return items.filter((_, i) => i !== index)
}

// --- field-name edits, the ones that span three structures ---

export function fieldNames(recipe: Recipe): string[] {
  return Object.keys(recipe.fields)
}

/** The group that collects `name`, or null when the field is declared but unbound. */
export function groupForField(recipe: Recipe, name: string): FieldGroup | null {
  return recipe.field_groups.find((g) => g.field_names.includes(name)) ?? null
}

export function candidatesFor(recipe: Recipe, name: string): Candidate[] {
  const group = groupForField(recipe, name)
  return group?.bindings[name] ?? []
}

export function addField(recipe: Recipe, name: string, groupId?: string): Recipe {
  if (!name || recipe.fields[name]) return recipe
  const target = groupId ?? recipe.field_groups[0]?.group_id
  return {
    ...recipe,
    fields: { ...recipe.fields, [name]: newFieldSpec() },
    field_groups: recipe.field_groups.map((g) =>
      g.group_id === target ? { ...g, field_names: [...g.field_names, name] } : g,
    ),
  }
}

export function updateField(recipe: Recipe, name: string, spec: FieldSpec): Recipe {
  return { ...recipe, fields: { ...recipe.fields, [name]: spec } }
}

export function renameField(recipe: Recipe, from: string, to: string): Recipe {
  if (from === to || !to || recipe.fields[to] || !recipe.fields[from]) return recipe
  // Object key order is the field order in the left pane, so rebuild the map
  // in place rather than delete-and-append, which would jump the row to the end.
  const fields: Record<string, FieldSpec> = {}
  for (const [key, spec] of Object.entries(recipe.fields)) fields[key === from ? to : key] = spec
  return {
    ...recipe,
    fields,
    field_groups: recipe.field_groups.map((g) => {
      if (!g.field_names.includes(from)) return g
      const bindings: Record<string, Candidate[]> = {}
      for (const [key, cands] of Object.entries(g.bindings)) bindings[key === from ? to : key] = cands
      return {
        ...g,
        field_names: g.field_names.map((n) => (n === from ? to : n)),
        bindings,
        repeat: g.repeat?.row_field === from ? { ...g.repeat, row_field: to } : g.repeat,
      }
    }),
  }
}

export function removeField(recipe: Recipe, name: string): Recipe {
  const fields = { ...recipe.fields }
  delete fields[name]
  return {
    ...recipe,
    fields,
    field_groups: recipe.field_groups.map((g) => {
      if (!g.field_names.includes(name)) return g
      const bindings = { ...g.bindings }
      delete bindings[name]
      return { ...g, field_names: g.field_names.filter((n) => n !== name), bindings }
    }),
  }
}

/** Move a field to another group, carrying its candidates with it. */
export function moveFieldToGroup(recipe: Recipe, name: string, toGroupId: string): Recipe {
  const from = groupForField(recipe, name)
  if (from?.group_id === toGroupId) return recipe
  const carried = from?.bindings[name] ?? []
  return {
    ...recipe,
    field_groups: recipe.field_groups.map((g) => {
      if (g.group_id === from?.group_id) {
        const bindings = { ...g.bindings }
        delete bindings[name]
        return { ...g, field_names: g.field_names.filter((n) => n !== name), bindings }
      }
      if (g.group_id === toGroupId) {
        return {
          ...g,
          field_names: [...g.field_names, name],
          bindings: { ...g.bindings, [name]: carried },
        }
      }
      return g
    }),
  }
}

export function updateGroup(recipe: Recipe, groupId: string, patch: Partial<FieldGroup>): Recipe {
  return {
    ...recipe,
    field_groups: recipe.field_groups.map((g) => (g.group_id === groupId ? { ...g, ...patch } : g)),
  }
}

export function setCandidates(recipe: Recipe, name: string, candidates: Candidate[]): Recipe {
  const group = groupForField(recipe, name)
  if (!group) return recipe
  return updateGroup(recipe, group.group_id, { bindings: { ...group.bindings, [name]: candidates } })
}

/**
 * Sort a field's candidates by `priority`, the order replay actually uses.
 *
 * Resolution is priority-ordered with list position only breaking ties
 * (contract §6), so a list that *looks* ordered in the editor but carries
 * stale priorities would mislead. Reordering rewrites priority to match.
 */
export function reorderCandidates(candidates: Candidate[], from: number, to: number): Candidate[] {
  const moved = moveItem(candidates, from, to)
  const sorted = [...candidates].map((c) => c.priority ?? 100).sort((a, b) => a - b)
  return moved.map((c, i) => ({ ...c, priority: sorted[i] }))
}

// --- display helpers ---

export function describeLocator(loc: Locator | undefined): string {
  if (!loc) return '--'
  switch (loc.kind) {
    case 'css':
    case 'xpath': {
      const scope = loc.within ? `${describeLocator(loc.within)} >> ` : ''
      const idx = loc.index != null ? `[${loc.index}]` : loc.all ? '[all]' : ''
      const attr = loc.attribute && loc.attribute !== 'text' ? ` @${loc.attribute}` : ''
      return `${scope}${loc.selector ?? ''}${idx}${attr}`
    }
    case 'ax_role': {
      const name = loc.name_in?.length
        ? ` in [${loc.name_in.join(', ')}]`
        : loc.name_contains
          ? ` ~ "${loc.name_contains}"`
          : loc.name_regex
            ? ` =~ /${loc.name_regex}/`
            : ''
      return `role=${loc.role ?? '?'}${name}`
    }
    case 'text':
      return `text "${loc.text ?? ''}"`
    default:
      return `${loc.path ?? ''}${loc.path_lang === 'jmespath' ? ' (jmespath)' : ''}`
  }
}

export function describeTypeSpec(type: TypeSpec): string {
  switch (type.kind) {
    case 'scalar':
      return type.value_type ?? 'string'
    case 'list':
      return `list<${type.items ? describeTypeSpec(type.items) : 'string'}>`
    case 'object': {
      const keys = Object.keys(type.properties ?? {})
      return keys.length === 0 ? 'open map' : `object{${keys.join(', ')}}`
    }
    case 'table':
      return `table[${Object.keys(type.columns ?? {}).join(', ')}]`
  }
}

export function describePredicate(p: Predicate): string {
  switch (p.kind) {
    case 'selector_present':
    case 'selector_absent':
    case 'visible':
      return `${p.kind} ${p.selector ?? ''}`
    case 'text_present':
      return `text "${p.text ?? ''}"`
    case 'url_matches':
      return `url ~ ${p.url ?? ''}`
    case 'json_path_present':
      return `${p.source ?? 'json_ld'}:${p.path ?? ''}`
    case 'count_at_least':
      return `${p.selector ?? ''} >= ${p.n ?? 1}`
    case 'meta_equals':
      return `meta.${p.key ?? ''} = ${p.value ?? ''}`
  }
}

export function describeTransform(t: Transform): string {
  switch (t.op) {
    case 'regex_extract':
    case 'regex_replace':
      return `${t.op} /${t.pattern ?? ''}/${t.flags ?? ''}${t.op === 'regex_replace' ? ` -> "${t.repl ?? ''}"` : ''}`
    case 'split':
    case 'join':
      return `${t.op} "${t.sep ?? ''}"`
    case 'cast':
      return `cast ${t.to ?? 'string'}`
    case 'index':
      return `index ${t.i ?? 0}`
    case 'html_select':
      return `html_select ${t.selector ?? ''}`
    case 'json_path':
    case 'to_object':
      return `${t.op} ${t.path ?? t.key ?? ''}`
    case 'lua':
      return 'lua script'
    default:
      return t.op
  }
}

/**
 * Drop `undefined` values and empty containers before export.
 *
 * `additionalProperties: false` on most schema objects means a stray
 * `"selector": undefined` serializes to nothing but an empty `steps: []`
 * survives as noise. Keeping the exported document tight makes the JSON tab
 * diffable against a recipe written by hand or by the build agent.
 */
export function toExport(recipe: Recipe): Recipe {
  return prune(recipe) as Recipe
}

function prune(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(prune)
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
      if (raw === undefined) continue
      const cleaned = prune(raw)
      const isEmptyArray = Array.isArray(cleaned) && cleaned.length === 0
      // `match: []` is meaningful -- contract §1 reads it as "any URL" -- so
      // empty arrays are only dropped where the schema treats absent and
      // empty identically.
      if (isEmptyArray && DROPPABLE_EMPTY.has(key)) continue
      out[key] = cleaned
    }
    return out
  }
  return value
}

const DROPPABLE_EMPTY = new Set([
  'steps',
  'when',
  'detect',
  'transform',
  'assertions',
  'variants',
  'global_setup',
  'name_in',
])
