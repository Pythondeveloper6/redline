import { Link } from 'react-router-dom'
import { apiChecks } from '../apiChecks'
import { ago } from '../lib/format'
import { useNow } from '../lib/hooks'
import { registerRoute } from '../lib/routes'
import { GitChip, SummaryBadges, useCheckAccess } from '../components/CheckParts'
import { LoadError } from '../components/Unsupported'
import { Empty, I, Icon, Pill, Spinner, useAsync } from '../components/ui'

registerRoute({ path: '/checks', label: 'Checks', keywords: 'refactor check baseline compare regression suite verdict', hint: 'Refactor safety checks' })
registerRoute({ path: '/checks/new', label: 'New check suite', keywords: 'refactor check builder create suite endpoints', hint: 'Build a refactor check suite' })

export function CheckDisabled() {
  return <div className="card"><Empty title="Refactor checks are disabled"><p>The <span className="mono">checks</span> feature is switched off on this server. Enable it in the Redline configuration to use this page.</p></Empty></div>
}

export default function Checks() {
  const access = useCheckAccess()
  const now = useNow(15000)
  const d = useAsync(() => apiChecks.suites(), [])
  if (access.loaded && !access.enabled) return <CheckDisabled />
  const items = d.data?.items ?? []
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="m-0 text-[15px] font-semibold flex-1">Refactor checks</h1>
        {!access.readOnly && <Link to="/checks/new" className="btn btn-accent"><Icon d={I.play} size={12} />New suite</Link>}
      </div>
      <p className="m-0 text-muted max-w-3xl">Record a baseline against the old code, run the same read-only requests against the new code, and see what got faster, slower or changed.</p>
      {d.error ? <LoadError error={d.error} what="refactor checks" onRetry={d.reload} /> : d.loading && !d.data ? <Spinner /> : items.length === 0 ? (
        <div className="card">
          <Empty title="No check suites yet">
            <p>A suite lists the read-only endpoints to compare. Build one from the routes Redline has already seen, or from the CLI:</p>
            <p className="m-0"><code className="mono rounded bg-raised px-2 py-1">redline check init --from-traces</code></p>
            {!access.readOnly && <p><Link to="/checks/new" className="btn btn-accent inline-flex">Create your first suite</Link></p>}
          </Empty>
        </div>
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full border-collapse text-[12.5px]">
            <thead className="border-b border-line bg-raised/50"><tr>
              <th className="th">Suite</th><th className="th text-right">Requests</th><th className="th hidden md:table-cell">Last baseline</th>
              <th className="th">Last compare</th><th className="th hidden sm:table-cell">Updated</th>
            </tr></thead>
            <tbody>{items.map(s => (
              <tr key={s.id} className="border-b border-line last:border-0 hover:bg-raised/50 align-top">
                <td className="td"><Link to={`/checks/suites/${s.id}`} className="font-medium hover:underline">{s.name}</Link></td>
                <td className="td text-right num">{s.requests}</td>
                <td className="td hidden md:table-cell">
                  {s.last_baseline ? (
                    <Link to={`/checks/runs/${s.last_baseline.id}`} className="hover:underline inline-flex items-center gap-2">
                      <span>{s.last_baseline.label || 'baseline'}</span><GitChip git={s.last_baseline.git} />
                      <span className="text-muted" title={new Date(s.last_baseline.created_at * 1000).toLocaleString()}>{ago(s.last_baseline.created_at, now)}</span>
                    </Link>
                  ) : <Pill>no baseline</Pill>}
                </td>
                <td className="td !whitespace-normal">
                  {s.last_compare ? (
                    <Link to={`/checks/runs/${s.last_compare.id}`} className="inline-flex flex-wrap items-center gap-1.5 hover:underline">
                      {s.last_compare.status === 'done' ? <SummaryBadges summary={s.last_compare.summary} /> : <Pill tone={s.last_compare.status === 'failed' ? 'bad' : 'warn'}>{s.last_compare.status}</Pill>}
                      <span className="text-muted text-[11.5px]">{ago(s.last_compare.created_at, now)}</span>
                    </Link>
                  ) : <span className="text-faint">never compared</span>}
                </td>
                <td className="td hidden sm:table-cell text-muted" title={new Date(s.updated_at * 1000).toLocaleString()}>{ago(s.updated_at, now)}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
