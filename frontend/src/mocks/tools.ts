import { SQL_EXAMPLE, SQL_EXAMPLE_REPORT } from '../lib/sqlExample'
import type { Trace } from '../types'
import { SOURCE_EXAMPLE, SOURCE_REPORT } from '../lib/sourceExample'
import { DETECTOR_SAMPLES } from './detectorSamples'
import { mockChecks } from './checks'

type Rule = { id: string; kind: string; reason: string; route?: string; fingerprint?: string; location?: string; expires_at?: number }
const rules: Rule[] = []
let serial = 0

export async function mockTools(method: string, path: string, params: URLSearchParams, body: unknown, traces: Trace[]) {
  const ok = (json: unknown, status = 200) => ({ status, json })
  const error = (status: number, message: string) => ok({ error: { code: 'demo_mode', message } }, status)
  const ro = new URLSearchParams(location.search).get('ro') === '1'
  if (!path.startsWith('api/analysis/') && !path.startsWith('api/suppressions')) return null
  if (method !== 'GET' && ro) return error(403, 'This demo is read-only.')
  if (path === 'api/analysis/sql' && method === 'POST') {
    const b = body as {source?: string; dialect?: string}
    return b.source === SQL_EXAMPLE && b.dialect === 'mssql' ? ok(SQL_EXAMPLE_REPORT) : error(501, 'Demo mode supports the fixed SQL example. Use a running backend for arbitrary source.')
  }
  if (path === 'api/analysis/redaction') return error(501, 'A running backend is required to preview its actual redaction policy.')
  if (path === 'api/analysis/source' && method === 'POST') {
    const b = body as { source?: string }
    return b.source === SOURCE_EXAMPLE ? ok(SOURCE_REPORT) : error(501, 'Demo mode supports Load example. Use a running backend to analyze other source or a project.')
  }
  if (path === 'api/analysis/demo' && method === 'POST') {
    const samples = structuredClone(DETECTOR_SAMPLES) as Trace[]
    for (const trace of samples) { trace.id += '_' + ++serial; trace.started_at = Date.now()/1000 }
    traces.unshift(...samples)
    return ok({ synthetic: true, items: samples.map(t => ({ id: t.id, case: t.tags.sample_case, issue_kinds: t.issues.map(i => i.kind) })) }, 201)
  }
  if (path === 'api/analysis/trace') return error(501, 'Threshold evaluation requires a running backend; demo samples show the generated default findings.')
  if (path === 'api/analysis/database') return ok({ findings: [{ kind: 'wide_columns', title: 'Large column types selected', detail: 'Illustrative catalog result: review whether the body column is needed.', columns: ['body'] }], indexes: [], limitations: ['Demo catalog data. Use a running backend for inspection.'] })
  if (path === 'api/analysis/health') return ok({ meta: { framework: 'django', storage: { rows: traces.length, dropped: 0 }, settings: { profile_mode: 'opt_in', profiler_available: true } }, observed_traces: Math.min(traces.length, 200), profiled_traces: traces.slice(0, 200).filter(t => t.has_profile).length, test_traces: 0, integrations: [{ name: 'httpx', installed: true, module_loaded: true, instrumented: true }, { name: 'redis', installed: true, module_loaded: false, instrumented: false }, { name: 'boto3', installed: false, module_loaded: false, instrumented: false }], warnings: [], note: 'Illustrative development data; integration status does not confirm that calls were observed.' })
  if (path === 'api/analysis/coverage') {
    const saved = await mockChecks('GET', `api/checks/suites/${params.get('suite_id')}`, new URLSearchParams(), null)
    const suite = (saved?.json as { suite?: { requests: { method: string; path: string }[] } })?.suite
    return ok({ routes: (suite?.requests ?? []).map(r => ({ method: r.method, route: r.path, observed_paths: 1, suite_inputs: 1, status: 'single_input' })), unmatched_suite_requests: [], note: 'Illustrative single-input coverage in demo mode. A running backend compares actual retained traffic.' })
  }
  if (path === 'api/suppressions') {
    if (method === 'GET') return ok({ items: rules })
    if (method === 'POST') { const rule = { ...(body as Omit<Rule, 'id'>), id: (++serial).toString(16).padStart(32, '0') }; rules.push(rule); return ok(rule, 201) }
  }
  if (path.startsWith('api/suppressions/') && method === 'DELETE') { const index = rules.findIndex(r => r.id === path.split('/').pop()); if (index >= 0) rules.splice(index, 1); return ok({ deleted: true }) }
  return null
}

export function mockSuppressed(kind: string, routes: string[]) {
  return routes.length > 0 && routes.every(route => rules.some(rule => rule.kind === kind && rule.route === route && (!rule.expires_at || rule.expires_at > Date.now()/1000)))
}
