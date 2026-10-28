import { suiteToToml } from '../lib/suiteToml'
// Dev-only mocks for the refactor check (CONTRACT §19). Returns null for foreign paths.
import type {
  BaselineRequest, CheckReport, CheckRunDetail, CheckRunItem, CheckSuite, CheckSuiteDoc, CheckSummary, PerfStats, ReportRequest,
} from '../types'
import { buildEndpoints } from './data'

type Res = { status: number; json: unknown }
const nowS = () => Math.floor(Date.now() / 1000)
const MIN = 60, HOUR = 3600, DAY = 86400
const GIT_OLD = { sha: '9f3c2a1b7d', branch: 'main', dirty: false }
const GIT_NEW = { sha: 'c41e08d2aa', branch: 'refactor/orders-orm', dirty: true }

function rng(seed: number) {
  let s = seed >>> 0
  return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296 }
}
/** Roughly log-normal latency samples around `median` with relative spread `cv`. */
function samples(median: number, cv: number, n: number, seed: number, outlier = false): number[] {
  const r = rng(seed)
  const out: number[] = []
  for (let i = 0; i < n; i++) {
    const g = (r() + r() + r() + r() - 2) * 1.7 // ~N(0,1)
    out.push(Math.max(0.2, +(median * Math.exp(g * cv)).toFixed(2)))
  }
  if (outlier) out[n - 1] = +(median * 3.4).toFixed(2)
  return out
}
function stats(v: number[], failed = 0): PerfStats {
  const s = [...v].sort((a, b) => a - b)
  const q = (p: number) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))]
  const mean = v.reduce((a, b) => a + b, 0) / v.length
  const stdev = Math.sqrt(v.reduce((a, b) => a + (b - mean) ** 2, 0) / v.length)
  return { samples_ms: v, median: +q(0.5).toFixed(2), p95: +q(0.95).toFixed(2), mean: +mean.toFixed(2), stdev: +stdev.toFixed(2), failed }
}

// ---- suites -------------------------------------------------------------------------------------
const orders: CheckSuite = {
  name: 'orders refactor', base_url: 'http://127.0.0.1:8000', headers: { Authorization: 'Bearer ${env:API_TOKEN}', Accept: 'application/json' },
  timeout_s: 10, repeat: 20, warmup: 3, thresholds: { slower_pct: 10, slower_ms: 2, max_query_increase: 0 },
  requests: [
    { name: 'list orders', method: 'GET', path: '/orders?page=1', perf: true, response: true, ignore: ['data[*].created_at', 'meta.request_id'] },
    { name: 'order detail', method: 'GET', path: '/orders/1042', perf: true, response: true, ignore: ['meta.request_id'] },
    { name: 'customers', method: 'GET', path: '/customers', perf: true, response: true, ignore: [] },
    { name: 'product search', method: 'GET', path: '/products/search?q=lamp', perf: true, response: true, ignore: [] },
    { name: 'health', method: 'GET', path: '/health', perf: false, response: true, ignore: [] },
    { name: 'invoice 77', method: 'GET', path: '/invoices/77', perf: true, response: true, ignore: [], expect_status: 200 },
    { name: 'HEAD status', method: 'HEAD', path: '/status', perf: false, response: false, ignore: [] },
  ],
}
const catalog: CheckSuite = {
  name: 'catalog smoke', timeout_s: 10, repeat: 10, warmup: 1, thresholds: { slower_pct: 15, slower_ms: 5, max_query_increase: 2 },
  requests: [
    { name: 'products', method: 'GET', path: '/products/', perf: true, response: true, ignore: [] },
    { name: 'product 3', method: 'GET', path: '/products/3/', perf: true, response: true, ignore: [] },
    { name: 'categories', method: 'GET', path: '/categories/', perf: true, response: true, ignore: [] },
  ],
}
const suites = new Map<string, CheckSuiteDoc>()
const seedSuite = (id: string, suite: CheckSuite, age: number) => suites.set(id, { id, name: suite.name, suite, created_at: nowS() - age * 2, updated_at: nowS() - age })
seedSuite('s1', orders, 2 * HOUR)
seedSuite('s2', catalog, 3 * DAY)
seedSuite('s3', { name: 'billing readonly', repeat: 20, warmup: 3, requests: [{ name: 'GET /billing/plans', method: 'GET', path: '/billing/plans', perf: true, response: true }] }, 9 * DAY)

