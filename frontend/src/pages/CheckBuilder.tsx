import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { apiChecks } from '../apiChecks'
import { ms } from '../lib/format'
import {
  ignorePathError, isEnvPlaceholder, isSecretHeader, READ_ONLY_METHODS, validateSuite, volatileSuggestions, baselineRequestsOf, LIMITS,
} from '../lib/check'
import { useDebounced } from '../lib/hooks'
import type { BaselineRequest, CheckMethod, CheckRequestDef, CheckSuite, EndpointStats } from '../types'
import { useCheckAccess } from '../components/CheckParts'
import { LoadError } from '../components/Unsupported'
import { CheckDisabled } from './Checks'
import { Empty, ErrorBox, I, Icon, Pill, Spinner, useAsync } from '../components/ui'

// ---- editable state ------------------------------------------------------------------------------------
interface HRow { k: string; name: string; value: string }
interface RRow extends Omit<CheckRequestDef, 'headers'> { k: string; headers: HRow[]; ignore: string[]; ignore_headers: string[] }
interface Form {
  name: string; base_url: string; timeout_s: string; repeat: string; warmup: string
  slower_pct: string; slower_ms: string; max_query_increase: string
  headers: HRow[]; requests: RRow[]
}

let uid = 0
const key = () => 'k' + ++uid
const toRows = (h: Record<string, string> | undefined): HRow[] =>
  Object.entries(h ?? {}).map(([name, value]) => ({ k: key(), name, value: isSecretHeader(name) && !isEnvPlaceholder(value) ? '' : value }))
const fromRows = (rows: HRow[]) => Object.fromEntries(rows.filter(r => r.name.trim() && r.value.trim()).map(r => [r.name.trim(), r.value.trim()]))
const toReq = (r: CheckRequestDef): RRow => ({
  ...r, k: key(), method: r.method ?? 'GET', perf: r.perf !== false, response: r.response !== false,
  headers: toRows(r.headers), ignore: r.ignore ?? [], ignore_headers: r.ignore_headers ?? [],
})
const num = (s: string): number | undefined => (s.trim() === '' ? undefined : Number(s))

function toForm(s: CheckSuite): Form {
  return {
    name: s.name ?? '', base_url: s.base_url ?? '', timeout_s: String(s.timeout_s ?? 10), repeat: String(s.repeat ?? 20), warmup: String(s.warmup ?? 3),
    slower_pct: String(s.thresholds?.slower_pct ?? 10), slower_ms: String(s.thresholds?.slower_ms ?? 2), max_query_increase: String(s.thresholds?.max_query_increase ?? 0),
    headers: toRows(s.headers), requests: (s.requests ?? []).map(toReq),
  }
}
function toSuite(f: Form): CheckSuite {
  const out: CheckSuite = {
    name: f.name.trim(),
    timeout_s: num(f.timeout_s), repeat: num(f.repeat), warmup: num(f.warmup),
    thresholds: { slower_pct: num(f.slower_pct), slower_ms: num(f.slower_ms), max_query_increase: num(f.max_query_increase) },
    headers: fromRows(f.headers),
    requests: f.requests.map(r => {
      const q: CheckRequestDef = { name: r.name.trim(), method: r.method, path: r.path.trim(), headers: fromRows(r.headers), perf: r.perf !== false, response: r.response !== false, ignore: r.ignore, ignore_headers: r.ignore_headers }
      if (r.expect_status != null && !Number.isNaN(r.expect_status)) q.expect_status = r.expect_status
      return q
    }),
  }
  if (f.base_url.trim()) out.base_url = f.base_url.trim()
  return out
}
const emptyForm = (): Form => toForm({ name: '', requests: [] })

function uniqueName(want: string, taken: Set<string>) {
  if (!taken.has(want)) return want
  let i = 2
  while (taken.has(`${want} (${i})`)) i++
  return `${want} (${i})`
}

