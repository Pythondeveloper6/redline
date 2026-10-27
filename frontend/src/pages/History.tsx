import { useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { apiLoad } from '../apiLoad'
import { ms, pct } from '../lib/format'
import type { HistoryPoint } from '../types'
import HistoryCompare from '../components/HistoryCompare'
import { TimePanel, useWidth, type LineDef } from '../components/TimeChart'
import { LoadError } from '../components/Unsupported'
import { Empty, Spinner, useAsync } from '../components/ui'

const WINDOWS = ['24h', '7d', '30d', '90d']
const BUCKETS = ['1h', '1d']
const BUCKET_S: Record<string, number> = { '1h': 3600, '1d': 86400 }

const C = { p50: 'rgb(var(--info))', p95: 'rgb(var(--warn))', p99: 'rgb(var(--bad))', count: 'rgb(var(--faint))', err: 'rgb(var(--bad))', q: 'rgb(var(--ok))' }
const LAT: LineDef<HistoryPoint>[] = [
  { label: 'p50', color: C.p50, get: p => p.p50_ms }, { label: 'p95', color: C.p95, get: p => p.p95_ms }, { label: 'p99', color: C.p99, get: p => p.p99_ms },
]
const CNT: LineDef<HistoryPoint>[] = [{ label: 'Requests', color: C.count, get: p => p.count, bar: true }]
const ERR: LineDef<HistoryPoint>[] = [{ label: 'Error rate', color: C.err, get: p => p.error_rate }]
const QRY: LineDef<HistoryPoint>[] = [{ label: 'Avg queries', color: C.q, get: p => p.avg_queries }]

function Radio({ options, value, onChange, label }: { options: string[]; value: string; onChange: (v: string) => void; label: string }) {
  return (
    <div className="inline-flex rounded-md border border-line p-0.5 bg-surface" role="radiogroup" aria-label={label}>
      {options.map(w => <button key={w} role="radio" aria-checked={value === w} onClick={() => onChange(w)} className={`h-6 px-2.5 rounded text-[12px] font-medium ${value === w ? 'bg-raised text-ink' : 'text-muted hover:text-ink'}`}>{w}</button>)}
    </div>
  )
}

const fmtTime = (t: number, bucket: number) => {
  const d = new Date(t * 1000)
  return bucket >= 86400 ? d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

export default function History() {
  const [win, setWin] = useState('30d')
  const [bucket, setBucket] = useState('1d')
  const [ep, setEp] = useState('') // "METHOD route" or '' for the whole app
  const [hover, setHover] = useState<{ idx: number; x: number } | null>(null)
  const eps = useAsync(() => api.endpoints('7d').catch(() => ({ items: [] })), [])
  const rel = useAsync(() => apiLoad.releases(), [])
  const [method, route] = ep ? [ep.slice(0, ep.indexOf(' ')), ep.slice(ep.indexOf(' ') + 1)] : [undefined, undefined]
  const h = useAsync(() => apiLoad.history({ route, method, window: win, bucket }), [win, bucket, ep])
  const box = useRef<HTMLDivElement>(null)
  const width = useWidth(box)
  const bs = BUCKET_S[bucket]
  const points = h.data?.series ?? []
  const releases = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of h.data?.releases ?? []) m.set(r.release, r.first_seen)
    for (const r of rel.data?.items ?? []) if (!m.has(r.release)) m.set(r.release, r.first_seen)
    return [...m].map(([release, first_seen]) => ({ release, first_seen })).sort((a, b) => a.first_seen - b.first_seen)
  }, [h.data, rel.data])
  const totals = useMemo(() => {
    const n = points.reduce((s, p) => s + p.count, 0)
    const w = (get: (p: HistoryPoint) => number) => (n ? points.reduce((s, p) => s + get(p) * p.count, 0) / n : 0)
    return { n, err: w(p => p.error_rate), q: w(p => p.avg_queries), p95: Math.max(0, ...points.map(p => p.p95_ms)) }
  }, [points])
  const hp = hover ? points[hover.idx] : null
  const common = { points, bucket: bs, releases, hover, onHover: setHover }

  const pickWindow = (w: string) => { setWin(w); if (w === '24h') setBucket('1h') }
  const endpointOptions = (eps.data?.items ?? []).map(e => `${e.method} ${e.route}`)

  return (
    <div className="space-y-6">
      <div>
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <h1 className="m-0 text-[15px] font-semibold flex-1 min-w-[6rem]">History</h1>
          <select className="field max-w-full" value={ep} onChange={e => setEp(e.target.value)} aria-label="Endpoint">
            <option value="">Whole app</option>
            {endpointOptions.map(o => <option key={o} value={o}>{o}</option>)}
            {ep && !endpointOptions.includes(ep) && <option value={ep}>{ep}</option>}
          </select>
          <Radio options={WINDOWS} value={win} onChange={pickWindow} label="Time window" />
          <Radio options={BUCKETS} value={bucket} onChange={setBucket} label="Bucket size" />
        </div>
        {h.error ? <LoadError error={h.error} what="history" onRetry={h.reload} /> : h.loading && !h.data ? <Spinner /> : points.length === 0 ? (
          <div className="card"><Empty title="No history in this window"><p>History is rolled up hourly from recorded requests{ep ? ' for this endpoint' : ''}. Try a longer window.</p></Empty></div>
        ) : (
          <div className="card p-3 sm:p-4">
            <div className="flex flex-wrap gap-x-6 gap-y-1 mb-3 text-[12px] text-muted">
              <span><b className="text-ink num">{totals.n.toLocaleString()}</b> requests</span>
              <span>peak p95 <b className="text-ink num">{ms(totals.p95)}</b></span>
              <span>error rate <b className="text-ink num">{pct(totals.err)}</b></span>
              <span>avg queries <b className="text-ink num">{totals.q.toFixed(1)}</b></span>
              <span className="flex flex-wrap gap-3 ml-auto">
                {LAT.map(l => <span key={l.label} className="inline-flex items-center gap-1.5"><span className="w-3 h-0.5 rounded" style={{ background: l.color }} />{l.label}</span>)}
                <span className="inline-flex items-center gap-1.5"><span className="w-0 h-3 border-l border-dashed border-muted" />release</span>
              </span>
            </div>
            <div ref={box} className="relative">
              <PanelTitle>Latency</PanelTitle>
              <TimePanel {...common} lines={LAT} fmt={ms} height={170} labels title="Latency percentiles over time" />
              <PanelTitle>Requests</PanelTitle>
              <TimePanel {...common} lines={CNT} fmt={v => (v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 'k' : String(Math.round(v)))} height={90} title="Request count over time" />
              <PanelTitle>Error rate</PanelTitle>
              <TimePanel {...common} lines={ERR} fmt={v => pct(v)} height={90} title="Error rate over time" />
              <PanelTitle>Average queries per request</PanelTitle>
              <TimePanel {...common} lines={QRY} fmt={v => v.toFixed(v < 10 ? 1 : 0)} height={90} title="Average queries over time" />
              {hover && hp && (
                <div className="pointer-events-none absolute top-6 z-10 card shadow-lg px-2.5 py-2 text-[11.5px] space-y-0.5 min-w-[160px]"
                  style={hover.x > width * 0.6 ? { right: width - hover.x + 12 } : { left: hover.x + 12 }}>
                  <div className="font-semibold mb-0.5">{fmtTime(hp.t, bs)}</div>
                  <TipRow k="Requests" v={hp.count.toLocaleString()} /><TipRow k="p50" v={ms(hp.p50_ms)} c={C.p50} />
                  <TipRow k="p95" v={ms(hp.p95_ms)} c={C.p95} /><TipRow k="p99" v={ms(hp.p99_ms)} c={C.p99} />
                  <TipRow k="Errors" v={pct(hp.error_rate)} /><TipRow k="Avg queries" v={hp.avg_queries.toFixed(1)} />
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      {rel.error ? (h.error ? null : <LoadError error={rel.error} what="releases" onRetry={rel.reload} />) : rel.loading && !rel.data ? null : rel.data && <HistoryCompare releases={rel.data.items} />}
    </div>
  )
}

const PanelTitle = ({ children }: { children: string }) => <div className="text-[11px] font-medium text-muted mt-2 first:mt-0 pl-[46px]">{children}</div>
const TipRow = ({ k, v, c }: { k: string; v: string; c?: string }) => (
  <div className="flex justify-between gap-4"><span className="text-muted inline-flex items-center gap-1.5">{c && <span className="w-2 h-0.5 rounded" style={{ background: c }} />}{k}</span><span className="num">{v}</span></div>
)