// ---- runs -----------------------------------------------------------------------------------------
interface Stored { item: CheckRunItem; requests?: BaselineRequest[]; report?: CheckReport; startedMock?: number; total?: number; frozen?: number }
const runs = new Map<string, Stored>()

function baselineRequests(): BaselineRequest[] {
  const mk = (name: string, method: string, path: string, med: number, cv: number, q: number | null, vol: string[], kind: 'json' | 'text' | 'hash' = 'json', seed = 1, status = 200, perf = true): BaselineRequest => ({
    name, method, path,
    perf: perf ? stats(samples(med, cv, 20, seed), 0) : null,
    server: q == null ? null : { duration_ms_median: +(med * 0.8).toFixed(1), queries_median: q, queries_max: q + 1, query_ms_median: +(med * 0.4).toFixed(1), sql: { sel_orders: { count: 1, sql: 'SELECT * FROM orders WHERE page = ?' } }, issues: q > 30 ? [{ kind: 'n_plus_one', title: 'N+1 on order_items' }] : [] },
    response: { status, content_type: kind === 'json' ? 'application/json' : 'text/plain', headers: { 'content-type': kind === 'json' ? 'application/json' : 'text/plain' }, body_kind: kind, size: kind === 'json' ? 18432 : 2, ...(kind === 'hash' ? { sha256: 'e3b0c442' } : {}) },
    volatile: vol,
  })
  return [
    mk('list orders', 'GET', '/orders?page=1', 182, 0.12, 42, ['data[0].created_at', 'data[1].created_at', 'meta.request_id'], 'json', 11),
    mk('order detail', 'GET', '/orders/1042', 24, 0.1, 9, ['meta.request_id', 'meta.served_at'], 'json', 12),
    mk('customers', 'GET', '/customers', 41, 0.1, 5, [], 'json', 13),
    mk('product search', 'GET', '/products/search?q=lamp', 64, 0.62, 7, [], 'json', 14),
    mk('health', 'GET', '/health', 2, 0.1, null, [], 'text', 15, 200, false),
    mk('invoice 77', 'GET', '/invoices/77', 33, 0.1, 6, ['printed_at'], 'json', 16),
    { ...mk('HEAD status', 'HEAD', '/status', 1, 0.1, null, [], 'hash', 17, 204, false), perf: null },
  ]
}

