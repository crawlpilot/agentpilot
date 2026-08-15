import {
  ArrowLeft,
  ArrowLeftRight,
  Camera,
  Clock,
  Code,
  FileText,
  Globe,
  ListChecks,
  MousePointer2,
  MousePointerClick,
  MoveVertical,
  Plus,
  Type,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

/**
 * Renders one agent action as a human-readable card -- the persisted action is
 * `{ type: <PythonClassName>, ...fields }` (see `_action_to_dict` in
 * agentpilot/agent/loop.py), so we map the class name to an icon + friendly
 * label + a one-line summary of the salient fields, falling back to a raw
 * key/value dump for any action type we don't have a bespoke renderer for.
 */

type ActionDict = Record<string, unknown>

interface ActionSpec {
  icon: LucideIcon
  label: string
  summary: (a: ActionDict) => ReactNode
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v == null ? '' : String(v)
}

function code(v: unknown): ReactNode {
  const s = str(v)
  if (!s) return null
  return <code className="rounded border border-border bg-muted px-1 py-0.5 font-mono text-[11px]">{s}</code>
}

function truncate(s: string, n = 120): string {
  return s.length > n ? `${s.slice(0, n)}…` : s
}

const SPECS: Record<string, ActionSpec> = {
  NavigateAction: {
    icon: Globe,
    label: 'Navigate',
    summary: (a) => <>to {code(a.url)}</>,
  },
  GoBackAction: { icon: ArrowLeft, label: 'Go back', summary: () => 'to the previous page' },
  ClickAction: {
    icon: MousePointerClick,
    label: 'Click',
    summary: (a) => (
      <>
        {code(a.ref)}
        {a.all ? ' (all matching)' : ''}
      </>
    ),
  },
  FillAction: {
    icon: Type,
    label: 'Fill',
    summary: (a) => (
      <>
        {code(a.ref)} with {code(truncate(str(a.text), 60))}
      </>
    ),
  },
  SelectOptionAction: {
    icon: ListChecks,
    label: 'Select option',
    summary: (a) => (
      <>
        {code(a.ref)} &rarr; {code(Array.isArray(a.values) ? a.values.join(', ') : str(a.values))}
      </>
    ),
  },
  HoverAction: { icon: MousePointer2, label: 'Hover', summary: (a) => code(a.ref) },
  PressAction: { icon: Type, label: 'Press key', summary: (a) => code(a.key) },
  ScrollAction: {
    icon: MoveVertical,
    label: 'Scroll',
    summary: (a) => (
      <>
        {str(a.direction) || 'down'}
        {a.ref ? <> within {code(a.ref)}</> : null}
      </>
    ),
  },
  ExtractAction: {
    icon: FileText,
    label: 'Extract content',
    summary: (a) => <>as {code(str(a.format) || 'markdown')}</>,
  },
  ExecuteJsAction: {
    icon: Code,
    label: 'Run JavaScript',
    summary: (a) => code(truncate(str(a.script), 90)),
  },
  WaitAction: {
    icon: Clock,
    label: 'Wait',
    summary: (a) => (a.ref ? <>for {code(a.ref)}</> : a.ms ? <>{code(`${str(a.ms)}ms`)}</> : 'briefly'),
  },
  ScreenshotAction: {
    icon: Camera,
    label: 'Screenshot',
    summary: (a) => (a.full_page ? 'full page' : 'viewport'),
  },
  NewTabAction: { icon: Plus, label: 'New tab', summary: (a) => (a.url ? <>at {code(a.url)}</> : 'blank') },
  CloseTabAction: { icon: X, label: 'Close tab', summary: (a) => code(a.page_id) },
  SwitchTabAction: { icon: ArrowLeftRight, label: 'Switch tab', summary: (a) => <>to {code(a.page_id)}</> },
}

function humanizeType(type: string): string {
  return type.replace(/Action$/, '').replace(/([a-z])([A-Z])/g, '$1 $2')
}

export function AgentActionCard({ action }: { action: ActionDict }) {
  const type = str(action.type)
  const spec = SPECS[type]
  const Icon = spec?.icon ?? Wrench
  const label = spec?.label ?? (humanizeType(type) || 'Action')

  // Fallback summary: raw non-`type` fields, so an unmapped action still says
  // something useful instead of rendering blank.
  const fallback = Object.entries(action)
    .filter(([k]) => k !== 'type' && k !== 'terminates_sequence')
    .map(([k, v]) => `${k}=${truncate(str(v), 40)}`)
    .join('  ')

  return (
    <div className="flex items-start gap-2.5 rounded-md border border-border bg-muted/40 px-2.5 py-2">
      <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded border border-border bg-accent-tint text-accent">
        <Icon className="size-3" />
      </span>
      <div className="min-w-0 flex-1">
        <span className="text-xs font-semibold">{label}</span>
        <div className="mt-0.5 break-words text-xs text-muted-foreground">
          {spec ? spec.summary(action) : fallback || null}
        </div>
      </div>
    </div>
  )
}
