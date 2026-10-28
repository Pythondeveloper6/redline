// Unit tests for the refactor-check helpers: `npm test` (node --test, no extra dependencies).
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  baselineFileError, baselineRequestsOf, blockedReason, importErrorMessage, MAX_BASELINE_BYTES, parseBaselineText, PERF_NOT_JUDGED, stoppedNotice, boxStats, defaultDir, fmtMs, fmtPct, ignorePathError, isEnvPlaceholder, isSecretHeader,
  parseIgnorePath, perfLabel, reportOf, reportToMarkdown, requestPasses, responseLabel, sortRequests, summarize,
  summaryCounts, validateSuite, volatileSuggestions,
} from '../src/lib/check.ts'

const st = (median, failed = 0) => ({ samples_ms: [median], median, p95: median, mean: median, stdev: 0, failed })
const resp = (verdict, status = [200, 200], extra = {}) => ({ verdict, status, diff: [], text_diff: null, ignored: [], ...extra })
const req = (name, over = {}) => ({
  name, method: 'GET', path: '/' + name,
  perf: { verdict: 'same', before: st(10), after: st(10), change_ms: 0, change_pct: 0, p_value: 0.5, reason: null },
  server: null,
  response: resp('same'),
  ...over,
})
const perf = (verdict, b, a, extra = {}) => ({ verdict, before: st(b), after: st(a), change_ms: a - b, change_pct: ((a - b) / b) * 100, p_value: 0.001, reason: null, ...extra })

test('verdict labels carry a glyph so colour is never the only signal', () => {
  assert.equal(perfLabel('slower'), '↑ slower')
  assert.equal(perfLabel('faster'), '↓ faster')
  assert.equal(perfLabel('noisy'), '≈ noisy')
  assert.equal(perfLabel('weird'), '– n/a')
  assert.equal(responseLabel('broken'), '✕ broken')
  assert.equal(responseLabel('changed'), 'Δ changed')
  assert.equal(responseLabel(null), '– n/a')
})

test('number formatting', () => {
  assert.equal(fmtPct(12.34), '+12.3%')
  assert.equal(fmtPct(-38), '−38.0%')
  assert.equal(fmtPct(0.01), '0.0%')
  assert.equal(fmtPct(null), '-')
  assert.equal(fmtMs(1500), '1.50 s')
  assert.equal(fmtMs(12.34), '12.3 ms')
  assert.equal(fmtMs(null), '-')
})

test('summarize: counts, geometric mean, queries, wins and regressions', () => {
  const rows = [
    req('a', { perf: perf('faster', 100, 50), server: { before: { queries_median: 40 }, after: { queries_median: 4 }, queries_change: -36, new_issues: [], fixed_issues: [], new_sql: [], gone_sql: [] } }),
    req('b', { perf: perf('slower', 10, 20), response: resp('changed') }),
    req('c', { perf: { ...perf('n/a', 10, 10), verdict: 'n/a' }, response: resp('broken', [200, 500]) }),
    req('d', { perf: perf('noisy', 10, 11) }),
  ]
  const s = summarize(rows)
  assert.deepEqual([s.total, s.faster, s.slower, s.noisy, s.perfNa, s.changed, s.broken, s.passed], [4, 1, 1, 1, 1, 1, 1, 2])
  // ratios 0.5, 2.0, 1.1 (n/a excluded) -> geometric mean = 1.1^(1/3)
  assert.ok(Math.abs(s.speedChangePct - (Math.pow(1.1, 1 / 3) - 1) * 100) < 1e-9)
  assert.equal(s.speedSamples, 3)
  assert.deepEqual([s.queriesBefore, s.queriesAfter, s.queriesChange], [40, 4, -36])
  assert.equal(s.queriesChangePct, -90)
  assert.deepEqual(s.wins.map(w => w.name), ['a'])
  assert.deepEqual(s.regressions.map(w => w.name), ['b']) // noisy ('d') / same are not regressions
})

test('summarize ignores requests with failed calls in the speed mean, handles empty', () => {
  const failed = req('f', { perf: { ...perf('slower', 10, 100), after: st(100, 3) } })
  assert.equal(summarize([failed]).speedChangePct, null)
  assert.equal(summarize([]).total, 0)
  assert.equal(requestPasses(failed), false)
})

