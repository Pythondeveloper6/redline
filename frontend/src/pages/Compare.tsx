import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../api'
import { ms } from '../lib/format'
import type { CompareResult, TraceSummary } from '../types'
import { Empty, ErrorBox, I, Icon, Spinner, Sql, Status, useAsync } from '../components/ui'

const sign = (n: number) => (n > 0 ? '+' : n < 0 ? '−' : '')
/** Higher is worse for every metric on this page. */
const tone = (d: number) => (d > 0 ? 'text-bad' : d < 0 ? 'text-ok' : 'text-muted')

function Side({ label, t }: { label: string; t: TraceSummary }) {
  return (
    <Link to={`/requests/${t.id}`} className="card px-3.5 py-3 block hover:bg-raised/50 min-w-0">
      <div className="text-[11px] font-semibold text-faint">{label}</div>
      <div className="flex items-center gap-2 mt-0.5 min-w-0"><span className="mono font-bold">{t.method}</span><span className="truncate font-medium" title={t.path}>{t.path}</span><Status code={t.status} /></div>
      <div className="mono text-[11px] text-faint mt-0.5">{t.id.slice(0, 12)}</div>
    </Link>
  )
}

function Delta({ label, a, b, d, fmt }: { label: string; a: number; b: number; d: number; fmt: (n: number) => string }) {
  return (
    <div className="card px-3.5 py-3">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className="flex items-baseline gap-2 mt-0.5 num"><span className="text-[15px] text-muted">{fmt(a)}</span><span className="text-faint">→</span><span className="text-[22px] leading-tight font-semibold">{fmt(b)}</span></div>
      <div className={`text-[12px] num mt-0.5 font-medium ${tone(d)}`}>{d === 0 ? 'no change' : `${sign(d)}${fmt(Math.abs(d))}`}</div>
    </div>
  )
}

function Body({ r }: { r: CompareResult }) {
  const kinds = Object.entries(r.by_kind).sort((x, y) => Math.max(y[1].a_ms, y[1].b_ms) - Math.max(x[1].a_ms, x[1].b_ms))
  const sql = [...r.sql].sort((x, y) => Math.abs(y.b_ms - y.a_ms) - Math.abs(x.b_ms - x.a_ms))
  const n = (x: unknown[] | undefined) => x?.length ?? 0
  return (
    <div className="space-y-5">
      <div className="grid sm:grid-cols-2 gap-3"><Side label="A" t={r.a} /><Side label="B" t={r.b} /></div>
      <div className="grid sm:grid-cols-3 gap-3">
        <Delta label="Duration" a={r.a.duration_ms} b={r.b.duration_ms} d={r.delta.duration_ms} fmt={ms} />
        <Delta label="Queries" a={r.a.query_count} b={r.b.query_count} d={r.delta.query_count} fmt={x => String(Math.round(x))} />
        <Delta label="Query time" a={r.a.query_ms} b={r.b.query_ms} d={r.delta.query_ms} fmt={ms} />
      </div>

      <section>
        <h2 className="m-0 mb-2 text-[13px] font-semibold">Time by kind</h2>
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr><th className="th">Kind</th><th className="th text-right">A</th><th className="th text-right">B</th><th className="th text-right">Change</th><th className="th text-right">Count A → B</th></tr></thead>
            <tbody>
              {kinds.length === 0 && <tr><td colSpan={5} className="td text-muted">No spans.</td></tr>}
              {kinds.map(([k, v]) => (
                <tr key={k} className="border-b border-line last:border-0">
                  <td className="td font-medium">{k}</td><td className="td text-right num">{ms(v.a_ms)}</td><td className="td text-right num">{ms(v.b_ms)}</td>
                  <td className={`td text-right num font-medium ${tone(v.b_ms - v.a_ms)}`}>{v.b_ms === v.a_ms ? '–' : sign(v.b_ms - v.a_ms) + ms(Math.abs(v.b_ms - v.a_ms))}</td>
                  <td className="td text-right num text-muted">{v.a_count} → {v.b_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="m-0 mb-2 text-[13px] font-semibold">SQL by fingerprint <span className="text-faint font-normal num">{sql.length}</span></h2>
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr><th className="th">Query</th><th className="th text-right">Count A → B</th><th className="th text-right">Time A</th><th className="th text-right">Time B</th><th className="th text-right">Change</th></tr></thead>
            <tbody>
              {sql.length === 0 && <tr><td colSpan={5} className="td text-muted">No queries in either request.</td></tr>}
              {sql.map(q => (
                <tr key={q.fingerprint} className="border-b border-line last:border-0 align-top">
                  <td className="td !whitespace-normal max-w-[520px]"><Sql sql={q.sql} format={false} className="mono text-[11.5px] line-clamp-2" /></td>
                  <td className={`td text-right num ${q.a_count !== q.b_count ? 'font-semibold' : 'text-muted'}`}>{q.a_count} → {q.b_count}</td>
                  <td className="td text-right num">{ms(q.a_ms)}</td><td className="td text-right num">{ms(q.b_ms)}</td>
                  <td className={`td text-right num font-medium ${tone(q.b_ms - q.a_ms)}`}>{q.b_ms === q.a_ms ? '–' : sign(q.b_ms - q.a_ms) + ms(Math.abs(q.b_ms - q.a_ms))}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="text-[12.5px] text-muted">
        Issues: <b className="text-ink num">{n(r.issues.only_a)}</b> only in A · <b className="text-ink num">{n(r.issues.both)}</b> in both · <b className="text-ink num">{n(r.issues.only_b)}</b> only in B
      </section>
    </div>
  )
}

export default function Compare() {
  const { a = '', b = '' } = useParams()
  const c = useAsync(() => api.compare(a, b), [a, b])
  const back = <Link to="/requests" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />All requests</Link>
  const unsupported = c.error instanceof ApiError && (c.error.status === 404 || c.error.status === 501) && c.error.code !== 'trace_not_found'
  return (
    <div className="space-y-4">
      {back}
      <h1 className="m-0 text-[17px] font-semibold">Compare requests</h1>
      {c.loading && !c.data ? <Spinner />
        : unsupported ? <div className="card"><Empty title="Compare needs a newer backend" icon={<Icon d={I.zap} size={28} />}><p>This Redline server does not offer <code>api/compare</code> yet. Upgrade <code>redline-core</code> and the framework adapter to v0.2 or later.</p></Empty></div>
        : c.error ? <ErrorBox error={c.error} onRetry={c.reload} />
        : c.data ? <Body r={c.data} /> : null}
    </div>
  )
}
