import { AlertTriangle, Check, Loader2, MinusCircle, Play, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { GroupValidation, ValidationRun } from '@/hooks/useRecipeValidation'
import { cn } from '@/lib/utils'

interface Props {
  /** Pages the author is building against, offered as a picklist. */
  urls: string[]
  url: string
  onUrlChange: (url: string) => void
  /** The URL the page is currently sitting on, i.e. the one authored against. */
  authoredUrl?: string | null
  running: boolean
  progress: string | null
  result: ValidationRun | null
  error: string | null
  onRun: () => void
  disabled?: boolean
  /** Fields bound only to page JSON, which the DOM reader cannot check. */
  structuredOnly: string[]
}

function groupVerdict(group: GroupValidation): 'ok' | 'warn' | 'fail' {
  if (group.error) return 'fail'
  if (group.steps.some((s) => s.status === 'failed')) return 'fail'
  const emptyFields = group.fields.filter((f) => f.status === 'empty' || f.status === 'failed')
  const emptyTables = group.rowFields.filter((t) => t.rows.length === 0)
  if (emptyFields.length > 0 || emptyTables.length > 0) return 'fail'
  if (group.steps.some((s) => s.status === 'skipped')) return 'warn'
  if (group.fields.some((f) => f.status === 'fallback')) return 'warn'
  return 'ok'
}

/**
 * Validate the recipe from a fresh page.
 *
 * The preview above reads the page as it currently stands -- which is a page
 * the author has been clicking on. They opened the drawer by hand to pick the
 * field inside it, so a recipe that never recorded that click still previews
 * perfectly, and fails the first time it runs unattended.
 *
 * This navigates afresh for **every group**, re-runs `global_setup`, then the
 * group's own steps, then reads. It is what `replay.py::_replay_group`
 * actually does, and running it against a *different* sample URL is the
 * cheapest way to find out whether the recipe describes a pattern or just the
 * one page it was built on.
 */
export function StepValidate({
  urls,
  url,
  onUrlChange,
  authoredUrl,
  running,
  progress,
  result,
  error,
  onRun,
  disabled,
  structuredOnly,
}: Props) {
  const sameAsAuthored = authoredUrl != null && url === authoredUrl
  const verdicts = result?.groups.map(groupVerdict) ?? []
  const failed = verdicts.filter((v) => v === 'fail').length

  return (
    <div className="flex flex-col gap-3 border-t border-border pt-3">
      <div className="flex flex-col gap-1.5">
        <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
          Validate from a fresh page
        </p>
        <p className="text-[11px] leading-snug text-muted-foreground">
          Navigates afresh for each group, re-runs the reveal steps, then reads. This is what an
          unattended run does &mdash; and the only way to catch a field that only worked because
          you had already opened something by hand.
        </p>
      </div>

      {urls.length > 0 && (
        <div className="flex flex-col gap-1">
          <Select value={url} onValueChange={onUrlChange}>
            <SelectTrigger className="h-7 text-xs">
              <SelectValue placeholder="pick a sample URL" />
            </SelectTrigger>
            <SelectContent>
              {urls.map((u) => (
                <SelectItem key={u} value={u}>
                  {u}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {sameAsAuthored && urls.length > 1 && (
            <p className="flex items-start gap-1.5 text-[10px] leading-snug text-warning">
              <AlertTriangle className="mt-0.5 size-3 shrink-0" />
              This is the page you authored against. A different sample URL is a much stronger test
              &mdash; it is what separates a pattern from a recipe fitted to one page.
            </p>
          )}
        </div>
      )}

      <Button size="sm" onClick={onRun} disabled={disabled || running || !url}>
        {running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
        {running ? `Running ${progress ?? ''}…` : result ? 'Validate again' : 'Validate'}
      </Button>

      {error && <p className="text-[11px] text-destructive">{error}</p>}

      {result && (
        <div className="flex flex-col gap-2">
          <p
            className={cn(
              'flex items-center gap-1.5 text-[11px] font-medium',
              failed > 0 ? 'text-destructive' : 'text-success',
            )}
          >
            {failed > 0 ? <X className="size-3.5" /> : <Check className="size-3.5" />}
            {failed > 0
              ? `${failed} of ${result.groups.length} groups failed on a fresh page`
              : 'Every group reached its data from a fresh page'}
          </p>

          {result.groups.map((group, i) => {
            const verdict = verdicts[i]
            return (
              <div key={group.groupId} className="rounded-md border border-border p-2">
                <div className="flex items-center gap-1.5">
                  <Badge
                    variant={verdict === 'ok' ? 'success' : verdict === 'warn' ? 'warning' : 'destructive'}
                    className="shrink-0"
                  >
                    {verdict === 'ok' ? 'ok' : verdict === 'warn' ? 'check' : 'failed'}
                  </Badge>
                  <span className="font-mono text-[11px]">{group.groupId}</span>
                </div>

                {group.error && (
                  <p className="pt-1 text-[10px] text-destructive">{group.error}</p>
                )}

                {group.steps.length > 0 && (
                  <div className="flex flex-col gap-0.5 pt-1.5">
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Steps</p>
                    {group.steps.map((step, j) => (
                      <p
                        key={j}
                        className={cn(
                          'flex items-center gap-1 text-[10px]',
                          step.status === 'ok' && 'text-muted-foreground',
                          step.status === 'skipped' && 'text-warning',
                          step.status === 'failed' && 'text-destructive',
                        )}
                      >
                        {step.status === 'ok' ? (
                          <Check className="size-3 shrink-0" />
                        ) : step.status === 'skipped' ? (
                          <MinusCircle className="size-3 shrink-0" />
                        ) : (
                          <X className="size-3 shrink-0" />
                        )}
                        {j + 1}. {step.op}
                        {step.detail ? ` — ${step.detail}` : ''}
                      </p>
                    ))}
                  </div>
                )}

                <div className="flex flex-wrap gap-1 pt-1.5">
                  {group.fields.map((field) => (
                    <Badge
                      key={field.name}
                      variant={
                        field.status === 'resolved'
                          ? 'success'
                          : field.status === 'fallback'
                            ? 'accent'
                            : 'destructive'
                      }
                      className="text-[10px]"
                      title={field.error ?? String(field.value ?? '')}
                    >
                      {field.name}
                      {field.status === 'fallback' && ` #${field.candidate}`}
                    </Badge>
                  ))}
                  {group.rowFields.map((table) => (
                    <Badge
                      key={table.name}
                      variant={table.rows.length > 0 ? 'success' : 'destructive'}
                      className="text-[10px]"
                    >
                      {table.name}: {table.rows.length} rows
                    </Badge>
                  ))}
                </div>
              </div>
            )
          })}

          {structuredOnly.length > 0 && (
            <p className="text-[10px] leading-snug text-muted-foreground">
              Not checked here: <span className="font-mono">{structuredOnly.join(', ')}</span>. These
              read from the page&rsquo;s JSON rather than the DOM, which this reader does not
              address &mdash; not a failure, and the most durable binding available.
            </p>
          )}

          <p className="text-[10px] leading-snug text-muted-foreground">
            Clicks here come from page script rather than a trusted browser event, so a step that
            fails is a real problem while one that passes is not yet proof.
          </p>
        </div>
      )}
    </div>
  )
}
