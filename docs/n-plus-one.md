# N+1 detection

[← Index](index.md)

An **N+1** is one query to fetch a list, then one more query per item to fetch something
related. It is invisible in development (ten rows, one millisecond each) and ruinous in
production. Redline finds them in every recorded request, groups the repeats, points at
the line that causes them, and, for Django and SQLAlchemy, writes the patch.

## The four detectors

The detectors run over each **kept** trace when it finishes. They are in
`redline_core.detect` and are framework-free; the fix suggesters are the adapters' part.

| Kind | Fires when | Default threshold | Setting |
|---|---|---|---|
| `n_plus_one` | the same read fingerprint comes from the same callsite at least *T* times | 5 | `n_plus_one_threshold` |
| `duplicate_query` | identical SQL **and** identical params run at least *T* times | 2 | `duplicate_threshold` |
| `slow_query` | a single query takes at least *T* ms | 100 | `slow_query_ms` |
| `too_many_queries` | the request ran at least *T* queries | 50 | `too_many_queries` |

### N+1: the rules in detail

- Only **read** queries count (`SELECT` and `WITH`, i.e. spans with `explainable: true`).
  Ten `INSERT`s in a loop are a different problem (use `bulk_create` or `executemany`)
  and are not flagged.
- Queries are grouped by `(fingerprint, callsite file:line)`. The same statement from two
  different lines is two groups, so two unrelated queries that happen to share a shape
  do not add up to a false positive.
- A group is an issue at `count >= n_plus_one_threshold`.
- The title names the primary table and the line:
  `N+1: 30 queries on shop_orderitem from shop/views.py:37`.

### Duplicates

Exactly the same SQL string with exactly the same parameters, run at least
`duplicate_threshold` times. Typical causes are a helper called twice, or a property that
re-queries. A duplicate group whose spans all belong to an N+1 is **not** reported again,
so you see one issue, not two.

### Slow queries

Grouped by fingerprint and callsite. The title shows the slowest run. The detail tells
you to open [EXPLAIN](explain.md). Note `slow_query_ms = 0` flags **every** query: it is
a threshold, not an off switch.

### Too many queries

One issue per **endpoint** (`GET /orders/<int:pk>/`), not per request, because it has no
fingerprint or callsite. Set `too_many_queries` to `0` to turn it off.

## Severity

*T* is the relevant threshold from the table above.

| Kind | `critical` | `warning` | `info` |
|---|---|---|---|
| `n_plus_one` | at least 4×*T* repeats, **or** the repeats together take at least `slow_query_ms` | otherwise | n/a |
| `duplicate_query` | n/a | at least 5 copies, or at least `slow_query_ms` in total | otherwise |
| `slow_query` | slowest run at least 5×`slow_query_ms` | otherwise | n/a |
| `too_many_queries` | at least 2×*T* queries | otherwise | n/a |

Issues in a trace are sorted by severity, then by total time.

## Issue identity

