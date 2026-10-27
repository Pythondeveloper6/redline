import { editorUrl, useMeta } from '../lib/meta'
import type { Callsite } from '../types'

type Loc = Pick<Callsite, 'file' | 'line'> & Partial<Callsite>

/** `file:line` that opens in the user's editor when the backend exposes an editor URL template; plain text otherwise. */
export default function CallsiteLink({ callsite, className = '', children }: { callsite: Loc | null | undefined; className?: string; children?: React.ReactNode }) {
  const meta = useMeta()
  if (!callsite) return null
  const url = editorUrl(meta, callsite)
  const label = children ?? <>{callsite.file}<span className="text-faint">:</span>{callsite.line}</>
  if (!url) return <span className={`mono text-muted ${className}`}>{label}</span>
  return (
    <a href={url} onClick={e => e.stopPropagation()} title={`Open in ${meta?.editor || 'editor'}`}
      className={`mono text-muted hover:text-info hover:underline underline-offset-2 decoration-dotted ${className}`}>{label}</a>
  )
}