function reportRequests(): ReportRequest[] {
  const pair = (b: number, a: number, cvB: number, cvA: number, seed: number, failedAfter = 0) => {
    const before = stats(samples(b, cvB, 20, seed)), after = stats(samples(a, cvA, 20, seed + 100, false), failedAfter)
    return { before, after }
  }
  const cp = (b: PerfStats, a: PerfStats) => ({ change_ms: +(a.median - b.median).toFixed(2), change_pct: +(((a.median - b.median) / b.median) * 100).toFixed(1) })
  const same = (): ReportRequest['response'] => ({ verdict: 'same', status: [200, 200], diff: [], text_diff: null, ignored: [] })
  const out: ReportRequest[] = []

  { // 1 faster, N+1 fixed, ignored volatile fields
    const { before, after } = pair(182, 96, 0.12, 0.1, 11)
    out.push({
      name: 'list orders', method: 'GET', path: '/orders?page=1',
      perf: { verdict: 'faster', before, after, ...cp(before, after), p_value: 0.00002, reason: 'time' },
      server: {
        before: { duration_ms_median: 146, queries_median: 42, queries_max: 43, query_ms_median: 71, sql: { sel_items: { count: 40, sql: 'SELECT * FROM order_items WHERE order_id = ?' }, sel_orders: { count: 1 } }, issues: [{ kind: 'n_plus_one', title: 'N+1 on order_items' }] },
        after: { duration_ms_median: 71, queries_median: 3, queries_max: 3, query_ms_median: 21, sql: { sel_items_in: { count: 1 }, sel_orders: { count: 1 } }, issues: [] },
        queries_change: -39,
        new_issues: [],
        fixed_issues: [{ kind: 'n_plus_one', title: 'N+1 on order_items', fingerprint: 'sel_items', sample_trace_id: 'tr_0001' }],
        new_sql: [],
        gone_sql: [],
      },
      response: { verdict: 'same', status: [200, 200], diff: [], text_diff: null, ignored: ['data[0].created_at', 'data[1].created_at', 'meta.request_id'] },
    })
  }
  { // 2 slower (time), changed JSON with every diff kind, new duplicate query
    const { before, after } = pair(24, 41, 0.1, 0.12, 12)
    out.push({
      name: 'order detail', method: 'GET', path: '/orders/1042',
      perf: { verdict: 'slower', before, after, ...cp(before, after), p_value: 0.00001, reason: 'time' },
      server: {
        before: { duration_ms_median: 19, queries_median: 9, queries_max: 9, query_ms_median: 8, sql: {}, issues: [] },
        after: { duration_ms_median: 36, queries_median: 12, queries_max: 13, query_ms_median: 19, sql: {}, issues: [{ kind: 'duplicate_query', title: 'Duplicate query on customers' }] },
        queries_change: 3,
        new_issues: [{ kind: 'duplicate_query', title: 'SELECT * FROM customers WHERE id = ? (3x)', fingerprint: 'dup_cust', sample_trace_id: 'tr_0003' }],
        fixed_issues: [],
        new_sql: [],
        gone_sql: [],
      },
      response: {
        verdict: 'changed', status: [200, 200], text_diff: null, ignored: ['meta.request_id', 'meta.served_at'], diff_total: 54,
        diff: [
          { path: 'total', kind: 'changed', before: 129.9, after: 130 },
          { path: 'customer.phone', kind: 'removed', before: '+1 555 0100', after: null },
          { path: 'customer.display_name', kind: 'added', before: null, after: 'Ada Lovelace' },
          { path: 'items[0].quantity', kind: 'type', before: 2, after: '2' },
          { path: 'items[1].note', kind: 'changed', before: 'Gift wrap, deliver after 6pm and leave with the neighbour if nobody is home. '.repeat(5), after: null },
        ],
      },
    })
  }
  { // 3 same / same
    const { before, after } = pair(41, 40, 0.1, 0.1, 13)
    out.push({
      name: 'customers', method: 'GET', path: '/customers',
      perf: { verdict: 'same', before, after, ...cp(before, after), p_value: 0.41, reason: null },
      server: { before: { queries_median: 5 }, after: { queries_median: 5 }, queries_change: 0, new_issues: [], fixed_issues: [], new_sql: [], gone_sql: [] },
      response: same(),
    })
  }
  { // 4 noisy
    const { before, after } = pair(64, 71, 0.62, 0.7, 14)
    out.push({
      name: 'product search', method: 'GET', path: '/products/search?q=lamp',
      perf: { verdict: 'noisy', before, after, ...cp(before, after), p_value: 0.18, reason: 'cv' },
      server: { before: { queries_median: 7 }, after: { queries_median: 7 }, queries_change: 0, new_issues: [], fixed_issues: [], new_sql: [], gone_sql: [] },
      response: same(),
    })
  }
  { // 5 perf n/a, changed text
    out.push({
      name: 'health', method: 'GET', path: '/health',
      perf: { verdict: 'n/a', before: null, after: null, change_ms: null, change_pct: null, p_value: null, reason: null },
      server: null,
      response: {
        verdict: 'changed', status: [200, 200], diff: [], ignored: [],
        text_diff: '--- before\n+++ after\n@@ -1,4 +1,5 @@\n status: ok\n-version: 1.8.0\n+version: 1.9.0\n db: ok\n cache: ok\n+queue: ok',
      },
    })
  }
  { // 6 slower because of queries, broken response, failed calls
    const { before, after } = pair(33, 34, 0.1, 0.1, 16, 12)
    out.push({
      name: 'invoice 77', method: 'GET', path: '/invoices/77',
      perf: { verdict: 'slower', before, after, ...cp(before, after), p_value: 0.62, reason: 'queries' },
      server: {
        before: { queries_median: 6 }, after: { queries_median: 11, queries_max: 12 }, queries_change: 5,
        new_issues: [], fixed_issues: [],
        new_sql: [], gone_sql: [],
      },
      response: {
        verdict: 'broken', status: [200, 500], ignored: ['printed_at'], text_diff: null, diff_total: 2,
        diff: [{ path: 'error', kind: 'added', before: null, after: 'Internal Server Error' }, { path: 'lines', kind: 'removed', before: [{ sku: 'A-1' }], after: null }],
      },
    })
  }
  // 7 nothing measured
  out.push({
    name: 'HEAD status', method: 'HEAD', path: '/status',
    perf: { verdict: 'n/a', before: null, after: null, change_ms: null, change_pct: null, p_value: null, reason: null },
    server: null,
    response: { verdict: 'n/a', status: [204, 204], diff: [], text_diff: null, ignored: [] },
  })
  return out
}

