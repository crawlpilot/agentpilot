import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { hasDraft, useRecipeDoc } from './useRecipeDoc'
import { emptyRecipe } from '@/lib/recipe/document'
import type { Recipe } from '@/lib/recipe/types'

/**
 * The load half of the studio's editing state.
 *
 * `hydrate` exists because a document that arrives from a *fetch* can never
 * reach a `useState` initialiser -- that runs once, on first render, before any
 * query resolves. Every existing recipe arrives that way, so a surface that
 * relies on the initialiser shows an empty editor for all of them. That is
 * exactly what `RecipeStudioPage` did: it passed `emptyRecipe(name)` as the seed
 * and never called `hydrate`, so opening a built recipe gave you its title and
 * none of its fields.
 */

const DRAFT_PREFIX = 'agentpilot.studio.draft.'

function recipe(name: string, fieldName = 'price'): Recipe {
  return {
    ...emptyRecipe(name),
    fields: { [fieldName]: { type: { kind: 'scalar', value_type: 'string' } } },
  } as Recipe
}

afterEach(() => {
  window.localStorage.clear()
})

describe('hydrate', () => {
  it('delivers a document that arrives after the first render', () => {
    const { result } = renderHook(() => useRecipeDoc('r-1'))
    expect(Object.keys(result.current.doc.fields ?? {})).toEqual([])

    act(() => result.current.hydrate(recipe('Walgreens PDP')))

    expect(result.current.doc.name).toBe('Walgreens PDP')
    expect(Object.keys(result.current.doc.fields ?? {})).toEqual(['price'])
  })

  it('is once-only, so a re-render cannot undo edits made since the load', () => {
    const { result } = renderHook(() => useRecipeDoc('r-1'))
    act(() => result.current.hydrate(recipe('First')))
    act(() => result.current.update((r) => ({ ...r, name: 'Edited by hand' })))

    // A query that refetches hands the same document back; treating that as a
    // fresh load would throw away everything typed since.
    act(() => result.current.hydrate(recipe('First')))

    expect(result.current.doc.name).toBe('Edited by hand')
  })

  it('marks the loaded document as the clean baseline, not as unsaved work', () => {
    const { result } = renderHook(() => useRecipeDoc('r-1'))
    act(() => result.current.hydrate(recipe('Loaded')))

    expect(result.current.dirty).toBe(false)

    act(() => result.current.update((r) => ({ ...r, name: 'Changed' })))
    expect(result.current.dirty).toBe(true)
  })
})

describe('a local draft outranks the server copy', () => {
  it('declines to hydrate over one', () => {
    window.localStorage.setItem(
      DRAFT_PREFIX + 'r-1',
      JSON.stringify(recipe('Unsaved work', 'rating')),
    )

    const { result } = renderHook(() => useRecipeDoc('r-1'))
    act(() => result.current.hydrate(recipe('Server copy')))

    // The rule this pins: an unsaved draft is work somebody did, and silently
    // replacing it with the server copy is the one failure a draft mechanism
    // exists to prevent.
    expect(result.current.doc.name).toBe('Unsaved work')
    expect(Object.keys(result.current.doc.fields ?? {})).toEqual(['rating'])
  })

  it('which is why the shadowing has to be visible', () => {
    // The corollary, and the bug it caused: with no save button every visit left
    // a draft, so a recipe opened once and abandoned was shadowed forever with
    // nothing on screen saying so. `hasDraft` is how the studio now knows to say
    // it -- it was exported and unused.
    expect(hasDraft('r-1')).toBe(false)

    const { result } = renderHook(() => useRecipeDoc('r-1'))
    act(() => result.current.update((r) => ({ ...r, name: 'Touched' })))

    // Autosave is debounced by 400ms; the point here is only that editing is
    // what creates the shadow, which is why the banner reads the draft once at
    // mount rather than on every render.
    expect(result.current.dirty).toBe(true)
  })
})

describe('discardDraft', () => {
  it('clears the stored draft and restores the server copy', () => {
    const server = recipe('Server copy')
    window.localStorage.setItem(DRAFT_PREFIX + 'r-1', JSON.stringify(recipe('Stale draft')))

    const { result } = renderHook(() => useRecipeDoc('r-1', server))
    expect(result.current.doc.name).toBe('Stale draft')

    act(() => result.current.discardDraft())

    expect(result.current.doc.name).toBe('Server copy')
    expect(hasDraft('r-1')).toBe(false)
  })
})

describe('markSaved', () => {
  it('makes the current document the clean baseline', () => {
    const { result } = renderHook(() => useRecipeDoc('r-1'))
    act(() => result.current.hydrate(recipe('Loaded')))
    act(() => result.current.update((r) => ({ ...r, name: 'Edited' })))
    expect(result.current.dirty).toBe(true)

    act(() => result.current.markSaved(result.current.doc))

    expect(result.current.dirty).toBe(false)
    expect(result.current.savedAt).not.toBeNull()
  })
})
