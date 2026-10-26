import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ms, pct } from '../lib/format'
import type { EndpointStats } from '../types'
import Sparkline from '../components/Sparkline'
import { Empty, ErrorBox, Pill, Spinner, useAsync } from '../components/ui'

type Key = 'route' | 'count' | 'error_rate' | 'p50_ms' | 'p95_ms' | 'p99_ms' | 'avg_queries' | 'max_queries' | 'issue_count'
const WINDOWS = ['1h', '24h', '7d']

export default function Endpoints() {
  const [win, setWin] = useState('24h')
  const [key, setKey] = useState<Key>('p95_ms')
  const [dir, setDir] = useState<1 | -1>(-1)
  const d = useAsync(() => api.endpoints(win), [win])
  const rows = useMemo(() => {
    const r = [...(d.data?.items ?? [])]
    r.sort((a, b) => (a[key] < b[key] ? -1 : a[key] > b[key] ? 1 : 0) * dir)
    return r
  }, [d.data, key, dir])
  const maxP95 = Math.max(1, ...rows.map(r => r.p95_ms))
  const th = (k: Key, label: string, right = true, cls = '') => (
    <th className={`th ${right ? 'text-right' : ''} ${cls}`} aria-sort={key === k ? (dir === 1 ? 'ascending' : 'descending') : 'none'}>
      <button className={`inline-flex items-center gap-1 hover:text-ink ${key === k ? 'text-ink' : ''}`} onClick={() => { if (key === k) setDir(x => (x === 1 ? -1 : 1)); else { setKey(k); setDir(k === 'route' ? 1 : -1) } }}>
        {label}<span className="text-[9px]">{key === k ? (dir === 1 ? '▲' : '▼') : ''}</span>
      </button>
    </th>
  )
  const row = (r: EndpointStats) => (
    <tr key={r.method + r.route} className="border-b border-line last:border-0 hover:bg-raised/50">
      <td className="td"><Link to={`/requests`} className="hover:underline"><span className="mono font-semibold text-[11.5px] mr-2">{r.method}</span><span className="font-medium">{r.route}</span></Link></td>
      <td className="td text-right num">{r.count.toLocaleString()}</td>
      <td className={`td text-right num ${r.error_rate > 0.02 ? 'text-bad font-semibold' : r.error_rate > 0 ? 'text-warn' : 'text-muted'}`}>{pct(r.error_rate)}</td>
      <td className="td text-right num hidden md:table-cell">{ms(r.p50_ms)}</td>
      <td className="td text-right num"><span className={r.p95_ms >= 500 ? 'text-bad font-semibold' : r.p95_ms >= 100 ? 'text-warn' : ''}>{ms(r.p95_ms)}</span>
        <div className="h-0.5 rounded bg-raised mt-1 ml-auto w-16"><div className="h-full rounded bg-faint" style={{ width: (r.p95_ms / maxP95) * 100 + '%' }} /></div></td>
      <td className="td text-right num hidden md:table-cell">{ms(r.p99_ms)}</td>
      <td className="td text-right num hidden lg:table-cell">{r.avg_queries.toFixed(1)}</td>
      <td className={`td text-right num hidden lg:table-cell ${r.max_queries >= 50 ? 'text-warn font-semibold' : ''}`}>{r.max_queries}</td>
      <td className="td text-right">{r.issue_count ? <Pill tone="bad">{r.issue_count}</Pill> : <span className="text-faint">0</span>}</td>
      <td className="td hidden sm:table-cell"><Sparkline values={r.series.map(s => s.p95_ms)} /></td>
    </tr>
  )
  return (
    <div>
      <div className="flex items-center gap-3 mb-3">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Endpoints</h1>
        <div className="inline-flex rounded-md border border-line p-0.5 bg-surface" role="radiogroup" aria-label="Time window">
          {WINDOWS.map(w => <button key={w} role="radio" aria-checked={win === w} onClick={() => setWin(w)} className={`h-6 px-2.5 rounded text-[12px] font-medium ${win === w ? 'bg-raised text-ink' : 'text-muted hover:text-ink'}`}>{w}</button>)}
        </div>
      </div>
      {d.error && <ErrorBox error={d.error} onRetry={d.reload} />}
      {d.loading && !d.data ? <Spinner /> : d.data && rows.length === 0 ? (
        <div className="card"><Empty title="No endpoint data in this window"><p>Endpoints appear once requests have been recorded in the last {win}.</p></Empty></div>
      ) : d.data && (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr>
              {th('route', 'Route', false)}{th('count', 'Requests')}{th('error_rate', 'Errors')}{th('p50_ms', 'p50', true, 'hidden md:table-cell')}{th('p95_ms', 'p95')}{th('p99_ms', 'p99', true, 'hidden md:table-cell')}
              {th('avg_queries', 'Avg queries', true, 'hidden lg:table-cell')}{th('max_queries', 'Max queries', true, 'hidden lg:table-cell')}{th('issue_count', 'Issues')}
              <th className="th hidden sm:table-cell">p95 trend</th>
            </tr></thead>
            <tbody>{rows.map(row)}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}
