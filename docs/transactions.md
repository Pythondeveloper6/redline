# Transactions and locks

[← Index](index.md)

A database transaction holds a connection from the pool and, once it has written
anything, row locks that other requests can queue behind. The longer it stays open, the
worse both get. Redline records every transaction as a span and reports the two patterns
that hurt most.

## The `transaction` span

| Adapter | Where the span comes from |
|---|---|
| Django | `transaction.atomic` (the decorator, the context manager and `ATOMIC_REQUESTS`). The first atomic block entered while a trace is active opens the span; nested blocks (savepoints) only bump a depth counter |
| SQLAlchemy (FastAPI, Flask, Litestar) | the engine's begin, commit and rollback events |

The span's data:

| Key | Meaning |
|---|---|
| `db_alias` | which database |
| `outcome` | `commit` or `rollback` |
| `queries` | statements run inside |
| `http_out` | outbound HTTP calls made while the transaction was open |

Empty transactions and control statements such as `SAVEPOINT` are not counted, so a
read-only request with `ATOMIC_REQUESTS` does not fill the timeline with noise. Queries
inside the transaction carry `in_transaction: true` in their span data.

## Long transactions

`long_transaction` fires when a transaction span lasts at least `long_transaction_ms`
(default 200). `critical` at five times that.

```text
Transaction open for 1,840 ms (12 queries, rolled back)   shop/services.py:88
```

Typical causes, in order of how often they show up:

1. **Work that does not need the transaction** inside it: rendering, serializing, sending
   an email, hashing a password.
2. **An outbound call** (see below).
3. **A loop of writes** that should be a bulk operation.
4. **Waiting on a lock** another transaction holds. The queries are fast; the time is spent
   waiting.

Fix by narrowing the `atomic` block (or the `with session.begin()`) to the statements that
must be atomic, and doing everything else before or after it.

## External call inside a transaction

`external_in_transaction` fires when a `transaction` span has `http_out > 0`:

```text
HTTP call to api.payments.example while a transaction was open   shop/checkout.py:41
```

This is worse than it looks. A slow or failing third party now holds your connection and
your locks, and a rollback cannot undo the call: you may have charged a card for an order
that no longer exists. The standard fix is to commit first and call afterwards, or to
record the intent in the transaction and let a background job make the call (the
*transactional outbox* pattern). Use `transaction.on_commit` in Django.

```python
# Before: the charge happens inside the transaction
with transaction.atomic():
    order = Order.objects.create(...)
    gateway.charge(order)               # http_out inside a transaction

# After: charge only once the order is safely committed
with transaction.atomic():
    order = Order.objects.create(...)
    transaction.on_commit(lambda: gateway.charge(order))
```

## Pool waits

When SQLAlchemy checks a connection out of its pool and has to wait, Redline records a
`pool_wait` span if the wait is 1 ms or more. The data says which database, the pool size
and how many connections were already checked out:

```text
pool_wait  db_alias=default  pool_size=5  checked_out=5   waited 340 ms
```

A request that spends its time here is not slow because of its own queries. The pool is
exhausted: other requests hold connections too long (often *because* of a long transaction
or an external call inside one), or the pool is simply too small for the concurrency. Look
at the `long_transaction` issues first. In a [load test](load-testing.md), pool waits are
the typical shape of a knee: latency climbs while each individual query stays fast.

## Lock waits

Redline does not read `pg_locks`. A lock wait appears as a slow query or a long
transaction whose queries are individually trivial. The tell is a statement that is fast
under `EXPLAIN` (see [EXPLAIN](explain.md)) but slow in the trace, concentrated on the same
table or row from several requests. Reading `pg_locks` is on the [roadmap](roadmap.md).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `long_transaction_ms` | `200` | Minimum open time reported as `long_transaction` |

There is no setting for `external_in_transaction`: any outbound call inside a transaction
fires it. To see outbound calls at all you must instrument the client
([Integrations](integrations.md)).
