import { useEffect } from 'react'
import { Logo } from './components/Shell'
import { setTheme, useIsDark } from './theme'
import RequestDetail from './pages/RequestDetail'
import { getSnapshot } from './lib/snapshot'
import { Icon, I } from './components/ui'

/** Offline snapshot: only the request detail of the inlined trace; no navigation, no API. */
export default function SnapshotApp() {
  const snap = getSnapshot()!
  const t = snap.trace
  const dark = useIsDark()
  useEffect(() => { document.title = `Redline snapshot: ${t.method} ${t.path}` }, [t])
  return (
    <div className="min-h-full flex flex-col">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto max-w-[1500px] px-3 sm:px-5 min-h-12 flex items-center gap-3">
          <Logo />
          <span className="flex-1" />
          <button className="btn" onClick={() => setTheme(dark ? 'light' : 'dark')} aria-label="Toggle theme"><Icon d={dark ? I.sun : I.moon} size={12} /></button>
        </div>
        <div className="border-t border-info/20 bg-info/10 text-info">
          <div className="mx-auto max-w-[1500px] px-3 sm:px-5 py-1.5 text-[12px]" role="status">
            Offline snapshot of <b className="mono break-all">{t.method} {t.path}</b> captured {new Date(t.started_at * 1000).toLocaleString()}
          </div>
        </div>
      </header>
      <main className="flex-1 mx-auto w-full max-w-[1500px] px-3 sm:px-5 py-4">
        <RequestDetail snapshotId={t.id} />
      </main>
    </div>
  )
}
