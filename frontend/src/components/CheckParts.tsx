import { useId } from 'react'
import { Link } from 'react-router-dom'
import { useFeatures } from '../lib/meta'
import { blockedReason, boxStats, fmtMs, gitShort, PERF_NOT_JUDGED, perfStyle, responseStyle, summaryCounts } from '../lib/check'
import type { CheckGit, CheckRunKind, CheckSummary, JsonDiffEntry, PerfStats } from '../types'
import { DiffView, Pill } from './ui'

/** Perf verdict badge: glyph + text + colour. */
export function PerfBadge({ v, reason }: { v: string | null | undefined; reason?: string | null }) {
  const s = perfStyle(v)
  const notJudged = v === 'n/a' && reason === 'broken'
  return (
    <Pill tone={s.tone} title={notJudged ? PERF_NOT_JUDGED : s.help + (reason ? ` (reason: ${reason})` : '')}>
      <span aria-hidden>{s.glyph}</span>{s.label}{v === 'slower' && reason === 'queries' ? <span className="font-normal opacity-80">(queries)</span> : null}
      {notJudged ? <span className="font-normal opacity-80">(not judged: response broken)</span> : null}
    </Pill>
  )
}
export function ResponseBadge({ v }: { v: string | null | undefined }) {
  const s = responseStyle(v)
  return <Pill tone={s.tone} title={s.help}><span aria-hidden>{s.glyph}</span>{s.label}</Pill>
}
export const KindBadge = ({ kind }: { kind: CheckRunKind }) => <Pill tone={kind === 'baseline' ? 'info' : 'neutral'}>{kind}</Pill>

export function GitChip({ git }: { git: CheckGit | null | undefined }) {
  const t = gitShort(git)
  return t ? <span className="mono text-[11.5px] text-muted" title={git?.sha ?? undefined}>{t}</span> : <span className="text-faint">-</span>
}

/** Verdict count badges for a finished compare (zero counts are left out). */
export function SummaryBadges({ summary }: { summary: CheckSummary | null | undefined }) {
  const c = summaryCounts(summary)
  const bits: { n: number; node: React.ReactNode }[] = [
    { n: c.broken, node: <Pill tone="bad"><span aria-hidden>✕</span>{c.broken} broken</Pill> },
    { n: c.missing, node: <Pill tone="bad"><span aria-hidden>?</span>{c.missing} missing</Pill> },
    { n: c.changed, node: <Pill tone="warn"><span aria-hidden>Δ</span>{c.changed} changed</Pill> },
    { n: c.slower, node: <Pill tone="bad"><span aria-hidden>↑</span>{c.slower} slower</Pill> },
    { n: c.faster, node: <Pill tone="ok"><span aria-hidden>↓</span>{c.faster} faster</Pill> },
    { n: c.noisy, node: <Pill tone="warn"><span aria-hidden>≈</span>{c.noisy} noisy</Pill> },
  ].filter(b => b.n > 0)
  if (!summary) return <span className="text-faint">-</span>
  if (!bits.length) return <Pill tone="ok"><span aria-hidden>✓</span>no differences</Pill>
  return <span className="inline-flex flex-wrap gap-1">{bits.map((b, i) => <span key={i}>{b.node}</span>)}</span>
}

/** Access state for the checks UI: read-only instance, feature off, or a start refused by the API. */
export function useCheckAccess() {
  const { meta, readOnly, has, loaded } = useFeatures()
  const fromUiKnownOff = loaded && meta?.features?.checks_from_ui === false
  const metaReason = readOnly ? blockedReason('read_only') : !has('checks') ? blockedReason('feature_disabled') : fromUiKnownOff ? blockedReason('checks_from_ui_disabled') : null
  return { meta, readOnly, loaded, enabled: has('checks'), metaReason }
}

// ---- latency distribution ---------------------------------------------------------------------------------
const W = 400, PAD_L = 4, PAD_R = 4

