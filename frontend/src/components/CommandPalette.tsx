import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { ago, ms } from '../lib/format'
import { fuzzyScore } from '../lib/fuzzy'
import { getRoutes } from '../lib/routes'
import { feature, logout } from '../lib/meta'
import type { EndpointStats, IssueGroup, Meta, TraceSummary } from '../types'
import { Modal } from './Modal'
import { I, Icon, Kbd, Status } from './ui'

type Group = 'Pages' | 'Actions' | 'Requests' | 'Endpoints' | 'Issues'
interface Item { id: string; group: Group; label: string; hint?: string; extra?: string; to: string; status?: number; run?: () => void }

interface Data { traces: TraceSummary[]; endpoints: EndpointStats[]; issues: IssueGroup[] }
let cache: { at: number; data: Data } | null = null

async function loadData(): Promise<Data> {
  if (cache && Date.now() - cache.at < 15000) return cache.data
  const [t, e, i] = await Promise.allSettled([api.traces({}, null, 50), api.endpoints('24h'), api.issues('24h')])
  const data: Data = {
    traces: t.status === 'fulfilled' ? t.value.items : [],
    endpoints: e.status === 'fulfilled' ? e.value.items : [],
    issues: i.status === 'fulfilled' ? i.value.items : [],
  }
  cache = { at: Date.now(), data }
  return data
}

const GROUP_ORDER: Group[] = ['Pages', 'Actions', 'Requests', 'Endpoints', 'Issues']
const PER_GROUP = 8

export default function CommandPalette({ open, onClose, meta }: { open: boolean; onClose: () => void; meta?: Meta }) {
  const nav = useNavigate()
  const [q, setQ] = useState('')
  const [data, setData] = useState<Data | null>(cache?.data ?? null)
  const [active, setActive] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    setQ(''); setActive(0)
    let live = true
    loadData().then(d => live && setData(d))
    return () => { live = false }
  }, [open])

  const all = useMemo<Item[]>(() => {
    const items: Item[] = getRoutes().filter(r => r.path !== '/load' || feature(meta, 'load')).map(r => ({ id: 'p' + r.path, group: 'Pages', label: r.label, hint: r.hint, extra: r.keywords, to: r.path }))
    if (meta?.access?.login) items.push({ id: 'a-logout', group: 'Actions', label: 'Log out', hint: 'End this session', extra: 'sign out logout', to: '', run: logout })
    for (const t of data?.traces ?? []) items.push({
      id: 't' + t.id, group: 'Requests', label: `${t.method} ${t.path}`, status: t.status,
      hint: `${ms(t.duration_ms)} · ${t.query_count} queries · ${ago(t.started_at)}`, extra: `${t.route ?? ''} ${t.view ?? ''} ${t.status}`, to: '/requests/' + t.id,
    })
    for (const e of data?.endpoints ?? []) items.push({
      id: 'e' + e.method + e.route, group: 'Endpoints', label: `${e.method} ${e.route}`,
      hint: `p95 ${ms(e.p95_ms)} · ${e.count.toLocaleString()} req`, to: '/requests?route=' + encodeURIComponent(e.route),
    })
    for (const g of data?.issues ?? []) items.push({
      id: 'i' + g.id, group: 'Issues', label: g.title, hint: `${g.occurrences.toLocaleString()}× · ${g.kind.replace(/_/g, ' ')}`,
      extra: g.routes.join(' '), to: `/requests/${g.sample_trace_id}?tab=issues`,
    })
    return items
  }, [data, meta])

  const results = useMemo<Item[]>(() => {
    const query = q.trim()
    const by = new Map<Group, { it: Item; s: number }[]>()
    for (const it of all) {
      const s = query ? Math.max(fuzzyScore(query, it.label), fuzzyScore(query, it.label + ' ' + (it.extra ?? '')) - 50) : 0
      if (query && s < 0) continue
      by.set(it.group, [...(by.get(it.group) ?? []), { it, s }])
    }
    const out: Item[] = []
    for (const g of GROUP_ORDER) {
      const rows = by.get(g) ?? []
      if (query) rows.sort((a, b) => b.s - a.s)
      out.push(...rows.slice(0, g === 'Pages' ? 12 : PER_GROUP).map(r => r.it))
    }
    return out
  }, [all, q])

  useEffect(() => { setActive(0) }, [q])
  useEffect(() => { listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [active, results])

  const go = (it: Item | undefined) => { if (!it) return; onClose(); if (it.run) it.run(); else nav(it.to) }
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(a => Math.min(results.length - 1, a + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(a => Math.max(0, a - 1)) }
    else if (e.key === 'Home') { e.preventDefault(); setActive(0) }
    else if (e.key === 'End') { e.preventDefault(); setActive(Math.max(0, results.length - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); go(results[active]) }
  }

  let lastGroup: Group | null = null
  return (
    <Modal open={open} onClose={onClose} width="max-w-xl" top label="Command palette">
      <div className="flex items-center gap-2 px-3 border-b border-line">
        <Icon d={I.search} size={14} className="text-faint shrink-0" />
        <input data-autofocus autoFocus value={q} onChange={e => setQ(e.target.value)} onKeyDown={onKey}
          role="combobox" aria-expanded="true" aria-controls="palette-list" aria-autocomplete="list" aria-label="Search pages, requests, endpoints and issues"
          aria-activedescendant={results[active] ? 'pal-' + results[active].id : undefined}
          placeholder="Search pages, requests, endpoints, issues…" className="flex-1 h-11 bg-transparent outline-none focus-visible:outline-none text-[13.5px] placeholder:text-faint" />
        <Kbd>Esc</Kbd>
      </div>
      <div ref={listRef} id="palette-list" role="listbox" aria-label="Results" className="overflow-y-auto py-1 max-h-[55vh]">
        {results.length === 0 && <div className="px-4 py-8 text-center text-muted text-[12.5px]">{data ? 'No matches.' : 'Loading…'}</div>}
        {results.map((it, i) => {
          const head = it.group !== lastGroup ? it.group : null
          lastGroup = it.group
          return (
            <div key={it.id}>
              {head && <div className="px-3 pt-2 pb-1 text-[10.5px] font-semibold uppercase tracking-wide text-faint" role="presentation">{head}</div>}
              <div id={'pal-' + it.id} role="option" aria-selected={i === active} onMouseMove={() => i !== active && setActive(i)} onClick={() => go(it)}
                className={`mx-1 px-2.5 py-1.5 rounded-md cursor-pointer flex items-center gap-2 ${i === active ? 'bg-accent/10 text-ink' : 'text-ink'}`}>
                {it.status != null && <Status code={it.status} />}
                <span className={`flex-1 min-w-0 truncate ${it.group === 'Pages' || it.group === 'Actions' ? 'font-medium' : 'mono text-[12px]'}`}>{it.label}</span>
                {it.hint && <span className="hidden sm:block text-[11px] text-muted truncate max-w-[45%] num">{it.hint}</span>}
              </div>
            </div>
          )
        })}
      </div>
      <div className="flex items-center gap-3 px-3 py-2 border-t border-line text-[11px] text-faint">
        <span><Kbd>↑</Kbd> <Kbd>↓</Kbd> move</span><span><Kbd>Enter</Kbd> open</span><span className="ml-auto"><Kbd>?</Kbd> shortcuts</span>
      </div>
    </Modal>
  )
}
