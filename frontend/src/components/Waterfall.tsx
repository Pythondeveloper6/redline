import { useMemo, useState } from 'react'
import { KIND_COLOR, ms } from '../lib/format'
import type { Span, SpanKind } from '../types'

interface Row { span: Span; depth: number; hasKids: boolean }

function flatten(spans: Span[], collapsed: Set<string>): Row[] {
  const kids = new Map<string | null, Span[]>()
  const ids = new Set(spans.map(s => s.id))
  for (const s of spans) {
    const p = s.parent_id && ids.has(s.parent_id) ? s.parent_id : null
    kids.set(p, [...(kids.get(p) ?? []), s])
  }
  for (const l of kids.values()) l.sort((a, b) => a.start_ms - b.start_ms)
  const out: Row[] = []
  const walk = (p: string | null, depth: number) => {
    for (const s of kids.get(p) ?? []) {
      const has = (kids.get(s.id)?.length ?? 0) > 0
      out.push({ span: s, depth, hasKids: has })
      if (has && !collapsed.has(s.id)) walk(s.id, depth + 1)
    }
  }
  walk(null, 0)
  return out
}

export default function Waterfall({ spans, total, issueSpanIds, onSelect, selected }: {
  spans: Span[]; total: number; issueSpanIds: Set<string>; onSelect: (s: Span) => void; selected?: string | null
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [hidden, setHidden] = useState<Set<SpanKind>>(new Set())
  const [tip, setTip] = useState<{ s: Span; x: number; y: number } | null>(null)
  const end = Math.max(total, ...spans.map(s => s.start_ms + s.duration_ms), 1)
  const rows = useMemo(() => flatten(spans.filter(s => !hidden.has(s.kind)), collapsed), [spans, collapsed, hidden])
  const kinds = useMemo(() => [...new Set(spans.map(s => s.kind))], [spans])
  const ticks = [0, 0.25, 0.5, 0.75, 1]
  const toggle = <T,>(set: Set<T>, v: T) => { const n = new Set(set); if (n.has(v)) n.delete(v); else n.add(v); return n }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-1.5 mb-3">
        {kinds.map(k => (
          <button key={k} onClick={() => setHidden(h => toggle(h, k))} aria-pressed={!hidden.has(k)}
            className={`inline-flex items-center gap-1.5 h-6 px-2 rounded-md border border-line text-[11.5px] ${hidden.has(k) ? 'opacity-40' : ''}`}>
            <span className="w-2 h-2 rounded-sm" style={{ background: KIND_COLOR[k] }} />{k}
            <span className="text-faint num">{spans.filter(s => s.kind === k).length}</span>
          </button>
        ))}
      </div>
      <div className="card overflow-x-auto">
        <div className="min-w-[640px]">
          <div className="flex border-b border-line bg-raised/50 text-[10.5px] text-faint num">
            <div className="w-[34%] max-w-[360px] shrink-0 px-3 py-1.5 text-[11px] text-muted font-medium">Span</div>
            <div className="flex-1 relative h-7 mr-3">
              {ticks.map(t => <span key={t} className="absolute top-1.5 whitespace-nowrap" style={{ left: t * 100 + '%', transform: t === 1 ? 'translateX(-100%)' : t === 0 ? 'none' : 'translateX(-50%)' }}>{ms(end * t)}</span>)}
            </div>
          </div>
          <div className="relative">
            {rows.map(({ span: s, depth, hasKids }) => {
              const left = (s.start_ms / end) * 100
              const width = Math.max(0.35, (s.duration_ms / end) * 100)
              const flagged = issueSpanIds.has(s.id)
              return (
                <div key={s.id} onClick={() => onSelect(s)} onMouseMove={e => setTip({ s, x: e.clientX, y: e.clientY })} onMouseLeave={() => setTip(null)}
                  className={`flex items-center h-[26px] cursor-pointer border-b border-line/50 hover:bg-raised/70 ${selected === s.id ? 'bg-accent/10' : ''}`}>
                  <div className="w-[34%] max-w-[360px] shrink-0 flex items-center gap-1 px-3 min-w-0" style={{ paddingLeft: 12 + depth * 14 }}>
                    {hasKids
                      ? <button className="w-3.5 text-faint text-[9px] shrink-0" aria-label="Toggle children" onClick={e => { e.stopPropagation(); setCollapsed(c => toggle(c, s.id)) }}>{collapsed.has(s.id) ? '▶' : '▼'}</button>
                      : <span className="w-3.5 shrink-0" />}
                    <span className="w-2 h-2 rounded-sm shrink-0" style={{ background: KIND_COLOR[s.kind] }} />
                    <span className={`truncate text-[12px] ${s.kind === 'sql' ? 'mono !text-[11px]' : ''}`}>{s.name}</span>
                    {flagged && <span className="w-1.5 h-1.5 rounded-full bg-accent shrink-0" title="Part of an issue" />}
                  </div>
                  <div className="flex-1 relative h-full mr-3">
                    {ticks.slice(1, -1).map(t => <span key={t} className="absolute top-0 bottom-0 w-px bg-line/60" style={{ left: t * 100 + '%' }} />)}
                    <span className="absolute top-[6px] h-[14px] rounded-[3px]" style={{ left: left + '%', width: width + '%', background: KIND_COLOR[s.kind], opacity: s.kind === 'middleware' ? 0.45 : 0.9, boxShadow: flagged ? '0 0 0 1.5px rgb(var(--accent))' : undefined }} />
                    {width < 55 && <span className="absolute top-[5px] text-[10.5px] text-muted num whitespace-nowrap" style={left > 70 ? { right: 100 - left + 0.5 + '%' } : { left: left + width + 0.5 + '%' }}>{ms(s.duration_ms)}</span>}
                  </div>
                </div>
              )
            })}
            {rows.length === 0 && <div className="p-6 text-center text-muted">No spans to show.</div>}
          </div>
        </div>
      </div>
      {tip && (
        <div className="fixed z-50 pointer-events-none card shadow-xl px-2.5 py-2 max-w-[360px] text-[12px]"
          style={{ left: Math.max(8, Math.min(tip.x + 14, window.innerWidth - 380)), top: Math.min(tip.y + 14, window.innerHeight - 110) }}>
          <div className="flex items-center gap-1.5 font-semibold"><span className="w-2 h-2 rounded-sm" style={{ background: KIND_COLOR[tip.s.kind] }} />{tip.s.kind}<span className="num ml-auto pl-3">{ms(tip.s.duration_ms)}</span></div>
          <div className="mono text-[11px] text-muted break-all line-clamp-3 mt-1">{tip.s.name}</div>
          <div className="text-faint text-[11px] mt-1 num">starts at {ms(tip.s.start_ms)}{tip.s.callsite ? ` · ${tip.s.callsite.file}:${tip.s.callsite.line}` : ''}</div>
        </div>
      )}
    </div>
  )
}
