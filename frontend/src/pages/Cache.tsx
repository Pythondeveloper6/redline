import { useMemo, useState } from 'react'
import { api } from '../api'
import { isUnsupported } from '../apiLoad'
import { pct } from '../lib/format'
import { registerRoute } from '../lib/routes'
import type { CacheStat } from '../types'
import WindowPicker, { Meter } from '../components/WindowPicker'
import { Empty, ErrorBox, I, Icon, Pill, Spinner, useAsync } from '../components/ui'

registerRoute({ path: '/cache', label: 'Cache', keywords: 'hit rate misses stampede unread sets redis memcached', hint: 'Cache hit rates, unread sets, stampedes' })

const rateTone = (r: number | null) => (r == null ? 'rgb(var(--faint))' : r >= 0.9 ? 'rgb(var(--ok))' : r >= 0.6 ? 'rgb(var(--warn))' : 'rgb(var(--bad))')

export default function Cache() {
  const [win, setWin] = useState('24h')
  const [onlyProblems, setOnly] = useState(false)
  const d = useAsync(() => api.cache(win), [win])
  const rows = useMemo(() => {
    let r = [...(d.data?.items ?? [])]
    if (onlyProblems) r = r.filter(c => c.stampedes > 0 || c.unread_sets > 0 || (c.hit_rate != null && c.hit_rate < 0.6))
    return r.sort((a, b) => b.stampedes - a.stampedes || b.gets + b.sets - (a.gets + a.sets))
  }, [d.data, onlyProblems])
  const all = d.data?.items ?? []
  const totalGets = all.reduce((a, c) => a + c.gets, 0), totalHits = all.reduce((a, c) => a + c.hits, 0)
  const stampedes = all.reduce((a, c) => a + c.stampedes, 0), unread = all.reduce((a, c) => a + c.unread_sets, 0)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Cache</h1>
        <label className="inline-flex items-center gap-1.5 h-7 px-2 rounded-md border border-line bg-surface text-[12px] cursor-pointer select-none">
          <input type="checkbox" className="accent-[rgb(var(--accent))]" checked={onlyProblems} onChange={e => setOnly(e.target.checked)} /> Problems only
        </label>
        <WindowPicker value={win} onChange={setWin} />
      </div>
      {d.error && (isUnsupported(d.error)
        ? <div className="card"><Empty title="Cache analytics aren't available here"><p>Upgrade <span className="mono">redline-core</span> to use this page.</p></Empty></div>
        : <ErrorBox error={d.error} onRetry={d.reload} />)}
      {d.loading && !d.data ? <Spinner /> : d.data && all.length === 0 ? (
        <div className="card"><Empty title="No cache activity recorded" icon={<Icon d={I.zap} size={28} />}><p>Cache calls made through Django&apos;s cache framework or an instrumented Redis client show up here once requests have been recorded in the last {win}.</p></Empty></div>
      ) : d.data && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat label="Overall hit rate" value={totalHits + (totalGets - totalHits) > 0 ? pct(totalHits / totalGets) : '-'} tone={rateTone(totalGets ? totalHits / totalGets : null)} />
            <Stat label="Reads" value={totalGets.toLocaleString()} />
            <Stat label="Stampedes" value={String(stampedes)} bad={stampedes > 0} />
            <Stat label="Unread sets" value={unread.toLocaleString()} warn={unread > 0} />
          </div>
          {rows.length === 0 ? <div className="card"><Empty title="No problem patterns" icon={<Icon d={I.check} size={28} />}><p>Every key pattern reads well.</p></Empty></div> : (
            <div className="card overflow-x-auto">
              <table className="w-full border-collapse text-[12.5px]">
                <thead className="border-b border-line bg-raised/50"><tr>
                  <th className="th">Key pattern</th><th className="th w-[190px]">Hit rate</th><th className="th text-right hidden sm:table-cell">Reads</th>
                  <th className="th text-right hidden md:table-cell">Misses</th><th className="th text-right hidden md:table-cell">Sets</th><th className="th text-right">Unread sets</th><th className="th text-right">Stampedes</th>
                </tr></thead>
                <tbody>{rows.map((c: CacheStat) => (
                  <tr key={c.backend + c.key_pattern} className={`border-b border-line last:border-0 hover:bg-raised/50 ${c.stampedes > 0 ? 'bg-bad/5' : ''}`}>
                    <td className="td !whitespace-normal max-w-[360px]"><div className="mono break-all">{c.key_pattern}</div><div className="text-[11px] text-faint">{c.backend}</div></td>
                    <td className="td">
                      <div className="flex items-center gap-2">
                        <Meter value={c.hit_rate ?? 0} tone={rateTone(c.hit_rate)} className="w-28 shrink-0" />
                        <span className="num w-12 text-right font-semibold">{c.hit_rate == null ? '-' : pct(c.hit_rate)}</span>
                      </div>
                    </td>
                    <td className="td text-right num hidden sm:table-cell">{c.gets.toLocaleString()}</td>
                    <td className="td text-right num hidden md:table-cell text-muted">{c.misses.toLocaleString()}</td>
                    <td className="td text-right num hidden md:table-cell text-muted">{c.sets.toLocaleString()}</td>
                    <td className="td text-right">{c.unread_sets > 0 ? <Pill tone="warn" title="Values written but never read in this window: wasted work">{c.unread_sets.toLocaleString()}</Pill> : <span className="text-faint">0</span>}</td>
                    <td className="td text-right">{c.stampedes > 0 ? <Pill tone="bad" title="Several requests missed this key within a second">{c.stampedes}</Pill> : <span className="text-faint">0</span>}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          <div className="text-[11px] text-faint">A stampede is a burst of 3 or more requests missing the same key pattern within a second. Unread sets are cache writes that nothing read in this window.</div>
        </>
      )}
    </div>
  )
}

function Stat({ label, value, tone, bad, warn }: { label: string; value: string; tone?: string; bad?: boolean; warn?: boolean }) {
  return (
    <div className="card px-3.5 py-3">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className={`text-[22px] leading-tight font-semibold num mt-0.5 ${bad ? 'text-bad' : warn ? 'text-warn' : ''}`} style={tone ? { color: tone } : undefined}>{value}</div>
    </div>
  )
}
