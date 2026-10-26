import type { Trace, TraceFilters } from '../types'
import { ALL, EXPLAIN_PG, EXPLAIN_SQLITE, buildEndpoints, buildIssueGroups, buildProfile, summarize } from './data'

let traces: Trace[] = ALL.slice()
const delay = (n = 120) => new Promise(r => setTimeout(r, n + Math.random() * 80))

const statusOk = (s: number, f: string) =>
  /^\dxx$/.test(f) ? Math.floor(s / 100) === Number(f[0]) : s === Number(f)

function filtered(sp: URLSearchParams): Trace[] {
  const f: TraceFilters = Object.fromEntries(sp.entries())
  return traces.filter(t => {
    if (f.q && !(t.path + ' ' + (t.route ?? '') + ' ' + (t.view ?? '')).toLowerCase().includes(f.q.toLowerCase())) return false
    if (f.method && t.method !== f.method) return false
    if (f.status && !statusOk(t.status, f.status)) return false
    if (f.min_ms && t.duration_ms < Number(f.min_ms)) return false
    if (f.min_queries && t.query_count < Number(f.min_queries)) return false
    if (f.has_issues === '1' && t.issues.length === 0) return false
    return true
  })
}

export async function mockFetch(method: string, url: string, body: unknown): Promise<{ status: number; json: unknown }> {
  await delay()
  const u = new URL(url, 'http://x/')
  const path = u.pathname.replace(/^\/?(\.\/)?/, '').replace(/^\//, '')
  const ok = (json: unknown) => ({ status: 200, json })
  const err = (status: number, code: string, message: string) => ({ status, json: { error: { code, message } } })

  if (path === 'api/meta') {
    return ok({
      version: '0.1.0a1', framework: 'django',
      settings: { thresholds: { n_plus_one: 5, slow_query_ms: 100, too_many_queries: 50 }, sampling: { sample_rate: 1 }, profiling_enabled: true, explain_analyze_enabled: false },
      storage: { rows: traces.length, path: '.redline/redline.db' },
    })
  }
  if (path === 'api/traces' && method === 'GET') {
    const list = filtered(u.searchParams)
    const limit = Number(u.searchParams.get('limit') ?? 50)
    const start = Number(u.searchParams.get('cursor') ?? 0)
    const items = list.slice(start, start + limit).map(summarize)
    const next = start + limit < list.length ? String(start + limit) : null
    return ok({ items, next_cursor: next, total: list.length })
  }
  if (path === 'api/traces' && method === 'DELETE') {
    const n = traces.length
    traces = []
    return ok({ deleted: n })
  }
  let m = path.match(/^api\/traces\/([^/]+)\/profile$/)
  if (m) {
    const t = traces.find(x => x.id === m![1])
    return t?.has_profile ? ok(buildProfile()) : err(404, 'no_profile', 'This request was not profiled.')
  }
  m = path.match(/^api\/traces\/([^/]+)$/)
  if (m) {
    const t = traces.find(x => x.id === m![1])
    if (!t) return err(404, 'not_found', 'Trace not found.')
    if (method === 'DELETE') { traces = traces.filter(x => x !== t); return ok({ deleted: 1 }) }
    return ok(t)
  }
  if (path === 'api/endpoints') return ok({ items: buildEndpoints() })
  if (path === 'api/issues') {
    const kind = u.searchParams.get('kind')
    return ok({ items: buildIssueGroups().filter(g => !kind || g.kind === kind) })
  }
  if (path === 'api/explain') {
    const b = body as { trace_id: string; span_id: string; analyze: boolean }
    if (b.analyze) return err(403, 'analyze_disabled', 'EXPLAIN ANALYZE is disabled. Set explain_analyze=True to allow it.')
    const t = traces.find(x => x.id === b.trace_id)
    const s = t?.spans.find(x => x.id === b.span_id)
    if (!s) return err(404, 'not_found', 'Span not found.')
    if (!s.data.explainable) return err(400, 'not_explainable', 'Only SELECT queries can be explained.')
    return ok(s.data.vendor === 'sqlite' ? EXPLAIN_SQLITE : EXPLAIN_PG)
  }
  return err(404, 'not_found', 'Unknown mock endpoint ' + path)
}
