import { Link } from 'react-router-dom'
import { call } from '../api'
import { ErrorBox, Pill, Spinner, useAsync } from '../components/ui'
import { ms } from '../lib/format'
import { registerRoute } from '../lib/routes'
import type { Page } from '../types'

registerRoute({ path: '/jobs', label: 'Jobs', keywords: 'celery task worker import retry', hint: 'Recorded background jobs' })

export default function Jobs() {
  const d = useAsync(() => call<Page>('GET', './api/traces?method=TASK&limit=100'), [])
  return <div className="space-y-4"><div className="flex flex-wrap justify-between gap-3"><h1 className="text-[15px] font-semibold">Jobs</h1><button className="btn" onClick={d.reload}>Refresh jobs</button></div>
    <p className="text-muted">Latest 100 retained task recordings. A worker must configure a recorder and shared or separately inspected storage. Missing jobs do not prove no jobs ran.</p>
    {d.error ? <ErrorBox error={d.error} onRetry={d.reload} /> : !d.data ? <Spinner /> : <>
      {!d.data.items.length && <div className="card p-4 space-y-2"><h2 className="font-semibold">No jobs recorded</h2><p>Wrap work with <code>job()</code> or install the optional Celery integration in the worker process. Task arguments are not captured.</p></div>}
      {d.data.items.map(item => <Link key={`${item.id}:${item.tags.secondary_store ?? 'primary'}`} to={`/requests/${item.id}`} aria-disabled={item.ambiguous_trace_id} onClick={event => { if (item.ambiguous_trace_id) event.preventDefault() }} className="card block p-4 space-y-2 hover:bg-raised/50"><div className="flex flex-wrap justify-between gap-2"><span className="font-semibold break-all">{item.tags['job.name'] ?? item.path}</span><Pill tone={item.status >= 400 ? 'bad' : item.tags['job.outcome'] === 'retry' ? 'warn' : 'ok'}>{item.tags['job.outcome'] ?? String(item.status)}</Pill></div><div className="text-muted">{ms(item.duration_ms)} · {item.query_count} queries · attempt {item.tags['job.attempt'] ?? 'unknown'} · {new Date(item.started_at * 1000).toLocaleString()}</div>{item.tags.synthetic_demo && <p className="text-info">Synthetic workflow example; timings are illustrative.</p>}{item.ambiguous_trace_id && <p className="text-warn">Duplicate trace ID across stores. Inspection is unavailable.</p>}{item.tags.commit_observed_before_failure && <p className="text-warn">A database commit was observed before this failure. Inspect transaction evidence before rerunning.</p>}</Link>)}
    </>}
  </div>
}
