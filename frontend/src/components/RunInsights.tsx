import { Fragment, useRef } from 'react'
import { ms, pct } from '../lib/format'
import type { RunMetrics, RunStep, RunSummary, StepMetrics } from '../types'
import { Pill } from './ui'
import { useWidth } from './TimeChart'

const PAD_L = 48, PAD_R = 52 // the same plot margins as RunChart, so steps line up

const LABELS: Record<string, string> = {
  cpu_pct: 'App CPU %', rss_mb: 'Memory MB', gc_pause_ms: 'GC pause ms/s', pool_checked_out: 'Pool checked out',
  pool_wait_ms: 'Pool wait ms/s', threads: 'Threads',
}
const ORDER = ['cpu_pct', 'rss_mb', 'pool_checked_out', 'pool_wait_ms', 'gc_pause_ms', 'threads']

/** Resource metrics under the latency chart, one small row per metric, on the same step axis. */
export function ResourceChart({ steps, metrics }: { steps: RunStep[]; metrics: StepMetrics[] }) {
  const box = useRef<HTMLDivElement>(null)
  const width = useWidth(box)
  const by = new Map(metrics.map(m => [m.step, m]))
  const names = ORDER.filter(n => metrics.some(m => m.metrics[n]))
  extra(metrics).forEach(n => names.push(n))
  if (!names.length || !steps.length) return null
  const plotW = Math.max(10, width - PAD_L - PAD_R), band = plotW / steps.length
  const rowH = 38
  return (
    <div ref={box} className="w-full" aria-label="Resource metrics by step">
      {names.map(name => {
        const vals = steps.map(s => by.get(s.step)?.metrics[name]?.avg ?? null)
        const max = Math.max(1e-9, ...vals.map(v => v ?? 0), ...steps.map(s => by.get(s.step)?.metrics[name]?.max ?? 0))
        const y = (v: number) => rowH - 6 - (v / max) * (rowH - 12)
        const pts = vals.map((v, i) => (v == null ? null : [PAD_L + band * (i + 0.5), y(v)] as const))
        const d = pts.reduce<string>((acc, p) => (p ? `${acc}${acc ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}` : acc), '')
        const lastVal = [...vals].reverse().find(v => v != null)
        return (
          <svg key={name} width={width} height={rowH} role="img" aria-label={`${LABELS[name] ?? name} per step`} className="block">
            <text x={4} y={14} className="fill-muted" fontSize="10.5">{LABELS[name] ?? name}</text>
            <path d={d} fill="none" stroke="rgb(var(--accent))" strokeWidth="1.5" strokeLinejoin="round" />
            {pts.map((p, i) => p && <circle key={i} cx={p[0]} cy={p[1]} r="2.2" fill="rgb(var(--accent))" />)}
            <text x={width - PAD_R + 6} y={rowH / 2 + 4} className="fill-ink" fontSize="11">{lastVal != null ? fmt(lastVal) : ''}</text>
          </svg>
        )
      })}
    </div>
  )
}

const extra = (metrics: StepMetrics[]) =>
  [...new Set(metrics.flatMap(m => Object.keys(m.metrics)))].filter(n => !ORDER.includes(n) && !n.startsWith('pool_size') && n !== 'pool_capacity').sort()
const fmt = (v: number) => (Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1))

const tone = (r: string) => (r === 'unknown' ? 'warn' : 'bad')

