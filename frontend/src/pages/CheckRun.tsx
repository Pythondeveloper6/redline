import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiChecks } from '../apiChecks'
import { ago, bytes } from '../lib/format'
import { useNow } from '../lib/hooks'
import {
  baselineRequestsOf, defaultDir, fmtMs, fmtPct, fmtSigned, PERF_NOT_JUDGED, perfStyle, reportOf, reportToMarkdown, sortRequests, stoppedNotice, summarize, type SortDir, type SortKey,
} from '../lib/check'
import type { BaselineRequest, CheckReport, CheckRunDetail, ReportRequest } from '../types'
import { GitChip, JsonDiffList, KindBadge, LatencyStrip, PerfBadge, ResponseBadge, TextDiff, TraceLink, useCheckAccess } from '../components/CheckParts'
import { LoadError } from '../components/Unsupported'
import { CopyButton, ConfirmDialog, Empty, ErrorBox, I, Icon, Pill, Spinner, Sql, useAsync } from '../components/ui'
import { RunStatus } from './Load'
import { CheckDisabled } from './Checks'

const POLL_MS = 1000

export default function CheckRun() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const access = useCheckAccess()
  const now = useNow(15000)
  const d = useAsync(() => apiChecks.run(id), [id])
  const last = useRef<CheckRunDetail | undefined>(undefined)
  const prevId = useRef(id)
  if (prevId.current !== id) { prevId.current = id; last.current = undefined }
  if (d.data) last.current = d.data
  const run = d.data ?? last.current
  const running = run?.status === 'running'
  const reload = useRef(d.reload); reload.current = d.reload
  useEffect(() => {
    if (!running) return
    const t = setInterval(() => reload.current(), POLL_MS)
    return () => clearInterval(t)
  }, [running])

  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actErr, setActErr] = useState<string | null>(null)
  const act = async (fn: () => Promise<unknown>, after?: () => void) => {
    setBusy(true); setActErr(null)
    try { await fn(); after?.() } catch (e) { setActErr((e as Error).message) } finally { setBusy(false); setConfirm(false) }
  }

  if (access.loaded && !access.enabled) return <CheckDisabled />
  if (!run && d.error) return <div className="space-y-3"><Back /><LoadError error={d.error} what="refactor checks" onRetry={d.reload} /></div>
  if (!run) return <div className="space-y-3"><Back /><Spinner /></div>

  const report = reportOf(run)
  const recorded = baselineRequestsOf(run)
  const hasResults = run.kind === 'compare' ? !!report : recorded.length > 0
  const backTo = run.suite_id ? `/checks/suites/${run.suite_id}` : '/checks'
  return (
    <div className="space-y-5">
      <Back to={backTo} label={run.suite_id ? 'Suite' : 'All suites'} />
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold">{run.label || (run.kind === 'baseline' ? 'Baseline' : 'Comparison')}</h1>
        <KindBadge kind={run.kind} /><RunStatus status={run.status} />
        <GitChip git={run.git} />
        <span className="text-muted text-[12px]" title={new Date(run.created_at * 1000).toLocaleString()}>started {ago(run.created_at, now)}</span>
        <span className="flex-1" />
        {report && <CopyButton text={reportToMarkdown(report)} label="Copy as Markdown" />}
        {running && !access.readOnly && <button className="btn" disabled={busy} onClick={() => act(() => apiChecks.stopRun(id), d.reload)}><Icon d={I.pause} size={12} />Stop</button>}
        {!running && !access.readOnly && <button className="btn" disabled={busy} onClick={() => setConfirm(true)}><Icon d={I.trash} size={12} />Delete</button>}
      </div>
      {actErr && <div className="text-bad text-[12px]" role="alert">{actErr}</div>}
      {d.error && <div className="text-warn text-[12px]" role="status">Lost contact with the server; showing the last known state{running ? ' and retrying' : ''}.</div>}

      {running && <Progress run={run} />}
      {run.status === 'failed' && <ErrorBox error={new Error(run.error || 'The run failed.')} />}
      {run.status === 'stopped' && <div className="card p-3 text-warn" role="status" data-testid="stopped">{stoppedNotice(run, hasResults)}</div>}

      {run.kind === 'compare' && report ? <ReportView report={report} />
        : run.kind === 'baseline' && recorded.length > 0 ? <BaselineView run={run} requests={recorded} />
          : run.status === 'done' ? <div className="card"><Empty title="No results stored"><p>This run finished but has no per-request results (it may have been trimmed by retention).</p></Empty></div>
            : null}

      <ConfirmDialog open={confirm} busy={busy} title="Delete this run?" confirmLabel="Delete run"
        body={run.kind === 'baseline' ? <>The baseline will be removed; comparisons made against it are kept only if they were retained separately.</> : <>The report will be removed. Recorded traces are kept.</>}
        onCancel={() => setConfirm(false)} onConfirm={() => act(() => apiChecks.deleteRun(id), () => nav(backTo))} />
    </div>
  )
}

