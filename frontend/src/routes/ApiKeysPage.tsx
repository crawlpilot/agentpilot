import { useState } from 'react'
import { KeyRound } from 'lucide-react'
import { useAuth } from '@/lib/auth/AuthContext'
import { useApiKeysList } from '@/hooks/useApiKeys'
import { ApiKeyCreateDialog } from '@/components/app/ApiKeyCreateDialog'
import { ApiKeyRevealOnce } from '@/components/app/ApiKeyRevealOnce'
import { ApiKeyTable } from '@/components/app/ApiKeyTable'
import { EmptyState } from '@/components/app/EmptyState'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import type { ApiKeyCreateOut } from '@/lib/api/types'

export function ApiKeysPage() {
  const { isAdmin, setAdminToken } = useAuth()
  const [tokenInput, setTokenInput] = useState('')
  const [revealedKey, setRevealedKey] = useState<ApiKeyCreateOut | null>(null)
  const { tenant } = useAuth()
  const { data, isLoading } = useApiKeysList(tenant ?? '')

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">API Keys</h1>
        <p className="text-sm text-muted-foreground">
          Manage API keys — requires the platform admin token, not a per-tenant login.
        </p>
      </div>

      {!isAdmin ? (
        <EmptyState
          icon={<KeyRound className="size-8" />}
          title="Admin token required"
          description="Enter the platform's BAAS_ADMIN_TOKEN to manage API keys."
          action={
            <form
              className="flex items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                if (tokenInput.trim()) setAdminToken(tokenInput.trim())
              }}
            >
              <div className="flex flex-col gap-1.5 text-left">
                <Label htmlFor="admin-token-inline">Admin token</Label>
                <Input
                  id="admin-token-inline"
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
      ) : (
        <>
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">Tenant: {tenant}</span>
            <ApiKeyCreateDialog tenant={tenant ?? ''} onCreated={setRevealedKey} />
          </div>

          {isLoading ? (
            <Skeleton className="h-32" />
          ) : !data || data.api_keys.length === 0 ? (
            <EmptyState title="No API keys yet" description="Create one to start calling the API." />
          ) : (
            <div className="rounded-lg border border-border">
              <ApiKeyTable tenant={tenant ?? ''} keys={data.api_keys} />
            </div>
          )}
        </>
      )}

      <ApiKeyRevealOnce apiKey={revealedKey} onClose={() => setRevealedKey(null)} />
    </div>
  )
}
