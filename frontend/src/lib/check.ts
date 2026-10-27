// Pure helpers for the refactor-check UI (CONTRACT §19). No React, no DOM: unit-tested with `node --test`.
import type {
  BaselineRequest, CheckReport, CheckSuite, CheckSummary, PerfVerdict, ReportRequest, ResponseVerdict,
} from '../types.ts'

// ---- verdicts -----------------------------------------------------------------------------------
export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral'
export interface VerdictStyle { label: string; glyph: string; tone: Tone; help: string }

export const PERF_VERDICT: Record<PerfVerdict, VerdictStyle> = {
  faster: { label: 'faster', glyph: '↓', tone: 'ok', help: 'Median latency dropped significantly' },
  slower: { label: 'slower', glyph: '↑', tone: 'bad', help: 'Median latency (or query count) rose beyond the thresholds' },
  same: { label: 'same', glyph: '=', tone: 'neutral', help: 'No significant change' },
  noisy: { label: 'noisy', glyph: '≈', tone: 'warn', help: 'Too much run-to-run variance to judge; raise repeat' },
  'n/a': { label: 'n/a', glyph: '–', tone: 'neutral', help: 'Not measured' },
}
export const RESPONSE_VERDICT: Record<ResponseVerdict, VerdictStyle> = {
  same: { label: 'same', glyph: '✓', tone: 'ok', help: 'Response is identical outside ignored fields' },
  changed: { label: 'changed', glyph: 'Δ', tone: 'warn', help: 'Status, content type or body differs' },
  broken: { label: 'broken', glyph: '✕', tone: 'bad', help: 'Now failing (4xx/5xx), unexpected status or too many failed calls' },
  'n/a': { label: 'n/a', glyph: '–', tone: 'neutral', help: 'Not compared' },
}
export const perfStyle = (v: string | null | undefined): VerdictStyle => PERF_VERDICT[v as PerfVerdict] ?? PERF_VERDICT['n/a']
export const responseStyle = (v: string | null | undefined): VerdictStyle => RESPONSE_VERDICT[v as ResponseVerdict] ?? RESPONSE_VERDICT['n/a']
/** Text label including the glyph, e.g. "↑ slower" (colour is never the only signal). */
export const perfLabel = (v: string | null | undefined) => { const s = perfStyle(v); return `${s.glyph} ${s.label}` }
export const responseLabel = (v: string | null | undefined) => { const s = responseStyle(v); return `${s.glyph} ${s.label}` }

/** A request passes when it is neither slower, changed nor broken (the default `--fail-on`). */
export const requestPasses = (r: ReportRequest) => r.perf.verdict !== 'slower' && r.response.verdict !== 'changed' && r.response.verdict !== 'broken'

// ---- numbers --------------------------------------------------------------------------------------
export function fmtMs(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '-'
  if (v === 0) return '0 ms'
  if (Math.abs(v) >= 1000) return (v / 1000).toFixed(2) + ' s'
  if (Math.abs(v) >= 100) return Math.round(v) + ' ms'
  if (Math.abs(v) >= 10) return v.toFixed(1) + ' ms'
  return v.toFixed(2) + ' ms'
}
/** Signed percent with a real minus sign: "+12.4%", "−38.0%", "0.0%". */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '-'
  const r = Number(v.toFixed(digits))
  if (r === 0) return (0).toFixed(digits) + '%'
  return (r > 0 ? '+' : '−') + Math.abs(r).toFixed(digits) + '%'
}
export function fmtSigned(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '-'
  return v > 0 ? `+${v}` : v < 0 ? `−${Math.abs(v)}` : '0'
}
export const gitShort = (g: { sha?: string | null; branch?: string | null; dirty?: boolean | null } | null | undefined): string => {
  if (!g || (!g.sha && !g.branch)) return ''
  return [g.branch, g.sha ? g.sha.slice(0, 7) : null].filter(Boolean).join('@') + (g.dirty ? '+dirty' : '')
}

