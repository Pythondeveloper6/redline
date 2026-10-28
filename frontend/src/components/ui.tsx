import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { copyText } from '../lib/format'
import { tokenizeSql, formatSql } from '../lib/sql'
import type { IssueKind, Severity } from '../types'
import { ISSUE_LABEL, ISSUE_STYLE } from '../lib/format'
import CallsiteLink from './CallsiteLink'

export function Icon({ d, size = 14, className = '' }: { d: string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <path d={d} />
    </svg>
  )
}
export const I = {
  copy: 'M9 9h10v10H9zM5 15V5h10', check: 'M5 12l5 5 9-10', x: 'M6 6l12 12M18 6L6 18', sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 8a4 4 0 100 8 4 4 0 000-8z',
  moon: 'M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z', pause: 'M8 5v14M16 5v14', play: 'M7 4l13 8-13 8z',
  down: 'M12 4v12M6 12l6 6 6-6', search: 'M11 4a7 7 0 100 14 7 7 0 000-14zM21 21l-5-5', trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
  chev: 'M9 6l6 6-6 6', zap: 'M13 2L4 14h7l-1 8 9-12h-7z', back: 'M15 6l-6 6 6 6', logout: 'M10 5H5v14h5M15 8l4 4-4 4M19 12H9', alert: 'M12 9v4M12 17h.01M10.3 3.9L2.4 18a2 2 0 001.7 3h15.8a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z',
}

export function CopyButton({ text, label = 'Copy', className = '' }: { text: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button className={`btn ${className}`} onClick={async () => { if (await copyText(text)) { setDone(true); setTimeout(() => setDone(false), 1400) } }}>
      <Icon d={done ? I.check : I.copy} size={12} />{done ? 'Copied' : label}
    </button>
  )
}

// Tailwind needs literal class names; keep a safelist-friendly map.
export const TONE: Record<string, string> = {
  ok: 'bg-ok/10 text-ok', warn: 'bg-warn/10 text-warn', bad: 'bg-bad/10 text-bad', info: 'bg-info/10 text-info', neutral: 'bg-raised text-muted',
}
export function Pill({ tone = 'neutral', children, title }: { tone?: keyof typeof TONE; children: ReactNode; title?: string }) {
  return <span title={title} className={`inline-flex items-center gap-1 rounded px-1.5 h-[18px] text-[11px] font-medium whitespace-nowrap ${TONE[tone]}`}>{children}</span>
}
export const statusTone = (s: number) => (s >= 500 ? 'bad' : s >= 400 ? 'warn' : s >= 300 ? 'info' : 'ok') as keyof typeof TONE
export function Status({ code }: { code: number }) { return <Pill tone={statusTone(code)}><span className="num font-semibold">{code}</span></Pill> }

export const sevTone = (s: Severity) => (s === 'critical' ? 'bad' : s === 'warning' ? 'warn' : 'info') as keyof typeof TONE
export function IssueBadge({ kind, icon = false }: { kind: IssueKind; icon?: boolean }) {
  const st = ISSUE_STYLE[kind]
  return <Pill tone={st?.tone ?? 'info'}>{icon && st && <Icon d={st.d} size={11} />}{ISSUE_LABEL[kind] ?? kind}</Pill>
}
export function Sev({ s }: { s: Severity }) { return <Pill tone={sevTone(s)}>{s}</Pill> }

export function Sql({ sql, format = true, className = '' }: { sql: string; format?: boolean; className?: string }) {
  const text = format ? formatSql(sql) : sql
  return (
    <pre className={`m-0 whitespace-pre-wrap break-words leading-relaxed ${className}`}>
      {tokenizeSql(text).map((t, i) => (t.t === 'txt' ? t.v : <span key={i} className={`tok-${t.t}`}>{t.v}</span>))}
    </pre>
  )
}

export function DiffView({ diff, title = 'Suggested diff', copyLabel = 'Copy diff' }: { diff: string; title?: string; copyLabel?: string }) {
  return (
    <div className="rounded-md border border-line overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 bg-raised border-b border-line">
        <span className="text-[11px] font-medium text-muted">{title}</span>
        <CopyButton text={diff} label={copyLabel} className="h-6" />
      </div>
      <pre className="m-0 overflow-x-auto py-1">
        {diff.split('\n').map((l, i) => {
          const cls = l.startsWith('+++') || l.startsWith('---') ? 'text-muted font-semibold'
            : l.startsWith('+') ? 'bg-ok/10 text-ok' : l.startsWith('-') ? 'bg-bad/10 text-bad'
            : l.startsWith('@@') ? 'text-info' : 'text-muted'
          return <div key={i} className={`px-3 min-h-[1.45em] ${cls}`}>{l || ' '}</div>
        })}
      </pre>
    </div>
  )
}

