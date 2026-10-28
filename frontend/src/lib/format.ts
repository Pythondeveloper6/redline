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
  serializer: '#6366f1', resolver: '#0ea5e9', transaction: '#84cc16', blocking: '#ef4444', pool_wait: '#f97316',
}
export const ISSUE_LABEL: Record<string, string> = {
  n_plus_one: 'N+1', duplicate_query: 'Duplicate', slow_query: 'Slow query', too_many_queries: 'Too many queries',
  blocking_in_async: 'Blocking in async', large_result: 'Large result', unbounded_query: 'Unbounded query',
  inefficient_count: 'Inefficient count', slow_serializer: 'Slow serializer', long_transaction: 'Long transaction',
  external_in_transaction: 'HTTP in transaction', slow_external: 'Slow external call', duplicate_external: 'Duplicate external call',
  slow_cache_decode: 'Slow cache decode',
  json_decode_hotspot: 'JSON decoding hotspot',
  many_orm_instances: 'Many ORM instances',
  retry_storm: 'Retry storm',
  repeated_writes: 'Repeated writes',
  deep_offset: 'Deep offset',
  database_round_trips: 'Database round trips',
  connection_pool_wait: 'Connection pool wait',
  large_response: 'Large response',
  repeated_scan: 'Repeated scan',
  large_cache_entry: 'Large cache entry',

}
/** Icon path (24x24 stroke icon) and tone per issue kind. */
export const ISSUE_STYLE: Record<string, { tone: 'bad' | 'warn' | 'info'; d: string }> = {
  n_plus_one: { tone: 'bad', d: 'M4 6h16M4 12h16M4 18h16M8 3v6M12 9v6M16 15v6' },
  duplicate_query: { tone: 'warn', d: 'M9 9h10v10H9zM5 15V5h10' },
  slow_query: { tone: 'bad', d: 'M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z' },
  too_many_queries: { tone: 'info', d: 'M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 7v10c0 1.7 3.6 3 8 3s8-1.3 8-3V7' },
  blocking_in_async: { tone: 'bad', d: 'M12 3a9 9 0 100 18 9 9 0 000-18zM5.6 5.6l12.8 12.8' },
  large_result: { tone: 'warn', d: 'M4 6h16M4 10h16M4 14h16M4 18h10' },
  unbounded_query: { tone: 'warn', d: 'M3 12h18M17 8l4 4-4 4M7 8l-4 4 4 4' },
  inefficient_count: { tone: 'info', d: 'M5 5h14v14H5zM9 9h6M9 13h6' },
  slow_serializer: { tone: 'warn', d: 'M8 4H6a2 2 0 00-2 2v3a2 2 0 01-2 2 2 2 0 012 2v3a2 2 0 002 2h2M16 4h2a2 2 0 012 2v3a2 2 0 002 2 2 2 0 00-2 2v3a2 2 0 01-2 2h-2' },
  long_transaction: { tone: 'warn', d: 'M6 3h12M6 21h12M7 3c0 5 5 6 5 9s-5 4-5 9M17 3c0 5-5 6-5 9s5 4 5 9' },
  external_in_transaction: { tone: 'bad', d: 'M12 3a9 9 0 100 18 9 9 0 000-18zM3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18' },
  slow_external: { tone: 'bad', d: 'M12 3a9 9 0 100 18 9 9 0 000-18zM12 7v5l3 2' },
  duplicate_external: { tone: 'warn', d: 'M9 9h10v10H9zM5 15V5h10' },
  retry_storm: { tone: 'bad', d: 'M20 11a8 8 0 00-14.9-3M4 4v4h4M4 13a8 8 0 0014.9 3M20 20v-4h-4' },
}
