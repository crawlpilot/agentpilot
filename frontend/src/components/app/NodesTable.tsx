import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { NodeStatusBadge } from '@/components/app/NodeStatusBadge'
import type { NodeOut } from '@/lib/api/types'

function formatUptime(startedAt: number | null): string {
  if (startedAt === null) return '--'
  const seconds = Math.max(0, Date.now() / 1000 - startedAt)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

function formatPct(pct: number | null): string {
  return pct !== null ? `${pct.toFixed(0)}%` : '--'
}

export function NodesTable({ nodes }: { nodes: NodeOut[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Node</TableHead>
          <TableHead>Address</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Contexts</TableHead>
          <TableHead>Memory</TableHead>
          <TableHead>CPU</TableHead>
          <TableHead>Uptime</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {nodes.map((n) => (
          <TableRow key={n.node_id}>
            <TableCell className="font-mono text-xs">{n.node_id}</TableCell>
            <TableCell className="text-muted-foreground">{n.addr ?? '--'}</TableCell>
            <TableCell>
              <NodeStatusBadge live={n.live} />
            </TableCell>
            <TableCell className="tabular-nums">
              {n.active !== null && n.idle !== null && n.max_contexts !== null
                ? `${n.active} active / ${n.idle} idle / ${n.max_contexts} max`
                : '--'}
            </TableCell>
            <TableCell className="text-right tabular-nums text-muted-foreground">
              {formatPct(n.mem_used_pct)}
            </TableCell>
            <TableCell className="text-right tabular-nums text-muted-foreground">
              {formatPct(n.cpu_used_pct)}
            </TableCell>
            <TableCell className="text-muted-foreground">{formatUptime(n.started_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
