import { SOURCE_EXAMPLE } from '../lib/sourceExample'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { call } from '../api'
import { apiChecks } from '../apiChecks'
import { useMeta } from '../lib/meta'
import { registerRoute } from '../lib/routes'
import { CopyButton, ErrorBox, Pill, Spinner, useAsync } from '../components/ui'

registerRoute({ path: '/analysis', label: 'Analysis tools', keywords: 'static analyze health coverage suppressions pytest', hint: 'Source analysis and recording quality' })

type Finding = { id: string; kind: string; file: string; line: number; detail: string; suggestion: string; limitation: string }
type SourceReport = { findings: Finding[]; errors: { file: string; line?: number; message: string }[]; files_analyzed?: number; skipped?: number; limited?: boolean }
type Coverage = { routes: { method: string; route: string; observed_paths: number; suite_inputs: number; status: string }[]; note: string; unmatched_suite_requests: string[] }
type Rule = { id: string; kind: string; route?: string; fingerprint?: string; location?: string; reason: string; expires_at?: number }
const SAMPLE = SOURCE_EXAMPLE

export default function Analysis() {
  const [tab, setTab] = useState('source')
  return <div className="space-y-4">
    <h1 className="text-[15px] font-semibold m-0">Analysis tools</h1>
    <nav className="flex flex-wrap gap-2" aria-label="Analysis tools">{[['source', 'Python source'], ['samples', 'Detector samples'], ['health', 'Recording health'], ['coverage', 'Suite coverage'], ['suppressions', 'Suppressions']].map(([key, label]) => <button key={key} className={`btn ${tab === key ? 'btn-accent' : ''}`} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
    {tab === 'source' && <Source />}{tab === 'samples' && <Samples />}{tab === 'health' && <Health />}{tab === 'coverage' && <SuiteCoverage />}{tab === 'suppressions' && <Suppressions />}
  </div>
}

function Source() {
  const meta = useMeta()
  const [source, setSource] = useState('')
  const [report, setReport] = useState<SourceReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const run = async (project: boolean) => {
    setBusy(true); setError(null)
    try { setReport(await call<SourceReport>('POST', './api/analysis/source', project ? { project: true } : { source, file: 'input.py' })) }
    catch (e) { setError(e) } finally { setBusy(false) }
  }
  return <div className="space-y-4">
    <p className="text-muted">Find syntax-based performance candidates without executing source. Project scanning uses the configured project root. UI scans require analysis_from_ui or debug mode.</p>
    <label className="flex flex-col gap-2">Python source (up to 200 KB)<textarea className="field mono w-full !h-60 py-2" value={source} onChange={e => setSource(e.target.value)} spellCheck={false} /></label>
    <div className="flex flex-wrap gap-2">
      <button className="btn btn-accent" disabled={!!meta?.access?.read_only || busy || !source.trim()} onClick={() => run(false)}>Analyze source</button>
      <button className="btn" disabled={!!meta?.access?.read_only || busy} onClick={() => run(true)}>Analyze configured project</button>
      <button className="btn" disabled={busy} onClick={() => { setSource(SAMPLE); setReport(null) }}>Load example</button>
      <label className="btn cursor-pointer">Open Python file<input className="sr-only" type="file" accept=".py" onChange={async e => { const file = e.target.files?.[0]; if (!file) return; if (file.size > 200000) { setError(new Error('Choose a Python file under 200 KB.')); return } setSource(await file.text()); setReport(null) }} /></label>
      {report && <CopyButton text={JSON.stringify(report, null, 2)} label="Copy report JSON" />}
    </div>
    {busy && <div role="status"><Spinner />Analyzing source…</div>}
    {!!error && <ErrorBox error={error instanceof Error ? error : new Error(String(error))} />}
    {report && <div className="space-y-3" aria-live="polite">
      <p>{report.findings.length} candidates · {report.errors.length} source errors{report.files_analyzed != null ? ` · ${report.files_analyzed} files analyzed · ${report.skipped ?? 0} skipped` : ''}</p>
      {report.limited && <p className="text-warn">Scan reached its file or byte limit. Coverage is incomplete.</p>}
      {report.errors.map((e, i) => <p key={i} className="text-bad">{e.file}{e.line ? `:${e.line}` : ''}: {e.message}</p>)}
      {!report.findings.length && !report.errors.length && <div className="card p-4">No supported patterns found. This does not prove the code is free of performance problems.</div>}
      {report.findings.map(f => <article key={f.id} className="card p-4 space-y-2"><div className="flex flex-wrap gap-2"><Pill>{f.kind.replaceAll('_', ' ')}</Pill><code>{f.file}:{f.line}</code></div><p className="font-medium">{f.detail}</p><p>{f.suggestion}</p><p className="text-muted">{f.limitation}</p><Link className="text-info hover:underline" to="/checks/new">Build a check to verify the fix</Link></article>)}
    </div>}
  </div>
}

function Health() {
  const d = useAsync(() => call<{ meta: { framework: string; settings: { profile_mode: string; profiler_available: boolean }; storage: { rows: number; dropped: number } }; observed_traces: number; profiled_traces: number; test_traces: number; test_requests?: { id: string; name: string; method: string; path: string }[]; warnings: string[]; note: string }>('GET', './api/analysis/health'), [])
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />
  if (!d.data) return <Spinner />
  const h = d.data
  return <div className="space-y-3"><button className="btn" onClick={d.reload}>Refresh recording health</button>
    <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">{[['Retained traces', h.meta.storage.rows], ['Observed sample', h.observed_traces], ['Profiled in sample', h.profiled_traces], ['Dropped writes', h.meta.storage.dropped]].map(([label, value]) => <div key={label} className="card p-4"><div className="text-muted">{label}</div><div className="text-xl num">{value}</div></div>)}</div>
    {h.warnings.map(w => <p key={w} className="text-warn">{w}</p>)}<p>{h.note}</p>
    <p>Framework: {h.meta.framework} · Profile mode: {h.meta.settings.profile_mode} · Profiler installed: {h.meta.settings.profiler_available ? 'yes' : 'no'} · Test traces in sample: {h.test_traces}</p>
    {!!h.test_requests?.length && <div className="card p-3 space-y-2"><h2 className="font-medium">Recorded test requests</h2>{h.test_requests.map(t => <div key={t.id}><Link className="text-info hover:underline" to={`/requests/${t.id}?tab=issues`}>{t.name}: {t.method} {t.path}</Link></div>)}</div>}
    <p className="text-muted">Open a request’s time breakdown to inspect span coverage, truncation and dropped logs. Test assertions and check budgets remain available in <Link to="/checks" className="text-info">Checks</Link>.</p>
  </div>
}

function SuiteCoverage() {
  const suites = useAsync(() => apiChecks.suites(), [])
  const [selected, setSelected] = useState('')
  const d = useAsync(() => selected ? call<Coverage>('GET', `./api/analysis/coverage?suite_id=${encodeURIComponent(selected)}`) : Promise.resolve(null), [selected])
  return <div className="space-y-3"><label className="flex flex-col gap-2 max-w-md">Check suite<select className="field" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select a suite</option>{suites.data?.items.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
    {suites.error ? <ErrorBox error={suites.error} /> : suites.data?.items.length === 0 ? <p>No suites yet. <Link to="/checks/new" className="text-info">Create a suite</Link>.</p> : null}
    {d.error ? <ErrorBox error={d.error} onRetry={d.reload} /> : d.loading ? <Spinner /> : d.data && <><p className="text-muted">{d.data.note}</p>
      <div className="card overflow-x-auto"><table className="w-full"><thead><tr><th className="th">Route</th><th className="th">Observed paths</th><th className="th">Suite inputs</th><th className="th">Coverage</th></tr></thead><tbody>{d.data.routes.map(r => <tr key={`${r.method}:${r.route}`} className="border-t"><td className="td mono">{r.method} {r.route}</td><td className="td">{r.observed_paths}</td><td className="td">{r.suite_inputs}</td><td className="td"><Pill tone={r.status === 'missing' ? 'bad' : r.status === 'single_input' ? 'warn' : 'ok'}>{r.status.replaceAll('_', ' ')}</Pill></td></tr>)}</tbody></table></div>
      {!!d.data.unmatched_suite_requests.length && <p>Suite requests absent from observed traffic: {d.data.unmatched_suite_requests.join(', ')}</p>}</>}
  </div>
}

function Suppressions() {
  const meta = useMeta()
  const d = useAsync(() => call<{ items: Rule[] }>('GET', './api/suppressions'), [])
  const [kind, setKind] = useState('')
  const [route, setRoute] = useState('')
  const [reason, setReason] = useState('')
  const [fingerprint, setFingerprint] = useState('')
  const [location, setLocation] = useState('')
  const [days, setDays] = useState('30')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const mutate = async (method: string, path: string, body?: unknown) => { setBusy(true); setError(null); try { await call(method, path, body); d.reload() } catch(e) { setError(e) } finally { setBusy(false) } }
  return <div className="space-y-3"><p>Suppressions acknowledge accepted findings. Raw findings remain available, and test/check policies still evaluate them.</p>
    <form className="card p-4 space-y-3 max-w-xl" onSubmit={e => { e.preventDefault(); mutate('POST', './api/suppressions', { kind, ...(route ? { route } : {}), ...(fingerprint ? { fingerprint } : {}), ...(location ? { location } : {}), reason, ...(days ? { expires_at: Date.now() / 1000 + Number(days) * 86400 } : {}) }) }}>
      <label className="flex flex-col gap-1">Issue kind<input className="field" required value={kind} onChange={e => setKind(e.target.value)} placeholder="n_plus_one" /></label>
      <label className="flex flex-col gap-1">Exact route (optional)<input className="field" value={route} onChange={e => setRoute(e.target.value)} placeholder="/reports/" /></label>
      <label className="flex flex-col gap-1">Exact query fingerprint (optional)<input className="field mono" value={fingerprint} onChange={e => setFingerprint(e.target.value)} /></label>
      <label className="flex flex-col gap-1">Exact callsite (optional)<input className="field mono" placeholder="shop/views.py:41" value={location} onChange={e => setLocation(e.target.value)} /></label>
      <p className="text-muted">Choose at least one route, fingerprint or callsite. Multiple selectors must all match.</p>
      <label className="flex flex-col gap-1">Reason<input className="field" required maxLength={500} value={reason} onChange={e => setReason(e.target.value)} /></label>
      <label className="flex flex-col gap-1">Expires in days (blank for no expiry)<input type="number" min="1" step="1" className="field" value={days} onChange={e => setDays(e.target.value)} /></label>
      <button className="btn btn-accent" disabled={!!meta?.access?.read_only || busy || !(route || fingerprint || location)}>Save suppression</button>
    </form>
    {!!error && <ErrorBox error={error instanceof Error ? error : new Error(String(error))} />}{d.error ? <ErrorBox error={d.error} onRetry={d.reload} /> : d.loading ? <Spinner /> : d.data?.items.map(rule => <div key={rule.id} className="card p-3 space-y-2"><div className="flex flex-wrap gap-2"><code>{rule.kind}</code><code>{[rule.route, rule.fingerprint, rule.location].filter(Boolean).join(' · ')}</code><Pill>{rule.expires_at && rule.expires_at <= Date.now()/1000 ? 'expired' : 'active'}</Pill></div><p>{rule.reason}</p><p className="text-muted">{rule.expires_at ? `Expires ${new Date(rule.expires_at*1000).toLocaleString()}` : 'No expiry'}</p><button className="btn" disabled={!!meta?.access?.read_only || busy} onClick={() => mutate('DELETE', `./api/suppressions/${rule.id}`)}>Remove suppression</button></div>)}
    {d.data?.items.length === 0 && <p>No suppressions saved.</p>}
  </div>
}


function Samples() {
  const meta = useMeta()
  const [items, setItems] = useState<{ id: string; case: string; issue_kinds: string[] }[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const load = async () => { setBusy(true); setError(null); try { const result = await call<{items: typeof items}>('POST', './api/analysis/demo', {}); setItems(result.items) } catch (e) { setError(e instanceof Error ? e : new Error(String(e))) } finally { setBusy(false) } }
  return <div className="space-y-3"><p>Record nine clearly marked synthetic traces to inspect detector evidence, time breakdowns and thresholds. These samples do not execute SQL, call services or represent measured application performance.</p>
    <p className="text-muted">For real application traffic, use the demo shop’s /patterns endpoint. Synthetic traces cannot verify a real refactor or be replayed as application endpoints.</p>
    <button className="btn btn-accent" disabled={!!meta?.access?.read_only || busy} onClick={load}>Record synthetic detector samples</button>
    {busy && <Spinner />}{error && <ErrorBox error={error} />}
    {items.map(item => <div key={item.id} className="card p-3"><Link className="text-info hover:underline" to={`/requests/${item.id}?tab=issues`}>Open {item.case.replaceAll('_', ' ')} sample</Link><p className="text-muted">{item.issue_kinds.join(', ') || 'No findings at the server’s current thresholds'}</p></div>)}
  </div>
}
