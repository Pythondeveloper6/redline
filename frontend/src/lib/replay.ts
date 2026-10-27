// Pure helpers for the Replay modal. A replay is a plain `fetch` from the browser (same origin,
// the viewer's own cookies). Redacted values are never sent: the user must re-enter them or drop them.
import { isMasked, tokenizeBody } from './curl.ts'
import { sameOriginUrl } from './url.ts'

export interface ReplayTrace {
  id: string; method: string; path: string
  request: { headers: Record<string, unknown>; query: Record<string, unknown>; body: string | null }
}
export interface ReplayField { key: string; kind: 'header' | 'query' | 'body'; name: string; removable: boolean }

/** Headers a browser refuses to set from script (or sets itself); they are dropped from the replay. */
const FORBIDDEN = /^(host|content-length|connection|accept-encoding|cookie|cookie2|origin|referer|user-agent|keep-alive|transfer-encoding|te|trailer|upgrade|via|date|dnt|expect|sec-.*|proxy-.*|access-control-request-.*)$/i
export const canSetHeader = (name: string) => !FORBIDDEN.test(name)

const sendsBody = (t: ReplayTrace) => t.request.body != null && t.request.body !== '' && !['GET', 'HEAD'].includes(t.method.toUpperCase())

/** Every redacted value the user has to supply (or drop) before the request can be sent. */
export function redactedFields(t: ReplayTrace): ReplayField[] {
  const out: ReplayField[] = []
  for (const [k, v] of Object.entries(t.request.headers)) {
    if (isMasked(v) && canSetHeader(k)) out.push({ key: 'header:' + k.toLowerCase(), kind: 'header', name: k, removable: true })
  }
  for (const [k, raw] of Object.entries(t.request.query)) {
    if ((Array.isArray(raw) ? raw : [raw]).some(isMasked)) out.push({ key: 'query:' + k, kind: 'query', name: k, removable: true })
  }
  if (sendsBody(t)) {
    const seen = new Set<string>()
    for (const p of tokenizeBody(t.request.body!)) {
      if (typeof p !== 'string' && !seen.has(p.v)) { seen.add(p.v); out.push({ key: 'body:' + p.v, kind: 'body', name: p.v, removable: false }) }
    }
  }
  return out
}

/** True when the original request carried a redacted Cookie: the browser sends its own cookies instead. */
export const hadRedactedCookie = (t: ReplayTrace) => Object.entries(t.request.headers).some(([k, v]) => k.toLowerCase() === 'cookie' && isMasked(v))

/** Fields that still block sending: not removed and empty. */
export function missingFields(fields: ReplayField[], values: Record<string, string>, removed: ReadonlySet<string>): ReplayField[] {
  return fields.filter(f => !removed.has(f.key) && !(values[f.key] ?? '').length)
}

/** `origin` is the page's own origin (`location.origin`): the replay never leaves it. */
export interface ReplayOpts { profile: boolean; replayOf: string; origin: string }

/** Thrown by {@link buildReplay} when the recorded path would resolve to another origin. */
export class CrossOriginReplay extends Error {
  constructor(path: string) { super(`Refusing to replay ${JSON.stringify(path)}: it does not resolve to this site.`) }
}

export function buildReplay(t: ReplayTrace, values: Record<string, string>, removed: ReadonlySet<string>, opts: ReplayOpts): { url: string; init: RequestInit } {
  const method = t.method.toUpperCase()
  const u = new URLSearchParams()
  for (const [k, raw] of Object.entries(t.request.query)) {
    if (removed.has('query:' + k)) continue
    for (const v of Array.isArray(raw) ? raw : [raw]) {
      u.append(k, isMasked(v) ? values['query:' + k] ?? '' : typeof v === 'string' ? v : JSON.stringify(v))
    }
  }
  const qs = u.toString()
  const target = sameOriginUrl(t.path + (qs ? (t.path.includes('?') ? '&' : '?') + qs : ''), opts.origin)
  if (target === null) throw new CrossOriginReplay(t.path)
  const url = target.href

  const headers: Record<string, string> = {}
  for (const [k, v] of Object.entries(t.request.headers)) {
    if (!canSetHeader(k) || removed.has('header:' + k.toLowerCase())) continue
    headers[k] = isMasked(v) ? values['header:' + k.toLowerCase()] ?? '' : typeof v === 'string' ? v : JSON.stringify(v)
  }
  headers['X-Redline-Replay-Of'] = opts.replayOf
  if (opts.profile) headers['X-Redline-Profile'] = '1'

  let body: string | undefined
  if (sendsBody(t)) {
    const ct = Object.entries(t.request.headers).find(([k]) => k.toLowerCase() === 'content-type')?.[1]
    const raw = t.request.body!
    const mode = /json/i.test(String(ct ?? '')) || /^\s*[{[]/.test(raw) ? 'json' : /urlencoded/i.test(String(ct ?? '')) ? 'form' : 'raw'
    body = tokenizeBody(raw).map(p => {
      if (typeof p === 'string') return p
      const val = values['body:' + p.v] ?? ''
      return mode === 'json' ? JSON.stringify(val).slice(1, -1) : mode === 'form' ? encodeURIComponent(val) : val
    }).join('')
  }
  return { url, init: { method, headers, body, credentials: 'same-origin' } }
}
