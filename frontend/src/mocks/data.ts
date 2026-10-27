import type {
  Callsite, EndpointStats, ExplainResult, Issue, IssueGroup, IssueKind, Span, Speedscope, Suggestion, Trace, TraceSummary,
} from '../types'

// Deterministic PRNG so screenshots are stable.
let seed = 42
const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296)
const pick = <T,>(a: T[]) => a[Math.floor(rnd() * a.length)]
const hex = (n = 32) => Array.from({ length: n }, () => Math.floor(rnd() * 16).toString(16)).join('')
const NOW = Date.now() / 1000

const cs = (file: string, line: number, fn: string, code: string): Callsite => ({
  file, line, function: fn, code,
  stack: [
    { file: 'shop/urls.py', line: 12, function: '<module>' },
    { file: file, line: line - 6, function: fn },
    { file, line, function: fn },
  ],
})

let spanN = 0
const sp = (p: Partial<Span> & Pick<Span, 'kind' | 'name' | 'start_ms' | 'duration_ms'>): Span => ({
  id: 's' + (++spanN).toString(16).padStart(5, '0'), parent_id: null, callsite: null, data: {}, ...p,
})
const sql = (sqlText: string, start: number, dur: number, parent: string | null, c: Callsite | null, extra: Record<string, any> = {}): Span =>
  sp({
    kind: 'sql', name: sqlText.replace(/\s+/g, ' ').slice(0, 120), start_ms: start, duration_ms: dur, parent_id: parent, callsite: c,
    data: {
      sql: sqlText, params: [], fingerprint: 'fp_' + sqlText.replace(/\d+|'[^']*'/g, '?').replace(/\s+/g, ' ').slice(0, 60).replace(/\W+/g, '_'),
      db_alias: 'default', vendor: 'postgresql', rows: 1, many: false, explainable: /^\s*(select|with)/i.test(sqlText), ...extra,
    },
  })

function issueOf(i: Omit<Issue, 'id'>, spans: Span[]): Issue {
  void spans
  return { id: hex(16), ...i }
}

const NPLUS_DIFF = `--- a/shop/views.py
+++ b/shop/views.py
@@ -38,7 +38,7 @@ def order_list(request):
     user = request.user
-    orders = Order.objects.filter(customer=user).order_by("-created")[:24]
+    orders = Order.objects.filter(customer=user).prefetch_related("items").order_by("-created")[:24]
     rows = []
     for order in orders:
         rows.append({"id": order.pk, "items": [i.sku for i in order.items.all()]})
`
const sugN: Suggestion = {
  summary: "Add .prefetch_related('items') to the Order queryset",
  code: "orders = Order.objects.filter(customer=user).prefetch_related('items')",
  diff: NPLUS_DIFF, confidence: 'high', docs_url: 'https://docs.djangoproject.com/en/stable/ref/models/querysets/#prefetch-related',
}