// ---- summary --------------------------------------------------------------------------------------
export interface DerivedSummary {
  total: number
  faster: number; slower: number; same: number; noisy: number; perfNa: number
  respSame: number; changed: number; broken: number; respNa: number
  passed: number
  /** Geometric mean of after/before median ratios as a percent (negative = faster); null when nothing qualifies. */
  speedChangePct: number | null
  speedSamples: number
  queriesBefore: number; queriesAfter: number; queriesChange: number; queriesChangePct: number | null; queriesSamples: number
  wins: { name: string; pct: number }[]
  regressions: { name: string; pct: number }[]
}

/** Derives the §19.4 summary from the per-request results (the UI never trusts a separate summary object). */
export function summarize(requests: ReportRequest[]): DerivedSummary {
  const s: DerivedSummary = {
    total: requests.length, faster: 0, slower: 0, same: 0, noisy: 0, perfNa: 0, respSame: 0, changed: 0, broken: 0, respNa: 0, passed: 0,
    speedChangePct: null, speedSamples: 0, queriesBefore: 0, queriesAfter: 0, queriesChange: 0, queriesChangePct: null, queriesSamples: 0, wins: [], regressions: [],
  }
  let logSum = 0
  for (const r of requests) {
    const pv = r.perf.verdict
    if (pv === 'faster') s.faster++; else if (pv === 'slower') s.slower++; else if (pv === 'same') s.same++; else if (pv === 'noisy') s.noisy++; else s.perfNa++
    const rv = r.response.verdict
    if (rv === 'same') s.respSame++; else if (rv === 'changed') s.changed++; else if (rv === 'broken') s.broken++; else s.respNa++
    if (requestPasses(r)) s.passed++
    const b = r.perf.before, a = r.perf.after
    if (pv !== 'n/a' && b && a && b.median > 0 && a.median > 0 && !b.failed && !a.failed) {
      logSum += Math.log(a.median / b.median); s.speedSamples++
      if (r.perf.change_pct != null) {
        // Only significant changes count as wins/regressions (same as the CLI summary).
        if (pv === 'faster') s.wins.push({ name: r.name, pct: r.perf.change_pct })
        else if (pv === 'slower') s.regressions.push({ name: r.name, pct: r.perf.change_pct })
      }
    }
    const qb = r.server?.before?.queries_median, qa = r.server?.after?.queries_median
    if (qb != null && qa != null) { s.queriesBefore += qb; s.queriesAfter += qa; s.queriesSamples++ }
  }
  if (s.speedSamples) s.speedChangePct = (Math.exp(logSum / s.speedSamples) - 1) * 100
  s.queriesChange = s.queriesAfter - s.queriesBefore
  if (s.queriesSamples && s.queriesBefore > 0) s.queriesChangePct = (s.queriesChange / s.queriesBefore) * 100
  s.wins.sort((x, y) => x.pct - y.pct); s.regressions.sort((x, y) => y.pct - x.pct)
  s.wins = s.wins.slice(0, 5); s.regressions = s.regressions.slice(0, 5)
  return s
}

/** Counts from a run-list summary object (flat or under `counts`); missing fields are 0. */
export function summaryCounts(sm: CheckSummary | null | undefined) {
  const c = { ...(sm ?? {}), ...(sm?.counts ?? {}) } as Record<string, number | undefined>
  const n = (k: string) => (typeof c[k] === 'number' ? (c[k] as number) : 0)
  return { faster: n('faster'), slower: n('slower'), same: n('same'), noisy: n('noisy'), changed: n('changed'), broken: n('broken'), passed: n('passed') }
}

// ---- sorting --------------------------------------------------------------------------------------
export type SortKey = 'name' | 'perf' | 'before' | 'after' | 'change' | 'queries' | 'response'
export type SortDir = 'asc' | 'desc'
const PERF_RANK: Record<string, number> = { slower: 0, noisy: 1, same: 2, faster: 3, 'n/a': 4 }
const RESP_RANK: Record<string, number> = { broken: 0, changed: 1, same: 2, 'n/a': 3 }

