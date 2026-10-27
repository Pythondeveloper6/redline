"""Flask hooks: route template, view span, template spans and unhandled errors.

Installed once per app from ``Redline.init_app`` (not at all when ``enabled=False``). Each
hook is a cheap no-op when no trace is active – one ``ContextVar.get()`` – and they write
into the trace the WSGI middleware started, found through the contextvar:

* ``request_started`` – the route template (``/orders/<int:order_id>``) and the view's
  dotted name, known as soon as Flask has matched the URL;
* ``app.dispatch_request`` – wrapped in a ``view`` span (only the view call, not the
  ``before_request`` / ``after_request`` hooks), also for ``async def`` views;
* ``before_render_template`` / ``template_rendered`` – a ``template`` span per top-level
  render; queries a template triggers (lazy loading) nest under it;
* ``app.async_to_sync`` – every ``async def`` view/hook runs inside
  :func:`redline_core.blocking.track_request` on the event loop asgiref creates for it, so
  a blocking call in a coroutine (``time.sleep``, a sync driver) becomes a ``blocking``
  span;
* ``got_request_exception`` – the unhandled exception for the trace's ``error``. Flask
  turns ``HTTPException`` (``abort(404)``) into a response without this signal, so a 4xx is
  never an error.
"""

from __future__ import annotations

import contextvars
import functools
from typing import Any

from flask import Flask, got_request_exception, request, request_started
from flask.signals import before_render_template, template_rendered

from redline_core.blocking import track_request
from redline_core.recorder import SpanContext, current

from ._middleware import ERROR_KEY, PASSTHROUGH_KEY

__all__ = ["install_hooks", "view_name"]

_open_templates: contextvars.ContextVar[list[SpanContext] | None] = contextvars.ContextVar(
    "redline_flask_templates", default=None
)


def view_name(app: Flask, endpoint: str) -> str | None:
    """Dotted name of the view function behind ``endpoint`` (``shop.views.order_detail``)."""
    func = app.view_functions.get(endpoint)
    if func is None:
        return None
    target = getattr(func, "view_class", None) or getattr(func, "func", None) or func
    module = getattr(target, "__module__", None)
    name = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    if name is None:
        name, module = type(target).__qualname__, type(target).__module__
    return f"{module}.{name}" if module else name


def install_hooks(app: Flask, on_first_request: Any = None) -> None:
    """Connect the signal receivers and wrap ``dispatch_request`` (idempotent)."""
    if getattr(app, "_redline_hooks", False):
        return
    app._redline_hooks = True  # type: ignore[attr-defined]

    # blinker holds receivers weakly by default; these closures must outlive this call.
    def on_request_started(sender: Flask, **extra: Any) -> None:
        _open_templates.set(None)  # nothing can be mid-render at the start of a request
        if on_first_request is not None:
            on_first_request()
        builder = current()
        rule = request.url_rule
        if builder is None or rule is None:
            return
        builder.set_route(request.script_root + rule.rule, view_name(sender, rule.endpoint))

    def on_exception(sender: Flask, exception: BaseException, **extra: Any) -> None:
        request.environ[ERROR_KEY] = exception
        for ctx in reversed(_open_templates.get() or []):  # a render that raised
            ctx.__exit__(type(exception), exception, None)
        _open_templates.set(None)

    def before_render(sender: Flask, template: Any, **extra: Any) -> None:
        if current() is None:
            return
        name = template.name or "<string>"
        ctx = SpanContext(name, "template", {"template": name})
        ctx.__enter__()
        stack = _open_templates.get()
        if stack is None:
            stack = []
            _open_templates.set(stack)
        stack.append(ctx)

    def after_render(sender: Flask, template: Any, **extra: Any) -> None:
        stack = _open_templates.get()
        if stack:
            stack.pop().__exit__(None, None, None)

    def after_request(response: Any) -> Any:
        # A file served by the WSGI server must come back from the middleware untouched.
        if response.direct_passthrough:
            request.environ[PASSTHROUGH_KEY] = True
        return response

    request_started.connect(on_request_started, app, weak=False)
    got_request_exception.connect(on_exception, app, weak=False)
    before_render_template.connect(before_render, app, weak=False)
    template_rendered.connect(after_render, app, weak=False)
    app.after_request(after_request)

    original = app.dispatch_request

    def dispatch_request() -> Any:
        rule = request.url_rule
        if rule is None or current() is None:
            return original()
        name = view_name(app, rule.endpoint) or rule.endpoint
        with SpanContext(name, "view", {"endpoint": rule.endpoint}):
            return original()

    app.dispatch_request = dispatch_request  # type: ignore[method-assign]

    to_sync = app.async_to_sync

    def async_to_sync(func: Any) -> Any:
        @functools.wraps(func)
        async def tracked(*args: Any, **kwargs: Any) -> Any:
            builder = current()  # asgiref copies the context into its loop thread
            if builder is None:
                return await func(*args, **kwargs)
            async with track_request(builder):  # binds the loop this coroutine runs on
                return await func(*args, **kwargs)

        return to_sync(tracked)

    app.async_to_sync = async_to_sync  # type: ignore[method-assign]
