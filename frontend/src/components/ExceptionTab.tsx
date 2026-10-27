import { useState } from 'react'
import { isRedacted } from '../lib/format'
import type { ErrorFrame, TraceError } from '../types'
import CallsiteLink from './CallsiteLink'
import { CopyButton, Icon, I, Redacted } from './ui'

function Frame({ f, last, n }: { f: ErrorFrame; last: boolean; n: number }) {
  const locals = Object.entries(f.locals ?? {})
  const [open, setOpen] = useState(last)
  return (
    <div className={`border-b border-line last:border-0 ${last ? 'bg-bad/5' : ''}`}>
      <div className="px-3 py-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-faint num text-[11px] w-5 text-right">{n}</span>
          <CallsiteLink callsite={f} className="!text-ink font-semibold" />
          <span className="text-muted mono">in {f.function}</span>
          {last && <span className="text-[10.5px] font-semibold text-bad uppercase tracking-wide">raised here</span>}
          <span className="flex-1" />
          {locals.length > 0 && (
            <button className="inline-flex items-center gap-1 text-[11.5px] text-muted hover:text-ink" aria-expanded={open} onClick={() => setOpen(o => !o)}>
              <Icon d={I.chev} size={11} className={open ? 'rotate-90' : ''} />{locals.length} {locals.length === 1 ? 'local' : 'locals'}
            </button>
          )}
        </div>
        {f.code && <pre className="m-0 mt-1 ml-7 rounded bg-raised px-2 py-1 overflow-x-auto">{f.code}</pre>}
      </div>
      {open && locals.length > 0 && (
        <div className="ml-10 mr-3 mb-3 rounded-md border border-line overflow-x-auto">
          <table className="w-full text-[12px]">
            <tbody>{locals.map(([k, v]) => (
              <tr key={k} className="border-b border-line/60 last:border-0">
                <td className="td mono text-muted align-top w-[1%]">{k}</td>
                <td className="td !whitespace-pre-wrap break-all mono">{isRedacted(v) ? <Redacted /> : v}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function ExceptionTab({ error }: { error: TraceError }) {
  const frames = error.frames ?? []
  return (
    <div className="space-y-3">
      <div className="rounded-lg border border-bad/30 bg-bad/5 p-3">
        <div className="flex items-center gap-2 text-bad font-semibold"><Icon d={I.alert} size={15} /><span className="mono">{error.type}</span></div>
        <div className="mt-0.5 break-words">{error.message}</div>
      </div>
      {frames.length > 0 && (
        <div>
          <div className="text-[12px] font-semibold mb-1.5">Project frames <span className="text-faint font-normal">(outermost first)</span></div>
          <div className="card overflow-hidden">{frames.map((f, i) => <Frame key={i} f={f} n={i + 1} last={i === frames.length - 1} />)}</div>
        </div>
      )}
      <div>
        <div className="flex items-center mb-1.5"><span className="text-[12px] font-semibold flex-1">Traceback</span><CopyButton text={error.traceback} className="h-6" /></div>
        <pre className="m-0 rounded-lg border border-line bg-surface p-3 overflow-auto max-h-96">{error.traceback}</pre>
      </div>
      {frames.length === 0 && <div className="text-[11.5px] text-muted">Local variables are only captured when <code>capture_locals</code> is enabled.</div>}
    </div>
  )
}
