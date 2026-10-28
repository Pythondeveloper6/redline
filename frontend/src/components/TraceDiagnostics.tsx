import { ms } from '../lib/format'
import type { Trace } from '../types'

export function TraceDiagnostics({ trace }: { trace: Trace }) {
  const d = trace.diagnostics
  if (!d) return null
  const labels: Record<string, string> = { database: 'Database only', external: 'External only', pool_wait: 'Pool wait only', overlap: 'Overlapping categories', remaining: 'Remaining / uninstrumented' }
  return <details className="card p-3">
    <summary className="cursor-pointer font-medium">Time breakdown and recording health</summary>
    <div className="mt-3 space-y-3">
      {d.warnings.map(w => <p key={w} className="text-warn" role="status">{w}</p>)}
      <dl className="grid grid-cols-[1fr_auto] gap-2 m-0 max-w-xl">{Object.entries(d.time_ms).map(([key, value]) => <div className="contents" key={key}><dt>{labels[key] ?? key}</dt><dd className="m-0 num">{ms(value)}</dd></div>)}</dl>
      <p className="text-muted m-0">{d.note}</p>
      {!!Object.keys(trace.runtime ?? {}).length && <p className="text-muted">Observed target runtime: {Object.entries(trace.runtime ?? {}).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p>}
      <p className="text-muted m-0">Observed spans: {Object.entries(d.observed_span_kinds).map(([key, count]) => `${key}: ${count}`).join(' · ') || 'none'}. Profile: {d.profile_available ? 'available' : 'not recorded'}.</p>
    </div>
  </details>
}
