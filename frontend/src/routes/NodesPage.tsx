import { useState } from 'react'
import { Server } from 'lucide-react'
import { useAuth } from '@/lib/auth/AuthContext'
import { useNodesList } from '@/hooks/useNodes'
import { NodesTable } from '@/components/app/NodesTable'
import { EmptyState } from '@/components/app/EmptyState'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'

export function NodesPage() {
  const { isAdmin, setAdminToken } = useAuth()
  const [tokenInput, setTokenInput] = useState('')
  const { data, isLoading, isError } = useNodesList()
  const nodes = data?.nodes ?? []

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Nodes</h1>
        <p className="text-sm text-muted-foreground">
          Worker fleet capacity and liveness, as seen by the gateway's placer — requires the platform admin token.
        </p>
      </div>

      {!isAdmin ? (
        <EmptyState
          icon={<Server className="size-8" />}
          title="Admin token required"
          description="Enter the platform's AGENTPILOT_ADMIN_TOKEN to view the fleet."
          action={
            <form
              className="flex items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                if (tokenInput.trim()) setAdminToken(tokenInput.trim())
              }}
            >
              <div className="flex flex-col gap-1.5 text-left">
                <Label htmlFor="admin-token-inline-nodes">Admin token</Label>
                <Input
                  id="admin-token-inline-nodes"
                  type="password"
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  className="w-64"
                />
              </div>
              <button type="submit" className="hidden" />
            </form>
          }
        />
      ) : isLoading ? (
        <div className="flex flex-col gap-2">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12" />
          ))}
        </div>
      ) : isError ? (
        <EmptyState title="Couldn't load nodes" description="Check that the gateway is reachable and your admin token is valid." />
      ) : nodes.length === 0 ? (
        <EmptyState
          icon={<Server className="size-8" />}
          title="No nodes registered"
          description="No worker has self-registered into the fleet yet."
        />
      ) : (
        <div className="rounded-lg border border-border">
          <NodesTable nodes={nodes} />
        </div>
      )}
    </div>
  )
}
