import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiLoad } from '../apiLoad'
import { ms } from '../lib/format'
import type { CompareEndpoint, CompareIssue, ReleaseInfo, Verdict } from '../types'
import { Empty, IssueBadge, Pill, Sev, Spinner, TONE, useAsync } from './ui'
import { LoadError } from './Unsupported'

type Side = { release: string } | { from: string; to: string }
const CUSTOM = '__custom__'
const DAY = 86400

const pad = (n: number) => String(n).padStart(2, '0')
function toLocal(epoch: number) {
  const d = new Date(epoch * 1000)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const fromLocal = (s: string) => Math.floor(new Date(s).getTime() / 1000)
function spec(s: Side) {
  if ('release' in s) return `release:${s.release}`
  return `range:${fromLocal(s.from)}-${fromLocal(s.to)}`
}
const valid = (s: Side) => 'release' in s ? !!s.release : Number.isFinite(fromLocal(s.from)) && Number.isFinite(fromLocal(s.to)) && fromLocal(s.to) > fromLocal(s.from)

/** A = previous release, B = latest; fewer than two releases: A = previous 7 days, B = last 24 h. */
export function defaultSides(releases: ReleaseInfo[]): [Side, Side] {
  const r = [...releases].sort((a, b) => a.first_seen - b.first_seen)
  if (r.length >= 2) return [{ release: r[r.length - 2].release }, { release: r[r.length - 1].release }]
  const now = Math.floor(Date.now() / 1000)
  return [{ from: toLocal(now - 8 * DAY), to: toLocal(now - DAY) }, { from: toLocal(now - DAY), to: toLocal(now) }]
}

function SidePicker({ label, value, onChange, releases }: { label: string; value: Side; onChange: (s: Side) => void; releases: ReleaseInfo[] }) {
  const sel = 'release' in value ? value.release : CUSTOM
  const now = Math.floor(Date.now() / 1000)
  return (
    <fieldset className="min-w-0 flex-1 basis-64 border border-line rounded-md px-3 pb-2.5 pt-1 m-0">
      <legend className="px-1 text-[11px] font-semibold text-muted">{label}</legend>
      <select className="field w-full" aria-label={`${label} source`} value={sel}
        onChange={e => onChange(e.target.value === CUSTOM ? { from: toLocal(now - 7 * DAY), to: toLocal(now) } : { release: e.target.value })}>
        {releases.map(r => <option key={r.release} value={r.release}>release {r.release}</option>)}
        <option value={CUSTOM}>Custom time range…</option>
      </select>
      {'from' in value && (
        <div className="flex flex-wrap gap-2 mt-2">
          <input type="datetime-local" className="field flex-1 min-w-[10rem]" aria-label={`${label} start`} value={value.from} onChange={e => onChange({ ...value, from: e.target.value })} />
          <input type="datetime-local" className="field flex-1 min-w-[10rem]" aria-label={`${label} end`} value={value.to} onChange={e => onChange({ ...value, to: e.target.value })} />
        </div>
      )}
    </fieldset>
  )
}

const VERDICT_TONE: Record<Verdict, keyof typeof TONE> = { regressed: 'bad', improved: 'ok', same: 'neutral', new: 'info', gone: 'neutral' }
const VERDICT_ORDER: Record<Verdict, number> = { regressed: 0, new: 1, gone: 2, same: 3, improved: 4 }

const signed = (v: number | null | undefined, digits = 0, suffix = '') =>
  v == null ? '-' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}${suffix}`

function EndpointRow({ e }: { e: CompareEndpoint }) {
  const dp = e.delta_p95_pct
  return (
    <tr className="border-b border-line last:border-0 hover:bg-raised/50">
      <td className="td"><span className="mono font-semibold text-[11.5px] mr-2">{e.method}</span><span className="font-medium">{e.route}</span></td>
      <td className="td"><Pill tone={VERDICT_TONE[e.verdict]}>{e.verdict}</Pill></td>
      <td className="td text-right num hidden sm:table-cell">{e.a ? ms(e.a.p95_ms) : '-'}</td>
      <td className="td text-right num hidden sm:table-cell">{e.b ? ms(e.b.p95_ms) : '-'}</td>
      <td className={`td text-right num font-semibold ${e.verdict === 'regressed' ? 'text-bad' : e.verdict === 'improved' ? 'text-ok' : 'text-muted'}`}>{signed(dp, 0, '%')}</td>
      <td className="td text-right num hidden md:table-cell">{e.a ? e.a.avg_queries.toFixed(1) : '-'} <span className="text-faint">→</span> {e.b ? e.b.avg_queries.toFixed(1) : '-'}</td>
      <td className={`td text-right num ${(e.delta_queries ?? 0) >= 1 ? 'text-bad' : (e.delta_queries ?? 0) <= -1 ? 'text-ok' : 'text-muted'}`}>{signed(e.delta_queries, 1)}</td>
      <td className="td text-right num hidden lg:table-cell text-muted">{e.a ? e.a.count.toLocaleString() : '-'} <span className="text-faint">→</span> {e.b ? e.b.count.toLocaleString() : '-'}</td>
    </tr>
  )
}

function IssueList({ title, tone, items }: { title: string; tone: keyof typeof TONE; items: CompareIssue[] }) {
  return (
    <div className="card p-3 min-w-0">
      <div className="flex items-center gap-2 mb-2"><span className="text-[12.5px] font-semibold">{title}</span><Pill tone={tone}>{items.length}</Pill>
        <span className="flex-1" /><Link to="/issues" className="text-[11.5px] text-info hover:underline">All issues</Link></div>
      {items.length === 0 ? <div className="text-faint text-[12px] py-2">None</div> : (
        <ul className="m-0 p-0 list-none space-y-2">
          {items.map((i, n) => (
            <li key={(i.id ?? i.issue_id ?? '') + n} className="text-[12.5px]">
              <div className="flex flex-wrap items-center gap-1.5">{i.severity && <Sev s={i.severity} />}<IssueBadge kind={i.kind} /></div>
              <Link to="/issues" className="block mt-1 break-words hover:underline">{i.title}</Link>
              {i.route && <span className="mono text-[11px] text-muted">{i.route}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function HistoryCompare({ releases }: { releases: ReleaseInfo[] }) {
  const init = useMemo(() => defaultSides(releases), [releases])
  const [a, setA] = useState<Side>(init[0])
  const [b, setB] = useState<Side>(init[1])
  const [applied, setApplied] = useState<{ a: string; b: string }>({ a: spec(init[0]), b: spec(init[1]) })
  const d = useAsync(() => apiLoad.compare(applied.a, applied.b), [applied.a, applied.b])
  const ok = valid(a) && valid(b)
  const rows = useMemo(() => [...(d.data?.endpoints ?? [])].sort((x, y) =>
    VERDICT_ORDER[x.verdict] - VERDICT_ORDER[y.verdict] || (y.delta_p95_pct ?? 0) - (x.delta_p95_pct ?? 0)), [d.data])
  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const r of rows) c[r.verdict] = (c[r.verdict] ?? 0) + 1
    return c
  }, [rows])

  return (
    <section className="space-y-3" aria-label="Compare">
      <h2 className="m-0 text-[14px] font-semibold">Compare releases or periods</h2>
      <div className="card p-3 flex flex-wrap items-end gap-3">
        <SidePicker label="A (baseline)" value={a} onChange={setA} releases={releases} />
        <SidePicker label="B (candidate)" value={b} onChange={setB} releases={releases} />
        <button className="btn btn-accent h-8" disabled={!ok || d.loading} onClick={() => setApplied({ a: spec(a), b: spec(b) })}>Compare</button>
      </div>
      {!ok && <div className="text-[12px] text-warn">Pick a release or a valid time range (end after start) for both sides.</div>}
      {d.error ? <LoadError error={d.error} what="comparing periods" onRetry={d.reload} />
        : d.loading && !d.data ? <Spinner /> : d.data && (
        <>
          <div className="flex flex-wrap gap-2 text-[12px]">
            {(['regressed', 'new', 'gone', 'same', 'improved'] as Verdict[]).map(v => counts[v] ? <Pill key={v} tone={VERDICT_TONE[v]}>{counts[v]} {v}</Pill> : null)}
          </div>
          {rows.length === 0 ? (
            <div className="card"><Empty title="No endpoints to compare"><p>Neither side has recorded requests in that period.</p></Empty></div>
          ) : (
            <div className="card overflow-x-auto">
              <table className="w-full border-collapse text-[12.5px]">
                <thead className="border-b border-line bg-raised/50"><tr>
                  <th className="th">Endpoint</th><th className="th">Verdict</th>
                  <th className="th text-right hidden sm:table-cell">p95 A</th><th className="th text-right hidden sm:table-cell">p95 B</th>
                  <th className="th text-right">Δ p95</th><th className="th text-right hidden md:table-cell">Avg queries</th><th className="th text-right">Δ queries</th>
                  <th className="th text-right hidden lg:table-cell">Requests</th>
                </tr></thead>
                <tbody>{rows.map(e => <EndpointRow key={e.method + e.route} e={e} />)}</tbody>
              </table>
            </div>
          )}
          <div className="grid gap-3 md:grid-cols-3">
            <IssueList title="New issues" tone="bad" items={d.data.issues.new} />
            <IssueList title="Resolved" tone="ok" items={d.data.issues.resolved} />
            <IssueList title="Persisting" tone="warn" items={d.data.issues.persisting} />
          </div>
        </>
      )}
    </section>
  )
}
