import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiChecks } from '../apiChecks'
import { ApiError } from '../api'
import { ago } from '../lib/format'
import { useNow } from '../lib/hooks'
import { baselineFileError, blockedReason, importErrorMessage, parseBaselineText } from '../lib/check'
import { GitChip, KindBadge, SummaryBadges, useCheckAccess } from '../components/CheckParts'
import { LoadError } from '../components/Unsupported'
import { ConfirmDialog, Empty, I, Icon, Pill, Spinner, useAsync } from '../components/ui'
import { RunStatus } from './Load'
import { CheckDisabled } from './Checks'

export default function CheckSuite() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const access = useCheckAccess()
  const now = useNow(15000)
  const suite = useAsync(() => apiChecks.suite(id), [id])
  const runs = useAsync(() => apiChecks.runs(id), [id])
  const items = runs.data?.items ?? []
  const baselines = items.filter(r => r.kind === 'baseline' && r.status === 'done').sort((a, b) => b.created_at - a.created_at)
  const anyRunning = items.some(r => r.status === 'running')

  // Poll while something is running.
  const reload = useRef(runs.reload); reload.current = runs.reload
  useEffect(() => {
    if (!anyRunning) return
    const t = setInterval(() => reload.current(), 2000)
    return () => clearInterval(t)
  }, [anyRunning])

  const [baselineId, setBaselineId] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState<'baseline' | 'compare' | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [blocked, setBlocked] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  useEffect(() => { if (!baselineId && baselines[0]) setBaselineId(baselines[0].id) }, [baselines, baselineId])

  const reason = blocked ?? access.metaReason
  const fileInput = useRef<HTMLInputElement>(null)
  const [importing, setImporting] = useState(false)
  const [importErr, setImportErr] = useState<string | null>(null)
  const [imported, setImported] = useState<string | null>(null)
  const importFile = async (file: File | undefined) => {
    if (fileInput.current) fileInput.current.value = '' // picking the same file again must fire onChange
    if (!file) return
    setImportErr(null); setImported(null)
    const tooBig = baselineFileError(file)
    if (tooBig) { setImportErr(tooBig); return }
    setImporting(true)
    try {
      const parsed = parseBaselineText(await file.text())
      if (!parsed.ok) { setImportErr(parsed.error); return }
      const r = await apiChecks.importBaseline(parsed.doc, id)
      runs.reload()
      setBaselineId(r.id)
      setImported(file.name)
    } catch (e) {
      setImportErr(importErrorMessage((e as ApiError).code, (e as Error).message))
    } finally { setImporting(false) }
  }
  const start = async (kind: 'baseline' | 'compare') => {
    setBusy(kind); setErr(null)
    try {
      const r = await apiChecks.startRun({ suite_id: id, kind, ...(kind === 'compare' ? { baseline_id: baselineId } : {}), ...(label.trim() ? { label: label.trim() } : {}) })
      nav(`/checks/runs/${r.id}`)
    } catch (e) {
      const code = (e as ApiError).code
      const why = blockedReason(code)
      if (why) setBlocked(why); else setErr((e as Error).message)
    } finally { setBusy(null) }
  }

  if (access.loaded && !access.enabled) return <CheckDisabled />
  if (suite.error) return <div className="space-y-3"><Back /><LoadError error={suite.error} what="refactor checks" onRetry={suite.reload} /></div>
  if (!suite.data) return <div className="space-y-3"><Back /><Spinner /></div>
  const s = suite.data.suite
  const noBaseline = baselines.length === 0

  return (
    <div className="space-y-5">
      <Back />
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold">{suite.data.name}</h1>
        <span className="text-muted text-[12px]">{s.requests.length} request{s.requests.length === 1 ? '' : 's'} · {s.repeat ?? 20}× + {s.warmup ?? 3} warmup</span>
        <span className="flex-1" />
        <Link to={`/checks/suites/${id}/edit`} className="btn">{access.readOnly ? 'View suite' : 'Edit suite'}</Link>
        {!access.readOnly && <button className="btn" onClick={() => setConfirm(true)}><Icon d={I.trash} size={12} />Delete</button>}
      </div>

      <section className="card p-4 space-y-3" aria-label="Run a check">
        {reason && (
          <div className="rounded-md border border-warn/30 bg-warn/5 text-warn p-3 text-[12.5px] flex gap-2" role="status" data-testid="blocked">
            <Icon d={I.alert} size={15} className="shrink-0 mt-0.5" /><div>{reason}</div>
          </div>
        )}
        <div className="grid gap-4 md:grid-cols-2">
          <div className="space-y-2">
            <div className="text-[13px] font-semibold">1. Record a baseline</div>
            <p className="m-0 text-muted">Run the suite against the current (old) code and keep the results as the reference.</p>
            <button className="btn btn-accent" disabled={!!reason || busy !== null} aria-describedby={reason ? 'why' : undefined} onClick={() => start('baseline')}>
              <Icon d={I.play} size={12} />{busy === 'baseline' ? 'Starting…' : 'Record baseline'}
            </button>
          </div>
          <div className="space-y-2">
            <div className="text-[13px] font-semibold">2. Compare with a baseline</div>
            <p className="m-0 text-muted">After changing the code, run the same requests again and see what differs.</p>
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor="bl-pick">Baseline</label>
              <select id="bl-pick" className="field max-w-full" value={baselineId} disabled={noBaseline} onChange={e => setBaselineId(e.target.value)}>
                {noBaseline && <option value="">No baseline recorded yet</option>}
                {baselines.map(b => <option key={b.id} value={b.id}>{b.label || 'baseline'} · {ago(b.created_at, now)}{b.git?.sha ? ` · ${b.git.sha.slice(0, 7)}` : ''}</option>)}
              </select>
              <button className="btn btn-accent" disabled={!!reason || noBaseline || !baselineId || busy !== null} onClick={() => start('compare')}>
                <Icon d={I.zap} size={12} />{busy === 'compare' ? 'Starting…' : 'Compare'}
              </button>
            </div>
            {noBaseline && <p className="m-0 text-[11.5px] text-faint">Record a baseline first, or import one.</p>}
            <div className="flex flex-wrap items-center gap-2">
              <input ref={fileInput} id="bl-file" type="file" accept="application/json,.json" className="sr-only" tabIndex={-1} onChange={e => importFile(e.target.files?.[0])} />
              <button type="button" className="btn" disabled={access.readOnly || importing} onClick={() => fileInput.current?.click()}>
                <Icon d={I.down} size={12} />{importing ? 'Importing…' : 'Import baseline…'}
              </button>
              <span className="text-[11.5px] text-faint">A <code className="mono">*.baseline.json</code> from <code className="mono">redline check record</code> (up to 10 MiB).</span>
            </div>
            {imported && <p className="m-0 text-[12px] text-ok" role="status">Imported {imported}; it is selected above.</p>}
            {importErr && <div className="text-bad text-[12px]" role="alert">{importErr}</div>}
          </div>
        </div>
        <label className="flex flex-col gap-1 text-[11.5px] text-muted max-w-sm">Label (optional)
          <input className="field text-ink" value={label} onChange={e => setLabel(e.target.value)} placeholder="after eager loading" maxLength={80} />
        </label>
        {err && <div className="text-bad text-[12px]" role="alert">{err}</div>}
        {reason && <p id="why" className="sr-only">{reason}</p>}
      </section>

      <section className="space-y-2" aria-label="Runs">
        <h2 className="m-0 text-[14px] font-semibold">Runs</h2>
        {runs.error ? <LoadError error={runs.error} what="refactor checks" onRetry={runs.reload} /> : runs.loading && !runs.data ? <Spinner /> : items.length === 0 ? (
          <div className="card"><Empty title="No runs yet"><p>Record a baseline to get started. Runs are kept per suite (the most recent 50).</p></Empty></div>
        ) : (
          <div className="card overflow-x-auto">
            <table className="w-full border-collapse text-[12.5px]">
              <thead className="border-b border-line bg-raised/50"><tr>
                <th className="th">Run</th><th className="th">Status</th><th className="th hidden md:table-cell">Code</th><th className="th hidden sm:table-cell">Started</th><th className="th">Result</th>
              </tr></thead>
              <tbody>{items.map(r => (
                <tr key={r.id} className="border-b border-line last:border-0 hover:bg-raised/50">
                  <td className="td"><Link to={`/checks/runs/${r.id}`} className="inline-flex items-center gap-2 hover:underline"><KindBadge kind={r.kind} /><span className="font-medium">{r.label || (r.kind === 'baseline' ? 'baseline' : 'compare')}</span></Link></td>
                  <td className="td"><RunStatus status={r.status} /></td>
                  <td className="td hidden md:table-cell"><GitChip git={r.git} /></td>
                  <td className="td hidden sm:table-cell text-muted" title={new Date(r.created_at * 1000).toLocaleString()}>{ago(r.created_at, now)}</td>
                  <td className="td !whitespace-normal">
                    {r.status === 'running' ? <span className="text-muted num">{r.progress ? `${Math.round((r.progress.done / Math.max(1, r.progress.total)) * 100)}%` : '…'}</span>
                      : r.kind === 'baseline' ? (r.status === 'done' ? <Pill tone="info">reference</Pill> : <span className="text-faint">-</span>)
                        : r.status === 'done' ? <SummaryBadges summary={r.summary} /> : <span className="text-faint">-</span>}
                  </td>
                </tr>))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmDialog open={confirm} title="Delete this suite?" confirmLabel="Delete suite"
        body={<>The suite and its recorded runs will be removed. Recorded traces are kept.</>}
        onCancel={() => setConfirm(false)}
        onConfirm={async () => { try { await apiChecks.deleteSuite(id); nav('/checks') } catch (e) { setErr((e as Error).message); setConfirm(false) } }} />
    </div>
  )
}

const Back = () => <Link to="/checks" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />All suites</Link>
