import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ms } from '../lib/format'
import { useFeatures } from '../lib/meta'
import { buildReplay, hadRedactedCookie, missingFields, redactedFields } from '../lib/replay'
import type { Trace } from '../types'
import { Modal } from './Modal'
import { Pill, Status } from './ui'

interface Outcome { status: number; ms: number; statusText: string }

/** Re-sends a recorded request from the browser. Redacted values must be re-entered or removed; they are never sent. */
export default function ReplayModal({ trace, open, onClose }: { trace: Trace; open: boolean; onClose: () => void }) {
  const fields = useMemo(() => redactedFields(trace), [trace])
  const [values, setValues] = useState<Record<string, string>>({})
  const [removed, setRemoved] = useState<Set<string>>(new Set())
  const [profile, setProfile] = useState(false)
  const [sendCookies, setSendCookies] = useState(false)
  const features = useFeatures()
  const canProfile = features.has('profile')
  const [busy, setBusy] = useState(false)
  const [out, setOut] = useState<Outcome | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => { if (open) { setValues({}); setRemoved(new Set()); setSendCookies(false); setOut(null); setErr(null); setBusy(false) } }, [open, trace.id])

  const missing = missingFields(fields, values, removed)
  const body = trace.request.body
  const truncated = trace.request.body_truncated
  const send = async () => {
    if (missing.length) return
    if (!features.has('replay') || (!['GET', 'HEAD'].includes(trace.method) && !features.has('replay_writes'))) { setErr('Replay is disabled by this instance’s policy.'); return }
    setBusy(true); setErr(null); setOut(null)
    const t0 = performance.now()
    try {
      const { url, init } = buildReplay(trace, values, removed, { profile: profile && canProfile, replayOf: trace.id, origin: location.origin, sendCookies })
      const res = await fetch(url, init)
      setOut({ status: res.status, ms: performance.now() - t0, statusText: res.statusText })
      void res.body?.cancel()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }
  const toggle = (k: string) => setRemoved(r => { const n = new Set(r); if (n.has(k)) n.delete(k); else n.add(k); return n })

  return (
    <Modal open={open} onClose={onClose} title="Replay request" width="max-w-xl">
      <div className="p-4 space-y-4 overflow-y-auto">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mono font-bold">{trace.method}</span>
          <span className="mono break-all">{trace.path}</span>
          <Status code={trace.status} /><span className="text-[11px] text-faint">original</span>
        </div>
        <p className="m-0 text-muted text-[12px]">
          Sends this request again from your browser to this same site, without your cookies unless you allow them below. It will run for real, so side effects (writes, emails, payments) happen again.
        </p>
        {hadRedactedCookie(trace) && <p className="m-0 text-[12px] text-muted">The recorded Cookie header was redacted; it is never replayed.</p>}
        {truncated && <div className="rounded-md border border-warn/30 bg-warn/5 p-2 text-[12px] text-warn">The recorded body was truncated; the replay will send only the captured part.</div>}

        {fields.length > 0 && (
          <fieldset className="space-y-2 m-0 p-0 border-0">
            <legend className="text-[12px] font-semibold mb-1.5">Redacted values: enter them again</legend>
            {fields.map(f => {
              const gone = removed.has(f.key)
              return (
                <div key={f.key} className="flex flex-wrap sm:flex-nowrap items-center gap-2">
                  <label htmlFor={'rf-' + f.key} className="w-full sm:w-40 shrink-0 text-[12px] break-all">
                    <span className="mono">{f.name}</span> <Pill>{f.kind}</Pill>
                  </label>
                  <input id={'rf-' + f.key} type="password" autoComplete="off" spellCheck={false} disabled={gone}
                    className="field flex-1 min-w-0 w-full disabled:opacity-40" placeholder={gone ? 'will not be sent' : 'required'}
                    value={values[f.key] ?? ''} onChange={e => setValues(v => ({ ...v, [f.key]: e.target.value }))} />
                  {f.removable && (
                    <label className="inline-flex items-center gap-1 text-[11.5px] text-muted whitespace-nowrap cursor-pointer">
                      <input type="checkbox" className="accent-[rgb(var(--accent))]" checked={gone} onChange={() => toggle(f.key)} /> Remove
                    </label>
                  )}
                </div>
              )
            })}
          </fieldset>
        )}

        {body && !truncated && fields.every(f => f.kind !== 'body') && (
          <details className="text-[12px]"><summary className="cursor-pointer text-muted">Request body</summary><pre className="m-0 mt-1 rounded bg-raised p-2 overflow-auto max-h-40 whitespace-pre-wrap break-words">{body}</pre></details>
        )}

        <label className="flex items-start gap-2 cursor-pointer select-none">
          <input type="checkbox" className="mt-0.5 accent-[rgb(var(--accent))]" checked={sendCookies} onChange={e => setSendCookies(e.target.checked)} />
          <span>Send my cookies <span className="text-muted">(off by default)</span></span>
        </label>
        {sendCookies && <div role="note" className="rounded-md border border-warn/30 bg-warn/5 p-2 text-[12px] text-warn">The request runs as you, with your session on this site. Only do this for a request you have read and trust: anyone whose traffic was recorded chose its method, path and body.</div>}

        {canProfile && <label className="flex items-start gap-2 cursor-pointer select-none">
          <input type="checkbox" className="mt-0.5 accent-[rgb(var(--accent))]" checked={profile} onChange={e => setProfile(e.target.checked)} />
          <span>Profile this replay <span className="text-muted">(adds <code>X-Redline-Profile: 1</code>)</span></span>
        </label>}

        {err && <div role="alert" className="rounded-md border border-bad/30 bg-bad/5 p-2 text-bad text-[12px] break-words">Request failed: {err}</div>}
        {out && (
          <div role="status" className="rounded-md border border-line bg-raised/50 p-3 space-y-1.5">
            <div className="flex items-center gap-2"><Status code={out.status} /><span className="num">{ms(out.ms)}</span><span className="text-muted text-[12px]">round trip{out.statusText ? ` · ${out.statusText}` : ''}</span></div>
            <Link className="text-info hover:underline text-[12px]" to={`/requests?replay_of=${encodeURIComponent(trace.id)}`} onClick={onClose}>Find the new trace in Requests</Link>
          </div>
        )}
      </div>
      <footer className="flex items-center gap-2 px-4 py-3 border-t border-line">
        {missing.length > 0 && <span className="text-[12px] text-warn mr-auto">Fill or remove: {missing.map(m => m.name).join(', ')}</span>}
        <span className="flex-1" />
        <button className="btn" onClick={onClose}>{out ? 'Close' : 'Cancel'}</button>
        <button className="btn btn-accent" onClick={send} disabled={busy || missing.length > 0} data-autofocus={fields.length === 0 ? '' : undefined}>{busy ? 'Sending…' : out ? 'Send again' : 'Send request'}</button>
      </footer>
    </Modal>
  )
}
