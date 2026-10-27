export const WINDOWS = ['1h', '24h', '7d']

/** Segmented time-window picker shared by the analysis pages. */
export default function WindowPicker({ value, onChange, options = WINDOWS }: { value: string; onChange: (w: string) => void; options?: string[] }) {
  return (
    <div className="inline-flex rounded-md border border-line p-0.5 bg-surface" role="radiogroup" aria-label="Time window">
      {options.map(w => (
        <button key={w} role="radio" aria-checked={value === w} onClick={() => onChange(w)}
          className={`h-6 px-2.5 rounded text-[12px] font-medium ${value === w ? 'bg-raised text-ink' : 'text-muted hover:text-ink'}`}>{w}</button>
      ))}
    </div>
  )
}

/** Thin horizontal meter. `value` is 0..1. */
export function Meter({ value, tone = 'rgb(var(--info))', className = '' }: { value: number; tone?: string; className?: string }) {
  const v = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return (
    <div className={`h-1.5 rounded-full bg-raised overflow-hidden ${className}`} role="presentation">
      <div className="h-full rounded-full" style={{ width: v * 100 + '%', background: tone }} />
    </div>
  )
}