function buildReport(suite: CheckSuite, runId: string, baseline: { id: string; label: string | null; created_at: number }): CheckReport {
  return {
    format: 'redline-check-report', version: 1, suite,
    baseline: { id: baseline.id, label: baseline.label, git: GIT_OLD, created_at: baseline.created_at },
    run: { id: runId, git: GIT_NEW, created_at: nowS() - 20 * MIN, base_url: 'http://127.0.0.1:8000' },
    requests: reportRequests(),
  }
}

function counts(rep: CheckReport): CheckSummary {
  const c = { faster: 0, slower: 0, same: 0, noisy: 0, changed: 0, broken: 0, passed: 0 }
  for (const r of rep.requests) {
    if (r.perf.verdict === 'faster') c.faster++; else if (r.perf.verdict === 'slower') c.slower++; else if (r.perf.verdict === 'same') c.same++; else if (r.perf.verdict === 'noisy') c.noisy++
    if (r.response.verdict === 'changed') c.changed++; else if (r.response.verdict === 'broken') c.broken++
    if (r.perf.verdict !== 'slower' && r.response.verdict !== 'changed' && r.response.verdict !== 'broken') c.passed++
  }
  return { counts: c }
}

function seedRuns() {
  const t = nowS()
  const b1: CheckRunItem = { id: 'b1', suite_id: 's1', kind: 'baseline', baseline_id: null, status: 'done', label: 'before ORM refactor', git: GIT_OLD, created_at: t - 3 * HOUR, finished_at: t - 3 * HOUR + 41, summary: null }
  runs.set('b1', { item: b1, requests: baselineRequests() })
  const rep = buildReport(orders, 'c1', b1)
  runs.set('c1', { item: { id: 'c1', suite_id: 's1', kind: 'compare', baseline_id: 'b1', status: 'done', label: 'after eager loading', git: GIT_NEW, created_at: t - 20 * MIN, finished_at: t - 20 * MIN + 44, summary: counts(rep) }, report: rep })
  runs.set('c2', { item: { id: 'c2', suite_id: 's1', kind: 'compare', baseline_id: 'b1', status: 'failed', label: null, git: GIT_NEW, created_at: t - 2 * HOUR, finished_at: t - 2 * HOUR + 3, summary: null, error: 'Could not reach http://127.0.0.1:8000 (connection refused). Is the app running?' } })
  runs.set('c3', { item: { id: 'c3', suite_id: 's1', kind: 'compare', baseline_id: 'b1', status: 'stopped', label: 'aborted attempt', git: GIT_NEW, created_at: t - 150 * MIN, finished_at: t - 148 * MIN, summary: null } })
  runs.set('r1', { item: { id: 'r1', suite_id: 's1', kind: 'compare', baseline_id: 'b1', status: 'running', label: 'live demo', git: GIT_NEW, created_at: t - 25, finished_at: null, summary: null }, total: 7 * 20 + 7 * 3, frozen: 0.42 })
  const b2: CheckRunItem = { id: 'b2', suite_id: 's2', kind: 'baseline', baseline_id: null, status: 'done', label: null, git: { sha: '77aa001bcd', branch: 'main', dirty: true }, created_at: t - 3 * DAY, finished_at: t - 3 * DAY + 12, summary: null }
  runs.set('b2', { item: b2, requests: baselineRequests().slice(0, 3).map((r, i) => ({ ...r, name: catalog.requests[i].name, path: catalog.requests[i].path })) })
}
seedRuns()

