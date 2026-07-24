import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Copy, ExternalLink, MoreHorizontal } from 'lucide-react'
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
import { useReleaseSession } from '@/hooks/useSessionMutations'
import { useToast } from '@/components/ui/toast'
import { useAuth } from '@/lib/auth/AuthContext'
import { API_BASE_URL } from '@/lib/config'
import type { SessionOut } from '@/lib/api/types'

// Mirrors `_rewrite_ws_url` in `agentpilot/gateway/routes/cdp.py`: the CDP
// relay is fully addressable client-side from session_id + host + api_key,
// with no need to round-trip through `/cdp/json/version` just to display it.
function buildCdpUrl(sessionId: string, apiKey: string): string {
  const proto = API_BASE_URL.startsWith('https://') ? 'wss' : 'ws'
  const host = API_BASE_URL.replace(/^https?:\/\//, '')
  return `${proto}://${host}/v1/sessions/${sessionId}/cdp?api_key=${encodeURIComponent(apiKey)}`
}

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
  const { apiKey } = useAuth()

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Identity</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Tier</TableHead>
          <TableHead>Node</TableHead>
          <TableHead>Memory</TableHead>
          <TableHead>Lease expires</TableHead>
          <TableHead>Live</TableHead>
          <TableHead>CDP</TableHead>
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
              {s.state === 'active' ? (
                <a
                  href={`/sessions/${s.session_id}/live`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-sm text-accent hover:underline"
                >
                  Live
                  <ExternalLink className="size-3.5" />
                </a>
              ) : (
                <span className="text-sm text-muted-foreground">--</span>
              )}
            </TableCell>
            <TableCell onClick={(e) => e.stopPropagation()}>
              {s.enable_cdp && s.state === 'active' && apiKey ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className="gap-1 text-sm"
                  onClick={async () => {
                    await navigator.clipboard.writeText(buildCdpUrl(s.session_id, apiKey))
                    toast({ title: 'CDP URL copied' })
                  }}
                >
                  <Copy className="size-3.5" />
                  Copy URL
                </Button>
              ) : (
                <span className="text-sm text-muted-foreground">--</span>
              )}
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
  )
}
