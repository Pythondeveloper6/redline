import { useState } from 'react'
import { api, ApiError } from '../api'
import { ms } from '../lib/format'
import { isReadOnly } from '../lib/meta'
import type { ExplainResult, Meta, PlanNode, Span } from '../types'
import { CopyButton, Drawer, Pill, Sev, Spinner, Sql, ErrorBox, sevTone, Icon, I } from './ui'
import { useEffect } from 'react'

const FLAG_LABEL: Record<string, string> = { seq_scan: 'seq scan', large_table: 'large table', sort: 'sort', disk_sort: 'spills to disk', missing_index: 'missing index' }

function Node({ n, depth, maxCost }: { n: PlanNode; depth: number; maxCost: number }) {
  const bad = n.flags.some(f => f === 'seq_scan' || f === 'disk_sort' || f === 'missing_index')
  return (
    <div>
      <div className="flex items-start gap-2 py-1.5" style={{ paddingLeft: depth * 18 }}>
        <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${bad ? 'bg-bad' : 'bg-faint'}`} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-semibold mono">{n.label}</span>
            {n.flags.map(f => <Pill key={f} tone={f === 'sort' || f === 'large_table' ? 'warn' : 'bad'}>{FLAG_LABEL[f] ?? f}</Pill>)}
            <span className="ml-auto flex gap-3 text-[11px] text-muted num">
              {n.rows != null && <span>rows {n.rows.toLocaleString()}</span>}
              {n.cost != null && <span>cost {n.cost.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>}
              {n.actual_ms != null && <span>actual {ms(n.actual_ms)}</span>}
            </span>
          </div>
          {n.detail && <div className="mono text-[11px] text-muted break-words">{n.detail}</div>}
          {n.cost != null && <div className="h-0.5 mt-1 rounded bg-raised"><div className={`h-full rounded ${bad ? 'bg-bad' : 'bg-faint'}`} style={{ width: Math.max(2, (n.cost / maxCost) * 100) + '%' }} /></div>}
        </div>
      </div>
      {n.children.map((c, i) => <Node key={i} n={c} depth={depth + 1} maxCost={maxCost} />)}
    </div>
  )
}
const maxOf = (n: PlanNode): number => Math.max(n.cost ?? 0, ...n.children.map(maxOf), 1)

export function ExplainDrawer({ traceId, span, onClose }: { traceId: string; span: Span | null; onClose: () => void }) {
  const [res, setRes] = useState<ExplainResult | null>(null)
  const [err, setErr] = useState<ApiError | Error | null>(null)
  const [loading, setLoading] = useState(false)
  const [showRaw, setShowRaw] = useState(false)
  const [meta, setMeta] = useState<Meta | null>(null)
  const readOnly = isReadOnly(meta)
  useEffect(() => { api.meta().then(setMeta, () => {}) }, [])
  // Unknown (meta not loaded) counts as enabled; the API still enforces it with a 403.
  const analyzeOff = meta?.settings?.explain_analyze_enabled === false
  const analyzeHint = meta?.framework?.toLowerCase().includes('django')
    ? 'ANALYZE executes the query. Enable it with REDLINE = {"EXPLAIN_ANALYZE": True} in Django settings.'
    : 'ANALYZE executes the query. Enable it with install(app, settings={"explain_analyze": True}) in FastAPI.'

  const run = async (analyze: boolean) => {
    if (!span) return
    setLoading(true); setErr(null)
    try { setRes(await api.explain(traceId, span.id, analyze)) } catch (e) { setErr(e as Error); if (!analyze) setRes(null) } finally { setLoading(false) }
  }
  useEffect(() => { setRes(null); setErr(null); setShowRaw(false); if (span) void run(false) }, [span?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const disabled = err instanceof ApiError && err.code === 'analyze_disabled'
  return (
    <Drawer open={!!span} onClose={onClose} wide title="Query plan" subtitle={res ? <>{res.vendor}{res.analyzed ? ' · EXPLAIN ANALYZE' : ' · EXPLAIN (estimates only)'}</> : undefined}>
      {span && <div className="rounded-md bg-raised p-3 max-h-40 overflow-auto"><Sql sql={span.data.sql} /></div>}
      {loading && !res && <Spinner />}
      {err && !disabled && <ErrorBox error={err} onRetry={() => run(false)} />}
      {disabled && (
        <div className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-[12.5px]">
          <div className="font-semibold text-warn mb-0.5">EXPLAIN ANALYZE is turned off</div>
          <div className="text-muted">ANALYZE actually runs the query, so Redline only allows it when you set <code>explain_analyze</code> to true. The estimated plan below is still available.</div>
        </div>
      )}
      {res && (
        <>
          <div className="flex items-center gap-2">
            <div className="text-[12px] font-semibold flex-1">Plan</div>
            {!res.analyzed && !readOnly && <button className="btn" onClick={() => run(true)} disabled={loading || analyzeOff} title={analyzeOff ? analyzeHint : 'Runs the query for real to get actual timings'}>Run with ANALYZE</button>}
          </div>
          <div className="card p-3"><Node n={res.tree} depth={0} maxCost={maxOf(res.tree)} /></div>
          {res.findings.length > 0 && (
            <div>
              <div className="text-[12px] font-semibold mb-2">Findings</div>
              <div className="space-y-2">
                {res.findings.map((f, i) => (
                  <div key={i} className={`rounded-md border p-2.5 ${sevTone(f.severity) === 'bad' ? 'border-bad/30 bg-bad/5' : sevTone(f.severity) === 'warn' ? 'border-warn/30 bg-warn/5' : 'border-line bg-raised'}`}>
                    <div className="flex items-center gap-2"><Sev s={f.severity} /><span className="font-semibold">{f.title}</span></div>
                    <div className="text-muted mt-1">{f.detail}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {res.suggested_indexes.length > 0 && (
            <div>
              <div className="text-[12px] font-semibold mb-2">Suggested indexes</div>
              <div className="space-y-2">
                {res.suggested_indexes.map((ix, i) => (
                  <div key={i} className="rounded-md border border-line overflow-hidden">
                    <div className="flex justify-end px-2 py-1 bg-raised border-b border-line"><CopyButton text={ix} label="Copy SQL" className="h-6" /></div>
                    <div className="p-3 overflow-x-auto"><Sql sql={ix} format={false} /></div>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div>
            <button className="btn btn-ghost -ml-2" onClick={() => setShowRaw(s => !s)}><Icon d={I.chev} size={12} className={showRaw ? 'rotate-90' : ''} />Raw plan</button>
            {showRaw && <pre className="m-0 mt-2 rounded-md bg-raised p-3 overflow-auto max-h-80 whitespace-pre">{res.text || JSON.stringify(res.plan, null, 2)}</pre>}
          </div>
        </>
      )}
    </Drawer>
  )
}