test('summaryCounts accepts flat and nested shapes', () => {
  assert.deepEqual(summaryCounts({ slower: 2, counts: { faster: 3 } }), { faster: 3, slower: 2, same: 0, noisy: 0, changed: 0, broken: 0, missing: 0, passed: 0 })
  assert.equal(summaryCounts(null).slower, 0)
})

test('sortRequests: worst-first verdicts, numbers, nulls last, stable', () => {
  const rows = [
    req('a', { perf: perf('faster', 100, 50) }),
    req('b', { perf: perf('slower', 10, 20) }),
    req('c', { perf: { ...perf('n/a', 1, 1), before: null, after: null, change_pct: null } }),
    req('d', { perf: perf('same', 10, 10) }),
  ]
  assert.deepEqual(sortRequests(rows, 'perf', 'asc').map(r => r.name), ['b', 'd', 'a', 'c'])
  assert.deepEqual(sortRequests(rows, 'change', 'desc').map(r => r.name), ['b', 'd', 'a', 'c'])
  assert.deepEqual(sortRequests(rows, 'change', 'asc').map(r => r.name), ['a', 'd', 'b', 'c'])
  assert.deepEqual(sortRequests(rows, 'name', 'desc').map(r => r.name), ['d', 'c', 'b', 'a'])
  assert.deepEqual(sortRequests(rows, 'before', 'asc').map(r => r.name), ['b', 'd', 'a', 'c'])
  const byResp = [req('x'), req('y', { response: resp('broken', [200, 500]) }), req('z', { response: resp('changed') })]
  assert.deepEqual(sortRequests(byResp, 'response', 'asc').map(r => r.name), ['y', 'z', 'x'])
  assert.deepEqual(rows.map(r => r.name), ['a', 'b', 'c', 'd'], 'input is not mutated')
  assert.equal(defaultDir('name'), 'asc')
  assert.equal(defaultDir('change'), 'desc')
})

test('ignore paths: accepted syntax', () => {
  for (const p of ['meta.request_id', 'data[*].created_at', 'items[0].id', '*.id', 'a.*', '["a.b"].c', 'data["x.y"][2]', '[0]', 'a-b.c_d', '*']) {
    assert.equal(ignorePathError(p), null, p)
  }
  assert.deepEqual(parseIgnorePath('data[*].id').segments, ['data', '[*]', 'id'])
  assert.deepEqual(parseIgnorePath('["a.b"].c').segments, ['["a.b"]', 'c'])
})

test('ignore paths: rejected syntax', () => {
  for (const p of ['', '  ', '.a', 'a.', 'a..b', 'a[', 'a[x]', 'a[]', 'a]b', 'a b', 'a."b"', '[""]', 'a[0]b', "a['x']"]) {
    assert.ok(ignorePathError(p), JSON.stringify(p))
  }
})

const suite = (over = {}) => ({
  name: 's', repeat: 20, warmup: 3, timeout_s: 10,
  thresholds: { slower_pct: 10, slower_ms: 2, max_query_increase: 0 },
  requests: [{ name: 'r1', method: 'GET', path: '/a', perf: true, response: true }],
  ...over,
})

test('validateSuite: valid suite', () => {
  const v = validateSuite(suite())
  assert.deepEqual(v.fields, {})
  assert.deepEqual(v.warnings, [])
})

test('validateSuite: §19.1 ranges', () => {
  assert.ok(validateSuite(suite({ repeat: 0 })).fields.repeat)
  assert.ok(validateSuite(suite({ repeat: 1001 })).fields.repeat)
  assert.ok(validateSuite(suite({ repeat: 2.5 })).fields.repeat)
  assert.equal(validateSuite(suite({ repeat: 1000 })).fields.repeat, undefined)
  assert.ok(validateSuite(suite({ warmup: -1 })).fields.warmup)
  assert.ok(validateSuite(suite({ warmup: 101 })).fields.warmup)
  assert.equal(validateSuite(suite({ warmup: 0 })).fields.warmup, undefined)
  assert.ok(validateSuite(suite({ timeout_s: 0 })).fields.timeout_s)
  assert.ok(validateSuite(suite({ thresholds: { slower_pct: -1 } })).fields['thresholds.slower_pct'])
  assert.ok(validateSuite(suite({ thresholds: { slower_ms: -0.5 } })).fields['thresholds.slower_ms'])
  assert.ok(validateSuite(suite({ thresholds: { max_query_increase: 1.5 } })).fields['thresholds.max_query_increase'])
  assert.ok(validateSuite(suite({ name: ' ' })).fields.name)
  assert.ok(validateSuite(suite({ base_url: 'localhost:8000' })).fields.base_url)
  assert.ok(validateSuite(suite({ requests: [] })).fields.requests)
})

