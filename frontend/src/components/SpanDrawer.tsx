import { KIND_COLOR, ms } from '../lib/format'
import type { Span } from '../types'
import CallsiteLink from './CallsiteLink'
import { CopyButton, Drawer, Sql } from './ui'

export function KV({ k, children }: { k: string; children: React.ReactNode }) {
  return <div className="flex gap-3 py-1 border-b border-line/60 last:border-0"><div className="w-28 shrink-0 text-muted">{k}</div><div className="min-w-0 break-words flex-1">{children}</div></div>
}

export function StackList({ span }: { span: Span }) {
  const cs = span.callsite
  if (!cs) return null
  return (
    <div>
      <div className="text-[12px] font-semibold mb-1.5">Call site</div>
      <div className="card p-3 space-y-1.5">
        <div className="mono"><CallsiteLink callsite={cs} className="!text-ink font-semibold" /> <span className="text-muted">in {cs.function}</span></div>
        {cs.code && <pre className="m-0 rounded bg-raised px-2 py-1 overflow-x-auto">{cs.code}</pre>}
        {cs.stack.length > 0 && (
          <div className="pt-1.5 mt-1.5 border-t border-line">
            <div className="text-[11px] text-muted mb-1">Stack (outermost first)</div>
            {cs.stack.map((f, i) => (
              <div key={i} className={`mono text-[11.5px] ${i === cs.stack.length - 1 ? 'text-ink font-semibold' : 'text-muted'}`}><CallsiteLink callsite={f} className="!text-inherit" /> <span className="text-faint">{f.function}</span></div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default function SpanDrawer({ span, onClose, onExplain }: { span: Span | null; onClose: () => void; onExplain: (s: Span) => void }) {
  const d = span?.data ?? {}
  return (
    <Drawer open={!!span} onClose={onClose} wide
      title={span && <span className="inline-flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-sm" style={{ background: KIND_COLOR[span.kind] }} />{span.kind === 'sql' ? 'SQL query' : span.name}</span>}
      subtitle={span && <span className="num">{ms(span.duration_ms)} · starts at {ms(span.start_ms)}</span>}>
      {span && (
        <>
          {span.kind === 'sql' && (
            <>
              <div className="rounded-md bg-raised p-3 overflow-x-auto"><Sql sql={d.sql ?? span.name} /></div>
              <div className="flex gap-2">
                <CopyButton text={d.sql ?? ''} label="Copy SQL" />
                {d.explainable ? <button className="btn btn-accent" onClick={() => onExplain(span)}>Explain</button> : <span className="text-[12px] text-muted self-center">Only SELECT queries can be explained.</span>}
              </div>
              {Array.isArray(d.params) && d.params.length > 0 && <div><div className="text-[12px] font-semibold mb-1">Params</div><pre className="m-0 rounded-md bg-raised p-2 overflow-x-auto">{JSON.stringify(d.params)}</pre></div>}
            </>
          )}
          <div className="card px-3 py-1.5">
            <KV k="Kind">{span.kind}</KV>
            <KV k="Duration"><span className="num">{ms(span.duration_ms)}</span></KV>
            <KV k="Start offset"><span className="num">{ms(span.start_ms)}</span></KV>
            {span.kind === 'sql' && <><KV k="Database">{d.db_alias} <span className="text-muted">({d.vendor})</span></KV><KV k="Rows"><span className="num">{d.rows ?? '-'}</span></KV><KV k="Fingerprint"><span className="mono text-[11px]">{d.fingerprint}</span></KV></>}
            {span.kind === 'cache' && <><KV k="Operation">{d.op}</KV><KV k="Key"><span className="mono">{d.key}</span></KV><KV k="Hit">{String(d.hit)}</KV><KV k="Backend">{d.backend}</KV></>}
            {span.kind === 'http_out' && <><KV k="Request">{d.method} <span className="mono break-all">{d.url}</span></KV><KV k="Status">{d.status}</KV></>}
            {span.kind === 'template' && <KV k="Template"><span className="mono">{d.template}</span></KV>}
            {(span.kind === 'view' || span.kind === 'middleware' || span.kind === 'custom') && <KV k="Name"><span className="mono">{span.name}</span></KV>}
          </div>
          <StackList span={span} />
        </>
      )}
    </Drawer>
  )
}
