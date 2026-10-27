import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from '../api'
import { setTheme, useTheme, type ThemePref } from '../theme'
import { isTyping } from '../lib/hooks'
import CommandPalette from './CommandPalette'
import ShortcutsHelp from './ShortcutsHelp'
import { ConfirmDialog, I, Icon, Kbd, useAsync } from './ui'

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2">
      <svg width="22" height="22" viewBox="0 0 64 64" aria-hidden>
        <defs>
          <linearGradient id="rl-logo-bg" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#f87171" />
            <stop offset="1" stopColor="#dc2626" />
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="15" fill="url(#rl-logo-bg)" />
        <path d="M12 26 H52" stroke="#fff" strokeOpacity=".55" strokeWidth="3" strokeLinecap="round" strokeDasharray="2 6" />
        <path d="M10 42 H19 L25 33 L32 47 L40 17 L46 37 H54" fill="none" stroke="#fff" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
        <circle cx="40" cy="17" r="5.5" fill="#fff" />
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

const PRIMARY = [
  { to: '/requests', label: 'Requests' }, { to: '/endpoints', label: 'Endpoints' },
  { to: '/issues', label: 'Issues' }, { to: '/database', label: 'Database' },
]
const MORE = [
  { to: '/cache', label: 'Cache', hint: 'Hit rates, stampedes' }, { to: '/external', label: 'External', hint: 'Outbound HTTP' },
  { to: '/history', label: 'History', hint: 'Trends and releases' }, { to: '/load', label: 'Load', hint: 'Load runs' },
]

function MoreMenu({ pathname }: { pathname: string }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const active = MORE.find(m => pathname === m.to || pathname.startsWith(m.to + '/'))
  useEffect(() => { setOpen(false) }, [pathname])
  useEffect(() => {
    if (!open) return
    const down = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', down); document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', down); document.removeEventListener('keydown', key) }
  }, [open])
  return (
    <div className="relative" ref={ref}>
      <button aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(o => !o)}
        className={`px-2.5 h-8 inline-flex items-center gap-1 rounded-md text-[12.5px] font-medium ${active ? 'bg-raised text-ink' : 'text-muted hover:text-ink hover:bg-raised/60'}`}>
        {active ? active.label : 'More'}<Icon d="M6 9l6 6 6-6" size={11} />
      </button>
      {open && (
        <div role="menu" className="absolute left-0 sm:left-auto sm:right-0 top-9 z-40 w-48 card shadow-xl py-1 anim-fade">
          {MORE.map(m => (
            <NavLink key={m.to} to={m.to} role="menuitem" className={({ isActive }) => `flex flex-col px-3 py-1.5 hover:bg-raised ${isActive ? 'bg-raised' : ''}`}>
              <span className="text-[12.5px] font-medium">{m.label}</span><span className="text-[11px] text-muted">{m.hint}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Shell() {
  const meta = useAsync(() => api.meta(), [])
  const [confirm, setConfirm] = useState(false)
  const [busy, setBusy] = useState(false)
  const [epoch, setEpoch] = useState(0)
  const [palette, setPalette] = useState(false)
  const [help, setHelp] = useState(false)
  const { pathname } = useLocation()
  const onList = pathname === '/requests'
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); setHelp(false); setPalette(o => !o); return }
      if (isTyping(e) || e.metaKey || e.ctrlKey || e.altKey || document.querySelector('[aria-modal="true"]')) return
      // On the Requests list `/` focuses the filter box (handled by the page); everywhere else it opens the palette.
      if (e.key === '/' && !onList) { e.preventDefault(); setPalette(true) }
      else if (e.key === '?') { e.preventDefault(); setHelp(true) }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onList])
  const link = ({ isActive }: { isActive: boolean }) =>
    `px-2.5 h-8 inline-flex items-center rounded-md text-[12.5px] font-medium ${isActive ? 'bg-raised text-ink' : 'text-muted hover:text-ink hover:bg-raised/60'}`
  return (
    <div className="min-h-full flex flex-col">
      <header className="sticky top-0 z-30 border-b border-line bg-surface/90 backdrop-blur">
        <div className="mx-auto max-w-[1500px] px-3 sm:px-5 min-h-12 py-1.5 sm:py-0 flex flex-wrap sm:flex-nowrap items-center gap-x-3 gap-y-0">
          <NavLink to="/requests" className="shrink-0"><Logo /></NavLink>
          <nav className="order-last w-full sm:order-none sm:w-auto flex items-center gap-0.5 sm:ml-3 -mx-1 sm:mx-0 pb-1.5 sm:pb-0">
            {PRIMARY.map(n => <NavLink key={n.to} to={n.to} className={link}>{n.label}</NavLink>)}
            <MoreMenu pathname={pathname} />
          </nav>
          <div className="flex-1" />
          {meta.data && (
            <span className="hidden md:inline-flex items-center gap-1.5 text-[11px] text-muted mono" title={meta.data.storage.path}>
              {meta.data.framework} <span className="text-faint">v{meta.data.version}</span>
            </span>
          )}
          <button className="btn text-muted" onClick={() => setPalette(true)} aria-label="Open command palette" title="Command palette">
            <Icon d={I.search} size={12} /><span className="hidden sm:inline">Search</span><span className="hidden md:inline-flex gap-0.5"><Kbd>{/mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent) ? '⌘' : 'Ctrl'}</Kbd><Kbd>K</Kbd></span>
          </button>
          <ThemeToggle />
          <button className="btn" onClick={() => setConfirm(true)} title="Delete all recorded requests"><Icon d={I.trash} size={12} /><span className="hidden sm:inline">Clear all</span></button>
        </div>
      </header>
      <main className="flex-1 mx-auto w-full max-w-[1500px] px-3 sm:px-5 py-4" key={epoch}>
        <Outlet />
      </main>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
      <ShortcutsHelp open={help} onClose={() => setHelp(false)} />
      <ConfirmDialog open={confirm} busy={busy} title="Clear all recorded requests?" confirmLabel="Delete everything"
        body={<>This permanently deletes every stored request, query, profile and issue{meta.data ? <> ({meta.data.storage.rows.toLocaleString()} requests)</> : null}. It cannot be undone.</>}
        onCancel={() => setConfirm(false)}
        onConfirm={async () => { setBusy(true); try { await api.clear(); setEpoch(e => e + 1); meta.reload() } finally { setBusy(false); setConfirm(false) } }} />
    </div>
  )
}
