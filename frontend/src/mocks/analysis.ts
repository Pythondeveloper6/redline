// Dev-only mocks for api/sql, api/indexes, api/cache, api/external (CONTRACT §12). Returns null for foreign paths.
import type { CacheStat, ExternalStat, IndexAdvice, SqlStat } from '../types'
import { ALL } from './data'

type Res = { status: number; json: unknown }

function sqlStats(): SqlStat[] {
  const m = new Map<string, SqlStat & { _t: Set<string> }>()
  for (const t of ALL) {
    for (const s of t.spans) {
      if (s.kind !== 'sql') continue
      const fp = String(s.data.fingerprint)
      const e = m.get(fp) ?? { fingerprint: fp.slice(-16).padStart(16, '0'), sql: String(s.data.sql).replace(/\b\d+\b/g, '?').replace(/'[^']*'/g, '?'), count: 0, traces: 0, total_ms: 0, max_ms: 0, mean_ms: 0, mean_rows: s.data.rows ?? null, routes: [] as string[], sample_trace_id: t.id, vendor: String(s.data.vendor ?? 'other'), _t: new Set<string>() }
      e.count++; e.total_ms += s.duration_ms; e.max_ms = Math.max(e.max_ms, s.duration_ms); e._t.add(t.id)
      const r = `${t.method} ${t.route}`
      if (t.route && !e.routes.includes(r)) e.routes.push(r)
      m.set(fp, e)
    }
  }
  return [...m.values()].map(({ _t, ...e }) => ({ ...e, traces: _t.size, mean_ms: e.total_ms / e.count })).sort((a, b) => b.total_ms - a.total_ms)
}

const CACHE: CacheStat[] = [
  { backend: 'redis', key_pattern: 'user:*:prefs', gets: 8120, hits: 7990, misses: 130, hit_rate: 0.984, sets: 140, deletes: 3, unread_sets: 0, stampedes: 0 },
  { backend: 'redis', key_pattern: 'product:*:detail', gets: 5400, hits: 3510, misses: 1890, hit_rate: 0.65, sets: 1900, deletes: 12, unread_sets: 0, stampedes: 2 },
  { backend: 'redis', key_pattern: 'cart:*:summary', gets: 920, hits: 210, misses: 710, hit_rate: 0.228, sets: 705, deletes: 40, unread_sets: 22, stampedes: 0 },
  { backend: 'locmem', key_pattern: 'feature-flags', gets: 12000, hits: 12000, misses: 0, hit_rate: 1, sets: 1, deletes: 0, unread_sets: 0, stampedes: 0 },
  { backend: 'redis', key_pattern: 'report:*:html', gets: 0, hits: 0, misses: 0, hit_rate: null, sets: 330, deletes: 0, unread_sets: 330, stampedes: 0 },
  { backend: 'redis', key_pattern: 'search:*', gets: 2100, hits: 1700, misses: 400, hit_rate: 0.8095, sets: 400, deletes: 0, unread_sets: 4, stampedes: 1 },
]

const EXTERNAL: ExternalStat[] = [
  { method: 'POST', host: 'payments.example.com', url_pattern: 'payments.example.com/v1/charge', count: 310, total_ms: 88400, p95_ms: 640, max_ms: 2100, errors: 21, error_rate: 0.0677, duplicates: 0, retries: 6 },
  { method: 'GET', host: 'rates.example.com', url_pattern: 'rates.example.com/latest/{id}', count: 4200, total_ms: 52100, p95_ms: 38, max_ms: 410, errors: 0, error_rate: 0, duplicates: 380, retries: 0 },
  { method: 'GET', host: 'api.github.com', url_pattern: 'api.github.com/repos/{id}/releases', count: 85, total_ms: 41300, p95_ms: 1220, max_ms: 3400, errors: 2, error_rate: 0.0235, duplicates: 0, retries: 0 },
  { method: 'POST', host: 'hooks.slack.com', url_pattern: 'hooks.slack.com/services/{id}', count: 40, total_ms: 9100, p95_ms: 260, max_ms: 380, errors: 0, error_rate: 0, duplicates: 2, retries: 0 },
]

const ADVICE: IndexAdvice = {
  candidates: [
    { table: 'shop_orderitem', columns: ['order_id'], create_sql: 'CREATE INDEX CONCURRENTLY ix_shop_orderitem_order_id ON shop_orderitem (order_id);', covers: ['0a1b2c3d4e5f6a7b'], queries: 1840, total_ms: 6420, est_saving_ms: 5700, vendor: 'postgresql', kind: 'btree', confidence: 'high', verified: true },
    { table: 'events', columns: ['kind', 'ts'], create_sql: 'CREATE INDEX ix_events_kind_ts ON events (kind, ts DESC);', covers: ['1111aaaa2222bbbb', '3333cccc4444dddd'], queries: 90, total_ms: 3100, est_saving_ms: 2650, vendor: 'sqlite', kind: 'composite', confidence: 'medium', verified: false },
    { table: 'shop_profile', columns: ['user_id'], create_sql: 'CREATE UNIQUE INDEX ix_shop_profile_user_id ON shop_profile (user_id);', covers: ['5555eeee6666ffff'], queries: 700, total_ms: 410, est_saving_ms: 180, vendor: 'postgresql', confidence: 'low' },
  ],
  unused: [
    { table: 'shop_order', index: 'ix_shop_order_legacy_ref', scans: 0, size_bytes: 48_234_496 },
    { table: 'shop_cartline', index: 'ix_cartline_product_qty', scans: 3, size_bytes: 9_437_184 },
  ],
  unused_supported: true,
}

export function mockAnalysis(method: string, path: string, sp: URLSearchParams): Res | null {
  if (method !== 'GET') return null
  if (path === 'api/sql') {
    const q = (sp.get('q') ?? '').toLowerCase()
    return { status: 200, json: { items: sqlStats().filter(s => !q || s.sql.toLowerCase().includes(q)) } }
  }
  if (path === 'api/indexes') {
    if (sessionStorage.getItem('redline-mock-no-indexes') === '1') return { status: 501, json: { error: { code: 'not_supported', message: 'The index advisor is not available in this build.' } } }
    return { status: 200, json: ADVICE }
  }
  if (path === 'api/cache') return { status: 200, json: { items: CACHE } }
  if (path === 'api/external') return { status: 200, json: { items: EXTERNAL } }
  return null
}