// ---------- special traces ----------
function buildNPlusOne(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 212.4 })
  const view = sp({ kind: 'view', name: 'shop.views.order_list', start_ms: 3.1, duration_ms: 205, parent_id: root.id })
  const c1 = cs('shop/views.py', 38, 'order_list', 'orders = Order.objects.filter(customer=user).order_by("-created")[:24]')
  const c2 = cs('shop/views.py', 42, 'order_list', 'rows.append({"id": order.pk, "items": [i.sku for i in order.items.all()]})')
  const spans: Span[] = [root, view]
  spans.push(sql('SELECT "shop_order"."id", "shop_order"."customer_id", "shop_order"."total", "shop_order"."created" FROM "shop_order" WHERE "shop_order"."customer_id" = 7 ORDER BY "shop_order"."created" DESC LIMIT 24', 6, 3.2, view.id, c1, { rows: 24 }))
  const nplus: Span[] = []
  let t = 11
  for (let i = 0; i < 24; i++) {
    const d = 3.5 + rnd() * 4
    const s = sql(`SELECT "shop_orderitem"."id", "shop_orderitem"."order_id", "shop_orderitem"."sku", "shop_orderitem"."qty" FROM "shop_orderitem" WHERE "shop_orderitem"."order_id" = ${1000 + i}`, t, d, view.id, c2, { rows: 3 })
    nplus.push(s); spans.push(s); t += d + 0.7
  }
  spans.push(sp({ kind: 'cache', name: 'get user:7:prefs', start_ms: t + 1, duration_ms: 0.4, parent_id: view.id, data: { op: 'get', key: 'user:7:prefs', hit: true, backend: 'redis' } }))
  spans.push(sp({ kind: 'template', name: 'orders/list.html', start_ms: t + 2, duration_ms: 41.2, parent_id: view.id, data: { template: 'orders/list.html' } }))
  spans.push(sp({ kind: 'template', name: 'orders/_row.html', start_ms: t + 5, duration_ms: 28.4, parent_id: spans[spans.length - 1].id, data: { template: 'orders/_row.html' } }))
  const totalSql = spans.filter(s => s.kind === 'sql').reduce((a, s) => a + s.duration_ms, 0)
  const issue = issueOf({
    kind: 'n_plus_one', severity: 'critical', title: 'N+1: 24 queries on shop_orderitem from shop/views.py:42',
    detail: 'The same query ran 24 times with a different order_id each time, once per order in the loop. Fetching all items in one extra query is much cheaper.',
    span_ids: nplus.map(s => s.id), fingerprint: nplus[0].data.fingerprint, callsite: c2, count: 24,
    total_ms: nplus.reduce((a, s) => a + s.duration_ms, 0), suggestion: sugN,
  }, spans)
  return {
    id: 'a1f3c0de' + '0'.repeat(24), framework: 'django', method: 'GET', path: '/orders/', route: '/orders/', view: 'shop.views.order_list',
    status: 200, started_at: NOW - 9, duration_ms: 212.4, query_count: 25, query_ms: totalSql,
    request: {
      headers: { host: 'localhost:8000', 'user-agent': 'Mozilla/5.0 (Macintosh)', accept: 'text/html', cookie: '[redacted]', authorization: '[redacted]' },
      query: { page: '1' }, body: null, body_truncated: false, client: '127.0.0.1',
    },
    response: { headers: { 'content-type': 'text/html; charset=utf-8', 'set-cookie': '[redacted]', 'x-frame-options': 'DENY' }, body: '<!doctype html>\n<html><body>…</body></html>', body_truncated: true, size: 48213 },
    spans, issues: [issue], tags: { git_sha: '9f2c1ab', sampled_by: 'always' }, error: null, has_profile: true,
  }
}

function buildDuplicate(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 64 })
  const view = sp({ kind: 'view', name: 'shop.views.cart', start_ms: 2, duration_ms: 60, parent_id: root.id })
  const c = cs('shop/context.py', 18, 'cart_summary', 'profile = Profile.objects.get(user_id=request.user.id)')
  const dup = [0, 1, 2].map(i => sql('SELECT "shop_profile"."id", "shop_profile"."user_id", "shop_profile"."currency" FROM "shop_profile" WHERE "shop_profile"."user_id" = 7 LIMIT 21', 5 + i * 14, 1.4, view.id, c, { params: [7] }))
  const other = sql('SELECT "shop_cartline"."id", "shop_cartline"."product_id", "shop_cartline"."qty" FROM "shop_cartline" WHERE "shop_cartline"."cart_id" = 311', 9, 2.1, view.id, cs('shop/views.py', 77, 'cart', 'lines = list(cart.lines.all())'), { rows: 4 })
  const spans = [root, view, ...dup, other]
  return {
    id: 'b2e4d1ef' + '0'.repeat(24), framework: 'django', method: 'GET', path: '/cart/', route: '/cart/', view: 'shop.views.cart', status: 200,
    started_at: NOW - 20, duration_ms: 64, query_count: 4, query_ms: 6.3,
    request: { headers: { host: 'localhost:8000', cookie: '[redacted]' }, query: {}, body: null, body_truncated: false, client: '127.0.0.1' },
    response: { headers: { 'content-type': 'text/html' }, body: '<html>cart</html>', body_truncated: false, size: 9120 },
    spans,
    issues: [issueOf({
      kind: 'duplicate_query', severity: 'warning', title: 'Duplicate: identical query ran 3 times from shop/context.py:18',
      detail: 'shop_profile was fetched by user_id = 7 three times in one request. Cache the result on the request or pass the object along.',
      span_ids: dup.map(d => d.id), fingerprint: dup[0].data.fingerprint, callsite: c, count: 3, total_ms: 4.2,
      suggestion: { summary: 'Fetch the profile once and reuse it (e.g. cached_property on a request helper)', code: 'profile = request.profile  # memoised', diff: null, confidence: 'medium', docs_url: null },
    }, spans)],
    tags: {}, error: null, has_profile: false,
  }
}