const Back = ({ to = '/checks', label = 'All suites' }: { to?: string; label?: string }) => (
  <Link to={to} className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />{label}</Link>
)

function Progress({ run }: { run: CheckRunDetail }) {
  const p = run.progress
  const pct = p && p.total > 0 ? Math.min(100, Math.round((p.done / p.total) * 100)) : null
  return (
    <section className="card p-4 space-y-2" aria-label="Run progress" aria-live="polite">
      <div className="flex items-center gap-2">
        <span className="w-1.5 h-1.5 rounded-full bg-info live-dot" aria-hidden />
        <span className="text-[13px] font-semibold">{run.kind === 'baseline' ? 'Recording baseline' : 'Comparing with baseline'}…</span>
        <span className="flex-1" />
        <span className="num text-muted">{p ? `${p.done.toLocaleString()} / ${p.total.toLocaleString()} calls` : 'starting'}{pct != null ? ` · ${pct}%` : ''}</span>
      </div>
      <div className="h-2 rounded-full bg-raised overflow-hidden" role="progressbar" aria-label="Run progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct ?? undefined}>
        <div className={`h-full bg-accent rounded-full transition-[width] duration-700 ${pct == null ? 'w-1/3 animate-pulse' : ''}`} style={pct != null ? { width: `${pct}%` } : undefined} />
      </div>
      <p className="m-0 text-[11.5px] text-muted">Requests are sent one at a time, round-robin, so the numbers stay comparable. This page refreshes every second and the report appears here when the run finishes.</p>
    </section>
  )
}

// ---- report -------------------------------------------------------------------------------------------------
function Card({ k, children, sub, tone = '' }: { k: string; children: ReactNode; sub?: ReactNode; tone?: string }) {
  return <div className="card px-3 py-2 min-w-0"><div className="text-[11px] text-muted">{k}</div><div className={`text-[18px] font-semibold num ${tone}`}>{children}</div>{sub && <div className="text-[11px] text-muted num">{sub}</div>}</div>
}

