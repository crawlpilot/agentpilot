import { useState } from 'react'
import { Download, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { CopyButton } from '@/components/app/CopyButton'
import { toExport } from '@/lib/recipe/document'
import type { Recipe } from '@/lib/recipe/types'

interface Props {
  recipe: Recipe
  onReplace: (next: Recipe) => void
}

/**
 * The raw document -- the escape hatch, and the thing you paste into a bug
 * report.
 *
 * Editing is deliberately explicit rather than live: a half-typed brace is not
 * a document, and rewriting the editor's state on every keystroke would
 * discard the rest of it. Paste, then apply.
 */
export function JsonTab({ recipe, onReplace }: Props) {
  const serialized = JSON.stringify(toExport(recipe), null, 2)
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  function apply() {
    if (draft === null) return
    try {
      const parsed = JSON.parse(draft) as Recipe
      if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.field_groups)) {
        setError('That parses, but it is not a recipe document (no field_groups array).')
        return
      }
      onReplace(parsed)
      setDraft(null)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  function download() {
    const blob = new Blob([serialized], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${recipe.name || 'recipe'}.v2.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <CopyButton text={serialized} />
        <Button size="sm" variant="outline" onClick={download}>
          <Download className="size-3.5" />
          Download
        </Button>
        <label className="inline-flex">
          <Button size="sm" variant="outline" asChild>
            <span className="cursor-pointer">
              <Upload className="size-3.5" />
              Import file
            </span>
          </Button>
          <input
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={async (e) => {
              const file = e.target.files?.[0]
              if (!file) return
              setDraft(await file.text())
              e.target.value = ''
            }}
          />
        </label>
        {draft !== null && (
          <>
            <Button size="sm" onClick={apply}>
              Apply
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(null)
                setError(null)
              }}
            >
              Discard
            </Button>
          </>
        )}
      </div>

      {error && <p className="text-xs text-destructive">{error}</p>}

      <Textarea
        className="min-h-[60vh] font-mono text-xs"
        spellCheck={false}
        value={draft ?? serialized}
        onChange={(e) => {
          setDraft(e.target.value)
          setError(null)
        }}
      />
      {draft !== null && (
        <p className="text-[11px] text-muted-foreground">
          Edited but not applied. The panes still show the previous document.
        </p>
      )}
    </div>
  )
}
