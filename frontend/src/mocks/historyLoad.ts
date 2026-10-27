// Dev-only mocks for history / releases / load runs (CONTRACT §12). Returns null for foreign paths.
import type { Bottleneck, CompareEndpoint, CompareIssue, HistoryPoint, PeriodStats, Run, RunStep } from '../types'

type Res = { status: number; json: unknown }
const DAY = 86400, HOUR = 3600
const nowS = () => Math.floor(Date.now() / 1000)

function rng(seed: number) {
  let s = seed >>> 0
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296 }
}
const hash = (s: string) => [...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) >>> 0, 7)

const RELEASES = () => {
  const n = nowS()
  return [
    { release: 'v1.8.0', first_seen: n - 60 * DAY, last_seen: n - 21 * DAY, requests: 412_300 },
    { release: 'v1.9.0', first_seen: n - 21 * DAY, last_seen: n - 3 * DAY, requests: 288_940 },
    { release: 'v1.9.1', first_seen: n - 3 * DAY, last_seen: n, requests: 61_220 },
  ]
}

const WIN: Record<string, number> = { '24h': DAY, '7d': 7 * DAY, '30d': 30 * DAY, '90d': 90 * DAY }
const ENDPOINT_BASE: Record<string, number> = { 'GET /api/orders/': 180, 'GET /api/orders/{id}/': 70, 'POST /api/checkout/': 320, 'GET /api/products/': 45 }

function history(route: string | null, method: string | null, window: string, bucket: string) {
  const bs = bucket === '1d' ? DAY : HOUR
  const span = WIN[window] ?? 30 * DAY
  const n = Math.max(1, Math.floor(span / bs))
  const end = Math.floor(nowS() / bs) * bs
  const key = route ? `${method} ${route}` : ''
  const base = key ? ENDPOINT_BASE[key] ?? 90 : 60
  const rand = rng(hash(key || 'app'))
  const rels = RELEASES()
  const series: HistoryPoint[] = []
  for (let i = 0; i < n; i++) {
    const t = end - (n - i) * bs
    const hod = ((t % DAY) / DAY) * Math.PI * 2
    const load = 0.55 + 0.45 * Math.sin(hod - 2) // daily traffic wave
    const count = Math.round((key ? 220 : 900) * (bs / HOUR) * (0.25 + load) * (0.9 + rand() * 0.2))
    // each release shifts latency: v1.9.0 slightly slower, v1.9.1 clearly regressed
    const rel = rels.filter(r => r.first_seen <= t).length
    const k = rel >= 3 ? 1.55 : rel === 2 ? 1.12 : 1
    const p50 = base * 0.4 * k * (0.92 + rand() * 0.16) * (1 + load * 0.2)
    const p95 = p50 * (2.6 + rand() * 0.5), p99 = p95 * (1.5 + rand() * 0.4)
    series.push({
      t, count, p50_ms: p50, p95_ms: p95, p99_ms: p99,
      error_rate: Math.max(0, 0.004 + (rel >= 3 ? 0.006 : 0) + (rand() - 0.5) * 0.004 + (rand() > 0.985 ? 0.03 : 0)),
      avg_queries: (key ? 6 : 5) + (rel >= 3 ? 4 : 0) + rand() * 0.8,
    })
  }
  return { series, releases: rels.filter(r => r.first_seen >= end - n * bs).map(r => ({ release: r.release, first_seen: r.first_seen })) }
}

