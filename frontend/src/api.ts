import type { CacheStat, ConfigResponse, LoginBody, CompareResult, EndpointStats, ExplainResult, ExternalStat, IndexAdvice, IssueGroup, Meta, Page, Speedscope, SqlStat, Trace, TraceFilters } from './types'
import { getSnapshot } from './lib/snapshot'

export class ApiError extends Error {
  status: number
  code: string
  /** Seconds to wait before retrying (429 `Retry-After`). */
  retryAfter?: number
  constructor(status: number, code: string, message: string, retryAfter?: number) {
    super(message)
    this.status = status
    this.code = code
    this.retryAfter = retryAfter
  }
}

// ---- Login gate -------------------------------------------------------------
// Any API call answering 401 `login_required` flips this store; App.tsx subscribes
// (useSyncExternalStore) and renders the full-page Login screen instead of the Shell.
export type LoginKind = 'token' | 'basic'
let loginKind: LoginKind | null = null
const loginListeners = new Set<() => void>()
export function getLoginRequired(): LoginKind | null { return loginKind }
export function subscribeLoginRequired(cb: () => void) {
  loginListeners.add(cb)
  return () => { loginListeners.delete(cb) }
}
function setLoginRequired(kind: LoginKind) {
  if (loginKind === kind) return
  loginKind = kind
  loginListeners.forEach(cb => cb())
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

/** Offline snapshot: answer from `window.__REDLINE_SNAPSHOT__`, never touch the network. */
function fromSnapshot(method: string, url: string): { status: number; json: any } {
  const snap = getSnapshot()!
  const path = new URL(url, 'http://x/').pathname.replace(/^\/?(\.\/)?/, '').replace(/^\//, '')
  const err = (status: number, code: string, message: string) => ({ status, json: { error: { code, message } } })
  if (method !== 'GET') return err(403, 'snapshot', 'This is an offline snapshot; nothing can be changed.')
  if (path === 'api/meta') {
    return { status: 200, json: { version: 'snapshot', framework: snap.trace.framework, settings: {}, storage: { rows: 1, path: '' }, features: { load_from_ui: false, sse: false, snapshot: false } } }
  }
  if (path === `api/traces/${snap.trace.id}`) return { status: 200, json: snap.trace }
  if (path === `api/traces/${snap.trace.id}/profile`) {
    return snap.profile ? { status: 200, json: snap.profile } : err(404, 'profile_not_found', 'This snapshot has no profile.')
  }
  return err(404, 'snapshot', 'Not available in an offline snapshot.')
}

async function raw(method: string, url: string, body?: unknown): Promise<{ status: number; json: any; retryAfter?: number }> {
  if (getSnapshot()) return fromSnapshot(method, url)
  if (import.meta.env.DEV) {
    if (!mockReady) mockReady = wantsMock() ? initMock(true) : Promise.resolve()
    await mockReady
    if (mock) return mock(method, url, body)
  }
  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers: { 'X-Redline-UI': '1', ...(body ? { 'Content-Type': 'application/json' } : {}), ...csrfHeaders(method) },
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
  const ra = Number(res.headers.get('retry-after'))
  return { status: res.status, json: await res.json(), retryAfter: Number.isFinite(ra) && ra > 0 ? ra : undefined }
}

export async function call<T>(method: string, url: string, body?: unknown): Promise<T> {
  const { status, json, retryAfter } = await raw(method, url, body)
  if (status >= 400) {
    const e = json?.error
    if (status === 401 && e?.code === 'login_required') setLoginRequired(e.login === 'basic' ? 'basic' : 'token')
    // Redline answers {"error": {code, message}}; anything else (a proxy, a foreign
    // framework error) may use {"detail": "..."}.
    const detail = typeof json?.detail === 'string' ? json.detail : undefined
    throw new ApiError(status, e?.code ?? (status === 401 ? 'unauthorized' : status === 403 ? 'forbidden' : 'error'), e?.message ?? detail ?? `Request failed (${status})`, retryAfter)
  }
  return json as T
}

/** GET a text/plain body (e.g. the TOML export). The dev mock answers `{text}` JSON for these. */
export async function callText(url: string): Promise<string> {
  if (getSnapshot()) throw new ApiError(403, 'snapshot', 'This is an offline snapshot; nothing can be exported.')
  if (import.meta.env.DEV) {
    if (!mockReady) mockReady = wantsMock() ? initMock(true) : Promise.resolve()
    await mockReady
    if (mock) {
      const r = await mock('GET', url, undefined)
      if (r.status >= 400) { const e = (r.json as any)?.error; throw new ApiError(r.status, e?.code ?? 'error', e?.message ?? `Request failed (${r.status})`) }
      return String((r.json as any)?.text ?? '')
    }
  }
  let res: Response
  try { res = await fetch(url, { headers: { 'X-Redline-UI': '1' }, credentials: 'same-origin' }) } catch { throw new ApiError(0, 'network', 'Cannot reach the Redline API.') }
  if (!res.ok) {
    let e: any = null
    try { e = (await res.json())?.error } catch { /* not JSON */ }
    throw new ApiError(res.status, e?.code ?? 'error', e?.message ?? `Request failed (${res.status})`)
  }
  return res.text()
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
  config: () => call<ConfigResponse>('GET', './api/config'),
  login: (body: LoginBody) => call<{ ok: boolean; expires_at: number }>('POST', './api/login', body),
  logout: () => call<{ ok: boolean }>('POST', './api/logout', {}),
  clear: () => call<{ deleted: number }>('DELETE', './api/traces'),
  sql: (p: { q?: string; window: string; limit?: number }) => call<{ items: SqlStat[] }>('GET', './api/sql' + qs(p)),
  indexes: (window: string) => call<IndexAdvice>('GET', './api/indexes' + qs({ window })),
  cache: (window: string) => call<{ items: CacheStat[] }>('GET', './api/cache' + qs({ window })),
  external: (window: string) => call<{ items: ExternalStat[] }>('GET', './api/external' + qs({ window })),
  snapshotUrl: (id: string) => `./api/traces/${enc(id)}/snapshot`,
  profileUrl: (id: string) => `./api/traces/${enc(id)}/profile`,
}