export function Redacted() {
  return <span className="inline-flex items-center rounded bg-raised px-1.5 h-[18px] text-[11px] text-faint italic select-none">••• redacted</span>
}

export function Empty({ title, children, icon }: { title: string; children?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center text-center px-6 py-14 text-muted">
      {icon && <div className="mb-3 text-faint">{icon}</div>}
      <div className="text-[14px] font-semibold text-ink mb-1">{title}</div>
      <div className="max-w-md space-y-2">{children}</div>
    </div>
  )
}

const ERROR_TITLE: Record<string, string> = {
  unauthorized: 'Sign in required',
  forbidden: 'Access denied',
  csrf_failed: 'Request blocked (CSRF check failed)',
  network: 'Cannot reach the server',
}

export function ErrorBox({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const code = (error as Error & { code?: string }).code
  const title = (code && ERROR_TITLE[code]) || 'Something went wrong'
  return (
    <div className="m-4 rounded-lg border border-bad/30 bg-bad/5 p-4 text-bad flex items-center gap-3" role="alert">
      <Icon d={I.alert} size={16} />
      <div className="flex-1 min-w-0"><div className="font-semibold">{title}</div><div className="text-[12px] opacity-90 break-words">{error.message}</div></div>
      {onRetry && <button className="btn" onClick={onRetry}>Retry</button>}
    </div>
  )
}

export function Spinner() {
  return <div className="p-10 text-center text-faint text-[12px]">Loading…</div>
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="mono rounded border border-line bg-raised px-1 text-[10px] text-muted">{children}</kbd>
}

export function Drawer({ open, onClose, title, subtitle, children, wide }: { open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.stopPropagation(); onClose() } }
    window.addEventListener('keydown', h, true)
    ref.current?.focus()
    return () => window.removeEventListener('keydown', h, true)
  }, [open, onClose])
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-black/40 anim-fade" onClick={onClose} />
      <aside ref={ref} tabIndex={-1} role="dialog" aria-modal="true"
        className={`relative z-10 h-full w-full ${wide ? 'sm:max-w-[760px]' : 'sm:max-w-[620px]'} bg-surface border-l border-line shadow-2xl flex flex-col anim-slide outline-none`}>
        <header className="flex items-start gap-3 px-4 py-3 border-b border-line">
          <div className="flex-1 min-w-0"><div className="text-[14px] font-semibold truncate">{title}</div>{subtitle && <div className="text-[12px] text-muted mt-0.5">{subtitle}</div>}</div>
          <button className="btn btn-ghost !px-1.5" onClick={onClose} aria-label="Close"><Icon d={I.x} size={16} /></button>
        </header>
        <div className="flex-1 overflow-y-auto p-4 space-y-4">{children}</div>
      </aside>
    </div>,
    document.body,
  )
}

export function ConfirmDialog({ open, title, body, confirmLabel, onConfirm, onCancel, busy }: { open: boolean; title: string; body: ReactNode; confirmLabel: string; onConfirm: () => void; onCancel: () => void; busy?: boolean }) {
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open, onCancel])
  if (!open) return null
  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center p-4">
      <div className="absolute inset-0 bg-black/50 anim-fade" onClick={onCancel} />
      <div role="alertdialog" aria-modal="true" className="relative card w-full max-w-sm p-5 shadow-2xl anim-fade">
        <div className="text-[15px] font-semibold mb-1">{title}</div>
        <div className="text-muted mb-4">{body}</div>
        <div className="flex justify-end gap-2">
          <button className="btn" onClick={onCancel} autoFocus>Cancel</button>
          <button className="btn btn-accent" onClick={onConfirm} disabled={busy}>{confirmLabel}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: string; badge?: ReactNode }[]; value: T; onChange: (t: T) => void }) {
  return (
    <div className="flex gap-1 border-b border-line overflow-x-auto" role="tablist">
      {tabs.map(t => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}
          className={`relative px-3 h-9 text-[12.5px] font-medium whitespace-nowrap inline-flex items-center gap-1.5 ${value === t.id ? 'text-ink' : 'text-muted hover:text-ink'}`}>
          {t.label}{t.badge}
          {value === t.id && <span className="absolute left-2 right-2 -bottom-px h-0.5 bg-accent rounded-full" />}
        </button>
      ))}
    </div>
  )
}

export function Callsite({ cs }: { cs: { file: string; line: number; function: string } | null }) {
  return <CallsiteLink callsite={cs} />
}

/** Small data-fetching hook. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [state, set] = useState<{ data?: T; error?: Error; loading: boolean }>({ loading: true })
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let live = true
    set(s => ({ ...s, loading: true }))
    fn().then(data => live && set({ data, loading: false }), error => live && set({ error, loading: false }))
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])
  return { ...state, reload: () => setTick(t => t + 1) }
}
