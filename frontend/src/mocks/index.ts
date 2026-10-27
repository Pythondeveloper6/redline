import type { Trace, TraceFilters } from '../types'
import { mockHistoryLoad } from './historyLoad'
import { mockAnalysis } from './analysis'
import { ALL, genericTraceAt, EXPLAIN_PG, EXPLAIN_SQLITE, buildEndpoints, buildIssueGroups, buildProfile, summarize } from './data'

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
    if (f.route && t.route !== f.route) return false
    if (f.replay_of && t.tags.replay_of !== f.replay_of) return false
    if (f.has_issues === '1' && t.issues.length === 0) return false
    if (f.since && t.started_at <= Number(f.since)) return false
    if (f.sql && !t.spans.some(x => x.kind === 'sql' && String(x.data.sql).toLowerCase().includes(f.sql!.toLowerCase()))) return false
    return true
  })
}

/** A synthetic new request for the dev-only Live mode. */
export function mockNewTrace() {
  const t = genericTraceAt()
  traces = [t, ...traces]
  return summarize(t)
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
      project_root: '/Users/dev/shop', editor: 'vscode', editor_url_template: 'vscode://file/{file}:{line}', release: 'mock-1',
      features: { load_from_ui: true, sse: true, snapshot: true },
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
  if (path === 'api/compare') {
    const a = traces.find(x => x.id === u.searchParams.get('a')); const b = traces.find(x => x.id === u.searchParams.get('b'))
    if (!a || !b) return err(404, 'trace_not_found', 'Trace not found.')
    const kinds: Record<string, { a_ms: number; b_ms: number; a_count: number; b_count: number }> = {}
    const sql = new Map<string, { fingerprint: string; sql: string; a_count: number; b_count: number; a_ms: number; b_ms: number }>()
    for (const [side, t] of [['a', a], ['b', b]] as const) {
      for (const s of t.spans) {
        const k = (kinds[s.kind] ??= { a_ms: 0, b_ms: 0, a_count: 0, b_count: 0 })
        k[`${side}_ms`] += s.duration_ms; k[`${side}_count`] += 1
        if (s.kind === 'sql') {
          const fp = String(s.data.fingerprint ?? s.name)
          const q = sql.get(fp) ?? { fingerprint: fp, sql: String(s.data.sql ?? s.name), a_count: 0, b_count: 0, a_ms: 0, b_ms: 0 }
          q[`${side}_count`] += 1; q[`${side}_ms`] += s.duration_ms; sql.set(fp, q)
        }
      }
    }
    const ka = new Set(a.issues.map(i => i.kind)); const kb = new Set(b.issues.map(i => i.kind))
    return ok({
      a: summarize(a), b: summarize(b),
      delta: { duration_ms: b.duration_ms - a.duration_ms, query_count: b.query_count - a.query_count, query_ms: b.query_ms - a.query_ms },
      by_kind: kinds, sql: [...sql.values()],
      issues: { only_a: [...ka].filter(k => !kb.has(k)), only_b: [...kb].filter(k => !ka.has(k)), both: [...ka].filter(k => kb.has(k)) },
    })
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
  const an = mockAnalysis(method, path, u.searchParams)
  if (an) return an
  const hl = await mockHistoryLoad(method, path, u.searchParams, body)
  if (hl) return hl
  return err(404, 'not_found', 'Unknown mock endpoint ' + path)
}
