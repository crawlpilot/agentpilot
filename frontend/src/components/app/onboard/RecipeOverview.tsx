import { useState } from 'react'
import { ArrowRight, MousePointerClick } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { SourceBadge } from '@/components/app/wizard/SourceBadge'
import { targetAccepts } from '@/lib/recipe/urlmatch'
import type { Candidate, FieldGroup, Recipe, Step } from '@/lib/recipe/types'

/**
 * The scraper, in the terms someone deciding whether to keep it thinks in.
 *
 * The build hands back a document, and a document is not an answer to "did it
 * work?". Two things have to be visible together for that: **what it read**,
 * which is the value, and **where it read it from**, which is what predicts
 * whether it will still work next month. A `price` of 2290 is reassuring; a
 * `price` of 2290 pulled out of `hydration` with a CSS fallback behind it is
 * the thing worth publishing, and one read off `div:nth-child(4) > span` is
 * not, however right it looks today.
 *
 * So every field shows its winning source, how many fallbacks stand behind it,
 * and the value it actually produced on a real page.
 */
export function RecipeOverview({
  recipe,
  values,
  verdicts = {},
}: {
  recipe: Recipe
  values: Record<string, unknown>
  /**
   * The judge's per-field findings, where it rejected one. Shown against the
   * field rather than in a separate list: "this is the breadcrumb trail" only
   * means anything next to the value it is describing.
   */
  verdicts?: Record<string, { ok: boolean; reason?: string }>
}) {
  const groups = recipe.field_groups ?? []
  const fields = recipe.fields ?? {}
  const setup = recipe.global_setup ?? []

  return (
    <div className="flex w-full max-w-3xl flex-col gap-4 text-left">
      <TargetLine recipe={recipe} />

      {setup.length > 0 && (
        <StepList
          title="Before every read"
          hint="Runs once per page load, for every group below."
          steps={setup}
        />
      )}

      {groups.map((group) => (
        <GroupCard
          key={group.group_id}
          group={group}
          fields={fields}
          values={values}
          verdicts={verdicts}
        />
      ))}
    </div>
  )
}

/**
 * Which URLs the recipe will accept, and a box to check one against it.
 *
 * The matcher is guessed from the sample URLs and decides, before a browser is
 * ever opened, whether this is a reusable recipe or a bookmark. Too narrow is
 * the failure that hides: everything looks right, and the first time anyone
 * finds out is when a perfectly good recipe refuses their URL. So the check is
 * offered here, where the person still has the page in front of them, using the
 * same matching code the worker runs.
 */