// ---- compare -------------------------------------------------------------
const st = (count: number, p95: number, q: number, err = 0.004): PeriodStats => ({ count, error_rate: err, p50_ms: p95 * 0.38, p95_ms: p95, p99_ms: p95 * 1.7, avg_queries: q })
function compare(a: string, b: string) {
  const row = (route: string, method: string, A: PeriodStats | null, B: PeriodStats | null, verdict: CompareEndpoint['verdict']): CompareEndpoint => ({
    route, method, a: A, b: B, verdict,
    delta_p95_pct: A && B ? ((B.p95_ms - A.p95_ms) / A.p95_ms) * 100 : null,
    delta_queries: A && B ? B.avg_queries - A.avg_queries : null,
  })
  const endpoints = [
    row('/api/products/', 'GET', st(9200, 112, 3), st(9650, 108, 3), 'same'),
    row('/api/orders/', 'GET', st(4100, 280, 6.1), st(4300, 610, 14.4, 0.011), 'regressed'),
    row('/api/orders/{id}/', 'GET', st(2800, 190, 5), st(2700, 120, 3), 'improved'),
    row('/api/checkout/', 'POST', st(900, 720, 12), st(940, 905, 12.2), 'regressed'),
    row('/api/wishlist/', 'GET', null, st(310, 88, 2), 'new'),
    row('/api/legacy/export/', 'GET', st(40, 1500, 31), null, 'gone'),
    row('/api/users/me/', 'GET', st(7000, 35, 2), st(7300, 36, 2), 'same'),
  ]
  const iss = (id: string, kind: CompareIssue['kind'], severity: CompareIssue['severity'], title: string, route: string, occurrences: number): CompareIssue => ({ id, kind, severity, title, route, occurrences })
  return {
    a: { spec: a }, b: { spec: b }, endpoints,
    issues: {
      new: [iss('i1', 'n_plus_one', 'critical', 'N+1: 38 queries to "orders_orderline" in OrderSerializer', 'GET /api/orders/', 4300), iss('i2', 'duplicate_query', 'warning', 'Same query to "auth_user" repeated 4 times', 'POST /api/checkout/', 940)],
      resolved: [iss('i3', 'slow_query', 'warning', 'Slow query: SELECT … FROM orders_order ORDER BY created_at (210 ms)', 'GET /api/orders/{id}/', 2800)],
      persisting: [iss('i4', 'too_many_queries', 'info', 'Requests exceed 50 queries', 'GET /api/legacy/export/', 40), iss('i5', 'slow_query', 'warning', 'Slow query on "catalog_product" without an index on sku', 'GET /api/products/', 9200)],
    },
  }
}

// ---- load runs -----------------------------------------------------------
const TARGETS = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500]
function mkStep(i: number, kneeAt: number, seed: number): RunStep {
  const r = rng(seed + i * 17)
  const target = TARGETS[i]
  const past = Math.max(0, i - kneeAt + 1) // steps beyond the knee
  const achieved = target * (past ? Math.max(0.35, 1 - 0.09 * past - 0.04) : 0.985 + r() * 0.015)
  const p95 = 38 + i * 3 + (past ? 55 * past * past : 0) + r() * 4
  const err = past > 1 ? Math.min(0.4, 0.015 * past * past) : 0.001 * r()
  const sent = Math.round(achieved * 15)
  return {
    step: i + 1, target_rps: target, achieved_rps: Math.round(achieved * 10) / 10, sent, ok: Math.round(sent * (1 - err)), errors: Math.round(sent * err),
    dropped: past ? Math.round(target * 15 - sent) : 0, p50_ms: p95 * 0.35, p95_ms: p95, p99_ms: p95 * 1.8, max_ms: p95 * 3.4, error_rate: err,
    error_breakdown: err > 0.005 ? { '503': Math.round(sent * err * 0.7), timeout: Math.round(sent * err * 0.3) } : {},
  }
}
const stepsFor = (n: number, kneeAt: number, seed: number) => Array.from({ length: n }, (_, i) => mkStep(i, kneeAt, seed))
const KNEE_AT = 6 // index of the knee (step 7, 350 rps)