function buildSlow(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 1384 })
  const view = sp({ kind: 'view', name: 'shop.views.search', start_ms: 4, duration_ms: 1376, parent_id: root.id })
  const c = cs('shop/search.py', 54, 'search_products', 'qs = Product.objects.filter(description__icontains=term).order_by("-rating")')
  const slow = sql(`SELECT "shop_product"."id", "shop_product"."name", "shop_product"."rating" FROM "shop_product" WHERE UPPER("shop_product"."description"::text) LIKE UPPER('%wireless%') ORDER BY "shop_product"."rating" DESC LIMIT 50`, 8, 1240.5, view.id, c, { rows: 50 })
  const http = sp({ kind: 'http_out', name: 'GET api.recs.internal/v1/similar', start_ms: 1255, duration_ms: 96.2, parent_id: view.id, data: { method: 'GET', url: 'https://api.recs.internal/v1/similar?sku=W-100&token=%5Bredacted%5D', status: 200 } })
  const spans = [root, view, slow, http]
  return {
    id: 'c3d5e2f0' + '0'.repeat(24), framework: 'django', method: 'GET', path: '/search/', route: '/search/', view: 'shop.views.search', status: 200,
    started_at: NOW - 33, duration_ms: 1384, query_count: 1, query_ms: 1240.5,
    request: { headers: { host: 'localhost:8000' }, query: { q: 'wireless' }, body: null, body_truncated: false, client: '127.0.0.1' },
    response: { headers: { 'content-type': 'application/json' }, body: '{"results":[{"id":1,"name":"Wireless Mouse"},{"id":2,"name":"Wireless Keyboard"}],"count":2}', body_truncated: false, size: 88 },
    spans,
    issues: [issueOf({
      kind: 'slow_query', severity: 'critical', title: 'Slow query: 1240 ms on shop_product from shop/search.py:54',
      detail: 'A case-insensitive substring search scans the whole shop_product table. A trigram index lets Postgres use an index for LIKE patterns.',
      span_ids: [slow.id], fingerprint: slow.data.fingerprint, callsite: c, count: 1, total_ms: 1240.5,
      suggestion: { summary: 'Add a pg_trgm GIN index on shop_product.description', code: 'CREATE INDEX CONCURRENTLY ON shop_product USING gin (description gin_trgm_ops);', diff: null, confidence: 'medium', docs_url: 'https://www.postgresql.org/docs/current/pgtrgm.html' },
    }, spans)],
    tags: { run_id: 'r_8c2a' }, error: null, has_profile: false,
  }
}

function buildError(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 18.2 })
  const view = sp({ kind: 'view', name: 'shop.views.checkout', start_ms: 2, duration_ms: 15, parent_id: root.id })
  const c = cs('shop/views.py', 112, 'checkout', 'order = Order.objects.select_for_update().get(pk=pk)')
  const q = sql('SELECT "shop_order"."id", "shop_order"."total" FROM "shop_order" WHERE "shop_order"."id" = 99999 FOR UPDATE', 4, 2.0, view.id, c, { rows: 0, vendor: 'postgresql', explainable: false })
  return {
    id: 'd4e6f3a1' + '0'.repeat(24), framework: 'django', method: 'POST', path: '/checkout/99999/', route: '/checkout/<int:pk>/', view: 'shop.views.checkout', status: 500,
    started_at: NOW - 47, duration_ms: 18.2, query_count: 1, query_ms: 2.0,
    request: {
      headers: { 'content-type': 'application/json', authorization: '[redacted]', cookie: '[redacted]' }, query: {},
      body: '{"coupon":"SPRING","card_number":"[redacted]","password":"[redacted]","address":{"city":"Berlin"}}', body_truncated: false, client: '10.0.0.4',
    },
    response: { headers: { 'content-type': 'text/html' }, body: '<h1>Server Error (500)</h1>', body_truncated: false, size: 27 },
    spans: [root, view, q], issues: [], tags: {}, has_profile: false,
    logs: [
      { t_ms: 1.2, level: 'INFO', logger: 'shop.views', message: 'checkout started for order 99999', callsite: { file: 'shop/views.py', line: 108, function: 'checkout', code: 'logger.info("checkout started for order %s", pk)', stack: [] } },
      { t_ms: 3.0, level: 'DEBUG', logger: 'django.db.backends', message: '(0.002) SELECT "shop_order"."id" FROM "shop_order" WHERE "shop_order"."id" = 99999 FOR UPDATE; args=(99999,)', callsite: null },
      { t_ms: 9.4, level: 'WARNING', logger: 'shop.payments', message: 'coupon SPRING expires in 2 days', callsite: { file: 'shop/payments.py', line: 41, function: 'apply_coupon', code: null, stack: [] } },
      { t_ms: 16.8, level: 'ERROR', logger: 'django.request', message: 'Internal Server Error: /checkout/99999/', callsite: null },
    ],
    logs_dropped: 3,
    error: { frames: [
      { file: 'shop/views.py', line: 98, function: 'checkout', code: 'cart = load_cart(request)', locals: { request: '<WSGIRequest: POST \'/checkout/99999/\'>', pk: '99999', coupon: "'SPRING'" } },
      { file: 'shop/views.py', line: 112, function: 'checkout', code: 'order = Order.objects.select_for_update().get(pk=pk)', locals: { pk: '99999', card_number: '[redacted]', password: '[redacted]', payload: "{'coupon': 'SPRING', 'address': {'city': 'Berlin'}}", user: '<User: dev@example.com>' } },
    ], type: 'shop.models.Order.DoesNotExist', message: 'Order matching query does not exist.', traceback: 'Traceback (most recent call last):\n  File "django/core/handlers/exception.py", line 55, in inner\n    response = get_response(request)\n  File "shop/views.py", line 112, in checkout\n    order = Order.objects.select_for_update().get(pk=pk)\n  File "django/db/models/query.py", line 637, in get\n    raise self.model.DoesNotExist(\nshop.models.Order.DoesNotExist: Order matching query does not exist.' },
  }
}