test('validateSuite: requests', () => {
  const r = (over) => ({ name: 'r', method: 'GET', path: '/a', ...over })
  const f = (reqs) => validateSuite(suite({ requests: reqs })).fields
  assert.ok(f([r({ method: 'POST' })])['requests.0.method'])
  assert.ok(f([r({ path: 'http://evil.test/x' })])['requests.0.path'])
  assert.ok(f([r({ path: '//evil.test' })])['requests.0.path'])
  assert.ok(f([r({ path: 'orders' })])['requests.0.path'])
  assert.ok(f([r(), r()])['requests.1.name'])
  assert.ok(f([r({ expect_status: 99 })])['requests.0.expect_status'])
  assert.equal(f([r({ expect_status: 204 })])['requests.0.expect_status'], undefined)
  assert.ok(f([r({ ignore: ['ok', 'bad..path'] })])['requests.0.ignore'])
  assert.ok(validateSuite(suite({ requests: [r({ perf: false, response: false })] })).warnings.length)
  assert.ok(validateSuite(suite({ repeat: 1000, requests: Array.from({ length: 6 }, (_, i) => r({ name: 'n' + i })) })).warnings.length)
})

test('secret headers must use ${env:NAME}', () => {
  assert.equal(isSecretHeader('Authorization'), true)
  assert.equal(isSecretHeader('x-api-key'), true)
  assert.equal(isSecretHeader('Accept'), false)
  assert.equal(isEnvPlaceholder('Bearer ${env:API_TOKEN}'), true)
  assert.equal(isEnvPlaceholder('${env:A}'), true)
  assert.equal(isEnvPlaceholder('Bearer abc'), false)
  assert.ok(validateSuite(suite({ headers: { Authorization: 'Bearer abc' } })).fields.headers)
  assert.deepEqual(validateSuite(suite({ headers: { Authorization: 'Bearer ${env:API_TOKEN}', Accept: 'application/json' } })).fields, {})
  assert.deepEqual(validateSuite(suite({ headers: { Authorization: '' } })).fields, {})
  assert.ok(validateSuite(suite({ headers: { 'bad name': 'x' } })).fields.headers)
  const perReq = validateSuite(suite({ requests: [{ name: 'r', method: 'GET', path: '/', headers: { Cookie: 'sid=1' } }] }))
  assert.ok(perReq.fields['requests.0.headers'])
})

test('blockedReason maps API error codes', () => {
  assert.match(blockedReason('read_only'), /read-only/)
  assert.match(blockedReason('feature_disabled'), /switched off/)
  assert.match(blockedReason('checks_from_ui_disabled'), /checks_from_ui/)
  assert.equal(blockedReason('run_active'), null)
  assert.equal(blockedReason(undefined), null)
})

test('boxStats', () => {
  assert.equal(boxStats([]), null)
  assert.equal(boxStats(null), null)
  assert.deepEqual(boxStats([5]), { min: 5, q1: 5, median: 5, q3: 5, max: 5, n: 1 })
  assert.deepEqual(boxStats([4, 1, 3, 2, 5]), { min: 1, q1: 2, median: 3, q3: 4, max: 5, n: 5 })
  assert.equal(boxStats([1, 2, 3, 4]).median, 2.5)
})

test('volatileSuggestions skips ones already ignored', () => {
  const bl = [{ name: 'a', volatile: ['x', 'y.z'] }, { name: 'b', volatile: ['q'] }]
  assert.deepEqual(volatileSuggestions(bl, 'a', ['x']), ['y.z'])
  assert.deepEqual(volatileSuggestions(bl, 'nope', []), [])
  assert.deepEqual(volatileSuggestions(null, 'a', []), [])
})

