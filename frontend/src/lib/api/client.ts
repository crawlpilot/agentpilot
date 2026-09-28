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

/**
 * The validation reasons behind a 422, or an empty list when the error is not one.
 *
 * The server puts them in `details.errors` -- `validate_document`'s output, one
 * string per structural problem, naming the field group and the reason. They are
 * also summarised into `message`, but truncated there: a document with six
 * problems shows three and "(and 3 more)". An authoring surface wants all of
 * them at once, because fixing one at a time through a save round trip each is a
 * miserable way to author anything.
 */
export function validationErrors(err: unknown): string[] {
  if (!(err instanceof ApiError)) return []
  const details = err.details
  if (!details || typeof details !== 'object') return []
  const errors = (details as { errors?: unknown }).errors
  if (!Array.isArray(errors)) return []
  return errors.filter((e): e is string => typeof e === 'string')
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