// ---- small widgets ------------------------------------------------------------------------------------------
function Field({ label, error, hint, children, className = '' }: { label: string; error?: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={`flex flex-col gap-1 text-[11.5px] text-muted ${className}`}>
      <span className="font-medium">{label}</span>
      {children}
      {error ? <span className="text-bad" role="alert">{error}</span> : hint ? <span className="text-faint">{hint}</span> : null}
    </label>
  )
}
const inv = (e?: string) => (e ? { 'aria-invalid': true as const } : {})

function HeadersEditor({ rows, onChange, error, label }: { rows: HRow[]; onChange: (r: HRow[]) => void; error?: string; label: string }) {
  const set = (k: string, p: Partial<HRow>) => onChange(rows.map(r => (r.k === k ? { ...r, ...p } : r)))
  const hasAuth = rows.some(r => r.name.toLowerCase() === 'authorization')
  return (
    <div className="space-y-2">
      {rows.length === 0 && <div className="text-[12px] text-faint">No headers.</div>}
      {rows.map(r => {
        const secret = isSecretHeader(r.name)
        return (
          <div key={r.k} className="flex flex-wrap sm:flex-nowrap items-start gap-2">
            <input className="field mono w-full sm:w-44" aria-label={`${label} header name`} placeholder="Header-Name" value={r.name} onChange={e => set(r.k, { name: e.target.value })} />
            <input className="field mono flex-1 min-w-0 w-full" aria-label={`${label} header value`} autoComplete="off" spellCheck={false}
              placeholder={secret ? (r.name.toLowerCase() === 'authorization' ? 'Bearer ${env:API_TOKEN}' : '${env:SECRET_NAME}') : 'value, or ${env:NAME}'}
              value={r.value} onChange={e => set(r.k, { value: e.target.value })} />
            <button type="button" className="btn btn-ghost !px-1.5" aria-label={`Remove header ${r.name || ''}`} onClick={() => onChange(rows.filter(x => x.k !== r.k))}><Icon d={I.x} size={13} /></button>
          </div>
        )
      })}
      {error && <div className="text-bad text-[11.5px]" role="alert">{error}</div>}
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className="btn" onClick={() => onChange([...rows, { k: key(), name: '', value: '' }])}>Add header</button>
        {!hasAuth && <button type="button" className="btn btn-ghost" onClick={() => onChange([...rows, { k: key(), name: 'Authorization', value: '' }])}>Add Authorization</button>}
      </div>
      <p className="m-0 text-[11.5px] text-faint">Secrets are never pre-filled or stored. Reference an environment variable as <code className="mono">{'${env:NAME}'}</code>; it is expanded when the run starts (so the server needs that variable).</p>
    </div>
  )
}

function IgnoreEditor({ paths, onChange, suggestions, error }: { paths: string[]; onChange: (p: string[]) => void; suggestions: string[]; error?: string }) {
  const [text, setText] = useState('')
  const live = text.trim() ? ignorePathError(text) : null
  const add = (p: string) => {
    const v = p.trim()
    if (!v || ignorePathError(v) || paths.includes(v)) return
    onChange([...paths, v]); setText('')
  }
  return (
    <div className="space-y-2">
      {paths.length > 0 && (
        <ul className="m-0 p-0 list-none flex flex-wrap gap-1.5">
          {paths.map(p => {
            const bad = ignorePathError(p)
            return (
              <li key={p} className={`inline-flex items-center gap-1 rounded bg-raised pl-1.5 pr-0.5 h-6 mono text-[11.5px] ${bad ? 'text-bad ring-1 ring-bad/40' : ''}`} title={bad ?? undefined}>
                {p}
                <button type="button" className="btn btn-ghost !h-5 !px-1" aria-label={`Remove ignore path ${p}`} onClick={() => onChange(paths.filter(x => x !== p))}><Icon d={I.x} size={11} /></button>
              </li>
            )
          })}
        </ul>
      )}
      <div className="flex gap-2">
        <input className="field mono flex-1 min-w-0" aria-label="Add ignore path" placeholder="data[*].created_at" value={text} {...inv(live ?? undefined)}
          onChange={e => setText(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); add(text) } }} />
        <button type="button" className="btn" disabled={!text.trim() || !!live} onClick={() => add(text)}>Add</button>
      </div>
      {live && <div className="text-bad text-[11.5px]" role="alert">{live}</div>}
      {error && !live && <div className="text-bad text-[11.5px]" role="alert">{error}</div>}
      {suggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11.5px] text-muted">Changes on every call (from the baseline):</span>
          {suggestions.map(s => <button type="button" key={s} className="btn !h-6 mono" onClick={() => onChange([...paths, s])} title="Ignore this path in the response diff">+ {s}</button>)}
        </div>
      )}
      <p className="m-0 text-[11.5px] text-faint">Paths: dotted keys, <code className="mono">[n]</code>, <code className="mono">[*]</code> any index, <code className="mono">*</code> any key, <code className="mono">["a.b"]</code> for keys with dots.</p>
    </div>
  )
}