function sortValue(r: ReportRequest, k: SortKey): string | number | null {
  switch (k) {
    case 'name': return r.name.toLowerCase()
    case 'perf': return PERF_RANK[r.perf.verdict] ?? 9
    case 'before': return r.perf.before?.median ?? null
    case 'after': return r.perf.after?.median ?? null
    case 'change': return r.perf.change_pct
    case 'queries': return r.server?.queries_change ?? null
    case 'response': return RESP_RANK[r.response.verdict] ?? 9
  }
}
/**
 * Stable sort. `perf`/`response` ascending = worst first (slower/broken on top). Null values always sort last,
 * whatever the direction.
 */
export function sortRequests(rows: ReportRequest[], key: SortKey, dir: SortDir): ReportRequest[] {
  const m = dir === 'asc' ? 1 : -1
  return rows.map((r, i) => ({ r, i, v: sortValue(r, key) })).sort((a, b) => {
    if (a.v == null && b.v == null) return a.i - b.i
    if (a.v == null) return 1
    if (b.v == null) return -1
    const c = typeof a.v === 'string' ? a.v.localeCompare(b.v as string) : (a.v as number) - (b.v as number)
    return c !== 0 ? c * m : a.i - b.i
  }).map(x => x.r)
}
/** Default direction when a column header is first clicked: verdict and numeric "badness" columns start worst-first. */
export const defaultDir = (k: SortKey): SortDir => (k === 'name' ? 'asc' : k === 'perf' || k === 'response' ? 'asc' : 'desc')

// ---- ignore-path validation (§19.1 JSON path syntax) --------------------------------------------
export type PathCheck = { ok: true; segments: string[] } | { ok: false; error: string }
/**
 * Validates a JSON path: dotted keys, `[n]` indexes, `[*]` any index, `*` any key; keys containing dots or
 * brackets are written `["a.b"]`.
 */
