import type { CompareResult, EndpointStats, ExplainResult, IssueGroup, Meta, Page, Speedscope, Trace, TraceFilters } from './types'

export class ApiError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

type Mock = (method: string, url: string, body: unknown) => Promise<{ status: number; json: unknown }>
let mock: Mock | null = null
let mockReady: Promise<void> | null = null

export function isMocked() { return mock !== null }

// Dev only: `?mock=1`, or fall back to the mock when no backend answers.
// `import.meta.env.DEV` is a build-time constant, so this whole branch and the
// dynamic import of ./mocks are removed from production bundles.
async function initMock(force: boolean) {
  if (!import.meta.env.DEV) return
  const m = await import('./mocks')
  mock = m.mockFetch
  void force
}
function wantsMock() {
  if (!import.meta.env.DEV) return false
  try {
    if (new URLSearchParams(location.search).get('mock') === '1') { sessionStorage.setItem('redline-mock', '1'); return true }
    return sessionStorage.getItem('redline-mock') === '1'
  } catch { return false }
}

// CSRF for mutating calls. The Django adapter embeds
// <meta name="redline-csrf" content="<token>" data-header="X-CSRFToken"> in the page;
// otherwise fall back to Django's default `csrftoken` cookie. Adapters that need no
// token simply ignore the header.
function csrfHeaders(method: string): Record<string, string> {
  if (method === 'GET' || method === 'HEAD') return {}
  try {
    const meta = document.querySelector('meta[name="redline-csrf"]')
    const token = meta?.getAttribute('content')
    if (token) return { [meta!.getAttribute('data-header') || 'X-CSRFToken']: token }
    const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)
    if (m) return { 'X-CSRFToken': decodeURIComponent(m[1]) }
  } catch { /* no DOM */ }
  return {}
}

async function raw(method: string, url: string, body?: unknown): Promise<{ status: number; json: any }> {
  if (import.meta.env.DEV) {
    if (!mockReady) mockReady = wantsMock() ? initMock(true) : Promise.resolve()
    await mockReady
    if (mock) return mock(method, url, body)
  }
  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...csrfHeaders(method) },
      credentials: 'same-origin',
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    if (import.meta.env.DEV) { await initMock(false); return mock!(method, url, body) }
    throw new ApiError(0, 'network', 'Cannot reach the Redline API.')
  }
  const ct = res.headers.get('content-type') || ''
  if (!ct.includes('json')) {
    if (import.meta.env.DEV) { await initMock(false); return mock!(method, url, body) }
    throw new ApiError(res.status, 'bad_response', 'Unexpected response from the Redline API.')
  }
  return { status: res.status, json: await res.json() }
}

export async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const { status, json } = await raw(method, url, body)
  if (status >= 400) {
    const e = json?.error
    // Redline answers {"error": {code, message}}; anything else (a proxy, a foreign
    // framework error) may use {"detail": "..."}.
    const detail = typeof json?.detail === 'string' ? json.detail : undefined
    throw new ApiError(status, e?.code ?? (status === 401 ? 'unauthorized' : status === 403 ? 'forbidden' : 'error'), e?.message ?? detail ?? `Request failed (${status})`)
  }
  return json as T
}

const enc = encodeURIComponent

export const qs = (o: Record<string, string | number | undefined | null>) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  meta: () => call<Meta>('GET', './api/meta'),
  traces: (f: TraceFilters, cursor?: string | null, limit = 50) =>
    call<Page>('GET', './api/traces' + qs({ ...f, limit, cursor })),
  trace: (id: string) => call<Trace>('GET', `./api/traces/${enc(id)}`),
  profile: (id: string) => call<Speedscope>('GET', `./api/traces/${enc(id)}/profile`),
  endpoints: (window: string) => call<{ items: EndpointStats[] }>('GET', './api/endpoints' + qs({ window })),
  issues: (window: string, kind?: string) => call<{ items: IssueGroup[] }>('GET', './api/issues' + qs({ window, kind })),
  compare: (a: string, b: string) => call<CompareResult>('GET', './api/compare' + qs({ a, b })),
  explain: (trace_id: string, span_id: string, analyze = false) =>
    call<ExplainResult>('POST', './api/explain', { trace_id, span_id, analyze }),
  clear: () => call<{ deleted: number }>('DELETE', './api/traces'),
  profileUrl: (id: string) => `./api/traces/${enc(id)}/profile`,
}
