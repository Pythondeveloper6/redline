import type { CheckRequestDef } from '../types.ts'
import { parseIgnorePath } from './check.ts'

export function policyErrors(r: Pick<CheckRequestDef, 'array_keys' | 'tolerances' | 'budgets'>): Record<string, string> {
  const errors: Record<string, string> = {}
  const finite = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0
  for (const [path, key] of Object.entries(r.array_keys ?? {})) {
    if (!parseIgnorePath(path).ok || typeof key !== 'string' || !key.trim()) errors.array_keys = 'Use valid JSON paths and non-empty identity field names.'
  }
  for (const [path, rule] of Object.entries(r.tolerances ?? {})) {
    if (!parseIgnorePath(path).ok || !rule || !Object.keys(rule).length || Object.entries(rule).some(([key, value]) => !['absolute', 'relative'].includes(key) || !finite(value))) errors.tolerances = 'Use valid paths and finite non-negative absolute or relative tolerances.'
  }
  for (const [key, value] of Object.entries(r.budgets ?? {})) {
    if (key === 'forbidden_issues') {
      if (!Array.isArray(value) || value.some(v => typeof v !== 'string' || !v.trim())) errors.budgets = 'Forbidden issues must be a list of issue kinds.'
    } else if (['max_queries', 'max_external_calls'].includes(key) && !Number.isInteger(value)) errors.budgets = 'Query and external-call budgets must be whole numbers.'
    else if (!['max_queries', 'max_median_ms', 'max_p95_ms', 'max_external_calls'].includes(key) || !finite(value)) errors.budgets = 'Budgets must be finite non-negative numbers.'
  }
  return errors
}
