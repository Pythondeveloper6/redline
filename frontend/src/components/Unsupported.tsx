import { Empty, ErrorBox } from './ui'
import { isUnsupported } from '../apiLoad'

/** Friendly message for 404/501 answers from a backend that predates an endpoint. */
export function Unsupported({ what, children }: { what: string; children?: React.ReactNode }) {
  return (
    <div className="card">
      <Empty title={`This backend doesn't support ${what} yet`}>
        <p>The Redline server answering these requests is older than the UI, or its storage backend cannot serve this data. Upgrade <span className="mono">redline-core</span> to use this page.</p>
        {children}
      </Empty>
    </div>
  )
}

/** Renders an error, using the "unsupported" empty state for 404/405/501. */
export function LoadError({ error, what, onRetry }: { error: Error; what: string; onRetry?: () => void }) {
  return isUnsupported(error) ? <Unsupported what={what} /> : <ErrorBox error={error} onRetry={onRetry} />
}
