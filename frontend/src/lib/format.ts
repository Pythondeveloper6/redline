export function ms(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v === 0) return '0 ms'
  if (v >= 10000) return (v / 1000).toFixed(1) + ' s'
  if (v >= 1000) return (v / 1000).toFixed(2) + ' s'
  if (v >= 100) return Math.round(v) + ' ms'
  if (v >= 10) return v.toFixed(1) + ' ms'
  if (v >= 0.1) return v.toFixed(2) + ' ms'
  return (v * 1000).toFixed(0) + ' µs'
}
export function ago(epoch: number, now = Date.now() / 1000): string {
  const d = Math.max(0, now - epoch)
  if (d < 5) return 'just now'
  if (d < 60) return Math.floor(d) + 's ago'
  if (d < 3600) return Math.floor(d / 60) + 'm ago'
  if (d < 86400) return Math.floor(d / 3600) + 'h ago'
  return Math.floor(d / 86400) + 'd ago'
}
export const clock = (epoch: number) => new Date(epoch * 1000).toLocaleTimeString([], { hour12: false })
export const num = (n: number) => n.toLocaleString()
export function bytes(n: number | null | undefined) {
  if (n == null) return '-'
  if (n < 1024) return n + ' B'
  if (n < 1048576) return (n / 1024).toFixed(1) + ' KB'
  return (n / 1048576).toFixed(1) + ' MB'
}
export const pct = (r: number) => (r * 100).toFixed(r > 0 && r < 0.01 ? 2 : 1) + '%'

export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch {
    try {
      const t = document.createElement('textarea')
      t.value = text; t.style.position = 'fixed'; t.style.opacity = '0'
      document.body.appendChild(t); t.select()
      const ok = document.execCommand('copy'); t.remove(); return ok
    } catch { return false }
  }
}

export const REDACTED = /^\[?\s*(redacted|\*{3,}|•{3,}|<redacted>)\s*\]?$/i
export const isRedacted = (v: unknown) => typeof v === 'string' && REDACTED.test(v.trim())

export const KIND_COLOR: Record<string, string> = {
  sql: '#3b82f6', cache: '#f59e0b', http_out: '#a855f7', template: '#14b8a6',
  middleware: '#94a3b8', view: '#10b981', custom: '#ec4899',
}
export const ISSUE_LABEL: Record<string, string> = {
  n_plus_one: 'N+1', duplicate_query: 'Duplicate', slow_query: 'Slow query', too_many_queries: 'Too many queries',
}
