import { useEffect, useRef, useState, type RefObject } from 'react'
import type { ReleaseMark } from '../types'

/** Measured width of an element (re-measured on resize). */
export function useWidth(ref: RefObject<HTMLElement | null>, fallback = 600) {
  const [w, setW] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    setW(el.clientWidth || fallback)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setW(el.clientWidth || fallback))
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref, fallback])
  return w
}

/** Round a maximum up to a "nice" axis bound. */
export function niceMax(v: number) {
  if (!(v > 0)) return 1
  const p = Math.pow(10, Math.floor(Math.log10(v)))
  const n = v / p
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p
}

export interface LineDef<P> { label: string; color: string; get: (p: P) => number; bar?: boolean }

interface Hover { idx: number; x: number }

const PAD_L = 46, PAD_R = 10

/** Binary search for the point whose bucket contains t (or the nearest). */
function nearest(ts: number[], t: number) {
  let lo = 0, hi = ts.length - 1
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ts[mid] <= t) lo = mid; else hi = mid - 1 }
  return lo
}

/**
 * One time-series panel: lines or bars, y axis from 0, release markers as dashed vertical
 * lines. Hover is controlled by the parent so several panels share one cursor + tooltip.
 */
export function TimePanel<P extends { t: number }>({ points, bucket, releases, lines, fmt, height = 120, labels = false, hover, onHover, title }: {
  points: P[]; bucket: number; releases: ReleaseMark[]; lines: LineDef<P>[]; fmt: (v: number) => string
  height?: number; labels?: boolean; hover: Hover | null; onHover: (h: Hover | null) => void; title: string
}) {
  const box = useRef<HTMLDivElement>(null)
  const width = useWidth(box)
  const padT = labels ? 20 : 8, padB = 6
  const plotW = Math.max(10, width - PAD_L - PAD_R), plotH = height - padT - padB
  const t0 = points[0]?.t ?? 0, t1 = (points[points.length - 1]?.t ?? 0) + bucket
  const span = Math.max(1, t1 - t0)
  const X = (t: number) => PAD_L + ((t - t0) / span) * plotW
  const max = niceMax(Math.max(0, ...points.flatMap(p => lines.map(l => l.get(p)))))
  const Y = (v: number) => padT + plotH - (v / max) * plotH
  const ts = points.map(p => p.t)
  const ticks = [0, max / 2, max]
  const barW = Math.max(1, (plotW / Math.max(1, points.length)) - (points.length > 200 ? 0 : 1))

  const move = (e: React.MouseEvent<SVGRectElement>) => {
    if (!points.length) return
    const r = e.currentTarget.getBoundingClientRect()
    const t = t0 + ((e.clientX - r.left) / r.width) * span
    const idx = nearest(ts, t)
    onHover({ idx, x: X(points[idx].t + bucket / 2) })
  }
  const marks = releases.filter(r => r.first_seen >= t0 && r.first_seen <= t1)
  let lastLabelX = -1e9

  return (
    <div ref={box} className="relative w-full">
      <svg width={width} height={height} className="block" role="img" aria-label={title}>
        {ticks.map((v, i) => (
          <g key={i}>
            <line x1={PAD_L} x2={width - PAD_R} y1={Y(v)} y2={Y(v)} stroke="rgb(var(--line))" strokeWidth="1" />
            <text x={PAD_L - 6} y={Y(v) + 3.5} textAnchor="end" fontSize="10" fill="rgb(var(--faint))" className="num">{fmt(v)}</text>
          </g>
        ))}
        {lines.map(l => l.bar ? (
          <g key={l.label} fill={l.color} opacity="0.55">
            {points.map(p => {
              const h = Math.max(0, plotH - (Y(l.get(p)) - padT))
              return <rect key={p.t} x={X(p.t)} y={padT + plotH - h} width={barW} height={h} />
            })}
          </g>
        ) : (
          <path key={l.label} d={points.map((p, i) => `${i ? 'L' : 'M'}${(X(p.t + bucket / 2)).toFixed(1)},${Y(l.get(p)).toFixed(1)}`).join('')}
            fill="none" stroke={l.color} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
        ))}
        {marks.map(r => {
          const x = X(r.first_seen)
          const show = labels && x - lastLabelX > 72
          if (show) lastLabelX = x
          const flip = x > width - 90
          return (
            <g key={r.release + r.first_seen}>
              <line x1={x} x2={x} y1={labels ? 14 : padT} y2={padT + plotH} stroke="rgb(var(--muted))" strokeWidth="1" strokeDasharray="3 3" />
              {show && <text x={flip ? x - 3 : x + 3} y={11} fontSize="10" fontWeight="600" textAnchor={flip ? 'end' : 'start'} fill="rgb(var(--muted))">{r.release.length > 14 ? r.release.slice(0, 13) + '…' : r.release}</text>}
              <title>{`Release ${r.release}`}</title>
            </g>
          )
        })}
        {hover && points[hover.idx] && (
          <g pointerEvents="none">
            <line x1={hover.x} x2={hover.x} y1={padT} y2={padT + plotH} stroke="rgb(var(--ink))" strokeOpacity="0.35" />
            {lines.filter(l => !l.bar).map(l => <circle key={l.label} cx={hover.x} cy={Y(l.get(points[hover.idx]))} r="3" fill={l.color} />)}
          </g>
        )}
        <rect x={PAD_L} y={padT} width={plotW} height={plotH} fill="transparent" onMouseMove={move} onMouseLeave={() => onHover(null)} />
      </svg>
    </div>
  )
}