function buildSqlite(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 342 })
  const view = sp({ kind: 'view', name: 'app.routes.list_events', start_ms: 1, duration_ms: 340, parent_id: root.id })
  const c = cs('app/routes.py', 31, 'list_events', 'rows = session.execute(select(Event).where(Event.kind == kind).order_by(Event.ts.desc()))')
  const q = sql('SELECT events.id, events.kind, events.ts, events.payload FROM events WHERE events.kind = ? ORDER BY events.ts DESC', 3, 310.2, view.id, c, { vendor: 'sqlite', params: ['signup'], rows: 8120 })
  const spans = [root, view, q]
  return {
    id: 'e5f7a4b2' + '0'.repeat(24), framework: 'fastapi', method: 'GET', path: '/events', route: '/events', view: 'app.routes.list_events', status: 200,
    started_at: NOW - 61, duration_ms: 342, query_count: 1, query_ms: 310.2,
    request: { headers: { host: 'localhost:8001', 'x-api-key': '[redacted]' }, query: { kind: 'signup' }, body: null, body_truncated: false, client: '127.0.0.1' },
    response: { headers: { 'content-type': 'application/json' }, body: '[{"id":1,"kind":"signup"}]', body_truncated: true, size: 912004 },
    spans,
    issues: [issueOf({
      kind: 'slow_query', severity: 'warning', title: 'Slow query: 310 ms on events from app/routes.py:31',
      detail: 'Filtering events by kind and sorting by ts scans and sorts every row. A composite index serves both.',
      span_ids: [q.id], fingerprint: q.data.fingerprint, callsite: c, count: 1, total_ms: 310.2,
      suggestion: { summary: 'Add an index on events(kind, ts)', code: 'CREATE INDEX ix_events_kind_ts ON events (kind, ts DESC);', diff: null, confidence: 'high', docs_url: null },
    }, spans)],
    tags: {}, error: null, has_profile: false,
  }
}