export function parseIgnorePath(input: string): PathCheck {
  const s = input.trim()
  if (!s) return { ok: false, error: 'Path is empty' }
  const segs: string[] = []
  let i = 0
  let first = true
  const key = () => {
    const m = /^[^.[\]"'\s]+/.exec(s.slice(i))
    if (!m) return false
    segs.push(m[0]); i += m[0].length
    return true
  }
  while (i < s.length) {
    const c = s[i]
    if (c === '[') {
      const close = s[i + 1] === '"' ? s.indexOf('"]', i + 2) : s.indexOf(']', i)
      if (close < 0) return { ok: false, error: `Unclosed "[" at position ${i + 1}` }
      if (s[i + 1] === '"') {
        const k = s.slice(i + 2, close)
        if (!k) return { ok: false, error: 'Empty quoted key ["" ]' }
        segs.push(`["${k}"]`); i = close + 2
      } else {
        const inner = s.slice(i + 1, close)
        if (inner !== '*' && !/^\d+$/.test(inner)) return { ok: false, error: `"[${inner}]" must be an index, "*", or a quoted key like ["a.b"]` }
        segs.push(`[${inner}]`); i = close + 1
      }
    } else if (c === '.') {
      if (first) return { ok: false, error: 'Path cannot start with "."' }
      i++
      if (i >= s.length) return { ok: false, error: 'Path cannot end with "."' }
      if (s[i] === '.' || s[i] === '[') return { ok: false, error: `Unexpected "${s[i]}" after "."` }
      if (!key()) return { ok: false, error: `Invalid key at position ${i + 1}` }
    } else if (c === ']' || c === '"' || c === "'" || /\s/.test(c)) {
      return { ok: false, error: c === ']' ? 'Unexpected "]"' : /\s/.test(c) ? 'Spaces are not allowed (quote the key: ["a b"])' : `Unexpected ${c} (use ["key"])` }
    } else {
      if (!first) return { ok: false, error: `Expected "." or "[" at position ${i + 1}` }
      if (!key()) return { ok: false, error: `Invalid key at position ${i + 1}` }
    }
    first = false
  }
  return { ok: true, segments: segs }
}
export const ignorePathError = (p: string): string | null => { const r = parseIgnorePath(p); return r.ok ? null : r.error }

// ---- suite validation (§19.1 ranges) ------------------------------------------------------------
export const LIMITS = { repeat: [1, 1000], warmup: [0, 100], maxRequests: 200, maxTimedCalls: 5000 } as const
export const READ_ONLY_METHODS = ['GET', 'HEAD', 'OPTIONS'] as const
const SECRET_RE = /^(authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|x-csrf-?token|x-xsrf-token|api-key|apikey|x-access-token)$/i
export const isSecretHeader = (name: string) => SECRET_RE.test(name.trim()) || /(token|secret|password|passwd|api[-_]?key)/i.test(name)
export const isEnvPlaceholder = (v: string) => /^\s*(Bearer\s+|Basic\s+|Token\s+)?\$\{env:[A-Za-z_][A-Za-z0-9_]*\}\s*$/.test(v)
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

export interface SuiteErrors {
  /** Field-level messages keyed by e.g. `name`, `repeat`, `thresholds.slower_pct`, `requests.2.path`, `headers`. */
  fields: Record<string, string>
  warnings: string[]
}
const intIn = (v: unknown, lo: number, hi: number) => typeof v === 'number' && Number.isInteger(v) && v >= lo && v <= hi

function validateHeaders(h: Record<string, string> | undefined, key: string, f: Record<string, string>) {
  for (const [k, v] of Object.entries(h ?? {})) {
    if (!HEADER_NAME.test(k)) { f[key] = `"${k}" is not a valid header name`; return }
    if (isSecretHeader(k) && v.trim() && !isEnvPlaceholder(v)) { f[key] = `${k} looks like a secret: use \${env:NAME} instead of a literal value (literals are stored in the suite)`; return }
  }
}

/** Validates a suite against §19.1; the server stays the authority (unknown keys etc.). */
export function validateSuite(s: CheckSuite): SuiteErrors {
  const f: Record<string, string> = {}
  const warnings: string[] = []
  if (!s.name?.trim()) f.name = 'Name is required'
  if (s.base_url && !/^https?:\/\/[^\s/]+/.test(s.base_url.trim())) f.base_url = 'Must start with http:// or https://'
  if (s.timeout_s != null && !(typeof s.timeout_s === 'number' && s.timeout_s > 0 && s.timeout_s <= 3600)) f.timeout_s = 'Must be greater than 0 (seconds)'
  if (s.repeat != null && !intIn(s.repeat, ...LIMITS.repeat)) f.repeat = `Whole number from ${LIMITS.repeat[0]} to ${LIMITS.repeat[1]}`
  if (s.warmup != null && !intIn(s.warmup, ...LIMITS.warmup)) f.warmup = `Whole number from ${LIMITS.warmup[0]} to ${LIMITS.warmup[1]}`
  const t = s.thresholds ?? {}
  if (t.slower_pct != null && !(typeof t.slower_pct === 'number' && t.slower_pct >= 0)) f['thresholds.slower_pct'] = 'Must be 0 or more'
  if (t.slower_ms != null && !(typeof t.slower_ms === 'number' && t.slower_ms >= 0)) f['thresholds.slower_ms'] = 'Must be 0 or more'
  if (t.max_query_increase != null && !intIn(t.max_query_increase, 0, 1_000_000)) f['thresholds.max_query_increase'] = 'Whole number, 0 or more'
  validateHeaders(s.headers, 'headers', f)
  if (!s.requests.length) f.requests = 'Add at least one request'
  if (s.requests.length > LIMITS.maxRequests) f.requests = `At most ${LIMITS.maxRequests} requests`
  const seen = new Set<string>()
  s.requests.forEach((r, i) => {
    const p = `requests.${i}.`
    if (!r.name.trim()) f[p + 'name'] = 'Name is required'
    else if (seen.has(r.name.trim())) f[p + 'name'] = 'Name must be unique within the suite'
    seen.add(r.name.trim())
    if (!(READ_ONLY_METHODS as readonly string[]).includes(r.method)) f[p + 'method'] = 'Only GET, HEAD and OPTIONS can be checked'
    if (!r.path.startsWith('/') || r.path.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(r.path)) f[p + 'path'] = 'Must be a path starting with a single "/" (absolute URLs are refused)'
    else if (/\s/.test(r.path)) f[p + 'path'] = 'Must not contain spaces'
    if (r.expect_status != null && !intIn(r.expect_status, 100, 599)) f[p + 'expect_status'] = 'HTTP status from 100 to 599'
    const bad = (r.ignore ?? []).map(ignorePathError).find(Boolean)
    if (bad) f[p + 'ignore'] = bad
    validateHeaders(r.headers, p + 'headers', f)
    if (r.perf === false && r.response === false) warnings.push(`"${r.name}" has both Perf and Response off, so it checks nothing.`)
  })
  const calls = s.requests.filter(r => r.perf !== false).length * (s.repeat ?? 20)
  if (calls > LIMITS.maxTimedCalls) warnings.push(`${calls.toLocaleString()} timed calls exceed the ${LIMITS.maxTimedCalls.toLocaleString()} call cap for UI runs; the server may refuse to start it.`)
  return { fields: f, warnings }
}

// ---- API error -> explanation -----------------------------------------------------------------------
/** Why a run could not be started, from the API error code (or null for an unrelated error). */
export function blockedReason(code: string | undefined): string | null {
  switch (code) {
    case 'read_only': return 'This Redline instance is read-only, so runs cannot be started from the UI.'
    case 'feature_disabled': return 'The checks feature is switched off on this server.'
    case 'checks_disabled':
    case 'checks_from_ui_disabled':
    case 'forbidden':
      return 'Starting checks from the UI is turned off (checks_from_ui). It is on by default only in debug mode; enable it in the Redline settings, or run `redline check record` / `redline check compare` from a shell.'
    default: return null
  }
}

// ---- box plot ---------------------------------------------------------------------------------------
export interface BoxStats { min: number; q1: number; median: number; q3: number; max: number; n: number }
const quantile = (sorted: number[], q: number) => {
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos), hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}
export function boxStats(samples: number[] | null | undefined): BoxStats | null {
  const v = (samples ?? []).filter(x => Number.isFinite(x)).sort((a, b) => a - b)
  if (!v.length) return null
  return { min: v[0], q1: quantile(v, 0.25), median: quantile(v, 0.5), q3: quantile(v, 0.75), max: v[v.length - 1], n: v.length }
}

