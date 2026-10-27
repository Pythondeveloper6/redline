import { useState } from 'react'
import { apiLoad } from '../apiLoad'
import type { NewRunBody } from '../types'
import { CopyButton, I, Icon } from './ui'

export const CLI_COMMAND = 'redline load scenario.toml --steps 10:200:10'

/** Shown instead of the form when the backend does not allow starting runs from the UI. */
export function CliHint() {
  return (
    <div className="card p-4 space-y-2">
      <div className="text-[13px] font-semibold">Start a run from the command line</div>
      <p className="m-0 text-muted max-w-2xl">Starting runs from the UI is disabled on this server (<span className="mono">load_from_ui</span> is off). Run the load tester from a shell and it will appear here, tagged against the same store:</p>
      <div className="flex flex-wrap items-center gap-2">
        <code className="mono rounded bg-raised px-2 py-1 break-all">{CLI_COMMAND}</code>
        <CopyButton text={CLI_COMMAND} />
      </div>
    </div>
  )
}

type Mode = 'constant' | 'ramp' | 'steps'
interface Row { method: string; path: string; weight: string }
const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

function Num({ label, value, onChange, min = 1 }: { label: string; value: string; onChange: (v: string) => void; min?: number }) {
  return (
    <label className="flex flex-col gap-1 text-[11.5px] text-muted">{label}
      <input type="number" min={min} className="field w-28 num text-ink" value={value} onChange={e => onChange(e.target.value)} />
    </label>
  )
}

export default function NewRunForm({ onStarted, onCancel }: { onStarted: (id: string | undefined) => void; onCancel: () => void }) {
  const [name, setName] = useState('')
  const [rows, setRows] = useState<Row[]>([{ method: 'GET', path: '/', weight: '1' }])
  const [mode, setMode] = useState<Mode>('steps')
  const [v, setV] = useState({ rps: '50', start: '10', end: '200', step: '10', duration: '60', stepDuration: '15' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const set = (k: keyof typeof v) => (x: string) => setV(s => ({ ...s, [k]: x }))
  const upd = (i: number, p: Partial<Row>) => setRows(r => r.map((x, j) => (j === i ? { ...x, ...p } : x)))

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    const requests = rows.filter(r => r.path.trim()).map(r => ({ method: r.method, path: r.path.trim(), weight: Math.max(1, Number(r.weight) || 1) }))
    if (!requests.length) { setErr('Add at least one request path.'); return }
    const n = (s: string) => Number(s)
    const profile: NewRunBody['profile'] =
      mode === 'constant' ? { mode, rps: n(v.rps), duration_s: n(v.duration) }
        : mode === 'ramp' ? { mode, start_rps: n(v.start), end_rps: n(v.end), duration_s: n(v.duration) }
          : { mode, start_rps: n(v.start), end_rps: n(v.end), step_rps: n(v.step), step_duration_s: n(v.stepDuration), duration_s: n(v.stepDuration) * (Math.floor((n(v.end) - n(v.start)) / Math.max(1, n(v.step))) + 1) }
    if (Object.values(profile).some(x => typeof x === 'number' && !(x > 0))) { setErr('All numbers must be greater than zero.'); return }
    setBusy(true); setErr(null)
    try {
      const run = await apiLoad.startRun({ name: name.trim() || `run ${new Date().toLocaleTimeString()}`, requests, profile })
      onStarted(run.id)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }

  return (
    <form onSubmit={submit} className="card p-4 space-y-4">
      <div className="flex items-center gap-2">
        <div className="text-[13px] font-semibold flex-1">New load run</div>
        <button type="button" className="btn btn-ghost !px-1.5" onClick={onCancel} aria-label="Close form"><Icon d={I.x} size={14} /></button>
      </div>
      <p className="m-0 text-muted">The target is always this application's own base URL. Requests are sent open-loop at the chosen rate.</p>
      <label className="flex flex-col gap-1 text-[11.5px] text-muted max-w-sm">Name
        <input className="field text-ink" value={name} placeholder="checkout ramp" onChange={e => setName(e.target.value)} />
      </label>
      <fieldset className="border-0 p-0 m-0 space-y-2">
        <legend className="text-[11.5px] text-muted mb-1">Requests</legend>
        {rows.map((r, i) => (
          <div key={i} className="flex flex-wrap items-center gap-2">
            <select className="field" aria-label="Method" value={r.method} onChange={e => upd(i, { method: e.target.value })}>{METHODS.map(m => <option key={m}>{m}</option>)}</select>
            <input className="field mono flex-1 min-w-[10rem]" aria-label="Path" placeholder="/api/orders/" value={r.path} onChange={e => upd(i, { path: e.target.value })} />
            <label className="flex items-center gap-1.5 text-[11.5px] text-muted">weight
              <input type="number" min={1} className="field w-16 num text-ink" aria-label="Weight" value={r.weight} onChange={e => upd(i, { weight: e.target.value })} /></label>
            <button type="button" className="btn btn-ghost !px-1.5" disabled={rows.length === 1} aria-label="Remove request" onClick={() => setRows(x => x.filter((_, j) => j !== i))}><Icon d={I.x} size={12} /></button>
          </div>
        ))}
        <button type="button" className="btn" onClick={() => setRows(x => [...x, { method: 'GET', path: '', weight: '1' }])}>Add request</button>
      </fieldset>
      <fieldset className="border-0 p-0 m-0">
        <legend className="text-[11.5px] text-muted mb-1">Load profile</legend>
        <div className="inline-flex rounded-md border border-line p-0.5 bg-surface mb-3" role="radiogroup" aria-label="Profile mode">
          {(['constant', 'ramp', 'steps'] as Mode[]).map(m => <button type="button" key={m} role="radio" aria-checked={mode === m} onClick={() => setMode(m)} className={`h-6 px-2.5 rounded text-[12px] font-medium ${mode === m ? 'bg-raised text-ink' : 'text-muted hover:text-ink'}`}>{m}</button>)}
        </div>
        <div className="flex flex-wrap gap-3">
          {mode === 'constant' && <><Num label="Requests / s" value={v.rps} onChange={set('rps')} /><Num label="Duration (s)" value={v.duration} onChange={set('duration')} /></>}
          {mode === 'ramp' && <><Num label="Start rps" value={v.start} onChange={set('start')} /><Num label="End rps" value={v.end} onChange={set('end')} /><Num label="Duration (s)" value={v.duration} onChange={set('duration')} /></>}
          {mode === 'steps' && <><Num label="Start rps" value={v.start} onChange={set('start')} /><Num label="End rps" value={v.end} onChange={set('end')} /><Num label="Step rps" value={v.step} onChange={set('step')} /><Num label="Step duration (s)" value={v.stepDuration} onChange={set('stepDuration')} /></>}
        </div>
      </fieldset>
      {err && <div className="text-bad text-[12px]" role="alert">{err}</div>}
      <div className="flex gap-2">
        <button className="btn btn-accent h-8" disabled={busy}><Icon d={I.play} size={12} />{busy ? 'Starting…' : 'Start run'}</button>
        <button type="button" className="btn h-8" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  )
}