function ReportView({ report }: { report: CheckReport }) {
  const sm = useMemo(() => summarize(report.requests), [report])
  const now = useNow(60000)
  const [key, setKey] = useState<SortKey>('perf')
  const [dir, setDir] = useState<SortDir>('asc')
  const [open, setOpen] = useState<Set<string>>(() => new Set(report.requests.filter(r => r.response.verdict === 'broken').slice(0, 1).map(r => r.name)))
  const [onlyDiff, setOnlyDiff] = useState(false)
  const rows = useMemo(() => {
    const base = onlyDiff ? report.requests.filter(r => r.perf.verdict === 'slower' || r.perf.verdict === 'faster' || r.perf.verdict === 'noisy' || r.response.verdict === 'changed' || r.response.verdict === 'broken' || r.response.verdict === 'missing') : report.requests
    return sortRequests(base, key, dir)
  }, [report, key, dir, onlyDiff])
  const toggle = (n: string) => setOpen(s => { const x = new Set(s); x.has(n) ? x.delete(n) : x.add(n); return x })
  const sort = (k: SortKey) => { if (k === key) setDir(dir === 'asc' ? 'desc' : 'asc'); else { setKey(k); setDir(defaultDir(k)) } }
  const th = (k: SortKey, label: string, cls = '') => (
    <th className={`th ${cls}`} aria-sort={key === k ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'}>
      <button type="button" className="inline-flex items-center gap-1 hover:text-ink font-medium" onClick={() => sort(k)}>
        {label}<span aria-hidden className={key === k ? 'text-ink' : 'text-faint'}>{key === k ? (dir === 'asc' ? '▲' : '▼') : '↕'}</span>
      </button>
    </th>
  )
  const speedTone = sm.speedChangePct == null ? '' : sm.speedChangePct < -0.05 ? 'text-ok' : sm.speedChangePct > 0.05 ? 'text-bad' : ''
  const qTone = sm.queriesChange < 0 ? 'text-ok' : sm.queriesChange > 0 ? 'text-bad' : ''
  const bl = report.baseline
  return (
    <div className="space-y-4">
      <p className="m-0 text-[12.5px] text-muted flex flex-wrap items-center gap-x-2 gap-y-1">
        Baseline{' '}
        {bl?.id ? <Link to={`/checks/runs/${bl.id}`} className="text-ink font-medium hover:underline">{bl.label || 'baseline'}</Link> : <b className="text-ink font-medium">{bl?.label || bl?.file || 'baseline'}</b>}
        <GitChip git={bl?.git} /> <span>recorded {ago(bl?.created_at ?? 0, now)}</span>
        <span aria-hidden>→</span> current <GitChip git={report.run?.git} /> <span className="mono text-[11.5px]">{report.run?.base_url}</span>
      </p>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3" role="list" aria-label="Summary">
        <div role="listitem"><Card k="Passed" sub={`of ${sm.total} request${sm.total === 1 ? '' : 's'}`} tone={sm.passed === sm.total ? 'text-ok' : ''}><span aria-hidden>{sm.passed === sm.total ? '✓ ' : ''}</span>{sm.passed}</Card></div>
        <div role="listitem"><Card k="Changed" sub="response differs" tone={sm.changed ? 'text-warn' : ''}><span aria-hidden>{sm.changed ? 'Δ ' : ''}</span>{sm.changed}</Card></div>
        <div role="listitem"><Card k="Broken" sub="now failing" tone={sm.broken ? 'text-bad' : ''}><span aria-hidden>{sm.broken ? '✕ ' : ''}</span>{sm.broken}</Card></div>
        <div role="listitem"><Card k="Noisy" sub="too variable to judge" tone={sm.noisy ? 'text-warn' : ''}><span aria-hidden>{sm.noisy ? '≈ ' : ''}</span>{sm.noisy}</Card></div>
        {report.requests.some(r => r.budgets?.some(b => !b.passed)) && <div role="listitem"><Card k="Budget failures" tone="text-bad" sub="absolute endpoint limits">{report.requests.filter(r => r.budgets?.some(b => !b.passed)).length}</Card></div>}
        {sm.missing > 0 && <div role="listitem"><Card k="Missing" sub="not in the baseline" tone="text-bad"><span aria-hidden>? </span>{sm.missing}</Card></div>}
        <div role="listitem"><Card k="Faster" sub="significant" tone={sm.faster ? 'text-ok' : ''}><span aria-hidden>{sm.faster ? '↓ ' : ''}</span>{sm.faster}</Card></div>
        <div role="listitem"><Card k="Slower" sub="past thresholds" tone={sm.slower ? 'text-bad' : ''}><span aria-hidden>{sm.slower ? '↑ ' : ''}</span>{sm.slower}</Card></div>
        <div role="listitem"><Card k="Overall speed" tone={speedTone} sub={sm.speedChangePct == null ? 'nothing measured' : `${sm.speedChangePct < 0 ? 'faster' : sm.speedChangePct > 0 ? 'slower' : 'unchanged'} · geometric mean of ${sm.speedSamples}`}>{fmtPct(sm.speedChangePct)}</Card></div>
        <div role="listitem"><Card k="Queries" tone={qTone} sub={sm.queriesSamples ? `${sm.queriesBefore} → ${sm.queriesAfter}${sm.queriesChangePct != null ? ` (${fmtPct(sm.queriesChangePct, 0)})` : ''}` : 'no server-side data'}>{sm.queriesSamples ? fmtSigned(sm.queriesChange) : '-'}</Card></div>
      </div>

      {(report.summary?.warnings ?? []).map(w => <div key={w} className="card p-3 text-warn text-[12.5px]" role="status">{w}</div>)}

      {(sm.wins.length > 0 || sm.regressions.length > 0) && (
        <div className="grid sm:grid-cols-2 gap-3">
          <Top title="Biggest wins" items={sm.wins} tone="text-ok" />
          <Top title="Biggest regressions" items={sm.regressions} tone="text-bad" />
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <h2 className="m-0 text-[14px] font-semibold flex-1">Requests</h2>
        <label className="inline-flex items-center gap-1.5 text-[12px] cursor-pointer"><input type="checkbox" className="accent-[rgb(var(--accent))]" checked={onlyDiff} onChange={e => setOnlyDiff(e.target.checked)} />Only differences</label>
      </div>
      <div className="card overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="border-b border-line bg-raised/50"><tr>
            <th className="th w-6"><span className="sr-only">Expand</span></th>
            {th('name', 'Request')}{th('perf', 'Perf')}
            {th('before', 'Median', 'text-right hidden md:table-cell')}{th('change', 'Δ%', 'text-right')}
            {th('queries', 'Queries', 'text-right hidden sm:table-cell')}{th('response', 'Response')}
          </tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={7} className="td text-center text-muted py-8">Nothing differs: every request passed.</td></tr>}
            {rows.map(r => {
              const isOpen = open.has(r.name)
              const q = r.server
              return (
                <Fragment key={r.name}>
                  <tr className="border-b border-line last:border-0 hover:bg-raised/50 cursor-pointer" onClick={() => toggle(r.name)} data-row={r.name}>
                    <td className="td"><button type="button" className="text-faint" aria-expanded={isOpen} aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${r.name}`} onClick={e => { e.stopPropagation(); toggle(r.name) }}><Icon d={I.chev} size={12} className={isOpen ? 'rotate-90' : ''} /></button></td>
                    <td className="td !whitespace-normal min-w-[9.5rem] max-w-[22rem]"><div className="font-medium">{r.name} {r.budgets?.some(b => !b.passed) && <Pill tone="bad">Budget failed</Pill>}</div><div className="mono text-[11.5px] text-muted break-words [overflow-wrap:anywhere]"><span className="font-semibold">{r.method}</span> {r.path}</div></td>
                    <td className="td"><PerfBadge v={r.perf.verdict} reason={r.perf.reason} /></td>
                    <td className="td text-right num hidden md:table-cell">{r.perf.before && r.perf.after ? <>{fmtMs(r.perf.before.median)} <span className="text-faint">→</span> <b>{fmtMs(r.perf.after.median)}</b></> : <span className="text-faint">-</span>}</td>
                    <td className={`td text-right num font-medium ${r.perf.change_pct == null ? 'text-faint' : r.perf.verdict === 'faster' ? 'text-ok' : r.perf.verdict === 'slower' ? 'text-bad' : 'text-muted'}`}>{fmtPct(r.perf.change_pct)}</td>
                    <td className="td text-right num hidden sm:table-cell">{q?.before && q?.after ? <>{q.before.queries_median} <span className="text-faint">→</span> <b className={q.after.queries_median > q.before.queries_median ? 'text-bad' : q.after.queries_median < q.before.queries_median ? 'text-ok' : ''}>{q.after.queries_median}</b></> : <span className="text-faint">-</span>}</td>
                    <td className="td"><ResponseBadge v={r.response.verdict} /></td>
                  </tr>
                  {isOpen && <tr className="border-b border-line bg-raised/30"><td /><td colSpan={6} className="px-2 sm:px-3 py-3"><div className="sticky left-0 w-[calc(100vw-3.25rem)] sm:w-auto"><RequestDetail r={r} /></div></td></tr>}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="m-0 text-[11.5px] text-faint">Verdicts: <b className="font-medium">slower</b> needs a significant change (Mann–Whitney, p &lt; 0.01) past both thresholds, or more queries than the baseline allows. <b className="font-medium">noisy</b> means the spread was too large to judge; raise repeat.</p>
    </div>
  )
}

function Top({ title, items, tone }: { title: string; items: { name: string; pct: number }[]; tone: string }) {
  return (
    <div className="card px-3 py-2">
      <div className="text-[11px] text-muted mb-1">{title}</div>
      {items.length === 0 ? <div className="text-faint text-[12px]">None</div> : (
        <ul className="m-0 p-0 list-none space-y-0.5">{items.map(i => <li key={i.name} className="flex justify-between gap-3 text-[12.5px]"><span className="truncate">{i.name}</span><b className={`num ${tone}`}>{fmtPct(i.pct)}</b></li>)}</ul>
      )}
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="space-y-1.5"><h3 className="m-0 text-[12px] font-semibold text-muted uppercase tracking-wide">{title}</h3>{children}</section>
}

function RequestDetail({ r }: { r: ReportRequest }) {
  const p = r.perf, s = r.server, resp = r.response
  const afterColor = p.verdict === 'slower' ? 'rgb(var(--bad))' : p.verdict === 'faster' ? 'rgb(var(--ok))' : 'rgb(var(--accent))'
  return (
    <div className="space-y-4">
      <Section title="Latency">
        <LatencyStrip before={p.before} after={p.after} afterColor={afterColor} />
        <div className="flex flex-wrap gap-x-5 gap-y-0.5 text-[12px] text-muted">
          {p.change_ms != null && <span>Change <b className="num text-ink font-medium">{p.change_ms > 0 ? '+' : p.change_ms < 0 ? '−' : ''}{fmtMs(Math.abs(p.change_ms))}</b> ({fmtPct(p.change_pct)})</span>}
          {p.p_value != null && <span>p-value <b className="num text-ink font-medium">{p.p_value < 0.0001 ? '< 0.0001' : p.p_value.toFixed(4)}</b></span>}
          <span>{p.reason === 'broken' ? PERF_NOT_JUDGED : perfStyle(p.verdict).help}{p.reason === 'queries' ? ' (query count rose)' : p.reason === 'cv' ? ' (high variance)' : ''}</span>
        </div>
        {(p.before?.failed || p.after?.failed) ? <div className="text-bad text-[12px]" role="status">Failed calls: {p.before?.failed ?? 0} before, {p.after?.failed ?? 0} after (of the timed calls).</div> : null}
      </Section>

      {!!r.budgets?.length && <Section title="Endpoint budgets">
        <div className="space-y-2">{r.budgets.map(b => <div key={b.rule} className="flex flex-wrap items-center gap-2">
          <Pill tone={b.passed ? 'ok' : 'bad'}>{b.passed ? 'Pass' : 'Fail'}</Pill>
          <span className="mono">{b.rule}</span><span>Observed: {JSON.stringify(b.observed) ?? 'unavailable'} · Limit: {JSON.stringify(b.limit)}</span>
          {b.reason && <span className="text-bad">{b.reason}</span>}
        </div>)}</div>
      </Section>}
      <Section title="Response">
        <div className="flex flex-wrap items-center gap-2 text-[12px]">
          <ResponseBadge v={resp.verdict} />
          <span className="text-muted">status</span><b className="num">{resp.status[0] ?? '-'} <span className="text-faint font-normal">→</span> {resp.status[1] ?? '-'}</b>
        </div>
        {resp.rules && (Object.keys(resp.rules.array_keys).length > 0 || Object.keys(resp.rules.tolerances).length > 0) && <div className="space-y-1 text-muted">
          <p>Explicit comparison rules applied:</p>
          {Object.entries(resp.rules.array_keys).map(([path, key]) => <div key={path}>Match <code>{path}</code> by <code>{key}</code></div>)}
          {Object.entries(resp.rules.tolerances).map(([path, rule]) => <div key={path}><code>{path}</code>: absolute {rule.absolute ?? 0}, relative {rule.relative ?? 0}</div>)}
        </div>}
        {resp.diff.length > 0 && <JsonDiffList diff={resp.diff} total={resp.diff_total} />}
        {resp.text_diff && <TextDiff diff={resp.text_diff} />}
        {resp.verdict === 'same' && <div className="text-muted text-[12px]">Identical outside the ignored fields.</div>}
        {resp.verdict === 'missing' && <div className="text-bad text-[12px]">This request is not in the baseline: record the baseline again with the current suite.</div>}
        {resp.verdict === 'n/a' && <div className="text-muted text-[12px]">The response is not compared for this request.</div>}
        {resp.ignored.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11.5px] text-muted" title="Fields that differ on every call, plus your ignore rules">Ignored (changes on every call or by rule):</span>
            {resp.ignored.map(i => <code key={i} className="mono rounded bg-raised px-1.5 text-[11.5px]">{i}</code>)}
          </div>
        )}
      </Section>

      <Section title="Server-side">
        {!s ? <div className="text-muted text-[12px]">No server-side metrics: no traces tagged with this check arrived (the app may not be instrumented, or it records to a different store). Client-side numbers above still apply.</div> : (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-x-5 gap-y-0.5 text-[12px] text-muted">
              <span>Queries (median) <b className="num text-ink font-medium">{s.before?.queries_median ?? '-'} → {s.after?.queries_median ?? '-'}</b>{s.queries_change != null && <> ({fmtSigned(s.queries_change)})</>}</span>
              {s.before?.duration_ms_median != null && s.after?.duration_ms_median != null && <span>Server time <b className="num text-ink font-medium">{fmtMs(s.before.duration_ms_median)} → {fmtMs(s.after.duration_ms_median)}</b></span>}
              {s.before?.query_ms_median != null && s.after?.query_ms_median != null && <span>Query time <b className="num text-ink font-medium">{fmtMs(s.before.query_ms_median)} → {fmtMs(s.after.query_ms_median)}</b></span>}
            </div>
            {(s.new_issues.length > 0 || s.fixed_issues.length > 0) && (
              <ul className="m-0 p-0 list-none space-y-1">
                {s.new_issues.map((i, k) => <li key={'n' + k} className="flex flex-wrap items-center gap-2 text-[12.5px]"><Pill tone="bad"><span aria-hidden>+</span>new issue</Pill><Pill>{i.kind.replace(/_/g, ' ')}</Pill><span>{i.title}</span><TraceLink id={i.sample_trace_id} /></li>)}
                {s.fixed_issues.map((i, k) => <li key={'f' + k} className="flex flex-wrap items-center gap-2 text-[12.5px]"><Pill tone="ok"><span aria-hidden>✓</span>fixed issue</Pill><Pill>{i.kind.replace(/_/g, ' ')}</Pill><span>{i.title}</span><TraceLink id={i.sample_trace_id} label="baseline trace" /></li>)}
              </ul>
            )}
            {s.not_judged && (s.not_judged.fixed_issues.length > 0 || s.not_judged.gone_sql.length > 0 || s.not_judged.queries_change != null) && (
              <div className="text-[12px] text-muted" role="note">
                {PERF_NOT_JUDGED}: not counted as wins{s.not_judged.queries_change != null && <> (queries {fmtSigned(s.not_judged.queries_change)})</>}
                {s.not_judged.fixed_issues.length > 0 && <>, {s.not_judged.fixed_issues.length} issue{s.not_judged.fixed_issues.length === 1 ? '' : 's'} no longer seen</>}
                {s.not_judged.gone_sql.length > 0 && <>, {s.not_judged.gone_sql.length} SQL statement{s.not_judged.gone_sql.length === 1 ? '' : 's'} no longer run</>}.
              </div>
            )}
            {s.new_sql.length > 0 && <SqlList title="New SQL" tone="bad" mark="+" items={s.new_sql} />}
            {s.gone_sql.length > 0 && <SqlList title="Gone SQL" tone="ok" mark="−" items={s.gone_sql} />}
            {s.new_issues.length === 0 && s.fixed_issues.length === 0 && s.new_sql.length === 0 && s.gone_sql.length === 0 && <div className="text-muted text-[12px]">No new or fixed issues and no change in the SQL statements.</div>}
          </div>
        )}
      </Section>
    </div>
  )
}

function SqlList({ title, items, tone, mark }: { title: string; items: { fingerprint: string; sql: string; count?: number; sample_trace_id?: string | null }[]; tone: 'ok' | 'bad'; mark: string }) {
  return (
    <div className="space-y-1.5">
      <div className="text-[11.5px] font-medium text-muted">{title}</div>
      {items.map(q => (
        <div key={q.fingerprint} className={`rounded-md border border-line px-2.5 py-1.5 border-l-2 ${tone === 'ok' ? 'border-l-ok' : 'border-l-bad'}`}>
          <div className="flex items-center gap-2 mb-0.5">
            <Pill tone={tone}><span aria-hidden>{mark}</span>{title.toLowerCase()}</Pill>
            {q.count != null && <span className="text-[11.5px] text-muted num">{q.count}× per request</span>}
            <span className="flex-1" /><TraceLink id={q.sample_trace_id} />
          </div>
          <Sql sql={q.sql} format={false} className="text-[12px]" />
        </div>
      ))}
    </div>
  )
}

// ---- baseline ----------------------------------------------------------------------------------------------------------
function BaselineView({ run, requests }: { run: CheckRunDetail; requests: BaselineRequest[] }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (n: string) => setOpen(s => { const x = new Set(s); x.has(n) ? x.delete(n) : x.add(n); return x })
  const timed = requests.reduce((a, r) => a + (r.perf?.samples_ms.length ?? 0), 0)
  const volatile = requests.reduce((a, r) => a + r.volatile.length, 0)
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Card k="Requests recorded">{requests.length}</Card>
        <Card k="Timed calls" sub="per-request samples kept">{timed.toLocaleString()}</Card>
        <Card k="Volatile fields" sub="ignored in later comparisons">{volatile}</Card>
        <Card k="Code"><span className="text-[14px]"><GitChip git={run.git} /></span></Card>
      </div>
      <p className="m-0 text-muted max-w-3xl">This is a baseline: the reference every comparison is measured against. Pick it as the baseline when you compare after your change.{run.suite_id && <> <Link className="text-info hover:underline" to={`/checks/suites/${run.suite_id}`}>Compare now →</Link></>}</p>
      <div className="card overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]">
          <thead className="border-b border-line bg-raised/50"><tr>
            <th className="th w-6"><span className="sr-only">Expand</span></th><th className="th">Request</th><th className="th text-right">Median</th><th className="th text-right hidden sm:table-cell">p95</th>
            <th className="th text-right hidden md:table-cell">Mean ± σ</th><th className="th text-right hidden sm:table-cell">Queries</th><th className="th">Response</th>
          </tr></thead>
          <tbody>{requests.map(r => {
            const isOpen = open.has(r.name)
            return (
              <Fragment key={r.name}>
                <tr className="border-b border-line last:border-0 hover:bg-raised/50 cursor-pointer" onClick={() => toggle(r.name)}>
                  <td className="td"><button type="button" className="text-faint" aria-expanded={isOpen} aria-label={`${isOpen ? 'Collapse' : 'Expand'} ${r.name}`} onClick={e => { e.stopPropagation(); toggle(r.name) }}><Icon d={I.chev} size={12} className={isOpen ? 'rotate-90' : ''} /></button></td>
                  <td className="td !whitespace-normal min-w-[9.5rem] max-w-[22rem]"><div className="font-medium">{r.name}</div><div className="mono text-[11.5px] text-muted break-words [overflow-wrap:anywhere]"><span className="font-semibold">{r.method}</span> {r.path}</div></td>
                  <td className="td text-right num">{r.perf ? fmtMs(r.perf.median) : <span className="text-faint">not timed</span>}</td>
                  <td className="td text-right num hidden sm:table-cell">{r.perf ? fmtMs(r.perf.p95) : '-'}</td>
                  <td className="td text-right num hidden md:table-cell">{r.perf ? `${fmtMs(r.perf.mean)} ± ${fmtMs(r.perf.stdev)}` : '-'}</td>
                  <td className="td text-right num hidden sm:table-cell">{r.server ? r.server.queries_median : <span className="text-faint">-</span>}</td>
                  <td className="td">{r.response ? <span className="inline-flex items-center gap-1.5"><Pill tone={r.response.status != null && r.response.status < 400 ? 'ok' : 'bad'}>{r.response.status ?? 'fail'}</Pill><span className="text-muted text-[11.5px]">{r.response.body_kind} · {bytes(r.response.size)}</span></span> : <span className="text-faint">not compared</span>}</td>
                </tr>
                {isOpen && (
                  <tr className="border-b border-line bg-raised/30"><td /><td colSpan={6} className="px-2 sm:px-3 py-3"><div className="sticky left-0 w-[calc(100vw-3.25rem)] sm:w-auto space-y-3">
                    <Section title="Recorded latency"><LatencyStrip before={r.perf} after={null} names={['recorded', '']} />{r.perf && r.perf.failed > 0 && <div className="text-bad text-[12px]">{r.perf.failed} failed call(s) while recording.</div>}</Section>
                    {r.server && <Section title="Server-side"><div className="text-[12px] text-muted">Queries median <b className="num text-ink font-medium">{r.server.queries_median}</b>{r.server.queries_max != null && <> (max {r.server.queries_max})</>}{r.server.duration_ms_median != null && <> · server time <b className="num text-ink font-medium">{fmtMs(r.server.duration_ms_median)}</b></>}</div>
                      {(r.server.issues?.length ?? 0) > 0 && <div className="flex flex-wrap gap-1.5">{r.server.issues!.map((i, k) => <Pill key={k} tone="warn">{i.kind.replace(/_/g, ' ')}</Pill>)}</div>}</Section>}
                    <Section title="Volatile fields">
                      {r.volatile.length === 0 ? <div className="text-muted text-[12px]">Nothing changed between the two captured responses.</div> : (
                        <div className="flex flex-wrap gap-1.5">{r.volatile.map(v => <code key={v} className="mono rounded bg-raised px-1.5 text-[11.5px]">{v}</code>)}</div>
                      )}
                      <p className="m-0 text-[11.5px] text-faint">These differed between the two responses captured while recording, so later comparisons ignore them.</p>
                    </Section>
                  </div></td></tr>
                )}
              </Fragment>
            )
          })}</tbody>
        </table>
      </div>
    </div>
  )
}
