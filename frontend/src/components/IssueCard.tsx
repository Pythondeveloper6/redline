import { ISSUE_LABEL } from '../lib/format'
import type { Suggestion } from '../types'
import { CopyButton, DiffView, Pill } from './ui'

export function SuggestionBlock({ s }: { s: Suggestion }) {
  return (
    <div className="space-y-2.5">
      <div className="flex items-start gap-2">
        <div className="flex-1"><div className="text-[12px] text-muted mb-0.5">Suggested fix</div><div className="font-medium">{s.summary}</div></div>
        <Pill tone={s.confidence === 'high' ? 'ok' : s.confidence === 'medium' ? 'warn' : 'neutral'} title="How sure the suggester is">{s.confidence} confidence</Pill>
      </div>
      {s.code && (
        <div className="flex items-start gap-2 rounded-md bg-raised border border-line px-3 py-2">
          <pre className="m-0 flex-1 overflow-x-auto whitespace-pre-wrap break-words">{s.code}</pre>
          <CopyButton text={s.code} className="h-6 shrink-0" />
        </div>
      )}
      {s.diff && <DiffView diff={s.diff} />}
      {safeUrl(s.docs_url) && <a href={safeUrl(s.docs_url)!} target="_blank" rel="noreferrer" className="text-info text-[12px] hover:underline">Read the docs</a>}
    </div>
  )
}
// Only http(s) links: a suggestion comes from a (possibly third-party) suggester, and a
// `javascript:` URL in an href would run in the Redline page.
function safeUrl(url: string | null): string | null {
  if (!url) return null
  try { return ['http:', 'https:'].includes(new URL(url, location.href).protocol) ? url : null } catch { return null }
}
export const kindLabel = (k: string) => ISSUE_LABEL[k] ?? k

const EXTRA_LABEL: Record<string, string> = {
  rows: 'Rows', resolver_path: 'Resolver path', path: 'Path', host: 'Host', pattern: 'Pattern', expr: 'Expression', old: 'Current', new: 'Replace with',
  fix_line: 'Fix line', wide_columns: 'Wide columns', serializer: 'Serializer', library: 'Library', items: 'Items', call: 'Call', lag_ms: 'Loop lag (ms)',
  queries: 'Queries', http_out: 'Outbound calls', calls: 'Calls', url: 'URL', status: 'Status', attempts: 'Attempts', limit: 'Limit', db_alias: 'Database',
}
const CODEY = new Set(['resolver_path', 'path', 'host', 'expr', 'old', 'new', 'url', 'call', 'serializer'])
function fmtVal(v: unknown): string {
  if (Array.isArray(v)) return v.join(', ')
  if (v && typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
/** Kind-specific facts the detectors attach (`issue.extra`): rows, resolver path, host, ... */
export function IssueExtra({ extra }: { extra?: Record<string, any> }) {
  const entries = Object.entries(extra ?? {}).filter(([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0))
  if (!entries.length) return null
  return (
    <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 rounded-md bg-raised/60 border border-line px-3 py-2 text-[12px]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{EXTRA_LABEL[k] ?? k.replace(/_/g, ' ')}</dt>
          <dd className={`m-0 min-w-0 break-words ${CODEY.has(k) ? 'mono' : 'num'}`}>{typeof v === 'number' ? v.toLocaleString() : fmtVal(v)}</dd>
        </div>
      ))}
    </dl>
  )
}