function TargetLine({ recipe }: { recipe: Recipe }) {
  const match = recipe.target?.match ?? []
  const [probe, setProbe] = useState('')
  const trimmed = probe.trim()
  const accepted = trimmed ? targetAccepts(recipe.target ?? {}, trimmed) : null

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted-foreground">Applies to</span>
        {match.length === 0 ? (
          // `validate_document` warns about this too. It is worth surfacing here
          // rather than only in the linter: a recipe in a shared catalogue that
          // accepts any URL will be pointed at pages it was never built for.
          <Badge variant="warning">any URL — worth narrowing before publishing</Badge>
        ) : (
          match.map((matcher, i) => (
            <code key={i} className="rounded bg-muted px-1.5 py-0.5 font-mono">
              {matcher.pattern}
            </code>
          ))
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <input
          className="h-6 min-w-0 flex-1 rounded border border-border bg-transparent px-1.5 font-mono text-[11px]"
          value={probe}
          placeholder="try another page of the same kind…"
          onChange={(e) => setProbe(e.target.value)}
        />
        {accepted !== null &&
          (accepted ? (
            <Badge variant="success">accepted</Badge>
          ) : (
            <Badge variant="destructive">refused</Badge>
          ))}
      </div>
      {accepted === false && (
        <p className="text-[11px] text-muted-foreground">
          Widen the pattern in the studio&rsquo;s Target tab — usually by replacing the part of
          the path that names one page with <code className="font-mono">*</code>.
        </p>
      )}
    </div>
  )
}

function GroupCard({
  group,
  fields,
  values,
  verdicts,
}: {
  group: FieldGroup
  fields: Record<string, unknown>
  values: Record<string, unknown>
  verdicts: Record<string, { ok: boolean; reason?: string }>
}) {
  const steps = group.steps ?? []
  const repeat = group.repeat

  return (
    <div className="rounded-md border border-border">
      {steps.length > 0 && (
        <div className="border-b border-border bg-muted/40 px-3 py-2">
          <StepList
            title="First"
            hint="Needed to make the values below reachable."
            steps={steps}
            compact
          />
        </div>
      )}

      {repeat && (
        <div className="border-b border-border px-3 py-1.5 text-[11px] text-muted-foreground">
          One row per{' '}
          <Badge variant="outline" className="mx-1">
            {repeat.kind === 'json'
              ? 'item in the page’s JSON'
              : repeat.kind === 'dom_rows'
                ? 'row element on the page'
                : 'option it clicks through'}
          </Badge>
          {repeat.kind === 'dom' && (
            // Worth saying out loud: this is the only repeat kind that mutates
            // the page, and it costs one page interaction per row.
            <span> — up to {repeat.max_iterations}, clicking each in turn</span>
          )}
        </div>
      )}

      <div className="divide-y divide-border">
        {Object.entries(group.bindings ?? {}).map(([name, candidates]) => (
          <FieldRow
            key={name}
            name={name}
            candidates={candidates}
            spec={fields[name]}
            value={values[name]}
            verdict={verdicts[name]}
          />
        ))}
      </div>
    </div>
  )
}

function FieldRow({
  name,
  candidates,
  spec,
  value,
  verdict,
}: {
  name: string
  candidates: Candidate[]
  spec: unknown
  value: unknown
  verdict?: { ok: boolean; reason?: string }
}) {
  const chain = [...(candidates ?? [])].sort((a, b) => (a.priority ?? 100) - (b.priority ?? 100))
  const winner = chain[0]
  const fallbacks = chain.length - 1
  const empty = value === undefined || value === null || value === ''
  const required = Boolean((spec as { required?: boolean } | undefined)?.required)

  return (
    <div className="flex flex-col gap-1 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="font-mono text-xs font-medium">{name}</span>
        {required && <Badge variant="outline">required</Badge>}
        {winner && <SourceBadge kind={winner.locator.kind} />}
        {verdict && !verdict.ok && <Badge variant="warning">looks wrong</Badge>}
        {fallbacks > 0 && (
          <Badge
            variant="outline"
            title="Tried in order; the first that reads a value wins. More is more durable."
          >
            +{fallbacks} fallback{fallbacks === 1 ? '' : 's'}
          </Badge>
        )}
      </div>

      {winner && (
        <code className="truncate font-mono text-[11px] text-muted-foreground" title={where(winner)}>
          {where(winner)}
        </code>
      )}

      <p className={`truncate text-xs ${empty ? 'italic text-muted-foreground' : ''}`}>
        {empty ? 'nothing collected on the sample page' : preview(value)}
      </p>

      {verdict && !verdict.ok && verdict.reason && (
        <p className="text-[11px] text-warning">{verdict.reason}</p>
      )}
    </div>
  )
}

function StepList({
  title,
  hint,
  steps,
  compact = false,
}: {
  title: string
  hint: string
  steps: Step[]
  compact?: boolean
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <MousePointerClick className="size-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">{title}</span>
        {!compact && <span className="text-[11px] text-muted-foreground">{hint}</span>}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        {steps.map((step, i) => (
          <span key={i} className="flex items-center gap-1">
            {i > 0 && <ArrowRight className="size-3 text-muted-foreground" />}
            <Badge variant="outline" className="font-normal">
              {describe(step)}
            </Badge>
          </span>
        ))}
      </div>
    </div>
  )
}

/** Where a candidate reads from, in one line. */
function where(candidate: Candidate): string {
  const { kind, selector, path, role, name_contains, attribute } = candidate.locator
  if (path) return path
  if (kind === 'ax_role') return `${role ?? 'role'}${name_contains ? ` “${name_contains}”` : ''}`
  const base = selector ?? ''
  return attribute && attribute !== 'text' ? `${base} @${attribute}` : base
}

function describe(step: Step): string {
  const target = step.target
  const what =
    target?.selector ?? target?.name_contains ?? target?.role ?? (step.args?.text as string) ?? ''
  const op = step.op.replace(/_/g, ' ')
  return what ? `${op} ${truncate(String(what), 28)}` : op
}

function preview(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return `${value.length} item${value.length === 1 ? '' : 's'} — ${truncate(JSON.stringify(value), 90)}`
  }
  return truncate(JSON.stringify(value) ?? '', 120)
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}
