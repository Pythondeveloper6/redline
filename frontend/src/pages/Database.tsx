import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { isUnsupported } from '../apiLoad'
import { bytes, ms } from '../lib/format'
import { useDebounced } from '../lib/hooks'
import { registerRoute } from '../lib/routes'
import type { IndexCandidate, SqlStat } from '../types'
import WindowPicker, { Meter } from '../components/WindowPicker'
import { CopyButton, Empty, ErrorBox, I, Icon, Pill, Spinner, Sql, Tabs, useAsync } from '../components/ui'

registerRoute({ path: '/database', label: 'Database', keywords: 'sql queries index advisor unused indexes fingerprints', hint: 'SQL search and index advice' })

type Tab = 'queries' | 'indexes' | 'unused'

function QueriesTab({ win }: { win: string }) {
  const nav = useNavigate()
  const [qIn, setQIn] = useState('')
  const q = useDebounced(qIn)
  const [open, setOpen] = useState<string | null>(null)
  const d = useAsync(() => api.sql({ q: q || undefined, window: win }), [q, win])
  const items = d.data?.items ?? []
  const max = Math.max(1, ...items.map(i => i.total_ms))
  return (
    <div className="space-y-3">
      <div className="relative max-w-md">
        <Icon d={I.search} size={13} className="absolute left-2 top-[7px] text-faint" />
        <input className="field w-full pl-7" placeholder="Search SQL (table, column, keyword)" value={qIn} onChange={e => setQIn(e.target.value)} aria-label="Search SQL" />
      </div>
      {d.error && <ErrorBox error={d.error} onRetry={d.reload} />}
      {d.loading && !d.data ? <Spinner /> : d.data && items.length === 0 ? (
        <div className="card"><Empty title={q ? 'No query matches' : 'No queries in this window'}><p>{q ? 'Try a table or column name.' : `Queries appear once requests have been recorded in the last ${win}.`}</p></Empty></div>
      ) : d.data && (
        <div className="card divide-y divide-line">
          {items.map((s: SqlStat) => {
            const isOpen = open === s.fingerprint
            return (
              <div key={s.fingerprint}>
                <div className="flex items-start gap-2 px-3 py-2.5 hover:bg-raised/50 cursor-pointer" onClick={() => nav(`/requests/${s.sample_trace_id}?tab=sql`)}
                  role="link" tabIndex={0} onKeyDown={e => { if (e.key === 'Enter') nav(`/requests/${s.sample_trace_id}?tab=sql`) }} title="Open a sample request">
                  <button className="mt-0.5 text-faint shrink-0" aria-expanded={isOpen} aria-label={isOpen ? 'Collapse SQL' : 'Expand SQL'}
                    onClick={e => { e.stopPropagation(); setOpen(isOpen ? null : s.fingerprint) }}><Icon d={I.chev} size={12} className={isOpen ? 'rotate-90' : ''} /></button>
                  <div className="flex-1 min-w-0">
                    <div className={`mono text-[11.5px] ${isOpen ? 'whitespace-pre-wrap break-words' : 'truncate'}`}>{isOpen ? <Sql sql={s.sql} /> : s.sql}</div>
                    <div className="flex flex-wrap items-center gap-1 mt-1">
                      {s.routes.slice(0, 3).map(r => <span key={r} className="mono rounded bg-raised px-1.5 text-[10.5px] text-muted">{r}</span>)}
                      {s.routes.length > 3 && <span className="text-[11px] text-faint">+{s.routes.length - 3}</span>}
                      {s.vendor && s.vendor !== 'other' && <Pill>{s.vendor}</Pill>}
                    </div>
                    {isOpen && (
                      <div className="flex flex-wrap items-center gap-2 mt-2" onClick={e => e.stopPropagation()}>
                        <CopyButton text={s.sql} label="Copy SQL" className="h-6" />
                        <Link className="btn h-6" to={`/requests/${s.sample_trace_id}?tab=sql`}>Open sample request</Link>
                        {s.mean_rows != null && <span className="text-[11.5px] text-muted num">~{s.mean_rows.toLocaleString()} rows per call</span>}
                      </div>
                    )}
                  </div>
                  <div className="grid grid-cols-[auto] sm:grid-cols-[64px_74px_74px] gap-x-3 text-right num shrink-0 text-[12px]">
                    <div><div className="font-semibold">{ms(s.total_ms)}</div><div className="text-[10.5px] text-faint sm:hidden">{s.count.toLocaleString()}×</div></div>
                    <div className="hidden sm:block"><div>{s.count.toLocaleString()}×</div><div className="text-[10.5px] text-faint">{s.traces.toLocaleString()} req</div></div>
                    <div className="hidden sm:block"><div>{ms(s.mean_ms ?? s.total_ms / Math.max(1, s.count))}</div><div className="text-[10.5px] text-faint">max {ms(s.max_ms)}</div></div>
                    <div className="col-span-full sm:col-span-3"><Meter value={s.total_ms / max} tone="rgb(var(--info))" className="mt-1" /></div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}
      {items.length > 0 && <div className="text-[11px] text-faint">Columns: total time, executions (requests), mean (max). Click a row to open a sample request.</div>}
    </div>
  )
}

function CandidateCard({ c }: { c: IndexCandidate }) {
  return (
    <div className="card p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono font-semibold">{c.table}</span>
        <span className="text-muted">({c.columns.join(', ')})</span>
        {c.kind && <Pill tone="info">{c.kind}</Pill>}
        {c.confidence && <Pill tone={c.confidence === 'high' ? 'ok' : c.confidence === 'medium' ? 'warn' : 'neutral'}>{c.confidence} confidence</Pill>}
        {c.verified && <Pill tone="ok"><Icon d={I.check} size={10} />verified</Pill>}
        <Pill>{c.vendor}</Pill>
        <span className="flex-1" />
        <span className="num text-[12px] text-muted">saves ~<b className="text-ok">{ms(c.est_saving_ms)}</b> of {ms(c.total_ms)}</span>
      </div>
      <div className="flex items-start gap-2 rounded-md bg-raised border border-line px-3 py-2">
        <pre className="m-0 flex-1 overflow-x-auto whitespace-pre-wrap break-words">{c.create_sql}</pre>
        <CopyButton text={c.create_sql} label="Copy SQL" className="h-6 shrink-0" />
      </div>
      <div className="text-[11.5px] text-muted num">Covers {c.covers.length} {c.covers.length === 1 ? 'query' : 'queries'} · {c.queries.toLocaleString()} executions in the window</div>
    </div>
  )
}

function Unavailable({ error, onRetry, what }: { error: Error; onRetry: () => void; what: string }) {
  return isUnsupported(error)
    ? <div className="card"><Empty title={`${what} isn't available here`}><p>This backend or storage can&apos;t compute it (HTTP {(error as Error & { status?: number }).status}). Upgrade <span className="mono">redline-core</span>, or check that the store supports workload analysis.</p></Empty></div>
    : <ErrorBox error={error} onRetry={onRetry} />
}

function IndexesTab({ win }: { win: string }) {
  const d = useAsync(() => api.indexes(win), [win])
  if (d.error) return <Unavailable error={d.error} onRetry={d.reload} what="The index advisor" />
  if (d.loading && !d.data) return <Spinner />
  const cands = d.data?.candidates ?? []
  if (cands.length === 0) return <div className="card"><Empty title="No index suggestions" icon={<Icon d={I.check} size={28} />}><p>No recorded query pattern in the last {win} looks like it needs a new index. Check back once more traffic has been recorded.</p></Empty></div>
  return <div className="space-y-3">{cands.map(c => <CandidateCard key={c.table + c.columns.join(',') + c.create_sql} c={c} />)}</div>
}

function UnusedTab({ win }: { win: string }) {
  const d = useAsync(() => api.indexes(win), [win])
  if (d.error) return <Unavailable error={d.error} onRetry={d.reload} what="Unused-index detection" />
  if (d.loading && !d.data) return <Spinner />
  if (!d.data?.unused_supported) return <div className="card"><Empty title="Unused-index detection isn't supported for this database"><p>Redline reads index statistics where the database exposes them (for example PostgreSQL&apos;s <span className="mono">pg_stat_user_indexes</span>). It is not available for SQLite or MySQL here.</p></Empty></div>
  const rows = d.data.unused
  if (rows.length === 0) return <div className="card"><Empty title="No unused indexes" icon={<Icon d={I.check} size={28} />}><p>Every index has been scanned at least once.</p></Empty></div>
  return (
    <div className="card overflow-x-auto">
      <table className="w-full border-collapse text-[12.5px]">
        <thead className="border-b border-line bg-raised/50"><tr><th className="th">Table</th><th className="th">Index</th><th className="th text-right">Scans</th><th className="th text-right">Size</th><th className="th" /></tr></thead>
        <tbody>{rows.map(u => (
          <tr key={u.table + u.index} className="border-b border-line last:border-0 hover:bg-raised/50">
            <td className="td mono">{u.table}</td><td className="td mono">{u.index}</td>
            <td className="td text-right num">{u.scans ?? '-'}</td><td className="td text-right num">{bytes(u.size_bytes)}</td>
            <td className="td text-right"><CopyButton text={u.drop_sql ?? `DROP INDEX ${u.index};`} label="Copy DROP" className="h-6" /></td>
          </tr>
        ))}</tbody>
      </table>
      <div className="px-3 py-2 text-[11px] text-muted border-t border-line">Statistics reset on restart or failover. Confirm an index is unused on a replica and across a full business cycle before dropping it.</div>
    </div>
  )
}

export default function Database() {
  const [sp, setSp] = useSearchParams()
  const tab = (sp.get('tab') as Tab) || 'queries'
  const [win, setWin] = useState('24h')
  const setTab = (t: Tab) => setSp(t === 'queries' ? {} : { tab: t }, { replace: true })
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Database</h1>
        <WindowPicker value={win} onChange={setWin} />
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[{ id: 'queries', label: 'Queries' }, { id: 'indexes', label: 'Index advisor' }, { id: 'unused', label: 'Unused indexes' }]} />
      <div className="pt-4">
        {tab === 'queries' && <QueriesTab win={win} />}
        {tab === 'indexes' && <IndexesTab win={win} />}
        {tab === 'unused' && <UnusedTab win={win} />}
      </div>
    </div>
  )
}
