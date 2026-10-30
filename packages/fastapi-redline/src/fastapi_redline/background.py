"""Explicit tracing for FastAPI and Starlette in-process background tasks."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette._utils import is_async_callable
from starlette.background import BackgroundTasks

from redline_core.jobs import job, propagation_headers
from redline_core.recorder import current, get_recorder


def add_background_task(
    background_tasks: BackgroundTasks,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    """Schedule ``func`` and record its execution as a separate job trace.

    Only the function's fixed name and safe correlation metadata are recorded. Arguments
    and return values are passed through to the application function and are not added
    to the job recording.
    """
    builder = current()
    recorder = builder.recorder if builder is not None else get_recorder()
    parent = propagation_headers() if builder is not None else None
    name = getattr(
        func,
        "__qualname__",
        getattr(func, "__name__", type(func).__qualname__),
    )

    if is_async_callable(func):

        async def run_async() -> Any:
            with job(
                name,
                recorder=recorder,
                parent=parent,
                capture_error_details=False,
            ):
                return await func(*args, **kwargs)

        background_tasks.add_task(run_async)
    else:

        def run_sync() -> Any:
            with job(
                name,
                recorder=recorder,
                parent=parent,
                capture_error_details=False,
            ):
                return func(*args, **kwargs)

        # Starlette recognizes this as sync and dispatches it through its thread pool.
        background_tasks.add_task(run_sync)
