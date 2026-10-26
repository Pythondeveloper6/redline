import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ms } from '../lib/format'
import { useIsDark } from '../theme'
import type { Speedscope } from '../types'

interface N { name: string; file?: string; line?: number; frame: number; total: number; self: number; start: number; depth: number; children: N[]; parent: N | null }

function build(p: Speedscope): { root: N; total: number } {
  const prof = p.profiles[0]
  const frames = p.shared.frames
  const mk = (frame: number, parent: N | null): N => ({ name: frame < 0 ? 'all' : frames[frame].name, file: frame < 0 ? undefined : frames[frame].file, line: frame < 0 ? undefined : frames[frame].line, frame, total: 0, self: 0, start: 0, depth: parent ? parent.depth + 1 : 0, children: [], parent })
  const root = mk(-1, null)
  const child = (n: N, f: number) => n.children.find(c => c.frame === f) ?? (n.children.push(mk(f, n)), n.children[n.children.length - 1])
  if (prof.type === 'evented' && prof.events) {
    let stack: N[] = [root]; let last = prof.startValue
    for (const e of prof.events) {
      const top = stack[stack.length - 1]
      top.self += e.at - last; last = e.at
      if (e.type === 'O') stack.push(child(top, e.frame)); else stack = stack.length > 1 ? stack.slice(0, -1) : stack
    }
  } else if (prof.samples) {
    prof.samples.forEach((s, i) => {
      let n = root; for (const f of s) n = child(n, f)
      n.self += prof.weights?.[i] ?? 1
    })
  }
  const fin = (n: N, start: number): number => {
    n.start = start; let t = n.self
    let off = start + n.self
    for (const c of n.children) { const ct = fin(c, off); off += ct; t += ct }
    n.total = t; return t
  }
  const total = fin(root, 0)
  return { root, total }
}

const ROW = 20
function hue(name: string) { let h = 0; for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0; return h % 360 }

export default function Flamegraph({ profile, unit = 'ms' }: { profile: Speedscope; unit?: string }) {
  const { root, total } = useMemo(() => build(profile), [profile])
  const dark = useIsDark()
  const wrap = useRef<HTMLDivElement>(null)
  const cv = useRef<HTMLCanvasElement>(null)
  const [focus, setFocus] = useState<N>(root)
  const [q, setQ] = useState('')
  const [hover, setHover] = useState<{ n: N; x: number; y: number } | null>(null)
  const [width, setWidth] = useState(800)
  const hits = useRef<{ n: N; x: number; w: number; y: number }[]>([])
  useEffect(() => setFocus(root), [root])
  useEffect(() => {
    const el = wrap.current; if (!el) return
    const ro = new ResizeObserver(() => setWidth(el.clientWidth)); ro.observe(el); setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])
  const toMs = unit === 'seconds' ? 1000 : unit === 'microseconds' ? 0.001 : unit === 'nanoseconds' ? 1e-6 : 1
  const fmt = (v: number) => ms(v * toMs)
  const maxDepth = useMemo(() => { let m = 0; const w = (n: N) => { m = Math.max(m, n.depth); n.children.forEach(w) }; w(focus); return m - focus.depth + 1 }, [focus])
  const height = maxDepth * ROW + 2
  const ql = q.trim().toLowerCase()

  const draw = useCallback(() => {
    const c = cv.current; if (!c) return
    const dpr = window.devicePixelRatio || 1
    c.width = width * dpr; c.height = height * dpr
    const g = c.getContext('2d')!; g.scale(dpr, dpr); g.clearRect(0, 0, width, height)
    g.font = '11px ui-monospace, Menlo, monospace'; g.textBaseline = 'middle'
    const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim().split(' ').join(',')
    const scale = width / (focus.total || 1)
    const out: typeof hits.current = []
    const visit = (n: N) => {
      const x = (n.start - focus.start) * scale, w = n.total * scale
      if (x + w < 0 || x > width || w < 0.5) return
      const y = (n.depth - focus.depth) * ROW
      const match = ql && (n.name.toLowerCase().includes(ql) || (n.file ?? '').toLowerCase().includes(ql))
      const dim = ql && !match
      const h = hue(n.name)
      g.globalAlpha = dim ? 0.25 : 1
      g.fillStyle = match ? `rgb(${accent})` : n.frame < 0 ? (dark ? '#3f3f46' : '#d4d4d8') : dark ? `hsl(${h} 38% 36%)` : `hsl(${h} 55% 72%)`
      g.fillRect(x + 0.5, y + 0.5, Math.max(0, w - 1), ROW - 1)
      if (w > 34) {
        g.fillStyle = match ? '#fff' : dark ? '#f4f4f5' : '#18181b'
        g.save(); g.beginPath(); g.rect(x + 3, y, w - 6, ROW); g.clip()
        g.fillText(n.name, x + 4, y + ROW / 2 + 0.5); g.restore()
      }
      g.globalAlpha = 1
      out.push({ n, x, w, y })
      n.children.forEach(visit)
    }
    visit(focus)
    hits.current = out
  }, [width, height, focus, ql, dark])
  useEffect(draw, [draw])

  const find = (e: React.MouseEvent) => {
    const r = cv.current!.getBoundingClientRect(); const x = e.clientX - r.left, y = e.clientY - r.top
    for (let i = hits.current.length - 1; i >= 0; i--) { const h = hits.current[i]; if (x >= h.x && x <= h.x + h.w && y >= h.y && y < h.y + ROW) return h.n }
    return null
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <input className="field w-56" placeholder="Highlight function or file" value={q} onChange={e => setQ(e.target.value)} aria-label="Search profile" />
        <button className="btn" onClick={() => setFocus(root)} disabled={focus === root}>Reset zoom</button>
        <span className="text-[12px] text-muted num">{profile.profiles[0].name} · {fmt(total)} sampled</span>
        {focus !== root && <span className="text-[12px] text-muted">Zoomed to <span className="mono text-ink">{focus.name}</span></span>}
      </div>
      <div ref={wrap} className="card p-1 overflow-hidden relative">
        <canvas ref={cv} style={{ width, height, display: 'block', cursor: 'pointer' }}
          onMouseMove={e => { const n = find(e); setHover(n ? { n, x: e.clientX, y: e.clientY } : null) }}
          onMouseLeave={() => setHover(null)} onClick={e => { const n = find(e); if (n && n.children.length + 1 > 0) setFocus(n) }} />
      </div>
      <div className="text-[11px] text-faint mt-2">Click a frame to zoom into it. Width is time spent in that function and everything it called.</div>
      {hover && (
        <div className="fixed z-50 pointer-events-none card shadow-xl px-2.5 py-2 max-w-[380px] text-[12px]"
          style={{ left: Math.max(8, Math.min(hover.x + 14, window.innerWidth - 400)), top: Math.min(hover.y + 16, window.innerHeight - 120) }}>
          <div className="mono font-semibold break-all">{hover.n.name}</div>
          {hover.n.file && <div className="mono text-[11px] text-muted break-all">{hover.n.file}{hover.n.line ? ':' + hover.n.line : ''}</div>}
          <div className="num text-[11.5px] mt-1 flex gap-3"><span>total {fmt(hover.n.total)} ({((hover.n.total / total) * 100).toFixed(1)}%)</span><span>self {fmt(hover.n.self)}</span></div>
        </div>
      )}
    </div>
  )
}