/** The verdict, recovery, drift, flows and the other run-wide results the CLI prints. */
export default function RunInsights({ summary, metrics }: { summary: RunSummary | null | undefined; metrics?: RunMetrics | null }) {
  const s = summary
  if (!s) return null
  const limit = metrics?.limiting_resource ?? s.limiting_resource
  const flows = Object.entries(s.flows ?? {})
  const blocks = [limit, s.recovery?.length, s.drift?.length, flows.length, s.identities, s.streams, s.websockets, s.sessions, s.generator?.limited_steps.length, s.ttfb_p95_ms].some(Boolean)
  if (!blocks) return null
  return (
    <section className="space-y-3" aria-label="Run insights">
      {limit && (
        <div className="card px-3 py-2 flex flex-wrap items-center gap-2" role="status">
          <span className="text-muted text-[12px]">Likely limiting resource</span>
          <Pill tone={tone(limit.resource)}>{limit.resource}</Pill>
          <span className="text-[12.5px]">step {limit.step}: {limit.detail}</span>
          {limit.note && <span className="text-[12px] text-muted w-full">{limit.note}</span>}
        </div>
      )}
      {s.generator && s.generator.limited_steps.length > 0 && (
        <div className="card px-3 py-2 text-[12.5px] text-warn" role="status">
          The load generator was near its CPU limit at step{s.generator.limited_steps.length > 1 ? 's' : ''} {s.generator.limited_steps.join(', ')} (peak {s.generator.max_cpu_pct}% of a core):
          the rate there may be capped by the generator, not the app. Add agents or workers.
        </div>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        {s.ttfb_p95_ms != null && (
          <div className="card px-3 py-2 text-[12.5px]"><div className="text-[11px] text-muted">Time to first byte</div>
            p50 <b className="num">{ms(s.ttfb_p50_ms ?? 0)}</b> · p95 <b className="num">{ms(s.ttfb_p95_ms)}</b> <span className="text-muted">(last byte p95 {ms(s.p95_ms ?? 0)})</span></div>
        )}
        {s.sessions && <Peak label="Sessions in flight (peak)" p={s.sessions} />}
        {s.streams && <Peak label="Held streams" p={s.streams} unit="events" count={s.streams.events} />}
        {s.websockets && <Peak label="WebSockets" p={s.websockets} unit="messages" count={s.websockets.messages} />}
        {s.identities && (
          <div className="card px-3 py-2 text-[12.5px]"><div className="text-[11px] text-muted">Identities</div>
            <b className="num">{s.identities.count}</b> users, {s.identities.requests.toLocaleString()} requests
            {s.identities.min != null && <span className="text-muted"> (per identity {s.identities.min}–{s.identities.max})</span>}</div>
        )}
      </div>
      {(s.recovery?.length || s.drift?.length) ? (
        <div className="card overflow-x-auto"><table className="w-full border-collapse text-[12.5px]">
          <thead className="border-b border-line bg-raised/50"><tr><th className="th">Step</th><th className="th">Check</th><th className="th">Result</th></tr></thead>
          <tbody>
            {(s.recovery ?? []).map(r => (
              <tr key={`r${r.step}`} className="border-b border-line last:border-0"><td className="td num">{r.step}</td><td className="td">Recovery after the spike</td>
                <td className="td">{r.recovered_after_s != null ? <>p95 back to its pre-spike level after <b>{r.recovered_after_s}s</b></> : <span className="text-bad">did not recover within the stage</span>}
                  <span className="text-muted"> (before {ms(r.baseline_p95_ms)}, peak {ms(r.peak_p95_ms)})</span></td></tr>))}
            {(s.drift ?? []).map(d => (
              <tr key={`d${d.step}`} className="border-b border-line last:border-0"><td className="td num">{d.step}</td><td className="td">Drift</td>
                <td className="td"><Pill tone={d.verdict === 'degrading' ? 'bad' : 'ok'}>{d.verdict}</Pill>
                  <span className="text-muted"> p95 {ms(d.first_third_p95_ms)} → {ms(d.last_third_p95_ms)} ({d.p95_slope_ms_per_min > 0 ? '+' : ''}{d.p95_slope_ms_per_min} ms/min)</span></td></tr>))}
          </tbody></table></div>
      ) : null}
      {flows.length > 0 && (
        <div className="card overflow-x-auto"><table className="w-full border-collapse text-[12.5px]">
          <thead className="border-b border-line bg-raised/50"><tr><th className="th">Flow / step</th><th className="th text-right">Runs</th><th className="th text-right">Errors</th><th className="th text-right">p50</th><th className="th text-right">p95</th></tr></thead>
          <tbody>{flows.map(([name, f]) => (
            <Fragment key={name}>
              <tr className="border-b border-line bg-raised/30"><td className="td font-medium">{name}</td><td className="td text-right num">{f.count.toLocaleString()}</td>
                <td className={`td text-right num ${f.errors ? 'text-bad' : 'text-muted'}`}>{f.errors ? `${f.errors} (${pct(f.error_rate)})` : 0}</td><td className="td text-right num">{ms(f.p50_ms)}</td><td className="td text-right num">{ms(f.p95_ms)}</td></tr>
              {f.steps.map(st => (
                <tr key={st.step} className="border-b border-line last:border-0"><td className="td pl-6 text-muted">{st.step}. {st.name}</td><td className="td text-right num">{st.count.toLocaleString()}</td>
                  <td className={`td text-right num ${st.errors ? 'text-bad' : 'text-muted'}`}>{st.errors}</td><td className="td text-right num">{ms(st.p50_ms)}</td><td className="td text-right num">{ms(st.p95_ms)}</td></tr>))}
            </Fragment>))}
          </tbody></table></div>
      )}
      {s.metric_source_errors && s.metric_source_errors.length > 0 && (
        <div className="text-[12px] text-warn">Metric sources that failed: {s.metric_source_errors.join(', ')}</div>
      )}
    </section>
  )
}

function Peak({ label, p, unit, count }: { label: string; p: { max_open: number; count?: number }; unit?: string; count?: number }) {
  return (
    <div className="card px-3 py-2 text-[12.5px]"><div className="text-[11px] text-muted">{label}</div>
      peak <b className="num">{p.max_open.toLocaleString()}</b>{p.count != null && <span> · {p.count.toLocaleString()} held</span>}
      {unit && count != null && <span className="text-muted"> · {count.toLocaleString()} {unit}</span>}</div>
  )
}
