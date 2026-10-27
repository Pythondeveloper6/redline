# Static N+1 scan

[← Index](index.md)

Runtime detection finds an N+1 only after something has run it, and only on a path your
test traffic reached. The **static scan** reads your templates (and, for Django, your
serializers) *without running anything*, and flags the patterns that are almost always an
N+1. Use it in CI to catch the problem before it ships.

```bash
python manage.py redline_scan          # Django
flask redline scan                     # Flask
```

## What it finds

### Template loops

A loop whose body reads a relation off the loop variable:

```django
{% for order in orders %}
  <tr>
    <td>{{ order.customer.name }}</td>        {# one query per order, unless prefetched #}
    <td>{{ order.items.count }}</td>          {# a COUNT per order #}
    <td>{% for i in order.items.all %}...{% endfor %}</td>
  </tr>
{% endfor %}
```

Each of `order.customer.name`, `order.items.count` and `order.items.all` costs a query per
iteration unless the view fetched the relation up front with `select_related` or
`prefetch_related`.

The scanner understands Django and Jinja syntax: comments (`{# #}` and
`{% comment %}`), `{% verbatim %}` and `{% raw %}`, nested loops, `{% with %}` and
`{% set %}` aliases, filters (`order.customer.name|upper`) and Jinja subscripts
(`order['customer'].name`). A template it cannot make sense of is **skipped with a
warning**, never an exception. It is dependency-free and best-effort.

### DRF serializers (Django)

A `ModelSerializer` with a nested relation serializer, used by a view whose queryset lacks
the matching `select_related` or `prefetch_related`. The check is on by default and skipped
with `--no-drf`.

## Fewer false positives in Django

Without help, any `x.a.b` in a loop could be a plain attribute. In Django the scanner asks
the app registry whether the **first hop is a real relation** (a foreign key, one-to-one,
many-to-many or reverse relation) and keeps only those chains. It also uses the relation's
kind to choose the right fix: `select_related` for a forward foreign key, `prefetch_related`
for a reverse or many-to-many relation. Flask has no model registry, so the Jinja dialect
cannot check that a hop is a relation and you triage its findings by hand.

## Running it

=== "Django"

    ```bash
    python manage.py redline_scan                    # every template directory
    python manage.py redline_scan templates/shop/    # specific files or directories
    python manage.py redline_scan --json             # machine-readable
    python manage.py redline_scan --fail             # exit 1 if anything is found
    python manage.py redline_scan --dialect jinja templates/jinja/
    python manage.py redline_scan --no-drf           # skip the serializer check
    ```

=== "Flask"

    ```bash
    flask redline scan [PATHS...] [--json] [--fail]
    ```

    <!-- verify: flask redline scan -->
    Runs the Jinja dialect over your template folders. Output and exit codes are the same.

Text output is an aligned table:

```text
LOCATION                      KIND      SEVERITY  CHAIN                SUGGESTION
shop/orders/list.html:14      template  warning   order.customer.name  select_related("customer")
shop/orders/list.html:17      template  warning   order.items.all      prefetch_related("items")
shop/api/serializers.py:22    serializer warning  items (nested)       prefetch_related("items") on OrderViewSet.queryset

3 possible N+1 finding(s).
```

`--json` prints `{"findings": [...], "warnings": [...]}` where each finding is:

```json
{
  "file": "shop/orders/list.html",
  "line": 14,
  "loop_var": "order",
  "chain": "order.customer.name",
  "kind": "template",
  "severity": "warning",
  "message": "...",
  "suggestion": "select_related(\"customer\")"
}
```

## In CI

```yaml
- name: Static N+1 scan
  run: python manage.py redline_scan --fail
```

`--fail` exits with status 1 when anything is found. Start by running it without `--fail`
to see the baseline, fix or accept what it finds, then turn it on so a new loop with a
relation inside cannot slip in.

## Limits

- It is **static**. It cannot know that the view already prefetched `customer`, so a
  finding is "check this", not "this is slow". The runtime detector is the confirmation.
- It reads templates and DRF serializers. Python code that does `for o in orders:
  o.customer.name` is what the runtime detector and its fix diff are for.
