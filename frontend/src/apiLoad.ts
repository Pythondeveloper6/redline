import { call, qs } from './api'
import type { Bottlenecks, CompareResp, HistoryResp, NewRunBody, ReleaseInfo, Run, RunItem } from './types'

const enc = encodeURIComponent

/** History, releases and load-test runs (CONTRACT §12). Older backends answer 404/501 for these. */
export const apiLoad = {
  history: (p: { route?: string; method?: string; window: string; bucket: string }) =>
    call<HistoryResp>('GET', './api/history' + qs(p)),
  releases: () => call<{ items: ReleaseInfo[] }>('GET', './api/releases'),
  compare: (a: string, b: string) => call<CompareResp>('GET', './api/history/compare' + qs({ a, b })),
  runs: () => call<{ items: RunItem[] }>('GET', './api/runs'),
  run: (id: string) => call<Run>('GET', `./api/runs/${enc(id)}`),
  bottlenecks: (id: string) => call<Bottlenecks>('GET', `./api/runs/${enc(id)}/bottlenecks`),
  startRun: (body: NewRunBody) => call<Partial<RunItem>>('POST', './api/runs', body),
  stopRun: (id: string) => call<unknown>('POST', `./api/runs/${enc(id)}/stop`, {}),
  deleteRun: (id: string) => call<unknown>('DELETE', `./api/runs/${enc(id)}`),
}

/** True when the backend predates these endpoints (or the store cannot serve them). */
export const isUnsupported = (e: unknown) => {
  const s = (e as { status?: number } | null)?.status
  return s === 404 || s === 405 || s === 501
}
