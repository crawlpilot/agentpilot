import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth/AuthContext'
import { listSessions } from '@/lib/api/sessions'
import { listNodes } from '@/lib/api/nodes'
import { ApiError } from '@/lib/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

export function LoginPage() {
  const { isAuthed, isAdmin, login, setAdminToken } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [apiKey, setApiKey] = useState('')
  const [tenant, setTenant] = useState('')
  const [adminToken, setAdminTokenInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (isAuthed || isAdmin) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)

    const trimmedKey = apiKey.trim()
    const trimmedTenant = tenant.trim()
    const trimmedAdminToken = adminToken.trim()

    if ((trimmedKey || trimmedTenant) && !(trimmedKey && trimmedTenant)) {
      setError('Both tenant and API key are required together.')
      return
    }
    if (!trimmedKey && !trimmedAdminToken) {
      setError('Enter a tenant API key, an admin token, or both.')
      return
    }

    setSubmitting(true)
    try {
      // Neither field persists to storage until the backend has actually
      // accepted it -- without this, any non-empty string "logs in" the UI
      // shell (isAuthed/isAdmin are pure presence checks), leaving a garbage
      // credential silently 401ing on every subsequent call instead of
      // failing at the door.
      if (trimmedKey) {
        await listSessions(trimmedKey)
      }
      if (trimmedAdminToken) {
        await listNodes(trimmedAdminToken)
      }
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setError('Invalid API key or admin token.')
      } else {
        setError('Could not reach the gateway to verify credentials.')
      }
      setSubmitting(false)
      return
    }

    if (trimmedKey) login(trimmedKey, trimmedTenant)
    if (trimmedAdminToken) setAdminToken(trimmedAdminToken)

    const from = (location.state as { from?: string } | null)?.from
    navigate(from ?? (trimmedKey ? '/' : '/nodes'), { replace: true })
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-muted p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in</CardTitle>
          <CardDescription>
            Paste a tenant API key (from an operator, via <code>/v1/api-keys</code>) and the tenant
            it belongs to -- or just the admin token below if you only need API Keys/Nodes.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="tenant">Tenant</Label>
              <Input
                id="tenant"
                placeholder="acme"
                value={tenant}
                onChange={(e) => setTenant(e.target.value)}
                autoFocus
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="api-key">API key</Label>
              <Input
                id="api-key"
                type="password"
                placeholder="bk_live_..."
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
            </div>
            <details className="text-sm text-muted-foreground">
              <summary className="cursor-pointer select-none">
                Platform admin token (optional, for managing API keys)
              </summary>
              <div className="mt-2 flex flex-col gap-1.5">
                <Label htmlFor="admin-token">Admin token</Label>
                <Input
                  id="admin-token"
                  type="password"
                  placeholder="AGENTPILOT_ADMIN_TOKEN"
                  value={adminToken}
                  onChange={(e) => setAdminTokenInput(e.target.value)}
                />
              </div>
            </details>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="mt-2" disabled={submitting}>
              {submitting ? 'Verifying...' : 'Continue'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
