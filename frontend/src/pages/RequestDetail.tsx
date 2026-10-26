import { Fragment, lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, ApiError } from '../api'
import { bytes, clock, isRedacted, KIND_COLOR, ms } from '../lib/format'
import type { Issue, Span, Trace } from '../types'
import { Callsite, CopyButton, Empty, ErrorBox, I, Icon, IssueBadge, Pill, Redacted, Sev, Spinner, Sql, Status, Tabs, useAsync } from '../components/ui'
import Waterfall from '../components/Waterfall'
import SpanDrawer, { KV } from '../components/SpanDrawer'
import { ExplainDrawer } from '../components/Plan'
import { SuggestionBlock } from '../components/IssueCard'

const Flamegraph = lazy(() => import('../components/Flamegraph'))

type Tab = 'timeline' | 'sql' | 'issues' | 'profile' | 'http'

function Card({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: string }) {
  return (
    <div className="card px-3.5 py-3">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className={`text-[22px] leading-tight font-semibold num mt-0.5 ${tone ?? ''}`}>{value}</div>
      {sub && <div className="text-[11.5px] text-muted mt-0.5 num">{sub}</div>}
    </div>
  )
}

function SqlTab({ trace, issueBySpan, onOpen, onExplain }: { trace: Trace; issueBySpan: Map<string, Issue>; onOpen: (s: Span) => void; onExplain: (s: Span) => void }) {
  const groups = useMemo(() => {
    const m = new Map<string, Span[]>()
    for (const s of trace.spans) if (s.kind === 'sql') { const k = s.data.fingerprint ?? s.name; m.set(k, [...(m.get(k) ?? []), s]) }
    return [...m.entries()].map(([fp, spans]) => ({ fp, spans, total: spans.reduce((a, s) => a + s.duration_ms, 0), max: Math.max(...spans.map(s => s.duration_ms)) }))
      .sort((a, b) => b.total - a.total)
  }, [trace])
  const [open, setOpen] = useState<Set<string>>(new Set())
  if (!groups.length) return <div className="card"><Empty title="No queries in this request" /></div>
  const maxTotal = groups[0].total
  return (
    <div className="card divide-y divide-line">
      {groups.map(g => {
        const first = g.spans[0]; const isOpen = open.has(g.fp)
        const issue = g.spans.map(s => issueBySpan.get(s.id)).find(Boolean)
        return (
          <div key={g.fp}>
            <button className="w-full text-left px-3 py-2.5 hover:bg-raised/50 flex gap-3 items-start"
              onClick={() => setOpen(o => { const n = new Set(o); if (n.has(g.fp)) n.delete(g.fp); else n.add(g.fp); return n })} aria-expanded={isOpen}>
              <Icon d={I.chev} size={12} className={`mt-1 shrink-0 text-faint transition-transform ${isOpen ? 'rotate-90' : ''}`} />
              <div className="flex-1 min-w-0">
                <div className="mono text-[11.5px] truncate">{first.name}</div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1 text-[11.5px] text-muted">
                  <Callsite cs={first.callsite} />
                  {issue && <IssueBadge kind={issue.kind} />}
                </div>
              </div>
              <div className="text-right shrink-0 num w-[110px]">
                <div className="font-semibold">{ms(g.total)}</div>
                <div className="text-[11px] text-muted">{g.spans.length}× · max {ms(g.max)}</div>
                <div className="h-1 rounded bg-raised mt-1"><div className="h-full rounded" style={{ width: (g.total / maxTotal) * 100 + '%', background: KIND_COLOR.sql }} /></div>
              </div>
            </button>
            {isOpen && (
              <div className="px-3 pb-3 pl-9 space-y-3">
                <div className="rounded-md bg-raised p-3 overflow-x-auto"><Sql sql={first.data.sql} /></div>
                <div className="rounded-md border border-line overflow-x-auto">
                  <table className="w-full text-[12px]">
                    <thead className="bg-raised/60"><tr><th className="th">#</th><th className="th">Start</th><th className="th">Duration</th><th className="th">Params</th><th className="th">Rows</th><th className="th" /></tr></thead>
                    <tbody>
                      {g.spans.slice(0, 50).map((s, i) => (
                        <tr key={s.id} className="border-t border-line hover:bg-raised/40 cursor-pointer" onClick={() => onOpen(s)}>
                          <td className="td num text-muted">{i + 1}</td><td className="td num">{ms(s.start_ms)}</td><td className="td num">{ms(s.duration_ms)}</td>
                          <td className="td mono max-w-[260px] truncate">{JSON.stringify(s.data.params ?? [])}</td><td className="td num">{s.data.rows ?? '-'}</td>
                          <td className="td text-right"><button className="btn h-6" disabled={!s.data.explainable} onClick={e => { e.stopPropagation(); onExplain(s) }}>Explain</button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {g.spans.length > 50 && <div className="px-3 py-1.5 text-[11px] text-muted border-t border-line">Showing 50 of {g.spans.length}.</div>}
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

function IssuesTab({ issues, spans }: { issues: Issue[]; spans: Span[] }) {
  if (!issues.length) return <div className="card"><Empty title="No issues detected" icon={<Icon d={I.check} size={28} />}><p>This request has no N+1 queries, duplicates, slow queries or query-budget problems.</p></Empty></div>
  return (
    <div className="space-y-3">
      {issues.map(i => (
        <div key={i.id} className="card p-4 space-y-3">
          <div className="flex flex-wrap items-center gap-2"><Sev s={i.severity} /><IssueBadge kind={i.kind} /><span className="num text-[12px] text-muted">{i.count}× · {ms(i.total_ms)} total</span></div>
          <div className="text-[14px] font-semibold">{i.title}</div>
          <p className="m-0 text-muted max-w-3xl">{i.detail}</p>
          {i.callsite && <div className="text-[12px]"><Callsite cs={i.callsite} />{i.callsite.code && <pre className="m-0 mt-1 rounded bg-raised px-2 py-1 overflow-x-auto">{i.callsite.code}</pre>}</div>}
          {i.suggestion && <div className="border-t border-line pt-3"><SuggestionBlock s={i.suggestion} /></div>}
          <div className="text-[11px] text-faint">{i.span_ids.length} affected span{i.span_ids.length === 1 ? '' : 's'}: {spans.filter(s => i.span_ids.includes(s.id)).length ? 'highlighted in the timeline' : ''}</div>
        </div>
      ))}
    </div>
  )
}

function Headers({ title, h }: { title: string; h: Record<string, any> }) {
  const e = Object.entries(h)
  return (
    <div className="card">
      <div className="px-3 py-2 border-b border-line text-[12px] font-semibold">{title} <span className="text-faint font-normal num">{e.length}</span></div>
      {e.length === 0 ? <div className="px-3 py-3 text-muted">None</div> : (
        <div className="px-3 py-1">{e.map(([k, v]) => <KV key={k} k={k}>{isRedacted(v) ? <Redacted /> : <span className="mono break-all">{typeof v === 'string' ? v : JSON.stringify(v)}</span>}</KV>)}</div>
      )}
    </div>
  )
}
function Body({ title, body, truncated }: { title: string; body: string | null; truncated: boolean }) {
  const pretty = useMemo(() => { if (!body) return null; try { return JSON.stringify(JSON.parse(body), null, 2) } catch { return body } }, [body])
  return (
    <div className="card">
      <div className="px-3 py-2 border-b border-line flex items-center gap-2 text-[12px] font-semibold">{title}{truncated && <Pill tone="warn">truncated</Pill>}<span className="flex-1" />{body && <CopyButton text={pretty ?? ''} className="h-6" />}</div>
      {pretty == null ? <div className="px-3 py-3 text-muted">No body captured</div> : (
        <pre className="m-0 p-3 overflow-auto max-h-[420px] whitespace-pre-wrap break-words">
          {pretty.split(/("(?:\[redacted\]|\*{3,}|•{3,}|<redacted>)")/i).map((p, i) => (i % 2 ? <Redacted key={i} /> : <Fragment key={i}>{p}</Fragment>))}
        </pre>
      )}
    </div>
  )
}

function ProfileTab({ trace }: { trace: Trace }) {
  const p = useAsync(() => (trace.has_profile ? api.profile(trace.id) : Promise.resolve(null)), [trace.id])
  if (!trace.has_profile || (p.error instanceof ApiError && p.error.status === 404)) {
    return <div className="card"><Empty title="This request was not profiled" icon={<Icon d={I.zap} size={28} />}>
      <p>Profiles are opt-in because they add overhead. Re-run the request with <code>?_redline=profile</code> or send the header <code>X-Redline-Profile: 1</code>. It needs the <code>redline-core[profile]</code> extra.</p>
    </Empty></div>
  }
  if (p.loading) return <Spinner />
  if (p.error) return <ErrorBox error={p.error} onRetry={p.reload} />
  return (
    <div>
      <div className="flex justify-end mb-2 -mt-1"><a className="btn" href={api.profileUrl(trace.id)} download={`redline-${trace.id}.speedscope.json`}><Icon d={I.down} size={12} />Download speedscope JSON</a></div>
      <Suspense fallback={<Spinner />}><Flamegraph profile={p.data!} unit={p.data!.profiles[0].unit} /></Suspense>
    </div>
  )
}

export default function RequestDetail() {
  const { id = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const tab = (sp.get('tab') as Tab) || 'timeline'
  const setTab = (t: Tab) => setSp({ tab: t }, { replace: true })
  const t = useAsync(() => api.trace(id), [id])
  const [span, setSpan] = useState<Span | null>(null)
  const [explain, setExplain] = useState<Span | null>(null)
  const [tbOpen, setTbOpen] = useState(false)
  useEffect(() => { setSpan(null); setExplain(null) }, [id])

  const trace = t.data
  const issueSpanIds = useMemo(() => new Set(trace?.issues.flatMap(i => i.span_ids) ?? []), [trace])
  const issueBySpan = useMemo(() => { const m = new Map<string, Issue>(); trace?.issues.forEach(i => i.span_ids.forEach(s => m.set(s, i))); return m }, [trace])

  if (t.loading && !trace) return <Spinner />
  if (t.error) return <><Back /><ErrorBox error={t.error} onRetry={t.reload} /></>
  if (!trace) return null
  const slow = trace.duration_ms >= 500
  return (
    <div className="space-y-4">
      <Back />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="mono font-bold text-[14px]">{trace.method}</span>
        <h1 className="m-0 text-[17px] font-semibold break-all">{trace.path}</h1>
        <Status code={trace.status} />
        <span className="text-muted text-[12px] w-full sm:w-auto">{clock(trace.started_at)} · {trace.framework}{trace.route && trace.route !== trace.path ? <> · <span className="mono">{trace.route}</span></> : null}{trace.view ? <> · <span className="mono">{trace.view}</span></> : null}</span>
      </div>
      {trace.error && (
        <div className="rounded-lg border border-bad/30 bg-bad/5 p-3">
          <div className="flex items-center gap-2 text-bad font-semibold"><Icon d={I.alert} size={15} />{trace.error.type}</div>
          <div className="mt-0.5">{trace.error.message}</div>
          <button className="btn btn-ghost -ml-2 mt-1 text-bad" onClick={() => setTbOpen(o => !o)}>{tbOpen ? 'Hide' : 'Show'} traceback</button>
          {tbOpen && <pre className="m-0 mt-1 rounded bg-surface border border-line p-3 overflow-auto max-h-72">{trace.error.traceback}</pre>}
        </div>
      )}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <Card label="Status" value={trace.status} tone={trace.status >= 500 ? 'text-bad' : trace.status >= 400 ? 'text-warn' : ''} sub={bytes(trace.response.size)} />
        <Card label="Duration" value={ms(trace.duration_ms)} tone={slow ? 'text-bad' : ''} sub={`${trace.spans.length} spans`} />
        <Card label="Queries" value={trace.query_count} tone={trace.query_count >= 50 ? 'text-warn' : ''} sub={trace.query_count ? `${ms(trace.query_ms / trace.query_count)} avg` : undefined} />
        <Card label="Query time" value={ms(trace.query_ms)} sub={trace.duration_ms ? `${Math.round((trace.query_ms / trace.duration_ms) * 100)}% of request` : undefined} />
        <Card label="Issues" value={trace.issues.length} tone={trace.issues.length ? 'text-bad' : 'text-ok'} sub={trace.issues.length ? trace.issues.map(i => i.kind.replace(/_/g, ' ')).join(', ') : 'clean'} />
      </div>
      <div>
        <Tabs value={tab} onChange={setTab} tabs={[
          { id: 'timeline', label: 'Timeline' },
          { id: 'sql', label: 'SQL', badge: <span className="num text-faint">{trace.query_count}</span> },
          { id: 'issues', label: 'Issues', badge: trace.issues.length ? <span className="num rounded-full bg-accent text-white px-1.5 text-[10px] leading-4">{trace.issues.length}</span> : undefined },
          { id: 'profile', label: 'Profile' },
          { id: 'http', label: 'Request / Response' },
        ]} />
        <div className="pt-4">
          {tab === 'timeline' && <Waterfall spans={trace.spans} total={trace.duration_ms} issueSpanIds={issueSpanIds} onSelect={setSpan} selected={span?.id} />}
          {tab === 'sql' && <SqlTab trace={trace} issueBySpan={issueBySpan} onOpen={setSpan} onExplain={setExplain} />}
          {tab === 'issues' && <IssuesTab issues={trace.issues} spans={trace.spans} />}
          {tab === 'profile' && <ProfileTab trace={trace} />}
          {tab === 'http' && (
            <div className="space-y-3">
              {Object.keys(trace.tags).length > 0 && <div className="flex flex-wrap gap-1.5">{Object.entries(trace.tags).map(([k, v]) => <Pill key={k}><span className="text-faint">{k}</span> {v}</Pill>)}</div>}
              <div className="grid lg:grid-cols-2 gap-3 items-start">
                <div className="space-y-3">
                  <Headers title="Request headers" h={trace.request.headers} />
                  {Object.keys(trace.request.query).length > 0 && <Headers title="Query parameters" h={trace.request.query} />}
                  <Body title="Request body" body={trace.request.body} truncated={trace.request.body_truncated} />
                </div>
                <div className="space-y-3">
                  <Headers title="Response headers" h={trace.response.headers} />
                  <Body title="Response body" body={trace.response.body} truncated={trace.response.body_truncated} />
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
      <SpanDrawer span={span} onClose={() => setSpan(null)} onExplain={s => { setSpan(null); setExplain(s) }} />
      <ExplainDrawer traceId={trace.id} span={explain} onClose={() => setExplain(null)} />
    </div>
  )
}

function Back() {
  return <Link to="/requests" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />All requests</Link>
}
