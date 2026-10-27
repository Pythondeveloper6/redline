// Unit tests for the pure request builders: `npm test` (node --test, no extra dependencies;
// Node strips the TypeScript types of the imported modules).
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildCurl, buildHttpie } from '../src/lib/curl.ts'
import { buildReplay, CrossOriginReplay } from '../src/lib/replay.ts'
import { safePath, sameOriginUrl } from '../src/lib/url.ts'

const ORIGIN = 'https://app.example'
const trace = (path, extra = {}) => ({
  id: 't1',
  method: 'GET',
  path,
  request: { headers: { Authorization: '[redacted]', 'X-Team': 'red' }, query: { q: '1' }, body: null },
  ...extra,
})
const replay = (path) =>
  buildReplay(trace(path), { 'header:authorization': 'secret' }, new Set(), {
    profile: false,
    replayOf: 't1',
    origin: ORIGIN,
  })

const HOSTILE = [
  '//evil.example/x',
  '/\\evil.example/x',
  '\\\\evil.example/x',
  '/\t/evil.example/x',
  '/\n/evil.example/x',
  'https://evil.example/x',
  'evil.example/x',
  '',
]

test('sameOriginUrl accepts plain paths only', () => {
  assert.equal(sameOriginUrl('/orders/1?x=2', ORIGIN)?.href, ORIGIN + '/orders/1?x=2')
  assert.equal(sameOriginUrl('/a/../../b', ORIGIN)?.href, ORIGIN + '/b')
  for (const p of HOSTILE) assert.equal(sameOriginUrl(p, ORIGIN), null, JSON.stringify(p))
})

test('replay of a same-origin path targets this origin', () => {
  const { url, init } = replay('/orders/1')
  assert.equal(url, ORIGIN + '/orders/1?q=1')
  assert.equal(init.headers.Authorization, 'secret')
  assert.equal(init.credentials, 'same-origin')
})

test('replay refuses paths that would leave the origin', () => {
  for (const p of HOSTILE) assert.throws(() => replay(p), CrossOriginReplay, JSON.stringify(p))
})

test('safePath keeps every path on the origin', () => {
  assert.equal(safePath('/x'), '/x')
  assert.equal(safePath('//evil.example/x'), '/evil.example/x')
  assert.equal(safePath('/\\evil.example'), '/evil.example')
  assert.equal(safePath('/\t/evil'), '/evil')
  assert.equal(safePath('x'), '/x')
})

test('cURL and HTTPie still build, against the origin', () => {
  const spec = { method: 'GET', path: '//evil.example/x', query: {}, headers: {}, body: null }
  assert.equal(buildCurl(spec, ORIGIN).command, 'curl https://app.example/evil.example/x')
  assert.equal(buildHttpie(spec, ORIGIN).command, 'http GET https://app.example/evil.example/x')
  const back = { ...spec, path: '/\\evil.example/x' }
  assert.equal(buildCurl(back, ORIGIN).command, 'curl https://app.example/evil.example/x')
  const ok = { ...spec, path: '/orders', query: { q: 'a b' } }
  assert.equal(buildCurl(ok, ORIGIN).command, "curl 'https://app.example/orders?q=a%20b'")
})