An issue id is `sha1("kind|fingerprint|file:line")[:16]`. The same problem at the same
line therefore gets the same id in every request, which is what lets the
[Issues page](issues-and-endpoints.md#issues) count occurrences. See that page for how
deduplication behaves when a line moves.

## How the fix is found

After the detectors produce an issue, **suggesters** run in registration order and the
first one to return something wins. Exceptions in a suggester are logged and skipped:
a broken suggester can never break a request. The framework-free
`GenericSuggester` always comes last and gives low-confidence advice in words.

Fix suggestion has two halves:

1. **Which relation?** Map the repeated SQL back to your models.
2. **Where is the queryset?** Read your source at the callsite, find the expression
   that builds the list being looped over, and edit it.

If both succeed you get a **unified diff**. If only the first does, you get a one-line
code hint at lower confidence.

=== "Django"

    The repeated statement is mapped through the app registry (table name to model):

    | Repeated query | Suggestion |
    |---|---|
    | `FROM child WHERE child.parent_id = ?` (reverse foreign key, `order.items.all()`) | `prefetch_related("items")` |
    | `FROM parent WHERE parent.id = ?` (forward FK or one-to-one, `item.product`) | `select_related("product")` |
    | reverse one-to-one (`customer.profile`) | `select_related("profile")` |
    | many-to-many, either side | `prefetch_related("tags")` or `prefetch_related("products")` |
    | nested loops (`for item in order.items.all(): item.product`) | `prefetch_related("items__product")` on the **outer** queryset |
    | `GenericRelation` | `prefetch_related(...)` |
    | `GenericForeignKey` | `prefetch_related(...)`, **low** confidence |
    | the relation is filtered per row (`order.items.filter(...)`) | a `Prefetch(..., queryset=..., to_attr=...)` hint, no diff |

    When several foreign keys point at the fetched model, the source text at the callsite
    (`item.product.name`) picks the right one.

    Then the source is read. In each project frame of the callsite stack, the loop
    variable on the offending line (`item` in `item.product`) is traced back through the
    enclosing `for` loops (across relation hops such as `for item in order.items.all()`)
    to the assignment that builds the root queryset (`Order.objects...`). Failing that,
    any queryset of the parent model in the frame's function or class is used. That covers:

    - loops in **templates** (`{% for item in order.items.all %}`): the diff goes on the
      queryset in the view's `render()` call;
    - **generic class-based views** (`model = Order`, `queryset = ...`,
      `get_queryset()`): the diff adds or edits the `queryset` attribute;
    - **DRF serializers** that read a relation: the diff targets the view's `queryset` or
      `get_queryset()`, because the middleware adds the view to the stack.

    The fix is inserted **before ordering and slicing**, **replaces** a bare `.all()`, or
    **extends** an existing `prefetch_related(...)` call. The `duplicate_query` issue kind
    also gets a suggestion when the duplicates are a forward relation re-read per object.

    A real diff from the demo shop (`/orders/`):

    ```diff
    --- a/shop/views.py
    +++ b/shop/views.py
    @@ -32,5 +32,5 @@
     def order_list(request):
         """N+1: one query for the orders, then one per order for its items."""
    -    orders = Order.objects.select_related("customer").order_by("-id")[:30]
    +    orders = Order.objects.select_related("customer").prefetch_related("items").order_by("-id")[:30]
         data = []
         for order in orders:
    ```

    The suggestion text that goes with it: *Add .prefetch_related("items") to the Order
    queryset (shop/views.py:34): one extra query for all Order rows instead of one per
    row.* Note how it extended the existing `select_related("customer")` chain rather than
    inventing a new statement.

=== "SQLAlchemy"

    The repeated statement is parsed into `(table, column)` equality filters and matched
    against the ORM relationships of your mapped classes (`registry.mappers`):

    | Repeated query | Suggestion |
    |---|---|
    | child table filtered by its foreign key column | `selectinload(Parent.children)` |
    | parent looked up by primary key, once per row | `joinedload(Child.parent)` |
    | association (secondary) table filtered by a parent column | `selectinload(Parent.tags)` |

    The suggester then walks the callsite's project frames looking for the
    `select(Parent)` or `session.query(Parent)` expression that builds the list, and
    appends `.options(...)` at the end of the chain, plus the import
    (`from sqlalchemy.orm import selectinload`) when it is missing. Both lazy-load forms
    (`order.items`) and explicit per-row queries (`select(OrderItem).where(OrderItem.order_id
    == order.id)`) are recognised.

    The demo's `GET /orders`:

    ```diff
    --- a/shop/api.py
    +++ b/shop/api.py
    -        orders = (await session.scalars(select(Order).order_by(Order.id).limit(30))).all()
    +        orders = (await session.scalars(select(Order).order_by(Order.id).limit(30).options(selectinload(Order.items)))).all()
    ```

    (The real output also contains the import line and context lines.) The diff is
    mechanical: it appends to the end of the chain, so you may want to reformat.

    SQLModel needs nothing extra: its tables live in a normal SQLAlchemy registry. If the
    suggester cannot find your models, pass `install(app, models=Base)`.

    Only `n_plus_one` issues get SQLAlchemy suggestions in this version. Duplicates, slow
    queries and query budgets get the generic words.

## Confidence

Every suggestion carries one of three levels. The UI shows it as a pill.

| Level | Meaning | Examples |
|---|---|---|
| **high** | The relation was identified **and** the queryset was located; a diff was produced | Django: one unambiguous relation, a path of at most two hops, an exact queryset match. SQLAlchemy: any suggestion that produced a diff |
| **medium** | The relation is right but you must place it, or the match was less exact | a code hint without a diff; a path of more than two hops; several candidate relations; a queryset that already has the prefetch (so something *bypasses* it); a filtered relation (`Prefetch` hint) |
| **low** | A guess, or generic advice | `GenericForeignKey`; an ambiguous hint; the framework-free `GenericSuggester` |

!!! tip "Treat a diff as a starting point"

    `high` confidence means Redline is confident about the *relation and the line*, not
    that the change is right for your code. Eager loading changes memory use (a prefetch
    loads every related row), can change query count in other paths that share the
    queryset, and is wrong when you only need a count (use `annotate(Count(...))`).
    Read the diff, apply it, **re-run the request, and check the issue is gone**.

## When the suggester cannot help

- The queryset is built in another module, dynamically, or through a helper the static
  walk does not follow: you get the hint without a diff, with lower confidence.
- The callsite is in library code and the view cannot be identified. Check
  `project_root` (see [How it works](how-it-works.md#callsites)).
- The relation is one the suggester does not model (custom managers that rewrite the
  queryset, raw SQL, `.extra()`): you get the generic advice.
- The source file changed since the trace was recorded: the diff is computed against the
  **current** file on disk, at the line recorded then. Re-record after editing.

## Tuning

| Situation | What to change |
|---|---|
| Too many N+1 reports for tiny loops | raise `n_plus_one_threshold` (it must be at least 2) |
| A legitimate repeated query (a cache warm-up) | none to ignore one issue; lower noise with `ignore_paths` for the endpoint |
| You want a stricter query budget in tests | there is no pytest plugin yet; it is planned for [v0.3](roadmap.md) |

## Write your own suggester

For another ORM, or a house style, implement the `FixSuggester` protocol. See
[Extending](extending.md#fixsuggester).
