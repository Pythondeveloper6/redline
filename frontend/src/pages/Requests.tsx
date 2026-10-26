import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { ago, ms } from '../lib/format'
import { isTyping, useDebounced, useNow } from '../lib/hooks'
import type { Page, TraceFilters, TraceSummary } from '../types'
import { CopyButton, Empty, ErrorBox, I, Icon, IssueBadge, Kbd, Status } from '../components/ui'

const METHODS = ['', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE']

function DurationBar({ t, max }: { t: TraceSummary; max: number }) {
  const w = Math.max(2, (Math.sqrt(t.duration_ms) / Math.sqrt(max)) * 100)
  const qShare = t.duration_ms > 0 ? Math.min(1, t.query_ms / t.duration_ms) : 0
  const tone = t.duration_ms >= 500 ? 'bg-bad' : t.duration_ms >= 100 ? 'bg-warn' : 'bg-faint'
  return (
    <div className="flex items-center gap-2 sm:min-w-[150px]">
      <div className="hidden sm:block w-20 h-1.5 rounded-full bg-raised overflow-hidden shrink-0" title={`${ms(t.duration_ms)} total, ${ms(t.query_ms)} in queries`}>
        <div className="h-full rounded-full relative overflow-hidden" style={{ width: w + '%' }}>
          <div className={`absolute inset-0 ${tone} opacity-40`} />
          <div className={`absolute inset-y-0 left-0 ${tone}`} style={{ width: qShare * 100 + '%' }} />
        </div>
      </div>
      <span className="num w-[62px] text-right">{ms(t.duration_ms)}</span>
    </div>
  )
}

export function InstallHelp({ framework }: { framework?: string }) {
  const dj = `MIDDLEWARE = [\n    "django_redline.middleware.RedlineMiddleware",\n    # ...\n]\n\n# urls.py\npath("redline/", include("django_redline.urls")),`
  const fa = `from fastapi_redline import install\n\ninstall(app, engine=engine)  # UI at /_redline`
  const show = framework === 'django' ? [['Django', dj]] : framework === 'fastapi' ? [['FastAPI', fa]] : [['Django', dj], ['FastAPI', fa]]
  return (
    <div className="grid gap-3 md:grid-cols-2 text-left w-full max-w-3xl mt-4">
      {show.map(([n, code]) => (
        <div key={n} className="card overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 bg-raised border-b border-line"><span className="text-[11px] font-medium text-muted">{n}</span><CopyButton text={code} className="h-6" /></div>
          <pre className="m-0 p-3 overflow-x-auto text-ink">{code}</pre>
        </div>
      ))}
    </div>
  )
}

export default function Requests() {
  const nav = useNavigate()
  const now = useNow()
  const [f, setF] = useState<TraceFilters>({})
  const [qInput, setQInput] = useState('')
  const q = useDebounced(qInput)
  const filters = useMemo<TraceFilters>(() => ({ ...f, q: q || undefined }), [f, q])
  const hasFilters = Object.values(filters).some(Boolean)
  const [paused, setPaused] = useState(false)
  const [head, setHead] = useState<Page | null>(null)
  const [extra, setExtra] = useState<TraceSummary[]>([])
  const [extraCursor, setExtraCursor] = useState<string | null | undefined>(undefined)
  const [err, setErr] = useState<Error | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [sel, setSel] = useState(0)
  const [fw, setFw] = useState<string>()
  const search = useRef<HTMLInputElement>(null)
  const fkey = JSON.stringify(filters)

  const poll = useCallback(async () => {
    try { const p = await api.traces(filters); setHead(p); setErr(null) } catch (e) { setErr(e as Error) }
  }, [filters])

  useEffect(() => { api.meta().then(m => setFw(m.framework), () => {}) }, [])
  useEffect(() => { setExtra([]); setExtraCursor(undefined); setSel(0); setHead(null); void poll() }, [fkey]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (paused) return
    const t = setInterval(poll, 2000)
    return () => clearInterval(t)
  }, [paused, poll])

  const items = useMemo(() => {
    const seen = new Set<string>(); const out: TraceSummary[] = []
    for (const t of [...(head?.items ?? []), ...extra]) if (!seen.has(t.id)) { seen.add(t.id); out.push(t) }
    return out
  }, [head, extra])
  const cursor = extraCursor !== undefined ? extraCursor : head?.next_cursor ?? null
  const max = useMemo(() => Math.max(1, ...items.map(i => i.duration_ms)), [items])

  const loadMore = async () => {
    if (!cursor) return
    setLoadingMore(true)
    try { const p = await api.traces(filters, cursor); setExtra(e => [...e, ...p.items]); setExtraCursor(p.next_cursor) } catch (e) { setErr(e as Error) } finally { setLoadingMore(false) }
  }

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === '/' && !isTyping(e)) { e.preventDefault(); search.current?.focus(); return }
      if (e.key === 'Escape' && isTyping(e)) { (e.target as HTMLElement).blur(); return }
      if (isTyping(e) || e.metaKey || e.ctrlKey) return
      if (e.key === 'j') setSel(s => Math.min(items.length - 1, s + 1))
      else if (e.key === 'k') setSel(s => Math.max(0, s - 1))
      else if (e.key === 'Enter' && items[sel]) nav('/requests/' + items[sel].id)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [items, sel, nav])
  useEffect(() => { document.querySelector(`tr[data-sel="true"]`)?.scrollIntoView({ block: 'nearest' }) }, [sel])

  const set = (k: keyof TraceFilters) => (v: string) => setF(o => ({ ...o, [k]: v || undefined }))
  const empty = head && head.total === 0 && !hasFilters

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <div className="relative flex-1 min-w-[180px] max-w-sm">
          <Icon d={I.search} size={13} className="absolute left-2 top-[7px] text-faint" />
          <input ref={search} className="field w-full pl-7 pr-7" placeholder="Search path, route or view" value={qInput} onChange={e => setQInput(e.target.value)} aria-label="Search" />
          <span className="absolute right-2 top-[5px]"><Kbd>/</Kbd></span>
        </div>
        <select className="field" value={f.method ?? ''} onChange={e => set('method')(e.target.value)} aria-label="Method">
          {METHODS.map(m => <option key={m} value={m}>{m || 'Any method'}</option>)}
        </select>
        <select className="field" value={f.status ?? ''} onChange={e => set('status')(e.target.value)} aria-label="Status class">
          <option value="">Any status</option><option value="2xx">2xx</option><option value="3xx">3xx</option><option value="4xx">4xx</option><option value="5xx">5xx</option>
        </select>
        <select className="field" value={f.min_ms ?? ''} onChange={e => set('min_ms')(e.target.value)} aria-label="Minimum duration">
          <option value="">Any duration</option><option value="50">≥ 50 ms</option><option value="100">≥ 100 ms</option><option value="500">≥ 500 ms</option><option value="1000">≥ 1 s</option>
        </select>
        <select className="field" value={f.min_queries ?? ''} onChange={e => set('min_queries')(e.target.value)} aria-label="Minimum queries">
          <option value="">Any queries</option><option value="5">≥ 5 queries</option><option value="10">≥ 10 queries</option><option value="25">≥ 25 queries</option><option value="50">≥ 50 queries</option>
        </select>
        <label className="inline-flex items-center gap-1.5 h-7 px-2 rounded-md border border-line bg-surface text-[12px] cursor-pointer select-none">
          <input type="checkbox" className="accent-[rgb(var(--accent))]" checked={f.has_issues === '1'} onChange={e => set('has_issues')(e.target.checked ? '1' : '')} /> Has issues
        </label>
        {hasFilters && <button className="btn btn-ghost" onClick={() => { setF({}); setQInput('') }}>Reset</button>}
        <div className="flex-1" />
        <span className="text-[12px] text-muted num hidden sm:inline">{head ? `${items.length.toLocaleString()} of ${head.total.toLocaleString()}` : ''}</span>
        <button className="btn" onClick={() => setPaused(p => !p)} aria-pressed={paused}>
          {paused ? <Icon d={I.play} size={11} /> : <span className="w-1.5 h-1.5 rounded-full bg-ok live-dot" />}
          {paused ? 'Resume' : 'Live'}
        </button>
      </div>

      {err && !head && <ErrorBox error={err} onRetry={poll} />}
      {empty ? (
        <div className="card"><Empty title="No requests recorded yet" icon={<Icon d={I.zap} size={28} />}>
          <p>Add the Redline middleware, then load any page of your app. Requests appear here within a couple of seconds.</p>
          <div className="flex justify-center"><InstallHelp framework={fw} /></div>
        </Empty></div>
      ) : head && items.length === 0 ? (
        <div className="card"><Empty title="No requests match these filters"><p>Loosen a filter or reset them all.</p></Empty></div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50">
              <tr>
                <th className="th hidden sm:table-cell">Method</th><th className="th">Path</th><th className="th">Status</th><th className="th">Duration</th>
                <th className="th text-right">Queries</th><th className="th text-right hidden md:table-cell">Query time</th>
                <th className="th hidden sm:table-cell">Issues</th><th className="th text-right hidden sm:table-cell">When</th>
              </tr>
            </thead>
            <tbody>
              {!head && Array.from({ length: 8 }, (_, i) => <tr key={i} className="border-b border-line"><td colSpan={4} className="td"><div className="h-3 rounded bg-raised animate-pulse" /></td></tr>)}
              {items.map((t, i) => (
                <tr key={t.id} data-sel={i === sel} onClick={() => nav('/requests/' + t.id)} onMouseEnter={() => setSel(i)}
                  className="border-b border-line last:border-0 cursor-pointer hover:bg-raised/60">
                  <td className="td mono font-semibold text-[11.5px] w-[64px] hidden sm:table-cell">{t.method}</td>
                  <td className="td max-w-[150px] sm:max-w-[420px]">
                    <div className="truncate font-medium" title={t.path}><span className="sm:hidden mono text-[11px] font-semibold mr-1.5">{t.method}</span>{t.path}</div>
                    <div className="sm:hidden flex items-center gap-1 mt-0.5 text-[11px] text-muted">{t.issue_kinds.map(k => <IssueBadge key={k} kind={k} />)}<span className="num">{ago(t.started_at, now)}</span></div>
                    {t.route && t.route !== t.path && <div className="truncate mono text-[11px] text-faint" title={t.route}>{t.route}</div>}
                  </td>
                  <td className="td"><Status code={t.status} /></td>
                  <td className="td"><DurationBar t={t} max={max} /></td>
                  <td className={`td text-right num ${t.query_count >= 25 ? 'text-warn font-semibold' : ''}`}>{t.query_count}</td>
                  <td className="td text-right num text-muted hidden md:table-cell">{ms(t.query_ms)}</td>
                  <td className="td hidden sm:table-cell"><span className="inline-flex gap-1">{t.issue_kinds.map(k => <IssueBadge key={k} kind={k} />)}</span></td>
                  <td className="td text-right text-muted num hidden sm:table-cell">{ago(t.started_at, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {cursor && (
            <div className="p-3 text-center border-t border-line">
              <button className="btn" onClick={loadMore} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Load more'}</button>
            </div>
          )}
        </div>
      )}
      <div className="hidden md:flex gap-4 mt-3 text-[11px] text-faint">
        <span><Kbd>j</Kbd> <Kbd>k</Kbd> move</span><span><Kbd>Enter</Kbd> open</span><span><Kbd>/</Kbd> search</span>
      </div>
    </div>
  )
}
