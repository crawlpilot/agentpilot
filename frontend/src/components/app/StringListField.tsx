import { useState } from 'react'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'

// A labeled comma/newline-separated list editor holding its own text state so
// the user can type freely (trailing separators, spaces) while the parent only
// ever receives a cleaned `string[]` (empty = `[]`). Shared by the Scrape/Crawl
// tabs' `include_tags`/`exclude_tags`, the Crawl tab's `include_paths`/
// `exclude_paths`, and the action builder's snapshot `roles`.
export function StringListField({
  label,
  value,
  onChange,
  placeholder,
  rows,
}: {
  label: string
  value: string[]
  onChange: (v: string[]) => void
  placeholder?: string
  // When set (and > 1), renders a multi-line Textarea instead of an Input.
  rows?: number
}) {
  const [text, setText] = useState(() => value.join(', '))

  function handleChange(next: string) {
    setText(next)
    onChange(
      next
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter(Boolean),
    )
  }

  const multiline = rows !== undefined && rows > 1

  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      {multiline ? (
        <Textarea
          className="text-sm"
          rows={rows}
          placeholder={placeholder}
          value={text}
          onChange={(e) => handleChange(e.target.value)}
        />
      ) : (
        <Input
          className="text-sm"
          placeholder={placeholder}
          value={text}
          onChange={(e) => handleChange(e.target.value)}
        />
      )}
    </div>
  )
}