// ---- progress ------------------------------------------------------------------------------------------
const RUN_MS = 12000
function advance(id: string): Stored | undefined {
  const s = runs.get(id)
  if (!s || s.item.status !== 'running') return s
  const total = s.total ?? 100
  let f: number
  if (s.frozen != null) f = s.frozen
  else f = Math.min(1, (Date.now() - (s.startedMock ?? Date.now())) / RUN_MS)
  s.item.progress = { done: Math.floor(total * f), total, current: s.item.kind === 'baseline' ? 'recording' : 'measuring' }
  if (f >= 1) {
    s.item.status = 'done'; s.item.finished_at = nowS(); s.item.progress = null
    const suite = suites.get(s.item.suite_id ?? '')?.suite ?? orders
    if (s.item.kind === 'baseline') s.requests = baselineRequests()
    else {
      const bl = runs.get(s.item.baseline_id ?? '')?.item ?? runs.get('b1')!.item
      s.report = buildReport(suite, id, bl); s.item.summary = counts(s.report)
    }
  }
  return s
}

const detail = (s: Stored): CheckRunDetail => ({
  ...s.item, suite: suites.get(s.item.suite_id ?? '')?.suite ?? null,
  ...(s.report ? { report: s.report } : {}), ...(s.requests ? { requests: s.requests } : {}),
})

function toml(d: CheckSuiteDoc): string {
  return suiteToToml(d.suite)
}