function Toggle({ label, checked, onChange, help }: { label: string; checked: boolean; onChange: (v: boolean) => void; help: string }) {
  return (
    <label className="inline-flex items-center gap-1.5 text-[12px] cursor-pointer" title={help}>
      <input type="checkbox" className="accent-[rgb(var(--accent))]" checked={checked} onChange={e => onChange(e.target.checked)} />{label}
    </label>
  )
}

function RequestCard({ r, idx, errors, suggestions, onChange, onRemove }: {
  r: RRow; idx: number; errors: Record<string, string>; suggestions: string[]; onChange: (p: Partial<RRow>) => void; onRemove: () => void
}) {
  const e = (f: string) => errors[`requests.${idx}.${f}`]
  const adv = !!(r.headers.length || r.ignore.length || r.ignore_headers.length || r.expect_status != null || e('headers') || e('ignore') || e('expect_status') || suggestions.length)
  return (
    <div className="card p-3 space-y-3" data-testid="request-card">
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Method" error={e('method')} className="w-24">
          <select className="field mono" value={r.method} onChange={x => onChange({ method: x.target.value as CheckMethod })} {...inv(e('method'))}>
            {READ_ONLY_METHODS.map(m => <option key={m}>{m}</option>)}
          </select>
        </Field>
        <Field label="Path (with query)" error={e('path')} className="flex-[2_1_14rem] min-w-0">
          <input className="field mono" value={r.path} placeholder="/orders?page=1" onChange={x => onChange({ path: x.target.value })} {...inv(e('path'))} spellCheck={false} />
        </Field>
        <Field label="Name" error={e('name')} className="flex-[1_1_10rem] min-w-0">
          <input className="field" value={r.name} placeholder="list orders" onChange={x => onChange({ name: x.target.value })} {...inv(e('name'))} />
        </Field>
        <button type="button" className="btn btn-ghost !px-1.5 mb-0.5" aria-label={`Remove request ${r.name || r.path}`} onClick={onRemove}><Icon d={I.trash} size={13} /></button>
      </div>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
        <Toggle label="Perf" checked={r.perf !== false} onChange={v => onChange({ perf: v })} help="Time this request (repeat times) and compare latency and query counts" />
        <Toggle label="Response" checked={r.response !== false} onChange={v => onChange({ response: v })} help="Compare status, headers and body" />
        {r.perf === false && r.response === false && <Pill tone="warn">checks nothing</Pill>}
      </div>
      <details open={adv} className="group">
        <summary className="cursor-pointer text-[12px] text-muted hover:text-ink select-none">Headers, ignore paths, expected status</summary>
        <div className="mt-3 space-y-4">
          <div className="space-y-1.5"><div className="text-[11.5px] font-medium text-muted">Ignore paths (response diff)</div>
            <IgnoreEditor paths={r.ignore} onChange={ignore => onChange({ ignore })} suggestions={suggestions} error={e('ignore')} /></div>
          <div className="space-y-1.5"><div className="text-[11.5px] font-medium text-muted">Request headers (merged over the suite headers)</div>
            <HeadersEditor rows={r.headers} onChange={headers => onChange({ headers })} error={e('headers')} label={`Request ${idx + 1}`} /></div>
          <div className="flex flex-wrap gap-3">
            <Field label="Expected status" error={e('expect_status')} hint="optional" className="w-32">
              <input className="field num" inputMode="numeric" placeholder="200" value={r.expect_status ?? ''} {...inv(e('expect_status'))}
                onChange={x => onChange({ expect_status: x.target.value.trim() === '' ? null : Number(x.target.value) })} />
            </Field>
            <Field label="Response headers to drop from comparison" hint="comma separated; content-type is always compared" className="flex-1 min-w-[12rem]">
              <input className="field mono" placeholder="date, x-request-id" value={r.ignore_headers.join(', ')}
                onChange={x => onChange({ ignore_headers: x.target.value.split(',').map(s => s.trim()).filter(Boolean) })} />
            </Field>
          </div>
        </div>
      </details>
    </div>
  )
}

