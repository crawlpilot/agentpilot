import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { MoreHorizontal, Radio } from 'lucide-react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { SessionStateBadge } from '@/components/app/SessionStateBadge'
import { LiveViewModal } from '@/components/app/LiveViewModal'
import { useReleaseSession } from '@/hooks/useSessionMutations'
import { useToast } from '@/components/ui/toast'
import type { SessionOut } from '@/lib/api/types'

function LeaseCountdown({ expiresAt }: { expiresAt: number | null }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])
  if (expiresAt === null) return <span className="text-muted-foreground">--</span>
  const remainingSeconds = Math.max(0, Math.round(expiresAt - now / 1000))
  return <span className="tabular-nums">{remainingSeconds}s</span>
}

export function SessionsTable({ sessions }: { sessions: SessionOut[] }) {
  const navigate = useNavigate()
  const releaseSession = useReleaseSession()
  const { toast } = useToast()
  const [liveViewSessionId, setLiveViewSessionId] = useState<string | null>(null)

  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Identity</TableHead>
            <TableHead>State</TableHead>
            <TableHead>Tier</TableHead>
            <TableHead>Node</TableHead>
            <TableHead>Memory</TableHead>
            <TableHead>Lease expires</TableHead>
            <TableHead />
            <TableHead />
          </TableRow>
        </TableHeader>
        <TableBody>
          {sessions.map((s) => (
            <TableRow key={s.session_id} className="cursor-pointer" onClick={() => navigate(`/sessions/${s.session_id}`)}>
              <TableCell className="font-mono text-xs">
                {s.tenant}/{s.domain}/{s.name}
              </TableCell>
              <TableCell>
                <SessionStateBadge state={s.state} />
              </TableCell>
              <TableCell>
                <Badge variant="outline">{s.tier}</Badge>
              </TableCell>
              <TableCell className="text-muted-foreground">{s.node_id}</TableCell>
              <TableCell className="text-right text-muted-foreground">
                {s.rss_mb !== null ? `${s.rss_mb.toFixed(0)} MB` : '--'}
              </TableCell>
              <TableCell>
                <LeaseCountdown expiresAt={s.lease_expires_at} />
              </TableCell>
              <TableCell onClick={(e) => e.stopPropagation()}>
                <Button
                  size="icon"
                  variant="ghost"
                  disabled={s.state !== 'active'}
                  onClick={() => setLiveViewSessionId(s.session_id)}
                  title="Watch live"
                >
                  <Radio className="size-4" />
                </Button>
              </TableCell>
              <TableCell onClick={(e) => e.stopPropagation()}>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button size="icon" variant="ghost">
                      <MoreHorizontal className="size-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem onClick={() => navigate(`/playground?session=${s.session_id}`)}>
                      Open in Playground
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      variant="destructive"
                      disabled={s.state !== 'active' || releaseSession.isPending}
                      onClick={() =>
                        releaseSession.mutate(s.session_id, {
                          onSuccess: () => toast({ title: 'Session released' }),
                          onError: (err) =>
                            toast({ title: 'Release failed', description: err.message, variant: 'destructive' }),
                        })
                      }
                    >
                      Release
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {liveViewSessionId && (
        <LiveViewModal
          sessionId={liveViewSessionId}
          open={liveViewSessionId !== null}
          onOpenChange={(open) => !open && setLiveViewSessionId(null)}
        />
      )}
    </>
  )
}