/** Small before/after latency strip: every timed sample as a dot, a box for q1..q3, a line at the median. */
export function LatencyStrip({ before, after, afterColor = 'rgb(var(--accent))', names = ['before', 'after'] }: { before: PerfStats | null; after: PerfStats | null; afterColor?: string; names?: [string, string] }) {
  const id = useId()
  const rows = [{ label: names[0], st: before, color: 'rgb(var(--muted))' }, { label: names[1], st: after, color: afterColor }].filter(r => r.st)
  if (!rows.length) return <div className="text-muted text-[12px]">No latency samples (perf is off for this request).</div>
  const all = rows.flatMap(r => r.st!.samples_ms)
  const lo = Math.min(...all), hi = Math.max(...all)
  const span = hi - lo || Math.max(hi * 0.1, 1)
  const x = (v: number) => PAD_L + ((v - lo) / span) * (W - PAD_L - PAD_R)
  const rowH = 26, top = 4
  const H = top + rows.length * rowH + 16
  const desc = rows.map(r => { const b = boxStats(r.st!.samples_ms)!; return `${r.label}: median ${fmtMs(b.median)}, middle half ${fmtMs(b.q1)} to ${fmtMs(b.q3)}, range ${fmtMs(b.min)} to ${fmtMs(b.max)}, ${b.n} samples` }).join('; ')
  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto max-w-[520px] block" role="img" aria-labelledby={id}>
        <title id={id}>{desc}</title>
        {rows.map((r, i) => {
          const b = boxStats(r.st!.samples_ms)!
          const cy = top + i * rowH + rowH / 2
          return (
            <g key={r.label}>
              <line x1={x(b.min)} x2={x(b.max)} y1={cy} y2={cy} stroke={r.color} strokeWidth="1" opacity=".6" />
              <rect x={x(b.q1)} y={cy - 7} width={Math.max(2, x(b.q3) - x(b.q1))} height="14" rx="2" fill={r.color} opacity=".18" stroke={r.color} strokeWidth="1" />
              {r.st!.samples_ms.map((v, j) => <circle key={j} cx={x(v)} cy={cy + ((j * 7) % 9) - 4} r="1.7" fill={r.color} opacity=".55" />)}
              <line x1={x(b.median)} x2={x(b.median)} y1={cy - 9} y2={cy + 9} stroke={r.color} strokeWidth="2.2" />
            </g>
          )
        })}
        <line x1={PAD_L} x2={W - PAD_R} y1={H - 14} y2={H - 14} stroke="rgb(var(--line))" />
        <text x={PAD_L} y={H - 3} fontSize="10" fill="rgb(var(--muted))">{fmtMs(lo)}</text>
        <text x={W - PAD_R} y={H - 3} fontSize="10" textAnchor="end" fill="rgb(var(--muted))">{fmtMs(hi)}</text>
      </svg>
      <figcaption className="flex flex-wrap gap-x-4 gap-y-0.5 text-[11.5px] text-muted mt-1">
        {rows.map(r => {
          const b = boxStats(r.st!.samples_ms)!
          return (
            <span key={r.label} className="inline-flex items-center gap-1.5">
              <span aria-hidden className="inline-block w-2.5 h-2.5 rounded-sm" style={{ background: r.color }} />
              <b className="text-ink font-medium">{r.label}</b> median <span className="num text-ink">{fmtMs(b.median)}</span> · p95 <span className="num">{fmtMs(r.st!.p95)}</span> · n={b.n}
              {r.st!.failed > 0 && <span className="text-bad font-medium">· {r.st!.failed} failed</span>}
            </span>
          )
        })}
      </figcaption>
    </figure>
  )
}

// ---- diffs ----------------------------------------------------------------------------------------------------
const KIND_TONE = { added: 'ok', removed: 'bad', changed: 'warn', type: 'info' } as const
const KIND_HELP = { added: 'Present now, absent before', removed: 'Present before, gone now', changed: 'Value changed', type: 'Value type changed' }

function Val({ v }: { v: unknown }) {
  if (v === undefined) return <span className="text-faint italic">missing</span>
  const text = JSON.stringify(v) ?? String(v)
  return <code className="mono break-all whitespace-pre-wrap">{text.length > 200 ? text.slice(0, 200) + '…' : text}</code>
}

export function JsonDiffList({ diff, total }: { diff: JsonDiffEntry[]; total?: number }) {
  const more = (total ?? diff.length) - diff.length
  return (
    <div className="rounded-md border border-line overflow-x-auto">
      <table className="w-full border-collapse text-[12px]">
        <thead className="bg-raised border-b border-line"><tr><th className="th">Path</th><th className="th">Kind</th><th className="th">Before</th><th className="th">After</th></tr></thead>
        <tbody>
          {diff.map((d, i) => (
            <tr key={i} className="border-b border-line last:border-0 align-top">
              <td className="td !whitespace-normal"><code className="mono break-all">{d.path}</code></td>
              <td className="td"><Pill tone={KIND_TONE[d.kind] ?? 'neutral'} title={KIND_HELP[d.kind]}>{d.kind}</Pill></td>
              <td className="td !whitespace-normal min-w-[8rem] max-w-[22rem]">{d.kind === 'added' ? <span className="text-faint italic">absent</span> : <Val v={d.before} />}</td>
              <td className="td !whitespace-normal min-w-[8rem] max-w-[22rem]">{d.kind === 'removed' ? <span className="text-faint italic">absent</span> : <Val v={d.after} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {more > 0 && <div className="px-3 py-1.5 text-[11.5px] text-muted border-t border-line bg-raised/50">…and {more} more difference{more === 1 ? '' : 's'} not shown (the report keeps the first {diff.length}).</div>}
    </div>
  )
}

export function TextDiff({ diff }: { diff: string }) {
  return <DiffView diff={diff} title="Response text diff" copyLabel="Copy diff" />
}

export function TraceLink({ id, label = 'sample trace' }: { id: string | null | undefined; label?: string }) {
  return id ? <Link to={`/requests/${id}`} className="text-[11.5px] text-info hover:underline whitespace-nowrap">{label} →</Link> : null
}
