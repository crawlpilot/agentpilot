import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Radio } from 'lucide-react'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useExecuteSession } from '@/hooks/useExecuteSession'
import { useReleaseSession } from '@/hooks/useSessionMutations'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { SessionStateBadge } from '@/components/app/SessionStateBadge'
import { LiveViewModal } from '@/components/app/LiveViewModal'
import { EmptyState } from '@/components/app/EmptyState'
import { useToast } from '@/components/ui/toast'

export function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const { toast } = useToast()
  const { data } = useSessionsList()
  const session = data?.sessions.find((s) => s.session_id === sessionId)
  const executeSession = useExecuteSession()
  const releaseSession = useReleaseSession()
  const [liveViewOpen, setLiveViewOpen] = useState(false)
  const [thumbnail, setThumbnail] = useState<string | null>(null)

  useEffect(() => {
    if (!sessionId || !session || session.state !== 'active') return
    const poll = () =>
      executeSession.mutate(
        { sessionId, actions: [{ type: 'screenshot' }] },
        {
          onSuccess: (result) => {
            if (result.screenshots[0]) setThumbnail(result.screenshots[0])
          },
        },
      )
    poll()
    const id = setInterval(poll, 5000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, session?.state])

  if (!sessionId) return null
  if (!session) {
    return (
      <EmptyState
        title="Session not found"
        description="It may have already been released."
        action={
          <Button variant="outline" onClick={() => navigate('/sessions')}>
            Back to sessions
          </Button>
        }
      />
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <Button size="icon" variant="ghost" onClick={() => navigate('/sessions')}>
          <ArrowLeft className="size-4" />
        </Button>
        <h1 className="font-mono text-lg font-semibold">
          {session.tenant}/{session.domain}/{session.name}
        </h1>
        <SessionStateBadge state={session.state} />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <CardHeader>
            <CardTitle>Details</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <div className="flex justify-between">
              <span className="text-muted-foreground">Tier</span>
              <Badge variant="outline">{session.tier}</Badge>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Headful</span>
              <span>{session.headful ? 'yes' : 'no'}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Node</span>
              <span>{session.node_id}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted-foreground">Memory</span>
              <span>{session.rss_mb !== null ? `${session.rss_mb.toFixed(0)} MB` : '--'}</span>
            </div>
            <div className="flex flex-col gap-2 pt-2">
              <Button disabled={session.state !== 'active'} onClick={() => setLiveViewOpen(true)}>
                <Radio className="size-4" />
                Watch live
              </Button>
              <Button
                variant="destructive"
                disabled={session.state !== 'active' || releaseSession.isPending}
                onClick={() =>
                  releaseSession.mutate(session.session_id, {
                    onSuccess: () => {
                      toast({ title: 'Session released' })
                      navigate('/sessions')
                    },
                  })
                }
              >
                Release session
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Live thumbnail</CardTitle>
          </CardHeader>
          <CardContent>
            {thumbnail ? (
              <img src={`data:image/png;base64,${thumbnail}`} alt="session thumbnail" className="w-full rounded-md border border-border" />
            ) : (
              <div className="flex h-48 items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
                {session.state === 'active' ? 'Loading preview…' : 'Session is not active'}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {liveViewOpen && (
        <LiveViewModal sessionId={session.session_id} open={liveViewOpen} onOpenChange={setLiveViewOpen} />
      )}
    </div>
  )
}
