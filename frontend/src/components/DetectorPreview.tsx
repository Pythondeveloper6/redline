import { useState } from 'react'
import { call } from '../api'
import { useMeta } from '../lib/meta'
import type { Issue } from '../types'
import { Modal } from './Modal'
import { IssueExtra } from './IssueCard'
import { ErrorBox, IssueBadge, Spinner } from './ui'

const FIELDS = [
  ['repeated_writes_threshold', 'Repeated writes', 5], ['deep_offset_rows', 'Deep OFFSET rows', 10000],
  ['round_trip_count', 'Minimum query round trips', 10], ['round_trip_ms', 'Cumulative query time (ms)', 100],
  ['pool_wait_ms', 'Pool wait (ms)', 50], ['large_response_bytes', 'Response bytes', 1000000],
  ['large_cache_bytes', 'Cache bytes', 1000000], ['orm_instances_threshold', 'ORM instances', 500],
  ['cache_decode_ms', 'Explicit cache decoding (ms)', 50],
  ['repeated_scans_threshold', 'Aggregate scans', 2], ['json_decode_ms', 'Sampled JSON decoding (ms)', 50],
] as const

export function DetectorPreview({ traceId }: { traceId: string }) {
  const meta = useMeta()
  const [open, setOpen] = useState(false)
  const [settings, setSettings] = useState<Record<string, number>>({})
  const [issues, setIssues] = useState<Issue[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const run = async () => { setBusy(true); setError(null); try { const result = await call<{ issues: Issue[] }>('POST', './api/analysis/trace', { trace_id: traceId, settings }); setIssues(result.issues) } catch (e) { setError(e instanceof Error ? e : new Error(String(e))) } finally { setBusy(false) } }
  return <><button className="btn" disabled={!!meta?.access?.read_only} onClick={() => setOpen(true)}>Try detector thresholds</button>
    <Modal open={open} onClose={() => setOpen(false)} title="Analyze recorded request" width="max-w-3xl"><div className="p-4 space-y-4 overflow-y-auto">
      <p className="text-muted">Preview findings against existing evidence. Blank fields use the server setting; placeholders show built-in defaults. Previewing does not change global settings or stored findings. Missing evidence cannot be reconstructed.</p>
      <form className="space-y-3" onSubmit={e => { e.preventDefault(); run() }}><div className="grid sm:grid-cols-2 gap-3">{FIELDS.map(([name, label, fallback]) => <label key={name} className="flex flex-col gap-1">{label}<input className="field" type="number" min="0" step="any" placeholder={String(fallback)} value={settings[name] ?? ''} onChange={e => { const next = { ...settings }; if (e.target.value === '') delete next[name]; else next[name] = Number(e.target.value); setSettings(next) }} /></label>)}</div><button className="btn btn-accent" disabled={busy}>Analyze recorded evidence</button></form>
      {busy && <Spinner />}{error && <ErrorBox error={error} />}
      {issues && <div className="space-y-3" aria-live="polite"><p>{issues.length} findings in this preview</p>{issues.map(issue => <article key={issue.id} className="card p-3 space-y-2"><IssueBadge kind={issue.kind} /><p className="font-medium">{issue.title}</p><p>{issue.detail}</p><IssueExtra extra={issue.extra} /></article>)}</div>}
    </div></Modal>
  </>
}