function buildAsync(): Trace {
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: 486 })
  const view = sp({ kind: 'view', name: 'app.graphql.orders', start_ms: 2, duration_ms: 480, parent_id: root.id })
  const c = cs('app/resolvers.py', 54, 'resolve_customer', 'return await db.fetch_one(Customer, id=order.customer_id)')
  const spans: Span[] = [root, view]
  const tx = sp({ kind: 'transaction', name: 'transaction default', start_ms: 10, duration_ms: 310, parent_id: view.id, data: { db_alias: 'default', outcome: 'commit', queries: 4, http_out: 1 } })
  spans.push(tx)
  spans.push(sql('INSERT INTO audit_log (actor, action) VALUES (?, ?)', 12, 2.2, tx.id, c, { in_transaction: true, rows: 1, explainable: false }))
  const call = sp({ kind: 'http_out', name: 'POST payments.example.com/v1/charge', start_ms: 20, duration_ms: 280, parent_id: tx.id, callsite: cs('app/billing.py', 33, 'charge', 'httpx.post(PAY_URL, json=body)'), data: { method: 'POST', url: 'https://payments.example.com/v1/charge', status: 200, host: 'payments.example.com' } })
  spans.push(call)
  const ser = sp({ kind: 'serializer', name: 'OrderSerializer', start_ms: 325, duration_ms: 96, parent_id: view.id, data: { library: 'drf', serializer: 'OrderSerializer', items: 240, many: true } })
  spans.push(ser)
  const big = sql('SELECT orders.*, orders.notes, orders.raw_payload FROM orders', 330, 88, ser.id, cs('app/views.py', 71, 'list_orders', 'Order.objects.all()'), { rows: 24000, has_limit: false, wide_columns: ['notes', 'raw_payload'] })
  spans.push(big)
  const res = [0, 1, 2].map(i => sp({ kind: 'resolver', name: `Order.customer`, start_ms: 425 + i * 18, duration_ms: 14, parent_id: view.id, data: { path: 'orders.*.customer', field: 'customer', parent_type: 'Order' } }))
  spans.push(...res)
  const blk = sp({ kind: 'blocking', name: 'time.sleep', start_ms: 90, duration_ms: 120, parent_id: view.id, callsite: cs('app/legacy.py', 12, 'warm_cache', 'time.sleep(0.12)'), data: { reason: 'sync_call', call: 'time.sleep', lag_ms: 118 } })
  const lag = sp({ kind: 'blocking', name: 'event loop lag', start_ms: 440, duration_ms: 35, parent_id: view.id, data: { reason: 'loop_lag', call: null, lag_ms: 34.6 } })
  const pw = sp({ kind: 'pool_wait', name: 'wait for connection', start_ms: 4, duration_ms: 24, parent_id: view.id, data: { db_alias: 'default', pool_size: 10, checked_out: 10 } })
  spans.push(blk, lag, pw)
  const mk = (kind: IssueKind, severity: Issue['severity'], title: string, detail: string, ids: string[], count: number, total: number, extra: Record<string, any>, callsite: Callsite | null = c): Issue =>
    issueOf({ kind, severity, title, detail, span_ids: ids, fingerprint: null, callsite, count, total_ms: total, suggestion: null, extra }, spans)
  const issues: Issue[] = [
    mk('external_in_transaction', 'critical', 'HTTP call to payments.example.com while a transaction is open', 'The transaction held its connection and row locks for 310 ms while waiting for an external service. Call the API before opening the transaction.', [tx.id, call.id], 1, 280, { host: 'payments.example.com', queries: 4 }, call.callsite),
    mk('long_transaction', 'warning', 'Transaction held open for 310 ms', 'Long transactions hold locks and a pooled connection.', [tx.id], 1, 310, { queries: 4, http_out: 1 }),
    mk('large_result', 'warning', 'Query returned 24,000 rows', 'Fetching this many rows into memory is slow and memory hungry. Paginate or select fewer columns.', [big.id], 1, 88, { rows: 24000, wide_columns: ['notes', 'raw_payload'] }, big.callsite),
    mk('unbounded_query', 'info', 'SELECT without LIMIT on orders', 'No LIMIT: the response grows with the table.', [big.id], 1, 88, { rows: 24000 }, big.callsite),
    mk('slow_serializer', 'warning', 'OrderSerializer took 96 ms for 240 items', 'Serialising many objects dominates the request. Check for per-item queries and SerializerMethodFields.', [ser.id], 1, 96, { library: 'drf', serializer: 'OrderSerializer', items: 240 }, null),
    mk('n_plus_one', 'critical', 'N+1 in GraphQL resolver orders.*.customer', 'The resolver for Order.customer runs once per order.', res.map(r => r.id), 3, 42, { resolver_path: 'orders.*.customer' }),
    mk('blocking_in_async', 'critical', 'time.sleep blocked the event loop for 118 ms', 'A synchronous call inside an async view stalls every other request on this worker. Use asyncio.sleep or run it in a thread.', [blk.id, lag.id], 2, 153, { call: 'time.sleep', lag_ms: 118 }, blk.callsite),
    mk('inefficient_count', 'info', 'len(queryset) loads every row to count them', 'Use queryset.count() so the database counts.', [], 1, 4, { pattern: 'len', expr: 'len(orders)', old: 'len(orders)', new: 'orders.count()', fix_line: 71 }),
    mk('slow_external', 'warning', 'POST payments.example.com took 280 ms', 'The external call is the slowest part of the request.', [call.id], 1, 280, { host: 'payments.example.com' }, call.callsite),
    mk('duplicate_external', 'warning', 'GET rates.example.com called 3 times', 'Cache the response or call it once.', [], 3, 140, { host: 'rates.example.com' }, null),
    mk('retry_storm', 'critical', 'POST payments.example.com retried 4 times', 'Retries without backoff amplify an outage.', [], 4, 900, { host: 'payments.example.com', attempts: 4 }, null),
  ]
  const queries = spans.filter(x => x.kind === 'sql')
  return {
    id: 'f6a8b5c3' + '0'.repeat(24), framework: 'fastapi', method: 'POST', path: '/graphql', route: '/graphql', view: 'app.graphql.orders', status: 200,
    started_at: NOW - 33, duration_ms: 486, query_count: queries.length, query_ms: queries.reduce((a, x) => a + x.duration_ms, 0),
    request: { headers: { host: 'localhost:8001', 'content-type': 'application/json' }, query: {}, body: '{"query":"{ orders { id customer { name } } }"}', body_truncated: false, client: '127.0.0.1' },
    response: { headers: { 'content-type': 'application/json' }, body: '{"data":{"orders":[]}}', body_truncated: false, size: 1480220 },
    spans, issues, tags: {}, error: null, has_profile: false,
    logs: [
      { t_ms: 3, level: 'INFO', logger: 'app.graphql', message: 'executing query orders', callsite: null },
      { t_ms: 95, level: 'WARNING', logger: 'asyncio', message: 'Executing <Task pending name=\'Task-12\'> took 0.118 seconds', callsite: null },
    ], logs_dropped: 0,
  }
}

