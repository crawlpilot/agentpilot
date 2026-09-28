// The one lint whose message named the wrong repeat kind.
//
// `json` and `dom_rows` both need a `rows_locator` and one branch catches both,
// which was right -- but it said "A json repeat needs a rows_locator"
// unconditionally. An author whose *table* repeat was missing its row selector
// was told about a repeat kind their group does not use, on the one error that
// also blocks saving (`recipe/v2/validate.py` rejects it server-side with 422).

import { describe, expect, it } from 'vitest'
import { emptyRecipe } from './document'
import { lintRecipe } from './lint'
import type { Recipe, RepeatSpec } from './types'

function tableRecipe(repeat: RepeatSpec): Recipe {
  return {
    ...emptyRecipe('specs'),
    fields: {
      specs: {
        type: {
          kind: 'table',
          columns: {
            name: { kind: 'scalar', value_type: 'string' },
            value: { kind: 'scalar', value_type: 'string' },
          },
        },
        description: '',
      },
    },
    field_groups: [
      {
        group_id: 'core',
        field_names: ['specs'],
        bindings: {
          name: [{ locator: { kind: 'css', selector: 'th', attribute: 'text' }, priority: 60 }],
          value: [{ locator: { kind: 'css', selector: 'td', attribute: 'text' }, priority: 60 }],
        },
        repeat,
      },
    ],
  }
}

const rowsLocatorIssue = (recipe: Recipe) =>
  lintRecipe(recipe).find((i) => i.message.includes('rows_locator'))

describe('a repeat missing its rows_locator', () => {
  it('names dom_rows when the repeat is dom_rows', () => {
    const issue = rowsLocatorIssue(
      tableRecipe({ kind: 'dom_rows', row_field: 'specs', max_iterations: 100 }),
    )
    expect(issue?.severity).toBe('error')
    expect(issue?.message).toBe('A dom_rows repeat needs a rows_locator.')
  })

  it('still names json when the repeat is json', () => {
    const issue = rowsLocatorIssue(
      tableRecipe({ kind: 'json', row_field: 'specs', max_iterations: 100 }),
    )
    expect(issue?.message).toBe('A json repeat needs a rows_locator.')
  })

  it('says nothing once the rows_locator is there', () => {
    const issue = rowsLocatorIssue(
      tableRecipe({
        kind: 'dom_rows',
        row_field: 'specs',
        max_iterations: 100,
        rows_locator: { kind: 'css', selector: 'tr', attribute: 'text' },
      }),
    )
    expect(issue).toBeUndefined()
  })
})

describe('the never-verified lint', () => {
  const approved = (verified: number[]): Recipe => ({
    ...emptyRecipe('p'),
    status: 'approved',
    fields: { price: { type: { kind: 'scalar', value_type: 'float' }, description: '' } },
    field_groups: [
      {
        group_id: 'core',
        field_names: ['price'],
        bindings: {
          price: verified.map((verified_on, i) => ({
            locator: { kind: 'css' as const, selector: `.p${i}`, attribute: 'text' },
            priority: 60 + i,
            verified_on,
          })),
        },
      },
    ],
  })

  const unverified = (recipe: Recipe) =>
    lintRecipe(recipe).filter((i) => i.message.includes('has ever resolved'))

  it('says nothing when a healthy primary carries the field', () => {
    // The fallback reads 0 because it was never REACHED, not because it is
    // broken -- `verified_on` counts pages a candidate actually won on. Warning
    // here would fire on almost every fallback in every approved recipe.
    expect(unverified(approved([2, 0]))).toHaveLength(0)
  })

  it('warns once when nothing in the chain has ever resolved', () => {
    const issues = unverified(approved([0, 0]))
    expect(issues).toHaveLength(1)
    expect(issues[0].severity).toBe('warning')
  })

  it('stays quiet on a draft, which has not been reviewed yet', () => {
    expect(unverified({ ...approved([0, 0]), status: 'draft' })).toHaveLength(0)
  })
})
