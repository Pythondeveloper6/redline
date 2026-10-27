import { useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../api'
import { setTheme, useTheme, type ThemePref } from '../theme'
import { ConfirmDialog, I, Icon, useAsync } from './ui'

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2">
      <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
        <rect width="32" height="32" rx="7" fill="rgb(var(--accent))" />
        <path d="M6 21h5l3-10 4 14 3-9 2 5h3" fill="none" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="text-[15px] font-bold tracking-tight">Redline</span>
    </span>
  )
}

const THEMES: { id: ThemePref; label: string; d: string }[] = [
  { id: 'light', label: 'Light', d: I.sun }, { id: 'system', label: 'System', d: 'M4 5h16v11H4zM9 20h6M12 16v4' }, { id: 'dark', label: 'Dark', d: I.moon },
]

function ThemeToggle() {
  const t = useTheme()
  return (
    <div className="inline-flex rounded-md border border-line p-0.5 bg-surface" role="radiogroup" aria-label="Theme">
      {THEMES.map(o => (
        <button key={o.id} role="radio" aria-checked={t === o.id} title={o.label} aria-label={o.label} onClick={() => setTheme(o.id)}
          className={`h-6 w-6 grid place-items-center rounded ${t === o.id ? 'bg-raised text-ink' : 'text-faint hover:text-ink'}`}>
          <Icon d={o.d} size={13} />
        </button>
      ))}
    </div>
  )
}

export default function Shell() {
  const meta = useAsync(() => api.meta(), [])
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [epoch, setEpoch] = useState(0)
  const link = ({ isActive }: { isActive: boolean }) =>
    `px-2.5 h-8 inline-flex items-center rounded-md text-[12.5px] font-medium ${isActive ? 'bg-raised text-ink' : 'text-muted hover:text-ink hover:bg-raised/60'}`
  return (
    <div className="min-h-full flex flex-col">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto max-w-[1500px] px-3 sm:px-5 min-h-12 py-1.5 sm:py-0 flex flex-wrap sm:flex-nowrap items-center gap-x-3 gap-y-0">
          <NavLink to="/requests" className="shrink-0"><Logo /></NavLink>
          <nav className="order-last w-full sm:order-none sm:w-auto flex items-center gap-0.5 sm:ml-3 -mx-1 sm:mx-0 pb-1.5 sm:pb-0">
            <NavLink to="/requests" className={link}>Requests</NavLink>
            <NavLink to="/endpoints" className={link}>Endpoints</NavLink>
            <NavLink to="/issues" className={link}>Issues</NavLink>
            <NavLink to="/history" className={link}>History</NavLink>
            <NavLink to="/load" className={link}>Load</NavLink>
          </nav>
          <div className="flex-1" />
          {meta.data && (
            <span className="hidden md:inline-flex items-center gap-1.5 text-[11px] text-muted mono" title={meta.data.storage.path}>
              {meta.data.framework} <span className="text-faint">v{meta.data.version}</span>
            </span>
          )}
          <ThemeToggle />
          <button className="btn" onClick={() => setConfirm(true)} title="Delete all recorded requests"><Icon d={I.trash} size={12} /><span className="hidden sm:inline">Clear all</span></button>
        </div>
      </header>
      <main className="flex-1 mx-auto w-full max-w-[1500px] px-3 sm:px-5 py-4" key={epoch}>
        <Outlet />
      </main>
      <ConfirmDialog open={confirm} busy={busy} title="Clear all recorded requests?" confirmLabel="Delete everything"
        body={<>This permanently deletes every stored request, query, profile and issue{meta.data ? <> ({meta.data.storage.rows.toLocaleString()} requests)</> : null}. It cannot be undone.</>}
        onCancel={() => setConfirm(false)}
        onConfirm={async () => { setBusy(true); try { await api.clear(); setEpoch(e => e + 1); meta.reload() } finally { setBusy(false); setConfirm(false) } }} />
    </div>
  )
}