// ---- route picker ----------------------------------------------------------------------------------------------
function Picker({ onAdd, onAddManual, startOpen }: { onAdd: (routes: string[] | null) => Promise<void>; onAddManual: () => void; startOpen: boolean }) {
  const [open, setOpen] = useState(startOpen)
  const [q, setQ] = useState('')
  const [method, setMethod] = useState('')
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const d = useAsync(() => api.endpoints('7d'), [])
  const dq = useDebounced(q, 150)
  const rows = useMemo(() => (d.data?.items ?? [])
    .filter((e: EndpointStats) => (READ_ONLY_METHODS as readonly string[]).includes(e.method))
    .filter(e => (!method || e.method === method) && (!dq || e.route.toLowerCase().includes(dq.toLowerCase()))), [d.data, dq, method])
  const rk = (e: EndpointStats) => `${e.method} ${e.route}`
  const allSel = rows.length > 0 && rows.every(e => sel.has(rk(e)))
  const run = async (routes: string[] | null) => {
    setBusy(true); setErr(null)
    try { await onAdd(routes); setSel(new Set()) } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <section className="card" aria-label="Add requests from seen traffic">
      <button type="button" className="w-full flex items-center gap-2 px-3 py-2.5 text-left" aria-expanded={open} onClick={() => setOpen(o => !o)}>
        <Icon d={I.chev} size={12} className={open ? 'rotate-90' : ''} />
        <span className="text-[13px] font-semibold flex-1">Add requests</span>
        <span className="text-[11.5px] text-muted">from routes Redline has seen, or by hand</span>
      </button>
      {open && (
        <div className="border-t border-line p-3 space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <input className="field flex-1 min-w-[10rem]" aria-label="Search routes" placeholder="Search routes…" value={q} onChange={e => setQ(e.target.value)} />
            <select className="field" aria-label="Method filter" value={method} onChange={e => setMethod(e.target.value)}>
              <option value="">All read-only methods</option>{READ_ONLY_METHODS.map(m => <option key={m}>{m}</option>)}
            </select>
            <button type="button" className="btn" onClick={onAddManual}>Add by hand</button>
          </div>
          {d.error ? <ErrorBox error={d.error} onRetry={d.reload} /> : d.loading && !d.data ? <Spinner /> : rows.length === 0 ? (
            <Empty title={dq || method ? 'No matching routes' : 'No read-only routes seen yet'}><p>Only GET, HEAD and OPTIONS routes can be checked. Browse the app so Redline records some traffic, or add a request by hand.</p></Empty>
          ) : (
            <div className="rounded-md border border-line max-h-72 overflow-auto">
              <table className="w-full border-collapse text-[12px]">
                <thead className="bg-raised sticky top-0"><tr>
                  <th className="th w-8"><input type="checkbox" aria-label="Select all shown" checked={allSel} onChange={() => setSel(allSel ? new Set() : new Set(rows.map(rk)))} /></th>
                  <th className="th">Route</th><th className="th text-right hidden sm:table-cell">p50</th><th className="th text-right hidden sm:table-cell">Queries</th><th className="th text-right">Seen</th>
                </tr></thead>
                <tbody>{rows.map(e => (
                  <tr key={rk(e)} className="border-t border-line hover:bg-raised/50 cursor-pointer" onClick={() => setSel(s => { const n = new Set(s); n.has(rk(e)) ? n.delete(rk(e)) : n.add(rk(e)); return n })}>
                    <td className="td"><input type="checkbox" aria-label={`Select ${rk(e)}`} checked={sel.has(rk(e))} onChange={() => undefined} /></td>
                    <td className="td !whitespace-normal"><span className="mono font-semibold mr-2">{e.method}</span><span className="mono break-all">{e.route}</span></td>
                    <td className="td text-right num hidden sm:table-cell">{ms(e.p50_ms)}</td>
                    <td className="td text-right num hidden sm:table-cell">{e.avg_queries.toFixed(1)}</td>
                    <td className="td text-right num text-muted">{e.count.toLocaleString()}</td>
                  </tr>))}
                </tbody>
              </table>
            </div>
          )}
          {err && <div className="text-bad text-[12px]" role="alert">{err}</div>}
          <div className="flex flex-wrap items-center gap-2">
            <button type="button" className="btn btn-accent" disabled={busy || sel.size === 0} onClick={() => run([...sel].map(s => s.slice(s.indexOf(' ') + 1)))}>Add {sel.size || ''} selected</button>
            <button type="button" className="btn" disabled={busy} onClick={() => run(null)} title="Most recent concrete path of each seen read-only route (up to 20)">Add up to 20 recent routes</button>
            <span className="text-[11.5px] text-muted">A concrete sample path is filled in for each route. Redacted header values are never copied.</span>
          </div>
        </div>
      )}
    </section>
  )
}