interface MRun extends Run { _started?: number; _seed: number; _kneeAt: number }
function summarize(steps: RunStep[], kneeAt: number, done: boolean): Run['summary'] {
  const ok = steps.filter(s => s.step <= kneeAt)
  return {
    knee_step: steps.length > kneeAt ? kneeAt + 1 : null,
    max_ok_rps: ok.length ? Math.round(ok[ok.length - 1].achieved_rps) : null,
    verdict: !done ? null : steps.length > kneeAt ? 'saturated' : 'healthy',
  }
}
const n0 = nowS()
let runs: MRun[] = [
  { id: 'r_9f31', name: 'checkout ramp', status: 'done', created_at: n0 - 3 * HOUR, finished_at: n0 - 3 * HOUR + 150, config: { requests: [{ method: 'POST', path: '/api/checkout/', weight: 1 }], profile: { mode: 'steps', start_rps: 50, end_rps: 500, step_rps: 50, step_duration_s: 15 } }, steps: stepsFor(10, KNEE_AT, 3), summary: summarize(stepsFor(10, KNEE_AT, 3), KNEE_AT, true), _seed: 3, _kneeAt: KNEE_AT },
  { id: 'r_7a02', name: 'browse mix (nightly)', status: 'done', created_at: n0 - 30 * HOUR, finished_at: n0 - 30 * HOUR + 150, config: { profile: { mode: 'steps' } }, steps: stepsFor(10, 99, 9), summary: summarize(stepsFor(10, 99, 9), 99, true), _seed: 9, _kneeAt: 99 },
  { id: 'r_live', name: 'orders list soak', status: 'running', created_at: n0 - 4, finished_at: null, config: { profile: { mode: 'steps' } }, steps: [], summary: { knee_step: null, max_ok_rps: null, verdict: null }, _started: Date.now(), _seed: 5, _kneeAt: 5 },
]
const STEP_MS = 5000 // simulated: a new step every 5 s

function tick(r: MRun) {
  if (r.status !== 'running' || !r._started) return
  const n = Math.min(TARGETS.length, Math.floor((Date.now() - r._started) / STEP_MS))
  r.steps = stepsFor(n, r._kneeAt, r._seed)
  const done = n >= TARGETS.length
  if (done) { r.status = 'done'; r.finished_at = nowS() }
  r.summary = summarize(r.steps, r._kneeAt, done)
}
const pub = (r: MRun, withSteps: boolean) => {
  tick(r)
  const { _started, _seed, _kneeAt, steps, ...rest } = r
  void _started; void _seed; void _kneeAt
  return withSteps ? { ...rest, steps } : rest
}

function bottlenecks(r: MRun) {
  const steps = r.steps
  const series = (f: (i: number) => number, c: (i: number) => number): Bottleneck['per_step'] =>
    steps.map((s, i) => ({ step: s.step, mean_ms: f(i), p95_ms: f(i) * 2.2, count: Math.round(c(i)) }))
  const grow = (a: number, b: number, curve = 2.2) => (i: number) => a + (b - a) * Math.pow(i / Math.max(1, TARGETS.length - 1), curve)
  const items: Omit<Bottleneck, 'growth'>[] = [
    { key: 'sql:1', kind: 'sql', label: 'SELECT … FROM orders_order WHERE customer_id = %s AND status IN (…) ORDER BY created_at DESC', callsite: { file: 'shop/orders/views.py', line: 88, function: 'OrderViewSet.get_queryset', code: null, stack: [] }, per_step: series(grow(3, 410), i => 600 + i * 300) },
    { key: 'lock:1', kind: 'custom', label: 'inventory.reserve() row lock wait', callsite: { file: 'shop/inventory/services.py', line: 142, function: 'reserve', code: null, stack: [] }, per_step: series(grow(1.2, 96, 3), i => 90 + i * 40) },
    { key: 'http:1', kind: 'http_out', label: 'POST payments.example.com/v1/charges', callsite: { file: 'shop/payments/client.py', line: 57, function: 'charge', code: null, stack: [] }, per_step: series(grow(120, 260, 1.5), i => 80 + i * 30) },
    { key: 'sql:2', kind: 'sql', label: 'SELECT … FROM catalog_product WHERE id IN (…)', callsite: { file: 'shop/catalog/api.py', line: 31, function: 'list_products', code: null, stack: [] }, per_step: series(grow(2, 9), i => 400 + i * 200) },
    { key: 'cache:1', kind: 'cache', label: 'cache.get product:*', callsite: { file: 'shop/catalog/cache.py', line: 12, function: 'cached_product', code: null, stack: [] }, per_step: series(grow(0.4, 0.5, 1), i => 1500 + i * 600) },
  ]
  return {
    steps: steps.map(s => ({ step: s.step, target_rps: s.target_rps })),
    items: items.map(b => {
      const first = b.per_step[0]?.mean_ms || 1
      return { ...b, growth: Math.round((b.per_step[b.per_step.length - 1].mean_ms / first) * 10) / 10 }
    }).sort((a, b) => b.growth - a.growth),
  }
}

