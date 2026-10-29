import type { Trace } from '../types'

export function captureNotice(quality: Trace['truncated']): string | null {
  if (!quality) return null
  const parts = ['Recording evidence is incomplete; missing findings do not prove this request is healthy.']
  if (['privacy_failure', 'capture_failure', 'invalid_capture'].includes(quality.reason ?? '')) parts.push('Capture failed safely. Only limited metadata was retained.')
  if (quality.reason === 'trace_budget' || quality.capture_limits) parts.push('Capture size limits omitted or shortened some evidence.')
  if (quality.dropped_spans) parts.push(`${quality.dropped_spans} spans were counted but not retained.`)
  if (quality.capture_errors || quality.analysis_failed) parts.push('Some observation or analysis failed.')
  parts.push('Recorded query totals may include operations whose details are unavailable.')
  return parts.join(' ')
}
