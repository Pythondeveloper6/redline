import { useMemo, useState } from 'react'
import { api } from '../api'
import { registerRoute } from '../lib/routes'
import type { ConfigResponse } from '../types'
import { LoadError } from '../components/Unsupported'
import { Empty, I, Icon, Pill, Spinner, useAsync } from '../components/ui'

registerRoute({ path: '/settings', label: 'Settings', keywords: 'config configuration preset access token features sampling toml env', hint: 'Effective configuration' })

const GROUPS = ['capture', 'sampling', 'detectors', 'storage', 'access', 'features', 'other']
const TITLES: Record<string, string> = {
  capture: 'Capture', sampling: 'Sampling', detectors: 'Detectors', storage: 'Storage', access: 'Access', features: 'Features', other: 'Other',
}

// Tailwind needs literal class names.
const SOURCES: Record<string, { cls: string; tip: (name: string) => string }> = {
  default: { cls: 'bg-raised text-faint', tip: () => 'Built-in default; nothing overrides it' },
  adapter: { cls: 'bg-info/10 text-info', tip: () => 'Set by the framework adapter (for example in Django settings or install())' },
  preset: { cls: 'bg-ok/10 text-ok', tip: () => 'Set by the active preset (dev, staging or production)' },
  file: { cls: 'bg-accent/10 text-accent', tip: () => 'Set in the redline.toml configuration file' },
  env: { cls: 'bg-warn/10 text-warn', tip: n => `Set by an environment variable REDLINE_${n.toUpperCase()}` },
  framework: { cls: 'bg-ink/10 text-ink', tip: () => 'Provided by the framework (its own authentication or settings)' },
}

function SourceChip({ name, source }: { name: string; source: string | undefined }) {
  const s = source ?? 'default'
  const d = SOURCES[s] ?? SOURCES.default
  return <span title={d.tip(name)} className={`inline-flex items-center rounded px-1.5 h-[18px] text-[11px] font-medium whitespace-nowrap ${d.cls}`}>{s}</span>
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

function Scalar({ v }: { v: unknown }) {
  if (v === null || v === undefined) return <span className="text-faint">—</span>
  if (v === '***') return <span title="Secret value, never sent to the browser" className="inline-flex items-center gap-1 rounded bg-raised px-1.5 h-[18px] text-[11px] text-faint italic select-none">••• hidden</span>
  if (typeof v === 'boolean') return <Pill tone={v ? 'ok' : 'neutral'}>{v ? 'on' : 'off'}</Pill>
  if (typeof v === 'number') return <span className="num">{v.toLocaleString(undefined, { maximumFractionDigits: 6 })}</span>
  if (typeof v === 'string') return v === '' ? <span className="text-faint">(empty)</span> : <span className="mono break-all">{v}</span>
  return <span className="mono break-all">{JSON.stringify(v)}</span>
}

function Value({ v }: { v: unknown }) {
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="text-faint">— (none)</span>
    return <span className="inline-flex flex-wrap gap-1">{v.map((x, i) => <span key={i} className="mono rounded bg-raised px-1.5 text-[11.5px] break-all">{typeof x === 'string' ? x : JSON.stringify(x)}</span>)}</span>
  }
  if (isObj(v)) {
    const e = Object.entries(v)
    if (e.length === 0) return <span className="text-faint">— (none)</span>
    return (
      <div className="space-y-0.5">
        {e.map(([k, x]) => <div key={k} className="flex flex-wrap items-center gap-1.5"><span className="mono text-muted">{k}:</span><Scalar v={x} /></div>)}
      </div>
    )
  }
  return <Scalar v={v} />
}

export default function Settings() {
  const d = useAsync(() => api.config(), [])
  const [q, setQ] = useState('')
  const data: ConfigResponse | undefined = d.data
  const groups = useMemo(() => {
    if (!data) return []
    const listed = new Set(Object.values(data.groups ?? {}).flat())
    const extra = Object.keys(data.settings ?? {}).filter(k => !listed.has(k))
    const needle = q.trim().toLowerCase()
    const names = [...GROUPS, ...Object.keys(data.groups ?? {}).filter(g => !GROUPS.includes(g))]
    return names.map(g => {
      const keys = [...(data.groups?.[g] ?? []), ...(g === 'other' ? extra : [])]
      return { id: g, keys: needle ? keys.filter(k => k.toLowerCase().includes(needle)) : keys }
    }).filter(g => g.keys.length > 0)
  }, [data, q])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Settings</h1>
        <label className="sr-only" htmlFor="settings-filter">Filter settings by name</label>
        <input id="settings-filter" type="search" value={q} onChange={e => setQ(e.target.value)} placeholder="Filter settings…"
          className="h-7 w-48 rounded-md border border-line bg-surface px-2 text-[12px] outline-none focus:border-accent" />
      </div>
      {d.error ? <LoadError error={d.error} what="the settings page" onRetry={d.reload} /> : d.loading && !data ? <Spinner /> : data && (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted">
            <span>Preset <Pill tone="info">{data.preset}</Pill></span>
            <span>Config file <span className="mono text-ink break-all">{data.file ?? 'none'}</span></span>
            <span className="text-faint">Read-only: change these in your config file, environment or framework settings.</span>
          </div>
          {data.warnings.length > 0 ? (
            <div className="rounded-lg border border-warn/30 bg-warn/5 p-3 flex items-start gap-2" role="status">
              <span className="text-warn mt-0.5"><Icon d={I.alert} size={15} /></span>
              <div className="min-w-0">
                <div className="font-semibold">{data.warnings.length === 1 ? '1 warning' : `${data.warnings.length} warnings`}</div>
                <ul className="m-0 mt-1 pl-4 list-disc space-y-0.5 text-[12.5px]">{data.warnings.map((w, i) => <li key={i} className="break-words">{w}</li>)}</ul>
              </div>
            </div>
          ) : <div className="text-[12px] text-muted flex items-center gap-1.5"><span className="text-ok"><Icon d={I.check} size={13} /></span>No warnings</div>}
          {groups.length === 0 ? <div className="card"><Empty title="No settings match">Nothing is named like “{q}”.</Empty></div> : groups.map(g => (
            <section key={g.id} className="card overflow-hidden" aria-label={TITLES[g.id] ?? g.id}>
              <h2 className="m-0 px-3 py-2 border-b border-line bg-raised/50 text-[12px] font-semibold">{TITLES[g.id] ?? g.id}</h2>
              <div>
                {g.keys.map(k => (
                  <div key={k} className="px-3 py-2 border-b border-line last:border-0 flex flex-wrap items-start gap-x-4 gap-y-1">
                    <div className="mono w-56 shrink-0 break-all">{k}</div>
                    <div className="flex-1 min-w-[10rem] text-[12.5px]"><Value v={data.settings?.[k]} /></div>
                    <SourceChip name={k} source={data.sources?.[k]} />
                  </div>
                ))}
              </div>
            </section>
          ))}
          <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-faint">
            Sources:{Object.keys(SOURCES).map(s => <SourceChip key={s} name="name" source={s} />)}
          </div>
        </>
      )}
    </div>
  )
}
