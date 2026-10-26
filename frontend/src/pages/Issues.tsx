import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ago, ISSUE_LABEL } from '../lib/format'
import { useNow } from '../lib/hooks'
import { Callsite, Empty, ErrorBox, I, Icon, IssueBadge, Sev, Spinner, useAsync } from '../components/ui'
import { SuggestionBlock } from '../components/IssueCard'

const WINDOWS = ['1h', '24h', '7d']
export default function Issues() {
  const [win, setWin] = useState('24h')
  const [kind, setKind] = useState('')
  const now = useNow(30000)
  const d = useAsync(() => api.issues(win, kind || undefined), [win, kind])
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Issues</h1>
        <select className="field" value={kind} onChange={e => setKind(e.target.value)} aria-label="Issue kind">
          <option value="">All kinds</option>{Object.entries(ISSUE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <div className="inline-flex rounded-md border border-line p-0.5 bg-surface" role="radiogroup" aria-label="Time window">
          {WINDOWS.map(w => <button key={w} role="radio" aria-checked={win === w} onClick={() => setWin(w)} className={`h-6 px-2.5 rounded text-[12px] font-medium ${win === w ? 'bg-raised text-ink' : 'text-muted hover:text-ink'}`}>{w}</button>)}
        </div>
      </div>
      {d.error && <ErrorBox error={d.error} onRetry={d.reload} />}
      {d.loading && !d.data ? <Spinner /> : d.data && d.data.items.length === 0 ? (
        <div className="card"><Empty title="No issues found" icon={<Icon d={I.check} size={28} />}><p>No N+1, duplicate, slow or over-budget queries in the last {win}.</p></Empty></div>
      ) : (
        <div className="space-y-3">
          {d.data?.items.map(g => (
            <div key={g.id} className="card p-4 space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Sev s={g.severity} /><IssueBadge kind={g.kind} />
                <span className="flex-1" />
                <span className="num text-[12px] text-muted"><b className="text-ink">{g.occurrences.toLocaleString()}</b> occurrences · first {ago(g.first_seen, now)} · last {ago(g.last_seen, now)}</span>
              </div>
              <div className="text-[14px] font-semibold break-words">{g.title}</div>
              <p className="m-0 text-muted max-w-3xl">{g.detail}</p>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px]">
                {g.callsite && <Callsite cs={g.callsite} />}
                <span className="flex flex-wrap gap-1.5">{g.routes.map(r => <span key={r} className="mono rounded bg-raised px-1.5 text-[11px]">{r}</span>)}</span>
                <Link className="text-info hover:underline ml-auto" to={`/requests/${g.sample_trace_id}?tab=issues`}>Open sample request</Link>
              </div>
              {g.suggestion && <div className="border-t border-line pt-3"><SuggestionBlock s={g.suggestion} /></div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
