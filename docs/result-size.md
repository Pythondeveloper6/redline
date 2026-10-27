# Large and unbounded results

[← Index](index.md)

A query can be perfectly indexed and still be the slowest thing in the request, because of
how much it returns. Fetching, building model objects and serializing 40,000 rows costs
far more than finding them. Three detectors cover the ways this goes wrong. They all read
two facts the adapters now record on every `sql` span: how many **rows** came back, and
whether the statement has a **`LIMIT`**.

| Kind | Fires when | Default |
|---|---|---|
| `large_result` | a SELECT returned at least this many rows | `large_result_rows = 1000` |
| `unbounded_query` | a SELECT with no `LIMIT` returned at least this many rows | `unbounded_rows = 200` |
| `inefficient_count` | a count or existence question answered by loading rows | a source-line pattern |

Severity is `warning`, and `critical` once the row count is five times the threshold.

## Large result

```text
Large result: 48,210 rows from shop_order   (shop/reports.py:31)
```

The detail says what to do: paginate, stream in chunks, or select fewer columns. Where
the adapter's fix suggester can tell which columns are wide (text, blob and JSON fields),
the issue lists them (`extra["wide_columns"]`), because dropping those is often the
cheapest fix:

```python
# Before: every column, including a 40 KB `notes` field, for 48,000 orders
orders = Order.objects.filter(status="open")

# After: only what the report needs, and a chunked iterator
orders = Order.objects.filter(status="open").only("id", "total").iterator(chunk_size=2000)
```

## Unbounded query

```text
Unbounded query: 1,350 rows, no LIMIT from shop_product   (shop/views.py:12)
```

The query has no `LIMIT`, `FETCH FIRST`, `TOP` or `ROWNUM`, and returned a lot. It is fast
today and keeps getting slower as the table grows, which is exactly why it is worth
flagging at 200 rows and not waiting for 200,000. `has_limit` is computed from the SQL
text; a limit inside a subquery does not count as the top-level limit.

Add pagination (`[:50]`, `.limit(50)`, keyset pagination) or, if you really do need all of
it, iterate in chunks.

## Count and exists

`inefficient_count` looks at the **source line** that issued the query (the callsite) and
recognises four idioms:

| Source line | What went wrong | Suggested |
|---|---|---|
| `len(orders)` | loaded every row to count them | `orders.count()` |
| `bool(orders)` | loaded every row to see if there are any | `orders.exists()` |
| `if orders:` | same, through truthiness | `orders.exists()` |
| `orders.count() > 0` (also `!= 0`, `>= 1`, `== 0`) | counted every match to compare with zero | `orders.exists()` |

```python
# Before
pending = Order.objects.filter(status="pending")
if pending:                      # SELECT every column of every pending order
    notify(len(pending))

# After
pending = Order.objects.filter(status="pending")
if pending.exists():             # SELECT 1 ... LIMIT 1
    notify(pending.count())      # SELECT COUNT(*)
```

When the source line can be located, the issue carries a one-line fix (`fix_line`) shown
as a diff. Severity is `info` for small tables and `warning` when the query returned at
least `unbounded_rows` rows or took at least `slow_query_ms`. A query that is already
bounded (`first()`, `get()`, a slice) is skipped.

The detector only inspects the callsite line itself, so `len([1, 2])` on a line that does
not run a query never fires. The `.count()` and `.exists()` names are Django ORM
spellings; for SQLAlchemy the hint is the same idea (`select(func.count())`,
`select(exists())`).

## Where the numbers come from

`rows` is the cursor's `rowcount` when the driver reports one, otherwise the number of rows
the ORM or driver has fetched so far (adapters update the span as rows are fetched, so a
lazily iterated queryset shows the rows actually read). When neither is known the span has
no `rows`, and these detectors skip it: they never guess.

The **Database** page ([Database](database.md)) shows mean rows per fingerprint across all
requests, which helps spot a query that returns a lot only occasionally.

## Settings

| Setting | Default |
|---|---|
| `large_result_rows` | `1000` |
| `unbounded_rows` | `200` |

See [Settings](settings.md#detectors).
