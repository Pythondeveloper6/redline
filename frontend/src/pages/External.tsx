import { useMemo, useState } from 'react'
import { api } from '../api'
import { isUnsupported } from '../apiLoad'
import { ms, pct } from '../lib/format'
import { registerRoute } from '../lib/routes'
import type { ExternalStat } from '../types'
import WindowPicker from '../components/WindowPicker'
import { Empty, ErrorBox, I, Icon, Pill, Spinner, useAsync } from '../components/ui'

registerRoute({ path: '/external', label: 'External calls', keywords: 'http outbound requests api hosts p95 retries duplicates errors', hint: 'Outbound HTTP latency, errors, retries' })

type Key = 'url_pattern' | 'count' | 'p95_ms' | 'error_rate' | 'duplicates' | 'retries'

export default function External() {
  const [win, setWin] = useState('24h')
  const [key, setKey] = useState<Key>('p95_ms')
  const [dir, setDir] = useState<1 | -1>(-1)
  const d = useAsync(() => api.external(win), [win])
  const rows = useMemo(() => {
    const r = [...(d.data?.items ?? [])]
    r.sort((a, b) => (a[key] < b[key] ? -1 : a[key] > b[key] ? 1 : 0) * dir)
    return r
  }, [d.data, key, dir])
  const th = (k: Key, label: string, right = true, cls = '') => (
    <th className={`th ${right ? 'text-right' : ''} ${cls}`} aria-sort={key === k ? (dir === 1 ? 'ascending' : 'descending') : 'none'}>
      <button className={`inline-flex items-center gap-1 hover:text-ink ${key === k ? 'text-ink' : ''}`} onClick={() => { if (key === k) setDir(x => (x === 1 ? -1 : 1)); else { setKey(k); setDir(k === 'url_pattern' ? 1 : -1) } }}>
        {label}<span className="text-[9px]">{key === k ? (dir === 1 ? '▲' : '▼') : ''}</span>
      </button>
    </th>
  )
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold flex-1">External calls</h1>
        <WindowPicker value={win} onChange={setWin} />
      </div>
      {d.error && (isUnsupported(d.error)
        ? <div className="card"><Empty title="External-call analytics aren't available here"><p>Upgrade <span className="mono">redline-core</span> to use this page.</p></Empty></div>
        : <ErrorBox error={d.error} onRetry={d.reload} />)}
      {d.loading && !d.data ? <Spinner /> : d.data && rows.length === 0 ? (
        <div className="card"><Empty title="No outbound HTTP calls recorded" icon={<Icon d={I.zap} size={28} />}><p>Calls made with <span className="mono">requests</span>, <span className="mono">httpx</span> or <span className="mono">urllib3</span> during a request show up here, grouped by URL pattern.</p></Empty></div>
      ) : d.data && (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr>
              {th('url_pattern', 'Endpoint', false)}{th('count', 'Calls', true, 'hidden sm:table-cell')}{th('p95_ms', 'p95')}<th className="th text-right hidden md:table-cell">Max</th>
              {th('error_rate', 'Errors')}{th('duplicates', 'Duplicates', true, 'hidden sm:table-cell')}{th('retries', 'Retries', true, 'hidden sm:table-cell')}
            </tr></thead>
            <tbody>{rows.map((e: ExternalStat) => (
              <tr key={e.method + e.url_pattern} className="border-b border-line last:border-0 hover:bg-raised/50">
                <td className="td !whitespace-normal max-w-[420px]">
                  <div className="break-all"><span className="mono font-semibold text-[11.5px] mr-2">{e.method}</span><span className="mono">{e.url_pattern}</span></div>
                  <div className="text-[11px] text-faint">{e.host}</div>
                </td>
                <td className="td text-right num hidden sm:table-cell">{e.count.toLocaleString()}</td>
                <td className="td text-right num"><span className={e.p95_ms >= 1000 ? 'text-bad font-semibold' : e.p95_ms >= 300 ? 'text-warn' : ''}>{ms(e.p95_ms)}</span></td>
                <td className="td text-right num hidden md:table-cell text-muted">{ms(e.max_ms)}</td>
                <td className={`td text-right num ${e.error_rate > 0.05 ? 'text-bad font-semibold' : e.error_rate > 0 ? 'text-warn' : 'text-muted'}`}>{pct(e.error_rate)} <span className="text-faint text-[11px]">({e.errors})</span></td>
                <td className="td text-right hidden sm:table-cell">{e.duplicates > 0 ? <Pill tone="warn" title="Requests that called this endpoint 2 or more times">{e.duplicates.toLocaleString()}</Pill> : <span className="text-faint">0</span>}</td>
                <td className="td text-right hidden sm:table-cell">{e.retries > 0 ? <Pill tone="bad" title="Requests with 3 or more calls and at least one error">{e.retries.toLocaleString()}</Pill> : <span className="text-faint">0</span>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="text-[11px] text-faint">Errors are 5xx, 429 and calls that got no response. Duplicates are requests that called the same endpoint twice or more; retries are requests with three or more calls and an error.</div>
    </div>
  )
}
