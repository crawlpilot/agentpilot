import type { ApiErrorBody, ErrorCode } from './types'

export class ApiError extends Error {
  code: ErrorCode
  status: number
  retryAfter: number | null
  details: unknown

  constructor(message: string, code: ErrorCode, status: number, retryAfter: number | null, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.retryAfter = retryAfter
    this.details = details
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE' | 'PUT'
  body?: unknown
  token?: string | null
  query?: Record<string, string | undefined>
}

function buildUrl(path: string, query?: Record<string, string | undefined>): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, value)
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const res = await fetch(buildUrl(path, opts.query), {
    method: opts.method ?? 'GET',
    headers: {
      'Content-Type': 'application/json',
      ...(opts.token ? { Authorization: `Bearer ${opts.token}` } : {}),
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })

  if (!res.ok) {
    let body: ApiErrorBody | null = null
    try {
      body = (await res.json()) as ApiErrorBody
    } catch {
      // Non-JSON error body (e.g. a raw 401 from a proxy) -- fall through to
      // the generic message below rather than throwing on the parse itself.
    }
    const retryAfterHeader = res.headers.get('Retry-After')
    throw new ApiError(
      body?.error ?? res.statusText ?? 'request failed',
      body?.code ?? 'INTERNAL_ERROR',
      res.status,
      retryAfterHeader ? Number(retryAfterHeader) : null,
      body?.details,
    )
  }

  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}