let seq = 100
export async function mockChecks(method: string, path: string, params: URLSearchParams, body: unknown): Promise<Res | null> {
  if (!path.startsWith('api/checks/')) return null
  const ok = (json: unknown, status = 200): Res => ({ status, json })
  const err = (status: number, code: string, message: string): Res => ({ status, json: { error: { code, message } } })
  const q = new URLSearchParams(typeof location !== 'undefined' ? location.search : '')
  const readOnly = q.get('ro') === '1'
  const fromUi = q.get('checks_from_ui') !== '0'
  if (method !== 'GET' && readOnly) return err(403, 'read_only', 'Redline is read-only.')
  const rest = path.slice('api/checks/'.length)

  if (rest === 'suites' && method === 'GET') {
    const items = [...suites.values()].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0)).map(d => {
      const rs = [...runs.values()].map(r => r.item).filter(r => r.suite_id === d.id && r.status !== 'running').sort((a, b) => b.created_at - a.created_at)
      const lb = rs.find(r => r.kind === 'baseline' && r.status === 'done'), lc = rs.find(r => r.kind === 'compare')
      return {
        id: d.id, name: d.name, requests: d.suite.requests.length, updated_at: d.updated_at ?? 0,
        last_baseline: lb ? { id: lb.id, label: lb.label, git: lb.git, created_at: lb.created_at } : null,
        last_compare: lc ? { id: lc.id, status: lc.status, created_at: lc.created_at, summary: lc.summary } : null,
      }
    })
    return ok({ items })
  }
  if (rest === 'suites' && method === 'POST') {
    const b = body as { name: string; suite: CheckSuite }
    if (!b?.name || !b.suite) return err(400, 'invalid_suite', 'name and suite are required.')
    if (b.suite.requests.some(r => !['GET', 'HEAD', 'OPTIONS'].includes(r.method))) return err(400, 'invalid_suite', 'Only GET, HEAD and OPTIONS requests can be checked.')
    const id = 's' + ++seq
    const d: CheckSuiteDoc = { id, name: b.name, suite: { ...b.suite, name: b.name }, created_at: nowS(), updated_at: nowS() }
    suites.set(id, d)
    return ok(d, 201)
  }
  if (rest === 'suites/draft' && method === 'POST') {
    const b = (body ?? {}) as { routes?: string[]; limit?: number }
    const eps = buildEndpoints().filter(e => ['GET', 'HEAD', 'OPTIONS'].includes(e.method))
    const chosen = (b.routes?.length ? eps.filter(e => b.routes!.includes(e.route)) : eps).slice(0, b.limit ?? 20)
    return ok({
      name: 'draft from traffic', base_url: null, headers: { Authorization: 'Bearer ${env:API_TOKEN}' }, repeat: 20, warmup: 3, timeout_s: 10,
      thresholds: { slower_pct: 10, slower_ms: 2, max_query_increase: 0 },
      requests: chosen.map(e => ({
        name: `${e.method} ${e.route.replace(/\{(\w+)\}/g, '1')}`, method: e.method, path: e.route.replace(/\{(\w+)\}/g, '1'),
        headers: {}, perf: true, response: true, ignore: [], ignore_headers: [],
      })),
    })
  }
  let m = rest.match(/^suites\/([^/]+)\/export$/)
  if (m) {
    const d = suites.get(m[1]); if (!d) return err(404, 'not_found', 'Suite not found.')
    return ok({ text: toml(d) })
  }
  m = rest.match(/^suites\/([^/]+)$/)
  if (m) {
    const d = suites.get(m[1]); if (!d) return err(404, 'not_found', 'Suite not found.')
    if (method === 'GET') return ok(d)
    if (method === 'PUT') {
      const b = body as { name: string; suite: CheckSuite }
      d.name = b.name; d.suite = { ...b.suite, name: b.name }; d.updated_at = nowS()
      return ok(d)
    }
    if (method === 'DELETE') { suites.delete(m[1]); for (const [k, r] of runs) if (r.item.suite_id === m[1]) runs.delete(k); return ok({ deleted: 1 }) }
  }
  if (rest === 'runs' && method === 'POST') {
    if (!fromUi) return err(403, 'checks_from_ui_disabled', 'Starting checks from the UI is disabled (checks_from_ui is off).')
    const b = body as { suite_id: string; kind: 'baseline' | 'compare'; baseline_id?: string; label?: string }
    const d = suites.get(b.suite_id); if (!d) return err(404, 'not_found', 'Suite not found.')
    if ([...runs.values()].some(r => r.item.suite_id === b.suite_id && r.item.status === 'running' && r.frozen == null)) return err(409, 'run_active', 'A run for this suite is already in progress.')
    if (b.kind === 'compare' && !b.baseline_id) return err(400, 'invalid_request', 'baseline_id is required for a compare run.')
    const id = 'n' + ++seq
    runs.set(id, {
      item: { id, suite_id: b.suite_id, kind: b.kind, baseline_id: b.baseline_id ?? null, status: 'running', label: b.label || null, git: b.kind === 'baseline' ? GIT_OLD : GIT_NEW, created_at: nowS(), finished_at: null, summary: null, progress: { done: 0, total: 100 } },
      startedMock: Date.now(), total: d.suite.requests.length * ((d.suite.repeat ?? 20) + (d.suite.warmup ?? 3)),
    })
    return ok({ id }, 202)
  }
  if (rest === 'runs' && method === 'GET') {
    const sid = params.get('suite_id')
    const items = [...runs.keys()].map(k => advance(k)!.item).filter(r => !sid || r.suite_id === sid).sort((a, b) => b.created_at - a.created_at)
    return ok({ items })
  }
  if (rest === 'baselines/import' && method === 'POST') {
    const id = 'imp' + ++seq
    runs.set(id, { item: { id, suite_id: null, kind: 'baseline', baseline_id: null, status: 'done', label: 'imported', git: GIT_OLD, created_at: nowS(), finished_at: nowS() }, requests: baselineRequests() })
    return ok({ id }, 201)
  }
  m = rest.match(/^runs\/([^/]+)(\/stop)?$/)
  if (m) {
    const s = advance(m[1]); if (!s) return err(404, 'run_not_found', 'Run not found.')
    if (m[2] && method === 'POST') {
      if (s.item.status !== 'running') return err(409, 'run_not_active', 'This run is not running.')
      s.item.status = 'stopped'; s.item.finished_at = nowS(); s.item.progress = null
      return ok({ stopped: true })
    }
    if (method === 'DELETE') {
      if (s.item.status === 'running') return err(409, 'run_active', 'Stop the run before deleting it.')
      runs.delete(m[1]); return ok({ deleted: 1 })
    }
    if (method === 'GET') return ok(detail(s))
  }
  return err(404, 'not_found', 'Unknown mock endpoint ' + path)
}
