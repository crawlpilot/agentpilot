// Client-side run history: neither `/v1/scrape` nor `/v1/map` persist a row
// server-side (both are synchronous, stateless), and `/v1/crawl` only
// supports get/cancel by id, not list-all -- so "Recent Runs" has no backend
// to read from and lives entirely in localStorage instead. Per-tenant key
// (multiple tenants could share a browser), matching this app's existing
// `agentpilot.*` localStorage-key namespacing (see `lib/auth/tokenStorage.ts`).

import type { CrawlJobStatus, ScrapeFormat } from './api/types'

export type RecentRunEndpoint = 'scrape' | 'map' | 'crawl'
export type RecentRunStatus = 'success' | 'error' | CrawlJobStatus

export interface RecentRunEntry {
  id: string
  endpoint: RecentRunEndpoint
  url: string
  formats?: ScrapeFormat[]
  status: RecentRunStatus
  startedAt: string // ISO
  jobId?: string // crawl only -- lets a card deep-link back to resume polling
}

const MAX_ENTRIES = 25

function storageKey(tenant: string): string {
  return `agentpilot.recentRuns.${tenant}`
}

export function loadRecentRuns(tenant: string): RecentRunEntry[] {
  try {
    const raw = localStorage.getItem(storageKey(tenant))
    return raw ? (JSON.parse(raw) as RecentRunEntry[]) : []
  } catch {
    // Corrupt/foreign JSON under this key -- treat as empty rather than
    // throwing and breaking the whole Playground page.
    return []
  }
}

export function appendRecentRun(tenant: string, entry: RecentRunEntry): RecentRunEntry[] {
  const next = [entry, ...loadRecentRuns(tenant)].slice(0, MAX_ENTRIES)
  localStorage.setItem(storageKey(tenant), JSON.stringify(next))
  return next
}

export function updateRecentRun(tenant: string, id: string, patch: Partial<RecentRunEntry>): RecentRunEntry[] {
  const next = loadRecentRuns(tenant).map((r) => (r.id === id ? { ...r, ...patch } : r))
  localStorage.setItem(storageKey(tenant), JSON.stringify(next))
  return next
}

// Keyed by `jobId`, not `id`: a crawl resumed via a Recent-Runs card click
// (or a bookmarked `?id=` URL) has no locally-created entry `id` to update
// from this page load -- `jobId` is the one identifier that survives a
// remount. A no-op (returns the list unchanged) if nothing matches.
export function updateRecentRunByJobId(
  tenant: string,
  jobId: string,
  patch: Partial<RecentRunEntry>,
): RecentRunEntry[] {
  const next = loadRecentRuns(tenant).map((r) => (r.jobId === jobId ? { ...r, ...patch } : r))
  localStorage.setItem(storageKey(tenant), JSON.stringify(next))
  return next
}
