import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useRevokeApiKey } from '@/hooks/useApiKeys'
import { useToast } from '@/components/ui/toast'
import type { ApiKeyOut } from '@/lib/api/types'

function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.round(diffMs / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function ApiKeyTable({ tenant, keys }: { tenant: string; keys: ApiKeyOut[] }) {
  const revoke = useRevokeApiKey(tenant)
  const { toast } = useToast()

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Prefix</TableHead>
          <TableHead>Name</TableHead>
          <TableHead>Created</TableHead>
          <TableHead>Last used</TableHead>
          <TableHead>Status</TableHead>
          <TableHead />
        </TableRow>
      </TableHeader>
      <TableBody>
        {keys.map((k) => (
          <TableRow key={k.key_id}>
            <TableCell className="font-mono text-xs">{k.prefix}…</TableCell>
            <TableCell>{k.name}</TableCell>
            <TableCell className="text-muted-foreground">{relativeTime(k.created_at)}</TableCell>
            <TableCell className="text-muted-foreground">{relativeTime(k.last_used_at)}</TableCell>
            <TableCell>
              {k.revoked_at ? <Badge variant="destructive">revoked</Badge> : <Badge variant="success">active</Badge>}
            </TableCell>
            <TableCell>
              <Button
                size="sm"
                variant="ghost"
                disabled={Boolean(k.revoked_at) || revoke.isPending}
                onClick={() => {
                  if (!confirm(`Revoke "${k.name}"? This immediately invalidates the key; any running integration using it will start failing.`)) return
                  revoke.mutate(k.key_id, { onSuccess: () => toast({ title: 'Key revoked' }) })
                }}
              >
                Revoke
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
