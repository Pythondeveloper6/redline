// Pure builders for "Copy as cURL / HTTPie". No DOM access, so they are easy to unit test.
//
// Redacted values (the backend's mask is "[redacted]") are never emitted; they become shell
// variables such as "$AUTHORIZATION" that the user must export before running the command.
import { safePath } from './url.ts'

/** Text piece of a command: a literal, or a reference to an environment variable. */
export type Part = string | { v: string }

export interface ReqSpec {
  method: string
  path: string
  query: Record<string, unknown>
  headers: Record<string, unknown>
  body: string | null
}
export interface Built { command: string; vars: string[] }

const MASK = /^\s*\[redacted\]\s*$/i
export const isMasked = (v: unknown): boolean => typeof v === 'string' && MASK.test(v)

/** Environment variable name for a redacted header / key: `x-api-key` -> `X_API_KEY`. */
export function toVar(name: string): string {
  let v = name.toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '')
  if (!v) v = 'REDACTED'
  return /^[0-9]/.test(v) ? '_' + v : v
}

/** Single-quote a string for POSIX shells (also safe for newlines). */
export function shq(s: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s
  return "'" + s.replace(/'/g, "'\\''") + "'"
}

/** Join literals and variables into one shell word, e.g. `'Authorization: '"$AUTHORIZATION"`. */
export function word(parts: Part[]): string {
  const merged: Part[] = []
  for (const p of parts) {
    if (typeof p === 'string' && typeof merged[merged.length - 1] === 'string') merged[merged.length - 1] += p
    else merged.push(p)
  }
  const out = merged.map(p => (typeof p === 'string' ? (p === '' ? '' : shq(p)) : `"$${p.v}"`)).join('')
  return out === '' ? "''" : out
}

// "key": "[redacted]" (JSON) | key=[redacted] (form) | "[redacted]" | [redacted]
const BODY_MASK = /"((?:[^"\\]|\\.)*)"(\s*:\s*)"\[redacted\]"|([^&=\s"]+)=\[redacted\]|"\[redacted\]"|\[redacted\]/gi

/** Splits a captured body into literal text and the places where a redacted value was. */
export function tokenizeBody(body: string): Part[] {
  const out: Part[] = []
  let last = 0
  for (const m of body.matchAll(BODY_MASK)) {
    const at = m.index ?? 0
    if (at > last) out.push(body.slice(last, at))
    if (m[1] !== undefined) out.push(`"${m[1]}"${m[2]}"`, { v: toVar(m[1].replace(/\\(.)/g, '$1')) }, '"')
    else if (m[3] !== undefined) out.push(m[3] + '=', { v: toVar(m[3]) })
    else if (m[0].startsWith('"')) out.push('"', { v: 'REDACTED' }, '"')
    else out.push({ v: 'REDACTED' })
    last = at + m[0].length
  }
  if (last < body.length) out.push(body.slice(last))
  return out
}

const SKIP_HEADERS = new Set(['host', 'content-length', 'connection', 'accept-encoding', 'transfer-encoding', 'keep-alive'])
const str = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v))

function collect(parts: Part[], vars: string[]) {
  for (const p of parts) if (typeof p !== 'string' && !vars.includes(p.v)) vars.push(p.v)
}

/**
 * URL parts: base + path + query string with redacted values left as variables. The path is
 * always kept on `base` (`//evil.example/x` becomes `<base>/evil.example/x`).
 */
export function urlParts(base: string, path: string, query: Record<string, unknown>): Part[] {
  path = safePath(path)
  const parts: Part[] = [base.replace(/\/+$/, '') + path]
  let sep = path.includes('?') ? '&' : '?'
  for (const [k, raw] of Object.entries(query)) {
    for (const v of Array.isArray(raw) ? raw : [raw]) {
      parts.push(`${sep}${encodeURIComponent(k)}=`)
      if (isMasked(v)) parts.push({ v: toVar(k) })
      else parts.push(encodeURIComponent(str(v)))
      sep = '&'
    }
  }
  return parts
}

const hasBody = (r: ReqSpec) => r.body != null && r.body !== '' && !['GET', 'HEAD'].includes(r.method.toUpperCase())

function headerEntries(r: ReqSpec): [string, unknown][] {
  return Object.entries(r.headers).filter(([k]) => !SKIP_HEADERS.has(k.toLowerCase()))
}

export function buildCurl(r: ReqSpec, base: string): Built {
  const vars: string[] = []
  const method = r.method.toUpperCase()
  const url = urlParts(base, r.path, r.query)
  collect(url, vars)
  const first = ['curl']
  if (method === 'HEAD') first.push('-I')
  else if (method !== 'GET') first.push('-X', shq(method))
  first.push(word(url))
  const lines: string[] = [first.join(' ')]
  for (const [k, v] of headerEntries(r)) {
    const parts: Part[] = isMasked(v) ? [`${k}: `, { v: toVar(k) }] : [`${k}: ${str(v)}`]
    collect(parts, vars)
    lines.push(`-H ${word(parts)}`)
  }
  if (hasBody(r)) {
    const parts = tokenizeBody(r.body!)
    collect(parts, vars)
    lines.push(`--data-raw ${word(parts)}`)
  }
  return { command: lines.join(' \\\n  '), vars }
}

const hesc = (s: string) => s.replace(/[\\=:@;]/g, c => '\\' + c)

export function buildHttpie(r: ReqSpec, base: string): Built {
  const vars: string[] = []
  const method = r.method.toUpperCase()
  const url = urlParts(base, r.path, {})
  const head: string[] = ['http']
  if (hasBody(r)) {
    const parts = tokenizeBody(r.body!)
    collect(parts, vars)
    head.push(`--raw ${word(parts)}`)
  }
  head.push(method, word(url))
  const lines = [head.join(' ')]
  for (const [k, raw] of Object.entries(r.query)) {
    for (const v of Array.isArray(raw) ? raw : [raw]) {
      const parts: Part[] = isMasked(v) ? [`${hesc(k)}==`, { v: toVar(k) }] : [`${hesc(k)}==${str(v)}`]
      collect(parts, vars)
      lines.push(word(parts))
    }
  }
  for (const [k, v] of headerEntries(r)) {
    let parts: Part[]
    if (isMasked(v)) parts = [`${hesc(k)}:`, { v: toVar(k) }]
    else if (str(v) === '') parts = [`${hesc(k)};`]
    else parts = [`${hesc(k)}:${str(v)}`]
    collect(parts, vars)
    lines.push(word(parts))
  }
  return { command: lines.join(' \\\n  '), vars }
}

/** Reminder printed next to a command that references environment variables. */
export function varsNote(vars: string[]): string {
  return vars.length ? `Secrets were redacted when this request was recorded. Set ${vars.map(v => '$' + v).join(', ')} before running it.` : ''
}
