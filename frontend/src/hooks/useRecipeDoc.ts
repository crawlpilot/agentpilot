import { useCallback, useEffect, useRef, useState } from 'react'
import { emptyRecipe } from '@/lib/recipe/document'
import type { Recipe } from '@/lib/recipe/types'

const DRAFT_PREFIX = 'agentpilot.studio.draft.'
const UNDO_DEPTH = 50

/**
 * The studio's editing state: one recipe document, undo, and a local draft.
 *
 * The local draft is not a nicety. There is no `PUT /v1/recipes/{id}` yet
 * (see `docs/recipe-studio.md`), so a reload is currently the difference
 * between an afternoon of authoring and nothing. It is also the right
 * behaviour once the endpoint lands: a studio that loses work on a refresh
 * is one nobody trusts with a 39-field document.
 */
export function useRecipeDoc(draftKey: string, initial?: Recipe) {
  const [doc, setDoc] = useState<Recipe>(() => loadDraft(draftKey) ?? initial ?? emptyRecipe())
  const [past, setPast] = useState<Recipe[]>([])
  const [savedAt, setSavedAt] = useState<number | null>(null)
  // The baseline `doc` is compared against for the dirty flag: whatever was
  // last loaded from or written to the server, never the restored draft --
  // a restored draft is by definition unsaved work.
  const baseline = useRef<string>(JSON.stringify(initial ?? null))

  const update = useCallback((fn: (current: Recipe) => Recipe) => {
    setDoc((current) => {
      const next = fn(current)
      if (next === current) return current
      setPast((history) => [...history, current].slice(-UNDO_DEPTH))
      return next
    })
  }, [])

  const undo = useCallback(() => {
    setPast((history) => {
      if (history.length === 0) return history
      setDoc(history[history.length - 1])
      return history.slice(0, -1)
    })
  }, [])

  /** Replace the document wholesale -- a server load, or a JSON-tab paste. */
  const reset = useCallback((next: Recipe, markSaved = false) => {
    setDoc(next)
    setPast([])
    if (markSaved) baseline.current = JSON.stringify(next)
  }, [])

  const markSaved = useCallback((next: Recipe) => {
    baseline.current = JSON.stringify(next)
    setSavedAt(Date.now())
  }, [])

  useEffect(() => {
    const handle = window.setTimeout(() => saveDraft(draftKey, doc), 400)
    return () => window.clearTimeout(handle)
  }, [draftKey, doc])

  const discardDraft = useCallback(() => {
    clearDraft(draftKey)
    if (initial) reset(initial, true)
  }, [draftKey, initial, reset])

  return {
    doc,
    update,
    undo,
    reset,
    markSaved,
    discardDraft,
    canUndo: past.length > 0,
    dirty: JSON.stringify(doc) !== baseline.current,
    savedAt,
  }
}

function loadDraft(key: string): Recipe | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_PREFIX + key)
    return raw ? (JSON.parse(raw) as Recipe) : null
  } catch {
    // A corrupt or quota-blocked draft must never keep the studio from
    // opening -- fall back to the server copy.
    return null
  }
}

function saveDraft(key: string, doc: Recipe) {
  try {
    window.localStorage.setItem(DRAFT_PREFIX + key, JSON.stringify(doc))
  } catch {
    // Over quota. The document is still in memory; losing the draft is
    // strictly better than throwing inside a render effect.
  }
}

function clearDraft(key: string) {
  try {
    window.localStorage.removeItem(DRAFT_PREFIX + key)
  } catch {
    // ignore
  }
}

export function hasDraft(key: string): boolean {
  return loadDraft(key) !== null
}
