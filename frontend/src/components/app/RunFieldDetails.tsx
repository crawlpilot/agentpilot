import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { AssertionResult, FieldProvenance, FieldStatus } from '@/lib/recipe/types'

/**
 * Which selector actually produced each value, out of the several that could
 * have.
 *
 * A recipe binds a *chain* per field — structured data first, then the DOM —
 * and `run.data` shows only what came out the end of it. So a value that looks
 * wrong raised a question the UI could not answer: was that the id selector or
 * the third-choice xpath, and what happened to the ones ahead of it?
 *
 * `candidate 2 of 4` is the drift signal the operational model turns on: a
 * field that starts resolving from candidate 2 instead of candidate 0 is
 * breaking, days before it breaks. It is shown as a warning for that reason,
 * not as an error — the run succeeded, and that is exactly what makes it easy
 * to miss.
 */

function statusVariant(status?: FieldStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'resolved':
      return 'success'
    case 'fallback':
      // Not a failure: it produced a value. But it produced it from a selector
      // that was not the first choice, which is the thing worth noticing.
      return 'warning'
    case 'suspect':
      return 'warning'
    case 'failed':
      return 'destructive'
    default:
      return 'outline'
  }
}

const OUTCOME_LABEL: Record<string, string> = {
  won: 'produced the value',
  empty: 'matched nothing',
  raised: 'threw',
  transform_failed: 'cleanup errored',
  cleaned_to_nothing: 'matched, but cleaned up to nothing',
}

/** A locator as one readable line — its kind, then whatever addresses it. */
function describeLocator(locator?: Record<string, unknown> | null): string {
  if (!locator) return '—'
  const kind = String(locator.kind ?? '?')
  const target =
    (locator.selector as string | undefined) ??
    (locator.path as string | undefined) ??
    [locator.role, locator.name_contains].filter(Boolean).join(' · ') ??
    ''
  const bits: string[] = []
  if (locator.attribute && locator.attribute !== 'text') bits.push(`@${locator.attribute}`)
  if (locator.all) bits.push('all')
  if (typeof locator.index === 'number') bits.push(`[${locator.index}]`)
  if (locator.path_lang && locator.path_lang !== 'simple') bits.push(String(locator.path_lang))
  return `${kind}  ${target}${bits.length ? `  (${bits.join(', ')})` : ''}`
}

function LocatorLine({ locator }: { locator?: Record<string, unknown> | null }) {
  const within = locator?.within as Record<string, unknown> | undefined
  return (
    <div className="flex flex-col gap-0.5">
      <code className="break-all font-mono text-[11px]">{describeLocator(locator)}</code>
      {within && (
        // Scoping is not decoration: a selector confined to `#specifications`
        // keeps working when the page around it is redesigned, and stops
        // matching a same-shaped table somewhere else entirely.
        <code className="break-all font-mono text-[10px] text-muted-foreground">
          within {describeLocator(within)}
        </code>
      )}
    </div>
  )
}

interface Props {
  data: Record<string, unknown>
  provenance: Record<string, FieldProvenance>
  fieldStatus?: Record<string, FieldStatus>
  assertions?: Record<string, AssertionResult[]>
  truncated?: Record<string, boolean>
}

export function RunFieldDetails({
  data,
  provenance,
  fieldStatus = {},
  assertions = {},
  truncated = {},
}: Props) {
  const [open, setOpen] = useState<Record<string, boolean>>({})

  // Every field the run has anything to say about, not just the ones that
  // produced a value: a field that came back empty is the one most worth
  // seeing the attempts for.
  const fields = Array.from(
    new Set([...Object.keys(provenance), ...Object.keys(data), ...Object.keys(fieldStatus)]),
  ).sort()

  if (fields.length === 0) return null

  return (
    <div className="flex flex-col gap-1">
      {fields.map((name) => {
        const prov = provenance[name] as (FieldProvenance & Record<string, unknown>) | undefined
        const status = fieldStatus[name]
        const attempts = (prov?.attempts as Array<Record<string, unknown>> | undefined) ?? []
        const columns = (prov?.columns as Record<string, Record<string, unknown>> | undefined) ?? {}
        const failedChecks = (assertions[name] ?? []).filter((c) => !c.passed)
        // The chain position only means something next to its length.
        const chain =
          typeof prov?.candidate === 'number' && typeof prov?.candidates === 'number'
            ? `candidate ${prov.candidate + 1} of ${prov.candidates}`
            : undefined
        const expandable = attempts.length > 0 || Object.keys(columns).length > 0
        const isOpen = open[name] ?? false

        return (
          <div key={name} className="rounded-md border border-border">
            <button
              type="button"
              className="flex w-full items-start gap-2 px-2 py-1.5 text-left"
              onClick={() => expandable && setOpen((p) => ({ ...p, [name]: !isOpen }))}
              aria-expanded={expandable ? isOpen : undefined}
              disabled={!expandable}
            >
              {expandable ? (
                isOpen ? (
                  <ChevronDown className="mt-0.5 size-3.5 shrink-0" />
                ) : (
                  <ChevronRight className="mt-0.5 size-3.5 shrink-0" />
                )
              ) : (
                <span className="size-3.5 shrink-0" />
              )}

              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-xs font-medium">{name}</span>
                  {status && (
                    <Badge variant={statusVariant(status)} className="capitalize">
                      {status}
                    </Badge>
                  )}
                  {prov?.source && <Badge variant="outline">{prov.source}</Badge>}
                  {prov?.repeat_kind ? (
                    <Badge variant="outline">rows: {String(prov.repeat_kind)}</Badge>
                  ) : null}
                  {typeof prov?.rows === 'number' && (
                    <span className="text-[11px] text-muted-foreground">{prov.rows} rows</span>
                  )}
                  {chain && (
                    <span
                      className={
                        prov && prov.candidate > 0
                          ? 'text-[11px] font-medium text-warning'
                          : 'text-[11px] text-muted-foreground'
                      }
                    >
                      {chain}
                    </span>
                  )}
                  {truncated[name] && <Badge variant="warning">truncated</Badge>}
                </div>

                <LocatorLine locator={prov?.locator as Record<string, unknown> | undefined} />

                {failedChecks.length > 0 && (
                  <p className="text-[11px] text-destructive">
                    {failedChecks.map((c) => c.detail || c.kind).join('; ')}
                  </p>
                )}
              </div>
            </button>

            {isOpen && (
              <div className="flex flex-col gap-2 border-t border-border px-2 py-2 pl-7">
                {attempts.length > 0 && (
                  <div className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-muted-foreground">
                      Every candidate tried, in order
                    </span>
                    {attempts.map((a, i) => {
                      const outcome = String(a.outcome ?? '')
                      return (
                        <div key={i} className="flex items-start gap-2">
                          <Badge
                            variant={outcome === 'won' ? 'success' : 'outline'}
                            className="shrink-0"
                          >
                            {OUTCOME_LABEL[outcome] ?? outcome}
                          </Badge>
                          <div className="min-w-0 flex-1">
                            <LocatorLine
                              locator={a.locator as Record<string, unknown> | undefined}
                            />
                            {a.detail ? (
                              <code className="break-all font-mono text-[10px] text-muted-foreground">
                                {String(a.detail)}
                              </code>
                            ) : null}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}

                {Object.keys(columns).length > 0 && (
                  <div className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium text-muted-foreground">
                      Column selectors, resolved inside one row
                    </span>
                    {Object.entries(columns).map(([col, locator]) => (
                      <div key={col} className="flex items-start gap-2">
                        <span className="shrink-0 font-mono text-[11px]">{col}</span>
                        <LocatorLine locator={locator} />
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
