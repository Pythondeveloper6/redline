import { useMemo, useState } from 'react'
import { ms } from '../lib/format'
import type { LogRecord } from '../types'
import CallsiteLink from './CallsiteLink'
import { Empty, Icon, I, Pill } from './ui'

const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
const RANK: Record<string, number> = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 }
const TONE: Record<string, string> = {
  DEBUG: 'text-faint', INFO: 'text-info', WARNING: 'text-warn', ERROR: 'text-bad', CRITICAL: 'text-bad font-bold',
}
const ROW: Record<string, string> = { ERROR: 'bg-bad/5', CRITICAL: 'bg-bad/10', WARNING: 'bg-warn/5' }

export default function LogsTab({ logs, dropped }: { logs: LogRecord[]; dropped: number }) {
  const [min, setMin] = useState(0)
  const [only, setOnly] = useState<string | null>(null)
  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const l of logs) c[l.level] = (c[l.level] ?? 0) + 1
    return c
  }, [logs])
  const shown = useMemo(() => logs.filter(l => (only ? l.level === only : (RANK[l.level] ?? 1) >= min)), [logs, min, only])
  if (!logs.length) {
    return <div className="card"><Empty title="No log lines captured" icon={<Icon d={I.zap} size={28} />}>
      <p>Records emitted through Python&apos;s <span className="mono">logging</span> module while this request was served appear here. Log capture needs to be enabled in the Redline settings.</p>
      {dropped > 0 && <p>{dropped} lines were dropped by the per-request cap.</p>}
    </Empty></div>
  }
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {LEVELS.map(l => (
          <button key={l} aria-pressed={only === l} disabled={!counts[l]} onClick={() => setOnly(o => (o === l ? null : l))}
            className={`inline-flex items-center gap-1.5 h-6 px-2 rounded-md border text-[11.5px] disabled:opacity-35 ${only === l ? 'border-accent bg-accent/10' : 'border-line'}`}>
            <span className={`font-semibold ${TONE[l]}`}>{l}</span><span className="num text-faint">{counts[l] ?? 0}</span>
          </button>
        ))}
        <span className="flex-1" />
        <label className="inline-flex items-center gap-1.5 text-[12px] text-muted">Min level
          <select className="field" value={min} onChange={e => { setMin(Number(e.target.value)); setOnly(null) }} aria-label="Minimum level">
            {LEVELS.map((l, i) => <option key={l} value={i}>{l}</option>)}
          </select>
        </label>
      </div>
      {dropped > 0 && (
        <div className="rounded-lg border border-warn/30 bg-warn/5 px-3 py-2 text-[12px] flex items-center gap-2" role="status">
          <span className="text-warn"><Icon d={I.alert} size={14} /></span>
          {dropped} more log {dropped === 1 ? 'line was' : 'lines were'} dropped because this request hit the per-request log cap. Raise <code>max_logs_per_trace</code> to keep more.
        </div>
      )}
      <div className="card overflow-hidden">
        {shown.length === 0 ? <div className="p-6 text-center text-muted">No lines at this level.</div> : shown.map((l, i) => (
          <div key={i} className={`flex gap-3 px-3 py-1.5 border-b border-line/60 last:border-0 ${ROW[l.level] ?? ''}`}>
            <span className="num text-faint text-[11px] w-[62px] shrink-0 pt-px text-right">{ms(l.t_ms)}</span>
            <span className={`mono text-[11px] w-[62px] shrink-0 pt-px ${TONE[l.level] ?? ''}`}>{l.level === 'WARNING' ? 'WARN' : l.level === 'CRITICAL' ? 'CRIT' : l.level}</span>
            <div className="min-w-0 flex-1">
              <div className="mono text-[12px] whitespace-pre-wrap break-words">{l.message}</div>
              <div className="flex flex-wrap items-center gap-x-3 text-[11px] text-faint mt-0.5">
                <span className="mono">{l.logger}</span>
                {l.callsite && <CallsiteLink callsite={l.callsite} />}
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="text-[11px] text-faint"><Pill>{shown.length}</Pill> of {logs.length} lines shown.</div>
    </div>
  )
}
