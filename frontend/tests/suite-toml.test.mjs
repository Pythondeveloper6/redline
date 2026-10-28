import assert from 'node:assert/strict'
import { test } from 'node:test'
import { suiteToToml } from '../src/lib/suiteToml.ts'

test('development export preserves all nested policy and environment fields', () => {
  const text = suiteToToml({ name: 'rules', environment: { dataset: 'fixture' }, requests: [{ name: 'items', method: 'GET', path: '/items', array_keys: { items: 'id' }, tolerances: { 'items[*].price': { absolute: .01 } }, budgets: { max_queries: 5, forbidden_issues: ['n_plus_one'] } }] })
  assert.match(text, /environment = \{ "dataset" = "fixture" \}/)
  assert.match(text, /array_keys = \{ "items" = "id" \}/)
  assert.match(text, /"items\[\*\].price" = \{ "absolute" = 0.01 \}/)
  assert.match(text, /"forbidden_issues" = \["n_plus_one"\]/)
})
