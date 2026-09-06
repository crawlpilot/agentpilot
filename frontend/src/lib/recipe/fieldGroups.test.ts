import { describe, it, expect } from 'vitest'
import { readGroup } from './fieldGroups'
import type { RecipeFieldGroup } from '@/lib/api/types'

/**
 * `field_groups` is one JSON column holding two schemas, and the detail page
 * has to render either. It did not: reading `reveal_steps.length` is fine for
 * an agent-built recipe and a `TypeError` for every recipe the studio writes,
 * so opening one white-screened the page.
 *
 * The v2 group below is the real shape, taken from a recipe saved by the
 * wizard -- `bindings` and `field_names` only, with `steps` on the group that
 * needed a reveal click and absent on the one that did not.
 */

const V2_GROUP: RecipeFieldGroup = {
  group_id: 'group_1',
  field_names: ['img', 'h1', 'span'],
  bindings: { h1: [{ locator: { kind: 'css', selector: 'h1.name' }, priority: 60 }] },
}

const V2_GROUP_WITH_STEPS: RecipeFieldGroup = {
  group_id: 'group_2',
  field_names: ['ul'],
  bindings: { ul: [{ locator: { kind: 'css', selector: 'ul.tags' } }] },
  steps: [{ op: 'click', target: { kind: 'css', selector: '.more' } }],
}

const V1_GROUP: RecipeFieldGroup = {
  group_id: 'core',
  field_names: ['price'],
  field_locators: { price: [{ selector: '.price' }] },
  reveal_steps: [{ op: 'click' }],
}

describe('readGroup', () => {
  it('does not throw on a studio-authored group with no reveal steps', () => {
    // The exact crash: `group.reveal_steps.length` where the key is absent.
    expect(readGroup(V2_GROUP).steps).toEqual([])
  })

  it('reads v2 bindings as the group locators', () => {
    const { locators, v2 } = readGroup(V2_GROUP)
    expect(v2).toBe(true)
    expect(locators).toBe(V2_GROUP.bindings)
  })

  it('reads v2 steps when the group has a reveal click', () => {
    expect(readGroup(V2_GROUP_WITH_STEPS).steps).toHaveLength(1)
  })

  it('still reads an agent-built group the old way', () => {
    const { locators, steps, v2 } = readGroup(V1_GROUP)
    expect(v2).toBe(false)
    expect(locators).toBe(V1_GROUP.field_locators)
    expect(steps).toBe(V1_GROUP.reveal_steps)
  })

  it('survives a group carrying neither shape', () => {
    const { locators, steps } = readGroup({ group_id: 'x', field_names: [] })
    expect(locators).toBeNull()
    expect(steps).toEqual([])
  })
})