export const SPECIAL: Trace[] = [buildNPlusOne(), buildDuplicate(), buildSlow(), buildError(), buildSqlite(), buildAsync()]

// ---------- generic traces ----------
const ROUTES: [string, string, string, string][] = [
  ['GET', '/products/', '/products/', 'shop.views.product_list'],
  ['GET', '/products/<int:pk>/', '/products/%d/', 'shop.views.product_detail'],
  ['POST', '/cart/add/', '/cart/add/', 'shop.views.cart_add'],
  ['GET', '/api/orders/<int:pk>/', '/api/orders/%d/', 'shop.api.order_detail'],
  ['GET', '/healthz', '/healthz', 'shop.views.health'],
  ['GET', '/account/', '/account/', 'shop.views.account'],
  ['PUT', '/api/profile/', '/api/profile/', 'shop.api.profile_update'],
]
const generic: Trace[] = []
function genericTrace(i: number): Trace {
  const [method, route, pathFmt, view] = pick(ROUTES)
  const path = pathFmt.includes('%d') ? pathFmt.replace('%d', String(1 + Math.floor(rnd() * 900))) : pathFmt
  const dur = 8 + Math.pow(rnd(), 3) * 420
  const nq = route === '/healthz' ? 0 : 1 + Math.floor(rnd() * 9)
  const root = sp({ kind: 'middleware', name: 'RedlineMiddleware', start_ms: 0, duration_ms: dur })
  const v = sp({ kind: 'view', name: view, start_ms: 1, duration_ms: dur - 2, parent_id: root.id })
  const spans: Span[] = [root, v]
  let t = 2, qms = 0
  for (let k = 0; k < nq; k++) {
    const d = 0.5 + rnd() * (dur / (nq + 2))
    spans.push(sql(`SELECT * FROM shop_item_${k % 3} WHERE id = ${Math.floor(rnd() * 999)}`, t, d, v.id, cs('shop/views.py', 20 + k, view.split('.').pop()!, 'obj = Item.objects.get(pk=pk)')))
    t += d + 1; qms += d
  }
  const status = rnd() < 0.05 ? 500 : rnd() < 0.08 ? 404 : rnd() < 0.05 ? 302 : method === 'POST' ? 201 : 200
  return {
    id: hex(), framework: 'django', method, path, route, view, status, started_at: NOW - 60 - i * 7.3 - rnd() * 5,
    duration_ms: dur, query_count: nq, query_ms: qms,
    request: { headers: { host: 'localhost:8000' }, query: {}, body: null, body_truncated: false, client: '127.0.0.1' },
    response: { headers: { 'content-type': 'application/json' }, body: '{"ok":true}', body_truncated: false, size: 11 },
    spans, issues: [], tags: {}, error: null, has_profile: false,
  }
}
for (let i = 0; i < 110; i++) generic.push(genericTrace(i))
/** One more generic trace stamped with the current time (dev Live mode). */
export function genericTraceAt(): Trace {
  const t = genericTrace(0)
  t.started_at = Date.now() / 1000
  t.id = hex()
  return t
}

export const ALL: Trace[] = [...SPECIAL, ...generic].sort((a, b) => b.started_at - a.started_at)

export function summarize(t: Trace): TraceSummary {
  return {
    id: t.id, method: t.method, path: t.path, route: t.route, view: t.view, status: t.status, started_at: t.started_at,
    duration_ms: t.duration_ms, query_count: t.query_count, query_ms: t.query_ms, issue_count: t.issues.length,
    issue_kinds: [...new Set(t.issues.map(i => i.kind))] as IssueKind[], has_profile: t.has_profile, tags: t.tags,
  }
}

