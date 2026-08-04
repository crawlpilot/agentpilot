import { useState } from 'react'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

// A labeled JSON-object editor holding its own text state so the user can
// type freely (including transient invalid states) while the parent only
// ever receives a valid object or `null` (empty = "no value provided", a
// valid state, not an error). Shared by the Scrape tab's `extract.json_schema`,
// the Agent Run tab's `output_schema`, and the Recipe create form's
// `field_schema`.
export function JsonTextareaField({
  label,
  value,
  onChange,
  placeholder,
  rows = 6,
}: {
  label: string
  value: Record<string, unknown> | null
  onChange: (v: Record<string, unknown> | null) => void
  placeholder?: string
  rows?: number
}) {
  const [text, setText] = useState(() => (value ? JSON.stringify(value, null, 2) : ''))
  const [error, setError] = useState<string | null>(null)

  function handleChange(next: string) {
    setText(next)
    const trimmed = next.trim()
    if (!trimmed) {
      setError(null)
      onChange(null)
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      setError('Invalid JSON')
      return
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      setError('Must be a JSON object')
      return
    }
    setError(null)
    onChange(parsed as Record<string, unknown>)
  }

  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Textarea
        className="font-mono text-xs"
        rows={rows}
        placeholder={placeholder}
        value={text}
        onChange={(e) => handleChange(e.target.value)}
      />
      {error && <span className="text-xs text-destructive">{error}</span>}
    </div>
  )
}