// ---- page -------------------------------------------------------------------------------------------------------------
export default function CheckBuilder() {
  const { id } = useParams()
  const access = useCheckAccess()
  const nav = useNavigate()
  const existing = useAsync(() => (id ? apiChecks.suite(id) : Promise.resolve(null)), [id])
  const [form, setForm] = useState<Form | null>(id ? null : emptyForm())
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [showErrors, setShowErrors] = useState(false)
  const [baseline, setBaseline] = useState<BaselineRequest[] | null>(null)
  const topRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (id && existing.data) { setForm(toForm(existing.data.suite)); setDirty(false) }
  }, [id, existing.data])
  // Volatile-field suggestions: from the most recent finished baseline of this suite.
  useEffect(() => {
    if (!id) return
    let live = true
    apiChecks.runs(id).then(async r => {
      const b = r.items.filter(x => x.kind === 'baseline' && x.status === 'done').sort((a, c) => c.created_at - a.created_at)[0]
      if (!b) return
      const det = await apiChecks.run(b.id)
      if (live) setBaseline(baselineRequestsOf(det))
    }).catch(() => undefined)
    return () => { live = false }
  }, [id])
  useEffect(() => {
    if (!dirty) return
    const h = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [dirty])

  const suite = useMemo(() => (form ? toSuite(form) : null), [form])
  const v = useMemo(() => (suite ? validateSuite(suite) : { fields: {}, warnings: [] }), [suite])
  const errs = showErrors ? v.fields : {}

  if (access.loaded && !access.enabled) return <CheckDisabled />
  if (id && existing.error) return <div className="space-y-3"><Back to="/checks" /><LoadError error={existing.error} what="refactor checks" onRetry={existing.reload} /></div>
  if (!form) return <div className="space-y-3"><Back to="/checks" /><Spinner /></div>

  const patch = (p: Partial<Form>) => { setForm(f => ({ ...f!, ...p })); setDirty(true) }
  const patchReq = (k: string, p: Partial<RRow>) => patch({ requests: form.requests.map(r => (r.k === k ? { ...r, ...p } : r)) })
  const field = (name: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => patch({ [name]: e.target.value } as Partial<Form>)

  const addFromDraft = async (routes: string[] | null) => {
    const d = await apiChecks.draft(routes ? { routes, limit: Math.max(routes.length, 1) } : { limit: 20 })
    const taken = new Set(form.requests.map(r => r.name))
    const have = new Set(form.requests.map(r => `${r.method} ${r.path}`))
    const added: RRow[] = []
    for (const q of d.requests ?? []) {
      if (have.has(`${q.method} ${q.path}`)) continue
      const row = toReq(q)
      row.name = uniqueName(row.name, taken); taken.add(row.name)
      added.push(row)
    }
    if (!added.length) throw new Error('Those routes are already in the suite, or Redline has no read-only traffic for them yet.')
    // Suite-level headers from the draft (e.g. an Authorization placeholder) only when none are set.
    const extra = form.headers.length === 0 && d.headers ? { headers: toRows(d.headers) } : {}
    patch({ requests: [...form.requests, ...added], ...extra })
  }
  const addManual = () => {
    const taken = new Set(form.requests.map(r => r.name))
    patch({ requests: [...form.requests, toReq({ name: uniqueName('GET /', taken), method: 'GET', path: '/' })] })
  }

  const save = async () => {
    setShowErrors(true); setSaveErr(null)
    if (Object.keys(v.fields).length) { topRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }); return }
    setBusy(true)
    try {
      const doc = id ? await apiChecks.updateSuite(id, suite!.name, suite!) : await apiChecks.createSuite(suite!.name, suite!)
      setDirty(false)
      nav(`/checks/suites/${doc.id ?? id}`)
    } catch (e) { setSaveErr((e as Error).message); topRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }) } finally { setBusy(false) }
  }
  const exportToml = async () => {
    if (!id) return
    try {
      const text = await apiChecks.exportSuite(id)
      const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
      const a = document.createElement('a')
      a.href = url; a.download = `${(suite!.name || 'suite').replace(/[^\w.-]+/g, '-')}.toml`
      document.body.appendChild(a); a.click(); a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) { setSaveErr((e as Error).message) }
  }

  const errCount = Object.keys(v.fields).length
  const readOnly = access.readOnly
  return (
    <div className="space-y-4 pb-20" ref={topRef}>
      <Back to={id ? `/checks/suites/${id}` : '/checks'} label={id ? 'Back to suite' : 'All suites'} />
      <h1 className="m-0 text-[15px] font-semibold">{id ? 'Edit suite' : 'New suite'}</h1>
      {readOnly && <div className="card p-3 text-warn" role="status">This Redline instance is read-only: suites cannot be saved. You can still look around.</div>}
      {(saveErr || (showErrors && errCount > 0)) && (
        <div className="rounded-lg border border-bad/30 bg-bad/5 p-3 text-bad text-[12.5px]" role="alert">
          {saveErr ?? `Fix ${errCount} problem${errCount === 1 ? '' : 's'} before saving.`}
          {showErrors && errCount > 0 && (
            <ul className="m-0 mt-1 pl-4">{Object.entries(v.fields).slice(0, 6).map(([k, m]) => <li key={k}><span className="mono">{k}</span>: {m}</li>)}</ul>
          )}
        </div>
      )}

      <section className="card p-4 space-y-4" aria-label="Suite settings">
        <div className="flex flex-wrap gap-3">
          <Field label="Suite name" error={errs.name} className="flex-[2_1_14rem] min-w-0">
            <input className="field" value={form.name} onChange={field('name')} placeholder="orders refactor" {...inv(errs.name)} />
          </Field>
          <Field label="Base URL" error={errs.base_url} hint="optional; blank = this app" className="flex-[2_1_14rem] min-w-0">
            <input className="field mono" value={form.base_url} onChange={field('base_url')} placeholder="http://127.0.0.1:8000" {...inv(errs.base_url)} spellCheck={false} />
          </Field>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <Field label="Repeat" error={errs.repeat} hint={`${LIMITS.repeat[0]}–${LIMITS.repeat[1]} timed calls`}><input type="number" className="field num" min={LIMITS.repeat[0]} max={LIMITS.repeat[1]} value={form.repeat} onChange={field('repeat')} {...inv(errs.repeat)} /></Field>
          <Field label="Warmup" error={errs.warmup} hint={`${LIMITS.warmup[0]}–${LIMITS.warmup[1]} untimed`}><input type="number" className="field num" min={LIMITS.warmup[0]} max={LIMITS.warmup[1]} value={form.warmup} onChange={field('warmup')} {...inv(errs.warmup)} /></Field>
          <Field label="Timeout (s)" error={errs.timeout_s}><input type="number" className="field num" min={1} value={form.timeout_s} onChange={field('timeout_s')} {...inv(errs.timeout_s)} /></Field>
          <Field label="Slower if > %" error={errs['thresholds.slower_pct']} hint="median change"><input type="number" className="field num" min={0} step="any" value={form.slower_pct} onChange={field('slower_pct')} {...inv(errs['thresholds.slower_pct'])} /></Field>
          <Field label="and > ms" error={errs['thresholds.slower_ms']} hint="absolute change"><input type="number" className="field num" min={0} step="any" value={form.slower_ms} onChange={field('slower_ms')} {...inv(errs['thresholds.slower_ms'])} /></Field>
          <Field label="Max extra queries" error={errs['thresholds.max_query_increase']} hint="more = slower"><input type="number" className="field num" min={0} value={form.max_query_increase} onChange={field('max_query_increase')} {...inv(errs['thresholds.max_query_increase'])} /></Field>
        </div>
        <details open={form.headers.length > 0 || !!errs.headers}>
          <summary className="cursor-pointer text-[12px] text-muted hover:text-ink select-none">Suite headers (sent with every request)</summary>
          <div className="mt-3"><HeadersEditor rows={form.headers} onChange={headers => patch({ headers })} error={errs.headers} label="Suite" /></div>
        </details>
      </section>

      <Picker onAdd={addFromDraft} onAddManual={addManual} startOpen={form.requests.length === 0} />

      <section aria-label="Requests" className="space-y-3">
        <div className="flex items-center gap-2">
          <h2 className="m-0 text-[14px] font-semibold flex-1">Requests <span className="text-muted font-normal num">({form.requests.length})</span></h2>
          <button type="button" className="btn" onClick={addManual}>Add request</button>
        </div>
        {errs.requests && <div className="text-bad text-[12px]" role="alert">{errs.requests}</div>}
        {form.requests.length === 0 ? (
          <div className="card"><Empty title="No requests yet"><p>Pick routes above or add a request by hand. Only GET, HEAD and OPTIONS are allowed.</p></Empty></div>
        ) : form.requests.map((r, i) => (
          <RequestCard key={r.k} r={r} idx={i} errors={errs} suggestions={volatileSuggestions(baseline, r.name, r.ignore)}
            onChange={p => patchReq(r.k, p)} onRemove={() => patch({ requests: form.requests.filter(x => x.k !== r.k) })} />
        ))}
      </section>
      {v.warnings.length > 0 && <ul className="m-0 pl-4 text-warn text-[12px]" role="status">{v.warnings.map(w => <li key={w}>{w}</li>)}</ul>}

      <div className="fixed bottom-0 inset-x-0 z-20 border-t border-line bg-surface/95 backdrop-blur">
        <div className="mx-auto max-w-[1500px] px-3 sm:px-5 py-2 flex flex-wrap items-center gap-2">
          {!readOnly && <button type="button" className="btn btn-accent" disabled={busy} onClick={save}>{busy ? 'Saving…' : id ? 'Save changes' : 'Save suite'}</button>}
          <button type="button" className="btn" disabled={!id || dirty} onClick={exportToml} title={id ? (dirty ? 'Save first to export the current edits' : 'Download the suite as TOML') : 'Save the suite first, then export it'}>
            <Icon d={I.down} size={12} />Export TOML
          </button>
          <Link to={id ? `/checks/suites/${id}` : '/checks'} className="btn btn-ghost">Cancel</Link>
          {dirty && <span className="text-[11.5px] text-muted">Unsaved changes</span>}
        </div>
      </div>
    </div>
  )
}

const Back = ({ to, label = 'All suites' }: { to: string; label?: string }) => (
  <Link to={to} className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-ink"><Icon d={I.back} size={12} />{label}</Link>
)
