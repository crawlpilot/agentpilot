import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Play } from 'lucide-react'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { PlaygroundActionBuilder } from '@/components/app/PlaygroundActionBuilder'
import { PlaygroundResultPanel } from '@/components/app/PlaygroundResultPanel'
import { CodeSnippetPanel } from '@/components/app/CodeSnippetPanel'
import { SnapshotTree } from '@/components/app/SnapshotTree'
import { OpenSessionSheet } from '@/components/app/OpenSessionSheet'
import { EmptyState } from '@/components/app/EmptyState'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { useAuth } from '@/lib/auth/AuthContext'
import { useToast } from '@/components/ui/toast'
import type { ActionIn, AXSnapshot } from '@/lib/api/types'

export function PlaygroundInteractTab() {
  const { apiKey } = useAuth()
  const { toast } = useToast()
  const [searchParams] = useSearchParams()
  const { data: sessionsData } = useSessionsList()
  const sessions = (sessionsData?.sessions ?? []).filter((s) => s.state === 'active')

  // Empty string, not `null`, as the "no selection" sentinel -- Radix's
  // `Select` must receive the same *type* of `value` on every render or it
  // logs a "changing from uncontrolled to controlled" warning the first
  // time a session is picked.
  const [sessionId, setSessionId] = useState<string>(searchParams.get('session') ?? '')
  const [actions, setActions] = useState<ActionIn[]>([{ type: 'navigate', url: '' }])
  const execute = useExecuteSession()
  const refPicker = useExecuteSession()
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerSnapshot, setPickerSnapshot] = useState<AXSnapshot | null>(null)
  const [pickerTargetIndex, setPickerTargetIndex] = useState<number | null>(null)

  function runSequence() {
    if (!sessionId) return
    execute.mutate(
      { sessionId, actions },
      {
        onError: (err) => toast({ title: 'Execute failed', description: err.message, variant: 'destructive' }),
      },
    )
  }

  function openRefPicker(index: number) {
    if (!sessionId) return
    setPickerTargetIndex(index)
    setPickerOpen(true)
    setPickerSnapshot(null)
    refPicker.mutate(
      { sessionId, actions: [{ type: 'snapshot' }] },
      {
        onSuccess: (result) => setPickerSnapshot(result.snapshots[0] ?? null),
        onError: (err) => {
          toast({ title: 'Snapshot failed', description: err.message, variant: 'destructive' })
          setPickerOpen(false)
        },
      },
    )
  }

  function pickRef(ref: string) {
    if (pickerTargetIndex === null) return
    setActions((prev) =>
      prev.map((a, i) => (i === pickerTargetIndex && 'ref' in a ? { ...a, ref } : a)),
    )
    setPickerOpen(false)
  }

  if (!apiKey) {
    return (
      <EmptyState
        title="Tenant API key required"
        description="Sign in with a tenant API key to run sessions -- an admin token alone doesn't carry tenant scope."
      />
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <Select value={sessionId} onValueChange={setSessionId}>
          <SelectTrigger className="w-72">
            <SelectValue placeholder="Choose a session" />
          </SelectTrigger>
          <SelectContent>
            {sessions.map((s) => (
              <SelectItem key={s.session_id} value={s.session_id}>
                {s.domain}/{s.name} ({s.session_id.slice(0, 8)})
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <OpenSessionSheet trigger={<Button variant="outline">Open a new session</Button>} />
      </div>

      {!sessionId ? (
        <EmptyState title="Pick a session to begin" description="Or open a new one above." />
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="flex flex-col gap-4">
            <PlaygroundActionBuilder actions={actions} onChange={setActions} onPickRefForIndex={openRefPicker} />
            <Button onClick={runSequence} disabled={execute.isPending} className="w-fit">
              <Play className="size-4" />
              {execute.isPending ? 'Running…' : 'Run sequence'}
            </Button>
            <CodeSnippetPanel sessionId={sessionId || null} apiKey={apiKey} actions={actions} />
          </div>
          <div className="flex flex-col gap-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Result</p>
            {execute.data ? (
              <PlaygroundResultPanel result={execute.data} />
            ) : (
              <EmptyState title="No result yet" description="Run the sequence to see output here." />
            )}
          </div>
        </div>
      )}

      <Sheet open={pickerOpen} onOpenChange={setPickerOpen}>
        <SheetContent side="right">
          <SheetHeader>
            <SheetTitle>Pick an element</SheetTitle>
          </SheetHeader>
          <div className="flex-1 overflow-auto">
            {pickerSnapshot ? (
              <SnapshotTree node={pickerSnapshot.root} onPickRef={pickRef} />
            ) : (
              <p className="text-sm text-muted-foreground">Taking a snapshot…</p>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
