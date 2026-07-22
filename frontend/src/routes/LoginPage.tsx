import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth/AuthContext'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

export function LoginPage() {
  const { isAuthed, login, setAdminToken } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [apiKey, setApiKey] = useState('')
  const [tenant, setTenant] = useState('')
  const [adminToken, setAdminTokenInput] = useState('')

  if (isAuthed) {
    const from = (location.state as { from?: string } | null)?.from ?? '/'
    return <Navigate to={from} replace />
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!apiKey.trim() || !tenant.trim()) return
    login(apiKey.trim(), tenant.trim())
    if (adminToken.trim()) setAdminToken(adminToken.trim())
    navigate('/', { replace: true })
  }

  return (
    <div className="flex min-h-svh items-center justify-center bg-muted p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Sign in</CardTitle>
          <CardDescription>
            Paste a tenant API key (from an operator, via <code>/v1/api-keys</code>) and the tenant
            it belongs to.
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
            <Button type="submit" className="mt-2">
              Continue
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
