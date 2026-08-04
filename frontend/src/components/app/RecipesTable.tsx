import { useNavigate } from 'react-router-dom'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { RecipeHealthStatus, RecipeOut } from '@/lib/api/types'

function healthVariant(status: RecipeHealthStatus): NonNullable<BadgeProps['variant']> {
  switch (status) {
    case 'healthy':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'broken':
      return 'destructive'
  }
}

function relativeTime(iso: string | null): string {
  if (!iso) return '--'
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function RecipesTable({ recipes }: { recipes: RecipeOut[] }) {
  const navigate = useNavigate()
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>URL pattern</TableHead>
          <TableHead>Health</TableHead>
          <TableHead>Schedule</TableHead>
          <TableHead>Last run</TableHead>
          <TableHead>Updated</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {recipes.map((r) => (
          <TableRow key={r.recipe_id} className="cursor-pointer" onClick={() => navigate(`/recipes/${r.recipe_id}`)}>
            <TableCell className="font-medium">{r.name}</TableCell>
            <TableCell className="max-w-xs truncate font-mono text-xs text-muted-foreground">{r.url_pattern}</TableCell>
            <TableCell>
              <Badge variant={healthVariant(r.health_status)} className="capitalize">
                {r.health_status}
              </Badge>
            </TableCell>
            <TableCell className="text-muted-foreground">
              {r.schedule_interval_seconds ? `every ${r.schedule_interval_seconds}s` : 'On-demand'}
            </TableCell>
            <TableCell className="text-muted-foreground">{relativeTime(r.last_run_at)}</TableCell>
            <TableCell className="text-muted-foreground">{relativeTime(r.updated_at)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
