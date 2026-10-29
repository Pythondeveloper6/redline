import { Link } from 'react-router-dom'
import { call } from '../api'
import { ErrorBox, Pill, Spinner, useAsync } from '../components/ui'
import { ms } from '../lib/format'
import { registerRoute } from '../lib/routes'
import type { Page } from '../types'

registerRoute({ path: '/jobs', label: 'Jobs', keywords: 'celery task worker import retry', hint: 'Recorded background jobs' })

type JobSummary = {
  window: number
  sampled: number
  total: number
  truncated: boolean
  outcomes: Record<string, number>
  first_failing_stages: Record<string, number>
  unattributed_failures: number
  jobs: { name: string; queue: string; total: number; outcomes: Record<string, number>; first_failing_stages: Record<string, number>; p50_ms: number; p95_ms: number; last_started_at: number }[]
  limitation: string
}

const outcomeTone = (outcome: string) => outcome === 'failed' ? 'bad' : outcome === 'retry' ? 'warn' : outcome === 'succeeded' ? 'ok' : 'neutral'

export default function Jobs() {
  const d = useAsync(() => call<Page>('GET', './api/traces?method=TASK&limit=100'), [])
  const summary = useAsync(() => call<JobSummary>('GET', './api/jobs/summary?window=1d&limit=500'), [])
  const reload = () => { d.reload(); summary.reload() }
  const outcomes = summary.data ? Object.entries(summary.data.outcomes).sort((a, b) => b[1] - a[1]) : []
  const stages = summary.data ? Object.entries(summary.data.first_failing_stages).sort((a, b) => b[1] - a[1]).slice(0, 8) : []
  return <div className="space-y-4"><div className="flex flex-wrap justify-between gap-3"><h1 className="text-[15px] font-semibold">Jobs</h1><button className="btn" onClick={reload}>Refresh jobs</button></div>
    <p className="text-muted">Latest 100 retained task recordings and a 24-hour summary. A worker must configure a recorder and shared or separately inspected storage. Missing jobs do not prove no jobs ran.</p>
    {summary.error ? <ErrorBox error={summary.error} onRetry={summary.reload} /> : summary.data && <div className="grid gap-4 md:grid-cols-2">
      <section className="card p-4 space-y-2"><div className="flex flex-wrap items-center justify-between gap-2"><h2 className="font-semibold">Outcome distribution</h2><span className="text-muted">{summary.data.sampled} of {summary.data.total} recordings</span></div>
        <div className="flex flex-wrap gap-2">{outcomes.length ? outcomes.map(([outcome, count]) => <Pill key={outcome} tone={outcomeTone(outcome)}>{outcome}: {count}</Pill>) : <span className="text-muted">No task outcomes in this window.</span>}</div>
        {summary.data.truncated && <p className="text-warn">The window has more recordings than the 500-row sample; counts are a lower bound.</p>}
        <p className="text-muted">{summary.data.limitation}</p>
      </section>
      <section className="card p-4 space-y-2"><h2 className="font-semibold">First failing stage</h2>
        {stages.length ? <ul className="space-y-1">{stages.map(([stage, count]) => <li key={stage} className="flex justify-between gap-3"><span className="break-all">{stage}</span><span className="num font-semibold">{count}</span></li>)}</ul> : <p className="text-muted">No stage-attributed failures in this window.</p>}
        {summary.data.unattributed_failures > 0 && <p className="text-muted">{summary.data.unattributed_failures} failed job(s) have no recorded stage.</p>}
        <p className="text-muted">A stage is attributed when it raises; later stages are not blamed.</p>
      </section>
    </div>}
    {d.error ? <ErrorBox error={d.error} onRetry={d.reload} /> : !d.data ? <Spinner /> : <>
      {!d.data.items.length && <div className="card p-4 space-y-2"><h2 className="font-semibold">No jobs recorded</h2><p>Wrap work with <code>job()</code> or install the optional Celery integration in the worker process. Task arguments are not captured.</p></div>}
      {d.data.items.map(item => <Link key={`${item.id}:${item.tags.secondary_store ?? 'primary'}`} to={`/requests/${item.id}`} aria-disabled={item.ambiguous_trace_id} onClick={event => { if (item.ambiguous_trace_id) event.preventDefault() }} className="card block p-4 space-y-2 hover:bg-raised/50"><div className="flex flex-wrap justify-between gap-2"><span className="font-semibold break-all">{item.tags['job.name'] ?? item.path}</span><Pill tone={item.status >= 400 ? 'bad' : item.tags['job.outcome'] === 'retry' ? 'warn' : 'ok'}>{item.tags['job.outcome'] ?? String(item.status)}</Pill></div><div className="text-muted">{ms(item.duration_ms)} · {item.query_count} queries · attempt {item.tags['job.attempt'] ?? 'unknown'} · {new Date(item.started_at * 1000).toLocaleString()}</div>{item.tags['job.first_failing_stage'] && <p className="text-warn">First failing stage: {item.tags['job.first_failing_stage']}</p>}{item.tags.synthetic_demo && <p className="text-info">Synthetic workflow example; timings are illustrative.</p>}{item.ambiguous_trace_id && <p className="text-warn">Duplicate trace ID across stores. Inspection is unavailable.</p>}{item.tags.commit_observed_before_failure && <p className="text-warn">A database commit was observed before this failure. Inspect transaction evidence before rerunning.</p>}</Link>)}
    </>}
  </div>
}