export async function mockHistoryLoad(method: string, path: string, params: URLSearchParams, body: unknown): Promise<Res | null> {
  const ok = (json: unknown): Res => ({ status: 200, json })
  const err = (status: number, code: string, message: string): Res => ({ status, json: { error: { code, message } } })

  if (path === 'api/history' && method === 'GET') return ok(history(params.get('route'), params.get('method'), params.get('window') ?? '30d', params.get('bucket') ?? '1d'))
  if (path === 'api/releases') return ok({ items: RELEASES() })
  if (path === 'api/history/compare') {
    const a = params.get('a') ?? '', b = params.get('b') ?? ''
    if (!/^(release:.+|range:\d+-\d+)$/.test(a) || !/^(release:.+|range:\d+-\d+)$/.test(b)) return err(400, 'invalid_filter', 'a and b must be release:<name> or range:<start>-<end>.')
    return ok(compare(a, b))
  }
  if (path === 'api/runs' && method === 'GET') return ok({ items: runs.map(r => pub(r, false)).sort((a, b) => b.created_at - a.created_at) })
  if (path === 'api/runs' && method === 'POST') {
    const b = body as { name?: string; requests?: unknown[] } | undefined
    if (!b?.requests?.length) return err(400, 'invalid_body', 'requests must not be empty.')
    const r: MRun = { id: 'r_' + Math.random().toString(16).slice(2, 6), name: b.name || 'untitled', status: 'running', created_at: nowS(), finished_at: null, config: b as Run['config'], steps: [], summary: { knee_step: null, max_ok_rps: null, verdict: null }, _started: Date.now(), _seed: 11, _kneeAt: 7 }
    runs = [r, ...runs]
    return ok(pub(r, true))
  }
  let m = path.match(/^api\/runs\/([^/]+)\/bottlenecks$/)
  if (m) {
    const r = runs.find(x => x.id === m![1])
    if (!r) return err(404, 'run_not_found', 'Run not found.')
    tick(r)
    return ok(r.steps.length < 2 ? { steps: r.steps.map(s => ({ step: s.step, target_rps: s.target_rps })), items: [] } : bottlenecks(r))
  }
  m = path.match(/^api\/runs\/([^/]+)\/stop$/)
  if (m && method === 'POST') {
    const r = runs.find(x => x.id === m![1])
    if (!r) return err(404, 'run_not_found', 'Run not found.')
    tick(r)
    if (r.status === 'running') { r.status = 'stopped'; r.finished_at = nowS(); r.summary = summarize(r.steps, r._kneeAt, true) }
    return ok(pub(r, true))
  }
  m = path.match(/^api\/runs\/([^/]+)$/)
  if (m) {
    const r = runs.find(x => x.id === m![1])
    if (!r) return err(404, 'run_not_found', 'Run not found.')
    if (method === 'DELETE') {
      tick(r)
      if (r.status === 'running') return err(409, 'run_active', 'Stop the run before deleting it.')
      runs = runs.filter(x => x !== r)
      return ok({ deleted: 1 })
    }
    return ok(pub(r, true))
  }
  return null
}
