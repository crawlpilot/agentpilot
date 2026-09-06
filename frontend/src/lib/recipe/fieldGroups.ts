import type { RecipeFieldGroup } from '@/lib/api/types'

interface ReadableGroup {
  /** Where each field's value comes from, whichever key the recipe used. */
  locators: Record<string, unknown> | null
  /** Reveal steps, whichever key the recipe used. Never undefined. */
  steps: Record<string, unknown>[]
  /** True for the v2 shape, whose bindings key a table by COLUMN. */
  v2: boolean
}

/**
 * Read a group without caring which schema wrote it.
 *
 * `field_groups` is one JSON column holding two shapes: an agent *build*
 * writes `field_locators` + `reveal_steps`, and the studio writes `bindings` +
 * `steps` (`save_document` stores the v2 document's groups verbatim). Nothing
 * converts between them.
 *
 * This existed as `group.reveal_steps.length` inline, which is fine for a v1
 * recipe and a `TypeError` for every recipe authored in the studio -- so the
 * detail page white-screened on exactly the recipes the wizard produces. The
 * defaulting is the entire fix; it is a function so it can be tested without
 * rendering.
 */
export function readGroup(group: RecipeFieldGroup): ReadableGroup {
  return {
    locators: group.bindings ?? group.field_locators ?? null,
    steps: group.steps ?? group.reveal_steps ?? [],
    v2: group.bindings !== undefined,
  }
}
