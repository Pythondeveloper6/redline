import { useRef, useState } from 'react'
import { ms, pct } from '../lib/format'
import type { RunStep } from '../types'
import { niceMax, useWidth } from './TimeChart'

const PAD_L = 48, PAD_R = 52

/**
 * Per-step load chart: target vs achieved RPS as bars (left axis), p95/p99 as lines (right axis,
 * ms), an error-rate strip underneath, and the knee step highlighted.
 */
export default function RunChart({ steps, knee }: { steps: RunStep[]; knee: number | null | undefined }) {
  const box = useRef<HTMLDivElement>(null)
  const width = useWidth(box)
  const [hover, setHover] = useState<number | null>(null)
  const H = 264, errH = 44, padT = 22, gap = 22
  const plotW = Math.max(10, width - PAD_L - PAD_R), plotH = H - padT - errH - gap - 14
  const n = Math.max(1, steps.length), band = plotW / n
  const maxRps = niceMax(Math.max(1, ...steps.map(s => Math.max(s.target_rps, s.achieved_rps))))
  const maxMs = niceMax(Math.max(1, ...steps.map(s => s.p99_ms)))
  const maxErr = Math.max(0.01, ...steps.map(s => s.error_rate))
  const cx = (i: number) => PAD_L + band * (i + 0.5)
  const yR = (v: number) => padT + plotH - (v / maxRps) * plotH
  const yM = (v: number) => padT + plotH - (v / maxMs) * plotH
  const errTop = padT + plotH + gap
  const bw = Math.min(28, band * 0.7)
  const kneeIdx = knee == null ? -1 : steps.findIndex(s => s.step === knee)
  const line = (get: (s: RunStep) => number) => steps.map((s, i) => `${i ? 'L' : 'M'}${cx(i).toFixed(1)},${yM(get(s)).toFixed(1)}`).join('')
  const labelEvery = Math.ceil(n / Math.max(1, Math.floor(plotW / 44)))
  const h = hover != null ? steps[hover] : null

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mb-2 text-[11.5px] text-muted">
        <Key color="rgb(var(--faint))" label="Target RPS" /><Key color="rgb(var(--info))" label="Achieved RPS" />
        <Key color="rgb(var(--warn))" label="p95" line /><Key color="rgb(var(--bad))" label="p99" line />
        <Key color="rgb(var(--bad))" label="Error rate" />
        {kneeIdx >= 0 && <span className="inline-flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-accent/20 border border-accent/50" />Knee (step {knee})</span>}
      </div>
      <div ref={box} className="relative w-full">
        <svg width={width} height={H} className="block" role="img" aria-label="Load test steps: RPS, latency and error rate">
          {kneeIdx >= 0 && (
            <g>
              <rect x={PAD_L + band * kneeIdx} y={padT - 8} width={band} height={H - padT + 8} fill="rgb(var(--accent))" opacity="0.1" />
              <text x={cx(kneeIdx)} y={10} textAnchor="middle" fontSize="10" fontWeight="700" fill="rgb(var(--accent))">KNEE</text>
            </g>
          )}
          {[0, 0.5, 1].map(f => (
            <g key={f}>
              <line x1={PAD_L} x2={width - PAD_R} y1={yR(maxRps * f)} y2={yR(maxRps * f)} stroke="rgb(var(--line))" />
              <text x={PAD_L - 6} y={yR(maxRps * f) + 3.5} textAnchor="end" fontSize="10" fill="rgb(var(--faint))" className="num">{Math.round(maxRps * f)}</text>
              <text x={width - PAD_R + 6} y={yM(maxMs * f) + 3.5} fontSize="10" fill="rgb(var(--faint))" className="num">{ms(maxMs * f)}</text>
            </g>
          ))}
          <text x={PAD_L - 6} y={padT - 8} textAnchor="end" fontSize="9.5" fill="rgb(var(--faint))">rps</text>
          {steps.map((s, i) => (
            <g key={s.step}>
              <rect x={cx(i) - bw / 2} y={yR(s.target_rps)} width={bw} height={Math.max(0, padT + plotH - yR(s.target_rps))} fill="rgb(var(--faint))" opacity="0.28" />
              <rect x={cx(i) - bw / 2 + bw * 0.2} y={yR(s.achieved_rps)} width={bw * 0.6} height={Math.max(0, padT + plotH - yR(s.achieved_rps))} fill="rgb(var(--info))" opacity="0.85" />
              <rect x={cx(i) - bw / 2} y={errTop + errH - (s.error_rate / maxErr) * errH} width={bw} height={(s.error_rate / maxErr) * errH} fill="rgb(var(--bad))" opacity="0.8" />
              {i % labelEvery === 0 && <text x={cx(i)} y={H - 1} textAnchor="middle" fontSize="10" fill="rgb(var(--faint))" className="num">{s.step}</text>}
            </g>
          ))}
          <line x1={PAD_L} x2={width - PAD_R} y1={errTop + errH} y2={errTop + errH} stroke="rgb(var(--line))" />
          <text x={PAD_L - 6} y={errTop + 9} textAnchor="end" fontSize="9.5" fill="rgb(var(--faint))">{pct(maxErr)}</text>
          <path d={line(s => s.p99_ms)} fill="none" stroke="rgb(var(--bad))" strokeWidth="1.8" strokeLinejoin="round" />
          <path d={line(s => s.p95_ms)} fill="none" stroke="rgb(var(--warn))" strokeWidth="1.8" strokeLinejoin="round" />
          {steps.map((s, i) => <circle key={s.step} cx={cx(i)} cy={yM(s.p95_ms)} r={hover === i ? 3.5 : 2} fill="rgb(var(--warn))" />)}
          {hover != null && <rect x={PAD_L + band * hover} y={padT - 8} width={band} height={H - padT + 8} fill="rgb(var(--ink))" opacity="0.06" pointerEvents="none" />}
          <rect x={PAD_L} y={0} width={plotW} height={H} fill="transparent"
            onMouseMove={e => { const r = e.currentTarget.getBoundingClientRect(); setHover(Math.max(0, Math.min(n - 1, Math.floor(((e.clientX - r.left) / r.width) * n)))) }}
            onMouseLeave={() => setHover(null)} />
        </svg>
        {h && hover != null && (
          <div className="pointer-events-none absolute top-0 z-10 card shadow-lg px-2.5 py-2 text-[11.5px] space-y-0.5 min-w-[150px]"
            style={{ left: cx(hover) > width * 0.6 ? undefined : cx(hover) + 12, right: cx(hover) > width * 0.6 ? width - cx(hover) + 12 : undefined }}>
            <div className="font-semibold">Step {h.step}{h.step === knee && <span className="text-accent"> · knee</span>}</div>
            <Row k="Target" v={`${h.target_rps} rps`} /><Row k="Achieved" v={`${h.achieved_rps.toFixed(1)} rps`} />
            <Row k="p95" v={ms(h.p95_ms)} /><Row k="p99" v={ms(h.p99_ms)} /><Row k="Errors" v={pct(h.error_rate)} />
            {h.dropped > 0 && <Row k="Dropped" v={String(h.dropped)} />}
          </div>
        )}
      </div>
    </div>
  )
}

const Row = ({ k, v }: { k: string; v: string }) => <div className="flex justify-between gap-4"><span className="text-muted">{k}</span><span className="num">{v}</span></div>
const Key = ({ color, label, line }: { color: string; label: string; line?: boolean }) => (
  <span className="inline-flex items-center gap-1.5"><span className={line ? 'w-3 h-0.5 rounded' : 'w-2.5 h-2.5 rounded-sm'} style={{ background: color, opacity: line ? 1 : 0.8 }} />{label}</span>
)
