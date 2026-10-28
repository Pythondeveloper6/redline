import { useState } from 'react'
import type { CheckRequestDef } from '../types'
import { policyErrors } from '../lib/policies'

type Policy = Pick<CheckRequestDef, 'budgets' | 'array_keys' | 'tolerances'>
type Props = { value: Policy; onChange: (v: Policy) => void }

export function CheckPolicyEditor({ value, onChange }: Props) {
  const [arrayPath, setArrayPath] = useState('')
  const [identity, setIdentity] = useState('id')
  const [numericPath, setNumericPath] = useState('')
  const errors = policyErrors(value)
  const remove = <T,>(rules: Record<string, T>, key: string) => Object.fromEntries(Object.entries(rules).filter(([k]) => k !== key))
  const limits = [
    ['max_queries', 'Maximum queries per call'], ['max_external_calls', 'Maximum external calls per call'],
    ['max_median_ms', 'Maximum median latency (ms)'], ['max_p95_ms', 'Maximum p95 latency (ms)'],
  ] as const
  return <details className="card p-3" open={Object.keys(value.budgets ?? {}).length > 0 || Object.keys(value.array_keys ?? {}).length > 0 || Object.keys(value.tolerances ?? {}).length > 0}>
    <summary className="cursor-pointer font-medium">Budgets and response rules</summary>
    <div className="mt-3 space-y-4">
      <fieldset className="space-y-2"><legend className="font-medium mb-2">Absolute endpoint budgets</legend>
        <p className="text-muted">Checked independently of the baseline. Missing required measurements fail the budget.</p>
        <div className="grid sm:grid-cols-2 gap-3">{limits.map(([key, label]) => <label key={key} className="flex flex-col gap-1 text-muted">{label}
          <input type="number" min="0" step={key === 'max_queries' || key === 'max_external_calls' ? 1 : 'any'} className="field w-full" value={value.budgets?.[key] ?? ''} placeholder="No limit"
            onChange={e => onChange({ budgets: e.target.value === '' ? remove(value.budgets ?? {}, key) : { ...value.budgets, [key]: Number(e.target.value) } })} />
        </label>)}</div>
        <label className="flex flex-col gap-1 text-muted">Forbidden issue kinds (comma separated)
          <input className="field" placeholder="n_plus_one, blocking_in_async" value={value.budgets?.forbidden_issues?.join(', ') ?? ''}
            onChange={e => onChange({ budgets: { ...value.budgets, forbidden_issues: e.target.value.split(',').map(s => s.trim()).filter(Boolean) } })} />
        </label>
      </fieldset>
      <fieldset className="space-y-2"><legend className="font-medium mb-2">Match arrays by identity</legend>
        <p className="text-muted">Use only where array order is irrelevant. Missing or duplicate identities fail comparison.</p>
        {Object.entries(value.array_keys ?? {}).map(([path, field]) => <div key={path} className="flex flex-wrap items-center gap-2"><code>{path}</code><span>matched by</span><code>{field}</code>
          <button type="button" className="btn" aria-label={`Remove array rule ${path}`} onClick={() => onChange({ array_keys: remove(value.array_keys ?? {}, path) })}>Remove</button></div>)}
        <div className="flex flex-wrap gap-2">
          <label className="flex flex-col gap-1 flex-1 min-w-0">Array path<input className="field" placeholder="items" value={arrayPath} onChange={e => setArrayPath(e.target.value)} /></label>
          <label className="flex flex-col gap-1">Identity field<input className="field w-28" value={identity} onChange={e => setIdentity(e.target.value)} /></label>
          <button type="button" className="btn self-end" disabled={!arrayPath.trim() || !identity.trim()} onClick={() => { onChange({ array_keys: { ...value.array_keys, [arrayPath.trim()]: identity.trim() } }); setArrayPath('') }}>Add array rule</button>
        </div>
      </fieldset>
      <fieldset className="space-y-2"><legend className="font-medium mb-2">Numeric tolerances</legend>
        <p className="text-muted">Relative tolerance is a fraction: 0.01 allows 1% of the baseline value. Other fields remain exact.</p>
        {Object.entries(value.tolerances ?? {}).map(([path, rule]) => <div key={path} className="flex flex-wrap items-end gap-2">
          <code className="self-center break-all">{path}</code>
          {(['absolute', 'relative'] as const).map(key => <label className="flex flex-col gap-1" key={key}>{key}<input className="field w-28" type="number" min="0" step="any" value={rule[key] ?? ''} placeholder="0" onChange={e => onChange({ tolerances: { ...value.tolerances, [path]: { ...rule, [key]: e.target.value === '' ? 0 : Number(e.target.value) } } })} /></label>)}
          <button type="button" className="btn" aria-label={`Remove tolerance ${path}`} onClick={() => onChange({ tolerances: remove(value.tolerances ?? {}, path) })}>Remove</button>
        </div>)}
        <div className="flex gap-2"><label className="flex flex-col gap-1 flex-1 min-w-0">Numeric field path<input className="field" placeholder="total" value={numericPath} onChange={e => setNumericPath(e.target.value)} /></label>
          <button type="button" className="btn self-end" disabled={!numericPath.trim()} onClick={() => { onChange({ tolerances: { ...value.tolerances, [numericPath.trim()]: { absolute: 0 } } }); setNumericPath('') }}>Add tolerance</button>
        </div>
      </fieldset>
      {Object.values(errors).map(error => <p key={error} role="alert" className="text-bad">{error}</p>)}
    </div>
  </details>
}