// ---------- endpoints / issues ----------
export function buildEndpoints(): EndpointStats[] {
  const map = new Map<string, Trace[]>()
  for (const t of ALL) {
    const k = t.method + ' ' + t.route
    map.set(k, [...(map.get(k) ?? []), t])
  }
  const out: EndpointStats[] = []
  for (const [, ts] of map) {
    const d = ts.map(t => t.duration_ms).sort((a, b) => a - b)
    const q = (p: number) => d[Math.min(d.length - 1, Math.floor(p * d.length))]
    const series = Array.from({ length: 48 }, (_, i) => ({ t: NOW - (48 - i) * 1800, p95_ms: q(0.95) * (0.85 + rnd() * 0.3), count: Math.floor(1 + rnd() * 30) }))
    out.push({
      route: ts[0].route!, method: ts[0].method, count: ts.length * 37, error_rate: ts.filter(t => t.status >= 500).length / ts.length,
      p50_ms: q(0.5), p95_ms: q(0.95), p99_ms: q(0.99), avg_queries: ts.reduce((a, t) => a + t.query_count, 0) / ts.length,
      max_queries: Math.max(...ts.map(t => t.query_count)), avg_query_ms: ts.reduce((a, t) => a + t.query_ms, 0) / ts.length,
      issue_count: ts.reduce((a, t) => a + t.issues.length, 0), series,
    })
  }
  return out.sort((a, b) => b.p95_ms - a.p95_ms)
}

export function buildIssueGroups(): IssueGroup[] {
  const groups: IssueGroup[] = []
  for (const t of SPECIAL) for (const i of t.issues) {
    groups.push({
      id: i.id, kind: i.kind, severity: i.severity, title: i.title, detail: i.detail,
      occurrences: i.kind === 'n_plus_one' ? 212 : i.kind === 'slow_query' ? 17 : 41,
      first_seen: NOW - 3600 * (2 + Math.floor(rnd() * 20)), last_seen: t.started_at, routes: [`${t.method} ${t.route}`],
      sample_trace_id: t.id, sample_span_ids: i.span_ids, suggestion: i.suggestion, callsite: i.callsite,
    })
  }
  groups.push({
    id: hex(16), kind: 'too_many_queries', severity: 'info', title: 'Too many queries: 58 queries in GET /reports/',
    detail: 'This request ran 58 queries, above the budget of 50.', occurrences: 9, first_seen: NOW - 86400, last_seen: NOW - 600,
    routes: ['GET /reports/'], sample_trace_id: SPECIAL[0].id, sample_span_ids: [], suggestion: null, callsite: null,
  })
  return groups
}

// ---------- explain ----------
export const EXPLAIN_PG: ExplainResult = {
  vendor: 'postgresql', analyzed: false,
  plan: [{ Plan: { 'Node Type': 'Limit' } }],
  text: `Limit  (cost=48211.20..48211.33 rows=50 width=44)
  ->  Sort  (cost=48211.20..48711.20 rows=200000 width=44)
        Sort Key: rating DESC
        ->  Seq Scan on shop_product  (cost=0.00..41822.00 rows=200000 width=44)
              Filter: (upper((description)::text) ~~ '%WIRELESS%'::text)`,
  tree: {
    label: 'Limit', detail: '', cost: 48211.33, rows: 50, actual_ms: null, flags: [], children: [{
      label: 'Sort', detail: 'Sort Key: rating DESC', cost: 48711.2, rows: 200000, actual_ms: null, flags: ['sort'], children: [{
        label: 'Seq Scan on shop_product', detail: "Filter: (upper((description)::text) ~~ '%WIRELESS%'::text)",
        cost: 41822, rows: 200000, actual_ms: null, flags: ['seq_scan', 'large_table'], children: [],
      }],
    }],
  },
  findings: [
    { severity: 'critical', title: 'Sequential scan over ~200,000 rows', detail: 'shop_product is read in full to evaluate the LIKE filter on description.' },
    { severity: 'warning', title: 'Sort on rating needs all matching rows', detail: 'The sort runs before the LIMIT, so every match is ordered. An index on (rating) may allow an ordered scan.' },
  ],
  suggested_indexes: [
    'CREATE EXTENSION IF NOT EXISTS pg_trgm;\nCREATE INDEX CONCURRENTLY shop_product_description_trgm ON shop_product USING gin (upper(description) gin_trgm_ops);',
    'CREATE INDEX CONCURRENTLY shop_product_rating_idx ON shop_product (rating DESC);',
  ],
}
export const EXPLAIN_SQLITE: ExplainResult = {
  vendor: 'sqlite', analyzed: false,
  plan: [[3, 0, 0, 'SCAN events'], [12, 0, 0, 'USE TEMP B-TREE FOR ORDER BY']],
  text: 'SCAN events\nUSE TEMP B-TREE FOR ORDER BY',
  tree: {
    label: 'Query plan', detail: '', cost: null, rows: null, actual_ms: null, flags: [], children: [
      { label: 'SCAN events', detail: '', cost: null, rows: null, actual_ms: null, flags: ['seq_scan'], children: [] },
      { label: 'USE TEMP B-TREE FOR ORDER BY', detail: '', cost: null, rows: null, actual_ms: null, flags: ['sort'], children: [] },
    ],
  },
  findings: [
    { severity: 'critical', title: 'Full table scan on events', detail: 'No index covers the WHERE kind = ? filter.' },
    { severity: 'warning', title: 'Temporary B-tree used for ORDER BY', detail: 'Sorting happens in a temp structure because no index provides ts order.' },
  ],
  suggested_indexes: ['CREATE INDEX ix_events_kind_ts ON events (kind, ts DESC);'],
}

