// `localStorage`, not an httpOnly cookie: a cookie would make the credential
// invisible to the Playground's copy-pasteable curl/Python snippets (the
// whole point of that screen). Not `sessionStorage` either (the original
// choice here) -- that bounded exposure to one tab's lifetime, but meant
// every new tab required signing in again, since sessionStorage is per-tab
// even for the same origin. `localStorage` is shared across tabs and
// survives restarts, so a stolen token (XSS) stays valid longer/more widely
// than sessionStorage would have -- an accepted trade for the UX win here,
// since this is v1's single shared operator credential, not per-user auth
// with a real threat model that trade would undermine. `AuthContext.tsx`
// listens for the browser's `storage` event to pick up login/logout that
// happened in another tab without needing a reload.

const API_KEY_STORAGE_KEY = 'agentpilot.apiKey'
const TENANT_STORAGE_KEY = 'agentpilot.tenant'
const ADMIN_TOKEN_STORAGE_KEY = 'agentpilot.adminToken'

export function getStoredApiKey(): string | null {
  return localStorage.getItem(API_KEY_STORAGE_KEY)
}

export function setStoredApiKey(value: string | null): void {
  if (value) localStorage.setItem(API_KEY_STORAGE_KEY, value)
  else localStorage.removeItem(API_KEY_STORAGE_KEY)
}

// The backend resolves tenant from the api key server-side; the client
// still needs to know it too (`SessionOpenRequest.tenant` must match, or the
// gateway 403s -- see `auth_deps.py`'s tenant-mismatch check), so it's
// captured once at login time alongside the key rather than guessed.
export function getStoredTenant(): string | null {
  return localStorage.getItem(TENANT_STORAGE_KEY)
}

export function setStoredTenant(value: string | null): void {
  if (value) localStorage.setItem(TENANT_STORAGE_KEY, value)
  else localStorage.removeItem(TENANT_STORAGE_KEY)
}

export function getStoredAdminToken(): string | null {
  return localStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)
}

export function setStoredAdminToken(value: string | null): void {
  if (value) localStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, value)
  else localStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY)
}

export const TOKEN_STORAGE_KEYS = [
  API_KEY_STORAGE_KEY,
  TENANT_STORAGE_KEY,
  ADMIN_TOKEN_STORAGE_KEY,
] as const