// ---- volatile suggestions -----------------------------------------------------------------------
/** Volatile paths recorded in a baseline for `requestName` that are not yet ignored. */
export function volatileSuggestions(baseline: Pick<BaselineRequest, 'name' | 'volatile'>[] | null | undefined, requestName: string, ignored: string[]): string[] {
  const row = baseline?.find(b => b.name === requestName)
  const have = new Set(ignored)
  return (row?.volatile ?? []).filter(p => !have.has(p))
}

// ---- Markdown -----------------------------------------------------------------------------------------
const cell = (s: string) => s.replace(/\|/g, '\\|').replace(/\r?\n/g, ' ')
const code = (s: string) => '`' + s.replace(/`/g, "'") + '`'
const val = (v: unknown) => { const s = v === undefined ? 'undefined' : JSON.stringify(v); return s.length > 80 ? s.slice(0, 77) + '...' : s }

/** Builds a PR-comment style Markdown report (summary line, table, collapsible diffs) from a report. */
export function reportToMarkdown(rep: CheckReport): string {
  const sm = summarize(rep.requests)
  const out: string[] = []
  out.push(`### Redline refactor check: ${rep.suite?.name ?? 'suite'}`, '')
  const parts = [`**${sm.passed}/${sm.total} passed**`]
  if (sm.slower) parts.push(`${sm.slower} slower`)
  if (sm.faster) parts.push(`${sm.faster} faster`)
  if (sm.changed) parts.push(`${sm.changed} changed`)
  if (sm.broken) parts.push(`${sm.broken} broken`)
  if (sm.noisy) parts.push(`${sm.noisy} noisy`)
  if (sm.speedChangePct != null) parts.push(`overall speed ${fmtPct(sm.speedChangePct)}`)
  if (sm.queriesSamples) parts.push(`queries ${sm.queriesBefore} → ${sm.queriesAfter} (${fmtSigned(sm.queriesChange)})`)
  out.push(parts.join(' · '), '')
  const bl = [rep.baseline?.label, gitShort(rep.baseline?.git)].filter(Boolean).join(' ')
  const cur = gitShort(rep.run?.git)
  out.push(`Baseline: ${bl || 'unnamed'} | Current: ${cur || 'working tree'} | Target: ${rep.run?.base_url ?? ''}`, '')
  out.push('| Request | Perf | Median before → after | Δ | Queries | Response |', '| --- | --- | --- | ---: | --- | --- |')
  for (const r of rep.requests) {
    const q = r.server?.before && r.server?.after ? `${r.server.before.queries_median} → ${r.server.after.queries_median}` : '-'
    out.push(`| ${code(cell(`${r.method} ${r.path}`))} | ${perfLabel(r.perf.verdict)} | ${fmtMs(r.perf.before?.median)} → ${fmtMs(r.perf.after?.median)} | ${fmtPct(r.perf.change_pct)} | ${q} | ${responseLabel(r.response.verdict)} |`)
  }
  const detail = rep.requests.filter(r => r.response.diff.length || r.response.text_diff || r.server?.new_issues.length || r.server?.fixed_issues.length || r.server?.new_sql.length)
  for (const r of detail) {
    out.push('', `<details><summary>${cell(r.method + ' ' + r.path)} (${responseLabel(r.response.verdict)}, ${perfLabel(r.perf.verdict)})</summary>`, '')
    if (r.response.diff.length) {
      out.push('| Path | Kind | Before | After |', '| --- | --- | --- | --- |')
      for (const d of r.response.diff) out.push(`| ${code(cell(d.path))} | ${d.kind} | ${code(cell(val(d.before)))} | ${code(cell(val(d.after)))} |`)
      const total = r.response.diff_total ?? r.response.diff.length
      if (total > r.response.diff.length) out.push('', `…and ${total - r.response.diff.length} more differences`)
      out.push('')
    }
    if (r.response.text_diff) out.push('```diff', r.response.text_diff.replace(/```/g, "'''"), '```', '')
    for (const i of r.server?.new_issues ?? []) out.push(`- New issue: ${i.kind}${i.title ? ' ' + i.title : ''}`)
    for (const i of r.server?.fixed_issues ?? []) out.push(`- Fixed issue: ${i.kind}${i.title ? ' ' + i.title : ''}`)
    for (const q of r.server?.new_sql ?? []) out.push(`- New SQL: ${code(cell(q.sql.slice(0, 120)))}`)
    for (const q of r.server?.gone_sql ?? []) out.push(`- Gone SQL: ${code(cell(q.sql.slice(0, 120)))}`)
    out.push('', '</details>')
  }
  return out.join('\n').replace(/\n{3,}/g, '\n\n') + '\n'
}

/** Report for a compare run detail, tolerating a backend that returns it under `report` or `results`. */
export function reportOf(run: { report?: CheckReport | null; results?: unknown } | null | undefined): CheckReport | null {
  if (!run) return null
  if (run.report && Array.isArray(run.report.requests)) return run.report
  const r = run.results as { requests?: unknown } | undefined
  if (r && !Array.isArray(r) && Array.isArray(r.requests)) return r as CheckReport
  return null
}
/** Recorded requests of a baseline run detail (`requests`, or an array under `results`). */
export function baselineRequestsOf(run: { requests?: BaselineRequest[] | null; results?: unknown } | null | undefined): BaselineRequest[] {
  if (!run) return []
  if (Array.isArray(run.requests)) return run.requests
  if (Array.isArray(run.results)) return run.results as BaselineRequest[]
  return []
}
