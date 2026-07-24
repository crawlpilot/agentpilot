import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { useSessionsList } from '@/hooks/useSessionsList'
import { useReleaseSession } from '@/hooks/useSessionMutations'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { SessionStateBadge } from '@/components/app/SessionStateBadge'
import { EmptyState } from '@/components/app/EmptyState'
import { useToast } from '@/components/ui/toast'

export function SessionDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const { toast } = useToast()
  const { data } = useSessionsList()
  const session = data?.sessions.find((s) => s.session_id === sessionId)
  const releaseSession = useReleaseSession()

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

      <Card className="max-w-md">
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
    </div>
  )
}