const report = () => ({
  format: 'redline-check-report', version: 1,
  suite: { name: 'orders | refactor', requests: [] },
  baseline: { id: 'b1', label: 'before', git: { sha: 'abcdef1234', branch: 'main', dirty: false }, created_at: 1 },
  run: { id: 'r1', git: { sha: '1234567890', branch: 'feat', dirty: true }, created_at: 2, base_url: 'http://127.0.0.1:8000' },
  requests: [
    req('list', { path: '/orders?a=1|2', perf: perf('faster', 100, 50), server: { before: { queries_median: 40 }, after: { queries_median: 4 }, queries_change: -36, new_issues: [], fixed_issues: [{ kind: 'n_plus_one', title: 'N+1 on items' }], new_sql: [], gone_sql: [{ fingerprint: 'f', sql: 'SELECT 1' }] } }),
    req('detail', {
      perf: perf('slower', 10, 20),
      response: resp('changed', [200, 200], {
        text_diff: '--- a\n+++ b\n-x\n+y',
        diff: [{ path: 'a.b', kind: 'changed', before: 1, after: 'two' }, { path: 'c', kind: 'added', before: undefined, after: { k: 1 } }], diff_total: 60,
      }),
      server: { before: null, after: null, queries_change: null, new_issues: [{ kind: 'duplicate_query' }], fixed_issues: [], new_sql: [{ fingerprint: 'g', sql: 'SELECT * FROM t | x' }], gone_sql: [] },
    }),
  ],
})

