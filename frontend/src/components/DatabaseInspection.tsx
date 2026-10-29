import { useState } from 'react'
import { call } from '../api'
import { useMeta } from '../lib/meta'
import { Modal } from './Modal'
import { CopyButton, ErrorBox, Pill, Spinner } from './ui'

type Report = { table?: string; findings: { kind: string; title: string; detail: string; suggestion?: string; columns?: string[]; column?: string; expression?: string }[]; indexes?: { name: string; columns: string[]; included_columns?: string[]; kind: string }[]; limitations: string[] }

export function DatabaseInspection({ traceId, spanId }: { traceId: string; spanId: string }) {
  const meta = useMeta()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const run = async () => { setOpen(true); setBusy(true); setError(null); try { setReport(await call('POST', './api/analysis/database', { trace_id: traceId, span_id: spanId })) } catch (e) { setError(e instanceof Error ? e : new Error(String(e))) } finally { setBusy(false) } }
  return <span onClick={e => e.stopPropagation()}><button className="btn h-6 ml-1" disabled={!!meta?.access?.read_only || busy} onClick={e => { e.stopPropagation(); run() }}>Inspect columns / indexes</button>
    <Modal open={open} onClose={() => setOpen(false)} title="Database inspection" width="max-w-3xl"><div className="p-4 overflow-y-auto space-y-3 whitespace-normal">
      {busy && <Spinner />}{error && <ErrorBox error={error} onRetry={run} />}
      {!busy && report && <><p className="text-muted">On-demand catalog inspection{report.table ? ` of ${report.table}` : ''}. No schema changes are executed.</p>
        {report.limitations.map(text => <p key={text} className="text-muted">{text}</p>)}
        {report.findings.length === 0 && <p>No supported catalog candidates found.</p>}
        {report.findings.map((f, i) => <article key={i} className="card p-3 space-y-2"><Pill>{f.kind.replaceAll('_', ' ')}</Pill><h3 className="font-semibold">{f.title}</h3><p>{f.detail}</p>{f.columns && <p>Columns: {f.columns.join(', ')}</p>}{f.column && <p>Generated column: <code>{f.column}</code></p>}{f.expression && <pre className="overflow-x-auto whitespace-pre-wrap">{f.expression}</pre>}{f.suggestion && <div className="space-y-2"><pre className="overflow-x-auto whitespace-pre-wrap break-words">{f.suggestion}</pre><CopyButton text={f.suggestion} /></div>}</article>)}
        {!!report.indexes?.length && <details><summary className="cursor-pointer">Existing indexes</summary>{report.indexes.map(index => <p key={index.name}><code>{index.name}</code>: {index.columns.join(', ')} ({index.kind}){index.included_columns?.length ? ` · include: ${index.included_columns.join(', ')}` : ''}</p>)}</details>}
      </>}
    </div></Modal>
  </span>
}
