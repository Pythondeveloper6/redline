# Django Ninja

[← Index](index.md)

[Django Ninja](https://django-ninja.dev) apps are served by `django-redline`; there is no
separate package. Install and mount Redline exactly as in [Django](django.md), and your
Ninja endpoints are recorded like any other Django view, with two Ninja-specific
improvements.

## What you get

Ninja registers **one Django view per path** and dispatches to the operation inside it.
Left alone, every Ninja request would look like `ninja.operation.view_wrapper` with a route
template that is the same for every HTTP method. Redline looks inside:

| Trace field | Without the Ninja support | With it |
|---|---|---|
| `view` | `ninja.operation.view_wrapper` | `shop.api.list_orders`, your endpoint function |
| `route` | the Django URL pattern for the path | the same, resolved per operation, so `GET /api/orders/{order_id}` and `DELETE /api/orders/{order_id}` aggregate separately on the [Endpoints](issues-and-endpoints.md#endpoints) page |

Nothing is imported unless Ninja is already loaded in the process, so non-Ninja projects
pay nothing.

## Setup

```python
# settings.py
INSTALLED_APPS = [..., "django_redline"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django_redline.middleware.RedlineMiddleware",
    ...,
]
```

```python
# urls.py
from django.urls import include, path
from ninja import NinjaAPI

api = NinjaAPI()

urlpatterns = [
    path("api/", api.urls),
    path("redline/", include("django_redline.urls")),
]
```

Your Ninja routes do not change. Sync and `async def` operations are both supported; the
async rules from [Django: async views](django.md#async-views) apply.

## Schemas and serialization

Ninja serializes with Pydantic. The `serializer` spans come from [DRF](serializers.md)
and from [FastAPI](fastapi.md); Ninja's response building is not hooked separately, so a slow
one shows up as time in the `view` span. Wrap
the suspect code in a [custom span](custom-spans.md) if you need to isolate it.

## Load testing a Ninja API

Ninja publishes an OpenAPI document, so scenarios can be generated from it:

```bash
redline load init --from-openapi http://localhost:8000/api/openapi.json -o scenario.toml
redline load scenario.toml --steps 10:100:10 --step-duration 20s
```

See [Load testing](load-testing.md#generating-scenarios).

## N+1 in Ninja endpoints

The Django fix suggester works unchanged, because Ninja endpoints use the Django ORM: an
N+1 inside `list_orders` is reported with a `select_related` or `prefetch_related` diff at
the queryset line. See [N+1 detection](n-plus-one.md).
