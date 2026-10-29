import { SQL_EXAMPLE } from '../lib/sqlExample'
import { useState } from 'react'
import { call } from '../api'
import { useMeta } from '../lib/meta'
import { CopyButton, ErrorBox, Pill, Spinner } from './ui'

type Report = { findings: { id: string; kind: string; file: string; line: number; detail: string }[]; skipped: { line: number; reason: string }[]; statements: number; note: string }


export function SqlChecker() {
  const meta = useMeta()
  const [source, setSource] = useState('')
  const [dialect, setDialect] = useState('mssql')
  const [report, setReport] = useState<Report | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const run = async () => { setBusy(true); setError(null); try { setReport(await call<Report>('POST', './api/analysis/sql', { source, dialect })) } catch(e) { setError(e instanceof Error ? e : new Error(String(e))) } finally { setBusy(false) } }
  return <div className="space-y-4"><p className="text-muted">Inspect raw SQL in Python without importing the application or connecting to a database. Dynamic SQL is reported as unresolved. Online compilation is available through the CLI only.</p>
    <label className="flex flex-col gap-2 max-w-xs">Database dialect<select className="field" value={dialect} onChange={e => { setDialect(e.target.value); setReport(null) }}>{['mssql', 'postgresql', 'mysql', 'sqlite', 'other'].map(v => <option key={v}>{v}</option>)}</select></label>
    <label className="flex flex-col gap-2">Python source (up to 200 KB)<textarea className="field mono w-full !h-60 py-2" value={source} onChange={e => { setSource(e.target.value); setReport(null) }} spellCheck={false} /></label>
    <div className="flex flex-wrap gap-2"><button className="btn btn-accent" disabled={busy || !!meta?.access?.read_only || !source.trim()} onClick={run}>Check SQL</button><button className="btn" disabled={busy} onClick={() => { setSource(SQL_EXAMPLE); setReport(null) }}>Load example</button>{report && <CopyButton text={JSON.stringify(report, null, 2)} label="Copy SQL report" />}</div>
    {busy && <div role="status"><Spinner />Checking SQL…</div>}{error && <ErrorBox error={error} />}
    {report && <div className="space-y-3" aria-live="polite"><p>{report.statements} statements · {report.findings.length} candidates · {report.skipped.length} unresolved</p><p className="text-muted">{report.note}</p>{!report.findings.length && <p>No supported lint patterns found. This does not establish SQL or schema correctness.</p>}{report.findings.map(f => <article className="card p-4 space-y-2" key={f.id}><Pill>{f.kind}</Pill><code className="block">{f.file}:{f.line}</code><p>{f.detail}</p></article>)}{report.skipped.map((s,i) => <p className="text-warn" key={i}>Line {s.line}: {s.reason}</p>)}</div>}
  </div>
}

export function RedactionPreview() {
  const meta = useMeta()
  const [sample, setSample] = useState('{"email":"example@example.test","password":"example","notes":"sample text"}')
  const [route, setRoute] = useState('/')
  const [report, setReport] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const run = async () => { setBusy(true); setError(null); try { setReport(await call('POST', './api/analysis/redaction', { sample: JSON.parse(sample), method: 'POST', route })) } catch(e) { setError(e instanceof Error ? e : new Error(String(e))) } finally { setBusy(false) } }
  return <div className="space-y-4"><p className="text-muted">Preview the current redaction rules using invented sample values. The sample is sent to this Redline instance and is not saved as a trace. Pattern-based redaction cannot recognize every sensitive value.</p><label className="flex flex-col gap-2">POST route template<input className="field" value={route} onChange={e => setRoute(e.target.value)} /></label><label className="flex flex-col gap-2">Sample JSON object<textarea className="field mono w-full !h-40" value={sample} onChange={e => setSample(e.target.value)} /></label><button className="btn btn-accent" disabled={busy || !!meta?.access?.read_only} onClick={run}>Preview redaction</button>{busy && <Spinner />}{error && <ErrorBox error={error} />}{report != null && <pre className="card p-4 overflow-x-auto" aria-live="polite">{JSON.stringify(report, null, 2)}</pre>}</div>
}
