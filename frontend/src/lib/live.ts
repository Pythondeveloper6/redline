import { useEffect, useRef, useState } from 'react'
import { isMocked } from '../api'
import type { TraceSummary } from '../types'

export type StreamState = 'off' | 'connecting' | 'live' | 'polling'

/**
 * Subscribes to `./api/stream?since=<epoch>` (SSE, `event: trace`, data = TraceSummary).
 * Reconnects with backoff (the server closes the stream every few minutes); after repeated
 * failures it reports `'polling'` and the caller falls back to interval polling.
 */
export function useLiveStream(enabled: boolean, getSince: () => number, onTrace: (t: TraceSummary) => void): StreamState {
  const [state, setState] = useState<StreamState>('off')
  const since = useRef(getSince); since.current = getSince
  const cb = useRef(onTrace); cb.current = onTrace

  useEffect(() => {
    if (!enabled) { setState('off'); return }
    setState('connecting')

    // Dev mock backend: no real stream, emit a synthetic request now and then.
    if (import.meta.env.DEV && isMocked()) {
      const t = setInterval(async () => {
        const m = await import('../mocks')
        cb.current(m.mockNewTrace())
      }, 3500)
      setState('live')
      return () => clearInterval(t)
    }
    if (typeof EventSource === 'undefined') { setState('polling'); return }

    let es: EventSource | null = null
    let timer: number | undefined
    let failures = 0
    let openedAt = 0
    let stopped = false
    // Insertion-order cursor: traces are saved when they finish, so a slow request started
    // before already-seen ones still arrives after them. Resumes reconnects past what was seen.
    let lastSeq: number | null = null
    const connect = () => {
      if (stopped) return
      const after = lastSeq === null ? '' : `&after=${lastSeq}`
      es = new EventSource(`./api/stream?since=${encodeURIComponent(String(since.current()))}${after}`)
      es.onopen = () => { openedAt = Date.now(); setState('live') }
      es.addEventListener('trace', ev => {
        try {
          const t = JSON.parse((ev as MessageEvent).data) as TraceSummary
          if (typeof t.seq === 'number') lastSeq = t.seq
          cb.current(t)
        } catch { /* malformed frame */ }
      })
      es.onerror = () => {
        es?.close(); es = null
        if (stopped) return
        // A connection that lived a while was healthy: its end (server-side time limit) is not a failure.
        if (openedAt && Date.now() - openedAt > 5000) failures = 0; else failures++
        openedAt = 0
        if (failures >= 3) { setState('polling'); return }
        setState('connecting')
        timer = window.setTimeout(connect, failures === 0 ? 300 : Math.min(8000, 800 * 2 ** failures))
      }
    }
    connect()
    return () => { stopped = true; es?.close(); window.clearTimeout(timer) }
  }, [enabled])

  return state
}
