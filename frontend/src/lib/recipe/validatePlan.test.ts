import { describe, it, expect } from 'vitest'
import { buildValidationPlan, structuredOnlyFields } from './validatePlan'
import { emptyRecipe } from './document'
import type { Recipe } from './types'

/**
 * The plan must mirror `replay.py::_replay_group`. Where it does not, the
 * validation is reassuring rather than useful -- it would pass recipes that
 * replay then fails, which is worse than not validating at all.
 */

const css = (selector: string) => ({ kind: 'css' as const, selector })

function recipe(patch: Partial<Recipe>): Recipe {
  return { ...emptyRecipe('r'), ...patch }
}

describe('buildValidationPlan', () => {
  it('prefixes every group with global_setup, not just the first', () => {
    // `_replay_group` navigates then runs `global_setup` per group. A plan
    // that ran it once would leave later groups facing a consent wall.
    const plan = buildValidationPlan(recipe({
      global_setup: [{ op: 'click', target: css('#consent') }],
      fields: {
        a: { type: { kind: 'scalar', value_type: 'string' } },
        b: { type: { kind: 'scalar', value_type: 'string' } },
      },
      field_groups: [
        { group_id: 'g1', field_names: ['a'], bindings: { a: [{ locator: css('.a') }] } },
        {
          group_id: 'g2',
          field_names: ['b'],
          bindings: { b: [{ locator: css('.b') }] },
          steps: [{ op: 'click', target: css('#more') }],
        },
      ],
    }))

    expect(plan).toHaveLength(2)
    expect(plan[0].steps.map((s) => s.selector)).toEqual(['#consent'])
    // Global setup first, then the group's own steps, in that order.
    expect(plan[1].steps.map((s) => s.selector)).toEqual(['#consent', '#more'])
  })

  it('reads only the fields its own group collects', () => {
    const plan = buildValidationPlan(recipe({
      fields: {
        a: { type: { kind: 'scalar', value_type: 'string' } },
        b: { type: { kind: 'scalar', value_type: 'string' } },
      },
      field_groups: [
        { group_id: 'g1', field_names: ['a'], bindings: { a: [{ locator: css('.a') }] } },
        { group_id: 'g2', field_names: ['b'], bindings: { b: [{ locator: css('.b') }] } },
      ],
    }))
    expect(plan[0].fields.map((f) => f.name)).toEqual(['a'])
    expect(plan[1].fields.map((f) => f.name)).toEqual(['b'])
  })

  it('reads a table row-wise, by column', () => {
    const plan = buildValidationPlan(recipe({
      fields: {
        items: {
          type: {
            kind: 'table',
            columns: {
              title: { kind: 'scalar', value_type: 'string' },
              price: { kind: 'scalar', value_type: 'price' },
            },
          },
        },
      },
      field_groups: [{
        group_id: 'g1',
        field_names: ['items'],
        bindings: { title: [{ locator: css('.t') }], price: [{ locator: css('.p') }] },
        repeat: {
          kind: 'dom_rows',
          rows_locator: { kind: 'css', selector: 'li.card', within: css('#results') },
          row_field: 'items',
          max_iterations: 50,
        },
      }],
    }))
    expect(plan[0].fields).toEqual([])
    expect(plan[0].rowFields).toHaveLength(1)
    expect(Object.keys(plan[0].rowFields[0].columns).sort()).toEqual(['price', 'title'])
    expect(plan[0].rowFields[0].rows.within?.selector).toBe('#results')
    expect(plan[0].rowFields[0].maxRows).toBe(50)
  })

  it('carries a candidate chain through, so a fallback still counts', () => {
    const plan = buildValidationPlan(recipe({
      fields: { a: { type: { kind: 'scalar', value_type: 'string' } } },
      field_groups: [{
        group_id: 'g1',
        field_names: ['a'],
        bindings: { a: [{ locator: css('.gone') }, { locator: css('.real') }] },
      }],
    }))
    expect(plan[0].fields[0].candidates.map((c) => c.selector)).toEqual(['.gone', '.real'])
  })

  it('drops steps and candidates the DOM reader cannot address', () => {
    const plan = buildValidationPlan(recipe({
      global_setup: [{ op: 'click', target: { kind: 'ax_role', role: 'button' } }],
      fields: { a: { type: { kind: 'scalar', value_type: 'string' } } },
      field_groups: [{
        group_id: 'g1',
        field_names: ['a'],
        bindings: {
          a: [{ locator: { kind: 'json_ld', path: 'x' } }, { locator: css('.a') }],
        },
      }],
    }))
    // Silently pretending to have run an ax_role click would be a lie about
    // what was verified.
    expect(plan[0].steps).toEqual([])
    expect(plan[0].fields[0].candidates.map((c) => c.selector)).toEqual(['.a'])
  })
})

describe('structuredOnlyFields', () => {
  it('names fields the DOM reader cannot check, so empty is not read as broken', () => {
    const r = recipe({
      fields: {
        a: { type: { kind: 'scalar', value_type: 'string' } },
        b: { type: { kind: 'scalar', value_type: 'string' } },
      },
      field_groups: [{
        group_id: 'g1',
        field_names: ['a', 'b'],
        bindings: {
          a: [{ locator: { kind: 'json_ld', path: 'offers.price' } }],
          b: [{ locator: css('.b') }],
        },
      }],
    })
    expect(structuredOnlyFields(r)).toEqual(['a'])
  })
})

describe('structured candidates in the plan', () => {
  const jsonRecipe = (): Recipe => ({
    ...emptyRecipe('zara'),
    fields: {
      name: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
      title: { type: { kind: 'scalar', value_type: 'string' }, description: '' },
    },
    field_groups: [
      {
        group_id: 'core',
        field_names: ['name', 'title'],
        bindings: {
          name: [
            {
              priority: 15,
              locator: { kind: 'hydration', path: '__NEXT_DATA__.props.name', path_lang: 'simple' },
              verified_on: 1,
            },
          ],
          title: [{ priority: 60, locator: { kind: 'css', selector: 'h1' }, verified_on: 1 }],
        },
      },
    ],
  })

  it('carries a JSON-bound field so validation can resolve it', () => {
    // `fields` is filtered to what the in-page reader handles, which is the
    // DOM -- so a hydration-only field is absent there and validated blank.
    const [group] = buildValidationPlan(jsonRecipe())

    expect(group.fields.find((f) => f.name === 'name')?.candidates).toEqual([])
    expect(group.scalarCandidates.map((c) => c.name)).toEqual(['name'])
  })

  it('does not carry a field the DOM reader can already answer for', () => {
    const [group] = buildValidationPlan(jsonRecipe())
    expect(group.scalarCandidates.some((c) => c.name === 'title')).toBe(false)
  })
})
