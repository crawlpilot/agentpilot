import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Boxes, Gauge, ShieldCheck } from 'lucide-react'
import { useAuth } from '@/lib/auth/AuthContext'
import { listSessions } from '@/lib/api/sessions'
import { listNodes } from '@/lib/api/nodes'
import { ApiError } from '@/lib/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Logo } from '@/components/app/Logo'

const BRAND_POINTS = [
  { icon: Boxes, title: 'Warm session pool', body: 'Reusable browser contexts over a stable HTTP/WebSocket API.' },
  { icon: ShieldCheck, title: 'Anti-detection built in', body: 'Patched Playwright avoids common CDP bot-detection signals.' },
  { icon: Gauge, title: 'Scrape, crawl, and drive', body: 'One console for extraction, mapping, and page automation.' },
]

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
    // Split screen: a brand/value panel on the left (hidden on small
    // screens, where it's pure decoration), the sign-in form on the right --
    // the standard enterprise SaaS login shape (Firecrawl, Vercel, Linear).
    <div className="grid min-h-svh lg:grid-cols-2">
      <aside className="relative hidden flex-col justify-between overflow-hidden bg-accent p-10 text-white lg:flex">
        {/* Soft radial highlights so the flat red panel reads less like a
            solid block and more like a considered surface. */}
        <div
          className="pointer-events-none absolute inset-0 opacity-40"
          style={{
            background:
              'radial-gradient(circle at 20% 20%, rgba(255,255,255,0.25), transparent 45%), radial-gradient(circle at 80% 80%, rgba(0,0,0,0.25), transparent 40%)',
          }}
        />
        <div className="relative">
          <Logo inverted />
        </div>
        <div className="relative flex flex-col gap-6">
          <div>
            <h1 className="text-2xl font-semibold leading-tight">Browser-as-a-Service, ready to drive.</h1>
            <p className="mt-2 max-w-sm text-sm text-white/70">
              Open a warm browser session once, then scrape, crawl, and automate it over a stable API.
            </p>
          </div>
          <ul className="flex flex-col gap-4">
            {BRAND_POINTS.map(({ icon: Icon, title, body }) => (
              <li key={title} className="flex gap-3">
                <span className="grid size-8 shrink-0 place-items-center rounded-md bg-white/15">
                  <Icon className="size-4" />
                </span>
                <div>
                  <p className="text-sm font-medium">{title}</p>
                  <p className="text-xs text-white/60">{body}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <p className="relative text-xs text-white/50">Multi-tenant · warm session pool · anti-detection</p>
      </aside>

      <div className="flex items-center justify-center bg-background p-6">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            <Logo />
          </div>
          <div className="mb-6">
            <h2 className="text-xl font-semibold">Sign in</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Paste a tenant API key (from an operator, via <code>/v1/api-keys</code>) and the tenant it
              belongs to -- or just the admin token below if you only need API Keys/Nodes.
            </p>
          </div>
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
        </div>
      </div>
    </div>
  )
}
