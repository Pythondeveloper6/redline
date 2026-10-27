import { Fragment, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { apiLoad } from '../apiLoad'
import { ago, ms, pct } from '../lib/format'
import { useNow } from '../lib/hooks'
import { feature, isReadOnly } from '../lib/meta'
import type { Bottleneck, RunItem } from '../types'
import NewRunForm, { CliHint } from '../components/NewRunForm'
import RunChart from '../components/RunChart'
import Sparkline from '../components/Sparkline'
import { LoadError } from '../components/Unsupported'
import { Callsite, ConfirmDialog, Empty, I, Icon, Pill, Spinner, useAsync, type TONE } from '../components/ui'

const POLL_MS = 2000

type Tone = keyof typeof TONE
function statusTone(s: string): Tone {
  if (s === 'running') return 'info'
  if (/fail|error/.test(s)) return 'bad'
  if (/stop|cancel|abort/.test(s)) return 'warn'
  return 'ok'
}
export function RunStatus({ status }: { status: string }) {
  return (
    <Pill tone={statusTone(status)}>
      {status === 'running' && <span className="w-1.5 h-1.5 rounded-full bg-info live-dot" aria-hidden />}{status}
    </Pill>
  )
}
const verdictTone = (v: string): Tone => (/ok|pass|healthy|good/i.test(v) ? 'ok' : /fail|bad|break|saturat|degrad|error/i.test(v) ? 'bad' : 'warn')
const Verdict = ({ v }: { v: string | null | undefined }) => (v ? <Pill tone={verdictTone(v)}>{v}</Pill> : <span className="text-faint">-</span>)

/** True when the server lets the UI start runs (`meta.features.load_from_ui`). */
function useLoadFromUi() {
  const m = useAsync(() => api.meta(), [])
  return { ready: !m.loading, enabled: feature(m.data, 'load_from_ui') && !isReadOnly(m.data), readOnly: isReadOnly(m.data), loadOff: m.data ? !feature(m.data, 'load') : false }
}

/** Calls `fn` every `interval` ms while `active`. */
function usePolling(fn: () => void, active: boolean, interval = POLL_MS) {
  const ref = useRef(fn)
  ref.current = fn
  useEffect(() => {
    if (!active) return
    const t = setInterval(() => ref.current(), interval)
    return () => clearInterval(t)
  }, [active, interval])
}

export default function Load() {
  const { id } = useParams()
  const feat = useLoadFromUi()
  if (feat.loadOff) return <div className="card"><Empty title="Load testing is disabled"><p>The <span className="mono">load</span> feature is switched off on this server. Enable it in the Redline configuration to use this page.</p></Empty></div>
  return id ? <RunDetail id={id} /> : <RunList />
}

// ---------------------------------------------------------------------------
function RunList() {
  const nav = useNavigate()
  const now = useNow(15000)
  const feat = useLoadFromUi()
  const [form, setForm] = useState(false)
  const d = useAsync(() => apiLoad.runs(), [])
  const items = d.data?.items ?? []
  usePolling(d.reload, items.some(r => r.status === 'running'))
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Load tests</h1>
        {feat.enabled && !form && <button className="btn btn-accent" onClick={() => setForm(true)}><Icon d={I.play} size={12} />New run</button>}
      </div>
      {form && feat.enabled && <NewRunForm onCancel={() => setForm(false)} onStarted={rid => { setForm(false); if (rid) nav(`/load/${rid}`); else d.reload() }} />}
      {feat.ready && !feat.enabled && !feat.readOnly && <CliHint />}
      {d.error ? <LoadError error={d.error} what="load testing" onRetry={d.reload} /> : d.loading && !d.data ? <Spinner /> : items.length === 0 ? (
        <div className="card"><Empty title="No load runs yet"><p>Runs started from the CLI or (when enabled) this page are stored here with a per-step chart and bottleneck analysis.</p></Empty></div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr>
              <th className="th">Name</th><th className="th">Status</th><th className="th hidden sm:table-cell">Created</th>
              <th className="th text-right">Max OK RPS</th><th className="th text-right hidden md:table-cell">Knee step</th><th className="th">Verdict</th>
            </tr></thead>
            <tbody>{items.map((r: RunItem) => (
              <tr key={r.id} className="border-b border-line last:border-0 hover:bg-raised/50">
                <td className="td"><Link to={`/load/${r.id}`} className="font-medium hover:underline">{r.name || r.id}</Link></td>
                <td className="td"><RunStatus status={r.status} /></td>
                <td className="td hidden sm:table-cell text-muted" title={new Date(r.created_at * 1000).toLocaleString()}>{ago(r.created_at, now)}</td>
                <td className="td text-right num">{r.summary?.max_ok_rps != null ? r.summary.max_ok_rps.toLocaleString() : <span className="text-faint">-</span>}</td>
                <td className="td text-right num hidden md:table-cell">{r.summary?.knee_step != null ? r.summary.knee_step : <span className="text-faint">{r.status === 'running' ? '…' : 'none'}</span>}</td>
                <td className="td"><Verdict v={r.summary?.verdict} /></td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
function RunDetail({ id }: { id: string }) {
  const nav = useNavigate()
  const now = useNow(15000)
  const feat = useLoadFromUi()
  const d = useAsync(() => apiLoad.run(id), [id])
  const bn = useAsync(() => apiLoad.bottlenecks(id), [id])
  const run = d.data
  const running = run?.status === 'running'
  usePolling(() => { d.reload(); bn.reload() }, !!running)
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actErr, setActErr] = useState<string | null>(null)
  const act = async (fn: () => Promise<unknown>, after?: () => void) => {
    setBusy(true); setActErr(null)
    try { await fn(); after?.() } catch (e) { setActErr((e as Error).message) } finally { setBusy(false); setConfirm(false) }
  }

  if (d.error) return <div className="space-y-3"><Back /><LoadError error={d.error} what="load testing" onRetry={d.reload} /></div>
  if (!run) return <div className="space-y-3"><Back /><Spinner /></div>
  const knee = run.summary?.knee_step
  const steps = run.steps ?? []
  return (
    <div className="space-y-5">
      <Back />
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold">{run.name || run.id}</h1>
        <RunStatus status={run.status} /><Verdict v={run.summary?.verdict} />
        <span className="text-muted text-[12px]" title={new Date(run.created_at * 1000).toLocaleString()}>started {ago(run.created_at, now)}</span>
        <span className="flex-1" />
        {running && feat.enabled && <button className="btn" disabled={busy} onClick={() => act(() => apiLoad.stopRun(id), d.reload)}><Icon d={I.pause} size={12} />Stop</button>}
        {!running && !feat.readOnly && <button className="btn" disabled={busy} onClick={() => setConfirm(true)}><Icon d={I.trash} size={12} />Delete</button>}
      </div>
      {actErr && <div className="text-bad text-[12px]" role="alert">{actErr}</div>}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat k="Max OK RPS" v={run.summary?.max_ok_rps != null ? run.summary.max_ok_rps.toLocaleString() : '-'} />
        <Stat k="Knee" v={knee != null ? `step ${knee}` : running ? 'looking…' : 'none'} tone={knee != null ? 'text-accent' : undefined} />
        <Stat k="Steps" v={String(steps.length)} />
        <Stat k="Peak p95" v={steps.length ? ms(Math.max(...steps.map(s => s.p95_ms))) : '-'} />
      </div>

      <section className="card p-3 sm:p-4" aria-label="Steps chart">
        {steps.length === 0 ? <Empty title={running ? 'Waiting for the first step…' : 'No steps recorded'} /> : <RunChart steps={steps} knee={knee} />}
      </section>

      {steps.length > 0 && (
        <section className="card overflow-x-auto" aria-label="Steps table">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr>
              <th className="th">Step</th><th className="th text-right">Target</th><th className="th text-right">Achieved</th>
              <th className="th text-right hidden md:table-cell">Sent</th><th className="th text-right hidden md:table-cell">Dropped</th>
              <th className="th text-right hidden sm:table-cell">p50</th><th className="th text-right">p95</th><th className="th text-right hidden sm:table-cell">p99</th><th className="th text-right">Errors</th>
            </tr></thead>
            <tbody>{steps.map(s => (
              <tr key={s.step} data-sel={s.step === knee} className="border-b border-line last:border-0">
                <td className="td num font-medium">{s.step}{s.step === knee && <span className="ml-2"><Pill tone="bad">knee</Pill></span>}</td>
                <td className="td text-right num">{s.target_rps}</td>
                <td className={`td text-right num ${s.achieved_rps < s.target_rps * 0.9 ? 'text-warn font-semibold' : ''}`}>{s.achieved_rps.toFixed(1)}</td>
                <td className="td text-right num hidden md:table-cell">{s.sent.toLocaleString()}</td>
                <td className={`td text-right num hidden md:table-cell ${s.dropped ? 'text-warn' : 'text-muted'}`}>{s.dropped}</td>
                <td className="td text-right num hidden sm:table-cell">{ms(s.p50_ms)}</td>
                <td className="td text-right num">{ms(s.p95_ms)}</td>
                <td className="td text-right num hidden sm:table-cell">{ms(s.p99_ms)}</td>
                <td className={`td text-right num ${s.error_rate > 0.01 ? 'text-bad font-semibold' : s.error_rate > 0 ? 'text-warn' : 'text-muted'}`}>{pct(s.error_rate)}</td>
              </tr>))}
            </tbody>
          </table>
        </section>
      )}

      <section aria-label="Bottlenecks" className="space-y-2">
        <h2 className="m-0 text-[14px] font-semibold">Bottlenecks</h2>
        <p className="m-0 text-muted">Spans whose cost grows fastest as load increases, from traces tagged with this run. Sorted by growth.</p>
        {bn.error ? <LoadError error={bn.error} what="bottleneck analysis" onRetry={bn.reload} />
          : bn.loading && !bn.data ? <Spinner /> : bn.data && bn.data.items.length === 0 ? (
            <div className="card"><Empty title="No bottlenecks found"><p>Needs recorded traces tagged with this run at two or more steps (the app must be profiling the load).</p></Empty></div>
          ) : bn.data && <BottleneckTable items={bn.data.items} targets={Object.fromEntries(bn.data.steps.map(s => [s.step, s.target_rps]))} />}
      </section>

      <ConfirmDialog open={confirm} busy={busy} title="Delete this run?" confirmLabel="Delete run"
        body={<>The run and its per-step results will be removed. Recorded traces are kept.</>}
        onCancel={() => setConfirm(false)} onConfirm={() => act(() => apiLoad.deleteRun(id), () => nav('/load'))} />
    </div>
  )
}

const Back = () => <Link to="/load" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />All runs</Link>
const Stat = ({ k, v, tone }: { k: string; v: string; tone?: string }) => (
  <div className="card px-3 py-2"><div className="text-[11px] text-muted">{k}</div><div className={`text-[18px] font-semibold num ${tone ?? ''}`}>{v}</div></div>
)

function BottleneckTable({ items, targets }: { items: Bottleneck[]; targets: Record<number, number | null> }) {
  const [open, setOpen] = useState<string | null>(null)
  const rows = [...items].sort((a, b) => b.growth - a.growth)
  return (
    <div className="card overflow-x-auto">
      <table className="w-full border-collapse text-[12.5px]">
        <thead className="border-b border-line bg-raised/50"><tr>
          <th className="th w-6" /><th className="th">Span</th><th className="th hidden sm:table-cell">Per step</th><th className="th text-right">Mean</th><th className="th text-right">Growth</th>
        </tr></thead>
        <tbody>{rows.map(b => {
          const first = b.per_step[0]?.mean_ms ?? 0, last = b.per_step[b.per_step.length - 1]?.mean_ms ?? 0
          const isOpen = open === b.key
          return (
            <Fragment key={b.key}>
              <tr className="border-b border-line last:border-0 hover:bg-raised/50 cursor-pointer" onClick={() => setOpen(isOpen ? null : b.key)}>
                <td className="td"><button className="text-faint" aria-expanded={isOpen} aria-label={isOpen ? 'Collapse' : 'Expand'}><Icon d={I.chev} size={12} className={isOpen ? 'rotate-90' : ''} /></button></td>
                <td className="td !whitespace-normal max-w-[34rem]">
                  <div className="flex items-center gap-2"><Pill>{b.kind}</Pill><span className="mono break-all line-clamp-2">{b.label}</span></div>
                  {b.callsite && <div className="text-[11.5px] mt-0.5">{typeof b.callsite === 'string' ? <span className="mono text-muted">{b.callsite}</span> : <Callsite cs={b.callsite} />}</div>}
                </td>
                <td className="td hidden sm:table-cell"><Sparkline values={b.per_step.map(p => p.mean_ms)} width={110} color={b.growth >= 5 ? 'rgb(var(--bad))' : 'rgb(var(--muted))'} /></td>
                <td className="td text-right num">{ms(first)} <span className="text-faint">→</span> <b className={b.growth >= 5 ? 'text-bad' : ''}>{ms(last)}</b></td>
                <td className={`td text-right num font-semibold ${b.growth >= 5 ? 'text-bad' : b.growth >= 2 ? 'text-warn' : 'text-muted'}`}>×{b.growth >= 10 ? Math.round(b.growth) : b.growth.toFixed(1)}</td>
              </tr>
              {isOpen && (
                <tr className="border-b border-line bg-raised/30"><td />
                  <td colSpan={4} className="px-3 py-3">
                    <table className="text-[12px] border-collapse"><thead><tr className="text-muted">
                      <th className="th !py-1">Step</th><th className="th !py-1 text-right">Target rps</th><th className="th !py-1 text-right">Mean</th><th className="th !py-1 text-right">p95</th><th className="th !py-1 text-right">Count</th>
                    </tr></thead><tbody>{b.per_step.map(p => (
                      <tr key={p.step}><td className="td !py-0.5 num">{p.step}</td><td className="td !py-0.5 text-right num text-muted">{targets[p.step] ?? '-'}</td>
                        <td className="td !py-0.5 text-right num">{ms(p.mean_ms)}</td><td className="td !py-0.5 text-right num">{ms(p.p95_ms)}</td><td className="td !py-0.5 text-right num text-muted">{p.count.toLocaleString()}</td></tr>))}
                    </tbody></table>
                  </td></tr>
              )}
            </Fragment>
          )
        })}</tbody>
      </table>
    </div>
  )
}
