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