// ---------- speedscope profile ----------
interface PN { n: string; file?: string; line?: number; self: number; c?: PN[] }
const TREE: PN = {
  n: 'RedlineMiddleware.__call__', file: 'django_redline/middleware.py', line: 61, self: 1.2, c: [
    { n: 'get_response', file: 'django/core/handlers/base.py', line: 140, self: 0.8, c: [
      { n: 'order_list', file: 'shop/views.py', line: 30, self: 2.5, c: [
        { n: 'QuerySet.__iter__', file: 'django/db/models/query.py', line: 384, self: 0.4, c: [
          { n: 'SQLCompiler.execute_sql', file: 'django/db/models/sql/compiler.py', line: 1555, self: 1.1, c: [
            { n: 'CursorWrapper.execute', file: 'django/db/backends/utils.py', line: 79, self: 3.0, c: [
              { n: 'psycopg.Cursor.execute', file: 'psycopg/cursor.py', line: 723, self: 3.2 }] }] }] },
        { n: 'RelatedManager.all', file: 'django/db/models/fields/related_descriptors.py', line: 703, self: 6.0, c: [
          { n: 'QuerySet._fetch_all', file: 'django/db/models/query.py', line: 1942, self: 2.2, c: [
            { n: 'SQLCompiler.execute_sql', file: 'django/db/models/sql/compiler.py', line: 1555, self: 4.5, c: [
              { n: 'CursorWrapper.execute', file: 'django/db/backends/utils.py', line: 79, self: 16.0, c: [
                { n: 'psycopg.Cursor.execute', file: 'psycopg/cursor.py', line: 723, self: 58.0 }] }] },
            { n: 'ModelIterable.__iter__', file: 'django/db/models/query.py', line: 90, self: 9.0 }] }] },
        { n: 'render', file: 'django/template/response.py', line: 105, self: 2.0, c: [
          { n: 'Template.render', file: 'django/template/base.py', line: 170, self: 5.0, c: [
            { n: 'NodeList.render', file: 'django/template/base.py', line: 1005, self: 14.0, c: [
              { n: 'ForNode.render', file: 'django/template/defaulttags.py', line: 189, self: 12.0 },
              { n: 'VariableNode.render', file: 'django/template/base.py', line: 1085, self: 8.0 }] }] }] }] },
      { n: 'SessionMiddleware.__call__', file: 'django/contrib/sessions/middleware.py', line: 17, self: 3.0 }] }],
}
export function buildProfile(): Speedscope {
  const frames: { name: string; file?: string; line?: number }[] = []
  const idx = new Map<string, number>()
  const events: { type: 'O' | 'C'; at: number; frame: number }[] = []
  let t = 0
  const walk = (n: PN) => {
    const key = n.n + '@' + n.file + n.line
    if (!idx.has(key)) { idx.set(key, frames.length); frames.push({ name: n.n, file: n.file, line: n.line }) }
    const f = idx.get(key)!
    events.push({ type: 'O', at: t, frame: f })
    const half = n.self / 2
    t += half
    for (const c of n.c ?? []) walk(c)
    t += n.self - half
    events.push({ type: 'C', at: t, frame: f })
  }
  walk(TREE)
  return { name: 'GET /orders/', shared: { frames }, profiles: [{ type: 'evented', name: 'MainThread', unit: 'milliseconds', startValue: 0, endValue: t, events }] }
}