test('reportToMarkdown: summary, table, collapsible diffs, escaping', () => {
  const md = reportToMarkdown(report())
  assert.match(md, /^### Redline refactor check: orders \| refactor/)
  assert.match(md, /\*\*1\/2 passed\*\* · 1 slower · 1 faster · 1 changed/)
  assert.match(md, /queries 40 → 4 \(−36\)/)
  assert.match(md, /Baseline: before main@abcdef1 \| Current: feat@1234567\+dirty \| Target: http:\/\/127\.0\.0\.1:8000/)
  assert.match(md, /\| `GET \/orders\?a=1\\\|2` \| ↓ faster \| 100 ms → 50\.0 ms \| −50\.0% \| 40 → 4 \| ✓ same \|/)
  assert.match(md, /\| `GET \/detail` \| ↑ slower \| 10\.0 ms → 20\.0 ms \| \+100\.0% \| - \| Δ changed \|/)
  assert.match(md, /<details><summary>GET \/detail \(Δ changed, ↑ slower\)<\/summary>/)
  assert.match(md, /\| `a\.b` \| changed \| `1` \| `"two"` \|/)
  assert.match(md, /…and 58 more differences/)
  assert.match(md, /```diff\n--- a\n\+\+\+ b\n-x\n\+y\n```/)
  assert.match(md, /- New issue: duplicate_query/)
  assert.match(md, /- Fixed issue: n_plus_one N\+1 on items/)
  assert.match(md, /- New SQL: `SELECT \* FROM t \\\| x`/)
  assert.equal((md.match(/<details>/g) || []).length, (md.match(/<\/details>/g) || []).length)
  assert.ok(!/\n{3,}/.test(md))
})

test('reportToMarkdown: clean report has no details block', () => {
  const r = report(); r.requests = [req('ok')]
  const md = reportToMarkdown(r)
  assert.match(md, /\*\*1\/1 passed\*\*/)
  assert.ok(!md.includes('<details>'))
})

test('reportOf / baselineRequestsOf tolerate both result shapes', () => {
  const r = report()
  assert.equal(reportOf({ report: r }), r)
  assert.equal(reportOf({ results: r }), r)
  assert.equal(reportOf({ report: null }), null)
  assert.equal(reportOf(null), null)
  const b = [{ name: 'a' }]
  assert.equal(baselineRequestsOf({ requests: b }), b)
  assert.equal(baselineRequestsOf({ results: b }), b)
  assert.deepEqual(baselineRequestsOf({}), [])
})

test('a broken response is not judged: left out of the speed mean and the query totals, markdown says why', () => {
  const srv = (qb, qa) => ({ before: { queries_median: qb }, after: { queries_median: qa }, queries_change: null, new_issues: [], fixed_issues: [], new_sql: [], gone_sql: [] })
  const ok = req('ok', { perf: perf('faster', 100, 50), server: srv(4, 2) })
  const broken = req('boom', {
    perf: { ...perf('n/a', 100, 10), reason: 'broken', note: PERF_NOT_JUDGED },
    server: { ...srv(30, 1), not_judged: { fixed_issues: [], gone_sql: [], queries_change: -29 } },
    response: resp('broken', [200, 500]),
  })
  const s = summarize([ok, broken])
  assert.equal(s.speedSamples, 1)
  assert.ok(Math.abs(s.speedChangePct + 50) < 1e-9)
  assert.deepEqual([s.queriesBefore, s.queriesAfter, s.queriesSamples], [4, 2, 1])
  assert.deepEqual(s.wins.map(w => w.name), ['ok'])
  const md = reportToMarkdown({ suite: { name: 's' }, baseline: {}, run: {}, requests: [ok, broken] })
  assert.ok(md.includes('perf not judged: response broken'))
  // even if an older report still says "faster" for a broken response, it is not counted
  const legacy = req('legacy', { perf: perf('faster', 100, 10), response: resp('broken', [200, 500]) })
  assert.equal(summarize([legacy]).speedSamples, 0)
})

test('missing requests: own verdict, count, and they do not pass', () => {
  const m = req('new', { perf: { ...perf('n/a', 1, 1), before: null }, response: resp('missing', [null, 200]) })
  assert.equal(responseLabel('missing'), '? missing')
  assert.equal(requestPasses(m), false)
  const s = summarize([m, req('a')])
  assert.deepEqual([s.missing, s.passed, s.respNa], [1, 1, 0])
  assert.equal(summaryCounts({ missing: 2 }).missing, 2)
  assert.ok(reportToMarkdown({ suite: { name: 's' }, baseline: {}, run: {}, requests: [m] }).includes('1 missing from the baseline'))
})

test('stoppedNotice: partial results are labelled, the "no results" message only when there are none', () => {
  const run = { progress: { done: 40, total: 200 } }
  const withResults = stoppedNotice(run, true)
  assert.ok(withResults.includes('partial: stopped after 40 of 200 calls'))
  assert.ok(!withResults.includes('No results were kept'))
  const none = stoppedNotice(run, false)
  assert.ok(none.includes('No results were kept'))
  assert.ok(!none.includes('partial'))
  assert.ok(stoppedNotice({}, true).includes('partial: stopped before it finished'))
})

test('baseline import: client-side size and JSON checks, API error wording', () => {
  assert.equal(baselineFileError({ name: 'b.json', size: 1000 }), null)
  assert.equal(baselineFileError({ name: 'b.json', size: MAX_BASELINE_BYTES }), null)
  assert.match(baselineFileError({ name: 'big.json', size: MAX_BASELINE_BYTES + 1 }), /larger than 10 MiB/)
  assert.match(baselineFileError({ name: 'e.json', size: 0 }), /empty/)
  assert.equal(parseBaselineText('{"format":"redline-check-baseline"}').ok, true)
  assert.equal(parseBaselineText('nope').ok, false)
  assert.equal(parseBaselineText('[1]').ok, false)
  assert.equal(importErrorMessage('invalid_baseline', 'Not a Redline baseline (format).'), 'Not a usable baseline: Not a Redline baseline (format).')
  assert.match(importErrorMessage('payload_too_large', 'x'), /10 MiB/)
  assert.equal(importErrorMessage('boom', 'raw message'), 'raw message')
})

test('budget-only failures cannot be shown as passed or omitted from Markdown', () => {
  const row = req('budget', { budgets: [{ rule: 'max_queries', observed: 8, limit: 5, passed: false }] })
  assert.equal(requestPasses(row), false)
  assert.equal(summarize([row]).passed, 0)
  const text = reportToMarkdown({ suite: { name: 'budgets' }, requests: [row], baseline: {}, run: {} })
  assert.match(text, /1 budget failures/)
  assert.match(text, /Budget failed: max_queries/)
})

test('policy validation rejects invalid paths, identity names, non-finite tolerances and fractional counts', () => {
  const base = { name: 'rules', requests: [{ name: 'items', method: 'GET', path: '/items' }] }
  const check = rules => validateSuite({ ...base, requests: [{ ...base.requests[0], ...rules }] }).fields
  assert.ok(check({ array_keys: { 'items[': 'id' } })['requests.0.array_keys'])
  assert.ok(check({ array_keys: { items: '' } })['requests.0.array_keys'])
  assert.ok(check({ tolerances: { price: { relative: Infinity } } })['requests.0.tolerances'])
  assert.ok(check({ budgets: { max_queries: 1.5 } })['requests.0.budgets'])
  assert.equal(Object.keys(check({ array_keys: { items: 'id' }, tolerances: { total: { absolute: .01 } }, budgets: { max_queries: 5 } })).length, 0)
})
