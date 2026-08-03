import { useCallback, useState } from 'react'
import { useAuth } from '@/lib/auth/AuthContext'
import {
  appendRecentRun,
  loadRecentRuns,
  updateRecentRun,
  updateRecentRunByJobId,
  type RecentRunEndpoint,
  type RecentRunEntry,
} from '@/lib/recentRuns'

export function useRecentRuns(endpoint: RecentRunEndpoint) {
  const { tenant } = useAuth()
  const [all, setAll] = useState<RecentRunEntry[]>(() => (tenant ? loadRecentRuns(tenant) : []))

  const append = useCallback(
    (entry: Omit<RecentRunEntry, 'id' | 'startedAt'>) => {
      if (!tenant) return
      const full: RecentRunEntry = { ...entry, id: crypto.randomUUID(), startedAt: new Date().toISOString() }
      setAll(appendRecentRun(tenant, full))
    },
    [tenant],
  )

  const update = useCallback(
    (id: string, patch: Partial<RecentRunEntry>) => {
      if (!tenant) return
      setAll(updateRecentRun(tenant, id, patch))
    },
    [tenant],
  )

  const updateByJobId = useCallback(
    (jobId: string, patch: Partial<RecentRunEntry>) => {
      if (!tenant) return
      setAll(updateRecentRunByJobId(tenant, jobId, patch))
    },
    [tenant],
  )

  return { runs: all.filter((r) => r.endpoint === endpoint), append, update, updateByJobId }
}
