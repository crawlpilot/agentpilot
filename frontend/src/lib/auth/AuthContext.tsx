import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  getStoredAdminToken,
  getStoredApiKey,
  getStoredTenant,
  setStoredAdminToken,
  setStoredApiKey,
  setStoredTenant,
  TOKEN_STORAGE_KEYS,
} from './tokenStorage'

interface AuthContextValue {
  apiKey: string | null
  tenant: string | null
  adminToken: string | null
  isAuthed: boolean
  isAdmin: boolean
  login: (apiKey: string, tenant: string) => void
  logout: () => void
  setAdminToken: (token: string | null) => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [apiKey, setApiKeyState] = useState<string | null>(() => getStoredApiKey())
  const [tenant, setTenantState] = useState<string | null>(() => getStoredTenant())
  const [adminToken, setAdminTokenState] = useState<string | null>(() => getStoredAdminToken())

  // `localStorage` is shared across tabs, but a `storage` event only fires
  // in *other* tabs when it changes -- this is what makes a login/logout in
  // one tab take effect live in every other already-open tab, not just ones
  // opened afterward (which would pick it up from `useState`'s initializer
  // above regardless).
  useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key !== null && !TOKEN_STORAGE_KEYS.includes(e.key as (typeof TOKEN_STORAGE_KEYS)[number])) {
        return
      }
      setApiKeyState(getStoredApiKey())
      setTenantState(getStoredTenant())
      setAdminTokenState(getStoredAdminToken())
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      apiKey,
      tenant,
      adminToken,
      isAuthed: apiKey !== null && tenant !== null,
      isAdmin: adminToken !== null,
      login: (key: string, t: string) => {
        setStoredApiKey(key)
        setStoredTenant(t)
        setApiKeyState(key)
        setTenantState(t)
      },
      logout: () => {
        setStoredApiKey(null)
        setStoredTenant(null)
        setStoredAdminToken(null)
        setApiKeyState(null)
        setTenantState(null)
        setAdminTokenState(null)
      },
      setAdminToken: (token: string | null) => {
        setStoredAdminToken(token)
        setAdminTokenState(token)
      },
    }),
    [apiKey, tenant, adminToken],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
