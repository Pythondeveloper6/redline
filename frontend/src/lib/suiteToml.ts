import type { CheckSuite } from '../types.ts'

function value(input: unknown): string {
  if (Array.isArray(input)) return `[${input.map(value).join(', ')}]`
  if (typeof input === 'object' && input !== null) return `{ ${Object.entries(input).filter(([, v]) => v != null).map(([key, v]) => `${JSON.stringify(key)} = ${value(v)}`).join(', ')} }`
  if (typeof input === 'number' && !Number.isFinite(input)) throw new Error('TOML needs finite numbers')
  return JSON.stringify(input)
}

/** Development export uses all suite fields, including nested response rules. */
export function suiteToToml(suite: CheckSuite): string {
  const lines: string[] = []
  for (const [key, v] of Object.entries(suite)) if (!['requests', 'thresholds'].includes(key) && v != null) lines.push(`${key} = ${value(v)}`)
  if (suite.thresholds) {
    lines.push('', '[thresholds]')
    for (const [key, v] of Object.entries(suite.thresholds)) if (v != null) lines.push(`${key} = ${value(v)}`)
  }
  for (const request of suite.requests) {
    lines.push('', '[[requests]]')
    for (const [key, v] of Object.entries(request)) if (v != null) lines.push(`${key} = ${value(v)}`)
  }
  return lines.join('\n') + '\n'
}
