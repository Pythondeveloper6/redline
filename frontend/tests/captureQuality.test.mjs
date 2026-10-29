import assert from 'node:assert/strict'
import { test } from 'node:test'
import { captureNotice } from '../src/lib/captureQuality.ts'

test('complete captures have no incomplete warning', () => {
  assert.equal(captureNotice(null), null)
  assert.equal(captureNotice(undefined), null)
})
test('byte-only capture limits do not invent span counts or complete analysis', () => {
  const text = captureNotice({ capture_limits: true, incomplete_evidence: true })
  assert.match(text, /size limits/)
  assert.doesNotMatch(text, /undefined|more than|still include/)
  assert.match(text, /missing findings do not prove/)
})
test('privacy and analysis failures have clear evidence warnings', () => {
  assert.match(captureNotice({ reason: 'privacy_failure' }), /Only limited metadata/)
  assert.match(captureNotice({ analysis_failed: true }), /analysis failed/)
  assert.match(captureNotice({ capture_errors: 2 }), /observation/)
})
test('span cap distinguishes retained detail from observed totals', () => {
  const text = captureNotice({ dropped_spans: 42, max_spans: 100 })
  assert.match(text, /42 spans were counted but not retained/)
  assert.match(text, /query totals may include/)
})
