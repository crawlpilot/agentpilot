// `sessionStorage`, not `localStorage` or an httpOnly cookie: a cookie would
// make the credential invisible to the Playground's copy-pasteable
// curl/Python snippets (the whole point of that screen), and sessionStorage
// bounds exposure to the tab's lifetime -- an acceptable tradeoff for what
// is, in v1, a single shared operator credential rather than a per-user
// session (see the enterprise-UI plan's "Frontend architecture" section).

const API_KEY_STORAGE_KEY = 'baas.apiKey'
const TENANT_STORAGE_KEY = 'baas.tenant'
const ADMIN_TOKEN_STORAGE_KEY = 'baas.adminToken'

export function getStoredApiKey(): string | null {
  return sessionStorage.getItem(API_KEY_STORAGE_KEY)
}

export function setStoredApiKey(value: string | null): void {
  if (value) sessionStorage.setItem(API_KEY_STORAGE_KEY, value)
  else sessionStorage.removeItem(API_KEY_STORAGE_KEY)
}

// The backend resolves tenant from the api key server-side; the client
// still needs to know it too (`SessionOpenRequest.tenant` must match, or the
// gateway 403s -- see `auth_deps.py`'s tenant-mismatch check), so it's
// captured once at login time alongside the key rather than guessed.
export function getStoredTenant(): string | null {
  return sessionStorage.getItem(TENANT_STORAGE_KEY)
}

export function setStoredTenant(value: string | null): void {
  if (value) sessionStorage.setItem(TENANT_STORAGE_KEY, value)
  else sessionStorage.removeItem(TENANT_STORAGE_KEY)
}

export function getStoredAdminToken(): string | null {
  return sessionStorage.getItem(ADMIN_TOKEN_STORAGE_KEY)
}

export function setStoredAdminToken(value: string | null): void {
  if (value) sessionStorage.setItem(ADMIN_TOKEN_STORAGE_KEY, value)
  else sessionStorage.removeItem(ADMIN_TOKEN_STORAGE_KEY)
}
