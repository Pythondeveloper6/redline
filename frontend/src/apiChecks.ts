import { call, callText, qs } from './api'
import type { CheckDraftBody, CheckRunDetail, CheckRunItem, CheckStartBody, CheckSuite, CheckSuiteDoc, CheckSuiteItem } from './types'

const enc = encodeURIComponent

/** Refactor check routes (CONTRACT §19.5). Older backends answer 404/501 (see `isUnsupported`). */
export const apiChecks = {
  suites: () => call<{ items: CheckSuiteItem[] }>('GET', './api/checks/suites'),
  suite: (id: string) => call<CheckSuiteDoc>('GET', `./api/checks/suites/${enc(id)}`),
  createSuite: (name: string, suite: CheckSuite) => call<CheckSuiteDoc>('POST', './api/checks/suites', { name, suite }),
  updateSuite: (id: string, name: string, suite: CheckSuite) => call<CheckSuiteDoc>('PUT', `./api/checks/suites/${enc(id)}`, { name, suite }),
  deleteSuite: (id: string) => call<unknown>('DELETE', `./api/checks/suites/${enc(id)}`),
  exportSuite: (id: string) => callText(`./api/checks/suites/${enc(id)}/export`),
  draft: (body: CheckDraftBody) => call<CheckSuite>('POST', './api/checks/suites/draft', body),
  startRun: (body: CheckStartBody) => call<{ id: string }>('POST', './api/checks/runs', body),
  runs: (suiteId?: string) => call<{ items: CheckRunItem[] }>('GET', './api/checks/runs' + qs({ suite_id: suiteId })),
  run: (id: string) => call<CheckRunDetail>('GET', `./api/checks/runs/${enc(id)}`),
  stopRun: (id: string) => call<unknown>('POST', `./api/checks/runs/${enc(id)}/stop`, {}),
  deleteRun: (id: string) => call<unknown>('DELETE', `./api/checks/runs/${enc(id)}`),
  importBaseline: (baseline: unknown, suiteId?: string) => call<{ id: string }>('POST', './api/checks/baselines/import' + qs({ suite_id: suiteId }), baseline),
}
